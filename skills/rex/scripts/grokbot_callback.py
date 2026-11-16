#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in, durable lifecycle webhooks for Grok Bot."""
import argparse
import fcntl
import hashlib
import http.client
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import rex_visible as rex

CONFIG = Path.home() / ".config/agent-tooling/grokbot-callback.env"


def receipt(outcome, **fields):
    """Only fixed diagnostics and delivery metadata, never exception/config text."""
    rex.state_dir()
    row = {"finished_at": datetime.now().astimezone().isoformat(), "outcome": outcome, **fields}
    with (rex.STATE / "grokbot-callback-receipts.jsonl").open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def config():
    with CONFIG.open() as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError("Config must be an owned regular file with mode 600")
        values = {}
        for line in stream:
            key, separator, value = line.rstrip("\r\n").partition("=")
            if separator and key in ("GROKBOT_WEBHOOK_URL", "GROKBOT_WEBHOOK_HEADER"):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    url = urlsplit(values.get("GROKBOT_WEBHOOK_URL", ""))
    header = values.get("GROKBOT_WEBHOOK_HEADER", "")
    name, separator, value = header.partition(":")
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or not separator or not name or not value.strip() or "\r" in header or "\n" in header):
        raise ValueError("Invalid callback configuration")
    return url, name, value.removeprefix(" ")


def waiting(data):
    return bool(data.get("background_tasks") or data.get("session_crons"))


def now():
    return datetime.now().astimezone().isoformat()


@contextmanager
def database():
    rex.state_dir()
    with closing(sqlite3.connect(rex.STATE / "grokbot-callback.sqlite3", timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, record TEXT)")
        yield db


def actor_key(agent, session, child=""):
    if agent == "codex":
        return child or session
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["claude", session, child], separators=(",", ":"))))


def git_metadata(cwd):
    def git(*args):
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1, check=False)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return {"repo": git("rev-parse", "--show-toplevel"), "branch": git("branch", "--show-current"),
            "head_sha": git("rev-parse", "HEAD")}


def input_request(data):
    name = data.get("hook_event_name")
    notification = data.get("notification_type", "")
    if name == "PermissionRequest" or (name == "Notification" and notification == "permission_prompt"):
        kind = "approval"
    elif name == "Notification" and notification in ("idle_prompt", "elicitation_dialog"):
        kind = "question"
    elif name == "Notification" and notification == "trust_prompt":
        kind = "trust"
    elif name == "Notification" and notification == "input_prompt":
        kind = "other"
    else:
        return None
    inputs = data.get("tool_input") or {}
    prompt = data.get("message") or data.get("prompt") or (inputs.get("description") if isinstance(inputs, dict) else "") or inputs
    if not isinstance(prompt, str):
        prompt = json.dumps(prompt, ensure_ascii=False)
    return kind, prompt[:1000]


def lineage(item, sessions):
    root = sessions.get(item["root"], item)
    parent = sessions.get(item["parent"])
    return {"requester_bot": item["requester"], "root_session_id": root["id"],
            "parent_session_id": parent["id"] if parent else "", "agent_session_id": item["id"],
            "depth": item["depth"], "agent_kind": item["agent"], "role": "subagent" if item["parent"] else "agent"}


