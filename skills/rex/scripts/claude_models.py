#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Resolve a Claude role to its model alias and effort from claude_models.toml.

Launchers pass both explicitly:

    ["claude", "--model", resolve(role), "--effort", effort(role), ...]

`role` is a ClaudeRole or its wire value ("normal"). An unknown role, or a
malformed table, raises; there is no default model.
"""
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

CONFIG = Path(__file__).resolve().with_name("claude_models.toml")


class ClaudeRole(StrEnum):
    MECHANICAL = "mechanical"
    NORMAL = "normal"
    HARD = "hard"
    REVIEW = "review"


class ClaudeModel(StrEnum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


class ClaudeEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class RoleModel:
    model: ClaudeModel
    effort: ClaudeEffort


def load(path: Path = CONFIG) -> dict[ClaudeRole, RoleModel]:
    with path.open("rb") as stream:
        roles = tomllib.load(stream)["roles"]
    table = {}
    for name, entry in roles.items():
        if set(entry) != {"model", "effort"}:
            raise ValueError(f"role {name!r} needs exactly model and effort")
        table[ClaudeRole(name)] = RoleModel(ClaudeModel(entry["model"]), ClaudeEffort(entry["effort"]))
    if missing := set(ClaudeRole) - set(table):
        raise ValueError(f"roles missing from {path.name}: {sorted(missing)}")
    return table


def resolve(role: ClaudeRole | str) -> ClaudeModel:
    """Model alias for the role; the result is a str."""
    return load()[ClaudeRole(role)].model


def effort(role: ClaudeRole | str) -> ClaudeEffort:
    """Effort level for the role; the result is a str."""
    return load()[ClaudeRole(role)].effort
