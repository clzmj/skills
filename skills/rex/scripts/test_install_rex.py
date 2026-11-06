#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import copy
import importlib.util
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("rex_installer", Path(__file__).with_name("install_hooks.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
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
            self.assertFalse((runtime.parent / "python_tooling.py").exists())
            self.assertFalse((home / ".claude").exists())
            self.assertFalse((home / ".local/state/rex-visible").exists())
            self.assertEqual(first, path.read_text())
            self.assertEqual(second["changed"], [])
            data = json.loads(first)
            self.assertEqual(data["hooks"]["PreToolUse"][0], unrelated)
            self.assertEqual(data["trust"], original["trust"])
            self.assertEqual(data["permissions"], original["permissions"])
            for event, groups in data["hooks"].items():
                for group in groups:
                    for handler in group["hooks"]:
                        if handler == python_hook:
                            continue
                        words = shlex.split(handler["command"])
                        self.assertEqual(words[:3], ["/opt/tools/uv", "run", "--script"])
                        self.assertEqual(words[-1], "hook")
                        if event == "SessionEnd":
                            self.assertEqual(handler["timeout"], 3)
            instructions = (home / ".codex/AGENTS.md").read_text()
            self.assertTrue(instructions.startswith("Personal instructions\n"))
            self.assertEqual(instructions.count("<!-- rex-visible:start -->"), 1)
            self.assertEqual(instructions.count("<!-- codex-coordination:start -->"), 1)

    def test_migration_only_changes_recognized_rex_handlers(self):
        home = Path("/test-home")
        root = home / ".local/share/agent-tooling/rex"
        for old in (root, home / "Developer/tools/rex-visible", home / "repo/skills/python-tooling/scripts/rex", home / "repo/skills/rex/scripts"):
            owned = {"type": "command", "command": shlex.join(["/usr/bin/python3", str(old / "codex_sessions.py"), "hook"]), "timeout": 80}
            unrelated = {"type": "command", "command": "/usr/bin/python3 /custom/codex_sessions.py hook", "timeout": 7}
            data = {"hooks": {"SessionEnd": [{"hooks": [owned, copy.deepcopy(unrelated)], "matcher": "keep"}]}}
            installer.rex_hooks(data, root, home, "/opt/tools/uv", True)
            self.assertEqual(shlex.split(owned["command"]), ["/opt/tools/uv", "run", "--script", str(root / "codex_sessions.py"), "hook"])
            self.assertEqual(owned["timeout"], 3)
            self.assertEqual(data["hooks"]["SessionEnd"][0]["hooks"][1], unrelated)
            self.assertEqual(data["hooks"]["SessionEnd"][0]["matcher"], "keep")

    def test_claude_has_visible_hooks_only_and_preserves_settings(self):
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
            self.assertEqual(set(data["hooks"]), {"SessionStart", "UserPromptSubmit", "PreToolUse"})
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
