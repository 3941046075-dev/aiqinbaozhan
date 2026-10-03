#!/usr/bin/env python3
"""Generate a sourced Chinese AI digest and publish it once to Telegram."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deep_report import enrich_schema, editorial_prompt, validate_editorial, brief_post, build_pdf


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
OPENAI_API_URL = "https://api.openai.com/v1/responses"
TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_MODEL = "gpt-5.6-luna"
STATE_PATH = "state/publish-state.json"
MAX_RETRIES = 3
MAX_GENERATION_ATTEMPTS = 2


def configure_stdio() -> None:
    """Keep Chinese text and emoji readable on Windows and in CI logs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


configure_stdio()


class AppError(RuntimeError):
    """An expected, safely reportable application error."""


@dataclass(frozen=True)
class RuntimeConfig:
    openai_api_key: str
    telegram_bot_token: str
    telegram_channel: str
    dry_run: bool
    model: str
    github_token: str
    github_repository: str


def parse_bool(value: str | None, *, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AppError("DRY_RUN 必须是 true 或 false。")


def load_config(env: dict[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if env is None else env
    dry_run = parse_bool(values.get("DRY_RUN"), default=True)
    config = RuntimeConfig(
        openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_channel=values.get("TELEGRAM_CHANNEL", "").strip(),
        dry_run=dry_run,
        model=values.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        github_token=values.get("GITHUB_TOKEN", "").strip(),
        github_repository=values.get("GITHUB_REPOSITORY", "").strip(),
    )
    if not config.openai_api_key:
        raise AppError("缺少 OPENAI_API_KEY。请在 GitHub Actions Secret 中配置。")
    if not dry_run:
        missing = []
        if not config.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not config.telegram_channel:
            missing.append("TELEGRAM_CHANNEL")
        if not config.github_token:
            missing.append("GITHUB_TOKEN（由 GitHub Actions 自动提供）")
        if not config.github_repository:
            missing.append("GITHUB_REPOSITORY（由 GitHub Actions 自动提供）")
        if missing:
            raise AppError("真实发布模式缺少配置：" + "、".join(missing))
    return config


def utf16_length(text: str) -> int:
    """Telegram measures message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def json_schema() -> dict[str, Any]:
    news_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "what_happened": {"type": "string"},
            "why_important": {"type": "string"},
            "published_at": {"type": "string"},
            "source_name": {"type": "string"},
            "source_url": {"type": "string"},
        },
        "required": [
            "headline",
            "what_happened",
            "why_important",
            "published_at",
            "source_name",
            "source_url",
        ],
    }
    return enrich_schema({
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {"type": "array", "items": news_item},
            "watch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "thing": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["thing", "reason"],
            },
        },
        "required": ["items", "watch"],
    })


def build_prompt(now: datetime) -> str:
    window_start = now - timedelta(hours=24)
    start_text = window_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_text = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    china_date = now.astimezone(CHINA_TZ).strftime("%Y-%m-%d")
    return editorial_prompt() + f"""
你是中文科技频道“AI情报站”的严谨主编。请先使用网页搜索，再筛选新闻。

时间窗口：{start_text} 至 {end_text}（UTC）；中国日期：{china_date}。

任务：只选出时间窗口内真正重要、可信且适合公开发布的 1～8 条不同 AI 新闻；少而精，只有1～3条也可发布。范围仅限：
OpenAI、Anthropic、Google、Meta、AI 模型、Agent、AI 工具、芯片/算力、融资/商业化、
AI 创业机会、重要政策。

硬性事实规则：
1. 每条都必须有可直接打开的权威来源 URL 和明确发布时间；published_at 必须是带时区的 ISO 8601。
2. 优先公司官网、政府文件、论文原文等一手来源；重大商业新闻可用 Reuters、AP、Bloomberg、
   Financial Times、Wall Street Journal 等可靠媒体。
3. 必须确认 published_at 落在上述 24 小时窗口内。旧闻、汇总文、观点文、传闻、泄露、
   无法确认发布时间或无法交叉核实的内容一律不写。
4. 不得编造新闻、数字、人物、产品更新或 URL；不要把推测写成事实。
5. 用自己的中文概括，不照抄标题或正文。语言专业、简洁、高信息密度，不夸张、不标题党。
6. what_happened 回答“发生了什么”；why_important 回答“为什么重要”，避免空话和重复。
7. source_url 必须逐字复制你实际搜索并阅读过的那篇来源页面 URL。
8. 如果严格筛选后只有1～3条，返回实际条目，不用低质量内容凑数；一条都没有就返回空列表，
   程序会停止发布。数量减少不能放宽来源、时间、真实性与重要性标准。
9. “今日值得关注的一件事”应从入选新闻提炼一个最值得继续跟踪的具体事项，不新增未经来源支持的事实。

仅按给定 JSON Schema 返回，不要输出 Markdown 或额外说明。
""".strip()


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 120,
    retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "aiqinbaozhan/1.0"}
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=encoded, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise AppError("远程接口返回了非对象 JSON。")
                return parsed
        except urllib.error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace")[:1200]
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                time.sleep(2 ** (attempt - 1))
                continue
            detail = extract_remote_error(response_text)
            raise AppError(f"远程接口 HTTP {exc.code}：{detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
                continue
            raise AppError(f"远程接口网络错误：{exc.reason if hasattr(exc, 'reason') else exc}") from None
        except json.JSONDecodeError:
            raise AppError("远程接口没有返回有效 JSON。") from None
    raise AppError("远程接口请求失败。")


def extract_remote_error(text: str) -> str:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:500]
            if isinstance(payload.get("description"), str):
                return payload["description"][:500]
    except json.JSONDecodeError:
        pass
    return "请求失败，远程服务未提供可用错误说明。"


def telegram_retry_after(text: str) -> int | None:
    """Only retry an explicit flood-control rejection, never an ambiguous send."""
    try:
        payload = json.loads(text)
        delay = payload.get("parameters", {}).get("retry_after")
        if (payload.get("ok") is False and payload.get("error_code") == 429
                and type(delay) is int and 1 <= delay <= 60):
            return delay
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def telegram_send_request(request, *, timeout=120):
    for attempt in range(3):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")[:1200]
            delay = telegram_retry_after(text) if exc.code == 429 else None
            if delay is not None and attempt < 2:
                print(f"Telegram明确限流拒绝：等待{delay}秒后重试（{attempt + 1}/2）。")
                time.sleep(delay)
                continue
            # Restore the body for the caller's existing safe error handling.
            import io
            exc.fp = io.BytesIO(text.encode("utf-8"))
            raise


def generate_digest(config: RuntimeConfig, now: datetime) -> dict[str, Any]:
    previous_count: int | None = None
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        prompt = build_prompt(now)
        if previous_count is not None:
            prompt += f"""

这是第 {attempt} 次独立检索。上一次严格筛选后只有 {previous_count} 条合格新闻。
请重新覆盖所有指定主题，重点检查公司官方 newsroom、产品博客、更新日志、政府文件和可靠商业媒体，
寻找可能遗漏的同一 24 小时窗口内事件。不得放宽时间、可信度或重要性标准，也不得为凑数而编造。
""".rstrip()

        response = request_digest_response(config, prompt)
        digest, searched_urls = parse_digest_response(response)
        try:
            validate_digest(digest, now, searched_urls=searched_urls)
        except AppError:
            count = digest_item_count(digest)
            if count is not None and attempt < MAX_GENERATION_ATTEMPTS:
                previous_count = count
                print(f"首次内容未通过事实或双层排版校验（{count} 条）；重新检索一次，不放宽标准。")
                continue
            raise
        return digest
    raise AppError("内容生成重试结束，但仍没有得到合格的双层新闻数据。")


def request_digest_response(config: RuntimeConfig, prompt: str) -> dict[str, Any]:
    payload = {
        "model": config.model,
            "instructions": "严格执行事实核查、时间窗口和 JSON 输出要求。",
        "input": prompt,
        "tools": [
            {
                "type": "web_search_preview",
                "search_context_size": "high",
                "user_location": {
                    "type": "approximate",
                    "country": "CN",
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "reasoning": {"effort": "medium"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_ai_brief",
                "strict": True,
                "schema": json_schema(),
            }
        },
        "max_output_tokens": 14000,
        "store": False,
    }
    response = request_json(
        OPENAI_API_URL,
        method="POST",
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        body=payload,
    )
    return response


def parse_digest_response(response: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    if response.get("status") != "completed":
        detail = response.get("error") or response.get("incomplete_details") or response.get("status")
        raise AppError(f"OpenAI 内容生成未完成：{detail}")
    output = response.get("output")
    if not isinstance(output, list) or not any(
        isinstance(item, dict) and item.get("type") == "web_search_call" for item in output
    ):
        raise AppError("OpenAI 响应中没有网页搜索记录；为避免无来源内容，停止发布。")
    text = extract_output_text(response)
    try:
        digest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"OpenAI 输出不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列。") from None
    searched_urls = extract_search_source_urls(response)
    if not searched_urls:
        raise AppError("网页搜索没有返回可核验的来源 URL；为避免无来源内容，停止发布。")
    return digest, searched_urls


def digest_item_count(digest: Any) -> int | None:
    if not isinstance(digest, dict):
        return None
    items = digest.get("items")
    return len(items) if isinstance(items, list) else None


def extract_output_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    chunks.append(value)
    if not chunks:
        raise AppError("OpenAI 响应中没有可用文本。")
    return "".join(chunks)


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    # Search providers and publishers commonly add or remove tracking query
    # parameters. Article URLs are identified by host + path; root/query-only
    # URLs keep their query because it may contain the actual article id.
    query = parsed.query if path == "/" else ""
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host + port, path, query, ""))


def canonical_host(value: str) -> str:
    host = (urllib.parse.urlsplit(value.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def extract_search_source_urls(response: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        sources = action.get("sources", []) if isinstance(action, dict) else []
        for source in sources:
            url = source.get("url") if isinstance(source, dict) else None
            if isinstance(url, str):
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    urls.add(canonical_url(url))
    return urls


def parse_published_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AppError("新闻缺少 published_at。")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise AppError(f"新闻发布时间不是有效 ISO 8601：{value}") from None
    if parsed.tzinfo is None:
        raise AppError(f"新闻发布时间缺少时区：{value}")
    return parsed


def validate_digest(
    digest: Any,
    now: datetime,
    *,
    searched_urls: set[str] | None = None,
) -> None:
    if not isinstance(digest, dict):
        raise AppError("生成结果不是 JSON 对象。")
    items = digest.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        actual = len(items) if isinstance(items, list) else 0
        raise AppError(f"严格筛选后得到 {actual} 条新闻，不满足 1～8 条深度新闻要求，停止发布。")
    window_start = now - timedelta(hours=24)
    seen_urls: set[str] = set()
    searched_hosts = {canonical_host(url) for url in searched_urls or set()}
    required_text_fields = ("headline", "what_happened", "why_important", "source_name")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise AppError(f"第 {index} 条新闻结构无效。")
        for field in required_text_fields:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AppError(f"第 {index} 条新闻缺少字段 {field}。")
        published_at = parse_published_at(item.get("published_at")).astimezone(timezone.utc)
        if published_at < window_start.astimezone(timezone.utc) or published_at > now.astimezone(timezone.utc):
            raise AppError(f"第 {index} 条新闻发布时间不在过去 24 小时内，停止发布。")
        source_url = item.get("source_url")
        if not isinstance(source_url, str):
            raise AppError(f"第 {index} 条新闻缺少来源 URL。")
        parsed_url = urllib.parse.urlparse(source_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise AppError(f"第 {index} 条新闻的来源 URL 无效。")
        normalized_url = source_url.strip().rstrip("/")
        if normalized_url in seen_urls:
            raise AppError(f"第 {index} 条新闻与其他条目使用了重复来源 URL。")
        seen_urls.add(normalized_url)
        if searched_urls is not None:
            exact_match = canonical_url(source_url) in searched_urls
            host_match = canonical_host(source_url) in searched_hosts
            if not exact_match and not host_match:
                raise AppError(f"第 {index} 条新闻的来源站点不在本次网页搜索结果中，停止发布。")
    watch = digest.get("watch")
    if not isinstance(watch, dict) or not all(
        isinstance(watch.get(field), str) and watch[field].strip() for field in ("thing", "reason")
    ):
        raise AppError("生成结果缺少“今日值得关注的一件事”。")
    try:
        validate_editorial(digest)
    except ValueError as exc:
        raise AppError(str(exc)) from None


def clean_text(value: Any) -> str:
    text = str(value).strip()
    return re.sub(r"\s+", " ", text)


def format_post(digest: dict[str, Any], now: datetime) -> str:
    try:
        return brief_post(digest, now.astimezone(CHINA_TZ))
    except ValueError as exc:
        raise AppError(str(exc)) from None


def legacy_format_post(digest: dict[str, Any], now: datetime) -> str:
    local = now.astimezone(CHINA_TZ)
    lines = [f"🔥 今日 AI 情报｜{local.year}年{local.month}月{local.day}日", ""]
    for index, item in enumerate(digest["items"], start=1):
        lines.extend(
            [
                f"{index}. {clean_text(item['headline'])}",
                f"发生了什么：{clean_text(item['what_happened'])}",
                f"为什么重要：{clean_text(item['why_important'])}",
                f"来源：{clean_text(item['source_name'])} {item['source_url'].strip()}",
                "",
            ]
        )
    watch = digest["watch"]
    lines.extend(
        [
            "今日值得关注的一件事",
            clean_text(watch["thing"]),
            f"关注理由：{clean_text(watch['reason'])}",
        ]
    )
    post = "\n".join(lines).strip()
    length = utf16_length(post)
    if length > TELEGRAM_TEXT_LIMIT:
        raise AppError(f"生成帖子长度为 {length}，超过 Telegram 的 {TELEGRAM_TEXT_LIMIT} 字限制。")
    return post


class GitHubStateStore:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository
        self.api_base = f"https://api.github.com/repos/{repository}"
        self.branch = self._get_default_branch()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_default_branch(self) -> str:
        data = request_json(self.api_base, headers=self.headers)
        branch = data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise AppError("无法读取 GitHub 仓库默认分支。")
        return branch

    def read(self) -> tuple[dict[str, Any], str]:
        query = urllib.parse.urlencode({"ref": self.branch})
        data = request_json(f"{self.api_base}/contents/{STATE_PATH}?{query}", headers=self.headers)
        encoded = data.get("content")
        sha = data.get("sha")
        if not isinstance(encoded, str) or not isinstance(sha, str):
            raise AppError("GitHub 发布状态文件结构无效。")
        try:
            state = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("GitHub 发布状态文件无法解析。") from None
        if not isinstance(state, dict):
            raise AppError("GitHub 发布状态不是 JSON 对象。")
        return state, sha

    def write(self, state: dict[str, Any], sha: str, message: str) -> str:
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
            "branch": self.branch,
        }
        data = request_json(
            f"{self.api_base}/contents/{STATE_PATH}",
            method="PUT",
            headers=self.headers,
            body=body,
        )
        result = data.get("content")
        new_sha = result.get("sha") if isinstance(result, dict) else None
        if not isinstance(new_sha, str):
            raise AppError("GitHub 没有确认发布状态更新。")
        return new_sha


def publish_to_telegram(token: str, channel: str, post: str) -> int:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": channel,
        "text": post,
        "disable_web_page_preview": True,
    }
    # Retry only explicit flood-control rejection; a transport timeout is ambiguous.
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with telegram_send_request(request) as reply:
            response = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AppError(f"Telegram主帖 HTTP {exc.code}：{extract_remote_error(exc.read().decode('utf-8', errors='replace'))}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise AppError("Telegram主帖网络或响应异常，发送结果未知，禁止自动重发。") from None
    if response.get("ok") is not True:
        description = response.get("description", "Telegram 未确认发送成功。")
        raise AppError(f"Telegram 发送失败：{description}")
    result = response.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int):
        raise AppError("Telegram 返回成功但没有 message_id；按未知发送状态处理。")
    return message_id


def is_claimed_for_today(state: dict[str, Any], date_key: str) -> bool:
    return state.get("date") == date_key and state.get("status") in {"sending", "sending_main", "sending_document", "published"}


class TelegramRejectedError(AppError):
    """Telegram explicitly rejected a document; safe to retry the document only."""


def publish_document(token: str, channel: str, path: Path, main_id: int, date_key: str) -> int:
    boundary = "AIReport" + uuid.uuid4().hex
    fields = {"chat_id": channel, "caption": f"📖 AI情报站｜{date_key} 全球 AI 深度情报\n上方精简主帖的完整版：背景、影响、创业机会与可靠来源。",
              "reply_parameters": json.dumps({"message_id": main_id, "allow_sending_without_reply": False})}
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="aiqinbaozhan-{date_key}-deep-report.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode(), path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(chunks),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    # Same flood-control policy as the main post; no ambiguous transport retry.
    try:
        with telegram_send_request(request) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = extract_remote_error(exc.read().decode("utf-8", errors="replace")[:1200])
        error_type = TelegramRejectedError if 400 <= exc.code < 500 else AppError
        raise error_type(f"Telegram PDF HTTP {exc.code}：{detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise AppError("Telegram PDF 网络或响应异常，上传结果未知，禁止自动重发。") from None
    if result.get("ok") is not True:
        raise TelegramRejectedError(f"Telegram 拒绝 PDF：{result.get('description', '未提供说明')}")
    message_id = result.get("result", {}).get("message_id")
    if not isinstance(message_id, int):
        raise AppError("PDF 返回成功但没有 message_id，上传结果未知。")
    return message_id


def run(config: RuntimeConfig, now: datetime | None = None) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    date_key = current.astimezone(CHINA_TZ).date().isoformat()
    store: GitHubStateStore | None = None
    state: dict[str, Any] | None = None
    state_sha: str | None = None

    if not config.dry_run:
        store = GitHubStateStore(config.github_token, config.github_repository)
        state, state_sha = store.read()
        if is_claimed_for_today(state, date_key):
            if state["status"] in {"sending_main", "sending_document"}:
                raise AppError(f"{date_key} 的 {state['status']} 发送结果未知；请核对频道并按 README 恢复，禁止盲目重发。")
            print(f"{date_key} 已存在状态 {state['status']}，为避免重复发送，本次跳过。")
            return 0

    resuming = bool(state and state.get("date") == date_key and state.get("status") == "main_sent")
    if resuming:
        digest = state.get("digest")
        try:
            current = datetime.fromisoformat(state["generated_at"])
            validate_digest(digest, current)
            if not isinstance(state.get("telegram_message_id"), int):
                raise ValueError("缺少主帖ID")
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(f"PDF续传状态无效：{exc}") from None
        print("检测到已发送主帖：只补发同一份深度PDF，不再次搜索或发送主帖。")
    else:
        digest = generate_digest(config, current)
    post = format_post(digest, current)
    output_dir = Path("output/pdf")
    try:
        pdf_path = build_pdf(digest, current.astimezone(CHINA_TZ), output_dir / f"aiqinbaozhan-{date_key}-deep-report.pdf")
    except Exception as exc:
        raise AppError(f"PDF生成失败，尚未进行本次Telegram发送：{exc}") from None
    if not pdf_path.is_file() or pdf_path.stat().st_size > 49 * 1024 * 1024:
        raise AppError("PDF文件缺失或过大，停止发送。")

    print("\n===== 生成的 Telegram 帖子 =====\n")
    print(post)
    print("\n===== 帖子结束 =====\n")

    if config.dry_run:
        print(f"DRY_RUN=true：主帖仅日志预览，PDF已生成：{pdf_path}；没有调用 Telegram，也没有修改发布状态。")
        return 0

    assert store is not None and state_sha is not None
    content_hash = hashlib.sha256(post.encode("utf-8")).hexdigest()
    claimed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    claim = state if resuming else {
        "date": date_key,
        "status": "sending_main",
        "claimed_at": claimed_at,
        "content_sha256": content_hash,
        "generated_at": current.isoformat(timespec="seconds"),
        "digest": digest,
        "channel": config.telegram_channel,
    }
    if resuming and claim.get("channel") != config.telegram_channel:
        raise AppError("续传频道与原主帖不同，停止发送。")
    if not resuming:
        state_sha = store.write(claim, state_sha, f"chore: claim publication for {date_key}")
        try:
            message_id = publish_to_telegram(config.telegram_bot_token, config.telegram_channel, post)
        except Exception as exc:
            raise AppError(f"主帖发送未确认成功，保留 sending_main 占位，未发送PDF；请先核对频道。错误：{exc}") from None
        claim = {**claim, "status": "main_sent", "telegram_message_id": message_id}
        try:
            state_sha = store.write(claim, state_sha, f"chore: save main message for {date_key}")
        except Exception as exc:
            raise AppError(f"主帖ID={message_id}已发送，但状态保存失败，尚未发送PDF；原占位阻止主帖重复。错误：{exc}") from None
    else:
        message_id = claim["telegram_message_id"]
    uploading = {**claim, "status": "sending_document"}
    state_sha = store.write(uploading, state_sha, f"chore: claim PDF upload for {date_key}")
    try:
        document_id = publish_document(config.telegram_bot_token, config.telegram_channel, pdf_path, message_id, date_key)
    except TelegramRejectedError:
        store.write(claim, state_sha, f"chore: PDF rejected; preserve main for {date_key}")
        raise
    except Exception as exc:
        raise AppError(f"主帖ID={message_id}已发送，PDF结果未知，保留 sending_document 占位，请核对频道。错误：{exc}") from None

    published = {
        **uploading,
        "status": "published",
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telegram_message_id": message_id,
        "telegram_document_message_id": document_id,
        "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
    }
    try:
        store.write(published, state_sha, f"chore: mark publication complete for {date_key}")
    except Exception as exc:
        raise AppError(
            f"Telegram主帖ID={message_id}和PDF ID={document_id}已发送，但状态未标记为 published；"
            f"sending_document 占位仍会阻止重复发送。错误：{exc}"
        ) from None
    print(f"发布成功：频道 {config.telegram_channel}，主帖ID={message_id}，PDF ID={document_id}。")
    return 0


def redact_secrets(message: str, config: RuntimeConfig | None = None) -> str:
    safe = message
    if config:
        for secret in (config.openai_api_key, config.telegram_bot_token, config.github_token):
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
    safe = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", safe)
    safe = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[REDACTED]", safe)
    safe = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", safe, flags=re.I)
    return safe


def main() -> int:
    config: RuntimeConfig | None = None
    try:
        config = load_config()
        return run(config)
    except KeyboardInterrupt:
        print("错误：运行被中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{redact_secrets(str(exc), config)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
