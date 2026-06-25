import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from botmother.code_tools import extract_python_code, run_mypy_static_check, validate_generated_code
from botmother.tokens import is_valid_telegram_token, mask_token


class CodeToolsTests(unittest.TestCase):
    def test_extract_python_code_from_fence(self):
        raw = "```python\nprint('hello')\n```"
        self.assertEqual(extract_python_code(raw), "print('hello')")

    def test_validate_allows_simple_bot_code(self):
        result = validate_generated_code(
            "import os\n"
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
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
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "mode = ParseMode.MARKDOWN\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("legacy", result.error)

    def test_validate_rejects_legacy_markdown_string_parse_mode(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "    application.bot.send_message(1, 'hi', parse_mode='Markdown')\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("legacy", result.error)

    def test_validate_requires_command_menu_registration(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("set_my_commands", result.error)

    def test_validate_rejects_syntax_error(self):
        result = validate_generated_code("def nope(:\n    pass")
        self.assertFalse(result.ok)
        self.assertIn("Syntax error", result.error)

    def test_validate_rejects_denied_import(self):
        result = validate_generated_code("import subprocess\nprint('x')")
        self.assertFalse(result.ok)
        self.assertIn("Denied import", result.error)

    def test_validate_rejects_aliased_os_system(self):
        result = validate_generated_code(
            "import os as operating\noperating.system('whoami')"
        )
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

    def test_static_check_rejects_undefined_handler_name(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "from telegram.ext import CommandHandler\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "    application.add_handler(CommandHandler('start', missing_start))\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("Static type check", result.error)
        self.assertIn("missing_start", result.error)

    def test_static_check_rejects_local_use_before_assignment(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def start(update, context):\n"
            "    await update.message.reply_text(text)\n"
            "    text = 'hello'\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("used before local assignment", result.error)

    def test_static_check_rejects_awaiting_sync_function(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "def make_text():\n"
            "    return 'hello'\n"
            "async def start(update, context):\n"
            "    await make_text()\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("cannot await sync function", result.error)

    def test_static_check_rejects_calling_non_callable_value(self):
        result = validate_generated_code(
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def setup(application):\n"
            "    await application.bot.set_my_commands([])\n"
            "def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "    label = 'Start'\n"
            "    label()\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("not callable", result.error)

    def test_static_check_rejects_asyncio_run_without_calling_main(self):
        result = validate_generated_code(
            "import asyncio\n"
            "from typing import Any\n"
            "async def error_handler(update, context):\n"
            "    pass\n"
            "async def main():\n"
            "    application: Any = object()\n"
            "    application.add_error_handler(error_handler)\n"
            "    await application.bot.set_my_commands([])\n"
            "if __name__ == '__main__':\n"
            "    asyncio.run(main)\n"
        )
        self.assertFalse(result.ok)
        self.assertIn("asyncio.run expected", result.error)

    def test_mypy_static_check_rejects_obvious_type_error(self):
        with patch("botmother.code_tools.subprocess.run") as run:
            run.return_value = CompletedProcess(
                args=[],
                returncode=1,
                stdout="bot.py:1: error: Incompatible types in assignment [assignment]\n",
                stderr="",
            )

            result = run_mypy_static_check("value: int = 'bad'")

        self.assertFalse(result.ok)
        self.assertIn("Mypy static type check failed", result.error)
        self.assertIn("Incompatible types", result.error)

    def test_mypy_static_check_skips_when_mypy_is_not_installed(self):
        with patch("botmother.code_tools.subprocess.run") as run:
            run.return_value = CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="No module named mypy",
            )

            result = run_mypy_static_check("value: int = 'ok'")

        self.assertTrue(result.ok, result.error)

    def test_token_validation_and_masking(self):
        token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_123"
        self.assertTrue(is_valid_telegram_token(token))
        self.assertEqual(mask_token(token), "123456:ABCD..._123")


if __name__ == "__main__":
    unittest.main()
