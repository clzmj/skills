#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Synthetic Claude hook boundaries; child question support remains P13-unproven."""

import unittest

import claude_questions as adapter
import question_model as m


def hook(**changes):
    value = dict(
        hook_event_name="PreToolUse",
        session_id="fixture",
        turn_id="turn",
        tool_name="AskUserQuestion",
        tool_use_id="same-call",
        tool_input={
            "questions": [
                {
                    "question": "Pick labels?",
                    "header": "Labels",
                    "multiSelect": True,
                    "options": [
                        {"label": "One", "description": "First"},
                        {"label": "Two", "description": "Second"},
                    ],
                }
            ]
        },
    )
    value.update(changes)
    return m.parse_hook_input(value, m.AgentKind.CLAUDE)


class ClaudeQuestions(unittest.TestCase):
    def test_synthetic_pre_and_permission_bind_same_call(self):
        pre = adapter.detect(hook())
        permission = adapter.detect(hook(hook_event_name="PermissionRequest"))
        assert pre is not None and permission is not None
        self.assertEqual(pre.call, permission.call)
        self.assertIsNone(adapter.detect(hook(hook_event_name="PostToolUse")))
        self.assertIsNone(adapter.detect(hook(tool_name="Bash")))

    def test_permission_without_native_call_id_needs_matcher_binding(self):
        self.assertIsNone(
            adapter.detect(
                hook(
                    hook_event_name="PermissionRequest",
                    tool_use_id=None,
                )
            )
        )

    def test_per_item_validation_options_multiselect_and_minted_identity(self):
        ask = adapter.detect(
            hook(
                tool_input={
                    "questions": [
                        None,
                        {"question": 9},
                        {"question": " "},
                        {"question": "Malformed boolean", "multiSelect": "true"},
                        {
                            "question": "Pick?",
                            "multiSelect": True,
                            "options": [
                                None,
                                {"label": 8},
                                {"label": "Two", "description": 8},
                            ],
                        },
                        {"question": "Default?"},
                    ]
                }
            )
        )
        assert ask is not None
        questions = adapter.normalize(ask)
        self.assertEqual([q.text.text for q in questions], ["Pick?", "Default?"])
        self.assertTrue(questions[0].multi_select)
        self.assertFalse(questions[1].multi_select)
        self.assertTrue(all(q.allows_free_text for q in questions))
        self.assertEqual(questions[0].options[0].index, 2)
        self.assertEqual(questions[0].options[0].label.text, "Two")
        self.assertIsNone(questions[0].options[0].description)
        self.assertEqual(questions[0].native_key.claude_text, "Pick?")
        self.assertNotEqual(questions[0].id, adapter.normalize(ask)[0].id)

    def test_observe_native_answer_only_on_matching_result(self):
        post = hook(hook_event_name="PostToolUse")
        self.assertEqual(
            adapter.observe(post, {"answers": {"Pick labels?": "One"}}),
            m.LifecycleKind.NATIVE_ANSWER,
        )
        self.assertIsNone(adapter.observe(hook(), {"answers": {"Pick labels?": "One"}}))
        self.assertIsNone(adapter.observe(post, {"accepted": True}))
        self.assertIsNone(adapter.observe(post, {"answers": {}}))

    def test_observe_recorded_json_answer(self):
        self.assertEqual(
            adapter.observe(
                hook(hook_event_name="PostToolUse"),
                '{"answers":{"Pick labels?":"One"}}',
            ),
            m.LifecycleKind.NATIVE_ANSWER,
        )
