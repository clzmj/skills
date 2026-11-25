#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Claude question call binding and per-item normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from uuid import uuid4

import codex_questions as native
import question_model as m


def detect(input: m.HookInput) -> m.DetectedAsk | None:
    if input.tool != m.QuestionTool.CLAUDE_ASK or input.event not in (
        m.HookEvent.PRE_TOOL_USE,
        m.HookEvent.PERMISSION_REQUEST,
    ):
        return None
    # A PermissionRequest without the native call id must be attached by
    # bind_call(); its content is never minted into a new call identity.
    if input.event is m.HookEvent.PERMISSION_REQUEST and not input.tool_use_id:
        return None
    return m.DetectedAsk(input, m.QuestionTool.CLAUDE_ASK, native.native_call(input))


def normalize(ask: m.DetectedAsk) -> tuple[m.Question, ...]:
    raw = ask.input.tool_input.get(m.ClaudeQuestionField.QUESTIONS)
    if not isinstance(raw, list):
        return ()
    result = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        question = native.text(item.get(m.ClaudeQuestionField.QUESTION))
        if question is None or not question.text.strip():
            continue
        multi = item.get(m.ClaudeQuestionField.MULTI_SELECT, False)
        if not isinstance(multi, bool):
            continue
        result.append(
            m.Question(
                m.QuestionId(uuid4()),
                ask.call,
                question,
                native.text(item.get(m.ClaudeQuestionField.HEADER)),
                native.options(item.get(m.ClaudeQuestionField.OPTIONS)),
                multi,
                True,
                m.NativeQuestionKey(claude_text=question.text),
                None,
            )
        )
    return tuple(result)


def _semantic_question(question: m.Question) -> tuple[object, ...]:
    return (
        question.text.text,
        question.header.text if question.header else None,
        tuple(
            (
                int(option.index),
                option.label.text,
                option.description.text if option.description else None,
            )
            for option in question.options
        ),
        question.multi_select,
        question.allows_free_text,
    )


def bind_call(
    input: m.HookInput, existing_questions: Sequence[m.Question]
) -> m.NativeCallId | None:
    """Find one same-turn caller by its complete normalized question content.

    The caller supplies questions only from the current actor/turn's open
    batch. Permission and post hooks without a NativeCallId attach only when
    their complete semantic question tuple identifies exactly one prior call.
    Content never becomes a new identity and an ambiguous match fails closed.
    """
    if (
        input.event not in (m.HookEvent.PERMISSION_REQUEST, m.HookEvent.POST_TOOL_USE)
        or input.tool != m.QuestionTool.CLAUDE_ASK
        or input.tool_use_id
    ):
        return None
    probe = normalize(
        m.DetectedAsk(input, m.QuestionTool.CLAUDE_ASK, m.NativeCallId("binding"))
    )
    if not probe:
        return None
    expected = tuple(_semantic_question(question) for question in probe)
    by_call: dict[str, list[m.Question]] = {}
    for question in existing_questions:
        by_call.setdefault(str(question.origin_call), []).append(question)
    matches = [
        call
        for call, questions in by_call.items()
        if tuple(_semantic_question(question) for question in questions) == expected
    ]
    return m.NativeCallId(matches[0]) if len(matches) == 1 else None


def observe(input: m.HookInput, response: object) -> m.LifecycleKind | None:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    if (
        input.event is m.HookEvent.POST_TOOL_USE
        and input.tool == m.QuestionTool.CLAUDE_ASK
        and isinstance(response, Mapping)
        and response.get(m.ClaudeQuestionField.ANSWERS)
    ):
        return m.LifecycleKind.NATIVE_ANSWER
    return None
