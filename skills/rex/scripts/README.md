# Rex runtime

Independent uv-managed helpers for the installed Rex CLI. [POLICY.md](POLICY.md) controls command routing; [COORDINATION.md](COORDINATION.md) controls agent visibility and scoped messaging.

Install from this skill with `uv run --script scripts/install_hooks.py` (optionally `--agents codex` or `--agents claude-code`). This installer only manages Rex hooks. The runtime runs from `~/.local/share/agent-tooling/rex`. Mutable state lives in `~/.local/state/rex-visible` (override with `REX_VISIBLE_STATE_DIR`) so skill updates preserve session bindings, receipts, and the local coordination database. Neither state nor transcripts belong in Git.

Jobs and read-only agent views use the existing main agent Rex session. Successful jobs and inactive views close after 30 seconds; failed jobs and infrastructure/deployment output close after 120 seconds. Running services remain until they exit. `--close-after -1` explicitly pins a job. Cleanup only closes owned panes and verified empty legacy tool sessions.

The hooks inspect commands and record lifecycle events. They never launch model tasks, approve tools, or invoke Computer Use. The agent runs substantial commands through `rex_visible.py run` under ordinary permissions. Codex uses native messaging or supported `codex queue` for independent sessions. Claude uses native Agent/SendMessage; its adapter records read-only lifecycle and transcript views, never Codex queues.

## Grokbot run-finished callback

`grokbot_callback.py` is copied with the rest of the runtime by this installer; it does not add a Codex or Claude hook. Configure `~/.config/agent-tooling/grokbot-callback.env` with mode `0600` and these values:

```sh
GROKBOT_WEBHOOK_URL=https://your-webhook
GROKBOT_WEBHOOK_HEADER=Authorization: Bearer your-token
```

`GROKBOT_WEBHOOK_HEADER` is the complete header line (optional surrounding quotes are accepted; no shell expansion). Use dedicated callback credentials. Keep this file private and never log its contents. Set the requester when launching Codex or Claude from Rex:

```sh
chmod 600 ~/.config/agent-tooling/grokbot-callback.env
GROKBOT_CALLBACK=dinesh codex
GROKBOT_CALLBACK=dinesh claude
uv run --script ~/.local/share/agent-tooling/rex/grokbot_callback.py doctor
uv run --script ~/.local/share/agent-tooling/rex/grokbot_callback.py test
```

Without `GROKBOT_CALLBACK`, manual sessions never post. `doctor` checks config presence, ownership and permissions; `test` sends `{\"event\":\"test\"}` and prints the HTTP status. A Stop with background tasks or scheduled wakeups stays waiting and does not notify. Claude's existing StopFailure hook reports a failed turn; SessionEnd alone does not imply completion.

On turn completion, the detached callback sends a `run_finished` event with `status` (`completed` or `failed`), `agent` (`codex` or `claude`), `requester`, `agent_session_id`, `rex_session`, `rex_block`, `cwd`, `repo` (Git root path), `branch`, `head_sha`, `last_message` (limited to 2,000 characters), and `finished_at` (ISO 8601 with offset). Git fields are empty outside a repository. Delivery uses a five-second HTTP timeout and at most two retries. Atomic claims deduplicate each session/turn, using native turn IDs when supplied or a generation recorded by UserPromptSubmit. Claims and one-line `grokbot-callback-receipts.jsonl` receipts live under the existing `REX_VISIBLE_STATE_DIR` (default `~/.local/state/rex-visible`). Failed deliveries are recorded without secrets and are not replayed by repeated Stop hooks.

`demo/cleanup_demo.py` tests real task sharing, successful and failed commands, a localhost HTTP server, and timed view cleanup. Run it through the visible runner as `uv run --script demo/cleanup_demo.py`. All standalone scripts use inline Python metadata and uv launchers. The demo's final layout comparison expects no concurrent unrelated tab changes.

Claude Code 2.1.322 support uses documented SessionStart/End, subagent, tool, task, teammate, model-switch, failure and notification hooks. Opaque Claude IDs are retained alongside provider-namespaced internal keys. The main session comes from an explicit/bound Rex session or an unambiguous Codex/Claude foreground process. Session-level subagent parent edges remain provisional until an explicit immediate-parent registration. Stop with background tasks or scheduled wakeups means waiting, not completed.

See [Claude hooks](https://code.claude.com/docs/en/hooks), [subagents](https://code.claude.com/docs/en/sub-agents), and [agent teams](https://code.claude.com/docs/en/agent-teams) for the supported lifecycle and native coordination interfaces. Hook processes never start a model, synthesize mailbox traffic, or expand tool permissions.
