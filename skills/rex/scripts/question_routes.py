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
from typing import Callable, Sequence

import question_model as m

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
                    "-s",
                    session,
                    "-b",
                    rex_target(block),
                    "--trim",
                    "--unwrap",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=ROUTE_TIMEOUT,
                pass_fds=self.pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def send(
        self,
        route: m.Route,
        body: m.SendBody,
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
                return RouteResult(RouteOutcome.UNKNOWN, None, route)
            args = ["codex", "queue", "--thread", body["codex_thread"]]
            if route is m.Route.CODEX_QUEUE_RESUME:
                args.append("--resume")
            args.extend(["--message", body["text"]])
            return self._call(route, args)

        if route is not m.Route.REX_SEND:
            return RouteResult(RouteOutcome.UNKNOWN, None, route)

        args = [
            "rex",
            "send",
            "-s",
            body["rex_session"],
            "-b",
            rex_target(body["rex_block"]),
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
