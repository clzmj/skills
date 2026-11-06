# Rex runtime

Optional Python 3 helpers for the installed Rex CLI. [POLICY.md](POLICY.md) controls command routing; [COORDINATION.md](COORDINATION.md) controls agent visibility and scoped messaging.

Install through the parent skill's `scripts/install_hooks.py --with-rex`. The runtime runs from `~/.local/share/agent-tooling/rex`. Mutable state lives in `~/.local/state/rex-visible` (override with `REX_VISIBLE_STATE_DIR`) so skill updates preserve session bindings, receipts, and the local coordination database. Neither state nor transcripts belong in Git.

Jobs and read-only agent views use the existing main Codex Rex session. Successful jobs and inactive views close after 30 seconds; failed jobs and infrastructure/deployment output close after 120 seconds. Running services remain until they exit. `--close-after -1` explicitly pins a job. Cleanup only closes owned panes and verified empty legacy tool sessions.

The hooks inspect commands and record lifecycle events. They never launch model tasks, approve tools, or invoke Computer Use. The agent runs substantial commands through `rex_visible.py run` under ordinary permissions. Native messaging remains native; independent-session messaging uses supported `codex queue` calls.

`demo/cleanup_demo.py` tests real task sharing, successful and failed commands, a localhost HTTP server, and timed view cleanup. Run it through the visible runner. The demo's final layout comparison expects no concurrent unrelated tab changes.
