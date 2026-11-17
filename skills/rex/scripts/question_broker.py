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
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from typing import Callable, Final, Generator, TypeVar, TypedDict, cast
from uuid import UUID

import grokbot_callback as callback
import question_ledger as ledger
import question_model as m
import question_routes as routes

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
            pass

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
        except BlockingIOError:
            return 4
        except (ValueError, KeyError):
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
