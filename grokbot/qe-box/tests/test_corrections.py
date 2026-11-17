#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""§7.9 12b–12h: current evidence outranks mutable dispatch state."""

from datetime import timedelta

from test_registration import BoxTest, entry, revision, progress, box
from test_decisions import answer

m = box.m


def report(
    ticket, outcome, now, *, payload=None, takeover=None, skew=0, replayed=False
):
    result = dict(
        schema="qe.result/1",
        plan_id=ticket.plan["plan_id"],
        plan_digest=ticket.plan["plan_digest"],
        lease_id=ticket.plan["lease_id"],
        action=ticket.plan["action"],
        ok=True,
        error_kind=None,
        send=outcome,
        route_used=m.Route.REX_SEND if outcome else None,
        pane_check=m.PaneCheck.SKIPPED_NO_MARKER if outcome else None,
        takeover_id=takeover,
        local_answer_dispatched=False,
        replayed=replayed,
        mac_batch_rev=1 if outcome else None,
        mac_state=m.BatchState.BOX_ANSWERED if outcome else None,
        mac_now=(now + timedelta(seconds=skew)).isoformat(),
        payload=payload,
    )
    return m.RESULT_PREFIX + box.encoded(
        m.parse_result({**result, "result_digest": m.digest(result)})
    )


