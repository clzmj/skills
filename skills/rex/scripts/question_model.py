#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Typed question-escalation domain and validated host/wire boundaries.

This module is a leaf apart from the shared agent enum definitions. It carries
no delivery authority; box-issued plans remain inputs to the Mac.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from typing import (
    Any,
    Final,
    Literal,
    NewType,
    TypeVar,
    TypedDict,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)
from types import UnionType
from uuid import UUID

from agent_types import AgentKind, Event, HookEvent, InputKind

DigestSchema = Literal["qe.digest/1"]
RegisterSchema = Literal["qe.register/1"]
HandoffSchema = Literal["qe.l4handoff/1"]
StatusSchema = Literal["qe.status/1"]
BriefSchema = Literal["qe.brief/1"]
ProgressSchema = Literal["qe.progress/1"]
REGISTER_SCHEMA: Final[RegisterSchema] = "qe.register/1"
HANDOFF_SCHEMA: Final[HandoffSchema] = "qe.l4handoff/1"
STATUS_SCHEMA: Final[StatusSchema] = "qe.status/1"
BRIEF_SCHEMA: Final[BriefSchema] = "qe.brief/1"
PROGRESS_SCHEMA: Final[ProgressSchema] = "qe.progress/1"
DecisionLevel = Literal[3, 4]
CardGen = NewType("CardGen", int)

# Variable identities are not fixed-width host UUIDs. These explicit byte caps
# close the registration envelope size proof without truncating an identity.
TASK_ID_MAX: Final = 256
ACTOR_KEY_MAX: Final = 256
SESSION_ID_MAX: Final = 128
TURN_ID_MAX: Final = 128
NATIVE_CALL_ID_MAX: Final = 128
REX_ID_MAX: Final = 128
CALLBACK_BOT_MAX: Final = 32
BRIEF_ASK_CARLOS_MAX: Final = 16
BRIEF_ASK_CARLOS_ENTRY_MAX: Final = 256
SENSITIVE_TOPICS_MAX: Final = 8
SENSITIVE_TOPIC_MAX: Final = 128
MAC_HITS_MAX: Final = 32
GUARD_RULE_ID_MAX: Final = 64
CWD_MAX: Final = 1024
REMOTE_MAX: Final = 512
CALLBACK_BOT_PATTERN: Final = re.compile(rf"[a-z0-9-]{{1,{CALLBACK_BOT_MAX}}}")


class IdentityField(StrEnum):
    TASK = "task_id"
    ACTOR = "actor"
    ROOT_ACTOR = "root_actor"
    SESSION = "session"
    ROOT_SESSION = "root_session"
    TURN = "turn"
    CALL = "call"
    REX_SESSION = "rex_session"
    REX_BLOCK = "rex_block"


IDENTITY_CAPS: Final[Mapping[IdentityField, int]] = {
    IdentityField.TASK: TASK_ID_MAX,
    IdentityField.ACTOR: ACTOR_KEY_MAX,
    IdentityField.ROOT_ACTOR: ACTOR_KEY_MAX,
    IdentityField.SESSION: SESSION_ID_MAX,
    IdentityField.ROOT_SESSION: SESSION_ID_MAX,
    IdentityField.TURN: TURN_ID_MAX,
    IdentityField.CALL: NATIVE_CALL_ID_MAX,
    IdentityField.REX_SESSION: REX_ID_MAX,
    IdentityField.REX_BLOCK: REX_ID_MAX,
}
UTF8: Final = "utf-8"
UNICODE_NORMALIZATION: Final = "NFC"
COLLABORATOR_ACK_PREFIX: Final = "Acknowledge receipt"
COLLABORATOR_HEADER_PATTERN: Final = re.compile(r"\[Codex team (?P<body>[^\]]*)\]")


