"""Pure helpers shared by the QE probe harness and its self-checks."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
import re
from typing import Mapping


class ProbeId(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P2B = "P2b"
    P3 = "P3"
    P3B = "P3b"
    P4 = "P4"
    P4B = "P4b"
    P5 = "P5"
    P6 = "P6"
    P7A = "P7a"
    P7B = "P7b"
    P8 = "P8"
    P9 = "P9"
    P10 = "P10"
    P11 = "P11"
    P12 = "P12"
    P13 = "P13"
    P14 = "P14"
    P14_STUB = "P14-stub"
    P15 = "P15"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    RECORDED = "RECORDED"
    PENDING_BOT_ACTION = "PENDING-BOT-ACTION"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: ProbeId
    date: date
    versions: Mapping[str, str]
    commands: tuple[str, ...]
    payload: str
    observation: str
    outcome: Outcome
    fallback: str | None = None


CONTRIBUTING = "# Contributing\n\n- use tabs\n- lint with oxlint\n"
APPROVED_ADR = "# ADR 001: Fixture decision\n\nStatus: Accepted\n\nThis approved ADR is a probe fixture.\n"
FIXTURE_BRIEF = """# Probe brief

Use the fixture repository conventions when making changes.

```toml qe-escalation
schema = "qe.brief/1"
project = "personal"
l0_assume = ["test file names under tests/", "fixture data values"]
ask_carlos = ["database schema", "public API names"]
```
"""


def build_fixture(root: Path = Path("/tmp/qe-fixture")) -> Path:
    """Create the small deterministic repository used by QE probes."""
    (root / "docs/adr").mkdir(parents=True, exist_ok=True)
    (root / "CONTRIBUTING.md").write_text(CONTRIBUTING, encoding="utf-8")
    (root / "docs/adr/001.md").write_text(APPROVED_ADR, encoding="utf-8")
    (root / "BRIEF.md").write_text(FIXTURE_BRIEF, encoding="utf-8")
    return root


_SECRET = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,})\b"
)
_CREDENTIAL_FIELD = re.compile(
    r"(?i)((?:\bbearer\s+)|(?:[\"']?\b(?:token|api[_-]?key|secret|password)\b[\"']?\s*[:=]\s*[\"']?))[A-Za-z0-9_./+=:-]{8,}"
)
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
_AGENT_ID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
JIM_TEST_AGENT_ID = "e3b7bb61-41a1-4408-abac-58b26a2c47d0"


def redact(
    text: str, allowed_agent_ids: frozenset[str] = frozenset({JIM_TEST_AGENT_ID})
) -> str:
    """Redact common credentials, URL userinfo, and undocumented agent IDs."""
    redacted = _SECRET.sub("[REDACTED_TOKEN]", text)
    redacted = _CREDENTIAL_FIELD.sub(r"\1[REDACTED_TOKEN]", redacted)
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", redacted)
    return _AGENT_ID.sub(
        lambda match: (
            match.group(0)
            if match.group(0).lower()
            in {agent_id.lower() for agent_id in allowed_agent_ids}
            else "[REDACTED_AGENT_ID]"
        ),
        redacted,
    )
