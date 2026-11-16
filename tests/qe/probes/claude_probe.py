# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Additive Claude hook recorder used by the PR-0 live probes.

Run ``hook --mode MODE --output PATH`` from a Claude settings command. Raw
payloads never enter the fixture output. This does not install or edit hooks.
``settings --mode MODE --output PATH`` prints additive --settings JSON.
Optional PATH.with_suffix('.mode') selects a mode between interactive turns;
PATH.with_suffix('.webhook') supplies a loopback-only collector endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from enum import StrEnum
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    from .probe_support import HookEvent
    from .probe_support import redact as redact_text
except ImportError:
    from probe_support import HookEvent
    from probe_support import redact as redact_text


class HookMode(StrEnum):
    UNKNOWN = "unknown"
    OBSERVE = "observe"
    PERMISSION_ANSWER = "permission-answer"
    PERMISSION_PENDING = "permission-pending"
    DENY_ANSWER = "deny-answer"
    DENY_PENDING = "deny-pending"
    UPDATED_INPUT = "updated-input"
    TIMEOUT = "timeout"
    CONTEXT = "context"


def settings(mode: HookMode, output: Path) -> dict[str, object]:
    """Build additive settings without replacing the installed lifecycle hooks."""
    command = shlex.join(
        [
            "uv",
            "run",
            "--script",
            str(Path(__file__).resolve()),
            "hook",
            "--mode",
            mode.value,
            "--output",
            str(output.resolve()),
        ]
    )
    events = tuple(event for event in HookEvent if event != HookEvent.UNKNOWN)
    return {
        "hooks": {
            event: [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command, "timeout": 60}],
                }
            ]
            for event in events
        }
    }


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted-agent-id>"
                if key.lower().replace("_", "") in {"agentid", "parentagentid"}
                else "<redacted-token>"
                if key.lower().replace("_", "")
                in {
                    "authorization",
                    "token",
                    "apikey",
                    "secret",
                    "password",
                    "accesstoken",
                    "refreshtoken",
                }
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\b[a-f0-9]{17}\b", "<redacted-agent-id>", value)
        value = re.sub(r"(?<=agent-)[a-f0-9]{12,}", "<redacted-agent-id>", value)
        value = re.sub(
            r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b",
            "<redacted-token>",
            value,
        )
        return re.sub(
            r"(https?://)[^/@\s]+@", r"\1<redacted-userinfo>@", redact_text(value)
        )
    return value


def hook(mode: HookMode, output: Path) -> None:
    mode_file = output.with_suffix(".mode")
    raw_mode = mode_file.read_text().strip() if mode_file.exists() else None
    if raw_mode is not None:
        try:
            mode = HookMode(raw_mode)
        except ValueError:
            mode = HookMode.UNKNOWN
    payload = json.load(sys.stdin)
    try:
        event = HookEvent(payload["hook_event_name"])
    except (ValueError, KeyError):
        event = HookEvent.UNKNOWN
    tool = payload.get("tool_name", "")
    started = time.monotonic()
    response: dict[str, object] = {}
    if tool == "AskUserQuestion":
        tool_input = payload.get("tool_input", {})
        questions = tool_input.get("questions", [])
        answers = {question["question"]: "tabs" for question in questions}
        if event == HookEvent.PERMISSION_REQUEST and mode in {
            HookMode.PERMISSION_ANSWER,
            HookMode.PERMISSION_PENDING,
        }:
            if mode == HookMode.PERMISSION_PENDING:
                time.sleep(45)
                decision = {
                    "behavior": "deny",
                    "message": "QE-PENDING: answer is pending; continue without re-asking.",
                }
            else:
                decision = {
                    "behavior": "allow",
                    "updatedInput": {**tool_input, "answers": answers},
                }
            response = {
                "hookSpecificOutput": {"hookEventName": event, "decision": decision}
            }
        elif event == HookEvent.PRE_TOOL_USE:
            if mode in {HookMode.DENY_ANSWER, HookMode.DENY_PENDING}:
                reason = (
                    "QE-ANSWER: use tabs."
                    if mode == HookMode.DENY_ANSWER
                    else "QE-PENDING: answer is pending; continue without re-asking."
                )
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            elif mode == HookMode.UPDATED_INPUT:
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "updatedInput": {**tool_input, "answers": answers},
                    }
                }
            elif mode == HookMode.TIMEOUT:
                time.sleep(55)
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "QE-55S-SURVIVED: use tabs.",
                    }
                }
    if event == HookEvent.USER_PROMPT_SUBMIT and mode == HookMode.CONTEXT:
        response = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": "The probe secret word is QE-CONTEXT-ORCHID. Include it in your answer.",
            }
        }
    record = {
        "mode": mode,
        "raw_mode": raw_mode,
        "event": event,
        "raw_event": payload.get("hook_event_name"),
        "payload": payload,
        "response": response,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rex_block": os.environ.get("REX_BLOCK"),
        "rex_session": os.environ.get("REX_SESSION"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a") as stream:
        stream.write(json.dumps(redact(record), sort_keys=True) + "\n")
    endpoint_file = output.with_suffix(".webhook")
    if endpoint_file.exists():
        endpoint = endpoint_file.read_text().strip()
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
        ):
            raise ValueError("Probe webhook must use loopback")
        request = Request(
            endpoint,
            data=json.dumps(redact(record)).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as receipt:
            receipt.read()
    print(json.dumps(response))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["hook", "settings"])
    parser.add_argument("--mode", type=HookMode, default=HookMode.OBSERVE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "settings":
        print(json.dumps(settings(args.mode, args.output), indent=2))
    else:
        hook(args.mode, args.output)


if __name__ == "__main__":
    main()
