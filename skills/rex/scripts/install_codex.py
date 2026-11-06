#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compatibility entry point for the independent Rex installer."""
from pathlib import Path
import runpy
import sys

sys.argv = [sys.argv[0], *sys.argv[1:], "--agents", "codex"]
runpy.run_path(str(Path(__file__).resolve().with_name("install_hooks.py")), run_name="__main__")
