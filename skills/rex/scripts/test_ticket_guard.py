#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Ticket guardrail: launcher refusals, hook enforcement, callback fields, install."""
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import grokbot_callback as callback
import install_hooks as installer

SCRIPTS = Path(__file__).resolve().parent
PAGE = "4233f9dc5b7981c3ba8dd57a99b94956"
TICKET_ID = "4233f9dc-5b79-81c3-ba8d-d57a99b94956"
URL = "https://app.notion.com/p/" + PAGE
TASK = "demo-task"


def write_record(home, task=TASK, raw=None, age_hours=0.0, **fields):
    path = home / ".local/state/agent-tickets" / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"task_id": task, "ticket_url": URL, "ticket_id": TICKET_ID, "status": "In progress",
              "verified_at": (datetime.now(UTC) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "verified_by": "dinesh", **fields}
    path.write_text(raw if raw is not None else json.dumps(record))
    return path


class Sandbox(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.rex_log = self.home / "rex.log"
        self.fake("rex", f'echo "$@" >> {shlex.quote(str(self.rex_log))}\n'
                         'echo \'{"window_id": "w1", "block_ids": ["b1"]}\'\n')
        self.fake("claude", 'env > "$HOME/claude.env"\nprintf "%s\\n" "$@" > "$HOME/claude.args"\n')
        self.worktree = self.home / "work tree"
        self.worktree.mkdir()
        self.brief = self.home / "brief dir" / "brief.md"
        self.brief.parent.mkdir()
        self.brief.write_text("do it")
        self.environment = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                            "REX_SERVER": "unix:///nowhere", "REX_VISIBLE_STATE_DIR": str(self.home / "state")}

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)


class LauncherTests(Sandbox):
    def launch(self, *extra, **overrides):
        options = {"ticket": URL, "label": TASK, "role": "normal", "worktree": str(self.worktree),
                   "brief": str(self.brief), "callback": "dinesh", "session": "s1", **overrides}
        argv = [item for key, value in options.items() for item in (f"--{key}", value)]
        return subprocess.run([sys.executable, str(SCRIPTS / "rex_run.py"), *argv, *extra],
                              env=self.environment, capture_output=True, text=True, check=False)

    def assert_refused(self, result, code, text):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertIn(text, result.stderr)
        self.assertFalse(self.rex_log.exists(), "a refused run must not open a tab")

    def test_refuses_without_record(self):
        self.assert_refused(self.launch(), 4, "no ticket record")

    def test_refuses_bad_ticket_argument(self):
        write_record(self.home)
        for value in ("nonsense", "https://evil.example/p/" + PAGE, PAGE[:-1]):
            self.assert_refused(self.launch(ticket=value), 11, "not a Notion")

    def test_refuses_other_ticket(self):
        write_record(self.home)
        self.assert_refused(self.launch(ticket="0" * 32), 10, "does not match")

    def test_refuses_wrong_task_id(self):
        write_record(self.home, task_id="other-task")
        self.assert_refused(self.launch(), 6, "task id")

    def test_refuses_closed_status(self):
        for status in ("Done", "Won't do"):
            write_record(self.home, status=status)
            self.assert_refused(self.launch(), 7, status)

    def test_refuses_stale_record(self):
        write_record(self.home, age_hours=25)
        self.assert_refused(self.launch(), 8, "older than")

    def test_refuses_corrupt_record(self):
        for fields in ({"raw": "{"}, {"raw": "[]"}, {"status": "Archived"}, {"verified_by": "me"},
                       {"verified_at": "2026-01-01T00:00:00"}, {"ticket_url": "https://app.notion.com/p/" + "0" * 32}):
            write_record(self.home, **fields)
            self.assert_refused(self.launch(), 5, "invalid")

    def test_refuses_label_that_escapes_the_record_directory(self):
        self.assert_refused(self.launch(label="../x"), 3, "task id")

    def test_refuses_model_outside_policy_and_missing_paths(self):
        write_record(self.home)
        self.assert_refused(self.launch(model="gpt"), 12, "model")
        self.assert_refused(self.launch(brief=str(self.home / "none.md")), 13, "brief")
        self.assert_refused(self.launch(worktree=str(self.home / "none")), 13, "worktree")

    def test_valid_record_opens_a_tab_that_exports_the_ticket(self):
        write_record(self.home)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"window_id": "w1", "block_id": "b1",
                                                     "ticket": {"id": TICKET_ID, "url": URL}})
        logged = self.rex_log.read_text()
        self.assertIn(f"window new -s s1 {TASK} -c {self.worktree} --focus=false --keep-open --json -- /bin/sh", logged)
        script = self.home / "state" / f"rex-run-{TASK}.sh"
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        subprocess.run(["/bin/sh", str(script)], env={**self.environment, "GROKBOT_NO_PR": "1"}, check=True)
        environment = dict(line.split("=", 1) for line in (self.home / "claude.env").read_text().splitlines() if "=" in line)
        self.assertEqual((environment["GROKBOT_TICKET"], environment["GROKBOT_TICKET_URL"]), (TICKET_ID, URL))
        self.assertEqual((environment["GROKBOT_TASK_ID"], environment["GROKBOT_CALLBACK"]), (TASK, "dinesh"))
        self.assertEqual(environment["GROKBOT_BRIEF"], str(self.brief))
        self.assertNotIn("GROKBOT_NO_PR", environment)
        arguments = (self.home / "claude.args").read_text().splitlines()
        self.assertEqual(arguments[:4], ["--model", "sonnet", "--effort", "medium"])
        self.assertIn(str(self.brief.parent), arguments)
        self.assertNotIn("--disallowedTools", arguments)

    def test_review_and_no_pr_and_model_override(self):
        write_record(self.home)
        result = self.launch("--review", "--no-pr", model="opus")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{TASK}-review", self.rex_log.read_text())
        script = (self.home / "state" / f"rex-run-{TASK}-review.sh").read_text()
        for expected in ("--model opus", "--disallowedTools", "GROKBOT_REVIEW=1", "GROKBOT_NO_PR=1"):
            self.assertIn(expected, script)

    def test_rex_failure_is_reported(self):
        write_record(self.home)
        self.fake("rex", "echo boom >&2\nexit 1\n")
        result = self.launch()
        self.assertEqual(result.returncode, 14, result.stderr)
        self.assertIn("boom", result.stderr)


