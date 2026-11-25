#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Claude lifecycle adapter: shared Rex visibility, native Claude coordination."""
import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from pathlib import Path

import codex_sessions as store

POLICY = Path(__file__).with_name("COORDINATION.md")
MemberState = store.MemberState
HookEvent = store.grokbot_callback.HookEvent
AgentKind = store.grokbot_callback.AgentKind


def native_id(value):
    if not isinstance(value, str) or not value or len(value) > 256 or re.search(r"[\x00-\x1f\x7f-\x9f]", value):
        raise ValueError("Claude session and agent identifiers must be nonempty opaque strings without control characters")
    return value


def identity(session, agent=""):
    session = native_id(session)
    if agent:
        native_id(agent)
    # The shared provider-namespaced key is internal, never a Claude resume identifier.
    return store.grokbot_callback.actor_key(AgentKind.CLAUDE, session, agent)


def register(team, session, agent="", parent_agent=None, cwd=None, transcript="", model="", effort="", name="", provisional=True, parent_session=None, role=None):
    root = identity(session)
    actor = identity(session, agent)
    assigned = store.delegated_team(actor) if agent else None
    if assigned and assigned != team:
        raise ValueError("A delegated Claude agent cannot move to another team")
    if agent and parent_session:
        raise ValueError("--parent-session applies to an independent Claude session; use --parent-agent for a native child")
    root_parent = identity(parent_session, parent_agent or "") if parent_session else ""
    if root_parent:
        store.member(team, root_parent)
    elif parent_agent:
        store.member(team, identity(session, parent_agent))
    store.register(team, root, "claude-" + session, cwd=cwd, provider=AgentKind.CLAUDE, native_session=session, model=model if not agent else "",
                   parent=root_parent, provisional_parent=provisional, role=(role or "coordinator") if not agent else None)
    if root_parent:
        with store.database() as db:
            db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?)", (root, store.member(team, root)["parent"], team))
    actor = identity(session, agent)
    if agent:
        parent = identity(session, parent_agent) if parent_agent else root
        if parent_agent and not store.groups_for(parent):
            raise ValueError("Register the immediate Claude parent in this team first")
        store.register(team, actor, (name or "agent") + "-" + agent, mode="native", cwd=cwd, parent=parent,
                       transcript=transcript, model=model, effort=effort, provisional_parent=provisional, role=role or "worker",
                       provider=AgentKind.CLAUDE, native_session=session, native_id=agent)
        with store.database() as db:
            db.execute("INSERT OR IGNORE INTO delegations VALUES (?,?,?)", (actor, store.member(team, actor)["parent"], team))
    elif transcript:
        store.register(team, root, "claude-" + session, cwd=cwd, transcript=transcript, model=model, effort=effort,
                       provider=AgentKind.CLAUDE, native_session=session)
    return actor


def assistant_lines(path, session="", agent=""):
    """Parse only explicit Claude JSONL; never guess a subagent transcript from root text."""
    if not path:
        return []
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 100000))
            rows = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    result = []
    for row in rows:
        try:
            record = json.loads(row)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "assistant" or (record.get("sessionId") and record["sessionId"] != session):
            continue
        if record.get("agentId") and record["agentId"] != agent:
            continue
        message = record.get("message") or {}
        if not isinstance(message, dict):
            continue
        for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "text":
                result.append(store.public_text(part.get("text", "")))
    return result[-8:]


def bind_environment(session, actor, team):
    """SessionStart's documented environment file carries identity into later Bash jobs."""
    path = os.environ.get("CLAUDE_ENV_FILE")
    if path:
        with Path(path).open("a") as stream:
            stream.writelines(f"export {key}={shlex.quote(value)}\n" for key, value in (("REX_COORD_PROVIDER", AgentKind.CLAUDE), ("REX_COORD_SESSION", session),
                               ("REX_COORD_THREAD", actor), ("REX_COORD_TEAM", team)))
            if os.environ.get("GROKBOT_CALLBACK"):
                stream.write(f"export GROKBOT_PARENT_SESSION={shlex.quote(session)}\n")


def handle_hook(data):
    try:
        return lifecycle_hook(data)
    finally:
        if isinstance(data, dict):
            try:
                store.callback_context(data, AgentKind.CLAUDE)
            except Exception:  # noqa: BLE001, S110 - fail open without logging callback exception text.
                pass


