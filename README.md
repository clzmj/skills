# Personal agent skills

Personal agent skills for Codex and Claude Code: `python-tooling`, `rex`, and `anti-strings`, with a one-line installer and curated upstream packs. [skills.clzmj.dev](https://skills.clzmj.dev)

The hosted installer bundles `python-tooling` and `rex`; install `anti-strings` separately with Skills CLI. [Question-escalation delivery research is in progress](docs/qe/question-delivery-evidence.md), with probes in [tests/qe](tests/qe/).

| Skill            | Purpose                                                                      |
| ---------------- | ---------------------------------------------------------------------------- |
| `python-tooling` | Use uv for Python scripts, dependencies, Ruff, and ty                        |
| `rex`            | Manage Rex jobs, agent views, and completed panes                            |
| `anti-strings`   | Audit domain values for closed sets, typed records, and validated boundaries |

## Install

```sh
curl -fsSL https://skills.clzmj.dev/install | sh
```

The interactive installer guides agent, scope, method, and favorite-pack choices, then presents core and selected upstream skills on one page. Use arrow keys and Space to select; `/` filters, `a` selects all, and Enter continues. Browse project folders in the installer. The review accepts `y` to install or `n` to cancel. Defaults are global scope, symlinked core skills, detected agents, and no hooks or favorites. `--method symlink` is the default; use `--method copy` for independent copies. Symlink sources must remain available. Project scope skips global hooks and native plugins.

For unattended installation, pass explicit choices with `--yes`:

```sh
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --yes --agents codex --core-only
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --yes --scope project --project-dir "$PWD" --core-only
curl -fsSL https://skills.clzmj.dev/install | sh -s -- --dry-run --yes --core-only
```

Use `--favorites owner/repo` to select packs or `--skills owner/repo:name1,name2` to select skills. `--hooks` enables both Rex and Python hook integrations; `--no-hooks` skips them. Hooks are off by default and independent of core-skill selection. The installer does not approve hooks or expand tool permissions. Source checkouts can run `bun run init` with the same options.

The bootstrap verifies a SHA-256 checksum and archive paths before writing. Hosted public release archives contain the two core skills, installer modules, favorites manifest, and license. They exclude Git history, infrastructure, CI files, secrets, and Terraform state. See [installer details](docs/installer.md) for all flags, retry behavior, and removal.

| Optional favorite    | Upstream                                                         | Installation                |
| -------------------- | ---------------------------------------------------------------- | --------------------------- |
| ADHD                 | [Udit Akhouri](https://github.com/UditAkhourii/adhd)             | Skills CLI                  |
| Security audit       | [Cloudflare](https://github.com/cloudflare/security-audit-skill) | Skills CLI                  |
| Elephant & Goldfish  | [Google](https://github.com/google/elephant-goldfish-model)      | Complete native plugin      |
| Ponytail             | [DietrichGebert](https://github.com/DietrichGebert/ponytail)     | Complete native plugin      |
| Matt Pocock's skills | [Matt Pocock](https://github.com/mattpocock/skills)              | Select skills from the pack |

Requirements vary by selection: Bun/bunx, Git, uv for hooks, and agent CLIs for native plugins. Rex workflows also need the local Rex CLI. Skills CLI runs through `bunx --bun`; upstream skills may document Node commands. Git and agent authentication use your existing configuration.

## Install individual skills

```sh
bunx --bun skills add clzmj/skills --skill python-tooling -g -a codex claude-code -y
bunx --bun skills add clzmj/skills --skill rex -g -a codex claude-code -y
bunx --bun skills add clzmj/skills --skill anti-strings -g -a codex claude-code -y
```

Only Rex and Python tooling provide hook installers:

```sh
uv run --script ~/.agents/skills/python-tooling/scripts/install_hooks.py
uv run --script ~/.agents/skills/rex/scripts/install_hooks.py
```

Each installer supports `--agents codex` or `--agents claude-code`, backs up changes, and preserves unrelated settings. Neither installs the other skill. Python 3.11+ is selected by uv. Python hooks add project context and guard pip mutations in uv projects; they run no checks or package installs. Set `PYTHON_TOOLING_DISABLED=1` in the hook environment to permit an explicitly requested exception. This guard is not a shell security boundary.

Rex jobs and agent views reuse the existing main session and close completed owned panes under their retention rules. Runtime files are under `~/.local/share/agent-tooling`; mutable Rex state is under `~/.local/state/rex-visible`.

Claude Code retains its native `Agent` and `SendMessage` workflow. Rex records Claude session, child-agent, task, and background activity in read-only views; it does not send Claude messages or change workspace trust. Availability follows the installed Claude CLI version.

## Update and remove

Global installs provide `~/.local/bin/clzmj-skills`; add `~/.local/bin` to `PATH`. Project installs provide `.clzmj-skills/bin/clzmj-skills`.

```sh
clzmj-skills update --yes
clzmj-skills self-update
clzmj-skills uninstall --dry-run
clzmj-skills uninstall
```

`update` reuses saved selections and updates ordinary skills. `self-update` replaces the installer with a verified release; it leaves installed skills, hooks, native plugins, and saved choices unchanged. Uninstall removes unchanged owned skills and its launcher/settings; modified paths are preserved. Hooks, native plugins, and cached releases remain. Remove owned hooks and Rex instruction blocks separately.

For individual Skills CLI installs:

```sh
bunx --bun skills update python-tooling rex -g
bunx --bun skills remove NAME -g -y
```

Remove the corresponding owned hooks before removing an individual skill.

## Check

```sh
bun run test
uv sync --locked
uv run --locked ruff check skills/anti-strings/scripts/scan.py tests/test_anti_strings.py
uv run --locked ty check
uv run --locked python -m unittest discover -s tests -p test_anti_strings.py
```

`bun run test` runs the Python checks through uv and the Bun tests. The focused commands above check anti-strings; its scanner keeps inline dependencies, while the development environment supplies ty's parser dependencies. Unit tests require no network or Rex server. Route substantial commands through the configured visible runner.

## Hosting and cost controls

A Cloudflare Worker serves an allowlist from a private R2 bucket. Terraform owns the bucket and custom domain; Wrangler owns the Worker script and binding. Doppler supplies scoped GitHub environment secrets at runtime. See [deployment](docs/deployment.md) and [infrastructure](infra/README.md).

The Worker rejects unsupported routes, methods, and query strings before storage access, rate-limits anonymous IPs to 120 requests per minute, and caches releases at the edge. Rate limits are approximate per Cloudflare location; shared-IP clients share an allowance. These controls reduce usage and abuse but do not cap total spending. No paid plan upgrade is part of deployment.
