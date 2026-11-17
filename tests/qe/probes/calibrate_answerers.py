#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Observed P12 calibration; 20 serial calls per model, only with --run.

Imports the exact production argv/schema/environment and Broker.ladder_prompt.
No installed hook/config, real question, user pane, bot or box is touched.
Raw vendor output stays private. Only summary.json is intended for sanitization.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import TypedDict
from uuid import uuid4

WORDS = (
    "birch",
    "orbit",
    "violet",
    "paper",
    "silver",
    "cedar",
    "amber",
    "raven",
    "meadow",
    "cobalt",
    "maple",
    "coral",
    "lilac",
    "pebble",
    "willow",
    "linen",
)
PAD = "This isolated fixture preserves its documented local convention. "
TARGET_BYTES = 24 * 1024 - 64
SAMPLES_PER_MODEL = 20
MAX_CALLS = SAMPLES_PER_MODEL * 2


class Sample(TypedDict):
    model: str
    ordinal: int
    max_turns: int
    max_budget_usd: str
    wall_clock_s: int
    prompt_bytes: int
    num_turns: int | None
    exit_code: int | None
    timed_out: bool
    subtype: str | None
    elapsed_ms: int
    successful_evidence: bool
    model_family_valid: bool
    marker_evidence_valid: bool
    validation_failures: list[str]
    local_rejection: str | None
    parse_failure: str | None
    input_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    tokens: int | None
    usd: str | None
    model_ids: list[str]
    structured_output_present: bool
    api_key_source: str | None


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Required explicit approval guard for up to 40 serial model calls",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error(
            "Review this script, then use --run only in the authorized primary session"
        )
    if os.environ.get("GROKBOT_QE") == "1":
        parser.error("GROKBOT_QE must remain off during calibration")
    if os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get(
        "CLAUDE_CODE_OAUTH_TOKEN"
    ):
        parser.error("Calibration requires subscription OAuth only")
    os.umask(0o077)
    scripts = args.repo.resolve() / "skills/rex/scripts"
    sys.path.insert(0, str(scripts))
    import question_answerers as a
    import question_broker as b
    import question_guard as g
    import question_ledger as l
    import question_model as m

    source_files = {
        "answerer_schema_sha256": scripts / "answerer_output.v1.json",
        "answerer_adapter_sha256": scripts / "question_answerers.py",
        "broker_sha256": scripts / "question_broker.py",
        "guard_sha256": scripts / "question_guard.py",
        "ledger_sha256": scripts / "question_ledger.py",
        "model_sha256": scripts / "question_model.py",
    }
    source_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in source_files.items()
    }
    repo_head = subprocess.check_output(
        ["git", "-C", str(args.repo.resolve()), "rev-parse", "HEAD"], text=True
    ).strip()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    private = output_dir / "private"
    private.mkdir(mode=0o700)
    fixture = output_dir / "isolated-repo"
    fixture.mkdir(mode=0o700)
    marker = "Calibration marker: " + " ".join(secrets.choice(WORDS) for _ in range(8))
    (fixture / "README.md").write_text(
        "# Harmless calibration repository\n\nThe repository convention is tabs.\n"
        + marker
        + "\n"
    )
    # Explicitly suppress git hooks only for this isolated fixture's setup.
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(fixture)]
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
    subprocess.run([*git, "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.name=QE calibration",
            "-c",
            "user.email=qe@example.invalid",
            "commit",
            "-qm",
            "harmless fixture",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    brief = fixture / "BRIEF.md"
    question = m.Question(
        m.QuestionId(uuid4()),
        m.NativeCallId("p12-isolated-call"),
        m.Untrusted(
            "Which indentation convention should this isolated repository use? Read or Grep README.md before answering. Include the entire exact Calibration marker line from that file as evidence.quote at its real line. Use repository-relative evidence paths: README.md, never an absolute path. The marker is deliberately absent from this prompt; do not guess. Also cite the repository convention. Choose an offered option and return the complete answerer schema.",
            m.UntrustedSource.AGENT,
        ),
        None,
        (
            m.Option(
                m.OptionIndex(0), m.Untrusted("Use tabs", m.UntrustedSource.AGENT)
            ),
            m.Option(
                m.OptionIndex(1), m.Untrusted("Use spaces", m.UntrustedSource.AGENT)
            ),
        ),
        False,
        False,
        m.NativeQuestionKey(codex_id="p12-isolated-question"),
        None,
    )

    def write_brief(size: int) -> None:
        filler = (PAD * (size // len(PAD) + 1))[:size]
        brief.write_text(
            "# Isolated calibration\n## Goal\nInspect this fixture's README and retain the recorded convention.\n## Scope\n"
            + filler
            + "\n"
        )

    def create_batch(store: l.Ledger):
        asker = m.AskerRef(
            m.AgentKind.CODEX,
            m.ActorKey("codex:p12-isolated"),
            m.ActorKey("codex:p12-isolated"),
            m.ActorRole.ROOT,
            m.SessionId("p12-isolated"),
            m.TurnId("p12-turn"),
            m.TaskId("p12-calibration"),
            m.RexSessionId(""),
            m.RexBlockId(""),
            str(fixture),
            m.HeadSha(head),
            m.file_digest(brief.read_bytes()),
            m.CallbackBot("jim"),
        )
        call = m.NativeCall(
            m.QuestionTool.CODEX_BLOCKING,
            m.NativeCallId("p12-isolated-call"),
            asker.actor,
            asker.turn,
            l.now(),
            False,
            None,
        )
        return store.open_or_attach(
            asker,
            (question,),
            call,
            g.assess_guard((question,), cwd=str(fixture)),
            brief=brief,
        ).batch

    write_brief(0)
    with l.Ledger(private / "sizing.sqlite3") as sizing:
        sized_batch = create_batch(sizing)
        broker = b.Broker(sizing)
        low, high = 0, TARGET_BYTES
        while low < high:
            midpoint = (low + high + 1) // 2
            write_brief(midpoint)
            try:
                fits = (
                    len(broker.ladder_prompt(sized_batch).encode(m.UTF8))
                    <= TARGET_BYTES
                )
            except ValueError:
                fits = False
            if fits:
                low = midpoint
            else:
                high = midpoint - 1
        write_brief(low)
    samples: list[Sample] = []
    with l.Ledger(private / "calibration.sqlite3") as store:
        batch = create_batch(store)
        prompt = b.Broker(store).ladder_prompt(batch)
        prompt_bytes = len(prompt.encode(m.UTF8))
        assert TARGET_BYTES - 8 <= prompt_bytes <= TARGET_BYTES
        assert not a.redact(prompt).secret_present
        assert marker not in prompt
        (private / "prompt.txt").write_text(prompt)
        env = a.answerer_environment(m.AnswererKind.CLAUDE_PRINT)
        version = subprocess.run(
            ["claude", "--version"],
            cwd=fixture,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if version.returncode != 0:
            raise RuntimeError(
                "Claude CLI version check failed; no calibration started"
            )
        for model, level, wall in (
            (m.Model.HAIKU, m.EscalationLevel.L1_CHEAP, 90),
            (m.Model.SONNET, m.EscalationLevel.L2_NORMAL, 240),
        ):
            bounds = m.InvocationBounds(
                24 * 1024,
                4,
                None,
                timedelta(seconds=wall),
                0,
                a.CLAUDE_MAX_BUDGET_USD[model],
            )
            config = a.ModelConfig(model, True, True, bounds, True, True)
            argv = a.build_command(config)
            assert argv[argv.index("--max-turns") + 1] == "4"
            for ordinal in range(1, SAMPLES_PER_MODEL + 1):
                assert len(samples) < MAX_CALLS
                prefix = f"{model.value}-{ordinal}"
                started = time.monotonic()
                process = None
                timed_out = False
                raw = None
                failure = None
                with (
                    (private / f"{prefix}.stdout.json").open("wb") as stdout,
                    (private / f"{prefix}.stderr.txt").open("wb") as stderr,
                ):
                    try:
                        process = subprocess.Popen(
                            argv,
                            stdin=subprocess.PIPE,
                            stdout=stdout,
                            stderr=stderr,
                            cwd=fixture,
                            env=env,
                            start_new_session=True,
                        )
                        process.communicate(prompt.encode(m.UTF8), timeout=wall)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                    finally:
                        if process is not None:
                            if timed_out or process.poll() is None:
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                            process.wait()
                elapsed = time.monotonic() - started
                try:
                    raw = json.loads((private / f"{prefix}.stdout.json").read_bytes())
                    if not isinstance(raw, dict):
                        raise ValueError("provider result is not an object")
                    usage = a.parse_usage(raw)
                    parsed = a.parse_answerer_output(raw["structured_output"], batch)
                    failures = g.validate_answers(
                        batch.questions,
                        parsed.answers,
                        level=level,
                        repo=fixture,
                        hits=(),
                    )
                    classification = g.derive_floor(
                        batch.questions,
                        classification=parsed.classification,
                        cwd=str(fixture),
                    )
                    rejection = b.Broker(store).local_rejection(
                        batch, parsed, classification, level
                    )
                    valid_family = a.model_family_matches(raw, model)
                    valid_marker = any(
                        e.path == "README.md" and marker in e.quote.text
                        for answer in parsed.answers
                        for e in answer.evidence
                    )
                    checked = (
                        not timed_out
                        and process is not None
                        and process.returncode == 0
                        and valid_family
                        and raw.get("subtype") == "success"
                        and parsed.verdict is m.Verdict.ANSWER
                        and not failures
                        and rejection is None
                        and valid_marker
                        and all(answer.choice == (0,) for answer in parsed.answers)
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RecursionError,
                ) as exc:
                    failure = type(exc).__name__
                    usage = a.parse_usage(raw)
                    failures, valid_family, valid_marker, checked, rejection = (
                        (),
                        False,
                        False,
                        False,
                        None,
                    )
                sample: Sample = {
                    "model": model.value,
                    "ordinal": ordinal,
                    "max_turns": 4,
                    "max_budget_usd": str(a.CLAUDE_MAX_BUDGET_USD[model]),
                    "wall_clock_s": wall,
                    "prompt_bytes": prompt_bytes,
                    "num_turns": raw.get("num_turns")
                    if isinstance(raw, dict) and type(raw.get("num_turns")) is int
                    else None,
                    "exit_code": process.returncode if process else None,
                    "timed_out": timed_out,
                    "subtype": raw.get("subtype")
                    if isinstance(raw, dict)
                    and isinstance(raw.get("subtype"), str)
                    and raw.get("subtype")
                    in {
                        "success",
                        "error_max_turns",
                        "error_during_execution",
                        "error_max_budget_usd",
                    }
                    else None,
                    "elapsed_ms": round(elapsed * 1000),
                    "successful_evidence": checked,
                    "model_family_valid": valid_family,
                    "marker_evidence_valid": valid_marker,
                    "validation_failures": [f.value for f in failures],
                    "local_rejection": rejection.value if rejection else None,
                    "parse_failure": failure,
                    "input_tokens": raw.get("usage", {}).get("input_tokens")
                    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict)
                    else None,
                    "cache_creation_input_tokens": raw.get("usage", {}).get(
                        "cache_creation_input_tokens", 0
                    )
                    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict)
                    else None,
                    "cache_read_input_tokens": raw.get("usage", {}).get(
                        "cache_read_input_tokens", 0
                    )
                    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict)
                    else None,
                    "output_tokens": raw.get("usage", {}).get("output_tokens")
                    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict)
                    else None,
                    "tokens": usage.input_tokens + usage.output_tokens
                    if usage
                    else None,
                    "usd": str(usage.usd) if usage else None,
                    "model_ids": sorted(
                        k
                        for k in raw.get("modelUsage", {})
                        if re.fullmatch(r"claude-[a-z0-9.-]{1,80}", k)
                    )
                    if isinstance(raw, dict)
                    and isinstance(raw.get("modelUsage", {}), dict)
                    else [],
                    "structured_output_present": isinstance(raw, dict)
                    and isinstance(raw.get("structured_output"), dict),
                    # Only known metadata labels are safe to publish, never arbitrary
                    # vendor strings or authentication values.
                    "api_key_source": raw.get("apiKeySource")
                    if isinstance(raw, dict)
                    and raw.get("apiKeySource")
                    in ("oauth", "claude.ai", "subscription", "none")
                    else "unrecognized"
                    if isinstance(raw, dict) and "apiKeySource" in raw
                    else None,
                }
                samples.append(sample)
                print(json.dumps(sample), flush=True)
                (output_dir / "summary.json").write_text(
                    json.dumps({"samples": samples}, indent=2) + "\n"
                )
                if not checked:
                    raise SystemExit(1)
    summary = {
        "schema": "qe.p12-observed-read-calibration/1",
        "sample_count": len(samples),
        "max_calls": MAX_CALLS,
        "profile": "question_answerers.build_command + answerer_environment; Broker.ladder_prompt",
        "calibration_date": date.today().isoformat(),
        "tool_profile": {
            "cli": "claude",
            "version": version.stdout.strip()[:100],
            "python": sys.version.split()[0],
        },
        "repository_head": repo_head,
        "source_sha256": source_hashes,
        "max_turns": 4,
        "auth_method": "Configured subscription OAuth via CLAUDE_CODE_OAUTH_TOKEN; ANTHROPIC_API_KEY absent; no --bare; envelope auth source recorded separately when present",
        "prompt_bytes": prompt_bytes,
        "p95_method": "empirical nearest rank; n=20 per actual model family; rank 19; no headroom",
        "samples": samples,
        "models": {},
    }
    for model in ("haiku", "sonnet"):
        selected = [s for s in samples if s["model"] == model]
        usage_samples = [
            s
            for s in selected
            if s.get("model_family_valid")
            and s["tokens"] is not None
            and s["usd"] is not None
        ]
        token_values = sorted(
            tokens for s in usage_samples if (tokens := s["tokens"]) is not None
        )
        cost_values = sorted(
            Decimal(usd) for s in usage_samples if (usd := s["usd"]) is not None
        )
        wall_values = sorted(s["elapsed_ms"] for s in usage_samples)
        p50_index = max(0, __import__("math").ceil(0.50 * len(usage_samples)) - 1)
        p95_index = max(0, __import__("math").ceil(0.95 * len(usage_samples)) - 1)
        summary["models"][model] = {
            "sample_count": len(selected),
            "model_family_valid_count": sum(
                s.get("model_family_valid", False) for s in selected
            ),
            "successful_evidence_count": sum(
                s["successful_evidence"] for s in selected
            ),
            "actual_model_ids": sorted(
                {model_id for s in selected for model_id in s.get("model_ids", [])}
            ),
            "p95_rank_1_based": p95_index + 1 if usage_samples else None,
            "p50_tokens_observed": token_values[p50_index] if usage_samples else None,
            "p50_usd_observed": str(cost_values[p50_index]) if usage_samples else None,
            "p50_elapsed_ms_observed": wall_values[p50_index]
            if usage_samples
            else None,
            "p95_tokens_observed": token_values[p95_index] if usage_samples else None,
            "p95_usd_observed": str(cost_values[p95_index]) if usage_samples else None,
            "p95_elapsed_ms_observed": wall_values[p95_index]
            if usage_samples
            else None,
        }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    args.receipt.resolve().write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "summary": str(output_dir / "summary.json"),
                "calls": len(samples),
                "successful_evidence": sum(s["successful_evidence"] for s in samples),
            }
        )
    )
    return (
        0
        if all(s["successful_evidence"] for s in samples) and len(samples) == MAX_CALLS
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
