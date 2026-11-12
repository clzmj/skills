#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local Codex team membership, visible activity, and explicit session messaging."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid

import rex_visible as rex

ROOT = Path(__file__).resolve().parent
DB = rex.STATE / "coordination.sqlite3"
POLICY = ROOT / "COORDINATION.md"
STATES = ("working", "waiting", "blocked", "idle", "reported", "accepted", "ended")
RETAIN_SECONDS = 30
INACTIVE_STATES = {"idle", "reported", "accepted", "ended"}


def thread_id(value):
    return str(uuid.UUID(value))


@contextmanager
def database():
    DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                team TEXT, thread TEXT, name TEXT, mode TEXT, cwd TEXT,
                state TEXT, updated REAL, transcript TEXT, parent TEXT,
                PRIMARY KEY(team, thread), UNIQUE(team, name));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
                team TEXT, sender TEXT, recipient TEXT, kind TEXT, text TEXT,
                created REAL, reply_to TEXT);
            CREATE TABLE IF NOT EXISTS views (
                team TEXT, thread TEXT, session TEXT, block TEXT,
                PRIMARY KEY(team, thread));
            CREATE TABLE IF NOT EXISTS active_teams (thread TEXT PRIMARY KEY, team TEXT);
            CREATE TABLE IF NOT EXISTS delegations (child TEXT PRIMARY KEY, parent TEXT, team TEXT);
            CREATE INDEX IF NOT EXISTS events_team ON events(team,seq);
        """)
        columns = {r[1] for r in db.execute("PRAGMA table_info(members)")}
        for column, default in (("role", "worker"), ("model", ""), ("effort", ""), ("parent_source", "unknown"), ("provider", "codex"), ("native_session", ""), ("native_id", "")):
            if column not in columns:
                try:
                    db.execute(f"ALTER TABLE members ADD COLUMN {column} TEXT DEFAULT '{default}'")
                    if column == "role":
                        db.execute("UPDATE members SET role=CASE WHEN parent='' THEN 'coordinator' ELSE 'worker' END")
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error):
                        raise
        if "token" not in {r[1] for r in db.execute("PRAGMA table_info(views)")}:
            try:
                db.execute("ALTER TABLE views ADD COLUMN token TEXT DEFAULT ''")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
        yield db


def register(team, thread, name, mode="session", cwd=None, parent="", transcript="", activate=False, rename=False, role=None, model="", effort="", provisional_parent=False, refine_hook_parent=False, provider="codex", native_session="", native_id=""):
    thread = thread_id(thread)
    with database() as db:
        existing = db.execute("SELECT * FROM members WHERE team=? AND thread=?", (team, thread)).fetchone()
        if existing and existing["provider"] != provider:
            raise ValueError("A registered member cannot change providers")
        delegation = db.execute("SELECT parent,team FROM delegations WHERE child=?", (thread,)).fetchone()
        if delegation and delegation["team"] != team:
            raise ValueError("A delegated child cannot move to another team")
        if provisional_parent and existing and existing["parent"]:
            # Lifecycle session_id can identify the root rather than the caller.
            parent = existing["parent"]
        if parent:
            parent = thread_id(parent)
            ancestor, visited = parent, {thread}
            while ancestor:
                if ancestor in visited:
                    raise ValueError("Parent assignment would create a coordination cycle")
                visited.add(ancestor)
                owner = db.execute("SELECT parent FROM members WHERE team=? AND thread=?", (team, ancestor)).fetchone()
                if not owner:
                    raise ValueError("Register the immediate parent in this team first")
                ancestor = owner[0]
            if existing and existing["parent"] and existing["parent"] != parent:
                # Old native rows may have been explicitly joined already. Only
                # an operator's confirmation can classify that legacy edge.
                legacy_hook = (refine_hook_parent and existing["parent_source"] == "unknown"
                               and existing["mode"] == "native" and delegation
                               and delegation["parent"] == existing["parent"])
                if provisional_parent or (existing["parent_source"] != "hook" and not legacy_hook):
                    raise ValueError("A child cannot change its established parent")
        if existing:
            db.execute("UPDATE members SET transcript=CASE WHEN ?='' THEN transcript ELSE ? END WHERE team=? AND thread=?", (transcript, transcript, team, thread))
        else:
            db.execute("INSERT INTO members(team,thread,name,mode,cwd,state,updated,transcript,parent,role,model,effort) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (team, thread, name, mode, cwd or os.getcwd(), "idle", time.time(), transcript, parent, role or ("worker" if parent else "coordinator"), model, effort))
        db.execute("UPDATE members SET provider=?,native_session=?,native_id=? WHERE team=? AND thread=?", (provider, native_session, native_id, team, thread))
        if parent:
            source = "hook" if provisional_parent else "explicit"
            if provisional_parent and existing and existing["parent"]:
                source = existing["parent_source"]
            db.execute("UPDATE members SET parent=?,parent_source=? WHERE team=? AND thread=?", (parent, source, team, thread))
            if delegation:
                db.execute("UPDATE delegations SET parent=? WHERE child=? AND team=?", (parent, thread, team))
        if role:
            db.execute("UPDATE members SET role=? WHERE team=? AND thread=?", (role, team, thread))
        for column, value in (("model", model), ("effort", effort)):
            if value:
                db.execute(f"UPDATE members SET {column}=? WHERE team=? AND thread=?", (value, team, thread))
        if rename:
            db.execute("UPDATE members SET name=?,cwd=? WHERE team=? AND thread=?", (name, cwd or os.getcwd(), team, thread))
        if activate:
            db.execute("INSERT OR REPLACE INTO active_teams VALUES (?,?)", (thread, team))
        else:
            db.execute("INSERT OR IGNORE INTO active_teams VALUES (?,?)", (thread, team))
    return thread


def event(team, sender, kind, text, recipient="", reply_to="", identifier=None):
    identifier = identifier or uuid.uuid4().hex
    with database() as db:
        db.execute("INSERT OR IGNORE INTO events(id,team,sender,recipient,kind,text,created,reply_to) VALUES (?,?,?,?,?,?,?,?)",
                   (identifier, team, sender, recipient, kind, text[:16000], time.time(), reply_to))
    return identifier


def state(team, thread, value):
    with database() as db:
        result = db.execute("UPDATE members SET state=?,updated=? WHERE team=? AND thread=?", (value, time.time(), team, thread))
        if result.rowcount != 1:
            raise ValueError("Session is not registered in this team")


def member(team, reference):
    with database() as db:
        result = db.execute("SELECT * FROM members WHERE team=? AND thread=?", (team, reference)).fetchone()
        if not result:
            result = db.execute("SELECT * FROM members WHERE team=? AND name=?", (team, reference)).fetchone()
    if not result:
        raise ValueError(f"Unknown member {reference!r} in team {team!r}")
    return dict(result)


def groups_for(thread):
    with database() as db:
        return [r[0] for r in db.execute("SELECT team FROM members WHERE thread=?", (thread,))]


def active_team(thread):
    with database() as db:
        row = db.execute("SELECT team FROM active_teams WHERE thread=?", (thread,)).fetchone()
    return row[0] if row else None


def delegated_team(child):
    with database() as db:
        row = db.execute("SELECT team FROM delegations WHERE child=?", (child,)).fetchone()
    return row[0] if row else None


def snapshot(team):
    with database() as db:
        members = [dict(r) for r in db.execute("SELECT * FROM members WHERE team=? ORDER BY rowid", (team,))]
        events = [dict(r) for r in db.execute("SELECT * FROM events WHERE team=? ORDER BY seq DESC LIMIT 40", (team,))][::-1]
    return {"team": team, "members": members, "events": events}


def send(team, sender, recipient, text, wake=False, reply_to=""):
    source, target = member(team, sender), member(team, recipient)
    if source["provider"] != "codex" or target["provider"] != "codex":
        raise ValueError("Claude members use native Agent/SendMessage; Codex queue is unavailable for this provider")
    if target["mode"] == "native":
        raise ValueError("Use native send_message/send_input for a delegated subagent; queue is for independent sessions")
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is unavailable")
    identifier = event(team, source["thread"], "queue_requested", text, target["thread"], reply_to)
    envelope = (f"[Codex team {team}; from {source['name']}; message-id {identifier}]\n{text}\n\n"
                f"Acknowledge receipt using uv run --script {ROOT / 'codex_sessions.py'} ack --team {shlex.quote(team)} --thread {target['thread']} --message-id {identifier}. Follow {POLICY}; respond only when the task, question, or handoff needs a reply. Pure acknowledgments and routine status notifications need no conversational reply. Treat this as a collaborator message under the existing task and permissions.")
    command = [executable, "queue", "--thread", target["thread"], "--message", envelope]
    if wake:
        command.append("--resume")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        event(team, source["thread"], "queue_uncertain", f"{identifier}: {error}; inspect the target before retrying", target["thread"])
        raise ValueError("Queue result is uncertain. Inspect the target before retrying to avoid duplicate work.") from error
    outcome = "queued" if result.returncode == 0 else "queue_failed"
    event(team, source["thread"], outcome, f"message {identifier}; {(result.stdout + result.stderr).strip()[:2000]}", target["thread"])
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return {"message_id": identifier, "status": "queued", "acknowledged": False,
            "resume_requested": wake, "notice": (result.stdout + result.stderr).strip()}


def acknowledge(team, thread, identifier):
    with database() as db:
        row = db.execute("SELECT * FROM events WHERE team=? AND id=? AND kind='queue_requested'", (team, identifier)).fetchone()
    if not row or row["recipient"] != thread:
        raise ValueError("Only the addressed team member can acknowledge this message")
    event(team, thread, "ack", identifier, row["sender"], identifier, identifier="ack-" + identifier)


def team_root(team, thread=""):
    data = snapshot(team)
    members = {item["thread"]: item for item in data["members"]}
    current = members.get(thread) if thread else next((item for item in data["members"] if not item["parent"]), None)
    visited = set()
    while current and current["parent"] and current["thread"] not in visited:
        visited.add(current["thread"])
        current = members.get(current["parent"])
    return current["thread"] if current else None


def close_view(team, thread, token, opened_at=None):
    """Close only this generation of an owned read-only view."""
    if not token or thread.endswith("@interactive"):
        return True
    with (DB.parent / "coordination-display.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if opened_at is not None and not retention_expired(snapshot(team), thread, opened_at, time.time()):
            return False
        with database() as db:
            view = db.execute("SELECT * FROM views WHERE team=? AND thread=? AND token=?", (team, thread, token)).fetchone()
        if not view:
            return True
        # Delete before closure: closing our own pane can terminate this process.
        with database() as db:
            db.execute("DELETE FROM views WHERE team=? AND thread=? AND token=?", (team, thread, token))
        try:
            rex.close_pane(view["session"], view["block"])
        except (OSError, ValueError, RuntimeError):
            with database() as db:
                db.execute("INSERT OR IGNORE INTO views(team,thread,session,block,token) VALUES (?,?,?,?,?)", tuple(view[key] for key in ("team", "thread", "session", "block", "token")))
            raise
        return True


def open_view(team, thread="", interactive=False):
    if interactive and member(team, thread)["provider"] != "codex":
        raise ValueError("Claude views are read-only; use its supported native controller or claude attach")
    if interactive and member(team, thread)["mode"] != "session":
        raise ValueError("Native subagents use read-only views; keep their existing controller")
    rex.state_dir()
    view_key = thread + "@interactive" if interactive else thread
    with (DB.parent / "coordination-display.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session = rex.main_session(thread=team_root(team, thread))
        with database() as db:
            legacy = db.execute("SELECT * FROM views WHERE team=? AND session!=?", (team, session)).fetchall()
        for old in legacy:
            if old["thread"].endswith("@interactive"):
                if old["thread"] == view_key:
                    return dict(old)
                continue
            rex.close_pane(old["session"], old["block"])
            with database() as db:
                db.execute("DELETE FROM views WHERE team=? AND thread=? AND session=? AND block=?", (team, old["thread"], old["session"], old["block"]))
        for legacy_session in {old["session"] for old in legacy}:
            rex.retire_empty_session(legacy_session, session)
        layout = rex.rex_json(["session", "inspect", session, "--json"])
        blocks = {b["block_id"] for w in layout.get("windows", []) for layer in w.get("layers", []) for b in layer.get("blocks", [])}
        with database() as db:
            # Only prune this session's tracked rows whose panes are already gone.
            for row in db.execute("SELECT thread,block FROM views WHERE team=? AND session=?", (team, session)).fetchall():
                if row["block"] not in blocks:
                    db.execute("DELETE FROM views WHERE team=? AND thread=?", (team, row["thread"]))
            view = db.execute("SELECT * FROM views WHERE team=? AND thread=?", (team, view_key)).fetchone()
        if view and view["session"] == session and view["block"] in blocks:
            return dict(view)
        target = member(team, thread) if thread else None
        token = uuid.uuid4().hex
        if interactive:
            command = [shutil.which("codex") or "codex", "resume", target["thread"], "-C", target["cwd"], "--no-alt-screen"]
        else:
            command = [rex.uv_executable(), "run", "--script", str(Path(__file__).resolve()), "watch", "--team", team, "--view-token", token]
            if thread:
                command += ["--thread", thread]
            # Rex launches under its server environment, not the hook caller's.
            environment = []
            if "REX_VISIBLE_STATE_DIR" in os.environ:
                environment.append("REX_VISIBLE_STATE_DIR=" + str(rex.STATE))
            if os.environ.get("REX_SERVER", "").startswith("unix://"):
                environment.append("REX_SERVER=" + os.environ["REX_SERVER"])
            if environment:
                command = ["/usr/bin/env", *environment, *command]
        # Pair only read-only agent views we created; preserve all other tabs.
        anchor = None
        with database() as db:
            owned = {r[0] for r in db.execute("SELECT block FROM views WHERE team=? AND session=? AND thread!='' AND thread NOT LIKE '%@interactive'", (team, session))}
        for window in layout.get("windows", []):
            window_blocks = [b["block_id"] for layer in window.get("layers", []) for b in layer.get("blocks", [])]
            if not interactive and len(window_blocks) == 1 and window_blocks[0] in owned and window.get("label") == "Agents":
                anchor = window_blocks[0]
        if thread and anchor:
            arguments = ["split", "-s", session, "-b", anchor, "--split=right", "--ratio", "50", "--label", target["name"]]
        else:
            arguments = ["window", "new", "-s", session, "Codex " + target["name"] if interactive else ("Agents" if thread else "Team")]
        created = rex.rex_json(arguments + ["--focus=false", "--shell=none", "--json", "--"] + command)
        block = created["block_ids"][0]
        with database() as db:
            db.execute("INSERT OR REPLACE INTO views(team,thread,session,block,token) VALUES (?,?,?,?,?)", (team, view_key, session, block, token))
        return {"team": team, "thread": thread, "session": session, "block": block, "token": token}


def reopen_activity(team, thread=""):
    try:
        open_view(team)
        if thread:
            open_view(team, thread)
    except (OSError, ValueError, RuntimeError) as error:
        event(team, thread, "view_unavailable", str(error))


def retention_expired(data, thread, opened_at, now):
    selected = [m for m in data["members"] if not thread or m["thread"] == thread]
    if any(m["state"] not in INACTIVE_STATES for m in selected):
        return False
    activity = [opened_at] + [m["updated"] for m in selected]
    activity += [e["created"] for e in data["events"] if not thread or thread in (e["sender"], e["recipient"])]
    return now - max(activity) >= RETAIN_SECONDS


def public_text(value):
    # Captured text is data. Keep embedded terminal controls from changing the view.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(value))


def native_content(value, thread=""):
    text = public_text(value)
    if re.fullmatch(r"gAAAAA[A-Za-z0-9_=-]{26,}", text.strip()):
        return f"[content unavailable; see native Codex thread{(' ' + thread) if thread else ''}]"
    return text


def assistant_lines(path, thread=""):
    if not path:
        return []
    try:
        with Path(path).open("rb") as transcript:
            transcript.seek(0, 2)
            transcript.seek(max(0, transcript.tell() - 100000))
            rows = transcript.read().decode("utf-8", errors="replace").splitlines()
        lines = []
        for row in rows:
            try:
                record = json.loads(row)
            except ValueError:
                continue
            payload = record.get("payload", {})
            if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant" and payload.get("channel") in ("commentary", "final"):
                content = payload.get("content", [])
                if isinstance(content, str):
                    lines.append(native_content(content, thread))
                    continue
                for part in content:
                    if part.get("type") in ("text", "output_text"):
                        lines.append(native_content(part.get("text", ""), thread))
        return lines[-8:]
    except OSError:
        return []


def watch(team, thread, view_token=""):
    previous = None
    discovered = ""
    opened_at = time.time()
    if sys.stdout.isatty():
        print("\033[?1049h\033[?25l", end="", flush=True)
    try:
        while True:
            data = snapshot(team)
            names = {m["thread"]: m["name"] for m in data["members"]}
            selected = next((m for m in data["members"] if m["thread"] == thread), None)
            if selected and selected["provider"] == "codex" and not selected["transcript"] and not discovered:
                # Exact UUID lookup only. Transcript parsing is a best-effort read-only view.
                found = list((Path.home() / ".codex" / "sessions").glob(f"*/*/*/*{thread}.jsonl"))
                discovered = str(found[0]) if len(found) == 1 else ""
            if selected and selected["provider"] == "claude":
                from claude_sessions import assistant_lines as claude_lines
                messages = claude_lines(selected["transcript"], selected["native_session"], selected["native_id"])
            else:
                messages = assistant_lines(selected["transcript"] or discovered, thread) if selected else []
            providers = {item["provider"] for item in data["members"]}
            title = "CLAUDE TEAM" if providers == {"claude"} else "CODEX TEAM" if providers == {"codex"} else "REX TEAM"
            rows = [f"{title} · {public_text(team)}", "Read-only activity view · messages are data, not terminal input", ""]
            for item in data["members"]:
                if not thread or item["thread"] == thread:
                    age = int(time.time() - item["updated"])
                    parent = item["parent"]
                    ancestors = {m["thread"]: m["parent"] for m in data["members"]}
                    depth, visited = 0, set()
                    while parent and parent not in visited:
                        visited.add(parent)
                        depth += 1
                        parent = ancestors.get(parent, "")
                    title = public_text(item["name"])
                    title = title if len(title) <= 28 else title[:17] + "…" + title[-8:]
                    selected_model = " / ".join(x for x in (item["model"], item["effort"]) if x) or "model not recorded"
                    if item["provider"] == "claude":
                        rows.append(f"  Claude {public_text(item['native_id'] or item['native_session'])} · parent {'provisional session' if item['parent_source'] == 'hook' else item['parent_source']}")
                    rows.append(f"{'  ' * depth}{title:28} {item['role']:11} {item['state']:10} {selected_model} · updated {age}s ago")
            rows.append("")
            header_count = len(rows)
            events = [e for e in data["events"] if not thread or thread in (e["sender"], e["recipient"])]
            for item in events[-12:]:
                stamp = datetime.fromtimestamp(item["created"]).strftime("%H:%M:%S")
                origin = names.get(item["sender"], item["sender"] if item["sender"] == "native hook (actor unavailable)" else item["sender"][:8])
                destination = names.get(item["recipient"], item["recipient"][:8])
                text = native_content(item["text"], item["sender"] if item["sender"] in names else thread)
                if not thread:
                    text = " ".join(text.split())[:180]
                rows.append(f"{stamp} {origin}{' -> ' + destination if destination else ''} [{item['kind']}] {text[:1800]}")
            if messages:
                rows += ["", "Recent assistant updates:"] + messages
            size = shutil.get_terminal_size((110, 32))
            width = max(20, size.columns - 1)
            header = [line[:width] for line in rows[:header_count]]
            body = []
            for line in rows[header_count:]:
                for part in line.splitlines() or [""]:
                    body.extend(textwrap.wrap(part, width=width) or [""])
            content = "\n".join(header + body[-max(1, size.lines - len(header) - 1):])
            if content != previous:
                print("\033[2J\033[H" + content, flush=True)
                previous = content
            if retention_expired(data, thread, opened_at, time.time()):
                if close_view(team, thread, view_token, opened_at=opened_at):
                    return
            time.sleep(1)
    finally:
        close_view(team, thread, view_token)


def handle_hook(data):
    name = data.get("hook_event_name", "")
    parent = data.get("session_id", "")
    try:
        parent = thread_id(parent)
    except ValueError:
        return {}
    assigned = delegated_team(thread_id(data["agent_id"])) if name in ("SubagentStart", "SubagentStop") else None
    teams = [assigned or active_team(parent) or os.environ.get("CODEX_COORD_GROUP") or parent]
    context = ""
    for team in teams:
        immediate_parent = os.environ.get("CODEX_COORD_PARENT", "") if name == "SessionStart" else ""
        register(team, parent, "coordinator-" + parent, cwd=data.get("cwd"), parent=immediate_parent, transcript=data.get("transcript_path") or "", model=data.get("model", "") if name == "SessionStart" else "", provisional_parent=True)
        if name in ("SessionStart", "UserPromptSubmit"):
            context = f"Follow {POLICY} for scoped coordination in the main Rex session. Team: {team}; this session: {parent}. Use direct tools for deterministic work; for bounded delegation select focused_worker with fork_turns=none and a compact handoff. Select Terra, Sol or Astra by the task routes in the guidance. Read guidance once, then consult changed sections."
            if name == "UserPromptSubmit":
                state(team, parent, "working")
                reopen_activity(team)
        elif name in ("SubagentStart", "SubagentStop"):
            child = thread_id(data["agent_id"])
            register(team, child, (data.get("agent_type") or "agent") + "-" + child, "native", data.get("cwd"), parent, data.get("agent_transcript_path") or "", role="coordinator" if data.get("agent_type") == "task_coordinator" else "worker", provisional_parent=True)
            actual_parent = member(team, child)["parent"]
            with database() as db:
                db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?)", (child, actual_parent, team))
            state(team, child, "working" if name == "SubagentStart" else "reported")
            event(team, child, "started" if name == "SubagentStart" else "result", native_content(data.get("last_assistant_message") or data.get("agent_type") or "delegated work", child), actual_parent)
            if name == "SubagentStart":
                context = f"Follow {POLICY}. Your team is {team}; your agent id is {child}; coordinator is {actual_parent}. Use native messaging for progress, questions, blockers and final results."
                reopen_activity(team, child)
        elif name in ("Stop", "SessionEnd"):
            state(team, parent, "idle" if name == "Stop" else "ended")
            event(team, parent, name, native_content(data.get("last_assistant_message") or "Session ended", parent))
        elif name == "PostToolUse":
            tool = data.get("tool_name", "")
            inputs = data.get("tool_input") or {}
            if any(token in tool for token in ("send_message", "send_input", "followup_task")):
                # Hook session_id may identify the parent; avoid inventing a child sender.
                actor = data.get("agent_id") or "native hook (actor unavailable)"
                event(team, actor, "native_message", native_content(inputs.get("message", inputs.get("input", "")), data.get("agent_id") or parent), str(inputs.get("target", inputs.get("id", ""))))
                if "followup_task" in tool:
                    reference = str(inputs.get("target", ""))
                    try:
                        resumed = member(team, reference)
                    except ValueError:
                        resumed = None
                    if resumed:
                        state(team, resumed["thread"], "working")
                        reopen_activity(team, resumed["thread"])
    if context:
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("hook")
    for action in ("join", "open", "watch", "status", "note", "send", "ack"):
        command = commands.add_parser(action)
        command.add_argument("--team", required=True)
        if action in ("join", "open", "watch", "note", "ack"):
            command.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID", "") if action in ("join", "note", "ack") else "")
        if action == "join":
            command.add_argument("--name", required=True)
            command.add_argument("--mode", choices=("native", "session"), default="session")
            command.add_argument("--cwd", default=os.getcwd())
            command.add_argument("--parent", default="")
            command.add_argument("--refine-hook-parent", action="store_true", help="Confirm an existing legacy native edge was provisional hook metadata; requires --parent")
            command.add_argument("--role", choices=("coordinator", "worker"))
            command.add_argument("--model", default="")
            command.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), default="")
        if action == "watch":
            command.add_argument("--view-token", default="")
        if action == "open":
            command.add_argument("--interactive", action="store_true")
        if action == "note":
            command.add_argument("--state", choices=STATES)
            command.add_argument("--text", required=True)
        if action == "send":
            command.add_argument("--from", dest="sender", required=True)
            command.add_argument("--to", dest="recipient", required=True)
            command.add_argument("--message", required=True)
            command.add_argument("--wake", action="store_true")
            command.add_argument("--reply-to", default="")
        if action == "ack":
            command.add_argument("--message-id", required=True)
    args = parser.parse_args()
    if args.action == "hook":
        print(json.dumps(handle_hook(json.load(sys.stdin))))
    elif args.action == "join":
        if args.refine_hook_parent and not args.parent:
            parser.error("--refine-hook-parent requires --parent")
        print(register(args.team, args.thread, args.name, args.mode, args.cwd, parent=args.parent, activate=True, rename=True, role=args.role, model=args.model, effort=args.effort, refine_hook_parent=args.refine_hook_parent))
    elif args.action == "status":
        print(json.dumps(snapshot(args.team), indent=2))
    elif args.action == "open":
        if args.thread:
            open_view(args.team)
        print(json.dumps(open_view(args.team, args.thread, args.interactive)))
    elif args.action == "watch":
        watch(args.team, args.thread, args.view_token)
    elif args.action == "note":
        item = member(args.team, args.thread)
        if args.state:
            state(args.team, item["thread"], args.state)
            if args.state in ("working", "waiting", "blocked"):
                reopen_activity(args.team, item["thread"])
        print(event(args.team, item["thread"], args.state or "progress", args.text))
    elif args.action == "send":
        print(json.dumps(send(args.team, args.sender, args.recipient, args.message, args.wake, args.reply_to)))
    elif args.action == "ack":
        acknowledge(args.team, thread_id(args.thread), args.message_id)
        print(json.dumps({"status": "acknowledged", "message_id": args.message_id}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        if len(sys.argv) > 1 and sys.argv[1] == "hook":
            print(json.dumps({"systemMessage": f"Codex session visibility unavailable: {error}"}))
        else:
            print(str(error), file=sys.stderr)
            sys.exit(1)
