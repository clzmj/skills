#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Replay recorded prompt wrappers, then explicitly synthetic question calls.

The PR-0 hook fixtures record UserPromptSubmit wrappers. They do not establish a
native question-call binding. P4/P13 question binding remains synthetic or
unproven; these tests use the recorded prompt payload only for lifecycle input.
"""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
QE_BOX = ROOT / "grokbot/qe-box"
sys.path.insert(0, str(QE_BOX))

import qe_box as box  # noqa: E402
import grokbot_callback as callback  # noqa: E402
import question_broker as broker  # noqa: E402
import question_hooks as hooks  # noqa: E402
import question_ledger as ledger  # noqa: E402
import question_model as m  # noqa: E402
import question_routes as routes  # noqa: E402


def recorded_prompt(name: str) -> dict[str, Any]:
    """Read only the first, recorded UserPromptSubmit payload from a fixture."""
    path = ROOT / "tests/qe/hooks" / name
    wrapper = json.loads(path.read_text().splitlines()[0])
    payload = wrapper["payload"]
    if payload.get("hook_event_name") != m.HookEvent.USER_PROMPT_SUBMIT:
        raise AssertionError(f"{name} does not start with a recorded prompt event")
    return payload


class FixtureReplay(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mac_state = self.root / "mac-state"
        self.store = ledger.Ledger(self.mac_state / m.LEDGER_FILE)
        self.addCleanup(self.store.close)
        self.box = box.Box(
            self.root / "box-state" / "qe-box.sqlite3",
            bots=(box.Bot("jim", "fixture-agent", "Jim"),),
        )
        self.addCleanup(self.box.close)
        self.log = self.root / "routes.jsonl"
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = f"""#!{sys.executable}
import json, os, pathlib, sys
program = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if program == "codex":
    if len(args) != 3 or args[0] != "queue" or not args[1].startswith("--thread=") or not args[2].startswith("--message="):
        raise SystemExit(64)
    with open(os.environ["QE_FIXTURE_ROUTE_LOG"], "a") as stream:
        stream.write(json.dumps(args) + "\\n")
    raise SystemExit(0)
if program == "rex" and args[:1] == ["send"]:
    raise SystemExit(64)
if program == "git" and args == ["rev-parse", "HEAD"]:
    raise SystemExit(0)
