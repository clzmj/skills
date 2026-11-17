#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in question detection. Hook observations never grant decision authority."""

from __future__ import annotations

import argparse
from dataclasses import replace
from enum import StrEnum
from collections.abc import Mapping
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Final, TypedDict
from uuid import uuid4

import claude_questions
import codex_questions
import grokbot_callback as callback
import question_broker as broker
import question_delivery as delivery
import question_guard as guard
import question_ledger as ledger
import question_model as m
from question_redact import redact

END_EVENTS: Final = {
    m.HookEvent.STOP: m.LifecycleKind.TURN_ENDED,
    m.HookEvent.SESSION_END: m.LifecycleKind.SESSION_ENDED,
    m.HookEvent.SUBAGENT_STOP: m.LifecycleKind.SESSION_ENDED,
}


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionDecision(TypedDict, total=False):
    behavior: PermissionBehavior
    message: str
    updatedInput: dict[str, object]


class PermissionOutput(TypedDict):
    hookEventName: m.HookEvent
    decision: PermissionDecision


class HookOutput(TypedDict, total=False):
    hookSpecificOutput: PermissionOutput


def inline_answers(store: ledger.Ledger, message: m.OutboundMessage) -> dict[str, str]:
    row = store.db.execute(
        "SELECT json FROM resolution WHERE batch=? AND rev=?",
        (str(message.batch), message.resolution_rev),
    ).fetchone()
    if row is None:
        raise ValueError("inline answer has no resolution")
    resolution = m.parse_record(m.Resolution, json.loads(row[0]))
    batch = store.get_batch(str(message.batch))
    questions = {question.id: question for question in batch.questions}
    answers = {}
    for answer in resolution.answers:
        question = questions[answer.question]
        labels = {option.index: option.label.text for option in question.options}
        values = [labels[index] for index in answer.choice]
        if answer.free_text:
            values.append(answer.free_text.text)
        text = question.text.text
        if text in answers:
            raise ValueError("inline answers have duplicate native question text")
        answers[text] = ", ".join(values)
    return answers


def _matches_inline_answers(
    store: ledger.Ledger, message: m.OutboundMessage, response: Mapping[str, object]
) -> bool:
    try:
        return response.get(m.ClaudeQuestionField.ANSWERS) == inline_answers(
            store, message
        )
    except (KeyError, TypeError, ValueError):
        return False


