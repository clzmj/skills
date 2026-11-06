#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Run focused Python/Rex checks through uv without test dependencies."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
for suite in (ROOT / 'tests', ROOT / 'skills/rex/scripts'):
    subprocess.run(['uv', 'run', '--no-project', 'python', '-m', 'unittest', 'discover', '-s', str(suite), '-p', 'test_*.py', '-v'], cwd=ROOT, check=True)
