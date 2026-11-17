#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Mac-local L1/L2 and pending delivery. Intents never grant box authority."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import time
from typing import Generator
from uuid import UUID, uuid4

import grokbot_callback as callback
import question_ledger as ledger
import question_model as m
import question_routes as routes

INLINE = frozenset({m.Route.INLINE_DENY_REASON, m.Route.INLINE_UPDATED_INPUT})
ARRIVED = (m.BoxNoteState.SENT, m.BoxNoteState.ECHOED, m.BoxNoteState.UNKNOWN)
BOOT = uuid4()


@contextmanager
def file_lock(path: Path, *, wait: float = 0) -> Generator[int | None, None, None]:
    """Keep the descriptor open so route children can inherit the same guard."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield None
                    return
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield fd
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class ResolutionHead:
    rev: m.ResolutionRev
    batch_rev: m.BatchRev


@dataclass(frozen=True, slots=True)
class TakeoverBlock:
    id: m.TakeoverId
    origin: m.PromptOrigin


class Delivery:
    def __init__(
        self, store: ledger.Ledger, *, runner: routes.RouteRunner | None = None
    ):
        self.store = store
        self.runner = runner or routes.RouteRunner()

    def recover(self) -> int:
        return self.store.recover_claims()

    def wait_inline(
        self, batch: str, event: m.HookEvent, deadline: datetime, pid: int | None = None
    ) -> None:
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO inline_waiter VALUES(?,?,?,?) ON CONFLICT(batch,event) DO UPDATE SET hook_pid=excluded.hook_pid,deadline=excluded.deadline",
                (batch, event, pid or os.getpid(), deadline.isoformat()),
            )

    def _inline_owner(self, batch: str) -> int | None:
        for row in self.store.db.execute(
            "SELECT hook_pid FROM inline_waiter WHERE batch=? AND deadline>? ORDER BY deadline DESC",
            (batch, ledger.now().isoformat()),
        ):
            try:
                os.kill(row[0], 0)
                return row[0]
            except ProcessLookupError:
                continue
            except PermissionError:
                return row[0]  # Unknown liveness must not let a worker steal it.
        return None

    @contextmanager
    def batch_lock(self, batch: str) -> Generator[int | None, None, None]:
        # Same file as run-plan. Children inherit the guard so a killed parent
        # cannot expose a staged route to another sender.
        UUID(batch)
        with file_lock(
            self.store.path.parent / m.WORKER_LOCK.format(batch=batch)
        ) as fd:
            yield fd

    def dispatch(self, outbound: str, *, inline_pid: int | None = None) -> bool:
        message = self.store.get_outbound(outbound)
        if inline_pid is not None:
            # Hooks never acquire the worker lock. The claim arbitrates with it.
            return self._dispatch(outbound, inline_pid=inline_pid)
        with self.batch_lock(str(message.batch)) as guard:
            return guard is not None and self._dispatch(outbound, guard=guard)

    def dispatch_batch(self, batch: str) -> int:
        with self.batch_lock(batch) as guard:
            return self.dispatch_batch_locked(batch, guard) if guard is not None else 0

    def dispatch_batch_locked(self, batch: str, guard: int) -> int:
        """The worker already holds this batch's lock; do not acquire it again."""
        self.recover()
        return sum(
            self._dispatch(str(item.id), guard=guard)
            for item in self.store.outbounds(batch)
            if item.state in (m.DeliveryState.PENDING, m.DeliveryState.DISPATCH_FAILED)
        )

    def _check(
        self, claim: ledger.Claim, *, apply: bool = True
    ) -> m.SendOutcome | None:
        with self.store.transaction():
            message = self.store.get_outbound(claim.outbound)
            if (
                message.state is not m.DeliveryState.CLAIMED
                or message.claim_owner != claim.owner
            ):
                return m.SendOutcome.NOT_SENT_SUPERSEDED
            batch = self.store.get_batch(str(message.batch))
            if not self.store.session_alive(message.target_actor):
                if apply:
                    if batch.state not in ledger.TERMINAL:
                        self.store.transition(str(batch.id), m.BatchState.STALE)
                    self.store.stop_claim(claim, abandon=True)
                return m.SendOutcome.NOT_SENT_SESSION_GONE
            cause = None
            if batch.state in ledger.TERMINAL or batch.state is m.BatchState.BLOCKED:
                cause = m.SuppressCause.CANCELLED
            elif message.purpose is m.OutboundPurpose.ANSWER:
                if batch.state in (m.BatchState.BOX_OWNED, m.BatchState.BOX_ANSWERED):
                    cause = m.SuppressCause.BOX_OWNED
                else:
                    current = self.store.db.execute(
                        "SELECT rev,batch_rev FROM resolution WHERE batch=? ORDER BY rev DESC LIMIT 1",
                        (str(batch.id),),
                    ).fetchone()
                    head = (
                        m.parse_record(ResolutionHead, dict(current))
                        if current
                        else None
                    )
                    if head is None or head.rev != message.resolution_rev:
                        cause = m.SuppressCause.NEWER_RESOLUTION
                    elif head.batch_rev != batch.rev:
                        cause = m.SuppressCause.NEWER_REVISION
                    else:
                        live = self.store.live_context(str(batch.id))
                        if live.changed:
                            if apply:
                                self.store.bump_for_context(str(batch.id), live)
                            cause = m.SuppressCause.CONTEXT_CHANGED
            elif (
                batch.state is not m.BatchState.BOX_OWNED
                or self.store.db.execute(
                    "SELECT 1 FROM box_note WHERE batch=? AND state IN (?,?,?)",
                    (str(batch.id), *ARRIVED),
                ).fetchone()
            ):
                cause = m.SuppressCause.NEWER_RESOLUTION
            if cause:
                if apply:
                    self.store.stop_claim(claim, cause=cause)
                return m.SendOutcome.NOT_SENT_SUPERSEDED
            rows = self.store.db.execute(
                "SELECT id,origin FROM takeover WHERE batch=? AND cleared_at IS NULL ORDER BY at,id",
                (str(batch.id),),
            ).fetchall()
            takeovers = tuple(m.parse_record(TakeoverBlock, dict(row)) for row in rows)
            if any(item.origin is m.PromptOrigin.HUMAN for item in takeovers):
                if apply:
                    self.store.stop_claim(claim, cause=m.SuppressCause.HUMAN_TAKEOVER)
                return m.SendOutcome.NOT_SENT_CANCELLED
            if takeovers:
                if apply:
                    self.store.stop_claim(claim, paused_by=str(takeovers[0].id))
                return m.SendOutcome.NOT_SENT_PAUSED
        return None

    def _dispatch(
        self, outbound: str, *, inline_pid: int | None = None, guard: int | None = None
    ) -> bool:
        with self.store.transaction():
            message = self.store.get_outbound(outbound)
            batch = self.store.get_batch(str(message.batch))
            plan = message.route_plan
            if any(route in INLINE for route in plan):
                waiter = self._inline_owner(str(message.batch))
                if waiter is not None:
                    if inline_pid != waiter:
                        return False
                    plan = tuple(route for route in plan if route in INLINE)
                elif inline_pid is not None:
                    return False
                else:
                    # PreToolUse can start a worker before PermissionRequest
                    # records its waiter. Leave that hook the full inline window.
                    if (
                        batch.asker.agent is m.AgentKind.CLAUDE
                        and ledger.now() < batch.opened_at + m.INLINE_WAIT
                    ):
                        return False
                    # Pick the effective route under the claim transaction;
                    # persisted text, token and route intent remain immutable.
                    plan = tuple(route for route in plan if route not in INLINE)
            elif inline_pid is not None:
                return False
            if not plan:
                return False
            if (
                message.purpose is m.OutboundPurpose.ANSWER
                and plan[0] is m.Route.INLINE_DENY_REASON
            ):
                return False  # Recorded P5 did not establish answer adoption.
            target_session = (
                str(message.target_actor).partition(":")[2] or batch.asker.session
            )
            state = callback.session_state(batch.asker.agent, target_session)
            running = state is not None and state["status"] == callback.Status.RUNNING
            thread = target_session if batch.asker.agent is m.AgentKind.CODEX else None
            selected = (
                plan
                if plan[0] in INLINE
                else routes.select_routes(plan, running=running, codex_thread=thread)
            )
            if not selected:
                return False
            claim = self.store.claim(
                outbound, f"{os.getpid()}:{BOOT}:{uuid4()}", selected[0]
            )
        if claim is None or self._check(claim) is not None:
            return False
        if claim.route in INLINE:
            return self.store.finish_claim(claim, m.DeliveryState.DISPATCHED)
        body = routes.RouteBody(
            rex_session=state["rex_session"] if state else str(batch.asker.rex_session),
            rex_block=state["rex_block"] if state else str(batch.asker.rex_block),
            codex_thread=thread,
            text=message.text,
        )
        previous = self.runner.pass_fds
        self.runner.pass_fds = (*previous, guard) if guard is not None else previous
        try:
            # Route fences are read-only: after Rex staged text, a refusal is
            # uncertain and must not release the claim back to PENDING.
            result = self.runner.send(
                claim.route, body, lambda: self._check(claim, apply=False)
            )
        finally:
            self.runner.pass_fds = previous
        if result.outcome is routes.RouteOutcome.FENCED:
            self._check(claim)
            return False
        outcome = (
            m.DeliveryState.DISPATCHED
            if result.outcome is routes.RouteOutcome.ACCEPTED
            else m.DeliveryState.DISPATCH_FAILED
            if result.outcome is routes.RouteOutcome.REFUSED
            else m.DeliveryState.DISPATCH_UNCERTAIN
        )
        return (
            self.store.finish_claim(claim, outcome)
            and outcome is m.DeliveryState.DISPATCHED
        )
