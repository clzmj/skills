#!/usr/bin/env python3
"""Run focused Python/Rex checks without third-party test dependencies."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"], cwd=ROOT, check=True)
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "skills/python-tooling/scripts/rex"), "-p", "test_*.py", "-v"], cwd=ROOT, check=True)
