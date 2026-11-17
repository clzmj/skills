#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Public ledger seams: transactions, state transitions and durable claims."""

import json
import fcntl
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import question_ledger as ledger
import question_guard
from question_model import BatchState


def asker(
    *, actor="actor-1", turn="turn-1", floor_brief=None, head="a" * 40, cwd="/repo"
):
    return ledger.m.parse_asker(
        {
            "agent": ledger.m.AgentKind.CODEX,
            "actor": actor,
            "root_actor": actor,
            "role": ledger.m.ActorRole.ROOT,
            "session": "session-1",
            "turn": turn,
            "task": "task-1",
            "rex_session": "rex-session",
            "rex_block": "rex-block",
            "cwd": cwd,
            "head_sha": head,
            "brief_digest": floor_brief,
            "callback_bot": "dinesh",
        }
    )


def question(call_id="call-1", text="Choose a direction"):
    qid = str(uuid4())
    return ledger.m.parse_question(
        {
            "id": qid,
            "origin_call": call_id,
            "text": {"text": text, "source": ledger.m.UntrustedSource.AGENT},
            "header": None,
            "options": [],
            "multi_select": False,
            "allows_free_text": True,
            "native_key": {"codex_id": "native-question-" + qid, "claude_text": None},
            "retired_rev": None,
            "digest": ledger.m.digest(
                {
                    "id": qid,
                    "text": text,
                    "header": None,
                    "options": [],
                    "multi_select": False,
                    "allows_free_text": True,
                }
            ),
        }
    )


def native_call(
    call_id, tool=ledger.m.QuestionTool.CODEX_BLOCKING, actor="actor-1", turn="turn-1"
):
    return ledger.m.NativeCall(
        tool,
        ledger.m.NativeCallId(call_id),
        ledger.m.ActorKey(actor),
        ledger.m.TurnId(turn),
        datetime.now(timezone.utc),
        False,
        None,
    )


def classification(floor=ledger.m.EscalationLevel.L1_CHEAP):
    return ledger.m.Classification(
        ledger.m.Category.DESIGN_CHOICE, False, (), (), floor, classified=True
    )


def registration(batch):
    m = ledger.m
    fields = {
        "schema": m.REGISTER_SCHEMA,
        "batch_id": str(batch.id),
        "batch_rev": batch.rev,
        "task_id": batch.asker.task,
        "actor": batch.asker.actor,
        "agent": batch.asker.agent,
        "role": batch.asker.role,
        "session": batch.asker.session,
        "rex_session": batch.asker.rex_session,
        "rex_block": batch.asker.rex_block,
        "cwd": batch.asker.cwd,
        "remote": "",
        "head_sha": batch.asker.head_sha,
        "callback_bot": batch.asker.callback_bot,
        "root_actor": batch.asker.root_actor,
        "root_session": batch.asker.session,
        "questions": [
            {
                "id": str(q.id),
                "text": q.text.text,
                "header": None,
                "options": [],
                "multi_select": q.multi_select,
                "allows_free_text": q.allows_free_text,
                "retired_rev": q.retired_rev,
                "truncated": False,
                "digest": q.digest,
            }
            for q in batch.questions
        ],
        "secret_present": False,
        "mac_floor": batch.classification.floor,
        "mac_hits": [],
        "brief_digest": batch.asker.brief_digest,
        "brief_ask_carlos": [],
        "brief_project": None,
        "classified": True,
        "category": batch.classification.category,
        "delegable": batch.classification.delegable,
        "sensitive_topics": [],
        "classification_floor": batch.classification.floor,
    }
    return {**fields, "content_digest": m.digest(fields)}


def progress(batch, seq, floor_raise=None):
    return ledger.m.parse_register_progress(
        {
            "schema": ledger.m.PROGRESS_SCHEMA,
            "batch_id": str(batch.id),
            "batch_rev": batch.rev,
            "seq": seq,
            "escalated": None,
            "recommendations": [],
            "local_resolution": None,
            "local_level": None,
            "floor_raise": floor_raise,
        }
    )


def outbound(batch, purpose=ledger.m.OutboundPurpose.ANSWER):
    m = ledger.m
    message = "[qe:test] answer"
    return m.OutboundMessage(
        m.OutboundId(uuid4()),
        batch.id,
        purpose,
        m.ResolutionRev(1),
        batch.asker.actor,
        None,
        message,
        m.digest(message),
        "token-1",
        (m.Route.CODEX_QUEUE,),
        m.DeliveryState.PENDING,
        None,
        None,
        None,
        None,
        ledger.now(),
    )


def resolution(batch):
    m = ledger.m
    return m.Resolution(
        batch.id,
        m.ResolutionRev(1),
        batch.rev,
        tuple(
            m.Answer(
                q.id,
                (),
                m.Untrusted("Previous answer", m.UntrustedSource.MODEL),
                m.Untrusted("Prior evidence", m.UntrustedSource.MODEL),
                (),
                None,
            )
            for q in batch.questions
        ),
        m.AnswererRef(
            m.AnswererKind.CODEX_EXEC, m.Model.GPT_LUNA, m.EscalationLevel.L1_CHEAP
        ),
        ledger.now(),
    )


def attempt(batch, ordinal=1, outcome=ledger.m.AttemptOutcome.FAILED):
    m = ledger.m
    return m.Attempt(
        batch.id,
        batch.rev,
        m.AttemptOrdinal(ordinal),
        batch.context,
        m.AnswererRef(
            m.AnswererKind.CODEX_EXEC, m.Model.GPT_LUNA, m.EscalationLevel.L1_CHEAP
        ),
        m.Verdict.ANSWER,
        outcome,
        (),
        batch.classification,
        m.EscalationReason.ANSWERER_FAILED
        if outcome is m.AttemptOutcome.FAILED
        else None,
        ledger.now(),
        timedelta(seconds=1),
        None,
    )


