import tempfile
import unittest
from pathlib import Path

from botmother.config import Settings
from botmother.db import Database
from botmother.runner import ProcessManager


def make_settings(tmp: str) -> Settings:
    return Settings(
        mother_bot_token="11111:mother_token_abcdefghijklmnopqrstuvwxyz",
        gemini_api_key="test",
        gemini_model="gemini-3.1-flash-lite",
        db_path=Path(tmp) / "botmother.sqlite3",
        workdir=Path(tmp) / "bots",
        owner_ids={1},
        python_bin="/usr/bin/python3",
        bwrap_bin="bwrap",
        require_bwrap=True,
    )


class RunnerTests(unittest.TestCase):
    def test_sandbox_command_does_not_include_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            manager = ProcessManager(settings, db)
            bot_dir = Path(tmp) / "bots" / "1"
            cmd = manager.build_sandbox_command(bot_dir)
            joined = " ".join(cmd)

            self.assertIn("bwrap", cmd[0])
            self.assertIn("--bind", cmd)
            self.assertIn("/app", cmd)
            self.assertIn("bot.py", cmd)
            self.assertNotIn("BOT_TOKEN", joined)
            self.assertNotIn("mother_token", joined)

    def test_child_env_contains_runtime_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            manager = ProcessManager(settings, db)
            env = manager._child_env("12345:abcdefghijklmnopqrstuvwxyzABCDE", "/app/bot.sqlite3")
            self.assertEqual(env["BOT_TOKEN"], "12345:abcdefghijklmnopqrstuvwxyzABCDE")
            self.assertEqual(env["BOT_DB_PATH"], "/app/bot.sqlite3")
            self.assertEqual(env["PYTHONUNBUFFERED"], "1")


if __name__ == "__main__":
    unittest.main()

