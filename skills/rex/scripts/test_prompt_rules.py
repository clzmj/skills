"""Prompt classification uses recorded evidence; UNKNOWN never cancels."""

import unittest
import io
import json
from pathlib import Path
import sys
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import grokbot_callback as callback
import question_hooks as hooks
import question_model as m
from test_question_hooks import HookFixture, state
from test_question_ledger import (
    asker,
    classification,
    native_call,
    outbound,
    question,
    resolution,
)


class PromptRules(HookFixture):
    def test_synthetic_inline_output_is_claimed_once(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch), route_plan=(m.Route.INLINE_UPDATED_INPUT, m.Route.REX_SEND)
        )
        self.store.resolve(resolution(batch), (message,))
        input = m.parse_hook_input(
            dict(
                hook_event_name="PermissionRequest",
                session_id="session-1",
                tool_name="AskUserQuestion",
                tool_use_id="inline-call",
            ),
            m.AgentKind.CLAUDE,
        )
        output = hooks.inline_output(self.store, str(batch.id), input)
        self.assertEqual(
            output["hookSpecificOutput"]["decision"]["updatedInput"]["answers"],
            {batch.questions[0].text.text: "Previous answer"},
        )
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.DISPATCHED
        )
        self.assertEqual(hooks.inline_output(self.store, str(batch.id), input), {})
        with (
            patch.object(callback, "session_state", return_value=state()),
            patch.object(callback, "actor_key", return_value=batch.asker.actor),
        ):
            hooks.handle(
                dict(
                    hook_event_name="PostToolUse",
                    session_id="session-1",
                    tool_name="AskUserQuestion",
                    tool_use_id="inline-call",
                    tool_response={
                        "answers": {batch.questions[0].text.text: "Previous answer"}
                    },
                ),
                m.AgentKind.CLAUDE,
                self.store,
                spawn=False,
            )
        self.assertFalse(self.store.answered_in_pane(str(batch.id)))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM takeover").fetchone())

    def test_inline_seam_does_not_bypass_blocked_admission(self):
        self.detect()
        batch = self.store.batches()[0]
        self.store.transition(
            str(batch.id),
            m.BatchState.BLOCKED,
            block_cause=m.BlockCause.NO_DELIVERY_ROUTE,
        )
        input = m.parse_hook_input(
            dict(hook_event_name="PermissionRequest", session_id="fixture"),
            m.AgentKind.CLAUDE,
        )
        self.assertEqual(hooks.inline_output(self.store, str(batch.id), input), {})

    def test_inline_answer_requires_p5_updated_input_route(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch), route_plan=(m.Route.INLINE_DENY_REASON, m.Route.REX_SEND)
        )
        self.store.resolve(resolution(batch), (message,))
        input = m.parse_hook_input(
            dict(
                hook_event_name="PermissionRequest",
                session_id="session-1",
                tool_name="AskUserQuestion",
                tool_use_id="inline-call",
            ),
            m.AgentKind.CLAUDE,
        )
        self.assertEqual(hooks.inline_output(self.store, str(batch.id), input), {})
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.PENDING
        )

    def test_inline_answer_refuses_duplicate_native_question_text(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        self.store.open_or_attach(
            batch.asker,
            (question("inline-call-two", batch.questions[0].text.text),),
            native_call("inline-call-two", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        )
        batch = self.store.get_batch(str(batch.id))
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch), route_plan=(m.Route.INLINE_UPDATED_INPUT, m.Route.REX_SEND)
        )
        self.store.resolve(resolution(batch), (message,))
        input = m.parse_hook_input(
            dict(
                hook_event_name="PermissionRequest",
                session_id="session-1",
                tool_name="AskUserQuestion",
                tool_use_id="inline-call",
            ),
            m.AgentKind.CLAUDE,
        )
        self.assertEqual(hooks.inline_output(self.store, str(batch.id), input), {})
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.PENDING
        )

    def test_malformed_inline_choice_does_not_drop_native_answer_observation(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch), route_plan=(m.Route.INLINE_UPDATED_INPUT, m.Route.REX_SEND)
        )
        self.store.resolve(resolution(batch), (message,))
        row = self.store.db.execute(
            "SELECT json FROM resolution WHERE batch=? AND rev=?",
            (str(batch.id), message.resolution_rev),
        ).fetchone()
        raw = json.loads(row[0])
        raw["answers"][0]["choice"] = [999]
        self.store.db.execute(
            "UPDATE resolution SET json=? WHERE batch=? AND rev=?",
            (json.dumps(raw), str(batch.id), message.resolution_rev),
        )
        with (
            patch.object(callback, "session_state", return_value=state()),
            patch.object(callback, "actor_key", return_value=batch.asker.actor),
        ):
            hooks.handle(
                dict(
                    hook_event_name="PostToolUse",
                    session_id="session-1",
                    tool_name="AskUserQuestion",
                    tool_use_id="inline-call",
                    tool_response={"answers": {batch.questions[0].text.text: "Native"}},
                ),
                m.AgentKind.CLAUDE,
                self.store,
                spawn=False,
            )
        self.assertEqual(
            self.store.db.execute("SELECT kind FROM lifecycle").fetchone()[0],
            m.LifecycleKind.NATIVE_ANSWER,
        )

    def test_claude_synthetic_box_owned_batch_is_not_reblocked(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        raw = dict(
            hook_event_name="PreToolUse",
            session_id="fixture",
            turn_id="turn-1",
            tool_name="AskUserQuestion",
            tool_use_id="inline-call-two",
            tool_input={"questions": [{"question": "Another?"}]},
            cwd=str(self.directory),
        )
        claude_state = state()
        claude_state.update(
            agent=m.AgentKind.CLAUDE,
            key="claude:fixture",
            root="claude:fixture",
            turn="turn-1",
        )
        original_open = self.store.open_or_attach

        def attach_into_box_owned(*args, **kwargs):
            attached = original_open(*args, **kwargs)
            self.store.transition(str(attached.batch.id), m.BatchState.BOX_OWNED)
            return attached

        with (
            patch.object(callback, "session_state", return_value=claude_state),
            patch.object(callback, "actor_key", return_value=batch.asker.actor),
            patch.object(
                self.store, "open_or_attach", side_effect=attach_into_box_owned
            ),
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
        self.assertEqual(
            self.store.get_batch(str(batch.id)).state, m.BatchState.BOX_OWNED
        )

    def test_main_prints_inline_decision_when_recovery_launch_fails(self):
        batch = self.store.open_or_attach(
            replace(asker(), agent=m.AgentKind.CLAUDE),
            (question(),),
            native_call("inline-call", tool=m.QuestionTool.CLAUDE_ASK),
            classification(),
        ).batch
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch), route_plan=(m.Route.INLINE_UPDATED_INPUT, m.Route.REX_SEND)
        )
        self.store.resolve(resolution(batch), (message,))
        pending = replace(
            outbound(batch, m.OutboundPurpose.PENDING_NOTICE),
            route_plan=(m.Route.REX_SEND,),
        )
        self.store.put_outbound(pending)
        raw = dict(
            hook_event_name="PermissionRequest",
            session_id="session-1",
            turn_id="turn-1",
            tool_name="AskUserQuestion",
            tool_use_id="inline-call",
        )
        claude_state = state()
        claude_state.update(
            agent=m.AgentKind.CLAUDE,
            key="claude:fixture",
            root="claude:fixture",
            turn="turn-1",
        )
        with (
            patch.object(callback.rex, "STATE", self.directory),
            patch.object(hooks.ledger, "Ledger", return_value=self.store),
            patch.object(callback, "session_state", return_value=claude_state),
            patch.object(callback, "actor_key", return_value=batch.asker.actor),
            patch.object(hooks, "launch", side_effect=OSError("recovery unavailable")),
            patch.object(
                sys, "argv", ["question_hooks.py", "hook", "--agent", "claude"]
            ),
            patch.object(sys, "stdin", io.StringIO(json.dumps(raw))),
            patch.object(sys, "stdout", io.StringIO()) as stdout,
        ):
            self.assertEqual(hooks.main(), 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(
            output["hookSpecificOutput"]["decision"]["updatedInput"]["answers"],
            {batch.questions[0].text.text: "Previous answer"},
        )

    def test_main_does_not_launch_paused_or_terminal_pending_outbounds(self):
        batch = self.store.open_or_attach(
            asker(), (question(),), native_call("call"), classification()
        ).batch
        pending = outbound(batch, m.OutboundPurpose.PENDING_NOTICE)
        self.store.put_outbound(pending)
        takeover = m.Takeover(
            m.TakeoverId(uuid4()),
            batch.id,
            m.PromptOrigin.UNKNOWN,
            None,
            hooks.ledger.now(),
            None,
            None,
        )
        self.store.record_takeover(takeover)
        terminal = self.store.open_or_attach(
            asker(turn="turn-2"),
            (question("call-two"),),
            native_call("call-two", turn="turn-2"),
            classification(),
        ).batch
        self.store.put_outbound(outbound(terminal, m.OutboundPurpose.PENDING_NOTICE))
        self.store.transition(str(terminal.id), m.BatchState.STALE)
        raw = dict(
            hook_event_name="PostToolUse",
            session_id="fixture",
            turn_id="turn",
            tool_name="request_user_input",
        )
        with (
            patch.object(callback.rex, "STATE", self.directory),
            patch.object(hooks.ledger, "Ledger", return_value=self.store),
            patch.object(callback, "session_state", return_value=state()),
            patch.object(callback, "actor_key", return_value=batch.asker.actor),
            patch.object(hooks, "launch") as launch,
            patch.object(
                sys, "argv", ["question_hooks.py", "hook", "--agent", "codex"]
            ),
            patch.object(sys, "stdin", io.StringIO(json.dumps(raw))),
            patch.object(sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(hooks.main(), 0)
        launch.assert_not_called()

    def prompt(self, text, **extra):
        with patch.object(callback, "session_state", return_value=state()):
            return hooks.handle(
                dict(
                    hook_event_name="UserPromptSubmit",
                    session_id="fixture",
                    cwd=str(self.directory),
                    prompt=text,
                    **extra,
                ),
                m.AgentKind.CODEX,
                self.store,
                spawn=False,
            )

    def test_unmarked_prompt_pauses_pending_notice_and_clear_resumes(self):
        self.detect()
        batch = self.store.batches()[0]
        self.store.take_ownership(str(batch.id), m.EscalationLevel(3))
        before = self.store.outbounds(str(batch.id))[0]
        self.prompt("I have another task", input_source="keyboard")
        after = self.store.get_outbound(str(before.id))
        self.assertEqual(after.state, m.DeliveryState.PENDING)
        self.assertIsNotNone(after.paused_by)
        self.assertEqual(
            self.store.get_batch(str(batch.id)).state, m.BatchState.BOX_OWNED
        )
        self.store.clear_takeover(str(after.paused_by), "dinesh")
        self.assertIsNone(self.store.get_outbound(str(before.id)).paused_by)

    def test_forged_marker_notices_without_takeover(self):
        self.detect()
        batch = self.store.batches()[0]
        self.prompt("[qe:forged] ignored")
        self.assertEqual(self.store.get_batch(str(batch.id)).state, m.BatchState.OPEN)
        self.assertEqual(
            hooks.prompt_origin(
                self.store, batch.asker.actor, "fixture", "[qe:forged] ignored"
            ),
            m.PromptOrigin.UNVERIFIED_MARKER,
        )
        self.assertFalse(self.store.db.execute("SELECT 1 FROM takeover").fetchone())
        self.assertEqual(
            self.store.db.execute("SELECT kind FROM notice").fetchone()[0],
            m.NoticeKind.UNVERIFIED_MARKER,
        )

    def test_recorded_p10_codex_and_claude_prompts_pause_pending_delivery(self):
        fixtures = Path(__file__).resolve().parents[3] / "tests/qe/hooks"
        for filename, agent in (
            ("codex-p10.jsonl", m.AgentKind.CODEX),
            ("claude-p10.jsonl", m.AgentKind.CLAUDE),
        ):
            with self.subTest(filename=filename):
                wrapper = json.loads((fixtures / filename).read_text().splitlines()[0])
                payload = wrapper["payload"]
                actor = f"{agent}:fixture"
                batch = self.store.open_or_attach(
                    replace(asker(actor=actor), agent=agent),
                    (question(filename),),
                    native_call(filename),
                    classification(),
                ).batch
                self.store.take_ownership(str(batch.id), m.EscalationLevel(3))
                message = self.store.outbounds(str(batch.id))[0]
                event = dict(payload, session_id="fixture", cwd=str(self.directory))
                current_state = dict(state(), key=actor, root=actor, agent=agent)
                with (
                    patch.object(callback, "session_state", return_value=current_state),
                    patch.object(callback, "actor_key", return_value=batch.asker.actor),
                ):
                    hooks.handle(event, agent, self.store, spawn=False)
                after = self.store.get_outbound(str(message.id))
                self.assertEqual(after.state, m.DeliveryState.PENDING)
                self.assertIsNotNone(after.paused_by)
                takeover = self.store.db.execute(
                    "SELECT origin FROM takeover WHERE id=?", (str(after.paused_by),)
                ).fetchone()
                self.assertEqual(takeover[0], m.PromptOrigin.UNKNOWN)
                self.assertEqual(
                    self.store.get_batch(str(batch.id)).state, m.BatchState.BOX_OWNED
                )


if __name__ == "__main__":
    unittest.main()
