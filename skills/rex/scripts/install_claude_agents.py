#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy the repo's Claude subagent definitions into ~/.claude/agents.

Idempotent; a differing destination is backed up before it is replaced. Other
files in the target are left alone, and settings are never touched.
"""
import argparse
import json
from pathlib import Path

from install_hooks import write

SOURCE = Path(__file__).resolve().parents[1] / "claude-agents"


def install(target: Path, dry_run: bool = False) -> list[str]:
    sources = sorted(SOURCE.glob("*.md"))
    if not sources:
        # The hooks runtime copy holds scripts only; run this from the skill.
        raise FileNotFoundError(f"no agent definitions in {SOURCE}")
    changed = []
    for source in sources:
        path, content = target / source.name, source.read_text()
        if path.exists() and path.read_text() == content:
            continue
        if not dry_run:
            write(path, content)
        changed.append(str(path))
    return changed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude/agents")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    changed = install(target, args.dry_run)
    print(json.dumps({"target": str(target), "dry_run": args.dry_run, "changed": changed}, indent=2))


if __name__ == "__main__":
    main()
