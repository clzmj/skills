"""Fresh other-vendor subprocess boundary. Default configuration fails closed.

Initial reservations are empirical measurements from a small P12 sample, not a
population guarantee. Tool validation additionally gates live enablement; tests
use local executables, never live model calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import json
import math
import os
import pwd
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
import tempfile
import tomllib
from typing import Callable, Mapping, Protocol, Generator

import question_model as m
from question_ledger import ReservationId
from question_redact import redact
from question_guard import CATEGORY_FLOORS

MAX_PROMPT_BYTES = 24 * 1024
MAX_CONCURRENT_ANSWERERS = 2
MIN_USEFUL_SECONDS = 15.0
# Fixed per-call ceilings keep calibration and production argv identical after
# observed p95 reservations change. Reservations remain measured, not multiplied.
CLAUDE_MAX_BUDGET_USD = {
    m.Model.HAIKU: Decimal("0.02"),
    m.Model.SONNET: Decimal("0.15"),
}
ROOT = Path(__file__).parent
ESSENTIAL_ENV = frozenset(("PATH", "HOME", "LANG", "TMPDIR"))
CLAUDE_OAUTH_KEYCHAIN_SERVICE = "grokbot-claude-oauth-token"
VENDOR_AUTH = {
    m.AnswererKind.CLAUDE_PRINT: frozenset(("CLAUDE_CODE_OAUTH_TOKEN",)),
    m.AnswererKind.CODEX_EXEC: frozenset(("OPENAI_API_KEY",)),
}


class _WaitCallbackFailed(Exception):
    def __init__(self, error: Exception):
        self.error = error
        super().__init__(str(error))


def _notify_wait(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        try:
            callback()
        except Exception as exc:
            # A local registration/ledger failure is not a failed paid model call.
            raise _WaitCallbackFailed(exc) from exc


class BudgetStore(Protocol):
    path: Path

    def reserve_budget(
        self, task: str, *, model: m.Model, tokens: int, usd: Decimal
    ) -> ReservationId | None: ...
    def settle_budget(
        self, reservation: ReservationId, *, usage: m.Usage | None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: m.Model
    enabled: bool
    bounded: bool
    bounds: m.InvocationBounds | None
    reservation_validated: bool = False
    tools_validated: bool = False


def _positive_integer(value: object) -> int:
    parsed = integer(value)
    if parsed < 1:
        raise ValueError("expected positive integer")
    return parsed


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("decimal must be a string")
    try:
        parsed = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError("invalid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("decimal must be nonnegative and finite")
    return parsed


def load_models(
    path: Path = ROOT / "question_models.toml",
) -> dict[m.Model, ModelConfig]:
    data = tomllib.loads(path.read_text())
    if set(data) != {"models"} or not isinstance(data["models"], dict):
        raise ValueError("expected model configuration")
    if set(data["models"]) != {model.value for model in m.Model}:
        raise ValueError("missing or unknown model")
    required = {
        "enabled",
        "bounded",
        "reservation_validated",
        "tools_validated",
        "evidence",
    }
    optional = {
        "max_turns",
        "max_output_tokens",
        "reserve_tokens",
        "reserve_usd",
        "observed_tokens",
        "observed_usd",
        "reservation_samples",
        "reservation_method",
    }
    configs = {}
    for name, raw in data["models"].items():
        model = m.Model(name)
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or set(raw) - required - optional
        ):
            raise ValueError("invalid model fields")
        enabled = boolean(raw["enabled"])
        bounded = boolean(raw["bounded"])
        reserved = boolean(raw["reservation_validated"])
        tools = boolean(raw["tools_validated"])
        if not isinstance(raw["evidence"], str) or not raw["evidence"].strip():
            raise ValueError("model evidence required")
        turns = _positive_integer(raw["max_turns"]) if "max_turns" in raw else None
        if "max_output_tokens" in raw:
            # P12 observed no accepted output-token bound on either CLI.
            raise ValueError("output-token bound has no validated CLI capability")
        if bounded != (turns is not None):
            raise ValueError("bounded models require a validated turn limit")
        if model in (m.Model.GPT_LUNA, m.Model.GPT_SOL) and (
            bounded or enabled or tools or reserved
        ):
            raise ValueError("P12 Codex is refused unbounded")
        tokens = (
            _positive_integer(raw["reserve_tokens"])
            if "reserve_tokens" in raw
            else None
        )
        usd = _decimal(raw["reserve_usd"]) if "reserve_usd" in raw else None
        for field in ("observed_tokens", "observed_usd"):
            if field in raw:
                if not isinstance(raw[field], list) or not raw[field]:
                    raise ValueError("expected nonempty observation list")
                for value in raw[field]:
                    if field == "observed_tokens":
                        _positive_integer(value)
                    else:
                        _decimal(value)
        if "reservation_samples" in raw:
            _positive_integer(raw["reservation_samples"])
        if (
            "reservation_method" in raw
            and raw["reservation_method"] != "nearest_rank_p95"
        ):
            raise ValueError("unknown reservation method")
        if reserved and (not bounded or tokens is None or usd is None or usd <= 0):
            raise ValueError("validated reservation requires bounded measured values")
        if enabled and not (bounded and reserved and tools):
            raise ValueError("enabled model lacks validated capabilities")
        bounds = None
        if reserved:
            assert tokens is not None and usd is not None
            bounds = m.InvocationBounds(
                MAX_PROMPT_BYTES,
                turns,
                None,
                timedelta(
                    seconds=90 if model in (m.Model.HAIKU, m.Model.GPT_LUNA) else 240
                ),
                tokens,
                usd,
            )
        configs[model] = ModelConfig(model, enabled, bounded, bounds, reserved, tools)
    return configs


@dataclass(frozen=True, slots=True)
class AnswererOutput:
    verdict: m.Verdict
    classification: m.Classification
    answers: tuple[m.Answer, ...]
    reversible: bool
    crosses_boundary: bool
    changes_adr: bool
    revert: m.Untrusted | None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    ordinal: int
    started_at: datetime
    latency: timedelta
    usage: m.Usage | None
    outcome: m.AttemptOutcome


@dataclass(frozen=True, slots=True)
class AnswerAttempt:
    model: m.Model
    outcome: m.AttemptOutcome
    reason: m.EscalationReason | None
    output: AnswererOutput | None = None
    attempts: tuple[InvocationResult, ...] = ()


def text(value: object) -> m.Untrusted:
    if not isinstance(value, str) or redact(value).secret_present:
        raise ValueError("invalid or secret-bearing model text")
    return m.Untrusted(value, m.UntrustedSource.MODEL)


def integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected integer")
    return value


def boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("expected boolean")
    return value


def parse_answerer_output(raw: object, batch: m.QuestionBatch) -> AnswererOutput:
    if not isinstance(raw, dict):
        raise ValueError("expected answerer object")
    required = {
        "verdict",
        "category",
        "delegable",
        "sensitive_topics",
        "answers",
        "reversible",
        "crosses_boundary",
        "changes_adr",
        "revert",
    }
    if set(raw) != required:
        raise ValueError("unexpected answerer fields")
    category = m.Category(raw["category"])
    delegable = boolean(raw["delegable"])
    topics = raw["sensitive_topics"]
    if not isinstance(topics, list) or len(topics) > m.SENSITIVE_TOPICS_MAX:
        raise ValueError("invalid sensitive topics")
    topics = tuple(text(t).text for t in topics)
    if any(len(t.encode(m.UTF8)) > m.SENSITIVE_TOPIC_MAX for t in topics):
        raise ValueError("sensitive topic too long")
    floor = CATEGORY_FLOORS[category]
    if not delegable or topics:
        floor = max(floor, m.EscalationLevel.L3_PRINCIPAL)
    classification = m.Classification(category, delegable, topics, (), floor, True)
    questions = tuple(q for q in batch.questions if q.retired_rev is None)
    answers = []
    seen = set()
    if not isinstance(raw["answers"], list):
        raise ValueError("expected answers list")
    for a in raw["answers"]:
        if not isinstance(a, dict) or set(a) != {
            "question",
            "choice",
            "free_text",
            "rationale",
            "evidence",
            "confidence",
        }:
            raise ValueError("invalid answer")
        ordinal = integer(a["question"])
        if ordinal < 1 or ordinal > len(questions) or ordinal in seen:
            raise ValueError("unknown or duplicate ordinal")
        seen.add(ordinal)
        q = questions[ordinal - 1]
        if not isinstance(a["choice"], list) or not isinstance(a["evidence"], list):
            raise ValueError("invalid answer lists")
        choices = tuple(m.OptionIndex(integer(i)) for i in a["choice"])
        if any(i not in {o.index for o in q.options} for i in choices) or (
            not q.multi_select and len(choices) > 1
        ):
            raise ValueError("choice not offered")
        free = None if a["free_text"] is None else text(a["free_text"])
        if free is not None and not q.allows_free_text:
            raise ValueError("free text not allowed")
        evidence = []
        for e in a["evidence"]:
            if not isinstance(e, dict) or set(e) != {"path", "line", "quote"}:
                raise ValueError("invalid evidence")
            evidence.append(
                m.EvidenceRef(
                    text(e["path"]).text,
                    None if e["line"] is None else integer(e["line"]),
                    text(e["quote"]),
                )
            )
        confidence = a["confidence"]
        if confidence is not None and (type(confidence) not in (int, float)):
            raise ValueError("invalid confidence")
        answers.append(
            m.Answer(
                q.id,
                choices,
                free,
                text(a["rationale"]),
                tuple(evidence),
                None if confidence is None else m.Confidence(float(confidence)),
            )
        )
    verdict = m.Verdict(raw["verdict"])
    if verdict == m.Verdict.ANSWER and len(seen) != len(questions):
        raise ValueError("missing answer")
    return AnswererOutput(
        verdict,
        classification,
        tuple(answers),
        boolean(raw["reversible"]),
        boolean(raw["crosses_boundary"]),
        boolean(raw["changes_adr"]),
        None if raw["revert"] is None else text(raw["revert"]),
    )


def parse_usage(raw: object) -> m.Usage | None:
    try:
        if not isinstance(raw, dict):
            return None
        usage = raw["usage"]
        if not isinstance(usage, dict):
            return None
        # Cache tokens are billed input too; don't undercount tool context.
        counts = (integer(usage["input_tokens"]),) + tuple(
            integer(usage.get(k, 0))
            for k in (
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        if any(count < 0 for count in counts):
            return None
        return m.Usage(
            sum(counts),
            integer(usage["output_tokens"]),
            Decimal(str(raw["total_cost_usd"])),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


@contextmanager
def answerer_slot(directory: Path) -> Generator[int | None, None, None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for n in range(MAX_CONCURRENT_ANSWERERS):
        fd = os.open(
            directory / f"answerer-{n}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            yield fd
            return
        finally:
            os.close(fd)
    yield None


def build_command(config: ModelConfig, *, executable: str = "claude") -> list[str]:
    """Return the exact production CLI profile, without starting a process."""
    bounds = config.bounds
    if bounds is None or bounds.max_turns is None:
        raise ValueError("command requires invocation turn bounds")
    schema = (ROOT / "answerer_output.v1.json").read_text()
    # tools restricts the available inventory; allowedTools alone only grants permissions.
    return [
        executable,
        "-p",
        "--model",
        config.model.value,
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--max-turns",
        str(bounds.max_turns),
        "--max-budget-usd",
        str(CLAUDE_MAX_BUDGET_USD[config.model]),
        "--tools",
        "Read,Grep,Glob",
        "--allowedTools",
        "Read Grep Glob",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]


def model_family_matches(raw: object, model: m.Model) -> bool:
    """Validate every CLI-reported actual model against the requested family.

    Missing usage identity is unvalidatable. Multiple reported models are
    accepted only when every one belongs to the explicitly requested family.
    """
    family = {m.Model.HAIKU: "haiku", m.Model.SONNET: "sonnet"}.get(model)
    if family is None or not isinstance(raw, dict):
        return False
    reported = raw.get("modelUsage")
    if not isinstance(reported, dict) or not reported:
        return False
    pattern = rf"claude-{family}-[0-9]+(?:[-.][0-9]+)*"
    return all(
        isinstance(key, str) and re.fullmatch(pattern, key) is not None
        for key in reported
    )


def answerer_environment(kind: m.AnswererKind) -> dict[str, str] | None:
    """Resolve auth in the parent; return None when Claude OAuth is unavailable.

    Explicit parent OAuth takes precedence over the macOS user's Keychain item.
    No API-key or token-file fallback. Never copy the token into os.environ.
    """
    env = {
        k: v for k, v in os.environ.items() if k in ESSENTIAL_ENV | VENDOR_AUTH[kind]
    }
    if kind != m.AnswererKind.CLAUDE_PRINT or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return env
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                CLAUDE_OAUTH_KEYCHAIN_SERVICE,
                "-a",
                pwd.getpwuid(os.getuid()).pw_name,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env={k: v for k, v in env.items() if k in ESSENTIAL_ENV},
        )
    except (OSError, KeyError, subprocess.SubprocessError, UnicodeError):
        # Subprocess diagnostics may contain secrets; expose no error text.
        return None
    token = result.stdout.rstrip("\r\n")
    if result.returncode != 0 or not token:
        return None
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


class Answerers:
    def __init__(
        self,
        store: BudgetStore,
        *,
        config_path: Path = ROOT / "question_models.toml",
        configs: Mapping[m.Model, ModelConfig] | None = None,
        slots_dir: Path | None = None,
        executable: str = "claude",
    ):
        self.store = store
        try:
            self.configs = load_models(config_path) if configs is None else configs
        except (OSError, ValueError):
            # Policy remains strict at load_models; runtime absence must still
            # let the worker register and escalate the batch without a call.
            self.configs = {}
        self.slots_dir = slots_dir or Path.home() / ".local/state/rex/qe-answerer-slots"
        self.executable = executable

    def invoke(
        self,
        batch: m.QuestionBatch,
        level: m.EscalationLevel,
        prompt: str,
        *,
        on_wait: Callable[[], None] | None = None,
        pass_fds: tuple[int, ...] = (),
        deadline: float | None = None,
    ) -> AnswerAttempt:
        if level not in (m.EscalationLevel.L1_CHEAP, m.EscalationLevel.L2_NORMAL):
            raise ValueError("answerers only support L1/L2")
        codex = batch.asker.agent == m.AgentKind.CODEX
        model = (
            (m.Model.HAIKU if level == 1 else m.Model.SONNET)
            if codex
            else (m.Model.GPT_LUNA if level == 1 else m.Model.GPT_SOL)
        )
        if deadline is not None:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError("deadline must be finite monotonic time")
            if deadline - time.monotonic() < MIN_USEFUL_SECONDS:
                return AnswerAttempt(
                    model, m.AttemptOutcome.FAILED, m.EscalationReason.TIMEOUT
                )
        config = self.configs.get(model)
        if config is None:
            return AnswerAttempt(
                model, m.AttemptOutcome.FAILED, m.EscalationReason.NO_ANSWERER
            )
        bounds = config.bounds
        if not config.bounded or (
            bounds is not None
            and bounds.max_turns is None
            and bounds.max_output_tokens is None
        ):
            return AnswerAttempt(
                model,
                m.AttemptOutcome.REFUSED_UNBOUNDED,
                m.EscalationReason.UNBOUNDED_ANSWERER,
            )
        if (
            not config.enabled
            or bounds is None
            or not config.reservation_validated
            or not config.tools_validated
            or bounds.max_turns is None
        ):
            return AnswerAttempt(
                model, m.AttemptOutcome.FAILED, m.EscalationReason.NO_ANSWERER
            )
        # Codex's CLI remains refused even if a local config claims otherwise.
        if not codex:
            return AnswerAttempt(
                model,
                m.AttemptOutcome.REFUSED_UNBOUNDED,
                m.EscalationReason.UNBOUNDED_ANSWERER,
            )
        if (
            len(prompt.encode()) > min(MAX_PROMPT_BYTES, bounds.max_prompt_bytes)
            or redact(prompt).secret_present
            or any(
                h.trigger == m.GuardTrigger.SECRET_PRESENT
                for h in batch.classification.hits
            )
        ):
            return AnswerAttempt(
                model, m.AttemptOutcome.REJECTED, m.EscalationReason.ANSWER_REJECTED
            )
        with answerer_slot(self.slots_dir) as acquired:
            if acquired is None:
                return AnswerAttempt(
                    model, m.AttemptOutcome.FAILED, m.EscalationReason.NO_ANSWERER
                )
            try:
                return self._attempts(
                    batch,
                    level,
                    prompt,
                    config,
                    on_wait,
                    (*pass_fds, acquired),
                    deadline,
                )
            except _WaitCallbackFailed as exc:
                raise exc.error.with_traceback(exc.error.__traceback__) from None

    def _attempts(
        self,
        batch: m.QuestionBatch,
        level: m.EscalationLevel,
        prompt: str,
        config: ModelConfig,
        on_wait: Callable[[], None] | None,
        pass_fds: tuple[int, ...],
        deadline: float | None,
    ) -> AnswerAttempt:
        bounds = config.bounds
        assert bounds is not None
        results = []
        # ponytail: CLAUDE_PRINT is hard-coded until Codex has validated bounds.
        kind = m.AnswererKind.CLAUDE_PRINT
        env = answerer_environment(kind)
        if env is None:
            return AnswerAttempt(
                config.model, m.AttemptOutcome.FAILED, m.EscalationReason.NO_ANSWERER
            )
        argv = build_command(config, executable=self.executable)
        deadline_s = min(bounds.wall_clock.total_seconds(), 90 if level == 1 else 240)
        for ordinal in (1, 2):
            _notify_wait(on_wait)
            if (
                deadline is not None
                and deadline - time.monotonic() < MIN_USEFUL_SECONDS
            ):
                return AnswerAttempt(
                    config.model,
                    m.AttemptOutcome.FAILED,
                    m.EscalationReason.TIMEOUT,
                    attempts=tuple(results),
                )
            reservation = self.store.reserve_budget(
                str(batch.asker.task),
                model=config.model,
                tokens=bounds.reserve_tokens,
                usd=bounds.reserve_usd,
            )
            if reservation is None:
                return AnswerAttempt(
                    config.model,
                    m.AttemptOutcome.FAILED,
                    m.EscalationReason.BUDGET_EXHAUSTED,
                    attempts=tuple(results),
                )
            started = datetime.now(timezone.utc)
            clock = time.monotonic()
            attempt_deadline = clock + deadline_s
            if deadline is not None:
                attempt_deadline = min(attempt_deadline, deadline)
            usage = None
            output = None
            reason = m.EscalationReason.ANSWERER_FAILED
            process = None
            try:
                # Temporary files avoid pipe deadlocks and bound memory consumption.
                with (
                    tempfile.TemporaryFile() as stdin,
                    tempfile.TemporaryFile() as stdout,
                    tempfile.TemporaryFile() as stderr,
                ):
                    stdin.write(prompt.encode())
                    stdin.seek(0)
                    if time.monotonic() >= attempt_deadline or (
                        deadline is not None
                        and deadline - time.monotonic() < MIN_USEFUL_SECONDS
                    ):
                        reason = m.EscalationReason.TIMEOUT
                        raise TimeoutError
                    process = subprocess.Popen(
                        argv,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        cwd=batch.asker.cwd,
                        env=env,
                        start_new_session=True,
                        pass_fds=pass_fds,
                    )
                    while process.poll() is None:
                        if time.monotonic() >= attempt_deadline:
                            reason = m.EscalationReason.TIMEOUT
                            raise TimeoutError
                        _notify_wait(on_wait)
                        time.sleep(0.02)
                    if time.monotonic() >= attempt_deadline:
                        reason = m.EscalationReason.TIMEOUT
                        raise TimeoutError
                    stdout.seek(0)
                    raw = json.loads(stdout.read(1024 * 1024 + 1))
                    usage = parse_usage(raw)
                    if not isinstance(raw, dict):
                        raise ValueError("expected provider result object")
                    if (
                        process.returncode != 0
                        or raw.get("is_error")
                        or raw.get("subtype") != "success"
                    ):
                        raise ValueError("answerer failed")
                    if not model_family_matches(raw, config.model):
                        reason = m.EscalationReason.MODEL_MISMATCH
                        raise ValueError("missing or mismatched actual model family")
                    output = parse_answerer_output(raw["structured_output"], batch)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                TimeoutError,
                RecursionError,
            ):
                pass
            finally:
                if process is not None:
                    # Terminate any surviving descendants, even if the leader exited.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                if process is None:
                    usage = m.Usage(0, 0, Decimal(0))
                self.store.settle_budget(reservation, usage=usage)
            outcome = (
                m.AttemptOutcome.REJECTED
                if reason == m.EscalationReason.MODEL_MISMATCH
                else m.AttemptOutcome.ANSWERED
                if output is not None
                else m.AttemptOutcome.FAILED
            )
            results.append(
                InvocationResult(
                    ordinal,
                    started,
                    timedelta(seconds=time.monotonic() - clock),
                    usage,
                    outcome,
                )
            )
            if reason == m.EscalationReason.MODEL_MISMATCH:
                return AnswerAttempt(
                    config.model, outcome, reason, attempts=tuple(results)
                )
            if reason == m.EscalationReason.TIMEOUT and output is None:
                # A timeout may retry only if the worker can afford a full fresh
                # per-level invocation window; never reset its absolute deadline.
                if deadline is not None and deadline - time.monotonic() < max(
                    deadline_s, MIN_USEFUL_SECONDS
                ):
                    return AnswerAttempt(
                        config.model, outcome, reason, attempts=tuple(results)
                    )
            if output is not None:
                return AnswerAttempt(
                    config.model, outcome, None, output, tuple(results)
                )
        return AnswerAttempt(
            config.model, m.AttemptOutcome.FAILED, reason, attempts=tuple(results)
        )
