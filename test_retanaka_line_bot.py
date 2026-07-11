from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    module_path = Path(__file__).resolve().parent / "retanaka_line_bot.py"
    spec = importlib.util.spec_from_file_location("retanaka_line_bot", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetanakaLineBotTests(unittest.TestCase):
    def test_env_example_lists_all_delivery_settings_without_values(self) -> None:
        env_path = Path(__file__).resolve().parent / ".retanaka.env.example"
        settings = {}
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                settings[key] = value

        self.assertEqual(
            settings,
            {
                "LINE_CHANNEL_ACCESS_TOKEN": "",
                "LINE_GROUP_ID": "",
                "LINEWORKS_WEBHOOK_URL": "",
            },
        )

    def test_parse_snapshot_extracts_prices_and_time(self) -> None:
        bot = load_module()
        html = """
        <html><body>
          <h1>リサイクル価格</h1>
          <p>リサイクル価格 2026年02月03日 09:30発表</p>
          <table>
            <tr><td>K24特定品</td><td>¥25,120</td></tr>
            <tr><td>Pt特定品</td><td>¥10,610</td></tr>
          </table>
          <table>
            <caption>銀製品</caption>
            <tr><td>1000（999）</td><td>¥328</td></tr>
          </table>
        </body></html>
        """

        try:
            snapshot = bot.parse_snapshot(html)
        except ValueError as exc:
            self.fail(str(exc))

        self.assertEqual(snapshot.published_at, "2026-02-03 09:30")
        self.assertEqual(snapshot.k24, 25120)
        self.assertEqual(snapshot.pt, 10610)
        self.assertEqual(snapshot.silver_999, 328)

    def test_parse_snapshot_extracts_silver_from_silver_section(self) -> None:
        bot = load_module()
        html = """
        <html><body>
          <p>リサイクル価格 2026年02月03日 09:30発表</p>
          <h2>貴金属価格</h2>
          <table>
            <tr><td>K24特定品</td><td>¥25,120</td></tr>
            <tr><td>Pt特定品</td><td>¥10,610</td></tr>
            <tr><td>Ag999</td><td>¥99,999</td></tr>
          </table>
          <table>
            <caption>銀製品</caption>
            <tr><td>1000（999）</td><td>¥328</td></tr>
          </table>
        </body></html>
        """

        try:
            snapshot = bot.parse_snapshot(html)
        except ValueError as exc:
            self.fail(str(exc))

        self.assertEqual(snapshot.silver_999, 328)

    def test_build_message_formats_day_over_day(self) -> None:
        bot = load_module()

        current = bot.PriceSnapshot(
            published_at="2026-02-03 09:30",
            k24=25120,
            pt=10610,
            fetched_at="2026-02-03T00:30:00+00:00",
            silver_999=328,
        )
        previous = bot.PriceSnapshot(
            published_at="2026-02-02 09:30",
            k24=25000,
            pt=10650,
            fetched_at="2026-02-02T00:30:00+00:00",
            silver_999=325,
        )

        message = bot.build_message(current, previous)

        self.assertIn("発表: 2026年02月03日(火)09時30分", message)
        self.assertIn("K24特定品: 25,120円/g（前日比 +120円）", message)
        self.assertIn("Pt特定品: 10,610円/g（前日比 -40円）", message)
        self.assertIn("Ag999: 328円/g（前日比 +3円）", message)

    def test_get_previous_snapshot_picks_latest_older_record(self) -> None:
        bot = load_module()

        state = {
            "history": [
                {"published_at": "2026-02-01 09:30", "k24": 24900, "pt": 10400},
                {"published_at": "2026-02-03 09:30", "k24": 25120, "pt": 10610},
                {"published_at": "2026-02-02 09:30", "k24": 25000, "pt": 10650},
            ]
        }

        previous = bot.get_previous_snapshot(state, "2026-02-03 09:30")

        self.assertIsNotNone(previous)
        self.assertEqual(previous.published_at, "2026-02-02 09:30")
        self.assertEqual(previous.k24, 25000)
        self.assertEqual(previous.pt, 10650)

    def test_build_lineworks_payload_uses_incoming_webhook_shape(self) -> None:
        bot = load_module()

        payload = bot.build_lineworks_payload("price message", "https://example.com/price")

        self.assertEqual(payload["title"], "RE:TANAKA価格")
        self.assertEqual(payload["body"], {"text": "price message"})
        self.assertEqual(
            payload["button"],
            {"label": "価格ページを開く", "url": "https://example.com/price"},
        )

    def test_build_line_messages_contains_text_and_image(self) -> None:
        bot = load_module()

        messages = bot.build_line_messages(
            "price message",
            "https://example.com/price.png",
        )

        self.assertEqual(
            messages,
            [
                {"type": "text", "text": "price message"},
                {
                    "type": "image",
                    "originalContentUrl": "https://example.com/price.png",
                    "previewImageUrl": "https://example.com/price.png",
                },
            ],
        )

    def test_main_delivers_first_update_to_both_channels_and_persists_channel_state(self) -> None:
        bot = load_module()
        current = bot.PriceSnapshot(
            published_at="2026-02-03 09:30",
            k24=25120,
            pt=10610,
            fetched_at="2026-02-03T00:30:00+00:00",
            silver_999=328,
        )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            line_sender = mock.Mock()
            lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                        "LINE_GROUP_ID": "group-id",
                        "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                    },
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                mock.patch.object(bot, "parse_snapshot", return_value=current),
                mock.patch.object(bot, "send_line_message", line_sender, create=True),
                mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
            ):
                result = bot.main(["--env-file", str(Path(directory) / "missing.env"), "--state-path", str(state_path)])

            message = bot.build_message(current, None)
            self.assertEqual(result, 0)
            line_sender.assert_called_once_with("line-token", "group-id", message)
            lineworks_sender.assert_called_once_with(
                "https://webhook.worksmobile.com/message/test", message
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["deliveries"],
                {
                    "line": {
                        "last_sent_published_at": "2026-02-03 09:30",
                        "last_sent_date": "2026-02-03",
                    },
                    "lineworks": {"last_sent_published_at": "2026-02-03 09:30"},
                },
            )

    def test_main_persists_lineworks_success_when_line_fails_then_retries_only_line(self) -> None:
        bot = load_module()
        current = bot.PriceSnapshot(
            published_at="2026-02-03 09:30",
            k24=25120,
            pt=10610,
            fetched_at="2026-02-03T00:30:00+00:00",
            silver_999=328,
        )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            common_args = ["--env-file", str(Path(directory) / "missing.env"), "--state-path", str(state_path)]
            lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                        "LINE_GROUP_ID": "group-id",
                        "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                    },
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                mock.patch.object(bot, "parse_snapshot", return_value=current),
                mock.patch.object(bot, "send_line_message", side_effect=RuntimeError("LINE unavailable"), create=True),
                mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
            ):
                self.assertEqual(bot.main(common_args), 1)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("last_sent_published_at", state["deliveries"]["line"])
            self.assertEqual(
                state["deliveries"]["lineworks"],
                {"last_sent_published_at": "2026-02-03 09:30"},
            )
            lineworks_sender.assert_called_once()

            retry_line_sender = mock.Mock()
            retry_lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                        "LINE_GROUP_ID": "group-id",
                        "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                    },
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                mock.patch.object(bot, "parse_snapshot", return_value=current),
                mock.patch.object(bot, "send_line_message", retry_line_sender, create=True),
                mock.patch.object(bot, "send_lineworks_message", retry_lineworks_sender),
            ):
                self.assertEqual(bot.main(common_args), 0)

            retry_line_sender.assert_called_once()
            retry_lineworks_sender.assert_not_called()

    def test_main_skips_line_after_the_first_update_of_a_published_day(self) -> None:
        bot = load_module()
        current = bot.PriceSnapshot(
            published_at="2026-02-03 15:00",
            k24=25150,
            pt=10620,
            fetched_at="2026-02-03T06:00:00+00:00",
            silver_999=329,
        )
        state = {
            "history": [],
            "deliveries": {
                "line": {
                    "last_sent_published_at": "2026-02-03 09:30",
                    "last_sent_date": "2026-02-03",
                },
                "lineworks": {"last_sent_published_at": "2026-02-03 09:30"},
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            line_sender = mock.Mock()
            lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                        "LINE_GROUP_ID": "group-id",
                        "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                    },
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                mock.patch.object(bot, "parse_snapshot", return_value=current),
                mock.patch.object(bot, "send_line_message", line_sender, create=True),
                mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
            ):
                self.assertEqual(
                    bot.main(["--env-file", str(Path(directory) / "missing.env"), "--state-path", str(state_path)]),
                    0,
                )

            line_sender.assert_not_called()
            lineworks_sender.assert_called_once()

    def test_main_does_not_send_or_regress_delivery_state_for_equal_or_older_timestamp(self) -> None:
        bot = load_module()
        state = {
            "history": [],
            "deliveries": {
                "line": {
                    "last_sent_published_at": "2026-02-04 09:30",
                    "last_sent_date": "2026-02-04",
                },
                "lineworks": {"last_sent_published_at": "2026-02-04 09:30"},
            },
        }

        for published_at in ("2026-02-04 09:30", "2026-02-03 15:00"):
            with self.subTest(published_at=published_at), tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                current = bot.PriceSnapshot(published_at, 25120, 10610, "2026-02-03T00:30:00+00:00", 328)
                line_sender = mock.Mock()
                lineworks_sender = mock.Mock()
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                            "LINE_GROUP_ID": "group-id",
                            "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                        },
                        clear=True,
                    ),
                    mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                    mock.patch.object(bot, "parse_snapshot", return_value=current),
                    mock.patch.object(bot, "send_line_message", line_sender, create=True),
                    mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
                ):
                    self.assertEqual(
                        bot.main(["--env-file", str(Path(directory) / "missing.env"), "--state-path", str(state_path)]),
                        0,
                    )

                line_sender.assert_not_called()
                lineworks_sender.assert_not_called()
                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["deliveries"], state["deliveries"])

    def test_force_send_overrides_equal_timestamp_suppression_for_both_channels(self) -> None:
        bot = load_module()
        current = bot.PriceSnapshot("2026-02-03 09:30", 25120, 10610, "2026-02-03T00:30:00+00:00", 328)
        state = {
            "history": [],
            "deliveries": {
                "line": {
                    "last_sent_published_at": "2026-02-03 09:30",
                    "last_sent_date": "2026-02-03",
                },
                "lineworks": {"last_sent_published_at": "2026-02-03 09:30"},
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            line_sender = mock.Mock()
            lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LINE_CHANNEL_ACCESS_TOKEN": "line-token",
                        "LINE_GROUP_ID": "group-id",
                        "LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test",
                    },
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", return_value="<html></html>"),
                mock.patch.object(bot, "parse_snapshot", return_value=current),
                mock.patch.object(bot, "send_line_message", line_sender, create=True),
                mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
            ):
                self.assertEqual(
                    bot.main(
                        [
                            "--force-send",
                            "--env-file",
                            str(Path(directory) / "missing.env"),
                            "--state-path",
                            str(state_path),
                        ]
                    ),
                    0,
                )

            line_sender.assert_called_once()
            lineworks_sender.assert_called_once()

    def test_test_lineworks_only_posts_without_fetching_or_mutating_state(self) -> None:
        bot = load_module()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            original_state = '{"unchanged": true}\n'
            state_path.write_text(original_state, encoding="utf-8")
            line_sender = mock.Mock()
            lineworks_sender = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {"LINEWORKS_WEBHOOK_URL": "https://webhook.worksmobile.com/message/test"},
                    clear=True,
                ),
                mock.patch.object(bot, "fetch_html", side_effect=AssertionError("must not fetch")),
                mock.patch.object(bot, "send_line_message", line_sender, create=True),
                mock.patch.object(bot, "send_lineworks_message", lineworks_sender),
            ):
                try:
                    result = bot.main(
                        [
                            "--test-lineworks-only",
                            "--env-file",
                            str(Path(directory) / "missing.env"),
                            "--state-path",
                            str(state_path),
                        ]
                    )
                except SystemExit as exc:
                    self.fail(f"--test-lineworks-only was not accepted: {exc}")

            self.assertEqual(result, 0)
            line_sender.assert_not_called()
            lineworks_sender.assert_called_once_with(
                "https://webhook.worksmobile.com/message/test",
                "RE:TANAKA LINE WORKS テスト送信",
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_save_state_removes_temporary_file_when_atomic_replace_fails(self) -> None:
        bot = load_module()

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text('{"before": true}\n', encoding="utf-8")
            with mock.patch.object(bot.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    bot.save_state(state_path, {"after": True})

            self.assertEqual(state_path.read_text(encoding="utf-8"), '{"before": true}\n')
            self.assertEqual(list(Path(directory).glob("state.json*")), [state_path])

    @unittest.skipIf(sys.platform == "win32", "fcntl locks are not available on Windows")
    def test_state_lock_declines_a_second_nonblocking_holder(self) -> None:
        bot = load_module()

        try:
            lock = bot.state_lock
        except AttributeError:
            self.fail("state_lock is not implemented")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with lock(state_path) as first_acquired:
                self.assertTrue(first_acquired)
                with lock(state_path) as second_acquired:
                    self.assertFalse(second_acquired)


if __name__ == "__main__":
    unittest.main()
