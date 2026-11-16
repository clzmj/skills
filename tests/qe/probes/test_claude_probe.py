"""Boundary checks for the additive live Claude recorder."""

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from tests.qe.probes.claude_probe import HookMode, hook, redact


class ClaudeRecorderTests(unittest.TestCase):
    def test_webhook_rejects_userinfo_disguised_as_loopback(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "hook.jsonl"
            output.with_suffix(".webhook").write_text(
                "http://127.0.0.1:80@outside.invalid/hooks"
            )
            with patch("sys.stdin", StringIO('{"hook_event_name":"Stop"}')):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    hook(HookMode.OBSERVE, output)

    def test_nested_redaction(self) -> None:
        self.assertEqual(
            redact(
                {
                    "agent_id": "private",
                    "url": "https://user:password@example.com",
                    "path": "agent-a123456789abcdef.jsonl",
                    "token": "ghp_abcdefghijklmnopqrst",
                }
            ),
            {
                "agent_id": "<redacted-agent-id>",
                "url": "https://<redacted-userinfo>@example.com",
                "path": "agent-<redacted-agent-id>.jsonl",
                "token": "<redacted-token>",
            },
        )

    def test_webhook_preserves_hook_stdout_and_posts_redacted_payload(self) -> None:
        received: list[object] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        with (
            TemporaryDirectory() as directory,
            HTTPServer(("127.0.0.1", 0), Handler) as server,
        ):
            thread = Thread(target=server.handle_request)
            thread.start()
            output = Path(directory) / "hook.jsonl"
            output.with_suffix(".webhook").write_text(
                f"http://127.0.0.1:{server.server_port}/hooks"
            )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "indent?"}]},
                "agent_id": "private-agent",
            }
            stdout = StringIO()
            with (
                patch("sys.stdin", StringIO(json.dumps(payload))),
                redirect_stdout(stdout),
            ):
                hook(HookMode.DENY_ANSWER, output)
            thread.join(timeout=3)
            self.assertEqual(
                json.loads(stdout.getvalue())["hookSpecificOutput"][
                    "permissionDecision"
                ],
                "deny",
            )
            self.assertEqual(len(received), 1)
            self.assertEqual(
                json.loads(output.read_text())["payload"]["agent_id"],
                "<redacted-agent-id>",
            )


if __name__ == "__main__":
    unittest.main()
