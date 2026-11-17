#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mac question broker. Plans are transport inputs and grant no box authority."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from typing import Callable, Final, Generator, TypeVar, TypedDict, cast
from uuid import UUID
from uuid import uuid4

import grokbot_callback as callback
import question_ledger as ledger
import question_model as m
import question_routes as routes
import question_guard as guard
from question_redact import redact

# An ordinary Stop is idle, not proof that the session ended. WAITING can
# contain background work and therefore uses the running Codex queue route.
TURN_RUNNING: Final = {
    callback.Status.IDLE: False,
    callback.Status.RUNNING: True,
    callback.Status.WAITING: True,
    callback.Status.COMPLETED: False,
    callback.Status.FAILED: False,
    callback.Status.ENDED: False,
    callback.Status.ERROR: False,
}
ARRIVED: Final = frozenset(
    {m.BoxNoteState.SENT, m.BoxNoteState.ECHOED, m.BoxNoteState.UNKNOWN}
)
SENT: Final = frozenset(
    {m.SendOutcome.SENT_ECHOED, m.SendOutcome.SENT_NO_ECHO, m.SendOutcome.SEND_UNKNOWN}
)
B64_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]+")


class Command(StrEnum):
    RUN_PLAN = "run-plan"
    STATUS = "status"
    REGISTRATIONS = "registrations"
    CLEAR_TAKEOVER = "clear-takeover"
    RECONCILE = "reconcile"
    CANCEL_LOCAL = "cancel-local"
    ACK = "ack"
    AUDIT = "audit"
    TICK = "tick"
    WORKER = "worker"


DIGEST_MAX: Final = 64 * 1024
DIGEST_COALESCE: Final = timedelta(seconds=30)


def cap_text(value: str | None, cap: int) -> str | None:
    if value is None:
        return None
    clean = "".join(c for c in value if ord(c) >= 32 or c in "\n\t")
    return clean.encode(m.UTF8)[:cap].decode(m.UTF8, errors="ignore")


def register_question(question: m.Question, cap: int) -> m.RegisterQuestion:
    text = cap_text(question.text.text, min(cap, m.QUESTION_TEXT_MAX)) or ""
    header = cap_text(
        question.header.text if question.header else None, min(cap, m.HEADER_MAX)
    )
    options: list[m.DigestOption] = [
        m.DigestOption(
            index=o.index,
            label=cap_text(o.label.text, min(cap, m.OPTION_LABEL_MAX)) or "",
            description=cap_text(
                o.description.text if o.description else None,
                min(cap, m.OPTION_DESCRIPTION_MAX),
            ),
        )
        for o in question.options
    ]
    truncated = (
        text != question.text.text
        or header != (question.header.text if question.header else None)
        or any(
            o["label"] != native.label.text
            or o["description"]
            != (native.description.text if native.description else None)
            for o, native in zip(options, question.options)
        )
    )
    wire = m.RegisterQuestion(
        id=str(question.id),
        text=text,
        header=header,
        options=options,
        multi_select=question.multi_select,
        allows_free_text=question.allows_free_text,
        retired_rev=question.retired_rev,
        truncated=truncated,
        digest="",
    )
    wire["digest"] = m.digest(
        {
            key: value
            for key, value in wire.items()
            if key not in ("digest", "retired_rev", "truncated")
        }
    )
    return wire


def block_oversized(store: ledger.Ledger, batch: m.QuestionBatch, detail: str) -> None:
    if batch.state is not m.BatchState.BLOCKED:
        store.transition(
            str(batch.id), m.BatchState.BLOCKED, block_cause=m.BlockCause.TOO_LARGE
        )
    if not store.db.execute(
        "SELECT 1 FROM notice WHERE batch=? AND kind=?",
        (str(batch.id), m.NoticeKind.TOO_LARGE),
    ).fetchone():
        store.notice(m.NoticeKind.TOO_LARGE, batch.asker.task, detail, str(batch.id))


def prepare_registration(
    store: ledger.Ledger,
    batch: m.QuestionBatch,
    remote: str = "",
    brief: m.BriefEscalation | None = None,
    root_session: str | None = None,
    secret_present: bool = False,
) -> m.RegisterEntry | None:
    """Freeze bounded redacted content once; identities and policy are never cut."""
    existing = store.db.execute(
        "SELECT entry_json FROM registration WHERE batch=? AND batch_rev=?",
        (str(batch.id), batch.rev),
    ).fetchone()
    if existing and existing[0]:
        return m.parse_register_entry(json.loads(existing[0]))
    if len(batch.questions) > m.MAX_QUESTIONS or any(
        len(q.options) > m.MAX_OPTIONS for q in batch.questions
    ):
        block_oversized(
            store,
            batch,
            "Question or option count exceeds registration limits; answer in the pane",
        )
        return None
    a = batch.asker
    origin = redact(remote).text
    cap = 1024
    while True:
        questions = [register_question(question, cap) for question in batch.questions]
        entry = m.RegisterEntry(
            schema=m.REGISTER_SCHEMA,
            batch_id=str(batch.id),
            batch_rev=batch.rev,
            task_id=a.task,
            actor=a.actor,
            agent=a.agent,
            role=a.role,
            session=a.session,
            rex_session=a.rex_session,
            rex_block=a.rex_block,
            cwd=a.cwd if len(a.cwd.encode(m.UTF8)) <= m.CWD_MAX else "",
            remote=origin if len(origin.encode(m.UTF8)) <= m.REMOTE_MAX else "",
            head_sha=a.head_sha,
            callback_bot=a.callback_bot,
            root_actor=a.root_actor,
            root_session=root_session or a.session,
            questions=questions,
            secret_present=secret_present
            or any(
                hit.trigger is m.GuardTrigger.SECRET_PRESENT
                for hit in batch.classification.hits
            ),
            mac_floor=max(batch.classification.floor, m.EscalationLevel.L3_PRINCIPAL),
            mac_hits=[
                m.DigestHit(rule_id=h.rule_id, trigger=h.trigger, strength=h.strength)
                for h in batch.classification.hits[: m.MAC_HITS_MAX]
            ],
            brief_digest=a.brief_digest,
            brief_ask_carlos=list(brief.ask_carlos) if brief else [],
            brief_project=brief.project if brief else None,
            classified=False,
            category=None,
            delegable=None,
            sensitive_topics=[],
            classification_floor=None,
            content_digest="",
        )
        entry["content_digest"] = m.digest(
            {key: value for key, value in entry.items() if key != "content_digest"}
        )
        if len(m.canonical(entry)) <= m.REGISTER_ENTRY_MAX:
            store.put_registration(entry)
            return entry
        if cap <= 32:
            block_oversized(
                store,
                batch,
                "Registration exceeds wire limit; answer in the pane",
            )
            return None
        cap //= 2


