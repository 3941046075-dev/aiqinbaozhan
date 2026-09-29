import base64
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch, Mock

import backfill_september as recovery


class BackfillSafetyTests(unittest.TestCase):
    def test_only_audited_dates(self):
        self.assertEqual(len(recovery.requested_dates("2026-09-18", "2026-09-29", date(2026, 9, 29))), 12)
        for start, end in [("2026-09-17", "2026-09-29"), ("2026-09-18", "2026-09-30"),
                           ("2026-09-20", "2026-09-19"), ("../../x", "2026-09-29")]:
            with self.assertRaises((ValueError, recovery.bot.AppError)):
                recovery.requested_dates(start, end, date(2026, 9, 29))

    def test_future_rejected(self):
        with self.assertRaises(recovery.bot.AppError):
            recovery.requested_dates("2026-09-18", "2026-09-29", date(2026, 9, 28))

    def test_must_prove_generation_skipped(self):
        store = SimpleNamespace(api_base="https://example.invalid", headers={})
        for conclusion in ["success", "failure", None]:
            with patch.object(recovery.bot, "request_json", return_value={"jobs": [{"steps": [
                {"name": "生成并按配置发布", "conclusion": conclusion}]}]}):
                with self.assertRaises(recovery.bot.AppError):
                    recovery.verify_skipped_generation(store, date(2026, 9, 19))

    def test_existing_ledger_not_overwritten(self):
        store = Mock()
        store.read.return_value = ({"date": "2026-09-19", "status": "sending_main"}, "sha")
        with patch.object(recovery.bot, "request_json") as api:
            recovery.ensure_ledger(store, date(2026, 9, 19), {}, False)
            api.assert_not_called()

    def test_network_error_never_treated_as_missing_ledger(self):
        store = Mock()
        store.read.side_effect = recovery.bot.AppError("远程接口网络错误")
        with patch.object(recovery.bot, "request_json") as api:
            with self.assertRaises(recovery.bot.AppError):
                recovery.ensure_ledger(store, date(2026, 9, 19), {}, False)
            api.assert_not_called()

    def test_dry_run_does_not_create_state(self):
        store = Mock()
        store.read.side_effect = recovery.bot.AppError("远程接口 HTTP 404：Not found")
        with patch.object(recovery.bot, "request_json") as api:
            recovery.ensure_ledger(store, date(2026, 9, 19), {}, True)
            api.assert_not_called()

    def test_new_ledger_preserves_ambiguous_daily_state(self):
        store = SimpleNamespace(api_base="https://example.invalid", headers={}, branch="main",
                                read=Mock(side_effect=recovery.bot.AppError("远程接口 HTTP 404：Not found")))
        state = {"date": "2026-09-19", "status": "sending_document", "telegram_message_id": 5}
        with patch.object(recovery.bot, "request_json") as api:
            recovery.ensure_ledger(store, date(2026, 9, 19), state, False)
            body = api.call_args.kwargs["body"]
            self.assertNotIn("sha", body)
            self.assertEqual(json.loads(base64.b64decode(body["content"])), state)

    def test_changed_rejected_snapshot_is_blocked(self):
        store = SimpleNamespace(api_base="https://example.invalid", headers={})
        with patch.object(recovery.bot, "request_json", return_value={"content": base64.b64encode(b'{}').decode()}):
            with self.assertRaises(recovery.bot.AppError):
                recovery.archived_rejected_state(store)

    def test_daily_published_skips_every_send(self):
        config = SimpleNamespace(github_token="fake", github_repository="test")
        with patch.object(recovery.bot, "GitHubStateStore") as store, patch.object(recovery.bot, "run") as run:
            store.return_value.read.return_value = ({"date": "2026-09-29", "status": "published"}, "sha")
            self.assertEqual(recovery.recover_day(config, date(2026, 9, 29), date(2026, 9, 29))["status"], "already_published")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
