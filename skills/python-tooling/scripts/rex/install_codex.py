#!/usr/bin/env python3
"""Compatibility entry point for the consolidated hook installer."""
from pathlib import Path
import runpy
import sys

sys.argv = [sys.argv[0], "--agents", "codex", "--with-rex", *sys.argv[1:]]
runpy.run_path(str(Path(__file__).resolve().parent.parent / "install_hooks.py"), run_name="__main__")
