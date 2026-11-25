#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in question detection. Hook observations never grant decision authority."""

from __future__ import annotations

import argparse
from dataclasses import replace
from collections.abc import Mapping
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Final, TypedDict

import claude_questions
import codex_questions
import grokbot_callback as callback
import question_broker as broker
import question_guard as guard
import question_ledger as ledger
import question_model as m
from question_redact import redact

# Recorded P5 failure: docs/qe/question-delivery-evidence.md, P5 section.
# Set false if a later probe passes; an absent failure must not block the route.
# TODO(PR-4): implement deny-plus-pending after resolving the coarse P5 gate;
# evidence: docs/qe/question-delivery-evidence.md:94-102.
P5_FAILED: Final = True

END_EVENTS: Final = {
    m.HookEvent.STOP: m.LifecycleKind.TURN_ENDED,
    m.HookEvent.SESSION_END: m.LifecycleKind.SESSION_ENDED,
    m.HookEvent.SUBAGENT_STOP: m.LifecycleKind.SESSION_ENDED,
}


class EnqueueError(Exception):
    """A detached broker command could not be launched."""


class HookOutput(TypedDict):
    """PR-3 never emits inline answers; its host response is an empty object."""


def remote(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        return redact(result.stdout.strip()).text if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def brief(
    cwd: str, origin: str
) -> tuple[m.BriefEscalation | None, Path | None, m.TextDigest | None]:
    name = os.environ.get(m.BRIEF_ENV)
    path = Path(name) if name else None
    content = path.read_bytes() if path else None
    return (
        guard.parse_brief(
            content.decode(m.UTF8) if content is not None else None,
            cwd=cwd,
            remote=origin,
        ),
        path,
        m.file_digest(content) if content is not None else None,
    )


def content_strings(value: object) -> tuple[str, ...]:
    """Only native question content, excluding opaque native identifiers."""
    if not isinstance(value, Mapping):
        return ()
    fields = (
        m.CodexQuestionField.QUESTION,
        m.CodexQuestionField.TITLE,
        m.CodexQuestionField.HEADER,
        m.CodexQuestionField.LABEL,
        m.CodexQuestionField.DESCRIPTION,
    )
    result = tuple(item for key in fields if isinstance(item := value.get(key), str))
    for key in (m.CodexQuestionField.QUESTIONS, m.CodexQuestionField.OPTIONS):
        items = value.get(key)
        if isinstance(items, list):
            result += tuple(text for item in items for text in content_strings(item))
    return result


def prompt_origin(
    store: ledger.Ledger, actor: str, session: str, prompt: str
) -> m.PromptOrigin | None:
    """Only PR-3 rules 1–3; later takeover rules belong to PR-4."""
    normalized = m.normalize_prompt(prompt)
    digest = m.body_digest(normalized)
    for row in store.db.execute(
        "SELECT text FROM outbound WHERE target_actor=?", (actor,)
    ):
        if m.body_digest(row[0]) == digest:
            return m.PromptOrigin.BROKER
    for row in store.db.execute(
        "SELECT delivery_id,text FROM box_note WHERE target_actor=?", (actor,)
    ):
        if m.body_digest(row[1]) == digest:
            with store.transaction():
                store.db.execute(
                    "UPDATE box_note SET state=? WHERE delivery_id=?",
                    (m.BoxNoteState.ECHOED, row[0]),
                )
            return m.PromptOrigin.BOX
    identifier = m.collaborator_message_id(prompt)
    database = store.path.parent / "coordination.sqlite3"
    if identifier and database.exists():
        try:
            with closing(
                sqlite3.connect(
                    database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
                )
            ) as db:
                row = db.execute(
                    "SELECT text FROM events WHERE id=? AND recipient=? AND kind=?",
                    (identifier, session, "queue_requested"),
                ).fetchone()
            if row and m.normalize_prompt(row[0]) == normalized:
                return m.PromptOrigin.COLLABORATOR
        except sqlite3.Error:
            pass
    return None


def launch(command: str, batch: str | None = None) -> None:
    args = [sys.executable, str(Path(broker.__file__).resolve()), command]
    if batch:
        args += ["--batch", batch]
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def handle(
    value: object, agent: m.AgentKind, store: ledger.Ledger, *, spawn: bool = True
) -> HookOutput:
    if os.environ.get(m.FLAG_ENV) != "1":
        return {}
    input = m.parse_hook_input(value, agent)
    state = callback.session_state(agent, input.session, input.child)
    if state is None:
        return {}
    actor = m.ActorKey(callback.actor_key(agent, input.session, input.child))
    if input.event in END_EVENTS:
        store.record_lifecycle(
            m.Lifecycle(actor, END_EVENTS[input.event], None, None, None, ledger.now())
        )
        if spawn:
            try:
                launch(broker.Command.TICK)
            except Exception as exc:
                raise EnqueueError from exc
        return {}
    if input.event is m.HookEvent.USER_PROMPT_SUBMIT:
        origin = prompt_origin(store, actor, input.session, input.prompt or "")
        store.record_lifecycle(
            m.Lifecycle(
                actor,
                m.LifecycleKind.PROMPT_SUBMITTED,
                None,
                origin,
                m.body_digest(input.prompt or ""),
                ledger.now(),
            )
        )
        return {}
    adapter = codex_questions if agent is m.AgentKind.CODEX else claude_questions
    if (
        agent is m.AgentKind.CLAUDE
        and input.tool == m.QuestionTool.CLAUDE_ASK
        and input.event in (m.HookEvent.PERMISSION_REQUEST, m.HookEvent.POST_TOOL_USE)
    ):
        current = [
            batch
            for batch in store.batches()
            if batch.asker.actor == actor
            and batch.asker.turn == (input.turn or state["turn"])
            and batch.state in ledger.OPEN_FOR_ATTACH
        ]
        if not input.tool_use_id:
            call = claude_questions.bind_call(
                input,
                tuple(question for batch in current for question in batch.questions),
            )
            if call is None:
                return {}  # Ambiguous permission activity never merges or retires questions.
            input = replace(input, tool_use_id=call)
        if input.event is m.HookEvent.PERMISSION_REQUEST and not any(
            call.call == input.tool_use_id for batch in current for call in batch.calls
        ):
            return {}  # Only PreToolUse opens a Claude batch.
    response = value.get("tool_response", value) if isinstance(value, Mapping) else None
    observed = adapter.observe(input, response)
    if observed:
        call = codex_questions.native_call(input)
        row = store.db.execute(
            "SELECT batch FROM native_call WHERE call=?", (call,)
        ).fetchone()
        with store.transaction():
            store.record_lifecycle(
                m.Lifecycle(actor, observed, call, None, None, ledger.now()),
                row[0] if row else None,
            )
            if (
                row
                and observed is m.LifecycleKind.NATIVE_ANSWER
                and store.get_batch(row[0]).state
                in {m.BatchState.OPEN, m.BatchState.ESCALATING, m.BatchState.BOX_OWNED}
            ):
                store.cancel_local(row[0], m.CancelCause.ANSWERED_IN_PANE)
        return {}
    detected = adapter.detect(input)
    if detected is None:
        return {}
    questions = adapter.normalize(detected)
    if not questions:
        return {}
    lineage = callback.session_lineage(state)
    origin = remote(input.cwd)
    policy_available = True
    try:
        policy, brief_path, brief_digest = brief(input.cwd, origin)
    except (OSError, UnicodeError, guard.BriefInvalid):
        policy, brief_path, brief_digest = None, None, None
        policy_available = False
    callback_bot = state["requester"].strip().casefold()
    valid_callback = bool(m.CALLBACK_BOT_PATTERN.fullmatch(callback_bot))
    asker = m.AskerRef(
        agent,
        actor,
        m.ActorKey(state["root"]),
        m.ActorRole.NATIVE_SUBAGENT
        if lineage["role"] is callback.Role.SUBAGENT
        else m.ActorRole.ROOT,
        m.SessionId(input.child or input.session),
        m.TurnId(input.turn or state["turn"]),
        m.TaskId(state["task_id"]),
        m.RexSessionId(state["rex_session"]),
        m.RexBlockId(state["rex_block"]),
        input.cwd,
        m.HeadSha(callback.git_metadata(input.cwd)["head_sha"]),
        brief_digest,
        m.CallbackBot(callback_bot if valid_callback else "unknown"),
    )
    secret = any(
        redact(text).secret_present for text in content_strings(input.tool_input)
    )
    classification = guard.derive_floor(
        questions,
        brief=policy,
        cwd=input.cwd,
        remote=origin,
        secret_present=secret,
        policy_available=policy_available,
    )
    call = m.NativeCall(
        detected.tool, detected.call, actor, asker.turn, ledger.now(), False, None
    )
    with store.transaction():
        attached = store.open_or_attach(
            asker, questions, call, classification, brief=brief_path
        )
        batch = attached.batch
        if attached.outcome is not m.AttachOutcome.DUPLICATE_CALL:
            if not policy_available:
                store.notice(
                    m.NoticeKind.BRIEF_INVALID,
                    asker.task,
                    "Brief unavailable or invalid; policy was not truncated",
                    str(batch.id),
                )
            if not valid_callback and batch.state is not m.BatchState.BLOCKED:
                store.transition(
                    str(batch.id),
                    m.BatchState.BLOCKED,
                    block_cause=m.BlockCause.UNKNOWN_CALLBACK_BOT,
                )
                store.notice(
                    m.NoticeKind.BLOCKED,
                    asker.task,
                    "Unknown callback bot",
                    str(batch.id),
                )
            # P5 answer adoption failed. Do not open an unattended parked dialog
            # or impersonate an inline answer; leave the native question visible.
            elif (
                P5_FAILED
                and agent is m.AgentKind.CLAUDE
                and batch.state is not m.BatchState.BLOCKED
            ):
                store.transition(
                    str(batch.id),
                    m.BatchState.BLOCKED,
                    block_cause=m.BlockCause.NO_DELIVERY_ROUTE,
                )
                store.notice(
                    m.NoticeKind.BLOCKED,
                    asker.task,
                    "Claude parked-dialog route blocked by recorded P5 failure; answer in the pane",
                    str(batch.id),
                )
            broker.prepare_registration(
                store,
                store.get_batch(str(batch.id)),
                origin,
                policy,
                lineage["root_session_id"],
                secret,
            )
    if spawn:
        try:
            launch(broker.Command.TICK)
            launch(broker.Command.WORKER, str(batch.id))
        except Exception as exc:
            raise EnqueueError from exc
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hook",))
    parser.add_argument("--agent", choices=list(m.AgentKind), required=True)
    args = parser.parse_args()
    if os.environ.get(m.FLAG_ENV) != "1":
        print("{}")
        return 0
    try:
        value = json.load(sys.stdin)
        with ledger.Ledger(callback.rex.STATE / m.LEDGER_FILE) as store:
            result = handle(value, m.AgentKind(args.agent), store)
    except Exception as exc:  # noqa: BLE001 - a broken hook must leave the native question available.
        try:
            callback.receipt(
                callback.Outcome.ENQUEUE_FAILED
                if isinstance(exc, EnqueueError)
                else callback.Outcome.HOOK_ERROR
            )
        except OSError:
            pass
        result = {}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
