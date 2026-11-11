# Rex runtime

Independent uv-managed helpers for the installed Rex CLI. [POLICY.md](POLICY.md) controls command routing; [COORDINATION.md](COORDINATION.md) controls agent visibility and scoped messaging.

Install from this skill with `uv run --script scripts/install_hooks.py` (optionally `--agents codex` or `--agents claude-code`). This installer only manages Rex hooks. The runtime runs from `~/.local/share/agent-tooling/rex`. Mutable state lives in `~/.local/state/rex-visible` (override with `REX_VISIBLE_STATE_DIR`) so skill updates preserve session bindings, receipts, and the local coordination database. Neither state nor transcripts belong in Git.

Jobs and read-only agent views use the existing main agent Rex session. Successful jobs and inactive views close after 30 seconds; failed jobs and infrastructure/deployment output close after 120 seconds. Running services remain until they exit. `--close-after -1` explicitly pins a job. Cleanup only closes owned panes and verified empty legacy tool sessions.

The hooks inspect commands and record lifecycle events. They never launch model tasks, approve tools, or invoke Computer Use. The agent runs substantial commands through `rex_visible.py run` under ordinary permissions. Codex uses native messaging or supported `codex queue` for independent sessions. Claude uses native Agent/SendMessage; its adapter records read-only lifecycle and transcript views, never Codex queues.

`demo/cleanup_demo.py` tests real task sharing, successful and failed commands, a localhost HTTP server, and timed view cleanup. Run it through the visible runner as `uv run --script demo/cleanup_demo.py`. All standalone scripts use inline Python metadata and uv launchers. The demo's final layout comparison expects no concurrent unrelated tab changes.

Claude Code 2.1.322 support uses documented SessionStart/End, subagent, tool, task, teammate, model-switch, failure and notification hooks. Opaque Claude IDs are retained alongside provider-namespaced internal keys. The main session comes from an explicit/bound Rex session or an unambiguous Codex/Claude foreground process. Session-level subagent parent edges remain provisional until an explicit immediate-parent registration. Stop with background tasks or scheduled wakeups means waiting, not completed.

See [Claude hooks](https://code.claude.com/docs/en/hooks), [subagents](https://code.claude.com/docs/en/sub-agents), and [agent teams](https://code.claude.com/docs/en/agent-teams) for the supported lifecycle and native coordination interfaces. Hook processes never start a model, synthesize mailbox traffic, or expand tool permissions.
