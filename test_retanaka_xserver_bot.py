import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    module_path = Path(__file__).resolve().parent / "xserver" / "retanaka_xserver_bot.py"
    spec = importlib.util.spec_from_file_location("retanaka_xserver_bot", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetanakaXserverBotTests(unittest.TestCase):
    def make_snapshot(self, bot, published_at="2026-07-11 09:30"):
        return bot.PriceSnapshot(
            published_at=published_at,
            k24=17000,
            pt=7000,
            silver_999=220,
            fetched_at="2026-07-11T00:30:00Z",
        )

    def write_delivery_config(self, directory, state_file):
        config_path = Path(directory) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "line_channel_access_token": "test-token",
                    "line_group_id": "test-group",
                    "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
                    "state_file": str(state_file),
                    "enable_section_screenshot": False,
                }
            ),
            encoding="utf-8",
        )
        return config_path

    def test_line_sends_only_first_update_of_day(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        first = self.make_snapshot(bot, "2026-07-11 09:30")
        later = self.make_snapshot(bot, "2026-07-11 14:00")

        self.assertTrue(bot.should_deliver_line(state, first, "2026-07-11"))
        state["deliveries"]["line"]["last_sent_date"] = "2026-07-11"
        self.assertFalse(bot.should_deliver_line(state, later, "2026-07-11"))
        next_day = self.make_snapshot(bot, "2026-07-12 09:30")
        self.assertTrue(bot.should_deliver_line(state, next_day, "2026-07-12"))

    def test_lineworks_sends_every_new_publication(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        first = self.make_snapshot(bot, "2026-07-11 09:30")
        later = self.make_snapshot(bot, "2026-07-11 14:00")

        self.assertTrue(bot.should_deliver_lineworks(state, first))
        state["deliveries"]["lineworks"]["last_sent_published_at"] = first.published_at
        self.assertFalse(bot.should_deliver_lineworks(state, first))
        self.assertTrue(bot.should_deliver_lineworks(state, later))

    def test_make_empty_state_uses_schema_v3(self):
        bot = load_module()

        self.assertEqual(bot.make_empty_state()["schema_version"], 3)

    def test_migrate_v2_preserves_legacy_pending_as_email(self):
        bot = load_module()

        state = bot.migrate_state(
            {
                "schema_version": 2,
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "transport": "lineworks",
                        "subject": "価格取得エラー",
                        "body": "本文",
                        "message_id": "<old@example.com>",
                        "occurred_at": "2026-07-13 10:00:00",
                    },
                    "lineworks_delivery_error": {
                        "subject": "LINE WORKS送信エラー",
                        "body": "本文",
                        "message_id": "<lw@example.com>",
                        "occurred_at": "2026-07-13 10:01:00",
                    },
                },
            }
        )

        self.assertEqual(state["pending_recovery_alerts"]["price_fetch_error"]["transport"], "email")
        self.assertEqual(
            state["pending_recovery_alerts"]["price_fetch_error"]["message_id"], "<old@example.com>"
        )
        self.assertEqual(state["pending_recovery_alerts"]["lineworks_delivery_error"]["transport"], "email")
        self.assertEqual(
            state["pending_recovery_alerts"]["lineworks_delivery_error"]["message_id"], "<lw@example.com>"
        )

    def test_migrate_v3_rejects_unknown_transport(self):
        bot = load_module()

        state = bot.migrate_state(
            {
                "schema_version": 3,
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "transport": "unknown",
                        "subject": "価格取得エラー",
                        "body": "本文",
                        "occurred_at": "2026-07-13 10:00:00",
                    }
                },
            }
        )

        self.assertEqual(state["pending_recovery_alerts"], {})

    def test_migrate_missing_schema_forces_valid_transport_to_email(self):
        bot = load_module()

        state = bot.migrate_state(
            {
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "transport": "lineworks",
                        "subject": "価格取得エラー",
                        "body": "本文",
                        "message_id": "<legacy@example.com>",
                        "occurred_at": "2026-07-13 10:00:00",
                    }
                },
            }
        )

        self.assertEqual(
            state["pending_recovery_alerts"]["price_fetch_error"]["transport"], "email"
        )

    def test_migrate_schema_v4_rejects_unknown_transport(self):
        bot = load_module()

        state = bot.migrate_state(
            {
                "schema_version": 4,
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "transport": "unknown",
                        "subject": "価格取得エラー",
                        "body": "本文",
                        "message_id": "<unknown@example.com>",
                        "occurred_at": "2026-07-13 10:00:00",
                    }
                },
            }
        )

        self.assertEqual(state["pending_recovery_alerts"], {})

    def test_migrate_legacy_pending_without_message_id_is_discarded(self):
        bot = load_module()

        state = bot.migrate_state(
            {
                "schema_version": 2,
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "transport": "lineworks",
                        "subject": "価格取得エラー",
                        "body": "本文",
                        "occurred_at": "2026-07-13 10:00:00",
                    }
                },
            }
        )

        self.assertEqual(state["pending_recovery_alerts"], {})

    def test_silver_parser_uses_real_silver_table_label_without_reading_platinum(self) -> None:
        bot = load_module()
        html = """
            <p>2026年7月11日 09:30 発表</p>
            <table id="gold"><tr><th>K24特定品</th><td>17,000</td></tr>
              <tr><th>1000（999）</th><td>99,999</td></tr></table>
            <table><caption>銀製品</caption><tr><th>1000（999）</th><td>220</td></tr></table>
            <table><caption>プラチナ製品</caption><tr><th>Pt特定品</th><td>7,000</td></tr>
              <tr><th>1000（999）</th><td>88,888</td></tr></table>
        """

        snapshot = bot.parse_snapshot(html)

        self.assertEqual(snapshot.silver_999, 220)

    def test_silver_parser_handles_live_ag_section_with_misnamed_table_id(self) -> None:
        bot = load_module()
        html = """
            <p>2026年7月11日 09:30 発表</p>
            <table><tr><th>K24特定品</th><td>&yen;21,817</td></tr></table>
            <section id="pt_price"><h3>プラチナ製品</h3><table>
              <tr><th>Pt特定品</th><td>&yen;7,747</td></tr>
              <tr><th>1000<br />(999)</th><td>&yen;7,500</td></tr>
            </table></section>
            <section id="ag_price"><h3>銀製品</h3><table id="pt_price_table">
              <tr><th>1000<br />(999)</th><td rowspan="6">&yen;190</td></tr>
            </table></section>
        """

        self.assertEqual(bot.parse_snapshot(html).silver_999, 190)

    def test_legacy_lineworks_state_migrates_to_schema_v3_without_marking_line_sent(self) -> None:
        bot = load_module()

        state = bot.migrate_state(
            {
                "last_sent_published_at": "2026-07-11 09:30",
                "last_attempt_date": "2026-07-11",
                "error_alert_dates": {"price_fetch_error": "2026-07-11"},
                "test_email_sent_at": "2026-07-11 09:00:00",
            }
        )

        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(
            state["deliveries"]["lineworks"]["last_sent_published_at"], "2026-07-11 09:30"
        )
        self.assertIsNone(state["deliveries"]["line"]["last_sent_date"])
        self.assertIsNone(state["deliveries"]["line"]["last_sent_published_at"])
        self.assertEqual(state["error_alert_dates"], {"price_fetch_error": "2026-07-11"})
        self.assertEqual(state["test_email_sent_at"], "2026-07-11 09:00:00")

    def test_migrate_state_preserves_partial_v2_history_and_defaults(self) -> None:
        bot = load_module()

        state = bot.migrate_state(
            {
                "schema_version": 2,
                "history": [{"published_at": "2026-07-10 09:30", "k24": 16000, "pt": 6000}],
                "deliveries": {"line": {"last_sent_date": "2026-07-10"}},
            }
        )

        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["deliveries"]["line"]["last_sent_date"], "2026-07-10")
        self.assertIsNone(state["deliveries"]["lineworks"]["last_sent_published_at"])

    def test_connection_reset_error_is_classified_in_japanese(self) -> None:
        bot = load_module()

        classified = bot.classify_error(RuntimeError("Connection reset by peer"))

        self.assertEqual(classified["cause"], "接続先との通信が途中で切断されました。")
        self.assertEqual(classified["bot_action"], "次回の毎分実行で自動的に再試行します。")
        self.assertEqual(classified["required_action"], "通常は対応不要です。繰り返す場合は接続先の障害状況を確認してください。")

    def test_operational_errors_are_classified_in_japanese(self) -> None:
        bot = load_module()

        self.assertIn("価格ページ", bot.classify_error(RuntimeError("価格を取得できませんでした"))["cause"])
        self.assertIn("設定", bot.classify_error(RuntimeError("missing required config: line_group_id"))["cause"])
        self.assertIn("スクリーンショット", bot.classify_error(RuntimeError("ScreenshotOne HTTP 500"))["cause"])
        self.assertIn("通知先", bot.classify_error(RuntimeError("LINE WORKS webhook HTTP 400"))["cause"])

    def test_non_lineworks_error_uses_lineworks_only(self):
        bot = load_module()
        state = bot.make_empty_state()
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "price_url": "https://example.com",
        }

        with mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
            bot, "send_alert_email"
        ) as email:
            result = bot.deliver_error_alert(
                config, state, "price_fetch_error", "価格取得エラー", "本文"
            )

        self.assertEqual(result, {"transport": "lineworks"})
        webhook.assert_called_once()
        email.assert_not_called()

    def test_lineworks_delivery_error_uses_email_only(self):
        bot = load_module()
        state = bot.make_empty_state()
        config = {"alert_email": "ops@example.com"}

        with mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
            bot, "send_alert_email", return_value="<error@example.com>"
        ) as email:
            result = bot.deliver_error_alert(
                config, state, "lineworks_delivery_error", "送信エラー", "本文"
            )

        self.assertEqual(result["transport"], "email")
        webhook.assert_not_called()
        email.assert_called_once()

    def test_lineworks_alert_failure_falls_back_to_one_email_without_recursion(self):
        bot = load_module()
        state = bot.make_empty_state()
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }

        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
        ) as webhook, mock.patch.object(
            bot, "send_alert_email", return_value="<fallback@example.com>"
        ) as email:
            result = bot.deliver_error_alert(
                config, state, "price_fetch_error", "価格取得エラー", "元の本文"
            )

        self.assertEqual(webhook.call_count, 1)
        self.assertEqual(email.call_count, 1)
        self.assertEqual(result, {"transport": "email", "message_id": "<fallback@example.com>"})
        self.assertIn("元の本文", email.call_args[0][2])
        self.assertIn("HTTP 500", email.call_args[0][2])
        self.assertEqual(
            state["pending_recovery_alerts"]["lineworks_delivery_error"]["message_id"],
            "<fallback@example.com>",
        )

    def test_direct_error_alert_redacts_configured_secret_from_all_delivery_paths(self):
        bot = load_module()
        state = bot.make_empty_state()
        secret = "direct-body-token"
        config = {
            "line_channel_access_token": secret,
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }

        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
        ) as webhook, mock.patch.object(
            bot, "send_alert_email", return_value="<fallback@example.com>"
        ) as email:
            result = bot.deliver_error_alert(
                config,
                state,
                "price_fetch_error",
                "価格取得エラー",
                "直接本文: {0}".format(secret),
            )

        webhook_body = webhook.call_args[0][1]["body"]["text"]
        email_body = email.call_args[0][2]
        stored_body = state["pending_recovery_alerts"]["lineworks_delivery_error"]["body"]
        self.assertEqual(result, {"transport": "email", "message_id": "<fallback@example.com>"})
        self.assertNotIn(secret, webhook_body)
        self.assertNotIn(secret, email_body)
        self.assertNotIn(secret, stored_body)
        self.assertIn("[伏字]", webhook_body)
        self.assertIn("[伏字]", email_body)
        self.assertIn("[伏字]", stored_body)

    def test_error_alert_redacts_configured_secrets_from_email_and_state(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        secret = "super-secret-token"
        config = {
            "alert_email": "ops@example.com",
            "line_channel_access_token": secret,
            "lineworks_webhook_url": "https://example.invalid/hook/secret-value",
        }

        with mock.patch.object(bot, "send_alert_email", return_value="<error-123@example.com>") as send:
            bot.maybe_send_error_alert(
                config,
                state,
                "line_delivery_error",
                "RE:TANAKA BOT: LINE送信エラー",
                ["エラー: Authorization: Bearer {0}".format(secret)],
            )

        sent_body = send.call_args[0][2]
        stored_body = state["pending_recovery_alerts"]["line_delivery_error"]["body"]
        self.assertNotIn(secret, sent_body)
        self.assertNotIn(secret, stored_body)
        self.assertIn("[伏字]", sent_body)

    def test_error_alert_stores_pending_recovery_with_message_id(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        config = {"alert_email": "ops@example.com"}

        with mock.patch.object(bot, "send_alert_email", return_value="<error-123@example.com>"):
            sent = bot.maybe_send_error_alert(
                config,
                state,
                "price_fetch_error",
                "RE:TANAKA BOT: 価格取得エラー",
                ["価格取得に失敗しました。"],
            )

        self.assertTrue(sent)
        self.assertEqual(
            state["pending_recovery_alerts"]["price_fetch_error"],
            {
                "transport": "email",
                "subject": "RE:TANAKA BOT: 価格取得エラー",
                "body": "価格取得に失敗しました。",
                "message_id": "<error-123@example.com>",
                "occurred_at": mock.ANY,
            },
        )

    def test_recovery_email_replies_with_headers_and_quoted_original(self) -> None:
        bot = load_module()
        pending = {
            "subject": "RE:TANAKA BOT: 価格取得エラー",
            "body": "価格取得に失敗しました。\n技術情報: Connection reset by peer",
            "message_id": "<error-123@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }

        message = bot.build_recovery_alert_email(pending)

        self.assertEqual(str(message["Subject"]), "Re: RE:TANAKA BOT: 価格取得エラー")
        self.assertEqual(message["In-Reply-To"], "<error-123@example.com>")
        self.assertEqual(message["References"], "<error-123@example.com>")
        body = message.get_payload(decode=True).decode("utf-8")
        self.assertIn("自動復旧済み・対応不要です。", body)
        self.assertIn("> 価格取得に失敗しました。", body)
        self.assertIn("> 技術情報: Connection reset by peer", body)

    def test_recovery_quote_prefixes_blank_lines_too(self) -> None:
        bot = load_module()

        body = bot.build_recovery_alert_body("1行目\n\n2行目")

        self.assertIn("> 1行目\n> \n> 2行目", body)

    def test_lineworks_recovery_uses_webhook_and_quote(self):
        bot = load_module()
        pending = {
            "transport": "lineworks",
            "subject": "価格取得エラー",
            "body": "元本文",
            "occurred_at": "2026-07-13 10:00:00",
        }
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "price_url": "https://example.com",
        }

        with mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
            bot, "send_alert_email"
        ) as email:
            self.assertTrue(bot.deliver_recovery_alert(config, bot.make_empty_state(), pending))

        self.assertIn("> 元本文", webhook.call_args[0][1]["body"]["text"])
        email.assert_not_called()

    def test_email_recovery_keeps_reply_headers(self):
        bot = load_module()
        pending = {
            "transport": "email",
            "subject": "LINE WORKS送信エラー",
            "body": "元本文",
            "message_id": "<error@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }

        with mock.patch.object(
            bot, "send_alert_email", return_value="<recovery@example.com>"
        ) as email:
            self.assertTrue(
                bot.deliver_recovery_alert(
                    {"alert_email": "ops@example.com"}, bot.make_empty_state(), pending
                )
            )

        self.assertEqual(email.call_args[1]["in_reply_to"], "<error@example.com>")

    def test_lineworks_recovery_failure_falls_back_and_tracks_lineworks_failure(self):
        bot = load_module()
        state = bot.make_empty_state()
        pending = {
            "transport": "lineworks",
            "subject": "価格取得エラー",
            "body": "元本文",
            "occurred_at": "2026-07-13 10:00:00",
        }
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }

        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
        ), mock.patch.object(
            bot, "send_alert_email", return_value="<fallback@example.com>"
        ):
            self.assertTrue(bot.deliver_recovery_alert(config, state, pending))

        self.assertEqual(
            state["pending_recovery_alerts"]["lineworks_delivery_error"]["message_id"],
            "<fallback@example.com>",
        )

    def test_recovery_redacts_legacy_pending_body_in_all_delivery_paths(self):
        bot = load_module()
        secret = "legacy-pending-secret"
        config = {
            "line_channel_access_token": secret,
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }
        legacy_pending = {
            "subject": "価格取得エラー",
            "body": "元本文: {0}".format(secret),
            "message_id": "<error@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }
        lineworks_pending = dict(legacy_pending, transport="lineworks")

        with mock.patch.object(
            bot, "send_alert_email", return_value="<recovery@example.com>"
        ) as email:
            self.assertTrue(bot.deliver_recovery_alert(config, bot.make_empty_state(), legacy_pending))
        email_body = email.call_args[0][2]

        with mock.patch.object(bot, "send_lineworks_webhook") as webhook:
            self.assertTrue(bot.deliver_recovery_alert(config, bot.make_empty_state(), lineworks_pending))
        webhook_body = webhook.call_args[0][1]["body"]["text"]

        fallback_state = bot.make_empty_state()
        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
        ), mock.patch.object(
            bot, "send_alert_email", return_value="<fallback@example.com>"
        ) as fallback_email:
            self.assertTrue(bot.deliver_recovery_alert(config, fallback_state, lineworks_pending))
        fallback_body = fallback_email.call_args[0][2]
        stored_body = fallback_state["pending_recovery_alerts"]["lineworks_delivery_error"]["body"]

        for body in (email_body, webhook_body, fallback_body, stored_body):
            self.assertNotIn(secret, body)
            self.assertIn("[伏字]", body)

    def test_failed_recovery_redacts_pending_body_before_state_is_saved(self):
        bot = load_module()
        secret = "legacy-pending-state-secret"
        config = {
            "line_channel_access_token": secret,
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            email_state_path = Path(temp_dir) / "email-state.json"
            email_state = bot.make_empty_state()
            email_state["pending_recovery_alerts"]["price_fetch_error"] = {
                "subject": "価格取得エラー",
                "body": "元本文: {0}".format(secret),
                "message_id": "<error@example.com>",
                "occurred_at": "2026-07-13 10:00:00",
            }
            with mock.patch.object(bot, "send_alert_email", return_value=False):
                self.assertFalse(
                    bot.maybe_send_recovery_alert(config, email_state, "price_fetch_error")
                )
            bot.save_state(str(email_state_path), email_state)

            webhook_state_path = Path(temp_dir) / "webhook-state.json"
            webhook_state = bot.make_empty_state()
            webhook_state["pending_recovery_alerts"]["price_fetch_error"] = {
                "transport": "lineworks",
                "subject": "価格取得エラー",
                "body": "元本文: {0}".format(secret),
                "occurred_at": "2026-07-13 10:00:00",
            }
            with mock.patch.object(
                bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
            ), mock.patch.object(bot, "send_alert_email", side_effect=RuntimeError("sendmail failed")):
                with self.assertRaises(RuntimeError):
                    bot.maybe_send_recovery_alert(config, webhook_state, "price_fetch_error")
            bot.save_state(str(webhook_state_path), webhook_state)

            for state_path in (email_state_path, webhook_state_path):
                stored = state_path.read_text(encoding="utf-8")
                self.assertNotIn(secret, stored)
                self.assertIn("[伏字]", stored)

    def test_read_alert_config_only_includes_valid_webhook(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            url = "https://webhook.worksmobile.com/message/example"
            path.write_text(json.dumps({"lineworks_webhook_url": url}), encoding="utf-8")

            alert_config = bot.read_alert_config_only(str(path))

        self.assertEqual(alert_config["lineworks_webhook_url"], url)

    def test_read_alert_config_only_discards_invalid_webhook(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps({"lineworks_webhook_url": "https://example.com/webhook"}),
                encoding="utf-8",
            )

            alert_config = bot.read_alert_config_only(str(path))

        self.assertEqual(alert_config["lineworks_webhook_url"], "")

    def test_failed_recovery_keeps_pending_alert_and_daily_suppression(self):
        bot = load_module()
        state = bot.make_empty_state()
        state["error_alert_dates"]["price_fetch_error"] = "2026-07-13"
        state["pending_recovery_alerts"]["price_fetch_error"] = {
            "transport": "email",
            "subject": "価格取得エラー",
            "body": "元本文",
            "message_id": "<error@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }

        with mock.patch.object(bot, "send_alert_email", return_value=False):
            self.assertFalse(bot.maybe_send_recovery_alert({}, state, "price_fetch_error"))

        self.assertIn("price_fetch_error", state["pending_recovery_alerts"])
        self.assertEqual(state["error_alert_dates"]["price_fetch_error"], "2026-07-13")

    def test_price_fetch_error_retries_next_day_without_replacing_first_pending_alert(self):
        bot = load_module()
        state = bot.make_empty_state()
        first_pending = {
            "transport": "email",
            "subject": "初回の価格取得エラー",
            "body": "初回の本文",
            "message_id": "<first@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }
        state["error_alert_dates"]["price_fetch_error"] = "2000-01-01"
        state["pending_recovery_alerts"]["price_fetch_error"] = dict(first_pending)

        with mock.patch.object(
            bot,
            "deliver_error_alert",
            return_value={"transport": "email", "message_id": "<second@example.com>"},
        ) as deliver:
            self.assertTrue(
                bot.maybe_send_error_alert(
                    {"alert_email": "ops@example.com"},
                    state,
                    "price_fetch_error",
                    "翌日の価格取得エラー",
                    ["翌日の本文"],
                )
            )

        deliver.assert_called_once()
        self.assertEqual(
            state["error_alert_dates"]["price_fetch_error"],
            bot.datetime.now().strftime("%Y-%m-%d"),
        )
        self.assertEqual(state["pending_recovery_alerts"]["price_fetch_error"], first_pending)

    def test_price_fetch_error_same_day_suppression_keeps_first_pending_alert(self):
        bot = load_module()
        state = bot.make_empty_state()
        first_pending = {
            "transport": "email",
            "subject": "初回の価格取得エラー",
            "body": "初回の本文",
            "message_id": "<first@example.com>",
            "occurred_at": "2026-07-13 10:00:00",
        }
        state["error_alert_dates"]["price_fetch_error"] = bot.datetime.now().strftime("%Y-%m-%d")
        state["pending_recovery_alerts"]["price_fetch_error"] = dict(first_pending)

        with mock.patch.object(bot, "deliver_error_alert") as deliver:
            self.assertFalse(
                bot.maybe_send_error_alert(
                    {"alert_email": "ops@example.com"},
                    state,
                    "price_fetch_error",
                    "同日の価格取得エラー",
                    ["同日の本文"],
                )
            )

        deliver.assert_not_called()
        self.assertEqual(state["pending_recovery_alerts"]["price_fetch_error"], first_pending)

    def test_price_webhook_failure_retries_delivery_alert_without_replacing_first_pending(self):
        bot = load_module()
        state = bot.make_empty_state()
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }

        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("initial HTTP 500")
        ), mock.patch.object(
            bot, "send_alert_email", return_value="<first@example.com>"
        ):
            self.assertTrue(
                bot.maybe_send_error_alert(
                    config,
                    state,
                    "price_fetch_error",
                    "RE:TANAKA BOT: 価格取得エラー",
                    ["初回の本文"],
                )
            )

        first_pending = dict(state["pending_recovery_alerts"]["lineworks_delivery_error"])
        with mock.patch.object(
            bot, "send_alert_email", return_value="<second@example.com>"
        ) as email:
            self.assertTrue(
                bot.maybe_send_delivery_error_alert(
                    config,
                    state,
                    "lineworks",
                    RuntimeError("price webhook HTTP 500"),
                    "2026-07-13 10:00",
                )
            )

        email.assert_called_once()
        self.assertEqual(
            state["error_alert_dates"]["lineworks_delivery_error"],
            bot.datetime.now().strftime("%Y-%m-%d"),
        )
        self.assertEqual(
            state["pending_recovery_alerts"]["lineworks_delivery_error"], first_pending
        )
        self.assertEqual(
            first_pending["message_id"], "<first@example.com>"
        )
        self.assertIn("初回の本文", first_pending["body"])

    def test_failed_webhook_and_fallback_do_not_mark_or_queue_error_alert(self):
        bot = load_module()
        state = bot.make_empty_state()
        config = {
            "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
            "alert_email": "ops@example.com",
            "price_url": "https://example.com",
        }

        with mock.patch.object(
            bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
        ), mock.patch.object(bot, "send_alert_email", return_value=False):
            self.assertFalse(
                bot.maybe_send_error_alert(
                    config,
                    state,
                    "price_fetch_error",
                    "RE:TANAKA BOT: 価格取得エラー",
                    ["価格取得に失敗しました。"],
                )
            )

        self.assertEqual(state["error_alert_dates"], {})
        self.assertEqual(state["pending_recovery_alerts"], {})

    def test_dry_run_fetch_failure_sends_no_alert_and_keeps_state(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            original = {"schema_version": 3, "last_observed_published_at": "2026-07-13 09:30"}
            state_path.write_text(json.dumps(original), encoding="utf-8")

            with mock.patch.object(
                bot, "fetch_html", side_effect=RuntimeError("Connection reset")
            ), mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
                bot, "send_alert_email"
            ) as email:
                self.assertEqual(bot.main(["--config", str(config_path), "--dry-run"]), 1)

            webhook.assert_not_called()
            email.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)

    def test_dry_run_config_failure_sends_no_alert_and_keeps_state(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = Path(temp_dir) / "config.json"
            original = {"schema_version": 3, "last_observed_published_at": "2026-07-13 09:30"}
            state_path.write_text(json.dumps(original), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "lineworks_webhook_url": "https://example.com/webhook",
                        "alert_email": "ops@example.com",
                        "state_file": str(state_path),
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
                bot, "send_alert_email"
            ) as email:
                self.assertEqual(bot.main(["--config", str(config_path), "--dry-run"]), 2)

            webhook.assert_not_called()
            email.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)

    def test_dry_run_with_no_state_displays_message_without_creating_state(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            output = io.StringIO()

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=self.make_snapshot(bot)
            ), mock.patch.object(bot, "send_lineworks_webhook") as webhook, mock.patch.object(
                bot.sys, "stdout", output
            ):
                self.assertEqual(bot.main(["--config", str(config_path), "--dry-run"]), 0)

            webhook.assert_not_called()
            self.assertFalse(state_path.exists())
            self.assertIn("田中貴金属", output.getvalue())

    def test_price_fetch_notification_log_does_not_claim_email_delivery(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            with mock.patch.object(
                bot, "fetch_html", side_effect=RuntimeError("Connection reset")
            ), mock.patch.object(bot, "send_lineworks_webhook"), mock.patch.object(
                bot.sys, "stderr", new_callable=io.StringIO
            ) as stderr:
                self.assertEqual(bot.main(["--config", str(config_path)]), 1)

        self.assertIn("価格取得エラー通知を送信しました", stderr.getvalue())
        self.assertNotIn("価格取得エラー通知メール", stderr.getvalue())

    def test_recovery_clears_daily_suppression_for_a_later_failure(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        state["error_alert_dates"]["price_fetch_error"] = "2026-07-13"
        state["pending_recovery_alerts"]["price_fetch_error"] = {
            "subject": "RE:TANAKA BOT: 価格取得エラー",
            "body": "価格取得に失敗しました。",
            "message_id": "<error-123@example.com>",
            "occurred_at": "2026-07-13 09:00:00",
        }

        with mock.patch.object(bot, "send_alert_email", return_value="<recovery-456@example.com>"):
            self.assertTrue(bot.maybe_send_recovery_alert({}, state, "price_fetch_error"))

        self.assertNotIn("price_fetch_error", state["error_alert_dates"])
        self.assertNotIn("price_fetch_error", state["pending_recovery_alerts"])

    def test_price_fetch_success_sends_recovery_once_and_clears_pending_alert(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["alert_email"] = "ops@example.com"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            snapshot = self.make_snapshot(bot)
            original_deliveries = {
                "line": {
                    "last_sent_date": "2026-07-11",
                    "last_sent_published_at": snapshot.published_at,
                },
                "lineworks": {"last_sent_published_at": snapshot.published_at},
            }
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "test_email_sent_at": "2026-07-13 09:00:00",
                        "last_observed_published_at": snapshot.published_at,
                        "deliveries": original_deliveries,
                        "pending_recovery_alerts": {
                            "price_fetch_error": {
                                "subject": "RE:TANAKA BOT: 価格取得エラー",
                                "body": "価格取得に失敗しました。",
                                "message_id": "<error-123@example.com>",
                                "occurred_at": "2026-07-13 09:00:00",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(bot, "send_alert_email", return_value="<recovery-456@example.com>") as send_email, mock.patch.object(
                bot, "send_line_messages"
            ) as send_line, mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks:
                self.assertEqual(bot.main(["--config", str(config_path)]), 0)
                self.assertEqual(bot.main(["--config", str(config_path)]), 0)

            send_email.assert_called_once()
            send_line.assert_not_called()
            send_lineworks.assert_not_called()
            saved_state = bot.load_state(str(state_path))
            self.assertEqual(saved_state["pending_recovery_alerts"], {})
            self.assertEqual(saved_state["deliveries"], original_deliveries)

    def test_lineworks_success_sends_threaded_recovery_for_previous_delivery_error(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["alert_email"] = "ops@example.com"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            snapshot = self.make_snapshot(bot)
            state = bot.make_empty_state()
            state["test_email_sent_at"] = "2026-07-13 09:00:00"
            state["last_observed_published_at"] = snapshot.published_at
            state["deliveries"]["line"]["last_sent_published_at"] = snapshot.published_at
            state["deliveries"]["lineworks"]["last_sent_published_at"] = "2026-07-11 09:29"
            state["pending_recovery_alerts"]["lineworks_delivery_error"] = {
                "transport": "email",
                "subject": "RE:TANAKA BOT: LINE WORKS送信エラー",
                "body": "LINE WORKS送信に失敗しました。",
                "message_id": "<error-123@example.com>",
                "occurred_at": "2026-07-13 09:00:00",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks, mock.patch.object(
                bot, "send_alert_email", return_value="<recovery-456@example.com>"
            ) as send_email:
                self.assertEqual(bot.main(["--config", str(config_path)]), 0)

            send_lineworks.assert_called_once()
            self.assertEqual(send_email.call_args[1]["in_reply_to"], "<error-123@example.com>")
            saved_state = bot.load_state(str(state_path))
            self.assertNotIn("lineworks_delivery_error", saved_state["pending_recovery_alerts"])

    def test_migrate_state_preserves_valid_pending_recovery_alerts(self) -> None:
        bot = load_module()

        state = bot.migrate_state(
            {
                "pending_recovery_alerts": {
                    "price_fetch_error": {
                        "subject": "RE:TANAKA BOT: 価格取得エラー",
                        "body": "価格取得に失敗しました。",
                        "message_id": "<error-123@example.com>",
                        "occurred_at": "2026-07-13 09:00:00",
                    },
                    "invalid": {"subject": "missing fields"},
                }
            }
        )

        self.assertEqual(
            state["pending_recovery_alerts"],
            {
                "price_fetch_error": {
                    "transport": "email",
                    "subject": "RE:TANAKA BOT: 価格取得エラー",
                    "body": "価格取得に失敗しました。",
                    "message_id": "<error-123@example.com>",
                    "occurred_at": "2026-07-13 09:00:00",
                }
            },
        )

    def test_observation_accepts_only_newer_publications(self) -> None:
        bot = load_module()
        state = bot.make_empty_state()
        state["last_observed_published_at"] = "2026-07-11 09:30"

        self.assertFalse(bot.is_new_observation(state, self.make_snapshot(bot, "2026-07-11 09:30")))
        self.assertFalse(bot.is_new_observation(state, self.make_snapshot(bot, "2026-07-11 09:29")))
        self.assertTrue(bot.is_new_observation(state, self.make_snapshot(bot, "2026-07-11 09:31")))

    def test_lineworks_only_test_does_not_mutate_state(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original_state = {
                "history": [{"published_at": "2026-07-10 09:30", "k24": 16000, "pt": 6000}],
                "line_last_sent_date": "2026-07-10",
                "lineworks_last_sent_published_at": "2026-07-10 09:30",
            }
            state_path.write_text(json.dumps(original_state), encoding="utf-8")
            config_path = self.write_delivery_config(temp_dir, state_path)

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=self.make_snapshot(bot)
            ), mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks, mock.patch.object(
                bot, "send_line_messages"
            ) as send_line:
                result = bot.main(["--config", str(config_path), "--test-lineworks-only"])

            self.assertEqual(result, 0)
            send_lineworks.assert_called_once()
            send_line.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)

    def test_second_process_skips_when_lock_is_held(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "runner.lock"
            with bot.acquire_process_lock(str(lock_path)) as first_acquired:
                self.assertTrue(first_acquired)
                with bot.acquire_process_lock(str(lock_path)) as second_acquired:
                    self.assertFalse(second_acquired)

    def test_lineworks_success_is_saved_when_line_delivery_fails(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            snapshot = self.make_snapshot(bot)
            state_path.write_text(
                json.dumps({"schema_version": 2, "last_observed_published_at": "2026-07-11 09:29"}),
                encoding="utf-8",
            )

            with mock.patch.object(bot, "datetime", wraps=bot.datetime) as mock_datetime:
                mock_datetime.now.return_value = bot.datetime(2026, 7, 11, 12, 0, 0)
                with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                    bot, "parse_snapshot", return_value=snapshot
                ), mock.patch.object(
                    bot, "send_line_messages", side_effect=RuntimeError("LINE unavailable")
                ), mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks:
                    result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 1)
            self.assertEqual(send_lineworks.call_count, 2)
            self.assertEqual(send_lineworks.call_args_list[0][0][1]["title"], "RE:TANAKA BOT: LINE送信エラー")
            self.assertEqual(send_lineworks.call_args_list[1][0][1]["title"], "RE:TANAKA価格")
            saved_state = bot.load_state(str(state_path))
            self.assertEqual(
                saved_state["deliveries"]["lineworks"]["last_sent_published_at"], snapshot.published_at
            )
            self.assertIsNone(saved_state["deliveries"]["line"]["last_sent_date"])

    def test_first_normal_run_bootstraps_observation_without_delivery(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            snapshot = self.make_snapshot(bot)

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(bot, "send_line_messages") as send_line, mock.patch.object(
                bot, "send_lineworks_webhook"
            ) as send_lineworks:
                result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 0)
            send_line.assert_not_called()
            send_lineworks.assert_not_called()
            saved_state = bot.load_state(str(state_path))
            self.assertEqual(saved_state["last_observed_published_at"], snapshot.published_at)

    def test_bootstrap_does_not_send_current_price_on_the_following_cron_run(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            snapshot = self.make_snapshot(bot)

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(bot, "send_line_messages") as send_line, mock.patch.object(
                bot, "send_lineworks_webhook"
            ) as send_lineworks:
                self.assertEqual(bot.main(["--config", str(config_path)]), 0)
                self.assertEqual(bot.main(["--config", str(config_path)]), 0)

            send_line.assert_not_called()
            send_lineworks.assert_not_called()

    def test_equal_observation_retries_only_failed_delivery_channel(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            snapshot = self.make_snapshot(bot)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "last_observed_published_at": snapshot.published_at,
                        "deliveries": {
                            "line": {"last_sent_published_at": snapshot.published_at, "last_sent_date": "2026-07-11"},
                            "lineworks": {"last_sent_published_at": None},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(bot, "send_line_messages") as send_line, mock.patch.object(
                bot, "send_lineworks_webhook"
            ) as send_lineworks:
                result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 0)
            send_line.assert_not_called()
            send_lineworks.assert_called_once()

    def test_older_observation_does_not_retry_pending_delivery(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            state_path.write_text(
                json.dumps({"schema_version": 2, "last_observed_published_at": "2026-07-11 09:31"}),
                encoding="utf-8",
            )

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=self.make_snapshot(bot, "2026-07-11 09:30")
            ), mock.patch.object(bot, "send_line_messages") as send_line, mock.patch.object(
                bot, "send_lineworks_webhook"
            ) as send_lineworks:
                result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 0)
            send_line.assert_not_called()
            send_lineworks.assert_not_called()

    def test_build_lineworks_payload_links_screenshot(self) -> None:
        bot = load_module()

        payload = bot.build_lineworks_payload(
            "price message",
            "https://example.com/price.png",
            "https://example.com/source",
        )

        self.assertEqual(payload["title"], "RE:TANAKA価格")
        self.assertEqual(payload["body"], {"text": "price message"})
        self.assertEqual(
            payload["button"],
            {"label": "価格表画像を見る", "url": "https://example.com/price.png"},
        )

    def test_lineworks_payload_removes_duplicate_body_heading(self) -> None:
        bot = load_module()
        payload = bot.build_lineworks_payload(
            "【田中貴金属 リサイクル価格】\n発表：2026年07月11日",
            None,
            "https://example.com/source",
        )

        self.assertEqual(payload["title"], "RE:TANAKA価格")
        self.assertEqual(payload["body"]["text"], "発表：2026年07月11日")

    def test_read_config_requires_lineworks_webhook(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "lineworks_webhook_url": "https://webhook.worksmobile.com/message/example",
                        "enable_section_screenshot": False,
                    }
                ),
                encoding="utf-8",
            )

            config = bot.read_config(str(config_path))

        self.assertEqual(
            config["lineworks_webhook_url"],
            "https://webhook.worksmobile.com/message/example",
        )

    def test_read_config_rejects_non_lineworks_url(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "lineworks_webhook_url": "https://example.com/webhook",
                        "enable_section_screenshot": False,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                bot.read_config(str(config_path))

    def test_save_state_uses_private_storage_and_durable_atomic_replace(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "storage" / "state.json"
            with mock.patch.object(bot.os, "fsync", wraps=os.fsync) as fsync, mock.patch.object(
                bot.os, "replace", wraps=os.replace
            ) as replace:
                bot.save_state(str(state_path), bot.make_empty_state())

            self.assertEqual(os.stat(str(state_path.parent)).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(str(state_path)).st_mode & 0o777, 0o600)
            self.assertGreaterEqual(fsync.call_count, 2)
            replace.assert_called_once()
            self.assertEqual(list(state_path.parent.glob("*.tmp")), [])

    def test_delivery_failures_send_independent_alerts_and_preserve_line_limit_alert(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            config_path = self.write_delivery_config(temp_dir, state_path)
            snapshot = self.make_snapshot(bot)
            state_path.write_text(
                json.dumps({"schema_version": 2, "last_observed_published_at": "2026-07-11 09:29"}),
                encoding="utf-8",
            )
            with mock.patch.object(bot, "datetime", wraps=bot.datetime) as mock_datetime:
                mock_datetime.now.return_value = bot.datetime(2026, 7, 11, 12, 0, 0)
                with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                    bot, "parse_snapshot", return_value=snapshot
                ), mock.patch.object(
                    bot, "send_line_messages", side_effect=RuntimeError("HTTP 429: monthly quota exceeded")
                ), mock.patch.object(
                    bot, "send_lineworks_webhook", side_effect=RuntimeError("HTTP 500")
                ), mock.patch.object(bot, "maybe_send_error_alert") as alert:
                    result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 1)
            self.assertEqual(
                [call[0][2] for call in alert.call_args_list],
                ["line_limit_error", "lineworks_delivery_error"],
            )

    def test_screenshot_is_reused_for_equal_publication_without_sleep_deletion(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "enable_section_screenshot": True,
                "screenshot_public_dir": temp_dir,
                "screenshot_public_base_url": "https://example.com/images",
                "screenshot_retention_hours": 72,
            }
            snapshot = self.make_snapshot(bot)
            with mock.patch.object(bot, "fetch_section_screenshot_bytes", return_value=b"\x89PNG first") as fetch:
                _, first_url = bot.capture_screenshot_if_enabled(config, snapshot)
                _, second_url = bot.capture_screenshot_if_enabled(config, snapshot)

            self.assertEqual(first_url, second_url)
            fetch.assert_called_once()
            self.assertNotIn("sleep", bot._run.__code__.co_names)

    def test_lineworks_only_test_includes_screenshot_link_without_normal_state_mutation(self) -> None:
        bot = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original_state = {"schema_version": 2, "last_observed_published_at": "2026-07-10 09:30"}
            state_path.write_text(json.dumps(original_state), encoding="utf-8")
            config_path = self.write_delivery_config(temp_dir, state_path)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config.update(
                {
                    "enable_section_screenshot": True,
                    "screenshotone_access_key": "test-key",
                    "screenshot_public_dir": str(Path(temp_dir) / "images"),
                    "screenshot_public_base_url": "https://example.com/images",
                }
            )
            config_path.write_text(json.dumps(raw_config), encoding="utf-8")

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=self.make_snapshot(bot)
            ), mock.patch.object(
                bot, "capture_screenshot_if_enabled", return_value=("/tmp/image.png", "https://example.com/image.png")
            ), mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks:
                result = bot.main(["--config", str(config_path), "--test-lineworks-only"])

            self.assertEqual(result, 0)
            self.assertEqual(send_lineworks.call_args[0][1]["button"]["url"], "https://example.com/image.png")
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)


if __name__ == "__main__":
    unittest.main()
