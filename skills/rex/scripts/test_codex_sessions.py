#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import codex_sessions as cs

A = "01a1a940-6afe-72e0-9c4d-3b7d8b69ecd0"
B = "01a1a940-6afe-72e0-9c4d-3b7d8b69ecd1"
C = "01a1a940-6afe-72e0-9c4d-3b7d8b69ecd2"


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = patch.object(cs, "DB", Path(self.folder.name) / "state.sqlite3")
        self.db.start()
        self.addCleanup(self.db.stop)
        cs.register("task", A, "main", activate=True)
        cs.register("task", B, "worker")

    def track_view(self, thread=B, session="main-rex", block="owned", token="generation"):
        with cs.database() as db:
            db.execute("INSERT INTO views(team,thread,session,block,token) VALUES (?,?,?,?,?)", ("task", thread, session, block, token))

    def test_open_uses_root_main_session_and_never_creates_session(self):
        cs.register("task", B, "worker", parent=A)
        layout = {"windows": [{"label": "Main", "layers": [{"blocks": [{"block_id": "codex"}]}]}]}
        with patch.dict(cs.os.environ, {}, clear=True), patch.object(cs.rex, "main_session", return_value="main-rex", create=True) as resolve, patch.object(cs.rex, "state_dir"), patch.object(cs.rex, "uv_executable", return_value="/opt/tools/uv"), patch.object(cs.rex, "rex_json", side_effect=[layout, {"block_ids": ["owned"]}]) as rex_json:
            view = cs.open_view("task", B)
        resolve.assert_called_once_with(thread=A)
        self.assertEqual(view["session"], "main-rex")
        self.assertEqual(rex_json.call_args_list[1].args[0][:4], ["window", "new", "-s", "main-rex"])
        self.assertIn("--view-token", rex_json.call_args_list[1].args[0])
        launch = rex_json.call_args_list[1].args[0]
        child = launch[launch.index("--") + 1:]
        self.assertEqual(child[:4], ["/opt/tools/uv", "run", "--script", str(Path(cs.__file__).resolve())])
        self.assertEqual(child[4], "watch")
        self.assertFalse(any(call.args[0][0] == "new" for call in rex_json.call_args_list))

    def test_reader_receives_explicit_nonsecret_state_directory(self):
        with patch.dict(cs.os.environ, {"REX_VISIBLE_STATE_DIR": self.folder.name, "REX_SERVER": "unix:///test/rex.sock"}), patch.object(cs.rex, "STATE", Path(self.folder.name)), patch.object(cs.rex, "main_session", return_value="main-rex"), patch.object(cs.rex, "state_dir"), patch.object(cs.rex, "uv_executable", return_value="/opt/tools/uv"), patch.object(cs.rex, "rex_json", side_effect=[{"windows": []}, {"block_ids": ["owned"]}]) as rex_json:
            cs.open_view("task", B)
        arguments = rex_json.call_args.args[0]
        child = arguments[arguments.index("--") + 1:]
        self.assertEqual(child[:3], ["/usr/bin/env", "REX_VISIBLE_STATE_DIR=" + self.folder.name, "REX_SERVER=unix:///test/rex.sock"])
        self.assertEqual(child[3:7], ["/opt/tools/uv", "run", "--script", str(Path(cs.__file__).resolve())])

    def test_open_reuses_tracked_pane_and_prunes_missing_rows(self):
        self.track_view()
        self.track_view(thread="", block="gone")
        layout = {"windows": [{"layers": [{"blocks": [{"block_id": "owned"}]}]}]}
        with patch.object(cs.rex, "main_session", return_value="main-rex", create=True), patch.object(cs.rex, "state_dir"), patch.object(cs.rex, "rex_json", return_value=layout) as rex_json:
            self.assertEqual(cs.open_view("task", B)["block"], "owned")
        self.assertEqual(rex_json.call_count, 1)
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM views").fetchone()[0], 1)

    def test_open_migrates_only_owned_readonly_views(self):
        self.track_view(session="legacy")
        self.track_view(thread=A + "@interactive", session="legacy", block="interactive")
        with patch.object(cs.rex, "main_session", return_value="main-rex", create=True), patch.object(cs.rex, "close_pane", create=True) as close, patch.object(cs.rex, "retire_empty_session") as retire, patch.object(cs.rex, "state_dir"), patch.object(cs.rex, "rex_json", side_effect=[{}, {"block_ids": ["new"]}]):
            cs.open_view("task", B)
        close.assert_called_once_with("legacy", "owned")
        retire.assert_called_once_with("legacy", "main-rex")
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT block FROM views WHERE thread=?", (A + "@interactive",)).fetchone()[0], "interactive")

    def test_missing_main_session_fails_without_creating_view(self):
        with patch.object(cs.rex, "main_session", side_effect=RuntimeError("ambiguous"), create=True), patch.object(cs.rex, "state_dir"), patch.object(cs.rex, "rex_json") as rex_json:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                cs.open_view("task", B)
        rex_json.assert_not_called()

    def test_retention_for_all_finished_states_and_board(self):
        for value in cs.INACTIVE_STATES:
            data = {"members": [{"thread": B, "state": value, "updated": 10}], "events": []}
            self.assertFalse(cs.retention_expired(data, B, 20, 49))
            self.assertTrue(cs.retention_expired(data, B, 20, 50))
            self.assertTrue(cs.retention_expired(data, "", 20, 50))
        for value in ("working", "waiting", "blocked"):
            data["members"][0]["state"] = value
            self.assertFalse(cs.retention_expired(data, "", 20, 100))

    def test_new_activity_extends_retention(self):
        data = {"members": [{"thread": B, "state": "reported", "updated": 10}], "events": [{"sender": B, "recipient": A, "created": 40}]}
        self.assertFalse(cs.retention_expired(data, B, 10, 69))
        self.assertTrue(cs.retention_expired(data, B, 10, 70))

    def test_close_only_exact_generation_and_never_interactive(self):
        self.track_view()
        self.track_view(thread=A + "@interactive", block="interactive")
        with patch.object(cs.rex, "close_pane", create=True) as close:
            cs.close_view("task", B, "old-generation")
            cs.close_view("task", A + "@interactive", "generation")
            close.assert_not_called()
            cs.close_view("task", B, "generation")
        close.assert_called_once_with("main-rex", "owned")
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM views").fetchone()[0], 1)

    def test_activity_recheck_preserves_view_that_resumed_before_close(self):
        self.track_view()
        with patch.object(cs, "retention_expired", return_value=False), patch.object(cs.rex, "close_pane", create=True) as close:
            self.assertFalse(cs.close_view("task", B, "generation", opened_at=0))
        close.assert_not_called()
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM views").fetchone()[0], 1)

    def test_watch_closes_owned_pane_even_on_render_error(self):
        self.track_view()
        with patch.object(cs, "snapshot", side_effect=RuntimeError("render failed")), patch.object(cs.rex, "close_pane", create=True) as close:
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                cs.watch("task", B, "generation")
        close.assert_called_once_with("main-rex", "owned")

    def test_failed_close_keeps_tracking_for_retry(self):
        self.track_view()
        with patch.object(cs.rex, "close_pane", side_effect=RuntimeError("unavailable"), create=True):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                cs.close_view("task", B, "generation")
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT token FROM views WHERE thread=?", (B,)).fetchone()[0], "generation")

    def test_new_prompt_reopens_idle_board(self):
        with patch.object(cs, "open_view") as opened:
            cs.handle_hook({"session_id": A, "hook_event_name": "UserPromptSubmit"})
        opened.assert_called_once_with("task")
        self.assertEqual(cs.member("task", A)["state"], "working")

    def test_followup_reopens_known_member_without_treating_messages_as_work(self):
        cs.state("task", B, "reported")
        with patch.object(cs, "open_view") as opened:
            cs.handle_hook({"session_id": A, "hook_event_name": "PostToolUse", "tool_name": "followup_task", "tool_input": {"target": B, "message": "Continue"}})
        self.assertEqual(cs.member("task", B)["state"], "working")
        self.assertEqual([call.args for call in opened.call_args_list], [("task",), ("task", B)])
        cs.state("task", B, "reported")
        with patch.object(cs, "open_view") as opened:
            cs.handle_hook({"session_id": A, "hook_event_name": "PostToolUse", "tool_name": "send_message", "tool_input": {"target": B, "message": "Thanks"}})
        opened.assert_not_called()
        self.assertEqual(cs.member("task", B)["state"], "reported")

    def test_watch_expiry_closes_and_removes_owned_view(self):
        self.track_view(thread="")
        with contextlib.redirect_stdout(io.StringIO()), patch.object(cs, "retention_expired", return_value=True), patch.object(cs.rex, "close_pane", create=True) as close:
            cs.watch("task", "", "generation")
        close.assert_called_once_with("main-rex", "owned")
        with cs.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM views").fetchone()[0], 0)

    def test_rejects_cross_team_and_invalid_ids(self):
        with self.assertRaises(ValueError):
            cs.member("other", B)
        with self.assertRaises(ValueError):
            cs.register("task", "../../other", "invalid")

    def test_exact_uuid_takes_precedence_over_alias(self):
        cs.register("task", C, B)
        self.assertEqual(cs.member("task", B)["thread"], B)

    def test_native_hook_preserves_unknown_authorship(self):
        cs.handle_hook({"session_id": A, "hook_event_name": "PostToolUse", "tool_name": "send_message", "tool_input": {"target": B, "message": "Progress"}})
        self.assertEqual(cs.snapshot("task")["events"][-1]["sender"], "native hook (actor unavailable)")

    def test_queue_is_not_acknowledgment(self):
        with patch.object(cs.shutil, "which", return_value="codex"), patch.object(cs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            result = cs.send("task", "main", "worker", "Check the shared interface", wake=True)
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["acknowledged"])
        self.assertIn("--resume", run.call_args.args[0])
        self.assertIn(B, run.call_args.args[0])
        self.assertNotIn("--remote", run.call_args.args[0])
        cs.acknowledge("task", B, result["message_id"])
        cs.acknowledge("task", B, result["message_id"])
        self.assertEqual(sum(e["kind"] == "ack" for e in cs.snapshot("task")["events"]), 1)
        with self.assertRaises(ValueError):
            cs.acknowledge("task", A, result["message_id"])

    def test_queue_resume_warning_is_preserved(self):
        with patch.object(cs.shutil, "which", return_value="codex"), patch.object(cs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "warning: failed to resume")):
            result = cs.send("task", A, B, "Report back", wake=True)
        self.assertEqual(result["status"], "queued")
        self.assertIn("failed to resume", result["notice"])

    def test_uncertain_queue_is_never_retried(self):
        with patch.object(cs.shutil, "which", return_value="codex"), patch.object(cs.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 20)) as run:
            with self.assertRaisesRegex(ValueError, "uncertain"):
                cs.send("task", A, B, "Review")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(cs.snapshot("task")["events"][-1]["kind"], "queue_uncertain")

    def test_native_message_uses_native_transport(self):
        cs.register("task", C, "native", mode="native")
        with patch.object(cs.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "native"):
                cs.send("task", A, C, "Hello")
        run.assert_not_called()

    def test_lifecycle_names_do_not_collide_on_uuid_prefix(self):
        with patch.object(cs, "open_view"):
            for child in (B, C):
                cs.handle_hook({"session_id": A, "agent_id": child, "agent_type": "worker", "hook_event_name": "SubagentStart"})
        self.assertEqual(len(cs.snapshot("task")["members"]), 3)

    def test_late_result_stays_with_its_original_assignment(self):
        with patch.object(cs, "open_view"):
            cs.handle_hook({"session_id": A, "agent_id": C, "agent_type": "worker", "hook_event_name": "SubagentStart"})
        cs.register("other", A, "main", activate=True)
        cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStop", "last_assistant_message": "Verified"})
        self.assertEqual(cs.member("task", C)["state"], "reported")
        self.assertFalse(cs.snapshot("other")["events"])
        with self.assertRaises(ValueError):
            cs.member("other", C)

    def test_explicit_join_refines_hook_parent_and_late_result_recipient(self):
        cs.register("task", B, "coordinator", parent=A, role="coordinator")
        with patch.object(cs, "open_view"):
            cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStart"})
        cs.register("task", C, "implementation", parent=B, rename=True)
        self.assertEqual(cs.member("task", C)["parent"], B)
        with cs.database() as db:
            self.assertEqual(tuple(db.execute("SELECT parent,team FROM delegations WHERE child=?", (C,)).fetchone()), (B, "task"))
        cs.register("other", A, "main", activate=True)
        cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStop", "last_assistant_message": "Verified"})
        self.assertEqual(cs.snapshot("task")["events"][-1]["recipient"], B)
        self.assertEqual(cs.member("task", C)["parent"], B)
        self.assertEqual(cs.member("task", C)["parent_source"], "explicit")
        self.assertFalse(cs.snapshot("other")["events"])

    def test_opaque_native_content_is_unavailable(self):
        opaque = "gAAAAA" + "A" * 200
        cs.handle_hook({"session_id": A, "hook_event_name": "PostToolUse", "tool_name": "send_message", "tool_input": {"target": B, "message": opaque}})
        text = cs.snapshot("task")["events"][-1]["text"]
        self.assertNotIn(opaque, text)
        self.assertIn("content unavailable", text)
        self.assertIn("native Codex thread", text)

    def test_refinement_cannot_change_explicit_owner_or_team(self):
        cs.register("task", B, "coordinator", parent=A)
        with patch.object(cs, "open_view"):
            cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStart"})
        cs.register("task", C, "worker", parent=B)
        for override in (False, True):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, "established parent"):
                cs.register("task", C, "worker", parent=A, refine_hook_parent=override)
        cs.register("other", A, "root")
        with self.assertRaisesRegex(ValueError, "another team"):
            cs.register("other", C, "worker", parent=A, refine_hook_parent=True)
        self.assertEqual(cs.delegated_team(C), "task")

    def test_refinement_rejects_cycle_and_parent_from_other_team(self):
        with patch.object(cs, "open_view"):
            cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStart"})
        cs.register("task", B, "nested", parent=C)
        with self.assertRaisesRegex(ValueError, "cycle"):
            cs.register("task", C, "worker", parent=B)
        other = "01a1a940-6afe-72e0-9c4d-3b7d8b69ecd3"
        cs.register("other", other, "root")
        with self.assertRaisesRegex(ValueError, "parent"):
            cs.register("task", C, "worker", parent=other)
        self.assertEqual(cs.member("task", C)["parent"], A)

    def test_legacy_native_edge_requires_explicit_confirmation(self):
        cs.register("task", B, "coordinator", parent=A)
        with patch.object(cs, "open_view"):
            cs.handle_hook({"session_id": A, "agent_id": C, "hook_event_name": "SubagentStart"})
        with cs.database() as db:
            db.execute("UPDATE members SET parent_source='unknown' WHERE thread=?", (C,))
        with self.assertRaisesRegex(ValueError, "established parent"):
            cs.register("task", C, "worker", parent=B)
        cs.register("task", C, "worker", parent=B, refine_hook_parent=True)
        self.assertEqual(cs.member("task", C)["parent_source"], "explicit")
        with self.assertRaisesRegex(ValueError, "established parent"):
            cs.register("task", C, "worker", parent=A, refine_hook_parent=True)

    def test_legacy_confirmation_requires_native_matching_delegation(self):
        cs.register("task", B, "worker", parent=A)
        with cs.database() as db:
            db.execute("UPDATE members SET parent_source='unknown' WHERE thread=?", (B,))
        cs.register("task", C, "coordinator")
        with self.assertRaisesRegex(ValueError, "established parent"):
            cs.register("task", B, "worker", parent=C, refine_hook_parent=True)
        with cs.database() as db:
            db.execute("UPDATE members SET mode='native' WHERE thread=?", (B,))
        with self.assertRaisesRegex(ValueError, "established parent"):
            cs.register("task", B, "worker", parent=C, refine_hook_parent=True)
        with cs.database() as db:
            db.execute("INSERT INTO delegations VALUES (?,?,?)", (B, C, "task"))
        with self.assertRaisesRegex(ValueError, "established parent"):
            cs.register("task", B, "worker", parent=C, refine_hook_parent=True)

    def test_board_hides_preexisting_opaque_event(self):
        opaque = "gAAAAA" + "A" * 200
        cs.event("task", A, "native_message", opaque, B)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(cs.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cs.watch("task", "")
        self.assertNotIn(opaque, output.getvalue())
        self.assertIn("content unavailable", output.getvalue())

    def test_opaque_transcript_text_and_string_content(self):
        path = Path(self.folder.name) / "opaque.jsonl"
        opaque = "gAAAAA" + "x" * 200
        records = [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": content}}
                   for content in ([{"type": "output_text", "text": opaque}], opaque)]
        path.write_text("\n".join(map(json.dumps, records)))
        lines = cs.assistant_lines(path, C)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("content unavailable", line)
            self.assertIn(C, line)
            self.assertNotIn(opaque, line)
        self.assertEqual(cs.native_content("gAAAAA is a literal prefix"), "gAAAAA is a literal prefix")

    def test_join_renames_without_hook_overwriting_name(self):
        cs.register("task", A, "lead", rename=True)
        cs.handle_hook({"session_id": A, "hook_event_name": "SessionStart"})
        self.assertEqual(cs.member("task", A)["name"], "lead")

    def test_root_and_nested_coordinator_metadata(self):
        self.assertEqual(cs.member("task", A)["role"], "coordinator")
        cs.register("task", B, "worker", parent=A, role="worker", model="gpt-6-luna", effort="low")
        cs.register("task", C, "child-coordinator", parent=B, role="coordinator", model="gpt-6.1-sol", effort="medium")
        self.assertEqual(cs.member("task", B)["parent"], A)
        self.assertEqual(cs.member("task", B)["model"], "gpt-6-luna")
        self.assertEqual(cs.member("task", C)["parent"], B)
        self.assertEqual(cs.member("task", C)["role"], "coordinator")
        with self.assertRaisesRegex(ValueError, "cycle"):
            cs.register("task", A, "main", parent=C)

    def test_parent_must_belong_to_the_same_team(self):
        cs.register("other", C, "other-root")
        with self.assertRaisesRegex(ValueError, "parent"):
            cs.register("task", B, "worker", parent=C)

    def test_transcript_view_omits_reasoning_and_other_roles(self):
        path = Path(self.folder.name) / "rollout.jsonl"
        records = [{"type": "response_item", "payload": {"type": "message", "role": role, "channel": channel, "content": [{"type": "output_text", "text": text}]}} for role, channel, text in [("assistant", "analysis", "private reasoning"), ("user", "final", "user input"), ("assistant", "commentary", "working"), ("assistant", "final", "verified")]]
        path.write_text("\n".join(map(json.dumps, records)))
        self.assertEqual(cs.assistant_lines(path), ["working", "verified"])


if __name__ == "__main__":
    unittest.main()