raise SystemExit(64)
"""
        for name in ("codex", "rex", "git"):
            executable = bindir / name
            executable.write_text(stub)
            executable.chmod(0o755)
        self.env = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "REX_VISIBLE_STATE_DIR": str(self.mac_state),
                "QE_FIXTURE_ROUTE_LOG": str(self.log),
                m.FLAG_ENV: "1",
                m.BRIEF_ENV: "",
                "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state_patch = patch.object(callback.rex, "STATE", self.mac_state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.state_lookup = patch.object(
            callback, "session_state", side_effect=self.session_state
        )
        self.state_lookup.start()
        self.addCleanup(self.state_lookup.stop)
        self.lineage = patch.object(
            callback, "session_lineage", side_effect=self.session_lineage
        )
        self.lineage.start()
        self.addCleanup(self.lineage.stop)
        self.git = patch.object(
            callback,
            "git_metadata",
            return_value={"repo": "", "branch": "", "head_sha": ""},
        )
        self.git.start()
        self.addCleanup(self.git.stop)
        self.remote = patch.object(hooks, "remote", return_value="")
        self.remote.start()
        self.addCleanup(self.remote.stop)
        self.mac = broker.Broker(
            self.store,
            runner=routes.RouteRunner(),
            lock_wait=0,
        )

    @staticmethod
    def session_state(
        agent: callback.AgentKind, session: str, child: str = ""
    ) -> callback.SessionState:
        actor = callback.actor_key(agent, session, child)
        return callback.SessionState(
            key=f"{agent}:{actor}",
            id=session,
            agent=agent,
            parent="",
            root=f"{agent}:{actor}",
            depth=0,
            task_id="fixture:qe-pr3",
            requester="Jim",
            turn="fixture-turn",
            status=callback.Status.RUNNING,
            rex_session="fixture-session",
            rex_block="block:fixture",
        )

    @staticmethod
    def session_lineage(state: callback.SessionState) -> callback.Lineage:
        return callback.Lineage(
            requester_bot=state["requester"],
            root_session_id=state["id"],
            parent_session_id="",
            agent_session_id=state["id"],
            depth=0,
            agent_kind=state["agent"],
            role=callback.Role.AGENT,
        )

    def submit_prompt(
        self, payload: dict[str, Any], agent: callback.AgentKind, session: str
    ) -> None:
        prompt = dict(
            payload, session_id=session, turn_id="fixture-turn", cwd=str(self.root)
        )
        hooks.handle(prompt, agent, self.store, spawn=False)

    def register_with_box(self, batch_id: str) -> m.RegisterEntry:
        batch = broker.Broker(self.store).worker(batch_id, spool_digest=False)
        (entry,) = self.store.due_registrations()
        self.assertEqual(entry["batch_id"], str(batch.id))
        registered = self.box.register(entry)
        ack = registered.plans[0].plan
        self.assertEqual(ack["action"], m.PlanAction.ACK)
        result = self.mac.run_plan(ack)
        self.box.result(m.RESULT_PREFIX + broker.encode(result))
        return entry

    @staticmethod
    def synthetic_codex_question(session: str, cwd: str) -> dict[str, Any]:
        return {
            "hook_event_name": m.HookEvent.PRE_TOOL_USE,
            "session_id": session,
            "turn_id": "fixture-turn",
            "cwd": cwd,
            "tool_name": "request_user_input",
            "tool_use_id": "synthetic-codex-call",
            "tool_input": {
                "questions": [
                    {
                        "id": "synthetic-indent",
                        "header": "Indent",
                        "question": "Which indentation style should the fixture use?",
                        "options": [{"label": "Tabs"}, {"label": "Spaces"}],
                    }
                ]
            },
        }

    def test_synthetic_codex_question_runs_through_real_box_ack_decision_and_send(
        self,
    ) -> None:
        prompt = recorded_prompt("codex-p1.jsonl")
        self.assertIn("request_user_input", prompt["prompt"])
        session = "fixture-codex-session"
        self.submit_prompt(prompt, m.AgentKind.CODEX, session)
        # P1's recorded payload is the prompt wrapper; the question event below
        # is synthetic and does not claim a recorded tool-call binding.
        hooks.handle(
            self.synthetic_codex_question(session, str(self.root)),
            m.AgentKind.CODEX,
            self.store,
            spawn=False,
        )
        (batch,) = self.store.batches()
        registered_entry = self.register_with_box(str(batch.id))
        self.assertFalse(registered_entry["classified"])
        self.assertEqual(registered_entry["agent"], m.AgentKind.CODEX)
        self.assertEqual(registered_entry["mac_floor"], m.EscalationLevel.L3_PRINCIPAL)
        self.assertEqual(
            self.box.show(str(batch.id))["record"].state, box.BoxBatchState.AWAITING_L3
        )

        answer = {
            "schema": "qe.decision/1",
            "answers": [
                {
                    "question_id": registered_entry["questions"][0]["id"],
                    "choice": [1],
                    "free_text": None,
                    "rationale": "The existing fixture uses spaces.",
                }
            ],
            "clearances": [],
        }
        with patch.object(m, "ECHO_WAIT", timedelta(0)):
            sent = self.box.decide(
                box.DecisionKind.ANSWER,
                3,
                str(batch.id),
                1,
                answer,
            )
        send_plan = sent.plans[0].plan
        self.assertEqual(send_plan["action"], m.PlanAction.SEND)
        send_result = self.mac.run_plan(send_plan)
        self.assertIn(
            send_result["send"], (m.SendOutcome.SENT_NO_ECHO, m.SendOutcome.SENT_ECHOED)
        )
        self.box.result(m.RESULT_PREFIX + broker.encode(send_result))

        shown = self.box.show(str(batch.id))
        self.assertEqual(shown["record"].state, box.BoxBatchState.DELIVERED)
        self.assertEqual(len(shown["deliveries"]), 1)
        call = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(call[0], "queue")
        self.assertEqual(call[1], f"--thread={registered_entry['root_session']}")
        self.assertTrue(call[2].startswith("--message="))
        self.assertEqual(
            call[2].removeprefix("--message="), shown["deliveries"][0].text
        )

    def test_claude_p5_failure_keeps_unclassified_batch_blocked_without_delivery(
        self,
    ) -> None:
        prompt = recorded_prompt("claude-p5-answer.jsonl")
        self.assertIn("AskUserQuestion", prompt["prompt"])
        session = "fixture-claude-p5-session"
        self.submit_prompt(prompt, m.AgentKind.CLAUDE, session)
        question_event = {
            "hook_event_name": m.HookEvent.PRE_TOOL_USE,
            "session_id": session,
            "turn_id": "fixture-turn",
            "cwd": str(self.root),
            "tool_name": "AskUserQuestion",
            "tool_use_id": "synthetic-claude-call",
            "tool_input": {
                "questions": [
                    {
                        "header": "Indentation",
                        "question": "Which indentation should the fixture use?",
                        "multiSelect": False,
                        "options": [{"label": "tabs"}, {"label": "spaces"}],
                    }
                ]
            },
        }
        hooks.handle(question_event, m.AgentKind.CLAUDE, self.store, spawn=False)
        permission_event = dict(
            question_event, hook_event_name=m.HookEvent.PERMISSION_REQUEST
        )
        hooks.handle(permission_event, m.AgentKind.CLAUDE, self.store, spawn=False)

        (batch,) = self.store.batches()
        self.assertEqual(batch.state, m.BatchState.BLOCKED)
        blocked = self.store.db.execute(
            "SELECT block_cause FROM batch WHERE id=?", (str(batch.id),)
        ).fetchone()
        self.assertEqual(blocked[0], m.BlockCause.NO_DELIVERY_ROUTE)
        worker_batch = broker.Broker(self.store).worker(
            str(batch.id), spool_digest=False
        )
        self.assertEqual(worker_batch.state, m.BatchState.BLOCKED)
        (entry,) = self.store.due_registrations()
        self.assertFalse(entry["classified"])

        registered = self.box.register(entry)
        ack_plan = registered.plans[0].plan
        ack_result = self.mac.run_plan(ack_plan)
        self.box.result(m.RESULT_PREFIX + broker.encode(ack_result))
        shown = self.box.show(str(batch.id))
        self.assertEqual(shown["record"].state, box.BoxBatchState.AWAITING_L3)
        self.assertEqual(shown["deliveries"], [])
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
