#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Shared lifecycle types used by callback and question handling."""
from enum import StrEnum


class Event(StrEnum):
    RUN_FINISHED = "run_finished"
    RUN_REJECTED = "run_rejected_no_ticket"
    NEEDS_INPUT = "needs_input"
    HEARTBEAT = "heartbeat"
    LINEAGE = "lineage"
    TEST = "test"


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"
    SESSION_END = "SessionEnd"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PERMISSION_REQUEST = "PermissionRequest"
    NOTIFICATION = "Notification"
    POST_MODEL_SWITCH = "PostModelSwitch"
    TEAMMATE_IDLE = "TeammateIdle"
    TASK_CREATED = "TaskCreated"
    TASK_COMPLETED = "TaskCompleted"


class InputKind(StrEnum):
    APPROVAL = "approval"
    QUESTION = "question"
    TRUST = "trust"
    OTHER = "other"
    QUESTION_DIGEST = "question_digest"


class AgentKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
