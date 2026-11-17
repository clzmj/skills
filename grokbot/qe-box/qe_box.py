#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Box authority for QE registrations, decisions and leased Mac plans.

Only local bot invocations decide. Mac reports can suppress delivery or raise a
floor, and are always shown as untrusted evidence. No command executes a plan.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import sys
import tomllib
from typing import (
    Callable,
    Final,
    Generator,
    Literal,
    TypeVar,
    TypedDict,
    cast,
    get_type_hints,
)
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills/rex/scripts"))
import question_model as m  # noqa: E402
import question_guard as guard  # noqa: E402
import question_redact as redaction  # noqa: E402


class BoxBatchState(StrEnum):
    REGISTERED = "registered"
    LOCAL = "local"
    AWAITING_L3 = "awaiting_l3"
    AWAITING_L4 = "awaiting_l4"
    DECIDED = "decided"
    DELIVERED = "delivered"
    CORRECTION_DUE = "correction_due"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    STALE = "stale"
    CLOSED = "closed"
    TOMBSTONED = "tombstoned"


class QueuedKind(StrEnum):
    ENTRY = "entry"
    PROGRESS = "progress"


class BoxBlockCause(StrEnum):
    SEND_FAILURES = "send_failures"
    UNKNOWN_CALLBACK_BOT = "unknown_callback_bot"
    MAC_NOT_DELIVERABLE = "mac_not_deliverable"


class DecisionKind(StrEnum):
    ANSWER = "answer"
    CORRECTION = "correction"


class CardPurpose(StrEnum):
    ASK = "ask"
    CORRECTION = "correction"


class CardGenState(StrEnum):
    INTENDED = "intended"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"
    CLOSED = "closed"


class PlanState(StrEnum):
    ISSUED = "issued"
    FINAL_RESULT = "final_result"
    FINAL_NONE = "final_none"
    FINAL_EXPIRED = "final_expired"


class BoxReport(StrEnum):
    FLOOR_DISAGREEMENT = "floor_disagreement"
    CARD_CAP_REACHED = "card_cap_reached"
    HANDOFF_UNACKED = "handoff_unacked"
    UNKNOWN_CALLBACK_BOT = "unknown_callback_bot"
    NON_MONOTONE_REGISTRATION = "non_monotone_registration"
    CLOSED_BATCH = "closed_batch"
    SEND_FAILED = "send_failed"
    CLOCK_SKEW = "clock_skew"
    SYNC_OVERSIZED = "sync_oversized"
    REGISTRATION_REFUSED = "registration_refused"
    RESULT_REFUSED = "result_refused"


class BoxRefusal(StrEnum):
    STALE_REVISION = "stale_revision"
    LEASED = "leased"
    TEXT_TOO_LONG = "text_too_long"
    CLOSED_BATCH = "closed_batch"
    WRONG_STATE = "wrong_state"
    FLOOR_TOO_HIGH = "floor_too_high"
    VALIDATION = "validation"
    CARD_NOT_ACTIVE = "card_not_active"
    CARD_CAP_REACHED = "card_cap_reached"
    SUPERSEDES_MISMATCH = "supersedes_mismatch"
    NON_MONOTONE_REGISTRATION = "non_monotone_registration"
    UNKNOWN_ENTRY = "unknown_entry"
    UNKNOWN_PLAN = "unknown_plan"
    LEASE_MISMATCH = "lease_mismatch"
    BAD_RESULT = "bad_result"
    CLOCK_SKEW_HOLD = "clock_skew_hold"


class MetaKey(StrEnum):
    CLOCK_SKEW_HOLD = "clock_skew_hold"
    SYNC_CURSOR = "sync_cursor"


class DeferredStatus(StrEnum):
    CANCEL = "cancel"
    STALE = "stale"


class Command(StrEnum):
    REGISTER = "register"
    SHOW = "show"
    STATUS = "status"
    DECIDE = "decide"
    DELIVER = "deliver"
    HANDOFF = "handoff"
    CARD = "card"
    CANCEL = "cancel"
    CLEAR_TAKEOVER = "clear-takeover"
    RECONCILE = "reconcile"
    RETRY = "retry"
    RESULT = "result"
    TICK = "tick"


class CardAction(StrEnum):
    INTENT = "intent"
    POSTED = "posted"
    CLOSE = "close"


REFUSAL_EXIT: Final = {BoxRefusal.LEASED: 4, BoxRefusal.CLOCK_SKEW_HOLD: 4}
LEASE_TTL: Final = timedelta(minutes=10)
MAX_SEND_FAILURES: Final = 3
CARD_REPOST_MIN: Final = timedelta(minutes=15)
CARD_INTENT_WAIT: Final = timedelta(minutes=10)
IDLE_CLOSE: Final = timedelta(days=30)
FULL_RETENTION: Final = timedelta(days=90)
TOMBSTONE_RETENTION: Final = timedelta(days=365)
CORRECTION_RENOTIFY: Final = timedelta(days=7)
HANDOFF_WAIT: Final = timedelta(hours=1)
REMINDERS: Final = (timedelta(hours=1), timedelta(hours=4), timedelta(hours=24))
TERMINAL: Final = frozenset(
    {BoxBatchState.CANCELLED, BoxBatchState.CLOSED, BoxBatchState.TOMBSTONED}
)
INACTIVE: Final = TERMINAL | {BoxBatchState.STALE}
CLOSABLE: Final = frozenset(
    {
        BoxBatchState.LOCAL,
        BoxBatchState.DELIVERED,
        BoxBatchState.CANCELLED,
        BoxBatchState.STALE,
        BoxBatchState.BLOCKED,
    }
)
CORRECTABLE: Final = frozenset(
    {
        BoxBatchState.LOCAL,
        BoxBatchState.DECIDED,
        BoxBatchState.DELIVERED,
        BoxBatchState.CORRECTION_DUE,
    }
)
RETRY_SAFE: Final = frozenset(m.PlanAction) - {m.PlanAction.SEND}
UNDISPATCHED: Final = frozenset(
    {m.DeliveryState.PENDING, m.DeliveryState.DISPATCH_FAILED, m.DeliveryState.CLAIMED}
)
DINESH: Final = m.DeciderId("dinesh")
DEFAULT_BROKER: Final = "$HOME/.local/share/agent-tooling/rex/question_broker.py"
DEFAULT_CONFIG: Final = Path.home() / ".config/qe-box/config.toml"
DEFAULT_STATE: Final = Path.home() / ".local/state/qe-box/qe-box.sqlite3"
RESULT_LINE: Final = re.compile(r"QE-RESULT ([A-Za-z0-9_-]+)")
UNTRUSTED_FIELDS: Final = (
    "record.identity_json",
    "deliveries.text",
    "plans.body_json",
    "revisions.content_json",
    "revisions.hits_json",
    "progress.json",
    "deliveries.late_result_json",
    "plans.result_json",
    "plans.late_result_json",
    "record.task",
    "record.callback_bot",
    "takeovers",
    "reports.detail",
)


class Refused(Exception):
    def __init__(self, reason: BoxRefusal, detail: str = ""):
        self.reason, self.detail = reason, detail
        super().__init__(reason.value)

    @property
    def exit_code(self) -> int:
        return REFUSAL_EXIT.get(self.reason, 3)


class DecisionAnswer(TypedDict):
    question_id: str
    choice: list[int]
    free_text: str | None
    rationale: str


class DecisionClearance(TypedDict):
    rule_id: str
    trigger: m.GuardTrigger
    reason: str


class DecisionInput(TypedDict):
    schema: Literal["qe.decision/1"]
    answers: list[DecisionAnswer]
    clearances: list[DecisionClearance]


@dataclass(frozen=True, slots=True)
class Bot:
    callback: str
    agent_id: str
    display: str

    def __post_init__(self) -> None:
        if (
            not m.CALLBACK_BOT_PATTERN.fullmatch(self.callback)
            or not self.agent_id
            or not self.display
        ):
            raise ValueError("invalid bot registry entry")


@dataclass(frozen=True, slots=True)
class Config:
    mac_machine_id: str
    broker_path: str = DEFAULT_BROKER

    def __post_init__(self) -> None:
        if not self.mac_machine_id or self.broker_path != DEFAULT_BROKER:
            raise ValueError("machine identity and prescribed Mac broker path required")


class BotRegistry(TypedDict):
    schema: Literal["qe.bots/1"]
    bots: list[Bot]


def load_config(path: Path) -> tuple[Config, tuple[Bot, ...]]:
    for private in (path.parent, path, path.with_name("bots.toml")):
        if private.is_symlink() or private.stat().st_mode & 0o077:
            raise ValueError("config directory/files must be private and not symlinks")
    config = m.parse_record(Config, tomllib.loads(path.read_text()))
    registry = m.parse_record(
        BotRegistry, tomllib.loads(path.with_name("bots.toml").read_text())
    )
    bots = tuple(registry["bots"])
    if len({bot.callback for bot in bots}) != len(bots):
        raise ValueError("duplicate callback bot")
    return config, bots


@dataclass(frozen=True, slots=True)
class Identity:
    task: str
    actor: str
    agent: m.AgentKind
    role: m.ActorRole
    session: str
    root_actor: str
    root_session: str
    rex_session: str
    rex_block: str
    callback_bot: str


@dataclass(frozen=True, slots=True)
class FloorRecord:
    batch: str
    task: str
    callback_bot: str
    identity_json: str
    latest_rev: int
    floor: m.EscalationLevel
    owned: bool
    state: BoxBatchState
    blocked_cause: BoxBlockCause | None
    correction_floor: m.EscalationLevel | None
    correction_notified_at: datetime | None
    progress_seq: int
    send_failures: int
    last_activity_at: datetime
    closed_at: datetime | None
    tombstoned_at: datetime | None
    local_resolution: int | None
    local_level: m.EscalationLevel | None
    kept_resolution: int | None
    escalated: bool


@dataclass(frozen=True, slots=True)
class Revision:
    batch: str
    rev: int
    content_json: str
    content_digest: str
    box_floor: m.EscalationLevel
    hits_json: str
    guard_version: str
    registered_at: datetime


@dataclass(frozen=True, slots=True)
class Progress:
    batch: str
    rev: int
    seq: int
    json: str
    applied_at: datetime


@dataclass(frozen=True, slots=True)
class Lease:
    batch: str
    lease_id: str
    delivery_id: str
    plan_id: str
    batch_rev: int
    floor: m.EscalationLevel
    issued_at: datetime
    dead_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    batch: str
    resolution_rev: int
    batch_rev: int
    level: m.EscalationLevel
    kind: DecisionKind
    supersedes: int | None
    answers_json: str
    clearances_json: str
    decided_by: str
    card_gen: int | None
    card_ref: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class Delivery:
    id: str
    batch: str
    resolution_rev: int
    purpose: m.DeliveryPurpose
    target_actor: str
    child_actor: str | None
    text: str
    text_digest: str
    body_digest: str
    ack_token: str
    route_set: str
    state: m.DeliveryState
    suppress_cause: m.SuppressCause | None
    paused_by: str | None
    attempt: int
    resend_allowed: bool
    authorized_rev: int
    authorized_floor: m.EscalationLevel
    failed_sends: int
    late_result_json: str | None
    delivered_at: datetime | None
    delivered_via: m.DeliveryEvidence | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: str
    action: m.PlanAction
    lease_id: str | None
    delivery_id: str | None
    attempt: int | None
    body_json: str
    plan_digest: str
    state: PlanState
    result_json: str | None
    issued_by: str
    issued_at: datetime
    final_at: datetime | None
    late_result_json: str | None


@dataclass(frozen=True, slots=True)
class Card:
    task: str
    callback_bot: str
    gen: int
    purpose: CardPurpose
    pairs_json: str
    state: CardGenState
    ref: str | None
    reconciles: int | None
    close_requested: bool
    at: datetime
    reminded: int


@dataclass(frozen=True, slots=True)
class Handoff:
    id: str
    task: str
    callback_bot: str
    purpose: CardPurpose
    batch_revs_json: str
    sent_count: int
    last_sent_at: datetime


@dataclass(frozen=True, slots=True)
class Report:
    id: int
    kind: BoxReport
    batch: str | None
    detail: str
    at: datetime
    notified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Takeover:
    takeover_id: str
    batch: str
    origin: m.PromptOrigin
    cleared: bool


@dataclass(frozen=True, slots=True)
class PlanTicket:
    plan: m.Plan
    command: str
    mac_machine_id: str


@dataclass(frozen=True, slots=True)
class Output:
    plans: tuple[PlanTicket, ...] = ()
    queued: bool = False
    handoffs: tuple[m.L4Handoff, ...] = ()
    notices: tuple[str, ...] = ()


class BoxShow(TypedDict):
    schema: Literal["qe.boxshow/1"]
    record: FloorRecord
    lease: Lease | None
    revisions: list[Revision]
    progress: list[Progress]
    decisions: list[Decision]
    cards: list[Card]
    deliveries: list[Delivery]
    plans: list[PlanRow]
    takeovers: list[Takeover]
    reports: list[Report]
    untrusted: list[str]


T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def encoded(value: object) -> str:
    return base64.urlsafe_b64encode(m.canonical(value)).rstrip(b"=").decode()


def json_text(value: object) -> str:
    return m.canonical(value).decode()


def identity(entry: m.RegisterEntry) -> Identity:
    return Identity(
        entry["task_id"],
        entry["actor"],
        entry["agent"],
        entry["role"],
        entry["session"],
        entry["root_actor"],
        entry["root_session"],
        entry["rex_session"],
        entry["rex_block"],
        entry["callback_bot"],
    )


def question_core(question: m.RegisterQuestion) -> object:
    return (
        question["id"],
        question["text"],
        question["header"],
        question["options"],
        question["multi_select"],
        question["allows_free_text"],
        question["truncated"],
    )


