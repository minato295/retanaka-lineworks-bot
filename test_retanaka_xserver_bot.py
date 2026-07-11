import importlib.util
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

    def test_legacy_lineworks_state_migrates_to_schema_v2_without_marking_line_sent(self) -> None:
        bot = load_module()

        state = bot.migrate_state(
            {
                "last_sent_published_at": "2026-07-11 09:30",
                "last_attempt_date": "2026-07-11",
                "error_alert_dates": {"price_fetch_error": "2026-07-11"},
                "test_email_sent_at": "2026-07-11 09:00:00",
            }
        )

        self.assertEqual(state["schema_version"], 2)
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

        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["deliveries"]["line"]["last_sent_date"], "2026-07-10")
        self.assertIsNone(state["deliveries"]["lineworks"]["last_sent_published_at"])

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

            with mock.patch.object(bot, "fetch_html", return_value="ignored"), mock.patch.object(
                bot, "parse_snapshot", return_value=snapshot
            ), mock.patch.object(
                bot, "send_line_messages", side_effect=RuntimeError("LINE unavailable")
            ), mock.patch.object(bot, "send_lineworks_webhook") as send_lineworks:
                result = bot.main(["--config", str(config_path)])

            self.assertEqual(result, 1)
            send_lineworks.assert_called_once()
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
