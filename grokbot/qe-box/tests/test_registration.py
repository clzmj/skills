#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Registration is an immutable report and a permanent monotone floor."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qe_box as box

m = box.m


def entry(*, batch=None, rev=1, floor=3, classified=True, **changes):
    question = dict(
        id=str(uuid4()),
        text="Which implementation?",
        header=None,
        options=[dict(index=0, label="Standard", description=None)],
        multi_select=False,
        allows_free_text=True,
        retired_rev=None,
        truncated=False,
        digest="",
    )
    question["digest"] = m.digest(
        {
            k: v
            for k, v in question.items()
            if k not in ("digest", "retired_rev", "truncated")
        }
    )
    value = dict(
        schema="qe.register/1",
        batch_id=batch or str(uuid4()),
        batch_rev=rev,
        task_id="fixture",
        actor="codex:fixture",
        agent="codex",
        role="root",
        session="fixture",
        rex_session="fixture",
        rex_block="fixture",
        cwd="/tmp/qe-fixture",
        remote="",
        head_sha="",
        callback_bot="jim",
        root_actor="codex:fixture",
        root_session="fixture",
        questions=[question],
        secret_present=False,
        mac_floor=floor,
        mac_hits=[],
        brief_digest=None,
        brief_ask_carlos=[],
        brief_project=None,
        classified=classified,
        category="repo_convention" if classified else None,
        delegable=True if classified else None,
        sensitive_topics=[],
        classification_floor=floor if classified else None,
    )
    value.update(changes)
    return m.parse_register_entry({**value, "content_digest": m.digest(value)})


def revision(value, **changes):
    raw = dict(value, batch_rev=value["batch_rev"] + 1)
    raw.update(changes)
    raw.pop("content_digest")
    return m.parse_register_entry({**raw, "content_digest": m.digest(raw)})


def progress(value, seq=1, *, floor=None, local=None, local_level=None, escalated=None):
    return m.parse_register_progress(
        dict(
            schema="qe.progress/1",
            batch_id=value["batch_id"],
            batch_rev=value["batch_rev"],
            seq=seq,
            escalated=escalated,
            recommendations=[],
            local_resolution=local,
            local_level=local_level,
            floor_raise=None
            if floor is None
            else dict(floor=floor, cause="mac_guard", classification=None),
        )
    )


class BoxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.box = box.Box(
            Path(self.temp.name) / "state" / "box.sqlite3",
            bots=(box.Bot("jim", "fixture-agent", "Jim"),),
            clock=lambda: self.now,
        )
        self.addCleanup(self.box.close)

    def assertRefused(self, refusal, operation, *args, **kwargs):
        with self.assertRaises(box.Refused) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.reason, refusal)


