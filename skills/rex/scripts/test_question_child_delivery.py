#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Synthetic §7.6 child relays; P13 live tool availability remains unproven."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
import unittest
from uuid import uuid4

import grokbot_callback as callback
import question_broker as broker
import question_delivery as delivery
import question_hooks as hooks
import question_model as m
import question_ledger as ledger
import test_question_broker_runplan as runplan
from test_question_hooks import state
from test_question_ledger import (
    classification,
    native_call,
    outbound,
    question,
    resolution,
)


class ChildDeliveryTests(unittest.TestCase):
    store: ledger.Ledger
    batch: m.QuestionBatch
    runner: runplan.StubRunner
    broker: broker.Broker
    setUp = runplan.RunPlanTests.setUp

    def child(self, *, agent=m.AgentKind.CODEX):
        who = replace(
            self.batch.asker,
            agent=agent,
            actor=m.ActorKey(callback.actor_key(agent, "fixture", "child")),
            role=m.ActorRole.NATIVE_SUBAGENT,
            session="fixture",
            root_actor=m.ActorKey(callback.actor_key(agent, "fixture")),
        )
        return self.store.open_or_attach(
            who,
            (question(),),
            native_call("child-call", actor=who.actor),
            classification(),
        ).batch

    def finish(self, actor):
        self.store.record_lifecycle(
            m.Lifecycle(
                actor,
                m.LifecycleKind.SESSION_ENDED,
                None,
                None,
                None,
                datetime.now(timezone.utc),
            )
        )

    def readers(self, agent=m.AgentKind.CODEX, root="fixture"):
        current = state()
        current.update(
            agent=agent, key=f"{agent}:fixture:child", root=f"{agent}:{root}"
        )
        return (
            patch.object(callback, "actor_state", return_value=current),
            patch.object(
                callback, "session_lineage", return_value={"root_session_id": root}
            ),
        )

    def text(self, batch):
        answer = resolution(batch).answers
        return m.render_answer(
            batch.id,
            m.ResolutionRev(1),
            batch.questions,
            answer,
            m.EscalationLevel.L3_PRINCIPAL,
            "/broker",
            m.OutboundId(uuid4()),
            "f" * 32,
            child_actor=batch.asker.actor,
        )

    def test_finished_child_status_and_tick_keep_root_live(self):
        batch = self.child()
        self.finish(batch.asker.actor)
        status = self.broker.status(batch_id=str(batch.id))["batches"][0]
        self.assertTrue(status["session_alive"])
        with patch.dict(broker.os.environ, {m.FLAG_ENV: "0"}):
            self.broker.tick()
        self.assertEqual(self.store.get_batch(str(batch.id)).state, m.BatchState.OPEN)
        self.finish(batch.asker.root_actor)
        self.assertFalse(
            self.broker.status(batch_id=str(batch.id))["batches"][0]["session_alive"]
        )

    def test_subagent_stop_hook_finishes_child_without_ending_root(self):
        batch = self.child()
        with (
            patch.dict(broker.os.environ, {m.FLAG_ENV: "1"}),
            patch.object(callback, "session_state", return_value=state()),
        ):
            hooks.handle(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": "fixture",
                    "agent_id": "child",
                },
                m.AgentKind.CODEX,
                self.store,
                spawn=False,
            )
        self.assertFalse(self.store.session_alive(batch.asker.actor))
        self.assertTrue(self.store.asker_session_alive(batch.asker))
        self.assertEqual(
            self.store.get_batch(str(batch.id)).classification.floor,
            m.EscalationLevel.L1_CHEAP,
        )

    def test_finished_child_box_send_reaches_root_with_preserved_answer(self):
        batch = self.child()
        self.finish(batch.asker.actor)
        text = self.text(batch)
        request = runplan.send_plan(
            batch,
            target_actor=batch.asker.root_actor,
            child_actor=batch.asker.actor,
            codex_thread="fixture",
            text=text,
        )
        first, second = self.readers()
        with first, second:
            result = self.broker.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(
            self.runner.calls[0].splitlines()[0],
            f"[qe:{batch.id}:r1] Your subagent {batch.asker.actor} asked a question; here is the answer from Dinesh (L3). If it has finished, decide whether its result is still valid. Otherwise pass the block below to that subagent unchanged, then run:",
        )
        self.assertEqual(self.runner.calls[0], text)
        self.assertTrue(
            any(
                n["kind"] == m.NoticeKind.LATE_DELIVERY
                for n in self.broker.status()["notices"]
            )
        )

    def test_changed_root_lineage_refuses_box_send(self):
        batch = self.child()
        request = runplan.send_plan(
            batch,
            target_actor=batch.asker.root_actor,
            child_actor=batch.asker.actor,
            codex_thread="fixture",
            text=self.text(batch),
        )
        first, second = self.readers(root="different")
        with first, second:
            result = self.broker.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_SESSION_GONE)
        self.assertEqual(self.runner.calls, [])

    def test_refused_finished_child_route_does_not_raise_delivery_notice(self):
        batch = self.child()
        self.finish(batch.asker.actor)
        request = runplan.send_plan(
            batch,
            target_actor=batch.asker.root_actor,
            child_actor=batch.asker.actor,
            codex_thread="fixture",
            text=self.text(batch),
        )
        self.runner.outcome = runplan.routes.RouteOutcome.REFUSED
        first, second = self.readers()
        with first, second:
            result = self.broker.run_plan(request)
        self.assertEqual(result["send"], m.SendOutcome.NOT_SENT_ROUTE_REFUSED)
        self.assertFalse(
            any(
                n["kind"] == m.NoticeKind.LATE_DELIVERY
                for n in self.broker.status()["notices"]
            )
        )

    def test_finished_child_local_delivery_reaches_live_root(self):
        batch = self.child()
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch),
            target_actor=batch.asker.root_actor,
            child_actor=batch.asker.actor,
            text=self.text(batch),
            route_plan=(m.Route.CODEX_QUEUE,),
        )
        self.store.resolve(resolution(batch), (message,))
        self.finish(batch.asker.actor)
        first, second = self.readers()
        with first, second:
            sent = delivery.Delivery(self.store, runner=self.runner).dispatch(
                str(message.id)
            )
        self.assertTrue(sent)
        self.assertIn("has finished", self.runner.calls[0])

    def test_pending_child_notice_names_child_without_stopping_root_turn(self):
        batch = self.child()
        self.store.take_ownership(str(batch.id), m.EscalationLevel.L3_PRINCIPAL)
        message = self.store.outbounds(str(batch.id))[0]
        self.assertEqual(message.target_actor, batch.asker.root_actor)
        self.assertEqual(message.child_actor, batch.asker.actor)
        self.assertIn(
            f"Your subagent {batch.asker.actor} asked a question", message.text
        )
        self.assertNotIn("end your turn", message.text)

    def test_resolve_local_renders_explicit_finished_variant(self):
        batch = self.child()
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        self.finish(batch.asker.actor)
        previous = resolution(batch)
        output = broker.answerers.AnswererOutput(
            m.Verdict.ANSWER,
            batch.classification,
            previous.answers,
            True,
            False,
            False,
            None,
        )
        self.broker.resolve_local(batch, output, previous.by)
        message = self.store.outbounds(str(batch.id))[0]
        first = message.text.splitlines()[0]
        self.assertEqual(
            first,
            f"[qe:{batch.id}:r1] Your subagent {batch.asker.actor} has finished, so this can't be delivered to it; decide whether its result is still valid.",
        )
        self.assertNotIn("pass the block below", message.text.lower())

    def test_claude_child_synthetic_inline_stays_in_child_context(self):
        batch = self.child(agent=m.AgentKind.CLAUDE)
        self.store.transition(str(batch.id), m.BatchState.ESCALATING)
        message = replace(
            outbound(batch),
            target_actor=batch.asker.actor,
            child_actor=None,
            route_plan=(m.Route.INLINE_UPDATED_INPUT,),
        )
        self.store.resolve(resolution(batch), (message,))
        input = m.parse_hook_input(
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "fixture",
                "agent_id": "child",
                "tool_name": "AskUserQuestion",
                "tool_use_id": "child-call",
                "tool_input": {
                    "questions": [{"question": batch.questions[0].text.text}]
                },
            },
            m.AgentKind.CLAUDE,
        )
        result = hooks.inline_output(self.store, str(batch.id), input)
        self.assertEqual(
            result["hookSpecificOutput"]["decision"]["behavior"],
            hooks.PermissionBehavior.ALLOW,
        )
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.DISPATCHED
        )
