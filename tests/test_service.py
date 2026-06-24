import asyncio
import tempfile
import unittest
from pathlib import Path

from botmother.ai import AIDecision, AIEnvVar
from botmother.config import Settings
from botmother.db import Database
from botmother.service import BotService


class FakeRunner:
    def __init__(self):
        self.active = {}
        self.start_count = 0
        self.stop_count = 0

    async def start_bot(self, bot_id: int) -> None:
        self.start_count += 1
        self.active[bot_id] = object()

    async def stop_bot(self, bot_id: int, mark_stopped: bool = True) -> None:
        self.stop_count += 1
        self.active.pop(bot_id, None)


class FakeGenerator:
    def __init__(self, edited_code: str, env=None):
        self.edited_code = edited_code
        self.env = tuple(env or ())
        self.current_code = None
        self.edit_prompt = None
        self.answer_history = None
        self.force_code = None

    def decide_edit(self, current_code: str, edit_prompt: str, answer_history, force_code: bool = False):
        self.current_code = current_code
        self.edit_prompt = edit_prompt
        self.answer_history = answer_history
        self.force_code = force_code
        return AIDecision("code", "Ready.", (), self.edited_code, self.env)


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


def make_service(tmp: str, edited_code: str = "print('new')", env=None):
    settings = make_settings(tmp)
    db = Database(settings.db_path)
    db.initialize()
    db.upsert_user(1, "owner", None, None)
    bot_id = db.create_bot(
        1,
        100,
        "Echo",
        "make echo",
        "12345:abcdefghijklmnopqrstuvwxyzABCDE",
        settings.workdir / "1",
    )
    db.add_revision(bot_id, "make echo", "print('old')", "ok", None)
    runner = FakeRunner()
    generator = FakeGenerator(edited_code, env=env)
    return BotService(settings, db, generator, runner), db, runner, generator, bot_id


class ServiceEditTests(unittest.TestCase):
    def test_invalid_prompt_edit_does_not_stop_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, _, bot_id = make_service(tmp, edited_code="import subprocess\n")
            runner.active[bot_id] = object()

            result = asyncio.run(service.edit_bot_with_prompt(1, bot_id, "add shell command support"))

            self.assertFalse(result.ok)
            self.assertEqual(runner.stop_count, 0)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["validation_status"], "failed")

    def test_valid_prompt_edit_restarts_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = [AIEnvVar("WEATHER_API_KEY", "secret")]
            service, db, runner, generator, bot_id = make_service(tmp, edited_code="print('new')", env=env)
            runner.active[bot_id] = object()

            result = asyncio.run(service.edit_bot_with_prompt(1, bot_id, "make it friendlier"))

            self.assertTrue(result.ok, result.message)
            self.assertEqual(runner.stop_count, 1)
            self.assertEqual(runner.start_count, 1)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["code"], "print('new')")
            self.assertEqual(db.get_bot_env_vars(bot_id), {"WEATHER_API_KEY": "secret"})
            self.assertEqual(generator.current_code, "print('old')")
            self.assertEqual(generator.edit_prompt, "make it friendlier")

    def test_get_source_returns_latest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, _, bot_id = make_service(tmp)

            result = service.get_source(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.code, "print('old')")


if __name__ == "__main__":
    unittest.main()
