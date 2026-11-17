#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in, durable lifecycle webhooks for Grok Bot."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import urlsplit

from agent_types import AgentKind, Event, HookEvent, InputKind
from question_model import (
    ASSUMPTION_PREFIX,
    DIGEST_SCHEMA,
    DIGEST_UNTRUSTED_FIELDS,
    EVENTS_DIR,
    FLAG_ENV,
    LEDGER_FILE,
    CodexQuestionField,
    DigestEnvelope,
    DigestMetadata,
    QuestionTool,
)
from question_guard import scan_assumptions
from question_redact import redact
import rex_visible as rex

CONFIG = Path.home() / ".config/agent-tooling/grokbot-callback.env"


class NotificationType(StrEnum):
    PERMISSION_PROMPT = "permission_prompt"
    IDLE_PROMPT = "idle_prompt"
    ELICITATION_DIALOG = "elicitation_dialog"
    TRUST_PROMPT = "trust_prompt"
    INPUT_PROMPT = "input_prompt"


class MemberState(StrEnum):
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    IDLE = "idle"
    REPORTED = "reported"
    ACCEPTED = "accepted"
    ENDED = "ended"


class Status(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    ENDED = "ended"
    ERROR = "error"  # Host input only; callbacks report failed.


class Role(StrEnum):
    AGENT = "agent"
    SUBAGENT = "subagent"


class Outcome(StrEnum):
    STARTED = "started"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"
    CONFIG_INVALID = "config_invalid"
    ENQUEUE_FAILED = "enqueue_failed"
    HOOK_ERROR = "hook_error"
    LOCAL_ONLY = "local_only"
    PENDING = "pending"
    SPOOL_INVALID = "spool_invalid"


class GitMetadata(TypedDict):
    repo: str
    branch: str
    head_sha: str


class Lineage(TypedDict):
    requester_bot: str
    root_session_id: str
    parent_session_id: str
    agent_session_id: str
    depth: int
    agent_kind: AgentKind
    role: Role


class ChildSummary(TypedDict):
    id: str
    depth: int
    status: Status


class Payload(GitMetadata):
    event: Event
    status: Status
    agent: AgentKind
    requester: str
    task_id: str
    turn_id: str
    lineage: Lineage
    children: list[ChildSummary]
    untrusted: list[str]
    prompt_text: str
    agent_session_id: str
    rex_session: str
    rex_block: str
    cwd: str
    last_message: str
    finished_at: str
    kind: NotRequired[InputKind]
    delivery_key: NotRequired[str]


class TestPayload(TypedDict):
    event: Event
    task_id: str
    requester: str
    untrusted: list[str]


class AssumptionEvent(TypedDict):
    task_id: str
    text: str
    batch_id: str | None
    metadata: NotRequired[DigestMetadata]


class Envelope(TypedDict):
    agent: AgentKind
    data: dict[str, Any]
    turn: str
    requester: str
    rex_session: str
    rex_block: str
    finished_at: str
    task_id: NotRequired[str]
    event: NotRequired[Event]
    status: NotRequired[Status]
    lineage: NotRequired[Lineage]
    children: NotRequired[list[ChildSummary]]
    last_message: NotRequired[str]
    metadata: NotRequired[GitMetadata]
    kind: NotRequired[InputKind]
    prompt_text: NotRequired[str]
    request_key: NotRequired[str]
    body: NotRequired[Payload]
    delivery_outcome: NotRequired[Outcome]


class SessionState(TypedDict):
    key: str
    id: str
    agent: AgentKind
    parent: str
    root: str
    depth: int
    task_id: str
    requester: str
    turn: str
    status: Status
    rex_session: str
    rex_block: str
    updated_at: NotRequired[str]
    native_turn: NotRequired[str]
    retired_turns: NotRequired[list[str]]


class SessionView(TypedDict):
    id: str
    agent: AgentKind
    task_id: str
    requester: str
    status: Status
    rex_session: str
    rex_block: str
    updated_at: NotRequired[str]
    turn_id: NotRequired[str]
    lineage: NotRequired[Lineage]


class DeliveryRecord(TypedDict, total=False):
    key: str
    event: Event
    task_id: str
    agent_session_id: str
    rex_session: str
    rex_block: str
    agent: AgentKind
    turn_id: str
    http_status: int
    spool: str
    outcome: str
    finished_at: str


class DeliveryFields(TypedDict):
    key: str
    event: Event
    task_id: str
    agent_session_id: str
    rex_session: str
    rex_block: str


class Health(TypedDict):
    pending: list[DeliveryRecord]
    failed: list[DeliveryRecord]
    running: list[SessionView]
    started: list[DeliveryRecord]
    sessions: list[SessionView]


class RetryResult(TypedDict):
    delivered: int
    failed: int


WEBHOOK_EVENTS = (Event.RUN_FINISHED, Event.NEEDS_INPUT)
TERMINAL_STATUSES = (Status.COMPLETED, Status.FAILED, Status.ENDED)
STATUS_HISTORY_LIMIT = 100
RECEIPT_MAX_BYTES = 1024 * 1024
ORPHAN_TMP_AGE_SECONDS = 24 * 60 * 60
UNTRUSTED_FIELDS = ("last_message", "prompt_text", "cwd", "repo", "branch", "head_sha", "task_id")


def private_fd(path: Path, flags: int) -> int:
    """Create privately regardless of umask; also tighten existing runtime files."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def receipt_lock(exclusive):
    with os.fdopen(private_fd(rex.STATE / "grokbot-receipts.lock", os.O_RDWR | os.O_CREAT), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def receipt(outcome, **fields):
    """Only fixed diagnostics and delivery metadata, never exception/config text."""
    rex.state_dir()
    row = DeliveryRecord(finished_at=datetime.now().astimezone().isoformat(), outcome=outcome, **fields)
    encoded = json.dumps(row) + "\n"
    path = rex.STATE / "grokbot-callback-receipts.jsonl"
    with receipt_lock(exclusive=True):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if path.exists() and path.stat().st_size + len(encoded.encode()) > RECEIPT_MAX_BYTES:
            # Keep one bounded archive, including when upgrading an oversized old log.
            with path.open("rb") as previous:
                size = path.stat().st_size
                if size > RECEIPT_MAX_BYTES:
                    previous.seek(size - RECEIPT_MAX_BYTES)
                    previous.readline()  # Discard a partial oldest record.
                tail = previous.read()
            archive = path.with_suffix(".jsonl.1")
            with os.fdopen(private_fd(archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "wb") as backup:
                backup.write(tail)
                backup.flush()
                os.fsync(backup.fileno())
            flags |= os.O_TRUNC
        with os.fdopen(private_fd(path, flags), "a") as stream:
            stream.write(encoded)


def receipt_lines():
    """Read both log generations under the writer's lock for a consistent snapshot."""
    with receipt_lock(exclusive=False):
        for name in ("grokbot-callback-receipts.jsonl.1", "grokbot-callback-receipts.jsonl"):
            try:
                with (rex.STATE / name).open() as stream:
                    yield from stream
            except FileNotFoundError:
                continue


def cleanup_orphan_temps():
    """Only stale callback spools; never delete a temporary file held by a writer."""
    cutoff = time.time() - ORPHAN_TMP_AGE_SECONDS
    for path in rex.STATE.glob("grokbot-pending-*.tmp"):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            with os.fdopen(os.open(path, os.O_RDWR), "r+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.fstat(stream.fileno()).st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            continue  # Includes a busy writer or another cleaner winning the race.


def config():
    with CONFIG.open() as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError("Config must be an owned regular file with mode 600")
        values = {}
        for line in stream:
            key, separator, value = line.rstrip("\r\n").partition("=")
            if separator and key in ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_HEADER"):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    url = urlsplit(values.get("GROKBOT_WEBHOOK_URL", ""))
    header = values.get("GROKBOT_WEBHOOK_HEADER", "")
    name, separator, value = header.partition(":")
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or not separator or not name or not value.strip() or "\r" in header or "\n" in header):
        raise ValueError("Invalid callback configuration")
    return url, name, value.removeprefix(" ")


def waiting(data):
    return bool(data.get("background_tasks") or data.get("session_crons"))


def now():
    return datetime.now().astimezone().isoformat()


@contextmanager
def database():
    rex.state_dir()
    path = rex.STATE / "grokbot-callback.sqlite3"
    os.close(private_fd(path, os.O_RDWR | os.O_CREAT))
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, record TEXT)")
        yield db


