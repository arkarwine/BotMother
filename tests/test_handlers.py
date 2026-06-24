import unittest

from botmother.ai import AIDecision, AIQuestion
from botmother.handlers import (
    chunk_text,
    format_ai_questions,
    format_bot_list,
    format_logs,
    compact_bot_label,
    help_category_text,
    parse_ask_args,
    parse_bot_id,
    parse_tail_args,
)


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


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
        self.assertIn("/tail", error)

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
        self.assertIn("/ask", error)

    def test_format_empty_bot_list(self):
        self.assertIn("New Bot", format_bot_list([]))

    def test_help_category_text(self):
        self.assertIn("Create", help_category_text("create"))
        self.assertIn("Buttons", help_category_text("fallback"))
        self.assertIn("BotMother", help_category_text("unknown"))

    def test_format_bot_list(self):
        text = format_bot_list([FakeRow(id=3, status="running", name="Echo")])
        self.assertIn("#3", text)
        self.assertIn("running", text)

    def test_compact_bot_label_truncates_long_names(self):
        text = compact_bot_label(FakeRow(id=3, status="running", name="Very long bot name that should shrink"))
        self.assertIn("#3", text)
        self.assertIn("🟢", text)
        self.assertLessEqual(len(text), 32)

    def test_format_logs(self):
        text = format_logs([FakeRow(stream="stderr", line="boom")])
        self.assertEqual(text, "[stderr] boom")

    def test_chunk_text(self):
        self.assertEqual(chunk_text("abcdef", 2), ["ab", "cd", "ef"])
        self.assertEqual(chunk_text("", 2), [""])

    def test_format_ai_questions_uses_ai_message_verbatim(self):
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

        self.assertEqual(text, decision.message)
        self.assertNotIn("Suggestions:", text)
        self.assertNotIn("Follow-up", text)


if __name__ == "__main__":
    unittest.main()
