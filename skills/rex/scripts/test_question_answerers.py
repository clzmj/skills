"""Stub-only answerer boundary tests; never invoke a vendor CLI."""

import unittest
import question_answerers as a
from question_model import Model


import json
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import tomllib
import re
from unittest.mock import patch
import question_model as m
from test_question_ledger import asker, question


class CapabilityTests(unittest.TestCase):
    def test_calibrated_models_have_bounds_codex_remains_refused(self):
        configs = a.load_models()
        for model in (Model.HAIKU, Model.SONNET):
            config = configs[model]
            self.assertFalse(config.enabled)
            self.assertTrue(
                config.bounded
                and config.tools_validated
                and config.reservation_validated
            )
            assert config.bounds is not None
            self.assertEqual(config.bounds.max_turns, 4)
        for model in (Model.GPT_LUNA, Model.GPT_SOL):
            self.assertFalse(configs[model].enabled or configs[model].bounded)

    def test_configuration_rejects_truthy_flags_and_invalid_numbers(self):
        source = (a.ROOT / "question_models.toml").read_text()

        def mutate(field, value):
            changed, count = re.subn(
                rf"^{re.escape(field)} = .*?$",
                f"{field} = {value}",
                source,
                count=1,
                flags=re.M,
            )
            self.assertEqual(count, 1)
            self.assertNotEqual(changed, source)
            return changed

        invalid = (
            mutate("enabled", '"false"'),
            mutate("bounded", '"true"'),
            mutate("reservation_validated", '"false"'),
            mutate("tools_validated", '"false"'),
            mutate("tools_validated", "false").replace(
                "enabled = false", "enabled = true", 1
            ),
            mutate("max_turns", "true"),
            mutate("max_turns", "0"),
            mutate("max_turns", "-1"),
            re.sub(r"^max_turns = .*?\n", "", source, count=1, flags=re.M),
            mutate("reserve_tokens", "true"),
            mutate("reserve_tokens", "-1"),
            mutate("reserve_usd", '"NaN"'),
            mutate("reserve_usd", '"-1"'),
            mutate("reserve_usd", "0.01"),
            source.replace("[models.sonnet]", "[models.unknown]", 1),
            source[: source.index("[models.sonnet]")],
            mutate("enabled", "true\nunknown = true"),
            mutate("observed_tokens", "[true]"),
            mutate("observed_usd", '["NaN"]'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    self.assertNotEqual(candidate, source)
                    path.write_text(candidate)
                    with self.assertRaises(ValueError):
                        a.load_models(path)

    def test_reservations_match_representative_read_bearing_calibration(self):
        fixture = (
            Path(__file__).parents[3]
            / "tests/qe/hooks/claude-p12-read-bearing-calibration.json"
        )
        raw = json.loads(fixture.read_text())
        self.assertEqual(raw["sample_count"], 6)
        self.assertEqual(raw["max_turns"], 4)
        self.assertEqual(raw["prompt_bytes"], 24512)
        configs = a.load_models()
        metadata = tomllib.loads((a.ROOT / "question_models.toml").read_text())[
            "models"
        ]
        for sample in raw["samples"]:
            model = Model(sample["model"])
            self.assertEqual(sample["exit_code"], 0)
            self.assertEqual(sample["subtype"], "success")
            self.assertFalse(sample["timed_out"])
            self.assertTrue(
                sample["successful_evidence"]
                and sample["model_family_valid"]
                and sample["marker_evidence_valid"]
            )
            self.assertIsNone(sample["parse_failure"])
            self.assertIsNone(sample["local_rejection"])
            self.assertEqual(sample["validation_failures"], [])
            self.assertEqual(sample["prompt_bytes"], 24512)
            self.assertEqual(sample["max_turns"], 4)
            reported = {actual: {} for actual in sample["model_ids"]}
            self.assertTrue(a.model_family_matches({"modelUsage": reported}, model))
        for model in (Model.HAIKU, Model.SONNET):
            samples = [sample for sample in raw["samples"] if sample["model"] == model]
            self.assertEqual(len(samples), 3)
            bounds = configs[model].bounds
            assert bounds is not None
            self.assertEqual(
                bounds.reserve_tokens,
                max(sample["tokens"] for sample in samples),
            )
            self.assertEqual(
                bounds.reserve_usd,
                max(Decimal(sample["usd"]) for sample in samples),
            )
            self.assertEqual(metadata[model]["reservation_samples"], 3)
            self.assertEqual(metadata[model]["reservation_method"], "nearest_rank_p95")
            self.assertEqual(
                metadata[model]["observed_tokens"],
                [sample["tokens"] for sample in samples],
            )
            self.assertEqual(
                metadata[model]["observed_usd"], [sample["usd"] for sample in samples]
            )


def batch(cwd):
    from datetime import datetime, timezone
    from uuid import uuid4

    return m.QuestionBatch(
        m.BatchId(uuid4()),
        m.BatchRev(1),
        replace(asker(), cwd=str(cwd)),
        (
            replace(
                question(),
                options=(
                    m.Option(
                        m.OptionIndex(0), m.Untrusted("Tabs", m.UntrustedSource.AGENT)
                    ),
                ),
                digest=m.TextDigest(""),
            ),
        ),
        (),
        m.Urgency.BLOCKING,
        m.BatchState.OPEN,
        m.Classification(
            m.Category.TRIVIAL_FACTUAL, True, (), (), m.EscalationLevel.L1_CHEAP
        ),
        m.ContextDigest("context"),
        m.EscalationLevel.L1_CHEAP,
        datetime.now(timezone.utc),
    )


def output():
    return {
        "verdict": "answer",
        "category": "trivial_factual",
        "delegable": True,
        "sensitive_topics": [],
        "reversible": True,
        "crosses_boundary": False,
        "changes_adr": False,
        "revert": None,
        "answers": [
            {
                "question": 1,
                "choice": [0],
                "free_text": None,
                "rationale": "Use tabs.",
                "confidence": 0.95,
                "evidence": [{"path": "README.md", "line": 1, "quote": "Use tabs."}],
            }
        ],
    }


class Store:
    def __init__(self, path):
        self.path = path / "ledger.sqlite3"
        self.reservations = []
        self.settlements = []

    def reserve_budget(self, task, **kwargs):
        self.reservations.append(kwargs)
        return str(len(self.reservations))

    def settle_budget(self, reservation, *, usage):
        self.settlements.append((reservation, usage))


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.script = self.root / "stub"
        bounds = m.InvocationBounds(
            24576, 2, None, timedelta(seconds=3), 100, Decimal(".01")
        )
        self.config = a.ModelConfig(m.Model.HAIKU, True, True, bounds, True, True)
        self.runner = a.Answerers(
            self.store,
            configs={m.Model.HAIKU: self.config},
            slots_dir=self.root / "slots",
            executable=str(self.script),
        )

    def stub(self, body):
        self.script.write_text("#!/usr/bin/env python3\n" + body)
        self.script.chmod(0o700)

    def test_ordinal_roundtrip_and_schema(self):
        raw = output()
        parsed = a.parse_answerer_output(json.loads(json.dumps(raw)), batch(self.root))
        self.assertEqual(parsed.answers[0].choice, (0,))
        self.assertEqual(parsed.classification.floor, m.EscalationLevel.L1_CHEAP)
        self.assertEqual(
            set(
                json.loads((a.ROOT / "answerer_output.v1.json").read_text())["required"]
            ),
            set(raw),
        )
        raw["answers"][0]["question"] = True
        with self.assertRaises(ValueError):
            a.parse_answerer_output(raw, batch(self.root))

    def test_each_retry_reserves_and_unknown_usage_spends_reservation(self):
        self.stub('print("bad json")\n')
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.reason, m.EscalationReason.ANSWERER_FAILED)
        self.assertEqual(len(self.store.reservations), 2)
        self.assertEqual(self.store.settlements, [("1", None), ("2", None)])
        self.assertEqual([r.ordinal for r in result.attempts], [1, 2])

    def test_success_is_ephemeral_other_vendor_scrubbed_and_inline_schema(self):
        payload = {
            "subtype": "success",
            "modelUsage": {"claude-haiku-5-5": {}},
            "structured_output": output(),
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
                "output_tokens": 5,
            },
            "total_cost_usd": ".001",
        }
        self.stub(
            'import json, os, sys\nassert "--no-session-persistence" in sys.argv\nassert json.loads(sys.argv[sys.argv.index("--json-schema")+1])["type"]=="object"\nassert sys.argv[sys.argv.index("--tools")+1]=="Read,Grep,Glob"\nassert "GROKBOT_CALLBACK" not in os.environ\nassert "REX_SESSION" not in os.environ\nassert "OTHER_SECRET" not in os.environ\nassert "UNRELATED_SETTING" not in os.environ\nassert "USER" not in os.environ\nassert "LOGNAME" not in os.environ\nassert "--strict-mcp-config" in sys.argv\nassert sys.argv[sys.argv.index("--permission-mode")+1]=="dontAsk"\nprint('
            + repr(json.dumps(payload))
            + ")\n"
        )
        with patch.dict(
            os.environ,
            {
                "GROKBOT_CALLBACK": "secret",
                "OTHER_SECRET": "secret",
                "UNRELATED_SETTING": "not-forwarded",
                "USER": "qe-test-user",
                "LOGNAME": "qe-test-login",
            },
        ):
            result = self.runner.invoke(
                batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
            )
        self.assertEqual(result.outcome, m.AttemptOutcome.ANSWERED)
        self.assertEqual(self.store.settlements[0][1], m.Usage(9, 5, Decimal(".001")))
        self.assertEqual(len(result.attempts), 1)

    def test_prompt_bound_and_secret_quarantine_start_no_process(self):
        for prompt in ("x" * 24577, "password=hunter2"):
            result = self.runner.invoke(
                batch(self.root), m.EscalationLevel.L1_CHEAP, prompt
            )
            self.assertEqual(result.outcome, m.AttemptOutcome.REJECTED)
        self.assertEqual(self.store.reservations, [])

    def test_timeout_kills_process_and_retries_when_worker_budget_allows(self):
        assert self.config.bounds is not None
        self.runner.configs = {
            m.Model.HAIKU: replace(
                self.config,
                bounds=replace(self.config.bounds, wall_clock=timedelta(seconds=0.08)),
            )
        }
        self.stub("import time\ntime.sleep(20)\n")
        waits = []
        result = self.runner.invoke(
            batch(self.root),
            m.EscalationLevel.L1_CHEAP,
            "Choose tabs.",
            on_wait=lambda: waits.append(True),
        )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertEqual(len(result.attempts), 2)
        self.assertTrue(waits)

    def test_global_slots_reject_third_without_reservation(self):
        with (
            a.answerer_slot(self.root / "slots") as one,
            a.answerer_slot(self.root / "slots") as two,
        ):
            self.assertTrue(one and two)
            result = self.runner.invoke(
                batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
            )
        self.assertEqual(result.reason, m.EscalationReason.NO_ANSWERER)
        self.assertFalse(self.store.reservations)

    def test_child_process_group_is_killed_and_guard_fd_inherited(self):
        assert self.config.bounds is not None
        self.runner.configs = {
            m.Model.HAIKU: replace(
                self.config,
                bounds=replace(self.config.bounds, wall_clock=timedelta(seconds=0.5)),
            )
        }
        marker = self.root / "descendant-pids"
        guard = os.open(self.root / "guard", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, guard)
        self.stub(
            "import os, subprocess, time\nos.fstat("
            + str(guard)
            + ')\np = subprocess.Popen(["sleep", "20"])\nwith open('
            + repr(str(marker))
            + ',"a") as f: f.write(str(p.pid)+"\\n")\ntime.sleep(20)\n'
        )
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "x" * 24000, pass_fds=(guard,)
        )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        pids = [int(pid) for pid in marker.read_text().splitlines()]
        self.assertEqual(len(pids), 2)
        # ps reports terminated descendants as absent or zombies pending OS reaping.
        import subprocess

        for pid in pids:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)

    def test_codex_and_unvalidated_config_start_no_process(self):
        claude = replace(
            batch(self.root),
            asker=replace(asker(), agent=m.AgentKind.CLAUDE, cwd=str(self.root)),
        )
        configs = {
            model: replace(config, enabled=False, tools_validated=False)
            for model, config in a.load_models().items()
        }
        self.runner.configs = configs
        result = self.runner.invoke(claude, m.EscalationLevel.L1_CHEAP, "Choose tabs.")
        self.assertEqual(result.outcome, m.AttemptOutcome.REFUSED_UNBOUNDED)
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.reason, m.EscalationReason.NO_ANSWERER)
        self.assertFalse(self.store.reservations)

    def test_nonobject_json_retries_and_malformed_usage_spends_reservation(self):
        self.stub('print("[]")\n')
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.reason, m.EscalationReason.ANSWERER_FAILED)
        self.assertEqual(len(result.attempts), 2)
        for usage in (
            [],
            {"input_tokens": 10, "cache_read_input_tokens": -9, "output_tokens": 2},
        ):
            self.assertIsNone(a.parse_usage({"usage": usage, "total_cost_usd": ".001"}))

    def test_sensitive_topics_obey_utf8_wire_byte_limit(self):
        raw = output()
        raw["sensitive_topics"] = ["é" * 65]
        with self.assertRaises(ValueError):
            a.parse_answerer_output(raw, batch(self.root))

    def test_absolute_deadline_is_shared_across_failed_retry(self):
        import time

        marker = self.root / "attempt-count"
        self.stub(
            "import pathlib, time\np=pathlib.Path("
            + repr(str(marker))
            + ")\nif p.exists():\n time.sleep(20)\nelse:\n p.touch()\n time.sleep(.15)\n print('invalid json')\n"
        )
        start = time.monotonic()
        with patch.object(a, "MIN_USEFUL_SECONDS", 0.01):
            result = self.runner.invoke(
                batch(self.root),
                m.EscalationLevel.L1_CHEAP,
                "Choose tabs.",
                deadline=start + 1.5,
            )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertEqual(len(result.attempts), 2)
        self.assertLess(time.monotonic() - start, 2.2)
        self.assertEqual(len(self.store.settlements), 2)

    def test_expired_deadline_starts_no_subprocess_or_reservation(self):
        import time

        result = self.runner.invoke(
            batch(self.root),
            m.EscalationLevel.L1_CHEAP,
            "Choose tabs.",
            deadline=time.monotonic() - 1,
        )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertFalse(self.store.reservations)

    def test_runtime_invalid_configuration_refuses_without_reserving_or_starting(self):
        for content in (None, "not valid toml =", "[models]\n"):
            path = self.root / "invalid-models.toml"
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content)
            runner = a.Answerers(
                self.store,
                config_path=path,
                slots_dir=self.root / "slots",
                executable=str(self.script),
            )
            result = runner.invoke(
                batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
            )
            self.assertEqual(result.reason, m.EscalationReason.NO_ANSWERER)
        self.assertFalse(self.store.reservations)

    def test_popen_oserror_releases_reservation_without_spend(self):
        self.runner.executable = str(self.root / "missing-answerer")
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.reason, m.EscalationReason.ANSWERER_FAILED)
        self.assertEqual(len(self.store.reservations), 2)
        self.assertEqual(
            self.store.settlements,
            [("1", m.Usage(0, 0, Decimal(0))), ("2", m.Usage(0, 0, Decimal(0)))],
        )

    def test_wait_callback_before_launch_has_no_reservation(self):
        error = ValueError("registration unavailable")
        with self.assertRaises(ValueError) as raised:
            self.runner.invoke(
                batch(self.root),
                m.EscalationLevel.L1_CHEAP,
                "Choose tabs.",
                on_wait=lambda: (_ for _ in ()).throw(error),
            )
        self.assertIs(raised.exception, error)
        self.assertEqual(self.store.reservations, [])
        self.assertEqual(self.store.settlements, [])

    def test_registration_callback_failure_is_reraised_after_kill_and_settle(self):
        import signal

        marker = self.root / "callback-child-pid"
        self.stub(
            "import os, pathlib, time\npathlib.Path("
            + repr(str(marker))
            + ").write_text(str(os.getpid()))\ntime.sleep(20)\n"
        )
        error = ValueError("registration transaction failed")
        failed = False

        def callback():
            nonlocal failed
            if marker.exists() and not failed:
                failed = True
                raise error

        with self.assertRaises(ValueError) as raised:
            self.runner.invoke(
                batch(self.root),
                m.EscalationLevel.L1_CHEAP,
                "Choose tabs.",
                on_wait=callback,
            )
        self.assertIs(raised.exception, error)
        self.assertEqual(len(self.store.reservations), 1)
        self.assertEqual(self.store.settlements, [("1", None)])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), signal.SIGCONT)

    def test_usage_missing_required_input_tokens_is_unknown(self):
        self.assertIsNone(
            a.parse_usage({"usage": {"output_tokens": 2}, "total_cost_usd": ".001"})
        )

    def test_deeply_nested_json_fails_and_retries_instead_of_crashing(self):
        self.stub('print("["*2000+"]"*2000)\n')
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.reason, m.EscalationReason.ANSWERER_FAILED)
        self.assertEqual(len(result.attempts), 2)

    def test_near_deadline_starts_no_reservation_or_process(self):
        import time

        result = self.runner.invoke(
            batch(self.root),
            m.EscalationLevel.L1_CHEAP,
            "Choose tabs.",
            deadline=time.monotonic() + 0.01,
        )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertFalse(self.store.reservations)

    def test_timeout_retries_only_with_full_invocation_window_left(self):
        import time

        assert self.config.bounds is not None
        self.runner.configs = {
            m.Model.HAIKU: replace(
                self.config,
                bounds=replace(self.config.bounds, wall_clock=timedelta(seconds=0.2)),
            )
        }
        self.stub("import time\ntime.sleep(20)\n")
        with patch.object(a, "MIN_USEFUL_SECONDS", 0.01):
            result = self.runner.invoke(
                batch(self.root),
                m.EscalationLevel.L1_CHEAP,
                "Choose tabs.",
                deadline=time.monotonic() + 0.25,
            )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertEqual(len(result.attempts), 1)
        with patch.object(a, "MIN_USEFUL_SECONDS", 0.01):
            result = self.runner.invoke(
                batch(self.root),
                m.EscalationLevel.L1_CHEAP,
                "Choose tabs.",
                deadline=time.monotonic() + 0.8,
            )
        self.assertEqual(result.reason, m.EscalationReason.TIMEOUT)
        self.assertEqual(len(result.attempts), 2)

    def test_actual_subprocess_argv_matches_public_production_builder(self):
        expected = a.build_command(self.config, executable=str(self.script))
        payload = {
            "subtype": "success",
            "modelUsage": {"claude-haiku-5-5": {}},
            "structured_output": output(),
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "total_cost_usd": ".001",
        }
        self.stub(
            "import sys\nassert sys.argv == "
            + repr(expected)
            + "\nprint("
            + repr(json.dumps(payload))
            + ")\n"
        )
        result = self.runner.invoke(
            batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
        )
        self.assertEqual(result.outcome, m.AttemptOutcome.ANSWERED)
        self.assertEqual(len(result.attempts), 1)

    def test_model_family_requires_every_actual_key_to_match_requested_family(self):
        cases = (
            ({}, False),
            ({"modelUsage": {}}, False),
            ({"modelUsage": []}, False),
            ({"modelUsage": {"": {}}}, False),
            ({"modelUsage": {"claude-sonnet-5-5": {}}}, False),
            ({"modelUsage": {"claude-haiku-5-5": {}, "claude-sonnet-5-5": {}}}, False),
            ({"modelUsage": {"claude-haiku-garbage": {}}}, False),
            ({"modelUsage": {"claude-haiku-5-5": {}}}, True),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertIs(a.model_family_matches(raw, m.Model.HAIKU), expected)
        self.assertTrue(
            a.model_family_matches(
                {"modelUsage": {"claude-sonnet-5-5": {}}}, m.Model.SONNET
            )
        )
        self.assertFalse(
            a.model_family_matches(
                {"modelUsage": {"claude-sonnet-5-5": {}}}, m.Model.GPT_SOL
            )
        )

    def test_missing_or_substituted_model_refused_once_and_usage_settled(self):
        for reported in (
            None,
            {},
            {"claude-sonnet-5-5": {}},
            {"claude-haiku-5-5": {}, "claude-sonnet-5-5": {}},
        ):
            self.store.reservations.clear()
            self.store.settlements.clear()
            payload = {
                "subtype": "success",
                "structured_output": output(),
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "total_cost_usd": ".001",
            }
            if reported is not None:
                payload["modelUsage"] = reported
            self.stub("print(" + repr(json.dumps(payload)) + ")\n")
            result = self.runner.invoke(
                batch(self.root), m.EscalationLevel.L1_CHEAP, "Choose tabs."
            )
            self.assertEqual(result.outcome, m.AttemptOutcome.REJECTED)
            self.assertEqual(result.reason, m.EscalationReason.MODEL_MISMATCH)
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(len(self.store.reservations), 1)
            self.assertEqual(
                self.store.settlements, [("1", m.Usage(2, 3, Decimal(".001")))]
            )
