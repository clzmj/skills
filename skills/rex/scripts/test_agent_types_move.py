#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for the shared callback types and read-only session readers."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_types
import grokbot_callback as callback


class AgentTypeMoveTests(unittest.TestCase):
    def test_callback_reexports_moved_types(self):
        self.assertIs(callback.AgentKind, agent_types.AgentKind)
        self.assertIs(callback.Event, agent_types.Event)
        self.assertIs(callback.HookEvent, agent_types.HookEvent)
        self.assertIs(callback.InputKind, agent_types.InputKind)

    def test_reader_loads_state_by_actor_key_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            state_dir = Path(folder)
            state_dir.mkdir(exist_ok=True)
            path = state_dir / "grokbot-callback.sqlite3"
            item = {
                "key": "child-key", "id": "child-id", "agent": "claude",
                "parent": "root-key", "root": "root-key", "depth": 1,
                "task_id": "task", "requester": "dinesh", "turn": "turn",
                "status": "idle", "rex_session": "rex", "rex_block": "block",
            }
            root = {**item, "key": "root-key", "id": "root-id", "parent": "", "depth": 0}
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sessions (key TEXT PRIMARY KEY, record TEXT)")
                db.executemany("INSERT INTO sessions VALUES (?, ?)",
                               [(item["key"], json.dumps(item)), (root["key"], json.dumps(root))])
            with patch.object(callback.rex, "STATE", state_dir):
                self.assertIsNone(callback.session_state(agent_types.AgentKind.CLAUDE, "native-session", "agent-id"))
                state = callback.session_state(agent_types.AgentKind.CODEX, "child-key")
                self.assertEqual(state, item)
                assert state is not None
                self.assertEqual(callback.session_lineage(state), {
                    "requester_bot": "dinesh", "root_session_id": "root-id",
                    "parent_session_id": "root-id", "agent_session_id": "child-id",
                    "depth": 1, "agent_kind": agent_types.AgentKind.CLAUDE,
                    "role": callback.Role.SUBAGENT,
                })
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    def test_reader_rejects_malformed_session_records(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
        ):
            path = Path(folder) / "grokbot-callback.sqlite3"
            valid = {
                "key": "bad",
                "id": "id",
                "agent": "codex",
                "parent": "",
                "root": "bad",
                "depth": 0,
                "task_id": "task",
                "requester": "bot",
                "turn": "turn",
                "status": "idle",
                "rex_session": "",
                "rex_block": "",
            }
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sessions (key TEXT PRIMARY KEY, record TEXT)")
                db.execute("INSERT INTO sessions VALUES (?, ?)", ("bad", "{}"))
            for record in (
                {"key": "bad"},
                {**valid, "depth": True},
                {**valid, "agent": "unknown"},
                {**valid, "status": "unknown"},
                {**valid, "retired_turns": [1]},
            ):
                with self.subTest(record=record), sqlite3.connect(path) as db:
                    db.execute(
                        "UPDATE sessions SET record=? WHERE key='bad'",
                        (json.dumps(record),),
                    )
                    db.commit()
                    self.assertIsNone(
                        callback.session_state(agent_types.AgentKind.CODEX, "bad")
                    )

    def test_reader_returns_none_for_missing_database_or_row(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(callback.rex, "STATE", Path(folder)):
            self.assertIsNone(callback.session_state(agent_types.AgentKind.CODEX, "missing"))
            path = Path(folder) / "grokbot-callback.sqlite3"
            self.assertFalse(path.exists())
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sessions (key TEXT PRIMARY KEY, record TEXT)")
            self.assertIsNone(callback.session_state(agent_types.AgentKind.CODEX, "missing"))


if __name__ == "__main__":
    unittest.main()
