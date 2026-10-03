import importlib.util
import json
import io
import urllib.error
import sys
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock


MODULE_PATH = Path(__file__).parents[1] / "src" / "ai_news_bot.py"
SPEC = importlib.util.spec_from_file_location("ai_news_bot", MODULE_PATH)
bot = importlib.util.module_from_spec(SPEC)
sys.modules["ai_news_bot"] = bot
assert SPEC.loader is not None
SPEC.loader.exec_module(bot)


NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def setUpModule():
    # A stale mock must fail locally instead of making a real API request.
    network_guard = patch.object(
        bot.urllib.request, "urlopen",
        side_effect=AssertionError("单元测试禁止真实网络请求；请模拟实际的请求入口。"),
    )
    network_guard.start()
    unittest.addModuleCleanup(network_guard.stop)


def sample_digest(count=4):
    return {
        "items": [
            {
                "headline": f"可信 AI 新闻 {index}",
                "region": "domestic" if index % 2 else "global",
                "category": "business" if index == 3 else "tool" if index == 4 else "news",
                "in_brief": index <= 4,
                "takeaway": "合成版式测试结论，不代表真实事件。",
                "background": "这是离线测试样例，用于检查中文排版，不是真实新闻。",
                "china_impact": "测试中国用户影响段落：实际可用范围应以官方说明为准。",
                "entrepreneur_impact": "测试商业分析段落：应先验证客户需求，不承诺收益。",
                "attention_reason": "测试关注判断：核验真实来源后才可以公开发布。",
                "key_updates": [],
                "what_happened": "某公司发布了经过来源确认的重要更新。",
                "why_important": "这会影响模型能力、开发者成本或行业竞争格局。",
                "published_at": (NOW - timedelta(hours=index)).isoformat(),
                "source_name": "官方来源",
                "source_url": f"https://example.com/news/{index}",
            }
            for index in range(1, count + 1)
        ],
        "watch": {"thing": "关注更新的实际可用范围", "reason": "官方后续文档会决定其真实影响。"},
        "trend": "版式测试：此处展示当天一句话趋势，不是真实新闻。",
        "editorial_comment": "版式测试：摘要可以省字，核验事实可不能省步骤。",
        "takeaways": ["测试结论一：事实需要来源。", "测试结论二：分析要说明限制。", "测试结论三：不要承诺收益。"],
    }


class ConfigTests(unittest.TestCase):
    def test_dry_run_only_requires_openai_key(self):
        config = bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "true"})
        self.assertTrue(config.dry_run)

    def test_live_mode_requires_publish_settings(self):
        with self.assertRaisesRegex(bot.AppError, "TELEGRAM_BOT_TOKEN"):
            bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "false"})

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaisesRegex(bot.AppError, "true 或 false"):
            bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "maybe"})


