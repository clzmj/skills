#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Box plans cross the real Mac broker boundary against private fixtures."""

import base64
import tempfile
import subprocess
import threading
import time
import io
import sys
import json
import sqlite3
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import unittest
import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import question_model as m
import question_ledger as ledger
import question_broker as broker
import question_routes as routes
from test_question_ledger import (
    asker,
    question,
    native_call,
    classification,
    registration,
    progress,
    outbound,
    resolution,
)


def plan(action, body):
    value = dict(
        schema="qe.plan/1",
        plan_id=str(uuid4()),
        action=action,
        issued_by="dinesh",
        lease_id=str(uuid4()) if action == m.PlanAction.SEND else None,
        body=body,
    )
    return m.parse_plan({**value, "plan_digest": m.digest(value)})


def send_plan(batch, **changes):
    body = dict(
        delivery_id=str(uuid4()),
        attempt=1,
        resend_after_reconcile=False,
        issued_at=datetime.now(timezone.utc).isoformat(),
        valid_s=480,
        batch_id=str(batch.id),
        batch_rev=batch.rev,
        floor=3,
        resolution_rev=1,
        purpose=m.DeliveryPurpose.ANSWER,
        target_actor=batch.asker.actor,
        child_actor=None,
        rex_session="fixture-session",
        rex_block="fixture-block",
        session_marker=None,
        allowed_routes=[m.Route.REX_SEND],
        codex_thread=None,
        text='[qe:fixture:r1] An answer with "quotes", Unicode á,\nLF and\rCR',
        ack_token="f" * 32,
        echo_wait_s=0,
    )
    body.update(changes)
    return plan(m.PlanAction.SEND, body)


class StubRunner(routes.RouteRunner):
    def __init__(
        self,
        store,
        *,
        outcome=routes.RouteOutcome.ACCEPTED,
        echo=False,
        before_call=None,
    ):
        super().__init__()
        self.store, self.outcome, self.echo = store, outcome, echo
        self.before_call = before_call
        self.calls = []

    def capture(self, session, block):
        return "pane marker"

    def send(self, route, body, fence):
        if self.store.db.in_transaction:
            raise AssertionError("SQLite transaction held across route")
        if self.before_call:
            self.before_call()
        failure = fence()
        if failure:
            return routes.RouteResult(routes.RouteOutcome.FENCED, failure, route)
        self.calls.append(body["text"])
        if self.echo:
            self.store.record_lifecycle(
                m.Lifecycle(
                    m.ActorKey(body["target_actor"]),
                    m.LifecycleKind.PROMPT_SUBMITTED,
                    None,
                    m.PromptOrigin.BOX,
                    m.body_digest(body["text"]),
                    datetime.now(timezone.utc),
                ),
                body["batch_id"],
            )
        return routes.RouteResult(self.outcome, None, route)


class RunPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch.object(broker.callback.rex, "STATE", Path(self.temp.name))
        state.start()
        self.addCleanup(state.stop)
        self.store = ledger.Ledger(Path(self.temp.name) / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        cwd = Path(__file__).resolve().parents[3]
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
        ).strip()
        self.batch = self.store.open_or_attach(
            asker(cwd=str(cwd), head=head),
            (question(),),
            native_call("call-1"),
            classification(),
        ).batch
        self.runner = StubRunner(self.store)
        self.broker = broker.Broker(self.store, runner=self.runner, lock_wait=0)

    def test_forged_ack_digest_is_refused_and_low_floor_cannot_lower_current(self):
        entry = registration(self.batch)
        self.store.put_registration(entry)
        self.store.progress(progress(self.batch, 1))
        body = dict(
            batch_id=str(self.batch.id),
            batch_rev=1,
            content_digest="0" * 64,
            progress_seq=999,
            floor=4,
            owned=True,
        )
        self.assertFalse(self.broker.run_plan(plan(m.PlanAction.ACK, body))["ok"])
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.OPEN
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT acked_progress_seq FROM registration"
            ).fetchone()[0],
            0,
        )
        body["content_digest"] = entry["content_digest"]
        self.assertTrue(self.broker.run_plan(plan(m.PlanAction.ACK, body))["ok"])
        body["floor"] = 1
        self.assertTrue(self.broker.run_plan(plan(m.PlanAction.ACK, body))["ok"])
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).classification.floor, 4
        )
        row = self.store.db.execute(
            "SELECT acked_progress_seq,acked_floor FROM registration"
        ).fetchone()
        self.assertEqual(tuple(row), (1, 4))

    def test_real_route_runner_refuses_when_no_allowed_route_is_available(self):
        request = send_plan(self.batch, allowed_routes=[m.Route.CODEX_QUEUE])
        live = self.store.live_context(str(self.batch.id))
        with (
            patch.object(self.store, "live_context", return_value=live),
            patch.object(routes.subprocess, "run") as call,
        ):
            result = broker.Broker(self.store).run_plan(request)
        call.assert_not_called()
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_ROUTE_REFUSED)
        note = self.broker.box_note(request["body"]["delivery_id"])
        assert note is not None
        self.assertEqual(
            note.state,
            m.BoxNoteState.NOT_SENT,
        )

    def test_real_mid_send_expiry_is_unknown_and_box_answered(self):
        at = datetime.now(timezone.utc)
        clock = [at]
        request = send_plan(self.batch, issued_at=at.isoformat())
        staged = []

        def call(args, **kwargs):
            staged.append(kwargs["input"])
            clock[0] = at + timedelta(minutes=9)
            return subprocess.CompletedProcess(args, 0, "", "")

        live = self.store.live_context(str(self.batch.id))
        with (
            patch.object(self.store, "live_context", return_value=live),
            patch.object(routes.subprocess, "run", side_effect=call),
        ):
            result = broker.Broker(self.store, clock=lambda: clock[0]).run_plan(request)
        self.assertEqual(staged, [request["body"]["text"]])
        self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
        note = self.broker.box_note(request["body"]["delivery_id"])
        assert note is not None
        self.assertEqual(
            note.state,
            m.BoxNoteState.UNKNOWN,
        )
        self.assertEqual(result["mac_state"], m.BatchState.BOX_ANSWERED)

    def test_post_route_exceptions_emit_result_and_mark_unknown(self):
        for error in (sqlite3.OperationalError("busy"), RuntimeError("route failure")):
            with self.subTest(error=type(error).__name__):
                request = send_plan(self.batch)
                original = ledger.Ledger.finish_box_note
                calls = [0]

                def finish(store, *args):
                    calls[0] += 1
                    if args[-1] is m.BoxNoteState.SENT:
                        raise error
                    return original(store, *args)

                output = io.StringIO()
                with (
                    patch.object(ledger.Ledger, "finish_box_note", finish),
                    patch.object(
                        broker.routes, "RouteRunner", return_value=self.runner
                    ),
                    redirect_stdout(output),
                ):
                    code = broker.main(["run-plan", "--b64", broker.encode(request)])
                self.assertEqual(code, 0)
                result = m.parse_result(
                    json.loads(
                        base64.urlsafe_b64decode(output.getvalue().split()[1] + "==")
                    )
                )
                self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
                note = self.broker.box_note(request["body"]["delivery_id"])
                assert note is not None
                self.assertEqual(
                    note.state,
                    m.BoxNoteState.UNKNOWN,
                )
                self.assertEqual(result["mac_state"], m.BatchState.BOX_ANSWERED)

    def test_replay_refreshes_mac_now_and_result_digest(self):
        at = datetime.now(timezone.utc)
        clock = [at]
        instance = broker.Broker(self.store, runner=self.runner, clock=lambda: clock[0])
        request = send_plan(self.batch, issued_at=at.isoformat())
        first = instance.run_plan(request)
        clock[0] += timedelta(minutes=2)
        replay = instance.run_plan(request)
        self.assertEqual(replay["mac_now"], clock[0].isoformat(timespec="microseconds"))
        self.assertNotEqual(first["result_digest"], replay["result_digest"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_persistent_database_fault_after_route_still_returns_unknown(self):
        for failed_table in ("box_note", "plan_seen"):
            with self.subTest(table=failed_table):
                request = send_plan(self.batch)

                def fail_writes():
                    self.store.db.set_authorizer(
                        lambda action, table, *_: (
                            sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_UPDATE and table == failed_table
                            else sqlite3.SQLITE_OK
                        )
                    )

                runner = StubRunner(self.store, before_call=fail_writes)
                try:
                    result = broker.Broker(self.store, runner=runner).run_plan(request)
                finally:
                    self.store.db.set_authorizer(None)
                self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
                self.assertTrue(m.parse_result(result)["ok"])
                note = self.broker.box_note(request["body"]["delivery_id"])
                assert note is not None
                self.assertEqual(
                    note.state,
                    m.BoxNoteState.UNKNOWN,
                )

    def test_status_masks_old_ack_when_box_note_is_prepared_or_not_sent(self):
        request = send_plan(self.batch)
        self.broker.run_plan(request)
        identifier = request["body"]["delivery_id"]
        self.assertTrue(self.store.ack(identifier, request["body"]["ack_token"]))
        for state in (m.BoxNoteState.PREPARED, m.BoxNoteState.NOT_SENT):
            self.store.db.execute(
                "UPDATE box_note SET state=? WHERE delivery_id=?", (state, identifier)
            )
            self.assertIsNone(
                self.broker.status()["batches"][0]["box_notes"][0]["acked_rev"]
            )

    def test_task_scoped_sync_keeps_batchless_notices_for_selected_tasks(self):
        wanted = self.store.notice(m.NoticeKind.BUDGET_EXHAUSTED, "task-1", "limit")
        self.store.notice(m.NoticeKind.BUDGET_EXHAUSTED, "other-task", "limit")
        for tasks in (["task-1"], ["task-1", "task-2"]):
            result = self.broker.run_plan(
                plan(m.PlanAction.SYNC, dict(tasks=tasks, cursor=None, plan_ids=[]))
            )
            payload = result["payload"]
            assert payload is not None
            self.assertEqual(
                [item["id"] for item in payload["status"]["notices"]],
                [wanted],
            )

    def test_sync_cursor_survives_acknowledging_previous_page(self):
        expected = []
        for index in range(12):
            batch = self.store.open_or_attach(
                asker(actor=f"page-{index}", turn=f"page-{index}"),
                (question(f"page-{index}", "q" * 1800),),
                native_call(
                    f"page-{index}", actor=f"page-{index}", turn=f"page-{index}"
                ),
                classification(),
            ).batch
            self.store.put_registration(registration(batch))
            expected.append(str(batch.id))
        found, cursor = [], None
        while True:
            result = self.broker.run_plan(
                plan(m.PlanAction.SYNC, dict(tasks=[], cursor=cursor, plan_ids=[]))
            )
            payload = result["payload"]
            assert payload is not None
            for entry in payload["registrations"]:
                found.append(entry["batch_id"])
                self.store.ack_registration(
                    dict(
                        batch_id=entry["batch_id"],
                        batch_rev=entry["batch_rev"],
                        content_digest=entry["content_digest"],
                        progress_seq=0,
                        floor=m.EscalationLevel.L1_CHEAP,
                        owned=False,
                    )
                )
            if not payload["more"]:
                break
            cursor = payload["cursor"]
        self.assertEqual(found, expected)

    def test_ack_replays_and_cannot_lower_floor(self):
        entry = registration(self.batch)
        self.store.put_registration(entry)
        request = plan(
            m.PlanAction.ACK,
            dict(
                batch_id=str(self.batch.id),
                batch_rev=1,
                content_digest=entry["content_digest"],
                progress_seq=0,
                floor=3,
                owned=True,
            ),
        )
        first = self.broker.run_plan(request)
        second = self.broker.run_plan(request)
        self.assertTrue(first["ok"])
        self.assertTrue(second["replayed"])
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).classification.floor, 3
        )
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_OWNED
        )

    def test_tampered_plan_records_nothing(self):
        request = plan(
            m.PlanAction.CANCEL, dict(batch_id=str(self.batch.id), by="dinesh")
        )
        request["body"]["by"] = "jim"
        encoded = base64.urlsafe_b64encode(m.canonical(request)).decode().rstrip("=")
        with self.assertRaises(ValueError):
            broker.decode_plan(encoded)
        self.assertEqual(self.broker.seen_results([request["plan_id"]]), [])

    def test_send_without_ack_takes_ownership_and_delayed_ack_does_not_reopen(self):
        entry = registration(self.batch)
        self.store.put_registration(entry)
        request = send_plan(self.batch)
        result = self.broker.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(result["pane_check"], m.PaneCheck.SKIPPED_NO_MARKER)
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_ANSWERED
        )
        ack = plan(
            m.PlanAction.ACK,
            dict(
                batch_id=str(self.batch.id),
                batch_rev=1,
                content_digest=entry["content_digest"],
                progress_seq=0,
                floor=3,
                owned=True,
            ),
        )
        self.assertTrue(self.broker.run_plan(ack)["ok"])
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_ANSWERED
        )
        replayed = self.broker.run_plan(request)
        self.assertEqual(replayed["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_echo_and_unknown_have_durable_notes(self):
        for outcome, echoed, expected, state in (
            (
                routes.RouteOutcome.ACCEPTED,
                True,
                m.SendOutcome.SENT_ECHOED,
                m.BoxNoteState.ECHOED,
            ),
            (
                routes.RouteOutcome.UNKNOWN,
                False,
                m.SendOutcome.SEND_UNKNOWN,
                m.BoxNoteState.UNKNOWN,
            ),
        ):
            with self.subTest(expected=expected):
                runner = StubRunner(self.store, outcome=outcome, echo=echoed)
                request = send_plan(self.batch, text="delivery-" + str(uuid4()))
                result = broker.Broker(self.store, runner=runner).run_plan(request)
                self.assertEqual(result["send"], expected)
                note = self.broker.box_note(request["body"]["delivery_id"])
                assert note is not None
                self.assertEqual(note.state, state)
                self.assertEqual(result["mac_state"], m.BatchState.BOX_ANSWERED)

    def test_expired_busy_and_stale_revision_never_type(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=20)
        self.assertEqual(
            self.broker.run_plan(send_plan(self.batch, issued_at=old.isoformat()))[
                "send"
            ],
            m.SendOutcome.NOT_SENT_EXPIRED,
        )
        lock = os.open(
            Path(self.temp.name) / m.WORKER_LOCK.format(batch=self.batch.id),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(
                self.broker.run_plan(send_plan(self.batch))["send"],
                m.SendOutcome.NOT_SENT_BUSY,
            )
        finally:
            os.close(lock)
        stale = send_plan(self.batch)
        self.store.open_or_attach(
            self.batch.asker,
            (question("call-2"),),
            native_call("call-2"),
            classification(),
        )
        self.assertEqual(
            self.broker.run_plan(stale)["send"], m.SendOutcome.NOT_SENT_STALE_REVISION
        )
        self.assertEqual(self.runner.calls, [])

    def test_expiry_is_rechecked_after_lock_and_before_keystroke(self):
        at = datetime.now(timezone.utc)
        clock = [at]
        runner = StubRunner(
            self.store,
            before_call=lambda: clock.__setitem__(0, at + timedelta(minutes=9)),
        )
        instance = broker.Broker(self.store, runner=runner, clock=lambda: clock[0])
        request = send_plan(self.batch, issued_at=at.isoformat())
        result = instance.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        note = instance.box_note(request["body"]["delivery_id"])
        assert note is not None
        self.assertEqual(note.state, m.BoxNoteState.NOT_SENT)
        self.assertEqual(runner.calls, [])

    def test_blocked_cancelled_stale_and_session_ended_never_type(self):
        for state, expected in (
            (m.BatchState.BLOCKED, m.SendOutcome.NOT_SENT_NOT_DELIVERABLE),
            (m.BatchState.CANCELLED, m.SendOutcome.NOT_SENT_CANCELLED),
            (m.BatchState.STALE, m.SendOutcome.NOT_SENT_SESSION_GONE),
        ):
            with self.subTest(state=state):
                with (
                    tempfile.TemporaryDirectory() as directory,
                    ledger.Ledger(Path(directory) / "db") as store,
                ):
                    batch = store.open_or_attach(
                        self.batch.asker,
                        (question(),),
                        native_call("other"),
                        classification(),
                    ).batch
                    store.transition(
                        str(batch.id),
                        state,
                        block_cause=m.BlockCause.NO_DELIVERY_ROUTE
                        if state == m.BatchState.BLOCKED
                        else None,
                    )
                    runner = StubRunner(store)
                    result = broker.Broker(store, runner=runner).run_plan(
                        send_plan(batch)
                    )
                    self.assertEqual(result["send"], expected)
                    self.assertEqual(store.get_batch(str(batch.id)).state, state)
                    self.assertEqual(runner.calls, [])
        self.store.record_lifecycle(
            m.Lifecycle(
                self.batch.asker.actor,
                m.LifecycleKind.SESSION_ENDED,
                None,
                None,
                None,
                datetime.now(timezone.utc),
            )
        )
        self.assertEqual(
            self.broker.run_plan(send_plan(self.batch))["send"],
            m.SendOutcome.NOT_SENT_SESSION_GONE,
        )

    def test_unknown_takeover_pauses_clear_resumes_native_answer_cancels(self):
        takeover = m.Takeover(
            m.TakeoverId(uuid4()),
            self.batch.id,
            m.PromptOrigin.UNKNOWN,
            None,
            datetime.now(timezone.utc),
            None,
            None,
        )
        self.store.record_takeover(takeover)
        result = self.broker.run_plan(send_plan(self.batch))
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_PAUSED)
        self.assertEqual(result["takeover_id"], str(takeover.id))
        clear = plan(
            m.PlanAction.CLEAR_TAKEOVER, dict(takeover_id=str(takeover.id), by="dinesh")
        )
        self.assertTrue(self.broker.run_plan(clear)["ok"])
        self.assertEqual(
            self.broker.run_plan(send_plan(self.batch))["send"],
            m.SendOutcome.SENT_NO_ECHO,
        )
        self.store.record_lifecycle(
            m.Lifecycle(
                self.batch.asker.actor,
                m.LifecycleKind.NATIVE_ANSWER,
                None,
                None,
                None,
                datetime.now(timezone.utc),
            ),
            str(self.batch.id),
        )
        self.assertEqual(
            self.broker.run_plan(send_plan(self.batch))["send"],
            m.SendOutcome.NOT_SENT_CANCELLED,
        )

    def test_stale_pane_route_refusal_supersession_and_already_sent(self):
        self.assertEqual(
            self.broker.run_plan(send_plan(self.batch, session_marker="absent"))[
                "send"
            ],
            m.SendOutcome.NOT_SENT_STALE_PANE,
        )
        refused = broker.Broker(
            self.store,
            runner=StubRunner(self.store, outcome=routes.RouteOutcome.REFUSED),
        )
        self.assertEqual(
            refused.run_plan(send_plan(self.batch))["send"],
            m.SendOutcome.NOT_SENT_ROUTE_REFUSED,
        )
        sent = send_plan(self.batch)
        self.broker.run_plan(sent)
        reused = dict(sent["body"])
        self.assertEqual(
            self.broker.run_plan(plan(m.PlanAction.SEND, reused))["send"],
            m.SendOutcome.NOT_SENT_SUPERSEDED,
        )
        reused["attempt"] = 2
        self.assertEqual(
            self.broker.run_plan(plan(m.PlanAction.SEND, reused))["send"],
            m.SendOutcome.NOT_SENT_ALREADY_SENT,
        )
        reused["attempt"] = 1
        reused["resend_after_reconcile"] = True
        self.assertEqual(
            self.broker.run_plan(plan(m.PlanAction.SEND, reused))["send"],
            m.SendOutcome.NOT_SENT_SUPERSEDED,
        )

    def test_dispatched_local_answer_is_reported_and_pending_notice_survives(self):
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        message = outbound(self.batch)
        self.store.resolve(resolution(self.batch), (message,))
        claim = self.store.claim(str(message.id), "fixture-owner", m.Route.CODEX_QUEUE)
        assert claim is not None
        self.store.finish_claim(claim, m.DeliveryState.DISPATCHED)
        result = self.broker.run_plan(send_plan(self.batch))
        self.assertTrue(result["local_answer_dispatched"])
        state = self.broker.status()["batches"][0]
        self.assertEqual(
            [
                item["state"]
                for item in state["outbound"]
                if item["purpose"] == m.OutboundPurpose.ANSWER
            ],
            [m.DeliveryState.DISPATCHED],
        )

    def test_retry_payload_is_immutable_and_reconcile_preserves_text(self):
        request = send_plan(self.batch)
        unknown = broker.Broker(
            self.store,
            runner=StubRunner(self.store, outcome=routes.RouteOutcome.UNKNOWN),
        )
        unknown.run_plan(request)
        reconcile = plan(
            m.PlanAction.RECONCILE,
            dict(
                target=m.ReconcileTargetKind.BOX_DELIVERY,
                target_id=request["body"]["delivery_id"],
                verdict=m.ReconcileVerdict.NOT_DELIVERED,
            ),
        )
        self.assertTrue(self.broker.run_plan(reconcile)["ok"])
        retry = dict(
            request["body"],
            attempt=2,
            resend_after_reconcile=True,
            text="changed malicious text",
        )
        self.assertFalse(self.broker.run_plan(plan(m.PlanAction.SEND, retry))["ok"])
        note = self.broker.box_note(request["body"]["delivery_id"])
        assert note is not None
        self.assertEqual(note.text, request["body"]["text"])

    def test_cancel_plan_is_idempotent_and_never_delivers(self):
        cancel = plan(
            m.PlanAction.CANCEL, dict(batch_id=str(self.batch.id), by="dinesh")
        )
        self.assertTrue(self.broker.run_plan(cancel)["ok"])
        self.assertTrue(self.broker.run_plan(cancel)["replayed"])
        self.assertEqual(
            self.broker.status()["batches"][0]["state"], m.BatchState.CANCELLED
        )
        self.assertEqual(self.runner.calls, [])

    def test_lock_wait_expiry_is_binding_and_overlapping_replay_is_unknown(self):
        request = send_plan(self.batch, valid_s=1)
        lock = os.open(
            Path(self.temp.name) / m.WORKER_LOCK.format(batch=self.batch.id),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        fcntl.flock(lock, fcntl.LOCK_EX)

        def release():
            time.sleep(1.1)
            os.close(lock)

        releaser = threading.Thread(target=release)
        releaser.start()
        waiting = broker.Broker(self.store, runner=self.runner, lock_wait=2)
        result = waiting.run_plan(request)
        releaser.join()
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertEqual(self.runner.calls, [])

    def test_overlapping_replay_cannot_release_lease_before_original_stops_typing(self):
        snapshot_taken = threading.Event()
        overlap = send_plan(self.batch)
        with ThreadPoolExecutor(max_workers=1) as pool:

            def concurrent():
                with ledger.Ledger(self.store.path) as store:

                    def clock():
                        snapshot_taken.set()
                        return datetime.now(timezone.utc)

                    return broker.Broker(store, clock=clock, lock_wait=2).run_plan(
                        overlap
                    )

            class OverlapRunner(StubRunner):
                replay = None

                def send(inner, route, body, fence):
                    inner.replay = pool.submit(concurrent)
                    self.assertTrue(snapshot_taken.wait(1))
                    with self.assertRaises(FutureTimeout):
                        inner.replay.result(timeout=0.1)
                    return super().send(route, body, fence)

            runner = OverlapRunner(self.store)
            result = broker.Broker(self.store, runner=runner).run_plan(overlap)
            self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
            assert runner.replay is not None
            replayed = runner.replay.result(timeout=2)
        self.assertEqual(replayed["send"], m.SendOutcome.SEND_UNKNOWN)
        self.assertTrue(replayed["replayed"])
        self.assertEqual(len(runner.calls), 1)

    def test_orphaned_started_plan_replays_unknown_only_after_guard_is_free(self):
        class CrashedRunner(StubRunner):
            def send(inner, route, body, fence):
                raise KeyboardInterrupt("fixture process died after starting")

        request = send_plan(self.batch)
        with self.assertRaises(KeyboardInterrupt):
            broker.Broker(self.store, runner=CrashedRunner(self.store)).run_plan(
                request
            )
        output = io.StringIO()
        with redirect_stdout(output):
            code = broker.main(["run-plan", "--b64", broker.encode(request)])
        self.assertEqual(code, 0)
        result = m.parse_result(
            json.loads(base64.urlsafe_b64decode(output.getvalue().split()[1] + "=="))
        )
        self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
        self.assertTrue(result["replayed"])
        self.assertEqual(
            self.broker.seen_results([request["plan_id"]])[0]["result_b64"], None
        )

    def test_duplicate_execution_guard_wait_is_bounded_without_a_result(self):
        request = send_plan(self.batch)
        path = Path(self.temp.name) / f"qe-plan-{request['plan_id']}"
        lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before = time.monotonic()
            with self.assertRaises(BlockingIOError):
                self.broker.run_plan(request)
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(self.runner.calls, [])
        finally:
            os.close(lock)

    def test_route_child_keeps_guard_after_parent_dies(self):
        root = Path(self.temp.name)
        bindir = root / "bin"
        bindir.mkdir()
        stub = bindir / "rex"
        stub.write_text(
            f"#!{sys.executable}\n"
            + "import os, pathlib, sys, time\n"
            + 'root = pathlib.Path(os.environ["REX_VISIBLE_STATE_DIR"])\n'
            + "text = sys.stdin.buffer.read().decode()\n"
            + '(root / "route-started").write_text(text)\n'
            + "deadline = time.monotonic() + 5\n"
            + 'while not (root / "route-release").exists() and time.monotonic() < deadline: time.sleep(.01)\n'
        )
        stub.chmod(0o755)
        request = send_plan(self.batch)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        env.update(
            REX_VISIBLE_STATE_DIR=str(root), PATH=f"{bindir}:{os.environ['PATH']}"
        )
        args = [
            sys.executable,
            str(Path(broker.__file__)),
            "run-plan",
            "--b64",
            broker.encode(request),
        ]
        original = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        duplicate = None
        try:
            deadline = time.monotonic() + 3
            while not (root / "route-started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((root / "route-started").exists())
            original.kill()
            original.wait(timeout=1)
            duplicate = subprocess.Popen(
                args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                duplicate.communicate(timeout=0.2)
            (root / "route-release").touch()
            stdout, stderr = duplicate.communicate(timeout=2)
            self.assertEqual(duplicate.returncode, 0, stderr)
            result = m.parse_result(
                json.loads(base64.urlsafe_b64decode(stdout.split()[1] + "=="))
            )
            self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
            self.assertTrue(result["replayed"])
            self.assertEqual(
                (root / "route-started").read_bytes().decode(), request["body"]["text"]
            )
        finally:
            (root / "route-release").touch()
            if original.poll() is None:
                original.kill()
            original.communicate(timeout=2)
            if duplicate is not None and duplicate.poll() is None:
                duplicate.kill()
                duplicate.communicate(timeout=2)

    def test_post_send_transition_error_remains_unknown(self):
        original = self.store.transition

        def transition(batch_id, target, **kwargs):
            if target is m.BatchState.BOX_ANSWERED:
                original(batch_id, m.BatchState.STALE)
            return original(batch_id, target, **kwargs)

        with patch.object(self.store, "transition", side_effect=transition):
            result = self.broker.run_plan(send_plan(self.batch))
        self.assertTrue(result["ok"])
        self.assertEqual(result["send"], m.SendOutcome.SEND_UNKNOWN)
        self.assertEqual(len(self.runner.calls), 1)

    def test_old_prompt_echo_does_not_ack_a_new_attempt(self):
        request = send_plan(self.batch)
        self.store.record_lifecycle(
            m.Lifecycle(
                self.batch.asker.actor,
                m.LifecycleKind.PROMPT_SUBMITTED,
                None,
                m.PromptOrigin.BOX,
                m.body_digest(request["body"]["text"]),
                datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        result = self.broker.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)

    def test_changed_retry_does_not_raise_ownership_floor(self):
        request = send_plan(self.batch)
        self.broker.run_plan(request)
        retry = dict(
            request["body"],
            attempt=2,
            resend_after_reconcile=True,
            text="changed",
            floor=4,
        )
        result = self.broker.run_plan(plan(m.PlanAction.SEND, retry))
        self.assertFalse(result["ok"])
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).classification.floor, 3
        )

    def test_result_timestamp_width_is_stable_for_size_measurement(self):
        request = plan(
            m.PlanAction.CANCEL, dict(batch_id=str(self.batch.id), by="dinesh")
        )
        result = broker.empty_result(
            request, datetime(2026, 11, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(result["mac_now"], "2026-11-16T00:00:00.000000+00:00")

    def test_deeply_nested_invalid_plan_has_usage_exit_without_traceback(self):
        depth = sys.getrecursionlimit() + 50
        raw = (
            '{"plan_id":"'
            + str(uuid4())
            + '","action":"send","body":'
            + "[" * depth
            + "0"
            + "]" * depth
            + "}"
        )
        value = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        with self.assertRaises(ValueError):
            broker.decode_plan(value)
        with redirect_stdout(io.StringIO()):
            code = broker.main(["run-plan", "--b64", value])
        self.assertEqual(code, 64)

    def test_sync_pages_registrations_and_large_single_item_without_dropping(self):
        expected = []
        for index in range(18):
            batch = self.store.open_or_attach(
                asker(actor=f"actor-{index}", turn=f"turn-{index}"),
                (question(f"large-call-{index}", "q" * 1800),),
                native_call(
                    f"large-call-{index}", actor=f"actor-{index}", turn=f"turn-{index}"
                ),
                classification(),
            ).batch
            entry = registration(batch)
            self.store.put_registration(entry)
            expected.append(str(batch.id))
        found = []
        cursor = None
        while True:
            result = self.broker.run_plan(
                plan(m.PlanAction.SYNC, dict(tasks=[], cursor=cursor, plan_ids=[]))
            )
            self.assertTrue(result["ok"])
            self.assertLessEqual(len(broker.encode(result)), m.MAX_RESULT_B64)
            payload = result["payload"]
            assert payload is not None
            found += [item["batch_id"] for item in payload["registrations"]]
            if not payload["more"]:
                break
            cursor = payload["cursor"]
        self.assertEqual(found, expected)
        # A valid entry can exceed 16 KiB when many question options are full.
        entry = dict(registration(self.batch))
        question_row = dict(entry["questions"][0])
        question_row["options"] = [
            dict(index=index, label="L" * 200, description="D" * 500)
            for index in range(8)
        ]
        entry["questions"] = [dict(question_row, id=str(uuid4())) for _ in range(4)]
        entry["content_digest"] = m.digest(
            {key: value for key, value in entry.items() if key != "content_digest"}
        )
        self.store.put_registration(m.parse_register_entry(entry))
        result = self.broker.run_plan(
            plan(m.PlanAction.SYNC, dict(tasks=[], cursor=None, plan_ids=[]))
        )
        self.assertTrue(result["ok"])
        self.assertGreater(len(broker.encode(result)), m.MAX_RESULT_B64)
        self.assertLessEqual(len(broker.encode(result)), m.MAX_SYNC_SINGLE_B64)
        payload = result["payload"]
        assert payload is not None
        self.assertEqual(len(payload["registrations"]), 1)
        self.assertTrue(payload["more"])
        self.assertEqual(payload["oversized"], [])

    def test_sync_reports_stored_results_and_callback_running_mapping(self):
        request = send_plan(self.batch)
        self.broker.run_plan(request)
        result = self.broker.run_plan(
            plan(
                m.PlanAction.SYNC,
                dict(tasks=["task-1"], cursor=None, plan_ids=[request["plan_id"]]),
            )
        )
        payload = result["payload"]
        assert payload is not None
        self.assertEqual(payload["plan_results"][0]["plan_id"], request["plan_id"])
        for status, expected in (
            (broker.callback.Status.RUNNING, True),
            (broker.callback.Status.WAITING, True),
            (broker.callback.Status.IDLE, False),
            (broker.callback.Status.COMPLETED, False),
            (broker.callback.Status.FAILED, False),
            (broker.callback.Status.ENDED, False),
            (broker.callback.Status.ERROR, False),
        ):
            with self.subTest(status=status):
                self.assertEqual(broker.TURN_RUNNING[status], expected)
        with patch.object(
            broker.callback,
            "session_state",
            return_value={"status": broker.callback.Status.COMPLETED},
        ):
            late = send_plan(
                self.batch,
                allowed_routes=[m.Route.CODEX_QUEUE, m.Route.CODEX_QUEUE_RESUME],
                codex_thread="fixture",
            )
            runner = StubRunner(self.store)
            result = broker.Broker(self.store, runner=runner).run_plan(late)
            self.assertEqual(result["route_used"], m.Route.CODEX_QUEUE_RESUME)
