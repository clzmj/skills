#!/usr/bin/env python3
"""Live local Rex integration demo; launches only disposable owned panes."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rex_visible as rex
import codex_sessions as cs


def layout(session):
    data = rex.rex_json(["session", "inspect", session, "--json"])
    return {w["window_id"]: [b["block_id"] for layer in w.get("layers", []) for b in layer.get("blocks", [])] for w in data["windows"]}


def wait_for(check, message, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            print("PASS " + message, flush=True)
            return value
        time.sleep(0.5)
    raise AssertionError(message)


def demo():
    session = rex.main_session()
    before_sessions = rex.rex_json(["ls", "--json"])["sessions"]
    before = layout(session)
    owned = []
    team = "cleanup-demo-" + uuid.uuid4().hex[:8]
    root, worker = str(uuid.uuid4()), str(uuid.uuid4())
    evidence = {"session": session, "before_sessions": before_sessions, "before_layout": before}

    def launch(task, command, kind="test"):
        job_id = uuid.uuid4().hex
        status = rex.STATE / (job_id + ".status.json")
        opened = rex.open_job(argparse.Namespace(task=task, command=command, kind=kind, cwd=str(ROOT), shell="/bin/zsh", close_after=8), job_id, status)
        owned.append((opened["session_id"], opened["block_id"]))
        assert opened["session_id"] == session
        return opened

    def receipt(job):
        path = Path(job["status_file"])
        return json.loads(path.read_text()) if path.exists() else None

    def capture(block):
        return subprocess.run(rex.rex_cli() + ["capture", "-s", session, "-b", block, "--trim", "--unwrap"], capture_output=True, text=True, check=True).stdout

    try:
        print("LIVE DEMO: main-session reuse, finite retention, and service lifetime", flush=True)
        success = launch("demo-project-one", "sleep 3; printf 'success demo complete\\n'")
        failure = launch("demo-project-two", "sleep 3; printf 'intentional failure demo\\n'; exit 7")
        current = layout(session)
        assert any(success["block_id"] in ids and failure["block_id"] in ids for ids in current.values())
        assert all(len(ids) <= 2 for wid, ids in current.items() if wid not in before)
        print("PASS two different tasks share one Checks tab in the main session", flush=True)

        server = launch("demo-http-server", shlex.join([sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1", "--directory", str(ROOT / "demo")]), "server")
        port = wait_for(lambda: re.search(r"port (\d+)", capture(server["block_id"])), "HTTP server announces readiness").group(1)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.status == 200
        print("PASS real local HTTP request returns 200", flush=True)
        wait_for(lambda: receipt(success), "successful job writes completion receipt")
        assert wait_for(lambda: receipt(failure), "failed job writes completion receipt")["exit_code"] == 7
        assert not receipt(server), "running service must remain active"
        assert failure["block_id"] in sum(layout(session).values(), [])
        print("PASS failed pane stays visible for review; running server stays open", flush=True)
        subprocess.run(rex.rex_cli() + ["send-key", "-s", session, "-b", server["block_id"], "ctrl+c"], check=True, capture_output=True)
        wait_for(lambda: receipt(server), "stopped server writes completion receipt")

        cs.register(team, root, "demo-coordinator", activate=True)
        cs.register(team, worker, "demo-worker", parent=root, mode="native")
        cs.state(team, root, "working")
        cs.state(team, worker, "working")
        board, view = cs.open_view(team), cs.open_view(team, worker)
        owned.extend((v["session"], v["block"]) for v in (board, view))
        assert board["session"] == view["session"] == session
        print("PASS team and worker views use the same main session", flush=True)
        cs.state(team, worker, "reported")
        cs.state(team, root, "idle")
        cs.event(team, worker, "result", "Live demo finished; automatic 30-second retention begins", root)
        evidence["jobs"] = [success, failure, server]
        evidence["views"] = [board, view]
        disposable = {b for _, b in owned}
        wait_for(lambda: not disposable.intersection(sum(layout(session).values(), [])), "all finished job panes, agent views, and their empty tabs disappear", timeout=45)
        after_sessions = rex.rex_json(["ls", "--json"])["sessions"]
        assert before_sessions == after_sessions, "demo created an extra session"
        assert layout(session) == before, "demo left tabs/panes or changed unrelated layout"
        with cs.database() as db:
            assert db.execute("SELECT COUNT(*) FROM views WHERE team=?", (team,)).fetchone()[0] == 0
        evidence.update({"passed": True, "after_sessions": after_sessions, "after_layout": layout(session)})
        rex.atomic_json(rex.STATE / "last-cleanup-demo.json", evidence)
        print("PASS original layout restored; session count unchanged; no demo views remain", flush=True)
    finally:
        # A failed assertion must not strand a disposable service or watcher.
        for sid, block in owned:
            rex.close_pane(sid, block)
        with cs.database() as db:
            db.execute("DELETE FROM views WHERE team=?", (team,))


if __name__ == "__main__":
    demo()
