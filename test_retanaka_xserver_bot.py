from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parent / "xserver" / "retanaka_xserver_bot.py"
    spec = importlib.util.spec_from_file_location("retanaka_xserver_bot", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetanakaXserverBotTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
