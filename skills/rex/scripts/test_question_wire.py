#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local-only question callback wire and host-input boundaries."""

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import grokbot_callback as callback
import question_ledger as ledger
from agent_types import AgentKind, Event, HookEvent, InputKind
from question_model import DIGEST_SCHEMA, DigestEnvelope, DigestPayload, digest


class QuestionWireTests(unittest.TestCase):
    def test_repeated_completion_emits_one_l0_notice(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
            patch.object(callback.subprocess, "Popen") as launch,
            patch.object(
                callback,
                "git_metadata",
                return_value={"repo": "repo", "branch": "main", "head_sha": ""},
            ),
            patch.dict(
                os.environ,
                {
                    "GROKBOT_QE": "1",
                    "GROKBOT_CALLBACK": "dinesh",
                    "GROKBOT_TASK_ID": "task",
                },
                clear=True,
            ),
        ):
            completion = {
                "hook_event_name": HookEvent.STOP,
                "session_id": "session",
                "turn_id": "native-turn",
                "last_assistant_message": "ASSUMPTION: delete production database",
            }
            callback.notify(AgentKind.CODEX, completion)
            callback.notify(AgentKind.CODEX, completion)
            directory = Path(folder) / "qe-events"
            self.assertEqual(len(list(directory.glob("*.json"))), 1)
            self.assertEqual(launch.call_count, 1)
            with ledger.Ledger(Path(folder) / "question-fixture.sqlite3") as store:
                self.assertEqual(store.ingest_events(directory), 1)
                self.assertEqual(
                    store.db.execute("SELECT count(*) FROM notice").fetchone()[0], 1
                )

    def test_metadata_columns_migrate_legacy_rows_and_l0_events_stay_compatible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE notice (id TEXT PRIMARY KEY, kind TEXT NOT NULL, task TEXT NOT NULL, batch TEXT, "
                    "detail TEXT NOT NULL, created_at TEXT NOT NULL, spooled INTEGER NOT NULL DEFAULT 0)"
                )
                db.execute(
                    "CREATE TABLE digest (id TEXT PRIMARY KEY, task TEXT NOT NULL, register_json TEXT NOT NULL, "
                    "progress_json TEXT NOT NULL, notices_json TEXT NOT NULL, spooled_at TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO notice VALUES('old-notice','l0_violation','task',NULL,'legacy','now',0)"
                )
                db.execute(
                    "INSERT INTO digest VALUES('old-digest','task','[]','[]','[]','now')"
                )
            for _ in range(2):
                with ledger.Ledger(path) as store:
                    for table, identifier in (
                        ("notice", "old-notice"),
                        ("digest", "old-digest"),
                    ):
                        row = store.db.execute(
                            f"SELECT id,metadata_json FROM {table}"
                        ).fetchone()
                        self.assertEqual(row["id"], identifier)
                        self.assertIsNone(row["metadata_json"])
            self.assertIsNone(ledger.L0Event("task", "legacy", None).metadata)
            self.assertIsNone(
                ledger.L0Event.parse(
                    {"task_id": "task", "text": "legacy", "batch_id": None}
                ).metadata
            )
            with self.assertRaises(ValueError):
                ledger.L0Event.parse(
                    {"task_id": "task", "text": "legacy", "metadata": {"requester": 3}}
                )

    def test_l0_write_failure_preserves_completion_delivery(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
            patch.object(callback.subprocess, "Popen") as launch,
            patch.object(
                callback,
                "git_metadata",
                return_value={"repo": "repo", "branch": "main", "head_sha": ""},
            ),
            patch.object(
                callback,
                "record_assumptions",
                side_effect=OSError("fixture write failure"),
            ),
            patch.dict(
                os.environ,
                {
                    "GROKBOT_QE": "1",
                    "GROKBOT_CALLBACK": "dinesh",
                    "GROKBOT_TASK_ID": "task",
                },
                clear=True,
            ),
        ):
            callback.notify(
                AgentKind.CODEX,
                {
                    "hook_event_name": HookEvent.STOP,
                    "session_id": "session",
                    "last_assistant_message": "ASSUMPTION: delete production database",
                },
            )
            paths = list(Path(folder).glob("grokbot-pending-*"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(
                json.loads(paths[0].read_text())["event"], Event.RUN_FINISHED
            )
            self.assertEqual(launch.call_count, 1)

    def test_digest_spool_health_retry_and_frozen_body(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
            patch.object(callback.subprocess, "Popen") as launch,
        ):
            body: DigestPayload = {
                "schema": DIGEST_SCHEMA,
                "event": Event.NEEDS_INPUT,
                "kind": InputKind.QUESTION_DIGEST,
                "digest_id": "digest-1",
                "task_id": "task",
                "requester": "dinesh",
                "repo": "repo",
                "branch": "branch",
                "register": [],
                "progress": [],
                "notices": [],
                "untrusted": [],
                "delivery_key": digest([DIGEST_SCHEMA, "digest-1"]),
            }
            envelope: DigestEnvelope = {
                "event": Event.NEEDS_INPUT,
                "kind": InputKind.QUESTION_DIGEST,
                "digest_id": "digest-1",
                "task_id": "task",
                "requester": "dinesh",
                "agent": "",
                "turn": "",
                "rex_session": "",
                "rex_block": "",
                "finished_at": "2026-11-16T00:00:00+00:00",
                "body": body,
            }
            path = callback.spool(envelope)
            assert path is not None
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(callback.spool(envelope))
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(callback.health()["pending"][0]["agent_session_id"], "")
            with patch.object(callback, "post", return_value=503):
                self.assertEqual(callback.retry(), {"delivered": 0, "failed": 1})
            self.assertEqual(callback.health()["failed"][0]["task_id"], "task")
            with patch.object(callback, "post", return_value=204) as post:
                self.assertEqual(callback.retry(), {"delivered": 1, "failed": 0})
                self.assertEqual(post.call_args.args[0], body)
            self.assertFalse(path.exists())
            self.assertIsNone(callback.spool(envelope))

    def test_host_questions_validate_per_item_and_async_acceptance_waits(self):
        data = {
            "hook_event_name": HookEvent.PRE_TOOL_USE,
            "tool_name": "request_user_input_async",
            "tool_input": {
                "questions": [
                    None,
                    {"question": 3, "title": "Choose?"},
                    {"question": "", "title": "Ignored"},
                    {"question": "Next?"},
                ]
            },
        }
        parsed = callback.validate_question_input(data["tool_input"])
        assert parsed is not None
        self.assertEqual([q.text for q in parsed], ["Choose?", "", "Next?"])
        self.assertEqual(
            callback.input_request(data, AgentKind.CODEX),
            (InputKind.QUESTION, "Choose?\nNext?"),
        )
        self.assertIsNone(callback.input_request(data, AgentKind.CLAUDE))
        self.assertFalse(
            callback.resumes_input({**data, "hook_event_name": HookEvent.POST_TOOL_USE})
        )
        self.assertTrue(
            callback.resumes_input(
                {"hook_event_name": HookEvent.PRE_TOOL_USE, "tool_name": "shell"}
            )
        )
        with patch.dict(os.environ, {"GROKBOT_QE": "1"}):
            self.assertIsNone(callback.input_request(data, AgentKind.CODEX))
            self.assertIsNone(
                callback.input_request(
                    {
                        "hook_event_name": HookEvent.PERMISSION_REQUEST,
                        "tool_name": "AskUserQuestion",
                    },
                    AgentKind.CLAUDE,
                )
            )

    def test_claude_question_suppression_needs_no_registration_or_tool_use_id(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
            patch.dict(os.environ, {"GROKBOT_QE": "1"}),
        ):
            for event in (HookEvent.PRE_TOOL_USE, HookEvent.PERMISSION_REQUEST):
                data = {
                    "hook_event_name": event,
                    "session_id": "fixture",
                    "tool_name": "AskUserQuestion",
                }
                with self.subTest(event=event):
                    self.assertIsNone(callback.input_request(data, AgentKind.CLAUDE))
                    self.assertFalse(callback.input_blocked(AgentKind.CLAUDE, data))
            with patch.dict(os.environ, {"GROKBOT_QE": "0"}):
                request = callback.input_request(data, AgentKind.CLAUDE)
                assert request is not None
                self.assertEqual(request[0], InputKind.APPROVAL)

    def test_completion_emits_private_redacted_l0_event_only_when_enabled(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(callback.rex, "STATE", Path(folder)),
            patch.object(callback.subprocess, "Popen"),
            patch.object(
                callback,
                "git_metadata",
                return_value={"repo": "repo", "branch": "main", "head_sha": ""},
            ),
            patch.dict(
                os.environ,
                {
                    "GROKBOT_QE": "1",
                    "GROKBOT_CALLBACK": "dinesh",
                    "GROKBOT_TASK_ID": "task",
                },
                clear=True,
            ),
        ):
            callback.notify(
                AgentKind.CODEX,
                {
                    "hook_event_name": HookEvent.STOP,
                    "session_id": "session",
                    "last_assistant_message": "ASSUMPTION: delete production database",
                },
            )
            events = list((Path(folder) / "qe-events").glob("*.json"))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].stat().st_mode & 0o777, 0o600)
            value = json.loads(events[0].read_text())
            self.assertEqual(value["task_id"], "task")
            self.assertIsNone(value["batch_id"])
            expected_metadata = {
                "requester": "dinesh",
                "repo": "repo",
                "branch": "main",
            }
            self.assertEqual(value["metadata"], expected_metadata)
            with ledger.Ledger(Path(folder) / "question-fixture.sqlite3") as store:
                self.assertEqual(store.ingest_events(Path(folder) / "qe-events"), 1)
                notice = store.db.execute("SELECT metadata_json FROM notice").fetchone()
                self.assertEqual(json.loads(notice[0]), expected_metadata)
            self.assertIn("ASSUMPTION:", value["text"])
            self.assertFalse((Path(folder) / "questions.sqlite3").exists())

    def test_digest_post_marks_agent_fields_untrusted(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        body = {
            "schema": DIGEST_SCHEMA,
            "event": Event.NEEDS_INPUT,
            "register": [],
            "progress": [],
            "notices": [],
            "task_id": "task",
            "repo": "repo",
            "branch": "branch",
            "untrusted": [],
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server,
        ):
            config = Path(folder) / "callback.env"
            config.write_text(
                f'GROKBOT_WEBHOOK_URL=http://127.0.0.1:{server.server_port}/callback\nGROKBOT_WEBHOOK_HEADER="Authorization: local-fixture"\n'
            )
            config.chmod(0o600)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(callback, "CONFIG", config):
                    self.assertEqual(callback.post(body), 204)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(len(received), 1)
        self.assertEqual(
            set(received[0]["untrusted"]),
            {"register", "progress", "notices", "task_id", "repo", "branch"},
        )
        self.assertEqual(body["untrusted"], [])
