#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Public API counterexamples from the PR-7a adversarial review."""

from copy import deepcopy
from datetime import timedelta

from test_registration import BoxTest, entry, revision, progress, box
from test_decisions import answer
from test_corrections import report

m = box.m


class ReviewRegressionTests(BoxTest):
    def l4(self, value, *, text=None):
        self.box.handoff(value["task_id"], "jim")
        card = self.box.card_intent(value["task_id"], "jim", by="jim")
        ref = f"review-card-{card.gen}"
        self.box.card_posted(value["task_id"], "jim", card.gen, ref, by="jim")
        return self.box.decide(
            box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            value["batch_rev"],
            answer(value, text=text),
            by="jim",
            card_gen=card.gen,
            card_ref=ref,
        ).plans[0]

    def sync(self, *, registrations=(), updates=()):
        ticket = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        payload = m.SyncPayload(
            registrations=list(registrations),
            progress=list(updates),
            status=m.Status(
                schema="qe.status/1",
                task_id="fixture",
                takeovers=[],
                notices=[],
                batches=[],
            ),
            plan_results=[],
            more=False,
            cursor=None,
            oversized=[],
        )
        self.box.result(report(ticket, None, self.now, payload=payload))

    def test_old_at_floor_observation_preserves_latest_pending_answer(self):
        value = entry()
        self.box.register(value)
        old_ticket = self.l4(value)
        self.box.result(report(old_ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        old = self.box.show(value["batch_id"])["deliveries"][0]
        value = revision(value, mac_floor=4, classification_floor=4)
        self.box.register(value)
        self.now += timedelta(minutes=15)  # Respect the public card repost window.
        latest_ticket = self.l4(value, text="Use the updated context.")
        self.box.result(report(latest_ticket, m.SendOutcome.NOT_SENT_BUSY, self.now))
        self.box.reconcile(m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=old.id)
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, box.BoxBatchState.DECIDED)
        self.assertEqual(
            current["deliveries"][-1].state, m.DeliveryState.DISPATCH_FAILED
        )
        self.assertTrue(self.box.deliver(current["deliveries"][-1].id).plans)

    def test_retry_after_blocked_send_and_raise_requires_new_authority(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        for attempt in range(3):
            self.box.result(report(ticket, m.SendOutcome.NOT_SENT_BUSY, self.now))
            if attempt < 2:
                row = self.box.show(value["batch_id"])["deliveries"][0]
                ticket = self.box.deliver(row.id).plans[0]
        self.assertEqual(
            self.box.show(value["batch_id"])["record"].state, box.BoxBatchState.BLOCKED
        )
        self.box.register(progress(value, floor=4))
        self.box.retry(value["batch_id"])
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, box.BoxBatchState.AWAITING_L3)
        self.assertEqual(current["deliveries"][0].state, m.DeliveryState.SUPPRESSED)
        self.assertTrue(self.l4(value, text="Reviewed at the raised floor."))

    def test_unknown_callback_retry_preserves_local_correction_precedence(self):
        value = entry(floor=1, callback_bot="missing")
        self.box.register(value)
        self.box.register(progress(value, local=1, local_level=1))
        self.box.register(progress(value, seq=2, floor=4))
        # Repair the public callback registry exactly as an operator retry requires.
        self.box.bots["missing"] = box.Bot("missing", "fixture", "Missing")
        self.box.retry(value["batch_id"])
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.state, box.BoxBatchState.CORRECTION_DUE)
        self.assertEqual((current.correction_floor, current.kept_resolution), (4, 1))

    def test_sync_rejects_semantically_invalid_registration_boundaries(self):
        for defect in (
            "digest",
            "identity_cap",
            "identity_control",
            "callback",
            "uuid",
            "question_cap",
            "question_control",
            "question_count",
        ):
            with self.subTest(defect=defect):
                raw = deepcopy(entry(task_id=f"review-{defect}"))
                if defect == "identity_cap":
                    raw["actor"] = "a" * (m.IDENTITY_CAPS["actor"] + 1)
                elif defect == "identity_control":
                    raw["session"] = "bad\x01session"
                elif defect == "callback":
                    raw["callback_bot"] = "invalid bot"
                elif defect == "uuid":
                    raw["batch_id"] = "not-a-uuid"
                elif defect in ("question_cap", "question_control"):
                    raw["questions"][0]["text"] = (
                        "q" * (m.QUESTION_TEXT_MAX + 1)
                        if defect == "question_cap"
                        else "bad\x01question"
                    )
                elif defect == "question_count":
                    raw["questions"] *= m.MAX_QUESTIONS + 1
                raw.pop("content_digest")
                raw["content_digest"] = "bad" if defect == "digest" else m.digest(raw)
                before = {item["record"].batch for item in self.box.status()}
                self.now += timedelta(hours=1)
                self.sync(registrations=[raw])
                self.assertEqual(
                    {item["record"].batch for item in self.box.status()}, before
                )
                # Rejected SYNC items must leave an operator-visible report.
                reports = self.box.db.execute(
                    "SELECT kind FROM report WHERE batch=?", (raw["batch_id"],)
                ).fetchall()
                self.assertTrue(reports)

    def test_sync_rejects_invalid_progress_without_changing_record(self):
        for defect in ("negative_seq", "incomplete_local", "inconsistent_raise"):
            with self.subTest(defect=defect):
                value = entry(task_id=f"review-{defect}")
                self.box.register(value)
                raw = dict(progress(value))
                if defect == "negative_seq":
                    raw["seq"] = -1
                elif defect == "incomplete_local":
                    raw["local_resolution"] = 1
                else:
                    raw["floor_raise"] = dict(
                        floor=4, cause="late_classification", classification=None
                    )
                before = self.box.show(value["batch_id"])
                self.now += timedelta(hours=1)
                self.sync(updates=[raw])
                current = self.box.show(value["batch_id"])
                self.assertEqual(current["record"], before["record"])
                self.assertEqual(current["progress"], before["progress"])
                self.assertGreater(len(current["reports"]), len(before["reports"]))

    def test_identical_answer_on_new_context_is_already_delivered(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        value = revision(value, head_sha="a" * 40)
        self.box.register(value)
        output = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 2, answer(value)
        )
        self.assertEqual(output.plans, ())
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, box.BoxBatchState.DELIVERED)
        self.assertEqual(len(current["deliveries"]), 1)

    def test_second_none_does_not_overwrite_late_real_result(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        kwargs = dict(plan_id=ticket.plan["plan_id"], why=m.NoResultWhy.TIMEOUT)
        self.box.result(**kwargs)
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        before = self.box.show(value["batch_id"])
        plan = next(p for p in before["plans"] if p.id == kwargs["plan_id"])
        evidence = (plan.late_result_json, before["deliveries"][0].late_result_json)
        self.assertIsNotNone(evidence[0])
        self.box.result(**kwargs)
        current = self.box.show(value["batch_id"])
        plan = next(p for p in current["plans"] if p.id == kwargs["plan_id"])
        self.assertEqual(
            (plan.late_result_json, current["deliveries"][0].late_result_json), evidence
        )

    def test_register_refuses_nonlocal_levels_in_local_progress(self):
        for level in (3, 4):
            with self.subTest(level=level):
                value = entry(floor=1)
                self.box.register(value)
                raw = dict(progress(value), local_resolution=1, local_level=level)
                before = self.box.show(value["batch_id"])
                self.assertRefused(box.BoxRefusal.VALIDATION, self.box.register, raw)
                current = self.box.show(value["batch_id"])
                self.assertEqual(current["record"], before["record"])
                self.assertEqual(current["progress"], before["progress"])

    def test_invalid_leased_sync_item_is_reported_without_losing_page(self):
        value = entry()
        self.box.register(value)
        self.box.decide(box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value))
        invalid = dict(value, content_digest="wrong")
        self.sync(registrations=[invalid])
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].state, box.BoxBatchState.DECIDED)
        self.assertTrue(
            any(
                r.kind == box.BoxReport.NON_MONOTONE_REGISTRATION
                for r in current["reports"]
            )
        )
        # The bad item is quarantined as a report; the enclosing typed result
        # and its other page items still finalize normally.
        plans = [p for p in current["plans"] if p.action == m.PlanAction.SYNC]
        self.assertEqual(plans[-1].state, box.PlanState.FINAL_RESULT)
