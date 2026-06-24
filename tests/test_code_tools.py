import unittest

from botmother.code_tools import extract_python_code, validate_generated_code
from botmother.tokens import is_valid_telegram_token, mask_token


class CodeToolsTests(unittest.TestCase):
    def test_extract_python_code_from_fence(self):
        raw = "```python\nprint('hello')\n```"
        self.assertEqual(extract_python_code(raw), "print('hello')")

    def test_validate_allows_simple_bot_code(self):
        result = validate_generated_code(
            "import os\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "def main():\n"
            "    application = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "print(os.environ['BOT_TOKEN'])"
        )
        self.assertTrue(result.ok, result.error)

    def test_validate_requires_global_error_handler(self):
        result = validate_generated_code("import os\nprint(os.environ['BOT_TOKEN'])")
        self.assertFalse(result.ok)
        self.assertIn("add_error_handler", result.error)

    def test_validate_rejects_legacy_markdown_parse_mode(self):
        result = validate_generated_code(
            "from telegram.constants import ParseMode\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "def main():\n"
            "    application = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "mode = ParseMode.MARKDOWN\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("legacy", result.error)

    def test_validate_rejects_legacy_markdown_string_parse_mode(self):
        result = validate_generated_code(
            "async def error_handler(update, context):\n"
            "    pass\n"
            "def main():\n"
            "    application = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "    application.bot.send_message(1, 'hi', parse_mode='Markdown')\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("legacy", result.error)

    def test_validate_rejects_syntax_error(self):
        result = validate_generated_code("def nope(:\n    pass")
        self.assertFalse(result.ok)
        self.assertIn("Syntax error", result.error)

    def test_validate_rejects_denied_import(self):
        result = validate_generated_code("import subprocess\nprint('x')")
        self.assertFalse(result.ok)
        self.assertIn("Denied import", result.error)

    def test_validate_rejects_aliased_os_system(self):
        result = validate_generated_code("import os as operating\noperating.system('whoami')")
        self.assertFalse(result.ok)
        self.assertIn("os.system", result.error)

    def test_validate_rejects_from_os_remove(self):
        result = validate_generated_code("from os import remove\nremove('x')")
        self.assertFalse(result.ok)
        self.assertIn("from os import remove", result.error)

    def test_validate_rejects_eval(self):
        result = validate_generated_code("eval('1 + 1')")
        self.assertFalse(result.ok)
        self.assertIn("eval", result.error)

    def test_token_validation_and_masking(self):
        token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_123"
        self.assertTrue(is_valid_telegram_token(token))
        self.assertEqual(mask_token(token), "123456:ABCD..._123")


if __name__ == "__main__":
    unittest.main()
