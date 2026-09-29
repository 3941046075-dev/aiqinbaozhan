"""Manually recover the audited 2026-09-18..29 outage; daily bot is unchanged."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ai_news_bot as bot

DAILY_PATH = bot.STATE_PATH
AUDIT_REF = "5ad2649edd7d5650bade767a8f2098531f43f831"
REJECTED_HASH = "ffc632a8d9f72b6026f954aeaea76d17f94afec6515c1828aabc2eb3c12500e7"
FAILED_RUNS = {
    19: 35411704105, 20: 35480629219, 21: 35549971820,
    22: 35674657085, 23: 35805123682, 24: 35941660169,
    25: 36080785878, 26: 36207388192, 27: 36284659750,
    28: 36364920962, 29: 36506648265,
}


def requested_dates(start: str, end: str, today: date) -> list[date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if not date(2026, 9, 18) <= first <= last <= min(date(2026, 9, 29), today):
        raise bot.AppError("仅允许补发已审计的 2026-09-18 至 2026-09-29，且不能选择未来日期。")
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def verify_skipped_generation(store, day: date) -> None:
    data = bot.request_json(
        f"{store.api_base}/actions/runs/{FAILED_RUNS[day.day]}/jobs?per_page=100",
        headers=store.headers,
    )
    steps = [step for job in data.get("jobs", []) for step in job.get("steps", [])
             if step.get("name") == "生成并按配置发布"]
    if len(steps) != 1 or steps[0].get("conclusion") != "skipped":
        raise bot.AppError(f"{day} 原任务未确认跳过生成/发送，停止该日补发。")


def archived_rejected_state(store):
    data = bot.request_json(
        f"{store.api_base}/contents/{DAILY_PATH}?ref={AUDIT_REF}", headers=store.headers)
    state = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    # Only the exact previously audited HTTP 429 rejection is eligible.
    if (state.get("date") != "2026-09-18" or state.get("status") != "sending_main"
            or state.get("content_sha256") != REJECTED_HASH
            or state.get("channel") != "@aiqinbaozhan"
            or state.get("telegram_message_id") is not None):
        raise bot.AppError("9月18日快照与已审计的明确限流拒绝不一致，禁止清除占位。")
    return state


def ensure_ledger(store, day: date, daily_state: dict, dry_run: bool, original=None):
    try:
        state, _ = store.read()
        if state.get("date") != day.isoformat():
            raise bot.AppError("补发记录日期不匹配。")
        return
    except bot.AppError as exc:
        if not str(exc).startswith("远程接口 HTTP 404："):
            raise
    initial = {"date": day.isoformat(), "status": "ready",
               "audit": "Manual recovery authorized 2026-09-29; original schedule unchanged."}
    if daily_state.get("date") == day.isoformat():
        if daily_state.get("status") in {"published", "main_sent", "sending", "sending_main", "sending_document"}:
            initial = daily_state.copy()
        if original is not None and daily_state == original:
            initial = {"date": day.isoformat(), "status": "ready", "original_state": original,
                       "audit": "Run 35294184897 explicitly rejected sendMessage with HTTP 429; no PDF attempted."}
    if not dry_run:
        # No sha: GitHub refuses to overwrite a concurrently created record.
        bot.request_json(f"{store.api_base}/contents/{bot.STATE_PATH}", method="PUT",
                         headers=store.headers, retries=1,
                         body={"message": f"chore: preserve audited recovery baseline for {day}",
                               "branch": store.branch,
                               "content": base64.b64encode(json.dumps(initial, ensure_ascii=False,
                                                          indent=2).encode()).decode()})


def recover_day(config, day: date, today: date):
    with patch.object(bot, "STATE_PATH", DAILY_PATH):
        store = bot.GitHubStateStore(config.github_token, config.github_repository)
        daily_state, _ = store.read()
    if daily_state.get("date") == day.isoformat() and daily_state.get("status") == "published":
        print(f"{day} 日常任务已发布，跳过。", flush=True)
        return {"date": str(day), "status": "already_published"}
    original = archived_rejected_state(store) if day.day == 18 else None
    if original is None:
        verify_skipped_generation(store, day)
    historical_now = (datetime.fromisoformat(original["generated_at"]) if original else
                      datetime(day.year, day.month, day.day, 9, tzinfo=bot.CHINA_TZ))
    path = DAILY_PATH if day == today else f"state/backfill/{day.isoformat()}.json"
    original_prompt, original_format = bot.build_prompt, bot.format_post
    def prompt(now):
        return original_prompt(now) + "\n这是历史补发。严格检索指定历史窗口，不得混入窗口之后的事实、后见之明或当前新闻。无法核实就不写。"
    def format_post(digest, now):
        post = f"🗂 补发｜原定日期 {day.isoformat()}（非实时快讯）\n\n" + original_format(digest, now)
        if len(post.encode("utf-16-le")) // 2 > bot.TELEGRAM_TEXT_LIMIT:
            raise bot.AppError("补发标记导致主帖超长，停止发送。")
        return post
    with patch.object(bot, "STATE_PATH", path), patch.object(bot, "build_prompt", prompt), patch.object(bot, "format_post", format_post):
        if path != DAILY_PATH:
            ensure_ledger(store, day, daily_state, config.dry_run, original)
        if original is not None:
            bot.validate_digest(original["digest"], historical_now)
            with patch.object(bot, "generate_digest", return_value=original["digest"]):
                bot.run(config, historical_now)
        else:
            bot.run(config, historical_now)
        if config.dry_run:
            return {"date": str(day), "status": "dry_run"}
        state, _ = store.read()
        if state.get("status") != "published":
            raise bot.AppError(f"{day} 未确认主帖和PDF全部发布：{state.get('status')}")
        return {key: state.get(key) for key in
                ("date", "status", "telegram_message_id", "telegram_document_message_id")}


def main():
    config = bot.load_config()
    if config.github_repository != "haoxu9144-ship-it/aiqinbaozhan" or config.telegram_channel != "@aiqinbaozhan":
        raise bot.AppError("本次恢复只允许原仓库及原频道。")
    today = datetime.now(bot.CHINA_TZ).date()
    days = requested_dates(os.environ["START_DATE"], os.environ["END_DATE"], today)
    results = []
    for index, day in enumerate(days):
        print(f"\n开始处理 {day}（{index + 1}/{len(days)}）", flush=True)
        try:
            result = recover_day(config, day, today)
        except Exception as exc:
            result = {"date": str(day), "status": "failed", "error": bot.redact_secrets(str(exc), config)}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        Path("output").mkdir(exist_ok=True)
        Path("output/backfill-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        if index + 1 < len(days) and not config.dry_run:
            time.sleep(30)
    return int(any(row["status"] == "failed" for row in results))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("补发初始化失败；未输出异常详情以防泄露凭据。请检查日期、配置和仓库权限。", file=sys.stderr)
        raise SystemExit(1)
