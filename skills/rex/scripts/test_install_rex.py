#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import copy
import importlib.util
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rex_installer", Path(__file__).with_name("install_hooks.py"))
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

SHELL_TOOL = installer.REX_VISIBLE_PRE_TOOL_MATCHER.split("|")[0]
LEGACY_SHELL_MATCHER = "|".join(installer.REX_VISIBLE_PRE_TOOL_MATCHER.split("|")[:2])


class InstallerTests(unittest.TestCase):
    def test_install_is_independent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home / ".codex/hooks.json"
            path.parent.mkdir()
            python_hook = {"type": "command", "command": "/usr/bin/python3 /custom/python_tooling.py", "timeout": 9}
            unrelated = {"hooks": [python_hook], "matcher": "custom"}
            original = {"hooks": {installer.HookEvent.PRE_TOOL_USE: [unrelated]}, "trust": {"enabled": False}, "permissions": {"allow": ["Bash(rg:*)"]}}
            path.write_text(json.dumps(original))
            (home / ".codex/AGENTS.md").write_text("Personal instructions\n")
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                result = installer.install(home, ["codex"])
                first = path.read_text()
                second = installer.install(home, ["codex"])
            runtime = home / ".local/share/agent-tooling/rex"
            self.assertEqual(result["runtime"], str(runtime))
            self.assertTrue((runtime / installer.CODEX_SESSIONS_SCRIPT).exists())
            self.assertTrue((runtime / "grokbot_callback.py").exists())
            self.assertFalse((runtime.parent / "python_tooling.py").exists())
            self.assertFalse((home / ".claude").exists())
            self.assertFalse((home / ".local/state/rex-visible").exists())
            self.assertEqual(first, path.read_text())
            self.assertEqual(second["changed"], [])
            data = json.loads(first)
            self.assertEqual(data["hooks"][installer.HookEvent.PRE_TOOL_USE][0], unrelated)
            self.assertEqual(data["trust"], original["trust"])
            self.assertEqual(data["permissions"], original["permissions"])
            self.assertIn(installer.HookEvent.PERMISSION_REQUEST, data["hooks"])
            pretool_handlers = [handler for group in data["hooks"][installer.HookEvent.PRE_TOOL_USE] for handler in group["hooks"]]
            self.assertTrue(any(installer.CODEX_SESSIONS_SCRIPT in handler["command"] for handler in pretool_handlers))
            codex_observers = [handler for group in data["hooks"][installer.HookEvent.PRE_TOOL_USE]
                               for handler in group["hooks"] if installer.CODEX_SESSIONS_SCRIPT in handler["command"]]
            self.assertTrue(codex_observers)
            codex_groups = [group for group in data["hooks"][installer.HookEvent.PRE_TOOL_USE]
                            if any(installer.CODEX_SESSIONS_SCRIPT in handler["command"] for handler in group["hooks"])]
            self.assertEqual(len(codex_groups), 1)
            self.assertEqual(codex_groups[0]["matcher"], installer.CODEX_QUESTION_TOOL_MATCHER)
            self.assertEqual(
                installer.CODEX_QUESTION_TOOL_MATCHER,
                "^({}|{})$".format(installer.ToolName.REQUEST_USER_INPUT,
                                    installer.ToolName.REQUEST_USER_INPUT_ASYNC),
            )
            self.assertIsNone(re.fullmatch(codex_groups[0]["matcher"], SHELL_TOOL))
            for event, groups in data["hooks"].items():
                for group in groups:
                    for handler in group["hooks"]:
                        if handler == python_hook:
                            continue
                        words = shlex.split(handler["command"])
                        self.assertEqual(words[:3], ["/opt/tools/uv", "run", "--script"])
                        self.assertEqual(words[-1], "hook")
                        if event == installer.HookEvent.SESSION_END:
                            self.assertEqual(handler["timeout"], 3)
            instructions = (home / ".codex/AGENTS.md").read_text()
            self.assertTrue(instructions.startswith("Personal instructions\n"))
            self.assertEqual(instructions.count("<!-- rex-visible:start -->"), 1)
            self.assertEqual(instructions.count("<!-- codex-coordination:start -->"), 1)

    def test_migration_only_changes_recognized_rex_handlers(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        for old in (root, home / "Developer/tools/rex-visible", home / "repo/skills/python-tooling/scripts/rex", home / "repo/skills/rex/scripts"):
            owned = {"type": "command", "command": shlex.join(["/usr/bin/python3", str(old / installer.CODEX_SESSIONS_SCRIPT), "hook"]), "timeout": 80}
            unrelated = {"type": "command", "command": "/usr/bin/python3 /custom/codex_sessions.py hook", "timeout": 7}
            data = {"hooks": {installer.HookEvent.SESSION_END: [{"hooks": [owned, copy.deepcopy(unrelated)], "matcher": "keep"}]}}
            installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
            self.assertEqual(shlex.split(owned["command"]), ["/opt/tools/uv", "run", "--script", str(root / installer.CODEX_SESSIONS_SCRIPT), "hook"])
            self.assertEqual(owned["timeout"], 3)
            self.assertEqual(data["hooks"][installer.HookEvent.SESSION_END][0]["hooks"][1], unrelated)
            self.assertEqual(data["hooks"][installer.HookEvent.SESSION_END][0]["matcher"], "keep")

    def test_post_tool_resume_observer_preserves_unrelated_matchers(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        for codex, script in ((True, installer.CODEX_SESSIONS_SCRIPT), (False, installer.CLAUDE_SESSIONS_SCRIPT)):
            owned = {"type": "command", "command": shlex.join(["uv", "run", "--script", str(root / script), "hook"])}
            unrelated = {"type": "command", "command": "/custom/observer", "timeout": 7}
            mixed = {"matcher": "Bash", "hooks": [copy.deepcopy(owned), unrelated], "custom": "preserved"}
            data = {"hooks": {installer.HookEvent.POST_TOOL_USE: [mixed, {"matcher": "SendMessage", "hooks": [copy.deepcopy(owned)]}]}}
            installer.rex_hooks(data, root, home, "/opt/tools/uv", codex)
            self.assertEqual(mixed, {"matcher": "Bash", "hooks": [unrelated], "custom": "preserved"})
            observers = [group for group in data["hooks"][installer.HookEvent.POST_TOOL_USE] if group is not mixed]
            self.assertEqual(len(observers), 2)
            self.assertTrue(all("matcher" not in group for group in observers))
            before = copy.deepcopy(data)
            installer.rex_hooks(data, root, home, "/opt/tools/uv", codex)
            self.assertEqual(data, before)

    def test_codex_observer_migration_preserves_mixed_group_matcher(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        owned = {"type": "command", "command": shlex.join(["uv", "run", "--script", str(root / installer.CODEX_SESSIONS_SCRIPT), "hook"])}
        unrelated = {"type": "command", "command": "/custom/observer", "timeout": 7}
        mixed = {"matcher": LEGACY_SHELL_MATCHER, "hooks": [copy.deepcopy(owned), unrelated], "custom": "preserved"}
        data = {"hooks": {installer.HookEvent.PRE_TOOL_USE: [mixed]}}
        installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
        self.assertEqual(mixed, {"matcher": LEGACY_SHELL_MATCHER, "hooks": [unrelated], "custom": "preserved"})
        observers = [group for group in data["hooks"][installer.HookEvent.PRE_TOOL_USE] if group is not mixed
                     and any(installer.CODEX_SESSIONS_SCRIPT in item["command"] for item in group["hooks"])]
        self.assertEqual(len(observers), 1)
        self.assertEqual(observers[0]["matcher"], installer.CODEX_QUESTION_TOOL_MATCHER)
        before = copy.deepcopy(data)
        installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
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
            self.assertTrue({installer.HookEvent.SESSION_START, installer.HookEvent.USER_PROMPT_SUBMIT, installer.HookEvent.PRE_TOOL_USE, installer.HookEvent.SUBAGENT_START, installer.HookEvent.SUBAGENT_STOP, installer.HookEvent.STOP, installer.HookEvent.STOP_FAILURE, installer.HookEvent.SESSION_END, "TeammateIdle", "TaskCreated", "TaskCompleted", installer.HookEvent.NOTIFICATION, installer.HookEvent.PERMISSION_REQUEST}.issubset(data["hooks"]))
            commands = [handler["command"] for groups in data["hooks"].values() for group in groups for handler in group["hooks"]]
            self.assertTrue(any(installer.CLAUDE_SESSIONS_SCRIPT in command for command in commands))
            self.assertFalse(any(installer.CODEX_SESSIONS_SCRIPT in command for command in commands))
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
