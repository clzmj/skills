#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Process-level delivery regressions for the local outbound dispatcher."""

import json
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import question_ledger as ledger
import question_model as m
from agent_types import HookEvent
from test_question_ledger import (
    asker,
    classification,
    native_call,
    outbound,
    question,
    resolution,
)


SCRIPTS = Path(__file__).resolve().parent


class QuestionDeliveryProcesses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.db_path = self.directory / "ledger.sqlite3"
        self.store = ledger.Ledger(self.db_path)
        self.addCleanup(self.store.close)
        subprocess.run(["git", "init", "-q", str(self.directory)], check=True)
        subprocess.run(
            ["git", "-C", str(self.directory), "config", "user.name", "QE fixture"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.directory),
                "config",
                "user.email",
                "qe@example.invalid",
            ],
            check=True,
        )
        (self.directory / "fixture.txt").write_text("delivery fixture\n")
        subprocess.run(
            ["git", "-C", str(self.directory), "add", "fixture.txt"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.directory), "commit", "-qm", "fixture"],
            check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(self.directory), "rev-parse", "HEAD"], text=True
        ).strip()
        self.batch = self.store.open_or_attach(
            asker(cwd=str(self.directory), head=head),
            (question(),),
            native_call("call-1"),
            classification(),
        ).batch
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        self.message = outbound(self.batch)
        self.message = replace(
            self.message,
            route_plan=(m.Route.CODEX_QUEUE, m.Route.CODEX_QUEUE_RESUME),
        )
        self.store.resolve(resolution(self.batch), (self.message,))
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "calls.jsonl"
        self._install_codex_stub()

    def _install_codex_stub(self):
        stub = self.bin / "codex"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, signal, sys, time\n"
            "args = sys.argv[1:]\n"
            "if (args[0:1] != ['queue'] or len(args) not in (3, 4) or "
            "not args[1].startswith('--thread=') or "
            "not args[-1].startswith('--message=') or "
            "(len(args) == 4 and args[2] != '--resume')):\n"
            "    sys.exit(91)\n"
            "with open(os.environ['QE_CALL_LOG'], 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(args[1:]) + '\\n'); f.flush(); os.fsync(f.fileno())\n"
            "if os.environ.get('QE_KILL_DISPATCHER') == '1':\n"
            "    os.kill(os.getppid(), signal.SIGKILL)\n"
            "    time.sleep(float(os.environ.get('QE_HOLD_AFTER_KILL', '0')))\n"
            ""
        )
        stub.chmod(0o755)

    def _env(self, **extra):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
            PYTHONPATH=str(SCRIPTS),
            QE_CALL_LOG=str(self.log),
        )
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def _dispatch_process(self, extra_env=None, outbound_id=None):
        code = (
            "import question_ledger as l, question_delivery as d, sys; "
            "from pathlib import Path; s=l.Ledger(Path(sys.argv[1])); "
            "raise SystemExit(0 if d.Delivery(s).dispatch(sys.argv[2]) else 1)"
        )
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(self.db_path),
                str(outbound_id or self.message.id),
            ],
            cwd=self.directory,
            env=self._env(**(extra_env or {})),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_two_processes_racing_one_answer_make_one_codex_queue_call(self):
        first = self._dispatch_process()
        second = self._dispatch_process()
        results = [
            process.communicate(timeout=15) + (process.returncode,)
            for process in (first, second)
        ]

        attempts = self.store.db.execute("SELECT * FROM dispatch_attempt").fetchall()
        self.assertEqual(
            sorted(result[2] for result in results),
            [0, 1],
            (
                results,
                [dict(row) for row in attempts],
                self.store.get_outbound(str(self.message.id)),
            ),
        )
        self.assertEqual(len(self._calls()), 1)
        (args,) = self._calls()
        self.assertEqual(args[0], "--thread=session-1")
        self.assertEqual(args[-1], f"--message={self.message.text}")
        self.assertIn("--resume", args)
        self.assertEqual(
            self.store.get_outbound(str(self.message.id)).state,
            m.DeliveryState.DISPATCHED,
        )

    def test_dispatcher_killed_after_route_acceptance_recovers_uncertain_without_resend(
        self,
    ):
        process = self._dispatch_process(
            {"QE_KILL_DISPATCHER": "1", "QE_HOLD_AFTER_KILL": "1"}
        )
        import time

        deadline = time.monotonic() + 5
        while not self.log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.log.exists(), "route stub did not record its call")
        competing = self._dispatch_process()
        competing.communicate(timeout=15)
        self.assertEqual(competing.returncode, 1)
        process.communicate(timeout=15)
        returncode = process.returncode
        self.assertEqual(returncode, -signal.SIGKILL)
        self.assertEqual(len(self._calls()), 1)

        from question_delivery import Delivery

        self.assertEqual(Delivery(self.store).recover(), 1)
        self.assertEqual(
            self.store.get_outbound(str(self.message.id)).state,
            m.DeliveryState.DISPATCH_UNCERTAIN,
        )
        retry = self._dispatch_process()
        retry.communicate(timeout=15)
        self.assertEqual(len(self._calls()), 1)

    def test_sweeper_wins_and_late_finish_claim_is_dropped(self):
        claim = self.store.claim(
            str(self.message.id), "pid:old-owner", m.Route.CODEX_QUEUE
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        future = datetime.now(timezone.utc) + m.CLAIM_STALE_AFTER + timedelta(seconds=1)
        self.assertEqual(self.store.recover_claims(future), 1)
        self.assertFalse(self.store.finish_claim(claim, m.DeliveryState.DISPATCHED))
        self.assertEqual(
            self.store.get_outbound(str(self.message.id)).state,
            m.DeliveryState.DISPATCH_UNCERTAIN,
        )

    def test_inline_waiter_and_worker_race_for_one_dispatch(self):
        from question_delivery import Delivery

        inline = replace(
            self.message,
            route_plan=(m.Route.INLINE_UPDATED_INPUT, m.Route.CODEX_QUEUE),
        )
        # Replace the persisted plan to model an answer created for inline first,
        # with a queued route available after the hook's deadline.
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE outbound SET route_plan_json=? WHERE id=?",
                (ledger.encoded(inline.route_plan), str(inline.id)),
            )
        now = datetime.now(timezone.utc)
        delivery = Delivery(self.store)
        event = HookEvent.PRE_TOOL_USE
        delivery.wait_inline(str(self.batch.id), event, now + timedelta(seconds=30))

        # A normal worker cannot steal a route while the hook's inline wait is live.
        worker = self._dispatch_process(outbound_id=str(inline.id))
        _, stderr = worker.communicate(timeout=15)
        self.assertEqual(worker.returncode, 1, stderr)
        self.assertEqual(self._calls(), [])

        # The hook-owned dispatch consumes the same claim and marks it delivered.
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            accepted = delivery.dispatch(str(inline.id), inline_pid=os.getpid())
        self.assertTrue(accepted)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            self.store.get_outbound(str(inline.id)).state,
            m.DeliveryState.DISPATCHED,
        )
        self.assertEqual(self._calls(), [])


if __name__ == "__main__":
    unittest.main()
