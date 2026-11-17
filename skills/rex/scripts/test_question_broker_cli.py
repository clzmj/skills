#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Exercise the public broker CLI against isolated ledger and route fixtures."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import question_broker as broker
import question_ledger as ledger
import question_model as m
from test_question_ledger import (
    asker,
    classification,
    native_call,
    outbound,
    question,
    registration,
    resolution,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
BROKER_CLI = SCRIPT_DIR / "question_broker.py"


def make_plan(action: m.PlanAction, body: dict[str, object]) -> m.Plan:
    value: dict[str, object] = {
        "schema": m.PLAN_SCHEMA,
        "plan_id": str(uuid4()),
        "action": action,
        "issued_by": "dinesh",
        "lease_id": str(uuid4()) if action is m.PlanAction.SEND else None,
        "body": body,
    }
    return m.parse_plan({**value, "plan_digest": m.digest(value)})


def send_plan(batch: m.QuestionBatch, **changes: object) -> m.Plan:
    body: dict[str, object] = {
        "delivery_id": str(uuid4()),
        "attempt": 1,
        "resend_after_reconcile": False,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "valid_s": 480,
        "batch_id": str(batch.id),
        "batch_rev": batch.rev,
        "floor": int(batch.classification.floor),
        "resolution_rev": 1,
        "purpose": m.DeliveryPurpose.ANSWER,
        "target_actor": str(batch.asker.actor),
        "child_actor": None,
        "rex_session": "fixture-session",
        "rex_block": "fixture-block",
        "session_marker": None,
        "allowed_routes": [m.Route.REX_SEND],
        "codex_thread": None,
        "text": "[qe:fixture] CLI route text",
        "ack_token": "a" * 32,
        "echo_wait_s": 0,
    }
    body.update(changes)
    return make_plan(m.PlanAction.SEND, body)


class BrokerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = bindir / "route-stub"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "entry = {'tool': os.path.basename(sys.argv[0]), 'argv': sys.argv[1:], 'stdin': sys.stdin.read() if sys.argv[1:2] == ['send'] else ''}\n"
            "with open(os.environ['ROUTE_STUB_LOG'], 'a') as f: f.write(json.dumps(entry) + '\\n')\n"
        )
        stub.chmod(0o755)
        (bindir / "rex").symlink_to(stub)
        (bindir / "codex").symlink_to(stub)
        self.route_log = self.root / "routes.jsonl"
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "PYTHONPATH": str(SCRIPT_DIR),
                "REX_VISIBLE_STATE_DIR": str(self.state),
                "ROUTE_STUB_LOG": str(self.route_log),
            }
        )

        self.store = ledger.Ledger(self.state / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        self.batch = self.store.open_or_attach(
            asker(cwd=str(REPO_ROOT), head="a" * 40),
            (question(),),
            native_call("cli-call-1"),
            classification(),
        ).batch

    def cli(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BROKER_CLI), *args],
            cwd=REPO_ROOT,
            env=self.env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    @staticmethod
    def encode_plan(request: m.Plan) -> str:
        return broker.encode(request)

    def decode_result(self, stdout: str) -> m.Result:
        lines = [
            line for line in stdout.splitlines() if line.startswith(m.RESULT_PREFIX)
        ]
        self.assertEqual(len(lines), 1, stdout)
        encoded = lines[0][len(m.RESULT_PREFIX) :]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return m.parse_result(json.loads(raw))

    def status(self, *args: str) -> dict[str, Any]:
        result = self.cli("status", *args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_run_plan_valid_emits_one_parseable_result_and_only_uses_route_stubs(
        self,
    ) -> None:
        request = send_plan(self.batch)
        result = self.cli("run-plan", "--b64", self.encode_plan(request))
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = self.decode_result(result.stdout)
        self.assertEqual(parsed["plan_id"], request["plan_id"])
        self.assertEqual(parsed["send"], m.SendOutcome.SENT_NO_ECHO)
        self.assertEqual(len(self.route_log.read_text().splitlines()), 2)
        self.assertEqual(
            self.status("--batch", str(self.batch.id))["batches"][0]["state"],
            m.BatchState.BOX_ANSWERED,
        )

    def test_bad_digest_emits_one_error_result_and_records_no_plan(self) -> None:
        request = send_plan(self.batch)
        request["plan_digest"] = "0" * 64
        result = self.cli("run-plan", "--b64", self.encode_plan(request))
        self.assertEqual(result.returncode, 64)
        parsed = self.decode_result(result.stdout)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error_kind"], m.RunPlanError.DIGEST_MISMATCH)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM plan_seen").fetchone()[0], 0
        )
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.OPEN
        )
        self.assertFalse(self.route_log.exists())

    def test_status_task_and_batch_filters(self) -> None:
        second = self.store.open_or_attach(
            asker(actor="actor-2", turn="turn-2", cwd=str(REPO_ROOT), head="b" * 40),
            (question("call-2"),),
            native_call("cli-call-2", actor="actor-2", turn="turn-2"),
            classification(),
        ).batch
        by_task = self.status("--task", "task-1")
        self.assertEqual(
            {b["batch_id"] for b in by_task["batches"]},
            {str(self.batch.id), str(second.id)},
        )
        by_batch = self.status("--batch", str(self.batch.id))
        self.assertEqual(
            [b["batch_id"] for b in by_batch["batches"]], [str(self.batch.id)]
        )

    def test_registrations_due_is_read_only(self) -> None:
        self.store.put_registration(registration(self.batch))
        before = self.store.db.execute(
            "SELECT tries,last_sent_at FROM registration"
        ).fetchone()
        result = self.cli("registrations", "--due", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(len(parsed["registrations"]), 1)
        after = self.store.db.execute(
            "SELECT tries,last_sent_at FROM registration"
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))

    def test_clear_takeover_and_cancel_local(self) -> None:
        takeover = m.Takeover(
            m.TakeoverId(uuid4()),
            self.batch.id,
            m.PromptOrigin.UNKNOWN,
            None,
            datetime.now(timezone.utc),
            None,
            None,
        )
        self.store.record_takeover(takeover)
        result = self.cli(
            "clear-takeover", "--takeover", str(takeover.id), "--by", "dinesh"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self.status("--batch", str(self.batch.id))["takeovers"][0]["cleared"]
        )

        result = self.cli("cancel-local", "--batch", str(self.batch.id))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.status("--batch", str(self.batch.id))["batches"][0]["state"],
            m.BatchState.CANCELLED,
        )

    def test_ack_stdin_accepts_correct_local_and_box_tokens_and_rejects_wrong_token(
        self,
    ) -> None:
        local = outbound(self.batch)
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        self.store.resolve(resolution(self.batch), (local,))
        premature = self.cli(
            "ack",
            "--outbound",
            str(local.id),
            "--token-stdin",
            input_text=local.ack_token + "\n",
        )
        self.assertEqual(premature.returncode, 3, premature.stderr)
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(local.id),)
            ).fetchone()[0],
            m.DeliveryState.PENDING,
        )
        claim = self.store.claim(str(local.id), "cli-fixture", m.Route.CODEX_QUEUE)
        assert claim is not None
        self.assertTrue(self.store.finish_claim(claim, m.DeliveryState.DISPATCHED))
        bad = self.cli(
            "ack", "--outbound", str(local.id), "--token-stdin", input_text="wrong\n"
        )
        self.assertEqual(bad.returncode, 3)
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(local.id),)
            ).fetchone()[0],
            m.DeliveryState.DISPATCHED,
        )
        good = self.cli(
            "ack",
            "--outbound",
            str(local.id),
            "--token-stdin",
            input_text=local.ack_token + "\n",
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(local.id),)
            ).fetchone()[0],
            m.DeliveryState.APPLIED,
        )

        request = send_plan(self.batch)
        delivered = self.cli("run-plan", "--b64", self.encode_plan(request))
        self.assertEqual(
            self.decode_result(delivered.stdout)["send"], m.SendOutcome.SENT_NO_ECHO
        )
        body = cast(m.SendBody, request["body"])
        bad_box = self.cli(
            "ack",
            "--outbound",
            body["delivery_id"],
            "--token-stdin",
            input_text="wrong",
        )
        self.assertEqual(bad_box.returncode, 3)
        good_box = self.cli(
            "ack",
            "--outbound",
            body["delivery_id"],
            "--token-stdin",
            input_text=body["ack_token"],
        )
        self.assertEqual(good_box.returncode, 0, good_box.stderr)
        notes = self.status("--batch", str(self.batch.id))["batches"][0]["box_notes"]
        self.assertEqual(notes[0]["acked_rev"], 1)

    def test_reconcile_resolves_local_uncertain_outbound(self) -> None:
        local = outbound(self.batch)
        self.store.transition(str(self.batch.id), m.BatchState.ESCALATING)
        self.store.resolve(resolution(self.batch), (local,))
        claim = self.store.claim(str(local.id), "worker", m.Route.CODEX_QUEUE)
        self.assertIsNotNone(claim)
        if claim is None:
            self.fail("fixture outbound could not be claimed")
        self.assertTrue(
            self.store.finish_claim(claim, m.DeliveryState.DISPATCH_UNCERTAIN)
        )
        result = self.cli(
            "reconcile",
            "--outbound",
            str(local.id),
            "--verdict",
            m.ReconcileVerdict.SEEN_IN_PANE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.store.db.execute(
                "SELECT state FROM outbound WHERE id=?", (str(local.id),)
            ).fetchone()[0],
            m.DeliveryState.OBSERVED,
        )

    def test_audit_text_and_tick_marks_ended_session_stale(self) -> None:
        self.store.audit(
            ledger.AuditKind.NOTICE,
            "task-1",
            "fixture audit detail",
            str(self.batch.id),
        )
        audit = self.cli("audit", "--task", "task-1")
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.assertIn("fixture audit detail", audit.stdout)

        self.store.record_lifecycle(
            m.Lifecycle(
                self.batch.asker.actor,
                m.LifecycleKind.SESSION_ENDED,
                None,
                None,
                None,
                datetime.now(timezone.utc),
            ),
            str(self.batch.id),
        )
        tick = self.cli("tick")
        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(
            self.store.get_batch(str(self.batch.id)).state, m.BatchState.STALE
        )


if __name__ == "__main__":
    unittest.main()