def parse_progress(value: object) -> m.RegisterProgress:
    progress = m.parse_register_progress(value)
    UUID(progress["batch_id"])
    if progress["local_resolution"] is not None and (
        progress["local_resolution"] < 1
        or progress["local_level"]
        not in (m.EscalationLevel.L1_CHEAP, m.EscalationLevel.L2_NORMAL)
    ):
        raise ValueError("local resolutions must be positive and at L1/L2")
    return progress


def questions(
    entry: m.RegisterEntry, *, redact: bool = False
) -> tuple[m.Question, ...]:
    def text(raw: str) -> m.Untrusted:
        return m.Untrusted(
            redaction.redact(raw).text if redact else raw, m.UntrustedSource.AGENT
        )

    return tuple(
        m.Question(
            m.QuestionId(UUID(q["id"])),
            m.NativeCallId("registered"),
            text(q["text"]),
            text(q["header"]) if q["header"] is not None else None,
            tuple(
                m.Option(
                    m.OptionIndex(o["index"]),
                    text(o["label"]),
                    text(o["description"]) if o["description"] is not None else None,
                )
                for o in q["options"]
            ),
            q["multi_select"],
            q["allows_free_text"],
            m.NativeQuestionKey(codex_id=q["id"]),
            m.BatchRev(q["retired_rev"]) if q["retired_rev"] is not None else None,
        )
        for q in entry["questions"]
    )


