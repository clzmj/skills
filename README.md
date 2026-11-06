# Skills

Two independent personal skills, plus a bootstrap for favorite upstream packs.

| Skill | Purpose |
| --- | --- |
| `python-tooling` | uv for all Python execution, standalone scripts, dependencies, Ruff, and ty |
| `rex` | Rex CLI use, visible jobs, agent coordination, and automatic pane cleanup |

Both are self-contained and installable on their own. Python scripts use uv shebangs and inline PEP 723 metadata; entry points run with `uv run --script`.

## Initialize a machine

Clone this private repository with your GitHub credentials, then run:

```sh
gh repo clone clzmj/skills
cd skills
bun run init
```

`init` installs both personal skills, registers their independent hooks, and installs the favorites in [favorites.json](favorites.json). Edit that file to change the list.

| Favorite | Upstream owner | Installation |
| --- | --- | --- |
| ADHD | [Udit Akhouri](https://github.com/UditAkhourii/adhd) | Skills CLI |
| Elephant & Goldfish | [Google](https://github.com/google/elephant-goldfish-model) | Complete native plugin |
| Ponytail | [DietrichGebert](https://github.com/DietrichGebert/ponytail) | Complete native plugin |
| Matt Pocock's skills | [Matt Pocock](https://github.com/mattpocock/skills) | Full skill pack |

EGM and Ponytail need their complete plugin resources and integration. Existing native plugins, disabled states, and pinned versions are preserved; Ponytail's known curated Codex alias also counts as installed. Ordinary skill entries are refreshed from their upstream source. Plannotator is excluded for now.

Requirements: Bun/bunx, uv, Git, the selected agent CLIs, and Node for Ponytail. Rex needs the installed local Rex CLI. Authentication is handled by your existing Git/agent configuration. Init validates its manifest and prerequisites, reads host inventories before modifying anything, and stops on command failure.

```sh
bun run init --dry-run
bun run init --agents codex
bun run init --no-hooks
```

The dry run makes no subprocess calls or writes; its native plugin commands are conditional because it does not inspect host state. `--agents` selects Codex and/or Claude Code (both by default). `--no-hooks` skips the two personal hook installers; native plugins retain their own packaged behavior.

Review new or changed hooks in the host's `/hooks` menu, then start/resume a session. Init never approves hooks or expands tool permissions.

## Install individual skills

```sh
bunx skills add clzmj/skills --skill python-tooling -g -a codex claude-code -y
bunx skills add clzmj/skills --skill rex -g -a codex claude-code -y
```

For private-repository SSH authentication, use `git@github.com:clzmj/skills.git` as the source. Skills CLI installs the files; each skill registers only its own hooks:

```sh
uv run --script ~/.agents/skills/python-tooling/scripts/install_hooks.py
uv run --script ~/.agents/skills/rex/scripts/install_hooks.py
```

Each installer supports `--agents codex` or `--agents claude-code`, backs up changed configuration, and preserves unrelated hooks/settings and host trust. Neither skill installs the other. Python 3.11+ is selected by uv.

Python hooks add project context and guard pip mutations in projects with both `pyproject.toml` and `uv.lock`. They run no checks or package installs. `PYTHON_TOOLING_DISABLED=1` in the hook environment allows an explicitly requested exceptional workflow. The recognizer is a convenience guard, not a shell security boundary.

Rex jobs and agent views reuse the existing main session, close completed owned panes under their retention rules, and have no Computer Use dependency. Runtime files remain under `~/.local/share/agent-tooling`; mutable Rex state stays in `~/.local/state/rex-visible` so skill updates preserve it.

## Update and remove

Run `bunx skills update python-tooling rex -g`, then rerun the relevant hook installers. Existing legacy runtime locations remain compatible. Remove only the corresponding owned hook definitions before `bunx skills remove NAME -g -y`. Removing Rex also requires removing its marked instruction blocks; preserve running and unrelated work.

## Check

```sh
bun run test
```

This runs standard-library Python tests through uv and Bun tests for the bootstrap. Unit tests require no network or Rex server. Agents should route substantial commands through their configured visible runner.
