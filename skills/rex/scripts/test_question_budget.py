import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import question_ledger as ledger
import question_model as model


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = ledger.Ledger(Path(self.folder.name) / "ledger.db")
        self.addCleanup(self.store.close)

    def reserve(self, model_name=model.Model.HAIKU, at=None):
        return self.store.reserve_budget(
            "task", model=model_name, tokens=100, usd="0.10", at=at
        )

    def test_settle_once_uses_reserved_day_and_missing_usage(self):
        at = datetime(2026, 1, 2, 5, 59, tzinfo=timezone.utc)
        first = self.reserve(at=at)
        second = self.reserve(at=at)
        self.assertNotEqual(first, second)
        self.store.settle_budget(first, usage=None)
        self.store.settle_budget(first, usage=None)
        for scope, key in (("task", "task"), ("day", "2026-01-01")):
            row = self.store.db.execute(
                "SELECT * FROM budget WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            self.assertEqual(
                (
                    row["tokens_reserved"],
                    row["tokens_spent"],
                    row["usd_reserved"],
                    row["usd_spent"],
                ),
                (100, 100, "0.10", "0.10"),
            )

    def test_two_overruns_block_same_model_and_day_only(self):
        at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        for usage in (
            model.Usage(101, 0, Decimal("0.10")),
            model.Usage(1, 0, Decimal("0.11")),
        ):
            reservation = self.reserve(at=at)
            self.store.settle_budget(reservation, usage=usage)
        self.assertIsNone(self.reserve(at=at))
        self.assertIsNotNone(self.reserve(model_name=model.Model.SONNET, at=at))
        self.assertIsNotNone(
            self.reserve(at=datetime(2026, 1, 3, 12, tzinfo=timezone.utc))
        )

    def test_invalid_decimals_are_value_errors(self):
        with self.assertRaises(ValueError):
            self.store.reserve_budget(
                "task", model=model.Model.HAIKU, tokens=1, usd="invalid"
            )
        self.store.db.execute(
            "INSERT INTO budget VALUES ('task','bad',0,0,0,'invalid','0')"
        )
        row = self.store.db.execute("SELECT * FROM budget WHERE key='bad'").fetchone()
        with self.assertRaises(ValueError):
            ledger.BudgetRecord.parse(row)


if __name__ == "__main__":
    unittest.main()