class LedgerTests(unittest.TestCase):
    def test_nested_transaction_commits_only_with_outer_and_rolls_back_together(self):
        with ledger.Ledger(self.store.path) as observer:
            with self.store.transaction():
                batch = self.store.open_or_attach(
                    asker(), (question(),), native_call("call-1"), classification()
                ).batch
                with self.store.transaction():
                    self.store.take_ownership(str(batch.id), batch.classification.floor)
                self.assertEqual(observer.batches(), ())
            self.assertEqual(
                observer.get_batch(str(batch.id)).state, BatchState.BOX_OWNED
            )
            with (
                self.assertRaisesRegex(RuntimeError, "rollback"),
                self.store.transaction(),
            ):
                with self.store.transaction():
                    self.store.transition(str(batch.id), BatchState.STALE)
                raise RuntimeError("rollback")
            self.assertEqual(
                observer.get_batch(str(batch.id)).state, BatchState.BOX_OWNED
            )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.store = ledger.Ledger(self.path)
        self.addCleanup(self.store.close)

    def test_complete_transition_table(self):
        legal = {
            BatchState.OPEN: {
                BatchState.OPEN,
                BatchState.ESCALATING,
                BatchState.BOX_OWNED,
                BatchState.BOX_ANSWERED,
                BatchState.CANCELLED,
                BatchState.STALE,
                BatchState.BLOCKED,
            },
            BatchState.ESCALATING: {
                BatchState.OPEN,
                BatchState.RESOLVED_LOCAL,
                BatchState.BOX_OWNED,
                BatchState.BOX_ANSWERED,
                BatchState.CANCELLED,
                BatchState.STALE,
                BatchState.BLOCKED,
            },
            BatchState.BOX_OWNED: {
                BatchState.BOX_OWNED,
                BatchState.BOX_ANSWERED,
                BatchState.CANCELLED,
                BatchState.STALE,
            },
            BatchState.RESOLVED_LOCAL: {
                BatchState.BOX_OWNED,
                BatchState.BOX_ANSWERED,
                BatchState.STALE,
            },
            BatchState.BOX_ANSWERED: {BatchState.BOX_ANSWERED, BatchState.STALE},
            BatchState.BLOCKED: {BatchState.CANCELLED, BatchState.STALE},
            BatchState.CANCELLED: set(),
            BatchState.STALE: set(),
        }
        for source in BatchState:
            for target in BatchState:
                with self.subTest(source=source, target=target):
                    if target in legal[source]:
                        ledger.check_transition(source, target)
                    else:
                        with self.assertRaises(ledger.IllegalTransition):
                            ledger.check_transition(source, target)

    def test_partial_index_is_generated_from_attach_states(self):
        with sqlite3.connect(self.path) as db:
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='batch_one_open'"
            ).fetchone()[0]
        for state in BatchState:
            self.assertEqual(f"'{state.value}'" in sql, state in ledger.OPEN_FOR_ATTACH)

    def test_private_creation_under_permissive_umask(self):
        import os

        old = os.umask(0)
        try:
            other = ledger.Ledger(self.path.parent / "private" / "ledger.db")
            other.audit(ledger.AuditKind.NOTICE, "task", "password=fixture-secret")
            other.close()
        finally:
            os.umask(old)
        self.assertEqual((self.path.parent / "private").stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.path.parent / "private" / "ledger.db").stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(
            (self.path.parent / "private" / ledger.m.AUDIT_FILE).stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            (self.path.parent / "private" / "question-audit.lock").stat().st_mode
            & 0o777,
            0o600,
        )

    def test_audit_redacts_secrets_and_rotates_to_one_archive(self):
        audit = self.path.parent / ledger.m.AUDIT_FILE
        self.store.audit(ledger.AuditKind.NOTICE, "task", "credential sk-" + "a" * 24)
        self.assertNotIn("sk-" + "a" * 24, audit.read_text())
        self.assertIn("[REDACTED]", audit.read_text())
        self.assertEqual(len(self.store.audit_events("task")), 1)

        first_payload = "first-rotation\n" + "x" * (1024 * 1024)
        audit.write_text(first_payload)
        self.store.audit(ledger.AuditKind.NOTICE, "task", "second rotation")
        archive = audit.with_suffix(".jsonl.1")
        self.assertEqual(archive.read_text(), first_payload)
        audit.write_text("third-rotation\n" + "y" * (1024 * 1024))
        self.store.audit(ledger.AuditKind.NOTICE, "task", "third rotation")
        self.assertTrue(archive.read_text().startswith("third-rotation\n"))
        self.assertNotIn("first-rotation", archive.read_text())

    def test_budget_reservation_is_atomic_across_connections(self):
        def reserve(_):
            with ledger.Ledger(self.path) as store:
                return store.reserve_budget(
                    "task",
                    model=ledger.m.Model.HAIKU,
                    tokens=100,
                    usd="0.10",
                    max_attempts=1,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_registration_ack_cannot_ack_unsent_future_progress(self):
        batch = self.store.open_or_attach(
            asker(),
            (question(),),
            native_call("call-1"),
            classification(),
        ).batch
        entry = registration(batch)
        self.store.put_registration(entry)
        self.store.progress(progress(batch, 1))
        self.store.ack_registration(
            {
                "batch_id": entry["batch_id"],
                "batch_rev": entry["batch_rev"],
                "content_digest": entry["content_digest"],
                "progress_seq": 999,
                "floor": ledger.m.EscalationLevel.L1_CHEAP,
                "owned": False,
            }
        )
        latest = self.store.progress(progress(batch, 2))
        self.assertEqual(self.store.due_progress(), (latest,))

    def test_audit_retains_json_metadata_usage_and_redacts_only_content(self):
        detail = {
            "batch_id": "01234567-89ab-cdef-0123-456789abcdef",
            "content_digest": "0123456789abcdef" * 4,
            "delivery_id": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123456789",
            "fallback_of": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123456789",
            "usage": {"input_tokens": 123, "output_tokens": 45, "usd": "0.01"},
            "rationale": {"text": "password=fixture-secret", "source": "model"},
        }
        self.store.audit(ledger.AuditKind.ATTEMPT, "task", ledger.encoded(detail))
        saved = json.loads(self.store.audit_events("task")[0].detail)
        self.assertEqual(saved["batch_id"], detail["batch_id"])
        self.assertEqual(saved["content_digest"], detail["content_digest"])
        self.assertEqual(saved["delivery_id"], detail["delivery_id"])
        self.assertEqual(saved["fallback_of"], detail["fallback_of"])
        self.assertEqual(saved["usage"], detail["usage"])
        self.assertNotIn("fixture-secret", saved["rationale"]["text"])

    def test_classified_flag_round_trips_through_ledger(self):
        unclassified = question_guard.derive_floor(
            (question("unclassified"),), rules=()
        )
        classified = question_guard.derive_floor(
            (question("classified"),), classification=classification(), rules=()
        )
        for call_id, result, expected in (
            ("unclassified", unclassified, False),
            ("classified", classified, True),
        ):
            batch = self.store.open_or_attach(
                asker(actor=call_id, turn=call_id),
                (question(call_id),),
                native_call(call_id, actor=call_id, turn=call_id),
                result,
            ).batch
            if not expected:
                self.assertFalse(
                    self.store.get_batch(str(batch.id)).classification.classified
                )
                stored = json.loads(
                    self.store.db.execute(
                        "SELECT classification_json FROM batch WHERE id=?",
                        (str(batch.id),),
                    ).fetchone()[0]
                )
                stored.pop("classified")
                stored["floor"] = int(ledger.m.EscalationLevel.L1_CHEAP)
                self.store.db.execute(
                    "UPDATE batch SET classification_json=? WHERE id=?",
                    (ledger.encoded(stored), str(batch.id)),
                )
            self.assertEqual(
                self.store.get_batch(str(batch.id)).classification.classified, expected
            )
            if not expected:
                self.assertGreaterEqual(
                    self.store.get_batch(str(batch.id)).classification.floor,
                    ledger.m.EscalationLevel.L3_PRINCIPAL,
                )

    def test_audit_detail_is_capped_per_line(self):
        detail = {
            "batch_id": "01234567-89ab-cdef-0123-456789abcdef",
            "content_digest": "0123456789abcdef" * 4,
            "answers": [
                {"answer_id": str(uuid4()), "text": "answer " + "x" * 600}
                for _ in range(3)
            ],
        }
        self.store.audit(ledger.AuditKind.ATTEMPT, "task", ledger.encoded(detail))
        saved = self.store.audit_events("task")[0].detail
        self.assertLessEqual(len(saved), 500)
        parsed = json.loads(saved)
        self.assertEqual(parsed["batch_id"], detail["batch_id"])
        self.assertEqual(parsed["content_digest"], detail["content_digest"])
        self.assertEqual(
            [answer["answer_id"] for answer in parsed["answers"]],
            [answer["answer_id"] for answer in detail["answers"]],
        )

    def test_audit_detail_preserves_metadata_when_it_alone_exceeds_cap(self):
        detail = {
            "answers": [
                {"answer_id": str(uuid4()), "text": "x" * 600} for _ in range(12)
            ],
            "content_digest": "0123456789abcdef" * 4,
        }
        saved = json.loads(ledger.redacted_audit_detail(ledger.encoded(detail)))
        self.assertEqual(saved["content_digest"], detail["content_digest"])
        self.assertEqual(
            [answer["answer_id"] for answer in saved["answers"]],
            [answer["answer_id"] for answer in detail["answers"]],
        )
        self.assertTrue(all(not answer["text"] for answer in saved["answers"]))

    def test_post_commit_audit_write_failure_logs_and_continues(self):
        original_open = os.open
        failed = False

        def open_once(path, *args, **kwargs):
            nonlocal failed
            if str(path).endswith("question-audit.lock") and not failed:
                failed = True
                raise OSError("audit unavailable")
            return original_open(path, *args, **kwargs)

        with (
            patch.object(ledger.os, "open", side_effect=open_once),
            self.assertLogs(ledger.__name__, level="ERROR") as logs,
        ):
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO budget VALUES(?,?,0,0,0,'0','0')",
                    (ledger.m.BudgetScope.TASK, "committed-task"),
                )
                self.store.audit(ledger.AuditKind.NOTICE, "task", "first")
                self.store.audit(ledger.AuditKind.NOTICE, "task", "second")

        self.assertTrue(failed)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM budget WHERE key='committed-task'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [event.detail for event in self.store.audit_events("task")], ["second"]
        )

    def test_all_unknown_takeovers_must_be_cleared_before_claim(self):
        m = ledger.m
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                actor = "takeover-" + str(reverse)
                batch = self.store.open_or_attach(
                    asker(actor=actor),
                    (question(actor),),
                    native_call(actor, actor=actor),
                    classification(),
                ).batch
                pending = outbound(batch)
                self.store.put_outbound(pending)
                takeovers = [
                    m.Takeover(
                        m.TakeoverId(uuid4()),
                        batch.id,
                        m.PromptOrigin.UNKNOWN,
                        None,
                        ledger.now(),
                        None,
                        None,
                    )
                    for _ in range(2)
                ]
                for takeover in takeovers:
                    self.store.record_takeover(takeover)
                if reverse:
                    takeovers.reverse()
                self.store.clear_takeover(str(takeovers[0].id), "owner")
                self.assertIsNone(
                    self.store.claim(str(pending.id), "worker", m.Route.CODEX_QUEUE)
                )
                late = ledger.replace(
                    outbound(batch), resolution_rev=m.ResolutionRev(2)
                )
                self.store.put_outbound(late)
                self.assertIsNone(
                    self.store.claim(str(late.id), "worker", m.Route.CODEX_QUEUE)
                )
                self.store.clear_takeover(str(takeovers[1].id), "owner")
                self.assertIsNotNone(
                    self.store.claim(str(pending.id), "worker", m.Route.CODEX_QUEUE)
                )

    def test_ack_rejects_non_ascii_token_without_crashing(self):
        batch = self.store.open_or_attach(
            asker(),
            (question(),),
            native_call("call-1"),
            classification(),
        ).batch
        pending = outbound(batch)
        self.store.put_outbound(pending)
        self.assertFalse(self.store.ack(str(pending.id), "tökén"))

    def test_ack_only_applies_delivery_that_may_have_been_sent(self):
        m = ledger.m
        batch = self.store.open_or_attach(
            asker(),
            (question(),),
            native_call("call-1"),
            classification(),
        ).batch
        for index, state in enumerate(m.DeliveryState, 1):
            with self.subTest(state=state):
                message = ledger.replace(
                    outbound(batch), state=state, resolution_rev=m.ResolutionRev(index)
                )
                self.store.put_outbound(message)
                allowed = state in {
                    m.DeliveryState.DISPATCHED,
                    m.DeliveryState.OBSERVED,
                    m.DeliveryState.DISPATCH_UNCERTAIN,
                    m.DeliveryState.APPLIED,
                }
                self.assertEqual(
                    self.store.ack(str(message.id), message.ack_token), allowed
                )

    def test_box_note_ack_requires_possible_delivery(self):
        m = ledger.m
        batch = self.store.open_or_attach(
            asker(), (question(),), native_call("call-1"), classification()
        ).batch
        for index, state in enumerate(m.BoxNoteState, 1):
            with self.subTest(state=state):
                note = m.BoxNote(
                    m.DeliveryId(f"delivery-{index}"),
                    m.SendAttempt(1),
                    m.PlanId(f"plan-{index}"),
                    batch.id,
                    batch.rev,
                    m.ResolutionRev(index),
                    m.DeliveryPurpose.ANSWER,
                    batch.asker.actor,
                    None,
                    "text",
                    m.digest("text"),
                    m.file_digest(b"token"),
                    m.EscalationLevel.L3_PRINCIPAL,
                    state,
                    None,
                    ledger.now(),
                )
                self.store.put_box_note(note)
                allowed = state in {
                    m.BoxNoteState.SENT,
                    m.BoxNoteState.ECHOED,
                    m.BoxNoteState.UNKNOWN,
                }
                self.assertEqual(
                    self.store.ack(str(note.delivery_id), "token"), allowed
                )
                row = self.store.db.execute(
                    "SELECT acked_rev FROM box_note WHERE delivery_id=?",
                    (str(note.delivery_id),),
                ).fetchone()
                self.assertEqual(row[0], index if allowed else None)

    def test_retain_cleans_only_unlocked_completed_plan_locks(self):
        for plan, result in (
            ("complete", "result"),
            ("locked", "result"),
            ("pending", None),
        ):
            self.store.db.execute(
                "INSERT INTO plan_seen VALUES(?,?,?,?,?)",
                (plan, "send", "digest", result, ledger.now().isoformat()),
            )
            (self.path.parent / f"qe-plan-{plan}").touch()
        missing = self.path.parent / "qe-plan-missing"
        missing.touch()
        with (self.path.parent / "qe-plan-locked").open("r+") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX)
            self.store.retain()
            self.assertFalse((self.path.parent / "qe-plan-complete").exists())
            self.assertTrue((self.path.parent / "qe-plan-locked").exists())
            self.assertTrue((self.path.parent / "qe-plan-pending").exists())
            self.assertTrue(missing.exists())
        self.store.retain()
        self.assertFalse((self.path.parent / "qe-plan-locked").exists())

    def test_ingest_events_replay_after_unlink_failure_is_deduplicated(self):
        event_id = str(uuid4())
        path = self.path.parent / (event_id + ".json")
        path.write_text(
            json.dumps(
                {"task_id": "task", "text": "Assumed permission", "batch_id": None}
            )
        )
        with patch.object(Path, "unlink", side_effect=OSError("crash after commit")):
            self.assertEqual(self.store.ingest_events(path.parent), 0)
        self.assertEqual(self.store.ingest_events(path.parent), 1)
        notices = self.store.db.execute(
            "SELECT id FROM notice WHERE task='task'"
        ).fetchall()
        self.assertEqual([row[0] for row in notices], [event_id])

    def test_context_bump_is_noop_for_cancelled_and_stale_batches(self):
        for state in (BatchState.CANCELLED, BatchState.STALE):
            with self.subTest(state=state):
                call = "terminal-" + state
                batch = self.store.open_or_attach(
                    asker(actor=call),
                    (question(call),),
                    native_call(call, actor=call),
                    classification(),
                ).batch
                closed = self.store.transition(str(batch.id), state)
                before = self.store.audit_events(batch.asker.task)
                self.assertEqual(
                    self.store.bump_for_context(
                        str(batch.id),
                        ledger.LiveContext(ledger.m.HeadSha("b" * 40), None, True),
                    ),
                    closed,
                )
                self.assertEqual(self.store.audit_events(batch.asker.task), before)

    def test_brief_path_is_an_explicit_open_input_not_process_environment(self):
        brief = self.path.parent / "explicit-brief.md"
        brief.write_text("original")
        with patch.dict(os.environ, {ledger.m.BRIEF_ENV: "/unrelated/brief.md"}):
            batch = self.store.open_or_attach(
                asker(floor_brief=ledger.m.file_digest(brief.read_bytes())),
                (question(),),
                native_call("call-1"),
                classification(),
                brief=brief,
            ).batch
        with patch.object(
            ledger.subprocess,
            "run",
            return_value=ledger.subprocess.CompletedProcess([], 0, "a" * 40),
        ):
            self.assertEqual(self.store.live_context(str(batch.id)).brief_path, brief)

    def test_open_or_attach_serializes_racing_calls_for_the_same_actor_turn(self):
        def attach(index):
            with ledger.Ledger(self.path) as store:
                call_id = f"race-{index}"
                return store.open_or_attach(
                    asker(),
                    (question(call_id),),
                    native_call(call_id),
                    classification(),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attach, range(2)))
        self.assertEqual(
            {result.outcome for result in results},
            {ledger.m.AttachOutcome.OPENED, ledger.m.AttachOutcome.ATTACHED},
        )
        self.assertEqual(len(self.store.batches()), 1)
        self.assertEqual(self.store.batches()[0].rev, 2)
        self.assertEqual(len(self.store.batches()[0].questions), 2)

    def test_explicit_async_fallback_retires_failed_call_question_and_keeps_floor(self):
        blocking = native_call("blocking")
        first = self.store.open_or_attach(
            asker(),
            (question("blocking"),),
            blocking,
            classification(ledger.m.EscalationLevel.L3_PRINCIPAL),
        )
        self.store.mark_failed("blocking")
        async_call = native_call("async", ledger.m.QuestionTool.CODEX_ASYNC)
        result = self.store.open_or_attach(
            asker(),
            (question("async", "Follow-up"),),
            async_call,
            classification(ledger.m.EscalationLevel.L1_CHEAP),
        )
        self.assertEqual(result.outcome, ledger.m.AttachOutcome.REVISED)
        self.assertEqual(result.batch.id, first.batch.id)
        self.assertEqual(result.batch.rev, 2)
        retired, active = result.batch.questions
        self.assertEqual(retired.retired_rev, 2)
        self.assertIsNone(active.retired_rev)
        self.assertEqual(
            result.batch.classification.floor, ledger.m.EscalationLevel.L3_PRINCIPAL
        )
        self.assertEqual(result.batch.calls[-1].fallback_of, blocking.call)

    def test_duplicate_call_is_deduplicated_but_new_call_after_answer_opens_batch(self):
        first = self.store.open_or_attach(
            asker(), (question("first"),), native_call("first"), classification()
        )
        duplicate = self.store.open_or_attach(
            asker(), (question("first"),), native_call("first"), classification()
        )
        self.assertEqual(duplicate.outcome, ledger.m.AttachOutcome.DUPLICATE_CALL)
        self.assertEqual(duplicate.batch.id, first.batch.id)
        self.store.transition(str(first.batch.id), BatchState.BOX_ANSWERED)
        reopened = self.store.open_or_attach(
            asker(), (question("second"),), native_call("second"), classification()
        )
        self.assertEqual(reopened.outcome, ledger.m.AttachOutcome.OPENED)
        self.assertNotEqual(reopened.batch.id, first.batch.id)
        self.assertEqual(reopened.batch.rev, 1)

    def test_delayed_registration_ack_records_floor_without_reopening_answered_batch(
        self,
    ):
        opened = self.store.open_or_attach(
            asker(), (question("call-ack"),), native_call("call-ack"), classification()
        )
        batch = self.store.transition(str(opened.batch.id), BatchState.BOX_ANSWERED)
        entry = registration(batch)
        self.store.put_registration(entry)
        self.store.ack_registration(
            {
                "batch_id": entry["batch_id"],
                "batch_rev": entry["batch_rev"],
                "content_digest": entry["content_digest"],
                "progress_seq": 0,
                "floor": ledger.m.EscalationLevel.L3_PRINCIPAL,
                "owned": True,
            }
        )
        self.assertEqual(
            self.store.get_batch(str(batch.id)).state, BatchState.BOX_ANSWERED
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT box_floor FROM batch WHERE id=?", (str(batch.id),)
            ).fetchone()[0],
            ledger.m.EscalationLevel.L3_PRINCIPAL,
        )

    def test_take_ownership_atomically_creates_pending_notice(self):
        opened = self.store.open_or_attach(
            asker(), (question("call-own"),), native_call("call-own"), classification()
        )
        answer = outbound(opened.batch)
        self.store.put_outbound(answer)
        with patch.object(
            self.store,
            "put_outbound",
            side_effect=RuntimeError("injected write failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.take_ownership(
                    str(opened.batch.id), ledger.m.EscalationLevel.L3_PRINCIPAL
                )
        self.assertEqual(
            self.store.get_batch(str(opened.batch.id)).state, BatchState.OPEN
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(answer.id),)
            ).fetchone()[0],
            ledger.m.DeliveryState.PENDING,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM outbound WHERE batch=? AND purpose=?",
                (str(opened.batch.id), ledger.m.OutboundPurpose.PENDING_NOTICE),
            ).fetchone()[0],
            0,
        )

        self.store.take_ownership(
            str(opened.batch.id), ledger.m.EscalationLevel.L3_PRINCIPAL
        )
        self.assertEqual(
            self.store.get_batch(str(opened.batch.id)).state, BatchState.BOX_OWNED
        )
        rows = self.store.db.execute(
            "SELECT state,resolution_rev FROM outbound WHERE batch=? AND purpose=?",
            (str(opened.batch.id), ledger.m.OutboundPurpose.PENDING_NOTICE),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows], [(ledger.m.DeliveryState.PENDING, 0)]
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(answer.id),)
            ).fetchone()[0],
            ledger.m.DeliveryState.SUPPRESSED,
        )

    def test_ownership_preserves_possible_dispatch(self):
        m = ledger.m
        for state in (
            m.DeliveryState.CLAIMED,
            m.DeliveryState.DISPATCH_UNCERTAIN,
            m.DeliveryState.DISPATCHED,
            m.DeliveryState.OBSERVED,
            m.DeliveryState.APPLIED,
        ):
            with self.subTest(state=state):
                opened = self.store.open_or_attach(
                    asker(turn=str(uuid4())),
                    (question(str(uuid4())),),
                    native_call(str(uuid4())),
                    classification(),
                )
                answer = ledger.replace(outbound(opened.batch), state=state)
                self.store.put_outbound(answer)
                self.assertTrue(
                    self.store.take_ownership(
                        str(opened.batch.id), m.EscalationLevel.L3_PRINCIPAL
                    )
                )
                row = self.store.db.execute(
                    "SELECT state FROM outbound WHERE id=?", (str(answer.id),)
                ).fetchone()
                self.assertEqual(row[0], state)

    def test_progress_floor_raise_is_copied_to_later_snapshot_and_due_state(self):
        opened = self.store.open_or_attach(
            asker(),
            (question("call-progress"),),
            native_call("call-progress"),
            classification(),
        )
        entry = registration(opened.batch)
        self.store.put_registration(entry)
        highest = {
            "floor": ledger.m.EscalationLevel.L4_OWNER,
            "cause": ledger.m.FloorRaiseCause.MAC_GUARD,
            "classification": None,
        }
        self.store.progress(progress(opened.batch, 1, highest))
        later = self.store.progress(progress(opened.batch, 2))
        self.assertEqual(later["floor_raise"], highest)
        self.assertEqual(self.store.due_progress(), (later,))

    def test_claim_is_compare_and_swap_and_orphan_recovery_is_uncertain(self):
        opened = self.store.open_or_attach(
            asker(),
            (question("call-claim"),),
            native_call("call-claim"),
            classification(),
        )
        pending = outbound(opened.batch)
        self.store.put_outbound(pending)

        def try_claim(owner):
            with ledger.Ledger(self.path) as store:
                return store.claim(str(pending.id), owner, ledger.m.Route.CODEX_QUEUE)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(try_claim, ("worker-a", "worker-b")))
        winner = next(claim for claim in claims if claim is not None)
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        wrong_owner = ledger.Claim(
            winner.outbound, "other-worker", winner.attempt, winner.route
        )
        self.assertFalse(
            self.store.finish_claim(wrong_owner, ledger.m.DeliveryState.DISPATCHED)
        )
        self.assertTrue(
            self.store.finish_claim(winner, ledger.m.DeliveryState.DISPATCHED)
        )
        self.assertFalse(
            self.store.finish_claim(winner, ledger.m.DeliveryState.DISPATCHED)
        )

        abandoned = outbound(opened.batch, ledger.m.OutboundPurpose.PENDING_NOTICE)
        self.store.put_outbound(abandoned)
        claim = self.store.claim(
            str(abandoned.id), "dead-worker", ledger.m.Route.CODEX_QUEUE
        )
        self.assertIsNotNone(claim)
        recovered = self.store.recover_claims(
            ledger.now()
            + timedelta(seconds=ledger.m.CLAIM_STALE_AFTER.total_seconds() + 1)
        )
        self.assertEqual(recovered, 1)
        state = self.store.db.execute(
            "SELECT state FROM outbound WHERE id=?", (str(abandoned.id),)
        ).fetchone()[0]
        self.assertEqual(state, ledger.m.DeliveryState.DISPATCH_UNCERTAIN)

    def test_box_note_retry_updates_attempt_metadata_but_preserves_delivery_content(
        self,
    ):
        opened = self.store.open_or_attach(
            asker(),
            (question("call-note"),),
            native_call("call-note"),
            classification(),
        )
        m = ledger.m
        first = m.BoxNote(
            m.DeliveryId("delivery-1"),
            m.SendAttempt(1),
            m.PlanId("plan-1"),
            opened.batch.id,
            opened.batch.rev,
            m.ResolutionRev(1),
            m.DeliveryPurpose.ANSWER,
            opened.batch.asker.actor,
            None,
            "original text",
            m.digest("original text"),
            m.digest("token-1"),
            m.EscalationLevel.L3_PRINCIPAL,
            m.BoxNoteState.SENT,
            None,
            ledger.now(),
        )
        retry = m.BoxNote(
            first.delivery_id,
            m.SendAttempt(2),
            m.PlanId("plan-2"),
            first.batch,
            first.batch_rev,
            first.resolution_rev,
            first.purpose,
            first.target_actor,
            first.child_actor,
            "changed text must not replace content",
            m.digest("changed text must not replace content"),
            m.digest("token-2"),
            m.EscalationLevel.L4_OWNER,
            m.BoxNoteState.PREPARED,
            None,
            ledger.now() + timedelta(seconds=1),
        )
        self.store.put_box_note(first)
        self.store.put_box_note(retry)
        row = self.store.db.execute(
            "SELECT attempt,plan_id,text,body_digest,ack_token_digest,floor,state FROM box_note WHERE delivery_id=?",
            (first.delivery_id,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                2,
                "plan-2",
                first.text,
                first.body_digest,
                first.ack_token_digest,
                first.floor,
                m.BoxNoteState.PREPARED,
            ),
        )
        self.assertFalse(
            self.store.finish_box_note(
                "delivery-1", 2, "stale-plan", m.BoxNoteState.SENT
            )
        )
        self.assertFalse(
            self.store.finish_box_note("delivery-1", 1, "plan-2", m.BoxNoteState.SENT)
        )
        self.assertTrue(
            self.store.finish_box_note("delivery-1", 2, "plan-2", m.BoxNoteState.SENT)
        )
        self.assertTrue(
            self.store.finish_box_note("delivery-1", 2, "plan-2", m.BoxNoteState.ECHOED)
        )
        self.assertFalse(
            self.store.finish_box_note("delivery-1", 2, "plan-2", m.BoxNoteState.SENT)
        )
        row = self.store.db.execute(
            "SELECT attempt,plan_id,text,body_digest,ack_token_digest,floor,state FROM box_note WHERE delivery_id=?",
            (first.delivery_id,),
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                2,
                "plan-2",
                first.text,
                first.body_digest,
                first.ack_token_digest,
                first.floor,
                m.BoxNoteState.ECHOED,
            ),
        )

    def test_live_context_invalidates_brief_changes_at_any_floor_and_head_changes_only_at_l3(
        self,
    ):
        brief = self.path.parent / "brief.md"
        brief.write_text("original brief")
        digest = ledger.m.file_digest(brief.read_bytes())
        low = self.store.open_or_attach(
            asker(floor_brief=digest, head="a" * 40),
            (question("call-live-low"),),
            native_call("call-live-low"),
            classification(),
        )
        high = self.store.open_or_attach(
            asker(actor="actor-high", floor_brief=digest, head="a" * 40),
            (question("call-live-high"),),
            native_call("call-live-high", actor="actor-high"),
            classification(ledger.m.EscalationLevel.L3_PRINCIPAL),
        )

        class GitHead:
            stdout = "b" * 40 + "\n"

        with patch.object(ledger.subprocess, "run", return_value=GitHead()):
            low_head = self.store.live_context(str(low.batch.id), brief)
            high_head = self.store.live_context(str(high.batch.id), brief)
        self.assertFalse(low_head.changed)
        self.assertTrue(high_head.changed)
        brief.write_text("updated brief")
        with patch.object(ledger.subprocess, "run", return_value=GitHead()):
            changed_brief = self.store.live_context(str(low.batch.id), brief)
        self.assertTrue(changed_brief.changed)

        owned = self.store.open_or_attach(
            asker(actor="actor-owned"),
            (question("call-owned"),),
            native_call("call-owned", actor="actor-owned"),
            classification(),
        )
        self.store.take_ownership(
            str(owned.batch.id), ledger.m.EscalationLevel.L3_PRINCIPAL
        )
        self.assertEqual(
            self.store.get_batch(str(owned.batch.id)).classification.floor,
            ledger.m.EscalationLevel.L3_PRINCIPAL,
        )
        with patch.object(ledger.subprocess, "run", return_value=GitHead()):
            owned_head = self.store.live_context(str(owned.batch.id))
        self.assertTrue(owned_head.changed)

    def test_live_context_reuses_opened_brief_path_after_environment_is_cleared(self):
        brief = self.path.parent / "saved-brief.md"
        brief.write_text("original brief")
        opened_digest = ledger.m.file_digest(brief.read_bytes())
        with patch.dict(os.environ, {ledger.m.BRIEF_ENV: str(brief)}):
            opened = self.store.open_or_attach(
                asker(floor_brief=opened_digest),
                (question("call-saved-brief"),),
                native_call("call-saved-brief"),
                classification(),
                brief=brief,
            )
        brief.write_text("edited original brief")
        with patch.dict(os.environ, {ledger.m.BRIEF_ENV: ""}):
            live = self.store.live_context(str(opened.batch.id))
        self.assertEqual(live.brief_path, brief)
        self.assertEqual(live.brief_digest, ledger.m.file_digest(brief.read_bytes()))
        self.assertTrue(live.changed)

    def test_bump_for_changed_lacana_brief_raises_effective_floor_to_l4(self):
        brief = self.path.parent / "project-brief.md"
        brief.write_text("baseline")
        opened_digest = ledger.m.file_digest(brief.read_bytes())
        with patch.dict(os.environ, {ledger.m.BRIEF_ENV: str(brief)}):
            opened = self.store.open_or_attach(
                asker(floor_brief=opened_digest),
                (question("call-lacana-brief"),),
                native_call("call-lacana-brief"),
                classification(),
                brief=brief,
            )
        brief.write_text(
            '```toml qe-escalation\nschema = "qe.brief/1"\nproject = "la_cana"\n'
            "l0_assume = []\nask_carlos = []\n```\n"
        )
        live = self.store.live_context(str(opened.batch.id), brief)
        bumped = self.store.bump_for_context(str(opened.batch.id), live)
        self.assertEqual(bumped.rev, 2)
        self.assertEqual(bumped.classification.floor, ledger.m.EscalationLevel.L4_OWNER)
        self.assertIn(
            ledger.m.GuardTrigger.LA_CANA,
            {hit.trigger for hit in bumped.classification.hits},
        )

    def test_context_bump_keeps_resolved_local_closed_when_another_batch_is_open(self):
        old = self.store.open_or_attach(
            asker(),
            (question("call-resolved-old"),),
            native_call("call-resolved-old"),
            classification(),
        )
        self.store.transition(str(old.batch.id), BatchState.ESCALATING)
        self.store.resolve(resolution(old.batch), (outbound(old.batch),))
        newer = self.store.open_or_attach(
            asker(),
            (question("call-open-newer"),),
            native_call("call-open-newer"),
            classification(),
        )
        self.assertEqual(newer.outcome, ledger.m.AttachOutcome.OPENED)
        bumped = self.store.bump_for_context(
            str(old.batch.id),
            ledger.LiveContext(ledger.m.HeadSha("b" * 40), None, True),
        )
        self.assertEqual(bumped.rev, 2)
        self.assertEqual(bumped.state, BatchState.RESOLVED_LOCAL)
        old_outbound = self.store.db.execute(
            "SELECT state,suppress_cause FROM outbound WHERE batch=?",
            (str(old.batch.id),),
        ).fetchone()
        self.assertEqual(
            tuple(old_outbound),
            (ledger.m.DeliveryState.SUPPRESSED, ledger.m.SuppressCause.CONTEXT_CHANGED),
        )

    def test_pending_notice_survives_box_owned_revision_bump(self):
        opened = self.store.open_or_attach(
            asker(),
            (question("call-owned-rev1"),),
            native_call("call-owned-rev1"),
            classification(),
        )
        self.store.take_ownership(
            str(opened.batch.id), ledger.m.EscalationLevel.L3_PRINCIPAL
        )
        before = self.store.db.execute(
            "SELECT id,state FROM outbound WHERE batch=? AND purpose=?",
            (str(opened.batch.id), ledger.m.OutboundPurpose.PENDING_NOTICE),
        ).fetchone()
        self.assertIsNotNone(before)
        revised = self.store.open_or_attach(
            asker(),
            (question("call-owned-rev2"),),
            native_call("call-owned-rev2"),
            classification(),
        )
        self.assertEqual(revised.batch.rev, 2)
        after = self.store.db.execute(
            "SELECT id,state FROM outbound WHERE batch=? AND purpose=?",
            (str(opened.batch.id), ledger.m.OutboundPurpose.PENDING_NOTICE),
        ).fetchone()
        self.assertEqual(tuple(after), (before["id"], ledger.m.DeliveryState.PENDING))

    def test_same_root_reask_links_related_batch_and_returns_prior_resolution_as_context(
        self,
    ):
        old_question = question("call-prior-old", "Same question text")
        old = self.store.open_or_attach(
            asker(), (old_question,), native_call("call-prior-old"), classification()
        )
        self.store.transition(str(old.batch.id), BatchState.ESCALATING)
        prior_resolution = resolution(old.batch)
        self.store.resolve(prior_resolution, (outbound(old.batch),))
        m = ledger.m
        prior_note = m.BoxNote(
            m.DeliveryId("prior-delivery"),
            m.SendAttempt(1),
            m.PlanId("prior-plan"),
            old.batch.id,
            old.batch.rev,
            m.ResolutionRev(1),
            m.DeliveryPurpose.ANSWER,
            old.batch.asker.actor,
            None,
            "Prior box answer",
            m.digest("Prior box answer"),
            m.digest("prior-token"),
            m.EscalationLevel.L3_PRINCIPAL,
            m.BoxNoteState.ECHOED,
            None,
            ledger.now(),
        )
        self.store.put_box_note(prior_note)

        new = self.store.open_or_attach(
            asker(),
            (question("call-prior-new", "Same question text"),),
            native_call("call-prior-new"),
            classification(),
        )
        related_to = self.store.db.execute(
            "SELECT related_to FROM batch WHERE id=?", (str(new.batch.id),)
        ).fetchone()[0]
        self.assertEqual(related_to, str(old.batch.id))
        self.assertEqual(
            self.store.prior_context(str(new.batch.id)), (prior_resolution, prior_note)
        )

    def test_record_attempt_preserves_completed_history_and_finalizes_late_attempt_as_stale(
        self,
    ):
        opened = self.store.open_or_attach(
            asker(),
            (question("call-attempt-old"),),
            native_call("call-attempt-old"),
            classification(),
        )
        completed = attempt(
            opened.batch, ordinal=1, outcome=ledger.m.AttemptOutcome.FAILED
        )
        self.assertEqual(self.store.record_attempt(completed), completed)
        in_flight = attempt(
            opened.batch, ordinal=2, outcome=ledger.m.AttemptOutcome.ANSWERED
        )
        self.store.open_or_attach(
            asker(),
            (question("call-attempt-new"),),
            native_call("call-attempt-new"),
            classification(),
        )
        finalized = self.store.record_attempt(in_flight)
        self.assertEqual(finalized.outcome, ledger.m.AttemptOutcome.STALE_REVISION)
        rows = self.store.db.execute(
            "SELECT ordinal,outcome,json FROM attempt WHERE batch=? ORDER BY ordinal",
            (str(opened.batch.id),),
        ).fetchall()
        self.assertEqual(
            [row[1] for row in rows],
            [ledger.m.AttemptOutcome.FAILED, ledger.m.AttemptOutcome.STALE_REVISION],
        )
        self.assertEqual(
            ledger.m.parse_record(ledger.m.Attempt, json.loads(rows[0][2])), completed
        )


if __name__ == "__main__":
    unittest.main()
