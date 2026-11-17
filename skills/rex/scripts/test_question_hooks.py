#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Question hooks are opt-in readers and detection is separate from delivery."""

import os
import io
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import grokbot_callback as callback
import question_hooks as hooks
import question_ledger as ledger
import question_broker as broker
import question_model as m


def state():
    return callback.SessionState(
        key="codex:fixture",
        id="fixture",
        agent=m.AgentKind.CODEX,
        parent="",
        root="codex:fixture",
        depth=0,
        task_id="fixture",
        requester="Jim",
        turn="turn",
        status=callback.Status.RUNNING,
        rex_session="fixture",
        rex_block="block:fixture",
    )


class HookFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {m.FLAG_ENV: "1"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = ledger.Ledger(self.directory / m.LEDGER_FILE)
        self.addCleanup(self.store.close)

    def detect(self, **changes):
        raw = dict(
            hook_event_name="PreToolUse",
            session_id="fixture",
            turn_id="turn",
            tool_name="request_user_input",
            tool_use_id="call-1",
            tool_input={
                "questions": [
                    {
                        "id": "one",
                        "question": "Which indentation?",
                        "options": [{"label": "Tabs"}, {"label": "Spaces"}],
                    }
                ]
            },
            cwd=str(self.directory),
        )
        raw.update(changes)
        with (
            patch.object(callback, "session_state", return_value=state()),
            patch.object(
                callback,
                "session_lineage",
                return_value={
                    "role": callback.Role.AGENT,
                    "root_session_id": "fixture",
                },
            ),
        ):
            return hooks.handle(raw, m.AgentKind.CODEX, self.store, spawn=False)


class QuestionHooks(HookFixture):
    def test_claude_second_call_attaches_to_blocked_batch_without_reblocking(self):
        raw = dict(
            hook_event_name="PreToolUse",
            session_id="fixture",
            tool_name="AskUserQuestion",
            tool_use_id="first",
            tool_input={"questions": [{"question": "First?"}]},
            cwd=str(self.directory),
        )
        current = state()
        current.update(
            agent=m.AgentKind.CLAUDE, key="claude:fixture", root="claude:fixture"
        )
        with (
            patch.object(callback, "session_state", return_value=current),
            patch.object(
                callback,
                "session_lineage",
                return_value={
                    "role": callback.Role.AGENT,
                    "root_session_id": "fixture",
                },
            ),
        ):
            hooks.handle(raw, m.AgentKind.CLAUDE, self.store, spawn=False)
            hooks.handle(
                {
                    **raw,
                    "tool_use_id": "second",
                    "tool_input": {"questions": [{"question": "Second?"}]},
                },
                m.AgentKind.CLAUDE,
                self.store,
                spawn=False,
            )
        (batch,) = self.store.batches()
        self.assertEqual(batch.state, m.BatchState.BLOCKED)
        self.assertEqual(batch.rev, 2)
        self.assertEqual(len(batch.questions), 2)
        self.assertEqual(len(self.store.due_registrations()), 2)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM notice").fetchone()[0], 1
        )

    def test_cli_refuses_overcap_identity_with_safe_local_receipt(self):
        bad = state()
        bad["task_id"] = "x" * (m.TASK_ID_MAX + 1)
        raw = dict(
            hook_event_name="PreToolUse",
            session_id="fixture",
            tool_name="request_user_input",
            tool_use_id="call",
            tool_input={"questions": [{"id": "one", "question": "Choose?"}]},
            cwd=str(self.directory),
        )
        with (
            patch.object(callback, "session_state", return_value=bad),
            patch.object(
                callback,
                "session_lineage",
                return_value={
                    "role": callback.Role.AGENT,
                    "root_session_id": "fixture",
                },
            ),
            patch.object(callback.rex, "STATE", self.directory),
            patch.object(callback, "receipt") as receipt,
            patch.object(
                sys, "argv", ["question_hooks.py", "hook", "--agent", "codex"]
            ),
            patch.object(sys, "stdin", io.StringIO(json.dumps(raw))),
            patch.object(sys, "stdout", io.StringIO()) as stdout,
        ):
            self.assertEqual(hooks.main(), 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")
        receipt.assert_called_once_with(callback.Outcome.ENQUEUE_FAILED)
        self.assertEqual(self.store.batches(), ())

    def test_detect_registers_unclassified_box_owned_without_dispatch(self):
        self.assertEqual(self.detect(), {})
        (first,) = self.store.batches()
        result = broker.Broker(self.store).worker(str(first.id), spool_digest=False)
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        (entry,) = self.store.due_registrations()
        self.assertFalse(entry["classified"])
        self.assertEqual(entry["callback_bot"], "jim")
        self.assertEqual(entry["root_session"], "fixture")
        self.assertEqual(entry["classification_floor"], None)
        self.assertEqual(entry["mac_floor"], m.EscalationLevel.L3_PRINCIPAL)

    def test_explicit_failure_retires_only_the_failed_call_and_duplicate_does_not_add(
        self,
    ):
        self.detect()
        self.detect()
        self.assertEqual(len(self.store.batches()[0].questions), 1)
        self.detect(
            hook_event_name="PostToolUse", tool_response={"error": "unavailable"}
        )
        self.detect(tool_name="request_user_input_async", tool_use_id="call-2")
        (batch,) = self.store.batches()
        self.assertEqual(batch.rev, 2)
        self.assertEqual(batch.questions[0].retired_rev, 2)
        self.assertEqual(batch.calls[1].fallback_of, "call-1")

    def test_same_text_without_failure_attaches_and_new_turn_opens_new_identity(self):
        self.detect()
        self.detect(tool_name="request_user_input_async", tool_use_id="call-2")
        (batch,) = self.store.batches()
        self.assertEqual(len(batch.questions), 2)
        self.assertIsNone(batch.questions[0].retired_rev)
        self.detect(turn_id="next", tool_use_id="call-3")
        self.assertEqual(len(self.store.batches()), 2)

    def test_flag_off_and_missing_row_do_not_create_batch(self):
        with patch.dict(os.environ, {m.FLAG_ENV: "0"}):
            self.detect()
        with patch.object(callback, "session_state", return_value=None):
            self.assertEqual(
                hooks.handle(
                    {"hook_event_name": "PreToolUse", "session_id": "missing"},
                    m.AgentKind.CODEX,
                    self.store,
                    spawn=False,
                ),
                {},
            )
        self.assertEqual(self.store.batches(), ())

    def test_native_answer_closes_ordinary_batch_before_same_turn_reask(self):
        self.detect()
        (first,) = self.store.batches()
        broker.Broker(self.store).worker(str(first.id), spool_digest=False)
        self.detect(
            hook_event_name="PostToolUse",
            tool_response={"answers": {"one": {"answers": ["Tabs"]}}},
        )
        self.assertEqual(
            self.store.get_batch(str(first.id)).state, m.BatchState.CANCELLED
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT cancel_cause FROM batch WHERE id=?", (str(first.id),)
            ).fetchone()[0],
            m.CancelCause.ANSWERED_IN_PANE,
        )
        self.detect(tool_use_id="second-call")
        current = self.store.batches()
        self.assertEqual(len(current), 2)
        self.assertNotEqual(current[0].id, current[1].id)


if __name__ == "__main__":
    unittest.main()
