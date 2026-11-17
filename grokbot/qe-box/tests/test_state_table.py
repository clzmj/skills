#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Accepting/refusing box state pairs, result rows, timers and retention."""

from datetime import timedelta
import sqlite3

from test_registration import BoxTest, entry, revision, progress, box
from test_decisions import answer
from test_corrections import report

m = box.m
S = box.BoxBatchState

# The visible REGISTERED and CLOSED transitions occur atomically inside register
# and tick. Their destination and rejection behavior are tested at those seams.
LEGAL = {
    S.LOCAL: {"correction3", "correction4", "card_correction", "cancel"},
    S.AWAITING_L3: {"answer3", "handoff_ask", "cancel"},
    S.AWAITING_L4: {"answer4", "card_ask", "cancel"},
    S.DECIDED: {"correction4", "card_correction", "cancel"},
    S.DELIVERED: {"correction4", "card_correction", "cancel"},
    S.CORRECTION_DUE: {
        "correction4",
        "card_correction",
        "handoff_correction",
        "cancel",
    },
    S.BLOCKED: {"retry", "cancel"},
    S.CANCELLED: set(),
    S.STALE: set(),
    S.TOMBSTONED: set(),
}
ACTIONS = (
    "answer3",
    "answer4",
    "correction3",
    "correction4",
    "handoff_ask",
    "handoff_correction",
    "card_ask",
    "card_correction",
    "cancel",
    "retry",
)


