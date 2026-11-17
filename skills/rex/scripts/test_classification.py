"""Classification admission through public hook, guard and worker boundaries."""

import question_guard as guard
import question_model as m
from test_question_hooks import HookFixture


class ClassificationAdmission(HookFixture):
    def test_low_guard_waits_for_classifier_without_faking_local_eligibility(self):
        self.detect()
        (batch,) = self.store.batches()
        self.assertFalse(batch.classification.classified)
        self.assertEqual(batch.classification.floor, m.EscalationLevel.L3_PRINCIPAL)
        self.assertEqual(self.store.due_registrations(), ())
        assessed = guard.assess_guard(batch.questions, cwd=batch.asker.cwd)
        self.assertFalse(assessed.classified)
        self.assertEqual(assessed.floor, m.EscalationLevel.L1_CHEAP)

    def test_deterministic_guard_registers_unclassified_immediately(self):
        self.detect(
            tool_input={"questions": [{"id": "one", "question": "Push to production?"}]}
        )
        (entry,) = self.store.due_registrations()
        self.assertFalse(entry["classified"])
        self.assertEqual(entry["mac_floor"], m.EscalationLevel.L4_OWNER)


class WorkerLadder(ClassificationAdmission):
    def setUp(self):
        super().setUp()
        import sys
        from datetime import timedelta
        from decimal import Decimal
        import question_answerers as a
        import question_broker as broker
        import question_routes as routes

        self.data = self.directory / "answer.json"
        self.log = self.directory / "models.log"
        self.script = self.directory / "answerer-stub"
        self.script.write_text(
            "#!" + sys.executable + "\n"
            "import json,sys\n"
            "from pathlib import Path\n"
            f"raw=json.loads(Path({str(self.data)!r}).read_text())\n"
            f"with Path({str(self.log)!r}).open('a') as f: f.write(sys.argv[sys.argv.index('--model')+1]+'\\n')\n"
            "if isinstance(raw,list): raw=raw[0 if sys.argv[sys.argv.index('--model')+1]=='haiku' else 1]\n"
            "print(json.dumps({'subtype':'success','structured_output':raw,'usage':{'input_tokens':1,'output_tokens':1},'total_cost_usd':0.001,'modelUsage':{'claude-'+sys.argv[sys.argv.index('--model')+1]+'-5-5':{}}}))\n"
        )
        self.script.chmod(0o700)
        bounds = m.InvocationBounds(
            24576, 2, None, timedelta(seconds=2), 100, Decimal(".01")
        )
        self.adapter = a.Answerers(
            self.store,
            configs={
                model: a.ModelConfig(model, True, True, bounds, True, True)
                for model in (m.Model.HAIKU, m.Model.SONNET)
            },
            slots_dir=self.directory / "slots",
            executable=str(self.script),
        )

        class Runner(routes.RouteRunner):
            def send(self, route, body, fence):
                failure = fence()
                return routes.RouteResult(
                    routes.RouteOutcome.FENCED
                    if failure
                    else routes.RouteOutcome.ACCEPTED,
                    failure,
                    route,
                )

        self.worker = broker.Broker(self.store, answerer=self.adapter, runner=Runner())
        (self.directory / "README.md").write_text("Use tabs.\n")

    def payload(self, **changes):
        from test_question_answerers import output

        result = output()
        result.update(changes)
        return result

    def run_worker(self, payload):
        import json

        self.data.write_text(json.dumps(payload))
        self.detect()
        (batch,) = self.store.batches()
        return self.worker.worker(str(batch.id), spool_digest=False)

    def test_validated_l1_can_resolve_before_box_ack(self):
        batch = self.run_worker(self.payload())
        self.assertEqual(batch.state, m.BatchState.RESOLVED_LOCAL)
        self.assertTrue(batch.classification.classified)
        self.assertEqual(batch.classification.floor, m.EscalationLevel.L1_CHEAP)
        (entry,) = self.store.due_registrations()
        self.assertTrue(entry["classified"])
        self.assertEqual(entry["mac_floor"], 1)
        self.assertEqual(self.log.read_text().splitlines(), ["haiku"])
        (progress,) = self.store.due_progress()
        self.assertEqual(progress["local_resolution"], 1)

    def test_low_confidence_l1_uses_stronger_other_vendor(self):
        first = self.payload()
        first["answers"][0]["confidence"] = 0.8
        batch = self.run_worker([first, self.payload()])
        self.assertEqual(batch.state, m.BatchState.RESOLVED_LOCAL)
        self.assertEqual(self.log.read_text().splitlines(), ["haiku", "sonnet"])
        (progress,) = self.store.due_progress()
        self.assertEqual(progress["local_level"], 2)

    def test_invalid_evidence_rejects_both_levels(self):
        raw = self.payload()
        raw["answers"][0]["evidence"][0]["quote"] = "Absent quote."
        batch = self.run_worker(raw)
        self.assertEqual(batch.state, m.BatchState.BOX_OWNED)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM resolution").fetchone()[0], 0
        )

    def test_rejected_answers_are_recorded_as_rejected_attempts(self):
        raw = self.payload()
        raw["answers"][0]["evidence"][0]["quote"] = "Absent quote."
        self.run_worker(raw)
        outcomes = [
            row[0]
            for row in self.store.db.execute(
                "SELECT outcome FROM attempt ORDER BY level"
            )
        ]
        self.assertEqual(
            outcomes, [m.AttemptOutcome.REJECTED, m.AttemptOutcome.REJECTED]
        )

    def test_classify_wait_commits_l3_and_late_low_result_cannot_be_local(self):
        import json
        from datetime import timedelta

        self.data.write_text(json.dumps(self.payload()))
        self.detect()
        (batch,) = self.store.batches()
        self.worker.clock = lambda: (
            batch.opened_at + m.CLASSIFY_WAIT + timedelta(seconds=1)
        )
        result = self.worker.worker(str(batch.id), spool_digest=False)
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertGreaterEqual(result.classification.floor, 3)
        (entry,) = self.store.due_registrations()
        self.assertFalse(entry["classified"])
        (progress,) = self.store.due_progress()
        self.assertEqual(
            progress["floor_raise"]["cause"], m.FloorRaiseCause.LATE_CLASSIFICATION
        )
        self.assertEqual(
            progress["floor_raise"]["classification"]["category"],
            m.Category.TRIVIAL_FACTUAL,
        )
        self.worker.ladder_progress(result)
        (later,) = self.store.due_progress()
        self.assertEqual(later["floor_raise"], progress["floor_raise"])

    def test_budget_exhaustion_calls_no_model_and_keeps_registration(self):
        for _ in range(20):
            reservation = self.store.reserve_budget(
                "fixture", model=m.Model.HAIKU, tokens=1, usd="0"
            )
            self.store.settle_budget(reservation, usage=None)
        result = self.run_worker(self.payload())
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertFalse(self.log.exists())
        self.assertTrue(self.store.due_registrations())
        self.assertEqual(
            self.store.db.execute(
                "SELECT kind FROM notice WHERE kind=?", (m.NoticeKind.BUDGET_EXHAUSTED,)
            ).fetchone()[0],
            m.NoticeKind.BUDGET_EXHAUSTED,
        )

    def test_higher_l2_category_reaches_box_as_cumulative_floor_raise(self):
        first = self.payload()
        first["answers"][0]["confidence"] = 0.8
        result = self.run_worker([first, self.payload(category="product_scope")])
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        (entry,) = self.store.due_registrations()
        self.assertEqual(entry["mac_floor"], 1)
        (progress,) = self.store.due_progress()
        self.assertEqual(progress["floor_raise"]["floor"], 4)

    def test_completed_l1_does_not_time_out_while_l2_runs(self):
        import json
        from datetime import timedelta

        first = self.payload()
        first["answers"][0]["confidence"] = 0.8
        self.data.write_text(json.dumps([first, self.payload()]))
        self.detect()
        (batch,) = self.store.batches()
        real_invoke = self.adapter.invoke

        def invoke(batch, level, prompt, **kwargs):
            if level == 2:
                self.worker.clock = lambda: (
                    batch.opened_at + m.CLASSIFY_WAIT + timedelta(seconds=1)
                )
            return real_invoke(batch, level, prompt, **kwargs)

        self.adapter.invoke = invoke
        result = self.worker.worker(str(batch.id), spool_digest=False)
        self.assertEqual(result.state, m.BatchState.RESOLVED_LOCAL)

    def test_local_design_answer_includes_its_revert(self):
        raw = self.payload(
            category="design_choice", revert="Restore the previous helper."
        )
        result = self.run_worker(raw)
        self.assertEqual(result.state, m.BatchState.RESOLVED_LOCAL)
        answers = [
            o
            for o in self.store.outbounds(str(result.id))
            if o.purpose is m.OutboundPurpose.ANSWER
        ]
        self.assertIn("Restore the previous helper.", answers[0].text)

    def test_design_crossing_boundary_is_not_local(self):
        result = self.run_worker(
            self.payload(
                category="design_choice",
                revert="Restore the previous helper.",
                crosses_boundary=True,
            )
        )
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM resolution").fetchone()[0], 0
        )

    def test_worker_recovery_never_replays_a_model_after_lost_completion(self):
        import json
        from unittest.mock import patch

        self.data.write_text(json.dumps(self.payload()))
        self.detect()
        (batch,) = self.store.batches()
        with patch.object(
            self.store, "record_attempt", side_effect=RuntimeError("fixture crash")
        ):
            with self.assertRaises(RuntimeError):
                self.worker.worker(str(batch.id), spool_digest=False)
        self.assertEqual(self.log.read_text().splitlines(), ["haiku"])
        result = self.worker.worker(str(batch.id), spool_digest=False)
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertEqual(self.log.read_text().splitlines(), ["haiku"])

    def test_box_raise_after_stubbed_local_delivery_requires_correction(self):
        from test_question_fixture_replay import box

        result = self.run_worker(self.payload())
        (entry,) = self.store.due_registrations()
        (local,) = self.store.due_progress()
        from contextlib import closing

        with closing(
            box.Box(
                self.directory / "box.sqlite3",
                bots=(box.Bot("jim", "fixture-agent", "Jim"),),
            )
        ) as remote:
            remote.register(entry)
            remote.register(local)
            self.assertEqual(
                remote.show(str(result.id))["record"].state, box.BoxBatchState.LOCAL
            )
            import re
            from unittest.mock import patch

            rules = guard.load_guard() + (
                m.GuardRule(
                    m.RuleId("fixture-after-delivery"),
                    m.GuardTrigger.POLICY_UNAVAILABLE,
                    m.GuardStrength.STRONG,
                    re.compile("indentation"),
                    (),
                    (),
                ),
            )
            with patch.object(guard, "load_guard", return_value=rules):
                raised_batch = self.worker.worker(str(result.id), spool_digest=False)
            self.assertEqual(raised_batch.state, m.BatchState.BOX_OWNED)
            self.assertEqual(self.log.read_text().splitlines(), ["haiku"])
            (raised,) = self.store.due_progress()
            # Every snapshot must retain the already delivered local resolution.
            self.assertEqual(raised["local_resolution"], 1)
            remote.register(raised)
            shown = remote.show(str(result.id))
            self.assertEqual(shown["record"].state, box.BoxBatchState.CORRECTION_DUE)
            self.assertEqual(shown["record"].correction_floor, 4)
            self.assertIn(
                box.BoxReport.FLOOR_DISAGREEMENT, [r.kind for r in shown["reports"]]
            )

    def stale_classification_case(
        self, register_old=False, register_current=False, retire_old=False
    ):
        import json
        import question_broker as broker

        self.data.write_text(json.dumps(self.payload(category="product_scope")))
        self.detect()
        (first,) = self.store.batches()
        invoke = self.adapter.invoke

        def racing(batch, level, prompt, **kwargs):
            result = invoke(batch, level, prompt, **kwargs)
            if register_old:
                broker.prepare_registration(
                    self.store, self.store.get_batch(str(batch.id))
                )
            if retire_old:
                self.store.mark_failed(str(batch.calls[0].call))
            self.detect(
                tool_use_id="attached-call",
                tool_name="request_user_input_async"
                if retire_old
                else "request_user_input",
            )
            if register_current:
                broker.prepare_registration(
                    self.store, self.store.get_batch(str(batch.id))
                )
            return result

        self.adapter.invoke = racing
        result = self.worker.worker(str(first.id), spool_digest=False)
        self.assertEqual(result.rev, 2)
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        self.assertEqual(result.classification.floor, 4)
        self.assertFalse(result.classification.classified)
        self.assertEqual(
            self.store.db.execute("SELECT outcome FROM attempt").fetchone()[0],
            m.AttemptOutcome.STALE_REVISION,
        )
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM resolution").fetchone()[0], 0
        )
        entries = [
            entry for entry in self.store.due_registrations() if entry["batch_rev"] == 2
        ]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["classified"])
        (progress,) = [p for p in self.store.due_progress() if p["batch_rev"] == 2]
        self.assertEqual(progress["floor_raise"]["floor"], 4)
        self.assertEqual(self.log.read_text().splitlines(), ["haiku"])
        provenance = [
            event.detail
            for event in self.store.audit_events(result.asker.task)
            if "source revision r1" in event.detail
        ]
        self.assertEqual(len(provenance), 1)
        self.assertIn("current revision r2", provenance[0])

    def test_stale_classification_raises_retained_questions_without_prior_registration(
        self,
    ):
        self.stale_classification_case()

    def test_stale_classification_raises_already_registered_new_revision(self):
        self.stale_classification_case(register_old=True, register_current=True)

    def test_stale_classification_floor_survives_retired_original_question(self):
        self.stale_classification_case(retire_old=True)
        (batch,) = self.store.batches()
        self.assertEqual(sum(q.retired_rev is not None for q in batch.questions), 1)

    def test_local_dispatch_precedes_digest_coalescing(self):
        import json
        import question_broker as broker
        from unittest.mock import patch

        self.data.write_text(json.dumps(self.payload()))
        self.detect()
        (batch,) = self.store.batches()
        sleep = broker.time.sleep
        coalesced = []

        def wait(seconds):
            if seconds == broker.DIGEST_COALESCE.total_seconds():
                answers = [
                    o
                    for o in self.store.outbounds(str(batch.id))
                    if o.purpose is m.OutboundPurpose.ANSWER
                ]
                self.assertEqual(
                    [o.state for o in answers], [m.DeliveryState.DISPATCHED]
                )
                coalesced.append(True)
            else:
                sleep(seconds)

        with (
            patch.object(broker.time, "sleep", side_effect=wait),
            patch.object(self.worker, "digest", return_value=[]) as digest,
        ):
            result = self.worker.worker(str(batch.id), spool_digest=True)
        self.assertEqual(result.state, m.BatchState.RESOLVED_LOCAL)
        self.assertEqual(coalesced, [True])
        digest.assert_called_once_with(batch.asker.task)

    def test_bounded_l2_recommendation_survives_escalation_and_later_progress(self):
        first, second = self.payload(), self.payload()
        first["answers"][0]["confidence"] = 0.8
        second["answers"][0]["confidence"] = 0.7
        second["answers"][0]["rationale"] = "Use the existing convention. " * 200
        result = self.run_worker([first, second])
        self.assertEqual(result.state, m.BatchState.BOX_OWNED)
        (progress,) = self.store.due_progress()
        self.assertEqual(len(progress["recommendations"]), 1)
        recommendation = progress["recommendations"][0]
        self.assertEqual(recommendation["model"], m.Model.SONNET)
        self.assertEqual(recommendation["level"], m.EscalationLevel.L2_NORMAL)
        self.assertEqual(recommendation["choices"], [[0]])
        self.assertEqual(recommendation["confidence"], 0.7)
        self.assertIn("Use the existing convention.", recommendation["rationale"])
        self.assertLessEqual(
            len(recommendation["rationale"].encode(m.UTF8)), m.QUESTION_TEXT_MAX
        )
        self.worker.ladder_progress(result)
        (later,) = self.store.due_progress()
        self.assertEqual(later["recommendations"], progress["recommendations"])

        quarantined = guard.derive_floor(
            result.questions,
            classification=result.classification,
            previous_floor=result.classification.floor,
            secret_present=True,
        )
        self.store.apply_classification(
            str(result.id), result.rev, result.context, quarantined
        )
        self.worker.ladder_progress(self.store.get_batch(str(result.id)))
        (quarantine_progress,) = self.store.due_progress()
        self.assertEqual(quarantine_progress["recommendations"], [])

    def test_reversed_l2_answers_keep_canonical_choices_and_bounded_wire_frame(self):
        import copy
        import json
        from uuid import uuid4
        import question_broker as broker

        first = self.payload()
        second_answer = copy.deepcopy(first["answers"][0])
        second_answer.update(
            question=2, choice=[1], rationale="Second convention advice. " * 300
        )
        first["answers"][0]["rationale"] = "First convention advice. " * 300
        first["answers"] = [second_answer, first["answers"][0]]
        second = copy.deepcopy(first)
        for answer in first["answers"]:
            answer["confidence"] = 0.8
        for answer in second["answers"]:
            answer["confidence"] = 0.7
        self.data.write_text(json.dumps([first, second]))
        self.detect(
            tool_input={
                "questions": [
                    {
                        "id": str(i),
                        "question": "Which indentation?",
                        "options": [{"label": "Tabs"}, {"label": "Spaces"}],
                    }
                    for i in (1, 2)
                ]
            }
        )
        (batch,) = self.store.batches()
        result = self.worker.worker(str(batch.id), spool_digest=False)
        (entry,) = self.store.due_registrations()
        (progress,) = self.store.due_progress()
        (advice,) = progress["recommendations"]
        self.assertEqual(advice["choices"], [[0], [1]])
        self.assertIn("First convention advice.", advice["rationale"])
        self.assertIn("Second convention advice.", advice["rationale"])
        envelope = self.worker.digest_envelope(
            str(uuid4()),
            result.asker.task,
            [entry],
            [progress],
            [],
            self.worker.clock().isoformat(),
        )
        self.assertLessEqual(len(m.canonical(envelope)), broker.DIGEST_MAX)
        self.detect(tool_use_id="later-revision")
        current = self.store.get_batch(str(batch.id))
        self.worker.ladder_progress(current)
        latest = [p for p in self.store.due_progress() if p["batch_rev"] == current.rev]
        self.assertEqual(latest[0]["recommendations"], [])
