#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in live probes: prepare fixture, collect redacted hooks, launch real TUI.

No hook trust, user configuration or permissions are changed by this harness.
Run serve in a visible Services pane before launch. Inspect /hooks normally.
"""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from urllib.request import Request, urlopen

try:
    from .probe_support import (
        HookEvent,
        Outcome,
        ProbeId,
        ProbeResult,
        build_fixture,
        redact,
    )
except ImportError:
    from probe_support import (
        HookEvent,
        Outcome,
        ProbeId,
        ProbeResult,
        build_fixture,
        redact,
    )

HERE = Path(__file__).resolve().parent
EVENTS = (HookEvent.USER_PROMPT_SUBMIT, HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE)
CONTEXT = "QE_CONTEXT_7fc1: the fixture uses tabs and oxlint."


class CodexMode(StrEnum):
    CONTEXT = "context"
    TIMEOUT = "timeout"
    DENY = "deny"
    OBSERVE = "observe"


class Transport(StrEnum):
    REX = "rex"
    QUEUE = "queue"
    QUEUE_RESUME = "queue-resume"


@dataclass(frozen=True, slots=True)
class HookRecord:
    recorded_at: str
    event: HookEvent
    payload: dict[str, object]


def record_result(result: ProbeResult, destination: Path) -> None:
    """Save typed evidence after redaction at the recording boundary."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        redact(json.dumps(asdict(result), default=str, indent=2)) + "\n"
    )


def command(
    argv: list[str], *, cwd: Path | None = None, text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """External CLI seam: tests replace executables on PATH."""
    return subprocess.run(
        argv, cwd=cwd, input=text, capture_output=True, text=True, check=False
    )


def versions() -> dict[str, str]:
    return {
        cli: command([cli, "version" if cli == "rex" else "--version"]).stdout.strip()
        for cli in ("codex", "claude", "rex")
    }


def prepare(root: Path, state: Path) -> None:
    build_fixture(root)
    state.mkdir(parents=True, exist_ok=True)
    command(["git", "init", "--quiet", str(root)])
    hook_cmd = shlex.join(
        [
            "uv",
            "run",
            "--script",
            str(HERE / "qe_probe.py"),
            "hook",
            "--state",
            str(state),
        ]
    )
    config = root / ".codex"
    config.mkdir(exist_ok=True)
    (config / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {"type": "command", "command": hook_cmd, "timeout": 60}
                            ]
                        }
                    ]
                    for event in EVENTS
                }
            },
            indent=2,
        )
        + "\n"
    )
    (state / "versions.json").write_text(json.dumps(versions(), indent=2) + "\n")


def hook(
    state: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
) -> None:
    data = json.load(sys.stdin)
    event = HookEvent(data["hook_event_name"])
    record = asdict(
        HookRecord(
            now().isoformat(),
            event,
            json.loads(redact(json.dumps(data))),
        )
    )
    state.mkdir(parents=True, exist_ok=True)
    with (state / "hooks.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    endpoint = state / "endpoint"
    if endpoint.exists():
        try:
            req = Request(
                endpoint.read_text().strip(),
                data=json.dumps(record).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(req, timeout=2):
                pass
        except OSError:
            pass
    mode_file = state / "mode"
    mode = (
        CodexMode(mode_file.read_text().strip())
        if mode_file.exists()
        else CodexMode.OBSERVE
    )
    output = {}
    if event == HookEvent.USER_PROMPT_SUBMIT and mode == CodexMode.CONTEXT:
        output = {
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": CONTEXT}
        }
    if event == HookEvent.USER_PROMPT_SUBMIT and mode == CodexMode.TIMEOUT:
        started = time.monotonic()
        time.sleep(55)
        output = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": f"QE_TIMEOUT_SURVIVED {time.monotonic() - started:.1f}",
            }
        }
    if (
        event == HookEvent.PRE_TOOL_USE
        and "request_user_input" in str(data.get("tool_name"))
        and mode == CodexMode.DENY
    ):
        output = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": "QE test: choose tabs; no follow-up question.",
            }
        }
    print(json.dumps(output))


