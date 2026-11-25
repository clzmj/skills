#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import question_model as m
from question_routes import RouteOutcome, RouteRunner, select_routes


class QuestionRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = bindir / "stub"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "if os.environ.get('ROUTE_SLEEP'): time.sleep(float(os.environ['ROUTE_SLEEP']))\n"
            "entry = {'argv': sys.argv[1:], 'stdin': sys.stdin.read() if sys.argv[1:2] == ['send'] else ''}\n"
            "with open(os.environ['ROUTE_LOG'], 'a') as f: f.write(json.dumps(entry) + '\\n')\n"
            "print(os.environ.get('ROUTE_STDOUT', 'captured'))\n"
            "raise SystemExit(int(os.environ.get('ROUTE_RC', '0')))\n"
        )
        stub.chmod(0o755)
        (bindir / "rex").symlink_to(stub)
        (bindir / "codex").symlink_to(stub)
        self.log = self.root / "calls.jsonl"
        self.env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "ROUTE_LOG": str(self.log),
        }
        self.old_env = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)

    def tearDown(self) -> None:
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def entries(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @staticmethod
    def body(text: str = "a 'quoted' $message") -> m.SendBody:
        return {
            "delivery_id": "d",
            "attempt": 1,
            "resend_after_reconcile": False,
            "issued_at": "2026-11-16T00:00:00Z",
            "valid_s": 60,
            "batch_id": "b",
            "batch_rev": 1,
            "floor": m.EscalationLevel.L1_CHEAP,
            "resolution_rev": 1,
            "purpose": m.DeliveryPurpose.ANSWER,
            "target_actor": "a",
            "child_actor": None,
            "rex_session": "session-id",
            "rex_block": "block-id",
            "session_marker": None,
            "allowed_routes": [m.Route.REX_SEND],
            "codex_thread": "thread-id",
            "text": text,
            "ack_token": "token",
            "echo_wait_s": 0,
        }

    def test_select_routes_obeys_turn_state_without_opposite_queue_fallback(
        self,
    ) -> None:
        both_codex = [m.Route.CODEX_QUEUE, m.Route.CODEX_QUEUE_RESUME, m.Route.REX_SEND]
        self.assertEqual(
            select_routes(both_codex, running=True, codex_thread="t"),
            (m.Route.CODEX_QUEUE, m.Route.REX_SEND),
        )
        self.assertEqual(
            select_routes(both_codex, running=False, codex_thread="t"),
            (m.Route.CODEX_QUEUE_RESUME, m.Route.REX_SEND),
        )
        self.assertEqual(
            select_routes(both_codex, running=True, codex_thread=None),
            (m.Route.REX_SEND,),
        )

    def test_capture_uses_rex_target_and_trim_unwrap(self) -> None:
        self.assertEqual(RouteRunner().capture("s", "block:b"), "captured\n")
        entry = self.entries()[0]
        self.assertEqual(
            entry["argv"],
            ["capture", "--session=s", "--block=block:b", "--trim", "--unwrap"],
        )

    def test_capture_keeps_option_like_session_as_an_option_value(self) -> None:
        RouteRunner().capture("-session", "block:b")
        self.assertEqual(
            self.entries()[0]["argv"],
            ["capture", "--session=-session", "--block=block:b", "--trim", "--unwrap"],
        )

    def test_rex_send_stages_text_then_a_separate_carriage_return(self) -> None:
        fences = 0

        def fence() -> m.SendOutcome | None:
            nonlocal fences
            fences += 1
            return None

        result = RouteRunner().send(m.Route.REX_SEND, self.body(), fence)
        self.assertEqual(result.outcome, RouteOutcome.ACCEPTED)
        self.assertEqual(fences, 2)
        entries = self.entries()
        self.assertEqual(
            [entry["stdin"] for entry in entries], ["a 'quoted' $message", "\r"]
        )
        self.assertEqual(
            entries[0]["argv"],
            ["send", "--session=session-id", "--block=block:block-id"],
        )

    def test_rex_send_keeps_option_like_session_as_an_option_value(self) -> None:
        body = self.body()
        body["rex_session"] = "-session"
        RouteRunner().send(m.Route.REX_SEND, body, lambda: None)
        self.assertEqual(
            self.entries()[0]["argv"],
            ["send", "--session=-session", "--block=block:block-id"],
        )

    def test_codex_queue_preserves_message_as_one_argument_and_resume_flag(
        self,
    ) -> None:
        result = RouteRunner().send(
            m.Route.CODEX_QUEUE_RESUME, self.body(), lambda: None
        )
        self.assertEqual(result.outcome, RouteOutcome.ACCEPTED)
        self.assertEqual(
            self.entries()[0]["argv"],
            [
                "queue",
                "--thread=thread-id",
                "--resume",
                "--message=a 'quoted' $message",
            ],
        )

    def test_codex_queue_keeps_option_like_values_as_option_values(self) -> None:
        body = self.body("-message 'quoted' $text")
        body["codex_thread"] = "-thread"
        RouteRunner().send(m.Route.CODEX_QUEUE, body, lambda: None)
        self.assertEqual(
            self.entries()[0]["argv"],
            ["queue", "--thread=-thread", "--message=-message 'quoted' $text"],
        )

    def test_missing_codex_thread_is_refused_without_calling_cli(self) -> None:
        body = self.body()
        body["codex_thread"] = None
        result = RouteRunner().send(m.Route.CODEX_QUEUE, body, lambda: None)
        self.assertEqual(result.outcome, RouteOutcome.REFUSED)
        self.assertFalse(self.log.exists())

    def test_unrecorded_nonzero_exit_is_unknown(self) -> None:
        os.environ["ROUTE_RC"] = "1"
        result = RouteRunner().send(m.Route.CODEX_QUEUE, self.body(), lambda: None)
        self.assertEqual(result.outcome, RouteOutcome.UNKNOWN)

    def test_fence_before_first_call_stops_without_running_route(self) -> None:
        result = RouteRunner().send(
            m.Route.REX_SEND,
            self.body(),
            lambda: m.SendOutcome.NOT_SENT_EXPIRED,
        )
        self.assertEqual(result.outcome, RouteOutcome.FENCED)
        self.assertEqual(result.fence, m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertFalse(self.log.exists())

    def test_fence_before_carriage_return_is_unknown_after_staging(self) -> None:
        calls = iter([None, m.SendOutcome.NOT_SENT_EXPIRED])
        result = RouteRunner().send(m.Route.REX_SEND, self.body(), lambda: next(calls))
        self.assertEqual(result.outcome, RouteOutcome.UNKNOWN)
        self.assertEqual(result.fence, m.SendOutcome.NOT_SENT_EXPIRED)
        self.assertEqual(len(self.entries()), 1)


if __name__ == "__main__":
    unittest.main()
