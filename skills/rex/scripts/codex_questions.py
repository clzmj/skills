#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Codex native question boundary; question IDs never derive from text."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
import json
from uuid import uuid4

import grokbot_callback as callback
import question_model as m
from question_redact import untrusted


class ResponseField(StrEnum):
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    IS_ERROR = "is_error"
    ANSWERS = "answers"


def native_call(input: m.HookInput) -> m.NativeCallId:
    # Missing host IDs use the spec's event-binding fallback, never batch or
    # question identity. Real host call IDs are the preferred duplicate key.
    value = input.tool_use_id or callback.request_identity(
        {"tool_name": input.tool, "tool_input": dict(input.tool_input)}
    )
    return m.NativeCallId(m.parse_identity(value, "call"))


def detect(input: m.HookInput) -> m.DetectedAsk | None:
    if input.event is not m.HookEvent.PRE_TOOL_USE or input.tool not in (
        m.QuestionTool.CODEX_BLOCKING,
        m.QuestionTool.CODEX_ASYNC,
    ):
        return None
    return m.DetectedAsk(input, m.QuestionTool(input.tool), native_call(input))


def text(value: object) -> m.Untrusted | None:
    return untrusted(value, m.UntrustedSource.AGENT) if isinstance(value, str) else None


def options(value: object) -> tuple[m.Option, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            continue
        label = text(raw.get(m.CodexQuestionField.LABEL))
        if label is not None:
            result.append(
                m.Option(
                    m.OptionIndex(index),
                    label,
                    text(raw.get(m.CodexQuestionField.DESCRIPTION)),
                )
            )
    return tuple(result)


def normalize(ask: m.DetectedAsk) -> tuple[m.Question, ...]:
    result = []
    for validated in callback.validate_question_input(dict(ask.input.tool_input)) or ():
        item = validated.raw
        question = text(validated.text)
        if question is None or not question.text.strip():
            continue
        key = item.get(m.CodexQuestionField.ID)
        result.append(
            m.Question(
                m.QuestionId(uuid4()),
                ask.call,
                question,
                text(item.get(m.CodexQuestionField.HEADER)),
                options(item.get(m.CodexQuestionField.OPTIONS)),
                False,
                True,
                m.NativeQuestionKey(
                    codex_id=key if isinstance(key, str) else str(uuid4())
                ),
                None,
            )
        )
    return tuple(result)


def observe(input: m.HookInput, response: object) -> m.LifecycleKind | None:
    if input.event is not m.HookEvent.POST_TOOL_USE or input.tool not in (
        m.QuestionTool.CODEX_BLOCKING,
        m.QuestionTool.CODEX_ASYNC,
    ):
        return None
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    if isinstance(response, Mapping):
        if (
            response.get(ResponseField.ERROR)
            or response.get(ResponseField.UNAVAILABLE) is True
            or response.get(ResponseField.IS_ERROR) is True
        ):
            return m.LifecycleKind.NATIVE_CALL_FAILED
        if input.tool == m.QuestionTool.CODEX_BLOCKING and response.get(
            ResponseField.ANSWERS
        ):
            return m.LifecycleKind.NATIVE_ANSWER
    return None
