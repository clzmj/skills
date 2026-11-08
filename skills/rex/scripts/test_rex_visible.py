#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import contextlib
import importlib.util
import io
import json
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rex_visible", Path(__file__).with_name("rex_visible.py"))
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)


class RoutingTests(unittest.TestCase):
    def test_important_commands(self):
        for command, kind in [
            ("cd /tmp && terraform -chdir=infra apply", "infra"),
            ("FOO=bar pnpm run dev", "server"),
            ("cargo test --workspace", "test"),
            ("tail -F app.log", "logs"),
            ("/bin/zsh -lc 'bun run build'", "build"),
            ("uv run pytest", "test"),
            ("uv run --no-project python -u -m http.server 0", "server"),
            ("npm ci", "install"),
        ]:
            with self.subTest(command=command):
                self.assertEqual(rv.classify(command), kind)

    def test_quick_reads_and_quoted_text_are_ignored(self):
        for command in ["rg terraform .", "git status -sb", "cat package.json", "printf '%s' 'terraform apply'", "rex run -- terraform apply"]:
            self.assertIsNone(rv.classify(command), command)

    def test_rex_in_one_segment_does_not_hide_direct_execution(self):
        self.assertEqual(rv.classify("rex ls --json; terraform apply"), "infra")

    def test_hook_denies_without_executing(self):
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "terraform apply"}}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(output), patch.object(rv.subprocess, "run") as execute:
            rv.hook()
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_remote_default_cannot_be_inherited(self):
        result = rv.subprocess.CompletedProcess([], 0, '{"endpoint":"unix:///run/user/1001/rex/server.sock"}', "")
        with patch.dict(rv.os.environ, {"REX_SERVER": "some-other-machine:22"}), patch.object(rv.shutil, "which", return_value="/bin/rex"), patch.object(rv.subprocess, "run", return_value=result) as discover:
            self.assertEqual(rv.rex_cli()[2], "unix:///run/user/1001/rex/server.sock")
            self.assertNotIn("REX_SERVER", discover.call_args.kwargs["env"])
            self.assertEqual(discover.call_args.args[0][-2:], ["whoami", "--json"])

    def test_existing_local_endpoint_needs_no_discovery(self):
        with patch.dict(rv.os.environ, {"REX_SERVER": "unix:///custom/rex.sock"}), patch.object(rv.shutil, "which", return_value="/bin/rex"), patch.object(rv.subprocess, "run") as discover:
            self.assertEqual(rv.rex_cli()[2], "unix:///custom/rex.sock")
            discover.assert_not_called()

    def test_local_discovery_rejects_remote_or_invalid_metadata(self):
        for code, output in [(0, '{"endpoint":"https://remote.example"}'), (0, '[]'), (0, 'invalid'), (1, '')]:
            result = rv.subprocess.CompletedProcess([], code, output, "")
            with self.subTest(output=output), patch.dict(rv.os.environ, {}, clear=True), patch.object(rv.shutil, "which", return_value="/bin/rex"), patch.object(rv.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError):
                    rv.rex_cli()

    def test_uv_launcher_is_absolute_and_required(self):
        with patch.object(rv.shutil, "which", return_value="/opt/tools/uv"):
            self.assertEqual(rv.uv_executable(), "/opt/tools/uv")
        with patch.object(rv.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "uv is required"):
                rv.uv_executable()

    def test_job_reports_actual_exit_and_success_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            args = rv.argparse.Namespace(status=str(status), kind="test", command="exit 0", shell="/bin/sh", close_after=30)
            rv.atomic_json(status.with_suffix(".launch.json"), {"session_id": "main", "block_id": "owned"})
            with contextlib.redirect_stdout(io.StringIO()), patch.object(rv.time, "sleep") as sleep, patch.object(rv, "close_pane") as close:
                self.assertEqual(rv.job(args), 0)
            self.assertEqual(json.loads(status.read_text())["exit_code"], 0)
            sleep.assert_called_once_with(30)
            close.assert_called_once_with("main", "owned")

    def test_completed_jobs_have_finite_retention_and_real_receipts(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            rv.atomic_json(status.with_suffix(".launch.json"), {"session_id": "main", "block_id": "owned"})
            for kind, code, delay in [("test", 0, 30), ("test", 7, 120), ("deploy", 0, 120), ("server", 0, 30), ("logs", 1, 120)]:
                args = argparse.Namespace(status=str(status), kind=kind, command=f"exit {code}", shell="/bin/sh", close_after=None)
                with self.subTest(kind=kind, code=code), contextlib.redirect_stdout(io.StringIO()), patch.object(rv.time, "sleep") as sleep, patch.object(rv, "close_pane") as close:
                    self.assertEqual(rv.job(args), 0)
                    self.assertEqual(json.loads(status.read_text())["exit_code"], code)
                    sleep.assert_called_once_with(delay)
                    close.assert_called_once_with("main", "owned")

    def test_main_session_uses_live_environment_without_creation(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(rv, "STATE", Path(folder)), patch.dict(rv.os.environ, {"REX_SESSION": "main"}, clear=True), patch.object(rv, "rex_json", return_value={"sessions": [{"session_id": "main"}]}) as call:
            self.assertEqual(rv.main_session(), "main")
            self.assertEqual(call.call_count, 1)

    def test_ambiguous_or_missing_host_never_creates_session(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(rv, "STATE", Path(folder)), patch.dict(rv.os.environ, {}, clear=True), patch.object(rv, "rex_json", return_value={"sessions": []}) as call:
            with self.assertRaisesRegex(RuntimeError, "unambiguously"):
                rv.main_session()
            call.assert_called_once_with(["ls", "--json"])

    def test_legacy_cleanup_preserves_main_and_nonempty_sessions(self):
        with patch.object(rv, "rex_json") as inspect, patch.object(rv.subprocess, "run") as execute:
            rv.retire_empty_session("main", "main")
            inspect.assert_not_called()
            inspect.side_effect = [{"sessions": [{"session_id": "old"}]}, {"blocks": [{"block_id": "interactive"}]}]
            rv.retire_empty_session("old", "main")
            execute.assert_not_called()
            inspect.side_effect = [{"sessions": [{"session_id": "old"}]}, {"blocks": []}]
            execute.return_value.returncode = 0
            with patch.object(rv, "rex_cli", return_value=["rex"]):
                rv.retire_empty_session("old", "main")
            self.assertEqual(execute.call_args.args[0], ["rex", "kill", "old"])

    def test_different_tasks_share_owned_tab_in_main_session(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(rv, "STATE", Path(folder)), patch.object(rv, "main_session", return_value="main"):
            calls = []
            def fake(arguments):
                calls.append(arguments)
                if arguments[:2] == ["session", "inspect"]:
                    return {"windows": [] if len(calls) == 1 else [{"label": "Checks", "layers": [{"blocks": [{"block_id": "owned-1"}]}]}]}
                return {"block_ids": ["owned-1" if len(calls) == 2 else "owned-2"]}
            with patch.object(rv, "rex_json", side_effect=fake), patch.object(rv, "uv_executable", return_value="/opt/tools/uv"):
                for task in ("first-project", "another-project"):
                    args = argparse.Namespace(task=task, kind="test", close_after=30, command="true", shell="/bin/sh", cwd=folder)
                    opened = rv.open_job(args, task, Path(folder) / (task + ".json"))
                    self.assertEqual(opened["session_id"], "main")
            self.assertEqual(calls[1][:2], ["window", "new"])
            self.assertEqual(calls[3][0], "split")
            for call in (calls[1], calls[3]):
                child = call[call.index("--") + 1:]
                self.assertEqual(child[:4], ["/opt/tools/uv", "run", "--script", str(Path(rv.__file__).resolve())])
                self.assertEqual(child[4], "job")
            self.assertFalse(any(c[0] == "new" for c in calls))


if __name__ == "__main__":
    unittest.main()
