#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Install Python tooling hooks for Codex and Claude Code."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import time


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def write(path, content):
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(path.name + ".agent-tooling-backup-" + stamp))
    temporary = path.with_name(path.name + ".agent-tooling.tmp")
    temporary.write_text(content)
    temporary.replace(path)
    return True


def merge_hook(data, event, command, matcher=None):
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    script = shlex.split(command)[-1]
    for group in groups:
        for handler in group.get("hooks", []):
            try:
                words = shlex.split(handler.get("command", ""))
            except ValueError:
                continue
            # The exact owned script path survives Python-to-uv launcher migration.
            if words and words[-1] == script:
                handler["command"] = command
                return
    group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
    if matcher:
        group["matcher"] = matcher
    groups.append(group)


def install(home, agents):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install and run Python tooling hooks")
    source = Path(__file__).resolve().parent
    runtime = home / ".local/share/agent-tooling"
    runtime.mkdir(parents=True, exist_ok=True)
    if source != runtime:
        shutil.copy2(source / "python_tooling.py", runtime / "python_tooling.py")
        shutil.copy2(Path(__file__).resolve(), runtime / "install_hooks.py")
    changed = []
    command = shlex.join([uv, "run", "--script", str(runtime / "python_tooling.py")])
    for agent in agents:
        path = home / (".codex/hooks.json" if agent == "codex" else ".claude/settings.json")
        data = read_json(path)
        merge_hook(data, "SessionStart", command)
        merge_hook(data, "PreToolUse", command, "Bash|exec_command|shell|shell_command")
        if write(path, json.dumps(data, indent=2) + "\n"):
            changed.append(str(path))
    return {"runtime": str(runtime), "changed": changed, "hook_review": "Review new or changed hooks in the host's /hooks menu. This installer never changes hook trust or tool permissions."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="+", choices=("codex", "claude-code"), default=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    print(json.dumps(install(args.home.expanduser().resolve(), args.agents), indent=2))