def actor_key(agent, session, child=""):
    if agent == AgentKind.CODEX:
        return child or session
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([AgentKind.CLAUDE, session, child], separators=(",", ":"))))


def git_metadata(cwd):
    def git(*args):
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1, check=False)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return GitMetadata(repo=git("rev-parse", "--show-toplevel"), branch=git("branch", "--show-current"),
                       head_sha=git("rev-parse", "HEAD"))


@dataclass(frozen=True, slots=True)
class ValidatedQuestion:
    text: str
    raw: Mapping[str, object]


def validate_question_input(value: object) -> tuple[ValidatedQuestion, ...] | None:
    """Salvage valid native items without coercing non-string question fields."""
    if not isinstance(value, dict) or not isinstance(value.get(CodexQuestionField.QUESTIONS), list):
        return None
    questions = []
    for item in value[CodexQuestionField.QUESTIONS]:
        if not isinstance(item, dict):
            continue
        text = next((item[key] for key in (CodexQuestionField.QUESTION, CodexQuestionField.TITLE) if isinstance(item.get(key), str)), None)
        if text is not None:
            questions.append(ValidatedQuestion(text, item))
    return tuple(questions) or None


def question_registered(data, agent) -> bool:
    call = data.get("tool_use_id")
    session = data.get("session_id")
    path = rex.STATE / LEDGER_FILE
    if not isinstance(call, str) or not call or not session or not path.exists():
        return False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            return db.execute(
                "SELECT 1 FROM native_call n JOIN registration r ON r.batch=n.batch WHERE n.call=? AND n.actor=? AND n.tool=? AND n.failed=0 AND r.entry_json IS NOT NULL LIMIT 1",
                (call, actor_key(agent, session, data.get("agent_id") or ""), data.get("tool_name")),
            ).fetchone() is not None
    except (sqlite3.Error, OSError):
        return False


