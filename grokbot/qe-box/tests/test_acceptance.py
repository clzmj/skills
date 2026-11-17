"""Real broker/box acceptance fixtures, with terminal executables stubbed.

These prove protocol behavior, not the pending live Mac/LLM-bot matrix.
"""

from dataclasses import replace
import json
from typing import cast

import test_lease_fence as fence
from test_registration import box
from test_question_ledger import (
    classification,
    native_call,
    outbound,
    question,
    registration,
    resolution,
)

m = box.m


class AcceptanceTests(fence.BrokerBoxFixture):
    def test_l4_card_answer_is_delivered_only_under_box_authorization(self):
        self.box.handoff(self.entry["task_id"], "jim")
        card = self.box.card_intent(self.entry["task_id"], "jim", by="jim")
        self.box.card_posted(
            self.entry["task_id"], "jim", card.gen, "reviewed-card", by="jim"
        )
        plan = (
            self.box.decide(
                box.DecisionKind.ANSWER,
                4,
                self.entry["batch_id"],
                1,
                self.decision(),
                by="jim",
                card_gen=card.gen,
                card_ref="reviewed-card",
            )
            .plans[0]
            .plan
        )
        row = self.delivery()
        self.assertEqual(row.authorized_rev, self.entry["batch_rev"])
        self.assertEqual(row.authorized_floor, self.shown()["record"].floor)
        self.assertIsNotNone(plan["lease_id"])
        self.assertEqual(self.execute(plan)["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(self.sends(), [cast(m.SendBody, plan["body"])["text"]])
        # An identical plan replay must not type again.
        self.assertTrue(self.execute(plan)["replayed"])
        self.assertEqual(len(self.sends()), 1)

    def test_mac_local_resolution_cannot_authorize_l3_or_l4(self):
        for level in (m.EscalationLevel.L3_PRINCIPAL, m.EscalationLevel.L4_OWNER):
            with self.subTest(level=level):
                local = resolution(self.batch)
                # Model validation is a separate gate. Simulate a caller that
                # bypassed it so this assertion must exercise Ledger.resolve.
                object.__setattr__(local, "by", replace(local.by, level=level))
                with self.assertRaisesRegex(
                    ValueError, "local resolutions must be L1/L2"
                ):
                    self.store.resolve(local, (outbound(self.batch),))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.store.outbounds(str(self.batch.id)), ())

    def test_ten_plan_round_trips_reject_copy_corruption_before_typing(self):
        for index in range(10):
            with self.subTest(copy=index):
                if index:
                    who = replace(
                        self.batch.asker,
                        actor=m.ActorKey(f"fixture-{index}"),
                        root_actor=m.ActorKey(f"fixture-{index}"),
                        turn=f"turn-{index}",
                    )
                    q = replace(
                        question(),
                        digest=m.TextDigest(""),
                        options=self.batch.questions[0].options,
                    )
                    q = replace(q, digest=m.question_digest(q))
                    self.batch = self.store.open_or_attach(
                        who,
                        (q,),
                        native_call(
                            f"roundtrip-{index}", actor=who.actor, turn=who.turn
                        ),
                        classification(m.EscalationLevel.L3_PRINCIPAL),
                    ).batch
                    self.entry = registration(self.batch)
                    self.entry["questions"][0]["options"] = [
                        dict(index=0, label="Standard", description=None)
                    ]
                    self.entry["content_digest"] = m.digest(
                        {k: v for k, v in self.entry.items() if k != "content_digest"}
                    )
                    self.store.put_registration(self.entry)
                    self.box.register(self.entry)
                plan = self.issue()
                changed = json.loads(json.dumps(plan))
                changed["body"]["text"] += " copy error"
                with self.assertRaises(ValueError):
                    self.execute(changed)
                self.assertEqual(len(self.sends()), index)
                result = self.execute(plan)
                changed_result = dict(result, mac_batch_rev=999)
                with self.assertRaises(box.Refused):
                    self.submit(changed_result)
                self.assertEqual(self.delivery().state, m.DeliveryState.PENDING)
                self.submit(result)
                self.assertEqual(len(self.sends()), index + 1)
                self.assertEqual(self.sends()[-1], plan["body"]["text"])
                row = self.delivery()
                self.assertEqual((row.authorized_rev, row.authorized_floor), (1, 3))
