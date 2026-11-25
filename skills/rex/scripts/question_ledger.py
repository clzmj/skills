#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Private Mac-side question storage. These records grant no box authority."""

from __future__ import annotations

import hashlib
import hmac
import fcntl
import json
import logging
import os
import secrets
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final, Generator, NewType
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import question_model as m

OPEN_FOR_ATTACH: Final = frozenset(
    {
        m.BatchState.OPEN,
        m.BatchState.ESCALATING,
        m.BatchState.BOX_OWNED,
        m.BatchState.BLOCKED,
    }
)
TERMINAL: Final = frozenset({m.BatchState.CANCELLED, m.BatchState.STALE})
UNDISPATCHED: Final = (
    m.DeliveryState.PENDING,
    m.DeliveryState.CLAIMED,
    m.DeliveryState.DISPATCH_FAILED,
)
_TRANSITIONS: Final = {
    m.BatchState.OPEN: frozenset(
        {
            m.BatchState.OPEN,
            m.BatchState.ESCALATING,
            m.BatchState.BOX_OWNED,
            m.BatchState.BOX_ANSWERED,
            m.BatchState.CANCELLED,
            m.BatchState.STALE,
            m.BatchState.BLOCKED,
        }
    ),
    m.BatchState.ESCALATING: frozenset(
        {
            m.BatchState.OPEN,
            m.BatchState.RESOLVED_LOCAL,
            m.BatchState.BOX_OWNED,
            m.BatchState.BOX_ANSWERED,
            m.BatchState.CANCELLED,
            m.BatchState.STALE,
            m.BatchState.BLOCKED,
        }
    ),
    m.BatchState.BOX_OWNED: frozenset(
        {
            m.BatchState.BOX_OWNED,
            m.BatchState.BOX_ANSWERED,
            m.BatchState.CANCELLED,
            m.BatchState.STALE,
        }
    ),
    m.BatchState.RESOLVED_LOCAL: frozenset(
        {m.BatchState.BOX_OWNED, m.BatchState.BOX_ANSWERED, m.BatchState.STALE}
    ),
    m.BatchState.BOX_ANSWERED: frozenset(
        {m.BatchState.BOX_ANSWERED, m.BatchState.STALE}
    ),
    m.BatchState.BLOCKED: frozenset({m.BatchState.CANCELLED, m.BatchState.STALE}),
    m.BatchState.CANCELLED: frozenset(),
    m.BatchState.STALE: frozenset(),
}

