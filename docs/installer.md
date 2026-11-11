# Installer choices and lifecycle

Run the hosted installer from the directory you want to configure:

```sh
curl -fsSL https://skills.clzmj.dev/install | sh
```

Interactive selection shows descriptions and owners, discovers individual skills
in selected packs, and ends with a preview and a default-no confirmation. Use
`--yes` for unattended execution. A dry run previews the plan without installing
skills or registering hooks; read-only upstream discovery can still occur. The
hosted bootstrap downloads and verifies its temporary payload.

| Option | Behavior |
| --- | --- |
| `--scope global` | Default; configure the selected agents for this user |
| `--scope project --project-dir "$PWD"` | Use an existing absolute project directory; skip global hooks and native plugins |
| `--method symlink` / `--method copy` | Symlink by default; use `copy` for independent copies; symlink sources must remain available |
| `--agents codex claude-code` | Select one or both agents; detected hosts are preselected |
| `--core rex python-tooling` / `--core none` | Select personal skills |
| `--core-only` | Install personal skills without upstream favorites |
| `--favorites owner/repo` | Select a favorite pack from `favorites.json` |
| `--skills owner/repo:name1,name2` | Select named skills within an upstream pack |
| `--all-favorites` | Explicitly select every configured favorite |
| `--hooks` / `--no-hooks` | Opt into or skip both Rex and Python hook integrations; off by default and independent of core selection |
| `--update` | Explicitly refresh existing ordinary skills |
| `--yes` / `-y` | Apply the selected plan without interactive confirmation |
| `--dry-run` | Preview without persistent installation changes |
| `--help` | Show installation options |

Existing ordinary skills are preserved during installation. Native plugin
inventories preserve existing pins, disabled states, and supported curated
aliases, including during an ordinary skill update. Project scope never runs
global hook installers or native plugin commands. Each personal hook installer
owns only its own configuration and preserves unrelated settings and host trust.

## Saved configuration and commands

Global settings live in `~/.config/clzmj-skills/install.json`; the global command
is `~/.local/bin/clzmj-skills`. Ensure `~/.local/bin` is on `PATH`. Project settings
live in `.clzmj-skills/install.json`; invoke
`.clzmj-skills/bin/clzmj-skills` from that project.

```sh
clzmj-skills install --yes --agents codex --core-only
clzmj-skills update --yes
clzmj-skills self-update --dry-run
clzmj-skills self-update
clzmj-skills uninstall --dry-run
```

`install` applies choices. `update` reuses saved choices and refreshes ordinary
skills. `self-update` fetches the hosted manifest and archive, verifies the
checksum and archive paths, checks the new command, and replaces the launcher
only after validation. It does not update installed skills or hook runtimes.
An already-current installer is a no-op.

## Completion, retries, and removal

The command reports completion only after its requested operations succeed.
On failure, fix the reported prerequisite or command and rerun with the same
choices. An earlier successful operation can remain installed. Use the exact
reported retry command, or rerun with `--update` to repair partially created
ordinary skill directories. An ordinary install preserves existing directories.
Verified release payloads can remain cached after a later installation failure.
A failed download, checksum check, or new-command check retains the previous
launcher.

`uninstall` compares owned skill trees against recorded fingerprints. It removes
unchanged owned skills and its managed launcher/settings. Modified or unverified
paths stay in place, and settings and launcher remain available for review.
It retains hooks, native plugins, and cached releases. Before manually removing a
personal skill's hook runtime, remove only its owned hook definitions and any
marked Rex instruction blocks; keep unrelated settings and running work.

The release archive contains installer code and the personal skills, not the
upstream favorites themselves. Those are fetched from their respective owners
when selected and retain their own licenses and host integration requirements.