def input_request(data, agent=AgentKind.CODEX) -> tuple[InputKind, str] | None:
    name = data.get("hook_event_name")
    notification = data.get("notification_type", "")
    tool = data.get("tool_name")
    if os.environ.get(FLAG_ENV) == "1" and tool in tuple(QuestionTool) and question_registered(data, agent):
        return None
    if agent == AgentKind.CODEX and name == HookEvent.PRE_TOOL_USE and tool in (QuestionTool.CODEX_BLOCKING, QuestionTool.CODEX_ASYNC):
        questions = validate_question_input(data.get("tool_input"))
        prompt = "\n".join(q.text for q in questions or () if q.text.strip())
        return (InputKind.QUESTION, prompt[:1000]) if prompt else None
    if name == HookEvent.PERMISSION_REQUEST:
        kind = InputKind.APPROVAL
    elif name == HookEvent.NOTIFICATION and notification == NotificationType.ELICITATION_DIALOG:
        kind = InputKind.QUESTION
    elif name == HookEvent.NOTIFICATION and notification == NotificationType.TRUST_PROMPT:
        kind = InputKind.TRUST
    elif name == HookEvent.NOTIFICATION and notification == NotificationType.INPUT_PROMPT:
        kind = InputKind.OTHER
    else:
        return None
    inputs = data.get("tool_input") or {}
    prompt = data.get("message") or data.get("prompt") or (inputs.get("description") if isinstance(inputs, dict) else "") or inputs
    if not isinstance(prompt, str):
        prompt = json.dumps(prompt, ensure_ascii=False)
    return kind, prompt[:1000]


def resumes_input(data) -> bool:
    """Async tool acceptance is not an answer; wait for later host activity."""
    return (data.get("hook_event_name") in (HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE)
            and not (data.get("hook_event_name") == HookEvent.POST_TOOL_USE
                     and data.get("tool_name") == QuestionTool.CODEX_ASYNC))


def input_blocked(agent, data):
    """Keep adapter lifecycle writes from reopening a terminal callback session."""
    path = rex.STATE / "grokbot-callback.sqlite3"
    if not input_request(data, agent) or not data.get("session_id") or not path.exists():
        return False
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("SELECT record FROM sessions WHERE key=?",
                             (actor_key(agent, data["session_id"], data.get("agent_id") or ""),)).fetchone()
        return bool(row and json.loads(row[0]).get("status") in TERMINAL_STATUSES)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return False  # The transactional notifier still guards delivery; hooks stay open.


def request_identity(data):
    identifier = data.get("request_id") or data.get("permission_request_id") or data.get("tool_use_id") or data.get("tool_call_id")
    identity = ["id", identifier] if identifier else ["content", data.get("tool_name") or "", data.get("tool_input") or {},
                                                    data.get("message") or data.get("prompt") or ""]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def lineage(item: SessionState, sessions: dict[str, SessionState]) -> Lineage:
    root = sessions.get(item["root"], item)
    parent = sessions.get(item["parent"])
    return Lineage(requester_bot=item["requester"], root_session_id=root["id"],
                   parent_session_id=parent["id"] if parent else "", agent_session_id=item["id"],
                   depth=item["depth"], agent_kind=item["agent"], role=Role.SUBAGENT if item["parent"] else Role.AGENT)


def _parse_session_state(record: str) -> SessionState:
    value = json.loads(record)
    string_fields = (
        "key",
        "id",
        "parent",
        "root",
        "task_id",
        "requester",
        "turn",
        "rex_session",
        "rex_block",
    )
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str) for key in string_fields
    ):
        raise ValueError("invalid session record")
    if type(value.get("depth")) is not int or value["depth"] < 0:
        raise ValueError("invalid session depth")
    value["agent"] = AgentKind(value["agent"])
    value["status"] = Status(value["status"])
    for key in ("updated_at", "native_turn"):
        if key in value and not isinstance(value[key], str):
            raise ValueError("invalid session field")
    if "retired_turns" in value and (
        not isinstance(value["retired_turns"], list)
        or any(not isinstance(turn, str) for turn in value["retired_turns"])
    ):
        raise ValueError("invalid retired turns")
    return cast(SessionState, value)


