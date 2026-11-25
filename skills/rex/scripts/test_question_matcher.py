#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bind Claude call-less events to one full semantic PreToolUse match.

The P4 recorder captures real root-interactive hook payloads. Its PermissionRequest
payload has no tool_use_id; the adapter binds it to the recorded PreToolUse by
complete normalized question semantics, scoped to the caller's open turn.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

import claude_questions as adapter
import grokbot_callback as callback
import question_hooks as hooks
import question_ledger as ledger
import question_model as m

ROOT = Path(__file__).resolve().parents[3]


def recorded_events() -> tuple[dict[str, Any], dict[str, Any]]:
    path = ROOT / "tests/qe/hooks/claude-p4.jsonl"
    events = [json.loads(line)["payload"] for line in path.read_text().splitlines()]
    pre = next(
        event
        for event in events
        if event["hook_event_name"] == m.HookEvent.PRE_TOOL_USE
    )
    permission = next(
        event
        for event in events
        if event["hook_event_name"] == m.HookEvent.PERMISSION_REQUEST
    )
    return pre, permission


class ClaudeQuestionMatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.pre_raw, self.permission_raw = recorded_events()
        self.pre = m.parse_hook_input(self.pre_raw, m.AgentKind.CLAUDE)
        self.permission = m.parse_hook_input(self.permission_raw, m.AgentKind.CLAUDE)
        detected = adapter.detect(self.pre)
        assert detected is not None
        self.pre_call = detected.call
        self.questions = adapter.normalize(detected)

    def test_recorded_permission_binds_to_exact_pretool_call(self) -> None:
        self.assertIsNone(self.permission.tool_use_id)
        self.assertEqual(
            adapter.bind_call(self.permission, self.questions),
            self.pre_call,
        )

    def test_binding_compares_all_semantic_fields(self) -> None:
        self.assertIsNone(adapter.bind_call(self.permission, ()))
        question = self.questions[0]
        changed_fields = (
            dict(text=m.Untrusted("Different?", m.UntrustedSource.AGENT)),
            dict(header=m.Untrusted("Different", m.UntrustedSource.AGENT)),
            dict(options=()),
            dict(multi_select=True),
            dict(allows_free_text=False),
        )
        for change in changed_fields:
            changed = replace(question, **change, digest=m.TextDigest(""))
            self.assertIsNone(adapter.bind_call(self.permission, (changed,)))
        wrong_event = replace(self.permission, event=m.HookEvent.USER_PROMPT_SUBMIT)
        self.assertIsNone(adapter.bind_call(wrong_event, self.questions))
        explicit_id = replace(self.permission, tool_use_id="already-known")
        self.assertIsNone(adapter.bind_call(explicit_id, self.questions))
        missing_id_post = replace(self.permission, event=m.HookEvent.POST_TOOL_USE)
        self.assertEqual(
            adapter.bind_call(missing_id_post, self.questions), self.pre_call
        )

    def test_ambiguous_identical_calls_are_not_bound(self) -> None:
        second = replace(
            self.questions[0],
            origin_call=m.NativeCallId("another-known-call"),
            digest=m.TextDigest(""),
        )
        self.assertIsNone(adapter.bind_call(self.permission, (*self.questions, second)))

    @staticmethod
    def _callback_state(
        agent: callback.AgentKind, session: str, child: str = ""
    ) -> callback.SessionState:
        actor = callback.actor_key(agent, session, child)
        return callback.SessionState(
            key=f"{agent}:{actor}",
            id=session,
            agent=agent,
            parent="",
            root=f"{agent}:{actor}",
            depth=0,
            task_id="fixture:claude-p4",
            requester="Jim",
            turn="fixture-turn",
            status=callback.Status.RUNNING,
            rex_session="fixture-session",
            rex_block="block:fixture",
        )

    @staticmethod
    def _lineage(state: callback.SessionState) -> callback.Lineage:
        return callback.Lineage(
            requester_bot=state["requester"],
            root_session_id=state["id"],
            parent_session_id="",
            agent_session_id=state["id"],
            depth=0,
            agent_kind=state["agent"],
            role=callback.Role.AGENT,
        )

    def test_recorded_p4_hooks_attach_once_to_one_blocked_batch(self) -> None:
        """Replay P4 with fixture-local identity and state; no host calls run."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = ledger.Ledger(root / m.LEDGER_FILE)
            self.addCleanup(store.close)
            session = "fixture-claude-p4"
            pre = dict(
                self.pre_raw,
                session_id=session,
                cwd=str(root),
            )
            permission = dict(
                self.permission_raw,
                session_id=session,
                cwd=str(root),
            )
            env = patch.dict(
                os.environ,
                {
                    "HOME": str(root / "home"),
                    "REX_VISIBLE_STATE_DIR": str(root / "state"),
                    m.FLAG_ENV: "1",
                    m.BRIEF_ENV: "",
                },
            )
            env.start()
            self.addCleanup(env.stop)
            state = self._callback_state
            state_patch = patch.object(callback, "session_state", side_effect=state)
            state_patch.start()
            self.addCleanup(state_patch.stop)
            lineage_patch = patch.object(
                callback, "session_lineage", side_effect=self._lineage
            )
            lineage_patch.start()
            self.addCleanup(lineage_patch.stop)
            git_patch = patch.object(
                callback,
                "git_metadata",
                return_value={"repo": "", "branch": "", "head_sha": ""},
            )
            git_patch.start()
            self.addCleanup(git_patch.stop)
            remote_patch = patch.object(hooks, "remote", return_value="")
            remote_patch.start()
            self.addCleanup(remote_patch.stop)
            state_dir_patch = patch.object(callback.rex, "STATE", root / "state")
            state_dir_patch.start()
            self.addCleanup(state_dir_patch.stop)

            matches: list[m.NativeCallId | None] = []
            attached_calls: list[m.NativeCallId] = []
            original_bind = adapter.bind_call

            def bind_and_record(
                input: m.HookInput, questions: tuple[m.Question, ...]
            ) -> m.NativeCallId | None:
                matched = original_bind(input, questions)
                matches.append(matched)
                return matched

            open_or_attach = store.open_or_attach

            def trace_attach(*args: Any, **kwargs: Any) -> m.AttachResult:
                attached_calls.append(args[2].call)
                return open_or_attach(*args, **kwargs)

            with patch.object(
                hooks.claude_questions, "bind_call", side_effect=bind_and_record
            ) as bind:
                with patch.object(store, "open_or_attach", side_effect=trace_attach):
                    hooks.handle(pre, m.AgentKind.CLAUDE, store, spawn=False)
                    hooks.handle(permission, m.AgentKind.CLAUDE, store, spawn=False)
                self.assertEqual(bind.call_count, 1)
                self.assertEqual(
                    bind.call_args.args[0].event, m.HookEvent.PERMISSION_REQUEST
                )
                self.assertEqual(matches, [self.pre_call])
                self.assertEqual(attached_calls, [self.pre_call, self.pre_call])

            batches = store.batches()
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0].rev, 1)
            self.assertEqual(len(batches[0].questions), 1)
            self.assertEqual(len(batches[0].calls), 1)
            self.assertEqual(batches[0].state, m.BatchState.BLOCKED)
            self.assertEqual(len(store.due_registrations()), 1)
            notices = store.db.execute("SELECT kind FROM notice").fetchall()
            self.assertEqual([row[0] for row in notices], [m.NoticeKind.BLOCKED])


if __name__ == "__main__":
    unittest.main()
