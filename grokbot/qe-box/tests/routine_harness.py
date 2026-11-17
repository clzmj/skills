#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Small, mechanical model of the §4.3 routine output boundary.

The harness deliberately never interprets machine-shell output. It copies one
well-formed QE-RESULT line, or reports no result and passes the raw output only
as stdin evidence.
"""

from __future__ import annotations

import re
import subprocess
from typing import Sequence


RESULT_LINE = re.compile(r"^QE-RESULT [A-Za-z0-9_-]+$")


def matching_result_lines(stdout: str) -> list[str]:
    return [line for line in stdout.split("\n") if RESULT_LINE.fullmatch(line)]


def finalize_plan(
    *,
    plan_id: str,
    stdout: str,
    stderr: str,
    question_text: str,
    runner=subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Invoke exactly one result command; all other output is evidence only."""
    lines = matching_result_lines(stdout)
    if len(lines) == 1:
        argv: Sequence[str] = ("qe-box", "result", "--line", lines[0])
        evidence = None
    else:
        argv = (
            "qe-box",
            "result",
            "--none",
            "--plan",
            plan_id,
            "--why",
            "no_result_line",
            "--detail-stdin",
        )
        evidence = f"question:\n{question_text}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    return runner(argv, input=evidence, text=True, capture_output=True, check=False)
