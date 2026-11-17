#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Spec §7.9 interleavings across the real box, broker and Mac ledger.

Only the terminal executables are fixtures; plan validation, leases, replay,
locks, fences, route selection and route subprocesses are production code.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qe_box as box
import question_broker as broker
import question_ledger as ledger
import question_routes as routes
from test_question_ledger import (
    asker,
    question,
    native_call,
    classification,
    registration,
)
from test_registration import BoxTest, progress, revision

m = box.m


class LeaseFenceTests(BoxTest):
    def setUp(self):
        super().setUp()
        echo_wait = patch.object(m, "ECHO_WAIT", timedelta(0))
        echo_wait.start()
        self.addCleanup(echo_wait.stop)
        self.now = datetime.now(timezone.utc)
        self.mac_now = self.now
        self.root = Path(self.temp.name)
        self.store = ledger.Ledger(self.root / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        cwd = Path(__file__).resolve().parents[3]
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
        ).strip()
        who = replace(asker(cwd=str(cwd), head=head), callback_bot="jim")
        q = replace(
            question(),
            digest=m.TextDigest(""),
            options=(
                m.Option(
                    m.OptionIndex(0), m.Untrusted("Standard", m.UntrustedSource.AGENT)
                ),
            ),
        )
        q = replace(q, digest=m.question_digest(q))
        self.batch = self.store.open_or_attach(
            who,
            (q,),
            native_call("call-1"),
            classification(m.EscalationLevel.L3_PRINCIPAL),
        ).batch
        raw = registration(self.batch)
        raw["questions"][0]["options"] = [
            dict(index=0, label="Standard", description=None)
        ]
        raw["content_digest"] = m.digest(
            {k: v for k, v in raw.items() if k != "content_digest"}
        )
        self.entry = m.parse_register_entry(raw)
        self.store.put_registration(self.entry)
        self.ack = self.box.register(self.entry).plans[0].plan
        self.log = self.root / "routes.jsonl"
        bindir = self.root / "bin"
        bindir.mkdir()
        script = f"""#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
program = pathlib.Path(sys.argv[0]).name
if program == "codex":
    messages = [arg.split("=", 1)[1] for arg in args if arg.startswith("--message=")]
    if len(messages) != 1:
        sys.exit(64)
    text = messages[0]
elif args and args[0] == "send":
    text = sys.stdin.read()
elif args and args[0] == "capture":
    print("pane marker")
    sys.exit(0)
else:
    sys.exit(64)
with open(os.environ["QE_ROUTE_LOG"], "a") as stream:
    stream.write(json.dumps([program, text]) + "\\n")
"""
        for name in ("rex", "codex"):
            executable = bindir / name
            executable.write_text(script)
            executable.chmod(0o755)
        env = patch.dict(
            os.environ,
            PATH=f"{bindir}:{os.environ['PATH']}",
            QE_ROUTE_LOG=str(self.log),
        )
        env.start()
        self.addCleanup(env.stop)
        state = patch.object(broker.callback.rex, "STATE", self.root)
        state.start()
        self.addCleanup(state.stop)
        self.mac = broker.Broker(
            self.store,
            runner=routes.RouteRunner(),
            clock=lambda: self.mac_now,
            lock_wait=0,
        )

    def decision(self):
        return dict(
            schema="qe.decision/1",
            answers=[
                dict(
                    question_id=self.entry["questions"][0]["id"],
                    choice=[0],
                    free_text="Use the standard implementation.",
                    rationale="It matches the existing interface.",
                )
            ],
            clearances=[],
        )

    def issue(self):
        with patch.object(m, "ECHO_WAIT", timedelta(0)):
            output = self.box.decide(
                box.DecisionKind.ANSWER, 3, self.entry["batch_id"], 1, self.decision()
            )
        return output.plans[0].plan

    def execute(self, plan):
        return self.mac.run_plan(plan)

    def submit(self, result):
        return self.box.result(m.RESULT_PREFIX + broker.encode(result))

    def shown(self):
        return self.box.show(self.entry["batch_id"])

    def delivery(self):
        return self.shown()["deliveries"][0]

    def sends(self):
        if not self.log.exists():
            return []
        return [
            text
            for _, text in map(json.loads, self.log.read_text().splitlines())
            if text != "\r"
        ]

    def advance(self, seconds, *, mac=True):
        self.now += timedelta(seconds=seconds)
        if mac:
            self.mac_now = self.now

    def none(self, plan):
        self.box.result(plan_id=plan["plan_id"], why=m.NoResultWhy.TOOL_ERROR)

    def assertLeased(self, plan):
        operations = (
            lambda: self.box.deliver(plan["body"]["delivery_id"]),
            lambda: self.box.decide(
                box.DecisionKind.ANSWER, 3, self.entry["batch_id"], 1, self.decision()
            ),
            lambda: self.box.reconcile(
                m.ReconcileVerdict.NOT_DELIVERED,
                delivery_id=plan["body"]["delivery_id"],
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(box.Refused) as caught:
                    operation()
                self.assertEqual(caught.exception.reason, box.BoxRefusal.LEASED)
                self.assertEqual(caught.exception.exit_code, 4)

    def test_01_queued_revision_applies_after_sent_result(self):
        plan = self.issue()
        newer = revision(self.entry, mac_floor=4, classification_floor=4)
        queued = self.box.register(newer)
        self.assertTrue(queued.queued)
        self.assertEqual(queued.plans, ())
        self.assertEqual(self.shown()["record"].latest_rev, 1)
        original = self.mac.runner.send

        def echoed(*args):
            outcome = original(*args)
            self.store.record_lifecycle(
                m.Lifecycle(
                    self.batch.asker.actor,
                    m.LifecycleKind.PROMPT_SUBMITTED,
                    None,
                    m.PromptOrigin.BOX,
                    m.body_digest(plan["body"]["text"]),
                    self.now,
                ),
                str(self.batch.id),
            )
            return outcome

        with patch.object(self.mac.runner, "send", side_effect=echoed):
            result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.SENT_ECHOED)
        self.submit(result)
        delivery = self.delivery()
        self.assertEqual(delivery.state, m.DeliveryState.OBSERVED)
        self.assertEqual((delivery.authorized_rev, delivery.authorized_floor), (1, 3))
        # r9/r11 precedence supersedes the old test-1 AWAITING_L3 wording.
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.CORRECTION_DUE)
        self.assertEqual(self.shown()["record"].correction_floor, 4)
        self.assertEqual(self.shown()["record"].latest_rev, 2)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.SEND for p in self.box.tick().plans)
        )
        self.assertEqual(len(self.sends()), 1)

    def test_02_mac_newer_revision_refuses_before_typing(self):
        plan = self.issue()
        self.box.register(revision(self.entry, mac_floor=4, classification_floor=4))
        self.store.bump_for_context(
            str(self.batch.id),
            ledger.LiveContext(self.batch.asker.head_sha, None, True, None),
        )
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_STALE_REVISION)
        self.submit(result)
        self.assertEqual(self.delivery().state, m.DeliveryState.SUPPRESSED)
        self.assertEqual(self.delivery().suppress_cause, m.SuppressCause.NEWER_REVISION)
        self.assertEqual(self.shown()["record"].latest_rev, 2)
        self.assertEqual(self.shown()["record"].floor, 4)
        self.assertIsNone(self.shown()["record"].correction_floor)
        self.assertEqual(self.sends(), [])

    def test_03_04_expiry_applies_queue_and_late_result_is_evidence(self):
        plan = self.issue()
        result = self.execute(plan)
        self.box.register(revision(self.entry, mac_floor=4, classification_floor=4))
        self.advance(600)
        output = self.box.tick()
        self.assertEqual(self.delivery().state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.assertEqual(self.shown()["record"].latest_rev, 2)
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.AWAITING_L3)
        self.assertIsNone(self.shown()["record"].correction_floor)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.SEND for p in output.plans)
        )
        self.submit(result)
        self.assertEqual(self.delivery().state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.assertIsNotNone(self.delivery().late_result_json)
        self.assertEqual(
            next(p for p in self.shown()["plans"] if p.id == plan["plan_id"]).state,
            box.PlanState.FINAL_EXPIRED,
        )

    def test_05_wrong_lease_and_unknown_plan_do_not_change_state(self):
        plan = self.issue()
        result = self.execute(plan)
        for field, refusal in (
            ("lease_id", box.BoxRefusal.LEASE_MISMATCH),
            ("plan_id", box.BoxRefusal.UNKNOWN_PLAN),
        ):
            with self.subTest(field=field):
                bad = dict(result, **{field: str(uuid4())})
                bad["result_digest"] = m.digest(
                    {k: v for k, v in bad.items() if k != "result_digest"}
                )
                self.assertRefused(refusal, self.submit, bad)
                self.assertEqual(self.delivery().state, m.DeliveryState.PENDING)

    def test_06_second_bot_cannot_decide_or_deliver_under_lease(self):
        self.assertLeased(self.issue())

    def test_07a_none_holds_queue_and_late_first_execution_until_death(self):
        plan = self.issue()
        self.none(plan)
        queued = self.box.register(
            revision(self.entry, mac_floor=4, classification_floor=4)
        )
        self.assertTrue(queued.queued)
        self.assertEqual(self.delivery().state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.assertEqual(
            next(p for p in self.shown()["plans"] if p.id == plan["plan_id"]).state,
            box.PlanState.FINAL_NONE,
        )
        self.assertLeased(plan)
        self.advance(470)
        late = self.execute(plan)
        self.assertEqual(late["send"], m.SendOutcome.SENT_NO_ECHO)
        self.submit(late)
        self.assertLeased(plan)
        self.advance(89)
        self.box.tick()
        self.assertEqual(self.shown()["record"].latest_rev, 1)
        self.advance(1)
        self.box.tick()
        self.assertEqual(self.shown()["record"].latest_rev, 2)
        self.assertEqual(self.delivery().state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.box.reconcile(
            m.ReconcileVerdict.SEEN_IN_PANE, delivery_id=self.delivery().id
        )
        self.assertEqual(self.delivery().state, m.DeliveryState.OBSERVED)
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.CORRECTION_DUE)
        self.assertEqual(self.shown()["record"].correction_floor, 4)
        self.assertEqual(self.shown()["record"].kept_resolution, 1)
        self.assertEqual(len(self.sends()), 1)

    def test_07a_not_delivered_after_raise_suppresses_without_resend(self):
        plan = self.issue()
        self.none(plan)
        self.box.register(revision(self.entry, mac_floor=4, classification_floor=4))
        self.advance(560)
        self.box.tick()
        self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, delivery_id=self.delivery().id
        )
        self.assertEqual(self.delivery().state, m.DeliveryState.SUPPRESSED)
        self.assertEqual(self.delivery().suppress_cause, m.SuppressCause.FLOOR_RAISED)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.SEND for p in self.box.tick().plans)
        )
        self.assertEqual(self.sends(), [])

    def test_07a_ii_10_never_started_plan_expires_on_mac(self):
        plan = self.issue()
        self.none(plan)
        self.advance(481)
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertEqual(self.sends(), [])

    def test_07a_iii_clock_residual_is_reported_and_holds_send(self):
        plan = self.issue()
        self.none(plan)
        self.advance(590)
        self.mac_now = self.now - timedelta(seconds=120)
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.submit(result)
        self.assertIn(
            box.BoxReport.CLOCK_SKEW,
            [report.kind for report in self.shown()["reports"]],
        )
        self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, delivery_id=self.delivery().id
        )
        self.assertRefused(
            box.BoxRefusal.CLOCK_SKEW_HOLD, self.box.deliver, self.delivery().id
        )

    def test_07b_08_lost_result_replay_and_delayed_result_are_evidence_only(self):
        plan = self.issue()
        original = self.execute(plan)
        self.none(plan)
        self.submit(original)
        replay = self.execute(plan)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["send"], original["send"])
        self.submit(replay)
        self.assertEqual(self.delivery().state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.assertIsNotNone(self.delivery().late_result_json)
        self.assertLeased(plan)
        self.assertEqual(len(self.sends()), 1)

    def test_09_accepted_result_is_idempotent(self):
        plan = self.issue()
        result = self.execute(plan)
        self.submit(result)
        before = self.shown()
        self.submit(result)
        self.assertEqual(self.shown(), before)
        self.assertEqual(len(self.sends()), 1)

    def test_11_retry_orders_type_exactly_once_and_supersede_old_attempt(self):
        first = self.issue()
        self.none(first)
        self.assertLeased(first)
        self.advance(560)
        self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, delivery_id=self.delivery().id
        )
        second = self.box.deliver(self.delivery().id).plans[0].plan
        self.assertEqual(second["body"]["attempt"], 2)
        self.assertTrue(second["body"]["resend_after_reconcile"])
        self.assertEqual(self.execute(first)["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.submit(self.execute(second))
        self.assertEqual(self.execute(first)["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        # Disable only time, retaining the actual persistent-attempt fence.
        self.mac_now = datetime.fromisoformat(first["body"]["issued_at"])
        fresh_old = dict(first, plan_id=str(uuid4()))
        fresh_old["plan_digest"] = m.digest(
            {k: v for k, v in fresh_old.items() if k != "plan_digest"}
        )
        self.assertEqual(
            self.execute(fresh_old)["send"], m.SendOutcome.NOT_SENT_SUPERSEDED
        )
        self.assertEqual(len(self.sends()), 1)

    def test_12_queued_progress_raise_suppresses_undelivered_decision(self):
        plan = self.issue()
        self.assertTrue(self.box.register(progress(self.entry, floor=4)).queued)
        # Expiry is the real broker's safe failure, with no transport call.
        self.advance(481)
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.submit(result)
        self.assertEqual(self.delivery().state, m.DeliveryState.SUPPRESSED)
        self.assertEqual(self.delivery().suppress_cause, m.SuppressCause.FLOOR_RAISED)
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.AWAITING_L3)
        self.assertIsNone(self.shown()["record"].correction_floor)
        self.box.register(progress(self.entry, 2, floor=3))
        self.assertEqual(self.shown()["record"].floor, 4)

    def takeover_order(self, *, clear_first):
        plan = self.issue()
        self.execute(plan)  # The Mac sent it, but the result line was lost.
        self.none(plan)
        self.advance(560)
        identifier = str(uuid4())
        self.store.record_takeover(
            m.Takeover(
                m.TakeoverId(identifier),
                self.batch.id,
                m.PromptOrigin.UNKNOWN,
                None,
                self.now,
                None,
                None,
            )
        )
        sync = next(
            ticket.plan
            for ticket in self.box.tick().plans
            if ticket.plan["action"] == m.PlanAction.SYNC
        )
        self.submit(self.execute(sync))
        if clear_first:
            for ticket in self.box.clear_takeover(identifier).plans:
                self.submit(self.execute(ticket.plan))
        for ticket in self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, delivery_id=self.delivery().id
        ).plans:
            self.submit(self.execute(ticket.plan))
        self.assertEqual(self.delivery().state, m.DeliveryState.PENDING)
        self.assertEqual(self.delivery().paused_by, None if clear_first else identifier)
        if not clear_first:
            self.assertFalse(
                any(
                    ticket.plan["action"] == m.PlanAction.SEND
                    for ticket in self.box.tick().plans
                )
            )
            for ticket in self.box.clear_takeover(identifier).plans:
                self.submit(self.execute(ticket.plan))
        self.assertIsNone(self.delivery().paused_by)
        retry = self.box.deliver(self.delivery().id).plans[0].plan
        self.assertTrue(retry["body"]["resend_after_reconcile"])
        self.assertEqual(self.execute(retry)["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(len(self.sends()), 2)

    def test_13_reconcile_then_clear_unknown_takeover_resumes(self):
        self.takeover_order(clear_first=False)

    def test_13_clear_then_reconcile_unknown_takeover_resumes(self):
        self.takeover_order(clear_first=True)

    def test_11_overlapping_same_plan_waits_for_real_route_and_replays_unknown(self):
        request = self.issue()
        snapshot = threading.Event()
        original = self.mac.runner.send
        with ThreadPoolExecutor(max_workers=1) as pool:

            def concurrent():
                with ledger.Ledger(self.store.path) as store:

                    def clock():
                        snapshot.set()
                        return self.mac_now

                    return broker.Broker(
                        store, runner=routes.RouteRunner(), clock=clock, lock_wait=2
                    ).run_plan(request)

            def overlap(*args):
                self.overlap = pool.submit(concurrent)
                self.assertTrue(snapshot.wait(1))
                with self.assertRaises(FutureTimeout):
                    self.overlap.result(timeout=0.1)
                self.assertLeased(request)
                return original(*args)

            with patch.object(self.mac.runner, "send", side_effect=overlap):
                result = self.execute(request)
            replay = self.overlap.result(timeout=2)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["send"], m.SendOutcome.SEND_UNKNOWN)
        self.submit(result)
        self.submit(replay)
        self.assertEqual(len(self.sends()), 1)

    def test_11_new_attempt_runs_before_old_attempt_and_only_types_once(self):
        first = self.issue()
        self.none(first)
        self.advance(560)
        self.box.reconcile(
            m.ReconcileVerdict.NOT_DELIVERED, delivery_id=self.delivery().id
        )
        second = self.box.deliver(self.delivery().id).plans[0].plan
        self.submit(self.execute(second))
        self.assertEqual(self.execute(first)["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertEqual(len(self.sends()), 1)

    def test_11_different_plan_with_same_attempt_is_superseded(self):
        first = self.issue()
        self.assertEqual(self.execute(first)["send"], m.SendOutcome.SENT_NO_ECHO)
        duplicate = dict(first, plan_id=str(uuid4()))
        duplicate["plan_digest"] = m.digest(
            {key: value for key, value in duplicate.items() if key != "plan_digest"}
        )
        self.assertEqual(
            self.execute(duplicate)["send"], m.SendOutcome.NOT_SENT_SUPERSEDED
        )
        self.assertEqual(len(self.sends()), 1)

    def test_14_send_owns_open_batch_and_delayed_ack_does_not_reopen(self):
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.OPEN
        )
        result = self.execute(self.issue())
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_ANSWERED
        )
        self.assertTrue(self.execute(self.ack)["ok"])
        current = self.store.get_batch(str(self.batch.id))
        self.assertEqual(current.state, m.BatchState.BOX_ANSWERED)
        self.assertEqual(current.classification.floor, 3)

    def test_15_clock_skew_report_holds_next_deliver(self):
        plan = self.issue()
        self.mac_now -= timedelta(seconds=120)
        self.submit(self.execute(plan))
        self.assertIn(
            box.BoxReport.CLOCK_SKEW,
            [report.kind for report in self.shown()["reports"]],
        )
        self.assertRefused(
            box.BoxRefusal.CLOCK_SKEW_HOLD, self.box.deliver, self.delivery().id
        )

    def test_16_binding_fence_rechecks_after_worker_lock_wait(self):
        plan = self.issue()
        original = self.mac.worker_lock

        @contextmanager
        def delayed_lock(batch, **kwargs):
            with original(batch, **kwargs) as acquired:
                self.mac_now += timedelta(seconds=481)
                yield acquired

        with patch.object(self.mac, "worker_lock", delayed_lock):
            result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertEqual(self.sends(), [])

    def test_16_expiry_between_rex_text_and_return_is_unknown(self):
        plan = self.issue()
        # Select Rex through real route selection by removing Codex's turn id.
        body = dict(plan["body"], codex_thread=None, allowed_routes=[m.Route.REX_SEND])
        request = dict(plan, body=body)
        request["plan_digest"] = m.digest(
            {k: v for k, v in request.items() if k != "plan_digest"}
        )
        original = routes.subprocess.run

        def crossing(*args, **kwargs):
            value = original(*args, **kwargs)
            if kwargs.get("input") == body["text"]:
                self.mac_now += timedelta(seconds=481)
            return value

        with patch.object(routes.subprocess, "run", side_effect=crossing):
            result = self.execute(request)
        self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_17_blocked_mac_is_unchanged_and_box_never_auto_retries(self):
        plan = self.issue()
        self.store.transition(
            str(self.batch.id),
            m.BatchState.BLOCKED,
            block_cause=m.BlockCause.NO_DELIVERY_ROUTE,
        )
        before = self.store.get_batch(str(self.batch.id))
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_NOT_DELIVERABLE)
        self.assertEqual(self.store.get_batch(str(self.batch.id)), before)
        self.submit(result)
        current = self.shown()["record"]
        self.assertEqual(current.state, box.BoxBatchState.BLOCKED)
        self.assertEqual(current.blocked_cause, box.BoxBlockCause.MAC_NOT_DELIVERABLE)
        self.assertFalse(
            any(p.plan["action"] == m.PlanAction.SEND for p in self.box.tick().plans)
        )
        self.assertEqual(self.sends(), [])

    def test_17_deliverability_is_an_allow_list_for_new_states(self):
        plan = self.issue()
        before = self.store.get_batch(str(self.batch.id))
        # Treat OPEN as a newly introduced state absent from the allow-list.
        # This seam preserves the real batch, parser and refusal path.
        with patch.object(
            m, "SEND_DELIVERABLE", m.SEND_DELIVERABLE - {m.BatchState.OPEN}
        ):
            result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_NOT_DELIVERABLE)
        self.assertEqual(self.store.get_batch(str(self.batch.id)), before)
        self.submit(result)
        self.assertEqual(
            self.shown()["record"].blocked_cause, box.BoxBlockCause.MAC_NOT_DELIVERABLE
        )
        self.assertEqual(self.sends(), [])