def inline_output(
    store: ledger.Ledger, batch_id: str, input: m.HookInput
) -> HookOutput:
    """Claim an existing valid intent. This seam does not enable Claude admission."""
    batch = store.get_batch(batch_id)
    if (
        input.agent is not m.AgentKind.CLAUDE
        or input.event is not m.HookEvent.PERMISSION_REQUEST
        or batch.state in ledger.TERMINAL
        or batch.state is m.BatchState.BLOCKED
    ):
        return {}
    dispatcher = delivery.Delivery(store)
    for message in store.outbounds(batch_id):
        if message.target_actor != batch.asker.actor or not any(
            route in delivery.INLINE for route in message.route_plan
        ):
            continue
        if message.purpose is m.OutboundPurpose.ANSWER:
            if m.Route.INLINE_UPDATED_INPUT not in message.route_plan:
                continue  # P5 never established deny-reason answer adoption.
            updated = dict(input.tool_input)
            try:
                answers = inline_answers(store, message)
            except (KeyError, TypeError, ValueError):
                continue  # Invalid synthetic answers stay with the native dialog.
            updated[str(m.ClaudeQuestionField.ANSWERS)] = answers
            decision = PermissionDecision(
                behavior=PermissionBehavior.ALLOW, updatedInput=updated
            )
        else:
            decision = PermissionDecision(
                behavior=PermissionBehavior.DENY, message=message.text
            )
        dispatcher.wait_inline(batch_id, input.event, ledger.now() + m.INLINE_WAIT)
        if dispatcher.dispatch(str(message.id), inline_pid=os.getpid()):
            return HookOutput(
                hookSpecificOutput=PermissionOutput(
                    hookEventName=input.event, decision=decision
                )
            )
    return {}


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
) -> m.PromptOrigin:
    """Apply §7.4 in order using only the markers actually recorded by P10."""
    normalized = m.normalize_prompt(prompt)
    digest = m.body_digest(normalized)
    for row in store.db.execute(
        "SELECT text FROM outbound WHERE target_actor=?", (actor,)
    ):
        if m.body_digest(row[0]) == digest:
            store.observe_outbounds(actor, prompt)
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
    # P10 found neither a queue marker (rule 4) nor positive keyboard evidence
    # (rule 6) for either host. Arbitrary payload fields cannot invent either.
    if "[qe:" in normalized or "[Codex team" in normalized:
        return m.PromptOrigin.UNVERIFIED_MARKER
    return m.PromptOrigin.UNKNOWN


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
    value: object,
    agent: m.AgentKind,
    store: ledger.Ledger,
    *,
    spawn: bool = True,
    workers: list[str] | None = None,
) -> HookOutput:
    if os.environ.get(m.FLAG_ENV) != "1":
        return {}

    def request_worker(batch: str) -> None:
        if workers is None:
            launch(broker.Command.WORKER, batch)
        else:
            workers.append(batch)

    input = m.parse_hook_input(value, agent)
    state = callback.session_state(agent, input.session, input.child)
    if state is None:
        return {}
    actor = m.ActorKey(callback.actor_key(agent, input.session, input.child))
    delivery.Delivery(store).recover()
    if input.event in END_EVENTS:
        store.record_lifecycle(
            m.Lifecycle(actor, END_EVENTS[input.event], None, None, None, ledger.now())
        )
        if spawn:
            launch(broker.Command.TICK)
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
        for batch in store.batches():
            if (
                batch.asker.actor != actor and batch.asker.root_actor != actor
            ) or batch.state in ledger.TERMINAL:
                continue
            if origin is m.PromptOrigin.UNVERIFIED_MARKER:
                store.notice(
                    m.NoticeKind.UNVERIFIED_MARKER,
                    batch.asker.task,
                    "Unverified delivery or collaborator marker",
                    str(batch.id),
                )
            elif origin in (m.PromptOrigin.UNKNOWN, m.PromptOrigin.HUMAN):
                store.record_takeover(
                    m.Takeover(
                        m.TakeoverId(uuid4()),
                        batch.id,
                        origin,
                        m.body_digest(input.prompt or ""),
                        ledger.now(),
                        None,
                        None,
                    )
                )
            if spawn:
                request_worker(str(batch.id))
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
            and batch.state in ledger.OPEN_FOR_ATTACH | {m.BatchState.RESOLVED_LOCAL}
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
        if input.event is m.HookEvent.PERMISSION_REQUEST:
            row = store.db.execute(
                "SELECT batch FROM native_call WHERE call=?",
                (codex_questions.native_call(input),),
            ).fetchone()
            if row and (output := inline_output(store, row[0], input)):
                return output
    response = value.get("tool_response", value) if isinstance(value, Mapping) else None
    observed = adapter.observe(input, response)
    if observed:
        call = codex_questions.native_call(input)
        row = store.db.execute(
            "SELECT batch FROM native_call WHERE call=?", (call,)
        ).fetchone()
        ours = bool(
            row
            and isinstance(response, Mapping)
            and any(
                message.purpose is m.OutboundPurpose.ANSWER
                and message.state
                in (
                    m.DeliveryState.DISPATCHED,
                    m.DeliveryState.OBSERVED,
                    m.DeliveryState.APPLIED,
                )
                and store.db.execute(
                    "SELECT 1 FROM dispatch_attempt WHERE outbound=? AND route=?",
                    (str(message.id), m.Route.INLINE_UPDATED_INPUT),
                ).fetchone()
                and _matches_inline_answers(store, message, response)
                for message in store.outbounds(row[0])
            )
        )
        with store.transaction():
            store.record_lifecycle(
                m.Lifecycle(actor, observed, call, None, None, ledger.now()),
                row[0] if row else None,
                ours=ours,
            )
            if row and observed is m.LifecycleKind.NATIVE_ANSWER and not ours:
                batch = store.get_batch(row[0])
                if (
                    batch.state not in ledger.TERMINAL
                    and batch.state is not m.BatchState.BLOCKED
                ):
                    store.record_takeover(
                        m.Takeover(
                            m.TakeoverId(uuid4()),
                            batch.id,
                            m.PromptOrigin.HUMAN,
                            None,
                            ledger.now(),
                            None,
                            None,
                        ),
                        cause=m.CancelCause.ANSWERED_IN_PANE,
                    )
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
    classification = guard.assess_guard(
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
        batch = store.get_batch(str(attached.batch.id))
        if attached.outcome is not m.AttachOutcome.DUPLICATE_CALL:
            if not policy_available:
                store.notice(
                    m.NoticeKind.BRIEF_INVALID,
                    asker.task,
                    "Brief unavailable or invalid; policy was not truncated",
                    str(batch.id),
                )
            if not valid_callback and batch.state in {
                m.BatchState.OPEN,
                m.BatchState.ESCALATING,
            }:
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
            elif agent is m.AgentKind.CLAUDE and batch.state in {
                m.BatchState.OPEN,
                m.BatchState.ESCALATING,
            }:
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
            batch = store.get_batch(str(batch.id))
            if len(batch.questions) > m.MAX_QUESTIONS or any(
                len(q.options) > m.MAX_OPTIONS for q in batch.questions
            ):
                broker.block_oversized(
                    store,
                    batch,
                    "Question or option count exceeds registration limits; answer in the pane",
                )
            elif store.guard_floor(
                str(batch.id)
            ) >= m.EscalationLevel.L3_PRINCIPAL or batch.state in {
                m.BatchState.BLOCKED,
                m.BatchState.BOX_OWNED,
            }:
                broker.prepare_registration(
                    store,
                    store.get_batch(str(batch.id)),
                    origin,
                    policy,
                    lineage["root_session_id"],
                    secret,
                )
            else:
                store.queue_classification(
                    str(batch.id),
                    remote=origin,
                    brief=policy,
                    root_session=lineage["root_session_id"],
                )
    if spawn:
        launch(broker.Command.TICK)
        request_worker(str(batch.id))
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hook",))
    parser.add_argument("--agent", choices=list(m.AgentKind), required=True)
    args = parser.parse_args()
    if os.environ.get(m.FLAG_ENV) != "1":
        print("{}")
        return 0
    result: HookOutput = {}
    try:
        value = json.load(sys.stdin)
        with ledger.Ledger(callback.rex.STATE / m.LEDGER_FILE) as store:
            workers: list[str] = []
            result = handle(value, m.AgentKind(args.agent), store, workers=workers)
            # Every hook recovers durable session intents after recording its
            # own observation. Check for a live worker before enqueueing one.
            try:
                input = m.parse_hook_input(value, m.AgentKind(args.agent))
                actor = callback.actor_key(input.agent, input.session, input.child)
                pending = store.db.execute(
                    """SELECT DISTINCT o.batch FROM outbound o JOIN batch b ON b.id=o.batch
                    WHERE (b.actor=? OR b.root_actor=?) AND b.state NOT IN (?,?)
                    AND o.paused_by IS NULL AND o.state IN (?,?)""",
                    (
                        actor,
                        actor,
                        *ledger.TERMINAL,
                        m.DeliveryState.PENDING,
                        m.DeliveryState.DISPATCH_FAILED,
                    ),
                ).fetchall()
                workers.extend(row[0] for row in pending)
                for batch in dict.fromkeys(workers):
                    with broker.Broker(store).worker_lock(batch, wait=0) as acquired:
                        available = acquired is not None
                    if available:
                        launch(broker.Command.WORKER, batch)
                        break
            except Exception:  # noqa: BLE001 - recovery cannot erase an inline decision.
                logging.getLogger(__name__).error("Question hook recovery failed")
                try:
                    callback.receipt(callback.Outcome.ENQUEUE_FAILED)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 - a broken hook must leave the native question available.
        try:
            callback.receipt(callback.Outcome.ENQUEUE_FAILED)
        except OSError:
            pass
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
