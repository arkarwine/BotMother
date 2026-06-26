import tempfile
import unittest
from pathlib import Path

from mgmtbot.db import MgmtDatabase


class MgmtBotCreditTests(unittest.TestCase):
    def test_mgmt_db_initializes_and_grants_credits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MgmtDatabase(Path(tmp) / "botmother.sqlite3")
            db.initialize()

            balance = db.grant_credits(123, 25, 1, 50, note="test grant")

            self.assertEqual(balance, 75)
            self.assertEqual(db.credit_balance(123, 50), 75)
            self.assertEqual(db.credit_ledger_for_user(123, 1)[0]["amount"], 25)


if __name__ == "__main__":
    unittest.main()
