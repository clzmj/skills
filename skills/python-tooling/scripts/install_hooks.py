#!/usr/bin/env python3
"""Install personal Python hooks, with optional local Rex integration."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import sys
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


def merge_hook(data, event, command, matcher=None, timeout=5):
    groups = data.setdefault("hooks", {}).setdefault(event, [])
    arguments = shlex.split(command)[1:]
    for group in groups:
        for handler in group.get("hooks", []):
            try:
                existing = shlex.split(handler.get("command", ""))
            except ValueError:
                continue
            if existing and existing[1:] == arguments:
                handler["command"] = command
                return
    group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher:
        group["matcher"] = matcher
    groups.append(group)


def marked_block(path, name, text):
    before = path.read_text() if path.exists() else ""
    begin, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    addition = f"{begin}\n{text.strip()}\n{end}"
    pattern = re.escape(begin) + r".*?" + re.escape(end)
    if begin in before and end in before:
        after = re.sub(pattern, lambda _: addition, before, flags=re.S)
    else:
        after = before.rstrip() + ("\n\n" if before.strip() else "") + addition + "\n"
    write(path, after)


def rex_hooks(data, root, codex):
    # Move owned legacy definitions to the stable runtime. The host controls trust.
    for script, events in (
        ("rex_visible.py", ("SessionStart", "UserPromptSubmit", "PreToolUse")),
        ("codex_sessions.py", ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop", "SessionEnd", "PostToolUse") if codex else ()),
    ):
        for event in events:
            groups = data.get("hooks", {}).get(event, [])
            command = shlex.join([shutil.which("python3") or sys.executable, str(root / script), "hook"])
            existing = []
            for group in groups:
                for handler in group.get("hooks", []):
                    try:
                        words = shlex.split(handler.get("command", ""))
                    except ValueError:
                        continue
                    if len(words) >= 2 and Path(words[-2]).name == script and words[-1] == "hook":
                        existing.append(handler)
            if existing:
                for handler in existing:
                    handler["command"] = command
                    if event == "SessionEnd":
                        handler["timeout"] = 3
                continue
            matcher = "Bash|exec_command|shell|shell_command" if event == "PreToolUse" else (".*(send_message|send_input|followup_task)$" if event == "PostToolUse" else None)
            merge_hook(data, event, command, matcher, timeout=3 if event == "SessionEnd" else (20 if event == "SubagentStart" else 5))


def install(home, agents, with_rex=False):
    source = Path(__file__).resolve().parent
    runtime = home / ".local" / "share" / "agent-tooling"
    runtime.mkdir(parents=True, exist_ok=True)
    if source != runtime:
        shutil.copy2(source / "python_tooling.py", runtime / "python_tooling.py")
        shutil.copy2(Path(__file__).resolve(), runtime / "install_hooks.py")
    if with_rex and (source / "rex").resolve() != (runtime / "rex").resolve():
        shutil.copytree(source / "rex", runtime / "rex", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".state"))
    changed = []
    command = shlex.join([shutil.which("python3") or sys.executable, str(runtime / "python_tooling.py")])
    for agent in agents:
        codex = agent == "codex"
        path = home / (".codex/hooks.json" if codex else ".claude/settings.json")
        data = read_json(path)
        merge_hook(data, "SessionStart", command)
        merge_hook(data, "PreToolUse", command, "Bash|exec_command|shell|shell_command")
        if with_rex:
            rex_hooks(data, runtime / "rex", codex)
        if write(path, json.dumps(data, indent=2) + "\n"):
            changed.append(str(path))
        if with_rex:
            instructions = home / (".codex/AGENTS.md" if codex else ".claude/CLAUDE.md")
            marked_block(instructions, "rex-visible", "## Visible terminal work\n\nRun important commands in the existing main Codex Rex session. Reuse its tabs and owned panes; never create task or team sessions for visibility. Before important terminal work, read " + str(runtime / "rex/POLICY.md") + " and follow its runner, layout, verification, and retention rules. Keep quick reads in ordinary tools. Preserve normal permissions and explicitly requested machines.")
            if codex:
                marked_block(instructions, "codex-coordination", "## Conversational agent coordination\n\nCoordinate the user's objective and delegate useful bounded work. Before delegation or session-to-session coordination, read " + str(runtime / "rex/COORDINATION.md") + ". Keep ownership clear, use economical models, and keep views in the main Codex Rex session.")
    return {"runtime": str(runtime), "changed": changed, "hook_review": "Review new or changed hooks in the host's /hooks menu. This installer never changes hook trust or tool permissions."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="+", choices=("codex", "claude-code"), default=["codex", "claude-code"])
    parser.add_argument("--with-rex", action="store_true", help="Also install the existing local Rex workflow (Rex CLI required)")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    print(json.dumps(install(args.home.expanduser().resolve(), args.agents, args.with_rex), indent=2))