class StateTableTests(BoxTest):
    def fixture(self, state):
        value = entry(floor=1 if state == S.LOCAL else 3, task_id=str(box.uuid4()))
        self.box.register(value)
        if state == S.LOCAL:
            self.box.register(progress(value, local=1, local_level=1))
        elif state == S.AWAITING_L4:
            self.box.handoff(value["task_id"], "jim")
        elif state in (S.DECIDED, S.DELIVERED, S.CORRECTION_DUE, S.BLOCKED, S.STALE):
            ticket = self.box.decide(
                box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
            ).plans[0]
            outcome = (
                m.SendOutcome.SENT_ECHOED
                if state in (S.DELIVERED, S.CORRECTION_DUE)
                else m.SendOutcome.NOT_SENT_SESSION_GONE
                if state == S.STALE
                else m.SendOutcome.NOT_SENT_BUSY
            )
            self.box.result(report(ticket, outcome, self.now))
            if state == S.CORRECTION_DUE:
                self.box.register(progress(value, floor=4))
            if state == S.BLOCKED:
                for _ in range(2):
                    delivery = self.box.show(value["batch_id"])["deliveries"][0]
                    ticket = self.box.deliver(delivery.id).plans[0]
                    self.box.result(
                        report(ticket, m.SendOutcome.NOT_SENT_BUSY, self.now)
                    )
        elif state in (S.CANCELLED, S.TOMBSTONED):
            self.box.cancel(value["batch_id"], from_mac=True)
            if state == S.TOMBSTONED:
                self.now += timedelta(days=31)
                self.box.tick()
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.state, state)
        self.assertEqual(
            current.correction_floor, 4 if state == S.CORRECTION_DUE else None
        )
        return value

    def act(self, action, value):
        batch, task = value["batch_id"], value["task_id"]
        if action.startswith("answer") or action.startswith("correction"):
            correction = action.startswith("correction")
            level = int(action[-1])
            kwargs = {}
            if level == 4:
                purpose = (
                    box.CardPurpose.CORRECTION if correction else box.CardPurpose.ASK
                )
                card = self.box.card_intent(task, "jim", purpose=purpose, by="jim")
                self.box.card_posted(task, "jim", card.gen, "card", by="jim")
                kwargs = dict(card_gen=card.gen, card_ref="card")
            return self.box.decide(
                box.DecisionKind.CORRECTION if correction else box.DecisionKind.ANSWER,
                level,
                batch,
                1,
                answer(value),
                by="jim" if level == 4 else "dinesh",
                supersedes=1 if correction else None,
                **kwargs,
            )
        if action.startswith("handoff"):
            return self.box.handoff(
                task, "jim", purpose=box.CardPurpose(action.split("_")[1])
            )
        if action.startswith("card"):
            return self.box.card_intent(
                task, "jim", purpose=box.CardPurpose(action.split("_")[1]), by="jim"
            )
        if action == "cancel":
            return self.box.cancel(batch)
        return self.box.retry(batch)

    def test_every_visible_state_action_pair_and_correction_floor(self):
        for state, legal in LEGAL.items():
            for action in ACTIONS:
                with self.subTest(state=state, action=action):
                    value = self.fixture(state)
                    if action not in legal:
                        with self.assertRaises(box.Refused) as refusal:
                            if action in ("answer4", "correction4"):
                                # Exercise decide itself: an invalid state must refuse
                                # before any absent/stale card could grant authority.
                                self.box.decide(
                                    box.DecisionKind.ANSWER
                                    if action == "answer4"
                                    else box.DecisionKind.CORRECTION,
                                    4,
                                    value["batch_id"],
                                    1,
                                    answer(value),
                                    by="jim",
                                    card_gen=999,
                                    card_ref="absent",
                                    supersedes=1,
                                )
                            else:
                                self.act(action, value)
                        if action in ("answer4", "correction4"):
                            self.assertEqual(
                                refusal.exception.reason,
                                box.BoxRefusal.CLOSED_BATCH
                                if state == S.TOMBSTONED
                                else box.BoxRefusal.WRONG_STATE,
                            )
                        current = self.box.show(value["batch_id"])["record"]
                        self.assertEqual(current.state, state)
                        self.assertEqual(
                            current.correction_floor,
                            4 if state == S.CORRECTION_DUE else None,
                        )
                    else:
                        self.act(action, value)
                        current = self.box.show(value["batch_id"])["record"]
                        expected = (
                            S.DECIDED
                            if action.startswith(("answer", "correction"))
                            or action == "retry"
                            else S.AWAITING_L4
                            if action == "handoff_ask"
                            else S.CANCELLED
                            if action == "cancel"
                            else state
                        )
                        self.assertEqual(current.state, expected)
                        self.assertEqual(
                            current.correction_floor,
                            4 if expected == S.CORRECTION_DUE else None,
                        )

    def test_send_result_table_and_immediate_safe_retry(self):
        expected = {
            m.SendOutcome.SENT_ECHOED: (m.DeliveryState.OBSERVED, S.DELIVERED),
            m.SendOutcome.SENT_NO_ECHO: (m.DeliveryState.DISPATCHED, S.DELIVERED),
            m.SendOutcome.SEND_UNKNOWN: (m.DeliveryState.DISPATCH_UNCERTAIN, S.DECIDED),
            m.SendOutcome.NOT_SENT_ALREADY_SENT: (
                m.DeliveryState.DISPATCH_UNCERTAIN,
                S.DECIDED,
            ),
            m.SendOutcome.NOT_SENT_STALE_REVISION: (
                m.DeliveryState.SUPPRESSED,
                S.DECIDED,
            ),
            m.SendOutcome.NOT_SENT_CANCELLED: (m.DeliveryState.SUPPRESSED, S.CANCELLED),
            m.SendOutcome.NOT_SENT_SESSION_GONE: (m.DeliveryState.ABANDONED, S.STALE),
            m.SendOutcome.NOT_SENT_NOT_DELIVERABLE: (
                m.DeliveryState.DISPATCH_FAILED,
                S.BLOCKED,
            ),
            m.SendOutcome.NOT_SENT_BUSY: (m.DeliveryState.DISPATCH_FAILED, S.DECIDED),
            m.SendOutcome.NOT_SENT_ROUTE_REFUSED: (
                m.DeliveryState.DISPATCH_FAILED,
                S.DECIDED,
            ),
            m.SendOutcome.NOT_SENT_STALE_PANE: (
                m.DeliveryState.DISPATCH_FAILED,
                S.DECIDED,
            ),
            m.SendOutcome.NOT_SENT_EXPIRED: (
                m.DeliveryState.DISPATCH_FAILED,
                S.DECIDED,
            ),
            m.SendOutcome.NOT_SENT_SUPERSEDED: (m.DeliveryState.PENDING, S.DECIDED),
            m.SendOutcome.NOT_SENT_PAUSED: (m.DeliveryState.PENDING, S.DECIDED),
        }
        for outcome, (delivery_state, batch_state) in expected.items():
            with self.subTest(outcome=outcome):
                value = entry(task_id=str(box.uuid4()))
                self.box.register(value)
                ticket = self.box.decide(
                    box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
                ).plans[0]
                self.box.result(
                    report(
                        ticket,
                        outcome,
                        self.now,
                        takeover="fixture-takeover"
                        if outcome == m.SendOutcome.NOT_SENT_PAUSED
                        else None,
                    )
                )
                current = self.box.show(value["batch_id"])
                self.assertEqual(current["record"].state, batch_state)
                self.assertIsNone(current["record"].correction_floor)
                self.assertEqual(current["deliveries"][0].state, delivery_state)
                if (
                    delivery_state == m.DeliveryState.DISPATCH_FAILED
                    and batch_state == S.DECIDED
                ):
                    retry = self.box.deliver(current["deliveries"][0].id).plans[0]
                    self.assertEqual(retry.plan["body"]["attempt"], 2)
                    self.assertEqual(
                        retry.plan["body"]["text"], ticket.plan["body"]["text"]
                    )

    def test_expired_old_revision_reconcile_cannot_authorize_resend(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        current = self.box.show(value["batch_id"])
        delivery = current["deliveries"][0]
        self.box.register(revision(value, mac_floor=4))
        self.box.reconcile(m.ReconcileVerdict.NOT_DELIVERED, delivery_id=delivery.id)
        self.assertRefused(box.BoxRefusal.WRONG_STATE, self.box.deliver, delivery.id)
        self.assertIsNone(self.box.show(value["batch_id"])["record"].correction_floor)

    def test_write_once_evidence_has_database_backstop(self):
        value = self.fixture(S.DELIVERED)
        delivery = self.box.show(value["batch_id"])["deliveries"][0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.box.db.execute(
                "UPDATE delivery SET delivered_at=? WHERE id=?",
                ((self.now + timedelta(days=1)).isoformat(), delivery.id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.box.db.execute(
                "DELETE FROM floor_ledger WHERE batch=?", (value["batch_id"],)
            )

    def test_retry_safe_plan_failure_is_reissued_with_new_id(self):
        value = entry()
        ticket = self.box.register(value).plans[0]
        self.box.result(
            plan_id=ticket.plan["plan_id"], why=m.NoResultWhy.NO_RESULT_LINE
        )
        reissued = [
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.ACK
        ]
        self.assertEqual(len(reissued), 1)
        self.assertNotEqual(reissued[0].plan["plan_id"], ticket.plan["plan_id"])
        self.assertEqual(reissued[0].plan["body"], ticket.plan["body"])

    def test_reconcile_l4_uncertain_inside_correction_due_recovers_from_evidence(self):
        value = self.fixture(S.DELIVERED)
        task = value["task_id"]
        card = self.box.card_intent(
            task, "jim", purpose=box.CardPurpose.CORRECTION, by="jim"
        )
        self.box.card_posted(task, "jim", card.gen, "card", by="jim")
        ticket = self.box.decide(
            box.DecisionKind.CORRECTION,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
            card_gen=card.gen,
            card_ref="card",
            supersedes=1,
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        self.box.register(progress(value, floor=4))
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, S.CORRECTION_DUE)
        self.box.reconcile(
            m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=current["deliveries"][-1].id
        )
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, S.DELIVERED)
        self.assertIsNone(current["record"].correction_floor)
        self.assertIsNotNone(current["deliveries"][-1].delivered_at)

    def test_unchanged_floor_progress_preserves_pending_or_uncertain_correction(self):
        for outcome in (m.SendOutcome.NOT_SENT_BUSY, m.SendOutcome.SEND_UNKNOWN):
            with self.subTest(outcome=outcome):
                value = self.fixture(S.CORRECTION_DUE)
                task = value["task_id"]
                card = self.box.card_intent(
                    task, "jim", purpose=box.CardPurpose.CORRECTION, by="jim"
                )
                self.box.card_posted(task, "jim", card.gen, "card", by="jim")
                ticket = self.box.decide(
                    box.DecisionKind.CORRECTION,
                    4,
                    value["batch_id"],
                    1,
                    answer(value),
                    by="jim",
                    card_gen=card.gen,
                    card_ref="card",
                    supersedes=1,
                ).plans[0]
                self.box.result(report(ticket, outcome, self.now))
                self.box.register(progress(value, 2))
                current = self.box.show(value["batch_id"])["record"]
                self.assertEqual(current.state, S.DECIDED)
                self.assertIsNone(current.correction_floor)

    def test_correction_keeps_older_uncertain_row_reconcilable(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        card = self.box.card_intent(
            "fixture", "jim", purpose=box.CardPurpose.CORRECTION, by="jim"
        )
        self.box.card_posted("fixture", "jim", card.gen, "card", by="jim")
        correction = self.box.decide(
            box.DecisionKind.CORRECTION,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
            card_gen=card.gen,
            card_ref="card",
            supersedes=1,
        ).plans[0]
        old = self.box.show(value["batch_id"])["deliveries"][0]
        self.assertEqual(old.state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.box.result(report(correction, m.SendOutcome.NOT_SENT_BUSY, self.now))
        self.box.reconcile(m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=old.id)
        current = self.box.show(value["batch_id"])
        old = current["deliveries"][0]
        self.assertEqual(old.state, m.DeliveryState.OBSERVED)
        self.assertEqual(old.delivered_via, m.DeliveryEvidence.SEEN_IN_PANE)
        self.assertIsNotNone(old.delivered_at)
        self.assertEqual(current["record"].state, S.DECIDED)
        self.assertEqual(
            current["deliveries"][-1].state, m.DeliveryState.DISPATCH_FAILED
        )

    def test_session_ended_sync_cannot_close_or_revive_a_leased_batch(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        sync = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        status = m.Status(
            schema="qe.status/1",
            task_id="fixture",
            takeovers=[],
            notices=[],
            batches=[
                m.StatusBatch(
                    batch_id=value["batch_id"],
                    batch_rev=1,
                    state=m.BatchState.STALE,
                    mac_floor=m.EscalationLevel(3),
                    acked_floor=None,
                    context="fixture",
                    callback_bot="jim",
                    session_alive=False,
                    answered_in_pane=False,
                    local_resolution=None,
                    outbound=[],
                    box_notes=[],
                )
            ],
        )
        payload = m.SyncPayload(
            registrations=[],
            progress=[],
            status=status,
            plan_results=[],
            more=False,
            cursor=None,
            oversized=[],
        )
        self.box.result(report(sync, None, self.now, payload=payload))
        self.assertEqual(self.box.show(value["batch_id"])["record"].state, S.DECIDED)
        self.box.tick()
        self.assertEqual(self.box.show(value["batch_id"])["record"].state, S.DECIDED)
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, S.STALE)
        self.assertIsNone(current["record"].correction_floor)
        self.box.tick()
        self.assertEqual(self.box.show(value["batch_id"])["record"].state, S.TOMBSTONED)
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        self.assertEqual(self.box.show(value["batch_id"])["record"].state, S.TOMBSTONED)

    def test_stale_uncertain_delivery_refuses_reconcile_and_only_closes(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        sync = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        status = m.Status(
            schema="qe.status/1",
            task_id="fixture",
            takeovers=[],
            notices=[],
            batches=[
                m.StatusBatch(
                    batch_id=value["batch_id"],
                    batch_rev=1,
                    state=m.BatchState.STALE,
                    mac_floor=m.EscalationLevel(3),
                    acked_floor=None,
                    context="fixture",
                    callback_bot="jim",
                    session_alive=False,
                    answered_in_pane=False,
                    local_resolution=None,
                    outbound=[],
                    box_notes=[],
                )
            ],
        )
        payload = m.SyncPayload(
            registrations=[],
            progress=[],
            status=status,
            plan_results=[],
            more=False,
            cursor=None,
            oversized=[],
        )
        self.box.result(report(sync, None, self.now, payload=payload))
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, S.STALE)
        for verdict in m.ReconcileVerdict:
            self.assertRefused(
                box.BoxRefusal.WRONG_STATE,
                self.box.reconcile,
                verdict,
                delivery_id=current["deliveries"][0].id,
            )
        self.box.tick()
        self.assertEqual(self.box.show(value["batch_id"])["record"].state, S.TOMBSTONED)

    def test_sync_transport_cache_is_purged_with_full_records(self):
        import json

        value = entry()
        question = dict(
            value["questions"][0],
            text="Unique retention sentinel for the erased question",
        )
        question["digest"] = m.digest(
            {
                k: v
                for k, v in question.items()
                if k not in ("digest", "retired_rev", "truncated")
            }
        )
        value = dict(value, questions=[question])
        value.pop("content_digest")
        value["content_digest"] = m.digest(value)
        sync = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        payload = m.SyncPayload(
            registrations=[value],
            progress=[],
            status=m.Status(
                schema="qe.status/1", task_id="", batches=[], takeovers=[], notices=[]
            ),
            plan_results=[],
            more=False,
            cursor=None,
            oversized=[],
        )
        self.box.result(report(sync, None, self.now, payload=payload))
        self.assertIn(
            question["text"],
            json.dumps(
                dict(
                    self.box.db.execute(
                        "SELECT result_json FROM plan WHERE id=?",
                        (sync.plan["plan_id"],),
                    ).fetchone()
                )
            ),
        )
        self.box.cancel(value["batch_id"], from_mac=True)
        self.now += timedelta(days=31)
        self.box.tick()
        self.now += timedelta(days=89)
        recent = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        self.box.result(report(recent, None, self.now, payload=payload))
        self.now += timedelta(days=2)
        self.box.tick()
        for table, columns in (
            ("revision", ("content_json",)),
            ("progress", ("json",)),
            ("plan", ("body_json", "result_json", "late_result_json")),
            ("delivery", ("text", "late_result_json")),
        ):
            for row in self.box.db.execute(f"SELECT {','.join(columns)} FROM {table}"):
                self.assertNotIn(question["text"], json.dumps(tuple(row)))

    def test_concurrent_tick_does_not_finalize_an_inflight_retry_safe_plan(self):
        value = entry()
        ticket = self.box.register(value).plans[0]
        self.box.tick()
        self.box.result(report(ticket, None, self.now))
        original = next(
            p
            for p in self.box.show(value["batch_id"])["plans"]
            if p.id == ticket.plan["plan_id"]
        )
        self.assertEqual(original.state, box.PlanState.FINAL_RESULT)
        self.assertIsNone(original.late_result_json)

    def test_handoff_tick_only_resends_latest_pair_set(self):
        value = entry()
        self.box.register(value)
        self.box.handoff("fixture", "jim")
        value = revision(value)
        self.box.register(value)
        self.box.handoff("fixture", "jim")
        self.now += timedelta(hours=1)
        payloads = self.box.tick().handoffs
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["batches"][0]["batch_rev"], 2)

    def test_sync_more_emits_next_page_immediately(self):
        ticket = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        payload = m.SyncPayload(
            registrations=[],
            progress=[],
            status=m.Status(
                schema="qe.status/1", task_id="", batches=[], takeovers=[], notices=[]
            ),
            plan_results=[],
            more=True,
            cursor="next-fixture-page",
            oversized=[],
        )
        output = self.box.result(report(ticket, None, self.now, payload=payload))
        self.assertEqual(len(output.plans), 1)
        self.assertEqual(output.plans[0].plan["action"], m.PlanAction.SYNC)
        self.assertEqual(output.plans[0].plan["body"]["cursor"], "next-fixture-page")
