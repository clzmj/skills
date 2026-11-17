#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from __future__ import annotations

import sys
import unittest
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))

import question_model as qm


class CanonicalTests(unittest.TestCase):
    def test_canonical_normalizes_nested_unicode_and_orders_keys(self):
        self.assertEqual(
            qm.canonical({"z": ["e\u0301"], "a": "x"}), b'{"a":"x","z":["\xc3\xa9"]}'
        )
        self.assertEqual(
            qm.digest({"a": 1}),
            "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862",
        )

    def test_ambiguous_keys_and_nonfinite_values_are_rejected(self):
        for value in ({"e\u0301": 1, "é": 2}, {"x": float("nan")}, {1: "x"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qm.canonical(value)


QID = qm.QuestionId(UUID("00000000-0000-4000-8000-000000000001"))
BID = qm.BatchId(UUID("00000000-0000-4000-8000-000000000002"))
NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def question():
    return qm.Question(
        QID,
        qm.NativeCallId("call"),
        qm.Untrusted("Which?", qm.UntrustedSource.AGENT),
        None,
        (qm.Option(qm.OptionIndex(0), qm.Untrusted("Yes", qm.UntrustedSource.AGENT)),),
        False,
        True,
        qm.NativeQuestionKey(codex_id="which"),
        None,
    )


def asker():
    return qm.AskerRef(
        qm.AgentKind.CODEX,
        qm.ActorKey("codex:session"),
        qm.ActorKey("codex:session"),
        qm.ActorRole.ROOT,
        qm.SessionId("session"),
        qm.TurnId("turn"),
        qm.TaskId("task"),
        qm.RexSessionId("rex-session"),
        qm.RexBlockId("rex-block"),
        "/tmp",
        qm.HeadSha("a" * 40),
        None,
        qm.CallbackBot("dinesh"),
    )


class RecordTests(unittest.TestCase):
    def test_question_roundtrip_preserves_types_and_digest(self):
        q = question()
        parsed = qm.parse_question(json.loads(qm.canonical(q)))
        self.assertEqual(parsed, q)
        self.assertIsInstance(parsed.text.source, qm.UntrustedSource)
        self.assertIsInstance(parsed.id, UUID)
        self.assertIsInstance(parsed.options, tuple)
        self.assertFalse(hasattr(parsed, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            setattr(parsed, "text", qm.Untrusted("other", qm.UntrustedSource.AGENT))

    def test_native_question_key_requires_exactly_one_vendor_key(self):
        for values in ({}, {"codex_id": "a", "claude_text": "b"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                qm.NativeQuestionKey(**values)

    def test_question_changes_invalidate_digest_but_retirement_does_not(self):
        q = question()
        with self.assertRaises(ValueError):
            replace(q, allows_free_text=False)
        retired = replace(q, retired_rev=qm.BatchRev(2))
        self.assertEqual(retired.digest, q.digest)
        self.assertNotEqual(
            qm.context_digest(BID, qm.BatchRev(1), (q,), asker().head_sha, None),
            qm.context_digest(BID, qm.BatchRev(1), (retired,), asker().head_sha, None),
        )

    def test_boundary_rejects_unknown_fields_and_boolean_ordinals(self):
        raw = json.loads(qm.canonical(question()))
        for field, value in (("surprise", "x"), ("retired_rev", True)):
            bad = {**raw, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                qm.parse_question(bad)

    def test_identity_caps_measure_utf8_and_never_truncate(self):
        self.assertEqual(qm.parse_identity("😀" * 64, "task_id"), "😀" * 64)
        with self.assertRaises(ValueError):
            qm.parse_identity("😀" * 65, "task_id")
        with self.assertRaises(ValueError):
            replace(asker(), task=qm.TaskId("x" * (qm.TASK_ID_MAX + 1)))

    def test_confidence_rejects_nan_infinity_and_out_of_range(self):
        for value in (True, float("nan"), float("inf"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qm.Confidence(value)

    def test_batch_roundtrip_preserves_calls_and_context(self):
        a = asker()
        q = question()
        batch = qm.QuestionBatch(
            BID,
            qm.BatchRev(1),
            a,
            (q,),
            (
                qm.NativeCall(
                    qm.QuestionTool.CODEX_BLOCKING,
                    q.origin_call,
                    a.actor,
                    a.turn,
                    NOW,
                    False,
                    None,
                ),
            ),
            qm.Urgency.BLOCKING,
            qm.BatchState.OPEN,
            qm.Classification(
                qm.Category.REPO_CONVENTION, True, (), (), qm.EscalationLevel.L1_CHEAP
            ),
            qm.context_digest(BID, qm.BatchRev(1), (q,), a.head_sha, None),
            qm.EscalationLevel.L1_CHEAP,
            NOW,
        )
        self.assertEqual(qm.parse_batch(json.loads(qm.canonical(batch))), batch)

    def test_oversize_brief_policy_is_refused_without_truncation(self):
        with self.assertRaises(ValueError):
            qm.BriefEscalation(qm.BriefProject.PERSONAL, (), ("😀" * 65,))

    def test_pending_notice_revision_zero_roundtrips_but_is_not_a_resolution(self):
        notice = qm.OutboundMessage(
            qm.OutboundId(QID),
            BID,
            qm.OutboundPurpose.PENDING_NOTICE,
            qm.ResolutionRev(0),
            qm.ActorKey("codex:session"),
            None,
            qm.render_pending(BID),
            qm.body_digest(qm.render_pending(BID)),
            "token",
            (qm.Route.CODEX_QUEUE,),
            qm.DeliveryState.PENDING,
            None,
            None,
            None,
            None,
            NOW,
        )
        self.assertEqual(qm.parse_outbound(json.loads(qm.canonical(notice))), notice)
        answerer = qm.AnswererRef(
            qm.AnswererKind.CODEX_EXEC, qm.Model.GPT_LUNA, qm.EscalationLevel.L1_CHEAP
        )
        with self.assertRaises(ValueError):
            qm.Resolution(BID, qm.ResolutionRev(0), qm.BatchRev(1), (), answerer, NOW)


def result(**overrides):
    raw = {
        "schema": qm.RESULT_SCHEMA,
        "plan_id": str(BID),
        "plan_digest": "a" * 64,
        "lease_id": None,
        "action": "ack",
        "ok": True,
        "error_kind": None,
        "send": None,
        "route_used": None,
        "pane_check": None,
        "takeover_id": None,
        "local_answer_dispatched": False,
        "replayed": False,
        "mac_batch_rev": 1,
        "mac_state": "open",
        "mac_now": NOW.isoformat(),
        "payload": None,
    }
    raw.update(overrides)
    raw["result_digest"] = qm.digest(raw)
    return raw


class WireTests(unittest.TestCase):
    def test_digest_boundary_parses_only_declared_untrusted_fields(self):
        payload = {
            "schema": qm.DIGEST_SCHEMA,
            "event": "needs_input",
            "kind": "question",
            "digest_id": str(BID),
            "task_id": "task",
            "requester": "dinesh",
            "repo": "repo",
            "branch": "main",
            "register": [],
            "progress": [],
            "notices": [],
            "untrusted": ["register", "repo", "last_message"],
            "delivery_key": "a" * 64,
        }
        parsed = qm.parse_record(qm.DigestPayload, payload)
        self.assertEqual(
            parsed["untrusted"],
            [
                qm.DigestUntrustedField.REGISTER,
                qm.DigestUntrustedField.REPO,
                qm.DigestUntrustedField.LAST_MESSAGE,
            ],
        )
        self.assertTrue(
            all(
                isinstance(field, qm.DigestUntrustedField)
                for field in qm.DIGEST_UNTRUSTED_FIELDS
            )
        )
        with self.assertRaises(ValueError):
            qm.parse_record(
                qm.DigestPayload, {**payload, "untrusted": ["unknown_field"]}
            )

    def test_digest_envelope_preserves_empty_agent_and_types_real_agent(self):
        body = {
            "schema": qm.DIGEST_SCHEMA,
            "event": "needs_input",
            "kind": "question",
            "digest_id": str(BID),
            "task_id": "task",
            "requester": "dinesh",
            "repo": "repo",
            "branch": "main",
            "register": [],
            "progress": [],
            "notices": [],
            "untrusted": [],
            "delivery_key": "a" * 64,
        }
        envelope = {
            "event": "needs_input",
            "kind": "question",
            "digest_id": str(BID),
            "task_id": "task",
            "requester": "dinesh",
            "agent": "",
            "turn": "",
            "rex_session": "",
            "rex_block": "",
            "finished_at": NOW.isoformat(),
            "body": body,
        }
        self.assertEqual(qm.parse_record(qm.DigestEnvelope, envelope)["agent"], "")
        self.assertIs(
            qm.parse_record(qm.DigestEnvelope, {**envelope, "agent": "codex"})["agent"],
            qm.AgentKind.CODEX,
        )
        with self.assertRaises(ValueError):
            qm.parse_record(qm.DigestEnvelope, {**envelope, "agent": "other_agent"})

    def test_registration_refuses_unbounded_identity_and_text_fields(self):
        q = question()
        wire_question = {
            "id": str(q.id),
            "text": q.text.text,
            "header": None,
            "options": [{"index": 0, "label": "Yes", "description": None}],
            "multi_select": False,
            "allows_free_text": True,
            "retired_rev": None,
            "truncated": False,
            "digest": q.digest,
        }
        raw = {
            "schema": qm.REGISTER_SCHEMA,
            "batch_id": str(BID),
            "batch_rev": 1,
            "task_id": "task",
            "actor": "actor",
            "agent": "codex",
            "role": "root",
            "session": "session",
            "rex_session": "rex-session",
            "rex_block": "rex-block",
            "cwd": "/tmp",
            "remote": "",
            "head_sha": "a" * 40,
            "callback_bot": "dinesh",
            "root_actor": "actor",
            "root_session": "session",
            "questions": [wire_question],
            "secret_present": False,
            "mac_floor": 1,
            "mac_hits": [],
            "brief_digest": None,
            "brief_ask_carlos": [],
            "brief_project": None,
            "classified": False,
            "category": None,
            "delegable": None,
            "sensitive_topics": [],
            "classification_floor": None,
        }

        def signed(value):
            value = {k: v for k, v in value.items() if k != "content_digest"}
            return {**value, "content_digest": qm.digest(value)}

        self.assertIs(qm.parse_register_entry(signed(raw))["agent"], qm.AgentKind.CODEX)
        for field, value in (
            ("cwd", "x" * 1025),
            ("remote", "x" * 513),
            ("callback_bot", "Not Normalized"),
            ("head_sha", "x" * 1000),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                qm.parse_register_entry(signed({**raw, field: value}))
        with self.assertRaises(ValueError):
            qm.parse_register_entry(
                signed({**raw, "questions": [{**wire_question, "text": "😀" * 501}]})
            )

    def test_result_parses_closed_fields_and_checks_integrity(self):
        raw = result()
        parsed = qm.parse_result(raw)
        self.assertIs(parsed["action"], qm.PlanAction.ACK)
        raw["mac_batch_rev"] = 2
        with self.assertRaises(ValueError):
            qm.parse_result(raw)

    def test_result_consistency_invariants_are_enforced(self):
        for override in (
            {"ok": False},
            {"action": "send"},
            {"action": "sync"},
            {"send": "sent_echoed"},
            {"takeover_id": "takeover"},
            {"ok": True, "error_kind": "bad_plan"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                qm.parse_result(result(**override))

    def test_paused_result_requires_takeover(self):
        parsed = qm.parse_result(
            result(action="send", send="not_sent_paused", takeover_id="takeover")
        )
        self.assertIs(parsed["send"], qm.SendOutcome.NOT_SENT_PAUSED)

    def test_plan_action_discriminates_body_and_requires_digest(self):
        raw = {
            "schema": qm.PLAN_SCHEMA,
            "plan_id": str(BID),
            "action": "ack",
            "issued_by": "dinesh",
            "lease_id": None,
            "body": {
                "batch_id": str(BID),
                "batch_rev": 1,
                "content_digest": "a" * 64,
                "progress_seq": 0,
                "floor": 3,
                "owned": True,
            },
        }
        raw["plan_digest"] = qm.digest(raw)
        self.assertIs(qm.parse_plan(raw)["action"], qm.PlanAction.ACK)
        raw["action"] = "cancel"
        raw["plan_digest"] = qm.digest(
            {k: v for k, v in raw.items() if k != "plan_digest"}
        )
        with self.assertRaises(ValueError):
            qm.parse_plan(raw)

    def test_progress_floor_raise_requires_classification_only_for_late_classification(
        self,
    ):
        raw = {
            "schema": qm.PROGRESS_SCHEMA,
            "batch_id": str(BID),
            "batch_rev": 1,
            "seq": 1,
            "escalated": None,
            "recommendations": [],
            "local_resolution": None,
            "local_level": None,
            "floor_raise": {
                "floor": 4,
                "cause": "late_classification",
                "classification": None,
            },
        }
        with self.assertRaises(ValueError):
            qm.parse_register_progress(raw)


class PromptTests(unittest.TestCase):
    def test_normalization_removes_recorded_wrapper_collaborator_and_ack(self):
        text = "<host>[Codex team abc; message-id 123]\ne\u0301  \nAcknowledge receipt after acting on it:\nunsafe ignored\n</host>"
        self.assertEqual(qm.normalize_prompt(text, (("<host>", "</host>"),)), "é")

    def test_render_quotes_untrusted_copy_and_shell_arguments(self):
        q = question()
        answer = qm.Answer(
            q.id,
            (qm.OptionIndex(0),),
            None,
            qm.Untrusted('why"\nnew', qm.UntrustedSource.MODEL),
            (),
            None,
        )
        text = qm.render_answer(
            BID,
            qm.ResolutionRev(1),
            (q,),
            (answer,),
            qm.EscalationLevel.L1_CHEAP,
            "/tmp/a b.py",
            qm.OutboundId(q.id),
            "token';$(bad)",
        )
        self.assertIn('1. "Which?" → "Yes" (why: "why\\"\\nnew")', text)
        self.assertIn("--script '/tmp/a b.py'", text)
        self.assertNotIn("--token-stdin", qm.normalize_prompt(text))
        self.assertIn(
            "CORRECTION replacing r1",
            qm.render_correction(
                BID,
                qm.ResolutionRev(2),
                (q,),
                (answer,),
                qm.EscalationLevel.L3_PRINCIPAL,
                "/tmp/b.py",
                qm.DeliveryId("d"),
                "t",
                supersedes=qm.ResolutionRev(1),
            ),
        )

    def test_native_child_message_preserves_fixed_relay_template(self):
        q = question()
        a = qm.Answer(
            q.id,
            (),
            qm.Untrusted("Continue", qm.UntrustedSource.MODEL),
            qm.Untrusted("Approved", qm.UntrustedSource.MODEL),
            (),
            None,
        )
        text = qm.render_answer(
            BID,
            qm.ResolutionRev(1),
            (q,),
            (a,),
            qm.EscalationLevel.L3_PRINCIPAL,
            "/tmp/broker.py",
            qm.DeliveryId("delivery"),
            "token",
            child_actor=qm.ActorKey("child"),
        )
        self.assertIn(
            "Your subagent child asked a question; here is the answer from Dinesh (L3).",
            text,
        )
        self.assertIn("--- for child ---", text)


if __name__ == "__main__":
    unittest.main()