def parse_identity(value: object, field: str, *, allow_empty: bool = False) -> str:
    identity_field = IdentityField(field)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError(f"invalid {field}")
    if len(value.encode(UTF8)) > IDENTITY_CAPS[identity_field] or any(
        ord(c) < 32 for c in value
    ):
        raise ValueError(f"invalid {field}")
    return value


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, str):
        return unicodedata.normalize(UNICODE_NORMALIZATION, value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical object keys must be strings")
            normalized = unicodedata.normalize(UNICODE_NORMALIZATION, key)
            if normalized in result:
                raise ValueError("duplicate NFC-normalized key")
            result[normalized] = _json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    return value


def canonical(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode(UTF8)


def digest(value: object) -> TextDigest:
    return TextDigest(hashlib.sha256(canonical(value)).hexdigest())


def file_digest(raw: bytes) -> TextDigest:
    return TextDigest(hashlib.sha256(raw).hexdigest())


DIGEST_SCHEMA: Final[DigestSchema] = "qe.digest/1"  # likewise one Final per Literal
FLAG_ENV: Final = "GROKBOT_QE"
BRIEF_ENV: Final = "GROKBOT_BRIEF"
BRIEF_FENCE: Final = "toml qe-escalation"
MARKER_PREFIX: Final = "[qe:"
MARKER_ANSWER: Final = "[qe:{batch_id}:r{rev}]"
MARKER_PENDING: Final = "[qe:{batch_id}:pending]"
ASSUMPTION_PREFIX: Final = "ASSUMPTION:"
LEDGER_FILE: Final = "question-ledger.sqlite3"
AUDIT_FILE: Final = "question-audit.jsonl"
EVENTS_DIR: Final = "qe-events"
WORKER_LOCK: Final = "qe-worker-{batch}"
TICK_LOCK: Final = "qe-tick"
LOCAL_TZ: Final = "America/Mexico_City"
REGISTER_RETRY: Final = timedelta(minutes=10)
CLAIM_STALE_AFTER: Final = timedelta(seconds=90)
INLINE_WAIT: Final = timedelta(seconds=45)
MAX_CONCURRENT_ANSWERERS: Final = 2
REGISTER_MAX_TRIES: Final = 3
ACK_PARAGRAPH_MARKER: Final = "Acknowledge receipt after acting on it:"
MAX_ABANDONED_CARD_GENERATIONS: Final = 3
QUESTION_TEXT_MAX: Final = 2000
HEADER_MAX: Final = 200
OPTION_LABEL_MAX: Final = 200
OPTION_DESCRIPTION_MAX: Final = 500
REGISTER_ENTRY_MAX: Final = (
    40 * 1024
)  # bytes of canonical JSON; see §5.6.1 for deterministic truncation
RESULT_PREFIX: Final = "QE-RESULT "
MAX_PLAN_JSON: Final = 48 * 1024
MAX_PLAN_B64: Final = 64 * 1024
MAX_DELIVERY_TEXT: Final = 16 * 1024
ECHO_WAIT: Final = timedelta(seconds=10)
ROUTE_TIMEOUT: Final = timedelta(
    seconds=20
)  # Mac side of run-plan; LEASE_TTL (10 min) is box-side (§7.9)
MAX_RESULT_B64: Final = 16 * 1024  # a SYNC result pages above this (§4.3)
MAX_SYNC_SINGLE_B64: Final = (
    64 * 1024
)  # a SYNC page carrying one large item alone (§4.3, r7 IR m1)
PLAN_VALID: Final = timedelta(minutes=8)
CLOCK_SKEW_MAX: Final = timedelta(minutes=1)  # SEND fence (§4.3)
RUN_PLAN_LOCK_WAIT: Final = timedelta(seconds=60)
CLASSIFY_WAIT: Final = timedelta(minutes=2)
MAX_QUESTIONS: Final = 8
MAX_OPTIONS: Final = 8  # per batch / per question; above → BLOCKED(TOO_LARGE) (§5.6.1)
LEVEL_LABEL: Final[Mapping[int, str]] = {
    1: "a fast model (L1)",
    2: "a stronger model (L2)",
    3: "Dinesh (L3)",
    4: "Carlos (L4)",
}  # keyed by EscalationLevel value; {level_label} in §7.5

BatchId = NewType("BatchId", UUID)
QuestionId = NewType("QuestionId", UUID)
OutboundId = NewType("OutboundId", UUID)
DigestId = NewType("DigestId", UUID)
NoticeId = NewType("NoticeId", UUID)
TakeoverId = NewType("TakeoverId", UUID)
BatchRev = NewType("BatchRev", int)
ResolutionRev = NewType("ResolutionRev", int)
AttemptOrdinal = NewType("AttemptOrdinal", int)
ActorKey = NewType("ActorKey", str)
SessionId = NewType("SessionId", str)
TurnId = NewType("TurnId", str)
NativeCallId = NewType("NativeCallId", str)
TaskId = NewType("TaskId", str)
RexBlockId = NewType("RexBlockId", str)
RexSessionId = NewType("RexSessionId", str)
CallbackBot = NewType("CallbackBot", str)
RuleId = NewType("RuleId", str)
HeadSha = NewType("HeadSha", str)
ContextDigest = NewType("ContextDigest", str)
TextDigest = NewType("TextDigest", str)
GuardVersion = NewType("GuardVersion", str)
OptionIndex = NewType("OptionIndex", int)
DeciderId = NewType("DeciderId", str)  # a normalized callback name or "dinesh"
PlanId = NewType("PlanId", str)
LeaseId = NewType("LeaseId", str)
DeliveryId = NewType("DeliveryId", str)
ProgressSeq = NewType("ProgressSeq", int)
SendAttempt = NewType("SendAttempt", int)  # per delivery, box-assigned, 1..


class QuestionTool(StrEnum):
    CODEX_BLOCKING = "request_user_input"
    CODEX_ASYNC = "request_user_input_async"
    CLAUDE_ASK = "AskUserQuestion"


class CodexQuestionField(StrEnum):
    QUESTIONS = "questions"
    QUESTION = "question"
    TITLE = "title"
    HEADER = "header"
    ID = "id"
    OPTIONS = "options"
    LABEL = "label"
    DESCRIPTION = "description"


class ClaudeQuestionField(StrEnum):
    QUESTIONS = "questions"
    QUESTION = "question"
    HEADER = "header"
    OPTIONS = "options"
    MULTI_SELECT = "multiSelect"
    ANSWERS = "answers"
    LABEL = "label"
    DESCRIPTION = "description"


class ActorRole(StrEnum):
    ROOT = "root"
    NATIVE_SUBAGENT = "native_subagent"


class Urgency(StrEnum):
    BLOCKING = "blocking"
    DEGRADING = "degrading"


class Category(StrEnum):
    TRIVIAL_FACTUAL = "trivial_factual"
    REPO_CONVENTION = "repo_convention"
    DESIGN_CHOICE = "design_choice"
    IRREVERSIBLE_OR_COSTLY = "irreversible_or_costly"
    PRODUCT_SCOPE = "product_scope"


class GuardTrigger(StrEnum):
    SECURITY = "security"
    SECRET_PRESENT = "secret_present"
    MONEY = "money"
    DATA_LOSS = "data_loss"
    SCOPE = "scope"
    MERGE_OR_PUSH = "merge_or_push"
    LA_CANA = "la_cana"
    BRIEF_ASK_CARLOS = "brief_ask_carlos"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DEPLOY_OR_INFRA = "deploy_or_infra"
    POLICY_UNAVAILABLE = "policy_unavailable"  # r7: NATIVE_SUBAGENT removed (D3)


class GuardStrength(StrEnum):
    STRONG = "strong"
    WEAK = "weak"


class EscalationLevel(IntEnum):
    L0_SELF = 0
    L1_CHEAP = 1
    L2_NORMAL = 2
    L3_PRINCIPAL = 3
    L4_OWNER = 4


class AnswererKind(StrEnum):
    CODEX_EXEC = "codex_exec"
    CLAUDE_PRINT = "claude_print"


class Model(StrEnum):
    GPT_LUNA = "gpt-6-luna"
    GPT_SOL = "gpt-6.1-sol"
    HAIKU = "haiku"
    SONNET = "sonnet"  # GPT names: COORDINATION.md:27-29; Claude aliases ASSUMED (P12)


class Verdict(StrEnum):
    ANSWER = "answer"
    ESCALATE = "escalate"


class EscalationReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    CATEGORY_FLOOR = "category_floor"
    GUARD_TRIGGER = "guard_trigger"
    NO_EVIDENCE = "no_evidence"
    TIMEOUT = "timeout"
    ANSWERER_FAILED = "answerer_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ANSWER_REJECTED = "answer_rejected"
    UNBOUNDED_ANSWERER = "unbounded_answerer"
    NO_ANSWERER = "no_answerer"
    NOT_DELEGABLE = "not_delegable"


class BatchState(StrEnum):  # Mac view (§6.5)
    OPEN = "open"
    ESCALATING = "escalating"
    RESOLVED_LOCAL = "resolved_local"
    BOX_OWNED = "box_owned"
    BOX_ANSWERED = "box_answered"
    CANCELLED = "cancelled"
    STALE = "stale"
    BLOCKED = "blocked"


SEND_DELIVERABLE: Final = frozenset(
    {
        BatchState.OPEN,
        BatchState.ESCALATING,
        BatchState.BOX_OWNED,
        BatchState.RESOLVED_LOCAL,
        BatchState.BOX_ANSWERED,
    }
)  # allow-list for run-plan SEND step 4 (r7 Codex M5)


class CancelCause(StrEnum):
    HUMAN_TOOK_OVER = "human_took_over"
    ANSWERED_IN_PANE = "answered_in_pane"


class BlockCause(StrEnum):
    UNKNOWN_CALLBACK_BOT = "unknown_callback_bot"
    NO_DELIVERY_ROUTE = "no_delivery_route"
    TOO_LARGE = "too_large"


class AttemptOutcome(StrEnum):
    ANSWERED = "answered"
    ESCALATED = "escalated"
    FAILED = "failed"
    STALE_REVISION = "stale_revision"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    REFUSED_UNBOUNDED = "refused_unbounded"


class RegistrationState(StrEnum):
    DUE = "due"
    SENT = "sent"
    ACKED = "acked"  # "overdue" is a flag column, not a state (§4.2)


class OutboundPurpose(StrEnum):
    PENDING_NOTICE = "pending_notice"
    ANSWER = "answer"  # Mac-local outbounds only


class DeliveryPurpose(StrEnum):
    ANSWER = "answer"
    CORRECTION = "correction"  # box deliveries and BoxNotes


# A message for a native subagent is either kind with child_actor set; it renders the relay template (§7.5–7.6). r7 deletes the RELAY purpose.
class BoxNoteState(StrEnum):
    PREPARED = "prepared"
    SENT = "sent"
    ECHOED = "echoed"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"


class Route(StrEnum):
    INLINE_DENY_REASON = "inline_deny_reason"
    INLINE_UPDATED_INPUT = "inline_updated_input"
    CODEX_QUEUE = "codex_queue"
    CODEX_QUEUE_RESUME = "codex_queue_resume"
    REX_SEND = "rex_send"  # r7: RUN_FINISHED_FOLLOWUP deleted (no command existed)


class DeliveryState(
    StrEnum
):  # local outbounds and, with the same values, box deliveries (§4.3 result table)
    PENDING = "pending"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    DISPATCH_UNCERTAIN = "dispatch_uncertain"
    DISPATCH_FAILED = "dispatch_failed"
    OBSERVED = "observed"
    APPLIED = "applied"  # r7: RELAYED deleted
    SUPPRESSED = "suppressed"
    ABANDONED = "abandoned"


class SuppressCause(StrEnum):
    NEWER_RESOLUTION = "newer_resolution"
    HUMAN_TAKEOVER = "human_takeover"
    CONTEXT_CHANGED = "context_changed"
    BOX_OWNED = "box_owned"
    CANCELLED = "cancelled"
    NEWER_REVISION = "newer_revision"
    FLOOR_RAISED = "floor_raised"


class ReconcileVerdict(StrEnum):
    SEEN_IN_PANE = "seen_in_pane"
    NOT_DELIVERED = "not_delivered"


class DeliveryEvidence(
    StrEnum
):  # box delivery rows only; write-once with delivered_at (§4.2, r11)
    SENT_ECHOED = "sent_echoed"
    SENT_NO_ECHO = "sent_no_echo"
    SEEN_IN_PANE = "seen_in_pane"
    ACKED = "acked"


class LifecycleKind(StrEnum):
    PROMPT_SUBMITTED = "prompt_submitted"
    NATIVE_ANSWER = "native_answer"
    NATIVE_CALL_FAILED = "native_call_failed"
    TURN_ENDED = "turn_ended"
    SESSION_ENDED = "session_ended"


class PromptOrigin(StrEnum):
    BROKER = "broker"
    BOX = "box"
    COLLABORATOR = "collaborator"
    HOST_QUEUED = "host_queued"
    UNVERIFIED_MARKER = "unverified_marker"
    UNKNOWN = "unknown"
    HUMAN = "human"


class NoticeKind(StrEnum):
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DISPATCH_UNCERTAIN = "dispatch_uncertain"
    BRIEF_INVALID = "brief_invalid"
    L0_VIOLATION = "l0_violation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LATE_DELIVERY = "late_delivery"
    TAKEOVER_SUSPECTED = "takeover_suspected"
    REGISTRATION_OVERDUE = "registration_overdue"
    UNVERIFIED_MARKER = "unverified_marker"
    TOO_LARGE = "too_large"


class ValidationFailure(StrEnum):  # §5.4; shared by the Mac (L1/L2) and qe-box (L3/L4)
    MISSING_QUESTION = "missing_question"
    UNKNOWN_QUESTION = "unknown_question"
    OPTION_NOT_OFFERED = "option_not_offered"
    FREE_TEXT_NOT_ALLOWED = "free_text_not_allowed"
    ANSWER_GUARD_HIT = "answer_guard_hit"
    SECRET_IN_ANSWER = "secret_in_answer"
    NO_EVIDENCE = "no_evidence"
    WEAK_CLEARANCE_MISSING = "weak_clearance_missing"


class UntrustedSource(StrEnum):
    AGENT = "agent"
    MODEL = "model"
    BRIEF = "brief"
    HOST = "host"


class BriefProject(StrEnum):
    PERSONAL = "personal"
    LA_CANA = "la_cana"
    ACTUAL_ENGINEERING = "actual_engineering"


class BudgetScope(StrEnum):
    TASK = "task"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class Untrusted:
    """Origin-labelled text; callers redact before wrapping it (§3, §8.4)."""

    text: str
    source: UntrustedSource


@dataclass(frozen=True, slots=True)
class HookInput:
    agent: AgentKind
    event: HookEvent
    session: SessionId
    child: str
    turn: TurnId | None
    tool: str | None
    tool_use_id: str | None
    tool_input: Mapping[str, object]
    prompt: str | None
    cwd: str


@dataclass(frozen=True, slots=True)
class DetectedAsk:
    input: HookInput
    tool: QuestionTool
    call: NativeCallId


@dataclass(frozen=True, slots=True)
class Confidence:
    value: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not math.isfinite(self.value)
            or not 0 <= self.value <= 1
        ):
            raise ValueError("confidence must be finite and in [0, 1]")


@dataclass(frozen=True, slots=True)
class Option:
    index: OptionIndex
    label: Untrusted
    description: Untrusted | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or self.index < 0:
            raise ValueError("option index must be nonnegative")


@dataclass(frozen=True, slots=True)
class NativeQuestionKey:
    codex_id: str | None = None
    claude_text: str | None = None

    def __post_init__(self) -> None:
        if (self.codex_id is None) == (self.claude_text is None):
            raise ValueError("exactly one native question key is required")


@dataclass(frozen=True, slots=True)
class Question:
    id: QuestionId
    origin_call: NativeCallId
    text: Untrusted
    header: Untrusted | None
    options: tuple[Option, ...]
    multi_select: bool
    allows_free_text: bool
    native_key: NativeQuestionKey
    retired_rev: BatchRev | None
    digest: TextDigest = TextDigest("")

    def __post_init__(self) -> None:
        if len({o.index for o in self.options}) != len(self.options):
            raise ValueError("duplicate option index")
        if self.retired_rev is not None and self.retired_rev < 1:
            raise ValueError("retirement revision must be positive")
        if not self.digest:
            object.__setattr__(self, "digest", question_digest(self))
        elif self.digest != question_digest(self):
            raise ValueError("question digest mismatch")


@dataclass(frozen=True, slots=True)
class AskerRef:
    agent: AgentKind
    actor: ActorKey
    root_actor: ActorKey
    role: ActorRole
    session: SessionId
    turn: TurnId
    task: TaskId
    rex_session: RexSessionId
    rex_block: RexBlockId
    cwd: str
    head_sha: HeadSha
    brief_digest: TextDigest | None
    callback_bot: CallbackBot

    def __post_init__(self) -> None:
        for key, value in (
            ("actor", self.actor),
            ("root_actor", self.root_actor),
            ("session", self.session),
            ("turn", self.turn),
            ("task_id", self.task),
            ("rex_session", self.rex_session),
            ("rex_block", self.rex_block),
        ):
            parse_identity(value, key, allow_empty=key.startswith("rex_"))
        if not CALLBACK_BOT_PATTERN.fullmatch(self.callback_bot):
            raise ValueError("invalid callback bot")


@dataclass(frozen=True, slots=True)
class NativeCall:
    tool: QuestionTool
    call: NativeCallId
    actor: ActorKey
    turn: TurnId
    seen_at: datetime
    failed: bool
    fallback_of: NativeCallId | None

    def __post_init__(self) -> None:
        parse_identity(self.call, "call")
        if self.call == self.fallback_of:
            raise ValueError("a call cannot fall back to itself")


@dataclass(frozen=True, slots=True)
class GuardHit:
    trigger: GuardTrigger
    strength: GuardStrength
    rule_id: RuleId
    question: QuestionId | None


@dataclass(frozen=True, slots=True)
class Classification:
    category: Category
    delegable: bool
    sensitive_topics: tuple[str, ...]
    hits: tuple[GuardHit, ...]
    floor: EscalationLevel
    classified: bool = False


@dataclass(frozen=True, slots=True)
class QuestionBatch:
    id: BatchId
    rev: BatchRev
    asker: AskerRef
    questions: tuple[Question, ...]
    calls: tuple[NativeCall, ...]
    urgency: Urgency
    state: BatchState
    classification: Classification
    context: ContextDigest
    level: EscalationLevel
    opened_at: datetime

    def __post_init__(self) -> None:
        if self.rev < 1:
            raise ValueError("batch revision must be positive")
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question id")


class AttachOutcome(StrEnum):
    OPENED = "opened"
    ATTACHED = "attached"
    REVISED = "revised"
    DUPLICATE_CALL = "duplicate_call"


@dataclass(frozen=True, slots=True)
class AttachResult:
    outcome: AttachOutcome
    batch: QuestionBatch


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    path: str
    line: int | None
    quote: Untrusted

    def __post_init__(self) -> None:
        if self.line is not None and (isinstance(self.line, bool) or self.line < 1):
            raise ValueError("evidence line must be positive")


@dataclass(frozen=True, slots=True)
class Answer:
    question: QuestionId
    choice: tuple[OptionIndex, ...]
    free_text: Untrusted | None
    rationale: Untrusted
    evidence: tuple[EvidenceRef, ...]
    confidence: Confidence | None

    def __post_init__(self) -> None:
        if len(set(self.choice)) != len(self.choice) or any(i < 0 for i in self.choice):
            raise ValueError("invalid answer choices")


@dataclass(frozen=True, slots=True)
class AnswererRef:
    kind: AnswererKind
    model: Model
    level: EscalationLevel


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    usd: Decimal

    def __post_init__(self) -> None:
        if (
            self.input_tokens < 0
            or self.output_tokens < 0
            or not self.usd.is_finite()
            or self.usd < 0
        ):
            raise ValueError("usage must be nonnegative and finite")


@dataclass(frozen=True, slots=True)
class InvocationBounds:
    max_prompt_bytes: int
    max_turns: int | None
    max_output_tokens: int | None
    wall_clock: timedelta
    reserve_tokens: int
    reserve_usd: Decimal

    def __post_init__(self) -> None:
        if (
            self.max_prompt_bytes < 1
            or self.wall_clock <= timedelta(0)
            or self.reserve_tokens < 0
            or not self.reserve_usd.is_finite()
            or self.reserve_usd < 0
            or any(
                v is not None and v < 1
                for v in (self.max_turns, self.max_output_tokens)
            )
        ):
            raise ValueError("invalid invocation bounds")


@dataclass(frozen=True, slots=True)
class Attempt:
    batch: BatchId
    batch_rev: BatchRev
    ordinal: AttemptOrdinal
    context: ContextDigest
    answerer: AnswererRef
    verdict: Verdict
    outcome: AttemptOutcome
    answers: tuple[Answer, ...]
    classification: Classification
    reason: EscalationReason | None
    started_at: datetime
    latency: timedelta
    usage: Usage | None


@dataclass(frozen=True, slots=True)
class Resolution:
    batch: BatchId
    rev: ResolutionRev
    batch_rev: BatchRev
    answers: tuple[Answer, ...]
    by: AnswererRef
    resolved_at: datetime

    def __post_init__(self) -> None:
        if (
            self.rev < 1
            or self.batch_rev < 1
            or self.by.level
            not in (EscalationLevel.L1_CHEAP, EscalationLevel.L2_NORMAL)
        ):
            raise ValueError("local resolutions require positive revisions and L1/L2")


@dataclass(frozen=True, slots=True)
class Registration:
    batch: BatchId
    batch_rev: BatchRev
    content_digest: str
    state: RegistrationState
    tries: int
    last_sent_at: datetime | None
    overdue: bool
    progress_seq: ProgressSeq
    acked_progress_seq: ProgressSeq
    acked_floor: EscalationLevel | None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    id: OutboundId
    batch: BatchId
    purpose: OutboundPurpose
    resolution_rev: ResolutionRev
    target_actor: ActorKey
    child_actor: ActorKey | None
    text: str
    body_digest: TextDigest
    ack_token: str
    route_plan: tuple[Route, ...]
    state: DeliveryState
    suppress_cause: SuppressCause | None
    claim_owner: str | None
    claimed_at: datetime | None
    paused_by: TakeoverId | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchAttempt:
    outbound: OutboundId
    n: int
    route: Route
    result: DeliveryState | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class Takeover:
    id: TakeoverId
    batch: BatchId
    origin: PromptOrigin
    prompt_digest: TextDigest | None
    at: datetime
    cleared_at: datetime | None
    cleared_by: DeciderId | None


@dataclass(frozen=True, slots=True)
class BoxNote:
    delivery_id: DeliveryId
    attempt: SendAttempt
    plan_id: PlanId
    batch: BatchId
    batch_rev: BatchRev
    resolution_rev: ResolutionRev
    purpose: DeliveryPurpose
    target_actor: ActorKey
    child_actor: ActorKey | None
    text: str
    body_digest: TextDigest
    ack_token_digest: TextDigest
    floor: EscalationLevel
    state: BoxNoteState
    acked_rev: ResolutionRev | None
    at: datetime


@dataclass(frozen=True, slots=True)
class WeakClearance:
    rule_id: RuleId
    trigger: GuardTrigger
    reason: Untrusted


@dataclass(frozen=True, slots=True)
class Lifecycle:
    actor: ActorKey
    kind: LifecycleKind
    call: NativeCallId | None
    origin: PromptOrigin | None
    prompt_digest: TextDigest | None
    at: datetime


@dataclass(frozen=True, slots=True)
class Notice:
    id: NoticeId
    kind: NoticeKind
    task: TaskId
    batch: BatchId | None
    detail: Untrusted
    created_at: datetime
    spooled: bool


@dataclass(frozen=True, slots=True)
class Cancellation:
    batch: BatchId
    batch_rev: BatchRev
    cause: CancelCause
    at: datetime
    prompt_digest: TextDigest | None


@dataclass(frozen=True, slots=True)
class GuardRule:
    id: RuleId
    trigger: GuardTrigger
    strength: GuardStrength
    pattern: re.Pattern[str]
    examples_hit: tuple[str, ...]
    examples_miss: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BriefEscalation:
    project: BriefProject
    l0_assume: tuple[str, ...]
    ask_carlos: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.ask_carlos) > BRIEF_ASK_CARLOS_MAX or any(
            len(t.encode(UTF8)) > BRIEF_ASK_CARLOS_ENTRY_MAX for t in self.ask_carlos
        ):
            raise ValueError("brief policy exceeds byte cap")


