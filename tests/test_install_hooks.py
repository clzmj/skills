import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "skills/python-tooling/scripts/install_hooks.py"
SPEC = importlib.util.spec_from_file_location("install_hooks", SOURCE)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_python_launcher_update_replaces_definition_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.object(installer.shutil, "which", return_value="/old-python"):
                installer.install(home, ["codex"])
            with patch.object(installer.shutil, "which", return_value="/stable/python3"):
                installer.install(home, ["codex"])
            data = json.loads((home / ".codex/hooks.json").read_text())
            for event in ("SessionStart", "PreToolUse"):
                handlers = [h for g in data["hooks"][event] for h in g["hooks"]]
                self.assertEqual(len(handlers), 1)
                self.assertTrue(handlers[0]["command"].startswith("/stable/python3 "))

    def test_preserves_other_hooks_settings_and_trust_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / ".codex/hooks.json"
            config.parent.mkdir()
            other = {"hooks": [{"type": "command", "command": "original-handler", "timeout": 3}]}
            config.write_text(json.dumps({"hooks": {"SessionStart": [other]}, "state": {"trusted_hash": "opaque"}}))
            claude = home / ".claude/settings.json"
            claude.parent.mkdir()
            claude.write_text(json.dumps({"model": "unchanged", "enabledPlugins": {"other@example": True}}))
            installer.install(home, ["codex", "claude-code"])
            first = config.read_text(), claude.read_text()
            self.assertEqual(installer.install(home, ["codex", "claude-code"])["changed"], [])
            self.assertEqual(first, (config.read_text(), claude.read_text()))
            data = json.loads(first[0])
            self.assertEqual(data["hooks"]["SessionStart"][0], other)
            self.assertEqual(data["state"], {"trusted_hash": "opaque"})
            self.assertEqual(json.loads(first[1])["enabledPlugins"], {"other@example": True})
            self.assertTrue((home / ".local/share/agent-tooling/python_tooling.py").exists())

    def test_optional_rex_migrates_legacy_handler_and_preserves_other_instruction_text(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / ".codex/hooks.json"
            config.parent.mkdir()
            command = "/custom/python /legacy/rex_visible.py hook"
            config.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}))
            instructions = home / ".codex/AGENTS.md"
            instructions.write_text("Keep this instruction.\n")
            installer.install(home, ["codex"], with_rex=True)
            data = json.loads(config.read_text())
            commands = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
            self.assertEqual(sum("rex_visible.py" in c for c in commands), 1)
            self.assertNotIn(command, commands)
            self.assertTrue(any(str(home / ".local/share/agent-tooling/rex/rex_visible.py") in c for c in commands))
            self.assertTrue(instructions.read_text().startswith("Keep this instruction."))
            before = instructions.read_text()
            installer.install(home, ["codex"], with_rex=True)
            self.assertEqual(instructions.read_text(), before)
            self.assertEqual(data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