class CorrectionTest(BoxTest):
    def assertState(self, value, state, correction=None, kept=None):
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, state)
        self.assertEqual(current["record"].correction_floor, correction)
        if kept is not None:
            self.assertEqual(current["record"].kept_resolution, kept)
        return current

    def l3(self, outcome=m.SendOutcome.SENT_ECHOED):
        value = entry(task_id=str(box.uuid4()))
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, outcome, self.now))
        return value, ticket

    def l4(self, value, *, correction=False, supersedes=None):
        purpose = box.CardPurpose.CORRECTION if correction else box.CardPurpose.ASK
        self.box.handoff(value["task_id"], value["callback_bot"], purpose=purpose)
        card = self.box.card_intent(
            value["task_id"], value["callback_bot"], purpose=purpose, by="jim"
        )
        self.box.card_posted(
            value["task_id"], "jim", card.gen, f"card-{card.gen}", by="jim"
        )
        return self.box.decide(
            box.DecisionKind.CORRECTION if correction else box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            value["batch_rev"],
            answer(value),
            by="jim",
            card_gen=card.gen,
            card_ref=f"card-{card.gen}",
            supersedes=supersedes,
        ).plans[0]

    def ack(self, value, delivery):
        ticket = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        note = m.StatusBoxNote(
            delivery_id=delivery.id,
            attempt=delivery.attempt,
            plan_id="fixture",
            resolution_rev=delivery.resolution_rev,
            purpose=delivery.purpose,
            state=m.BoxNoteState.ECHOED,
            acked_rev=delivery.resolution_rev,
        )
        status = m.Status(
            schema="qe.status/1",
            task_id=value["task_id"],
            takeovers=[],
            notices=[],
            batches=[
                m.StatusBatch(
                    batch_id=value["batch_id"],
                    batch_rev=value["batch_rev"],
                    state=m.BatchState.BOX_ANSWERED,
                    mac_floor=m.EscalationLevel(3),
                    acked_floor=None,
                    context="fixture",
                    callback_bot="jim",
                    session_alive=True,
                    answered_in_pane=False,
                    local_resolution=None,
                    outbound=[],
                    box_notes=[note],
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
        self.box.result(report(ticket, None, self.now, payload=payload))

    def test_12b_delivered_l3_floor_raise_requires_l4_correction(self):
        value, _ = self.l3()
        self.box.register(progress(value, floor=4))
        current = self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)
        self.assertRefused(
            box.BoxRefusal.WRONG_STATE,
            self.box.decide,
            box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
        )
        self.assertRefused(
            box.BoxRefusal.FLOOR_TOO_HIGH,
            self.box.decide,
            box.DecisionKind.CORRECTION,
            3,
            value["batch_id"],
            1,
            answer(value),
            supersedes=1,
        )
        ticket = self.l4(value, correction=True, supersedes=1)
        self.assertEqual(ticket.plan["body"]["resolution_rev"], 2)
        current = self.assertState(value, box.BoxBatchState.DECIDED)
        self.assertEqual(current["deliveries"][-1].state, m.DeliveryState.PENDING)

    def test_12d_local_resolution_considered_after_same_progress_event(self):
        for mode, expected_floor in (
            ("revision", 4),
            ("escalated", 3),
            ("same_progress", 3),
        ):
            with self.subTest(mode=mode):
                value = entry(floor=1)
                self.box.register(value)
                if mode != "same_progress":
                    self.box.register(progress(value, local=1, local_level=1))
                if mode == "revision":
                    self.box.register(revision(value, mac_floor=4))
                else:
                    self.box.register(
                        progress(
                            value,
                            2,
                            local=1 if mode == "same_progress" else None,
                            local_level=1 if mode == "same_progress" else None,
                            escalated=m.EscalationReason.LOW_CONFIDENCE,
                        )
                    )
                self.assertState(
                    value, box.BoxBatchState.CORRECTION_DUE, expected_floor, 1
                )

    def test_local_new_revision_below_l3_stays_local(self):
        value = entry(floor=1)
        self.box.register(value)
        self.box.register(progress(value, local=1, local_level=1))
        self.box.register(revision(value, mac_floor=2))
        self.assertState(value, box.BoxBatchState.LOCAL)

    def test_correction_handoff_raises_required_level_and_never_idle_closes(self):
        value = entry(floor=1)
        self.box.register(value)
        self.box.register(
            progress(
                value,
                local=1,
                local_level=1,
                escalated=m.EscalationReason.LOW_CONFIDENCE,
            )
        )
        self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 3, 1)
        self.box.handoff("fixture", "jim", purpose=box.CardPurpose.CORRECTION)
        self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)
        self.assertRefused(
            box.BoxRefusal.FLOOR_TOO_HIGH,
            self.box.decide,
            box.DecisionKind.CORRECTION,
            3,
            value["batch_id"],
            1,
            answer(value),
            supersedes=1,
        )
        self.now += timedelta(days=31)
        notices = self.box.tick().notices
        self.assertIn(f"correction_due:{value['batch_id']}", notices)
        self.assertNotIn(f"correction_due:{value['batch_id']}", self.box.tick().notices)
        self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)
        self.box.register(revision(value, mac_floor=4))
        self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)

    def test_12e_uncertain_old_l3_observation_preserves_pending_l4(self):
        value, _ = self.l3(m.SendOutcome.SEND_UNKNOWN)
        self.box.register(progress(value, floor=4))
        self.assertState(value, box.BoxBatchState.AWAITING_L3)
        old = self.box.show(value["batch_id"])["deliveries"][0]
        ticket = self.l4(value)
        self.box.result(report(ticket, m.SendOutcome.NOT_SENT_BUSY, self.now))
        self.box.reconcile(m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=old.id)
        self.assertState(value, box.BoxBatchState.DECIDED)

    def test_12f_below_floor_not_delivered_cannot_resume_after_clear(self):
        for origin in (m.PromptOrigin.UNKNOWN, m.PromptOrigin.HUMAN):
            for clear_first in (False, True):
                with self.subTest(origin=origin, clear_first=clear_first):
                    value, _ = self.l3(m.SendOutcome.SEND_UNKNOWN)
                    self.box.register(progress(value, floor=4))
                    old = self.box.show(value["batch_id"])["deliveries"][0]
                    takeover = str(box.uuid4())
                    self.box.db.execute(
                        "INSERT INTO takeover_seen VALUES (?,?,?,0)",
                        (takeover, value["batch_id"], origin.value),
                    )
                    if clear_first and origin == m.PromptOrigin.UNKNOWN:
                        plan = self.box.clear_takeover(takeover).plans[0]
                        self.box.result(report(plan, None, self.now))
                    self.box.reconcile(
                        m.ReconcileVerdict.NOT_DELIVERED, delivery_id=old.id
                    )
                    if not clear_first and origin == m.PromptOrigin.UNKNOWN:
                        plan = self.box.clear_takeover(takeover).plans[0]
                        self.box.result(report(plan, None, self.now))
                    row = self.box.show(value["batch_id"])["deliveries"][0]
                    self.assertEqual(row.state, m.DeliveryState.SUPPRESSED)
                    self.assertEqual(row.suppress_cause, m.SuppressCause.FLOOR_RAISED)
                    self.assertIsNone(row.paused_by)
                    self.assertState(value, box.BoxBatchState.AWAITING_L3)

    def test_12g_delivered_correction_is_newest_relevant_resolution(self):
        value, _ = self.l3()
        self.box.register(progress(value, floor=4))
        ticket = self.l4(value, correction=True, supersedes=1)
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        self.assertState(value, box.BoxBatchState.DELIVERED)
        self.box.register(revision(value, mac_floor=4))
        self.assertState(value, box.BoxBatchState.AWAITING_L3)

    def test_12g_pending_correction_does_not_erase_delivered_lower_answer(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                value, _ = self.l3()
                self.box.register(progress(value, floor=4))
                if pending:
                    ticket = self.l4(value, correction=True, supersedes=1)
                    self.box.result(
                        report(ticket, m.SendOutcome.NOT_SENT_BUSY, self.now)
                    )
                self.box.register(revision(value, mac_floor=4))
                self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)

    def test_12h_evidence_write_once_survives_applied_and_revision(self):
        for outcome in (
            m.SendOutcome.SENT_ECHOED,
            m.SendOutcome.SENT_NO_ECHO,
            m.SendOutcome.SEND_UNKNOWN,
        ):
            with self.subTest(outcome=outcome):
                value, _ = self.l3(outcome)
                delivered = self.box.show(value["batch_id"])["deliveries"][0]
                before = (delivered.delivered_at, delivered.delivered_via)
                self.now += timedelta(seconds=1)
                self.ack(value, delivered)
                row = self.box.show(value["batch_id"])["deliveries"][0]
                self.assertEqual(row.state, m.DeliveryState.APPLIED)
                self.assertEqual(
                    (row.delivered_at, row.delivered_via),
                    before if before[0] else (self.now, m.DeliveryEvidence.ACKED),
                )
                self.box.register(progress(value, floor=4))
                self.assertState(value, box.BoxBatchState.CORRECTION_DUE, 4, 1)
                correction = self.l4(value, correction=True, supersedes=1)
                self.box.result(report(correction, m.SendOutcome.SENT_ECHOED, self.now))
                delivery = self.box.show(value["batch_id"])["deliveries"][-1]
                self.ack(value, delivery)
                self.box.register(revision(value, mac_floor=4))
                self.assertState(value, box.BoxBatchState.AWAITING_L3)

    def test_ack_only_evidence_leaves_decided(self):
        value, _ = self.l3(m.SendOutcome.SEND_UNKNOWN)
        delivery = self.box.show(value["batch_id"])["deliveries"][0]
        self.ack(value, delivery)
        self.assertState(value, box.BoxBatchState.DECIDED)