@dataclass(frozen=True, slots=True)
class BudgetKey:
    scope: BudgetScope
    key: str


def question_digest(question: Question) -> TextDigest:
    return digest(
        {
            "id": str(question.id),
            "text": question.text.text,
            "header": question.header.text if question.header else None,
            "options": [
                {
                    "index": o.index,
                    "label": o.label.text,
                    "description": o.description.text if o.description else None,
                }
                for o in question.options
            ],
            "multi_select": question.multi_select,
            "allows_free_text": question.allows_free_text,
        }
    )


def context_digest(
    batch_id: BatchId,
    rev: BatchRev,
    questions: tuple[Question, ...],
    head_sha: HeadSha,
    brief_digest: TextDigest | None,
) -> ContextDigest:
    return ContextDigest(
        digest(
            [
                str(batch_id),
                rev,
                [
                    {
                        "id": str(q.id),
                        "digest": q.digest,
                        "header": q.header.text if q.header else None,
                        "options": [
                            {
                                "index": o.index,
                                "label": o.label.text,
                                "description": o.description.text
                                if o.description
                                else None,
                            }
                            for o in q.options
                        ],
                        "multi_select": q.multi_select,
                        "allows_free_text": q.allows_free_text,
                        "retired_rev": q.retired_rev,
                    }
                    for q in questions
                ],
                head_sha,
                brief_digest,
            ]
        )
    )