def serve(state: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            value = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            with (state / "webhook.jsonl").open("a") as stream:
                stream.write(redact(json.dumps(value)) + "\n")
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    state.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    (state / "endpoint").write_text(f"http://127.0.0.1:{server.server_port}/hooks\n")
    print((state / "endpoint").read_text(), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        (state / "endpoint").unlink(missing_ok=True)


def launch(root: Path, state: Path, prompt: str, resume: str = "") -> None:
    parent = os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "GROKBOT_PARENT_SESSION"
    )
    if not parent:
        raise ValueError(
            "Set CODEX_THREAD_ID or GROKBOT_PARENT_SESSION to the immediate parent"
        )
    env = [
        "env",
        "-u",
        "GROKBOT_CALLBACK",
        f"GROKBOT_PARENT_SESSION={parent}",
        f"REX_VISIBLE_STATE_DIR={state}",
        f"GROKBOT_BRIEF={root / 'BRIEF.md'}",
    ]
    argv = env + [
        "codex",
        "-m",
        "gpt-6-luna",
        "-c",
        'model_reasoning_effort="low"',
        "-s",
        "workspace-write",
        "-a",
        "on-request",
        "-c",
        'approvals_reviewer="auto_review"',
        "-C",
        str(root),
        "--no-alt-screen",
        prompt,
    ]
    if resume:
        argv = argv[:-1] + ["resume", resume, prompt]
    opened = command(
        [
            "rex",
            "run",
            "-s",
            os.environ["REX_SESSION"],
            "--label",
            "qe-codex-probe",
            "--focus=false",
            "-c",
            str(root),
            "--json",
            "--",
            *argv,
        ]
    )
    if opened.returncode:
        raise RuntimeError(opened.stderr)
    (state / "launch.json").write_text(opened.stdout)
    (state / "launch-command.txt").write_text(shlex.join(argv) + "\n")
    print(opened.stdout)


def deliver(
    session: str, block: str, thread: str, text: str, transport: Transport
) -> list[subprocess.CompletedProcess[str]]:
    if transport == Transport.REX:
        argv = ["rex", "send", "-s", session, "-b", block]
        return [command(argv, text=text), command(argv, text="\r")]
    argv = ["codex", "queue", "--thread", thread, "--message", text]
    if transport == Transport.QUEUE_RESUME:
        argv.append("--resume")
    return [command(argv)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("prepare", "hook", "serve", "launch", "versions", "deliver", "record"),
    )
    parser.add_argument("--root", type=Path, default=Path("/tmp/qe-fixture"))
    parser.add_argument("--state", type=Path, default=Path("/tmp/qe-probe-codex-state"))
    parser.add_argument(
        "--prompt",
        default="QE probe: reply READY and wait for the next user message. Do not edit files.",
    )
    parser.add_argument("--resume", default="")
    parser.add_argument("--thread", default="")
    parser.add_argument("--block", default="")
    parser.add_argument("--probe", type=ProbeId, choices=list(ProbeId))
    parser.add_argument("--outcome", type=Outcome, choices=list(Outcome))
    parser.add_argument("--observation", default="")
    parser.add_argument("--fallback", default="")
    parser.add_argument("--command", action="append", default=[])
    parser.add_argument("--payload-file", type=Path)
    parser.add_argument(
        "--transport", type=Transport, choices=list(Transport), default=Transport.REX
    )
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.root, args.state)
    elif args.action == "hook":
        hook(args.state)
    elif args.action == "serve":
        serve(args.state)
    elif args.action == "launch":
        launch(args.root, args.state, args.prompt, args.resume)
    elif args.action == "deliver":
        for result in deliver(
            os.environ["REX_SESSION"],
            args.block,
            args.thread,
            args.prompt,
            args.transport,
        ):
            print(
                redact(
                    json.dumps(
                        {
                            "exit_code": result.returncode,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        }
                    )
                )
            )
    elif args.action == "record":
        if args.probe is None or args.outcome is None:
            parser.error("record requires --probe and --outcome")
        result = ProbeResult(
            args.probe,
            datetime.now(timezone.utc).date(),
            versions(),
            tuple(args.command),
            args.payload_file.read_text() if args.payload_file else "",
            args.observation,
            args.outcome,
            args.fallback or None,
        )
        record_result(result, args.state / f"{args.probe}.json")
    else:
        print(json.dumps(versions()))


if __name__ == "__main__":
    main()
