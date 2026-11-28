#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Block bot and Rex Claude Code runs that have no valid Notion ticket record.

Dinesh verifies the ticket in Notion and writes
~/.local/state/agent-tickets/<GROKBOT_TASK_ID>.json. This only reads that file:
no network and no token. It is a guardrail, not a sandbox: a process running as
the same user can write the record.
"""
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from agent_types import HookEvent

ENFORCING_ENV = ("GROKBOT_CALLBACK", "GROKBOT_TASK_ID", "REX_BLOCK")
MAX_AGE_ENV = "GROKBOT_TICKET_MAX_AGE_HOURS"
DEFAULT_MAX_AGE_HOURS = 24.0
CLOCK_SKEW = timedelta(minutes=5)
VERIFIER = "dinesh"
NOTION_HOSTS = ("app.notion.com", "notion.so", "www.notion.so")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
PAGE_ID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


class TicketStatus(StrEnum):
    """Notion Issues Status values."""

    BACKLOG = "Backlog"
    READY = "Ready"
    IN_PROGRESS = "In progress"
    IN_REVIEW = "In review"
    BLOCKED = "Blocked"
    DONE = "Done"
    WONT_DO = "Won't do"


CLOSED = (TicketStatus.DONE, TicketStatus.WONT_DO)


class Refusal(IntEnum):
    """rex-run exit codes; 2 stays argparse's usage error."""

    BAD_TASK_ID = 3
    NO_RECORD = 4
    BAD_RECORD = 5
    WRONG_TASK = 6
    CLOSED_STATUS = 7
    STALE = 8
    NO_TICKET_ENV = 9
    TICKET_MISMATCH = 10
    BAD_TICKET = 11
    BAD_MODEL = 12
    BAD_PATH = 13
    REX_FAILED = 14


class TicketError(Exception):
    def __init__(self, refusal: Refusal, message: str):
        super().__init__(message)
        self.refusal = refusal


@dataclass(frozen=True, slots=True)
class Ticket:
    task_id: str
    id: str
    url: str
    status: TicketStatus
    verified_at: datetime


def page_id(value: str) -> str | None:
    """32 lowercase hex from a bare or dashed page id, or a Notion page URL."""
    value = value.strip()
    if PAGE_ID.fullmatch(value):
        return value.replace("-", "").lower()
    try:
        url = urlsplit(value if "://" in value else "https://" + value)
    except ValueError:
        return None
    if url.scheme != "https" or url.hostname not in NOTION_HOSTS:
        return None
    last = url.path.rstrip("/").rsplit("/", 1)[-1]
    match = re.search(rf"(?:^|-)({PAGE_ID.pattern})$", last, re.IGNORECASE)
    return match[1].replace("-", "").lower() if match else None


def record_path(task_id: str) -> Path:
    return Path.home() / ".local/state/agent-tickets" / f"{task_id}.json"


def max_age(environment: Mapping[str, str]) -> timedelta:
    try:
        hours = float(environment.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_HOURS))
    except ValueError:
        hours = DEFAULT_MAX_AGE_HOURS
    return timedelta(hours=hours if math.isfinite(hours) and 0 < hours <= 24 * 365 else 0)


def load(task_id: str, environment: Mapping[str, str] = os.environ) -> Ticket:
    """The verified, open, fresh ticket for a task, or TicketError."""
    if not TASK_ID.fullmatch(task_id):
        raise TicketError(Refusal.BAD_TASK_ID, "GROKBOT_TASK_ID is not a usable task id")
    path = record_path(task_id)
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        raise TicketError(Refusal.NO_RECORD, f"no ticket record at {path}; Dinesh must verify the ticket first") from None
    except (OSError, ValueError, RecursionError):
        raise TicketError(Refusal.BAD_RECORD, f"invalid ticket record {path}: unreadable or not JSON") from None
    try:
        if record["task_id"] != task_id:
            raise TicketError(Refusal.WRONG_TASK, f"ticket record {path} is for another task id")
        status = TicketStatus(record["status"])
        verified_at = datetime.fromisoformat(record["verified_at"])
        identifier, url = record["ticket_id"], record["ticket_url"]
        if (verified_at.tzinfo is None or record["verified_by"] != VERIFIER or not page_id(identifier)
                or not url.startswith("https://") or page_id(url) != page_id(identifier)):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TicketError(Refusal.BAD_RECORD, f"invalid ticket record {path}: missing or malformed fields") from None
    if status in CLOSED:
        raise TicketError(Refusal.CLOSED_STATUS, f"ticket status is {status.value}")
    age = datetime.now(UTC) - verified_at
    limit = max_age(environment)
    if age >= limit or age < -CLOCK_SKEW:
        raise TicketError(Refusal.STALE, f"ticket verification is older than {limit} or in the future; ask Dinesh to verify again")
    return Ticket(task_id, identifier, url, status, verified_at)


def enforced(environment: Mapping[str, str] = os.environ) -> bool:
    """Bot runs and Rex panes; plain terminals outside Rex are left alone."""
    return any(environment.get(name) for name in ENFORCING_ENV)


def session_ticket(environment: Mapping[str, str] = os.environ) -> Ticket:
    """The record must be valid and this session must have been launched with it."""
    task_id = environment.get("GROKBOT_TASK_ID", "")
    if not task_id:
        raise TicketError(Refusal.NO_TICKET_ENV, "GROKBOT_TASK_ID is not set")
    ticket = load(task_id, environment)
    launched = page_id(environment.get("GROKBOT_TICKET", ""))
    if not launched:
        raise TicketError(Refusal.NO_TICKET_ENV, "GROKBOT_TICKET is not set")
    if launched != page_id(ticket.id):
        raise TicketError(Refusal.TICKET_MISMATCH, "GROKBOT_TICKET does not match the ticket record")
    return ticket


def _specific(event: HookEvent, **fields: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event.value, **fields}}


def hook(event: object, environment: Mapping[str, str] = os.environ) -> dict | None:
    """Hook output, or None to stay out of the way (never grants permission)."""
    if event not in (HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.PRE_TOOL_USE):
        return None
    event = HookEvent(event)
    try:
        ticket = session_ticket(environment)
    except TicketError as error:
        reason = f"Blocked: this run has no valid Notion ticket ({error}). Start it with rex-run."
        if event == HookEvent.USER_PROMPT_SUBMIT:
            return {"decision": "block", "reason": reason}
        if event == HookEvent.PRE_TOOL_USE:
            return _specific(event, permissionDecision="deny", permissionDecisionReason=reason)
        return _specific(event, additionalContext=reason)
    if event == HookEvent.SESSION_START:
        return _specific(event, additionalContext=f"Notion ticket for this run: {ticket.url}")
    return None


def main() -> int:
    if not enforced():
        return 0
    try:
        output = hook(json.load(sys.stdin).get("hook_event_name"))
        if output:
            print(json.dumps(output))
        return 0
    except Exception as error:  # noqa: BLE001 - exit 2 blocks; any other failure code would let the run through.
        print(f"Ticket guard failed closed: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
