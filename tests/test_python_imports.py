"""Catch a dropped import: compile, import and name-check every Rex and QE-box module."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/rex/scripts"
QE_BOX = ROOT / "grokbot/qe-box"
MODULES = sorted([*SCRIPTS.rglob("*.py"), *QE_BOX.rglob("*.py")])
# The module's name is not "__main__", so entry points stay idle.
IMPORT = "import importlib, sys; sys.path[:0] = sys.argv[2:]; importlib.import_module(sys.argv[1])"
# Runs the installer at import (no __main__ guard); compile and name checks still cover it.
NOT_IMPORTABLE = frozenset({SCRIPTS / "install_codex.py"})


class PythonImportTests(unittest.TestCase):
    def test_modules_compile_and_import(self):
        self.assertTrue(MODULES)
        with tempfile.TemporaryDirectory() as home:
            for path in MODULES:
                with self.subTest(module=str(path.relative_to(ROOT))):
                    compile(path.read_bytes(), str(path), "exec")
                    if path in NOT_IMPORTABLE:
                        continue
                    result = subprocess.run(
                        [sys.executable, "-B", "-c", IMPORT, path.stem, str(path.parent), str(SCRIPTS), str(QE_BOX)],
                        cwd=home,
                        env={**os.environ, "HOME": home},
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_undefined_names(self):
        # Importing misses a name that is only used inside a function body.
        # --isolated: no per-file ignore in ruff.toml can switch this off.
        command = ["uv", "run", "--locked", "--only-group", "dev", "ruff", "check", "--isolated", "--no-cache"]
        command += ["--target-version", "py311", "--select", "F821,F822", *map(str, MODULES)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