def sync_sessions(sessions, members, current_key=""):
    """Use coordination's refined immediate edges, never infer delegation from cwd."""
    members_by_key = {m["thread"]: m for m in members}
    visited = set()

    def sync(key, ancestors):
        if key in visited or key in ancestors:
            return
        member = members_by_key.get(key)
        if not member or not member.get("parent"):
            return
        sync(member["parent"], ancestors | {key})
        parent = sessions.get(member["parent"])
        if not parent or member["parent"] in ancestors:
            return
        existing = sessions.get(key, {})
        # A delegated session keeps its original task when its controller switches.
        if existing and existing["task_id"] != parent["task_id"]:
            return
        status = {"working": "running", "waiting": "waiting", "blocked": "waiting",
                  "reported": "completed", "accepted": "completed", "idle": "completed", "ended": "ended"}.get(member.get("state"), "running")
        if key == current_key and existing:
            status = existing["status"]
        if existing.get("status") == "failed" and member.get("state") in ("blocked", "reported", "accepted", "idle"):
            status = "failed"
        sessions[key] = {**parent, **existing, "key": key,
                         "id": member.get("native_id") or member.get("native_session") or key,
                         "agent": member.get("provider", "codex"), "parent": member["parent"],
                         "root": parent["root"], "depth": parent["depth"] + 1, "status": status,
                         "native_turn": existing.get("native_turn", ""), "retired_turns": existing.get("retired_turns", [])}
        if status == "running" and existing.get("status") in ("completed", "failed", "ended"):
            # Native followup tools can resume a child without UserPromptSubmit.
            sessions[key]["turn"] = uuid.uuid4().hex
            sessions[key]["native_turn"] = ""
        visited.add(key)

    for key in members_by_key:
        sync(key, set())


def notify(agent, data, members=()):
    """Fail open: hooks only record local state and launch a detached HTTP worker."""
    try:
        _notify(agent, data, members)
    except Exception:  # noqa: BLE001 - every callback failure must leave the host hook open.
        try:
            receipt("enqueue_failed")
        except OSError:
            pass