def lifecycle_hook(data):
    if not isinstance(data, dict):
        raise TypeError("Claude hook input must be a JSON object")
    name = data.get("hook_event_name", "")
    callback = store.grokbot_callback
    if callback.input_request(data, AgentKind.CLAUDE) and callback.input_blocked(
        AgentKind.CLAUDE, data
    ):
        return {}
    session = native_id(data.get("session_id"))
    root = identity(session)
    agent = data.get("agent_id") or ""
    actor = identity(session, agent)
    # A native agent already belongs to its recorded task even if the coordinator switches tasks.
    callback_parent = store.callback_parent_member(os.environ.get("GROKBOT_PARENT_SESSION", ""))
    team = store.delegated_team(actor) or store.active_team(root) or (callback_parent["team"] if callback_parent else "") or os.environ.get("REX_COORD_TEAM") or "claude-" + session
    effort = data.get("effort") or {}
    effort = effort.get("level", "") if isinstance(effort, dict) else ""
    transcript = data.get("agent_transcript_path", "") if agent else data.get("transcript_path", "")
    register(team, session, agent, cwd=data.get("cwd"), transcript=transcript,
             model=data.get("model", "") if name == HookEvent.SESSION_START else data.get("to_model", "") if name == HookEvent.POST_MODEL_SWITCH else "",
             effort=effort, name=data.get("agent_type", ""))
    context = ""
    if name == HookEvent.SESSION_START:
        if callback_parent and not agent and callback_parent["thread"] != root:
            store.register(team, root, "claude-" + session, cwd=data.get("cwd"), parent=callback_parent["thread"],
                           provider=AgentKind.CLAUDE, native_session=session)
        inherited = os.environ.get("REX_COORD_THREAD", "")
        inherited_session = os.environ.get("REX_COORD_SESSION", "")
        if not agent and inherited and inherited != root and inherited_session != session:
            try:
                parent = store.member(team, inherited)
            except ValueError:
                parent = None
            if parent and parent["provider"] == AgentKind.CLAUDE and parent["native_session"] == inherited_session:
                register(team, session, cwd=data.get("cwd"), transcript=transcript, parent_session=parent["native_session"],
                         parent_agent=parent["native_id"] or None, model=data.get("model", ""), provisional=True)
        bind_environment(session, actor, team)
    if name in (HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT, HookEvent.SUBAGENT_START):
        context = (f"Follow {POLICY}. Provider: Claude; native session: {session}; native agent: {agent or 'main'}; "
                   f"Rex team: {team}; internal view key: {actor}. Use Claude's native Agent and SendMessage tools when available. "
                   "Rex records read-only activity; it does not deliver Claude messages or grant permissions. "
                   "Session-level hook parentage is provisional when the immediate caller is unavailable.")
    if name in (HookEvent.USER_PROMPT_SUBMIT, HookEvent.SUBAGENT_START, HookEvent.PRE_TOOL_USE):
        store.state(team, actor, MemberState.WORKING)
        if name != HookEvent.PRE_TOOL_USE:
            store.reopen_activity(team, actor if agent else "")
    elif name == HookEvent.SUBAGENT_STOP:
        store.state(team, actor, MemberState.REPORTED)
    elif name == HookEvent.STOP:
        pending = data.get("background_tasks") or data.get("session_crons")
        store.state(team, actor, MemberState.WAITING if pending else MemberState.IDLE)
    elif name == HookEvent.TEAMMATE_IDLE:
        # Older clients may omit agent_id: preserve the root state instead of attributing a teammate's idleness to it.
        if agent:
            store.state(team, actor, MemberState.IDLE)
    elif name == HookEvent.SESSION_END:
        store.state(team, actor, MemberState.ENDED)
        if not agent:
            with store.database() as db:
                active_states = (MemberState.WORKING, MemberState.WAITING, MemberState.BLOCKED)
                db.execute("UPDATE members SET state=?,updated=? WHERE provider=? AND native_session=? AND state IN (?,?,?)", (MemberState.ENDED, time.time(), AgentKind.CLAUDE, session, *active_states))
    elif name == HookEvent.STOP_FAILURE:
        store.state(team, actor, MemberState.BLOCKED)
    elif callback.input_request(data, AgentKind.CLAUDE):
        store.state(team, actor, MemberState.WAITING)
    text = data.get("last_assistant_message") or ""
    if name == HookEvent.TEAMMATE_IDLE:
        text = f"Teammate {data.get('teammate_name', agent or 'unknown')} idle"
    elif name == HookEvent.SESSION_END:
        text = str(data.get("reason") or "Session ended")
    if name in (HookEvent.TASK_CREATED, HookEvent.TASK_COMPLETED):
        text = f"{data.get('task_id', '')}: {data.get('task_subject', '')}"
    elif name == HookEvent.POST_TOOL_USE:
        if store.member(team, actor)["state"] == MemberState.WAITING:
            store.state(team, actor, MemberState.WORKING)
        tool = data.get("tool_name", "")
        # Observe supported native tools, without writing mailbox files or invoking a Codex queue.
        if tool == "SendMessage":
            inputs = data.get("tool_input") or {}
            text = f"Native SendMessage to {inputs.get('recipient', inputs.get('to', 'unknown'))}"
        elif tool in ("Agent", "Task", "TaskCreate", "TaskUpdate"):
            text = tool
        else:
            return {}
    elif name == HookEvent.PRE_TOOL_USE:
        return {}
    if name == HookEvent.STOP:
        background = [{key: item[key] for key in ("id", "type", "status", "agent_type", "name") if key in item}
                      for item in data.get("background_tasks", []) if isinstance(item, dict)]
        if background or data.get("session_crons"):
            text += "\nBackground work: " + json.dumps(background) + f"; scheduled wakeups: {len(data.get('session_crons') or [])}"
    if name == HookEvent.STOP_FAILURE:
        text = str(data.get("error", "Claude API failure"))
    unattributed = not agent and (name == HookEvent.TEAMMATE_IDLE or (name in (HookEvent.TASK_CREATED, HookEvent.TASK_COMPLETED) and data.get("teammate_name")))
    store.event(team, "Claude hook (teammate identity unavailable)" if unattributed else actor, name,
                store.public_text(text or data.get("agent_type") or name), store.member(team, actor)["parent"] if agent else "")
    if context and os.environ.get("GROKBOT_CALLBACK"):
        context += (" For a nested Codex or Claude CLI, explicitly set GROKBOT_PARENT_SESSION="
                    + (agent or session) + " in its launch environment; preserve GROKBOT_TASK_ID and GROKBOT_CALLBACK.")
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}} if context else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("hook")
    for action in ("join", "open", "status", "note"):
        command = commands.add_parser(action)
        command.add_argument("--team", required=True)
        command.add_argument("--session", default=os.environ.get("REX_COORD_SESSION", ""))
        command.add_argument("--agent", default="")
        if action == "join":
            command.add_argument("--parent-agent", default=None)
            command.add_argument("--parent-session", default=None)
            command.add_argument("--role", choices=("coordinator", "worker"))
            command.add_argument("--cwd", default=os.getcwd())
            command.add_argument("--name", default="agent")
            command.add_argument("--model", default="")
        if action == "note":
            command.add_argument("--state", choices=store.STATES)
            command.add_argument("--text", required=True)
    args = parser.parse_args()
    if args.action == "hook":
        print(json.dumps(handle_hook(json.load(sys.stdin))))
        return
    if args.action == "status":
        print(json.dumps(store.snapshot(args.team), indent=2))
        return
    actor = identity(args.session, args.agent)
    if args.action == "join":
        actor = register(args.team, args.session, args.agent, args.parent_agent, args.cwd, model=args.model, name=args.name, provisional=False, parent_session=args.parent_session, role=args.role)
        with store.database() as db:
            db.execute("INSERT OR REPLACE INTO active_teams VALUES (?,?)", (identity(args.session), args.team))
        print(actor)
    elif args.action == "open":
        print(json.dumps(store.open_view(args.team, actor if args.agent else "")))
    else:
        store.member(args.team, actor)
        if args.state:
            store.state(args.team, actor, args.state)
        print(store.event(args.team, actor, args.state or "progress", args.text))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as error:
        if len(sys.argv) > 1 and sys.argv[1] == "hook":
            print(json.dumps({"systemMessage": f"Claude Rex visibility unavailable: {error}"}))
        else:
            print(str(error), file=sys.stderr)
            sys.exit(1)