class DigestOption(TypedDict):
    index: int
    label: str
    description: str | None


class DigestHit(TypedDict):
    rule_id: str
    trigger: GuardTrigger
    strength: GuardStrength


class RegisterQuestion(TypedDict):
    id: str
    text: str
    header: str | None
    options: list[DigestOption]
    multi_select: bool
    allows_free_text: bool
    retired_rev: int | None
    truncated: bool
    digest: str  # box recomputes digest; truncated text forces box floor ≥ L3


class RegisterEntry(TypedDict):  # qe.register/1, one per batch revision (§4.2)
    schema: RegisterSchema
    batch_id: str
    batch_rev: int
    task_id: str
    actor: str
    agent: AgentKind
    role: ActorRole
    session: str
    rex_session: str
    rex_block: str
    cwd: str
    remote: str
    head_sha: str
    callback_bot: str
    root_actor: str
    root_session: str  # = actor/session unless role is a native child; from the lineage reader (grokbot_callback.lineage :430, root_session_id) (§7.6)
    questions: list[RegisterQuestion]
    secret_present: bool
    mac_floor: EscalationLevel
    mac_hits: list[DigestHit]
    brief_digest: str | None
    brief_ask_carlos: list[str]
    brief_project: BriefProject | None
    classified: bool  # False when CLASSIFY_WAIT ran out or no classifier is installed yet (PR-3/PR-4); box floor then ≥ L3 (§4.2)
    category: Category | None
    delegable: bool | None
    sensitive_topics: list[str]
    classification_floor: EscalationLevel | None
    content_digest: (
        str  # canonical digest of every field above it; immutable per (batch, rev)
    )


