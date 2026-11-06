#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import importlib.util
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'skills/python-tooling/scripts/install_hooks.py'
SPEC = importlib.util.spec_from_file_location('install_python_hooks', SOURCE)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_migrates_python_launcher_to_uv_without_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / '.codex/hooks.json'
            config.parent.mkdir()
            script = home / '.local/share/agent-tooling/python_tooling.py'
            config.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': shlex.join(['/old-python', str(script)])}]}]}}))
            with patch.object(installer.shutil, 'which', return_value='/stable/uv'):
                installer.install(home, ['codex'])
            data = json.loads(config.read_text())
            for event in ('SessionStart', 'PreToolUse'):
                handlers = [h for g in data['hooks'][event] for h in g['hooks']]
                self.assertEqual(len(handlers), 1)
                self.assertEqual(shlex.split(handlers[0]['command']), ['/stable/uv', 'run', '--script', str(script)])

    def test_preserves_rex_other_hooks_settings_trust_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / '.codex/hooks.json'
            config.parent.mkdir()
            other = {'hooks': [{'type': 'command', 'command': 'original-handler', 'timeout': 3}]}
            rex = {'hooks': [{'type': 'command', 'command': '/legacy/rex_visible.py hook'}]}
            config.write_text(json.dumps({'hooks': {'SessionStart': [other, rex]}, 'state': {'trusted_hash': 'opaque'}}))
            instructions = home / '.codex/AGENTS.md'
            instructions.write_text('Keep Rex and other instructions.\n')
            claude = home / '.claude/settings.json'
            claude.parent.mkdir()
            claude.write_text(json.dumps({'model': 'unchanged', 'enabledPlugins': {'other@example': True}}))
            installer.install(home, ['codex', 'claude-code'])
            first = config.read_text(), claude.read_text()
            self.assertEqual(installer.install(home, ['codex', 'claude-code'])['changed'], [])
            self.assertEqual(first, (config.read_text(), claude.read_text()))
            data = json.loads(first[0])
            self.assertEqual(data['hooks']['SessionStart'][:2], [other, rex])
            self.assertEqual(data['state'], {'trusted_hash': 'opaque'})
            self.assertEqual(json.loads(first[1])['enabledPlugins'], {'other@example': True})
            self.assertEqual(instructions.read_text(), 'Keep Rex and other instructions.\n')
            self.assertFalse((home / '.local/share/agent-tooling/rex').exists())
            self.assertTrue((home / '.local/share/agent-tooling/python_tooling.py').exists())

    def test_missing_uv_stops_before_writing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(installer.shutil, 'which', return_value=None):
            home = Path(folder)
            with self.assertRaisesRegex(RuntimeError, 'uv is required'):
                installer.install(home, ['codex'])
            self.assertEqual(list(home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