class Box:
    def __init__(
        self,
        path: Path,
        *,
        bots: tuple[Bot, ...] = (),
        config: Config | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.clock, self.config = clock, config or Config("fixture-machine")
        self.bots = {bot.callback: bot for bot in bots}
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("state paths cannot be symlinks")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        with self.transaction():
            if "notified_at" not in {
                row[1] for row in self.db.execute("PRAGMA table_info(report)")
            }:
                self.db.execute("ALTER TABLE report ADD COLUMN notified_at TEXT")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _rows(self, model: type[T], sql: str, args: tuple[object, ...] = ()) -> list[T]:
        hints = get_type_hints(model)
        result = []
        for row in self.db.execute(sql, args):
            raw = dict(row)
            for name, hint in hints.items():
                if hint is bool:
                    raw[name] = bool(raw[name])
            result.append(m.parse_record(model, raw))
        return result

    def _one(self, model: type[T], sql: str, args: tuple[object, ...]) -> T | None:
        return next(iter(self._rows(model, sql, args)), None)

    def _record(self, batch: str) -> FloorRecord:
        value = self._one(
            FloorRecord, "SELECT * FROM floor_record WHERE batch=?", (batch,)
        )
        if value is None:
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        return value

    def _save_record(self, value: FloorRecord) -> None:
        names = [field.name for field in fields(value)]
        values = [self._sql(getattr(value, name)) for name in names]
        self.db.execute(
            f"INSERT OR REPLACE INTO floor_record ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            values,
        )
        self.db.execute(
            "INSERT INTO floor_ledger VALUES (?,?,?,?) ON CONFLICT(batch) DO UPDATE SET max_floor=max(max_floor,excluded.max_floor), max_rev=max(max_rev,excluded.max_rev), owned=max(owned,excluded.owned)",
            (value.batch, int(value.floor), value.latest_rev, int(value.owned)),
        )

    @staticmethod
    def _sql(value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (StrEnum, m.EscalationLevel)):
            return value.value
        return int(value) if isinstance(value, bool) else value

    def _report(self, kind: BoxReport, batch: str | None, detail: str = "") -> bool:
        # These notices have no reset rule: keep one occurrence per batch lifetime.
        if kind in (BoxReport.SYNC_OVERSIZED, BoxReport.CARD_CAP_REACHED):
            if self.db.execute(
                "SELECT 1 FROM report WHERE kind=? AND batch IS ?",
                (kind.value, batch),
            ).fetchone():
                return False
        self.db.execute(
            "INSERT INTO report(kind,batch,detail,at) VALUES (?,?,?,?)",
            (
                kind.value,
                batch,
                redaction.redact(detail).text[:2048],
                self.clock().isoformat(),
            ),
        )
        return True

    def _unleased(self, batch: str) -> None:
        self._expire(batch)
        if self.db.execute("SELECT 1 FROM lease WHERE batch=?", (batch,)).fetchone():
            raise Refused(BoxRefusal.LEASED)

    def _registration_refused(self, batch: str, error: Refused | ValueError) -> None:
        reason = error.reason if isinstance(error, Refused) else BoxRefusal.VALIDATION
        kind = {
            BoxRefusal.NON_MONOTONE_REGISTRATION: BoxReport.NON_MONOTONE_REGISTRATION,
            BoxRefusal.CLOSED_BATCH: BoxReport.CLOSED_BATCH,
        }.get(reason, BoxReport.REGISTRATION_REFUSED)
        self._report(
            kind,
            batch,
            f"{reason}: {error.detail if isinstance(error, Refused) else error}",
        )

    def _open(self, batch: str) -> FloorRecord:
        self._unleased(batch)
        record = self._record(batch)
        if record.state in (BoxBatchState.CLOSED, BoxBatchState.TOMBSTONED):
            raise Refused(BoxRefusal.CLOSED_BATCH)
        if record.state == BoxBatchState.STALE:
            raise Refused(BoxRefusal.WRONG_STATE)
        return record

    def _entry(self, batch: str, rev: int | None = None) -> m.RegisterEntry:
        rev = self._record(batch).latest_rev if rev is None else rev
        row = self.db.execute(
            "SELECT content_json FROM revision WHERE batch=? AND rev=?", (batch, rev)
        ).fetchone()
        if row is None:
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        return m.parse_register_entry(json.loads(row[0]))

    def _newest_relevant(
        self, record: FloorRecord
    ) -> tuple[int, m.EscalationLevel] | None:
        row = self.db.execute(
            "SELECT d.resolution_rev,c.level FROM delivery d JOIN decision c ON c.batch=d.batch AND c.resolution_rev=d.resolution_rev WHERE d.batch=? AND d.delivered_at IS NOT NULL ORDER BY d.resolution_rev DESC LIMIT 1",
            (record.batch,),
        ).fetchone()
        relevant = (int(row[0]), m.EscalationLevel(row[1])) if row else None
        if (
            record.local_resolution is not None
            and record.local_level is not None
            and (relevant is None or record.local_resolution > relevant[0])
        ):
            relevant = (record.local_resolution, record.local_level)
        return relevant

    def _suppress(
        self,
        batch: str,
        cause: m.SuppressCause,
        *,
        below: int | None = None,
        keep_uncertain: bool = True,
    ) -> None:
        for delivery in self._rows(
            Delivery, "SELECT * FROM delivery WHERE batch=?", (batch,)
        ):
            if delivery.delivered_at is not None or delivery.state in (
                m.DeliveryState.SUPPRESSED,
                m.DeliveryState.ABANDONED,
            ):
                continue
            if keep_uncertain and delivery.state == m.DeliveryState.DISPATCH_UNCERTAIN:
                continue
            decision = self._one(
                Decision,
                "SELECT * FROM decision WHERE batch=? AND resolution_rev=?",
                (batch, delivery.resolution_rev),
            )
            if below is not None and decision is not None and decision.level >= below:
                continue
            self.db.execute(
                "UPDATE delivery SET state=?,suppress_cause=?,paused_by=NULL,updated_at=? WHERE id=?",
                (
                    m.DeliveryState.SUPPRESSED.value,
                    cause.value,
                    self.clock().isoformat(),
                    delivery.id,
                ),
            )

    def _supersede_cards(self, record: FloorRecord, purpose: CardPurpose) -> None:
        for card in self._rows(
            Card,
            "SELECT * FROM card_generation WHERE task=? AND callback_bot=? AND purpose=? AND state=?",
            (
                record.task,
                record.callback_bot,
                purpose.value,
                CardGenState.ACTIVE.value,
            ),
        ):
            if any(pair[0] == record.batch for pair in json.loads(card.pairs_json)):
                self.db.execute(
                    "UPDATE card_generation SET state=? WHERE task=? AND callback_bot=? AND gen=?",
                    (
                        CardGenState.SUPERSEDED.value,
                        card.task,
                        card.callback_bot,
                        card.gen,
                    ),
                )

    def _after_registration(
        self, old: FloorRecord | None, record: FloorRecord, *, new_revision: bool
    ) -> FloorRecord:
        if old is None:
            state = BoxBatchState.AWAITING_L3 if record.owned else BoxBatchState.LOCAL
            return replace(record, state=state)
        raised = record.floor > old.floor
        if new_revision:
            self._suppress(
                record.batch, m.SuppressCause.NEWER_REVISION, keep_uncertain=raised
            )
            self._supersede_cards(record, CardPurpose.ASK)
            self._supersede_cards(record, CardPurpose.CORRECTION)
        elif raised:
            self._suppress(
                record.batch, m.SuppressCause.FLOOR_RAISED, below=record.floor
            )
        if old.state in INACTIVE or old.state == BoxBatchState.BLOCKED:
            return record
        if (
            old.state == BoxBatchState.LOCAL
            and record.floor <= m.EscalationLevel.L2_NORMAL
            and not record.escalated
        ):
            return replace(record, correction_floor=None)
        newest = self._newest_relevant(record)
        pending = self.db.execute(
            "SELECT 1 FROM delivery d JOIN decision c ON c.batch=d.batch AND c.resolution_rev=d.resolution_rev WHERE d.batch=? AND d.delivered_at IS NULL AND d.state IN (?,?) AND c.level>=? LIMIT 1",
            (
                record.batch,
                m.DeliveryState.PENDING.value,
                m.DeliveryState.DISPATCH_FAILED.value,
                int(record.floor),
            ),
        ).fetchone()
        if old.state == BoxBatchState.CORRECTION_DUE or (
            (new_revision or raised)
            and newest is not None
            and newest[1] < record.floor
            and (new_revision or not pending)
        ):
            self._supersede_cards(record, CardPurpose.ASK)
            if old.state != BoxBatchState.CORRECTION_DUE:
                self._report(BoxReport.FLOOR_DISAGREEMENT, record.batch)
            return replace(
                record,
                state=BoxBatchState.CORRECTION_DUE,
                correction_floor=max(
                    old.correction_floor or record.floor, record.floor
                ),
                kept_resolution=newest[0] if newest else old.kept_resolution,
                correction_notified_at=old.correction_notified_at or self.clock(),
                owned=True,
            )
        invalid_decision = (
            raised
            and old.state == BoxBatchState.DECIDED
            and self.db.execute(
                "SELECT 1 FROM decision WHERE batch=? AND resolution_rev=(SELECT max(resolution_rev) FROM decision WHERE batch=?) AND level<?",
                (record.batch, record.batch, int(record.floor)),
            ).fetchone()
        )
        if (
            new_revision
            or invalid_decision
            or (record.state == BoxBatchState.LOCAL and record.owned)
        ):
            return replace(
                record, state=BoxBatchState.AWAITING_L3, correction_floor=None
            )
        return record

    def register(self, value: object) -> Output:
        try:
            return self._register(value)
        except Refused as error:
            if error.reason == BoxRefusal.NON_MONOTONE_REGISTRATION:
                batch = value.get("batch_id") if isinstance(value, dict) else None
                with self.transaction():
                    self._report(
                        BoxReport.NON_MONOTONE_REGISTRATION,
                        batch if isinstance(batch, str) else None,
                        error.detail,
                    )
            raise

    def _register(self, value: object) -> Output:
        if not isinstance(value, dict):
            raise Refused(BoxRefusal.VALIDATION, "registration must be an object")
        try:
            entry = (
                m.parse_register_entry(value)
                if value.get("schema") == "qe.register/1"
                else None
            )
            progress = (
                parse_progress(value)
                if value.get("schema") == "qe.progress/1"
                else None
            )
            if entry is None and progress is None:
                raise ValueError("unknown registration schema")
        except (ValueError, TypeError, KeyError) as error:
            raise Refused(BoxRefusal.VALIDATION, str(error)) from error
        batch = (
            entry["batch_id"]
            if entry is not None
            else cast(m.RegisterProgress, progress)["batch_id"]
        )
        with self.transaction():
            self._expire(batch)
            if self.db.execute(
                "SELECT 1 FROM lease WHERE batch=?", (batch,)
            ).fetchone():
                item = (
                    entry if entry is not None else cast(m.RegisterProgress, progress)
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO queued_registration(batch,kind,rev,seq,content_digest,json,queued_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        batch,
                        (
                            QueuedKind.ENTRY
                            if entry is not None
                            else QueuedKind.PROGRESS
                        ).value,
                        item["batch_rev"],
                        0
                        if entry is not None
                        else cast(m.RegisterProgress, progress)["seq"],
                        m.digest(item),
                        json_text(item),
                        self.clock().isoformat(),
                    ),
                )
                return Output(queued=True)
            if entry is not None:
                self._apply_entry(entry)
            else:
                self._apply_progress(cast(m.RegisterProgress, progress))
            return Output(plans=(self._ack(batch, cast(int, value["batch_rev"])),))

    def _apply_entry(self, entry: m.RegisterEntry) -> None:
        batch, rev = entry["batch_id"], entry["batch_rev"]
        old = self._one(
            FloorRecord, "SELECT * FROM floor_record WHERE batch=?", (batch,)
        )
        existing = self.db.execute(
            "SELECT content_digest FROM revision WHERE batch=? AND rev=?", (batch, rev)
        ).fetchone()
        if existing:
            if existing[0] == entry["content_digest"]:
                return
            raise Refused(BoxRefusal.NON_MONOTONE_REGISTRATION)
        ledger = self.db.execute(
            "SELECT max_floor,max_rev,owned FROM floor_ledger WHERE batch=?", (batch,)
        ).fetchone()
        if ledger and rev <= ledger[1]:
            raise Refused(BoxRefusal.NON_MONOTONE_REGISTRATION)
        if old and old.state in (BoxBatchState.CLOSED, BoxBatchState.TOMBSTONED):
            raise Refused(BoxRefusal.CLOSED_BATCH)
        if old:
            if json_text(identity(entry)) != old.identity_json:
                raise Refused(BoxRefusal.NON_MONOTONE_REGISTRATION, "identity changed")
            earlier = {q["id"]: q for q in self._entry(batch)["questions"]}
            later = {q["id"]: q for q in entry["questions"]}
            for qid, question in earlier.items():
                if (
                    qid not in later
                    or question_core(question) != question_core(later[qid])
                    or (
                        question["retired_rev"] is not None
                        and question["retired_rev"] != later[qid]["retired_rev"]
                    )
                ):
                    raise Refused(
                        BoxRefusal.NON_MONOTONE_REGISTRATION,
                        "question changed or removed",
                    )
        if len({q["id"] for q in entry["questions"]}) != len(entry["questions"]):
            raise Refused(BoxRefusal.VALIDATION, "duplicate question id")
        for question in entry["questions"]:
            if question["digest"] != m.digest(
                {
                    key: val
                    for key, val in question.items()
                    if key not in ("digest", "retired_rev", "truncated")
                }
            ):
                raise Refused(BoxRefusal.VALIDATION, "question digest mismatch")
        classification = None
        if entry["classified"]:
            if (
                entry["category"] is None
                or entry["delegable"] is None
                or entry["classification_floor"] is None
            ):
                raise Refused(BoxRefusal.VALIDATION, "classification incomplete")
            classification = m.Classification(
                entry["category"],
                entry["delegable"],
                tuple(entry["sensitive_topics"]),
                (),
                entry["classification_floor"],
                classified=True,
            )
        brief = m.BriefEscalation(
            entry["brief_project"] or m.BriefProject.PERSONAL,
            (),
            tuple(entry["brief_ask_carlos"]),
        )
        derived = guard.derive_floor(
            questions(entry),
            classification=classification,
            brief=brief,
            cwd=entry["cwd"],
            remote=entry["remote"],
            secret_present=entry["secret_present"],
        )
        floor = m.EscalationLevel(
            max(
                derived.floor,
                entry["mac_floor"],
                m.EscalationLevel.L3_PRINCIPAL
                if not entry["classified"]
                or any(q["truncated"] for q in entry["questions"])
                else 0,
                ledger[0] if ledger else 0,
            )
        )
        now = self.clock()
        record = (
            FloorRecord(
                batch,
                entry["task_id"],
                entry["callback_bot"],
                json_text(identity(entry)),
                rev,
                floor,
                bool((ledger and ledger[2]) or floor >= m.EscalationLevel.L3_PRINCIPAL),
                BoxBatchState.REGISTERED,
                None,
                None,
                None,
                0,
                0,
                now,
                None,
                None,
                None,
                None,
                None,
                False,
            )
            if old is None
            else replace(
                old,
                latest_rev=rev,
                floor=floor,
                owned=old.owned or floor >= m.EscalationLevel.L3_PRINCIPAL,
                last_activity_at=now,
            )
        )
        self.db.execute(
            "INSERT INTO revision VALUES (?,?,?,?,?,?,?,?)",
            (
                batch,
                rev,
                json_text(entry),
                entry["content_digest"],
                int(derived.floor),
                json_text(derived.hits),
                m.file_digest(guard.GUARD_FILE.read_bytes()),
                now.isoformat(),
            ),
        )
        record = self._after_registration(old, record, new_revision=old is not None)
        if record.callback_bot not in self.bots and (
            old is None
            or record.state
            in (
                BoxBatchState.REGISTERED,
                BoxBatchState.AWAITING_L3,
                BoxBatchState.AWAITING_L4,
            )
        ):
            record = replace(
                record,
                state=BoxBatchState.BLOCKED,
                blocked_cause=BoxBlockCause.UNKNOWN_CALLBACK_BOT,
            )
            self._report(BoxReport.UNKNOWN_CALLBACK_BOT, batch)
        self._save_record(record)

    def _apply_progress(self, progress: m.RegisterProgress) -> None:
        batch, rev = progress["batch_id"], progress["batch_rev"]
        if not self.db.execute(
            "SELECT 1 FROM revision WHERE batch=? AND rev=?", (batch, rev)
        ).fetchone():
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        old = self._record(batch)
        raise_ = progress["floor_raise"]
        floor = max(old.floor, raise_["floor"] if raise_ else old.floor)
        record = replace(
            old, floor=m.EscalationLevel(floor), owned=old.owned or floor >= 3
        )
        if progress["seq"] > old.progress_seq:
            record = replace(
                record,
                progress_seq=progress["seq"],
                escalated=old.escalated or progress["escalated"] is not None,
                local_resolution=progress["local_resolution"]
                if progress["local_resolution"] is not None
                and (
                    old.local_resolution is None
                    or progress["local_resolution"] >= old.local_resolution
                )
                else old.local_resolution,
                local_level=progress["local_level"]
                if progress["local_resolution"] is not None
                and (
                    old.local_resolution is None
                    or progress["local_resolution"] >= old.local_resolution
                )
                else old.local_level,
            )
            if record.escalated:
                record = replace(
                    record,
                    floor=max(record.floor, m.EscalationLevel.L3_PRINCIPAL),
                    owned=True,
                )
        # Every stale-seq raise is recorded; progress_seq never authorizes ignoring it.
        self.db.execute(
            "INSERT OR IGNORE INTO progress VALUES (?,?,?,?,?)",
            (
                batch,
                rev,
                progress["seq"],
                json_text(progress),
                self.clock().isoformat(),
            ),
        )
        record = replace(record, last_activity_at=self.clock())
        self._save_record(self._after_registration(old, record, new_revision=False))

    def _ack(self, batch: str, rev: int) -> PlanTicket:
        record = self._record(batch)
        row = self.db.execute(
            "SELECT content_digest FROM revision WHERE batch=? AND rev=?", (batch, rev)
        ).fetchone()
        if row is None:
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        return self._plan(
            m.PlanAction.ACK,
            m.AckBody(
                batch_id=batch,
                batch_rev=rev,
                content_digest=row[0],
                progress_seq=record.progress_seq,
                floor=record.floor,
                owned=record.owned,
            ),
        )

    def _unresolved_plan_ids(self) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT id FROM plan WHERE action=? AND state IN (?,?,?) AND late_result_json IS NULL",
                (
                    m.PlanAction.SEND.value,
                    PlanState.ISSUED.value,
                    PlanState.FINAL_NONE.value,
                    PlanState.FINAL_EXPIRED.value,
                ),
            )
        ]

    def _plan(
        self,
        action: m.PlanAction,
        body: m.SendBody
        | m.AckBody
        | m.SyncBody
        | m.ClearBody
        | m.ReconcileBody
        | m.CancelBody,
        *,
        lease_id: str | None = None,
        delivery: Delivery | None = None,
        by: str = DINESH,
    ) -> PlanTicket:
        if action == m.PlanAction.SYNC:
            sync = m.parse_record(m.SyncBody, body)
            outstanding = set(self._unresolved_plan_ids())
            body = m.SyncBody(
                tasks=sync["tasks"],
                cursor=sync["cursor"],
                plan_ids=[
                    identifier
                    for identifier in sync["plan_ids"]
                    if identifier in outstanding
                ],
            )
        raw = dict(
            schema=m.PLAN_SCHEMA,
            plan_id=str(uuid4()),
            action=action,
            issued_by=by,
            lease_id=lease_id,
            body=body,
        )
        plan = m.parse_plan({**raw, "plan_digest": m.digest(raw)})
        self.db.execute(
            "INSERT INTO plan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan["plan_id"],
                action.value,
                lease_id,
                delivery.id if delivery else None,
                delivery.attempt if delivery else None,
                json_text(body),
                plan["plan_digest"],
                PlanState.ISSUED.value,
                None,
                by,
                self.clock().isoformat(),
                None,
                None,
            ),
        )
        return PlanTicket(
            plan,
            f'"{self.config.broker_path}" run-plan --b64 {shlex.quote(encoded(plan))}',
            self.config.mac_machine_id,
        )

    def _expire(self, batch: str | None = None) -> None:
        sql, args = (
            ("SELECT * FROM lease", ())
            if batch is None
            else ("SELECT * FROM lease WHERE batch=?", (batch,))
        )
        for lease in self._rows(Lease, sql, args):
            plan = self._one(PlanRow, "SELECT * FROM plan WHERE id=?", (lease.plan_id,))
            cutoff = (
                lease.dead_at
                if plan and plan.state == PlanState.FINAL_NONE
                else lease.expires_at
            )
            if self.clock() < cutoff:
                continue
            if plan and plan.state == PlanState.ISSUED:
                self.db.execute(
                    "UPDATE plan SET state=?,final_at=? WHERE id=?",
                    (PlanState.FINAL_EXPIRED.value, self.clock().isoformat(), plan.id),
                )
                self.db.execute(
                    "UPDATE delivery SET state=?,updated_at=? WHERE id=?",
                    (
                        m.DeliveryState.DISPATCH_UNCERTAIN.value,
                        self.clock().isoformat(),
                        lease.delivery_id,
                    ),
                )
            self._release(lease.batch)

    def _release(self, batch: str) -> None:
        self.db.execute("DELETE FROM lease WHERE batch=?", (batch,))
        for row in self.db.execute(
            "SELECT n,kind,json FROM queued_registration WHERE batch=? ORDER BY n",
            (batch,),
        ).fetchall():
            raw = json.loads(row[2])
            try:
                with self._queue_savepoint():
                    if QueuedKind(row[1]) == QueuedKind.ENTRY:
                        value = m.parse_register_entry(raw)
                        self._apply_entry(value)
                    else:
                        value = parse_progress(raw)
                        self._apply_progress(value)
                    # The next register/tick emits ACKs; result never emits one.
                    self.db.execute(
                        "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                        (f"ack:{batch}:{value['batch_rev']}", "1"),
                    )
            except (Refused, ValueError) as error:
                self._registration_refused(batch, error)
            self.db.execute("DELETE FROM queued_registration WHERE n=?", (row[0],))
        self._apply_deferred_status(batch)

    def _apply_deferred_status(self, batch: str) -> None:
        for cause in DeferredStatus:
            key = f"{cause}:{batch}"
            if self.db.execute("SELECT 1 FROM box_meta WHERE key=?", (key,)).fetchone():
                record = self._record(batch)
                if record.state not in INACTIVE:
                    if cause == DeferredStatus.CANCEL:
                        self._cancel(batch)
                    else:
                        self._save_record(
                            replace(
                                record, state=BoxBatchState.STALE, correction_floor=None
                            )
                        )
                self.db.execute("DELETE FROM box_meta WHERE key=?", (key,))

    @contextmanager
    def _queue_savepoint(self) -> Generator[None]:
        self.db.execute("SAVEPOINT queued_item")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO queued_item")
            raise
        finally:
            self.db.execute("RELEASE queued_item")

    def show(self, batch: str) -> BoxShow:
        with self.transaction():
            self._expire(batch)
            record = self._record(batch)
            return BoxShow(
                schema="qe.boxshow/1",
                record=record,
                lease=self._one(Lease, "SELECT * FROM lease WHERE batch=?", (batch,)),
                revisions=self._rows(
                    Revision,
                    "SELECT * FROM revision WHERE batch=? ORDER BY rev",
                    (batch,),
                ),
                progress=self._rows(
                    Progress,
                    "SELECT * FROM progress WHERE batch=? ORDER BY seq",
                    (batch,),
                ),
                decisions=self._rows(
                    Decision,
                    "SELECT * FROM decision WHERE batch=? ORDER BY resolution_rev",
                    (batch,),
                ),
                cards=self._rows(
                    Card,
                    "SELECT * FROM card_generation WHERE task=? AND callback_bot=? ORDER BY gen",
                    (record.task, record.callback_bot),
                ),
                deliveries=self._rows(
                    Delivery,
                    "SELECT * FROM delivery WHERE batch=? ORDER BY resolution_rev",
                    (batch,),
                ),
                plans=self._rows(
                    PlanRow,
                    "SELECT p.* FROM plan p LEFT JOIN delivery d ON p.delivery_id=d.id WHERE d.batch=? OR (p.action<>? AND json_extract(p.body_json,'$.batch_id')=?) OR (p.action=? AND (json_array_length(p.body_json,'$.tasks')=0 OR EXISTS(SELECT 1 FROM json_each(p.body_json,'$.tasks') WHERE value=?))) ORDER BY p.issued_at,p.id",
                    (
                        batch,
                        m.PlanAction.SEND.value,
                        batch,
                        m.PlanAction.SYNC.value,
                        record.task,
                    ),
                ),
                takeovers=self._rows(
                    Takeover, "SELECT * FROM takeover_seen WHERE batch=?", (batch,)
                ),
                reports=self._rows(
                    Report,
                    "SELECT * FROM report WHERE batch=? OR batch IS NULL ORDER BY id",
                    (batch,),
                ),
                untrusted=list(UNTRUSTED_FIELDS),
            )

    def status(self, task: str | None = None) -> list[BoxShow]:
        with self.transaction():
            self._expire()
            return [
                self.show(row[0])
                for row in self.db.execute(
                    "SELECT batch FROM floor_record WHERE (? IS NULL OR task=?) ORDER BY task,batch",
                    (task, task),
                ).fetchall()
            ]

    def decide(
        self,
        kind: DecisionKind,
        level: m.DecisionLevel,
        batch: str,
        expected_rev: int,
        value: object,
        *,
        by: str = DINESH,
        card_gen: int | None = None,
        card_ref: str | None = None,
        supersedes: int | None = None,
    ) -> Output:
        kind = DecisionKind(kind)
        if level not in (3, 4):
            raise Refused(BoxRefusal.VALIDATION, "decision level must be 3 or 4")
        decision_level = m.EscalationLevel(level)
        try:
            data = m.parse_record(DecisionInput, value)
        except (ValueError, TypeError, KeyError) as error:
            raise Refused(BoxRefusal.VALIDATION, str(error)) from error
        with self.transaction():
            record = self._open(batch)
            if expected_rev != record.latest_rev:
                raise Refused(BoxRefusal.STALE_REVISION)
            if level == 3 and by != DINESH or level == 4 and by != record.callback_bot:
                raise Refused(
                    BoxRefusal.WRONG_STATE, "decider does not own this action"
                )
            if level < max(record.floor, record.correction_floor or 0):
                raise Refused(BoxRefusal.FLOOR_TOO_HIGH)
            if kind == DecisionKind.ANSWER:
                required_state = (
                    BoxBatchState.AWAITING_L3
                    if level == 3
                    else BoxBatchState.AWAITING_L4
                )
                if record.state != required_state:
                    raise Refused(BoxRefusal.WRONG_STATE)
            else:
                if (
                    record.state not in CORRECTABLE
                    or record.state == BoxBatchState.LOCAL
                    and record.local_resolution is None
                ):
                    raise Refused(BoxRefusal.WRONG_STATE)
                if level == 3 and record.state not in (
                    BoxBatchState.LOCAL,
                    BoxBatchState.CORRECTION_DUE,
                ):
                    raise Refused(BoxRefusal.WRONG_STATE)
                current = self._current_resolution(record)
                if supersedes is None or supersedes != current:
                    raise Refused(BoxRefusal.SUPERSEDES_MISMATCH)
            if level == 4:
                purpose = (
                    CardPurpose.ASK
                    if kind == DecisionKind.ANSWER
                    else CardPurpose.CORRECTION
                )
                card = self._one(
                    Card,
                    "SELECT * FROM card_generation WHERE task=? AND callback_bot=? AND gen=?",
                    (record.task, record.callback_bot, card_gen),
                )
                if (
                    card is None
                    or card.state != CardGenState.ACTIVE
                    or card.purpose != purpose
                    or card.ref != card_ref
                    or [batch, expected_rev] not in json.loads(card.pairs_json)
                ):
                    raise Refused(BoxRefusal.CARD_NOT_ACTIVE)
            entry = self._entry(batch)
            current_questions = questions(entry, redact=True)
            answers = tuple(
                m.Answer(
                    m.QuestionId(UUID(a["question_id"])),
                    tuple(m.OptionIndex(i) for i in a["choice"]),
                    m.Untrusted(a["free_text"], m.UntrustedSource.HOST)
                    if a["free_text"] is not None
                    else None,
                    m.Untrusted(a["rationale"], m.UntrustedSource.HOST),
                    (),
                    None,
                )
                for a in data["answers"]
            )
            clearances = tuple(
                m.WeakClearance(
                    m.RuleId(c["rule_id"]),
                    c["trigger"],
                    m.Untrusted(c["reason"], m.UntrustedSource.HOST),
                )
                for c in data["clearances"]
            )
            revrow = self._one(
                Revision,
                "SELECT * FROM revision WHERE batch=? AND rev=?",
                (batch, expected_rev),
            )
            if revrow is None:
                raise Refused(BoxRefusal.UNKNOWN_ENTRY)
            hits = m.parse_record(tuple[m.GuardHit, ...], json.loads(revrow.hits_json))
            failures = guard.validate_answers(
                current_questions,
                answers,
                level=decision_level,
                repo=ROOT,
                clearances=clearances,
                hits=hits,
            )
            if failures:
                raise Refused(BoxRefusal.VALIDATION, json_text(failures))
            by_question = {str(q.id): q for q in current_questions}
            for a in data["answers"]:
                question = by_question.get(a["question_id"])
                choices = (
                    {int(o.index): o.label.text for o in question.options}
                    if question
                    else {}
                )
                rendered = ", ".join(choices.get(i, "") for i in a["choice"])
                if a["free_text"]:
                    rendered += ("; " if rendered else "") + a["free_text"]
                if len(rendered) > 500 or len(a["rationale"]) > 200:
                    raise Refused(BoxRefusal.TEXT_TOO_LONG)
            previous = self.db.execute(
                "SELECT coalesce(max(resolution_rev),0) FROM decision WHERE batch=?",
                (batch,),
            ).fetchone()[0]
            resolution = max(previous, record.local_resolution or 0) + 1
            delivery_id, token = str(uuid4()), secrets.token_urlsafe(24)
            ident = m.parse_record(Identity, json.loads(record.identity_json))
            child = ident.actor if ident.role == m.ActorRole.NATIVE_SUBAGENT else None
            text = m.render_answer(
                m.BatchId(UUID(batch)),
                m.ResolutionRev(resolution),
                current_questions,
                answers,
                decision_level,
                self.config.broker_path,
                m.DeliveryId(delivery_id),
                token,
                supersedes=m.ResolutionRev(supersedes)
                if supersedes is not None
                else None,
                child_actor=m.ActorKey(child) if child else None,
            )
            # The shared renderer quotes filesystem paths. Here HOME is a
            # prescribed Mac-side expansion, not a literal box filesystem path.
            text = text.replace(
                shlex.quote(self.config.broker_path), f'"{self.config.broker_path}"'
            )
            if len(text.encode()) > m.MAX_DELIVERY_TEXT:
                raise Refused(BoxRefusal.TEXT_TOO_LONG)
            self.db.execute(
                "INSERT INTO decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch,
                    resolution,
                    expected_rev,
                    level,
                    kind.value,
                    supersedes,
                    json_text(data["answers"]),
                    json_text(data["clearances"]),
                    by,
                    card_gen,
                    card_ref,
                    self.clock().isoformat(),
                ),
            )
            if kind == DecisionKind.CORRECTION:
                self._suppress(batch, m.SuppressCause.NEWER_RESOLUTION)
            self._save_record(
                replace(
                    record,
                    state=BoxBatchState.DECIDED,
                    owned=True,
                    correction_floor=None,
                    kept_resolution=None,
                    last_activity_at=self.clock(),
                )
            )
            # Reviewing an unchanged answer on a context/revision bump grants no resend.
            newest = self._newest_relevant(record)
            if kind == DecisionKind.ANSWER and newest:
                prior = self._one(
                    Decision,
                    "SELECT * FROM decision WHERE batch=? AND resolution_rev=?",
                    (batch, newest[0]),
                )
                if (
                    prior
                    and prior.answers_json == json_text(data["answers"])
                    and prior.level >= record.floor
                ):
                    self._save_record(
                        replace(self._record(batch), state=BoxBatchState.DELIVERED)
                    )
                    return Output()
            routes = (
                (m.Route.CODEX_QUEUE, m.Route.CODEX_QUEUE_RESUME, m.Route.REX_SEND)
                if ident.agent == m.AgentKind.CODEX
                else (m.Route.REX_SEND,)
            )
            self.db.execute(
                "INSERT INTO delivery VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    batch,
                    resolution,
                    (
                        m.DeliveryPurpose.ANSWER
                        if kind == DecisionKind.ANSWER
                        else m.DeliveryPurpose.CORRECTION
                    ).value,
                    ident.root_actor if child else ident.actor,
                    child,
                    text,
                    m.digest(text),
                    m.body_digest(text),
                    token,
                    json_text(routes),
                    m.DeliveryState.PENDING.value,
                    None,
                    None,
                    0,
                    0,
                    expected_rev,
                    int(record.floor),
                    0,
                    None,
                    None,
                    None,
                    self.clock().isoformat(),
                ),
            )
            pause = self.db.execute(
                "SELECT takeover_id FROM takeover_seen WHERE batch=? AND cleared=0",
                (batch,),
            ).fetchone()
            if pause:
                self.db.execute(
                    "UPDATE delivery SET paused_by=? WHERE id=?",
                    (pause[0], delivery_id),
                )
                return Output(notices=(f"delivery_held:{delivery_id}:paused",))
            try:
                return Output(plans=(self._deliver(delivery_id, by=by),))
            except Refused as error:
                if error.reason != BoxRefusal.CLOCK_SKEW_HOLD:
                    raise
                return Output(notices=(f"delivery_held:{delivery_id}:{error.reason}",))

    def _current_resolution(self, record: FloorRecord) -> int | None:
        if record.state == BoxBatchState.CORRECTION_DUE:
            return record.kept_resolution
        row = self.db.execute(
            "SELECT max(resolution_rev) FROM decision WHERE batch=?", (record.batch,)
        ).fetchone()
        return max(row[0] or 0, record.local_resolution or 0) or None

    def deliver(self, delivery_id: str, *, by: str = DINESH) -> Output:
        with self.transaction():
            return Output(plans=(self._deliver(delivery_id, by=by),))

    def _deliver(self, delivery_id: str, *, by: str = DINESH) -> PlanTicket:
        delivery = self._one(
            Delivery, "SELECT * FROM delivery WHERE id=?", (delivery_id,)
        )
        if delivery is None:
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        record = self._open(delivery.batch)
        if self.db.execute(
            "SELECT value FROM box_meta WHERE key=?", (MetaKey.CLOCK_SKEW_HOLD.value,)
        ).fetchone():
            raise Refused(BoxRefusal.CLOCK_SKEW_HOLD)
        if (
            record.state != BoxBatchState.DECIDED
            or delivery.state not in UNDISPATCHED
            or delivery.paused_by is not None
            or delivery.failed_sends >= MAX_SEND_FAILURES
        ):
            raise Refused(BoxRefusal.WRONG_STATE)
        decision = self._one(
            Decision,
            "SELECT * FROM decision WHERE batch=? AND resolution_rev=?",
            (delivery.batch, delivery.resolution_rev),
        )
        if decision is None:
            raise Refused(BoxRefusal.UNKNOWN_ENTRY)
        if (
            decision.batch_rev != record.latest_rev
            or delivery.authorized_rev != record.latest_rev
        ):
            raise Refused(BoxRefusal.STALE_REVISION)
        if decision.level < record.floor:
            raise Refused(BoxRefusal.FLOOR_TOO_HIGH)
        if delivery.resolution_rev != self._current_resolution(record):
            raise Refused(BoxRefusal.SUPERSEDES_MISMATCH)
        unknown = self.db.execute(
            "SELECT takeover_id FROM takeover_seen WHERE batch=? AND cleared=0",
            (record.batch,),
        ).fetchone()
        if unknown:
            raise Refused(BoxRefusal.WRONG_STATE, "uncleared takeover")
        entry = self._entry(record.batch)
        ident = m.parse_record(Identity, json.loads(record.identity_json))
        now = self.clock()
        delivery = replace(delivery, attempt=delivery.attempt + 1)
        lease_id = str(uuid4())
        body = m.SendBody(
            delivery_id=delivery.id,
            attempt=delivery.attempt,
            resend_after_reconcile=delivery.resend_allowed,
            issued_at=now.isoformat(),
            valid_s=int(m.PLAN_VALID.total_seconds()),
            batch_id=record.batch,
            batch_rev=record.latest_rev,
            floor=record.floor,
            resolution_rev=delivery.resolution_rev,
            purpose=delivery.purpose,
            target_actor=delivery.target_actor,
            child_actor=delivery.child_actor,
            rex_session=ident.rex_session,
            rex_block=ident.rex_block,
            session_marker=None,
            allowed_routes=m.parse_record(
                list[m.Route], json.loads(delivery.route_set)
            ),
            codex_thread=entry["root_session"]
            if ident.agent == m.AgentKind.CODEX
            else None,
            text=delivery.text,
            ack_token=delivery.ack_token,
            echo_wait_s=int(m.ECHO_WAIT.total_seconds()),
        )
        ticket = self._plan(
            m.PlanAction.SEND, body, lease_id=lease_id, delivery=delivery, by=by
        )
        dead_at = now + m.PLAN_VALID + m.CLOCK_SKEW_MAX + m.ROUTE_TIMEOUT
        self.db.execute(
            "INSERT INTO lease VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record.batch,
                lease_id,
                delivery.id,
                ticket.plan["plan_id"],
                record.latest_rev,
                int(record.floor),
                now.isoformat(),
                dead_at.isoformat(),
                (now + LEASE_TTL).isoformat(),
            ),
        )
        self.db.execute(
            "UPDATE delivery SET attempt=?,resend_allowed=0,authorized_floor=?,updated_at=? WHERE id=?",
            (delivery.attempt, int(record.floor), now.isoformat(), delivery.id),
        )
        return ticket

    def _pairs(
        self, task: str, callback_bot: str, purpose: CardPurpose
    ) -> tuple[tuple[str, int], ...]:
        records = self._rows(
            FloorRecord,
            "SELECT * FROM floor_record WHERE task=? AND callback_bot=? ORDER BY batch",
            (task, callback_bot),
        )
        return tuple(
            (r.batch, r.latest_rev)
            for r in records
            if (purpose == CardPurpose.ASK and r.state == BoxBatchState.AWAITING_L4)
            or (
                purpose == CardPurpose.CORRECTION
                and r.state in CORRECTABLE
                and (r.state != BoxBatchState.LOCAL or r.local_resolution is not None)
            )
        )

    def handoff(
        self,
        task: str,
        callback_bot: str,
        *,
        purpose: CardPurpose = CardPurpose.ASK,
        resend: bool = False,
        by: str = DINESH,
    ) -> Output:
        with self.transaction():
            if by != DINESH:
                raise Refused(BoxRefusal.WRONG_STATE)
            records = self._rows(
                FloorRecord,
                "SELECT * FROM floor_record WHERE task=? AND callback_bot=? ORDER BY batch",
                (task, callback_bot),
            )
            required = (
                BoxBatchState.AWAITING_L3
                if purpose == CardPurpose.ASK
                else BoxBatchState.CORRECTION_DUE
            )
            for grouped in records:
                self._unleased(grouped.batch)
            eligible = [
                r
                for r in records
                if r.state == required
                or resend
                and purpose == CardPurpose.ASK
                and r.state == BoxBatchState.AWAITING_L4
            ]
            if not eligible or callback_bot not in self.bots:
                raise Refused(BoxRefusal.WRONG_STATE)
            for record in eligible:
                self._unleased(record.batch)
                self._save_record(
                    replace(
                        record,
                        state=BoxBatchState.AWAITING_L4
                        if purpose == CardPurpose.ASK
                        else record.state,
                        correction_floor=m.EscalationLevel.L4_OWNER
                        if purpose == CardPurpose.CORRECTION
                        else None,
                        owned=True,
                        last_activity_at=self.clock(),
                    )
                )
            pairs = tuple((r.batch, r.latest_rev) for r in eligible)
            existing = self._one(
                Handoff,
                "SELECT * FROM handoff WHERE task=? AND callback_bot=? AND purpose=? AND batch_revs_json=? ORDER BY last_sent_at DESC LIMIT 1",
                (task, callback_bot, purpose.value, json_text(pairs)),
            )
            if existing:
                if resend and existing.sent_count >= 3:
                    self._report(BoxReport.HANDOFF_UNACKED, None, task)
                    return Output(notices=(BoxReport.HANDOFF_UNACKED.value,))
                handoff_id = existing.id
                if resend:
                    self.db.execute(
                        "UPDATE handoff SET sent_count=sent_count+1,last_sent_at=? WHERE id=?",
                        (self.clock().isoformat(), handoff_id),
                    )
            else:
                handoff_id = str(uuid4())
                self.db.execute(
                    "INSERT INTO handoff VALUES (?,?,?,?,?,?,?)",
                    (
                        handoff_id,
                        task,
                        callback_bot,
                        purpose.value,
                        json_text(pairs),
                        1,
                        self.clock().isoformat(),
                    ),
                )
            return Output(
                handoffs=(self._handoff_payload(handoff_id, task, callback_bot, pairs),)
            )

    def _handoff_payload(
        self,
        handoff_id: str,
        task: str,
        callback_bot: str,
        pairs: tuple[tuple[str, int], ...],
    ) -> m.L4Handoff:
        batches = []
        for batch, rev in pairs:
            record, entry = self._record(batch), self._entry(batch, rev)
            revision = self._one(
                Revision, "SELECT * FROM revision WHERE batch=? AND rev=?", (batch, rev)
            )
            if revision is None:
                raise Refused(BoxRefusal.UNKNOWN_ENTRY)
            hits = m.parse_record(
                tuple[m.GuardHit, ...], json.loads(revision.hits_json)
            )
            progress_row = self.db.execute(
                "SELECT json FROM progress WHERE batch=? AND rev=? ORDER BY seq DESC LIMIT 1",
                (batch, rev),
            ).fetchone()
            recommendations = (
                m.parse_register_progress(json.loads(progress_row[0]))[
                    "recommendations"
                ]
                if progress_row
                else []
            )
            # Registered text was already treated as data; redact again at this egress.
            visible = questions(entry, redact=True)
            batches.append(
                m.HandoffBatch(
                    batch_id=batch,
                    batch_rev=rev,
                    floor=record.floor,
                    hits=[
                        m.DigestHit(
                            rule_id=h.rule_id, trigger=h.trigger, strength=h.strength
                        )
                        for h in hits
                    ],
                    questions=[
                        m.HandoffQuestion(
                            id=str(q.id),
                            text=q.text.text,
                            header=q.header.text if q.header else None,
                            options=[
                                m.DigestOption(
                                    index=o.index,
                                    label=o.label.text,
                                    description=o.description.text
                                    if o.description
                                    else None,
                                )
                                for o in q.options
                            ],
                            multi_select=q.multi_select,
                            allows_free_text=q.allows_free_text,
                        )
                        for q in visible
                        if q.retired_rev is None
                    ],
                    recommendations=[
                        m.DigestRecommendation(
                            **{
                                **recommendation,
                                "rationale": redaction.redact(
                                    recommendation["rationale"]
                                ).text,
                            }
                        )
                        for recommendation in recommendations
                    ],
                )
            )
        return m.L4Handoff(
            schema="qe.l4handoff/1",
            handoff_id=handoff_id,
            task_id=task,
            callback_bot=callback_bot,
            batches=batches,
        )

    def card_intent(
        self,
        task: str,
        callback_bot: str,
        *,
        purpose: CardPurpose = CardPurpose.ASK,
        gen: int | None = None,
        reconciles: int | None = None,
        by: str = DINESH,
    ) -> Card:
        try:
            return self._card_intent(
                task,
                callback_bot,
                purpose=purpose,
                gen=gen,
                reconciles=reconciles,
                by=by,
            )
        except Refused as error:
            if error.reason == BoxRefusal.CARD_CAP_REACHED:
                # The failed intent transaction has rolled back. Keep its alert.
                with self.transaction():
                    for batch, _ in self._pairs(task, callback_bot, purpose):
                        self._report(BoxReport.CARD_CAP_REACHED, batch)
            raise

    def _card_intent(
        self,
        task: str,
        callback_bot: str,
        *,
        purpose: CardPurpose = CardPurpose.ASK,
        gen: int | None = None,
        reconciles: int | None = None,
        by: str = DINESH,
    ) -> Card:
        with self.transaction():
            if by != callback_bot or callback_bot not in self.bots:
                raise Refused(
                    BoxRefusal.WRONG_STATE, "only the launching bot may claim a card"
                )
            for grouped in self._rows(
                FloorRecord,
                "SELECT * FROM floor_record WHERE task=? AND callback_bot=?",
                (task, callback_bot),
            ):
                self._unleased(grouped.batch)
            pairs = self._pairs(task, callback_bot, purpose)
            if not pairs:
                raise Refused(BoxRefusal.WRONG_STATE)
            for batch, _ in pairs:
                self._unleased(batch)
            cards = self._rows(
                Card,
                "SELECT * FROM card_generation WHERE task=? AND callback_bot=? ORDER BY gen",
                (task, callback_bot),
            )
            intended = [
                c
                for c in cards
                if c.state == CardGenState.INTENDED and c.purpose == purpose
            ]
            if intended:
                previous = intended[-1]
                if (
                    reconciles != previous.gen
                    or self.clock() - previous.at < CARD_INTENT_WAIT
                ):
                    raise Refused(
                        BoxRefusal.CARD_NOT_ACTIVE,
                        "pending intent must be reconciled after ten minutes",
                    )
            elif reconciles is not None:
                raise Refused(BoxRefusal.CARD_NOT_ACTIVE)
            abandoned = sum(
                c.state == CardGenState.ABANDONED
                and c.purpose == purpose
                and c.pairs_json == json_text(pairs)
                for c in cards
            )
            if abandoned >= m.MAX_ABANDONED_CARD_GENERATIONS:
                raise Refused(BoxRefusal.CARD_CAP_REACHED)
            active = [
                c
                for c in cards
                if c.state == CardGenState.ACTIVE and c.purpose == purpose
            ]
            if any(c.pairs_json == json_text(pairs) for c in active):
                raise Refused(
                    BoxRefusal.CARD_NOT_ACTIVE, "active card already covers these pairs"
                )
            previous_post = next(
                (
                    c
                    for c in reversed(cards)
                    if c.purpose == purpose
                    and c.state in (CardGenState.ACTIVE, CardGenState.SUPERSEDED)
                ),
                None,
            )
            if (
                previous_post
                and previous_post.pairs_json != json_text(pairs)
                and self.clock() - previous_post.at < CARD_REPOST_MIN
            ):
                raise Refused(
                    BoxRefusal.CARD_NOT_ACTIVE,
                    "revision bumps are coalesced for fifteen minutes",
                )
            next_gen = max((c.gen for c in cards), default=0) + 1
            if gen is not None and gen != next_gen:
                raise Refused(
                    BoxRefusal.CARD_NOT_ACTIVE, "generation compare-and-swap failed"
                )
            if intended:
                self.db.execute(
                    "UPDATE card_generation SET state=? WHERE task=? AND callback_bot=? AND gen=? AND state=?",
                    (
                        CardGenState.ABANDONED.value,
                        task,
                        callback_bot,
                        intended[-1].gen,
                        CardGenState.INTENDED.value,
                    ),
                )
            card = Card(
                task,
                callback_bot,
                next_gen,
                purpose,
                json_text(pairs),
                CardGenState.INTENDED,
                None,
                reconciles,
                False,
                self.clock(),
                0,
            )
            self.db.execute(
                "INSERT INTO card_generation VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                tuple(self._sql(getattr(card, f.name)) for f in fields(card)),
            )
            return card

    def card_posted(
        self, task: str, callback_bot: str, gen: int, ref: str, *, by: str = DINESH
    ) -> Card:
        with self.transaction():
            card = self._one(
                Card,
                "SELECT * FROM card_generation WHERE task=? AND callback_bot=? AND gen=?",
                (task, callback_bot, gen),
            )
            if by != callback_bot or callback_bot not in self.bots:
                raise Refused(BoxRefusal.WRONG_STATE)
            if (
                card is None
                or card.state != CardGenState.INTENDED
                or not ref.strip()
                or len(ref) > 1024
            ):
                raise Refused(BoxRefusal.CARD_NOT_ACTIVE)
            for batch, rev in json.loads(card.pairs_json):
                record = self._open(batch)
                if record.latest_rev != rev or (batch, rev) not in self._pairs(
                    task, callback_bot, card.purpose
                ):
                    raise Refused(BoxRefusal.STALE_REVISION)
            self.db.execute(
                "UPDATE card_generation SET state=? WHERE task=? AND callback_bot=? AND purpose=? AND state=?",
                (
                    CardGenState.SUPERSEDED.value,
                    task,
                    callback_bot,
                    card.purpose.value,
                    CardGenState.ACTIVE.value,
                ),
            )
            self.db.execute(
                "UPDATE card_generation SET state=?,ref=?,at=? WHERE task=? AND callback_bot=? AND gen=?",
                (
                    CardGenState.ACTIVE.value,
                    ref,
                    self.clock().isoformat(),
                    task,
                    callback_bot,
                    gen,
                ),
            )
            return replace(card, state=CardGenState.ACTIVE, ref=ref, at=self.clock())

    def card_close(
        self, task: str, callback_bot: str, gen: int, *, by: str = DINESH
    ) -> Card:
        with self.transaction():
            if by != callback_bot:
                raise Refused(BoxRefusal.WRONG_STATE)
            card = self._one(
                Card,
                "SELECT * FROM card_generation WHERE task=? AND callback_bot=? AND gen=?",
                (task, callback_bot, gen),
            )
            if card is None or card.state not in (
                CardGenState.ACTIVE,
                CardGenState.SUPERSEDED,
            ):
                raise Refused(BoxRefusal.CARD_NOT_ACTIVE)
            self.db.execute(
                "UPDATE card_generation SET state=?,close_requested=0 WHERE task=? AND callback_bot=? AND gen=?",
                (CardGenState.CLOSED.value, task, callback_bot, gen),
            )
            return replace(card, state=CardGenState.CLOSED, close_requested=False)

    def _evidence(self, delivery_id: str, via: m.DeliveryEvidence) -> None:
        # The only writer of these two columns; subsequent evidence never rewrites.
        self.db.execute(
            "UPDATE delivery SET delivered_at=?,delivered_via=? WHERE id=? AND delivered_at IS NULL",
            (self.clock().isoformat(), via.value, delivery_id),
        )

    def result(
        self,
        line: str | None = None,
        *,
        plan_id: str | None = None,
        why: m.NoResultWhy | None = None,
        detail: str = "",
    ) -> Output:
        return self._result(
            line, plan_id=plan_id, why=why, detail=detail, historical=False
        )

    def _result(
        self,
        line: str | None = None,
        *,
        plan_id: str | None = None,
        why: m.NoResultWhy | None = None,
        detail: str = "",
        historical: bool,
    ) -> Output:
        result = None
        if line is not None:
            try:
                matched = RESULT_LINE.fullmatch(line)
                if matched is None or len(matched[1]) > m.MAX_SYNC_SINGLE_B64:
                    raise ValueError("invalid result line")
                result = m.parse_result(
                    json.loads(
                        base64.b64decode(
                            matched[1] + "=" * (-len(matched[1]) % 4),
                            altchars=b"-_",
                            validate=True,
                        )
                    )
                )
                if (
                    result["action"] != m.PlanAction.SYNC
                    and len(matched[1]) > m.MAX_RESULT_B64
                ):
                    raise ValueError("result exceeds bound")
                m.parse_record(datetime, result["mac_now"])
                plan_id = result["plan_id"]
            except (ValueError, TypeError, KeyError, UnicodeError) as error:
                raise Refused(BoxRefusal.BAD_RESULT, str(error)) from error
        elif plan_id is None or why is None:
            raise Refused(BoxRefusal.BAD_RESULT, "none requires a plan and reason")
        with self.transaction():
            plan = self._one(PlanRow, "SELECT * FROM plan WHERE id=?", (plan_id,))
            if plan is None:
                raise Refused(BoxRefusal.UNKNOWN_PLAN)
            if result is not None and (result["lease_id"] != plan.lease_id):
                raise Refused(BoxRefusal.LEASE_MISMATCH)
            if result is not None and (
                result["action"] != plan.action
                or result["plan_digest"] != plan.plan_digest
            ):
                raise Refused(BoxRefusal.BAD_RESULT)
            self._expire()
            plan = self._one(PlanRow, "SELECT * FROM plan WHERE id=?", (plan_id,))
            if plan is None:
                raise Refused(BoxRefusal.UNKNOWN_PLAN)
            if plan.state != PlanState.ISSUED:
                if result is None:
                    return Output()
                if result is not None and plan.result_json == json_text(result):
                    return Output()
                evidence = (
                    json_text(result)
                    if result is not None
                    else json_text(
                        dict(why=why, detail=redaction.redact(detail[:2048]).text)
                    )
                )
                self.db.execute(
                    "UPDATE plan SET late_result_json=? WHERE id=? AND late_result_json IS NULL",
                    (evidence, plan.id),
                )
                if plan.delivery_id:
                    self.db.execute(
                        "UPDATE delivery SET late_result_json=? WHERE id=? AND late_result_json IS NULL",
                        (evidence, plan.delivery_id),
                    )
                return Output()
            if result is not None and not historical and not result["replayed"]:
                self._skew(result)
            if result is None:
                self.db.execute(
                    "UPDATE plan SET state=?,result_json=?,final_at=? WHERE id=?",
                    (
                        PlanState.FINAL_NONE.value,
                        json_text(
                            dict(why=why, detail=redaction.redact(detail[:2048]).text)
                        ),
                        self.clock().isoformat(),
                        plan.id,
                    ),
                )
                if plan.delivery_id:
                    self.db.execute(
                        "UPDATE delivery SET state=?,updated_at=? WHERE id=?",
                        (
                            m.DeliveryState.DISPATCH_UNCERTAIN.value,
                            self.clock().isoformat(),
                            plan.delivery_id,
                        ),
                    )
                return Output()
            if plan.action == m.PlanAction.SEND:
                lease = self._one(
                    Lease, "SELECT * FROM lease WHERE plan_id=?", (plan.id,)
                )
                if (
                    lease is None
                    or lease.lease_id != result["lease_id"]
                    or self.clock() >= lease.expires_at
                ):
                    raise Refused(BoxRefusal.LEASE_MISMATCH)
            self.db.execute(
                "UPDATE plan SET state=?,result_json=?,final_at=? WHERE id=?",
                (
                    PlanState.FINAL_RESULT.value,
                    json_text(result),
                    self.clock().isoformat(),
                    plan.id,
                ),
            )
            if plan.action == m.PlanAction.SEND:
                if plan.delivery_id is None:
                    raise Refused(BoxRefusal.UNKNOWN_ENTRY)
                delivery = self._one(
                    Delivery, "SELECT * FROM delivery WHERE id=?", (plan.delivery_id,)
                )
                if delivery is None:
                    raise Refused(BoxRefusal.UNKNOWN_ENTRY)
                self._send_result(delivery, result)
                self._release(delivery.batch)
            elif result["ok"]:
                if plan.action == m.PlanAction.SYNC:
                    if result["payload"] is None:
                        raise Refused(BoxRefusal.BAD_RESULT)
                    output = self._sync(result["payload"])
                    if result["payload"]["more"]:
                        body = m.parse_record(m.SyncBody, json.loads(plan.body_json))
                        self.db.execute(
                            "DELETE FROM box_meta WHERE key=?",
                            (MetaKey.SYNC_CURSOR.value,),
                        )
                        return Output(
                            plans=output.plans
                            + (
                                self._plan(
                                    m.PlanAction.SYNC,
                                    m.SyncBody(
                                        tasks=body["tasks"],
                                        cursor=result["payload"]["cursor"],
                                        plan_ids=body["plan_ids"],
                                    ),
                                ),
                            ),
                            notices=output.notices,
                        )
                    return output
                elif plan.action == m.PlanAction.CLEAR_TAKEOVER:
                    body = m.parse_record(m.ClearBody, json.loads(plan.body_json))
                    self._clear(body["takeover_id"])
            return Output()

    def _skew(self, result: m.Result) -> None:
        mac = m.parse_record(datetime, result["mac_now"])
        if abs(self.clock() - mac) > m.CLOCK_SKEW_MAX:
            self.db.execute(
                "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                (MetaKey.CLOCK_SKEW_HOLD.value, "1"),
            )
            self._report(BoxReport.CLOCK_SKEW, None)
        else:
            self.db.execute(
                "DELETE FROM box_meta WHERE key=?", (MetaKey.CLOCK_SKEW_HOLD.value,)
            )

    def _send_result(self, delivery: Delivery, result: m.Result) -> None:
        record = self._record(delivery.batch)
        outcome, now = result["send"], self.clock()
        state, cause, pause = delivery.state, None, None
        failed = delivery.failed_sends
        if result["local_answer_dispatched"]:
            self._report(BoxReport.FLOOR_DISAGREEMENT, delivery.batch)
        if outcome in (m.SendOutcome.SENT_ECHOED, m.SendOutcome.SENT_NO_ECHO):
            state = (
                m.DeliveryState.OBSERVED
                if outcome == m.SendOutcome.SENT_ECHOED
                else m.DeliveryState.DISPATCHED
            )
            self._evidence(delivery.id, m.DeliveryEvidence(outcome.value))
            if record.state == BoxBatchState.DECIDED:
                record = replace(
                    record, state=BoxBatchState.DELIVERED, correction_floor=None
                )
        elif outcome in (
            m.SendOutcome.SEND_UNKNOWN,
            m.SendOutcome.NOT_SENT_ALREADY_SENT,
        ):
            state = m.DeliveryState.DISPATCH_UNCERTAIN
        elif outcome == m.SendOutcome.NOT_SENT_STALE_REVISION:
            state, cause = m.DeliveryState.SUPPRESSED, m.SuppressCause.NEWER_REVISION
        elif outcome == m.SendOutcome.NOT_SENT_PAUSED:
            state, pause = m.DeliveryState.PENDING, result["takeover_id"]
            self.db.execute(
                "INSERT OR IGNORE INTO takeover_seen VALUES (?,?,?,?)",
                (pause, delivery.batch, m.PromptOrigin.UNKNOWN.value, 0),
            )
        elif outcome == m.SendOutcome.NOT_SENT_CANCELLED:
            state, cause = m.DeliveryState.SUPPRESSED, m.SuppressCause.CANCELLED
            self._cancel(delivery.batch)
            record = self._record(delivery.batch)
        elif outcome == m.SendOutcome.NOT_SENT_SESSION_GONE:
            state = m.DeliveryState.ABANDONED
            record = replace(record, state=BoxBatchState.STALE, correction_floor=None)
        elif outcome == m.SendOutcome.NOT_SENT_SUPERSEDED:
            return
        else:
            state = m.DeliveryState.DISPATCH_FAILED
            failed += 1
            if (
                outcome == m.SendOutcome.NOT_SENT_NOT_DELIVERABLE
                or failed >= MAX_SEND_FAILURES
            ):
                record = replace(
                    record,
                    state=BoxBatchState.BLOCKED,
                    blocked_cause=BoxBlockCause.MAC_NOT_DELIVERABLE
                    if outcome == m.SendOutcome.NOT_SENT_NOT_DELIVERABLE
                    else BoxBlockCause.SEND_FAILURES,
                    send_failures=failed,
                )
                self._report(BoxReport.SEND_FAILED, record.batch)
        self.db.execute(
            "UPDATE delivery SET state=?,suppress_cause=?,paused_by=?,failed_sends=?,updated_at=? WHERE id=?",
            (
                state.value,
                cause.value if cause else None,
                pause,
                failed,
                now.isoformat(),
                delivery.id,
            ),
        )
        self._save_record(replace(record, last_activity_at=now))

    def _recover_correction(
        self, record: FloorRecord, delivery: Delivery, decision: Decision
    ) -> FloorRecord:
        # Appendix C1721: only current, latest correction evidence satisfies the
        # outstanding correction. An old-revision row or ACK-only observation
        # cannot close a newer correction request.
        newest = self._newest_relevant(record)
        latest = self.db.execute(
            "SELECT max(resolution_rev) FROM decision WHERE batch=?", (record.batch,)
        ).fetchone()[0]
        if (
            decision.kind == DecisionKind.CORRECTION
            and newest
            and newest[0] == delivery.resolution_rev == latest
            and newest[1] >= max(record.floor, record.correction_floor or record.floor)
            and decision.batch_rev == record.latest_rev
        ):
            return replace(
                record,
                state=BoxBatchState.DELIVERED,
                correction_floor=None,
                kept_resolution=None,
            )
        return record

    def _clear(self, takeover: str) -> None:
        self.db.execute(
            "UPDATE takeover_seen SET cleared=1 WHERE takeover_id=?", (takeover,)
        )
        # Uncertain/suppressed rows are never revived by clearance.
        self.db.execute(
            "UPDATE delivery SET paused_by=NULL WHERE paused_by=? AND state=?",
            (takeover, m.DeliveryState.PENDING.value),
        )

    def clear_takeover(self, takeover: str, *, by: str = DINESH) -> Output:
        with self.transaction():
            if by != DINESH:
                raise Refused(BoxRefusal.WRONG_STATE)
            row = self._one(
                Takeover, "SELECT * FROM takeover_seen WHERE takeover_id=?", (takeover,)
            )
            if row is None or row.origin != m.PromptOrigin.UNKNOWN:
                raise Refused(BoxRefusal.UNKNOWN_ENTRY)
            return Output(
                plans=(
                    self._plan(
                        m.PlanAction.CLEAR_TAKEOVER,
                        m.ClearBody(takeover_id=takeover, by=m.DeciderId(by)),
                    ),
                )
            )

    def reconcile(
        self,
        verdict: m.ReconcileVerdict,
        *,
        delivery_id: str | None = None,
        outbound_id: str | None = None,
        by: str = DINESH,
    ) -> Output:
        with self.transaction():
            if by != DINESH or (delivery_id is None) == (outbound_id is None):
                raise Refused(BoxRefusal.WRONG_STATE)
            if outbound_id is not None:
                row = self.db.execute(
                    "SELECT batch,state FROM local_outbound_seen WHERE id=?",
                    (outbound_id,),
                ).fetchone()
                if row is None or row[1] != m.DeliveryState.DISPATCH_UNCERTAIN.value:
                    raise Refused(BoxRefusal.WRONG_STATE)
                self._unleased(row[0])
                return Output(
                    plans=(
                        self._plan(
                            m.PlanAction.RECONCILE,
                            m.ReconcileBody(
                                target=m.ReconcileTargetKind.LOCAL_OUTBOUND,
                                target_id=outbound_id,
                                verdict=verdict,
                            ),
                        ),
                    )
                )
            delivery = self._one(
                Delivery, "SELECT * FROM delivery WHERE id=?", (delivery_id,)
            )
            if delivery is None:
                raise Refused(BoxRefusal.UNKNOWN_ENTRY)
            record = self._open(delivery.batch)
            if delivery.state != m.DeliveryState.DISPATCH_UNCERTAIN:
                raise Refused(BoxRefusal.WRONG_STATE)
            decision = self._one(
                Decision,
                "SELECT * FROM decision WHERE batch=? AND resolution_rev=?",
                (delivery.batch, delivery.resolution_rev),
            )
            if decision is None:
                raise Refused(BoxRefusal.UNKNOWN_ENTRY)
            below = decision.level < record.floor
            state, cause, pause, resend = (
                delivery.state,
                None,
                delivery.paused_by,
                False,
            )
            if verdict == m.ReconcileVerdict.SEEN_IN_PANE:
                state, pause = m.DeliveryState.OBSERVED, None
                self._evidence(delivery.id, m.DeliveryEvidence.SEEN_IN_PANE)
                if (
                    not below
                    and record.state == BoxBatchState.DECIDED
                    and delivery.resolution_rev == self._current_resolution(record)
                    and decision.batch_rev == record.latest_rev
                ):
                    record = replace(
                        record, state=BoxBatchState.DELIVERED, correction_floor=None
                    )
                elif record.state == BoxBatchState.CORRECTION_DUE and not below:
                    record = self._recover_correction(record, delivery, decision)
                elif below and record.state in (
                    BoxBatchState.AWAITING_L3,
                    BoxBatchState.AWAITING_L4,
                    BoxBatchState.CORRECTION_DUE,
                ):
                    newest = self._newest_relevant(record)
                    if newest and newest[1] < record.floor:
                        self._supersede_cards(record, CardPurpose.ASK)
                        record = replace(
                            record,
                            state=BoxBatchState.CORRECTION_DUE,
                            correction_floor=max(
                                record.correction_floor or record.floor, record.floor
                            ),
                            kept_resolution=newest[0],
                            correction_notified_at=record.correction_notified_at
                            or self.clock(),
                        )
                        self._report(BoxReport.FLOOR_DISAGREEMENT, record.batch)
            elif below:
                state, cause, pause = (
                    m.DeliveryState.SUPPRESSED,
                    m.SuppressCause.FLOOR_RAISED,
                    None,
                )
            else:
                takeovers = self._rows(
                    Takeover,
                    "SELECT * FROM takeover_seen WHERE batch=? AND cleared=0",
                    (record.batch,),
                )
                human = any(t.origin != m.PromptOrigin.UNKNOWN for t in takeovers)
                if human:
                    state, cause, pause = (
                        m.DeliveryState.SUPPRESSED,
                        m.SuppressCause.HUMAN_TAKEOVER,
                        None,
                    )
                else:
                    state, resend = m.DeliveryState.PENDING, True
                    pause = next((t.takeover_id for t in takeovers), None)
            self.db.execute(
                "UPDATE delivery SET state=?,suppress_cause=?,paused_by=?,resend_allowed=?,updated_at=? WHERE id=?",
                (
                    state.value,
                    cause.value if cause else None,
                    pause,
                    int(resend),
                    self.clock().isoformat(),
                    delivery.id,
                ),
            )
            self._save_record(record)
            return Output(
                plans=(
                    self._plan(
                        m.PlanAction.RECONCILE,
                        m.ReconcileBody(
                            target=m.ReconcileTargetKind.BOX_DELIVERY,
                            target_id=delivery.id,
                            verdict=verdict,
                        ),
                    ),
                )
            )

    def _cancel(self, batch: str) -> None:
        record = self._record(batch)
        if record.state in INACTIVE:
            raise Refused(BoxRefusal.WRONG_STATE)
        self._suppress(batch, m.SuppressCause.CANCELLED, keep_uncertain=False)
        self._save_record(
            replace(
                record,
                state=BoxBatchState.CANCELLED,
                correction_floor=None,
                last_activity_at=self.clock(),
            )
        )
        self.db.execute(
            "UPDATE card_generation SET close_requested=1 WHERE task=? AND callback_bot=? AND state=?",
            (record.task, record.callback_bot, CardGenState.ACTIVE.value),
        )

    def cancel(self, batch: str, *, from_mac: bool = False, by: str = DINESH) -> Output:
        with self.transaction():
            record = self._open(batch)
            if not from_mac and by not in (DINESH, record.callback_bot):
                raise Refused(BoxRefusal.WRONG_STATE)
            self._cancel(batch)
            return (
                Output()
                if from_mac
                else Output(
                    plans=(
                        self._plan(
                            m.PlanAction.CANCEL,
                            m.CancelBody(batch_id=batch, by=m.DeciderId(by)),
                        ),
                    )
                )
            )

    def retry(self, batch: str, *, by: str = DINESH) -> Output:
        with self.transaction():
            record = self._open(batch)
            if record.state != BoxBatchState.BLOCKED or by not in (
                DINESH,
                record.callback_bot,
            ):
                raise Refused(BoxRefusal.WRONG_STATE)
            if (
                record.blocked_cause == BoxBlockCause.UNKNOWN_CALLBACK_BOT
                and record.callback_bot not in self.bots
            ):
                raise Refused(
                    BoxRefusal.WRONG_STATE, "register the callback bot before retry"
                )
            pending = self.db.execute(
                "SELECT 1 FROM delivery d JOIN decision c ON c.batch=d.batch AND c.resolution_rev=d.resolution_rev WHERE d.batch=? AND c.batch_rev=? AND c.level>=? AND d.delivered_at IS NULL AND d.state IN (?,?)",
                (
                    batch,
                    record.latest_rev,
                    int(record.floor),
                    m.DeliveryState.PENDING.value,
                    m.DeliveryState.DISPATCH_FAILED.value,
                ),
            ).fetchone()
            newest = self._newest_relevant(record)
            correction_floor, kept = None, None
            if newest and newest[1] < record.floor and not pending:
                state = BoxBatchState.CORRECTION_DUE
                correction_floor, kept = record.floor, newest[0]
                self._report(BoxReport.FLOOR_DISAGREEMENT, batch)
            elif pending:
                state = BoxBatchState.DECIDED
            elif record.floor <= m.EscalationLevel.L2_NORMAL and not record.owned:
                state = BoxBatchState.LOCAL
            else:
                state = BoxBatchState.AWAITING_L3
            self.db.execute(
                "UPDATE delivery SET failed_sends=0 WHERE batch=? AND state=?",
                (batch, m.DeliveryState.DISPATCH_FAILED.value),
            )
            self._save_record(
                replace(
                    record,
                    state=state,
                    blocked_cause=None,
                    send_failures=0,
                    correction_floor=correction_floor,
                    kept_resolution=kept,
                    correction_notified_at=self.clock()
                    if correction_floor is not None
                    else record.correction_notified_at,
                    last_activity_at=self.clock(),
                )
            )
            return Output()

    def _sync(self, payload: m.SyncPayload) -> Output:
        plans: list[PlanTicket] = []
        notices: list[str] = []
        for value in (*payload["registrations"], *payload["progress"]):
            batch, rev = value["batch_id"], value["batch_rev"]
            try:
                if self.db.execute(
                    "SELECT 1 FROM lease WHERE batch=?", (batch,)
                ).fetchone():
                    self.register(value)
                    continue
                with self._queue_savepoint():
                    if value["schema"] == "qe.register/1":
                        self._apply_entry(m.parse_register_entry(value))
                    else:
                        self._apply_progress(parse_progress(value))
                    self.db.execute(
                        "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                        (f"ack:{batch}:{rev}", "1"),
                    )
                    self.db.execute(
                        "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                        (f"registered:{batch}", "1"),
                    )
            except (Refused, ValueError) as error:
                self._registration_refused(batch, error)
        for identifier in payload["oversized"]:
            if self._report(
                BoxReport.SYNC_OVERSIZED,
                identifier,
                "untrusted identifier; item requires operator review",
            ):
                self.db.execute(
                    "UPDATE report SET notified_at=? WHERE kind=? AND batch IS ?",
                    (
                        self.clock().isoformat(),
                        BoxReport.SYNC_OVERSIZED.value,
                        identifier,
                    ),
                )
                notices.append(f"{BoxReport.SYNC_OVERSIZED}:{identifier}")
        for saved in payload["plan_results"]:
            if saved["result_b64"] is not None:
                try:
                    with self._queue_savepoint():
                        output = self._result(
                            m.RESULT_PREFIX + saved["result_b64"], historical=True
                        )
                    plans.extend(output.plans)
                    notices.extend(output.notices)
                except Refused as error:
                    saved_batch = self.db.execute(
                        "SELECT coalesce(d.batch,json_extract(p.body_json,'$.batch_id')) FROM plan p LEFT JOIN delivery d ON d.id=p.delivery_id WHERE p.id=?",
                        (saved["plan_id"],),
                    ).fetchone()
                    self._report(
                        BoxReport.RESULT_REFUSED,
                        saved_batch[0] if saved_batch else None,
                        f"{saved['plan_id']}: {error.reason}: {error.detail}",
                    )
        status = payload["status"]
        for takeover in status["takeovers"]:
            self.db.execute(
                "INSERT INTO takeover_seen VALUES (?,?,?,?) ON CONFLICT(takeover_id) DO UPDATE SET cleared=max(cleared,excluded.cleared)",
                (
                    takeover["id"],
                    takeover["batch_id"],
                    takeover["origin"].value,
                    int(takeover["cleared"]),
                ),
            )
            if takeover["cleared"]:
                self._clear(takeover["id"])
        for batch in status["batches"]:
            record = self._one(
                FloorRecord,
                "SELECT * FROM floor_record WHERE batch=?",
                (batch["batch_id"],),
            )
            if record is None:
                continue
            for outbound in batch["outbound"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO local_outbound_seen VALUES (?,?,?)",
                    (outbound["id"], record.batch, outbound["state"].value),
                )
            for note in batch["box_notes"]:
                if (
                    note["acked_rev"] is not None
                    and note["acked_rev"] >= note["resolution_rev"]
                ):
                    delivery = self._one(
                        Delivery,
                        "SELECT * FROM delivery WHERE id=? AND batch=? AND resolution_rev=?",
                        (note["delivery_id"], record.batch, note["resolution_rev"]),
                    )
                    if delivery:
                        self._evidence(delivery.id, m.DeliveryEvidence.ACKED)
                        self.db.execute(
                            "UPDATE delivery SET state=?,updated_at=? WHERE id=?",
                            (
                                m.DeliveryState.APPLIED.value,
                                self.clock().isoformat(),
                                delivery.id,
                            ),
                        )
                        # An ack is evidence only; it doesn't promote DECIDED to DELIVERED.
            if record.state in INACTIVE:
                continue
            if batch["state"] == m.BatchState.CANCELLED:
                if self.db.execute(
                    "SELECT 1 FROM lease WHERE batch=?", (record.batch,)
                ).fetchone():
                    self.db.execute(
                        "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                        (f"cancel:{record.batch}", "1"),
                    )
                else:
                    self._cancel(record.batch)
            elif not batch["session_alive"]:
                if self.db.execute(
                    "SELECT 1 FROM lease WHERE batch=?", (record.batch,)
                ).fetchone():
                    self.db.execute(
                        "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                        (f"stale:{record.batch}", "1"),
                    )
                else:
                    self._save_record(
                        replace(
                            record, state=BoxBatchState.STALE, correction_floor=None
                        )
                    )
        if payload["more"]:
            self.db.execute(
                "INSERT OR REPLACE INTO box_meta VALUES (?,?)",
                (MetaKey.SYNC_CURSOR.value, payload["cursor"] or ""),
            )
        return Output(plans=tuple(plans), notices=tuple(notices))

    def tick(self) -> Output:
        with self.transaction():
            self._expire()
            retry_plans = self._rows(
                PlanRow,
                "SELECT * FROM plan WHERE action<>?",
                (m.PlanAction.SEND.value,),
            )
            plans: list[PlanTicket] = []
            handoffs: list[m.L4Handoff] = []
            notices: list[str] = []
            for kind, batch in self.db.execute(
                "SELECT kind,batch FROM report WHERE kind IN (?,?) AND notified_at IS NULL GROUP BY kind,batch ORDER BY min(id)",
                (BoxReport.SYNC_OVERSIZED.value, BoxReport.CARD_CAP_REACHED.value),
            ).fetchall():
                notices.append(f"{BoxReport(kind)}:{batch}")
                self.db.execute(
                    "UPDATE report SET notified_at=? WHERE kind=? AND batch IS ?",
                    (self.clock().isoformat(), kind, batch),
                )
            for key, _ in self.db.execute(
                "SELECT key,value FROM box_meta WHERE key LIKE 'ack:%' OR key LIKE 'cancel:%' OR key LIKE 'registered:%' OR key LIKE 'stale:%'"
            ).fetchall():
                kind, rest = key.split(":", 1)
                if kind == "ack":
                    batch, rev = rest.rsplit(":", 1)
                    if not self.db.execute(
                        "SELECT 1 FROM lease WHERE batch=?", (batch,)
                    ).fetchone():
                        plans.append(self._ack(batch, int(rev)))
                        self.db.execute("DELETE FROM box_meta WHERE key=?", (key,))
                elif kind in tuple(DeferredStatus):
                    if not self.db.execute(
                        "SELECT 1 FROM lease WHERE batch=?", (rest,)
                    ).fetchone():
                        self._apply_deferred_status(rest)
                else:
                    notices.append(f"watchdog_registration:{rest}")
                    self.db.execute("DELETE FROM box_meta WHERE key=?", (key,))
            for old in retry_plans:
                if (
                    old.state == PlanState.ISSUED
                    and self.clock() - old.issued_at < LEASE_TTL
                ):
                    continue
                if self.db.execute(
                    "SELECT 1 FROM box_meta WHERE key=?", (f"retried:{old.id}",)
                ).fetchone():
                    continue
                if (
                    old.state == PlanState.FINAL_RESULT
                    and old.result_json
                    and m.parse_result(json.loads(old.result_json))["ok"]
                ):
                    continue
                body_types = {
                    m.PlanAction.ACK: m.AckBody,
                    m.PlanAction.SYNC: m.SyncBody,
                    m.PlanAction.CLEAR_TAKEOVER: m.ClearBody,
                    m.PlanAction.RECONCILE: m.ReconcileBody,
                    m.PlanAction.CANCEL: m.CancelBody,
                }
                body = m.parse_record(body_types[old.action], json.loads(old.body_json))
                ticket = self._plan(old.action, body, by=old.issued_by)
                plans.append(ticket)
                self.db.execute(
                    "INSERT INTO box_meta VALUES (?,?)",
                    (f"retried:{old.id}", ticket.plan["plan_id"]),
                )
                if old.state == PlanState.ISSUED:
                    self.db.execute(
                        "UPDATE plan SET state=?,final_at=? WHERE id=?",
                        (
                            PlanState.FINAL_EXPIRED.value,
                            self.clock().isoformat(),
                            old.id,
                        ),
                    )
            for record in self._rows(FloorRecord, "SELECT * FROM floor_record"):
                if record.state == BoxBatchState.CORRECTION_DUE:
                    if (
                        record.correction_notified_at is None
                        or self.clock() - record.correction_notified_at
                        >= CORRECTION_RENOTIFY
                    ):
                        notices.append(f"correction_due:{record.batch}")
                        self._save_record(
                            replace(record, correction_notified_at=self.clock())
                        )
                elif record.state in CLOSABLE and (
                    record.state == BoxBatchState.STALE
                    or self.clock() - record.last_activity_at >= IDLE_CLOSE
                ):
                    self._close_record(record)
                elif record.state == BoxBatchState.DECIDED:
                    for delivery in self._rows(
                        Delivery,
                        "SELECT * FROM delivery WHERE batch=? AND state IN (?,?) AND paused_by IS NULL",
                        (
                            record.batch,
                            m.DeliveryState.PENDING.value,
                            m.DeliveryState.DISPATCH_FAILED.value,
                        ),
                    ):
                        try:
                            plans.append(self._deliver(delivery.id))
                        except Refused as error:
                            if error.reason not in (
                                BoxRefusal.LEASED,
                                BoxRefusal.CLOCK_SKEW_HOLD,
                                BoxRefusal.WRONG_STATE,
                                BoxRefusal.STALE_REVISION,
                                BoxRefusal.FLOOR_TOO_HIGH,
                                BoxRefusal.SUPERSEDES_MISMATCH,
                            ):
                                raise
                for delivery in self._rows(
                    Delivery,
                    "SELECT * FROM delivery WHERE batch=? AND state=?",
                    (record.batch, m.DeliveryState.DISPATCH_UNCERTAIN.value),
                ):
                    if self.clock() - delivery.updated_at >= CARD_INTENT_WAIT:
                        notices.append(f"reconcile:{delivery.id}")
            for card in self._rows(
                Card,
                "SELECT * FROM card_generation WHERE state=?",
                (CardGenState.ACTIVE.value,),
            ):
                due = sum(self.clock() - card.at >= delay for delay in REMINDERS)
                if due > card.reminded:
                    notices.append(
                        f"card_reminder:{card.task}:{card.callback_bot}:{card.gen}"
                    )
                    self.db.execute(
                        "UPDATE card_generation SET reminded=? WHERE task=? AND callback_bot=? AND gen=?",
                        (due, card.task, card.callback_bot, card.gen),
                    )
            for handoff in self._rows(
                Handoff,
                "SELECT h.* FROM handoff h WHERE h.id=(SELECT newer.id FROM handoff newer WHERE newer.task=h.task AND newer.callback_bot=h.callback_bot AND newer.purpose=h.purpose ORDER BY newer.rowid DESC LIMIT 1)",
            ):
                if self.clock() - handoff.last_sent_at < HANDOFF_WAIT:
                    continue
                pairs = tuple(
                    (r.batch, r.latest_rev)
                    for r in self._rows(
                        FloorRecord,
                        "SELECT * FROM floor_record WHERE task=? AND callback_bot=? AND state IN (?,?) ORDER BY batch",
                        (
                            handoff.task,
                            handoff.callback_bot,
                            BoxBatchState.AWAITING_L4.value
                            if handoff.purpose == CardPurpose.ASK
                            else BoxBatchState.CORRECTION_DUE.value,
                            BoxBatchState.AWAITING_L3.value
                            if handoff.purpose == CardPurpose.ASK
                            else BoxBatchState.CORRECTION_DUE.value,
                        ),
                    )
                )
                if not pairs:
                    continue
                covered = any(
                    all(list(pair) in json.loads(c.pairs_json) for pair in pairs)
                    for c in self._rows(
                        Card,
                        "SELECT * FROM card_generation WHERE task=? AND callback_bot=? AND purpose=? AND state=?",
                        (
                            handoff.task,
                            handoff.callback_bot,
                            handoff.purpose.value,
                            CardGenState.ACTIVE.value,
                        ),
                    )
                )
                if not covered:
                    if handoff.sent_count >= 3 and handoff.batch_revs_json == json_text(
                        pairs
                    ):
                        self._report(BoxReport.HANDOFF_UNACKED, None, handoff.task)
                        notices.append(BoxReport.HANDOFF_UNACKED.value)
                    else:
                        handoffs.append(
                            self._handoff_payload(
                                handoff.id, handoff.task, handoff.callback_bot, pairs
                            )
                        )
            self._retention()
            if not any(ticket.plan["action"] == m.PlanAction.SYNC for ticket in plans):
                cursor_row = self.db.execute(
                    "SELECT value FROM box_meta WHERE key=?",
                    (MetaKey.SYNC_CURSOR.value,),
                ).fetchone()
                cursor = cursor_row[0] if cursor_row else None
                self.db.execute(
                    "DELETE FROM box_meta WHERE key=?", (MetaKey.SYNC_CURSOR.value,)
                )
                tasks = [
                    r[0]
                    for r in self.db.execute(
                        "SELECT DISTINCT task FROM floor_record WHERE state NOT IN (?,?,?,?) ORDER BY task",
                        tuple(s.value for s in INACTIVE),
                    )
                ]
                plans.append(
                    self._plan(
                        m.PlanAction.SYNC,
                        m.SyncBody(
                            tasks=tasks,
                            cursor=cursor,
                            plan_ids=self._unresolved_plan_ids(),
                        ),
                    )
                )
            return Output(
                tuple(plans), handoffs=tuple(handoffs), notices=tuple(notices)
            )

    def _close_record(self, record: FloorRecord) -> None:
        now = self.clock()
        self._save_record(
            replace(
                record,
                state=BoxBatchState.TOMBSTONED,
                correction_floor=None,
                closed_at=now,
                tombstoned_at=now,
            )
        )
        self.db.execute(
            "INSERT OR REPLACE INTO tombstone VALUES (?,?,?,?,?,?)",
            (
                record.batch,
                record.task,
                record.state.value,
                record.latest_rev,
                m.digest(record),
                now.isoformat(),
            ),
        )

    @staticmethod
    def _references_batch(value: object, batch: str, depth: int = 0) -> bool:
        # Transport JSON may embed stored results as base64url. Conservatively
        # discard deeply nested caches instead of retaining a purged record.
        if depth >= 8:
            return True
        if isinstance(value, str):
            if batch in value:
                return True
            if value.startswith("ey") and re.fullmatch(r"[A-Za-z0-9_-]+", value):
                try:
                    nested = json.loads(
                        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                    )
                    return Box._references_batch(nested, batch, depth + 1)
                except (ValueError, UnicodeError):
                    return False
        elif isinstance(value, dict):
            return any(
                Box._references_batch(item, batch, depth) for item in value.values()
            )
        elif isinstance(value, list):
            return any(Box._references_batch(item, batch, depth) for item in value)
        return False

    def _retention(self) -> None:
        for record in self._rows(
            FloorRecord,
            "SELECT * FROM floor_record WHERE state=?",
            (BoxBatchState.TOMBSTONED.value,),
        ):
            if (
                record.tombstoned_at is None
                or self.clock() - record.tombstoned_at < FULL_RETENTION
            ):
                continue
            self.db.execute(
                "DELETE FROM plan WHERE delivery_id IN (SELECT id FROM delivery WHERE batch=?) OR json_extract(body_json,'$.batch_id')=?",
                (record.batch, record.batch),
            )
            for table in (
                "revision",
                "progress",
                "queued_registration",
                "lease",
                "decision",
                "delivery",
                "takeover_seen",
                "local_outbound_seen",
                "report",
                "floor_record",
            ):
                self.db.execute(f"DELETE FROM {table} WHERE batch=?", (record.batch,))
        for table in ("card_generation", "handoff"):
            pair_column = (
                "pairs_json" if table == "card_generation" else "batch_revs_json"
            )
            for row in self.db.execute(
                f"SELECT rowid,{pair_column} FROM {table}"
            ).fetchall():
                pairs = json.loads(row[1])
                if pairs and not any(
                    self.db.execute(
                        "SELECT 1 FROM floor_record WHERE batch=?", (pair[0],)
                    ).fetchone()
                    for pair in pairs
                ):
                    self.db.execute(f"DELETE FROM {table} WHERE rowid=?", (row[0],))
        self.db.execute(
            "DELETE FROM box_meta WHERE key LIKE 'retried:%' AND substr(key,9) NOT IN (SELECT id FROM plan)"
        )
        purged = [
            row[0]
            for row in self.db.execute(
                "SELECT batch FROM tombstone WHERE at<=?",
                ((self.clock() - FULL_RETENTION).isoformat(),),
            )
        ]
        for plan in self._rows(
            PlanRow, "SELECT * FROM plan WHERE action=?", (m.PlanAction.SYNC.value,)
        ):
            for batch in purged:
                if any(
                    blob and self._references_batch(json.loads(blob), batch)
                    for blob in (plan.result_json, plan.late_result_json)
                ):
                    self.db.execute("DELETE FROM plan WHERE id=?", (plan.id,))
                    break
        # SYNC caches can contain many batches (including nested stored results).
        # They grant no authority and need not outlive the 90-day full-record window.
        self.db.execute(
            "DELETE FROM plan WHERE action<>? AND final_at IS NOT NULL AND final_at<=?",
            (m.PlanAction.SEND.value, (self.clock() - FULL_RETENTION).isoformat()),
        )
        self.db.execute(
            "DELETE FROM tombstone WHERE at<=?",
            ((self.clock() - TOMBSTONE_RETENTION).isoformat(),),
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--db", type=Path, default=DEFAULT_STATE)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument(
        "--by", default=DINESH, help="normalized bot recording the action"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser(Command.REGISTER)
    for name in (Command.SHOW, Command.STATUS):
        command = commands.add_parser(name)
        command.add_argument(
            "--batch" if name == Command.SHOW else "--task",
            required=name == Command.SHOW,
        )
        command.add_argument("--json", action="store_true")
    command = commands.add_parser(Command.DECIDE)
    command.add_argument("kind", choices=tuple(DecisionKind))
    command.add_argument("--level", type=int, choices=(3, 4), required=True)
    command.add_argument("--batch", required=True)
    command.add_argument("--expected-rev", type=int, required=True)
    command.add_argument("--card-gen", type=int)
    command.add_argument("--card", dest="card_ref")
    command.add_argument("--supersedes", type=int)
    command = commands.add_parser(Command.DELIVER)
    command.add_argument("--delivery", required=True)
    command = commands.add_parser(Command.HANDOFF)
    command.add_argument("--task", required=True)
    command.add_argument("--callback-bot", required=True)
    command.add_argument(
        "--purpose", choices=tuple(CardPurpose), default=CardPurpose.ASK
    )
    command.add_argument("--resend", action="store_true")
    card = commands.add_parser(Command.CARD).add_subparsers(
        dest="card_action", required=True
    )
    for action in (CardAction.INTENT, CardAction.POSTED, CardAction.CLOSE):
        command = card.add_parser(action)
        command.add_argument("--task", required=True)
        command.add_argument("--callback-bot", required=True)
        command.add_argument("--gen", type=int, required=action != CardAction.INTENT)
        if action == CardAction.INTENT:
            command.add_argument(
                "--purpose", choices=tuple(CardPurpose), default=CardPurpose.ASK
            )
            command.add_argument("--reconciles", type=int)
        if action == CardAction.POSTED:
            command.add_argument("--ref", required=True)
    command = commands.add_parser(Command.CANCEL)
    command.add_argument("--batch", required=True)
    command.add_argument("--from-mac", action="store_true")
    command = commands.add_parser(Command.CLEAR_TAKEOVER)
    command.add_argument("--takeover", required=True)
    command = commands.add_parser(Command.RECONCILE)
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--delivery")
    target.add_argument("--outbound")
    command.add_argument("--verdict", choices=tuple(m.ReconcileVerdict), required=True)
    command = commands.add_parser(Command.RETRY)
    command.add_argument("--batch", required=True)
    command = commands.add_parser(Command.RESULT)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--line")
    source.add_argument("--none", action="store_true")
    command.add_argument("--plan")
    command.add_argument("--why", choices=tuple(m.NoResultWhy))
    command.add_argument("--detail-stdin", action="store_true")
    commands.add_parser(Command.TICK)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    args.command = Command(args.command)
    if args.command == Command.CARD:
        args.card_action = CardAction(args.card_action)
    if (
        args.command == Command.RESULT
        and args.none
        and (args.plan is None or args.why is None)
    ):
        arguments.error("result --none requires --plan and --why")
    store = None
    try:
        if not m.CALLBACK_BOT_PATTERN.fullmatch(args.by):
            raise ValueError("invalid decider")
        config, bots = load_config(args.config)
        store = Box(args.db, bots=bots, config=config)
        output: object
        match args.command:
            case Command.REGISTER:
                output = store.register(
                    json.loads(sys.stdin.read(m.REGISTER_ENTRY_MAX + 1))
                )
            case Command.SHOW:
                output = store.show(args.batch)
            case Command.STATUS:
                output = store.status(args.task)
            case Command.DECIDE:
                output = store.decide(
                    DecisionKind(args.kind),
                    args.level,
                    args.batch,
                    args.expected_rev,
                    json.loads(sys.stdin.read(m.MAX_PLAN_JSON + 1)),
                    by=args.by,
                    card_gen=args.card_gen,
                    card_ref=args.card_ref,
                    supersedes=args.supersedes,
                )
            case Command.DELIVER:
                output = store.deliver(args.delivery, by=args.by)
            case Command.HANDOFF:
                output = store.handoff(
                    args.task,
                    args.callback_bot,
                    purpose=CardPurpose(args.purpose),
                    resend=args.resend,
                    by=args.by,
                )
            case Command.CARD:
                if args.card_action == CardAction.INTENT:
                    output = store.card_intent(
                        args.task,
                        args.callback_bot,
                        purpose=CardPurpose(args.purpose),
                        gen=args.gen,
                        reconciles=args.reconciles,
                        by=args.by,
                    )
                elif args.card_action == CardAction.POSTED:
                    output = store.card_posted(
                        args.task, args.callback_bot, args.gen, args.ref, by=args.by
                    )
                else:
                    output = store.card_close(
                        args.task, args.callback_bot, args.gen, by=args.by
                    )
            case Command.CANCEL:
                output = store.cancel(args.batch, from_mac=args.from_mac, by=args.by)
            case Command.CLEAR_TAKEOVER:
                output = store.clear_takeover(args.takeover, by=args.by)
            case Command.RECONCILE:
                output = store.reconcile(
                    m.ReconcileVerdict(args.verdict),
                    delivery_id=args.delivery,
                    outbound_id=args.outbound,
                    by=args.by,
                )
            case Command.RETRY:
                output = store.retry(args.batch, by=args.by)
            case Command.RESULT:
                output = store.result(
                    args.line,
                    plan_id=args.plan,
                    why=m.NoResultWhy(args.why) if args.why else None,
                    detail=sys.stdin.read(2048) if args.detail_stdin else "",
                )
            case Command.TICK:
                output = store.tick()
            case _:
                raise ValueError("unknown command")
        if args.command in (Command.SHOW, Command.STATUS) and not args.json:
            rows = (
                [output]
                if args.command == Command.SHOW
                else cast(list[BoxShow], output)
            )
            print("BATCH\tREV\tFLOOR\tSTATE\tCORRECTION")
            for item in cast(list[BoxShow], rows):
                record = item["record"]
                print(
                    f"{record.batch}\t{record.latest_rev}\t{record.floor}\t{record.state}\t{record.correction_floor}"
                )
        else:
            print(json_text(output))
        return 0
    except Refused as error:
        print(json_text(dict(refused=error.reason, detail=error.detail)))
        return error.exit_code
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        sqlite3.Error,
        AssertionError,
    ) as error:
        print(
            json_text(
                dict(
                    refused=BoxRefusal.VALIDATION,
                    detail=redaction.redact(str(error)).text[:2048],
                )
            )
        )
        return 3
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
