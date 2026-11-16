#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local-only callback delivery and hook integration checks."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import claude_sessions
import codex_sessions
import grokbot_callback as callback


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / "callback.env"
        self.requests = []
        self.statuses = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((body, self.headers["Authorization"]))
                self.send_response(owner.statuses.pop(0) if owner.statuses else 204)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.config.write_text(f'GROKBOT_WEBHOOK_URL=http://127.0.0.1:{self.server.server_port}/callback\n'
                               'GROKBOT_WEBHOOK_HEADER="Authorization: Bearer test-secret"\n')
        self.config.chmod(0o600)
        for item in (patch.object(callback, "CONFIG", self.config),
                     patch.object(callback.rex, "STATE", self.root / "state"),
                     patch.object(codex_sessions, "DB", self.root / "coordination.sqlite3"),
                     patch.object(codex_sessions, "reopen_activity"),
                     patch.dict(os.environ, {"GROKBOT_CALLBACK": "dinesh", "REX_SESSION": "rex-s", "REX_BLOCK": "rex-b"}, clear=True)):
            item.start()
            self.addCleanup(item.stop)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def data(self, name="Stop", **fields):
        return {"hook_event_name": name, "session_id": "f0578d19-354e-43fc-b380-0a0ce62ef249",
                "cwd": str(self.root), "last_assistant_message": "Done", **fields}

    def envelope(self, **fields):
        return {"agent": "codex", "data": self.data(**fields), "turn": "turn-1", "requester": "dinesh",
                "rex_session": "rex-s", "rex_block": "rex-b", "finished_at": datetime.now().astimezone().isoformat()}

    def spool(self, envelope):
        callback.rex.state_dir()
        path = self.root / "state" / "pending"
        path.write_text(json.dumps(envelope))
        return path

    def test_payload_shape_and_git_metadata(self):
        with patch.object(callback.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, value, "") for value in ("/repo\n", "feature\n", "abc123\n")]):
            body = callback.payload(self.envelope(last_assistant_message="x" * 2100))
        self.assertEqual(set(body), {"event", "status", "agent", "requester", "agent_session_id", "rex_session", "rex_block", "cwd", "repo", "branch", "head_sha", "last_message", "finished_at"})
        self.assertEqual((body["event"], body["status"], body["agent"], body["requester"]), ("run_finished", "completed", "codex", "dinesh"))
        self.assertEqual((body["repo"], body["branch"], body["head_sha"]), ("/repo", "feature", "abc123"))
        self.assertEqual(len(body["last_message"]), 2000)
        self.assertIsNotNone(datetime.fromisoformat(body["finished_at"]).utcoffset())
        self.assertEqual(callback.payload(self.envelope(status="failed"))["status"], "failed")
        envelope = self.envelope()
        envelope["agent"] = "claude"
        envelope["data"]["hook_event_name"] = "StopFailure"
        self.assertEqual(callback.payload(envelope)["status"], "failed")

    def test_opt_in_gating_and_waiting(self):
        with patch.object(callback.subprocess, "Popen") as launch:
            with patch.dict(os.environ, {}, clear=True):
                callback.notify("codex", self.data())
            self.assertFalse((self.root / "state").exists())
            for agent in ("codex", "claude"):
                for fields in ({"background_tasks": [{"status": "running"}]}, {"session_crons": [{"id": "wake"}]}, {"agent_id": "child"}):
                    callback.notify(agent, self.data(**fields))
                for name in ("SessionEnd", "SubagentStop", "PostToolUse"):
                    callback.notify(agent, self.data(name))
            launch.assert_not_called()
            callback.notify("codex", self.data())
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("test-secret", repr(launch.call_args))

    def test_adapters_call_notifier_and_codex_waits(self):
        with patch.object(callback, "notify") as notify:
            for adapter, agent in ((codex_sessions, "codex"), (claude_sessions, "claude")):
                adapter.handle_hook(self.data(background_tasks=[{"id": "pending"}]))
                notify.assert_called_with(agent, self.data(background_tasks=[{"id": "pending"}]))
            session = self.data()["session_id"]
            self.assertEqual(codex_sessions.member(session, session)["state"], "waiting")

    def test_dedupe_across_workers_and_multiple_turns(self):
        # Exercise the actual detached worker with a temporary HOME/config/state.
        home_config = self.root / ".config/agent-tooling/grokbot-callback.env"
        home_config.parent.mkdir(parents=True)
        home_config.write_text(self.config.read_text())
        home_config.chmod(0o600)
        environment = {**os.environ, "HOME": str(self.root), "REX_VISIBLE_STATE_DIR": str(self.root / "state")}
        children = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.dict(os.environ, environment), patch.object(callback.subprocess, "Popen", side_effect=launch):
            callback.notify("codex", self.data("UserPromptSubmit"))
            callback.notify("codex", self.data())
        for child in children:
            self.assertEqual(child.wait(timeout=5), 0)
        self.assertEqual(len(self.requests), 1)
        with patch.object(callback.subprocess, "Popen") as launch:
            callback.notify("codex", self.data())
            duplicate = Path(launch.call_args.args[0][-1])
        callback.deliver(duplicate)
        self.assertEqual(len(self.requests), 1)
        with patch.object(callback.subprocess, "Popen") as launch:
            callback.notify("codex", self.data("UserPromptSubmit"))
            callback.notify("codex", self.data())
            next_turn = Path(launch.call_args.args[0][-1])
        callback.deliver(next_turn)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][1], "Bearer test-secret")
        self.assertEqual(self.requests[0][0]["requester"], "dinesh")

    def test_atomic_duplicate_claim(self):
        paths = []
        callback.rex.state_dir()
        for index in range(4):
            path = self.root / "state" / f"pending-{index}"
            path.write_text(json.dumps(self.envelope()))
            paths.append(path)
        workers = [threading.Thread(target=callback.deliver, args=(path,)) for path in paths]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(self.requests), 1)

    def test_permissions_refused_and_secrets_never_logged(self):
        for mode in (0o644, 0o640, 0o400, 0o660):
            self.config.chmod(mode)
            with self.assertRaisesRegex(ValueError, "mode 600"):
                callback.config()
        callback.deliver(self.spool(self.envelope()))
        self.assertEqual(self.requests, [])
        self.config.chmod(0o600)
        self.statuses[:] = [500, 500, 500]
        callback.deliver(self.spool({**self.envelope(), "turn": "turn-2"}))
        receipts = (self.root / "state/grokbot-callback-receipts.jsonl").read_text()
        self.assertNotIn("test-secret", receipts)
        self.assertNotIn("Authorization", receipts)
        self.assertIn("config_invalid", receipts)
        self.assertIn('"http_status": 500', receipts)

    def test_retries_and_test_doctor_cli(self):
        self.statuses[:] = [503, 503, 202]
        self.assertEqual(callback.post({"event": "test"}), 202)
        self.assertEqual(len(self.requests), 3)
        for command in ("test", "doctor"):
            output = io.StringIO()
            with patch.object(callback.sys, "argv", ["grokbot_callback.py", command]), redirect_stdout(output):
                self.assertEqual(callback.main(), 0)
            self.assertNotIn("test-secret", output.getvalue())
            self.assertIn("HTTP 204" if command == "test" else "mode 600", output.getvalue())
        self.config.chmod(0o644)
        output = io.StringIO()
        with patch.object(callback.sys, "argv", ["grokbot_callback.py", "doctor"]), redirect_stderr(output):
            self.assertEqual(callback.main(), 1)
        self.assertNotIn("test-secret", output.getvalue())

    def test_timeout_and_enqueue_failure_are_fail_open(self):
        with patch.object(callback.http.client, "HTTPConnection", side_effect=TimeoutError("test-secret")) as connect:
            self.assertEqual(callback.post({"event": "test"}), 0)
            self.assertEqual(connect.call_count, 3)
            self.assertEqual(connect.call_args.kwargs["timeout"], 5)
        with patch.object(callback.subprocess, "Popen", side_effect=OSError("test-secret")):
            callback.notify("codex", self.data())
        self.assertEqual(list((self.root / "state").glob("grokbot-pending-*")), [])
        self.assertNotIn("test-secret", (self.root / "state/grokbot-callback-receipts.jsonl").read_text())

    def test_transcript_final_message_fallback(self):
        transcript = self.root / "transcript.jsonl"
        for agent, rows in (
            ("codex", [{"type": "response_item", "payload": {"role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "Working"}]}},
                       {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Final"}]}},
                       {"type": "response_item", "payload": {"role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "Private intermediate"}]}}]),
            ("claude", [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Final"}]}}]),
        ):
            transcript.write_text("\n".join(map(json.dumps, rows)))
            self.assertEqual(callback.final_message(agent, {"transcript_path": str(transcript)}), "Final")


if __name__ == "__main__":
    unittest.main()