DDL: Final = """
CREATE TABLE IF NOT EXISTS batch (id TEXT PRIMARY KEY, rev INTEGER NOT NULL, actor TEXT NOT NULL, root_actor TEXT NOT NULL,
 turn TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL, block_cause TEXT, cancel_cause TEXT, mac_floor INTEGER NOT NULL,
 box_floor INTEGER, escalated TEXT, context TEXT NOT NULL, asker_json TEXT NOT NULL, classification_json TEXT,
 opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, related_to TEXT, urgency TEXT NOT NULL, level INTEGER NOT NULL, brief_path TEXT);
CREATE TABLE IF NOT EXISTS question (id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batch(id), added_rev INTEGER NOT NULL,
 retired_rev INTEGER, origin_call TEXT NOT NULL, digest TEXT NOT NULL, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_call (call TEXT PRIMARY KEY, batch TEXT NOT NULL, tool TEXT NOT NULL, actor TEXT NOT NULL,
 turn TEXT NOT NULL, failed INTEGER NOT NULL DEFAULT 0, fallback_of TEXT UNIQUE, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempt (batch TEXT NOT NULL, batch_rev INTEGER NOT NULL, level INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, outcome TEXT NOT NULL, json TEXT NOT NULL, PRIMARY KEY(batch,batch_rev,level,ordinal));
CREATE TABLE IF NOT EXISTS ladder_run (batch TEXT NOT NULL, batch_rev INTEGER NOT NULL, started_at TEXT NOT NULL,
 PRIMARY KEY(batch,batch_rev));
CREATE TABLE IF NOT EXISTS resolution (batch TEXT NOT NULL, rev INTEGER NOT NULL, batch_rev INTEGER NOT NULL,
 level INTEGER NOT NULL CHECK(level IN (1,2)), json TEXT NOT NULL, PRIMARY KEY(batch,rev));
CREATE TABLE IF NOT EXISTS registration (batch TEXT NOT NULL, batch_rev INTEGER NOT NULL, content_digest TEXT NOT NULL,
 state TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_sent_at TEXT, overdue INTEGER NOT NULL DEFAULT 0,
 progress_seq INTEGER NOT NULL DEFAULT 0, acked_progress_seq INTEGER NOT NULL DEFAULT 0, acked_floor INTEGER,
 floor_raise_json TEXT, entry_json TEXT, PRIMARY KEY(batch,batch_rev));
CREATE TABLE IF NOT EXISTS progress (batch TEXT NOT NULL, seq INTEGER NOT NULL, batch_rev INTEGER NOT NULL,
 json TEXT NOT NULL, PRIMARY KEY(batch,seq));
CREATE TABLE IF NOT EXISTS box_note (delivery_id TEXT PRIMARY KEY, attempt INTEGER NOT NULL, plan_id TEXT NOT NULL,
 batch TEXT NOT NULL, batch_rev INTEGER NOT NULL, resolution_rev INTEGER NOT NULL, purpose TEXT NOT NULL,
 target_actor TEXT NOT NULL, child_actor TEXT, text TEXT NOT NULL, body_digest TEXT NOT NULL, ack_token_digest TEXT NOT NULL,
 floor INTEGER NOT NULL, state TEXT NOT NULL, acked_rev INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_seen (plan_id TEXT PRIMARY KEY, action TEXT NOT NULL, plan_digest TEXT NOT NULL, result_b64 TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lifecycle (actor TEXT NOT NULL, kind TEXT NOT NULL, call TEXT, batch TEXT, origin TEXT,
 prompt_digest TEXT, ours INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lifecycle_actor ON lifecycle(actor,kind);
CREATE TABLE IF NOT EXISTS cancellation (batch TEXT NOT NULL, batch_rev INTEGER NOT NULL, cause TEXT NOT NULL,
 prompt_digest TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS takeover (id TEXT PRIMARY KEY, batch TEXT NOT NULL, origin TEXT NOT NULL, prompt_digest TEXT,
 at TEXT NOT NULL, cleared_at TEXT, cleared_by TEXT);
CREATE TABLE IF NOT EXISTS outbound (id TEXT PRIMARY KEY, batch TEXT NOT NULL, purpose TEXT NOT NULL, resolution_rev INTEGER NOT NULL,
 target_actor TEXT NOT NULL, child_actor TEXT, text TEXT NOT NULL, body_digest TEXT NOT NULL, ack_token TEXT NOT NULL,
 route_plan_json TEXT NOT NULL, state TEXT NOT NULL, suppress_cause TEXT, paused_by TEXT REFERENCES takeover(id),
 claim_owner TEXT, claimed_at TEXT, applied_rev INTEGER, updated_at TEXT NOT NULL,
 UNIQUE(batch,purpose,resolution_rev,target_actor));
CREATE TABLE IF NOT EXISTS dispatch_attempt (outbound TEXT NOT NULL REFERENCES outbound(id), n INTEGER NOT NULL,
 route TEXT NOT NULL, result TEXT, started_at TEXT NOT NULL, finished_at TEXT, PRIMARY KEY(outbound,n));
CREATE TABLE IF NOT EXISTS inline_waiter (batch TEXT NOT NULL, event TEXT NOT NULL, hook_pid INTEGER NOT NULL,
 deadline TEXT NOT NULL, PRIMARY KEY(batch,event));
CREATE TABLE IF NOT EXISTS notice (id TEXT PRIMARY KEY, kind TEXT NOT NULL, task TEXT NOT NULL, batch TEXT,
 detail TEXT NOT NULL, created_at TEXT NOT NULL, spooled INTEGER NOT NULL DEFAULT 0, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS digest (id TEXT PRIMARY KEY, task TEXT NOT NULL, register_json TEXT NOT NULL,
 progress_json TEXT NOT NULL, notices_json TEXT NOT NULL, spooled_at TEXT NOT NULL, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS budget (scope TEXT NOT NULL, key TEXT NOT NULL, attempts INTEGER NOT NULL,
 tokens_reserved INTEGER NOT NULL, tokens_spent INTEGER NOT NULL, usd_reserved TEXT NOT NULL, usd_spent TEXT NOT NULL,
 PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS budget_reservation (id TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL,
 day TEXT NOT NULL, tokens INTEGER NOT NULL, usd TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0,
 overrun INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00');
CREATE TABLE IF NOT EXISTS classifier_pending (batch TEXT NOT NULL REFERENCES batch(id), batch_rev INTEGER NOT NULL,
 remote TEXT NOT NULL, brief_json TEXT, root_session TEXT NOT NULL, started_at TEXT NOT NULL,
 PRIMARY KEY(batch,batch_rev));
CREATE TABLE IF NOT EXISTS tick_state (id INTEGER PRIMARY KEY CHECK(id=1), last_run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


class IllegalTransition(ValueError):
    pass


def check_transition(source: m.BatchState, target: m.BatchState) -> None:
    if target not in _TRANSITIONS[source]:
        raise IllegalTransition(f"{source} -> {target}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def encoded(value: object) -> str:
    return m.canonical(value).decode("utf-8")


def redacted_audit_detail(detail: str) -> str:
    """Scan content values, never the serialized record or its identifiers."""
    from question_redact import redact

    identifiers = {
        "id",
        "batch",
        "batch_id",
        "question",
        "context",
        "content_digest",
        "prompt_digest",
        "head_sha",
        "actor",
        "root_actor",
        "target_actor",
        "child_actor",
        "session",
        "turn",
        "task",
        "origin_call",
        "call",
        "fallback_of",
        "digest",
        "related_to",
    }

    def clean(value: object, cap: int, key: str = "") -> object:
        if isinstance(value, dict):
            return {field: clean(item, cap, field) for field, item in value.items()}
        if isinstance(value, list):
            return [clean(item, cap) for item in value]
        if isinstance(value, str):
            return (
                value
                if key in identifiers or key.endswith(("_id", "_digest"))
                else redact(value).text[:cap]
            )
        return value

    try:
        record = json.loads(detail)
    except ValueError:
        return redact(detail).text[:500]
    cap = 500
    while True:
        saved = encoded(clean(record, cap))
        # Keep identifiers intact while shrinking content, then use a bounded fallback.
        if len(saved) <= 500:
            return saved
        if cap == 0:
            return encoded({"truncated": True, "content_digest": m.digest(detail)})
        cap //= 2


@dataclass(frozen=True, slots=True)
class Claim:
    outbound: str
    owner: str
    attempt: int
    route: m.Route
    previous_state: m.DeliveryState = m.DeliveryState.PENDING


@dataclass(frozen=True, slots=True)
class LiveContext:
    head_sha: m.HeadSha
    brief_digest: m.TextDigest | None
    changed: bool
    brief_path: Path | None = None


@dataclass(frozen=True, slots=True)
class PendingClassification:
    remote: str
    brief: m.BriefEscalation | None
    root_session: str
    started_at: datetime


class AuditKind(StrEnum):
    BATCH = "batch"
    ATTEMPT = "attempt"
    RESOLUTION = "resolution"
    REGISTRATION_SENT = "registration_sent"
    REGISTRATION_ACK = "registration_ack"
    BOX_NOTE = "box_note"
    OUTBOUND = "outbound"
    DISPATCH = "dispatch"
    CANCELLATION = "cancellation"
    TAKEOVER = "takeover"
    CLEARANCE = "clearance"
    NOTICE = "notice"
    BUDGET = "budget"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    kind: AuditKind
    task: str
    batch: str | None
    detail: str
    at: datetime


@dataclass(frozen=True, slots=True)
class L0Event:
    task_id: str
    text: str
    batch_id: str | None
    metadata: m.DigestMetadata | None = None

    @classmethod
    def parse(cls, value: object) -> L0Event:
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        task = value.get("task_id")
        text = value.get("text")
        batch = value.get("batch_id")
        if (
            not isinstance(task, str)
            or not isinstance(text, str)
            or (batch is not None and not isinstance(batch, str))
        ):
            raise ValueError("invalid event")
        raw_metadata = value.get("metadata")
        metadata = (
            m.parse_record(m.DigestMetadata, raw_metadata)
            if raw_metadata is not None
            else None
        )
        return cls(m.parse_identity(task, "task_id"), text, batch, metadata)


@dataclass(frozen=True, slots=True)
class BudgetRecord:
    attempts: int
    tokens_reserved: int
    tokens_spent: int
    usd_reserved: Decimal
    usd_spent: Decimal

    @classmethod
    def parse(cls, row: sqlite3.Row) -> BudgetRecord:
        try:
            return cls(
                int(row["attempts"]),
                int(row["tokens_reserved"]),
                int(row["tokens_spent"]),
                Decimal(row["usd_reserved"]),
                Decimal(row["usd_spent"]),
            )
        except InvalidOperation as exc:
            raise ValueError("invalid budget decimal") from exc


ReservationId = NewType("ReservationId", str)


class Ledger:
    """One connection per process/thread. Mutations serialize with BEGIN IMMEDIATE."""

    def __init__(self, path: Path):
        self.path = path
        self._pending_audits: list[tuple[AuditKind, str, str, str | None]] = []
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.transaction():
            # executescript would implicitly commit; execute each statement instead.
            for statement in DDL.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            for table in ("notice", "digest"):
                columns = {
                    row["name"]
                    for row in self.db.execute(f"PRAGMA table_info({table})")
                }
                if "metadata_json" not in columns:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN metadata_json TEXT"
                    )
            reservation_columns = {
                row["name"]
                for row in self.db.execute("PRAGMA table_info(budget_reservation)")
            }
            if "created_at" not in reservation_columns:
                self.db.execute(
                    "ALTER TABLE budget_reservation ADD COLUMN created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'"
                )
            states = ",".join(f"'{state.value}'" for state in sorted(OPEN_FOR_ATTACH))
            self.db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS batch_one_open ON batch(actor,turn) WHERE state IN ({states})"
            )
            if not self.db.execute("SELECT version FROM schema_version").fetchone():
                self.db.execute("INSERT INTO schema_version VALUES(1)")

    def close(self) -> None:
        self.db.close()

    def audit(
        self, kind: AuditKind, task: str, detail: str, batch: str | None = None
    ) -> None:
        if self.db.in_transaction:
            self._pending_audits.append((kind, task, detail, batch))
            return
        path = self.path.parent / m.AUDIT_FILE
        lock_fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size >= 1024 * 1024:
                path.replace(path.with_suffix(".jsonl.1"))
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(
                    encoded(
                        AuditEvent(
                            kind, task, batch, redacted_audit_detail(detail), now()
                        )
                    )
                    + "\n"
                )
        finally:
            os.close(lock_fd)

    def audit_events(self, task: str) -> tuple[AuditEvent, ...]:
        result = []
        for path in (
            self.path.parent / (m.AUDIT_FILE + ".1"),
            self.path.parent / m.AUDIT_FILE,
        ):
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                value = json.loads(line)
                event = AuditEvent(
                    AuditKind(value["kind"]),
                    value["task"],
                    value["batch"],
                    value["detail"],
                    datetime.fromisoformat(value["at"]),
                )
                if event.task == task:
                    result.append(event)
        return tuple(result)

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        if self.db.in_transaction:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self._pending_audits.clear()
            raise
        pending, self._pending_audits = self._pending_audits, []
        for kind, task, detail, batch in pending:
            try:
                self.audit(kind, task, detail, batch)
            except OSError:
                logging.getLogger(__name__).exception(
                    "could not write post-commit audit event (%s)", kind
                )

    def get_batch(self, batch_id: str) -> m.QuestionBatch:
        row = self.db.execute("SELECT * FROM batch WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(batch_id)
        classification = json.loads(row["classification_json"])
        classified = classification.get("classified", False)
        classification["classified"] = classified
        classification["floor"] = max(
            classification["floor"],
            row["mac_floor"],
            row["box_floor"] or 0,
            m.EscalationLevel.L3_PRINCIPAL if not classified else 0,
        )
        return m.parse_batch(
            {
                "id": row["id"],
                "rev": row["rev"],
                "asker": json.loads(row["asker_json"]),
                "questions": [
                    json.loads(q[0])
                    for q in self.db.execute(
                        "SELECT json FROM question WHERE batch=? ORDER BY rowid",
                        (batch_id,),
                    )
                ],
                "calls": [
                    {**dict(c), "failed": bool(c["failed"])}
                    for c in self.db.execute(
                        "SELECT tool,call,actor,turn,seen_at,failed,fallback_of FROM native_call WHERE batch=? ORDER BY rowid",
                        (batch_id,),
                    )
                ],
                "urgency": row["urgency"],
                "state": row["state"],
                "classification": classification,
                "context": row["context"],
                "level": row["level"],
                "opened_at": row["opened_at"],
            }
        )

    def guard_floor(self, batch_id: str) -> m.EscalationLevel:
        """Return committed floors without the conservative unclassified read floor."""
        row = self.db.execute(
            "SELECT mac_floor,box_floor FROM batch WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return m.EscalationLevel(max(row["mac_floor"], row["box_floor"] or 0))

    def start_ladder(
        self, batch_id: str, rev: m.BatchRev, context: m.ContextDigest
    ) -> bool:
        """Admit one paid run per revision; lost completion recovers through the box."""
        with self.transaction():
            batch = self.get_batch(batch_id)
            if (
                batch.rev != rev
                or batch.context != context
                or batch.state
                not in {
                    m.BatchState.OPEN,
                    m.BatchState.ESCALATING,
                }
            ):
                return False
            return bool(
                self.db.execute(
                    "INSERT OR IGNORE INTO ladder_run VALUES(?,?,?)",
                    (batch_id, rev, now().isoformat()),
                ).rowcount
            )

    def queue_classification(
        self,
        batch_id: str,
        *,
        remote: str,
        brief: m.BriefEscalation | None,
        root_session: str,
    ) -> None:
        """Persist first-seen inputs and deadline origin for the current revision."""
        if not isinstance(remote, str) or not isinstance(root_session, str):
            raise ValueError("classification metadata must be strings")
        if brief is not None:
            if not isinstance(brief, m.BriefEscalation):
                raise ValueError("invalid classification brief")
            brief = m.parse_record(m.BriefEscalation, json.loads(encoded(brief)))
        with self.transaction():
            row = self.db.execute(
                "SELECT rev FROM batch WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(batch_id)
            self.db.execute(
                """INSERT INTO classifier_pending VALUES(?,?,?,?,?,?)
                ON CONFLICT(batch,batch_rev) DO NOTHING""",
                (
                    batch_id,
                    row["rev"],
                    remote,
                    encoded(brief) if brief is not None else None,
                    root_session,
                    now().isoformat(),
                ),
            )

    def classification_pending(
        self, batch_id: str, batch_rev: m.BatchRev
    ) -> PendingClassification | None:
        row = self.db.execute(
            "SELECT * FROM classifier_pending WHERE batch=? AND batch_rev=?",
            (batch_id, batch_rev),
        ).fetchone()
        if row is None:
            return None
        return PendingClassification(
            row["remote"],
            m.parse_record(m.BriefEscalation, json.loads(row["brief_json"]))
            if row["brief_json"] is not None
            else None,
            row["root_session"],
            datetime.fromisoformat(row["started_at"]),
        )

    def apply_classification(
        self,
        batch_id: str,
        expected_rev: m.BatchRev,
        context: m.ContextDigest,
        classification: m.Classification,
    ) -> bool:
        """Apply proof only to its revision; stale results can only raise the floor."""
        if not isinstance(classification, m.Classification):
            raise ValueError("classification must be a typed result")
        classification = m.parse_record(
            m.Classification, json.loads(encoded(classification))
        )
        if not classification.classified:
            raise ValueError("classification must be complete")
        with self.transaction():
            row = self.db.execute(
                "SELECT rev,context,mac_floor,box_floor,state,task FROM batch WHERE id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["state"] in TERMINAL:
                return False
            if row["rev"] > expected_rev:
                # Questions are retained across revisions, including retired ones.
                # A stale result grants no new proof, but its higher floor survives.
                self.db.execute(
                    "UPDATE batch SET mac_floor=max(mac_floor,?) WHERE id=?",
                    (classification.floor, batch_id),
                )
                if classification.floor > max(row["mac_floor"], row["box_floor"] or 0):
                    self.audit(
                        AuditKind.CONTEXT,
                        row["task"],
                        f"Floor {classification.floor} from source revision r{expected_rev} carried floor-only to current revision r{row['rev']}; current proof unchanged",
                        batch_id,
                    )
                return False
            if row["rev"] != expected_rev or row["context"] != context:
                return False
            registered = self.db.execute(
                "SELECT entry_json FROM registration WHERE batch=? AND batch_rev=?",
                (batch_id, expected_rev),
            ).fetchone()
            unclassified_entry = bool(
                registered
                and registered["entry_json"]
                and not json.loads(registered["entry_json"])["classified"]
            )
            floor = max(classification.floor, row["mac_floor"], row["box_floor"] or 0)
            if unclassified_entry or row["state"] == m.BatchState.BOX_OWNED:
                floor = max(floor, m.EscalationLevel.L3_PRINCIPAL)
            classification = replace(classification, floor=m.EscalationLevel(floor))
            self.db.execute(
                "UPDATE batch SET classification_json=?,mac_floor=?,updated_at=? WHERE id=?",
                (encoded(classification), floor, now().isoformat(), batch_id),
            )
            return True

    def batches(self, *, task: str | None = None) -> tuple[m.QuestionBatch, ...]:
        rows = self.db.execute(
            "SELECT id FROM batch" + (" WHERE task=?" if task is not None else ""),
            () if task is None else (task,),
        )
        return tuple(self.get_batch(row[0]) for row in rows)

    def _related(
        self, asker: m.AskerRef, questions: tuple[m.Question, ...]
    ) -> str | None:
        text_digests = {m.digest(question.text.text) for question in questions}
        rows = self.db.execute(
            """SELECT b.id,q.json FROM batch b JOIN question q ON q.batch=b.id
            WHERE b.root_actor=? AND (EXISTS(SELECT 1 FROM resolution r WHERE r.batch=b.id)
                OR EXISTS(SELECT 1 FROM box_note n WHERE n.batch=b.id)) ORDER BY b.updated_at DESC""",
            (asker.root_actor,),
        )
        for row in rows:
            if (
                m.digest(m.parse_question(json.loads(row["json"])).text.text)
                in text_digests
            ):
                return row["id"]
        return None

    def prior_context(self, batch_id: str) -> tuple[m.Resolution | m.BoxNote, ...]:
        row = self.db.execute(
            "SELECT related_to FROM batch WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return ()
        resolutions = tuple(
            m.parse_record(m.Resolution, json.loads(item[0]))
            for item in self.db.execute(
                "SELECT json FROM resolution WHERE batch=? ORDER BY rev DESC LIMIT 1",
                (row[0],),
            )
        )
        notes = tuple(
            m.parse_box_note(dict(item))
            for item in self.db.execute(
                "SELECT * FROM box_note WHERE batch=? ORDER BY resolution_rev DESC LIMIT 1",
                (row[0],),
            )
        )
        return resolutions + notes

    def _save_batch(
        self,
        batch: m.QuestionBatch,
        related_to: str | None = None,
        *,
        brief: Path | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO batch(id,rev,actor,root_actor,turn,task,state,mac_floor,context,asker_json,
         classification_json,opened_at,updated_at,related_to,urgency,level,brief_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(id) DO UPDATE SET rev=excluded.rev,state=excluded.state,mac_floor=max(batch.mac_floor,excluded.mac_floor),
         context=excluded.context,asker_json=excluded.asker_json,classification_json=excluded.classification_json,
         updated_at=excluded.updated_at,level=excluded.level""",
            (
                str(batch.id),
                batch.rev,
                batch.asker.actor,
                batch.asker.root_actor,
                batch.asker.turn,
                batch.asker.task,
                batch.state,
                batch.classification.floor,
                batch.context,
                encoded(batch.asker),
                encoded(batch.classification),
                batch.opened_at.isoformat(),
                now().isoformat(),
                related_to,
                batch.urgency,
                batch.level,
                str(brief) if brief else None,
            ),
        )

    def _revision(
        self,
        batch: m.QuestionBatch,
        cause: m.SuppressCause,
        *,
        brief: Path | None = None,
    ) -> None:
        self._save_batch(batch, brief=brief)
        self.db.execute(
            "INSERT OR IGNORE INTO registration(batch,batch_rev,content_digest,state) VALUES(?,?,?,?)",
            (str(batch.id), batch.rev, "", m.RegistrationState.DUE),
        )
        self.db.execute(
            "UPDATE outbound SET state=?,suppress_cause=?,updated_at=? WHERE batch=? AND state IN (?,?,?) AND purpose=?",
            (
                m.DeliveryState.SUPPRESSED,
                cause,
                now().isoformat(),
                str(batch.id),
                *UNDISPATCHED,
                m.OutboundPurpose.ANSWER,
            ),
        )
        self._settle_dispatch_attempts(str(batch.id), m.DeliveryState.SUPPRESSED)

    def record_attempt(self, attempt: m.Attempt) -> m.Attempt:
        """Finalize against the current revision; settled history is immutable."""
        with self.transaction():
            batch = self.get_batch(str(attempt.batch))
            if batch.state in TERMINAL or batch.state in {
                m.BatchState.BOX_OWNED,
                m.BatchState.BOX_ANSWERED,
            }:
                attempt = replace(attempt, outcome=m.AttemptOutcome.SUPERSEDED)
            elif batch.rev != attempt.batch_rev or batch.context != attempt.context:
                attempt = replace(attempt, outcome=m.AttemptOutcome.STALE_REVISION)
            self.db.execute(
                "INSERT INTO attempt VALUES(?,?,?,?,?,?)",
                (
                    str(attempt.batch),
                    attempt.batch_rev,
                    attempt.answerer.level,
                    attempt.ordinal,
                    attempt.outcome,
                    encoded(attempt),
                ),
            )
            self.audit(
                AuditKind.ATTEMPT, batch.asker.task, encoded(attempt), str(batch.id)
            )
            return attempt

    def open_or_attach(
        self,
        asker: m.AskerRef,
        questions: tuple[m.Question, ...],
        call: m.NativeCall,
        classification: m.Classification,
        urgency: m.Urgency = m.Urgency.BLOCKING,
        *,
        brief: Path | None = None,
    ) -> m.AttachResult:
        with self.transaction():
            duplicate = self.db.execute(
                "SELECT batch FROM native_call WHERE call=?", (call.call,)
            ).fetchone()
            if duplicate:
                return m.AttachResult(
                    m.AttachOutcome.DUPLICATE_CALL, self.get_batch(duplicate[0])
                )
            states = tuple(sorted(OPEN_FOR_ATTACH))
            row = self.db.execute(
                "SELECT id FROM batch WHERE actor=? AND turn=? AND state IN (?,?,?,?)",
                (asker.actor, asker.turn, *states),
            ).fetchone()
            if row is None:
                bid = m.BatchId(uuid4())
                rev = m.BatchRev(1)
                context = m.context_digest(
                    bid, rev, questions, asker.head_sha, asker.brief_digest
                )
                batch = m.QuestionBatch(
                    bid,
                    rev,
                    asker,
                    questions,
                    (call,),
                    urgency,
                    m.BatchState.OPEN,
                    classification,
                    context,
                    m.EscalationLevel(1),
                    now(),
                )
                outcome = m.AttachOutcome.OPENED
            else:
                previous = self.get_batch(row[0])
                rev = m.BatchRev(previous.rev + 1)
                old_questions = previous.questions
                failed = self.db.execute(
                    """SELECT call FROM native_call WHERE batch=? AND tool=? AND failed=1
                    AND call NOT IN (SELECT fallback_of FROM native_call WHERE fallback_of IS NOT NULL)""",
                    (str(previous.id), m.QuestionTool.CODEX_BLOCKING),
                ).fetchall()
                outcome = m.AttachOutcome.ATTACHED
                if call.tool == m.QuestionTool.CODEX_ASYNC and len(failed) == 1:
                    call = replace(call, fallback_of=m.NativeCallId(failed[0][0]))
                    old_questions = tuple(
                        replace(q, retired_rev=rev)
                        if q.origin_call == call.fallback_of
                        else q
                        for q in old_questions
                    )
                    outcome = m.AttachOutcome.REVISED
                all_questions = old_questions + questions
                classification = replace(
                    classification,
                    floor=max(
                        classification.floor,
                        previous.classification.floor
                        if previous.classification.classified
                        else self.guard_floor(str(previous.id)),
                    ),
                    hits=tuple(
                        dict.fromkeys(
                            previous.classification.hits + classification.hits
                        )
                    ),
                )
                state = (
                    previous.state
                    if previous.state in {m.BatchState.BOX_OWNED, m.BatchState.BLOCKED}
                    else m.BatchState.OPEN
                )
                batch = replace(
                    previous,
                    rev=rev,
                    questions=all_questions,
                    calls=previous.calls + (call,),
                    state=state,
                    classification=classification,
                    context=m.context_digest(
                        previous.id,
                        rev,
                        all_questions,
                        previous.asker.head_sha,
                        previous.asker.brief_digest,
                    ),
                )
            self._revision(batch, m.SuppressCause.NEWER_REVISION, brief=brief)
            if outcome == m.AttachOutcome.OPENED:
                related = self._related(asker, questions)
                if related:
                    self.db.execute(
                        "UPDATE batch SET related_to=? WHERE id=?",
                        (related, str(batch.id)),
                    )
                    self.audit(
                        AuditKind.CONTEXT,
                        asker.task,
                        encoded({"related_to": related, "context_only": True}),
                        str(batch.id),
                    )
            for question in batch.questions:
                self.db.execute(
                    """INSERT INTO question VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE
                    SET retired_rev=excluded.retired_rev,json=excluded.json""",
                    (
                        str(question.id),
                        str(batch.id),
                        batch.rev,
                        question.retired_rev,
                        question.origin_call,
                        question.digest,
                        encoded(question),
                    ),
                )
            self.db.execute(
                "INSERT INTO native_call VALUES(?,?,?,?,?,?,?,?)",
                (
                    call.call,
                    str(batch.id),
                    call.tool,
                    call.actor,
                    call.turn,
                    call.failed,
                    call.fallback_of,
                    call.seen_at.isoformat(),
                ),
            )
            return m.AttachResult(outcome, batch)

    def transition(
        self,
        batch_id: str,
        target: m.BatchState,
        *,
        block_cause: m.BlockCause | None = None,
    ) -> m.QuestionBatch:
        with self.transaction():
            batch = self.get_batch(batch_id)
            # A newly attached revision can cross the wire count cap after
            # ownership. This narrow safety block must not reopen delivery or
            # broaden the ordinary transition table (§5.6.1 / §6.5).
            cap_block = (
                batch.state is m.BatchState.BOX_OWNED
                and target is m.BatchState.BLOCKED
                and block_cause is m.BlockCause.TOO_LARGE
            )
            if not cap_block:
                check_transition(batch.state, target)
            if target == m.BatchState.BLOCKED and block_cause is None:
                raise ValueError("a blocked batch requires a cause")
            result = replace(batch, state=target)
            self._save_batch(result)
            if block_cause:
                self.db.execute(
                    "UPDATE batch SET block_cause=? WHERE id=?", (block_cause, batch_id)
                )
            self.audit(
                AuditKind.BATCH,
                batch.asker.task,
                f"{batch.state} -> {target}",
                batch_id,
            )
            return result

    def mark_failed(self, call: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE native_call SET failed=1 WHERE call=?", (call,))

    def put_registration(self, entry: m.RegisterEntry) -> None:
        entry = m.parse_register_entry(entry)
        with self.transaction():
            existing = self.db.execute(
                "SELECT content_digest FROM registration WHERE batch=? AND batch_rev=?",
                (entry["batch_id"], entry["batch_rev"]),
            ).fetchone()
            if existing and existing[0] and existing[0] != entry["content_digest"]:
                raise ValueError("immutable registration changed")
            self.db.execute(
                """INSERT INTO registration(batch,batch_rev,content_digest,state,entry_json) VALUES(?,?,?,?,?)
                ON CONFLICT(batch,batch_rev) DO UPDATE SET content_digest=excluded.content_digest,entry_json=excluded.entry_json""",
                (
                    entry["batch_id"],
                    entry["batch_rev"],
                    entry["content_digest"],
                    m.RegistrationState.DUE,
                    encoded(entry),
                ),
            )

            self.db.execute(
                "UPDATE batch SET mac_floor=max(mac_floor,?) WHERE id=?",
                (
                    max(
                        entry["mac_floor"],
                        m.EscalationLevel.L3_PRINCIPAL
                        if not entry["classified"]
                        else 0,
                    ),
                    entry["batch_id"],
                ),
            )

    def progress(self, value: m.RegisterProgress) -> m.RegisterProgress:
        value = m.parse_register_progress(value)
        with self.transaction():
            previous = self.db.execute(
                "SELECT floor_raise_json FROM registration WHERE batch=? AND batch_rev=?",
                (value["batch_id"], value["batch_rev"]),
            ).fetchone()
            if previous is None:
                raise KeyError(value["batch_id"])
            cumulative = json.loads(previous[0]) if previous[0] else None
            raised = value["floor_raise"]
            if cumulative and (raised is None or cumulative["floor"] > raised["floor"]):
                value = m.parse_register_progress({**value, "floor_raise": cumulative})
            last = self.db.execute(
                "SELECT coalesce(max(seq),0) FROM progress WHERE batch=?",
                (value["batch_id"],),
            ).fetchone()[0]
            if value["seq"] <= last:
                raise ValueError("progress sequence must increase")
            self.db.execute(
                "INSERT INTO progress VALUES(?,?,?,?)",
                (value["batch_id"], value["seq"], value["batch_rev"], encoded(value)),
            )
            self.db.execute(
                "UPDATE registration SET progress_seq=?,floor_raise_json=? WHERE batch=? AND batch_rev=?",
                (
                    value["seq"],
                    encoded(value["floor_raise"]) if value["floor_raise"] else None,
                    value["batch_id"],
                    value["batch_rev"],
                ),
            )
            return value

    def ack_registration(self, body: m.AckBody) -> None:
        with self.transaction():
            row = self.db.execute(
                "SELECT content_digest FROM registration WHERE batch=? AND batch_rev=?",
                (body["batch_id"], body["batch_rev"]),
            ).fetchone()
            if row is None or row[0] != body["content_digest"]:
                raise ValueError("unknown registration digest")
            self.db.execute(
                """UPDATE registration SET state=?,acked_progress_seq=max(acked_progress_seq,min(?,progress_seq)),
                acked_floor=max(coalesce(acked_floor,0),?) WHERE batch=? AND batch_rev=?""",
                (
                    m.RegistrationState.ACKED,
                    body["progress_seq"],
                    body["floor"],
                    body["batch_id"],
                    body["batch_rev"],
                ),
            )
            batch = self.get_batch(body["batch_id"])
            self.db.execute(
                "UPDATE batch SET box_floor=max(coalesce(box_floor,0),?) WHERE id=?",
                (body["floor"], body["batch_id"]),
            )
            if body["owned"] and batch.state in {
                m.BatchState.OPEN,
                m.BatchState.ESCALATING,
                m.BatchState.RESOLVED_LOCAL,
            }:
                self.take_ownership(body["batch_id"], body["floor"])
            self.audit(
                AuditKind.REGISTRATION_ACK,
                batch.asker.task,
                encoded(body),
                str(batch.id),
            )

    def take_ownership(self, batch_id: str, floor: m.EscalationLevel) -> bool:
        with self.transaction():
            batch = self.get_batch(batch_id)
            dispatched = bool(
                self.db.execute(
                    "SELECT 1 FROM outbound WHERE batch=? AND purpose=? AND state IN (?,?,?,?,?)",
                    (
                        batch_id,
                        m.OutboundPurpose.ANSWER,
                        m.DeliveryState.CLAIMED,
                        m.DeliveryState.DISPATCH_UNCERTAIN,
                        m.DeliveryState.DISPATCHED,
                        m.DeliveryState.OBSERVED,
                        m.DeliveryState.APPLIED,
                    ),
                ).fetchone()
            )
            if batch.state in {
                m.BatchState.OPEN,
                m.BatchState.ESCALATING,
                m.BatchState.RESOLVED_LOCAL,
            }:
                self.transition(batch_id, m.BatchState.BOX_OWNED)
            self.db.execute(
                "UPDATE batch SET mac_floor=max(mac_floor,?) WHERE id=?",
                (floor, batch_id),
            )
            self.db.execute(
                "UPDATE outbound SET state=?,suppress_cause=? WHERE batch=? AND purpose=? AND state IN (?,?)",
                (
                    m.DeliveryState.SUPPRESSED,
                    m.SuppressCause.BOX_OWNED,
                    batch_id,
                    m.OutboundPurpose.ANSWER,
                    m.DeliveryState.PENDING,
                    m.DeliveryState.DISPATCH_FAILED,
                ),
            )
            if self.get_batch(batch_id).state == m.BatchState.BOX_OWNED:
                target = (
                    batch.asker.root_actor
                    if batch.asker.role == m.ActorRole.NATIVE_SUBAGENT
                    else batch.asker.actor
                )
                child = (
                    batch.asker.actor
                    if batch.asker.role == m.ActorRole.NATIVE_SUBAGENT
                    else None
                )
                text = m.render_pending(batch.id, child_actor=child)
                self.put_outbound(
                    m.OutboundMessage(
                        m.OutboundId(uuid4()),
                        batch.id,
                        m.OutboundPurpose.PENDING_NOTICE,
                        m.ResolutionRev(0),
                        target,
                        child,
                        text,
                        m.body_digest(text),
                        secrets.token_hex(16),
                        (
                            m.Route.CODEX_QUEUE,
                            m.Route.CODEX_QUEUE_RESUME,
                            m.Route.REX_SEND,
                        )
                        if batch.asker.agent is m.AgentKind.CODEX
                        else (m.Route.REX_SEND,),
                        m.DeliveryState.PENDING,
                        None,
                        None,
                        None,
                        None,
                        now(),
                    )
                )
            return dispatched

    def due_registrations(self) -> tuple[m.RegisterEntry, ...]:
        rows = self.db.execute(
            "SELECT entry_json FROM registration WHERE (state<>? OR progress_seq>acked_progress_seq) AND entry_json IS NOT NULL ORDER BY rowid",
            (m.RegistrationState.ACKED,),
        )
        return tuple(m.parse_register_entry(json.loads(row[0])) for row in rows)

    def due_progress(self) -> tuple[m.RegisterProgress, ...]:
        rows = self.db.execute("""SELECT p.json FROM progress p JOIN registration r ON r.batch=p.batch AND r.batch_rev=p.batch_rev
            WHERE p.seq=r.progress_seq AND p.seq>r.acked_progress_seq ORDER BY p.rowid""")
        return tuple(m.parse_register_progress(json.loads(row[0])) for row in rows)

    def registration_sent(
        self, batch_id: str, rev: int, *, at: datetime | None = None
    ) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE registration SET state=CASE WHEN state=? THEN ? ELSE ? END,tries=tries+1,last_sent_at=?,overdue=(tries+1>=?) WHERE batch=? AND batch_rev=?",
                (
                    m.RegistrationState.ACKED,
                    m.RegistrationState.ACKED,
                    m.RegistrationState.SENT,
                    (at or now()).isoformat(),
                    m.REGISTER_MAX_TRIES,
                    batch_id,
                    rev,
                ),
            )

            batch = self.get_batch(batch_id)
            self.audit(
                AuditKind.REGISTRATION_SENT,
                batch.asker.task,
                f"revision {rev}",
                batch_id,
            )
            row = self.db.execute(
                "SELECT tries FROM registration WHERE batch=? AND batch_rev=?",
                (batch_id, rev),
            ).fetchone()
            if row and row[0] == m.REGISTER_MAX_TRIES:
                self.notice(
                    m.NoticeKind.REGISTRATION_OVERDUE,
                    batch.asker.task,
                    "Registration not acknowledged",
                    batch_id,
                )

    def put_outbound(self, outbound: m.OutboundMessage) -> None:
        with self.transaction():
            inserted = self.db.execute(
                """INSERT OR IGNORE INTO outbound(id,batch,purpose,resolution_rev,target_actor,child_actor,text,body_digest,
                ack_token,route_plan_json,state,suppress_cause,paused_by,claim_owner,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(outbound.id),
                    str(outbound.batch),
                    outbound.purpose,
                    outbound.resolution_rev,
                    outbound.target_actor,
                    outbound.child_actor,
                    outbound.text,
                    outbound.body_digest,
                    outbound.ack_token,
                    encoded(outbound.route_plan),
                    outbound.state,
                    outbound.suppress_cause,
                    str(outbound.paused_by) if outbound.paused_by else None,
                    outbound.claim_owner,
                    outbound.claimed_at.isoformat() if outbound.claimed_at else None,
                    outbound.updated_at.isoformat(),
                ),
            )

            if not inserted.rowcount:
                return
            batch = self.get_batch(str(outbound.batch))
            self.audit(
                AuditKind.OUTBOUND,
                batch.asker.task,
                encoded({"outbound_id": str(outbound.id), "state": outbound.state}),
                str(batch.id),
            )

    def get_outbound(self, outbound: str) -> m.OutboundMessage:
        row = self.db.execute(
            "SELECT * FROM outbound WHERE id=?", (outbound,)
        ).fetchone()
        if row is None:
            raise KeyError(outbound)
        return m.parse_outbound(
            {
                "id": row["id"],
                "batch": row["batch"],
                "purpose": row["purpose"],
                "resolution_rev": row["resolution_rev"],
                "target_actor": row["target_actor"],
                "child_actor": row["child_actor"],
                "text": row["text"],
                "body_digest": row["body_digest"],
                "ack_token": row["ack_token"],
                "route_plan": json.loads(row["route_plan_json"]),
                "state": row["state"],
                "suppress_cause": row["suppress_cause"],
                "claim_owner": row["claim_owner"],
                "claimed_at": row["claimed_at"],
                "paused_by": row["paused_by"],
                "updated_at": row["updated_at"],
            }
        )

    def outbounds(self, batch: str) -> tuple[m.OutboundMessage, ...]:
        return tuple(
            self.get_outbound(row[0])
            for row in self.db.execute(
                "SELECT id FROM outbound WHERE batch=? ORDER BY rowid", (batch,)
            )
        )

    def _settle_dispatch_attempts(self, batch: str, state: m.DeliveryState) -> None:
        self.db.execute(
            "UPDATE dispatch_attempt SET result=?,finished_at=? WHERE result IS NULL AND outbound IN (SELECT id FROM outbound WHERE batch=? AND state=?)",
            (state, now().isoformat(), batch, state),
        )

    def observe_outbounds(self, actor: str, prompt: str) -> bool:
        """Recompute stored text digests; observations cannot resurrect suppression."""
        matched = False
        with self.transaction():
            for row in self.db.execute(
                "SELECT id,text FROM outbound WHERE target_actor=?", (actor,)
            ):
                if m.body_digest(row["text"]) != m.body_digest(prompt):
                    continue
                matched = True
                self.db.execute(
                    "UPDATE outbound SET state=?,updated_at=? WHERE id=? AND state IN (?,?,?,?,?)",
                    (
                        m.DeliveryState.OBSERVED,
                        now().isoformat(),
                        row["id"],
                        m.DeliveryState.PENDING,
                        m.DeliveryState.DISPATCH_FAILED,
                        m.DeliveryState.CLAIMED,
                        m.DeliveryState.DISPATCHED,
                        m.DeliveryState.DISPATCH_UNCERTAIN,
                    ),
                )
                row_batch = self.get_outbound(row["id"]).batch
                self._settle_dispatch_attempts(str(row_batch), m.DeliveryState.OBSERVED)
        return matched

    def resolve(
        self, resolution: m.Resolution, outbounds: tuple[m.OutboundMessage, ...]
    ) -> None:
        if resolution.by.level not in {m.EscalationLevel(1), m.EscalationLevel(2)}:
            raise ValueError("local resolutions must be L1/L2")
        with self.transaction():
            batch = self.get_batch(str(resolution.batch))
            if (
                resolution.batch_rev != batch.rev
                or resolution.by.level < batch.classification.floor
            ):
                raise ValueError("stale or below floor")
            current = self.db.execute(
                "SELECT coalesce(max(rev),0) FROM resolution WHERE batch=?",
                (str(batch.id),),
            ).fetchone()[0]
            if resolution.rev != current + 1 or not outbounds:
                raise ValueError("resolution must advance with intents")
            if any(
                o.batch != resolution.batch
                or o.resolution_rev != resolution.rev
                or o.purpose != m.OutboundPurpose.ANSWER
                for o in outbounds
            ):
                raise ValueError("intent does not belong to resolution")
            if batch.state != m.BatchState.RESOLVED_LOCAL:
                self.transition(str(batch.id), m.BatchState.RESOLVED_LOCAL)
            elif self.db.execute(
                "SELECT 1 FROM resolution WHERE batch=? AND batch_rev=?",
                (str(batch.id), batch.rev),
            ).fetchone():
                raise ValueError("this revision is already resolved")
            self.db.execute(
                "INSERT INTO resolution VALUES(?,?,?,?,?)",
                (
                    str(batch.id),
                    resolution.rev,
                    batch.rev,
                    resolution.by.level,
                    encoded(resolution),
                ),
            )
            for outbound in outbounds:
                self.put_outbound(outbound)

            self.audit(
                AuditKind.RESOLUTION,
                batch.asker.task,
                encoded(resolution),
                str(batch.id),
            )

    def claim(self, outbound: str, owner: str, route: m.Route) -> Claim | None:
        with self.transaction():
            previous = self.get_outbound(outbound).state
            updated = self.db.execute(
                """UPDATE outbound SET state=?,claim_owner=?,claimed_at=?,updated_at=? WHERE id=? AND state IN (?,?) AND paused_by IS NULL
                AND NOT EXISTS(SELECT 1 FROM takeover WHERE takeover.batch=outbound.batch AND origin=? AND cleared_at IS NULL)""",
                (
                    m.DeliveryState.CLAIMED,
                    owner,
                    now().isoformat(),
                    now().isoformat(),
                    outbound,
                    m.DeliveryState.PENDING,
                    m.DeliveryState.DISPATCH_FAILED,
                    m.PromptOrigin.UNKNOWN,
                ),
            )
            if updated.rowcount != 1:
                return None
            attempt = self.db.execute(
                "SELECT coalesce(max(n),0)+1 FROM dispatch_attempt WHERE outbound=?",
                (outbound,),
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO dispatch_attempt VALUES(?,?,?,?,?,?)",
                (outbound, attempt, route, None, now().isoformat(), None),
            )
            return Claim(outbound, owner, attempt, route, previous)

    def stop_claim(
        self,
        claim: Claim,
        *,
        cause: m.SuppressCause | None = None,
        paused_by: str | None = None,
        abandon: bool = False,
    ) -> bool:
        """Release or suppress only the current claim, preserving its previous state."""
        with self.transaction():
            state = (
                m.DeliveryState.ABANDONED
                if abandon
                else m.DeliveryState.SUPPRESSED
                if cause
                else claim.previous_state
            )
            changed = self.db.execute(
                "UPDATE outbound SET state=?,suppress_cause=?,paused_by=?,claim_owner=NULL,claimed_at=NULL,updated_at=? WHERE id=? AND state=? AND claim_owner=?",
                (
                    state,
                    cause,
                    paused_by,
                    now().isoformat(),
                    claim.outbound,
                    m.DeliveryState.CLAIMED,
                    claim.owner,
                ),
            ).rowcount
            if changed:
                self.db.execute(
                    "UPDATE dispatch_attempt SET result=?,finished_at=? WHERE outbound=? AND n=?",
                    (state, now().isoformat(), claim.outbound, claim.attempt),
                )
            return bool(changed)

    def finish_claim(self, claim: Claim, state: m.DeliveryState) -> bool:
        if state not in {
            m.DeliveryState.DISPATCHED,
            m.DeliveryState.DISPATCH_FAILED,
            m.DeliveryState.DISPATCH_UNCERTAIN,
        }:
            raise ValueError("invalid dispatch outcome")
        with self.transaction():
            changed = self.db.execute(
                "UPDATE outbound SET state=?,updated_at=? WHERE id=? AND state=? AND claim_owner=?",
                (
                    state,
                    now().isoformat(),
                    claim.outbound,
                    m.DeliveryState.CLAIMED,
                    claim.owner,
                ),
            ).rowcount
            if changed:
                self.db.execute(
                    "UPDATE dispatch_attempt SET result=?,finished_at=? WHERE outbound=? AND n=?",
                    (state, now().isoformat(), claim.outbound, claim.attempt),
                )
                row = self.db.execute(
                    "SELECT batch FROM outbound WHERE id=?", (claim.outbound,)
                ).fetchone()
                batch = self.get_batch(row[0])
                self.audit(
                    AuditKind.OUTBOUND,
                    batch.asker.task,
                    encoded({"outbound_id": claim.outbound, "state": state}),
                    str(batch.id),
                )
                if state is m.DeliveryState.DISPATCH_UNCERTAIN:
                    self.notice(
                        m.NoticeKind.DISPATCH_UNCERTAIN,
                        batch.asker.task,
                        f"Uncertain dispatch {claim.outbound}",
                        str(batch.id),
                    )
            else:
                logging.warning("Dropped late dispatch result for %s", claim.outbound)
            return bool(changed)

    def recover_claims(self, at: datetime | None = None) -> int:
        with self.transaction():
            cutoff = ((at or now()) - m.CLAIM_STALE_AFTER).isoformat()
            rows = self.db.execute(
                "SELECT id,batch,claim_owner,claimed_at FROM outbound WHERE state=?",
                (m.DeliveryState.CLAIMED,),
            ).fetchall()
            changed = 0
            for row in rows:
                owner = row["claim_owner"] or ""
                pid = owner.partition(":")[0]
                gone = False
                # PID reuse can defer recovery until CLAIM_STALE_AFTER; the
                # bounded stale cutoff still makes the claim uncertain.
                if pid.isdecimal():
                    try:
                        os.kill(int(pid), 0)
                    except ProcessLookupError:
                        gone = True
                    except PermissionError:
                        pass
                if not gone and row["claimed_at"] >= cutoff:
                    continue
                changed += self.db.execute(
                    "UPDATE outbound SET state=?,updated_at=? WHERE id=? AND state=? AND claim_owner=?",
                    (
                        m.DeliveryState.DISPATCH_UNCERTAIN,
                        now().isoformat(),
                        row["id"],
                        m.DeliveryState.CLAIMED,
                        row["claim_owner"],
                    ),
                ).rowcount
                self.db.execute(
                    "UPDATE dispatch_attempt SET result=?,finished_at=? WHERE outbound=? AND result IS NULL",
                    (m.DeliveryState.DISPATCH_UNCERTAIN, now().isoformat(), row["id"]),
                )
                batch = self.get_batch(row["batch"])
                self.notice(
                    m.NoticeKind.DISPATCH_UNCERTAIN,
                    batch.asker.task,
                    f"Orphaned dispatch {row['id']}",
                    str(batch.id),
                )
            return changed

    def put_box_note(self, note: m.BoxNote) -> None:
        with self.transaction():
            self.db.execute(
                """INSERT INTO box_note VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_id)
                DO UPDATE SET attempt=excluded.attempt,plan_id=excluded.plan_id,state=?,at=excluded.at
                WHERE excluded.attempt>box_note.attempt""",
                (
                    note.delivery_id,
                    note.attempt,
                    note.plan_id,
                    str(note.batch),
                    note.batch_rev,
                    note.resolution_rev,
                    note.purpose,
                    note.target_actor,
                    note.child_actor,
                    note.text,
                    note.body_digest,
                    note.ack_token_digest,
                    note.floor,
                    note.state,
                    note.acked_rev,
                    note.at.isoformat(),
                    m.BoxNoteState.PREPARED,
                ),
            )

            batch = self.get_batch(str(note.batch))
            self.audit(
                AuditKind.BOX_NOTE,
                batch.asker.task,
                encoded({"delivery_id": note.delivery_id, "attempt": note.attempt}),
                str(batch.id),
            )

    def finish_box_note(
        self, delivery_id: str, attempt: int, plan_id: str, state: m.BoxNoteState
    ) -> bool:
        with self.transaction():
            return bool(
                self.db.execute(
                    "UPDATE box_note SET state=? WHERE delivery_id=? AND attempt=? AND plan_id=? AND state<>?",
                    (state, delivery_id, attempt, plan_id, m.BoxNoteState.ECHOED),
                ).rowcount
            )

    def ack(self, outbound: str, token: str) -> bool:
        with self.transaction():
            row = self.db.execute(
                "SELECT ack_token,resolution_rev FROM outbound WHERE id=?", (outbound,)
            ).fetchone()
            if row and hmac.compare_digest(row[0].encode(), token.encode()):
                return bool(
                    self.db.execute(
                        """UPDATE outbound SET state=?,applied_rev=resolution_rev,updated_at=?
                    WHERE id=? AND state IN (?,?,?,?)""",
                        (
                            m.DeliveryState.APPLIED,
                            now().isoformat(),
                            outbound,
                            m.DeliveryState.DISPATCHED,
                            m.DeliveryState.OBSERVED,
                            m.DeliveryState.DISPATCH_UNCERTAIN,
                            m.DeliveryState.APPLIED,
                        ),
                    ).rowcount
                )
            row = self.db.execute(
                "SELECT ack_token_digest,resolution_rev FROM box_note WHERE delivery_id=?",
                (outbound,),
            ).fetchone()
            if row and hmac.compare_digest(
                row[0], hashlib.sha256(token.encode()).hexdigest()
            ):
                return bool(
                    self.db.execute(
                        "UPDATE box_note SET acked_rev=resolution_rev WHERE delivery_id=? AND state IN (?,?,?)",
                        (
                            outbound,
                            m.BoxNoteState.SENT,
                            m.BoxNoteState.ECHOED,
                            m.BoxNoteState.UNKNOWN,
                        ),
                    ).rowcount
                )
            return False

    def notice(
        self,
        kind: m.NoticeKind,
        task: str,
        detail: str,
        batch: str | None = None,
        *,
        notice_id: m.NoticeId | None = None,
        metadata: m.DigestMetadata | None = None,
    ) -> str:
        from question_redact import redact

        notice_key = str(notice_id or uuid4())
        with self.transaction():
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO notice(id,kind,task,batch,detail,created_at,spooled,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    notice_key,
                    kind,
                    task,
                    batch,
                    redact(detail).text[:500],
                    now().isoformat(),
                    False,
                    encoded(metadata) if metadata is not None else None,
                ),
            )
            if inserted.rowcount:
                self.audit(AuditKind.NOTICE, task, f"{kind}: {detail}", batch)
        return notice_key

    def clear_takeover(self, takeover: str, by: str) -> None:
        with self.transaction():
            row = self.db.execute(
                "SELECT origin FROM takeover WHERE id=?", (takeover,)
            ).fetchone()
            if (
                row is None
                or m.PromptOrigin(row["origin"]) is not m.PromptOrigin.UNKNOWN
            ):
                raise ValueError("only a known UNKNOWN takeover can be cleared")
            self.db.execute(
                "UPDATE takeover SET cleared_at=?,cleared_by=? WHERE id=? AND origin=? AND cleared_at IS NULL",
                (now().isoformat(), by, takeover, m.PromptOrigin.UNKNOWN),
            )
            if self.db.execute(
                "SELECT 1 FROM takeover WHERE id=? AND cleared_at IS NOT NULL",
                (takeover,),
            ).fetchone():
                self.db.execute(
                    """UPDATE outbound SET paused_by=(SELECT id FROM takeover
                    WHERE takeover.batch=outbound.batch AND origin=? AND cleared_at IS NULL
                    ORDER BY at,id LIMIT 1) WHERE paused_by=?""",
                    (m.PromptOrigin.UNKNOWN, takeover),
                )
                row = self.db.execute(
                    "SELECT batch FROM takeover WHERE id=?", (takeover,)
                ).fetchone()
                batch = self.get_batch(row[0])
                self.audit(
                    AuditKind.CLEARANCE,
                    batch.asker.task,
                    encoded({"takeover_id": takeover, "by": by}),
                    str(batch.id),
                )

    def live_context(self, batch_id: str, brief: Path | None = None) -> LiveContext:
        batch = self.get_batch(batch_id)
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=batch.asker.cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            # Unknown live context must invalidate, not bless, a pending high-floor send.
            head = ""
        if brief is None:
            stored = self.db.execute(
                "SELECT brief_path FROM batch WHERE id=?", (batch_id,)
            ).fetchone()[0]
            brief = Path(stored) if stored else None
        try:
            brief_digest = m.file_digest(brief.read_bytes()) if brief else None
        except OSError:
            brief_digest = m.TextDigest("")
        changed = brief_digest != batch.asker.brief_digest or (
            head != batch.asker.head_sha and batch.classification.floor >= 3
        )
        if head != batch.asker.head_sha and batch.classification.floor <= 2:
            self.audit(
                AuditKind.CONTEXT,
                batch.asker.task,
                "HEAD change allowed for a local answer",
                batch_id,
            )
        return LiveContext(m.HeadSha(head), brief_digest, changed, brief)

    def bump_for_context(self, batch_id: str, live: LiveContext) -> m.QuestionBatch:
        with self.transaction():
            batch = self.get_batch(batch_id)
            if not live.changed or batch.state in TERMINAL:
                return batch
            rev = m.BatchRev(batch.rev + 1)
            from question_guard import BriefInvalid, derive_floor, parse_brief

            try:
                brief = parse_brief(
                    live.brief_path.read_text() if live.brief_path else None,
                    cwd=batch.asker.cwd,
                )
                classification = derive_floor(
                    batch.questions,
                    brief=brief,
                    cwd=batch.asker.cwd,
                    classification=batch.classification,
                    previous_floor=batch.classification.floor,
                )
            except (OSError, BriefInvalid):
                classification = derive_floor(
                    batch.questions,
                    classification=batch.classification,
                    previous_floor=batch.classification.floor,
                    policy_available=False,
                )
                self.notice(
                    m.NoticeKind.BRIEF_INVALID,
                    batch.asker.task,
                    "Brief unavailable or invalid",
                    batch_id,
                )
            asker = replace(
                batch.asker, head_sha=live.head_sha, brief_digest=live.brief_digest
            )
            state = (
                m.BatchState.OPEN
                if batch.state
                in {
                    m.BatchState.OPEN,
                    m.BatchState.ESCALATING,
                }
                else batch.state
            )
            result = replace(
                batch,
                rev=rev,
                asker=asker,
                state=state,
                classification=classification,
                context=m.context_digest(
                    batch.id, rev, batch.questions, live.head_sha, live.brief_digest
                ),
            )
            self._revision(result, m.SuppressCause.CONTEXT_CHANGED)
            self.db.execute(
                "UPDATE batch SET brief_path=? WHERE id=?",
                (str(live.brief_path) if live.brief_path else None, batch_id),
            )
            self.audit(
                AuditKind.CONTEXT,
                batch.asker.task,
                f"revision {rev}: context changed",
                batch_id,
            )
            return result

    def ingest_events(self, directory: Path) -> int:
        """Ingest atomic L0 event files, leaving malformed input available for inspection."""
        count = 0
        for path in sorted(directory.glob("*.json")):
            try:
                value = L0Event.parse(json.loads(path.read_bytes()))
                notice_id = m.NoticeId(UUID(path.stem))
                self.notice(
                    m.NoticeKind.L0_VIOLATION,
                    value.task_id,
                    value.text,
                    value.batch_id,
                    notice_id=notice_id,
                    metadata=value.metadata,
                )
                path.unlink()
                count += 1
            except (OSError, ValueError):
                continue
        return count

    def record_lifecycle(
        self, signal: m.Lifecycle, batch_id: str | None = None, *, ours: bool = False
    ) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO lifecycle VALUES(?,?,?,?,?,?,?,?)",
                (
                    signal.actor,
                    signal.kind,
                    signal.call,
                    batch_id,
                    signal.origin,
                    signal.prompt_digest,
                    ours,
                    signal.at.isoformat(),
                ),
            )
            if signal.kind == m.LifecycleKind.NATIVE_CALL_FAILED and signal.call:
                self.mark_failed(signal.call)

    def session_alive(self, actor: str) -> bool:
        return not bool(
            self.db.execute(
                "SELECT 1 FROM lifecycle WHERE actor=? AND kind=?",
                (actor, m.LifecycleKind.SESSION_ENDED),
            ).fetchone()
        )

    def asker_session_alive(self, asker: m.AskerRef) -> bool:
        # SubagentStop finishes a child, but late answers still belong to its root.
        actor = (
            asker.root_actor
            if asker.role is m.ActorRole.NATIVE_SUBAGENT
            else asker.actor
        )
        return self.session_alive(actor)

    def late_child_notice(self, batch: m.QuestionBatch) -> None:
        if not self.db.execute(
            "SELECT 1 FROM notice WHERE batch=? AND kind=?",
            (str(batch.id), m.NoticeKind.LATE_DELIVERY),
        ).fetchone():
            self.notice(
                m.NoticeKind.LATE_DELIVERY,
                batch.asker.task,
                "Subagent has finished; its root must review any late answer",
                str(batch.id),
            )

    def answered_in_pane(self, batch_id: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM lifecycle WHERE batch=? AND kind=? AND ours=0",
                (batch_id, m.LifecycleKind.NATIVE_ANSWER),
            ).fetchone()
        )

    def record_takeover(
        self,
        takeover: m.Takeover,
        *,
        cause: m.CancelCause = m.CancelCause.HUMAN_TOOK_OVER,
    ) -> None:
        if takeover.origin not in {m.PromptOrigin.UNKNOWN, m.PromptOrigin.HUMAN}:
            raise ValueError("takeover requires positive human evidence or UNKNOWN")
        with self.transaction():
            self.db.execute(
                "INSERT INTO takeover VALUES(?,?,?,?,?,?,?)",
                (
                    str(takeover.id),
                    str(takeover.batch),
                    takeover.origin,
                    takeover.prompt_digest,
                    takeover.at.isoformat(),
                    None,
                    None,
                ),
            )
            if takeover.origin == m.PromptOrigin.UNKNOWN:
                self.db.execute(
                    "UPDATE outbound SET paused_by=? WHERE batch=? AND state IN (?,?,?) AND paused_by IS NULL",
                    (str(takeover.id), str(takeover.batch), *UNDISPATCHED),
                )
                batch = self.get_batch(str(takeover.batch))
                self.notice(
                    m.NoticeKind.TAKEOVER_SUSPECTED,
                    batch.asker.task,
                    f"Unverified input; takeover {takeover.id}",
                    str(batch.id),
                )
            else:
                self.cancel_local(
                    str(takeover.batch),
                    cause,
                    takeover.prompt_digest,
                )
            batch = self.get_batch(str(takeover.batch))
            self.audit(
                AuditKind.TAKEOVER, batch.asker.task, encoded(takeover), str(batch.id)
            )

    def cancel_local(
        self,
        batch_id: str,
        cause: m.CancelCause = m.CancelCause.HUMAN_TOOK_OVER,
        prompt_digest: str | None = None,
        *,
        from_box: bool = False,
    ) -> None:
        with self.transaction():
            batch = self.get_batch(batch_id)
            if batch.state in TERMINAL:
                return
            if batch.state in {
                m.BatchState.OPEN,
                m.BatchState.ESCALATING,
                m.BatchState.BOX_OWNED,
            } or (from_box and batch.state == m.BatchState.BLOCKED):
                self.transition(batch_id, m.BatchState.CANCELLED)
                self.db.execute(
                    "UPDATE batch SET cancel_cause=? WHERE id=?", (cause, batch_id)
                )
                self.db.execute(
                    "INSERT INTO cancellation VALUES(?,?,?,?,?)",
                    (batch_id, batch.rev, cause, prompt_digest, now().isoformat()),
                )
            self.db.execute(
                "UPDATE outbound SET state=?,suppress_cause=? WHERE batch=? AND state IN (?,?,?,?)",
                (
                    m.DeliveryState.SUPPRESSED,
                    m.SuppressCause.HUMAN_TAKEOVER,
                    batch_id,
                    *UNDISPATCHED,
                    m.DeliveryState.DISPATCH_UNCERTAIN,
                ),
            )
            self._settle_dispatch_attempts(batch_id, m.DeliveryState.SUPPRESSED)
            self.notice(m.NoticeKind.CANCELLED, batch.asker.task, cause.value, batch_id)
            self.audit(AuditKind.CANCELLATION, batch.asker.task, cause.value, batch_id)

    def reconcile(self, outbound: str, verdict: m.ReconcileVerdict) -> None:
        with self.transaction():
            row = self.db.execute(
                "SELECT batch,state,purpose FROM outbound WHERE id=?", (outbound,)
            ).fetchone()
            if row is None or row["state"] != m.DeliveryState.DISPATCH_UNCERTAIN:
                raise ValueError("only uncertain outbounds can be reconciled")
            batch = self.get_batch(row["batch"])
            state = (
                m.DeliveryState.OBSERVED
                if verdict == m.ReconcileVerdict.SEEN_IN_PANE
                else m.DeliveryState.PENDING
            )
            cause = None
            if (
                verdict == m.ReconcileVerdict.NOT_DELIVERED
                and row["purpose"] == m.OutboundPurpose.ANSWER
                and batch.state
                in {
                    m.BatchState.BOX_OWNED,
                    m.BatchState.BOX_ANSWERED,
                }
            ):
                state, cause = m.DeliveryState.SUPPRESSED, m.SuppressCause.BOX_OWNED
            self.db.execute(
                "UPDATE outbound SET state=?,suppress_cause=?,updated_at=? WHERE id=?",
                (state, cause, now().isoformat(), outbound),
            )

    def retain(self, at: datetime | None = None) -> int:
        at = at or now()
        removed = 0
        with self.transaction():
            for batch in self.batches():
                row = self.db.execute(
                    "SELECT updated_at FROM batch WHERE id=?", (str(batch.id),)
                ).fetchone()
                eligible = batch.state in TERMINAL or (
                    batch.state
                    in {m.BatchState.RESOLVED_LOCAL, m.BatchState.BOX_ANSWERED}
                    and not self.session_alive(batch.asker.actor)
                )
                if not eligible or datetime.fromisoformat(row[0]) > at - timedelta(
                    days=30
                ):
                    continue
                for table in ("dispatch_attempt",):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE outbound IN (SELECT id FROM outbound WHERE batch=?)",
                        (str(batch.id),),
                    )
                for table in (
                    "question",
                    "native_call",
                    "attempt",
                    "resolution",
                    "registration",
                    "classifier_pending",
                    "ladder_run",
                    "progress",
                    "lifecycle",
                    "cancellation",
                    "outbound",
                    "inline_waiter",
                    "takeover",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE batch=?", (str(batch.id),)
                    )
                self.db.execute("DELETE FROM batch WHERE id=?", (str(batch.id),))
                removed += 1
            cutoff = (at - timedelta(days=365)).isoformat()
            # A durable result makes contenders on either inode replay rather than route.
            # Leave unresolved, expired, and currently held locks alone.
            for row in self.db.execute(
                "SELECT plan_id FROM plan_seen WHERE result_b64 IS NOT NULL AND at>=?",
                (cutoff,),
            ):
                name = f"qe-plan-{row[0]}"
                if Path(name).name != name:
                    continue
                path = self.path.parent / name
                try:
                    fd = os.open(path, os.O_RDWR)
                except FileNotFoundError:
                    continue
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    path.unlink(missing_ok=True)
                finally:
                    os.close(fd)
            self.db.execute("DELETE FROM plan_seen WHERE at<?", (cutoff,))
            self.db.execute("DELETE FROM box_note WHERE at<?", (cutoff,))
        return removed

    def reserve_budget(
        self,
        task: str,
        *,
        model: m.Model,
        tokens: int,
        usd: str | Decimal,
        max_attempts: int = 20,
        max_tokens: int = 600_000,
        task_usd: Decimal = Decimal("1.50"),
        day_usd: Decimal = Decimal("10"),
        at: datetime | None = None,
    ) -> ReservationId | None:
        try:
            amount = Decimal(usd)
        except InvalidOperation as exc:
            raise ValueError("invalid reservation") from exc
        model = m.Model(model)
        if tokens < 0 or not amount.is_finite() or amount < 0:
            raise ValueError("invalid reservation")
        day = (at or now()).astimezone(ZoneInfo(m.LOCAL_TZ)).date().isoformat()
        with self.transaction():
            if (
                self.db.execute(
                    "SELECT count(*) FROM budget_reservation WHERE model=? AND day=? AND overrun=1",
                    (model, day),
                ).fetchone()[0]
                >= 2
            ):
                return None
            keys = (
                (m.BudgetScope.TASK, task, task_usd),
                (m.BudgetScope.DAY, day, day_usd),
            )
            for scope, key, cap in keys:
                self.db.execute(
                    "INSERT OR IGNORE INTO budget VALUES(?,?,0,0,0,'0','0')",
                    (scope, key),
                )
                row = BudgetRecord.parse(
                    self.db.execute(
                        "SELECT * FROM budget WHERE scope=? AND key=?", (scope, key)
                    ).fetchone()
                )
                if row.usd_reserved + row.usd_spent + amount > cap:
                    return None
                if scope == m.BudgetScope.TASK and (
                    row.attempts >= max_attempts
                    or row.tokens_reserved + row.tokens_spent + tokens > max_tokens
                ):
                    return None
            for scope, key, _ in keys:
                row = self.db.execute(
                    "SELECT usd_reserved FROM budget WHERE scope=? AND key=?",
                    (scope, key),
                ).fetchone()
                self.db.execute(
                    "UPDATE budget SET attempts=attempts+1,tokens_reserved=tokens_reserved+?,usd_reserved=? WHERE scope=? AND key=?",
                    (tokens, str(Decimal(row[0]) + amount), scope, key),
                )
            reservation = ReservationId(str(uuid4()))
            self.db.execute(
                "INSERT INTO budget_reservation (id,task,model,day,tokens,usd,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    reservation,
                    task,
                    model,
                    day,
                    tokens,
                    str(amount),
                    (at or now())
                    .astimezone(timezone.utc)
                    .isoformat(timespec="microseconds"),
                ),
            )
            self.audit(
                AuditKind.BUDGET, task, f"reserved {tokens} tokens, USD {amount}"
            )
            return reservation

    def settle_budget(
        self,
        reservation: ReservationId,
        *,
        usage: m.Usage | None,
    ) -> None:
        with self.transaction():
            reserved = self.db.execute(
                "SELECT * FROM budget_reservation WHERE id=?", (reservation,)
            ).fetchone()
            if reserved is None:
                raise ValueError("no matching reservation")
            if reserved["settled"]:
                return
            task, day = reserved["task"], reserved["day"]
            tokens_reserved = reserved["tokens"]
            try:
                usd_reserved = Decimal(reserved["usd"])
            except InvalidOperation as exc:
                raise ValueError("invalid reservation decimal") from exc
            tokens = (
                usage.input_tokens + usage.output_tokens if usage else tokens_reserved
            )
            usd = usage.usd if usage else usd_reserved
            for scope, key in ((m.BudgetScope.TASK, task), (m.BudgetScope.DAY, day)):
                raw = self.db.execute(
                    "SELECT * FROM budget WHERE scope=? AND key=?", (scope, key)
                ).fetchone()
                if raw is None:
                    raise ValueError("no matching reservation")
                row = BudgetRecord.parse(raw)
                if (
                    row.tokens_reserved < tokens_reserved
                    or row.usd_reserved < usd_reserved
                ):
                    raise ValueError("no matching reservation")
                self.db.execute(
                    "UPDATE budget SET tokens_reserved=tokens_reserved-?,tokens_spent=tokens_spent+?,usd_reserved=?,usd_spent=? WHERE scope=? AND key=?",
                    (
                        tokens_reserved,
                        tokens,
                        str(row.usd_reserved - usd_reserved),
                        str(row.usd_spent + usd),
                        scope,
                        key,
                    ),
                )
            self.db.execute(
                "UPDATE budget_reservation SET settled=1,overrun=? WHERE id=?",
                (int(tokens > tokens_reserved or usd > usd_reserved), reservation),
            )
            self.audit(AuditKind.BUDGET, task, f"settled {tokens} tokens, USD {usd}")

    def sweep_budget_reservations(self, *, at: datetime | None = None) -> int:
        """Settle reservations older than ten minutes as fully spent.

        Ten minutes exceeds the longest bounded answerer call (four minutes),
        leaving margin for scheduling and result settlement after process exit.
        """
        cutoff = (
            (at or now()).astimezone(timezone.utc) - timedelta(minutes=10)
        ).isoformat(timespec="microseconds")
        with self.transaction():
            expired = self.db.execute(
                "SELECT id FROM budget_reservation WHERE settled=0 AND created_at<? ORDER BY created_at,id",
                (cutoff,),
            ).fetchall()
            for row in expired:
                self.settle_budget(ReservationId(row["id"]), usage=None)
        return len(expired)
