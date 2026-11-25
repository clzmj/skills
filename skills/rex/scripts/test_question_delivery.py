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
from unittest.mock import patch

import question_delivery as delivery
import question_hooks as hooks
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
from test_question_takeover import AcceptedRoutes


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

    def test_x14_lost_result_after_rex_send_never_resends_before_reconcile(self):
        self.store.db.execute(
            "UPDATE outbound SET route_plan_json=? WHERE id=?",
            (ledger.encoded((m.Route.REX_SEND,)), str(self.message.id)),
        )
        stub = self.bin / "rex"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys\n"
            "assert sys.argv[1] == 'send'\n"
            "text = sys.stdin.read()\n"
            "with open(os.environ['QE_CALL_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(text) + '\\n'); stream.flush(); os.fsync(stream.fileno())\n"
            "if text == '\\r':\n"
            "    os.kill(os.getppid(), signal.SIGKILL)\n"
        )
        stub.chmod(0o755)
        process = self._dispatch_process()
        process.communicate(timeout=15)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(self._calls(), [self.message.text, "\r"])
        dispatcher = delivery.Delivery(self.store)
        self.assertEqual(dispatcher.recover(), 1)
        self.assertEqual(
            self.store.get_outbound(str(self.message.id)).state,
            m.DeliveryState.DISPATCH_UNCERTAIN,
        )
        retry = self._dispatch_process()
        retry.communicate(timeout=15)
        self.assertEqual(retry.returncode, 1)
        self.assertEqual(self._calls(), [self.message.text, "\r"])
        self.store.reconcile(str(self.message.id), m.ReconcileVerdict.SEEN_IN_PANE)
        self.assertEqual(
            self.store.get_outbound(str(self.message.id)).state,
            m.DeliveryState.OBSERVED,
        )
        self.assertFalse(dispatcher.dispatch(str(self.message.id)))
        self.assertEqual(self._calls(), [self.message.text, "\r"])

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

    def claude_pending(self):
        batch = self.store.open_or_attach(
            replace(
                self.batch.asker,
                agent=m.AgentKind.CLAUDE,
                actor=m.ActorKey("claude:session-1"),
            ),
            (question("claude-call"),),
            native_call(
                "claude-call", tool=m.QuestionTool.CLAUDE_ASK, actor="claude:session-1"
            ),
            classification(),
        ).batch
        self.store.take_ownership(str(batch.id), m.EscalationLevel.L3_PRINCIPAL)
        (message,) = self.store.outbounds(str(batch.id))
        # Synthetic inline seam: P4b/P5 admission remains disabled in production.
        message = replace(
            message, route_plan=(m.Route.INLINE_DENY_REASON, m.Route.REX_SEND)
        )
        self.store.db.execute(
            "UPDATE outbound SET route_plan_json=? WHERE id=?",
            (ledger.encoded(message.route_plan), str(message.id)),
        )
        return batch, message

    def test_worker_before_permission_request_preserves_inline_window(self):
        batch, message = self.claude_pending()
        runner = AcceptedRoutes()
        dispatcher = delivery.Delivery(self.store, runner=runner)
        # PreToolUse may launch the worker before PermissionRequest registers a waiter.
        with patch.object(
            ledger,
            "now",
            return_value=batch.opened_at + m.INLINE_WAIT - timedelta(microseconds=1),
        ):
            self.assertEqual(dispatcher.dispatch_batch(str(batch.id)), 0)
        self.assertEqual(runner.texts, [])
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.PENDING
        )
        input = m.parse_hook_input(
            dict(
                hook_event_name="PermissionRequest",
                session_id="session-1",
                tool_name="AskUserQuestion",
                tool_use_id="claude-call",
            ),
            m.AgentKind.CLAUDE,
        )
        output = hooks.inline_output(self.store, str(batch.id), input)
        self.assertEqual(
            output["hookSpecificOutput"]["decision"]["message"], message.text
        )
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.DISPATCHED
        )
        self.assertFalse(dispatcher.dispatch(str(message.id)))
        self.assertEqual(runner.texts, [])

    def test_expired_waiter_dispatches_late_while_inline_window_is_open(self):
        batch, message = self.claude_pending()
        runner = AcceptedRoutes()
        dispatcher = delivery.Delivery(self.store, runner=runner)
        dispatcher.wait_inline(
            str(batch.id), HookEvent.PERMISSION_REQUEST, batch.opened_at
        )
        with patch.object(ledger, "now", return_value=batch.opened_at):
            self.assertEqual(dispatcher.dispatch_batch(str(batch.id)), 1)
        self.assertEqual(runner.texts, [message.text])
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.DISPATCHED
        )

    def test_dead_waiter_dispatches_late_while_inline_window_is_open(self):
        batch, message = self.claude_pending()
        runner = AcceptedRoutes()
        dispatcher = delivery.Delivery(self.store, runner=runner)
        process = subprocess.Popen(["true"])
        process.wait(timeout=5)
        dispatcher.wait_inline(
            str(batch.id),
            HookEvent.PERMISSION_REQUEST,
            batch.opened_at + m.INLINE_WAIT,
            pid=process.pid,
        )
        with patch.object(ledger, "now", return_value=batch.opened_at):
            self.assertEqual(dispatcher.dispatch_batch(str(batch.id)), 1)
        self.assertEqual(runner.texts, [message.text])
        self.assertEqual(
            self.store.get_outbound(str(message.id)).state, m.DeliveryState.DISPATCHED
        )

    def test_expired_inline_window_selects_late_route_without_mutating_intent(self):
        batch, message = self.claude_pending()
        runner = AcceptedRoutes()
        dispatcher = delivery.Delivery(self.store, runner=runner)
        with patch.object(ledger, "now", return_value=batch.opened_at + m.INLINE_WAIT):
            self.assertTrue(dispatcher.dispatch(str(message.id)))
        after = self.store.get_outbound(str(message.id))
        self.assertEqual(after.route_plan, message.route_plan)
        self.assertEqual(
            (after.text, after.body_digest, after.ack_token),
            (message.text, message.body_digest, message.ack_token),
        )
        self.assertEqual(runner.texts, [message.text])
        self.assertEqual(
            self.store.db.execute(
                "SELECT route FROM dispatch_attempt WHERE outbound=?",
                (str(message.id),),
            ).fetchone()[0],
            m.Route.REX_SEND,
        )

    def test_broker_echo_observes_pending_and_failed_intents_without_resend(self):
        for initial in (m.DeliveryState.PENDING, m.DeliveryState.DISPATCH_FAILED):
            with self.subTest(initial=initial):
                self.store.db.execute(
                    "UPDATE outbound SET state=? WHERE id=?",
                    (initial, str(self.message.id)),
                )
                self.assertEqual(
                    hooks.prompt_origin(
                        self.store,
                        self.message.target_actor,
                        "session-1",
                        self.message.text + "\n",
                    ),
                    m.PromptOrigin.BROKER,
                )
                self.assertEqual(
                    self.store.get_outbound(str(self.message.id)).state,
                    m.DeliveryState.OBSERVED,
                )
                runner = AcceptedRoutes()
                self.assertFalse(
                    delivery.Delivery(self.store, runner=runner).dispatch(
                        str(self.message.id)
                    )
                )
                self.assertEqual(runner.texts, [])


if __name__ == "__main__":
    unittest.main()
