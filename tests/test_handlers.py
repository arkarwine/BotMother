import unittest

from botmother.handlers import chunk_text, format_bot_list, format_logs, parse_bot_id, parse_tail_args


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

    def test_format_empty_bot_list(self):
        self.assertIn("/newbot", format_bot_list([]))

    def test_format_bot_list(self):
        text = format_bot_list([FakeRow(id=3, status="running", name="Echo")])
        self.assertIn("#3", text)
        self.assertIn("running", text)

    def test_format_logs(self):
        text = format_logs([FakeRow(stream="stderr", line="boom")])
        self.assertEqual(text, "[stderr] boom")

    def test_chunk_text(self):
        self.assertEqual(chunk_text("abcdef", 2), ["ab", "cd", "ef"])
        self.assertEqual(chunk_text("", 2), [""])


if __name__ == "__main__":
    unittest.main()
