import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import question_answerers as answerers
import question_ledger as ledger
import question_model as model
from test_question_ledger import (
    asker,
    classification,
    native_call,
    question,
    registration,
)


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

    def test_sweep_settles_only_expired_reservations_as_spent(self):
        at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        first = self.reserve(at=at)
        self.store.reserve_budget(
            "other-task", model=model.Model.SONNET, tokens=50, usd="0.05", at=at
        )

        self.assertEqual(
            self.store.sweep_budget_reservations(at=at + ledger.timedelta(minutes=11)),
            2,
        )
        self.assertEqual(
            self.store.sweep_budget_reservations(at=at + ledger.timedelta(minutes=12)),
            0,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT settled FROM budget_reservation WHERE id=?", (first,)
            ).fetchone()[0],
            1,
        )
        for scope, key, tokens in (
            ("task", "task", 100),
            ("task", "other-task", 50),
            ("day", "2026-01-02", 150),
        ):
            row = self.store.db.execute(
                "SELECT * FROM budget WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            self.assertEqual((row["tokens_reserved"], row["tokens_spent"]), (0, tokens))
            self.assertEqual(Decimal(row["usd_reserved"]), Decimal(0))
            self.assertEqual(Decimal(row["usd_spent"]), Decimal(tokens) / 1000)

    def test_sweep_does_not_expire_live_or_recent_reservations(self):
        at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        reservation = self.reserve(at=at)
        self.assertEqual(
            self.store.sweep_budget_reservations(at=at + ledger.timedelta(minutes=9)), 0
        )
        self.assertEqual(
            self.store.sweep_budget_reservations(at=at + ledger.timedelta(minutes=10)),
            0,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT settled FROM budget_reservation WHERE id=?", (reservation,)
            ).fetchone()[0],
            0,
        )

    def test_tick_sweeps_crashed_worker_budget_even_with_activation_off(self):
        import os
        from unittest.mock import patch
        import question_broker as broker

        at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        self.reserve(at=at)
        with patch.dict(os.environ, {model.FLAG_ENV: "0"}):
            worker = broker.Broker(
                self.store, clock=lambda: at + ledger.timedelta(minutes=11)
            )
            self.assertTrue(worker.tick())
        row = self.store.db.execute(
            "SELECT * FROM budget WHERE scope='task'"
        ).fetchone()
        self.assertEqual((row["tokens_reserved"], row["tokens_spent"]), (0, 100))

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

    def test_typical_cold_answerer_call_is_not_an_overrun(self):
        # Reserves carry 1.15x headroom (Carlos, 2026-11-25): observed cold calls and
        # a fully cold call 14% above the pilot settle cleanly; 16% above overruns on
        # tokens (and on USD for Sonnet; Haiku's USD rounding leaves ~16.7%).
        hooks = Path(__file__).parents[3] / "tests/qe/hooks"
        receipt = json.loads((hooks / "claude-p12-oauth-calibration.json").read_text())
        pilot = json.loads((hooks / "claude-p12-fully-cold-pilot.json").read_text())
        configs = answerers.load_models()
        at = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

        def overrun(name, bounds, tokens, output_tokens, usd):
            reservation = self.store.reserve_budget(
                "task",
                model=name,
                tokens=bounds.reserve_tokens,
                usd=bounds.reserve_usd,
                at=at,
            )
            assert reservation is not None
            self.store.settle_budget(
                reservation,
                usage=model.Usage(tokens - output_tokens, output_tokens, usd),
            )
            return self.store.db.execute(
                "SELECT overrun FROM budget_reservation WHERE id=?", (reservation,)
            ).fetchone()[0]

        for name in (model.Model.HAIKU, model.Model.SONNET):
            bounds = configs[name].bounds
            assert bounds is not None
            samples = sorted(
                (s for s in receipt["samples"] if s["model"] == name.value),
                key=lambda s: Decimal(s["usd"]),
            )
            fully_cold = [s for s in pilot["samples"] if s["model"] == name.value]
            self.assertEqual(len(fully_cold), 1)
            # Median nonce call, the largest-token nonce call (Sonnet 26,789 tokens)
            # and the fully cold pilot: none may overrun.
            largest = max(samples, key=lambda s: s["tokens"])
            for sample in (samples[len(samples) // 2], largest, *fully_cold):
                with self.subTest(
                    model=name, usd=sample["usd"], tokens=sample["tokens"]
                ):
                    self.assertEqual(
                        overrun(
                            name,
                            bounds,
                            sample["tokens"],
                            sample["output_tokens"],
                            Decimal(sample["usd"]),
                        ),
                        0,
                    )
            # Margin, one dimension at a time (the other stays at the pilot value).
            # Tokens bind for both: +16% overruns. USD at +16% overruns Sonnet only;
            # rounding reserve_usd up to 4 decimals gives Haiku ~16.7% headroom.
            cold = fully_cold[0]
            usd_over = int(name is model.Model.SONNET)
            one = Decimal(1)
            for token_scale, usd_scale, expected in (
                (Decimal("1.14"), one, 0),
                (Decimal("1.16"), one, 1),
                (one, Decimal("1.14"), 0),
                (one, Decimal("1.16"), usd_over),
            ):
                # A model is refused after two overruns a day; start each case clean.
                self.store.db.execute("DELETE FROM budget_reservation")
                self.store.db.execute("DELETE FROM budget")
                with self.subTest(
                    model=name, token_scale=token_scale, usd_scale=usd_scale
                ):
                    self.assertEqual(
                        overrun(
                            name,
                            bounds,
                            int(cold["tokens"] * token_scale),
                            int(cold["output_tokens"] * token_scale),
                            Decimal(cold["usd"]) * usd_scale,
                        ),
                        expected,
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

    def test_twenty_attempt_cap_includes_inflight_and_settled_calls(self):
        for index in range(20):
            reservation = self.store.reserve_budget(
                "task", model=model.Model.HAIKU, tokens=1, usd="0"
            )
            self.assertIsNotNone(reservation)
            assert reservation is not None
            if index % 2:
                self.store.settle_budget(
                    reservation, usage=model.Usage(1, 0, Decimal(0))
                )
        self.assertIsNone(
            self.store.reserve_budget(
                "task", model=model.Model.HAIKU, tokens=1, usd="0"
            )
        )

    def test_token_cap_counts_reserved_plus_actual_usage(self):
        first = self.store.reserve_budget(
            "task", model=model.Model.HAIKU, tokens=400_000, usd="0"
        )
        assert first is not None
        self.store.settle_budget(first, usage=model.Usage(300_000, 0, Decimal(0)))
        self.assertIsNotNone(
            self.store.reserve_budget(
                "task", model=model.Model.HAIKU, tokens=300_000, usd="0"
            )
        )
        self.assertIsNone(
            self.store.reserve_budget(
                "task", model=model.Model.SONNET, tokens=1, usd="0"
            )
        )

    def test_task_dollar_cap_includes_inflight_across_connections(self):
        first = self.store.reserve_budget(
            "task", model=model.Model.HAIKU, tokens=0, usd="1.00"
        )
        assert first is not None
        with ledger.Ledger(self.store.path) as other:
            self.assertIsNotNone(
                other.reserve_budget(
                    "task", model=model.Model.SONNET, tokens=0, usd="0.50"
                )
            )
            self.assertIsNone(
                other.reserve_budget(
                    "task", model=model.Model.HAIKU, tokens=0, usd="0.01"
                )
            )
        self.store.settle_budget(first, usage=None)
        self.assertIsNone(
            self.store.reserve_budget(
                "task", model=model.Model.HAIKU, tokens=0, usd="0.01"
            )
        )

    def test_daily_dollar_cap_spans_tasks_and_local_midnight(self):
        before = datetime(2026, 1, 2, 5, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)
        for index in range(10):
            self.assertIsNotNone(
                self.store.reserve_budget(
                    f"task-{index}",
                    model=model.Model.HAIKU,
                    tokens=1,
                    usd="1",
                    at=before,
                )
            )
        self.assertIsNone(
            self.store.reserve_budget(
                "next-task", model=model.Model.HAIKU, tokens=1, usd="0.01", at=before
            )
        )
        self.assertIsNotNone(
            self.store.reserve_budget(
                "next-task", model=model.Model.HAIKU, tokens=1, usd="0.01", at=after
            )
        )


class ClassificationLedgerTests(unittest.TestCase):
    store: ledger.Ledger
    setUp = BudgetTests.setUp

    def open(self, call="call-1"):
        return self.store.open_or_attach(
            asker(),
            (question(call),),
            native_call(call),
            replace(classification(), classified=False),
        ).batch

    def test_pending_classification_survives_reopen_and_retry_timestamp(self):
        batch = self.open()
        at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        brief = model.BriefEscalation(
            model.BriefProject.PERSONAL, ("Use defaults",), ("Publish?",)
        )
        with patch.object(ledger, "now", return_value=at):
            self.store.queue_classification(
                str(batch.id), remote="origin", brief=brief, root_session="root"
            )
        self.store.queue_classification(
            str(batch.id), remote="replacement", brief=None, root_session="changed"
        )
        with ledger.Ledger(self.store.path) as reopened:
            pending = reopened.classification_pending(str(batch.id), batch.rev)
            assert pending is not None
            self.assertEqual(
                (
                    pending.remote,
                    pending.brief,
                    pending.root_session,
                    pending.started_at,
                ),
                ("origin", brief, "root", at),
            )
            self.assertIsNone(
                reopened.classification_pending(str(batch.id), batch.rev + 1)
            )
        newer = self.open("call-2")
        self.store.queue_classification(
            str(batch.id), remote="next", brief=None, root_session="root"
        )
        previous = self.store.classification_pending(str(batch.id), batch.rev)
        current = self.store.classification_pending(str(batch.id), newer.rev)
        assert previous is not None and current is not None
        self.assertEqual(previous.brief, brief)
        self.assertEqual(current.remote, "next")

    def test_apply_uses_raw_floor_without_persisting_conservative_read(self):
        batch = self.open()
        self.assertEqual(self.store.get_batch(str(batch.id)).classification.floor, 3)
        self.assertEqual(self.store.guard_floor(str(batch.id)), 1)
        self.assertTrue(
            self.store.apply_classification(
                str(batch.id), batch.rev, batch.context, classification()
            )
        )
        self.assertEqual(self.store.get_batch(str(batch.id)).classification.floor, 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM progress").fetchone()[0], 0
        )

    def test_apply_rejects_stale_revision_context_and_invalid_classification(self):
        batch = self.open()
        for rev, context in (
            (batch.rev + 1, batch.context),
            (batch.rev, model.ContextDigest("b" * 64)),
        ):
            self.assertFalse(
                self.store.apply_classification(
                    str(batch.id), rev, context, classification()
                )
            )
        for invalid in (
            replace(classification(), classified=False),
            {"classified": True},
            replace(classification(), delegable="yes"),
        ):
            with self.assertRaises(ValueError):
                self.store.apply_classification(
                    str(batch.id),
                    batch.rev,
                    batch.context,
                    invalid,  # ty: ignore[invalid-argument-type] -- exercise runtime validation
                )
        self.assertFalse(self.store.get_batch(str(batch.id)).classification.classified)

    def test_attach_pending_revision_carries_only_committed_floor(self):
        batch = self.open()
        second = self.open("call-2")
        self.assertEqual(second.id, batch.id)
        self.assertEqual(self.store.guard_floor(str(batch.id)), 1)
        self.assertTrue(
            self.store.apply_classification(
                str(batch.id), second.rev, second.context, classification()
            )
        )
        self.assertEqual(self.store.get_batch(str(batch.id)).classification.floor, 1)

    def test_late_result_preserves_registered_unclassified_ownership_floor(self):
        batch = self.open()
        entry = registration(self.store.get_batch(str(batch.id)))
        entry.pop("content_digest")
        entry["classified"] = False
        entry["content_digest"] = model.digest(entry)
        self.store.put_registration(entry)
        self.assertTrue(
            self.store.apply_classification(
                str(batch.id), batch.rev, batch.context, classification()
            )
        )
        self.assertEqual(self.store.guard_floor(str(batch.id)), 3)

    def test_apply_and_attach_never_lower_committed_box_floor(self):
        batch = self.open()
        self.store.db.execute(
            "UPDATE batch SET box_floor=4,state=? WHERE id=?",
            (model.BatchState.BOX_OWNED, str(batch.id)),
        )
        self.assertTrue(
            self.store.apply_classification(
                str(batch.id), batch.rev, batch.context, classification()
            )
        )
        self.assertEqual(self.store.guard_floor(str(batch.id)), 4)
        second = self.open("call-2")
        self.assertEqual(second.classification.floor, 4)
        self.assertEqual(second.state, model.BatchState.BOX_OWNED)

    def test_unclassified_registration_commits_floor_across_new_revision(self):
        batch = self.open()
        import question_broker as broker

        broker.prepare_registration(self.store, self.store.get_batch(str(batch.id)))
        self.assertEqual(
            self.store.guard_floor(str(batch.id)), model.EscalationLevel.L3_PRINCIPAL
        )
        newer = self.open("call-2")
        self.assertTrue(
            self.store.apply_classification(
                str(newer.id), newer.rev, newer.context, classification()
            )
        )
        self.assertEqual(
            self.store.get_batch(str(newer.id)).classification.floor,
            model.EscalationLevel.L3_PRINCIPAL,
        )


if __name__ == "__main__":
    unittest.main()