def _notify(agent, data, members):
    name = data.get("hook_event_name")
    request = input_request(data)
    if name not in ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop", "StopFailure", "SessionEnd", "PostToolUse", "PreToolUse") and not request:
        return
    session = data.get("session_id")
    if not session or (name in ("SubagentStart", "SubagentStop") and not data.get("agent_id")):
        return
    child = data.get("agent_id") or ""
    key = actor_key(agent, session, child)
    # Ordinary sessions never create callback state, including inherited env-less roots.
    if not os.environ.get("GROKBOT_CALLBACK") and not (rex.STATE / "grokbot-callback.sqlite3").exists():
        return
    metadata = None
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        sessions = {k: json.loads(row) for k, row in db.execute("SELECT key,record FROM sessions")}
        sync_sessions(sessions, members)
        item = sessions.get(key)
        inherited = os.environ.get("GROKBOT_PARENT_SESSION", "")
        parent_key = actor_key(agent, session) if child else ""
        if not child and inherited:
            matches = [k for k, row in sessions.items() if row["id"] == inherited or k == inherited]
            parent_key = matches[0] if len(matches) == 1 else "unknown-parent"
        parent = sessions.get(parent_key)
        if item and item["parent"]:
            parent = sessions.get(item["parent"])
        if not item:
            if parent:
                item = {**parent, "key": key, "id": child or session, "agent": agent, "parent": parent["key"],
                        "depth": parent["depth"] + 1, "status": "running", "native_turn": "", "retired_turns": []}
            elif child or inherited or any(m["thread"] == key and m.get("parent") for m in members):
                return  # A delegated session without a known opted-in ancestor cannot become a root.
            elif os.environ.get("GROKBOT_CALLBACK"):
                cwd = data.get("cwd") or os.getcwd()
                metadata = git_metadata(cwd)
                item = {"key": key, "id": session, "agent": agent, "parent": "", "root": key, "depth": 0,
                        "task_id": os.environ.get("GROKBOT_TASK_ID") or f"{metadata['repo'] or cwd}:{metadata['branch']}",
                        "requester": os.environ["GROKBOT_CALLBACK"], "turn": uuid.uuid4().hex, "status": "idle",
                        "rex_session": os.environ.get("REX_SESSION", ""), "rex_block": os.environ.get("REX_BLOCK", "")}
            else:
                return
        if not item["parent"] and not os.environ.get("GROKBOT_CALLBACK"):
            return
        # Reusing a root CLI for another bot task is explicit at the next prompt.
        if name == "UserPromptSubmit" and not item["parent"]:
            if not os.environ.get("GROKBOT_CALLBACK"):
                return
            metadata = metadata or git_metadata(data.get("cwd") or os.getcwd())
            item["task_id"] = os.environ.get("GROKBOT_TASK_ID") or f"{metadata['repo'] or data.get('cwd') or os.getcwd()}:{metadata['branch']}"
            item["requester"] = os.environ["GROKBOT_CALLBACK"]
            item["turn"] = uuid.uuid4().hex
        native_turn = data.get("turn_id") or data.get("turn-id")
        retired_turns = item.get("retired_turns")
        if not isinstance(retired_turns, list):
            retired_turns = []
            item["retired_turns"] = retired_turns
        if name == "UserPromptSubmit":
            if item["parent"]:
                item["turn"] = uuid.uuid4().hex
            previous = item.get("native_turn")
            if previous and previous != native_turn:
                retired_turns.append(previous)
            item["native_turn"] = native_turn or ""
        elif not item["parent"] and native_turn and (native_turn in retired_turns or
                (item.get("native_turn") and native_turn != item["native_turn"])):
            return  # A delayed old-turn hook cannot change the active task's owner.
        if native_turn:
            item["native_turn"] = native_turn
            item["turn"] = native_turn
        item["updated_at"] = now()
        # Native children share the controller's block; nested CLIs have their own.
        item["rex_session"] = os.environ.get("REX_SESSION") or item["rex_session"]
        item["rex_block"] = os.environ.get("REX_BLOCK") or item["rex_block"]
        failed = name == "StopFailure" or data.get("status") in ("failed", "error") or bool(data.get("error"))
        event = ""
        if request:
            event, item["status"] = "needs_input", "waiting"
        elif name == "UserPromptSubmit":
            event, item["status"] = ("lineage" if item["parent"] else "heartbeat"), "running"
        elif name == "SubagentStart" or (name == "SessionStart" and item["parent"]):
            event, item["status"] = "lineage", "running"
        elif name in ("Stop", "StopFailure", "SubagentStop"):
            item["status"] = "waiting" if waiting(data) else "failed" if failed else "completed"
            if not waiting(data):
                event = "lineage" if item["parent"] else "run_finished"
        elif name == "SessionEnd":
            item["status"] = "ended"
            if item["parent"]:
                event = "lineage"
        sessions[key] = item
        sync_sessions(sessions, members, current_key=key)
        for k, row in sessions.items():
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (k, json.dumps(row)))
        if name == "UserPromptSubmit":
            receipt("started", agent=agent, agent_session_id=item["id"], task_id=item["task_id"],
                    turn_id=item["turn"], rex_session=item["rex_session"], rex_block=item["rex_block"])
        if not event:
            return
        envelope = {"agent": agent, "data": data, "turn": item["turn"], "requester": item["requester"],
                    "task_id": item["task_id"], "rex_session": item["rex_session"], "rex_block": item["rex_block"],
                    "finished_at": now(), "event": event, "status": item["status"], "lineage": lineage(item, sessions),
                    "last_message": final_message(agent, data) if event == "run_finished" else "",
                    "metadata": metadata or git_metadata(data.get("cwd") or os.getcwd()),
                    "children": [{"id": row["id"], "depth": row["depth"], "status": row["status"]}
                                 for row in sessions.values() if row["root"] == item["root"] and row["parent"] and row["task_id"] == item["task_id"]]}
        if request:
            envelope.update(kind=request[0], prompt_text=request[1])
        # One canonical spool per event keeps the first prompt and survives a dead worker.
        delivery = delivery_key(envelope)
        spool = rex.STATE / ("grokbot-pending-" + delivery)
        if spool.exists() or (rex.STATE / ("grokbot-sent-" + delivery)).exists():
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=rex.STATE, prefix="grokbot-pending-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(envelope, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(spool)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "deliver", str(spool)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def final_message(agent, data):
    if "last_assistant_message" in data:
        return str(data["last_assistant_message"] or "")[:2000]
    path = data.get("transcript_path")
    if not path:
        return ""
    message = ""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 100000))
            for line in stream.read().decode("utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if agent == "claude" and row.get("type") == "assistant":
                    item = row.get("message") or {}
                    content = item.get("content", []) if isinstance(item, dict) else []
                elif agent == "codex" and row.get("type") == "response_item":
                    item = row.get("payload") or {}
                    if not isinstance(item, dict):
                        continue
                    if item.get("role") != "assistant" or item.get("phase") != "final_answer":
                        continue
                    content = item.get("content", [])
                elif agent == "codex" and row.get("type") == "event_msg":
                    item = row.get("payload") or {}
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "task_complete" and isinstance(item.get("last_agent_message"), str):
                        message = item["last_agent_message"]
                    continue
                else:
                    continue
                if not isinstance(content, list):
                    continue
                text = "\n".join(part.get("text", "") for part in content
                                 if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
                if text:
                    message = text
    except OSError:
        pass
    return message[:2000]


def payload(envelope):
    data = envelope["data"]
    cwd = data.get("cwd") or os.getcwd()
    metadata = envelope.get("metadata") or git_metadata(cwd)
    failed = data.get("hook_event_name") == "StopFailure" or data.get("status") in ("failed", "error") or bool(data.get("error"))
    ancestry = envelope.get("lineage") or {"requester_bot": envelope["requester"], "root_session_id": data["session_id"],
                "parent_session_id": "", "agent_session_id": data["session_id"], "depth": 0,
                "agent_kind": envelope["agent"], "role": "agent"}
    return {"event": envelope.get("event", "run_finished"), "status": envelope.get("status", "failed" if failed else "completed"),
            "agent": envelope["agent"], "requester": envelope["requester"],
            "task_id": envelope.get("task_id") or f"{metadata['repo'] or cwd}:{metadata['branch']}",
            "turn_id": envelope["turn"], "lineage": ancestry, "children": envelope.get("children", []),
            "untrusted": ["last_message", "prompt_text"], "prompt_text": envelope.get("prompt_text", "")[:1000],
            "agent_session_id": ancestry["agent_session_id"], "rex_session": envelope["rex_session"],
            "rex_block": envelope["rex_block"], "cwd": cwd, **metadata,
            "last_message": (envelope.get("last_message", str(data.get("last_assistant_message") or ""))[:2000]
                             if envelope.get("event", "run_finished") == "run_finished" else ""),
            "finished_at": envelope["finished_at"], **({"kind": envelope["kind"]} if "kind" in envelope else {})}


def post(body):
    url, name, value = config()
    encoded = json.dumps(body).encode()
    status = 0
    for _ in range(3):
        connection = None
        try:
            connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(url.hostname, url.port, timeout=5)
            connection.putrequest("POST", (url.path or "/") + ("?" + url.query if url.query else ""))
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(encoded)))
            connection.putheader(name, value)
            connection.endheaders(encoded)
            status = connection.getresponse().status
            if 200 <= status < 300:
                break
        except (OSError, ValueError, http.client.HTTPException):
            status = 0
        finally:
            if connection:
                connection.close()
    return status


