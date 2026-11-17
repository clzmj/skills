#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Native question adapters preserve each item and mint question identities."""

import unittest

import codex_questions as adapter
import question_model as m


def hook(**changes):
    value = dict(
        hook_event_name="PreToolUse",
        session_id="fixture",
        turn_id="turn",
        tool_name="request_user_input",
        tool_use_id="call-1",
        tool_input={
            "questions": [
                {
                    "id": "choice",
                    "question": "Which direction?",
                    "options": [
                        {"label": "Standard", "description": "Use repo defaults"}
                    ],
                }
            ]
        },
    )
    value.update(changes)
    return m.parse_hook_input(value, m.AgentKind.CODEX)


class CodexQuestions(unittest.TestCase):
    def test_detect_normalize_and_identity(self):
        detected = adapter.detect(hook())
        assert detected is not None
        (first,) = adapter.normalize(detected)
        (again,) = adapter.normalize(detected)
        self.assertNotEqual(first.id, again.id)
        self.assertEqual(first.origin_call, "call-1")
        self.assertEqual(first.native_key.codex_id, "choice")
        self.assertEqual(first.options[0].index, 0)
        assert first.options[0].description is not None
        self.assertEqual(first.options[0].description.text, "Use repo defaults")

    def test_per_item_validation_title_fallback_and_option_indexes(self):
        ask = adapter.detect(
            hook(
                tool_input={
                    "questions": [
                        None,
                        {"question": 7},
                        {"question": "", "title": "Do not fall through"},
                        {
                            "question": 7,
                            "title": "Title fallback",
                            "id": "native",
                            "options": [
                                None,
                                {"label": 9},
                                {"label": "Valid", "description": 9},
                            ],
                        },
                        {"question": "Next?", "header": "Next", "options": "bad"},
                    ]
                }
            )
        )
        assert ask is not None
        questions = adapter.normalize(ask)
        self.assertEqual([q.text.text for q in questions], ["Title fallback", "Next?"])
        self.assertEqual(questions[0].options[0].index, 2)
        self.assertEqual(questions[0].options[0].label.text, "Valid")
        self.assertIsNone(questions[0].options[0].description)
        self.assertEqual(questions[1].options, ())
        assert questions[1].header is not None
        self.assertEqual(questions[1].header.text, "Next")
        self.assertTrue(
            all(q.allows_free_text and not q.multi_select for q in questions)
        )
        self.assertNotEqual(questions[0].id, questions[1].id)

    def test_detect_exact_tools_pre_event_only_and_malformed_questions(self):
        for tool in ("request_user_input", "request_user_input_async"):
            self.assertIsNotNone(adapter.detect(hook(tool_name=tool)))
        self.assertIsNone(adapter.detect(hook(tool_name="shell")))
        self.assertIsNone(adapter.detect(hook(hook_event_name="PostToolUse")))
        ask = adapter.detect(hook(tool_input={"questions": "bad"}))
        assert ask is not None
        self.assertEqual(adapter.normalize(ask), ())

    def test_observe_async_acceptance_and_errors_are_not_native_answers(self):
        post = hook(hook_event_name="PostToolUse")
        self.assertEqual(
            adapter.observe(post, {"answers": {"choice": {"answers": ["Standard"]}}}),
            m.LifecycleKind.NATIVE_ANSWER,
        )
        for response in (
            {"accepted": True},
            {"answers": {"choice": "Standard"}},
            '{"accepted":true}',
        ):
            self.assertIsNone(
                adapter.observe(
                    hook(
                        hook_event_name="PostToolUse",
                        tool_name="request_user_input_async",
                    ),
                    response,
                )
            )
        for response in (
            {"error": "unavailable"},
            {"unavailable": True},
            {"is_error": True},
            '{"error":"unavailable"}',
        ):
            self.assertEqual(
                adapter.observe(post, response), m.LifecycleKind.NATIVE_CALL_FAILED
            )
        for response in ("tool unavailable", "No error, answer available"):
            self.assertIsNone(adapter.observe(post, response))
        self.assertIsNone(adapter.observe(hook(), {"answers": {"choice": "Standard"}}))

    def test_observe_recorded_json_answer(self):
        self.assertEqual(
            adapter.observe(
                hook(hook_event_name="PostToolUse"),
                '{"answers":{"choice":{"answers":["Standard"]}}}',
            ),
            m.LifecycleKind.NATIVE_ANSWER,
        )


if __name__ == "__main__":
    unittest.main()
