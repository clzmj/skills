# Skills

Three independently installable personal skills, plus a bootstrap for favorite upstream packs. The hosted installer currently bundles the two core skills; `anti-strings` is installed separately with Skills CLI.

| Skill | Purpose |
| --- | --- |
| `python-tooling` | uv for all Python execution, standalone scripts, dependencies, Ruff, and ty |
| `rex` | Rex CLI use, visible jobs, agent coordination, and automatic pane cleanup |
| `anti-strings` | Find repeated literals and review shared domain values for type-safe reuse |

All three are self-contained and installable on their own. Python scripts use uv shebangs and inline PEP 723 metadata; entry points run with `uv run --script`.

## Install

```sh
curl -fsSL https://skills.clzmj.dev/install | sh
```

The installer guides you through agents, scope, project folder, installation
method, and favorite packs, then shows **one Individual skills page** containing
the core skills and every selected ordinary source. Each row shows `repo/skill`
(for example, `mattpocock/skills/ask-matt`),
with the source and skill name in distinct colors. Use the arrow
keys to move, Space to toggle, `a` to select all, and Enter to continue. Browse
project folders without typing paths. `/` filters the same skill page while
preserving hidden selections; `a` and `n` affect the whole catalog. Esc clears a
filter first, then goes back and keeps completed choices.
The review page accepts `y` to install or `n` to cancel; installation shows a
progress bar, completed steps, and elapsed time. Defaults are global scope, symlinked core skills,
detected agents, and no hooks or upstream favorites. A project install keeps its
files in the selected project and skips global hooks and native plugins. Symlink
installation is the default; use `--method copy` for independent copies. Symlink
sources must remain available. Existing installs keep their saved method when
updated; changing it requires an explicit `--method` choice.
Symlinked skills share `.agents/skills` within the selected scope; Claude Code
links there. Selecting Claude alone does not change Codex settings.

For unattended installation, use explicit choices and `--yes`:

```sh
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --yes --agents codex --core-only
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --yes --scope project --project-dir "$PWD" --core-only
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --dry-run --yes --core-only
```

`--method symlink` is the default; choose `--method copy` for copies. Existing
skills are preserved unless you request an update. Hooks are independently
optional: `--hooks` enables both the Rex and Python hook integrations,
independently of the core skills selected;
`--no-hooks` skips them. Review new hooks in each host's `/hooks` menu. The
installer never approves hooks or expands tool permissions.

The bootstrap verifies a SHA-256 checksum and validates archive paths before
writing its payload. Hosted public release archives contain only the two core skills, installer
modules, favorites manifest, and license. Git history, infrastructure, CI files,
secrets, and Terraform state are excluded. Verified global releases live under
`~/.local/share/clzmj-skills/releases/VERSION`; project releases stay under
`.clzmj-skills/releases/VERSION`. See [installer details](docs/installer.md) for
selection flags, retry behavior, and removal.

