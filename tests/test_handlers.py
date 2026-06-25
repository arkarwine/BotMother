import unittest
import tempfile
from pathlib import Path

from botmother.ai import AIDecision, AIQuestion
from botmother.db import Database
from botmother.handlers import (
    USER_LOCALE_CACHE,
    apply_bot_template,
    chunk_text,
    compact_bot_label,
    format_ai_questions,
    format_bot_list,
    format_logs,
    help_category_text,
    locale_for_update,
    parse_ask_args,
    parse_bot_id,
    parse_tail_args,
    _remember_user,
)


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class FakeUser:
    id = 42
    username = "alice"
    first_name = "Alice"
    last_name = "Tester"
    language_code = "en"


class FakeChat:
    id = 100


class SlotUpdate:
    __slots__ = ("effective_user", "effective_chat")

    def __init__(self):
        self.effective_user = FakeUser()
        self.effective_chat = FakeChat()


class HandlerHelperTests(unittest.TestCase):
    def test_parse_bot_id(self):
        self.assertEqual(parse_bot_id(["12"]), 12)
        self.assertIsNone(parse_bot_id([]))
        self.assertIsNone(parse_bot_id(["abc"]))
        self.assertIsNone(parse_bot_id(["0"]))

    def test_parse_tail_args_defaults_limit(self):
        self.assertEqual(parse_tail_args(["12"]), (12, 30, None))

    def test_parse_tail_args_accepts_limit(self):
        self.assertEqual(parse_tail_args(["12", "50"]), (12, 50, None))

    def test_parse_tail_args_clamps_limit(self):
        self.assertEqual(parse_tail_args(["12", "500"]), (12, 100, None))

    def test_parse_tail_args_requires_bot_id(self):
        bot_id, limit, error = parse_tail_args([])
        self.assertIsNone(bot_id)
        self.assertEqual(limit, 30)
        self.assertIn("Logs", error)

    def test_parse_tail_args_rejects_bad_limit(self):
        bot_id, limit, error = parse_tail_args(["12", "nope"])
        self.assertEqual(bot_id, 12)
        self.assertEqual(limit, 30)
        self.assertIn("number", error)

    def test_parse_ask_args_accepts_inline_question(self):
        bot_id, question, error = parse_ask_args(["12", "what", "does", "it", "do?"])
        self.assertEqual(bot_id, 12)
        self.assertEqual(question, "what does it do?")
        self.assertIsNone(error)

    def test_parse_ask_args_allows_prompt_flow(self):
        bot_id, question, error = parse_ask_args(["12"])
        self.assertEqual(bot_id, 12)
        self.assertEqual(question, "")
        self.assertIsNone(error)

    def test_parse_ask_args_requires_bot_id(self):
        bot_id, question, error = parse_ask_args([])
        self.assertIsNone(bot_id)
        self.assertEqual(question, "")
        self.assertIn("Ask Bot", error)

    def test_format_empty_bot_list(self):
        self.assertIn("New Bot", format_bot_list([]))

    def test_help_category_text(self):
        self.assertIn("Create", help_category_text("create"))
        self.assertIn("Buttons", help_category_text("fallback"))
        self.assertIn("BotMother", help_category_text("unknown"))

    def test_format_bot_list_uses_bot_username(self):
        text = format_bot_list(
            [
                FakeRow(
                    id=3,
                    status="running",
                    name="Echo",
                    owner_username="alice",
                    bot_username="echo_bot",
                )
            ]
        )
        self.assertNotIn("#3", text)
        self.assertIn("@echo_bot", text)
        self.assertNotIn("@alice", text)
        self.assertIn("running", text)

    def test_compact_bot_label_truncates_long_names(self):
        text = compact_bot_label(
            FakeRow(
                id=3, status="running", name="Very long bot name that should shrink"
            )
        )
        self.assertNotIn("#3", text)
        self.assertIn("🟢", text)
        self.assertLessEqual(len(text), 38)

    def test_format_logs(self):
        text = format_logs([FakeRow(stream="stderr", line="boom")])
        self.assertEqual(text, "[stderr] boom")

    def test_chunk_text(self):
        self.assertEqual(chunk_text("abcdef", 2), ["ab", "cd", "ef"])
        self.assertEqual(chunk_text("", 2), [""])

    def test_apply_bot_template_adds_selected_mode(self):
        text = apply_bot_template("sell shoes", "shop")

        self.assertIn("Mode: e-commerce shop bot", text)
        self.assertIn("sell shoes", text)

    def test_apply_bot_template_defaults_to_other(self):
        text = apply_bot_template("custom workflow", "unknown")

        self.assertIn("Mode: custom bot", text)
        self.assertIn("custom workflow", text)

    def test_format_ai_questions_appends_structured_questions_after_message(self):
        decision = AIDecision(
            "questions",
            "Admin ID တွေ ပေးပါ။ KPay ကို ဖုန်းနံပါတ်နဲ့ပြမလား၊ QR နဲ့ပြမလား?",
            (
                AIQuestion("admin_ids", "Admin IDs?", ("123456789",)),
                AIQuestion("payment", "Payment display?", ("Phone only", "QR")),
            ),
            None,
            (),
        )

        text = format_ai_questions(decision)

        self.assertIn(decision.message, text)
        self.assertIn("Admin IDs?", text)
        self.assertIn("Payment display?", text)
        self.assertNotIn("Suggestions:", text)
        self.assertNotIn("Follow-up", text)

    def test_format_ai_questions_never_drops_questions_for_vague_preamble(self):
        decision = AIDecision(
            "questions",
            "I will create a complete E-commerce bot for you. Could you please clarify a few details?",
            (
                AIQuestion("product_types", "What product categories will your store sell?", ()),
                AIQuestion("admin_ids", "Which Telegram user IDs should receive order notifications?", ()),
            ),
            None,
            (),
        )

        text = format_ai_questions(decision)

        self.assertIn("Could you please clarify a few details?", text)
        self.assertIn("What product categories will your store sell?", text)
        self.assertIn("Which Telegram user IDs should receive order notifications?", text)

    def test_format_ai_questions_never_drops_questions_for_burmese_preamble(self):
        decision = AIDecision(
            "questions",
            "အွန်လိုင်းစတိုးအတွက် စနစ်တကျပြင်ဆင်ပေးပါမည်။ အောက်ပါအချက်အလက်များကို သိရှိလိုပါသည်။",
            (
                AIQuestion("products", "မည်သည့်ပစ္စည်းအမျိုးအစားများကို ရောင်းချမည်နည်း?", ()),
            ),
            None,
            (),
        )

        text = format_ai_questions(decision)

        self.assertIn("အောက်ပါအချက်အလက်များကို သိရှိလိုပါသည်", text)
        self.assertIn("မည်သည့်ပစ္စည်းအမျိုးအစားများကို ရောင်းချမည်နည်း?", text)

    def test_remember_user_uses_locale_cache_for_slot_restricted_updates(self):
        USER_LOCALE_CACHE.clear()
        update = SlotUpdate()
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            _remember_user(db, update)
            db.update_user_locale(FakeUser.id, "my")

            user_id = _remember_user(db, update)

        self.assertEqual(user_id, FakeUser.id)
        self.assertEqual(locale_for_update(update), "my")


if __name__ == "__main__":
    unittest.main()
