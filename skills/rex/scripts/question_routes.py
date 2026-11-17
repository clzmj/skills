#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""CLI routing for question delivery.

Route failures are conservative: the current probe evidence does not establish
any negative pre-acceptance error signature, so every nonzero exit is unknown.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence, TypedDict

import question_model as m
import grokbot_callback as callback

ROUTE_TIMEOUT = m.ROUTE_TIMEOUT.total_seconds()


class RouteOutcome(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class RouteResult:
    outcome: RouteOutcome
    fence: m.SendOutcome | None
    route: m.Route


class RouteBody(TypedDict):
    """Transport fields shared by local intents and box SEND plans."""

    codex_thread: str | None
    rex_session: str
    rex_block: str
    text: str


def relay_matches(asker: m.AskerRef, target: str, thread: str | None) -> bool:
    """Re-resolve the child root; cached or plan-supplied addresses grant nothing."""
    if asker.role is not m.ActorRole.NATIVE_SUBAGENT:
        return True
    state = callback.actor_state(asker.actor)
    if state is None:
        return False
    root = callback.session_lineage(state)["root_session_id"]
    actor = callback.actor_key(asker.agent, root)
    root_state = callback.actor_state(actor)
    return (
        target == asker.root_actor == actor
        and (asker.agent is not m.AgentKind.CODEX or thread == root)
        and root_state is not None
        and root_state["status"] != callback.Status.ENDED
    )


def rex_target(block: str) -> str:
    """Convert stored block ids to the target syntax accepted by Rex."""
    return block if block.startswith("block:") else f"block:{block}"


def select_routes(
    allowed: Sequence[m.Route], *, running: bool, codex_thread: str | None
) -> tuple[m.Route, ...]:
    """Select the turn-state route first, followed by permitted fallbacks."""
    if running and codex_thread is not None:
        preferred = m.Route.CODEX_QUEUE
    elif not running and codex_thread is not None:
        preferred = m.Route.CODEX_QUEUE_RESUME
    else:
        preferred = m.Route.REX_SEND
    ordered = [preferred]
    if preferred is not m.Route.REX_SEND:
        ordered.append(m.Route.REX_SEND)
    return tuple(route for route in ordered if route in allowed)


class RouteRunner:
    def __init__(
        self, *, timeout: float = ROUTE_TIMEOUT, pass_fds: tuple[int, ...] = ()
    ):
        self.timeout = timeout
        self.pass_fds = pass_fds

    def capture(self, session: str, block: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "rex",
                    "capture",
                    f"--session={session}",
                    f"--block={rex_target(block)}",
                    "--trim",
                    "--unwrap",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                pass_fds=self.pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def send(
        self,
        route: m.Route,
        body: RouteBody,
        fence: Callable[[], m.SendOutcome | None],
    ) -> RouteResult:
        def check_fence(*, staged: bool = False) -> RouteResult | None:
            failure = fence()
            if failure is None:
                return None
            return RouteResult(
                RouteOutcome.UNKNOWN if staged else RouteOutcome.FENCED,
                failure,
                route,
            )

        checked = check_fence()
        if checked:
            return checked

        if route in (m.Route.CODEX_QUEUE, m.Route.CODEX_QUEUE_RESUME):
            if not body["codex_thread"]:
                return RouteResult(RouteOutcome.REFUSED, None, route)
            args = ["codex", "queue", f"--thread={body['codex_thread']}"]
            if route is m.Route.CODEX_QUEUE_RESUME:
                args.append("--resume")
            args.append(f"--message={body['text']}")
            return self._call(route, args)

        if route is not m.Route.REX_SEND:
            return RouteResult(RouteOutcome.UNKNOWN, None, route)

        args = [
            "rex",
            "send",
            f"--session={body['rex_session']}",
            f"--block={rex_target(body['rex_block'])}",
        ]
        try:
            text_result = subprocess.run(
                args,
                input=body["text"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                pass_fds=self.pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return RouteResult(RouteOutcome.UNKNOWN, None, route)
        if text_result.returncode != 0:
            # No recorded negative signatures currently justify refusal.
            return RouteResult(RouteOutcome.UNKNOWN, None, route)

        checked = check_fence(staged=True)
        if checked:
            return checked
        try:
            cr_result = subprocess.run(
                args,
                input="\r",
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                pass_fds=self.pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return RouteResult(RouteOutcome.UNKNOWN, None, route)
        return RouteResult(
            RouteOutcome.ACCEPTED
            if cr_result.returncode == 0
            else RouteOutcome.UNKNOWN,
            None,
            route,
        )

    def _call(self, route: m.Route, args: list[str]) -> RouteResult:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                pass_fds=self.pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return RouteResult(RouteOutcome.UNKNOWN, None, route)
        # Negative pre-acceptance signatures remain unproven (docs evidence §7.8).
        return RouteResult(
            RouteOutcome.ACCEPTED if result.returncode == 0 else RouteOutcome.UNKNOWN,
            None,
            route,
        )
