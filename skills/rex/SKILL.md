---
name: rex
description: Manage Rex sessions, tabs, panes and commands, and keep substantial terminal work and agent coordination visible in the existing main Codex Rex session.
---

Run `rex __internal skill` first and follow the guide printed by the installed
Rex CLI. If unavailable, start with `rex whoami --json`, `rex --help`, and
`rex COMMAND --help`.

For visible command routing, read [scripts/POLICY.md](scripts/POLICY.md). For
agent delegation, session ownership, and scoped messaging, read
[scripts/COORDINATION.md](scripts/COORDINATION.md). Reuse the existing main Codex
Rex session; never create task or team sessions for visibility. Preserve normal
permissions and explicitly requested machines.

Install the independent hooks from this skill's directory:

```sh
uv run --script scripts/install_hooks.py
```

Use `--agents codex` or `--agents claude-code` to select one host. The installer
copies this skill's runtime to `~/.local/share/agent-tooling/rex`, preserves
unrelated hooks and settings, and leaves Python tooling hooks unchanged. Review
changed hooks in the host's `/hooks` menu. See [scripts/README.md](scripts/README.md)
for runtime state, retention, and verification details.