class FloorRaiseCause(StrEnum):
    LATE_CLASSIFICATION = "late_classification"
    MAC_GUARD = "mac_guard"


class LateClassification(TypedDict):
    category: Category
    delegable: bool
    sensitive_topics: list[str]
    classification_floor: EscalationLevel


class FloorRaise(
    TypedDict
):  # cumulative and monotone: the highest floor the Mac learned for this revision so far; the box applies max(stored, floor) even for a stale seq; never lowers (§4.2, r9)
    floor: EscalationLevel
    cause: FloorRaiseCause
    classification: LateClassification | None  # set iff cause is LATE_CLASSIFICATION


class RegisterProgress(
    TypedDict
):  # qe.progress/1; later facts about one revision, never its questions (§4.2)
    schema: ProgressSchema
    batch_id: str
    batch_rev: int
    seq: int
    escalated: EscalationReason | None  # sticky once set
    recommendations: list[
        DigestRecommendation
    ]  # L2's advice for escalated batches (never for quarantined ones)
    local_resolution: int | None
    local_level: EscalationLevel | None
    floor_raise: (
        FloorRaise | None
    )  # r7 Codex M3; once any raise is learned, set on EVERY later snapshot of this revision to the cumulative max (r9); included in the progress de-dup content_digest


class DigestRecommendation(TypedDict):
    level: EscalationLevel
    model: Model
    verdict: Verdict
    choices: list[list[int]]
    rationale: str
    confidence: float | None


PlanSchema = Literal["qe.plan/1"]
ResultSchema = Literal["qe.result/1"]


class PlanAction(StrEnum):
    SEND = "send"
    ACK = "ack"
    SYNC = "sync"
    CLEAR_TAKEOVER = "clear_takeover"
    RECONCILE = "reconcile"
    CANCEL = "cancel"


class ReconcileTargetKind(StrEnum):
    BOX_DELIVERY = "box_delivery"
    LOCAL_OUTBOUND = "local_outbound"


class NoResultWhy(StrEnum):
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    NO_RESULT_LINE = "no_result_line"
    RESULT_UNREADABLE = "result_unreadable"


class RunPlanError(StrEnum):
    BAD_PLAN = "bad_plan"
    DIGEST_MISMATCH = "digest_mismatch"


class PaneCheck(StrEnum):
    MATCHED = "matched"
    SKIPPED_NO_MARKER = "skipped_no_marker"


class SendBody(TypedDict):
    delivery_id: str
    attempt: int
    resend_after_reconcile: bool  # fence (below)
    issued_at: str
    valid_s: int  # box time, ISO-8601 UTC; PLAN_VALID
    batch_id: str
    batch_rev: int
    floor: EscalationLevel
    resolution_rev: int
    purpose: DeliveryPurpose
    target_actor: str
    child_actor: str | None
    rex_session: str
    rex_block: str
    session_marker: str | None
    allowed_routes: list[Route]
    codex_thread: str | None
    text: str
    ack_token: str
    echo_wait_s: int


class AckBody(TypedDict):
    batch_id: str
    batch_rev: int
    content_digest: str
    progress_seq: int
    floor: EscalationLevel
    owned: bool


class SyncBody(TypedDict):
    tasks: list[str]
    cursor: str | None
    plan_ids: list[str]  # plan_ids: report stored results (plan_seen)


class ClearBody(TypedDict):
    takeover_id: str
    by: DeciderId


class ReconcileBody(TypedDict):
    target: ReconcileTargetKind
    target_id: str
    verdict: ReconcileVerdict


class CancelBody(TypedDict):
    batch_id: str
    by: DeciderId


