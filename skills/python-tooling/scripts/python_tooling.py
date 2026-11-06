#!/usr/bin/env python3
"""Read-only Python workflow guidance for SessionStart and PreToolUse hooks.

This is a convenience guard, not a shell security boundary. It never runs tools.
Set PYTHON_TOOLING_DISABLED=1 in the hook environment to disable it.
"""

import json
import os
from pathlib import Path
import re
import shlex
import sys


SHELL_TOOLS = {"Bash", "exec_command", "shell", "shell_command"}


def project_root(cwd):
    """Use the nearest Python project, without crossing a repository boundary."""
    path = Path(cwd).resolve()
    for candidate in (path, *path.parents):
        if any((candidate / name).is_file() for name in
               ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
            return candidate
        if (candidate / ".git").exists():
            break
    return None


def configured(root, tool, metadata):
    return (
        (root / f"{tool}.toml").is_file()
        or (root / f".{tool}.toml").is_file()
        or (root / ".venv" / "bin" / tool).is_file()
        or re.search(r"(?m)^\s*\[tool\." + tool + r"(?:\.|\])", metadata)
        or re.search(r"[\"']" + tool + r"(?:[\s<>=!~;\[\"'])", metadata)
    )


def session_context(root):
    try:
        metadata = (root / "pyproject.toml").read_text()
    except (OSError, UnicodeError):
        metadata = ""
    guidance = [f"Python project: {root}. Follow the python-tooling skill and existing project scripts."]
    if (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file():
        guidance.append("This project uses uv: use uv add/remove for dependencies, uv sync --locked for the existing lock, and uv run for project commands.")
    for tool, command in (("ruff", "ruff format and ruff check"), ("ty", "ty check")):
        if configured(root, tool, metadata):
            guidance.append(f"{tool} is configured or locally installed; run relevant {command} checks through the project environment.")
    guidance.append("Run substantial checks through the host's normal visible terminal workflow. This hook runs no checks and installs nothing.")
    return " ".join(guidance)


def shell_segments(command):
    # Split operators before shlex removes quotes, so a quoted ";" is data.
    start = 0
    quote = None
    escaped = False
    comment = False
    for index, char in enumerate(command):
        if comment:
            if char != "\n":
                continue
            comment = False
        elif escaped:
            escaped = False
            continue
        elif char == "\\" and quote != "'":
            escaped = True
            continue
        elif quote:
            if char == quote:
                quote = None
            continue
        elif char in {"'", '"'}:
            quote = char
            continue
        elif char == "#" and (index == start or command[index - 1].isspace()):
            comment = True
            continue
        if char in ";&|\n":
            tokens = shlex.split(command[start:index], comments=True)
            if tokens:
                yield tokens
            start = index + 1
    tokens = shlex.split(command[start:], comments=True)
    if tokens:
        yield tokens


def executable(token):
    return Path(token).name


def strip_prefix(tokens):
    tokens = list(tokens)
    while tokens and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]):
        tokens.pop(0)
    if tokens and executable(tokens[0]) in {"env", "command"}:
        tokens.pop(0)
        while tokens and (tokens[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0])):
            option = tokens.pop(0)
            if option in {"-C", "--chdir"} or option.startswith("--chdir="):
                # This wrapper changes directories; do not infer the wrong project.
                return []
            if option in {"-u", "--unset"} and tokens:
                tokens.pop(0)
    return tokens


def pip_action(tokens):
    # pip's global options that consume a following value, before its subcommand.
    valued = {"--python", "--proxy", "--retries", "--timeout", "--exists-action",
              "--trusted-host", "--cert", "--client-cert", "--cache-dir", "--log"}
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 2 if option in valued else 1
    return tokens[index] if index < len(tokens) else None


def mutates_with_pip(tokens):
    tokens = strip_prefix(tokens)
    if not tokens:
        return False
    name = executable(tokens[0])
    if re.fullmatch(r"pip(?:\d+(?:\.\d+)?)?", name):
        return pip_action(tokens[1:]) in {"install", "uninstall"}
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name):
        return len(tokens) > 3 and tokens[1:3] == ["-m", "pip"] and pip_action(tokens[3:]) in {"install", "uninstall"}
    if name == "uv" and len(tokens) > 2 and tokens[1] == "pip":
        return pip_action(tokens[2:]) in {"install", "uninstall", "sync"}
    return False


def blocked_command(command, cwd, depth=0):
    if depth > 3:
        return False
    for tokens in shell_segments(command):
        tokens = strip_prefix(tokens)
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) == 2:
            cwd = (Path(cwd) / tokens[1]).resolve()
            continue
        name = executable(tokens[0])
        if name in {"sh", "bash", "zsh"} and len(tokens) >= 3 and tokens[1] in {"-c", "-lc"}:
            if blocked_command(tokens[2], cwd, depth + 1):
                return True
        if mutates_with_pip(tokens):
            root = project_root(cwd)
            if root and (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file():
                return True
    return False


def handle(payload):
    if os.environ.get("PYTHON_TOOLING_DISABLED") == "1" or not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    if event == "SessionStart":
        root = project_root(cwd)
        if root:
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": session_context(root)}}
    if event == "PreToolUse" and payload.get("tool_name") in SHELL_TOOLS:
        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return None
        command = tool_input.get("command", tool_input.get("cmd"))
        workdir = tool_input.get("workdir", cwd)
        if not isinstance(command, str) or not isinstance(workdir, str):
            return None
        workdir = (Path(cwd) / workdir).resolve()
        if blocked_command(command, workdir):
            reason = ("This uv project has pyproject.toml and uv.lock. Direct pip environment changes bypass its dependency workflow. "
                      "Preserve the command's intent using uv add (or uv add --dev), uv remove, or uv sync --locked as appropriate. "
                      "For an explicitly requested exceptional pip workflow, set PYTHON_TOOLING_DISABLED=1 in the hook environment.")
            return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny", "permissionDecisionReason": reason}}
    return None


def main():
    try:
        result = handle(json.load(sys.stdin))
    except (ValueError, TypeError, OSError, RuntimeError):
        # Malformed payloads or unparseable shell syntax must not disrupt work.
        return
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
