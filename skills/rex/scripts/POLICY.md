# Visible terminal work

Run important local commands in the existing main Codex Rex session without waiting for a reminder. Reuse its Checks, Services, and Operations tabs; never create task or team Rex sessions for command visibility. This applies to servers, log followers, infrastructure plans and changes, deployments, builds, test suites, installs, migrations, and other substantial jobs. Keep quick searches, file reads, status checks, and edits in the ordinary agent tools.

Before the first important command, read the installed Rex skill. Resolve the main session from `REX_SESSION`, a stored thread-to-session binding, or the unique Rex session with a foreground Codex process. If resolution is ambiguous, stop and report the ambiguity. Choose a stable, descriptive task slug and run:

```sh
uv run --script ~/.local/share/agent-tooling/rex/rex_visible.py run --task project-task --kind test --cwd /absolute/project --command 'cargo test'
```

Use `--detach` for servers and log followers. Supported kinds are `server`, `logs`, `infra`, `deploy`, `test`, `build`, `install`, and `job`. The runner returns the session, pane, and status-file IDs, streams output for finite commands, and returns their actual exit code. A detached launch confirms creation only: capture the returned pane and check readiness separately. Read a detached job's status file for its exit code when it finishes.

The runner uses the main session's Services/Checks/Operations tabs and splits a related owned tab into at most two panes. Preserve focus and unrelated sessions. Use the Rex CLI directly when the work needs a different layout, an interactive program, explicitly supplied environment values, or another specific existing session. Save IDs and verify the resulting layout. Commands run in the Rex server's local login-shell environment and supplied working directory; pass required environment settings explicitly, avoiding secrets in labels or unnecessary output.

Completed owned panes close automatically: successful finite checks after 30 seconds; failures and infrastructure/deployment commands after 120 seconds. Running servers and log followers remain until they exit, then close after the same respective delay. Use `--close-after SECONDS` to override; `--close-after -1` pins the pane open. Cleanup closes only explicitly owned panes and preserves unrelated work. The runner leaves failures visible during their retention window even if the agent finishes its turn.

The default destination is the local Unix-socket Rex server, even if an inherited Rex destination is remote. When the user explicitly requests another machine, use the Rex skill to target that machine and verify its identity and working directory. The local runner does not support remote execution. Explicit machine requests take precedence.

Keep the ordinary sandbox and approval flow. If Rex socket access is denied, request the appropriate tool escalation for the concrete command; report a blocked important command instead of silently running it elsewhere. Hooks only inspect and reject direct important shell calls; they never launch a command or grant approval. Treat them as a guardrail: custom scripts and specialized tool paths still require agent judgment. If creation times out, inspect sessions and returned IDs before retrying, especially for mutations.
