#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Open a Claude Code run in a Rex tab, only with a verified Notion ticket.

Exit codes: 2 usage, otherwise ticket_guard.Refusal.
"""
import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import claude_models
import rex_visible as rex
import ticket_guard
from ticket_guard import Refusal, TicketError

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")


def model_for(role: str, override: str | None) -> claude_models.ClaudeModel:
    if override is None:
        return claude_models.resolve(role)
    allowed = {entry.model for entry in claude_models.load().values()}
    if override not in allowed:
        raise TicketError(Refusal.BAD_MODEL, f"model {override!r} is not in the policy: {', '.join(sorted(allowed))}")
    return claude_models.ClaudeModel(override)


def directory(value: str, name: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise TicketError(Refusal.BAD_PATH, f"{name} is not a directory: {path}")
    return path


def launch_script(args: argparse.Namespace, ticket: ticket_guard.Ticket) -> str:
    model = model_for(args.role, args.model)
    worktree = directory(args.worktree, "worktree")
    brief = Path(args.brief).expanduser().resolve()
    if not brief.is_file():
        raise TicketError(Refusal.BAD_PATH, f"brief is not a file: {brief}")
    add_dirs = [brief.parent, *(directory(value, "add-dir") for value in args.add_dir)]
    claude = shutil.which("claude")
    if not claude:
        raise TicketError(Refusal.BAD_PATH, "claude is not on PATH")
    environment = {"PATH": os.environ.get("PATH", ""), "GROKBOT_TICKET": ticket.id, "GROKBOT_TICKET_URL": ticket.url,
                   "GROKBOT_TASK_ID": ticket.task_id, "GROKBOT_CALLBACK": args.callback, "GROKBOT_BRIEF": str(brief)}
    if parent := os.environ.get("GROKBOT_PARENT_SESSION"):
        environment["GROKBOT_PARENT_SESSION"] = parent
    if args.review:
        environment["GROKBOT_REVIEW"] = "1"
    if args.no_pr:
        environment["GROKBOT_NO_PR"] = "1"
    command = [claude, "--model", model.value, "--effort", claude_models.effort(args.role).value]
    for path in add_dirs:
        command += ["--add-dir", str(path)]
    if args.review:
        # ponytail: removes the edit tools only; Bash still asks the host for permission. Sandbox if that is not enough.
        command += ["--disallowedTools", *WRITE_TOOLS]
    command += ["--", f"Read and carry out the brief at {brief}."]
    return "\n".join(["#!/bin/sh", "unset GROKBOT_REVIEW GROKBOT_NO_PR GROKBOT_PARENT_SESSION",
                      *(f"export {key}={shlex.quote(value)}" for key, value in environment.items()),
                      f"cd {shlex.quote(str(worktree))} || exit 1", "exec " + shlex.join(command), ""])


def open_tab(args: argparse.Namespace, script_text: str) -> tuple[str, str]:
    tab = f"{args.label}-review" if args.review else args.label
    try:
        script = rex.state_dir() / f"rex-run-{tab}.sh"
        script.unlink(missing_ok=True)
        with os.fdopen(os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as stream:
            os.fchmod(stream.fileno(), 0o700)
            stream.write(script_text)
        session = args.session or rex.main_session()
        created = rex.rex_json(["window", "new", "-s", session, tab, "-c", str(Path(args.worktree).expanduser().resolve()),
                                "--focus=false", "--keep-open", "--json", "--", "/bin/sh", str(script)])
        return created["window_id"], created["block_ids"][0]
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
        raise TicketError(Refusal.REX_FAILED, f"Rex could not open the tab: {error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", required=True, help="Notion page URL or id")
    parser.add_argument("--label", required=True, help="tab label and GROKBOT_TASK_ID")
    parser.add_argument("--role", required=True, choices=[role.value for role in claude_models.ClaudeRole])
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--brief", required=True)
    parser.add_argument("--callback", required=True, help="bot to notify")
    parser.add_argument("--model", help="override the role's model; must be in claude_models.toml")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--session", default=os.environ.get("REX_SESSION", ""))
    parser.add_argument("--review", action="store_true", help="read-only reviewer run on the same ticket")
    parser.add_argument("--no-pr", action="store_true", help="the brief needs no PR, so finishing means Done")
    args = parser.parse_args(argv)
    try:
        requested = ticket_guard.page_id(args.ticket)
        if not requested:
            raise TicketError(Refusal.BAD_TICKET, "--ticket is not a Notion page URL or id")
        ticket = ticket_guard.load(args.label)
        if requested != ticket_guard.page_id(ticket.id):
            raise TicketError(Refusal.TICKET_MISMATCH, "--ticket does not match the ticket record")
        window, block = open_tab(args, launch_script(args, ticket))
    except TicketError as error:
        print(f"rex-run refused: {error}", file=sys.stderr)
        return error.refusal
    print(json.dumps({"window_id": window, "block_id": block, "ticket": {"id": ticket.id, "url": ticket.url}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
