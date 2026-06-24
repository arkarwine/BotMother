import tempfile
import unittest
from pathlib import Path

from botmother.db import Database


class DatabaseTests(unittest.TestCase):
    def test_create_bot_revision_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", "First", "Last")
            bot_id = db.create_bot(
                1,
                100,
                "Echo",
                "make echo",
                "12345:abcdefghijklmnopqrstuvwxyzABCDE",
                Path(tmp) / "1",
            )
            db.add_revision(bot_id, "make echo", "print('ok')", "ok", None)
            db.mark_started(bot_id, 999)
            db.add_log(bot_id, "stdout", "hello", keep_rows=10)

            bot = db.get_bot(bot_id)
            self.assertIsNotNone(bot)
            self.assertEqual(bot["status"], "running")
            self.assertEqual(db.latest_revision(bot_id)["code"], "print('ok')")
            self.assertEqual(db.get_logs(bot_id)[0]["line"], "hello")
            self.assertEqual(
                db.get_bot_by_token("12345:abcdefghijklmnopqrstuvwxyzABCDE")["id"],
                bot_id,
            )
            db.update_bot_username(bot_id, "echo_bot")
            self.assertEqual(db.get_bot(bot_id)["bot_username"], "echo_bot")
            db.set_bot_env_vars(bot_id, {"WEATHER_API_KEY": "secret"})
            self.assertEqual(db.get_bot_env_vars(bot_id), {"WEATHER_API_KEY": "secret"})

    def test_soft_delete_hides_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, None, None, None)
            token = "12345:abcdefghijklmnopqrstuvwxyzABCDE"
            bot_id = db.create_bot(1, 100, "Echo", "make echo", token, Path(tmp) / "1")
            db.soft_delete_bot(bot_id)
            self.assertIsNone(db.get_bot(bot_id))
            self.assertEqual(
                db.get_bot(bot_id, include_deleted=True)["status"], "deleted"
            )
            self.assertIsNone(db.get_bot_by_token(token))
            new_bot_id = db.create_bot(
                1, 100, "Echo again", "make echo again", token, Path(tmp) / "2"
            )
            self.assertEqual(db.get_bot_by_token(token)["id"], new_bot_id)

    def test_release_deleted_token_repairs_legacy_deleted_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, None, None, None)
            token = "12345:abcdefghijklmnopqrstuvwxyzABCDE"
            bot_id = db.create_bot(1, 100, "Echo", "make echo", token, Path(tmp) / "1")
            with db.session() as conn:
                conn.execute(
                    "UPDATE bots SET status = 'deleted', deleted_at = 123 WHERE id = ?",
                    (bot_id,),
                )

            self.assertIsNone(db.get_bot_by_token(token))
            self.assertEqual(db.release_deleted_token(token), 1)
            new_bot_id = db.create_bot(
                1, 100, "Echo again", "make echo again", token, Path(tmp) / "2"
            )
            self.assertEqual(db.get_bot_by_token(token)["id"], new_bot_id)


if __name__ == "__main__":
    unittest.main()