class Plan(TypedDict):
    schema: PlanSchema
    plan_id: str
    action: PlanAction
    issued_by: DeciderId
    lease_id: str | None  # SEND only; equals the box lease
    body: (
        SendBody | AckBody | SyncBody | ClearBody | ReconcileBody | CancelBody
    )  # discriminated by action
    plan_digest: str


class SendOutcome(StrEnum):
    SENT_ECHOED = (
        "sent_echoed"  # route accepted and the text appeared within echo_wait_s
    )
    SENT_NO_ECHO = "sent_no_echo"  # route accepted, no echo seen
    SEND_UNKNOWN = (
        "send_unknown"  # anything after a route may have accepted text (table below)
    )
    NOT_SENT_ROUTE_REFUSED = "not_sent_route_refused"  # every allowed route failed with a recorded pre-acceptance error
    NOT_SENT_STALE_REVISION = "not_sent_stale_revision"  # Mac rev > plan rev, or the live check bumped it (§6.3)
    NOT_SENT_PAUSED = (
        "not_sent_paused"  # uncleared UNKNOWN takeover; carries takeover_id
    )
    NOT_SENT_CANCELLED = (
        "not_sent_cancelled"  # HUMAN takeover, pane answer, Mac batch CANCELLED
    )
    NOT_SENT_SESSION_GONE = "not_sent_session_gone"  # session ended, or Mac batch STALE
    NOT_SENT_STALE_PANE = "not_sent_stale_pane"  # capture didn't show session_marker
    NOT_SENT_EXPIRED = "not_sent_expired"  # fence: Mac clock past issued_at + valid_s (checked before every route call)
    NOT_SENT_SUPERSEDED = (
        "not_sent_superseded"  # fence: a higher attempt for this delivery already ran
    )
    NOT_SENT_ALREADY_SENT = "not_sent_already_sent"  # fence: this delivery's BoxNote is SENT/ECHOED/UNKNOWN and no reconcile allowed a resend
    NOT_SENT_BUSY = (
        "not_sent_busy"  # WORKER_LOCK not acquired within RUN_PLAN_LOCK_WAIT
    )
    NOT_SENT_NOT_DELIVERABLE = "not_sent_not_deliverable"  # Mac batch state not in SEND_DELIVERABLE (e.g. BLOCKED)


class PlanSeenEntry(TypedDict):
    plan_id: str
    result_b64: str | None  # None: the run started and stored no result


class SyncPayload(TypedDict):
    registrations: list[RegisterEntry]
    progress: list[RegisterProgress]
    status: Status
    plan_results: list[PlanSeenEntry]
    more: bool
    cursor: str | None
    oversized: list[
        str
    ]  # batch ids whose item couldn't be encoded even alone; never silently dropped


class Result(TypedDict):
    schema: ResultSchema
    plan_id: str
    plan_digest: str
    lease_id: str | None
    action: PlanAction
    ok: bool  # the action ran to completion; False only with error_kind
    error_kind: RunPlanError | None
    send: SendOutcome | None  # SEND: set iff ok
    route_used: Route | None
    pane_check: PaneCheck | None
    takeover_id: str | None
    local_answer_dispatched: (
        bool  # SEND: a local L1/L2 answer for this batch was already dispatched
    )
    replayed: bool  # the stored result of an earlier run of this plan_id
    mac_batch_rev: int | None
    mac_state: BatchState | None
    mac_now: str  # mac_now: skew monitoring only
    payload: SyncPayload | None  # SYNC only
    result_digest: str


class HandoffQuestion(TypedDict):
    id: str
    text: str
    header: str | None
    options: list[DigestOption]
    multi_select: bool
    allows_free_text: bool


class HandoffBatch(TypedDict):
    batch_id: str
    batch_rev: int
    floor: EscalationLevel
    hits: list[DigestHit]
    questions: list[HandoffQuestion]
    recommendations: list[DigestRecommendation]


class L4Handoff(TypedDict):
    schema: HandoffSchema
    handoff_id: str
    task_id: str
    callback_bot: str
    batches: list[HandoffBatch]


class StatusOutbound(TypedDict):
    id: str
    purpose: OutboundPurpose
    resolution_rev: int
    state: DeliveryState
    route: Route | None
    acked_rev: int | None


class StatusBoxNote(TypedDict):
    delivery_id: str
    attempt: int
    plan_id: str
    resolution_rev: int
    purpose: DeliveryPurpose
    state: BoxNoteState
    acked_rev: int | None


class StatusTakeover(TypedDict):
    id: str
    batch_id: str
    origin: PromptOrigin
    cleared: bool  # no timestamps: the box never compares them


class StatusBatch(TypedDict):
    batch_id: str
    batch_rev: int
    state: BatchState
    mac_floor: EscalationLevel
    acked_floor: EscalationLevel | None
    context: str
    callback_bot: str
    session_alive: bool
    answered_in_pane: bool
    local_resolution: int | None
    outbound: list[StatusOutbound]
    box_notes: list[StatusBoxNote]


class Status(TypedDict):
    schema: StatusSchema
    task_id: str
    batches: list[StatusBatch]
    takeovers: list[StatusTakeover]
    notices: list[DigestNotice]


class DigestMetadata(TypedDict):
    requester: str
    repo: str
    branch: str


class DigestNotice(TypedDict):
    id: str
    kind: NoticeKind
    batch_id: str | None
    detail: str
    created_at: str


class DigestUntrustedField(StrEnum):
    LAST_MESSAGE = "last_message"
    PROMPT_TEXT = "prompt_text"
    CWD = "cwd"
    REPO = "repo"
    BRANCH = "branch"
    HEAD_SHA = "head_sha"
    TASK_ID = "task_id"
    REGISTER = "register"
    PROGRESS = "progress"
    NOTICES = "notices"


# Appendix C clarifies: the callback's existing fields plus question fields.
DIGEST_UNTRUSTED_FIELDS: Final[tuple[DigestUntrustedField, ...]] = tuple(
    DigestUntrustedField
)


class DigestPayload(TypedDict):
    schema: DigestSchema
    event: Event
    kind: InputKind
    digest_id: str
    task_id: str
    requester: str
    repo: str
    branch: str
    register: list[RegisterEntry]
    progress: list[RegisterProgress]
    notices: list[DigestNotice]
    untrusted: list[DigestUntrustedField]
    delivery_key: str


class DigestEnvelope(TypedDict):
    event: Event
    kind: InputKind
    digest_id: str
    task_id: str
    requester: str
    agent: AgentKind | Literal[""]  # §5.6.1 digest-only envelopes carry "".
    turn: str
    rex_session: str
    rex_block: str
    finished_at: str
    body: DigestPayload


PLAN_SCHEMA: Final[PlanSchema] = "qe.plan/1"
RESULT_SCHEMA: Final[ResultSchema] = "qe.result/1"
T = TypeVar("T")


