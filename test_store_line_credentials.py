import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    path = Path(__file__).resolve().parent / "scripts" / "store_line_credentials_in_keychain.py"
    spec = importlib.util.spec_from_file_location("store_line_credentials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreLineCredentialsTests(unittest.TestCase):
    def test_stores_token_and_channel_secret_without_printing_them(self):
        module = load_module()
        token = "secret-channel-token"
        channel_secret = "secret-channel-secret"
        output = io.StringIO()

        with mock.patch.object(module.getpass, "getpass", side_effect=[token, channel_secret]), mock.patch.object(
            module.subprocess, "run"
        ) as run, contextlib.redirect_stdout(output):
            module.main(["setup"])

        self.assertEqual(run.call_count, 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("RE:TANAKA LINE channel access token", commands[0])
        self.assertIn("RE:TANAKA LINE channel secret", commands[1])
        self.assertIn(token, commands[0])
        self.assertIn(channel_secret, commands[1])
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(channel_secret, output.getvalue())

    def test_rejects_invalid_group_id_before_keychain_write(self):
        module = load_module()
        with mock.patch.object(module.getpass, "getpass", side_effect=["token", "invalid"]), mock.patch.object(
            module.subprocess, "run"
        ) as run:
            with self.assertRaises(ValueError):
                module.main(["group"])
        run.assert_not_called()

    def test_stores_valid_group_id(self):
        module = load_module()
        group_id = "C" + "1" * 32
        with mock.patch.object(module.getpass, "getpass", return_value=group_id), mock.patch.object(
            module.subprocess, "run"
        ) as run:
            module.main(["group"])
        self.assertIn("RE:TANAKA LINE group ID", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
