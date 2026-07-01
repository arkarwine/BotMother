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
    RunnerError,
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
    )


class FakeSignalProcess:
    pid = 1234
    returncode = -2

    async def wait(self) -> int:
        return -2


class EmptyStream:
    async def readline(self):
        return b""


class FakeLaunchProcess:
    pid = 4321
    returncode = None
    stdout = EmptyStream()
    stderr = EmptyStream()

    async def wait(self) -> int:
        await asyncio.sleep(10)
        return 0


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

    def test_plain_command_does_not_include_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            db = Database(settings.db_path)
            manager = ProcessManager(settings, db)
            cmd = manager.build_plain_command()
            joined = " ".join(cmd)

            self.assertEqual(cmd, [settings.python_bin, "bot.py"])
            self.assertIn("bot.py", cmd)
            self.assertNotIn("BOT_TOKEN", joined)
            self.assertNotIn("mother_token", joined)

    def test_start_bot_launches_plain_python_in_bot_directory(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                settings = make_settings(tmp)
                db = Database(settings.db_path)
                db.initialize()
                db.upsert_user(1, "owner", None, None)
                bot_dir = settings.workdir / "1"
                bot_id = db.create_bot(
                    1,
                    100,
                    "Echo",
                    "make echo",
                    "12345:abcdefghijklmnopqrstuvwxyzABCDE",
                    bot_dir,
                )
                db.add_revision(bot_id, "make echo", "print('ok')", "ok", None)
                manager = ProcessManager(settings, db)

                manager._host_python_exists = lambda python_bin: True
                async def no_missing_imports(python_bin: str):
                    return []

                manager._missing_child_imports = no_missing_imports
                with patch(
                    "asyncio.create_subprocess_exec",
                    return_value=FakeLaunchProcess(),
                ) as mocked:
                    await manager.start_bot(bot_id)

                self.assertEqual(mocked.call_args.args[:2], (settings.python_bin, "bot.py"))
                self.assertEqual(mocked.call_args.kwargs["cwd"], str(bot_dir))
                self.assertEqual(
                    mocked.call_args.kwargs["env"]["BOT_DB_PATH"],
                    str(bot_dir / "bot.sqlite3"),
                )

        asyncio.run(run_case())

    def test_start_bot_reports_missing_child_dependency_before_launch(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                settings = make_settings(tmp)
                db = Database(settings.db_path)
                db.initialize()
                db.upsert_user(1, "owner", None, None)
                bot_dir = settings.workdir / "1"
                bot_id = db.create_bot(
                    1,
                    100,
                    "Echo",
                    "make echo",
                    "12345:abcdefghijklmnopqrstuvwxyzABCDE",
                    bot_dir,
                )
                db.add_revision(bot_id, "make echo", "print('ok')", "ok", None)
                manager = ProcessManager(settings, db)

                manager._host_python_exists = lambda python_bin: True
                async def missing_imports(python_bin: str):
                    return ["telegram"]

                manager._missing_child_imports = missing_imports
                with patch("asyncio.create_subprocess_exec") as mocked:
                    with self.assertRaises(RunnerError) as raised:
                        await manager.start_bot(bot_id)

                self.assertFalse(mocked.called)
                self.assertEqual(db.get_bot(bot_id)["status"], "dependency_missing")
                self.assertIn("Missing import(s): telegram", str(raised.exception))
                self.assertTrue(
                    any("Missing import(s): telegram" in row["line"] for row in db.get_logs(bot_id, 20))
                )

        asyncio.run(run_case())

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
