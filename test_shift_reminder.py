import tempfile
import io
import unittest
from pathlib import Path
from unittest import mock

from test_retanaka_xserver_bot import load_module


class ShiftReminderTests(unittest.TestCase):
    def test_user_ids_are_redacted_from_service_errors(self):
        bot = load_module()
        user = "U" + "1" * 32
        config = {"shift_reminder": {"users": {"user1": user}}}
        self.assertNotIn(user, bot.redact_sensitive_text("invalid userId=" + user, config))

    def test_already_accepted_retry_is_success_but_other_conflicts_fail(self):
        bot = load_module()
        accepted = bot.HTTPError(bot.LINE_PUSH_URL, 409, "Conflict",
            {"x-line-accepted-request-id": "accepted"}, io.BytesIO(b"{}"))
        with mock.patch.object(bot, "urlopen", side_effect=accepted) as request:
            bot.send_line_messages("token", "group", [{"type": "textV2", "text": "test"}], retry_key="key")
            self.assertEqual(request.call_args[0][0].get_header("X-line-retry-key"), "key")
        conflict = bot.HTTPError(bot.LINE_PUSH_URL, 409, "Conflict", {}, io.BytesIO(b"{}"))
        with mock.patch.object(bot, "urlopen", side_effect=conflict):
            with self.assertRaises(RuntimeError):
                bot.send_line_messages("token", "group", [], retry_key="key")

    def test_schedule_uses_japan_time_and_retry_key_survives_restart(self):
        bot = load_module()
        config = {"line_channel_access_token": "token", "line_group_id": "group",
            "shift_reminder": {"enabled": True, "users": {
                "user1": "U" + "1" * 32, "user2": "U" + "2" * 32}}}
        with mock.patch.object(bot, "send_line_messages", side_effect=RuntimeError("timeout")) as send:
            for minute in (0, 1):
                with self.assertRaises(RuntimeError):
                    bot.maybe_send_shift_reminder(config, bot.make_empty_state(),
                        bot.datetime(2026, 11, 25, 0, minute, tzinfo=bot.timezone.utc))
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args_list[0][1]["retry_key"], send.call_args_list[1][1]["retry_key"])
            self.assertFalse(bot.maybe_send_shift_reminder(config, bot.make_empty_state(),
                bot.datetime(2026, 11, 25, 9, tzinfo=bot.timezone.utc)))

    def test_monthly_reminder_and_price_state_are_independent(self):
        bot = load_module()
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "line_channel_access_token": "test-token",
                "line_group_id": "test-group",
                "state_file": str(Path(directory) / "state.json"),
                "shift_reminder": {"enabled": True, "users": {
                    "user1": "U" + "1" * 32, "user2": "U" + "2" * 32,
                }},
            }
            state = bot.make_empty_state()
            original = dict(state["deliveries"]["line"])
            with mock.patch.object(bot, "send_line_messages") as send:
                self.assertFalse(bot.maybe_send_shift_reminder(config, state, bot.datetime(2026, 11, 25, 8, 59)))
                self.assertTrue(bot.maybe_send_shift_reminder(config, state, bot.datetime(2026, 11, 25, 9, 0)))
                self.assertFalse(bot.maybe_send_shift_reminder(config, state, bot.datetime(2026, 11, 25, 9, 1)))
                send.assert_called_once()
                message = send.call_args[0][2][0]
                self.assertEqual(message["type"], "textV2")
                self.assertEqual(message["substitution"]["user1"]["mentionee"]["userId"], "U" + "1" * 32)
                self.assertEqual(state["deliveries"]["line"], original)
                restored = bot.load_state(config["state_file"])
                self.assertFalse(bot.maybe_send_shift_reminder(config, restored, bot.datetime(2026, 11, 25, 9, 2)))
                self.assertTrue(bot.maybe_send_shift_reminder(config, restored, bot.datetime(2026, 12, 25, 9, 0)))

    def test_failed_delivery_is_not_marked_sent(self):
        bot = load_module()
        config = {"line_channel_access_token": "token", "line_group_id": "group",
            "shift_reminder": {"enabled": True, "users": {
                "user1": "U" + "1" * 32, "user2": "U" + "2" * 32}}}
        state = bot.make_empty_state()
        with mock.patch.object(bot, "send_line_messages", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                bot.maybe_send_shift_reminder(config, state, bot.datetime(2026, 11, 25, 9, 0))
        self.assertIsNone(state["shift_reminder_month"])

    def test_disabled_reminder_never_sends(self):
        bot = load_module()
        with mock.patch.object(bot, "send_line_messages") as send:
            self.assertFalse(bot.maybe_send_shift_reminder({}, bot.make_empty_state(), bot.datetime(2026, 11, 25, 9)))
            send.assert_not_called()
