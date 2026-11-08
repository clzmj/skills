#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install independent Rex hooks without changing Python hooks or host trust."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import time


def write(path, content):
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".agent-tooling-backup-" + time.strftime("%Y%m%d-%H%M%S")))
    temporary = path.with_name(path.name + ".agent-tooling.tmp")
    temporary.write_text(content)
    temporary.replace(path)
    return True


def marked_block(path, name, text):
    before = path.read_text() if path.exists() else ""
    begin, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    addition = f"{begin}\n{text.strip()}\n{end}"
    if begin in before and end in before:
        after = re.sub(re.escape(begin) + r".*?" + re.escape(end), lambda _: addition, before, flags=re.S)
    else:
        after = before.rstrip() + ("\n\n" if before.strip() else "") + addition + "\n"
    write(path, after)


def owned_handler(command, script, home, root):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 3 or words[-1] != "hook":
        return False
    path = Path(words[-2])
    if path.name != script or not path.is_absolute():
        return False
    return (path.parent == root or path.parent == home / "Developer/tools/rex-visible"
            or path.parent == Path(__file__).resolve().parent
            or str(path.parent).endswith(("/skills/python-tooling/scripts/rex", "/skills/rex/scripts")))


def rex_hooks(data, root, home, uv, codex):
    definitions = [("rex_visible.py", ("SessionStart", "UserPromptSubmit", "PreToolUse"))]
    if codex:
        definitions.append(("codex_sessions.py", ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop", "SessionEnd", "PostToolUse")))
    for script, events in definitions:
        for event in events:
            command = shlex.join([uv, "run", "--script", str(root / script), "hook"])
            groups = data.setdefault("hooks", {}).setdefault(event, [])
            existing = [handler for group in groups for handler in group.get("hooks", [])
                        if owned_handler(handler.get("command", ""), script, home, root)]
            if existing:
                for handler in existing:
                    handler["command"] = command
                    if event == "SessionEnd":
                        handler["timeout"] = 3
                continue
            matcher = "Bash|exec_command|shell|shell_command" if event == "PreToolUse" else (".*(send_message|send_input|followup_task)$" if event == "PostToolUse" else None)
            group = {"hooks": [{"type": "command", "command": command,
                                 "timeout": 3 if event == "SessionEnd" else (20 if event == "SubagentStart" else 5)}]}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)


def install(home, agents):
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required; install uv and ensure it is on PATH")
    uv = str(Path(uv).absolute())
    source = Path(__file__).resolve().parent
    runtime = home / ".local/share/agent-tooling/rex"
    if source != runtime.resolve():
        shutil.copytree(source, runtime, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".state", "*.pyc"))
    changed = []
    for agent in agents:
        codex = agent == "codex"
        path = home / (".codex/hooks.json" if codex else ".claude/settings.json")
        data = json.loads(path.read_text()) if path.exists() else {}
        rex_hooks(data, runtime, home, uv, codex)
        if write(path, json.dumps(data, indent=2) + "\n"):
            changed.append(str(path))
        instructions = home / (".codex/AGENTS.md" if codex else ".claude/CLAUDE.md")
        marked_block(instructions, "rex-visible", "## Visible terminal work\n\nRun important commands in the existing main Codex Rex session. Reuse its tabs and owned panes; never create task or team sessions for visibility. Before important terminal work, read " + str(runtime / "POLICY.md") + " and follow its runner, layout, verification, and retention rules. Keep quick reads in ordinary tools. Preserve normal permissions and explicitly requested machines. Access other machines through their configured Rex connection directly, never SSH.")
        if codex:
            marked_block(instructions, "codex-coordination", "## Conversational agent coordination\n\nCoordinate the user's objective and delegate useful bounded work. Before delegation or session-to-session coordination, read " + str(runtime / "COORDINATION.md") + ". Keep ownership clear, use economical models, and keep views in the main Codex Rex session.")
    return {"runtime": str(runtime), "changed": changed,
            "hook_review": "Review new or changed hooks in the host's /hooks menu. This installer never changes hook trust or tool permissions."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="+", choices=("codex", "claude-code"), default=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()
    print(json.dumps(install(args.home.expanduser().resolve(), args.agents), indent=2))