class RegistrationsPage(TypedDict):
    registrations: list[m.RegisterEntry]
    progress: list[m.RegisterProgress]
    more: bool
    cursor: str | None


class PageTable(IntEnum):
    REGISTRATION = 0
    PROGRESS = 1
    BATCH = 2
    TAKEOVER = 3
    NOTICE = 4
    PLAN_SEEN = 5


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PageItem:
    append: Callable[[], None]
    pop: Callable[[], object]
    batch_id: str
    key: tuple[PageTable, int]


def page_items(
    destination: list[T],
    source: list[T],
    identity: Callable[[T], str],
    table: PageTable,
    position: Callable[[T], int],
) -> list[PageItem]:
    return [
        PageItem(
            lambda item=item: destination.append(item),
            destination.pop,
            identity(item),
            (table, position(item)),
        )
        for item in source
    ]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode(value: object) -> str:
    return base64.urlsafe_b64encode(m.canonical(value)).decode(m.UTF8).rstrip("=")


def decode_plan(value: str) -> m.Plan:
    if len(value) > m.MAX_PLAN_B64 or not B64_PATTERN.fullmatch(value):
        raise ValueError("invalid plan encoding or size")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        if len(raw) > m.MAX_PLAN_JSON:
            raise ValueError("plan too large")
        request = m.parse_plan(json.loads(raw))
    except (binascii.Error, UnicodeError, TypeError, RecursionError) as error:
        raise ValueError("invalid plan") from error
    # batch_id is used in the shared worker lock's filename, so validate it
    # before any filesystem or ledger mutation, including plan_seen.
    if request["action"] is m.PlanAction.SEND:
        UUID(cast(m.SendBody, request["body"])["batch_id"])
    return request


def seal(result: m.Result) -> m.Result:
    result["result_digest"] = m.digest(
        {key: value for key, value in result.items() if key != "result_digest"}
    )
    return m.parse_result(result)


def empty_result(request: m.Plan, at: datetime) -> m.Result:
    return {
        "schema": m.RESULT_SCHEMA,
        "plan_id": request["plan_id"],
        "plan_digest": request["plan_digest"],
        "lease_id": request["lease_id"],
        "action": request["action"],
        "ok": True,
        "error_kind": None,
        "send": None,
        "route_used": None,
        "pane_check": None,
        "takeover_id": None,
        "local_answer_dispatched": False,
        "replayed": False,
        "mac_batch_rev": None,
        "mac_state": None,
        "mac_now": at.isoformat(timespec="microseconds"),
        "payload": None,
        "result_digest": "",
    }


