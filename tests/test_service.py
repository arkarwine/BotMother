import asyncio
import tempfile
import unittest
from pathlib import Path

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


def make_service(tmp: str):
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
    return BotService(settings, db, object(), runner), db, runner, bot_id


class ServiceEditTests(unittest.TestCase):
    def test_invalid_manual_edit_does_not_stop_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, bot_id = make_service(tmp)
            runner.active[bot_id] = object()

            result = asyncio.run(service.edit_bot_code(1, bot_id, "import subprocess\n"))

            self.assertFalse(result.ok)
            self.assertEqual(runner.stop_count, 0)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["validation_status"], "failed")

    def test_valid_manual_edit_restarts_running_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, db, runner, bot_id = make_service(tmp)
            runner.active[bot_id] = object()

            result = asyncio.run(service.edit_bot_code(1, bot_id, "print('new')"))

            self.assertTrue(result.ok, result.message)
            self.assertEqual(runner.stop_count, 1)
            self.assertEqual(runner.start_count, 1)
            self.assertIn(bot_id, runner.active)
            self.assertEqual(db.latest_revision(bot_id)["code"], "print('new')")

    def test_get_source_returns_latest_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, _, _, bot_id = make_service(tmp)

            result = service.get_source(1, bot_id)

            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.code, "print('old')")


if __name__ == "__main__":
    unittest.main()

