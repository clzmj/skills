#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "python_tooling", Path(__file__).resolve().parents[1] / "skills/python-tooling/scripts/python_tooling.py"
)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class PythonToolingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pyproject.toml").write_text('[project]\nname = "example"\n')
        (self.root / "uv.lock").write_text("version = 1\n")
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def payload(self, command, tool="Bash", **extra):
        key = "cmd" if tool == "exec_command" else "command"
        return {"hook_event_name": "PreToolUse", "cwd": str(self.root),
                "tool_name": tool, "tool_input": {key: command, **extra}}

    def test_denies_environment_changes_with_shared_hook_schema(self):
        for command in ["pip install requests", "pip3 uninstall requests", "python -m pip install -r requirements.txt",
                        ".venv/bin/python3.12 -m pip uninstall foo", "uv pip sync requirements.txt",
                        "env FOO=bar pip --isolated install foo", "pip --python .venv install foo",
                        "true && pip install foo", "true\npip install foo", "bash -lc 'pip install foo'"]:
            with self.subTest(command=command):
                result = hook.handle(self.payload(command))["hookSpecificOutput"]
                self.assertEqual(result["hookEventName"], "PreToolUse")
                self.assertEqual(result["permissionDecision"], "deny")
                self.assertIn("uv add", result["permissionDecisionReason"])

    def test_allows_read_only_commands_and_literal_mentions(self):
        for command in ["pip list", "pip show install", "python -m pip --version", "pip download foo",
                        "uv pip compile requirements.in", "uv add foo", "uv sync --locked", "echo 'pip install foo'",
                        "printf '%s' 'a; pip install foo'", "echo ';' pip install foo", "# pip install foo\necho ok",
                        "env --chdir=/ pip install foo"]:
            with self.subTest(command=command):
                self.assertIsNone(hook.handle(self.payload(command)))

    def test_exec_command_respects_workdir(self):
        child = self.root / "src"
        child.mkdir()
        self.assertIsNotNone(hook.handle(self.payload("pip install foo", "exec_command", workdir=str(child))))
        self.assertIsNone(hook.handle(self.payload("pip install foo", "exec_command", workdir="/")))

    def test_nearest_project_wins_and_cd_changes_project(self):
        child = self.root / "legacy"
        child.mkdir()
        (child / "setup.py").write_text("")
        self.assertIsNone(hook.handle(self.payload("cd legacy && pip install foo")))
        self.assertIsNotNone(hook.handle(self.payload("cd legacy; cd ..; pip install foo")))

    def test_requires_both_uv_files(self):
        (self.root / "uv.lock").unlink()
        self.assertIsNone(hook.handle(self.payload("pip install foo")))

    def test_session_context_only_mentions_configured_tools(self):
        payload = {"hook_event_name": "SessionStart", "cwd": str(self.root)}
        context = hook.handle(payload)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("uses uv", context)
        self.assertNotIn("ruff", context)
        self.assertNotIn("ty check", context)
        (self.root / "pyproject.toml").write_text('[dependency-groups]\ndev = ["ruff>=0.9"]\n[tool.ty.environment]\n')
        context = hook.handle(payload)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ruff format and ruff check", context)
        self.assertIn("ty check", context)

    def test_no_project_and_repository_boundary(self):
        child = self.root / "other-repo"
        child.mkdir()
        (child / ".git").mkdir()
        self.assertIsNone(hook.handle({"hook_event_name": "SessionStart", "cwd": str(child)}))

    def test_opt_out_and_non_shell_tools(self):
        with patch.dict(os.environ, {"PYTHON_TOOLING_DISABLED": "1"}):
            self.assertIsNone(hook.handle(self.payload("pip install foo")))
        self.assertIsNone(hook.handle(self.payload("pip install foo", "Read")))

    def test_malformed_input_fails_quietly(self):
        for payload in ["{", "[]", json.dumps(self.payload("pip install '")),
                        json.dumps({"hook_event_name": "PreToolUse", "cwd": 7}),
                        json.dumps({**self.payload("pip install foo"), "tool_input": None})]:
            with self.subTest(payload=payload), patch("sys.stdin", io.StringIO(payload)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    hook.main()
                self.assertEqual(output.getvalue(), "")

    def test_main_emits_json_without_mutating_project(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch("sys.stdin", io.StringIO(json.dumps(self.payload("pip install foo")))):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                hook.main()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})


if __name__ == "__main__":
    unittest.main()