class RegistrationTests(BoxTest):
    def test_floor_raise_applies_independently_of_progress_high_water(self):
        value = entry(classified=False)
        self.box.register(value)
        self.box.register(progress(value, 11))
        self.box.register(progress(value, 10, floor=4))
        current = self.box.show(value["batch_id"])
        self.assertEqual(current["record"].floor, 4)
        self.assertEqual(current["record"].progress_seq, 11)
        self.assertEqual(current["record"].state, box.BoxBatchState.AWAITING_L3)
        self.assertIsNone(current["record"].correction_floor)

    def test_idempotence_identity_binding_and_immutable_revision(self):
        value = entry()
        first = self.box.register(value)
        second = self.box.register(value)
        self.assertEqual(first.plans[0].plan["body"], second.plans[0].plan["body"])
        for changes in (
            dict(actor="other"),
            dict(questions=[]),
            dict(callback_bot="other"),
        ):
            with self.subTest(changes=changes):
                changed = revision(value, **changes)
                self.assertRefused(
                    box.BoxRefusal.NON_MONOTONE_REGISTRATION, self.box.register, changed
                )
        changed = dict(value, secret_present=True)
        changed.pop("content_digest")
        changed["content_digest"] = m.digest(changed)
        self.assertRefused(
            box.BoxRefusal.NON_MONOTONE_REGISTRATION, self.box.register, changed
        )

    def test_12c_cumulative_and_non_cumulative_progress_orders(self):
        for cumulative in (False, True):
            for order in ((11, 10), (10, 11)):
                with self.subTest(cumulative=cumulative, order=order):
                    value = entry(classified=False)
                    self.box.register(value)
                    for seq in order:
                        self.box.register(
                            progress(
                                value, seq, floor=4 if seq == 10 or cumulative else None
                            )
                        )
                    self.box.register(progress(value, 12, floor=3))
                    current = self.box.show(value["batch_id"])["record"]
                    self.assertEqual(current.floor, 4)
                    self.assertEqual(current.progress_seq, 12)
                    self.assertIsNone(current.correction_floor)

    def test_guard_runs_on_all_registered_inputs(self):
        for field in (
            "text",
            "header",
            "label",
            "description",
            "brief",
            "cwd",
            "remote",
        ):
            with self.subTest(field=field):
                value = entry(floor=1)
                raw = dict(value)
                question = dict(raw["questions"][0])
                if field in ("text", "header"):
                    question[field] = "production deploy"
                elif field in ("label", "description"):
                    option = dict(question["options"][0])
                    option[field] = "production deploy"
                    question["options"] = [option]
                elif field == "brief":
                    raw["brief_ask_carlos"] = ["production deploy"]
                else:
                    raw[field] = "/production/deploy"
                question["digest"] = m.digest(
                    {
                        k: v
                        for k, v in question.items()
                        if k not in ("digest", "retired_rev", "truncated")
                    }
                )
                raw["questions"] = [question]
                raw.pop("content_digest")
                raw["content_digest"] = m.digest(raw)
                self.box.register(raw)
                current = self.box.show(raw["batch_id"])
                self.assertGreaterEqual(current["record"].floor, 3)
                self.assertTrue(current["revisions"][0].guard_version)

    def test_unknown_progress_and_malformed_digest_are_refused(self):
        value = entry()
        self.assertRefused(
            box.BoxRefusal.UNKNOWN_ENTRY, self.box.register, progress(value)
        )
        self.assertRefused(
            box.BoxRefusal.VALIDATION,
            self.box.register,
            dict(value, content_digest="bad"),
        )
        question = dict(value["questions"][0], digest="bad")
        raw = dict(value, questions=[question])
        raw.pop("content_digest")
        raw["content_digest"] = m.digest(raw)
        self.assertRefused(box.BoxRefusal.VALIDATION, self.box.register, raw)

    def test_purged_record_retains_floor_and_revision_forever(self):
        from datetime import timedelta

        value = entry(floor=4)
        self.box.register(value)
        self.box.cancel(value["batch_id"], from_mac=True)
        self.now += timedelta(days=31)
        self.box.tick()
        self.assertEqual(
            self.box.show(value["batch_id"])["record"].state,
            box.BoxBatchState.TOMBSTONED,
        )
        self.assertRefused(
            box.BoxRefusal.CLOSED_BATCH, self.box.register, revision(value)
        )
        self.now += timedelta(days=366)
        self.box.tick()
        self.assertRefused(
            box.BoxRefusal.UNKNOWN_ENTRY, self.box.show, value["batch_id"]
        )
        self.assertRefused(
            box.BoxRefusal.NON_MONOTONE_REGISTRATION, self.box.register, value
        )
        self.box.register(revision(value, mac_floor=1, classification_floor=1))
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.floor, 4)
        self.assertTrue(current.owned)
        self.assertIsNone(current.correction_floor)

    def test_unknown_callback_blocks_and_retry_requires_registry_fix(self):
        value = entry(callback_bot="missing")
        self.box.register(value)
        current = self.box.show(value["batch_id"])["record"]
        self.assertEqual(current.state, box.BoxBatchState.BLOCKED)
        self.assertEqual(current.blocked_cause, box.BoxBlockCause.UNKNOWN_CALLBACK_BOT)
        self.assertRefused(
            box.BoxRefusal.WRONG_STATE, self.box.retry, value["batch_id"]
        )
        self.box.bots["missing"] = box.Bot("missing", "fixture", "Missing")
        self.box.retry(value["batch_id"])
        self.assertEqual(
            self.box.show(value["batch_id"])["record"].state,
            box.BoxBatchState.AWAITING_L3,
        )

    def test_state_database_and_journal_remain_private_with_umask_zero(self):
        import os

        target = Path(self.temp.name) / "open-umask" / "box.sqlite3"
        prior = os.umask(0)
        try:
            store = box.Box(target, bots=(box.Bot("jim", "fixture", "Jim"),))
            try:
                with store.transaction():
                    store.register(entry())
                    self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        target.with_name(target.name + "-journal").stat().st_mode
                        & 0o777,
                        0o600,
                    )
            finally:
                store.close()
        finally:
            os.umask(prior)

    def test_registration_preserves_explicit_classification_status_at_guard_boundary(
        self,
    ):
        for classified, floor, state in (
            (True, 1, box.BoxBatchState.LOCAL),
            (False, 3, box.BoxBatchState.AWAITING_L3),
        ):
            with self.subTest(classified=classified):
                value = entry(floor=1, classified=classified)
                self.box.register(value)
                current = self.box.show(value["batch_id"])["record"]
                self.assertEqual(current.floor, floor)
                self.assertEqual(current.state, state)
                self.assertIsNone(current.correction_floor)
