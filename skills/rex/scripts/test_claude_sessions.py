#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claude_sessions as claude
import codex_sessions as store


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        for item in (patch.object(store, "DB", self.path / "coordination.sqlite3"),
                     patch.object(store, "reopen_activity"), patch.dict(os.environ, {}, clear=True)):
            item.start(); self.addCleanup(item.stop)
        self.session = "opaque-session:not-a-uuid"
        self.root = claude.identity(self.session)
        self.team = "claude-" + self.session

    def hook(self, event, **data):
        return claude.handle_hook({"session_id": self.session, "hook_event_name": event, "cwd": str(self.path), **data})

    def test_identity_is_provider_namespaced_stable_and_preserves_opaque_ids(self):
        self.assertEqual(claude.identity(self.session, "agent-a7"), claude.identity(self.session, "agent-a7"))
        self.assertNotEqual(claude.identity(self.session), claude.identity(self.session, "agent-a7"))
        self.assertNotEqual(claude.identity(self.session, "agent-a7"), claude.identity("other", "agent-a7"))
        self.hook("SessionStart", model="haiku", transcript_path="root.jsonl")
        self.hook("SubagentStart", agent_id="agent-a7", agent_type="researcher")
        child = store.member(self.team, claude.identity(self.session, "agent-a7"))
        self.assertEqual((child["provider"], child["native_session"], child["native_id"]), ("claude", self.session, "agent-a7"))
        self.assertEqual(child["parent"], self.root)
        self.assertEqual(child["parent_source"], "hook")
        self.assertEqual(child["model"], "")  # Hook does not claim an unobserved inherited model.
        self.assertEqual(store.member(self.team, self.root)["model"], "haiku")
        self.assertEqual(child["transcript"], "")
        for invalid in ("", "line\nbreak", 123, "x" * 257):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): claude.identity(invalid)

    def test_native_tool_actor_does_not_overwrite_root_and_parent_can_be_refined(self):
        self.hook("SessionStart")
        for agent in ("first", "second"):
            self.hook("SubagentStart", agent_id=agent)
        claude.register(self.team, self.session, "second", parent_agent="first", provisional=False)
        self.hook("PreToolUse", agent_id="second", tool_name="Bash", transcript_path="root.jsonl")
        child = store.member(self.team, claude.identity(self.session, "second"))
        self.assertEqual(child["parent"], claude.identity(self.session, "first"))
        self.assertEqual(child["parent_source"], "explicit")
        self.assertEqual(child["state"], "working")
        self.assertEqual(child["transcript"], "")
        self.assertEqual(store.member(self.team, self.root)["state"], "idle")

    def test_stop_payload_is_authoritative_and_background_work_stays_waiting(self):
        self.hook("UserPromptSubmit")
        self.hook("Stop", last_assistant_message="Waiting for the worker", background_tasks=[{"id": "t1", "type": "subagent", "status": "running", "command": "do not log"}])
        self.assertEqual(store.member(self.team, self.root)["state"], "waiting")
        self.assertNotIn("do not log", store.snapshot(self.team)["events"][-1]["text"])
        self.hook("TeammateIdle", teammate_name="unknown-id")
        self.assertEqual(store.member(self.team, self.root)["state"], "waiting")
        self.hook("SubagentStop", agent_id="agent-a7", agent_transcript_path="child.jsonl", last_assistant_message="Exact final result")
        child = store.member(self.team, claude.identity(self.session, "agent-a7"))
        self.assertEqual((child["transcript"], child["state"]), ("child.jsonl", "reported"))
        self.assertEqual(store.snapshot(self.team)["events"][-1]["text"], "Exact final result")
        self.hook("StopFailure", error="rate_limit")
        self.assertEqual(store.member(self.team, self.root)["state"], "blocked")
        self.hook("SessionEnd")
        self.assertEqual(store.member(self.team, self.root)["state"], "ended")

    def test_session_end_finishes_only_children_of_that_native_session(self):
        self.hook("SubagentStart", agent_id="child")
        claude.handle_hook({"session_id": "independent-background", "hook_event_name": "UserPromptSubmit"})
        self.hook("SessionEnd")
        self.assertEqual(store.member(self.team, claude.identity(self.session, "child"))["state"], "ended")
        self.assertEqual(store.member("claude-independent-background", claude.identity("independent-background"))["state"], "working")

    def test_independent_background_inherits_provisional_parent_and_explicit_join_refines_it(self):
        self.hook("SessionStart")
        self.hook("SubagentStart", agent_id="actual-caller")
        with patch.dict(os.environ, {"REX_COORD_THREAD": self.root, "REX_COORD_SESSION": self.session, "REX_COORD_TEAM": self.team}):
            claude.handle_hook({"session_id": "background-native-id", "hook_event_name": "SessionStart", "model": "haiku"})
        background = claude.identity("background-native-id")
        row = store.member(self.team, background)
        self.assertEqual((row["parent"], row["parent_source"]), (self.root, "hook"))
        claude.register(self.team, "background-native-id", parent_session=self.session, parent_agent="actual-caller", provisional=False)
        row = store.member(self.team, background)
        self.assertEqual((row["parent"], row["parent_source"]), (claude.identity(self.session, "actual-caller"), "explicit"))
        self.assertEqual(row["role"], "coordinator")
        claude.handle_hook({"session_id": "background-native-id", "hook_event_name": "Stop"})
        self.assertEqual(store.member(self.team, background)["parent_source"], "explicit")

    def test_claude_cannot_enter_codex_queue_or_codex_interactive_resume(self):
        self.hook("SessionStart")
        with patch.object(store.subprocess, "run") as execute:
            with self.assertRaisesRegex(ValueError, "Codex queue"):
                store.send(self.team, self.root, self.root, "message")
            with self.assertRaisesRegex(ValueError, "read-only"):
                store.open_view(self.team, self.root, interactive=True)
            execute.assert_not_called()

    def test_claude_transcripts_are_parsed_without_codex_payload_or_wrong_actor_text(self):
        path = self.path / "child.jsonl"
        rows = [{"type": "assistant", "sessionId": self.session, "agentId": "child", "message": {"content": [{"type": "text", "text": "Observed child update"}]}},
                {"type": "assistant", "sessionId": self.session, "agentId": "other", "message": {"content": [{"type": "text", "text": "Wrong actor"}]}},
                {"type": "assistant", "sessionId": "other", "message": {"content": [{"type": "text", "text": "Wrong session"}]}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "Private user text"}]}},
                {"type": "response_item", "payload": {"role": "assistant", "content": "Codex schema"}}]
        path.write_text("\n".join(map(json.dumps, rows)))
        self.assertEqual(claude.assistant_lines(str(path), self.session, "child"), ["Observed child update"])

    def test_session_environment_uses_supported_file_and_not_codex_identity(self):
        path = self.path / "environment"
        with patch.dict(os.environ, {"CLAUDE_ENV_FILE": str(path)}): self.hook("SessionStart")
        content = path.read_text()
        self.assertIn("export REX_COORD_PROVIDER=claude", content)
        self.assertIn("REX_COORD_THREAD", content)
        self.assertNotIn("CODEX_THREAD_ID", content)
        self.hook("PostToolUse", agent_id="worker", tool_name="SendMessage", tool_input={"recipient": "lead", "content": "do not duplicate private message"})
        self.assertEqual(store.snapshot(self.team)["events"][-1]["text"], "Native SendMessage to lead")


if __name__ == "__main__":
    unittest.main()
