#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Minimal P14 run-plan stand-in: validate the plan digest and echo it."""

import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import sys
from typing import Any

RESULT_PREFIX = "QE-RESULT "


@dataclass(frozen=True, slots=True)
class StubResult:
    plan_digest: str
    received_digest: str


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def plan_digest(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _decode_b64url(encoded: str) -> bytes:
    if not encoded or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for char in encoded
    ):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))


def run_plan(encoded: str) -> str:
    """Return the one result line, raising ValueError for invalid input."""
    raw = _decode_b64url(encoded)
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    supplied = plan.get("plan_digest")
    actual = plan_digest(plan)
    if not isinstance(supplied, str) or supplied != actual:
        raise ValueError("plan digest mismatch")
    result = StubResult(supplied, hashlib.sha256(raw).hexdigest())
    result_b64 = (
        base64.urlsafe_b64encode(canonical_json(asdict(result)))
        .decode("ascii")
        .rstrip("=")
    )
    return f"{RESULT_PREFIX}{result_b64}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "run-plan" or args[1] != "--b64":
        print("usage: run_plan_stub.py run-plan --b64 PLAN", file=sys.stderr)
        return 64
    try:
        print(run_plan(args[2]))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"invalid plan: {error}", file=sys.stderr)
        return 64
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
