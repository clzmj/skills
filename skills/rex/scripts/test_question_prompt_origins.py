#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Hook origin precedence and conservative unmatched-input handling."""

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import codex_sessions
import grokbot_callback as callback
import question_hooks as hooks
import question_ledger as ledger
import question_model as m
from test_question_ledger import asker, classification, native_call, outbound, question


class QuestionPromptOrigins(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.store = ledger.Ledger(self.root / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        self.state = callback.SessionState(
            key="actor-1",
            id="session-1",
            agent=m.AgentKind.CODEX,
            parent="",
            root="actor-1",
            depth=0,
            task_id="task-1",
            requester="dinesh",
            turn="turn-1",
            status=callback.Status.RUNNING,
            rex_session="rex-session",
            rex_block="rex-block",
        )
        for item in (
            patch.dict(os.environ, {m.FLAG_ENV: "1"}, clear=True),
            patch.object(callback, "session_state", return_value=self.state),
            patch.object(callback, "actor_key", return_value="actor-1"),
            patch.object(callback.rex, "STATE", self.root),
            patch.object(codex_sessions, "DB", self.root / "coordination.sqlite3"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.batch = self.store.open_or_attach(
            asker(), (question(),), native_call("call-1"), classification()
        ).batch

    def submit(self, prompt):
        return hooks.handle(
            {
                "hook_event_name": m.HookEvent.USER_PROMPT_SUBMIT,
                "session_id": "session-1",
                "turn_id": "turn-1",
                "prompt": prompt,
            },
            m.AgentKind.CODEX,
            self.store,
            spawn=False,
        )

    def last_origin(self):
        return self.store.db.execute(
            "SELECT origin FROM lifecycle ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]

    def test_broker_origin_recomputes_stored_text_not_untrusted_digest(self):
        text = "[qe:fixture:r1] Answer café"
        self.store.put_outbound(
            replace(outbound(self.batch), text=text, body_digest=m.TextDigest("0" * 64))
        )
        self.assertEqual(self.submit(text.replace("é", "e\u0301") + "  \n"), {})
        self.assertEqual(self.last_origin(), m.PromptOrigin.BROKER)
        self.submit("Different content")
        self.assertEqual(self.last_origin(), m.PromptOrigin.UNKNOWN)
        # A poisoned stored digest cannot bless different text.
        self.store.db.execute(
            "UPDATE outbound SET body_digest=?", (m.body_digest("Different content"),)
        )
        self.submit("Different content")
        self.assertEqual(self.last_origin(), m.PromptOrigin.UNKNOWN)

    def test_box_origin_marks_echoed_and_actor_scope_and_broker_precedence(self):
        text = "[qe:fixture:r1] Box answer"
        note = m.BoxNote(
            m.DeliveryId(str(uuid4())),
            m.SendAttempt(1),
            m.PlanId(str(uuid4())),
            self.batch.id,
            self.batch.rev,
            m.ResolutionRev(1),
            m.DeliveryPurpose.ANSWER,
            m.ActorKey("actor-1"),
            None,
            text,
            m.TextDigest("0" * 64),
            m.TextDigest("1" * 64),
            m.EscalationLevel.L3_PRINCIPAL,
            m.BoxNoteState.PREPARED,
            None,
            ledger.now(),
        )
        self.store.put_box_note(note)
        self.submit(text)
        self.assertEqual(self.last_origin(), m.PromptOrigin.BOX)
        self.assertEqual(
            self.store.db.execute("SELECT state FROM box_note").fetchone()[0],
            m.BoxNoteState.ECHOED,
        )
        self.store.put_outbound(replace(outbound(self.batch), text=text))
        self.submit(text)
        self.assertEqual(self.last_origin(), m.PromptOrigin.BROKER)
        with patch.object(callback, "actor_key", return_value="different-actor"):
            self.submit(text)
        self.assertEqual(self.last_origin(), m.PromptOrigin.UNVERIFIED_MARKER)

    def test_collaborator_requires_recipient_id_kind_and_exact_normalized_stored_text(
        self,
    ):
        codex_sessions.event(
            "team",
            "sender",
            "queue_requested",
            "Hello café",
            recipient="session-1",
            identifier="message-1",
        )
        header = "[Codex team team; from sender; message-id message-1]"
        self.submit(header + "\nHello cafe\u0301\n")
        self.assertEqual(self.last_origin(), m.PromptOrigin.COLLABORATOR)
        for prompt in (
            header + "\nHello different",
            header.replace("message-1", "missing") + "\nHello café",
        ):
            self.submit(prompt)
            self.assertEqual(self.last_origin(), m.PromptOrigin.UNKNOWN)
        for column, value in (("recipient", "other-session"), ("kind", "delivered")):
            with sqlite3.connect(self.root / "coordination.sqlite3") as db:
                db.execute(f"UPDATE events SET {column}=?", (value,))
            self.submit(header + "\nHello café")
            self.assertEqual(self.last_origin(), m.PromptOrigin.UNKNOWN)
            with sqlite3.connect(self.root / "coordination.sqlite3") as db:
                db.execute(
                    "UPDATE events SET recipient='session-1',kind='queue_requested'"
                )

    def test_unmatched_markers_notice_and_ordinary_input_pauses_without_cancelling(
        self,
    ):
        for prompt in (
            "[qe:unknown:r1] Unverified",
            "[Codex team unknown] Unverified",
            "Ordinary unmarked prompt",
        ):
            self.submit(prompt)
            expected = (
                m.PromptOrigin.UNVERIFIED_MARKER
                if "[qe:" in prompt or "[Codex team" in prompt
                else m.PromptOrigin.UNKNOWN
            )
            self.assertEqual(self.last_origin(), expected)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM takeover").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.OPEN
        )

    def test_lifecycle_end_events_and_missing_callback_row(self):
        for event, kind in (
            (m.HookEvent.STOP, m.LifecycleKind.TURN_ENDED),
            (m.HookEvent.SESSION_END, m.LifecycleKind.SESSION_ENDED),
            (m.HookEvent.SUBAGENT_STOP, m.LifecycleKind.SESSION_ENDED),
        ):
            self.assertEqual(
                hooks.handle(
                    {"hook_event_name": event, "session_id": "session-1"},
                    m.AgentKind.CODEX,
                    self.store,
                    spawn=False,
                ),
                {},
            )
            self.assertEqual(
                self.store.db.execute(
                    "SELECT kind FROM lifecycle ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0],
                kind,
            )
        count = self.store.db.execute("SELECT count(*) FROM lifecycle").fetchone()[0]
        with patch.object(callback, "session_state", return_value=None):
            self.submit("Missing callback row")
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM lifecycle").fetchone()[0], count
        )

    def test_observed_async_acceptance_has_no_lifecycle_but_native_answer_and_failure_do(
        self,
    ):
        value = {
            "hook_event_name": m.HookEvent.POST_TOOL_USE,
            "session_id": "session-1",
            "tool_name": str(m.QuestionTool.CODEX_ASYNC),
            "tool_use_id": "call-1",
            "tool_response": {"accepted": True},
        }
        hooks.handle(value, m.AgentKind.CODEX, self.store, spawn=False)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM lifecycle").fetchone()[0], 0
        )
        for response, kind in (
            ({"answers": {"choice": "Standard"}}, m.LifecycleKind.NATIVE_ANSWER),
            ({"error": "unavailable"}, m.LifecycleKind.NATIVE_CALL_FAILED),
        ):
            hooks.handle(
                {
                    **value,
                    "tool_name": str(m.QuestionTool.CODEX_BLOCKING),
                    "tool_response": response,
                },
                m.AgentKind.CODEX,
                self.store,
                spawn=False,
            )
            row = self.store.db.execute(
                "SELECT kind,batch FROM lifecycle ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(tuple(row), (kind, str(self.batch.id)))
