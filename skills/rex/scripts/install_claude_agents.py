#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy the repo's Claude subagent definitions into ~/.claude/agents.

Idempotent. Only files carrying the ownership marker are replaced, after a
backup; a same-named file without it is skipped unless --force. Other files in
the target are left alone, and settings are never touched.
"""
import argparse
import json
import sys
from pathlib import Path

from install_hooks import write

SOURCE = Path(__file__).resolve().parents[1] / "claude-agents"
# A whole frontmatter line (a YAML comment) in every definition we ship.
MARKER = "# managed-by: agent-tooling rex"


def install(target: Path, dry_run: bool = False, force: bool = False) -> tuple[list[str], list[str]]:
    """Return (changed, skipped) destination paths."""
    sources = sorted(SOURCE.glob("*.md"))
    if not sources:
        # The hooks runtime copy holds scripts only; run this from the skill.
        raise FileNotFoundError(f"no agent definitions in {SOURCE}")
    changed, skipped = [], []
    for source in sources:
        path, content = target / source.name, source.read_text()
        if path.exists():
            current = path.read_text(errors="replace")
            if current == content:
                continue
            if not force and MARKER not in current.splitlines():
                print(f"Skipped {path}: not installed by Rex. Rerun install_claude_agents.py "
                      "--force to back it up and replace it.", file=sys.stderr)
                skipped.append(str(path))
                continue
        if not dry_run:
            write(path, content)
        changed.append(str(path))
    return changed, skipped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude/agents")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="also replace same-named files Rex does not own")
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    changed, skipped = install(target, args.dry_run, args.force)
    print(json.dumps({"target": str(target), "dry_run": args.dry_run,
                      "changed": changed, "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