def delivery_key(envelope):
    return hashlib.sha256(json.dumps([envelope.get("task_id", ""), envelope["agent"],
                envelope.get("lineage", {}).get("agent_session_id") or envelope["data"]["session_id"],
                envelope["turn"], envelope.get("event", "run_finished"), envelope.get("kind", ""),
                envelope.get("status", "") if envelope.get("event") == "lineage" else ""]).encode()).hexdigest()


def deliver(path):
    rex.state_dir()
    try:
        envelope = json.loads(path.read_text())
    except FileNotFoundError:
        return True  # Another worker already delivered this spool.
    key = delivery_key(envelope)
    # A lock survives crashes without becoming a permanent failed-delivery claim.
    with (rex.STATE / ("grokbot-lock-" + key)).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sent = rex.STATE / ("grokbot-sent-" + key)
        if sent.exists():
            path.unlink(missing_ok=True)
            return True
        fields = {"key": key, "event": envelope.get("event", "run_finished"), "task_id": envelope.get("task_id", ""),
                  "agent_session_id": envelope.get("lineage", {}).get("agent_session_id") or envelope["data"]["session_id"],
                  "rex_session": envelope["rex_session"], "rex_block": envelope["rex_block"]}
        try:
            # Freeze payload on first attempt: retries preserve task metadata and text.
            if "body" not in envelope:
                envelope["body"] = {**payload(envelope), "delivery_key": key}
                temporary = path.with_suffix(".tmp")
                with temporary.open("w") as stream:
                    os.chmod(temporary, 0o600)
                    json.dump(envelope, stream)
                temporary.replace(path)
            status = post(envelope["body"])
            success = 200 <= status < 300
            receipt("delivered" if success else "delivery_failed", **fields, http_status=status)
        except (OSError, ValueError):
            receipt("config_invalid", **fields)
            return False
        if success:
            sent.touch(mode=0o600)
            path.unlink(missing_ok=True)
        return success


