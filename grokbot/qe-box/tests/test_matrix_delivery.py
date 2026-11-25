"""Joined X9/X13/X19 flows with real box/broker code and terminal fixtures."""

from dataclasses import replace
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_lease_fence import BrokerBoxFixture, box, broker, m
from test_question_ledger import classification, native_call, outbound, resolution
from test_registration import progress
from test_question_hooks import state
import question_hooks as hooks


class MatrixDeliveryTests(BrokerBoxFixture):
    entry: m.RegisterEntry

    def open_batch(self, *, floor=3, cwd=None, head=None):
        actor = m.ActorKey("codex:matrix-session")
        who = replace(
            self.batch.asker,
            actor=actor,
            root_actor=actor,
            session="matrix-session",
            task="matrix-task",
            turn="matrix-turn",
            cwd=str(cwd) if cwd else self.batch.asker.cwd,
            head_sha=head or self.batch.asker.head_sha,
        )
        question = replace(
            self.batch.questions[0],
            id=m.QuestionId(box.uuid4()),
            origin_call=m.NativeCallId("matrix-call"),
            digest=m.TextDigest(""),
        )
        question = replace(question, digest=m.question_digest(question))
        policy = replace(
            classification(m.EscalationLevel(floor)),
            delegable=True,
            category=m.Category.REPO_CONVENTION,
        )
        self.batch = self.store.open_or_attach(
            who,
            (question,),
            native_call("matrix-call", actor=actor, turn="matrix-turn"),
            policy,
        ).batch
        prepared = broker.prepare_registration(self.store, self.batch)
        assert prepared is not None
        self.entry = prepared
        self.ack = self.box.register(self.entry).plans[0].plan

    def card(self, *, purpose=box.CardPurpose.ASK):
        claimed = self.box.card_intent(
            self.entry["task_id"], "jim", purpose=purpose, by="jim"
        )
        return self.box.card_posted(
            self.entry["task_id"],
            "jim",
            claimed.gen,
            f"matrix-card-{claimed.gen}",
            by="jim",
        )

    def l4(self, card, *, kind=box.DecisionKind.ANSWER, supersedes=None):
        return (
            self.box.decide(
                kind,
                4,
                self.entry["batch_id"],
                self.entry["batch_rev"],
                self.decision(),
                by="jim",
                card_gen=card.gen,
                card_ref=card.ref,
                supersedes=supersedes,
            )
            .plans[0]
            .plan
        )

    def git(self, directory, *args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=QE Fixture",
                "-c",
                "user.email=qe@example.invalid",
                *args,
            ],
            cwd=directory,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def test_x13_head_move_refuses_send_then_new_card_reconfirms_latest_revision(self):
        repo = Path(self.temp.name) / "head-fixture"
        repo.mkdir()
        self.git(repo, "init", "--quiet")
        self.git(repo, "commit", "--quiet", "--allow-empty", "-m", "initial")
        initial_head = self.git(repo, "rev-parse", "HEAD")
        self.open_batch(floor=4, cwd=repo, head=initial_head)
        self.box.handoff(self.entry["task_id"], "jim")
        old = self.card()
        # HEAD moves while Carlos's old-revision card is awaiting an answer.
        self.git(repo, "commit", "--quiet", "--allow-empty", "-m", "new context")
        next_head = self.git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(initial_head, next_head)
        old_plan = self.l4(old)
        refused = self.execute(old_plan)
        self.assertEqual(refused["send"], m.SendOutcome.NOT_SENT_STALE_REVISION)
        self.assertEqual(refused["mac_batch_rev"], 2)
        self.assertEqual(self.sends(), [])
        self.submit(refused)
        self.batch = self.store.get_batch(self.entry["batch_id"])
        previous = self.entry
        prepared = broker.prepare_registration(self.store, self.batch)
        assert prepared is not None
        self.entry = prepared
        self.assertEqual(previous["questions"], self.entry["questions"])
        self.assertEqual(self.entry["head_sha"], next_head)
        self.box.register(self.entry)
        self.assertEqual(self.shown()["cards"][0].state, box.CardGenState.SUPERSEDED)
        self.assertEqual(self.shown()["record"].latest_rev, 2)
        self.box.handoff(self.entry["task_id"], "jim")
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.l4, old)
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.card)
        self.advance(15 * 60)
        current = self.card()
        confirmed = self.l4(current)
        self.assertEqual(confirmed["body"]["batch_rev"], 2)
        self.assertEqual(self.execute(confirmed)["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(self.sends(), [confirmed["body"]["text"]])
        delivery = self.shown()["deliveries"][-1]
        self.assertEqual((delivery.authorized_rev, delivery.authorized_floor), (2, 4))

    def test_x19_root_stop_preserves_pending_batch_and_later_send_uses_idle_route(self):
        self.open_batch()
        self.execute(self.ack)
        self.assertTrue(
            any(
                message.purpose is m.OutboundPurpose.PENDING_NOTICE
                for message in self.store.outbounds(str(self.batch.id))
            )
        )
        completed = state()
        completed.update(
            key=self.batch.asker.actor,
            id=self.batch.asker.session,
            root=self.batch.asker.root_actor,
            status=broker.callback.Status.COMPLETED,
        )
        with patch.object(broker.callback, "session_state", return_value=completed):
            with patch.dict(broker.os.environ, {m.FLAG_ENV: "1"}):
                hooks.handle(
                    {"hook_event_name": "Stop", "session_id": self.batch.asker.session},
                    m.AgentKind.CODEX,
                    self.store,
                    spawn=False,
                )
            self.assertTrue(self.store.session_alive(self.batch.asker.actor))
            self.assertTrue(
                self.mac.status(batch_id=str(self.batch.id))["batches"][0][
                    "session_alive"
                ]
            )
            with patch.dict(broker.os.environ, {m.FLAG_ENV: "0"}):
                self.mac.tick()
            self.assertEqual(
                self.store.get_batch(str(self.batch.id)).state, m.BatchState.BOX_OWNED
            )
            self.assertEqual(
                self.shown()["record"].state, box.BoxBatchState.AWAITING_L3
            )
            plan = self.issue()
            result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(result["route_used"], m.Route.CODEX_QUEUE_RESUME)
        self.submit(result)
        self.assertEqual(self.sends(), [plan["body"]["text"]])
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.DELIVERED)

    def test_x9_l2_correction_uses_box_resolution_two_and_suppresses_local_outbound(
        self,
    ):
        self.open_batch(floor=2)
        local = resolution(self.batch)
        local = replace(local, by=replace(local.by, level=m.EscalationLevel.L2_NORMAL))
        pending = outbound(self.batch)
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        self.store.resolve(local, (pending,))
        self.box.register(progress(self.entry, local=1, local_level=2))
        card = self.card(purpose=box.CardPurpose.CORRECTION)
        plan = self.l4(card, kind=box.DecisionKind.CORRECTION, supersedes=1)
        self.assertEqual(plan["body"]["resolution_rev"], 2)
        result = self.execute(plan)
        self.assertEqual(result["send"], m.SendOutcome.SENT_NO_ECHO)
        messages = self.store.outbounds(str(self.batch.id))
        local_row = next(message for message in messages if message.id == pending.id)
        self.assertEqual(local_row.state, m.DeliveryState.SUPPRESSED)
        self.assertEqual(self.sends(), [plan["body"]["text"]])
        self.submit(result)
        self.assertEqual(self.shown()["record"].state, box.BoxBatchState.DELIVERED)
