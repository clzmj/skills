"""Catch a dropped import: compile, import and name-check every tracked Python file."""

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/rex/scripts"
QE_BOX = ROOT / "grokbot/qe-box"
# NUL-separated, so git never quotes an unusual file name.
LS_FILES = ["git", "ls-files", "-z", "--", "*.py"]
# Not imported, because importing runs the program. Each value is the start of the
# module-level statement that does so; compile and name checks still cover the file.
NOT_IMPORTABLE = {
    ROOT / "scripts/check.py": "for suite in ",  # runs every suite, including this one
    SCRIPTS / "install_codex.py": "runpy.run_path(",  # runs the installer
    ROOT / "tests/installer-pty.py": "exercise(",  # drives an installer in a PTY
}
# Allowlist, not a denylist: a test run must not reach a live Rex server, bot session,
# secret store or the user's Claude/Anthropic/GitHub/XDG settings.
ENV_NAMES = ("PATH", "HOME", "LANG", "TMPDIR", "VIRTUAL_ENV")
ENV_PREFIXES = ("LC_", "UV_")  # locale and uv settings


# The module's name is not "__main__", so entry points stay idle. The __file__ check
# fails if the stem resolved to some other module, which would test nothing.
IMPORT = """
import importlib, sys
name, path, *roots = sys.argv[1:]
sys.path[:0] = roots
module = importlib.import_module(name)
assert module.__file__ == path, f"imported {module.__file__}, not {path}"
"""
# Nested inside check.py's `uv run --no-project`: this syncs the project .venv with
# the locked dev group (network on a cold cache), for ruff and for scan.py's parsers.
DEV = ["uv", "run", "--locked", "--only-group", "dev"]


def clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if k in ENV_NAMES or k.startswith(ENV_PREFIXES)
    }


def run(command, **options):
    options.setdefault("env", clean_env())
    return subprocess.run(command, capture_output=True, text=True, **options)


def tracked_modules():
    # Called by each test, not at import, so a git failure is a test failure.
    # Untracked files are skipped until they are `git add`ed.
    listed = subprocess.run(
        LS_FILES, cwd=ROOT, capture_output=True, text=True, env=clean_env(), check=True
    )
    return [ROOT / name for name in listed.stdout.split("\0") if name]


class PythonImportTests(unittest.TestCase):
    def test_modules_compile_and_import(self):
        modules = tracked_modules()
        for root in (SCRIPTS, QE_BOX):
            self.assertTrue([p for p in modules if p.is_relative_to(root)], root)
        find = [*DEV, "python", "-c", "import sys; print(sys.executable)"]
        found = run(find, cwd=ROOT, timeout=300)
        self.assertEqual(found.returncode, 0, found.stderr)
        python = found.stdout.strip()
        roots = [str(p) for p in (SCRIPTS, QE_BOX, ROOT)]
        with tempfile.TemporaryDirectory() as home:
            env = {**clean_env(), "HOME": home}
            for path in modules:
                with self.subTest(module=str(path.relative_to(ROOT))):
                    compile(path.read_bytes(), str(path), "exec")
                    if path in NOT_IMPORTABLE:
                        continue
                    name = path.stem
                    if (path.parent / "__init__.py").exists():  # relative imports
                        name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
                    command = [python, "-B", "-c", IMPORT, name, str(path)]
                    result = run(
                        [*command, str(path.parent), *roots],
                        cwd=home,
                        env=env,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_exemptions_are_current(self):
        # Read, never import: a still-needed exemption is one that is unsafe to run.
        modules = tracked_modules()
        for path, statement in NOT_IMPORTABLE.items():
            with self.subTest(module=str(path.relative_to(ROOT))):
                if path not in modules:
                    self.fail("exempt file is gone: drop its entry")
                body = ast.parse(path.read_bytes()).body
                self.assertTrue(
                    any(ast.unparse(node).startswith(statement) for node in body),
                    f"no module-level `{statement}` left: drop its entry",
                )

    def test_no_undefined_names(self):
        # Importing misses a name that is only used inside a function body.
        # --isolated: no per-file ignore in ruff.toml can switch this off.
        command = [*DEV, "ruff", "check", "--isolated", "--no-cache"]
        command += ["--target-version", "py311", "--select", "F821,F822"]
        modules = [str(p) for p in tracked_modules()]
        result = run([*command, *modules], cwd=ROOT, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
