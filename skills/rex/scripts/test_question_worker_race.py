#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""A worker must preserve guard hits committed by a concurrent question attach."""

from threading import Event, Thread, current_thread
import unittest
from unittest.mock import patch

import question_broker as broker
import question_guard as guard
import question_ledger as ledger
import question_model as m
import test_question_hooks as hook_fixtures
from test_question_ledger import native_call, question


class QuestionWorkerRace(unittest.TestCase):
    def test_concurrent_attach_preserves_new_revision_guard_hits(self):
        fixture = hook_fixtures.QuestionHooks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.detect()
        (initial,) = fixture.store.batches()
        new_question = question("new-call", "Delete the production database")
        new_classification = guard.derive_floor(
            (new_question,),
            cwd=str(fixture.directory),
            remote="",
            previous_floor=initial.classification.floor,
        )
        self.assertTrue(new_classification.hits)
        self.assertEqual(new_classification.floor, m.EscalationLevel.L4_OWNER)
        call = native_call(
            "new-call", actor=initial.asker.actor, turn=initial.asker.turn
        )
        computed = Event()
        release = Event()
        committed = Event()
        errors: list[Exception] = []
        derive_floor = guard.derive_floor

        def pause_worker(*args, **kwargs):
            classification = derive_floor(*args, **kwargs)
            if current_thread().name == "qe-worker":
                computed.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("Worker was not released")
            return classification

        def worker():
            try:
                with ledger.Ledger(fixture.store.path) as store:
                    broker.Broker(store).worker(str(initial.id))
            except Exception as error:
                errors.append(error)

        def attach():
            try:
                with ledger.Ledger(fixture.store.path) as store:
                    store.open_or_attach(
                        initial.asker, (new_question,), call, new_classification
                    )
                    committed.set()
            except Exception as error:
                errors.append(error)

        worker_thread = Thread(target=worker, name="qe-worker", daemon=True)
        attach_thread = Thread(target=attach, name="qe-attach", daemon=True)
        with patch.object(guard, "derive_floor", side_effect=pause_worker):
            worker_thread.start()
            try:
                self.assertTrue(computed.wait(timeout=5), "Worker did not classify")
                attach_thread.start()
                # Before the fix this commit wins the race and the worker then
                # overwrites its guard hits with the older revision's snapshot.
                committed.wait(timeout=0.2)
            finally:
                release.set()
                worker_thread.join(timeout=5)
                if attach_thread.ident is not None:
                    attach_thread.join(timeout=5)
        self.assertFalse(worker_thread.is_alive())
        self.assertFalse(attach_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(committed.is_set())
        updated = fixture.store.get_batch(str(initial.id))
        self.assertEqual(updated.rev, 2)
        self.assertEqual(updated.classification.floor, m.EscalationLevel.L4_OWNER)
        self.assertTrue(
            set(new_classification.hits).issubset(updated.classification.hits),
            "Worker overwrote guard hits from the attached question",
        )
