"""Box-clock watchdog timers and recovery remain observable through Box (§5.7)."""

from datetime import timedelta

from test_registration import BoxTest, box, entry, progress, revision
from test_decisions import answer
from test_corrections import report

m = box.m


class WatchdogTests(BoxTest):
    def awaiting(self):
        value = entry()
        self.box.register(value)
        self.box.handoff(value["task_id"], "jim")
        return value

    def active(self):
        value = self.awaiting()
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.box.card_posted("fixture", "jim", card.gen, "fixture-card", by="jim")
        return value, card

    def sync(
        self,
        *,
        registrations=(),
        updates=(),
        more=False,
        cursor=None,
        oversized=(),
        batches=(),
        takeovers=(),
    ):
        ticket = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        payload = m.SyncPayload(
            registrations=list(registrations),
            progress=list(updates),
            status=m.Status(
                schema="qe.status/1",
                task_id="fixture",
                takeovers=list(takeovers),
                notices=[],
                batches=list(batches),
            ),
            plan_results=[],
            more=more,
            cursor=cursor,
            oversized=list(oversized),
        )
        result = self.box.result(report(ticket, None, self.now, payload=payload))
        return ticket, result

    def test_handoff_waits_one_hour_and_rebuilds_latest_revision(self):
        value = self.awaiting()
        self.now += timedelta(hours=1) - timedelta(microseconds=1)
        self.assertEqual(self.box.tick().handoffs, ())
        self.now += timedelta(microseconds=1)
        resend = self.box.tick().handoffs
        self.assertEqual(len(resend), 1)
        self.assertEqual(resend[0]["batches"][0]["batch_rev"], 1)
        value = revision(value)
        self.box.register(value)
        self.box.handoff("fixture", "jim")
        self.assertEqual(self.box.tick().handoffs, ())
        self.now += timedelta(hours=1)
        self.assertEqual(self.box.tick().handoffs[0]["batches"][0]["batch_rev"], 2)

    def test_active_card_acknowledges_handoff_but_intent_does_not(self):
        self.awaiting()
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.now += timedelta(hours=1)
        self.assertEqual(len(self.box.tick().handoffs), 1)

        self.box.card_posted("fixture", "jim", card.gen, "found-card", by="jim")
        self.assertEqual(self.box.tick().handoffs, ())

    def test_handoff_resends_are_counted_only_after_bot_reports_send(self):
        value = self.awaiting()
        self.now += timedelta(hours=1)
        first = self.box.tick().handoffs[0]
        self.assertEqual(self.box.tick().handoffs[0], first)
        self.box.handoff("fixture", "jim", resend=True)
        self.assertEqual(self.box.tick().handoffs, ())
        self.now += timedelta(hours=1)
        self.assertEqual(len(self.box.tick().handoffs), 1)
        self.box.handoff("fixture", "jim", resend=True)
        self.now += timedelta(hours=1)
        self.assertEqual(len(self.box.tick().handoffs), 1)
        self.box.handoff("fixture", "jim", resend=True)
        self.now += timedelta(hours=1)
        exhausted = self.box.tick()
        self.assertEqual(exhausted.handoffs, ())
        self.assertIn(box.BoxReport.HANDOFF_UNACKED.value, exhausted.notices)
        self.assertNotIn(box.BoxReport.HANDOFF_UNACKED.value, self.box.tick().notices)
        refused = self.box.handoff("fixture", "jim", resend=True)
        self.assertEqual(refused.handoffs, ())
        self.assertNotIn(box.BoxReport.HANDOFF_UNACKED.value, refused.notices)
        reports = self.box.show(value["batch_id"])["reports"]
        self.assertEqual(
            sum(r.kind == box.BoxReport.HANDOFF_UNACKED for r in reports), 1
        )
        # A new reviewed revision receives its own retry budget.
        self.box.register(revision(value))
        self.box.handoff("fixture", "jim")
        self.now += timedelta(hours=1)
        self.assertEqual(len(self.box.tick().handoffs), 1)
        for _ in range(3):
            self.box.handoff("fixture", "jim", resend=True)
            self.now += timedelta(hours=1)
        self.assertIn(box.BoxReport.HANDOFF_UNACKED.value, self.box.tick().notices)
        self.assertNotIn(box.BoxReport.HANDOFF_UNACKED.value, self.box.tick().notices)
        reports = self.box.show(value["batch_id"])["reports"]
        self.assertEqual(
            sum(r.kind == box.BoxReport.HANDOFF_UNACKED for r in reports), 2
        )

    def test_answered_ask_and_correction_cards_do_not_remind(self):
        for correction in (False, True):
            with self.subTest(correction=correction):
                value = entry(task_id=str(box.uuid4()), floor=1 if correction else 3)
                self.box.register(value)
                purpose = (
                    box.CardPurpose.CORRECTION if correction else box.CardPurpose.ASK
                )
                if correction:
                    self.box.register(progress(value, local=1, local_level=1))
                else:
                    self.box.handoff(value["task_id"], "jim")
                card = self.box.card_intent(
                    value["task_id"], "jim", purpose=purpose, by="jim"
                )
                self.box.card_posted(
                    value["task_id"], "jim", card.gen, "answered", by="jim"
                )
                ticket = self.box.decide(
                    box.DecisionKind.CORRECTION
                    if correction
                    else box.DecisionKind.ANSWER,
                    4,
                    value["batch_id"],
                    1,
                    answer(value),
                    by="jim",
                    card_gen=card.gen,
                    card_ref="answered",
                    supersedes=1 if correction else None,
                ).plans[0]
                self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
                self.now += timedelta(hours=24)
                self.assertNotIn(
                    f"card_reminder:{value['task_id']}:jim:{card.gen}",
                    self.box.tick().notices,
                )

    def test_cancelled_card_with_close_request_does_not_remind(self):
        value, card = self.active()
        self.box.cancel(value["batch_id"], from_mac=True)
        self.assertTrue(self.box.show(value["batch_id"])["cards"][0].close_requested)
        self.now += timedelta(hours=1)
        self.assertNotIn(
            f"card_reminder:fixture:jim:{card.gen}", self.box.tick().notices
        )

    def test_partially_answered_card_still_reminds_for_remaining_pair(self):
        first = self.awaiting()
        second = entry()
        self.box.register(second)
        self.box.handoff("fixture", "jim")
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.box.card_posted("fixture", "jim", card.gen, "group-card", by="jim")
        ticket = self.box.decide(
            box.DecisionKind.ANSWER,
            4,
            first["batch_id"],
            1,
            answer(first),
            by="jim",
            card_gen=card.gen,
            card_ref="group-card",
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SENT_ECHOED, self.now))
        self.now += timedelta(hours=1)
        self.assertIn(f"card_reminder:fixture:jim:{card.gen}", self.box.tick().notices)

    def test_revision_bump_resets_handoff_timer_without_explicit_handoff(self):
        value = self.awaiting()
        self.now += timedelta(hours=2)
        self.box.register(revision(value))
        self.assertEqual(self.box.tick().handoffs, ())
        self.now += timedelta(hours=1) - timedelta(microseconds=1)
        self.assertEqual(self.box.tick().handoffs, ())
        self.box.register(progress(revision(value)))
        self.now += timedelta(microseconds=1)
        payload = self.box.tick().handoffs[0]
        self.assertEqual(payload["batches"][0]["batch_rev"], 2)
        recorded = self.box.handoff("fixture", "jim", resend=True).handoffs[0]
        self.assertEqual(recorded["handoff_id"], payload["handoff_id"])
        self.assertEqual(recorded["batches"], payload["batches"])
        # Each new revision gets exactly three resends, including this first one.
        for _ in range(2):
            self.now += timedelta(hours=1)
            self.assertEqual(len(self.box.tick().handoffs), 1)
            self.assertEqual(
                len(self.box.handoff("fixture", "jim", resend=True).handoffs), 1
            )
        self.now += timedelta(hours=1)
        exhausted = self.box.tick()
        self.assertEqual(exhausted.handoffs, ())
        self.assertIn(box.BoxReport.HANDOFF_UNACKED.value, exhausted.notices)
        self.assertEqual(self.box.handoff("fixture", "jim", resend=True).handoffs, ())

    def test_resend_never_escalates_unrelated_l3_batch(self):
        handed_off = self.awaiting()
        unreviewed = entry()
        self.box.register(unreviewed)
        self.now += timedelta(hours=1)
        payload = self.box.tick().handoffs[0]
        self.assertEqual(
            [b["batch_id"] for b in payload["batches"]], [handed_off["batch_id"]]
        )
        recorded = self.box.handoff("fixture", "jim", resend=True).handoffs[0]
        self.assertEqual(recorded["batches"], payload["batches"])
        self.assertEqual(
            self.box.show(unreviewed["batch_id"])["record"].state,
            box.BoxBatchState.AWAITING_L3,
        )

    def test_reminders_at_one_four_and_twenty_four_hours_then_stop(self):
        _, card = self.active()
        activated = self.now
        notice = f"card_reminder:fixture:jim:{card.gen}"
        for hours in (1, 4, 24):
            self.now = activated + timedelta(hours=hours) - timedelta(microseconds=1)
            self.assertNotIn(notice, self.box.tick().notices)
            self.now += timedelta(microseconds=1)
            self.assertIn(notice, self.box.tick().notices)
            self.assertNotIn(notice, self.box.tick().notices)
        self.now += timedelta(days=10)
        self.assertNotIn(notice, self.box.tick().notices)

    def test_reminders_start_at_posting_not_claim_and_closed_cards_stop(self):
        self.awaiting()
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.now += timedelta(hours=4)
        self.assertFalse(
            any(n.startswith("card_reminder:") for n in self.box.tick().notices)
        )
        self.box.card_posted("fixture", "jim", card.gen, "recovered-card", by="jim")
        self.assertFalse(
            any(n.startswith("card_reminder:") for n in self.box.tick().notices)
        )
        self.box.card_close("fixture", "jim", card.gen, by="jim")
        self.now += timedelta(days=1)
        self.assertFalse(
            any(n.startswith("card_reminder:") for n in self.box.tick().notices)
        )

    def test_uncertain_delivery_is_listed_for_reconciliation_after_ten_minutes(self):
        value = entry()
        self.box.register(value)
        ticket = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        ).plans[0]
        self.box.result(report(ticket, m.SendOutcome.SEND_UNKNOWN, self.now))
        delivery = self.box.show(value["batch_id"])["deliveries"][0]
        notice = f"reconcile:{delivery.id}"
        self.now += timedelta(minutes=10) - timedelta(microseconds=1)
        self.assertNotIn(notice, self.box.tick().notices)
        self.now += timedelta(microseconds=1)
        output = self.box.tick()
        self.assertIn(notice, output.notices)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.SEND for p in output.plans)
        )
        self.box.reconcile(m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=delivery.id)
        self.assertNotIn(notice, self.box.tick().notices)

    def test_correction_is_notified_weekly_and_never_closed_for_idleness(self):
        value = entry(floor=1)
        self.box.register(value)
        self.box.register(progress(value, local=1, local_level=1))
        self.box.register(progress(value, seq=2, floor=4))
        notice = f"correction_due:{value['batch_id']}"
        self.assertNotIn(notice, self.box.tick().notices)
        notified = self.box.show(value["batch_id"])["record"].correction_notified_at
        self.now += timedelta(days=7) - timedelta(microseconds=1)
        self.assertNotIn(notice, self.box.tick().notices)
        self.assertEqual(
            self.box.show(value["batch_id"])["record"].correction_notified_at, notified
        )
        self.now += timedelta(microseconds=1)
        self.assertIn(notice, self.box.tick().notices)
        self.now += timedelta(days=31)
        self.assertIn(notice, self.box.tick().notices)
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.state, box.BoxBatchState.CORRECTION_DUE)
        self.assertEqual(current.correction_floor, 4)
        self.assertEqual(current.kept_resolution, 1)

    def test_sync_scans_all_open_tasks_and_consumes_page_cursor(self):
        self.box.register(entry(task_id="z-task"))
        self.box.register(entry(task_id="a-task"))
        first, continuation = self.sync(more=True, cursor="next-page")
        self.assertEqual(first.plan["body"]["tasks"], ["a-task", "z-task"])
        self.assertEqual(len(continuation.plans), 1)
        second = continuation.plans[0]
        self.assertEqual(second.plan["action"], m.PlanAction.SYNC)
        self.assertEqual(second.plan["body"]["cursor"], "next-page")
        self.assertNotEqual(second.plan["plan_id"], first.plan["plan_id"])

    def test_watchdog_registration_and_progress_are_applied_and_reported_once(self):
        value = entry()
        self.sync(registrations=[value], updates=[progress(value, floor=4)])
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.floor, 4)
        notice = f"watchdog_registration:{value['batch_id']}"
        self.assertIn(notice, self.box.tick().notices)
        self.assertNotIn(notice, self.box.tick().notices)

    def test_unanswered_ack_and_cancel_plans_are_reissued_with_new_ids(self):
        value = entry()
        ack = self.box.register(value).plans[0]
        cancel = self.box.cancel(value["batch_id"]).plans[0]
        for original in (ack, cancel):
            self.box.result(
                plan_id=original.plan["plan_id"], why=m.NoResultWhy.TOOL_ERROR
            )
        retries = self.box.tick().plans
        for original in (ack, cancel):
            matches = [
                p for p in retries if p.plan["action"] == original.plan["action"]
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].plan["body"], original.plan["body"])
            self.assertNotEqual(matches[0].plan["plan_id"], original.plan["plan_id"])
        # Retry bookkeeping prevents the original failures from spawning duplicates.
        self.assertFalse(
            any(
                p.plan["action"] in (m.PlanAction.ACK, m.PlanAction.CANCEL)
                for p in self.box.tick().plans
            )
        )

    def test_clear_and_local_reconcile_plans_retry_without_granting_authority(self):
        value = entry()
        self.box.register(value)
        self.sync(
            batches=[
                m.StatusBatch(
                    batch_id=value["batch_id"],
                    batch_rev=1,
                    state=m.BatchState.BOX_OWNED,
                    mac_floor=m.EscalationLevel.L3_PRINCIPAL,
                    acked_floor=None,
                    context="fixture",
                    callback_bot="jim",
                    session_alive=True,
                    answered_in_pane=False,
                    local_resolution=None,
                    outbound=[
                        m.StatusOutbound(
                            id="uncertain-local",
                            purpose=m.OutboundPurpose.ANSWER,
                            resolution_rev=1,
                            state=m.DeliveryState.DISPATCH_UNCERTAIN,
                            route=None,
                            acked_rev=None,
                        )
                    ],
                    box_notes=[],
                )
            ],
            takeovers=[
                m.StatusTakeover(
                    id="unknown-takeover",
                    batch_id=value["batch_id"],
                    origin=m.PromptOrigin.UNKNOWN,
                    cleared=False,
                )
            ],
        )
        clear = self.box.clear_takeover("unknown-takeover").plans[0]
        reconcile = self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, outbound_id="uncertain-local"
        ).plans[0]
        for original in (clear, reconcile):
            self.box.result(plan_id=original.plan["plan_id"], why=m.NoResultWhy.TIMEOUT)
        retries = self.box.tick().plans
        for original in (clear, reconcile):
            retry = next(
                p for p in retries if p.plan["action"] == original.plan["action"]
            )
            self.assertEqual(retry.plan["body"], original.plan["body"])
            self.assertNotEqual(retry.plan["plan_id"], original.plan["plan_id"])
        self.assertEqual(
            self.box.show(value["batch_id"])["record"].state,
            box.BoxBatchState.AWAITING_L3,
        )

    def test_retry_safe_plan_waits_for_lease_ttl_and_success_stops_retry(self):
        value = entry()
        ack = self.box.register(value).plans[0]
        self.now += timedelta(minutes=10) - timedelta(microseconds=1)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.ACK for p in self.box.tick().plans)
        )
        self.now += timedelta(microseconds=1)
        retry = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.ACK
        )
        self.assertNotEqual(retry.plan["plan_id"], ack.plan["plan_id"])
        self.box.result(report(retry, None, self.now))
        self.now += timedelta(minutes=10)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.ACK for p in self.box.tick().plans)
        )

    def test_failed_sync_reissues_same_page_with_a_new_plan_id(self):
        initial = next(
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        )
        self.box.result(plan_id=initial.plan["plan_id"], why=m.NoResultWhy.TOOL_ERROR)
        retries = [
            p for p in self.box.tick().plans if p.plan["action"] == m.PlanAction.SYNC
        ]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].plan["body"], initial.plan["body"])
        self.assertNotEqual(retries[0].plan["plan_id"], initial.plan["plan_id"])
