"""Calibration opt-in guards; every subprocess entry point is mocked."""

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from . import calibrate_answerers as calibration


class CalibrationGuards(unittest.TestCase):
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
