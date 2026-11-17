"""Card posting claims and replies use the public Box boundary (§5.6.4)."""

from datetime import timedelta
import json
from typing import cast

from test_registration import BoxTest, box, entry, progress, revision
from test_decisions import answer


class CardTests(BoxTest):
    def awaiting(self, **changes):
        value = entry(**changes)
        self.box.register(value)
        self.box.handoff(value["task_id"], value["callback_bot"])
        return value

    def claim(self, value, **kwargs):
        return self.box.card_intent(
            value["task_id"], value["callback_bot"], by=value["callback_bot"], **kwargs
        )

    def post(self, value, card, ref="fixture-card"):
        return self.box.card_posted(
            value["task_id"],
            value["callback_bot"],
            card.gen,
            ref,
            by=value["callback_bot"],
        )

    def test_intent_is_exclusive_and_post_is_one_time_activation(self):
        value = self.awaiting()
        card = self.claim(value)
        self.assertEqual(card.state, box.CardGenState.INTENDED)
        self.assertEqual(json.loads(card.pairs_json), [[value["batch_id"], 1]])
        for kwargs in ({}, {"gen": 2}, {"reconciles": card.gen}):
            with self.subTest(kwargs=kwargs):
                self.assertRefused(
                    box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value, **kwargs
                )
        active = self.post(value, card)
        self.assertEqual(active.state, box.CardGenState.ACTIVE)
        self.assertEqual(active.ref, "fixture-card")
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.post, value, card)
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value)

    def test_reconcile_after_ten_minutes_is_compare_and_swap(self):
        value = self.awaiting()
        old = self.claim(value)
        self.now += timedelta(minutes=10) - timedelta(microseconds=1)
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value, reconciles=old.gen
        )
        self.now += timedelta(microseconds=1)
        winner = self.claim(value, gen=2, reconciles=old.gen)
        self.assertEqual(winner.reconciles, old.gen)
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value, gen=2, reconciles=old.gen
        )
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.post, value, old)
        self.post(value, winner)
        cards = self.box.show(value["batch_id"])["cards"]
        self.assertEqual(
            [c.state for c in cards],
            [box.CardGenState.ABANDONED, box.CardGenState.ACTIVE],
        )

    def test_recovery_can_record_found_card_without_reposting(self):
        value = self.awaiting()
        card = self.claim(value)
        self.now += timedelta(minutes=11)
        self.post(value, card, "found-in-chat")
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value, reconciles=card.gen
        )
        self.assertEqual(len(self.box.show(value["batch_id"])["cards"]), 1)

    def test_abandonment_cap_does_not_prevent_posting_last_claim(self):
        value = self.awaiting()
        card = self.claim(value)
        for _ in range(3):
            self.now += timedelta(minutes=10)
            card = self.claim(value, reconciles=card.gen)
        self.now += timedelta(minutes=10)
        self.assertRefused(
            box.BoxRefusal.CARD_CAP_REACHED, self.claim, value, reconciles=card.gen
        )
        self.assertEqual(self.post(value, card).state, box.CardGenState.ACTIVE)

    def test_generation_counter_is_scoped_by_task_and_launching_bot(self):
        self.box.bots["pam"] = box.Bot("pam", "pam-agent", "Pam")
        first = self.awaiting()
        second = self.awaiting(callback_bot="pam")
        third = self.awaiting(task_id="other-task")
        for value in (first, second, third):
            with self.subTest(task=value["task_id"], bot=value["callback_bot"]):
                self.assertEqual(self.claim(value).gen, 1)

    def test_correction_and_ask_share_counter_but_do_not_supersede_each_other(self):
        asking = self.awaiting()
        ask = self.post(asking, self.claim(asking))
        local = entry(floor=1)
        self.box.register(local)
        self.box.register(progress(local, local=1, local_level=1))
        correction = self.post(
            local,
            self.claim(local, purpose=box.CardPurpose.CORRECTION),
            "correction-card",
        )
        self.assertEqual(correction.gen, ask.gen + 1)
        cards = self.box.show(asking["batch_id"])["cards"]
        self.assertEqual(
            [c.state for c in cards], [box.CardGenState.ACTIVE, box.CardGenState.ACTIVE]
        )
        self.assertEqual(
            [c.purpose for c in cards],
            [box.CardPurpose.ASK, box.CardPurpose.CORRECTION],
        )

    def test_revision_bumps_fold_into_card_after_fifteen_minutes(self):
        value = self.awaiting()
        old = self.post(value, self.claim(value))
        value = revision(value)
        self.box.register(value)
        self.box.handoff(value["task_id"], value["callback_bot"])
        self.now += timedelta(minutes=15) - timedelta(microseconds=1)
        self.assertRefused(box.BoxRefusal.CARD_NOT_ACTIVE, self.claim, value)
        value = revision(value)
        self.box.register(value)
        self.box.handoff(value["task_id"], value["callback_bot"])
        self.now += timedelta(microseconds=1)
        new = self.post(value, self.claim(value), "latest-card")
        self.assertEqual(new.gen, old.gen + 1)
        self.assertEqual(json.loads(new.pairs_json), [[value["batch_id"], 3]])
        self.assertEqual(
            self.box.show(value["batch_id"])["cards"][0].state,
            box.CardGenState.SUPERSEDED,
        )

    def test_stale_intent_cannot_be_posted_and_bad_refs_do_not_consume_claim(self):
        value = self.awaiting()
        card = self.claim(value)
        for ref in ("", "  ", "x" * 1025):
            self.assertRefused(
                box.BoxRefusal.CARD_NOT_ACTIVE, self.post, value, card, ref
            )
        self.box.register(revision(value))
        self.assertRefused(box.BoxRefusal.STALE_REVISION, self.post, value, card)

    def test_superseded_reply_is_refused_and_active_reply_binds_batch_pair(self):
        value = self.awaiting()
        old = self.post(value, self.claim(value))
        extra = self.awaiting()
        self.now += timedelta(minutes=15)
        new = self.post(value, self.claim(value), "expanded-card")
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE,
            self.box.decide,
            box.DecisionKind.ANSWER,
            4,
            value["batch_id"],
            1,
            answer(value),
            by="jim",
            card_gen=old.gen,
            card_ref=old.ref,
        )
        output = self.box.decide(
            box.DecisionKind.ANSWER,
            4,
            extra["batch_id"],
            1,
            answer(extra),
            by="jim",
            card_gen=new.gen,
            card_ref=new.ref,
        )
        self.assertEqual(
            cast(box.m.SendBody, output.plans[0].plan["body"])["batch_id"],
            extra["batch_id"],
        )

    def test_mac_cancel_requests_closure_and_only_launching_bot_closes(self):
        value = self.awaiting()
        card = self.post(value, self.claim(value))
        self.box.cancel(value["batch_id"], from_mac=True)
        current = self.box.show(value["batch_id"])["cards"][0]
        self.assertTrue(current.close_requested)
        self.assertRefused(
            box.BoxRefusal.WRONG_STATE,
            self.box.card_close,
            "fixture",
            "jim",
            card.gen,
            by="dinesh",
        )
        closed = self.box.card_close("fixture", "jim", card.gen, by="jim")
        self.assertEqual(closed.state, box.CardGenState.CLOSED)
        self.assertFalse(closed.close_requested)
        self.assertRefused(
            box.BoxRefusal.CARD_NOT_ACTIVE,
            self.box.card_close,
            "fixture",
            "jim",
            card.gen,
            by="jim",
        )

    def test_card_posting_refuses_a_delivery_lease_without_consuming_intent(self):
        from test_corrections import report

        value = entry(floor=1)
        self.box.register(value)
        self.box.register(progress(value, local=1, local_level=1))
        card = self.claim(value, purpose=box.CardPurpose.CORRECTION)
        ticket = self.box.decide(
            box.DecisionKind.CORRECTION,
            3,
            value["batch_id"],
            1,
            answer(value),
            supersedes=1,
        ).plans[0]
        self.assertRefused(box.BoxRefusal.LEASED, self.post, value, card)
        self.assertEqual(
            self.box.show(value["batch_id"])["cards"][0].state,
            box.CardGenState.INTENDED,
        )
        self.box.result(report(ticket, box.m.SendOutcome.SENT_ECHOED, self.now))
        self.assertEqual(self.post(value, card).state, box.CardGenState.ACTIVE)
