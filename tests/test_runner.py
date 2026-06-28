import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botmother.config import Settings
from botmother.db import Database
from botmother.runner import (
    MAX_UNEXPECTED_SIGNAL_RESTARTS,
    ProcessManager,
    ProcessRecord,
    format_return_code,
    is_signal_exit,
)


def make_settings(tmp: str) -> Settings:
    return Settings(
        mother_bot_token="11111:mother_token_abcdefghijklmnopqrstuvwxyz",
        openrouter_api_key="test",
        openrouter_model="",
        openrouter_interaction_model="google/gemini-2.5-pro",
        openrouter_coding_model="deepseek/deepseek-v4-pro",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_app_name="BotMother tests",
        openrouter_app_url="",
        db_path=Path(tmp) / "botmother.sqlite3",
        workdir=Path(tmp) / "bots",
        owner_ids={1},
        python_bin="/usr/bin/python3",
        bwrap_bin="bwrap",
        require_bwrap=True,
    )


class FakeSignalProcess:
    pid = 1234
    returncode = -2

    async def wait(self) -> int:
        return -2


def create_running_bot(db: Database, settings: Settings) -> int:
    db.upsert_user(1, "owner", None, None)
    bot_id = db.create_bot(
        1,
        100,
        "Echo",
        "make echo",
        "12345:abcdefghijklmnopqrstuvwxyzABCDE",
        settings.workdir / "1",
    )
    db.mark_started(bot_id, 1234)
    return bot_id


class RunnerTests(unittest.TestCase):
    def test_format_return_code_names_signals(self):
        self.assertEqual(format_return_code(-2), "-2 (SIGINT)")
        self.assertEqual(format_return_code(1), "1")
        self.assertEqual(format_return_code(None), "unknown")

    def test_is_signal_exit(self):
        self.assertTrue(is_signal_exit(-2))
        self.assertFalse(is_signal_exit(0))
        self.assertFalse(is_signal_exit(1))
        self.assertFalse(is_signal_exit(None))

    def test_unexpected_signal_restart_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            db.initialize()
            bot_id = create_running_bot(db, settings)
            manager = ProcessManager(settings, db)
            started = []

            async def fake_start_bot(bot_id: int) -> None:
                started.append(bot_id)

            manager.start_bot = fake_start_bot

            with patch("botmother.runner.UNEXPECTED_SIGNAL_RESTART_DELAY_SECONDS", 0):
                asyncio.run(manager._restart_after_unexpected_signal(bot_id, "-2 (SIGINT)"))

            self.assertEqual(started, [bot_id])
            self.assertEqual(manager.unexpected_signal_restarts[bot_id], 1)

            manager.unexpected_signal_restarts[bot_id] = MAX_UNEXPECTED_SIGNAL_RESTARTS
            with patch("botmother.runner.UNEXPECTED_SIGNAL_RESTART_DELAY_SECONDS", 0):
                asyncio.run(manager._restart_after_unexpected_signal(bot_id, "-2 (SIGINT)"))

            self.assertEqual(started, [bot_id])

    def test_signal_exit_during_manager_shutdown_keeps_bot_running_for_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            db.initialize()
            bot_id = create_running_bot(db, settings)
            manager = ProcessManager(settings, db)
            record = ProcessRecord(process=FakeSignalProcess())
            manager.active[bot_id] = record
            manager.shutting_down = True

            with patch("botmother.runner.SIGNAL_EXIT_STATUS_GRACE_SECONDS", 0):
                asyncio.run(manager._watch_process(bot_id, record))

            self.assertEqual(db.get_bot(bot_id)["status"], "running")
            self.assertNotIn(bot_id, manager.active)
            self.assertTrue(any("manager shutdown" in row["line"] for row in db.get_logs(bot_id, 10)))

    def test_signal_exit_outside_shutdown_marks_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            db.initialize()
            bot_id = create_running_bot(db, settings)
            manager = ProcessManager(settings, db)
            record = ProcessRecord(process=FakeSignalProcess())
            manager.active[bot_id] = record
            restarted = []

            async def fake_restart_after_unexpected_signal(bot_id: int, return_code_text: str) -> None:
                restarted.append((bot_id, return_code_text))

            manager._restart_after_unexpected_signal = fake_restart_after_unexpected_signal

            with patch("botmother.runner.SIGNAL_EXIT_STATUS_GRACE_SECONDS", 0):
                asyncio.run(manager._watch_process(bot_id, record))

            self.assertEqual(db.get_bot(bot_id)["status"], "interrupted")
            self.assertEqual(restarted, [(bot_id, "-2 (SIGINT)")])

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
                openrouter_api_key=settings.openrouter_api_key,
                openrouter_model=settings.openrouter_model,
                openrouter_interaction_model=settings.openrouter_interaction_model,
                openrouter_coding_model=settings.openrouter_coding_model,
                openrouter_base_url=settings.openrouter_base_url,
                openrouter_app_name=settings.openrouter_app_name,
                openrouter_app_url=settings.openrouter_app_url,
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
