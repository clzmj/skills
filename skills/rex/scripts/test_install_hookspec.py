#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import copy
import importlib.util
import json
import shlex
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "rex_installer", Path(__file__).with_name("install_hooks.py")
)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class HookSpecTests(unittest.TestCase):
    def test_hook_spec_is_frozen_and_has_exact_question_matchers(self):
        codex = installer._hook_specs(True)
        claude = installer._hook_specs(False)
        codex_questions = [
            row[0] for row in codex if row[0].script == "question_hooks.py"
        ]
        claude_questions = [
            row[0] for row in claude if row[0].script == "question_hooks.py"
        ]
        self.assertEqual(
            {
                (item.event, item.matcher, item.timeout)
                for item in codex_questions
                if item.event
                in (installer.HookEvent.PRE_TOOL_USE, installer.HookEvent.POST_TOOL_USE)
            },
            {
                (
                    installer.HookEvent.PRE_TOOL_USE,
                    installer.CODEX_QUESTION_TOOL_MATCHER,
                    10,
                ),
                (
                    installer.HookEvent.POST_TOOL_USE,
                    installer.CODEX_QUESTION_TOOL_MATCHER,
                    10,
                ),
            },
        )
        self.assertEqual(
            {
                (item.event, item.matcher, item.timeout)
                for item in claude_questions
                if item.event
                in (
                    installer.HookEvent.PRE_TOOL_USE,
                    installer.HookEvent.PERMISSION_REQUEST,
                    installer.HookEvent.POST_TOOL_USE,
                )
            },
            {
                (
                    installer.HookEvent.PRE_TOOL_USE,
                    installer.CLAUDE_QUESTION_TOOL_MATCHER,
                    60,
                ),
                (
                    installer.HookEvent.PERMISSION_REQUEST,
                    installer.CLAUDE_QUESTION_TOOL_MATCHER,
                    60,
                ),
                (
                    installer.HookEvent.POST_TOOL_USE,
                    installer.CLAUDE_QUESTION_TOOL_MATCHER,
                    10,
                ),
            },
        )
        self.assertEqual(
            {
                (item.event, item.matcher)
                for item in codex_questions
                if item.event
                in (
                    installer.HookEvent.STOP,
                    installer.HookEvent.SESSION_END,
                    installer.HookEvent.SUBAGENT_STOP,
                )
            },
            {
                (event, None)
                for event in (
                    installer.HookEvent.STOP,
                    installer.HookEvent.SESSION_END,
                    installer.HookEvent.SUBAGENT_STOP,
                )
            },
        )
        with self.assertRaises(FrozenInstanceError):
            codex_questions[0].timeout = 99

    def test_question_hook_matcher_groups_coexist_for_same_event(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        data: dict[str, Any] = {"hooks": {}}
        first = installer.HookSpec(
            "question_hooks.py", installer.HookEvent.PRE_TOOL_USE, "^First$", 10
        )
        second = installer.HookSpec(
            "question_hooks.py", installer.HookEvent.PRE_TOOL_USE, "^Second$", 20
        )
        matchers = {first.matcher, second.matcher}
        installer._apply_spec(data, first, root, home, "uv", "codex", matchers)
        installer._apply_spec(data, second, root, home, "uv", "codex", matchers)
        groups = data["hooks"][installer.HookEvent.PRE_TOOL_USE.value]
        self.assertEqual(
            {group["matcher"] for group in groups}, {"^First$", "^Second$"}
        )
        self.assertEqual(len(groups), 2)

    def test_install_migrates_mixed_owned_group_and_preserves_unrelated_matcher(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        owned: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(
                ["uv", "run", "--script", str(root / "codex_sessions.py"), "hook"]
            ),
            "timeout": 80,
        }
        unrelated: dict[str, Any] = {
            "type": "command",
            "command": "/custom/observer",
            "timeout": 7,
        }
        mixed: dict[str, Any] = {
            "matcher": "Bash|exec_command",
            "hooks": [copy.deepcopy(owned), unrelated],
            "custom": "keep",
        }
        data: dict[str, Any] = {
            "hooks": {installer.HookEvent.PRE_TOOL_USE.value: [mixed]}
        }
        installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
        self.assertEqual(
            mixed,
            {"matcher": "Bash|exec_command", "hooks": [unrelated], "custom": "keep"},
        )
        codex_groups = [
            group
            for group in data["hooks"][installer.HookEvent.PRE_TOOL_USE.value]
            if any(
                "codex_sessions.py" in item.get("command", "")
                for item in group.get("hooks", [])
            )
        ]
        self.assertEqual(len(codex_groups), 1)
        self.assertEqual(
            codex_groups[0]["matcher"], installer.CODEX_QUESTION_TOOL_MATCHER
        )
        self.assertEqual(codex_groups[0]["hooks"][0]["timeout"], 10)

    def test_mixed_owned_post_observer_is_broadened_without_widening_unrelated_hook(
        self,
    ):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        owned: dict[str, Any] = {
            "type": "command",
            "command": shlex.join(
                ["uv", "run", "--script", str(root / "codex_sessions.py"), "hook"]
            ),
        }
        unrelated: dict[str, Any] = {
            "type": "command",
            "command": "/custom/observer",
            "timeout": 7,
        }
        mixed: dict[str, Any] = {
            "matcher": "SendMessage",
            "hooks": [copy.deepcopy(owned), unrelated],
            "custom": "keep",
        }
        data: dict[str, Any] = {
            "hooks": {installer.HookEvent.POST_TOOL_USE.value: [mixed]}
        }
        installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
        self.assertEqual(
            mixed, {"matcher": "SendMessage", "hooks": [unrelated], "custom": "keep"}
        )
        groups = data["hooks"][installer.HookEvent.POST_TOOL_USE.value]
        observers = [
            group
            for group in groups
            if any(
                "codex_sessions.py" in handler.get("command", "")
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(len(observers), 1)
        self.assertNotIn("matcher", observers[0])
        questions = [
            group
            for group in groups
            if any(
                "question_hooks.py" in handler.get("command", "")
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["matcher"], installer.CODEX_QUESTION_TOOL_MATCHER)

    def test_question_post_matcher_survives_rerun_and_owned_duplicates_are_removed(
        self,
    ):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        data: dict[str, Any] = {"hooks": {}}
        installer.rex_hooks(data, root, home, "uv", True)
        event = installer.HookEvent.POST_TOOL_USE.value
        question_groups = [
            group
            for group in data["hooks"][event]
            if any("question_hooks.py" in item["command"] for item in group["hooks"])
        ]
        self.assertEqual(len(question_groups), 1)
        self.assertEqual(
            question_groups[0]["matcher"], installer.CODEX_QUESTION_TOOL_MATCHER
        )
        duplicate = copy.deepcopy(question_groups[0]["hooks"][0])
        question_groups[0]["hooks"].append(duplicate)
        installer.rex_hooks(data, root, home, "uv", True)
        self.assertEqual(len(question_groups[0]["hooks"]), 1)
        before = copy.deepcopy(data)
        installer.rex_hooks(data, root, home, "uv", True)
        self.assertEqual(data, before)

    def test_host_hook_specs_include_agent_cli_value(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                installer.install(home, ["claude-code"])
            config = json.loads((home / ".claude/settings.json").read_text())
            question_commands = [
                handler["command"]
                for groups in config["hooks"].values()
                for group in groups
                for handler in group["hooks"]
                if "question_hooks.py" in handler["command"]
            ]
            self.assertTrue(question_commands)
            self.assertTrue(
                all(
                    shlex.split(command)[-2:] == ["--agent", "claude"]
                    for command in question_commands
                )
            )


if __name__ == "__main__":
    unittest.main()