class HookTests(Sandbox):
    def hook(self, event, raw=None, **environment):
        data = raw if raw is not None else json.dumps({"hook_event_name": event, "tool_name": "Bash"})
        return subprocess.run([sys.executable, str(SCRIPTS / "ticket_guard.py"), "hook"], input=data,
                              env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", **environment},
                              capture_output=True, text=True, check=False)

    def assert_blocked(self, **environment):
        prompt = self.hook("UserPromptSubmit", **environment)
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        self.assertEqual(json.loads(prompt.stdout)["decision"], "block")
        tool = json.loads(self.hook("PreToolUse", **environment).stdout)["hookSpecificOutput"]
        self.assertEqual((tool["hookEventName"], tool["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertTrue(tool["permissionDecisionReason"])

    VALID = {"GROKBOT_TASK_ID": TASK, "GROKBOT_TICKET": TICKET_ID}

    def test_enforced_sessions_without_a_ticket_are_blocked(self):
        for environment in ({"GROKBOT_CALLBACK": "dinesh"}, {"GROKBOT_TASK_ID": TASK}, {"REX_BLOCK": "block:1"}):
            self.assert_blocked(**environment)

    def test_record_failures_are_blocked(self):
        self.assert_blocked(**self.VALID)  # missing record
        for fields in ({"raw": "{"}, {"task_id": "other"}, {"status": "Done"}, {"age_hours": 25}):
            write_record(self.home, **fields)
            self.assert_blocked(**self.VALID)
        write_record(self.home)
        self.assert_blocked(GROKBOT_TASK_ID=TASK)  # record alone, not launched with the ticket
        self.assert_blocked(GROKBOT_TASK_ID=TASK, GROKBOT_TICKET="0" * 32)

    def test_valid_ticket_is_allowed_without_granting_permission(self):
        write_record(self.home)
        for event in ("UserPromptSubmit", "PreToolUse"):
            result = self.hook(event, **self.VALID)
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        context = json.loads(self.hook("SessionStart", **self.VALID).stdout)["hookSpecificOutput"]
        self.assertIn(URL, context["additionalContext"])

    def test_age_limit_is_configurable(self):
        write_record(self.home, age_hours=25)
        self.assertEqual(self.hook("PreToolUse", GROKBOT_TICKET_MAX_AGE_HOURS="48", **self.VALID).stdout, "")
        self.assert_blocked(GROKBOT_TICKET_MAX_AGE_HOURS="inf", **self.VALID)

    def test_unenforced_session_is_untouched(self):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse"):
            result = self.hook(event)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.hook("", raw="not json").returncode, 0)

    def test_guard_crash_fails_closed_when_enforced(self):
        for raw in ("not json", "[]"):
            self.assertEqual(self.hook("", raw=raw, REX_BLOCK="block:1").returncode, 2)


class CallbackTicketTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.spooled = []
        environment = {"HOME": str(self.home), "GROKBOT_CALLBACK": "dinesh", "GROKBOT_TASK_ID": TASK,
                       "GROKBOT_TICKET": TICKET_ID, "GROKBOT_BRIEF": str(self.brief)}
        for item in (patch.dict(os.environ, environment, clear=True),
                     patch.object(callback.rex, "STATE", self.home / "state"),
                     patch.object(callback, "git_metadata", return_value={"repo": str(self.worktree), "branch": "feat", "head_sha": "abc"}),
                     patch.object(callback, "spool", side_effect=self.spooled.append)):
            item.start()
            self.addCleanup(item.stop)

    def finish(self, session="f0578d19", gh=(0, "[]"), name="Stop", agent="claude", **fields):
        base = {"session_id": session, "cwd": str(self.worktree)}
        callback.notify(agent, {**base, "hook_event_name": "UserPromptSubmit"})
        self.spooled.clear()
        callback.notify(agent, {**base, "hook_event_name": name, "last_assistant_message": "ok", **fields})
        return self.body(gh)

    def body(self, gh=(0, "[]")):
        completed = subprocess.CompletedProcess([], gh[0], gh[1], "")
        with patch.object(callback.subprocess, "run", return_value=completed):
            return callback.payload(self.spooled[-1])

    def assert_suggested(self, body, status, reason=False):
        self.assertEqual((body["ticket_id"], body["ticket_url"], body["suggested_status"]), (TICKET_ID, URL, status))
        self.assertEqual("status_reason" in body, reason)

    def test_open_pr_suggests_in_review(self):
        write_record(self.home)
        body = self.finish(gh=(0, '[{"url": "https://github.com/o/r/pull/1"}]'))
        self.assert_suggested(body, "In review")
        self.assertEqual((body["event"], body["pr_url"]), ("run_finished", "https://github.com/o/r/pull/1"))

    def test_no_pr_brief_suggests_done_and_otherwise_in_progress(self):
        write_record(self.home)
        body = self.finish(session="a")
        self.assert_suggested(body, "In progress", reason=True)
        self.assertNotIn("pr_url", body)
        self.assert_suggested(self.finish(session="b", gh=(1, "")), "In progress", reason=True)
        with patch.dict(os.environ, {"GROKBOT_NO_PR": "1"}):
            self.assert_suggested(self.finish(session="c"), "Done")
        with patch.dict(os.environ, {"GROKBOT_REVIEW": "1"}):
            self.assert_suggested(self.finish(session="d"), "In review")

    def test_failed_and_blocked_runs(self):
        write_record(self.home)
        body = self.finish(session="a", name="StopFailure", error="rate_limit")
        self.assert_suggested(body, "In progress", reason=True)
        self.assertIn("rate_limit", body["status_reason"])
        (self.brief.parent / "QUESTION.md").write_text("?")
        os.utime(self.brief.parent / "QUESTION.md", (4_000_000_000, 4_000_000_000))
        self.assert_suggested(self.finish(session="b"), "Blocked", reason=True)

    def test_needs_input_suggests_blocked(self):
        write_record(self.home)
        body = self.finish(name="PermissionRequest", tool_name="Bash", tool_input={"command": "x"})
        self.assertEqual(body["event"], "needs_input")
        self.assert_suggested(body, "Blocked")

    def test_enforced_run_without_ticket_is_rejected_not_finished(self):
        callback.notify("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "a", "cwd": str(self.worktree)})
        callback.notify("claude", {"hook_event_name": "Stop", "session_id": "a", "cwd": str(self.worktree)})
        self.assertEqual({item["event"] for item in self.spooled}, {"run_rejected_no_ticket"})
        body = self.body()
        self.assertEqual((body["event"], body["status"]), ("run_rejected_no_ticket", "failed"))
        self.assertIn("no ticket record", body["status_reason"])
        self.assertNotIn("suggested_status", body)

    def test_expired_ticket_at_stop_is_rejected(self):
        write_record(self.home)
        callback.notify("claude", {"hook_event_name": "UserPromptSubmit", "session_id": "a", "cwd": str(self.worktree)})
        write_record(self.home, age_hours=25)
        callback.notify("claude", {"hook_event_name": "Stop", "session_id": "a", "cwd": str(self.worktree)})
        self.assertEqual([item["event"] for item in self.spooled], ["run_rejected_no_ticket"])

    def test_codex_runs_are_not_rejected(self):
        self.assertEqual(self.finish(agent="codex")["event"], "run_finished")


class InstallTests(unittest.TestCase):
    def test_installs_guard_fail_closed_and_launcher_shim(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.object(installer.shutil, "which", return_value="/opt/tools/uv"):
                installer.install(home, ["claude-code"])
                self.assertEqual(installer.install(home, ["claude-code"])["changed"], [])
            hooks = json.loads((home / ".claude/settings.json").read_text())["hooks"]
            guards = {event: (group.get("matcher"), handler.get("onFailure"))
                      for event, groups in hooks.items() for group in groups for handler in group["hooks"]
                      if "ticket_guard.py" in handler["command"]}
            self.assertEqual(guards, {"SessionStart": (None, None), "UserPromptSubmit": (None, "block"),
                                      "PreToolUse": ("*", "block")})
            shim = home / ".local/bin/rex-run"
            self.assertTrue(os.access(shim, os.X_OK))
            self.assertIn(str(home / ".local/share/agent-tooling/rex/rex_run.py"), shim.read_text())
            self.assertTrue((home / ".local/share/agent-tooling/rex/ticket_guard.py").exists())

    def test_codex_only_install_adds_no_guard_or_shim(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            installer.install(home, ["codex"])
            self.assertNotIn("ticket_guard", (home / ".codex/hooks.json").read_text())
            self.assertFalse((home / ".local/bin").exists())


if __name__ == "__main__":
    unittest.main()