class Broker:
    def __init__(
        self,
        store: ledger.Ledger,
        *,
        runner: routes.RouteRunner | None = None,
        clock: Callable[[], datetime] = utc_now,
        lock_wait: float = m.RUN_PLAN_LOCK_WAIT.total_seconds(),
    ):
        self.store = store
        self.runner = runner or routes.RouteRunner()
        self.clock = clock
        self.lock_wait = lock_wait

    def seen_results(self, plan_ids: list[str]) -> list[m.PlanSeenEntry]:
        found: list[m.PlanSeenEntry] = []
        for plan_id in plan_ids:
            row = self.store.db.execute(
                "SELECT plan_id,result_b64 FROM plan_seen WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row:
                found.append(m.parse_record(m.PlanSeenEntry, dict(row)))
        return found

    def box_note(self, delivery: str) -> m.BoxNote | None:
        row = self.store.db.execute(
            "SELECT * FROM box_note WHERE delivery_id=?", (delivery,)
        ).fetchone()
        return m.parse_box_note(dict(row)) if row else None

    def status(self, task: str | None = None, batch_id: str | None = None) -> m.Status:
        result: m.Status = {
            "schema": m.STATUS_SCHEMA,
            "task_id": task or "",
            "batches": [],
            "takeovers": [],
            "notices": [],
        }
        for batch in self.store.batches(task=task):
            identifier = str(batch.id)
            if batch_id is not None and identifier != batch_id:
                continue
            row = self.store.db.execute(
                "SELECT mac_floor FROM batch WHERE id=?", (identifier,)
            ).fetchone()
            acked = self.store.db.execute(
                "SELECT max(acked_floor) FROM registration WHERE batch=?", (identifier,)
            ).fetchone()[0]
            resolution = self.store.db.execute(
                "SELECT max(rev) FROM resolution WHERE batch=?", (identifier,)
            ).fetchone()[0]
            outbound: list[m.StatusOutbound] = []
            for item in self.store.db.execute(
                "SELECT id,purpose,resolution_rev,state,applied_rev FROM outbound WHERE batch=? ORDER BY rowid",
                (identifier,),
            ):
                route = self.store.db.execute(
                    "SELECT route FROM dispatch_attempt WHERE outbound=? ORDER BY n DESC LIMIT 1",
                    (item["id"],),
                ).fetchone()
                outbound.append(
                    m.parse_record(
                        m.StatusOutbound,
                        {
                            "id": item["id"],
                            "purpose": item["purpose"],
                            "resolution_rev": item["resolution_rev"],
                            "state": item["state"],
                            "route": route[0] if route else None,
                            "acked_rev": item["applied_rev"],
                        },
                    )
                )
            notes = [
                m.parse_record(m.StatusBoxNote, dict(item))
                for item in self.store.db.execute(
                    "SELECT delivery_id,attempt,plan_id,resolution_rev,purpose,state,acked_rev FROM box_note WHERE batch=? ORDER BY rowid",
                    (identifier,),
                )
            ]
            for note in notes:
                if note["state"] not in ARRIVED:
                    note["acked_rev"] = None
            result["batches"].append(
                {
                    "batch_id": identifier,
                    "batch_rev": batch.rev,
                    "state": batch.state,
                    "mac_floor": m.EscalationLevel(row[0]),
                    "acked_floor": m.EscalationLevel(acked)
                    if acked is not None
                    else None,
                    "context": batch.context,
                    "callback_bot": batch.asker.callback_bot,
                    "session_alive": self.store.session_alive(batch.asker.actor),
                    "answered_in_pane": self.store.answered_in_pane(identifier),
                    "local_resolution": resolution,
                    "outbound": outbound,
                    "box_notes": notes,
                }
            )
            for item in self.store.db.execute(
                "SELECT id,batch AS batch_id,origin,cleared_at FROM takeover WHERE batch=? ORDER BY rowid",
                (identifier,),
            ):
                result["takeovers"].append(
                    {
                        "id": item["id"],
                        "batch_id": item["batch_id"],
                        "origin": m.PromptOrigin(item["origin"]),
                        "cleared": item["cleared_at"] is not None,
                    }
                )
        ids = {item["batch_id"] for item in result["batches"]}
        for item in self.store.db.execute("SELECT * FROM notice ORDER BY rowid"):
            if (task is not None and item["task"] != task) or (
                batch_id is not None and item["batch"] != batch_id
            ):
                continue
            if (
                task is None
                and batch_id is None
                or item["batch"] in ids
                or item["batch"] is None
            ):
                result["notices"].append(
                    m.parse_record(
                        m.DigestNotice,
                        {
                            "id": item["id"],
                            "kind": item["kind"],
                            "batch_id": item["batch"],
                            "detail": item["detail"],
                            "created_at": item["created_at"],
                        },
                    )
                )
        return m.parse_record(m.Status, result)

    @contextmanager
    def _file_lock(
        self, name: str, *, wait: float | None = None
    ) -> Generator[int | None, None, None]:
        fd = os.open(
            self.store.path.parent / name,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        acquired = False
        deadline = time.monotonic() + (self.lock_wait if wait is None else wait)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield fd if acquired else None
        finally:
            os.close(fd)

    @contextmanager
    def worker_lock(
        self, batch: str, *, wait: float | None = None
    ) -> Generator[bool, None, None]:
        UUID(batch)
        with self._file_lock(m.WORKER_LOCK.format(batch=batch), wait=wait) as acquired:
            yield acquired is not None

    def fence(self, request: m.Plan, body: m.SendBody) -> m.SendOutcome | None:
        if self.clock() > datetime.fromisoformat(body["issued_at"]) + timedelta(
            seconds=body["valid_s"]
        ):
            return m.SendOutcome.NOT_SENT_EXPIRED
        note = self.box_note(body["delivery_id"])
        if note is not None:
            if note.attempt > body["attempt"] or (
                note.attempt == body["attempt"] and note.plan_id != request["plan_id"]
            ):
                return m.SendOutcome.NOT_SENT_SUPERSEDED
            if (
                note.state in ARRIVED
                and note.plan_id != request["plan_id"]
                and not body["resend_after_reconcile"]
            ):
                return m.SendOutcome.NOT_SENT_ALREADY_SENT
        return None

    def _send(self, request: m.Plan, result: m.Result) -> None:
        body = cast(m.SendBody, request["body"])
        result["send"] = self.fence(request, body)
        if result["send"] is not None:
            return
        with self.worker_lock(body["batch_id"]) as acquired:
            if not acquired:
                result["send"] = m.SendOutcome.NOT_SENT_BUSY
                return
            batch = self.store.get_batch(body["batch_id"])
            result["mac_batch_rev"], result["mac_state"] = batch.rev, batch.state
            if batch.state not in m.SEND_DELIVERABLE:
                result["send"] = {
                    m.BatchState.CANCELLED: m.SendOutcome.NOT_SENT_CANCELLED,
                    m.BatchState.STALE: m.SendOutcome.NOT_SENT_SESSION_GONE,
                }.get(batch.state, m.SendOutcome.NOT_SENT_NOT_DELIVERABLE)
                return
            if batch.rev > body["batch_rev"]:
                result["send"] = m.SendOutcome.NOT_SENT_STALE_REVISION
                return
            live = self.store.live_context(body["batch_id"])
            # The plan's high floor also needs the high-floor HEAD check even
            # when a delayed ACK has not yet raised the Mac's cached floor.
            if (
                body["floor"] >= m.EscalationLevel.L3_PRINCIPAL
                and live.head_sha != batch.asker.head_sha
            ):
                live = ledger.LiveContext(
                    live.head_sha, live.brief_digest, True, live.brief_path
                )
            if live.changed:
                updated = self.store.bump_for_context(body["batch_id"], live)
                result["mac_batch_rev"], result["mac_state"] = (
                    updated.rev,
                    updated.state,
                )
                result["send"] = m.SendOutcome.NOT_SENT_STALE_REVISION
                return
            if self.store.answered_in_pane(body["batch_id"]):
                result["send"] = m.SendOutcome.NOT_SENT_CANCELLED
                return
            takeovers = self.store.db.execute(
                "SELECT id,origin FROM takeover WHERE batch=? AND cleared_at IS NULL ORDER BY rowid",
                (body["batch_id"],),
            ).fetchall()
            if any(
                m.PromptOrigin(item["origin"]) is m.PromptOrigin.HUMAN
                for item in takeovers
            ):
                result["send"] = m.SendOutcome.NOT_SENT_CANCELLED
                return
            if takeovers:
                result["send"] = m.SendOutcome.NOT_SENT_PAUSED
                result["takeover_id"] = takeovers[0]["id"]
                return
            if not self.store.session_alive(
                batch.asker.actor
            ) or not self.store.session_alive(body["target_actor"]):
                result["send"] = m.SendOutcome.NOT_SENT_SESSION_GONE
                return
            if body["session_marker"] is None:
                result["pane_check"] = m.PaneCheck.SKIPPED_NO_MARKER
            else:
                capture = self.runner.capture(body["rex_session"], body["rex_block"])
                if capture is None or body["session_marker"] not in capture:
                    result["send"] = m.SendOutcome.NOT_SENT_STALE_PANE
                    return
                result["pane_check"] = m.PaneCheck.MATCHED
            note = self.box_note(body["delivery_id"])
            if note is not None and (
                note.text != body["text"]
                or note.ack_token_digest
                != m.file_digest(body["ack_token"].encode(m.UTF8))
                or str(note.batch) != body["batch_id"]
                or note.batch_rev != body["batch_rev"]
                or note.resolution_rev != body["resolution_rev"]
                or note.purpose != body["purpose"]
                or note.target_actor != body["target_actor"]
                or note.child_actor != body["child_actor"]
            ):
                raise ValueError("delivery payload changed between attempts")
            result["local_answer_dispatched"] = self.store.take_ownership(
                body["batch_id"], body["floor"]
            )
            result["send"] = self.fence(request, body)
            if result["send"] is not None:
                return
            prepared_at = self.clock()
            self.store.put_box_note(
                m.BoxNote(
                    m.DeliveryId(body["delivery_id"]),
                    m.SendAttempt(body["attempt"]),
                    m.PlanId(request["plan_id"]),
                    m.BatchId(UUID(body["batch_id"])),
                    m.BatchRev(body["batch_rev"]),
                    m.ResolutionRev(body["resolution_rev"]),
                    body["purpose"],
                    m.ActorKey(body["target_actor"]),
                    m.ActorKey(body["child_actor"]) if body["child_actor"] else None,
                    body["text"],
                    m.body_digest(body["text"]),
                    m.file_digest(body["ack_token"].encode(m.UTF8)),
                    body["floor"],
                    m.BoxNoteState.PREPARED,
                    None,
                    prepared_at,
                )
            )
            state = callback.session_state(
                batch.asker.agent, body["codex_thread"] or batch.asker.session
            )
            running = TURN_RUNNING[callback.Status(state["status"])] if state else False
            selected = routes.select_routes(
                body["allowed_routes"],
                running=running,
                codex_thread=body["codex_thread"],
            )
            result["send"] = m.SendOutcome.NOT_SENT_ROUTE_REFUSED
            for route in selected:
                # Commit uncertainty before any external call. Even a broken
                # database after typing must never leave PREPARED evidence.
                self.store.finish_box_note(
                    body["delivery_id"],
                    body["attempt"],
                    request["plan_id"],
                    m.BoxNoteState.UNKNOWN,
                )
                result["route_used"] = route
                dispatched = self.runner.send(
                    route, body, lambda: self.fence(request, body)
                )
                if dispatched.outcome is routes.RouteOutcome.REFUSED:
                    continue
                if dispatched.outcome is routes.RouteOutcome.FENCED:
                    result["send"] = dispatched.fence
                elif dispatched.outcome is routes.RouteOutcome.UNKNOWN:
                    result["send"] = m.SendOutcome.SEND_UNKNOWN
                else:
                    deadline = time.monotonic() + body["echo_wait_s"]
                    while True:
                        current = self.box_note(body["delivery_id"])
                        if (
                            current is not None
                            and current.state is m.BoxNoteState.ECHOED
                        ):
                            result["send"] = m.SendOutcome.SENT_ECHOED
                            break
                        # PR-3's hook stores normalized prompt digests; reading
                        # these rows also permits echo recovery after a crash.
                        echo = self.store.db.execute(
                            "SELECT 1 FROM lifecycle WHERE actor=? AND kind=? AND prompt_digest=? AND at>=?",
                            (
                                body["target_actor"],
                                m.LifecycleKind.PROMPT_SUBMITTED,
                                m.body_digest(body["text"]),
                                prepared_at.isoformat(),
                            ),
                        ).fetchone()
                        if echo:
                            result["send"] = m.SendOutcome.SENT_ECHOED
                            break
                        if time.monotonic() >= deadline:
                            result["send"] = m.SendOutcome.SENT_NO_ECHO
                            break
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                break
            note_state = {
                m.SendOutcome.SENT_ECHOED: m.BoxNoteState.ECHOED,
                m.SendOutcome.SENT_NO_ECHO: m.BoxNoteState.SENT,
                m.SendOutcome.SEND_UNKNOWN: m.BoxNoteState.UNKNOWN,
            }.get(result["send"], m.BoxNoteState.NOT_SENT)
            self.store.finish_box_note(
                body["delivery_id"], body["attempt"], request["plan_id"], note_state
            )
            if result["send"] in SENT:
                # A concurrently recorded human takeover may already have
                # cancelled the batch. Never reopen that terminal state.
                with self.store.transaction():
                    current = self.store.get_batch(body["batch_id"])
                    if current.state in m.SEND_DELIVERABLE:
                        self.store.transition(
                            body["batch_id"], m.BatchState.BOX_ANSWERED
                        )
            current = self.store.get_batch(body["batch_id"])
            result["mac_batch_rev"], result["mac_state"] = current.rev, current.state

    def reconcile(
        self,
        target: m.ReconcileTargetKind,
        identifier: str,
        verdict: m.ReconcileVerdict,
    ) -> None:
        if target is m.ReconcileTargetKind.LOCAL_OUTBOUND:
            self.store.reconcile(identifier, verdict)
            return
        note = self.box_note(identifier)
        if note is None:
            raise ValueError("unknown box delivery")
        with self.worker_lock(str(note.batch)) as acquired:
            if not acquired:
                raise BlockingIOError("batch busy")
            self.store.finish_box_note(
                identifier,
                note.attempt,
                note.plan_id,
                m.BoxNoteState.ECHOED
                if verdict is m.ReconcileVerdict.SEEN_IN_PANE
                else m.BoxNoteState.NOT_SENT,
            )

    def run_plan(self, request: m.Plan) -> m.Result:
        request = decode_plan(encode(request))
        prior = self.store.db.execute(
            "SELECT result_b64 FROM plan_seen WHERE plan_id=?", (request["plan_id"],)
        ).fetchone()
        result = empty_result(request, self.clock())
        # Acquire before publishing plan_seen, and hold through the durable
        # result write. An overlapping UNKNOWN must not release the box lease
        # while the original invocation can still start another keystroke.
        with self._file_lock(f"qe-plan-{request['plan_id']}") as guard:
            if guard is None:
                raise BlockingIOError("plan still executing; no result")
            current = self.store.db.execute(
                "SELECT result_b64 FROM plan_seen WHERE plan_id=?",
                (request["plan_id"],),
            ).fetchone()
            if current is not None:
                if current[0] is None or (
                    prior is not None
                    and prior[0] is None
                    and request["action"] is m.PlanAction.SEND
                ):
                    # Every route subprocess inherits this guard. Acquisition
                    # therefore proves the original and any route child have
                    # stopped typing, including after an owner crash.
                    if request["action"] is m.PlanAction.SEND:
                        result["send"] = m.SendOutcome.SEND_UNKNOWN
                    else:
                        result["ok"], result["error_kind"] = (
                            False,
                            m.RunPlanError.BAD_PLAN,
                        )
                else:
                    encoded = current[0]
                    result = m.parse_result(
                        json.loads(
                            base64.urlsafe_b64decode(
                                encoded + "=" * (-len(encoded) % 4)
                            )
                        )
                    )
                result["replayed"] = True
                result["mac_now"] = self.clock().isoformat(timespec="microseconds")
                return seal(result)
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO plan_seen VALUES(?,?,?,?,?)",
                    (
                        request["plan_id"],
                        request["action"],
                        request["plan_digest"],
                        None,
                        self.clock().isoformat(),
                    ),
                )
            previous_fds = self.runner.pass_fds
            self.runner.pass_fds = (*previous_fds, guard)
            try:
                try:
                    return self._execute_plan(request, result)
                except Exception:
                    if result["route_used"] is None:
                        raise
                    # Includes result persistence and a failed recovery write.
                    # The pre-route UNKNOWN note already fences later plans.
                    self._send_unknown(request, result)
                    result["mac_now"] = self.clock().isoformat(timespec="microseconds")
                    return seal(result)
            finally:
                self.runner.pass_fds = previous_fds

    def _send_unknown(self, request: m.Plan, result: m.Result) -> None:
        result["send"] = m.SendOutcome.SEND_UNKNOWN
        body = cast(m.SendBody, request["body"])
        try:
            with self.worker_lock(body["batch_id"]) as acquired:
                if acquired:
                    self.store.finish_box_note(
                        body["delivery_id"],
                        body["attempt"],
                        request["plan_id"],
                        m.BoxNoteState.UNKNOWN,
                    )
                    with self.store.transaction():
                        current = self.store.get_batch(body["batch_id"])
                        if current.state in m.SEND_DELIVERABLE:
                            self.store.transition(
                                body["batch_id"], m.BatchState.BOX_ANSWERED
                            )
                    current = self.store.get_batch(body["batch_id"])
                    result["mac_batch_rev"], result["mac_state"] = (
                        current.rev,
                        current.state,
                    )
        except Exception:
            # A continuing storage fault must not hide the SEND_UNKNOWN result.
            # PREPARED was committed as UNKNOWN before invoking the route.
            logging.getLogger(__name__).exception(
                "could not persist SEND_UNKNOWN recovery"
            )

    def _execute_plan(self, request: m.Plan, result: m.Result) -> m.Result:
        try:
            action = request["action"]
            if action is m.PlanAction.SEND:
                self._send(request, result)
            elif action is m.PlanAction.ACK:
                self.store.ack_registration(cast(m.AckBody, request["body"]))
            elif action is m.PlanAction.CLEAR_TAKEOVER:
                clear = cast(m.ClearBody, request["body"])
                self.store.clear_takeover(clear["takeover_id"], clear["by"])
            elif action is m.PlanAction.RECONCILE:
                reconcile = cast(m.ReconcileBody, request["body"])
                self.reconcile(
                    reconcile["target"], reconcile["target_id"], reconcile["verdict"]
                )
            elif action is m.PlanAction.CANCEL:
                self.store.cancel_local(
                    cast(m.CancelBody, request["body"])["batch_id"], from_box=True
                )
            elif action is m.PlanAction.SYNC:
                result["payload"] = self.sync(cast(m.SyncBody, request["body"]), result)
        except Exception as error:
            if (
                request["action"] is m.PlanAction.SEND
                and result["route_used"] is not None
            ):
                self._send_unknown(request, result)
            else:
                if not isinstance(error, (ValueError, KeyError, OSError)):
                    raise
                result["ok"], result["error_kind"] = False, m.RunPlanError.BAD_PLAN
                result["send"], result["payload"], result["takeover_id"] = (
                    None,
                    None,
                    None,
                )
        result["mac_now"] = self.clock().isoformat(timespec="microseconds")
        result = seal(result)
        encoded = encode(result)
        payload = result["payload"]
        item_count = (
            sum(
                len(items)
                for items in (
                    payload["registrations"],
                    payload["progress"],
                    payload["status"]["batches"],
                    payload["status"]["takeovers"],
                    payload["status"]["notices"],
                    payload["plan_results"],
                )
            )
            if payload is not None
            else 0
        )
        limit = m.MAX_SYNC_SINGLE_B64 if item_count == 1 else m.MAX_RESULT_B64
        if len(encoded) > limit:
            raise ValueError("result exceeds transport bound")
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE plan_seen SET result_b64=? WHERE plan_id=?",
                (encoded, request["plan_id"]),
            )
        return result

    def sync(
        self, body: m.SyncBody, result: m.Result, *, include_status: bool = True
    ) -> m.SyncPayload:
        # Keep every listing and its cursor lookup in the same snapshot. Retention
        # must not remove a listed row before its rowid is read.
        with self.store.transaction():
            return self._sync(body, result, include_status=include_status)

    def _sync(
        self, body: m.SyncBody, result: m.Result, *, include_status: bool
    ) -> m.SyncPayload:
        status = self.status(body["tasks"][0] if len(body["tasks"]) == 1 else None)
        if body["tasks"]:
            allowed = {
                str(batch.id)
                for batch in self.store.batches()
                if batch.asker.task in body["tasks"]
            }
            status["batches"] = [
                item for item in status["batches"] if item["batch_id"] in allowed
            ]
            status["takeovers"] = [
                item for item in status["takeovers"] if item["batch_id"] in allowed
            ]
            batchless_notices = {
                row[0]
                for row in self.store.db.execute(
                    "SELECT id FROM notice WHERE task IN ("
                    + ",".join("?" for _ in body["tasks"])
                    + ") AND batch IS NULL",
                    body["tasks"],
                )
            }
            status["notices"] = [
                item
                for item in status["notices"]
                if item["batch_id"] in allowed or item["id"] in batchless_notices
            ]
        payload: m.SyncPayload = {
            "registrations": [],
            "progress": [],
            "status": {
                "schema": m.STATUS_SCHEMA,
                "task_id": status["task_id"],
                "batches": [],
                "takeovers": [],
                "notices": [],
            },
            "plan_results": [],
            "more": False,
            "cursor": None,
            "oversized": [],
        }

        # Keyset cursors survive ACKs removing due rows between pages.
        def rowid(
            table: PageTable,
            identifier: str,
            rev: int | None = None,
            seq: int | None = None,
        ) -> int:
            if table is PageTable.REGISTRATION:
                query, args = (
                    "SELECT rowid FROM registration WHERE batch=? AND batch_rev=?",
                    (identifier, rev),
                )
            elif table is PageTable.PROGRESS:
                query, args = (
                    "SELECT rowid FROM progress WHERE batch=? AND batch_rev=? AND seq=?",
                    (identifier, rev, seq),
                )
            else:
                name = {
                    PageTable.BATCH: "batch",
                    PageTable.TAKEOVER: "takeover",
                    PageTable.NOTICE: "notice",
                    PageTable.PLAN_SEEN: "plan_seen",
                }[table]
                column = "plan_id" if table is PageTable.PLAN_SEEN else "id"
                query, args = (
                    f"SELECT rowid FROM {name} WHERE {column}=?",
                    (identifier,),
                )
            return self.store.db.execute(query, args).fetchone()[0]

        items = page_items(
            payload["registrations"],
            list(self.store.due_registrations()),
            lambda item: item["batch_id"],
            PageTable.REGISTRATION,
            lambda item: rowid(
                PageTable.REGISTRATION, item["batch_id"], item["batch_rev"]
            ),
        )
        items += page_items(
            payload["progress"],
            list(self.store.due_progress()),
            lambda item: item["batch_id"],
            PageTable.PROGRESS,
            lambda item: rowid(
                PageTable.PROGRESS, item["batch_id"], item["batch_rev"], item["seq"]
            ),
        )
        if include_status:
            items += page_items(
                payload["status"]["batches"],
                status["batches"],
                lambda item: item["batch_id"],
                PageTable.BATCH,
                lambda item: rowid(PageTable.BATCH, item["batch_id"]),
            )
            items += page_items(
                payload["status"]["takeovers"],
                status["takeovers"],
                lambda item: item["batch_id"],
                PageTable.TAKEOVER,
                lambda item: rowid(PageTable.TAKEOVER, item["id"]),
            )
            items += page_items(
                payload["status"]["notices"],
                status["notices"],
                lambda item: item["batch_id"] or item["id"],
                PageTable.NOTICE,
                lambda item: rowid(PageTable.NOTICE, item["id"]),
            )
            items += page_items(
                payload["plan_results"],
                self.seen_results(body["plan_ids"]),
                lambda item: item["plan_id"],
                PageTable.PLAN_SEEN,
                lambda item: rowid(PageTable.PLAN_SEEN, item["plan_id"]),
            )
        cursor = None
        if body["cursor"] is not None:
            kind, position = body["cursor"].split(":")
            cursor = (PageTable(int(kind)), int(position))
            if cursor[1] < 1:
                raise ValueError("invalid cursor")
        items.sort(key=lambda item: item.key)
        items = [item for item in items if cursor is None or item.key > cursor]
        count = 0
        previous = body["cursor"]
        for index, item in enumerate(items):
            item.append()
            payload["more"], payload["cursor"] = (
                index + 1 < len(items),
                f"{item.key[0]}:{item.key[1]}" if index + 1 < len(items) else None,
            )
            candidate = result.copy()
            candidate["payload"] = payload
            if len(encode(seal(candidate))) > m.MAX_RESULT_B64:
                if count:
                    item.pop()
                    payload["more"], payload["cursor"] = True, previous
                    break
                if len(encode(seal(candidate))) > m.MAX_SYNC_SINGLE_B64:
                    item.pop()
                    payload["oversized"].append(item.batch_id)
                break
            count += 1
            previous = f"{item.key[0]}:{item.key[1]}"
        return payload

    def registrations(self, cursor: str | None = None) -> RegistrationsPage:
        request = empty_result(
            cast(
                m.Plan,
                {
                    "plan_id": str(UUID(int=0)),
                    "plan_digest": "0" * 64,
                    "lease_id": None,
                    "action": m.PlanAction.SYNC,
                },
            ),
            self.clock(),
        )
        page = self.sync(
            {"tasks": [], "cursor": cursor, "plan_ids": []},
            request,
            include_status=False,
        )
        return {
            "registrations": page["registrations"],
            "progress": page["progress"],
            "more": page["more"],
            "cursor": page["cursor"],
        }

    def worker(self, batch_id: str, *, spool_digest: bool = True) -> m.QuestionBatch:
        """Phase 3 has no classifier or local dispatcher. Register immediately."""
        with self.worker_lock(batch_id, wait=0) as acquired:
            if not acquired:
                return self.store.get_batch(batch_id)
            with self.store.transaction():
                batch = self.store.get_batch(batch_id)
                if batch.state in ledger.TERMINAL:
                    return batch
                entry = prepare_registration(self.store, batch)
                if entry is not None:
                    # MAC_GUARD is produced by rechecking immutable content against
                    # the current guard table on recovery/retry of the same revision.
                    policy = m.BriefEscalation(
                        entry["brief_project"] or m.BriefProject.PERSONAL,
                        (),
                        tuple(entry["brief_ask_carlos"]),
                    )
                    classification = guard.derive_floor(
                        batch.questions,
                        classification=batch.classification,
                        brief=policy,
                        cwd=entry["cwd"],
                        remote=entry["remote"],
                        previous_floor=batch.classification.floor,
                        secret_present=entry["secret_present"],
                    )
                    raised = classification.floor > batch.classification.floor
                    previous = self.store.db.execute(
                        "SELECT escalated FROM batch WHERE id=?", (batch_id,)
                    ).fetchone()[0]
                    if raised or previous is None:
                        with self.store.transaction():
                            self.store.db.execute(
                                "UPDATE batch SET mac_floor=max(mac_floor,?),classification_json=?,escalated=? WHERE id=?",
                                (
                                    classification.floor,
                                    ledger.encoded(classification),
                                    m.EscalationReason.NO_ANSWERER,
                                    batch_id,
                                ),
                            )
                            seq = self.store.db.execute(
                                "SELECT coalesce(max(seq),0)+1 FROM progress WHERE batch=?",
                                (batch_id,),
                            ).fetchone()[0]
                            self.store.progress(
                                m.RegisterProgress(
                                    schema=m.PROGRESS_SCHEMA,
                                    batch_id=batch_id,
                                    batch_rev=batch.rev,
                                    seq=seq,
                                    escalated=m.EscalationReason.NO_ANSWERER,
                                    recommendations=[],
                                    local_resolution=None,
                                    local_level=None,
                                    floor_raise=m.FloorRaise(
                                        floor=classification.floor,
                                        cause=m.FloorRaiseCause.MAC_GUARD,
                                        classification=None,
                                    )
                                    if raised
                                    else None,
                                )
                            )
                    if batch.state is not m.BatchState.BLOCKED:
                        self.store.take_ownership(batch_id, classification.floor)
                task = batch.asker.task
        if spool_digest:
            # Coalesce across task siblings without holding the per-batch SEND lock.
            time.sleep(DIGEST_COALESCE.total_seconds())
            self.digest(task)
        return self.store.get_batch(batch_id)

    def digest_envelope(
        self,
        identifier: str,
        task: str,
        entries: list[m.RegisterEntry],
        progress: list[m.RegisterProgress],
        notices: list[m.DigestNotice],
        created: str,
        metadata: m.DigestMetadata | None = None,
    ) -> m.DigestEnvelope:
        cached = metadata or m.DigestMetadata(
            requester=entries[0]["callback_bot"] if entries else "",
            repo=entries[0]["cwd"] if entries else "",
            branch="",
        )
        body = m.DigestPayload(
            schema=m.DIGEST_SCHEMA,
            event=m.Event.NEEDS_INPUT,
            kind=m.InputKind.QUESTION_DIGEST,
            digest_id=identifier,
            task_id=task,
            requester=cached["requester"],
            repo=cached["repo"],
            branch=cached["branch"],
            register=entries,
            progress=progress,
            notices=notices,
            untrusted=list(m.DIGEST_UNTRUSTED_FIELDS),
            delivery_key=m.digest([m.DIGEST_SCHEMA, identifier]),
        )
        return m.DigestEnvelope(
            event=m.Event.NEEDS_INPUT,
            kind=m.InputKind.QUESTION_DIGEST,
            digest_id=identifier,
            task_id=task,
            requester=body["requester"],
            agent="",
            turn="",
            rex_session="",
            rex_block="",
            finished_at=created,
            body=body,
        )

    def digest_metadata(
        self, entries: list[m.RegisterEntry], notices: list[m.DigestNotice]
    ) -> m.DigestMetadata:
        if entries:
            entry = entries[0]
            metadata = callback.git_metadata(
                self.store.get_batch(entry["batch_id"]).asker.cwd
            )
            return m.DigestMetadata(
                requester=entry["callback_bot"],
                repo=metadata["repo"],
                branch=metadata["branch"],
            )
        for notice in notices:
            row = self.store.db.execute(
                "SELECT metadata_json FROM notice WHERE id=?", (notice["id"],)
            ).fetchone()
            if row and row[0]:
                return m.parse_record(m.DigestMetadata, json.loads(row[0]))
        for notice in notices:
            if notice["batch_id"]:
                batch = self.store.get_batch(notice["batch_id"])
                metadata = callback.git_metadata(batch.asker.cwd)
                return m.DigestMetadata(
                    requester=batch.asker.callback_bot,
                    repo=metadata["repo"],
                    branch=metadata["branch"],
                )
        # Legacy notices lack source metadata. Unknown is evidence, never a
        # reason to borrow another session's callback identity.
        return m.DigestMetadata(requester="", repo="", branch="")

    def digest(self, task: str) -> tuple[Path, ...]:
        """Durable IDs, bounded frames, retry timing and once-spooled notices."""
        with self._file_lock("qe-digest-" + m.digest(task), wait=0) as acquired:
            return self._digest_locked(task) if acquired is not None else ()

    def _digest_locked(self, task: str) -> tuple[Path, ...]:
        paths: list[Path] = []
        # Digest rows are committed before spooling. Rebuild a lost spool using
        # exactly the frozen row, including its key and timestamp.
        for row in self.store.db.execute("SELECT * FROM digest WHERE task=?", (task,)):
            envelope = self.digest_envelope(
                row["id"],
                task,
                json.loads(row["register_json"]),
                json.loads(row["progress_json"]),
                json.loads(row["notices_json"]),
                row["spooled_at"],
                m.parse_record(m.DigestMetadata, json.loads(row["metadata_json"]))
                if row["metadata_json"]
                else None,
            )
            key = envelope["body"]["delivery_key"]
            sent_marker = self.store.path.parent / ("grokbot-sent-" + key)
            if (
                not (self.store.path.parent / ("grokbot-pending-" + key)).exists()
                and not sent_marker.exists()
            ):
                path = callback.spool(envelope)
                if path is not None:
                    paths.append(path)
            self.account_digest(
                envelope["body"]["register"],
                envelope["body"]["notices"],
                row["spooled_at"],
            )
            if sent_marker.exists():
                with self.store.transaction():
                    self.store.db.execute("DELETE FROM digest WHERE id=?", (row["id"],))
        entries = [
            entry
            for entry in self.store.due_registrations()
            if entry["task_id"] == task and self.registration_retry_due(entry)
        ]
        keys = {(entry["batch_id"], entry["batch_rev"]) for entry in entries}
        progress = [
            item
            for item in self.store.due_progress()
            if (item["batch_id"], item["batch_rev"]) in keys
        ]
        notices = [
            m.DigestNotice(
                id=row["id"],
                kind=m.NoticeKind(row["kind"]),
                batch_id=row["batch"],
                detail=row["detail"],
                created_at=row["created_at"],
            )
            for row in self.store.db.execute(
                "SELECT * FROM notice WHERE task=? AND spooled=0 ORDER BY rowid",
                (task,),
            )
        ]
        while entries or progress or notices:
            identifier, created = str(uuid4()), self.clock().isoformat()
            metadata = self.digest_metadata(entries, notices)
            included_entries: list[m.RegisterEntry] = []
            included_progress: list[m.RegisterProgress] = []
            included_notices: list[m.DigestNotice] = []
            envelope = self.digest_envelope(
                identifier,
                task,
                included_entries,
                included_progress,
                included_notices,
                created,
                metadata,
            )

            def pack(source: list[T], destination: list[T]) -> None:
                while source:
                    destination.append(source[0])
                    envelope = self.digest_envelope(
                        identifier,
                        task,
                        included_entries,
                        included_progress,
                        included_notices,
                        created,
                        metadata,
                    )
                    if len(m.canonical(envelope)) > DIGEST_MAX:
                        destination.pop()
                        break
                    source.pop(0)

            while entries:
                entry = entries[0]
                related = [
                    item
                    for item in progress
                    if (item["batch_id"], item["batch_rev"])
                    == (entry["batch_id"], entry["batch_rev"])
                ]
                included_entries.append(entry)
                included_progress.extend(related)
                envelope = self.digest_envelope(
                    identifier,
                    task,
                    included_entries,
                    included_progress,
                    included_notices,
                    created,
                    metadata,
                )
                if len(m.canonical(envelope)) > DIGEST_MAX:
                    included_entries.pop()
                    for item in related:
                        included_progress.remove(item)
                    break
                entries.pop(0)
                progress = [item for item in progress if item not in related]
            pack(notices, included_notices)
            if not (included_entries or included_progress or included_notices):
                raise ValueError("digest item exceeds wire limit")
            envelope = self.digest_envelope(
                identifier,
                task,
                included_entries,
                included_progress,
                included_notices,
                created,
                metadata,
            )
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO digest(id,task,register_json,progress_json,notices_json,spooled_at,metadata_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        task,
                        ledger.encoded(included_entries),
                        ledger.encoded(included_progress),
                        ledger.encoded(included_notices),
                        created,
                        ledger.encoded(metadata),
                    ),
                )
            path = callback.spool(envelope)
            self.account_digest(included_entries, included_notices, created)
            if path is not None:
                paths.append(path)
        return tuple(paths)

    def account_digest(
        self,
        entries: list[m.RegisterEntry],
        notices: list[m.DigestNotice],
        created: str,
    ) -> None:
        """Complete the saved frame's accounting once, including crash recovery."""
        at = datetime.fromisoformat(created)
        with self.store.transaction():
            for entry in entries:
                row = self.store.db.execute(
                    "SELECT last_sent_at FROM registration WHERE batch=? AND batch_rev=?",
                    (entry["batch_id"], entry["batch_rev"]),
                ).fetchone()
                if row and (row[0] is None or datetime.fromisoformat(row[0]) < at):
                    self.store.registration_sent(
                        entry["batch_id"], entry["batch_rev"], at=at
                    )
            for notice in notices:
                self.store.db.execute(
                    "UPDATE notice SET spooled=1 WHERE id=?", (notice["id"],)
                )

    def registration_retry_due(self, entry: m.RegisterEntry) -> bool:
        row = self.store.db.execute(
            "SELECT last_sent_at,overdue FROM registration WHERE batch=? AND batch_rev=?",
            (entry["batch_id"], entry["batch_rev"]),
        ).fetchone()
        interval = timedelta(hours=1) if row["overdue"] else m.REGISTER_RETRY
        return (
            row["last_sent_at"] is None
            or datetime.fromisoformat(row["last_sent_at"]) <= self.clock() - interval
        )

    def tick(self) -> bool:
        fd = os.open(
            self.store.path.parent / m.TICK_LOCK, os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            at = self.clock()
            previous = self.store.db.execute(
                "SELECT last_run_at FROM tick_state WHERE id=1"
            ).fetchone()
            if previous and datetime.fromisoformat(previous[0]) > at - timedelta(
                seconds=60
            ):
                return False
            self.store.ingest_events(self.store.path.parent / m.EVENTS_DIR)
            self.store.recover_claims(at)
            for batch in self.store.batches():
                if batch.state not in ledger.TERMINAL and not self.store.session_alive(
                    batch.asker.actor
                ):
                    self.store.transition(str(batch.id), m.BatchState.STALE)
            self.store.retain(at)
            if os.environ.get(m.FLAG_ENV) == "1":
                for batch in self.store.batches():
                    if batch.state in ledger.OPEN_FOR_ATTACH:
                        self.worker(str(batch.id), spool_digest=False)
                tasks = (
                    {
                        entry["task_id"]
                        for entry in self.store.due_registrations()
                        if self.registration_retry_due(entry)
                    }
                    | {
                        row[0]
                        for row in self.store.db.execute(
                            "SELECT DISTINCT task FROM notice WHERE spooled=0"
                        )
                    }
                    | {
                        row[0]
                        for row in self.store.db.execute(
                            "SELECT DISTINCT task FROM digest"
                        )
                    }
                )
                for task in tasks:
                    oldest = self.store.db.execute(
                        "SELECT min(due_at) FROM (SELECT b.updated_at AS due_at FROM batch b JOIN registration r ON r.batch=b.id AND r.batch_rev=b.rev WHERE b.task=? AND (r.state<>? OR r.progress_seq>r.acked_progress_seq) UNION ALL SELECT created_at FROM notice WHERE task=? AND spooled=0 UNION ALL SELECT spooled_at FROM digest WHERE task=?)",
                        (task, m.RegistrationState.ACKED, task, task),
                    ).fetchone()[0]
                    if (
                        oldest
                        and datetime.fromisoformat(oldest) <= at - DIGEST_COALESCE
                    ):
                        self.digest(task)
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO tick_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET last_run_at=excluded.last_run_at",
                    (at.isoformat(),),
                )
            return True
        finally:
            os.close(fd)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    run = commands.add_parser(Command.RUN_PLAN)
    run.add_argument("--b64", required=True)
    status = commands.add_parser(Command.STATUS)
    status.add_argument("--task")
    status.add_argument("--batch")
    status.add_argument("--json", action="store_true", required=True)
    registrations = commands.add_parser(Command.REGISTRATIONS)
    registrations.add_argument("--due", action="store_true", required=True)
    registrations.add_argument("--cursor")
    registrations.add_argument("--json", action="store_true", required=True)
    clear = commands.add_parser(Command.CLEAR_TAKEOVER)
    clear.add_argument("--takeover", required=True)
    clear.add_argument("--by", required=True)
    reconcile = commands.add_parser(Command.RECONCILE)
    reconcile.add_argument("--outbound", required=True)
    reconcile.add_argument("--verdict", choices=list(m.ReconcileVerdict), required=True)
    cancel = commands.add_parser(Command.CANCEL_LOCAL)
    cancel.add_argument("--batch", required=True)
    ack = commands.add_parser(Command.ACK)
    ack.add_argument("--outbound", required=True)
    ack.add_argument("--token-stdin", action="store_true", required=True)
    audit = commands.add_parser(Command.AUDIT)
    audit.add_argument("--task", required=True)
    commands.add_parser(Command.TICK)
    worker = commands.add_parser(Command.WORKER)
    worker.add_argument("--batch", required=True)
    return cli