def session_state(
    agent: AgentKind, session: str, child: str = ""
) -> SessionState | None:
    """Read one callback session record without creating or modifying its database."""
    path = rex.STATE / "grokbot-callback.sqlite3"
    if not path.exists():
        return None
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as db:
            row = db.execute(
                "SELECT record FROM sessions WHERE key=?",
                (actor_key(agent, session, child),),
            ).fetchone()
        return _parse_session_state(row[0]) if row else None
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def session_lineage(state: SessionState) -> Lineage:
    """Build lineage from the session plus its root and parent records, read-only."""
    sessions = {state["key"]: state}
    path = rex.STATE / "grokbot-callback.sqlite3"
    if path.exists():
        keys = {key for key in (state["root"], state["parent"]) if key}
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                for key in keys:
                    row = db.execute("SELECT record FROM sessions WHERE key=?", (key,)).fetchone()
                    if row:
                        sessions[key] = _parse_session_state(row[0])
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            pass
    return lineage(state, sessions)


def sync_sessions(sessions: dict[str, SessionState], members, current_key=""):
    """Use coordination's refined immediate edges, never infer delegation from cwd."""
    members_by_key = {m["thread"]: m for m in members}
    visited = set()

    def sync(key, ancestors):
        if key in visited or key in ancestors:
            return
        member = members_by_key.get(key)
        if not member or not member.get("parent"):
            return
        sync(member["parent"], ancestors | {key})
        parent = sessions.get(member["parent"])
        if not parent or member["parent"] in ancestors:
            return
        existing = sessions.get(key)
        # A delegated session keeps its original task when its controller switches.
        if existing and existing["task_id"] != parent["task_id"]:
            return
        status = {MemberState.WORKING: Status.RUNNING, MemberState.WAITING: Status.WAITING, MemberState.BLOCKED: Status.WAITING,
                  MemberState.REPORTED: Status.COMPLETED, MemberState.ACCEPTED: Status.COMPLETED,
                  MemberState.IDLE: Status.COMPLETED, MemberState.ENDED: Status.ENDED}.get(member.get("state"), Status.RUNNING)
        if key == current_key and existing:
            status = existing["status"]
        if existing and existing.get("status") == Status.FAILED and member.get("state") in (MemberState.BLOCKED, MemberState.REPORTED, MemberState.ACCEPTED, MemberState.IDLE):
            status = Status.FAILED
        child = parent.copy()
        if existing:
            child.update(existing)
        child.update(key=key, id=member.get("native_id") or member.get("native_session") or key,
                     agent=AgentKind(member.get("provider", AgentKind.CODEX)), parent=member["parent"],
                     root=parent["root"], depth=parent["depth"] + 1, status=status,
                     native_turn=existing.get("native_turn", "") if existing else "",
                     retired_turns=existing.get("retired_turns", []) if existing else [])
        sessions[key] = child
        if status == Status.RUNNING and existing and existing.get("status") in (Status.COMPLETED, Status.FAILED, Status.ENDED):
            # Native followup tools can resume a child without UserPromptSubmit.
            sessions[key]["turn"] = uuid.uuid4().hex
            sessions[key]["native_turn"] = ""
        visited.add(key)

    for key in members_by_key:
        sync(key, set())


def notify(agent, data, members=()):
    """Fail open: hooks only record local state and launch a detached HTTP worker."""
    try:
        _notify(agent, data, members)
    except Exception:  # noqa: BLE001 - every callback failure must leave the host hook open.
        try:
            receipt(Outcome.ENQUEUE_FAILED)
        except OSError:
            pass


