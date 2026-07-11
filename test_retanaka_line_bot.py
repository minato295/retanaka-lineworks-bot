from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parent / "retanaka_line_bot.py"
    spec = importlib.util.spec_from_file_location("retanaka_line_bot", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetanakaLineBotTests(unittest.TestCase):
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
        </body></html>
        """

        snapshot = bot.parse_snapshot(html)

        self.assertEqual(snapshot.published_at, "2026-02-03 09:30")
        self.assertEqual(snapshot.k24, 25120)
        self.assertEqual(snapshot.pt, 10610)

    def test_build_message_formats_day_over_day(self) -> None:
        bot = load_module()

        current = bot.PriceSnapshot(
            published_at="2026-02-03 09:30",
            k24=25120,
            pt=10610,
            fetched_at="2026-02-03T00:30:00+00:00",
        )
        previous = bot.PriceSnapshot(
            published_at="2026-02-02 09:30",
            k24=25000,
            pt=10650,
            fetched_at="2026-02-02T00:30:00+00:00",
        )

        message = bot.build_message(current, previous)

        self.assertIn("発表: 2026年02月03日(火)09時30分", message)
        self.assertIn("K24特定品: 25,120円/g（前日比 +120円）", message)
        self.assertIn("Pt特定品: 10,610円/g（前日比 -40円）", message)

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


if __name__ == "__main__":
    unittest.main()