def bad_plan_result(value: str, error: ValueError) -> m.Result | None:
    try:
        if len(value) > m.MAX_PLAN_B64 or not B64_PATTERN.fullmatch(value):
            return None
        raw = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        UUID(raw["plan_id"])
        action = m.PlanAction(raw["action"])
        request = cast(
            m.Plan,
            {
                "plan_id": raw["plan_id"],
                "action": action,
                "lease_id": raw.get("lease_id"),
                "plan_digest": raw.get("plan_digest", ""),
            },
        )
        result = empty_result(request, utc_now())
        result["ok"] = False
        result["error_kind"] = (
            m.RunPlanError.DIGEST_MISMATCH
            if str(error) == "plan_digest mismatch"
            else m.RunPlanError.BAD_PLAN
        )
        return seal(result)
    except (ValueError, TypeError, KeyError, binascii.Error, RecursionError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    command = Command(args.command)
    request = None
    if command is Command.RUN_PLAN:
        try:
            request = decode_plan(args.b64)
        except ValueError as error:
            failed = bad_plan_result(args.b64, error)
            if failed is not None and len(encode(failed)) <= m.MAX_RESULT_B64:
                print(m.RESULT_PREFIX + encode(failed))
            return 64
    with ledger.Ledger(callback.rex.STATE / m.LEDGER_FILE) as store:
        broker = Broker(store)
        try:
            if command is Command.RUN_PLAN:
                assert request is not None
                print(m.RESULT_PREFIX + encode(broker.run_plan(request)))
            elif command is Command.STATUS:
                print(ledger.encoded(broker.status(args.task, args.batch)))
            elif command is Command.REGISTRATIONS:
                print(ledger.encoded(broker.registrations(args.cursor)))
            elif command is Command.CLEAR_TAKEOVER:
                store.clear_takeover(args.takeover, args.by)
            elif command is Command.RECONCILE:
                broker.reconcile(
                    m.ReconcileTargetKind.LOCAL_OUTBOUND,
                    args.outbound,
                    m.ReconcileVerdict(args.verdict),
                )
            elif command is Command.CANCEL_LOCAL:
                store.cancel_local(args.batch)
            elif command is Command.ACK:
                if not store.ack(args.outbound, sys.stdin.read(4097).strip()):
                    return 3
            elif command is Command.AUDIT:
                for event in store.audit_events(args.task):
                    print(
                        f"{event.at.isoformat()}\t{event.kind}\t{event.batch or '-'}\t{event.detail}"
                    )
            elif command is Command.TICK:
                broker.tick()
            elif command is Command.WORKER:
                if os.environ.get(m.FLAG_ENV) == "1":
                    broker.worker(args.batch)
        except BlockingIOError:
            return 4
        except (ValueError, KeyError):
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
