#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import copy
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from typing import TypedDict
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rex_installer", Path(__file__).with_name("install_hooks.py"))
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Handler(TypedDict, total=False):
    type: str
    command: str
    timeout: int


class HookGroup(TypedDict, total=False):
    matcher: str
    hooks: list[Handler]
    custom: str


class HookConfig(TypedDict):
    hooks: dict[str, list[HookGroup]]


class InstallerTests(unittest.TestCase):
    def test_operator_cli_can_enable_then_disable_both_hosts_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            commands_by_host: dict[str, list[str]] = {}
            for setting in ("on", "off", "off"):
                result = subprocess.run(
                    [sys.executable, str(Path(installer.__file__ or "")), "--home", str(home),
                     "--question-escalation", setting],
                    text=True, capture_output=True, check=True,
                )
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["question_escalation"], setting)
                for config in (home / ".codex/hooks.json", home / ".claude/settings.json"):
                    data = json.loads(config.read_text())
                    commands = [handler["command"] for groups in data["hooks"].values()
                                for group in groups for handler in group["hooks"]]
                    expected = "GROKBOT_QE=1" if setting == "on" else "GROKBOT_QE=0"
                    self.assertTrue(commands)
                    self.assertTrue(all(shlex.split(command)[:2] == ["env", expected]
                                        for command in commands))
                    previous = commands_by_host.get(str(config))
                    if previous is not None:
                        self.assertEqual(len(commands), len(previous))
                    commands_by_host[str(config)] = commands
                if not receipt["changed"]:
                    self.assertEqual(setting, "off")
            self.assertEqual(receipt["changed"], [])

    def test_default_hooks_disable_qe_even_when_parent_environment_enables_it(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            launcher = home / "fake uv"
            launcher.write_text('#!/usr/bin/env python3\nimport os\nprint(os.environ["GROKBOT_QE"])\n')
            launcher.chmod(0o755)
            data: HookConfig = {"hooks": {}}
            installer.rex_hooks(data, home / "runtime", home, str(launcher), True)
            for groups in data["hooks"].values():
                for group in groups:
                    for handler in group["hooks"]:
                        result = subprocess.run(shlex.split(handler["command"]),
                                                env={**os.environ, "GROKBOT_QE": "1"},
                                                text=True, capture_output=True, check=True)
                        self.assertEqual(result.stdout.strip(), "0")

    def test_install_is_independent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home / ".codex/hooks.json"
            path.parent.mkdir()
            python_hook = {"type": "command", "command": "/usr/bin/python3 /custom/python_tooling.py", "timeout": 9}
            unrelated = {"hooks": [python_hook], "matcher": "custom"}
            original = {"hooks": {"PreToolUse": [unrelated]}, "trust": {"enabled": False}, "permissions": {"allow": ["Bash(rg:*)"]}}
            path.write_text(json.dumps(original))
            (home / ".codex/AGENTS.md").write_text("Personal instructions\n")
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                result = installer.install(home, ["codex"])
                first = path.read_text()
                second = installer.install(home, ["codex"])
            runtime = home / ".local/share/agent-tooling/rex"
            self.assertEqual(result["runtime"], str(runtime))
            self.assertTrue((runtime / "codex_sessions.py").exists())
            self.assertTrue((runtime / "grokbot_callback.py").exists())
            self.assertFalse((runtime.parent / "python_tooling.py").exists())
            self.assertFalse((home / ".claude").exists())
            self.assertFalse((home / ".local/state/rex-visible").exists())
            self.assertEqual(first, path.read_text())
            self.assertEqual(second["changed"], [])
            data = json.loads(first)
            self.assertEqual(data["hooks"]["PreToolUse"][0], unrelated)
            self.assertEqual(data["trust"], original["trust"])
            self.assertEqual(data["permissions"], original["permissions"])
            self.assertIn("PermissionRequest", data["hooks"])
            for event, groups in data["hooks"].items():
                for group in groups:
                    for handler in group["hooks"]:
                        if handler == python_hook:
                            continue
                        words = shlex.split(handler["command"])
                        self.assertEqual(words[:5], ["env", "GROKBOT_QE=0", "/opt/tools/uv", "run", "--script"])
                        if "question_hooks.py" in handler["command"]:
                            self.assertEqual(words[-3:], ["hook", "--agent", "codex"])
                        else:
                            self.assertEqual(words[-1], "hook")
                        if event == "SessionEnd":
                            self.assertEqual(handler["timeout"], 5 if "question_hooks.py" in handler["command"] else 3)
            instructions = (home / ".codex/AGENTS.md").read_text()
            self.assertTrue(instructions.startswith("Personal instructions\n"))
            self.assertEqual(instructions.count("<!-- rex-visible:start -->"), 1)
            self.assertEqual(instructions.count("<!-- codex-coordination:start -->"), 1)

    def test_migration_only_changes_recognized_rex_handlers(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        for old in (root, home / "Developer/tools/rex-visible", home / "repo/skills/python-tooling/scripts/rex", home / "repo/skills/rex/scripts"):
            owned: Handler = {"type": "command", "command": shlex.join(["/usr/bin/python3", str(old / "codex_sessions.py"), "hook"]), "timeout": 80}
            unrelated: Handler = {"type": "command", "command": "/usr/bin/python3 /custom/codex_sessions.py hook", "timeout": 7}
            data: HookConfig = {"hooks": {"SessionEnd": [{"hooks": [owned, copy.deepcopy(unrelated)], "matcher": "keep"}]}}
            installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
            self.assertEqual(data["hooks"]["SessionEnd"][0]["hooks"], [unrelated])
            self.assertEqual(data["hooks"]["SessionEnd"][0]["matcher"], "keep")
            relocated = [handler for group in data["hooks"]["SessionEnd"] for handler in group["hooks"]
                         if installer.owned_handler(handler.get("command", ""), "codex_sessions.py", home, root)]
            self.assertEqual(len(relocated), 1)
            self.assertEqual(shlex.split(relocated[0]["command"]), ["env", "GROKBOT_QE=0", "/opt/tools/uv", "run", "--script", str(root / "codex_sessions.py"), "hook"])
            self.assertEqual(relocated[0]["timeout"], 3)

    def test_post_tool_resume_observer_preserves_unrelated_matchers(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        for codex, script in ((True, "codex_sessions.py"), (False, "claude_sessions.py")):
            owned: Handler = {"type": "command", "command": shlex.join(["uv", "run", "--script", str(root / script), "hook"])}
            unrelated: Handler = {"type": "command", "command": "/custom/observer", "timeout": 7}
            mixed: HookGroup = {"matcher": "Bash", "hooks": [copy.deepcopy(owned), unrelated], "custom": "preserved"}
            data: HookConfig = {"hooks": {"PostToolUse": [mixed, {"matcher": "SendMessage", "hooks": [copy.deepcopy(owned)]}]}}
            installer.rex_hooks(data, root, home, "/opt/tools/uv", codex)
            self.assertEqual(mixed, {"matcher": "Bash", "hooks": [unrelated], "custom": "preserved"})
            observers = [group for group in data["hooks"]["PostToolUse"]
                         if group is not mixed and any(installer.owned_handler(handler.get("command", ""), script, home, root)
                                                       for handler in group.get("hooks", []))]
            self.assertEqual(len(observers), 1)
            self.assertTrue(all("matcher" not in group for group in observers))
            before = copy.deepcopy(data)
            installer.rex_hooks(data, root, home, "/opt/tools/uv", codex)
            self.assertEqual(data, before)

    def test_claude_has_native_lifecycle_and_preserves_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home / ".claude/settings.json"
            path.parent.mkdir()
            original = {"permissions": {"deny": ["Bash(rm:*)"]}, "enabledPlugins": {"personal": True}}
            path.write_text(json.dumps(original))
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                installer.install(home, ["claude-code"])
            data = json.loads(path.read_text())
            self.assertEqual(data["permissions"], original["permissions"])
            self.assertEqual(data["enabledPlugins"], original["enabledPlugins"])
            self.assertTrue({"SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart", "SubagentStop", "Stop", "StopFailure", "SessionEnd", "TeammateIdle", "TaskCreated", "TaskCompleted", "Notification", "PermissionRequest"}.issubset(data["hooks"]))
            commands = [handler["command"] for groups in data["hooks"].values() for group in groups for handler in group["hooks"]]
            self.assertTrue(any("claude_sessions.py" in command for command in commands))
            self.assertFalse(any("codex_sessions.py" in command for command in commands))
            first = path.read_text()
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                self.assertEqual(installer.install(home, ["claude-code"])["changed"], [])
            self.assertEqual(first, path.read_text())
            self.assertIn("<!-- claude-coordination:start -->", (home / ".claude/CLAUDE.md").read_text())
            self.assertFalse((home / ".codex").exists())
            self.assertNotIn("codex-coordination", (home / ".claude/CLAUDE.md").read_text())

    def test_missing_uv_does_not_change_home(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(installer.shutil, "which", return_value=None):
            home = Path(folder)
            with self.assertRaisesRegex(RuntimeError, "uv is required"):
                installer.install(home, ["codex"])
            self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
