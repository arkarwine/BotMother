import unittest

from botmother.handlers import format_bot_list, format_logs, parse_bot_id


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


class HandlerHelperTests(unittest.TestCase):
    def test_parse_bot_id(self):
        self.assertEqual(parse_bot_id(["12"]), 12)
        self.assertIsNone(parse_bot_id([]))
        self.assertIsNone(parse_bot_id(["abc"]))
        self.assertIsNone(parse_bot_id(["0"]))

    def test_format_empty_bot_list(self):
        self.assertIn("/newbot", format_bot_list([]))

    def test_format_bot_list(self):
        text = format_bot_list([FakeRow(id=3, status="running", name="Echo")])
        self.assertIn("#3", text)
        self.assertIn("running", text)

    def test_format_logs(self):
        text = format_logs([FakeRow(stream="stderr", line="boom")])
        self.assertEqual(text, "[stderr] boom")


if __name__ == "__main__":
    unittest.main()