def _notify(agent, data, members):
    agent = AgentKind(agent)
    name = data.get("hook_event_name")
    request = input_request(data, agent)
    if name not in (HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP, HookEvent.STOP, HookEvent.STOP_FAILURE, HookEvent.SESSION_END, HookEvent.POST_TOOL_USE, HookEvent.PRE_TOOL_USE) and not request:
        return
    session = data.get("session_id")
    if not session or (name in (HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP) and not data.get("agent_id")):
        return
    child = data.get("agent_id") or ""
    key = actor_key(agent, session, child)
    # Ordinary sessions never create callback state, including inherited env-less roots.
    if not os.environ.get("GROKBOT_CALLBACK") and not (rex.STATE / "grokbot-callback.sqlite3").exists():
        return
    metadata = None
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        sessions: dict[str, SessionState] = {k: json.loads(row) for k, row in db.execute("SELECT key,record FROM sessions")}
        if request and sessions.get(key, {}).get("status") in TERMINAL_STATUSES:
            return  # Reject before coordination snapshots can overwrite a child's terminal state.
        sync_sessions(sessions, members)
        item = sessions.get(key)
        inherited = os.environ.get("GROKBOT_PARENT_SESSION", "")
        parent_key = actor_key(agent, session) if child else ""
        if not child and inherited:
            matches = [k for k, row in sessions.items() if row["id"] == inherited or k == inherited]
            parent_key = matches[0] if len(matches) == 1 else "unknown-parent"
        parent = sessions.get(parent_key)
        if item and item["parent"]:
            parent = sessions.get(item["parent"])
        if not item:
            if parent:
                item = parent.copy()
                item.update(key=key, id=child or session, agent=agent, parent=parent["key"],
                            depth=parent["depth"] + 1, status=Status.RUNNING, native_turn="", retired_turns=[])
            elif child or inherited or any(m["thread"] == key and m.get("parent") for m in members):
                return  # A delegated session without a known opted-in ancestor cannot become a root.
            elif os.environ.get("GROKBOT_CALLBACK"):
                cwd = data.get("cwd") or os.getcwd()
                metadata = git_metadata(cwd)
                item = SessionState(key=key, id=session, agent=agent, parent="", root=key, depth=0,
                                    task_id=os.environ.get("GROKBOT_TASK_ID") or f"{metadata['repo'] or cwd}:{metadata['branch']}",
                                    requester=os.environ["GROKBOT_CALLBACK"], turn=uuid.uuid4().hex, status=Status.IDLE,
                                    rex_session=os.environ.get("REX_SESSION", ""), rex_block=os.environ.get("REX_BLOCK", ""))
            else:
                return
        if not item["parent"] and not os.environ.get("GROKBOT_CALLBACK"):
            return
        if request and item["status"] in TERMINAL_STATUSES:
            return
        # Reusing a root CLI for another bot task is explicit at the next prompt.
        if name == HookEvent.USER_PROMPT_SUBMIT and not item["parent"]:
            if not os.environ.get("GROKBOT_CALLBACK"):
                return
            metadata = metadata or git_metadata(data.get("cwd") or os.getcwd())
            item["task_id"] = os.environ.get("GROKBOT_TASK_ID") or f"{metadata['repo'] or data.get('cwd') or os.getcwd()}:{metadata['branch']}"
            item["requester"] = os.environ["GROKBOT_CALLBACK"]
            item["turn"] = uuid.uuid4().hex
        native_turn = data.get("turn_id") or data.get("turn-id")
        retired_turns = item.get("retired_turns")
        if not isinstance(retired_turns, list):
            retired_turns = []
            item["retired_turns"] = retired_turns
        if name == HookEvent.USER_PROMPT_SUBMIT:
            if item["parent"]:
                item["turn"] = uuid.uuid4().hex
            previous = item.get("native_turn")
            if previous and previous != native_turn:
                retired_turns.append(previous)
            item["native_turn"] = native_turn or ""
        elif not item["parent"] and native_turn and (native_turn in retired_turns or
                (item.get("native_turn") and native_turn != item["native_turn"])):
            return  # A delayed old-turn hook cannot change the active task's owner.
        if native_turn:
            item["native_turn"] = native_turn
            item["turn"] = native_turn
        item["updated_at"] = now()
        # Native children share the controller's block; nested CLIs have their own.
        item["rex_session"] = os.environ.get("REX_SESSION") or item["rex_session"]
        item["rex_block"] = os.environ.get("REX_BLOCK") or item["rex_block"]
        failed = name == HookEvent.STOP_FAILURE or data.get("status") in (Status.FAILED, Status.ERROR) or bool(data.get("error"))
        event: Event | None = None
        if request:
            event, item["status"] = Event.NEEDS_INPUT, Status.WAITING
        elif resumes_input(data) and item["status"] == Status.WAITING:
            item["status"] = Status.RUNNING
        elif name == HookEvent.USER_PROMPT_SUBMIT:
            event, item["status"] = (Event.LINEAGE if item["parent"] else Event.HEARTBEAT), Status.RUNNING
        elif name == HookEvent.SUBAGENT_START or (name == HookEvent.SESSION_START and item["parent"]):
            event, item["status"] = Event.LINEAGE, Status.RUNNING
        elif name in (HookEvent.STOP, HookEvent.STOP_FAILURE, HookEvent.SUBAGENT_STOP):
            item["status"] = Status.WAITING if waiting(data) else Status.FAILED if failed else Status.COMPLETED
            if not waiting(data):
                event = Event.LINEAGE if item["parent"] else Event.RUN_FINISHED
        elif name == HookEvent.SESSION_END:
            item["status"] = Status.ENDED
            if item["parent"]:
                event = Event.LINEAGE
        sessions[key] = item
        sync_sessions(sessions, members, current_key=key)
        for k, row in sessions.items():
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (k, json.dumps(row)))
        if name == HookEvent.USER_PROMPT_SUBMIT:
            receipt(Outcome.STARTED, agent=agent, agent_session_id=item["id"], task_id=item["task_id"],
                    turn_id=item["turn"], rex_session=item["rex_session"], rex_block=item["rex_block"])
        if event not in WEBHOOK_EVENTS:
            return
        envelope = Envelope(agent=agent, data=data, turn=item["turn"], requester=item["requester"],
                            task_id=item["task_id"], rex_session=item["rex_session"], rex_block=item["rex_block"],
                            finished_at=now(), event=event, status=item["status"], lineage=lineage(item, sessions),
                            last_message=final_message(agent, data) if event == Event.RUN_FINISHED else "",
                            metadata=metadata or git_metadata(data.get("cwd") or os.getcwd()),
                            children=[ChildSummary(id=row["id"], depth=row["depth"], status=row["status"])
                                      for row in sessions.values() if row["root"] == item["root"] and row["parent"] and row["task_id"] == item["task_id"]])
        if request:
            envelope.update(kind=request[0], prompt_text=request[1], request_key=request_identity(data))
    created = spool(envelope)
    if created is not None and event == Event.RUN_FINISHED and os.environ.get(FLAG_ENV) == "1":
        try:
            source_metadata = envelope["metadata"]
            record_assumptions(
                envelope["task_id"],
                final_message(agent, data, limit=None),
                DigestMetadata(requester=envelope["requester"], repo=source_metadata["repo"], branch=source_metadata["branch"]),
            )
        except Exception:  # noqa: BLE001, S110 - optional L0 reporting must not affect completion delivery.
            pass


