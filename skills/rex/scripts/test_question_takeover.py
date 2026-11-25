"""Local resolution, pending-notice and takeover public seams."""

from dataclasses import replace
from pathlib import Path
import os
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import question_delivery as delivery
import question_ledger as ledger
import question_model as m
import question_hooks as hooks
import codex_sessions
import grokbot_callback as callback
import question_routes as routes
import question_broker as broker
from test_question_broker_runplan import plan
from test_question_ledger import (
    asker,
    classification,
    native_call,
    outbound,
    question,
    resolution,
)


class AcceptedRoutes(routes.RouteRunner):
    def __init__(self):
        super().__init__()
        self.texts = []

    def send(self, route, body, fence):
        failure = fence()
        if failure:
            return routes.RouteResult(routes.RouteOutcome.FENCED, failure, route)
        self.texts.append(body["text"])
        return routes.RouteResult(routes.RouteOutcome.ACCEPTED, None, route)


class LocalTakeover(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = ledger.Ledger(Path(self.folder.name) / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        self.batch = self.store.open_or_attach(
            asker(), (question(),), native_call("call-1"), classification()
        ).batch
        self.runner = AcceptedRoutes()
        self.delivery = delivery.Delivery(self.store, runner=self.runner)

    def resolve(self):
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        message = replace(outbound(self.batch), route_plan=(m.Route.REX_SEND,))
        self.store.resolve(resolution(self.batch), (message,))
        return message

    def takeover(self, origin):
        item = m.Takeover(
            m.TakeoverId(uuid4()), self.batch.id, origin, None, ledger.now(), None, None
        )
        self.store.record_takeover(item)
        return item

    def test_human_after_resolution_suppresses_uncertain_without_cancelling_batch(self):
        message = self.resolve()
        claim = self.store.claim(str(message.id), "worker", m.Route.REX_SEND)
        assert claim is not None
        self.store.finish_claim(claim, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.takeover(m.PromptOrigin.HUMAN)
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.RESOLVED_LOCAL
        )
        after = self.store.get_outbound(str(message.id))
        self.assertEqual(after.state, m.DeliveryState.SUPPRESSED)
        self.assertEqual(after.suppress_cause, m.SuppressCause.HUMAN_TAKEOVER)

    def test_unknowns_clear_individually_without_resuming_uncertainty(self):
        message = self.resolve()
        first = self.takeover(m.PromptOrigin.UNKNOWN)
        second = self.takeover(m.PromptOrigin.UNKNOWN)
        self.store.clear_takeover(str(first.id), "dinesh")
        self.assertEqual(self.store.get_outbound(str(message.id)).paused_by, second.id)
        self.store.clear_takeover(str(second.id), "dinesh")
        self.assertTrue(self.delivery.dispatch(str(message.id)))
        self.assertEqual(self.runner.texts, [message.text])

    def test_clear_takeover_refuses_missing_and_human_ids_in_plan_and_cli(self):
        human = self.takeover(m.PromptOrigin.HUMAN)
        for identifier in (str(uuid4()), str(human.id)):
            with self.subTest(identifier=identifier):
                request = plan(
                    m.PlanAction.CLEAR_TAKEOVER,
                    dict(takeover_id=identifier, by="dinesh"),
                )
                result = broker.Broker(self.store).run_plan(request)
                self.assertFalse(result["ok"])
                process = subprocess.run(
                    [
                        sys.executable,
                        str(Path(broker.__file__)),
                        "clear-takeover",
                        "--takeover",
                        identifier,
                        "--by",
                        "dinesh",
                    ],
                    env={**os.environ, "REX_VISIBLE_STATE_DIR": self.folder.name},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(process.returncode, 0)

    def test_clear_unknown_is_idempotent(self):
        unknown = self.takeover(m.PromptOrigin.UNKNOWN)
        self.store.clear_takeover(str(unknown.id), "dinesh")
        self.store.clear_takeover(str(unknown.id), "dinesh")

    def test_observe_while_claimed_closes_attempt_before_late_result(self):
        message = self.resolve()
        claim = self.store.claim(str(message.id), "worker", m.Route.REX_SEND)
        assert claim is not None
        self.assertTrue(
            self.store.observe_outbounds(message.target_actor, message.text)
        )
        self.assertFalse(self.store.finish_claim(claim, m.DeliveryState.DISPATCHED))
        row = self.store.db.execute(
            "SELECT result,finished_at FROM dispatch_attempt"
        ).fetchone()
        self.assertEqual(row["result"], m.DeliveryState.OBSERVED)
        self.assertIsNotNone(row["finished_at"])

    def test_native_takeover_closes_active_dispatch_attempt(self):
        message = self.resolve()
        self.store.claim(str(message.id), "worker", m.Route.REX_SEND)
        self.takeover(m.PromptOrigin.HUMAN)
        row = self.store.db.execute(
            "SELECT result,finished_at FROM dispatch_attempt"
        ).fetchone()
        self.assertEqual(row["result"], m.DeliveryState.SUPPRESSED)
        self.assertIsNotNone(row["finished_at"])

    def test_ownership_suppresses_answer_but_not_pending_and_intent_is_immutable(self):
        message = self.resolve()
        self.store.take_ownership(str(self.batch.id), m.EscalationLevel.L3_PRINCIPAL)
        self.assertFalse(self.delivery.dispatch(str(message.id)))
        first = self.store.outbounds(str(self.batch.id))[-1]
        self.store.take_ownership(str(self.batch.id), m.EscalationLevel.L3_PRINCIPAL)
        self.assertEqual(self.store.get_outbound(str(first.id)), first)
        self.assertTrue(self.delivery.dispatch(str(first.id)))
        self.assertEqual(self.runner.texts, [first.text])

    def test_pending_notice_only_suppressed_by_arrived_box_note(self):
        for state in m.BoxNoteState:
            with self.subTest(state=state):
                self.store.take_ownership(
                    str(self.batch.id), m.EscalationLevel.L3_PRINCIPAL
                )
                message = self.store.outbounds(str(self.batch.id))[-1]
                note = m.BoxNote(
                    m.DeliveryId(str(uuid4())),
                    m.SendAttempt(1),
                    m.PlanId(str(uuid4())),
                    self.batch.id,
                    self.batch.rev,
                    m.ResolutionRev(1),
                    m.DeliveryPurpose.ANSWER,
                    self.batch.asker.actor,
                    None,
                    "box answer",
                    m.body_digest("box answer"),
                    m.TextDigest("0" * 64),
                    m.EscalationLevel.L3_PRINCIPAL,
                    state,
                    None,
                    ledger.now(),
                )
                self.store.put_box_note(note)
                accepted = self.delivery.dispatch(str(message.id))
                self.assertEqual(accepted, state not in delivery.ARRIVED)
                self.store.db.execute("DELETE FROM box_note")
                self.store.db.execute("DELETE FROM dispatch_attempt")
                self.store.db.execute("DELETE FROM outbound")

    def test_observe_recomputes_text_and_ack_does_not_resend(self):
        message = self.resolve()
        self.assertTrue(self.delivery.dispatch(str(message.id)))
        self.assertTrue(
            self.store.observe_outbounds(message.target_actor, message.text + "\n")
        )
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.OBSERVED
        )
        self.assertFalse(self.store.ack(str(message.id), "wrong"))
        self.assertTrue(self.store.ack(str(message.id), message.ack_token))
        self.assertFalse(self.delivery.dispatch(str(message.id)))
        self.assertEqual(self.runner.texts, [message.text])

    def test_live_brief_change_prevents_local_dispatch(self):
        brief = Path(self.folder.name) / "brief.md"
        brief.write_text("changed after resolution")
        self.store.db.execute("UPDATE batch SET brief_path=?", (str(brief),))
        message = self.resolve()
        self.assertFalse(self.delivery.dispatch(str(message.id)))
        self.assertEqual(self.runner.texts, [])
        self.assertEqual(self.store.get_batch(str(self.batch.id)).rev, 2)

    def test_ended_target_before_delivery_abandons_without_typing_x5(self):
        message = self.resolve()
        self.store.record_lifecycle(
            m.Lifecycle(
                message.target_actor,
                m.LifecycleKind.SESSION_ENDED,
                None,
                None,
                None,
                ledger.now(),
            )
        )
        self.assertFalse(self.delivery.dispatch(str(message.id)))
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.ABANDONED
        )
        self.assertEqual(self.runner.texts, [])

    def test_unknown_after_rex_text_staging_stays_uncertain_after_clear(self):
        message = self.resolve()
        directory = Path(self.folder.name)
        stub = directory / "rex"
        log = directory / "sent.txt"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\nfrom pathlib import Path\nfrom uuid import uuid4\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "import question_ledger as l, question_model as m\n"
            "assert sys.argv[1] == 'send' and len(sys.argv) == 4\n"
            f"Path({str(log)!r}).open('a').write(sys.stdin.read())\n"
            f"with l.Ledger(Path({str(self.store.path)!r})) as store:\n"
            f" store.record_takeover(m.Takeover(m.TakeoverId(uuid4()), store.get_batch({str(self.batch.id)!r}).id, m.PromptOrigin.UNKNOWN, None, l.now(), None, None))\n"
        )
        stub.chmod(0o755)
        with patch.dict(
            os.environ, PATH=f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
        ):
            self.assertFalse(delivery.Delivery(self.store).dispatch(str(message.id)))
        after = self.store.get_outbound(str(message.id))
        self.assertEqual(after.state, m.DeliveryState.DISPATCH_UNCERTAIN)
        self.assertEqual(log.read_text(), message.text)
        self.store.clear_takeover(str(after.paused_by), "dinesh")
        self.assertFalse(self.delivery.dispatch(str(message.id)))

    def test_x3_human_takeover_cancels_open_batch_but_unknown_pauses(self):
        """X3: positive keyboard evidence cancels; uncertain input only pauses."""
        self.store.take_ownership(str(self.batch.id), m.EscalationLevel.L3_PRINCIPAL)
        pending = self.store.outbounds(str(self.batch.id))[0]
        unknown = m.Takeover(
            m.TakeoverId(uuid4()),
            self.batch.id,
            m.PromptOrigin.UNKNOWN,
            None,
            ledger.now(),
            None,
            None,
        )
        self.store.record_takeover(unknown)
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_OWNED
        )
        self.assertEqual(self.store.get_outbound(str(pending.id)).paused_by, unknown.id)

        other = self.store.open_or_attach(
            asker(turn="turn-2"),
            (question("call-2"),),
            native_call("call-2", turn="turn-2"),
            classification(),
        ).batch
        self.store.transition(str(other.id), m.BatchState.ESCALATING)
        human = m.Takeover(
            m.TakeoverId(uuid4()),
            other.id,
            m.PromptOrigin.HUMAN,
            None,
            ledger.now(),
            None,
            None,
        )
        self.store.record_takeover(human)
        self.assertEqual(
            self.store.get_batch(str(other.id)).state, m.BatchState.CANCELLED
        )

    def test_x3b_broker_boxnote_and_unverified_markers_are_not_takeovers(self):
        message = self.resolve()
        self.assertEqual(
            hooks.prompt_origin(
                self.store, self.batch.asker.actor, "fixture", message.text
            ),
            m.PromptOrigin.BROKER,
        )
        note = m.BoxNote(
            m.DeliveryId(str(uuid4())),
            m.SendAttempt(1),
            m.PlanId(str(uuid4())),
            self.batch.id,
            self.batch.rev,
            m.ResolutionRev(1),
            m.DeliveryPurpose.ANSWER,
            self.batch.asker.actor,
            None,
            "box answer",
            m.body_digest("box answer"),
            m.TextDigest("0" * 64),
            m.EscalationLevel.L3_PRINCIPAL,
            m.BoxNoteState.SENT,
            None,
            ledger.now(),
        )
        self.store.put_box_note(note)
        event = patch.object(
            codex_sessions, "DB", Path(self.folder.name) / "coordination.sqlite3"
        )
        event.start()
        try:
            codex_sessions.event(
                "team",
                "agent",
                "queue_requested",
                "collaborator text",
                recipient="fixture",
                identifier="message-1",
            )
            collaborator = (
                "[Codex team team; from agent; message-id message-1]\ncollaborator text"
            )
            cases = (
                (message.text, m.PromptOrigin.BROKER),
                ("box answer", m.PromptOrigin.BOX),
                (collaborator, m.PromptOrigin.COLLABORATOR),
                ("[qe:forged] ignored", m.PromptOrigin.UNVERIFIED_MARKER),
                (
                    "[Codex team fixture; from agent; message-id missing] hello",
                    m.PromptOrigin.UNVERIFIED_MARKER,
                ),
            )
            for text, expected in cases:
                with self.subTest(text=text[:24]):
                    with (
                        patch.dict(os.environ, {m.FLAG_ENV: "1"}),
                        patch.object(
                            callback,
                            "session_state",
                            return_value={
                                "key": "codex:fixture",
                                "id": "fixture",
                                "agent": m.AgentKind.CODEX,
                                "parent": "",
                                "root": "codex:fixture",
                                "depth": 0,
                                "task_id": "fixture",
                                "requester": "Jim",
                                "turn": "turn",
                                "status": callback.Status.RUNNING,
                                "rex_session": "fixture",
                                "rex_block": "block:fixture",
                            },
                        ),
                        patch.object(
                            callback, "actor_key", return_value=self.batch.asker.actor
                        ),
                    ):
                        hooks.handle(
                            {
                                "hook_event_name": m.HookEvent.USER_PROMPT_SUBMIT,
                                "session_id": "fixture",
                                "turn_id": "turn",
                                "prompt": text,
                            },
                            m.AgentKind.CODEX,
                            self.store,
                            spawn=False,
                        )
                    self.assertEqual(
                        self.store.db.execute(
                            "SELECT origin FROM lifecycle ORDER BY rowid DESC LIMIT 1"
                        ).fetchone()[0],
                        expected,
                    )
                    self.assertFalse(
                        self.store.db.execute("SELECT 1 FROM takeover").fetchone()
                    )
                    self.assertEqual(
                        self.store.get_batch(str(self.batch.id)).state,
                        m.BatchState.RESOLVED_LOCAL,
                    )
                    self.assertIsNone(
                        self.store.get_outbound(str(message.id)).paused_by
                    )
                    if expected is m.PromptOrigin.BROKER:
                        self.assertEqual(
                            self.store.get_outbound(str(message.id)).state,
                            m.DeliveryState.OBSERVED,
                        )
                    if expected is m.PromptOrigin.BOX:
                        self.assertEqual(
                            self.store.db.execute(
                                "SELECT state FROM box_note"
                            ).fetchone()[0],
                            m.BoxNoteState.ECHOED,
                        )
        finally:
            event.stop()

    def test_x3c_human_after_resolution_suppresses_before_dispatch(self):
        message = self.resolve()
        self.takeover(m.PromptOrigin.HUMAN)
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.SUPPRESSED
        )
        self.assertEqual(
            self.store.get_outbound(str(message.id)).suppress_cause,
            m.SuppressCause.HUMAN_TAKEOVER,
        )
        self.assertFalse(self.delivery.dispatch(str(message.id)))
        self.assertEqual(self.runner.texts, [])


if __name__ == "__main__":
    unittest.main()
