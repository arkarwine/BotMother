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

    def test_sandbox_binds_configured_venv_root_before_resolving_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / ".venv"
            python_bin = venv / "bin" / "python3"
            python_bin.parent.mkdir(parents=True)
            python_bin.write_text("", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text("", encoding="utf-8")
            settings = make_settings(tmp)
            settings = Settings(
                mother_bot_token=settings.mother_bot_token,
                gemini_api_key=settings.gemini_api_key,
                gemini_model=settings.gemini_model,
                db_path=settings.db_path,
                workdir=settings.workdir,
                owner_ids=settings.owner_ids,
                python_bin=str(python_bin),
                bwrap_bin=settings.bwrap_bin,
                require_bwrap=settings.require_bwrap,
            )
            db = Database(settings.db_path)
            manager = ProcessManager(settings, db)
            cmd = manager.build_sandbox_command(Path(tmp) / "bots" / "1")

            self.assertIn("--ro-bind", cmd)
            self.assertIn(str(venv), cmd)

    def test_child_env_contains_runtime_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            manager = ProcessManager(settings, db)
            env = manager._child_env(
                "12345:abcdefghijklmnopqrstuvwxyzABCDE",
                "/app/bot.sqlite3",
                {"WEATHER_API_KEY": "secret", "BOT_TOKEN": "override"},
            )
            self.assertEqual(env["BOT_TOKEN"], "12345:abcdefghijklmnopqrstuvwxyzABCDE")
            self.assertEqual(env["BOT_DB_PATH"], "/app/bot.sqlite3")
            self.assertEqual(env["PYTHONUNBUFFERED"], "1")
            self.assertEqual(env["WEATHER_API_KEY"], "secret")


if __name__ == "__main__":
    unittest.main()
