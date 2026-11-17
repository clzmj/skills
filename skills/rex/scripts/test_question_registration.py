#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Registration caps, cumulative guard raises, and durable digest retries."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
from datetime import timedelta
from unittest.mock import patch

import question_broker as broker
import question_guard as guard
import question_model as m
import test_question_hooks as hook_fixtures


class Registration(hook_fixtures.HookFixture):
    def test_worker_returns_without_waiting_for_digest(self):
        self.detect()
        (batch,) = self.store.batches()
        with patch.object(
            broker.time, "sleep", side_effect=AssertionError("worker must not wait")
        ):
            result = broker.Broker(self.store).worker(str(batch.id))
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertEqual(len(self.store.due_registrations()), 1)

    def test_two_sibling_workers_leave_one_coalesced_digest_to_tick(self):
        self.detect()
        self.detect(turn_id="sibling-turn", tool_use_id="sibling-call")
        worker = broker.Broker(self.store)
        sent = []

        def spool(envelope):
            sent.append(json.loads(m.canonical(envelope)))
            path = self.directory / (
                "grokbot-pending-" + envelope["body"]["delivery_key"]
            )
            path.write_bytes(m.canonical(envelope))
            return path

        with (
            patch.object(broker.time, "sleep"),
            patch.object(broker.callback, "spool", side_effect=spool),
            patch.object(self.store, "session_alive", return_value=True),
        ):
            for batch in self.store.batches():
                worker.worker(str(batch.id))
            self.assertEqual(sent, [], "workers must leave coalescing to tick")
            worker.tick()
            self.assertEqual(sent, [], "tick must honor DIGEST_COALESCE")
            with patch.object(
                worker, "clock", return_value=worker.clock() + timedelta(minutes=1)
            ):
                worker.tick()
                worker.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            {entry["batch_id"] for entry in sent[0]["body"]["register"]},
            {str(batch.id) for batch in self.store.batches()},
        )

    def test_worker_recovery_does_not_duplicate_progress_or_floor_raise(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        initial = self.store.due_progress()
        self.assertEqual(len(initial), 1)
        self.assertIsNone(initial[0]["floor_raise"])
        worker.worker(str(batch.id))
        self.assertEqual(self.store.due_progress(), initial)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM progress").fetchone()[0], 1
        )

    def test_ack_before_crash_recovery_keeps_ack_and_counts_saved_frame_once(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        (entry,) = self.store.due_registrations()
        (progress,) = self.store.due_progress()
        sent = []

        def spool(envelope):
            sent.append(envelope)
            path = self.directory / (
                "grokbot-pending-" + envelope["body"]["delivery_key"]
            )
            path.write_bytes(m.canonical(envelope))
            return path

        with patch.object(broker.callback, "spool", side_effect=spool):
            with (
                patch.object(
                    self.store,
                    "registration_sent",
                    side_effect=OSError("fixture crash"),
                ),
                self.assertRaises(OSError),
            ):
                worker.digest("fixture")
            self.store.ack_registration(
                m.AckBody(
                    batch_id=str(batch.id),
                    batch_rev=batch.rev,
                    content_digest=entry["content_digest"],
                    progress_seq=progress["seq"],
                    floor=m.EscalationLevel.L3_PRINCIPAL,
                    owned=True,
                )
            )
            worker.digest("fixture")
            worker.digest("fixture")
        row = self.store.db.execute("SELECT state,tries FROM registration").fetchone()
        self.assertEqual(row["state"], m.RegistrationState.ACKED)
        self.assertEqual(row["tries"], 1)
        self.assertEqual(len(sent), 1)

    def test_recovery_accounts_a_digest_spooled_before_a_crash_once(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        self.store.notice(m.NoticeKind.L0_VIOLATION, "fixture", "ASSUMPTION: guarded")
        sent = []

        def spool(envelope):
            sent.append(envelope)
            path = self.directory / (
                "grokbot-pending-" + envelope["body"]["delivery_key"]
            )
            path.write_bytes(m.canonical(envelope))
            return path

        with patch.object(broker.callback, "spool", side_effect=spool):
            with (
                patch.object(
                    self.store,
                    "registration_sent",
                    side_effect=OSError("fixture crash"),
                ),
                self.assertRaises(OSError),
            ):
                worker.digest("fixture")
            worker.digest("fixture")
            worker.digest("fixture")
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM digest").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.db.execute("SELECT tries FROM registration").fetchone()[0], 1
        )
        self.assertEqual(
            self.store.db.execute("SELECT spooled FROM notice").fetchone()[0], 1
        )

    def test_notice_only_digest_retains_actual_source_metadata(self):
        self.store.notice(
            m.NoticeKind.L0_VIOLATION,
            "no-batch",
            "ASSUMPTION: guarded operation",
            metadata=m.DigestMetadata(
                requester="jim", repo="fixture-repo", branch="fixture-branch"
            ),
        )
        with patch.object(
            broker.callback, "spool", return_value=self.directory / "fixture-spool"
        ) as spool:
            broker.Broker(self.store).digest("no-batch")
        body = spool.call_args.args[0]["body"]
        self.assertEqual(
            (body["requester"], body["repo"], body["branch"]),
            ("jim", "fixture-repo", "fixture-branch"),
        )

    def test_tick_ignores_fully_acked_historical_task(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        (entry,) = self.store.due_registrations()
        (progress,) = self.store.due_progress()
        self.store.ack_registration(
            m.AckBody(
                batch_id=str(batch.id),
                batch_rev=batch.rev,
                content_digest=entry["content_digest"],
                progress_seq=progress["seq"],
                floor=m.EscalationLevel.L3_PRINCIPAL,
                owned=True,
            )
        )
        self.store.transition(str(batch.id), m.BatchState.STALE)
        # PR-4 dispatch can emit notices; this fixture models fully drained history.
        self.store.db.execute("UPDATE notice SET spooled=1")
        with (
            patch.object(
                worker, "clock", return_value=worker.clock() + timedelta(minutes=1)
            ),
            patch.object(worker, "digest") as digest,
        ):
            worker.tick()
        digest.assert_not_called()

    def test_sibling_workers_claim_one_task_digest(self):
        self.detect()
        (batch,) = self.store.batches()
        broker.Broker(self.store).worker(str(batch.id))
        barrier, sent = Barrier(2), []

        def spool(envelope):
            try:
                barrier.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            sent.append(envelope)
            path = self.directory / (
                "grokbot-pending-" + envelope["body"]["delivery_key"]
            )
            path.write_bytes(m.canonical(envelope))
            return path

        def run(_):
            with __import__("question_ledger").Ledger(self.store.path) as store:
                return broker.Broker(store).digest("fixture")

        with (
            patch.object(broker.callback, "spool", side_effect=spool),
            ThreadPoolExecutor(2) as pool,
        ):
            list(pool.map(run, range(2)))
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            self.store.db.execute("SELECT tries FROM registration").fetchone()[0], 1
        )

    def test_multi_digest_never_sends_progress_before_its_entry(self):
        label = '😀"\\' * 500
        questions = [
            {
                "id": str(i),
                "question": label,
                "options": [{"label": label, "description": label} for _ in range(8)],
            }
            for i in range(8)
        ]
        for index in range(3):
            self.detect(
                turn_id=f"turn-{index}",
                tool_use_id=f"call-{index}",
                tool_input={"questions": questions},
            )
        worker = broker.Broker(self.store)
        for batch in self.store.batches():
            worker.worker(str(batch.id))
        sent = []
        with patch.object(
            broker.callback,
            "spool",
            side_effect=lambda envelope: sent.append(json.loads(m.canonical(envelope))),
        ):
            worker.digest("fixture")
        self.assertGreater(len(sent), 1)
        for envelope in sent:
            self.assertLessEqual(len(m.canonical(envelope)), broker.DIGEST_MAX)
            body = envelope["body"]
            seen = {
                (entry["batch_id"], entry["batch_rev"]) for entry in body["register"]
            }
            self.assertTrue(
                all(
                    (item["batch_id"], item["batch_rev"]) in seen
                    for item in body["progress"]
                )
            )

    def test_tick_spools_l0_notice_without_a_question_batch(self):
        self.store.notice(
            m.NoticeKind.L0_VIOLATION, "no-batch", "ASSUMPTION: guarded operation"
        )
        worker = broker.Broker(
            self.store,
            clock=lambda: __import__("question_ledger").now() + timedelta(minutes=1),
        )
        with patch.object(
            broker.callback, "spool", return_value=self.directory / "fixture-spool"
        ) as spool:
            worker.tick()
        self.assertEqual(spool.call_count, 1)
        self.assertEqual(
            spool.call_args.args[0]["body"]["notices"][0]["kind"],
            m.NoticeKind.L0_VIOLATION,
        )
        self.assertEqual(spool.call_args.args[0]["body"]["register"], [])

    def test_maximal_wire_is_bounded_and_redacted_without_cutting_identity(self):
        label = '😀"\\\x01' * 500
        questions = [
            {
                "id": str(index),
                "question": label,
                "header": label,
                "options": [{"label": label, "description": label} for _ in range(8)],
            }
            for index in range(8)
        ]
        self.detect(tool_input={"questions": questions})
        (batch,) = self.store.batches()
        (entry,) = self.store.due_registrations()
        self.assertLessEqual(len(m.canonical(entry)), m.REGISTER_ENTRY_MAX)
        self.assertEqual(entry["batch_id"], str(batch.id))
        self.assertTrue(all(q["truncated"] for q in entry["questions"]))
        self.assertNotIn("\x01", entry["questions"][0]["text"])

    def test_count_overflow_is_blocked_not_registered(self):
        self.detect(
            tool_input={
                "questions": [{"id": str(i), "question": "Choose"} for i in range(9)]
            }
        )
        (batch,) = self.store.batches()
        self.assertEqual(batch.state, m.BatchState.BLOCKED)
        self.assertEqual(self.store.due_registrations(), ())
        self.assertEqual(
            self.store.db.execute("SELECT kind FROM notice").fetchone()[0],
            m.NoticeKind.TOO_LARGE,
        )

    def test_attaching_past_cap_blocks_previously_box_owned_batch(self):
        self.detect(
            tool_input={
                "questions": [{"id": str(i), "question": "Choose"} for i in range(8)]
            }
        )
        (batch,) = self.store.batches()
        broker.Broker(self.store).worker(str(batch.id))
        self.detect(tool_use_id="overflow")
        self.assertEqual(
            self.store.get_batch(str(batch.id)).state, m.BatchState.BLOCKED
        )
        self.assertEqual(len(self.store.due_registrations()), 1)
        with self.assertRaises(__import__("question_ledger").IllegalTransition):
            __import__("question_ledger").check_transition(
                m.BatchState.BOX_OWNED, m.BatchState.BLOCKED
            )
        for _ in range(2):
            broker.Broker(self.store).worker(str(batch.id))
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM notice WHERE kind=?", (m.NoticeKind.TOO_LARGE,)
            ).fetchone()[0],
            1,
        )

    def test_oversized_brief_is_invalid_not_truncated(self):
        path = self.directory / "brief.md"
        path.write_text(
            '```toml qe-escalation\nschema="qe.brief/1"\nproject="personal"\nl0_assume=[]\nask_carlos=["'
            + "😀" * 100
            + '"]\n```'
        )
        with patch.dict(__import__("os").environ, {m.BRIEF_ENV: str(path)}):
            self.detect()
        (entry,) = self.store.due_registrations()
        self.assertEqual(entry["brief_ask_carlos"], [])
        self.assertEqual(entry["mac_floor"], m.EscalationLevel.L4_OWNER)
        self.assertEqual(
            self.store.db.execute("SELECT kind FROM notice").fetchone()[0],
            m.NoticeKind.BRIEF_INVALID,
        )

    def test_same_revision_new_guard_hit_raises_cumulatively(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        rules = guard.load_guard() + (
            m.GuardRule(
                m.RuleId("fixture-new-rule"),
                m.GuardTrigger.POLICY_UNAVAILABLE,
                m.GuardStrength.STRONG,
                __import__("re").compile("indentation"),
                (),
                (),
            ),
        )
        with patch.object(guard, "load_guard", return_value=rules):
            worker.worker(str(batch.id))
        (latest,) = self.store.due_progress()
        assert latest["floor_raise"] is not None
        self.assertEqual(latest["floor_raise"]["floor"], m.EscalationLevel.L4_OWNER)
        self.assertEqual(latest["floor_raise"]["cause"], m.FloorRaiseCause.MAC_GUARD)
        later = self.store.progress(
            {**latest, "seq": latest["seq"] + 1, "floor_raise": None}
        )
        self.assertEqual(later["floor_raise"], latest["floor_raise"])
        self.assertEqual(self.store.get_batch(str(batch.id)).rev, batch.rev)

    def test_digest_retry_key_body_and_recovery_are_stable(self):
        self.detect()
        (batch,) = self.store.batches()
        worker = broker.Broker(self.store)
        worker.worker(str(batch.id))
        sent = []

        def spool(envelope):
            sent.append(json.loads(m.canonical(envelope)))
            path = self.directory / (
                "grokbot-pending-" + envelope["body"]["delivery_key"]
            )
            path.write_bytes(m.canonical(envelope))
            return path

        with (
            patch.object(broker.callback, "spool", side_effect=spool),
            patch.object(
                broker.callback,
                "git_metadata",
                return_value={
                    "repo": "fixture-repo",
                    "branch": "first-branch",
                    "head_sha": "",
                },
            ) as git,
        ):
            paths = worker.digest("fixture")
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(worker.digest("fixture")), 0)
            paths[0].unlink()
            git.return_value = {
                "repo": "changed-repo",
                "branch": "second-branch",
                "head_sha": "",
            }
            worker.digest("fixture")
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[1]["body"]["branch"], "first-branch")
        self.assertEqual(
            sent[0]["body"]["delivery_key"],
            m.digest([m.DIGEST_SCHEMA, sent[0]["digest_id"]]),
        )
        self.assertEqual(
            self.store.db.execute("SELECT tries FROM registration").fetchone()[0], 1
        )
        self.assertFalse(
            worker.registration_retry_due(self.store.due_registrations()[0])
        )
        with patch.object(
            worker,
            "clock",
            return_value=worker.clock() + m.REGISTER_RETRY + timedelta(seconds=1),
        ):
            self.assertTrue(
                worker.registration_retry_due(self.store.due_registrations()[0])
            )