def health():
    """Expose metadata, never secrets or untrusted prompt/message text."""
    rex.state_dir()
    receipts = {}
    try:
        for line in (rex.STATE / "grokbot-callback-receipts.jsonl").read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("key"):
                    receipts[row["key"]] = row
            except (ValueError, AttributeError):
                continue
    except FileNotFoundError:
        pass
    pending, failed = [], []
    seen = set()
    for path in rex.STATE.glob("grokbot-pending-*"):
        if path.suffix == ".tmp":
            continue
        try:
            envelope = json.loads(path.read_text())
            key = delivery_key(envelope)
            if key in seen or (rex.STATE / ("grokbot-sent-" + key)).exists():
                continue
            seen.add(key)
            row = receipts.get(key, {})
            item = {"key": key, "spool": str(path), "event": envelope.get("event", "run_finished"),
                    "agent_session_id": envelope.get("lineage", {}).get("agent_session_id") or envelope["data"]["session_id"],
                    "task_id": envelope.get("task_id", ""), "rex_session": envelope["rex_session"], "rex_block": envelope["rex_block"],
                    "outcome": row.get("outcome", "pending")}
            (failed if item["outcome"] in ("delivery_failed", "config_invalid") else pending).append(item)
        except (OSError, ValueError, KeyError, TypeError):
            pending.append({"spool": str(path), "outcome": "spool_invalid"})
    # Old receipts may predate durable spooling. Keep their failures observable.
    failed += [row for key, row in receipts.items() if key not in seen and row["outcome"] in ("delivery_failed", "config_invalid")]
    with database() as db:
        sessions = [json.loads(row[0]) for row in db.execute("SELECT record FROM sessions")]
    running = [{k: row[k] for k in ("id", "agent", "task_id", "requester", "status", "rex_session", "rex_block", "updated_at") if k in row}
               for row in sessions if row["status"] in ("running", "waiting")]
    return {"pending": pending, "failed": failed, "running": running}


def retry():
    results = []
    for path in rex.STATE.glob("grokbot-pending-*"):
        if path.suffix == ".tmp":
            continue
        try:
            results.append(deliver(path))
        except (OSError, ValueError, KeyError, TypeError):
            results.append(False)
    return {"delivered": results.count(True), "failed": results.count(False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("test", "doctor", "deliver", "status", "retry"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "deliver":
            if not args.path:
                parser.error("deliver requires a spool path")
            deliver(Path(args.path))
            return 0
        if args.action in ("status", "retry"):
            result = health() if args.action == "status" else retry()
            print(json.dumps(result, indent=None if args.json else 2))
            return int(args.action == "retry" and bool(result.get("failed")))
        config()
        if args.action == "doctor":
            print("Callback config OK (owned file, mode 600)")
            return 0
        metadata = git_metadata(os.getcwd())
        status = post({"event": "test", "task_id": os.environ.get("GROKBOT_TASK_ID") or
                       f"{metadata['repo'] or os.getcwd()}:{metadata['branch']}",
                       "requester": os.environ.get("GROKBOT_CALLBACK", ""),
                       "untrusted": ["last_message", "prompt_text"]})
        print(f"HTTP {status}" if status else "HTTP request failed")
        return 0 if 200 <= status < 300 else 1
    except (OSError, ValueError):
        receipt("config_invalid")
        if args.action != "deliver":
            print("Callback config missing or invalid; require an owned file with mode 600", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
