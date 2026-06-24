from __future__ import annotations

from dataclasses import dataclass


SYSTEM_PROMPT = """You generate Telegram bot source code.

Return only raw Python code. Do not use Markdown fences. Do not return JSON. Do not describe the code.

The code must be a complete standalone Python file that can run with:
python bot.py

Runtime contract:
- Read the Telegram token from os.environ["BOT_TOKEN"].
- Read the bot-specific SQLite path from os.environ["BOT_DB_PATH"].
- You may use Python standard library, sqlite3, and python-telegram-bot.
- Use polling, not webhooks.
- Prefer python-telegram-bot async ApplicationBuilder.
- Create needed SQLite tables yourself inside BOT_DB_PATH.
- Keep the bot simple, robust, and friendly.

Do not import subprocess, socket, ctypes, importlib, or multiprocessing.
Do not call eval, exec, compile, __import__, os.system, os.remove, os.unlink, os.rmdir, os.rename, os.replace, shutil.move, or shutil.rmtree.
"""


@dataclass
class GeminiCodeGenerator:
    api_key: str
    model: str

    def __post_init__(self) -> None:
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("google-genai is not installed. Run: pip install -r requirements.txt") from exc
        self._client = genai.Client(api_key=self.api_key)

    def generate_code(self, user_prompt: str) -> str:
        prompt = (
            "Build this Telegram bot from the user's request. "
            "Return only the complete Python source file.\n\n"
            f"User request:\n{user_prompt.strip()}"
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"system_instruction": SYSTEM_PROMPT},
        )
        text = getattr(response, "text", None)
        if text:
            return text
        raise RuntimeError("Gemini returned an empty response.")

