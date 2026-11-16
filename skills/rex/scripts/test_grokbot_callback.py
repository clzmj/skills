#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local-only callback delivery and hook integration checks."""
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import claude_sessions
import codex_sessions
import grokbot_callback as callback

TEST_ACTIVITY_TOOL = "Bash"


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / "callback.env"
        self.git_metadata = callback.git_metadata
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
                     patch.object(callback, "git_metadata", return_value={"repo": "/repo", "branch": "feature", "head_sha": "abc123"}),
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

    def tearDown(self):
        self.assertTrue(all(body["event"] in ("run_finished", "needs_input", "test") for body, _ in self.requests))

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
        with patch.object(callback, "git_metadata", wraps=self.git_metadata), patch.object(callback.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, value, "") for value in ("/repo\n", "feature\n", "abc123\n")]):
            body = callback.payload(self.envelope(last_assistant_message="x" * 2100))
        self.assertEqual(set(body), {"event", "status", "agent", "requester", "agent_session_id", "rex_session", "rex_block", "cwd", "repo", "branch", "head_sha", "last_message", "finished_at", "task_id", "turn_id", "lineage", "children", "untrusted", "prompt_text"})
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
            launch.assert_not_called()  # Child lifecycle stays local.
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
                self.assertEqual(notify.call_args.args[:2], (agent, self.data(background_tasks=[{"id": "pending"}])))
                self.assertTrue(notify.call_args.args[2])
            session = self.data()["session_id"]
            self.assertEqual(codex_sessions.member(session, session)["state"], "waiting")

    def test_callback_bookkeeping_failure_is_silent_and_fail_open(self):
        for adapter in (codex_sessions, claude_sessions):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output), patch.object(adapter, "lifecycle_hook", return_value={"ok": True}), \
                    patch.object(codex_sessions, "callback_members", side_effect=RuntimeError("test-secret")):
                self.assertEqual(adapter.handle_hook(self.data()), {"ok": True})
            self.assertEqual(output.getvalue(), "")

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
            launch.assert_not_called()
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
        self.assertEqual(len(list((self.root / "state").glob("grokbot-pending-*"))), 1)
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

    def queued(self, agent, data, members=()):
        with patch.object(callback.subprocess, "Popen") as launch:
            callback.notify(agent, data, members)
        return Path(launch.call_args.args[0][-1]) if launch.called else None

    def session_status(self, agent, actor):
        return next(row["status"] for row in callback.health()["sessions"] if row["agent"] == agent and row["id"] == actor)

    def drain(self):
        for path in list((self.root / "state").glob("grokbot-pending-*")):
            callback.deliver(path)

    def test_input_kinds_prompt_limits_and_per_turn_dedupe(self):
        self.queued("claude", self.data("UserPromptSubmit"))
        for event in (self.data("PermissionRequest", request_id="approval-1", tool_input={"description": "x" * 1100}),
                      self.data("Notification", request_id="approval-1", notification_type="permission_prompt", message="duplicate approval"),
                      self.data("Notification", notification_type="elicitation_dialog", message="Choose a name?"),
                      self.data("Notification", notification_type="trust_prompt", message="Trust this workspace?"),
                      self.data("Notification", notification_type="input_prompt", message="Waiting")):
            self.queued("claude", event)
        self.drain()
        inputs = [body for body, _ in self.requests if body["event"] == "needs_input"]
        self.assertEqual({body["kind"] for body in inputs}, {"approval", "question", "trust", "other"})
        inputs = sorted(inputs, key=lambda body: body["kind"])
        self.assertEqual(len(inputs[0]["prompt_text"]), 1000)
        self.assertEqual(next(body for body in inputs if body["kind"] == "question")["prompt_text"], "Choose a name?")
        self.assertTrue(all(body["untrusted"] == ["last_message", "prompt_text", "cwd", "repo", "branch", "head_sha", "task_id"] for body in inputs))
        self.assertIsNone(self.queued("claude", self.data("Notification", notification_type="auth_success", message="Signed in")))
        self.queued("claude", self.data("UserPromptSubmit"))
        self.queued("claude", self.data("PermissionRequest", prompt="Again"))
        self.drain()
        self.assertEqual(sum(body["event"] == "needs_input" for body, _ in self.requests), 5)

    def test_codex_approval_uses_native_turn_and_description(self):
        self.queued("codex", self.data("UserPromptSubmit", turn_id="native-turn"))
        for _ in range(2):
            self.queued("codex", self.data("PermissionRequest", turn_id="native-turn", tool_name="Bash",
                                           tool_input={"description": "Run deployment?", "command": "deploy"}))
        self.drain()
        body = next(body for body, _ in self.requests if body["event"] == "needs_input")
        self.assertEqual((body["kind"], body["prompt_text"], body["turn_id"]), ("approval", "Run deployment?", "native-turn"))
        self.assertEqual(len(self.requests), 1)

    def test_codex_request_user_input_questions_emit_untrusted_question(self):
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.USER_PROMPT_SUBMIT, turn_id="turn-question"))
        questions = [{"question": "Which region?", "header": "Region", "id": "region",
                      "options": [{"label": "East", "description": "Eastern region"}, {"label": "West"}]},
                     {"title": "How many?", "options": ["one", "two"]}]
        event = self.data(callback.HookEvent.PRE_TOOL_USE, turn_id="turn-question", tool_name=callback.ToolName.REQUEST_USER_INPUT,
                          tool_use_id="stable-question-id", tool_input={"questions": questions})
        path = self.queued(callback.AgentKind.CODEX, event)
        self.assertIsNotNone(path)
        duplicate = self.queued(callback.AgentKind.CODEX, event)
        self.assertIsNone(duplicate)
        self.drain()
        body = next(body for body, _ in self.requests if body["event"] == callback.Event.NEEDS_INPUT)
        self.assertEqual(body["kind"], callback.InputKind.QUESTION)
        self.assertIn("Which region?", body["prompt_text"])
        self.assertIn("How many?", body["prompt_text"])
        self.assertEqual(body["untrusted"], list(callback.UNTRUSTED_FIELDS))
        self.assertEqual(body["lineage"]["agent_session_id"], event["session_id"])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.session_status(callback.AgentKind.CODEX, event["session_id"]), callback.Status.WAITING)
        self.queued(callback.AgentKind.CODEX, {**event, "hook_event_name": callback.HookEvent.POST_TOOL_USE, "tool_response": {"answers": {"region": "East"}}})
        self.assertEqual(self.session_status(callback.AgentKind.CODEX, event["session_id"]), callback.Status.RUNNING)

    def test_codex_question_malformed_fails_open_and_async_waits_for_later_activity(self):
        root = self.data()["session_id"]
        bad = self.data(callback.HookEvent.PRE_TOOL_USE, tool_name=callback.ToolName.REQUEST_USER_INPUT, tool_use_id="bad",
                        tool_input={"questions": [{"question": 3}]})
        self.assertIsNone(callback.input_request(bad))
        for inputs in (None, [], "invalid", {"questions": None}, {"questions": "bad"}, {"questions": [None, 3, {}]}):
            event = {**bad, "tool_input": inputs}
            self.assertIsNone(callback.input_request(event))
            with patch.object(callback.subprocess, "Popen") as launch:
                self.assertEqual(codex_sessions.handle_hook(event), {})
            launch.assert_not_called()
        with patch.object(callback.subprocess, "Popen"):
            codex_sessions.handle_hook(self.data(callback.HookEvent.USER_PROMPT_SUBMIT))
            codex_sessions.handle_hook(self.data(callback.HookEvent.PRE_TOOL_USE, tool_name=callback.ToolName.REQUEST_USER_INPUT_ASYNC,
                                                  tool_use_id="async", tool_input={"questions": [{"title": "Choose"}]}))
            self.assertEqual(codex_sessions.member(root, root)["state"], callback.MemberState.WAITING)
            self.assertEqual(self.session_status(callback.AgentKind.CODEX, root), callback.Status.WAITING)
            codex_sessions.handle_hook(self.data(callback.HookEvent.POST_TOOL_USE, tool_name=callback.ToolName.REQUEST_USER_INPUT_ASYNC,
                                                  tool_use_id="async", tool_input={"questions": [{"title": "Choose"}]}))
            self.assertEqual(codex_sessions.member(root, root)["state"], callback.MemberState.WAITING)
            self.assertEqual(self.session_status(callback.AgentKind.CODEX, root), callback.Status.WAITING)
            codex_sessions.handle_hook(self.data(callback.HookEvent.POST_TOOL_USE, tool_name=TEST_ACTIVITY_TOOL))
            self.assertEqual(codex_sessions.member(root, root)["state"], callback.MemberState.WORKING)
            self.assertEqual(self.session_status(callback.AgentKind.CODEX, root), callback.Status.RUNNING)

    def question_event(self, tool=callback.ToolName.REQUEST_USER_INPUT, text="Which region?", call="question-call"):
        return self.data(callback.HookEvent.PRE_TOOL_USE, tool_name=tool, tool_use_id=call,
                         tool_input={"questions": [{"question": text}]})

    def test_question_fallback_and_repeated_question_dedupe_per_turn(self):
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.USER_PROMPT_SUBMIT))
        self.queued(callback.AgentKind.CODEX, self.question_event())
        self.drain()
        # Same content, fresh call IDs, and schema/tool changes all retain one key.
        fallback = self.question_event(callback.ToolName.REQUEST_USER_INPUT_ASYNC, "  WHICH\n region? ", "fallback-call")
        fallback["tool_input"] = {"questions": [{"title": "  WHICH\n region? "}]}
        self.assertIsNone(self.queued(callback.AgentKind.CODEX, fallback))
        self.assertIsNone(self.queued(callback.AgentKind.CODEX, self.question_event(call="asked-again")))
        self.drain()
        self.assertEqual(len(self.requests), 1)
        # Distinct questions in the same turn, and the same question next turn, deliver.
        self.queued(callback.AgentKind.CODEX, self.question_event(text="Which city?", call="different"))
        self.drain()
        self.assertEqual(len(self.requests), 2)
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.USER_PROMPT_SUBMIT))
        self.queued(callback.AgentKind.CODEX, self.question_event(call="next-turn"))
        self.drain()
        self.assertEqual(len(self.requests), 3)

    def test_question_validation_uses_first_string_and_identity_precedes_truncation(self):
        event = self.question_event()
        event["tool_input"] = {"questions": [{"question": 7, "title": "Use title"}]}
        self.assertEqual(callback.input_request(event), (callback.InputKind.QUESTION, "Use title"))
        event["tool_input"] = {"questions": [{"question": "", "title": "Do not fall through"}]}
        self.assertIsNone(callback.input_request(event))
        first = self.question_event(text="x" * 1000 + "a")
        second = self.question_event(text="x" * 1000 + "b")
        self.assertEqual(callback.input_request(first), callback.input_request(second))
        self.assertNotEqual(callback.request_identity(first), callback.request_identity(second))

    def test_question_and_permission_deliver_independently_and_claude_tool_is_ignored(self):
        self.assertIsNone(self.queued(callback.AgentKind.CLAUDE, self.question_event()))
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.USER_PROMPT_SUBMIT))
        self.queued(callback.AgentKind.CODEX, self.question_event())
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.PERMISSION_REQUEST,
                                                      tool_use_id="approval-call", prompt="Allow this?"))
        self.drain()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual({body["kind"] for body, _ in self.requests},
                         {callback.InputKind.QUESTION, callback.InputKind.APPROVAL})

    def test_async_acceptance_real_delivery_and_private_files(self):
        event = self.question_event(callback.ToolName.REQUEST_USER_INPUT_ASYNC)
        self.queued(callback.AgentKind.CODEX, self.data(callback.HookEvent.USER_PROMPT_SUBMIT))
        previous_umask = os.umask(0)
        try:
            spool = self.queued(callback.AgentKind.CODEX, event)
            self.assertEqual(spool.stat().st_mode & 0o777, 0o600)
            self.drain()  # Real POST to the loopback receiver, not a mocked transport.
            self.queued(callback.AgentKind.CODEX, {**event, "hook_event_name": callback.HookEvent.POST_TOOL_USE,
                                                  "tool_response": json.dumps({"accepted": True})})
        finally:
            os.umask(previous_umask)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][0]["kind"], callback.InputKind.QUESTION)
        self.assertEqual(self.session_status(callback.AgentKind.CODEX, event["session_id"]), callback.Status.WAITING)
        records = [json.loads(line) for line in callback.receipt_lines()]
        delivered = [row for row in records if row["outcome"] == callback.Outcome.DELIVERED]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["http_status"], 204)
        for path in (self.root / "state").glob("grokbot-*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_claude_approval_notifications_do_not_duplicate_permission_requests(self):
        for notification_first in (False, True):
            with patch.object(callback.subprocess, "Popen"):
                claude_sessions.handle_hook(self.data("UserPromptSubmit"))
                permission = self.data("PermissionRequest", tool_name="Bash", tool_input={"command": "ls", "description": "Approve listing?"})
                notification = self.data("Notification", notification_type="permission_prompt", message="Claude needs permission")
                for event in ((notification, permission) if notification_first else (permission, notification)):
                    claude_sessions.handle_hook(event)
                claude_sessions.handle_hook(permission)
            start = len(self.requests)
            self.drain()
            self.assertEqual(len(self.requests) - start, 1)
            self.assertEqual(self.requests[-1][0]["event"], "needs_input")
            self.assertEqual(self.requests[-1][0]["prompt_text"], "Approve listing?")

    def test_terminal_root_and_child_ignore_all_late_input(self):
        for agent in ("codex", "claude"):
            for terminal, expected in (("Stop", "completed"), ("StopFailure", "failed"), ("SessionEnd", "ended")):
                root = f"{agent}-{terminal}"
                base = self.data("UserPromptSubmit", session_id=root)
                self.queued(agent, base)
                self.queued(agent, {**base, "hook_event_name": "SubagentStart", "agent_id": "child-" + root})
                for actor in ("", "child-" + root):
                    fields = {"agent_id": actor} if actor else {}
                    self.queued(agent, {**base, "hook_event_name": terminal, **fields})
                    for event in (self.data("PermissionRequest", prompt="Late approval"),
                                  self.data("Notification", notification_type="idle_prompt", message="Idle after Stop"),
                                  self.data("Notification", notification_type="elicitation_dialog", message="Late question")):
                        late = {**event, "session_id": root, **fields}
                        self.assertIsNone(self.queued(agent, late))
                        self.assertEqual(self.session_status(agent, actor or root), expected)
                # A fresh prompt can start a new root turn after completion.
                self.queued(agent, base)
                self.assertIsNotNone(self.queued(agent, {**base, "hook_event_name": "PermissionRequest", "prompt": "New turn approval"}))

    def test_late_claude_input_cannot_reopen_coordination_or_callback_state(self):
        root = self.data()["session_id"]
        child = "finished-child"
        with patch.object(callback.subprocess, "Popen"):
            claude_sessions.handle_hook(self.data("UserPromptSubmit"))
            claude_sessions.handle_hook(self.data("SubagentStart", agent_id=child))
            claude_sessions.handle_hook(self.data("SubagentStop", agent_id=child))
            claude_sessions.handle_hook(self.data("Stop"))
            for actor in ("", child):
                fields = {"agent_id": actor} if actor else {}
                for name, more in (("Notification", {"notification_type": "idle_prompt"}),
                                   ("Notification", {"notification_type": "permission_prompt"}),
                                   ("PermissionRequest", {"prompt": "Stale approval"})):
                    claude_sessions.handle_hook(self.data(name, **more, **fields))
                    self.assertEqual(self.session_status("claude", actor or root), "completed")
                    team = codex_sessions.active_team(claude_sessions.identity(root))
                    row = codex_sessions.member(team, claude_sessions.identity(root, actor))
                    self.assertEqual(row["state"], "reported" if actor else "idle")
        self.drain()
        self.assertEqual([body["event"] for body, _ in self.requests], ["run_finished"])

    def test_state_and_spool_permissions_are_private_from_creation(self):
        callback.rex.state_dir()
        callback.rex.STATE.chmod(0o755)
        created = []
        original = os.open
        original_connect = callback.sqlite3.connect

        def opened(path, flags, mode=0o777, **kwargs):
            descriptor = original(path, flags, mode, **kwargs)
            if str(path).startswith(str(self.root)) and flags & os.O_CREAT:
                created.append(Path(path).name)
                self.assertEqual(mode, 0o600)
                self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            return descriptor

        def connected(path, *args, **kwargs):
            self.assertTrue(Path(path).exists())  # SQLite never gets to create a permissive database.
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            return original_connect(path, *args, **kwargs)

        previous_umask = os.umask(0)
        try:
            with patch.object(os, "open", side_effect=opened), patch.object(callback.sqlite3, "connect", side_effect=connected):
                path = self.queued("codex", self.data("Stop"))
                self.assertEqual(callback.rex.STATE.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertTrue(callback.deliver(path))  # Cached body rewrite uses the same secure creator.
                with callback.database() as db:
                    db.execute("CREATE TABLE permissions_mode(value INT)")
                    db.execute("INSERT INTO permissions_mode VALUES (1)")
                    journal = callback.rex.STATE / "grokbot-callback.sqlite3-journal"
                    self.assertTrue(journal.exists())
                    self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                with codex_sessions.database() as db:
                    db.execute("INSERT INTO active_teams VALUES (?,?)", ("permission-session", "permission-team"))
                    journal = codex_sessions.DB.with_name(codex_sessions.DB.name + "-journal")
                    self.assertTrue(journal.exists())
                    self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                with patch.object(callback, "RECEIPT_MAX_BYTES", 512):
                    for _ in range(10):
                        callback.receipt("started", task_id="permissions")
        finally:
            os.umask(previous_umask)
        self.assertEqual(sum(name.endswith(".tmp") for name in created), 2)
        self.assertTrue({"grokbot-callback.sqlite3", "coordination.sqlite3", "grokbot-receipts.lock",
                         "grokbot-callback-receipts.jsonl", "grokbot-callback-receipts.jsonl.1"}.issubset(created))
        self.assertTrue(any(name.startswith("grokbot-lock-") for name in created))
        self.assertTrue(any(name.startswith("grokbot-sent-") for name in created))
        for directory in (self.root, callback.rex.STATE):
            for file in directory.iterdir():
                if file.is_file():
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600, file.name)

    def test_receipt_rotation_is_bounded_and_failed_spools_remain_visible(self):
        for invalid_config in (False, True):
            turn = "invalid-config" if invalid_config else "http-failure"
            self.queued("codex", self.data("UserPromptSubmit", turn_id=turn))
            path = self.queued("codex", self.data("Stop", turn_id=turn))
            if invalid_config:
                self.config.chmod(0o644)
            else:
                self.statuses[:] = [503, 503, 503]
            self.assertFalse(callback.deliver(path))
            self.config.chmod(0o600)
            envelope = json.loads(path.read_text())
            key = callback.delivery_key(envelope)
            with patch.object(callback, "RECEIPT_MAX_BYTES", 1024), ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(self.noise_receipt, range(50)))
            logs = sorted(callback.rex.STATE.glob("grokbot-callback-receipts.jsonl*"))
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(log.stat().st_size <= 1024 for log in logs))
            rows = [json.loads(line) for line in callback.receipt_lines()]
            self.assertFalse(any(row.get("key") == key for row in rows))
            outcome = "config_invalid" if invalid_config else "delivery_failed"
            self.assertTrue(any(row["key"] == key and row["outcome"] == outcome for row in callback.health()["failed"]))
            self.assertEqual(callback.retry(), {"delivered": 1, "failed": 0})
            self.assertEqual(self.requests[-1][0], envelope["body"])
            self.assertEqual(callback.health()["failed"], [])

    def noise_receipt(self, index):
        callback.receipt("started", task_id=f"rotation-{index}")

    def test_cleanup_removes_only_stale_unlocked_callback_temporaries(self):
        callback.rex.state_dir()
        old = callback.time.time() - callback.ORPHAN_TMP_AGE_SECONDS - 60
        stale = callback.rex.STATE / "grokbot-pending-orphan.tmp"
        locked = callback.rex.STATE / "grokbot-pending-writer.tmp"
        fresh = callback.rex.STATE / "grokbot-pending-fresh.tmp"
        unrelated = callback.rex.STATE / "other-runtime.tmp"
        for path in (stale, locked, fresh, unrelated):
            path.write_text("partial")
            if path != fresh:
                os.utime(path, (old, old))
        with locked.open("r+") as stream:
            callback.fcntl.flock(stream, callback.fcntl.LOCK_EX)
            self.assertEqual(callback.health()["pending"], [])
            self.assertFalse(stale.exists())
            self.assertTrue(all(path.exists() for path in (locked, fresh, unrelated)))
        self.assertEqual(callback.retry(), {"delivered": 0, "failed": 0})
        self.assertFalse(locked.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_status_caps_history_but_retains_live_sessions_and_actionable_spools(self):
        with patch.object(callback, "STATUS_HISTORY_LIMIT", 3):
            first = None
            for index in range(7):
                data = self.data("UserPromptSubmit", session_id=f"history-{index}")
                self.queued("codex", data)
                path = self.queued("codex", {**data, "hook_event_name": "Stop"})
                first = first or path
            key = callback.delivery_key(json.loads(first.read_text()))
            callback.receipt("delivery_failed", key=key, event="run_finished")
            for index in range(7):
                callback.receipt("delivery_failed", key=f"orphan-{index}", event="run_finished")
            for index in range(2):
                self.queued("codex", self.data("UserPromptSubmit", session_id=f"active-{index}"))
            output = io.StringIO()
            with patch.object(callback.sys, "argv", ["grokbot_callback.py", "status", "--json"]), redirect_stdout(output):
                self.assertEqual(callback.main(), 0)
            state = json.loads(output.getvalue())
            self.assertEqual(len(state["started"]), 3)
            self.assertEqual(len(state["running"]), 2)
            self.assertEqual(len(state["sessions"]), 5)
            self.assertEqual(len(state["pending"]), 6)
            self.assertEqual(len(state["failed"]), 4)
            self.assertIn(key, {row["key"] for row in state["failed"]})
            self.assertEqual({row["id"] for row in state["sessions"] if row["status"] == "completed"}, {"history-4", "history-5", "history-6"})
            with callback.database() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 9)

    def test_enum_and_typed_payloads_preserve_wire_values(self):
        self.assertEqual(json.loads(json.dumps({"event": callback.Event.NEEDS_INPUT, "kind": callback.InputKind.APPROVAL,
                                               "status": callback.Status.WAITING, "role": callback.Role.SUBAGENT,
                                               "notification_type": callback.NotificationType.PERMISSION_PROMPT,
                                               "member_state": callback.MemberState.WORKING})),
                         {"event": "needs_input", "kind": "approval", "status": "waiting", "role": "subagent",
                          "notification_type": "permission_prompt", "member_state": "working"})
        body = callback.payload(self.envelope())
        self.assertIsInstance(body, dict)
        self.assertIs(body["event"], callback.Event.RUN_FINISHED)
        self.assertIs(body["lineage"]["role"], callback.Role.AGENT)

    def test_distinct_approvals_in_one_turn_keep_duplicate_and_retry_identity(self):
        cases = (({"tool_use_id": "call-1", "prompt": "Proceed?"}, {"tool_use_id": "call-2", "prompt": "Proceed?"}),
                 ({"tool_name": "Bash", "tool_input": {"command": "ls", "description": "Proceed?"}},
                  {"tool_name": "Bash", "tool_input": {"description": "Proceed?", "command": "pwd"}}),
                 ({"prompt": "x" * 1000 + "first"}, {"prompt": "x" * 1000 + "second"}))
        for agent in ("codex", "claude"):
            for index, (first, second) in enumerate(cases):
                with self.subTest(agent=agent, case=index):
                    turn = f"{agent}-{index}"
                    self.queued(agent, self.data("UserPromptSubmit", turn_id=turn))
                    data = self.data("PermissionRequest", turn_id=turn, **first)
                    path = self.queued(agent, data)
                    self.assertIsNone(self.queued(agent, json.loads(json.dumps(data, sort_keys=True))))
                    other = self.queued(agent, self.data("PermissionRequest", turn_id=turn, **second))
                    self.assertIsNotNone(other)
                    self.assertNotEqual(path, other)
                    start = len(self.requests)
                    self.statuses[:] = [503, 503, 503]
                    self.assertFalse(callback.deliver(path))
                    self.assertIsNone(self.queued(agent, data))
                    self.assertTrue(callback.deliver(other))
                    self.assertEqual(callback.retry(), {"delivered": 1, "failed": 0})
                    received = [body for body, _ in self.requests[start:]]
                    self.assertEqual(len(received), 5)  # Three failed attempts, second approval, first retry.
                    self.assertEqual(len({body["delivery_key"] for body in received}), 2)
                    self.assertEqual(received[0], received[-1])
                    self.assertEqual(received[0]["delivery_key"], received[1]["delivery_key"])
                    self.assertIsNone(self.queued(agent, data))
                    self.assertIsNone(self.queued(agent, self.data("PermissionRequest", turn_id=turn, **second)))

    def test_repo_controlled_values_and_legacy_cached_payload_are_untrusted(self):
        envelope = {**self.envelope(cwd="/work/repo-controlled-path", hook_event_name="PermissionRequest", prompt="Proceed?"),
                    "event": "needs_input", "kind": "approval"}
        envelope["metadata"] = {"repo": "/repo/repo-controlled-name", "branch": "feature/repo-controlled-branch", "head_sha": "abc123"}
        body = callback.payload(envelope)
        self.assertEqual(body["task_id"], "/repo/repo-controlled-name:feature/repo-controlled-branch")
        self.assertEqual(set(body["untrusted"]), {"last_message", "prompt_text", "cwd", "repo", "branch", "head_sha", "task_id"})
        envelope["body"] = {**body, "delivery_key": "a" * 64, "untrusted": ["last_message", "prompt_text"]}
        self.assertTrue(callback.deliver(self.spool(envelope)))
        received = self.requests[-1][0]
        self.assertEqual(received["delivery_key"], "a" * 64)
        self.assertEqual(received["untrusted"], body["untrusted"])
        for field in ("cwd", "repo", "branch", "head_sha", "task_id"):
            self.assertEqual(received[field], body[field])
        self.assertEqual(callback.post({"event": "test", "task_id": body["task_id"]}), 204)
        self.assertEqual(self.requests[-1][0]["untrusted"], ["task_id"])

    def test_tool_activity_resumes_waiting_root_and_child_locally(self):
        root = self.data()["session_id"]
        child = "11111111-1111-4111-8111-111111111111"
        with patch.object(callback.subprocess, "Popen"):
            for adapter, agent in ((codex_sessions, "codex"), (claude_sessions, "claude")):
                adapter.handle_hook(self.data("UserPromptSubmit"))
                adapter.handle_hook(self.data("SubagentStart", agent_id=child))
                for name in ("PreToolUse", "PostToolUse"):
                    for actor in ("", child):
                        fields = {"agent_id": actor} if actor else {}
                        request = self.data("PermissionRequest", prompt="Proceed?", **fields)
                        adapter.handle_hook(request)
                        self.assertEqual(self.session_status(agent, actor or root), "waiting")
                        pending = len(callback.health()["pending"])
                        adapter.handle_hook(self.data(name, tool_name="Bash", **fields))
                        self.assertEqual(self.session_status(agent, actor or root), "running")
                        self.assertEqual(len(callback.health()["pending"]), pending)
                        member = next(row for row in codex_sessions.callback_members(request, agent) if row["thread"] == callback.actor_key(agent, root, actor))
                        self.assertEqual(member["state"], "working")
                adapter.handle_hook(self.data("PostToolUse", tool_name="Bash"))
                self.assertTrue(all(row["status"] == "running" for row in callback.health()["sessions"] if row["agent"] == agent))
        self.assertEqual(self.requests, [])

    def test_task_identity_is_captured_and_requester_preserved(self):
        with patch.dict(os.environ, {"GROKBOT_TASK_ID": "repo:task-a"}):
            first = self.queued("codex", self.data("Stop", turn_id="same-turn"))
        with patch.dict(os.environ, {"GROKBOT_TASK_ID": "repo:task-b", "GROKBOT_CALLBACK": "another-bot"}):
            self.queued("codex", self.data("UserPromptSubmit", turn_id="same-turn"))
            second = self.queued("codex", self.data("Stop", turn_id="same-turn"))
        callback.deliver(first)
        callback.deliver(second)
        self.assertEqual([(body["task_id"], body["requester"]) for body, _ in self.requests],
                         [("repo:task-a", "dinesh"), ("repo:task-b", "another-bot")])
        self.assertEqual(callback.payload(self.envelope())["task_id"], "/repo:feature")
        with patch.object(callback, "git_metadata", return_value={"repo": "", "branch": "", "head_sha": ""}):
            self.assertEqual(callback.payload(self.envelope())["task_id"], str(self.root) + ":")

    def test_codex_refined_lineage_and_root_children_summary(self):
        root = self.data()["session_id"]
        first = "11111111-1111-4111-8111-111111111111"
        second = "22222222-2222-4222-8222-222222222222"
        with patch.object(callback.subprocess, "Popen"):
            codex_sessions.handle_hook(self.data("UserPromptSubmit"))
            for child in (first, second):
                codex_sessions.handle_hook(self.data("SubagentStart", agent_id=child))
            codex_sessions.register(root, second, "nested", parent=first)
            codex_sessions.handle_hook(self.data("SubagentStop", agent_id=second))
            codex_sessions.handle_hook(self.data("SubagentStop", agent_id=first))
            codex_sessions.handle_hook(self.data("Stop"))
        self.drain()
        finished = [body for body, _ in self.requests if body["event"] == "run_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["children"], [{"id": first, "depth": 1, "status": "completed"},
                                                  {"id": second, "depth": 2, "status": "completed"}])
        self.assertEqual(len(self.requests), 1)
        child_end = next(row for row in callback.health()["sessions"] if row["id"] == second)
        self.assertEqual(child_end["status"], "completed")
        self.assertEqual(child_end["lineage"], {"requester_bot": "dinesh", "root_session_id": root,
                         "parent_session_id": first, "agent_session_id": second, "depth": 2,
                         "agent_kind": "codex", "role": "subagent"})

    def test_claude_native_and_nested_cli_never_finish_as_roots(self):
        root = "claude-root"
        first = {**self.data("UserPromptSubmit"), "session_id": root}
        self.queued("claude", first)
        self.queued("claude", {**first, "hook_event_name": "SubagentStart", "agent_id": "native-child"})
        self.queued("claude", {**first, "hook_event_name": "SubagentStop", "agent_id": "native-child"})
        nested = {**first, "session_id": "nested-cli"}
        with patch.dict(os.environ, {"GROKBOT_PARENT_SESSION": "native-child"}):
            self.queued("claude", nested)
            self.queued("claude", {**nested, "hook_event_name": "Stop"})
        self.queued("claude", {**first, "hook_event_name": "Stop"})
        self.drain()
        finished = [body for body, _ in self.requests if body["event"] == "run_finished"]
        self.assertEqual([body["agent_session_id"] for body in finished], [root])
        self.assertEqual(finished[0]["children"], [{"id": "native-child", "depth": 1, "status": "completed"},
                                                  {"id": "nested-cli", "depth": 2, "status": "completed"}])

    def test_cross_provider_child_and_no_unrelated_opt_in(self):
        root = self.data()["session_id"]
        self.queued("codex", self.data("UserPromptSubmit"))
        child = {**self.data("SessionStart"), "session_id": "claude-child"}
        with patch.dict(os.environ, {"GROKBOT_PARENT_SESSION": root}):
            path = self.queued("claude", child)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.queued("claude", {**child, "session_id": "manual-session"}))
            end = self.queued("claude", {**child, "hook_event_name": "Stop"})
        self.assertIsNone(path)
        self.assertIsNone(end)
        self.assertEqual(self.requests, [])
        body = next(row for row in callback.health()["sessions"] if row["id"] == "claude-child")
        self.assertEqual((body["status"], body["lineage"]["depth"], body["lineage"]["agent_kind"]), ("completed", 1, "claude"))
        self.assertEqual(body["lineage"]["root_session_id"], root)
        with patch.dict(os.environ, {"GROKBOT_PARENT_SESSION": "unknown"}):
            self.assertIsNone(self.queued("codex", {**self.data("Stop"), "session_id": "unregistered-nested"}))

    def test_heartbeat_running_health_and_session_end_without_completion(self):
        path = self.queued("codex", self.data("UserPromptSubmit"))
        self.assertIsNone(path)
        state = callback.health()
        self.assertEqual((len(state["pending"]), len(state["running"])), (0, 1))
        self.assertEqual(state["running"][0]["rex_block"], "rex-b")
        receipts = (self.root / "state/grokbot-callback-receipts.jsonl").read_text()
        self.assertIn('"outcome": "started"', receipts)
        self.assertIn('"rex_block": "rex-b"', receipts)
        self.assertEqual(state["started"][0]["rex_block"], "rex-b")
        self.assertEqual(state["sessions"][0]["status"], "running")
        self.drain()
        self.assertEqual(self.requests, [])
        self.assertIsNone(self.queued("codex", self.data("Stop", background_tasks=[{"id": "worker"}])))
        self.assertEqual(callback.health()["running"][0]["status"], "waiting")
        self.queued("codex", self.data("SessionEnd"))
        self.assertEqual(callback.health()["running"], [])
        self.assertEqual(self.requests, [])

    def test_heartbeat_and_lineage_stay_local_for_both_providers(self):
        for agent in ("codex", "claude"):
            self.assertIsNone(self.queued(agent, self.data("UserPromptSubmit")))
            for name in ("SubagentStart", "SubagentStop"):
                self.assertIsNone(self.queued(agent, self.data(name, agent_id="child")))
        self.drain()
        self.assertEqual(self.requests, [])
        self.assertEqual(list((self.root / "state").glob("grokbot-pending-*")), [])
        output = io.StringIO()
        with patch.object(callback.sys, "argv", ["grokbot_callback.py", "status", "--json"]), redirect_stdout(output):
            self.assertEqual(callback.main(), 0)
        state = json.loads(output.getvalue())
        self.assertEqual(len(state["started"]), 2)
        children = [row for row in state["sessions"] if row["lineage"]["role"] == "subagent"]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(row["status"] == "completed" and row["lineage"]["depth"] == 1 for row in children))
        self.assertTrue(all(row["rex_block"] == "rex-b" for row in children))
        for agent in ("codex", "claude"):
            self.queued(agent, self.data("PermissionRequest", prompt="Proceed?"))
            self.queued(agent, self.data("Stop"))
        self.drain()
        self.assertEqual(sorted(body["event"] for body, _ in self.requests), ["needs_input", "needs_input", "run_finished", "run_finished"])
        for body, _ in self.requests:
            if body["event"] == "run_finished":
                self.assertEqual(body["children"], [{"id": "child", "depth": 1, "status": "completed"}])

    def test_retry_retires_legacy_local_event_spools_without_http(self):
        callback.rex.state_dir()
        for event in ("heartbeat", "lineage"):
            envelope = {**self.envelope(), "event": event, "status": "running"}
            envelope["body"] = callback.payload(envelope)
            key = callback.delivery_key(envelope)
            path = self.root / "state" / ("grokbot-pending-" + key)
            path.write_text(json.dumps(envelope))
            callback.receipt("delivery_failed", key=key, event=event)
            self.assertEqual(callback.post(envelope["body"]), 0)
        self.assertEqual(callback.health()["pending"], [])
        self.assertEqual(callback.health()["failed"], [])
        self.assertEqual(callback.retry(), {"delivered": 0, "failed": 0})
        self.assertEqual(list((self.root / "state").glob("grokbot-pending-*")), [])
        self.assertEqual(self.requests, [])

    def test_failed_spool_retry_freezes_payload_and_health_is_safe(self):
        path = self.queued("codex", self.data("Stop", last_assistant_message="private text"))
        self.statuses[:] = [503, 503, 503]
        self.assertFalse(callback.deliver(path))
        self.assertTrue(path.exists())
        self.assertEqual(len(callback.health()["failed"]), 1)
        self.assertEqual(callback.health()["pending"], [])
        output = io.StringIO()
        with patch.object(callback.sys, "argv", ["grokbot_callback.py", "status", "--json"]), redirect_stdout(output):
            self.assertEqual(callback.main(), 0)
        self.assertNotIn("private text", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())
        with patch.object(callback, "git_metadata", return_value={"repo": "changed", "branch": "changed", "head_sha": "changed"}):
            self.assertEqual(callback.retry(), {"delivered": 1, "failed": 0})
        self.assertEqual(self.requests[0][0], self.requests[-1][0])
        self.assertFalse(path.exists())
        self.assertEqual(callback.health()["failed"], [])

    def test_children_have_independent_prompt_turns(self):
        self.queued("claude", self.data("UserPromptSubmit", turn_id="root-turn"))
        child = self.data("UserPromptSubmit", agent_id="child")
        for _ in range(2):
            self.queued("claude", child)
            self.queued("claude", {**child, "hook_event_name": "PermissionRequest", "prompt": "Proceed?"})
        self.queued("claude", self.data("Stop", turn_id="root-turn"))
        self.drain()
        self.assertEqual(sum(body["event"] == "needs_input" for body, _ in self.requests), 2)
        self.assertEqual(sum(body["event"] == "run_finished" for body, _ in self.requests), 1)


    def test_stale_native_turn_cannot_notify_a_new_task(self):
        with patch.dict(os.environ, {"GROKBOT_TASK_ID": "task-a"}):
            self.queued("codex", self.data("UserPromptSubmit", turn_id="turn-a"))
        with patch.dict(os.environ, {"GROKBOT_TASK_ID": "task-b", "GROKBOT_CALLBACK": "bot-b"}):
            self.queued("codex", self.data("UserPromptSubmit", turn_id="turn-b"))
            self.assertIsNone(self.queued("codex", self.data("Stop", turn_id="turn-a")))
            self.assertIsNone(self.queued("codex", self.data("PermissionRequest", turn_id="turn-a", prompt="Old approval")))
            self.queued("codex", self.data("Stop", turn_id="turn-b"))
        self.drain()
        finished = [body for body, _ in self.requests if body["event"] == "run_finished"]
        self.assertEqual([(body["task_id"], body["requester"], body["turn_id"]) for body in finished], [("task-b", "bot-b", "turn-b")])

    def test_final_transcript_is_frozen_before_worker_launch(self):
        transcript = self.root / "turns.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Task A done"}]}}))
        data = self.data("Stop", transcript_path=str(transcript))
        del data["last_assistant_message"]
        path = self.queued("claude", data)
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Task B private"}]}}))
        callback.deliver(path)
        self.assertEqual(self.requests[0][0]["last_message"], "Task A done")

    def test_claude_session_end_updates_active_child_health(self):
        with patch.object(callback.subprocess, "Popen"):
            claude_sessions.handle_hook(self.data("UserPromptSubmit"))
            claude_sessions.handle_hook(self.data("SubagentStart", agent_id="active-child"))
            self.assertEqual(len(callback.health()["running"]), 2)
            claude_sessions.handle_hook(self.data("SessionEnd"))
        self.assertEqual(callback.health()["running"], [])

    def test_callback_state_tracks_native_child_resume(self):
        root = self.data()["session_id"]
        child = "11111111-1111-4111-8111-111111111111"
        with patch.object(callback.subprocess, "Popen"):
            codex_sessions.handle_hook(self.data("UserPromptSubmit"))
            codex_sessions.handle_hook(self.data("SubagentStart", agent_id=child))
            codex_sessions.handle_hook(self.data("PermissionRequest", agent_id=child, prompt="First approval"))
            codex_sessions.handle_hook(self.data("SubagentStop", agent_id=child))
            codex_sessions.handle_hook(self.data("PostToolUse", tool_name="followup_task", tool_input={"target": child}))
        self.assertEqual(codex_sessions.member(root, child)["state"], "working")
        self.assertEqual({row["id"] for row in callback.health()["running"]}, {root, child})
        self.queued("codex", self.data("PermissionRequest", agent_id=child, prompt="Second approval"))
        self.drain()
        self.assertEqual(sum(body["event"] == "needs_input" for body, _ in self.requests), 2)


    def test_prompt_without_native_id_retires_previous_turn(self):
        self.queued("codex", self.data("UserPromptSubmit", turn_id="old"))
        self.queued("codex", self.data("UserPromptSubmit"))
        self.assertIsNone(self.queued("codex", self.data("Stop", turn_id="old")))
        fresh = self.queued("codex", self.data("PermissionRequest", turn_id="new", prompt="Current approval"))
        self.assertIsNotNone(fresh)
        self.assertEqual(json.loads(fresh.read_text())["turn"], "new")

    def test_atomic_spool_failure_does_not_poison_dedupe(self):
        with patch.object(callback.json, "dump", side_effect=OSError("test-secret")):
            self.assertIsNone(self.queued("codex", self.data("Stop")))
        self.assertEqual(list((self.root / "state").glob("grokbot-pending-*")), [])
        path = self.queued("codex", self.data("Stop"))
        self.assertIsNotNone(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(callback.deliver(path))

    def test_nested_cli_parent_environment_registers_cross_provider_edge(self):
        root = self.data()["session_id"]
        with patch.object(callback.subprocess, "Popen"):
            codex_sessions.handle_hook(self.data("UserPromptSubmit"))
            with patch.dict(os.environ, {"GROKBOT_PARENT_SESSION": root}):
                claude_sessions.handle_hook({**self.data("SessionStart"), "session_id": "nested-claude"})
            nested = claude_sessions.identity("nested-claude")
            self.assertEqual(codex_sessions.member(root, nested)["parent"], root)
            with patch.dict(os.environ, {"GROKBOT_PARENT_SESSION": "nested-claude"}):
                child = "33333333-3333-4333-8333-333333333333"
                codex_sessions.handle_hook(self.data("SessionStart", session_id=child))
                codex_sessions.handle_hook(self.data("Stop", session_id=child))
            self.assertEqual(codex_sessions.member(root, child)["parent"], nested)
        self.drain()
        self.assertEqual(self.requests, [])
        self.assertTrue(any(row["lineage"]["depth"] == 2 for row in callback.health()["sessions"]))

    def test_child_failure_and_waiting_status_match_webhooks(self):
        with patch.object(callback.subprocess, "Popen"):
            claude_sessions.handle_hook(self.data("UserPromptSubmit"))
            claude_sessions.handle_hook(self.data("SubagentStart", agent_id="child"))
            claude_sessions.handle_hook(self.data("Notification", agent_id="child", notification_type="trust_prompt", message="Trust?"))
            self.assertEqual(next(row for row in callback.health()["running"] if row["id"] == "child")["status"], "waiting")
            claude_sessions.handle_hook(self.data("SubagentStop", agent_id="child", status="failed"))
            claude_sessions.handle_hook(self.data("Stop"))
        self.drain()
        finished = next(body for body, _ in self.requests if body["event"] == "run_finished")
        self.assertEqual(finished["children"], [{"id": "child", "depth": 1, "status": "failed"}])


if __name__ == "__main__":
    unittest.main()
