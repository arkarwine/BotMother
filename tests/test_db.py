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
            self.assertEqual(db.latest_valid_revision(bot_id)["code"], "print('ok')")
            db.add_revision(bot_id, "bad", "def nope(:", "failed", "syntax")
            self.assertEqual(db.latest_revision(bot_id)["validation_status"], "failed")
            self.assertEqual(db.latest_valid_revision(bot_id)["code"], "print('ok')")
            self.assertEqual(db.get_logs(bot_id)[0]["line"], "hello")
            self.assertEqual(
                db.get_bot_by_token("12345:abcdefghijklmnopqrstuvwxyzABCDE")["id"],
                bot_id,
            )
            db.update_bot_username(bot_id, "echo_bot")
            self.assertEqual(db.get_bot(bot_id)["bot_username"], "echo_bot")
            db.set_bot_env_vars(bot_id, {"WEATHER_API_KEY": "secret"})
            self.assertEqual(db.get_bot_env_vars(bot_id), {"WEATHER_API_KEY": "secret"})

    def test_user_locale_preference_survives_user_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", "First", "Last")

            self.assertIsNone(db.get_user_locale(1))
            db.update_user_locale(1, "my")
            self.assertEqual(db.get_user_locale(1), "my")

            db.upsert_user(1, "updated", "Updated", "Name")
            self.assertEqual(db.get_user_locale(1), "my")

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

    def test_credit_account_grants_free_credits_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", None, None)

            self.assertEqual(db.credit_balance(1, 50), 50)
            self.assertEqual(db.credit_balance(1, 50), 50)
            ledger = db.credit_ledger_for_user(1)
            self.assertEqual(len([row for row in ledger if row["action"] == "initial_free"]), 1)

    def test_credit_reserve_settle_and_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", None, None)

            reservation_id, balance = db.reserve_credits(1, 10, "new_bot", 50)
            self.assertIsNotNone(reservation_id)
            self.assertEqual(balance, 40)
            self.assertTrue(db.settle_credit_reservation(reservation_id))
            self.assertEqual(db.credit_balance(1, 50), 40)

            reservation_id, balance = db.reserve_credits(1, 5, "edit", 50)
            self.assertEqual(balance, 35)
            self.assertTrue(db.refund_credit_reservation(reservation_id))
            self.assertEqual(db.credit_balance(1, 50), 40)

    def test_credit_runtime_accrual_charges_after_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", None, None)
            db.credit_balance(1, 50)
            first = db.accrue_runtime_credits(1, 1, 1000, 86400, 50)
            self.assertEqual(first.charged, 0)

            second = db.accrue_runtime_credits(1, 1, 1000 + 86400, 86400, 50)
            self.assertEqual(second.charged, 1)
            self.assertEqual(second.balance, 49)

    def test_credit_runtime_requests_stop_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", None, None)
            db.set_credit_balance(1, 0, None, 50)
            db.accrue_runtime_credits(1, 1, 1000, 86400, 50)

            result = db.accrue_runtime_credits(1, 1, 1000 + 86400, 86400, 50)

            self.assertTrue(result.should_stop)
            self.assertEqual(result.balance, 0)

    def test_runtime_meter_reset_preserves_accumulated_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "botmother.sqlite3")
            db.initialize()
            db.upsert_user(1, "user", None, None)
            db.credit_balance(1, 50)
            db.accrue_runtime_credits(1, 1, 1000, 86400, 50)
            db.accrue_runtime_credits(1, 1, 1600, 86400, 50)

            db.reset_runtime_meter_for_users([1], now=5000)
            result = db.accrue_runtime_credits(1, 1, 5001, 86400, 50)

            self.assertEqual(result.charged, 0)
            self.assertLess(result.due, 1)


if __name__ == "__main__":
    unittest.main()