class ValidationTests(unittest.TestCase):
    def test_editorial_comment_appears_once_before_takeaways(self):
        digest = sample_digest()
        bot.validate_digest(digest, NOW)
        post = bot.format_post(digest, NOW)
        self.assertEqual(post.count("😏 情报站锐评"), 1)
        self.assertEqual(post.count(digest["editorial_comment"]), 1)
        self.assertLess(post.index("💡 赚钱 / 创业机会"), post.index("😏 情报站锐评"))
        self.assertLess(post.index("😏 情报站锐评"), post.index("📌 今天只记住这 3 件事"))
        self.assertLessEqual(bot.utf16_length(post), 1100)

    def test_editorial_comment_length_boundaries(self):
        for length in (15, 45, 60):
            with self.subTest(length=length):
                digest = sample_digest()
                digest["editorial_comment"] = "测" * length
                bot.validate_digest(digest, NOW)
        for length in (0, 14, 61):
            with self.subTest(length=length):
                digest = sample_digest()
                digest["editorial_comment"] = "测" * length
                with self.assertRaisesRegex(bot.AppError, "情报站锐评"):
                    bot.validate_digest(digest, NOW)

    def test_editorial_comment_rejects_multiple_lines_or_invalid_structure(self):
        for comment in (None, ["测试观察"], "测" * 15 + "\n", "测" * 15 + "\r",
                        "测" * 15 + "\u2028", "情报站锐评：" + "测" * 15):
            with self.subTest(comment=comment):
                digest = sample_digest()
                digest["editorial_comment"] = comment
                with self.assertRaisesRegex(bot.AppError, "情报站锐评"):
                    bot.validate_digest(digest, NOW)

    def test_legacy_digest_remains_compatible_without_canned_comment(self):
        digest = sample_digest()
        del digest["editorial_comment"]
        bot.validate_digest(digest, NOW)
        self.assertNotIn("情报站锐评", bot.format_post(digest, NOW))

    def test_valid_digest_and_title(self):
        digest = sample_digest()
        bot.validate_digest(digest, NOW)
        post = bot.format_post(digest, NOW)
        self.assertTrue(post.startswith("🔥 今日 AI 情报｜2026年9月2日"))
        self.assertIn("⚡ 今天一句话", post)
        self.assertIn("📌 今天只记住这 3 件事", post)
        self.assertNotIn("发生了什么：", post)
        self.assertLessEqual(bot.utf16_length(post), 4096)

    def test_accepts_one_to_three_verified_items(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                digest = sample_digest(count)
                for item in digest["items"]:
                    item["region"] = "global"
                bot.validate_digest(digest, NOW)
                self.assertIn("🌍 全球 AI", bot.format_post(digest, NOW))

    def test_rejects_zero_or_too_many_items(self):
        for count in (0, 9):
            with self.subTest(count=count), self.assertRaisesRegex(bot.AppError, "不满足 1～8 条"):
                bot.validate_digest(sample_digest(count), NOW)

    def test_low_volume_still_checks_date_source_and_region(self):
        for field, value, error in (("published_at", (NOW - timedelta(hours=25)).isoformat(), "24 小时"),
                                    ("source_url", "invalid", "URL"), ("region", "invalid", "地区"),
                                    ("in_brief", False, "精选")):
            digest = sample_digest(1)
            digest["items"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(bot.AppError, error):
                bot.validate_digest(digest, NOW)
        with self.assertRaisesRegex(bot.AppError, "来源站点"):
            bot.validate_digest(sample_digest(1), NOW, searched_urls={"https://unrelated.invalid/news"})

    def test_rejects_old_news(self):
        digest = sample_digest()
        digest["items"][0]["published_at"] = (NOW - timedelta(hours=25)).isoformat()
        with self.assertRaisesRegex(bot.AppError, "不在过去 24 小时内"):
            bot.validate_digest(digest, NOW)

    def test_rejects_future_news(self):
        digest = sample_digest()
        digest["items"][0]["published_at"] = (NOW + timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(bot.AppError, "不在过去 24 小时内"):
            bot.validate_digest(digest, NOW)

    def test_rejects_duplicate_sources(self):
        digest = sample_digest()
        digest["items"][1]["source_url"] = digest["items"][0]["source_url"]
        with self.assertRaisesRegex(bot.AppError, "重复来源"):
            bot.validate_digest(digest, NOW)

    def test_telegram_length_counts_emoji_as_two_units(self):
        self.assertEqual(bot.utf16_length("🔥a"), 3)

    def test_accepts_article_path_variant_from_a_searched_host(self):
        digest = sample_digest()
        searched = {bot.canonical_url(item["source_url"]) for item in digest["items"]}
        searched.remove("https://example.com/news/1")
        searched.add("https://example.com/search-result/1")
        bot.validate_digest(digest, NOW, searched_urls=searched)

    def test_rejects_source_from_an_unsearched_host(self):
        digest = sample_digest()
        searched = {"https://different.example/news/1"}
        with self.assertRaisesRegex(bot.AppError, "来源站点不在本次网页搜索结果"):
            bot.validate_digest(digest, NOW, searched_urls=searched)


class ResponseTests(unittest.TestCase):
    def test_extract_output_text(self):
        payload = {
            "output": [
                {"type": "web_search_call"},
                {"type": "message", "content": [{"type": "output_text", "text": "{\"ok\":true}"}]},
            ]
        }
        self.assertEqual(bot.extract_output_text(payload), '{"ok":true}')

    def test_generate_requires_web_search_record(self):
        config = bot.RuntimeConfig("key", "", "", True, "model", "", "")
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample_digest())}]}
            ],
        }
        with patch.object(bot, "request_json", return_value=response):
            with self.assertRaisesRegex(bot.AppError, "没有网页搜索记录"):
                bot.generate_digest(config, NOW)

    def test_extracts_and_canonicalizes_search_sources(self):
        payload = {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"type": "url", "url": "https://Example.com/news/1/#section"}
                        ]
                    },
                }
            ]
        }
        self.assertEqual(
            bot.extract_search_source_urls(payload),
            {"https://example.com/news/1"},
        )

    def test_canonical_url_ignores_www_and_article_tracking_query(self):
        self.assertEqual(
            bot.canonical_url("https://www.Example.com/news/1/?utm_source=openai#section"),
            "https://example.com/news/1",
        )

    def test_canonical_url_keeps_query_for_query_only_article_urls(self):
        self.assertEqual(
            bot.canonical_url("https://example.com/?id=123#section"),
            "https://example.com/?id=123",
        )

    def test_telegram_send_success_is_not_retried(self):
        reply = MagicMock()
        reply.__enter__.return_value.read.return_value = b'{"ok":true,"result":{"message_id":7}}'
        with patch.object(bot.urllib.request, "urlopen", return_value=reply) as request:
            self.assertEqual(bot.publish_to_telegram("token", "@channel", "post"), 7)
            self.assertEqual(request.call_count, 1)

    def test_telegram_429_waits_before_retry(self):
        rejection = urllib.error.HTTPError('https://example.com', 429, 'limited', {},
            io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":5}}'))
        reply = MagicMock()
        reply.__enter__.return_value.read.return_value = b'{"ok":true,"result":{"message_id":7}}'
        with patch.object(bot.urllib.request, "urlopen", side_effect=[rejection, reply]) as send, patch.object(bot.time, "sleep") as sleep:
            self.assertEqual(bot.publish_to_telegram("token", "@channel", "post"), 7)
            sleep.assert_called_once_with(5)
            self.assertEqual(send.call_count, 2)

    def test_telegram_unknown_errors_are_not_retried(self):
        for error in (TimeoutError(), urllib.error.HTTPError('https://example.com', 500, 'failed', {}, io.BytesIO(b'{}')),
                      urllib.error.HTTPError('https://example.com', 429, 'limited', {}, io.BytesIO(b'{}'))):
            with self.subTest(error=error), patch.object(bot.urllib.request, "urlopen", side_effect=error) as send, patch.object(bot.time, "sleep") as sleep:
                with self.assertRaises(bot.AppError):
                    bot.publish_to_telegram("token", "@channel", "post")
                self.assertEqual(send.call_count, 1)
                sleep.assert_not_called()

    def test_telegram_retry_delay_is_bounded(self):
        for delay in (0, 61, True, "5", None):
            self.assertIsNone(bot.telegram_retry_after(json.dumps({"ok":False,"error_code":429,"parameters":{"retry_after":delay}})))

    def test_telegram_rate_limit_retry_is_not_infinite(self):
        errors = [urllib.error.HTTPError('https://example.com', 429, 'limited', {},
            io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":5}}')) for _ in range(3)]
        with patch.object(bot.urllib.request, "urlopen", side_effect=errors) as send, patch.object(bot.time, "sleep") as sleep:
            with self.assertRaisesRegex(bot.AppError, "429"):
                bot.publish_to_telegram("token", "@channel", "post")
            self.assertEqual(send.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_generation_retries_once_when_items_exceed_limit(self):
        config = bot.RuntimeConfig("key", "", "", True, "model", "", "")

        def response_for(count):
            digest = sample_digest(count)
            return {
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "sources": [
                                {"type": "url", "url": item["source_url"]}
                                for item in digest["items"]
                            ]
                        },
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(digest)}],
                    },
                ],
            }

        with patch.object(
            bot,
            "request_json",
            side_effect=[response_for(9), response_for(4)],
        ) as request:
            digest = bot.generate_digest(config, NOW)

        self.assertEqual(len(digest["items"]), 4)
        self.assertEqual(request.call_count, 2)
        second_prompt = request.call_args_list[1].kwargs["body"]["input"]
        self.assertIn("上一次严格筛选后只有 9 条", second_prompt)


class FlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        original = bot.build_pdf
        pdf = patch.object(bot, "build_pdf", side_effect=lambda digest, local, path: original(digest, local, Path(temp.name) / "test.pdf", demo=True))
        pdf.start()
        self.addCleanup(pdf.stop)

    def test_dry_run_never_sends_or_touches_state(self):
        config = bot.RuntimeConfig("key", "", "", True, "model", "", "")
        with (
            patch.object(bot, "generate_digest", return_value=sample_digest()),
            patch.object(bot, "publish_to_telegram") as send,
            patch.object(bot, "GitHubStateStore") as store,
        ):
            self.assertEqual(bot.run(config, NOW), 0)
            send.assert_not_called()
            store.assert_not_called()

    def test_existing_claim_skips_before_generation(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        fake_store = unittest.mock.Mock()
        fake_store.read.return_value = ({"date": "2026-09-02", "status": "published"}, "sha")
        with (
            patch.object(bot, "GitHubStateStore", return_value=fake_store),
            patch.object(bot, "generate_digest") as generate,
        ):
            self.assertEqual(bot.run(config, NOW), 0)
            generate.assert_not_called()

    def test_live_mode_claims_before_sending(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        fake_store = unittest.mock.Mock()
        fake_store.read.return_value = ({"date": None, "status": "never_published"}, "old-sha")
        fake_store.write.side_effect = ["claim-sha", "main-sha", "upload-sha", "done-sha"]
        events = []

        def record_send(*_args):
            events.append("send")
            self.assertEqual(fake_store.write.call_count, 1)
            return 123

        with (
            patch.object(bot, "GitHubStateStore", return_value=fake_store),
            patch.object(bot, "generate_digest", return_value=sample_digest()),
            patch.object(bot, "publish_to_telegram", side_effect=record_send),
            patch.object(bot, "publish_document", return_value=124),
        ):
            self.assertEqual(bot.run(config, NOW), 0)
        self.assertEqual(events, ["send"])
        self.assertEqual(fake_store.write.call_count, 4)
        self.assertEqual(fake_store.write.call_args_list[0].args[0]["status"], "sending_main")
        self.assertEqual(fake_store.write.call_args_list[3].args[0]["status"], "published")

    def test_resume_sends_only_pdf_without_generating_again(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": "2026-09-02", "status": "main_sent", "digest": sample_digest(),
                                   "generated_at": NOW.isoformat(), "telegram_message_id": 123, "channel": "@channel"}, "sha")
        store.write.return_value = "new-sha"
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest") as generate, patch.object(bot, "publish_to_telegram") as main, patch.object(bot, "publish_document", return_value=124) as pdf:
            self.assertEqual(bot.run(config, NOW), 0)
            generate.assert_not_called()
            main.assert_not_called()
            self.assertEqual(pdf.call_args.args[3], 123)

    def test_unknown_pdf_upload_blocks_retries(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": "2026-09-02", "status": "sending_document"}, "sha")
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest") as generate:
            with self.assertRaisesRegex(bot.AppError, "发送结果未知"):
                bot.run(config, NOW)
            generate.assert_not_called()

    def test_pdf_failure_prevents_main_send(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": None}, "sha")
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest", return_value=sample_digest()), patch.object(bot, "build_pdf", side_effect=ValueError("字体缺失")), patch.object(bot, "publish_to_telegram") as main:
            with self.assertRaisesRegex(bot.AppError, "PDF生成失败"):
                bot.run(config, NOW)
            main.assert_not_called()
            store.write.assert_not_called()

    def test_explicit_pdf_rejection_retains_main_sent(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": None}, "sha")
        store.write.return_value = "new-sha"
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest", return_value=sample_digest()), patch.object(bot, "publish_to_telegram", return_value=123), patch.object(bot, "publish_document", side_effect=bot.TelegramRejectedError("权限不足")):
            with self.assertRaisesRegex(bot.AppError, "权限不足"):
                bot.run(config, NOW)
        self.assertEqual(store.write.call_args.args[0]["status"], "main_sent")
        self.assertEqual(store.write.call_args.args[0]["telegram_message_id"], 123)

    def test_pdf_timeout_keeps_unknown_upload_claim(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": None}, "sha")
        store.write.return_value = "new-sha"
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest", return_value=sample_digest()), patch.object(bot, "publish_to_telegram", return_value=123), patch.object(bot, "publish_document", side_effect=bot.AppError("网络异常")):
            with self.assertRaisesRegex(bot.AppError, "网络异常"):
                bot.run(config, NOW)
        self.assertEqual(store.write.call_args.args[0]["status"], "sending_document")

    def test_failed_main_send_never_sends_document(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        store = unittest.mock.Mock()
        store.read.return_value = ({"date": None}, "sha")
        store.write.return_value = "new-sha"
        with patch.object(bot, "GitHubStateStore", return_value=store), patch.object(bot, "generate_digest", return_value=sample_digest()), patch.object(bot, "publish_to_telegram", side_effect=bot.AppError("结果未知")), patch.object(bot, "publish_document") as pdf:
            with self.assertRaisesRegex(bot.AppError, "结果未知"):
                bot.run(config, NOW)
            pdf.assert_not_called()
        self.assertEqual(store.write.call_args.args[0]["status"], "sending_main")


class EditorialTests(unittest.TestCase):
    def test_missing_domestic_news_rejected(self):
        digest = sample_digest()
        for item in digest["items"]:
            item["region"] = "global"
        with self.assertRaisesRegex(bot.AppError, "缺少合格国内"):
            bot.validate_digest(digest, NOW)

    def test_long_conclusion_rejected_not_truncated(self):
        digest = sample_digest()
        digest["items"][0]["takeaway"] = "长" * 59
        with self.assertRaisesRegex(bot.AppError, "过长"):
            bot.validate_digest(digest, NOW)

    def test_schema_contains_deep_fields(self):
        schema = bot.json_schema()
        self.assertIn("trend", schema["required"])
        self.assertIn("editorial_comment", schema["required"])
        self.assertEqual(schema["properties"]["editorial_comment"], {"type": "string"})
        self.assertNotIn("editorial_comment", schema["properties"]["items"]["items"]["properties"])
        self.assertIn("china_impact", schema["properties"]["items"]["items"]["required"])


class RedactionTests(unittest.TestCase):
    def test_redacts_all_tokens(self):
        config = bot.RuntimeConfig(
            "sk-secret-openai-value",
            "123456:telegram_secret",
            "@channel",
            False,
            "model",
            "github-secret-value",
            "owner/repo",
        )
        raw = "sk-secret-openai-value 123456:telegram_secret github-secret-value Bearer abc.def"
        safe = bot.redact_secrets(raw, config)
        self.assertNotIn("secret", safe)
        self.assertNotIn("abc.def", safe)


if __name__ == "__main__":
    unittest.main()