def _decode(annotation: Any, value: object) -> Any:
    """Validate once at the JSON boundary, retaining declared record types."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any or annotation is object:
        return value
    if origin in (Union, UnionType):
        for option in args:
            try:
                return _decode(option, value)
            except (ValueError, TypeError, KeyError):
                pass
        raise ValueError("value does not match union")
    if origin is Literal:
        if not any(type(value) is type(v) and value == v for v in args):
            raise ValueError("unknown protocol literal")
        return value
    if hasattr(annotation, "__supertype__"):
        decoded = _decode(annotation.__supertype__, value)
        names = {
            "ActorKey": "actor",
            "SessionId": "session",
            "TurnId": "turn",
            "NativeCallId": "call",
            "TaskId": "task_id",
            "RexBlockId": "rex_block",
            "RexSessionId": "rex_session",
        }
        if annotation.__name__ in names:
            parse_identity(
                decoded,
                names[annotation.__name__],
                allow_empty=annotation.__name__.startswith("Rex"),
            )
        if annotation.__supertype__ is int and (
            isinstance(decoded, bool)
            or decoded
            < (
                0
                if annotation.__name__
                in ("OptionIndex", "ProgressSeq", "ResolutionRev")
                else 1
            )
        ):
            raise ValueError("invalid ordinal or revision")
        return annotation(decoded)
    if annotation is type(None):
        if value is not None:
            raise ValueError("expected null")
        return None
    if annotation is UUID:
        if not isinstance(value, (str, UUID)):
            raise ValueError("expected UUID")
        return UUID(str(value))
    if annotation is datetime:
        if not isinstance(value, str):
            raise ValueError("expected datetime string")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("datetime must have timezone")
        return parsed
    if annotation is timedelta:
        if type(value) not in (int, float):
            raise ValueError("expected duration seconds")
        return timedelta(seconds=cast(float, value))
    if annotation is Decimal:
        if not isinstance(value, str):
            raise ValueError("expected decimal string")
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise ValueError("expected sequence")
        parsed = [_decode(args[0], item) for item in value]
        return tuple(parsed) if origin is tuple else parsed
    if origin is Mapping:
        if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
            raise ValueError("expected string-key mapping")
        return value
    if isinstance(annotation, type) and issubclass(annotation, (StrEnum, IntEnum)):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("invalid enum")
        return annotation(value)
    if isinstance(annotation, type) and (
        is_dataclass(annotation) or is_typeddict(annotation)
    ):
        if not isinstance(value, dict):
            raise ValueError("expected object")
        hints = get_type_hints(annotation)
        if set(value) != set(hints):
            raise ValueError("missing or unknown record fields")
        parsed = {key: _decode(hint, value[key]) for key, hint in hints.items()}
        return annotation(**parsed) if is_dataclass(annotation) else parsed
    if annotation in (str, bool, int, float):
        if type(value) is not annotation and not (
            annotation is float and type(value) is int
        ):
            raise ValueError(f"expected {annotation.__name__}")
        if annotation is float and not math.isfinite(cast(float, value)):
            raise ValueError("nonfinite number")
        return value
    raise ValueError("unsupported boundary type")


def parse_record(record_type: type[T], value: object) -> T:
    return cast(T, _decode(record_type, value))


def parse_question(value: object) -> Question:
    return parse_record(Question, value)


def parse_asker(value: object) -> AskerRef:
    return parse_record(AskerRef, value)


def parse_batch(value: object) -> QuestionBatch:
    return parse_record(QuestionBatch, value)


def parse_outbound(value: object) -> OutboundMessage:
    return parse_record(OutboundMessage, value)


def parse_box_note(value: object) -> BoxNote:
    return parse_record(BoxNote, value)


def parse_hook_input(value: object, agent: AgentKind | None = None) -> HookInput:
    """Accept a typed snapshot or the host's hook JSON, without coercion."""
    if agent is None:
        return parse_record(HookInput, value)
    if not isinstance(value, dict):
        raise ValueError("expected hook object")
    return parse_record(
        HookInput,
        {
            "agent": agent,
            "event": value.get("hook_event_name"),
            "session": value.get("session_id"),
            "child": value.get("agent_id", ""),
            "turn": value.get("turn_id"),
            "tool": value.get("tool_name"),
            "tool_use_id": value.get("tool_use_id"),
            "tool_input": value.get("tool_input", {}),
            "prompt": value.get("prompt"),
            "cwd": value.get("cwd", ""),
        },
    )


def _verify_digest(value: Mapping[str, object], field: str) -> None:
    if value[field] != digest(
        {key: item for key, item in value.items() if key != field}
    ):
        raise ValueError(f"{field} mismatch")


def parse_plan(value: object) -> Plan:
    plan = cast(Plan, _decode(Plan, value))
    if len(canonical(plan)) > MAX_PLAN_JSON:
        raise ValueError("plan too large")
    _verify_digest(plan, "plan_digest")
    body_types = {
        PlanAction.SEND: SendBody,
        PlanAction.ACK: AckBody,
        PlanAction.SYNC: SyncBody,
        PlanAction.CLEAR_TAKEOVER: ClearBody,
        PlanAction.RECONCILE: ReconcileBody,
        PlanAction.CANCEL: CancelBody,
    }
    plan["body"] = _decode(body_types[plan["action"]], plan["body"])
    if (plan["lease_id"] is not None) != (plan["action"] is PlanAction.SEND):
        raise ValueError("SEND requires exactly one lease")
    UUID(plan["plan_id"])
    if plan["action"] is PlanAction.SEND:
        body = cast(SendBody, plan["body"])
        if (
            body["attempt"] < 1
            or body["batch_rev"] < 1
            or body["resolution_rev"] < 1
            or not 0 < body["valid_s"] <= PLAN_VALID.total_seconds()
            or not 0 <= body["echo_wait_s"] <= ECHO_WAIT.total_seconds()
            or len(body["text"].encode(UTF8)) > MAX_DELIVERY_TEXT
            or not body["allowed_routes"]
        ):
            raise ValueError("invalid send bounds")
        _decode(datetime, body["issued_at"])
    return plan


def parse_result(value: object) -> Result:
    result = cast(Result, _decode(Result, value))
    _verify_digest(result, "result_digest")
    if (
        result["ok"] != (result["error_kind"] is None)
        or (result["send"] is not None)
        != (result["action"] is PlanAction.SEND and result["ok"])
        or (result["payload"] is not None)
        != (result["action"] is PlanAction.SYNC and result["ok"])
        or (result["takeover_id"] is not None)
        != (result["send"] is SendOutcome.NOT_SENT_PAUSED)
    ):
        raise ValueError("inconsistent result")
    UUID(result["plan_id"])
    _decode(datetime, result["mac_now"])
    return result


