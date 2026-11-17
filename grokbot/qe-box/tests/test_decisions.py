#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Decision authorization is box state, reviewed revision and active card."""

from test_registration import BoxTest, entry, box


def answer(value, *, text=None):
    return dict(
        schema="qe.decision/1",
        answers=[
            dict(
                question_id=q["id"],
                choice=[0],
                free_text=text,
                rationale="Use the reviewed standard.",
            )
            for q in value["questions"]
            if q["retired_rev"] is None
        ],
        clearances=[],
    )


class DecisionTests(BoxTest):
    def test_l3_decision_mints_leased_send_with_immutable_text(self):
        value = entry()
        self.box.register(value)
        output = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        )
        plan = output.plans[0].plan
        self.assertEqual(plan["action"], box.m.PlanAction.SEND)
        self.assertEqual(plan["body"]["batch_rev"], 1)
        self.assertEqual(plan["body"]["floor"], 3)
        self.assertIn("Answer to your question", plan["body"]["text"])
        record = self.box.show(value["batch_id"])
        self.assertEqual(record["record"].state, box.BoxBatchState.DECIDED)
        self.assertIsNone(record["record"].correction_floor)
        self.assertRefused(
            box.BoxRefusal.LEASED, self.box.deliver, record["deliveries"][0].id
        )

    def test_every_decision_authority_revision_floor_and_card_rejection(self):
        value = entry()
        self.box.register(value)
        for refusal, kwargs in (
            (box.BoxRefusal.STALE_REVISION, dict(expected_rev=2)),
            (box.BoxRefusal.WRONG_STATE, dict(by="jim")),
            (
                box.BoxRefusal.VALIDATION,
                dict(value=dict(schema="qe.decision/1", answers=[], clearances=[])),
            ),
        ):
            args = dict(
                kind=box.DecisionKind.ANSWER,
                level=3,
                batch=value["batch_id"],
                expected_rev=1,
                value=answer(value),
            )
            args.update(kwargs)
            self.assertRefused(refusal, self.box.decide, **args)
        self.box.handoff("fixture", "jim")
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE,
            self.box.decide,
            box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
        )
        self.assertRefused(
            box.BoxRefusal.WRONG_STATE,
            self.box.card_intent,
            "fixture",
            "jim",
            by="dinesh",
        )
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE,
            self.box.card_posted,
            "fixture",
            "jim",
            card.gen + 1,
            "card",
            by="jim",
        )
        self.box.card_posted("fixture", "jim", card.gen, "card", by="jim")
        for kwargs in (
            dict(card_gen=card.gen, card_ref="wrong"),
            dict(card_gen=card.gen + 1, card_ref="card"),
            dict(card_gen=card.gen, card_ref="card", by="dinesh"),
        ):
            args = dict(by="jim", card_gen=card.gen, card_ref="card")
            args.update(kwargs)
            self.assertRefused(
                box.BoxRefusal.WRONG_STATE
                if args["by"] == "dinesh"
                else box.BoxRefusal.CARD_NOT_ACTIVE,
                self.box.decide,
                box.DecisionKind.ANSWER,
                4,
                value["batch_id"],
                1,
                answer(value),
                **args,
            )
        output = self.box.decide(
            box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
            card_gen=card.gen,
            card_ref="card",
        )
        self.assertEqual(output.plans[0].plan["body"]["floor"], 3)

    def test_text_is_refused_instead_of_cut(self):
        value = entry()
        self.box.register(value)
        self.assertRefused(
            box.BoxRefusal.TEXT_TOO_LONG,
            self.box.decide,
            box.DecisionKind.ANSWER,
            3,
            value["batch_id"],
            1,
            answer(value, text="a" * 501),
        )
        self.assertEqual(self.box.show(value["batch_id"])["decisions"], [])

    def test_local_correction_l3_and_l4(self):
        from test_registration import progress
        from test_corrections import report

        for level in (3, 4):
            with self.subTest(level=level):
                value = entry(floor=1, task_id=str(box.uuid4()))
                self.box.register(value)
                self.box.register(progress(value, local=1, local_level=1))
                self.assertRefused(
                    box.BoxRefusal.SUPERSEDES_MISMATCH,
                    self.box.decide,
                    box.DecisionKind.CORRECTION,
                    level,
                    value["batch_id"],
                    1,
                    answer(value),
                    supersedes=99,
                    by="dinesh" if level == 3 else "jim",
                )
                kwargs = {}
                if level == 4:
                    card = self.box.card_intent(
                        value["task_id"],
                        "jim",
                        purpose=box.CardPurpose.CORRECTION,
                        by="jim",
                    )
                    self.box.card_posted(
                        value["task_id"], "jim", card.gen, "card", by="jim"
                    )
                    kwargs = dict(card_gen=card.gen, card_ref="card")
                output = self.box.decide(
                    box.DecisionKind.CORRECTION,
                    level,
                    value["batch_id"],
                    1,
                    answer(value),
                    supersedes=1,
                    by="dinesh" if level == 3 else "jim",
                    **kwargs,
                )
                self.assertEqual(output.plans[0].plan["body"]["resolution_rev"], 2)
                current = self.box.show(value["batch_id"])["record"]
                self.assertEqual(current.state, box.BoxBatchState.DECIDED)
                self.assertIsNone(current.correction_floor)
                self.assertTrue(current.owned)
                self.box.result(
                    report(output.plans[0], box.m.SendOutcome.SENT_ECHOED, self.now)
                )

    def test_intent_reconcile_cas_cap_and_close(self):
        from datetime import timedelta

        value = entry()
        self.box.register(value)
        self.box.handoff("fixture", "jim")
        card = self.box.card_intent("fixture", "jim", by="jim")
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE,
            self.box.card_intent,
            "fixture",
            "jim",
            reconciles=card.gen,
            by="jim",
        )
        for _ in range(3):
            self.now += timedelta(minutes=10)
            card = self.box.card_intent("fixture", "jim", reconciles=card.gen, by="jim")
        self.now += timedelta(minutes=10)
        self.assertRefused(
            box.BoxRefusal.CARD_CAP_REACHED,
            self.box.card_intent,
            "fixture",
            "jim",
            reconciles=card.gen,
            by="jim",
        )
        self.box.card_posted("fixture", "jim", card.gen, "card", by="jim")
        self.box.card_close("fixture", "jim", card.gen, by="jim")
        self.assertEqual(
            self.box.show(value["batch_id"])["cards"][-1].state, box.CardGenState.CLOSED
        )

    def test_ack_path_expands_home_on_the_mac_and_show_schema_round_trips(self):
        import json

        value = entry()
        self.box.register(value)
        output = self.box.decide(
            box.DecisionKind.ANSWER, 3, value["batch_id"], 1, answer(value)
        )
        self.assertIn(
            'uv run --script "$HOME/.local/share/agent-tooling/rex/question_broker.py" ack',
            output.plans[0].plan["body"]["text"],
        )
        current = self.box.show(value["batch_id"])
        self.assertEqual(
            box.m.parse_record(box.BoxShow, json.loads(box.json_text(current))), current
        )
