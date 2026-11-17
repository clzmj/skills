import tempfile
import unittest
from pathlib import Path

import question_ledger as ledger
from test_question_ledger import asker, classification, native_call, outbound, question


class AuditTests(unittest.TestCase):
    def test_audits_flush_only_after_outer_commit_and_direct_audits_work(self):
        with tempfile.TemporaryDirectory() as directory:
            with ledger.Ledger(Path(directory) / "ledger.db") as store:
                store.audit(ledger.AuditKind.NOTICE, "task", "direct")
                with store.transaction():
                    with store.transaction():
                        store.audit(ledger.AuditKind.NOTICE, "task", "committed")
                    self.assertEqual(
                        [e.detail for e in store.audit_events("task")], ["direct"]
                    )
                self.assertEqual(
                    [e.detail for e in store.audit_events("task")],
                    ["direct", "committed"],
                )

    def test_ignored_outbound_does_not_create_an_audit_event(self):
        with tempfile.TemporaryDirectory() as directory:
            with ledger.Ledger(Path(directory) / "ledger.db") as store:
                batch = store.open_or_attach(
                    asker(), (question(),), native_call("call-1"), classification()
                ).batch
                message = outbound(batch)
                store.put_outbound(message)
                before = store.audit_events(batch.asker.task)
                store.put_outbound(message)
                self.assertEqual(store.audit_events(batch.asker.task), before)

    def test_rolled_back_transaction_does_not_write_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            with ledger.Ledger(Path(directory) / "ledger.db") as store:
                with self.assertRaisesRegex(ValueError, "rollback"):
                    with store.transaction():
                        store.audit(ledger.AuditKind.NOTICE, "task", "rolled back")
                        raise ValueError("rollback")
                self.assertEqual(store.audit_events("task"), ())


if __name__ == "__main__":
    unittest.main()