def parse_register_entry(value: object) -> RegisterEntry:
    entry = cast(RegisterEntry, _decode(RegisterEntry, value))
    _verify_digest(entry, "content_digest")
    identity_values = cast(Mapping[str, object], entry)
    for key in IDENTITY_CAPS:
        if key in entry:
            parse_identity(
                identity_values[key], key, allow_empty=key.startswith("rex_")
            )
    if (
        entry["batch_rev"] < 1
        or len(entry["questions"]) > MAX_QUESTIONS
        or len(entry["mac_hits"]) > MAC_HITS_MAX
        or len(canonical(entry)) > REGISTER_ENTRY_MAX
    ):
        raise ValueError("registration exceeds bounds")
    if len(entry["brief_ask_carlos"]) > BRIEF_ASK_CARLOS_MAX or any(
        len(t.encode(UTF8)) > BRIEF_ASK_CARLOS_ENTRY_MAX
        for t in entry["brief_ask_carlos"]
    ):
        raise ValueError("brief policy exceeds bounds")
    if len(entry["sensitive_topics"]) > SENSITIVE_TOPICS_MAX or any(
        len(t.encode(UTF8)) > SENSITIVE_TOPIC_MAX for t in entry["sensitive_topics"]
    ):
        raise ValueError("classification exceeds bounds")
    UUID(entry["batch_id"])
    if not CALLBACK_BOT_PATTERN.fullmatch(entry["callback_bot"]):
        raise ValueError("invalid callback bot")
    if (
        len(entry["cwd"].encode(UTF8)) > CWD_MAX
        or len(entry["remote"].encode(UTF8)) > REMOTE_MAX
    ):
        raise ValueError("repository context exceeds bounds")
    if entry["head_sha"] and not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", entry["head_sha"]
    ):
        raise ValueError("invalid HEAD digest")
    for hit in entry["mac_hits"]:
        if not re.fullmatch(rf"[\x20-\x7e]{{1,{GUARD_RULE_ID_MAX}}}", hit["rule_id"]):
            raise ValueError("invalid guard rule id")
    for question in entry["questions"]:
        if len(question["options"]) > MAX_OPTIONS:
            raise ValueError("too many options")
        UUID(question["id"])
        text_fields = [
            (question["text"], QUESTION_TEXT_MAX),
            (question["header"], HEADER_MAX),
        ]
        for option in question["options"]:
            text_fields.extend(
                (
                    (option["label"], OPTION_LABEL_MAX),
                    (option["description"], OPTION_DESCRIPTION_MAX),
                )
            )
        if any(
            text is not None
            and (
                len(text.encode()) > cap
                or any(ord(c) < 32 and c not in "\n\t" for c in text)
            )
            for text, cap in text_fields
        ):
            raise ValueError("question text exceeds bounds or contains controls")
        if len({option["index"] for option in question["options"]}) != len(
            question["options"]
        ) or any(option["index"] < 0 for option in question["options"]):
            raise ValueError("invalid option indexes")
    return entry


def parse_register_progress(value: object) -> RegisterProgress:
    progress = cast(RegisterProgress, _decode(RegisterProgress, value))
    if progress["batch_rev"] < 1 or progress["seq"] < 0:
        raise ValueError("invalid progress revision")
    raise_ = progress["floor_raise"]
    if raise_ is not None and (raise_["classification"] is not None) != (
        raise_["cause"] is FloorRaiseCause.LATE_CLASSIFICATION
    ):
        raise ValueError("inconsistent floor raise")
    if (progress["local_resolution"] is None) != (progress["local_level"] is None):
        raise ValueError("incomplete local resolution")
    return progress


def collaborator_message_id(text: str) -> str | None:
    lines = text.splitlines()
    header = COLLABORATOR_HEADER_PATTERN.fullmatch(lines[0]) if lines else None
    if header is None:
        return None
    parts = header["body"].rsplit("; ", 2)
    if (
        len(parts) == 3
        and parts[0]
        and parts[1].startswith("from ")
        and parts[2].startswith("message-id ")
    ):
        return parts[2][len("message-id ") :] or None
    return None


def normalize_prompt(text: str, wrappers: tuple[tuple[str, str], ...] = ()) -> str:
    for prefix, suffix in wrappers:
        if text.startswith(prefix) and text.endswith(suffix):
            text = text[len(prefix) : len(text) - len(suffix) if suffix else None]
            break
    lines = text.splitlines()
    if lines and COLLABORATOR_HEADER_PATTERN.fullmatch(lines[0]):
        lines.pop(0)
    for index, line in enumerate(lines):
        if line.startswith(ACK_PARAGRAPH_MARKER) or line.startswith(
            COLLABORATOR_ACK_PREFIX
        ):
            lines = lines[:index]
            break
    return unicodedata.normalize(UNICODE_NORMALIZATION, "\n".join(lines).rstrip())


def body_digest(text: str) -> TextDigest:
    return file_digest(normalize_prompt(text).encode(UTF8))


def render_pending(batch_id: BatchId) -> str:
    return f"{MARKER_PENDING.format(batch_id=batch_id)} Your question was routed for an answer. Do not ask it again or reword it. Continue work that does not depend on it; otherwise stop at a safe point and end your turn with a summary. The answer will arrive as a user message."


def render_answer(
    batch_id: BatchId,
    rev: ResolutionRev,
    questions: tuple[Question, ...],
    answers: tuple[Answer, ...],
    level: EscalationLevel,
    broker: str,
    outbound_id: OutboundId | DeliveryId,
    token: str,
    *,
    supersedes: ResolutionRev | None = None,
    child_actor: ActorKey | None = None,
    child_finished: bool = False,
) -> str:
    """Render already-redacted domain text without adding module dependencies."""
    marker = MARKER_ANSWER.format(batch_id=batch_id, rev=rev)
    label = LEVEL_LABEL[level]

    def quoted(text: str, limit: int) -> str:
        return json.dumps(text[:limit], ensure_ascii=False)

    by_id = {q.id: q for q in questions}
    numbered = []
    for index, answer in enumerate(answers, 1):
        question = by_id[answer.question]
        choices = {o.index: o.label.text for o in question.options}
        answer_text = ", ".join(choices[i] for i in answer.choice)
        if answer.free_text:
            answer_text = (
                f"{answer_text}; {answer.free_text.text}"
                if answer_text
                else answer.free_text.text
            )
        numbered.append(
            f"{index}. {quoted(question.text.text, 200)} → {quoted(answer_text, 500)} (why: {quoted(answer.rationale.text, 200)})"
        )
    numbered.append(
        "Proceed. Do not ask this again. If this conflicts with the brief, stop and say so."
    )
    ack = f"uv run --script {shlex.quote(broker)} ack --outbound {shlex.quote(str(outbound_id))} --token-stdin <<< {shlex.quote(token)}"
    correction = (
        f"CORRECTION replacing r{supersedes}. If you already acted on r{supersedes}, reconcile now. "
        if supersedes is not None
        else ""
    )
    if child_actor is not None:
        child = parse_identity(str(child_actor), "actor")
        first = (
            f"Your subagent {child} has finished, so this can't be delivered to it; decide whether its result is still valid."
            if child_finished
            else f"Your subagent {child} asked a question; here is the answer from {label}. Pass the block below to that subagent unchanged, then run:"
        )
        return (
            f"{marker} {correction}{first}\n{ack}\n--- for {child} ---\n"
            + "\n".join(numbered)
            + "\n--- end ---"
        )
    first = f"{marker} {correction}Answer to your question(s) from {label}. This is r{rev}; if you have applied a higher r for this batch, ignore this."
    return first + "\n" + "\n".join(numbered) + f"\n{ACK_PARAGRAPH_MARKER}\n{ack}"


def render_correction(*args: Any, supersedes: ResolutionRev, **kwargs: Any) -> str:
    return render_answer(*args, supersedes=supersedes, **kwargs)