| Optional favorite | Upstream owner | Installation |
| --- | --- | --- |
| ADHD | [Udit Akhouri](https://github.com/UditAkhourii/adhd) | Skills CLI |
| Security audit | [Cloudflare](https://github.com/cloudflare/security-audit-skill) | Skills CLI |
| Elephant & Goldfish | [Google](https://github.com/google/elephant-goldfish-model) | Complete native plugin |
| Ponytail | [DietrichGebert](https://github.com/DietrichGebert/ponytail) | Complete native plugin |
| Matt Pocock's skills | [Matt Pocock](https://github.com/mattpocock/skills) | Select skills from the pack |

Choose favorites interactively or with `--favorites owner/repo`; use
`--skills owner/repo:name1,name2` to select individual skills. Wildcard packs are
discovered before selection so you can inspect their available skills. Complete
native plugins retain their resources and integration. Existing plugins,
disabled states, pinned versions, and supported curated aliases are preserved.
Edit [favorites.json](favorites.json) to change the available packs.

Requirements depend on your choices: Bun/bunx, Git, uv for hooks, and the selected
agent CLIs for native plugins. Rex workflows also need the installed local Rex
CLI. Skills CLI runs through `bunx --bun`; upstream skills may document Node
commands for their own workflows. Git and agent authentication use your existing
configuration. Source checkouts can run `bun run init` with the same options.

## Install individual skills

```sh
bunx --bun skills add clzmj/skills --skill python-tooling -g -a codex claude-code -y
bunx --bun skills add clzmj/skills --skill rex -g -a codex claude-code -y
bunx --bun skills add clzmj/skills --skill anti-strings -g -a codex claude-code -y
```

Skills CLI installs the files. Only Rex and Python tooling provide hook installers:

```sh
uv run --script ~/.agents/skills/python-tooling/scripts/install_hooks.py
uv run --script ~/.agents/skills/rex/scripts/install_hooks.py
```

Each installer supports `--agents codex` or `--agents claude-code`, backs up changed configuration, and preserves unrelated hooks/settings and host trust. Neither skill installs the other. Python 3.11+ is selected by uv.

Python hooks add project context and guard pip mutations in projects with both `pyproject.toml` and `uv.lock`. They run no checks or package installs. `PYTHON_TOOLING_DISABLED=1` in the hook environment allows an explicitly requested exceptional workflow. The recognizer is a convenience guard, not a shell security boundary.

Rex jobs and agent views reuse the existing main session, close completed owned panes under their retention rules, and have no Computer Use dependency. Runtime files remain under `~/.local/share/agent-tooling`; mutable Rex state stays in `~/.local/state/rex-visible` so skill updates preserve it.

Claude Code keeps its native `Agent` and `SendMessage` workflow. Rex records
Claude session, child-agent, task, and background activity in read-only views;
it does not send Claude messages, approve tools, or change workspace trust.
Native children share their parent's Claude session ID and have separate opaque
agent IDs. Independent `claude --bg` sessions have their own session IDs; use
`claude agents --json --all` and Claude's supported attach/logs/stop commands to
manage them. Availability follows the installed Claude CLI version.

## Update and remove

Global installs provide `~/.local/bin/clzmj-skills`; add `~/.local/bin` to your
`PATH`. Project installs provide `.clzmj-skills/bin/clzmj-skills`.

```sh
clzmj-skills update --yes
clzmj-skills self-update
clzmj-skills uninstall --dry-run
clzmj-skills uninstall
```

`update` reuses saved selections and explicitly updates ordinary skills.
`self-update` replaces the installer with a verified release; installed skills,
hooks, native plugins, and saved choices stay as they are. Native plugin versions
and disabled states remain under the host's control.

Uninstall removes only owned skills whose recorded contents are unchanged,
plus its managed launcher and settings. Modified paths are preserved for review.
Hooks, native plugins, and cached releases remain; remove the corresponding
owned hooks and Rex instruction blocks separately while preserving unrelated
configuration and running work.

Individual Skills CLI installations can instead use
`bunx --bun skills update python-tooling rex -g`, followed by their selected hook
installers. Remove their owned hooks before `bunx --bun skills remove NAME -g -y`.

## Check

```sh
bun run test
```

This runs standard-library Python tests through uv and Bun tests for the bootstrap. Unit tests require no network or Rex server. Agents should route substantial commands through their configured visible runner.

## Hosting and cost controls

A Cloudflare Worker serves an allowlist from a private R2 bucket. Terraform owns the bucket and custom domain; Wrangler owns the code and bindings. Doppler feeds scoped GitHub environments. See [deployment](docs/deployment.md) and [infrastructure](infra/README.md).

The service rejects unsupported routes, methods, and query strings before storage access, uses native rate limiting (120 requests/minute per anonymous IP), and caches releases at the edge. Native limits are approximate and per Cloudflare location; shared-IP clients share an allowance. These controls reduce usage and abuse but are not a global spending cap. No paid plan upgrade is part of deployment.
