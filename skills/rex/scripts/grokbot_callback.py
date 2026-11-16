#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Opt-in, detached turn-finished webhooks for Grok Bot."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

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


def notify(agent, data):
    """Keep hooks fail-open; only local bookkeeping and process creation happen here."""
    if not os.environ.get("GROKBOT_CALLBACK"):
        return
    name = data.get("hook_event_name")
    if name not in ("UserPromptSubmit", "Stop", "StopFailure") or data.get("agent_id"):
        return
    session = data.get("session_id")
    if not session:
        return
    spool = None
    try:
        rex.state_dir()
        key = hashlib.sha256(json.dumps([agent, session]).encode()).hexdigest()
        turn_file = rex.STATE / ("grokbot-turn-" + key)
        with turn_file.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            turn = stream.read() or uuid.uuid4().hex
            if name == "UserPromptSubmit":
                turn = uuid.uuid4().hex
            stream.seek(0)
            stream.truncate()
            stream.write(turn)
        if name == "UserPromptSubmit" or waiting(data):
            return
        # Native turn IDs take precedence; prompt generations cover clients without one.
        turn = data.get("turn_id") or data.get("turn-id") or turn
        envelope = {"agent": agent, "data": data, "turn": turn,
                    "requester": os.environ["GROKBOT_CALLBACK"],
                    "rex_session": os.environ.get("REX_SESSION", ""),
                    "rex_block": os.environ.get("REX_BLOCK", ""),
                    "finished_at": datetime.now().astimezone().isoformat()}
        with tempfile.NamedTemporaryFile(mode="w", dir=rex.STATE, prefix="grokbot-pending-", delete=False) as stream:
            spool = Path(stream.name)
            json.dump(envelope, stream)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "deliver", str(spool)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        spool = None
    except Exception:
        # Callback errors must never change the host's hook result.
        try:
            receipt("enqueue_failed")
        except OSError:
            pass
    finally:
        if spool:
            spool.unlink(missing_ok=True)


def final_message(agent, data):
    if "last_assistant_message" in data:
        return str(data["last_assistant_message"] or "")[:2000]
    path = data.get("transcript_path")
    if not path:
        return ""
    message = ""
    try:
        with Path(path).open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if agent == "claude" and row.get("type") == "assistant":
                    content = (row.get("message") or {}).get("content", [])
                elif agent == "codex" and row.get("type") == "response_item":
                    item = row.get("payload") or {}
                    if item.get("role") != "assistant" or item.get("phase") != "final_answer":
                        continue
                    content = item.get("content", [])
                elif agent == "codex" and row.get("type") == "event_msg":
                    item = row.get("payload") or {}
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

    def git(*args):
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1)
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    failed = data.get("hook_event_name") == "StopFailure" or data.get("status") in ("failed", "error") or bool(data.get("error"))
    return {"event": "run_finished", "status": "failed" if failed else "completed",
            "agent": envelope["agent"], "requester": envelope["requester"],
            "agent_session_id": data["session_id"], "rex_session": envelope["rex_session"],
            "rex_block": envelope["rex_block"], "cwd": cwd,
            "repo": git("rev-parse", "--show-toplevel"), "branch": git("branch", "--show-current"),
            "head_sha": git("rev-parse", "HEAD"), "last_message": final_message(envelope["agent"], data),
            "finished_at": envelope["finished_at"]}


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


def deliver(path):
    try:
        envelope = json.loads(path.read_text())
    finally:
        path.unlink(missing_ok=True)
    key = hashlib.sha256(json.dumps([envelope["agent"], envelope["data"]["session_id"], envelope["turn"]]).encode()).hexdigest()
    try:
        with (rex.STATE / ("grokbot-sent-" + key)).open("x"):
            pass
    except FileExistsError:
        return
    try:
        status = post(payload(envelope))
        receipt("delivered" if 200 <= status < 300 else "delivery_failed", key=key, http_status=status)
    except (OSError, ValueError):
        receipt("config_invalid", key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("test", "doctor", "deliver"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "deliver":
            deliver(Path(args.path))
            return 0
        config()
        if args.action == "doctor":
            print("Callback config OK (owned file, mode 600)")
            return 0
        status = post({"event": "test"})
        print(f"HTTP {status}" if status else "HTTP request failed")
        return 0 if 200 <= status < 300 else 1
    except (OSError, ValueError):
        receipt("config_invalid")
        if args.action != "deliver":
            print("Callback config missing or invalid; require an owned file with mode 600", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
