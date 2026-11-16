"""Self-checks for pure probe utilities and the P14 stub contract."""

import base64
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.qe.probes import qe_probe
from tests.qe.probes.probe_support import (
    Outcome,
    ProbeId,
    ProbeResult,
    build_fixture,
    redact,
)
from tests.qe.probes.run_plan_stub import canonical_json, plan_digest


STUB = Path(__file__).with_name("run_plan_stub.py")
EXPECTED_SPECIAL_PLAN_DIGEST = (
    "8dfcc47f344d50afcd5b8a495280a3b06f11155b3c05c1cc16128edfe0aa656c"
)
EXPECTED_SPECIAL_RECEIVED_DIGEST = (
    "9cd30519d37bc106c23904a4708b0ba9f455aa5ba594905f2b7034ed22065cc8"
)


def encoded_plan(payload: str) -> str:
    plan = {"action": "probe", "payload": payload}
    plan["plan_digest"] = plan_digest(plan)
    return base64.urlsafe_b64encode(canonical_json(plan)).decode("ascii").rstrip("=")


class ProbeSupportTests(unittest.TestCase):
    def test_redaction_preserves_json_and_masks_quoted_credentials(self) -> None:
        source = (
            '{"token": "abcdefgh12345678", "url": "ssh://user:password@example.test/x"}'
        )
        result = json.loads(redact(source))
        self.assertEqual(result["token"], "[REDACTED_TOKEN]")
        self.assertNotIn("user:password", result["url"])

    def test_fixture_has_required_conventions_adr_and_escalation_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = build_fixture(Path(temporary) / "fixture")
            self.assertIn("use tabs", (root / "CONTRIBUTING.md").read_text())
            self.assertIn("lint with oxlint", (root / "CONTRIBUTING.md").read_text())
            self.assertIn("Status: Accepted", (root / "docs/adr/001.md").read_text())
            self.assertIn("```toml qe-escalation", (root / "BRIEF.md").read_text())

    def test_result_model_is_typed_and_includes_probe_evidence_fields(self) -> None:
        result = ProbeResult(
            probe_id=ProbeId.P14_STUB,
            date=date(2026, 11, 16),
            versions={"codex": "test"},
            commands=("python run_plan_stub.py",),
            payload="redacted",
            observation="digest echoed",
            outcome=Outcome.PASS,
        )
        self.assertEqual(result.probe_id.value, "P14-stub")
        self.assertEqual(result.outcome, Outcome.PASS)
        self.assertIsNone(result.fallback)

    def test_redaction_masks_credentials_userinfo_and_other_agent_ids(self) -> None:
        raw = (
            "ghp_abcdefghijklmnopqrstuvwxyz123456 "
            "https://alice:password@example.test/path "
            "agent 11111111-2222-3333-4444-555555555555 "
            "Jim e3b7bb61-41a1-4408-abac-58b26a2c47d0"
        )
        cleaned = redact(raw)
        self.assertNotIn("ghp_", cleaned)
        self.assertNotIn("alice:password", cleaned)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", cleaned)
        self.assertEqual(redact("secret=abcdefgh12345678"), "secret=[REDACTED_TOKEN]")
        self.assertIn("e3b7bb61-41a1-4408-abac-58b26a2c47d0", cleaned)


