# Personal agent tooling

One Python tooling skill, its deterministic hooks, and the optional Rex runtime. Replaces the separate uv, Ruff LSP, ty LSP, and retired personal workflow plugins.

## Install

Install the skill with the [Skills CLI](https://github.com/vercel-labs/skills):

```sh
bunx skills add clzmj/agent-tooling --skill python-tooling -g -a codex claude-code -y
```

For a private repository with SSH authentication, use `git@github.com:clzmj/agent-tooling.git` instead of `clzmj/agent-tooling`.

Skills CLI installs skill files. Install the bundled hooks separately:

```sh
python3 ~/.agents/skills/python-tooling/scripts/install_hooks.py
```

To include the existing local Rex integration:

```sh
python3 ~/.agents/skills/python-tooling/scripts/install_hooks.py --with-rex
```

Review new or changed definitions in the host's `/hooks` menu, then start/resume a session. The installer backs up changed configuration, preserves unrelated hooks/settings, and never changes trust or tool permissions. `--agents codex` or `--agents claude-code` limits the install. Python 3.11+ is required; Rex additionally needs the local Rex CLI.

## Behavior

- The skill preserves existing project choices, prefers uv for new projects, and uses configured Ruff and ty checks.
- SessionStart hooks add relevant Python project context. PreToolUse hooks guard direct pip mutations in projects with `pyproject.toml` plus `uv.lock`; they leave read-only commands and other projects alone. `PYTHON_TOOLING_DISABLED=1` in the hook environment allows an explicitly requested exceptional workflow.
- Hooks run no builds, linters, type checkers, package installs, or model tasks. The pip recognizer is a convenience guard for common command forms, not a shell security boundary.
- Quality checks run through the agent's normal terminal workflow. The optional Rex integration reuses the main session, limits owned tabs to two panes, and closes completed jobs and views. It has no Computer Use dependency.
- Hook runtimes live in `~/.local/share/agent-tooling`; mutable Rex state lives in `~/.local/state/rex-visible`, outside the skill directory and Git.

## Update and remove

Run `bunx skills update python-tooling -g`, then rerun the bundled hook installer to update runtime files. Hook trust remains controlled by the host.

To remove Python hooks, remove only handlers pointing to `~/.local/share/agent-tooling/python_tooling.py` from `~/.codex/hooks.json` and/or `~/.claude/settings.json`, then run `bunx skills remove python-tooling -g -y`. To remove Rex, also remove its owned `rex_visible.py hook`/`codex_sessions.py hook` handlers and marked instruction blocks. Preserve running work and unrelated hooks.

## Check

```sh
bun run test
```

The package script runs Python's standard-library tests. From an agent session, route that command through the configured visible runner. No network or Rex server is required for unit tests.