def record_assumptions(task_id: str, text: str, metadata: DigestMetadata | None = None) -> None:
    """Write leaf event files; the ledger ingests them on its next tick or hook."""
    for line in text.splitlines():
        if not line.startswith(ASSUMPTION_PREFIX) or not scan_assumptions(line):
            continue
        directory = rex.STATE / EVENTS_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        event = AssumptionEvent(task_id=task_id, text=redact(line).text, batch_id=None)
        if metadata is not None:
            event["metadata"] = metadata
        path = directory / (str(uuid.uuid4()) + ".json")
        temporary = path.with_suffix(".tmp")
        try:
            with os.fdopen(private_fd(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as stream:
                json.dump(event, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def spool(envelope: Envelope | DigestEnvelope) -> Path | None:
    """Publish a canonical private spool and start its detached delivery worker."""
    rex.state_dir()
    key = delivery_key(envelope)
    path = rex.STATE / ("grokbot-pending-" + key)
    if path.exists() or (rex.STATE / ("grokbot-sent-" + key)).exists():
        return None
    write_spool(path, envelope)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "deliver", str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    return path


def write_spool(path: Path, envelope: Envelope | DigestEnvelope):
    """Publish atomically; tempfile creates the file with mode 0600, even at umask 0."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=rex.STATE, prefix="grokbot-pending-", suffix=".tmp", delete=False) as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            temporary = Path(stream.name)
            json.dump(envelope, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def final_message(agent, data, limit: int | None = 2000):
    if "last_assistant_message" in data:
        return str(data["last_assistant_message"] or "")[:limit]
    path = data.get("transcript_path")
    if not path:
        return ""
    message = ""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 100000))
            for line in stream.read().decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if agent == AgentKind.CLAUDE and row.get("type") == "assistant":
                    item = row.get("message") or {}
                    content = item.get("content", []) if isinstance(item, dict) else []
                elif agent == AgentKind.CODEX and row.get("type") == "response_item":
                    item = row.get("payload") or {}
                    if not isinstance(item, dict):
                        continue
                    if item.get("role") != "assistant" or item.get("phase") != "final_answer":
                        continue
                    content = item.get("content", [])
                elif agent == AgentKind.CODEX and row.get("type") == "event_msg":
                    item = row.get("payload") or {}
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "task_complete" and isinstance(item.get("last_agent_message"), str):
                        message = item["last_agent_message"]
                    continue
                else:
                    continue
                if not isinstance(content, list):
                    continue
                text = "\n".join(part.get("text", "") for part in content
                                 if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
                if text:
                    message = text
    except OSError:
        pass
    return message[:limit]


def payload(envelope) -> Payload:
    data = envelope["data"]
    cwd = data.get("cwd") or os.getcwd()
    metadata = envelope.get("metadata") or git_metadata(cwd)
    failed = data.get("hook_event_name") == HookEvent.STOP_FAILURE or data.get("status") in (Status.FAILED, Status.ERROR) or bool(data.get("error"))
    ancestry = envelope.get("lineage") or Lineage(requester_bot=envelope["requester"], root_session_id=data["session_id"],
                parent_session_id="", agent_session_id=data["session_id"], depth=0,
                agent_kind=AgentKind(envelope["agent"]), role=Role.AGENT)
    body = Payload(event=Event(envelope.get("event", Event.RUN_FINISHED)),
                   status=Status(envelope.get("status", Status.FAILED if failed else Status.COMPLETED)),
                   agent=AgentKind(envelope["agent"]), requester=envelope["requester"],
                   task_id=envelope.get("task_id") or f"{metadata['repo'] or cwd}:{metadata['branch']}",
                   turn_id=envelope["turn"], lineage=ancestry, children=envelope.get("children", []),
                   untrusted=list(UNTRUSTED_FIELDS), prompt_text=envelope.get("prompt_text", "")[:1000],
                   agent_session_id=ancestry["agent_session_id"], rex_session=envelope["rex_session"],
                   rex_block=envelope["rex_block"], cwd=cwd, repo=metadata["repo"], branch=metadata["branch"], head_sha=metadata["head_sha"],
                   last_message=(envelope.get("last_message", str(data.get("last_assistant_message") or ""))[:2000]
                                 if envelope.get("event", Event.RUN_FINISHED) == Event.RUN_FINISHED else ""),
                   finished_at=envelope["finished_at"])
    if "kind" in envelope:
        body["kind"] = InputKind(envelope["kind"])
    return body


def post(body):
    if body.get("event") not in (*WEBHOOK_EVENTS, Event.TEST):
        return 0
    url, name, value = config()
    # Apply current safety metadata to cached legacy payloads as well as new ones.
    outgoing = body.copy()
    fields = DIGEST_UNTRUSTED_FIELDS if body.get("schema") == DIGEST_SCHEMA else UNTRUSTED_FIELDS
    outgoing["untrusted"] = [field for field in fields if field in body]
    encoded = json.dumps(outgoing).encode()
    status = 0
    for _ in range(3):
        connection = None
        try:
            connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(url.hostname, url.port, timeout=5)
            connection.putrequest("POST", (url.path or "/") + ("?" + url.query if url.query else ""))
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(encoded)))
            connection.putheader(name, value)
            connection.endheaders(encoded)
            status = connection.getresponse().status
            if 200 <= status < 300:
                break
        except (OSError, ValueError, http.client.HTTPException):
            status = 0
        finally:
            if connection:
                connection.close()
    return status


def delivery_key(envelope):
    cached = (envelope.get("body") or {}).get("delivery_key")
    if isinstance(cached, str) and len(cached) == 64 and all(c in "0123456789abcdef" for c in cached):
        return cached  # Retries, including pre-upgrade spools, keep the original key.
    identity = [envelope.get("task_id", ""), envelope["agent"],
                envelope.get("lineage", {}).get("agent_session_id") or envelope["data"]["session_id"],
                envelope["turn"], envelope.get("event", Event.RUN_FINISHED), envelope.get("kind", ""),
                envelope.get("status", "") if envelope.get("event") == Event.LINEAGE else ""]
    if envelope.get("event") == Event.NEEDS_INPUT:
        identity.append(envelope.get("request_key") or request_identity(envelope["data"]))
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def deliver(path):
    rex.state_dir()
    try:
        envelope = json.loads(path.read_text())
    except FileNotFoundError:
        return True  # Another worker already delivered this spool.
    key = delivery_key(envelope)
    if envelope.get("event", Event.RUN_FINISHED) not in WEBHOOK_EVENTS:
        # Retire spools from older runtimes without waking a paid routine.
        receipt(Outcome.LOCAL_ONLY, key=key, event=envelope.get("event"))
        path.unlink(missing_ok=True)
        return None
    # A lock survives crashes without becoming a permanent failed-delivery claim.
    with os.fdopen(private_fd(rex.STATE / ("grokbot-lock-" + key), os.O_RDWR | os.O_CREAT), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sent = rex.STATE / ("grokbot-sent-" + key)
        if sent.exists():
            path.unlink(missing_ok=True)
            return True
        fields = DeliveryFields(key=key, event=envelope.get("event", Event.RUN_FINISHED), task_id=envelope.get("task_id", ""),
                                agent_session_id=envelope.get("lineage", {}).get("agent_session_id") or envelope.get("data", {}).get("session_id", ""),
                                rex_session=envelope.get("rex_session", ""), rex_block=envelope.get("rex_block", ""))
        try:
            # Freeze payload on first attempt: retries preserve task metadata and text.
            if "body" not in envelope:
                envelope["body"] = payload(envelope)
                envelope["body"]["delivery_key"] = key
                write_spool(path, envelope)
            status = post(envelope["body"])
        except (OSError, ValueError):
            envelope["delivery_outcome"] = Outcome.CONFIG_INVALID
            write_spool(path, envelope)
            receipt(Outcome.CONFIG_INVALID, **fields)
            return False
        if 200 <= status < 300:
            try:
                os.close(private_fd(sent, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                pass  # Another worker already recorded the accepted delivery.
            except Exception:
                path.unlink(missing_ok=True)
                try:
                    receipt(Outcome.DELIVERED, **fields, http_status=status)
                except Exception:  # noqa: BLE001, S110 - preserve the marker error after accepted delivery.
                    pass
                raise
            path.unlink(missing_ok=True)
            try:
                receipt(Outcome.DELIVERED, **fields, http_status=status)
            except Exception:  # noqa: BLE001, S110 - delivery is final; receipt errors must not cause another POST.
                pass
            return True
        envelope["delivery_outcome"] = Outcome.DELIVERY_FAILED
        write_spool(path, envelope)
        try:
            receipt(Outcome.DELIVERY_FAILED, **fields, http_status=status)
        except Exception:  # noqa: BLE001, S110 - the failed spool preserves retry state without a receipt.
            pass
        return False


def session_view(row: SessionState) -> SessionView:
    view = SessionView(id=row["id"], agent=row["agent"], task_id=row["task_id"], requester=row["requester"],
                       status=row["status"], rex_session=row["rex_session"], rex_block=row["rex_block"])
    if "updated_at" in row:
        view["updated_at"] = row["updated_at"]
    return view


def health() -> Health:
    """Bound historical output; retain every active session and actionable spool."""
    rex.state_dir()
    cleanup_orphan_temps()
    pending: list[DeliveryRecord] = []
    failed: list[DeliveryRecord] = []
    spools: dict[str, tuple[Path, Envelope]] = {}
    for path in rex.STATE.glob("grokbot-pending-*"):
        if path.suffix == ".tmp":
            continue
        try:
            envelope: Envelope = json.loads(path.read_text())
            if envelope.get("event", Event.RUN_FINISHED) not in WEBHOOK_EVENTS:
                continue
            key = delivery_key(envelope)
            if key not in spools and not (rex.STATE / ("grokbot-sent-" + key)).exists():
                spools[key] = (path, envelope)
        except (OSError, ValueError, KeyError, TypeError):
            pending.append(DeliveryRecord(spool=str(path), outcome=Outcome.SPOOL_INVALID))
    # Stream the log; only pending-spool receipts and bounded history stay in memory.
    receipts: dict[str, DeliveryRecord] = {}
    history: OrderedDict[str, DeliveryRecord] = OrderedDict()
    started: deque[DeliveryRecord] = deque(maxlen=STATUS_HISTORY_LIMIT)
    for line in receipt_lines():
        try:
            row: DeliveryRecord = json.loads(line)
            if row.get("outcome") == Outcome.STARTED:
                started.append(row)
            key = row.get("key")
            if not key:
                continue
            if key in spools:
                receipts[key] = row
            else:
                history.pop(key, None)
                if (row.get("outcome") in (Outcome.DELIVERY_FAILED, Outcome.CONFIG_INVALID)
                        and row.get("event", Event.RUN_FINISHED) in WEBHOOK_EVENTS
                        and not (rex.STATE / ("grokbot-sent-" + key)).exists()):
                    history[key] = row
                    if len(history) > STATUS_HISTORY_LIMIT:
                        history.popitem(last=False)
        except (ValueError, AttributeError):
            continue
    for key, (path, envelope) in spools.items():
        row = receipts.get(key, {})
        item = DeliveryRecord(key=key, spool=str(path), event=envelope.get("event", Event.RUN_FINISHED),
                              agent_session_id=envelope.get("lineage", {}).get("agent_session_id") or envelope.get("data", {}).get("session_id", ""),
                              task_id=envelope.get("task_id", ""), rex_session=envelope.get("rex_session", ""), rex_block=envelope.get("rex_block", ""),
                              outcome=row.get("outcome", envelope.get("delivery_outcome", Outcome.PENDING)))
        (failed if item["outcome"] in (Outcome.DELIVERY_FAILED, Outcome.CONFIG_INVALID) else pending).append(item)
    failed.extend(history.values())
    with database() as db:
        sessions: list[SessionState] = [json.loads(row[0]) for row in db.execute("SELECT record FROM sessions")]
    active = [row for row in sessions if row["status"] in (Status.RUNNING, Status.WAITING)]
    inactive = sorted((row for row in sessions if row["status"] not in (Status.RUNNING, Status.WAITING)),
                      key=lambda row: row.get("updated_at", ""))[-STATUS_HISTORY_LIMIT:]
    by_key = {row["key"]: row for row in sessions}
    local_sessions = []
    for session in active + inactive:
        view = session_view(session)
        view.update(turn_id=session["turn"], lineage=lineage(session, by_key))
        local_sessions.append(view)
    return Health(pending=pending, failed=failed, running=[session_view(row) for row in active],
                  started=list(started), sessions=local_sessions)


def retry():
    rex.state_dir()
    cleanup_orphan_temps()
    results = []
    for path in rex.STATE.glob("grokbot-pending-*"):
        if path.suffix == ".tmp":
            continue
        try:
            results.append(deliver(path))
        except (OSError, ValueError, KeyError, TypeError):
            results.append(False)
    return RetryResult(delivered=results.count(True), failed=results.count(False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("test", "doctor", "deliver", "status", "retry"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "deliver":
            if not args.path:
                parser.error("deliver requires a spool path")
            deliver(Path(args.path))
            return 0
        if args.action in ("status", "retry"):
            result = health() if args.action == "status" else retry()
            print(json.dumps(result, indent=None if args.json else 2))
            return int(args.action == "retry" and bool(result.get("failed")))
        config()
        if args.action == "doctor":
            print("Callback config OK (owned file, mode 600)")
            return 0
        metadata = git_metadata(os.getcwd())
        status = post(TestPayload(event=Event.TEST, task_id=os.environ.get("GROKBOT_TASK_ID") or
                                  f"{metadata['repo'] or os.getcwd()}:{metadata['branch']}",
                                  requester=os.environ.get("GROKBOT_CALLBACK", ""), untrusted=["task_id"]))
        print(f"HTTP {status}" if status else "HTTP request failed")
        return 0 if 200 <= status < 300 else 1
    except (OSError, ValueError):
        receipt(Outcome.CONFIG_INVALID)
        if args.action != "deliver":
            print("Callback config missing or invalid; require an owned file with mode 600", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