class HarnessCommandTests(unittest.TestCase):
    def make_stub_path(self, root: Path) -> tuple[Path, Path]:
        bin_dir = root / "bin"
        log_dir = root / "logs"
        bin_dir.mkdir()
        log_dir.mkdir()
        for cli in ("codex", "claude", "rex"):
            script = bin_dir / cli
            script.write_text(
                "#!/bin/sh\n"
                f"CLI={cli}\n"
                'printf \'%s\\0\' "$@" >> "$QE_STUB_LOG_DIR/$CLI.args"\n'
                "printf '\\0' >> \"$QE_STUB_LOG_DIR/$CLI.args\"\n"
                'if [ "$QE_STUB_MODE" = versions ]; then\n'
                "  printf '%s stub-version\\n' \"$CLI\"\n"
                "  exit 0\n"
                "fi\n"
                ': > "$QE_STUB_LOG_DIR/$CLI.current"\n'
                'if [ ! -t 0 ]; then cat > "$QE_STUB_LOG_DIR/$CLI.current"; fi\n'
                "printf 'ARGS:'\n"
                'for arg in "$@"; do printf \' <%s>\' "$arg"; done\n'
                "printf '\\nSTDIN:'\n"
                'cat "$QE_STUB_LOG_DIR/$CLI.current"\n'
                "printf '\\n'\n"
                'cat "$QE_STUB_LOG_DIR/$CLI.current" >> "$QE_STUB_LOG_DIR/$CLI.stdin"\n'
                "printf '\\0' >> \"$QE_STUB_LOG_DIR/$CLI.stdin\"\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
        return bin_dir, log_dir

    def stub_environment(self, bin_dir: Path, log_dir: Path, mode: str = "capture"):
        return mock.patch.dict(
            os.environ,
            {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "QE_STUB_LOG_DIR": str(log_dir),
                "QE_STUB_MODE": mode,
            },
        )

    def test_command_forwards_arguments_cwd_and_verbatim_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir, log_dir = self.make_stub_path(root)
            payload = 'quoted "value"\nnext\r'
            with self.stub_environment(bin_dir, log_dir):
                result = qe_probe.command(
                    ["codex", "check", "two words"], cwd=root, text=payload
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn("ARGS: <check> <two words>", result.stdout)
            self.assertIn('STDIN:quoted "value"', result.stdout)
            args = (log_dir / "codex.args").read_bytes().split(b"\0\0")[0].split(b"\0")
            self.assertEqual(args, [b"check", b"two words"])
            self.assertEqual(
                (log_dir / "codex.stdin").read_bytes(), payload.encode() + b"\0"
            )

    def test_versions_uses_codex_claude_and_rex_stubs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir, log_dir = self.make_stub_path(Path(temporary))
            with self.stub_environment(bin_dir, log_dir, mode="versions"):
                found = qe_probe.versions()
            self.assertEqual(
                found,
                {cli: f"{cli} stub-version" for cli in ("codex", "claude", "rex")},
            )
            self.assertEqual((log_dir / "codex.args").read_bytes(), b"--version\0\0")
            self.assertEqual((log_dir / "claude.args").read_bytes(), b"--version\0\0")
            self.assertEqual((log_dir / "rex.args").read_bytes(), b"version\0\0")

    def test_deliver_rex_and_queue_resume_arguments_and_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir, log_dir = self.make_stub_path(Path(temporary))
            message = 'quoted "question"\nsecond line'
            with self.stub_environment(bin_dir, log_dir):
                rex_results = qe_probe.deliver(
                    "session-1", "block:abc", "thread-1", message, "rex"
                )
                queue_results = qe_probe.deliver(
                    "session-1", "block:abc", "thread-1", message, "queue-resume"
                )
            self.assertEqual([item.returncode for item in rex_results], [0, 0])
            self.assertEqual(len(queue_results), 1)
            self.assertEqual(queue_results[0].returncode, 0)
            rex_calls = (log_dir / "rex.args").read_bytes().split(b"\0\0")
            self.assertEqual(
                rex_calls[:2],
                [
                    b"send\0-s\0session-1\0-b\0block:abc",
                    b"send\0-s\0session-1\0-b\0block:abc",
                ],
            )
            self.assertEqual(
                (log_dir / "rex.stdin").read_bytes(), message.encode() + b"\0\r\0"
            )
            codex_calls = (log_dir / "codex.args").read_bytes().split(b"\0\0")
            self.assertEqual(
                codex_calls[0],
                b"queue\0--thread\0thread-1\0--message\0"
                + message.encode()
                + b"\0--resume",
            )
            self.assertEqual((log_dir / "codex.stdin").read_bytes(), b"\0")


class RunPlanStubTests(unittest.TestCase):
    def test_result_round_trip_preserves_quotes_unicode_newlines_and_cr(self) -> None:
        source = 'quotes: "single\' double"; unicode: café 🐈\nnext line\rfinal'
        encoded = encoded_plan(source)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        completed = subprocess.run(
            ["uv", "run", "--script", str(STUB), "run-plan", "--b64", encoded],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        result_b64 = completed.stdout.removeprefix("QE-RESULT ").strip()
        result = json.loads(
            base64.urlsafe_b64decode(result_b64 + "=" * (-len(result_b64) % 4))
        )
        self.assertEqual(result["plan_digest"], EXPECTED_SPECIAL_PLAN_DIGEST)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), EXPECTED_SPECIAL_RECEIVED_DIGEST
        )
        self.assertEqual(result["received_digest"], EXPECTED_SPECIAL_RECEIVED_DIGEST)

    def test_64_kib_argv_arrives_intact_through_cli(self) -> None:
        plan = {"action": "probe", "payload": "x" * 48_000}
        plan["plan_digest"] = plan_digest(plan)
        raw = canonical_json(plan)
        self.assertLessEqual(len(raw), 49_152)
        raw += b" " * (49_152 - len(raw))
        best = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        self.assertEqual(len(best.encode("ascii")), 65_536)
        completed = subprocess.run(
            ["uv", "run", "--script", str(STUB), "run-plan", "--b64", best],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        self.assertTrue(completed.stdout.startswith("QE-RESULT "))
        result_b64 = completed.stdout.removeprefix("QE-RESULT ").strip()
        result = json.loads(
            base64.urlsafe_b64decode(result_b64 + "=" * (-len(result_b64) % 4))
        )
        self.assertEqual(result["received_digest"], hashlib.sha256(raw).hexdigest())

    def test_malformed_and_tampered_plans_exit_64_without_result(self) -> None:
        malformed = subprocess.run(
            ["uv", "run", "--script", str(STUB), "run-plan", "--b64", "%%%"],
            capture_output=True,
            text=True,
            check=False,
        )
        tampered_plan = {
            "action": "probe",
            "payload": "changed",
            "plan_digest": "0" * 64,
        }
        tampered = (
            base64.urlsafe_b64encode(canonical_json(tampered_plan))
            .decode("ascii")
            .rstrip("=")
        )
        invalid = subprocess.run(
            ["uv", "run", "--script", str(STUB), "run-plan", "--b64", tampered],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(malformed.returncode, 64)
        self.assertEqual(invalid.returncode, 64)
        self.assertEqual(malformed.stdout, "")
        self.assertEqual(invalid.stdout, "")


if __name__ == "__main__":
    unittest.main()
