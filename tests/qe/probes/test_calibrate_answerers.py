"""Calibration opt-in guards; every subprocess entry point is mocked."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import sys
import platform
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import calibrate_answerers as calibration


class CalibrationGuards(unittest.TestCase):
    def test_api_key_rejected_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            argv = [
                "calibrate_answerers.py",
                "--run",
                "--output-dir",
                str(output),
                "--receipt",
                str(Path(directory) / "receipt.json"),
            ]
            for auth in (
                {"ANTHROPIC_API_KEY": "fake"},
                {"ANTHROPIC_API_KEY": "fake", "CLAUDE_CODE_OAUTH_TOKEN": "fake"},
            ):
                with (
                    self.subTest(auth=auth),
                    patch.object(sys, "argv", argv),
                    patch.dict(os.environ, auth, clear=True),
                    patch.object(calibration.subprocess, "Popen") as popen,
                    patch.object(calibration.subprocess, "run") as run,
                    patch.object(
                        calibration.subprocess, "check_output"
                    ) as check_output,
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    calibration.main()
                self.assertEqual(raised.exception.code, 2)
                popen.assert_not_called()
                run.assert_not_called()
                check_output.assert_not_called()
                self.assertFalse(output.exists())

    def test_keychain_auth_resolved_before_calibration_subprocesses(self):
        for available in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "not-created"
                stderr = StringIO()
                secret = "keychain-private-value"
                with (
                    self.subTest(available=available),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "calibrate_answerers.py",
                            "--run",
                            "--output-dir",
                            str(output),
                            "--receipt",
                            str(Path(directory) / "receipt.json"),
                        ],
                    ),
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(platform, "system", return_value="Darwin"),
                    patch.object(
                        calibration.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], 0 if available else 44, secret + "\n", secret
                        ),
                    ) as security,
                    patch.object(calibration.subprocess, "Popen") as popen,
                    patch.object(
                        calibration.subprocess,
                        "check_output",
                        side_effect=RuntimeError("stop before any calibration"),
                    ) as git,
                    redirect_stderr(stderr),
                ):
                    with self.assertRaises(RuntimeError if available else SystemExit):
                        calibration.main()
                    self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)
                security.assert_called_once()
                self.assertEqual(
                    security.call_args.args[0][:4],
                    [
                        "/usr/bin/security",
                        "find-generic-password",
                        "-s",
                        "grokbot-claude-oauth-token",
                    ],
                )
                self.assertEqual(git.call_count, int(available))
                popen.assert_not_called()
                self.assertNotIn(secret, stderr.getvalue())
                self.assertFalse(output.exists())

    def test_missing_opt_in_or_active_qe_never_starts_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            argv = [
                "calibrate_answerers.py",
                "--output-dir",
                str(output),
                "--receipt",
                str(Path(directory) / "receipt.json"),
            ]
            for opt_in, active in ((False, "0"), (True, "1")):
                with (
                    self.subTest(opt_in=opt_in, active=active),
                    patch.object(sys, "argv", argv + (["--run"] if opt_in else [])),
                    patch.dict(os.environ, {"GROKBOT_QE": active}),
                    patch.object(calibration.subprocess, "Popen") as popen,
                    patch.object(calibration.subprocess, "run") as run,
                    patch.object(
                        calibration.subprocess, "check_output"
                    ) as check_output,
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    calibration.main()
                self.assertEqual(raised.exception.code, 2)
                popen.assert_not_called()
                run.assert_not_called()
                check_output.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
