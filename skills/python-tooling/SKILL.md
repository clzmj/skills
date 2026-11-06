---
name: python-tooling
description: Manage Python project environments and dependencies, and run Ruff formatting and linting or ty type checks when configured. Use when creating or changing a Python project, installing dependencies, or resolving Python tooling issues.
---

# Python tooling

Inspect the project’s existing packaging and tool configuration before changing it. Preserve its established workflow; do not migrate a project or replace its dependency manager as an incidental cleanup.

## New projects and dependencies

For a new project, prefer `uv` with `pyproject.toml` and a committed `uv.lock`. Use `uv init`, add runtime or development dependencies with `uv add` (use `uv add --dev` for development tools), then sync and run project commands with `uv run`. Keep the Python version consistent with the project’s declared support and pin it only when the project needs a specific interpreter.

For an existing uv project, treat `pyproject.toml` as dependency intent and `uv.lock` as the resolved environment. Make dependency changes through `uv add` or `uv remove` and include the resulting lockfile change. Use `uv sync --locked` when installing an existing lock without allowing it to change; use `uv lock --check` to confirm it matches the project metadata. Upgrade dependencies only when requested or needed to resolve the task, and keep upgrades as narrow as practical.

For projects using another manager or legacy files, use the existing manager and its lockfile. If migration is requested, first inspect the current dependency groups, Python constraints, scripts, and CI/install paths; propose or make a scoped migration that preserves them. Keep compatibility exports such as `requirements.txt` only when a consumer needs them.

## Code quality checks

When Ruff is configured or installed in the project, use its project configuration. Run `ruff format` for formatting and `ruff check` for lint diagnostics, typically through the project environment (`uv run ruff format …`, `uv run ruff check …`). Apply safe automatic lint fixes only when they are within the requested scope; review the diff and resolve remaining diagnostics deliberately. Keep lint rules and format settings in the project’s existing `ruff.toml` or `pyproject.toml` configuration.

When ty is configured or installed in the project, use `ty check` through that environment (for example, `uv run ty check`). Check that the project environment and declared dependencies are available before interpreting unresolved imports as source errors. Fix type issues in the relevant code and retain the project’s existing ty settings.

Use configured project scripts or CI commands when they define the canonical checks. Do not download tools on an automatic hook’s behalf; use tools already available in the project or explain what must be installed. For persistent tool use, add Ruff or ty to the project’s development dependencies rather than relying on an ephemeral runner.

## LSP reference

Ruff and ty can provide Python editor diagnostics through their language servers when the editor supports them. Configure or troubleshoot an LSP only when requested; ordinary code work can use the project’s command-line checks. Do not install both as competing servers when the editor permits only one server per file extension. If LSP setup is needed, use the editor’s documented server configuration and the project’s installed tool versions.

## Repository hooks

The bundled `scripts/install_hooks.py` installs deterministic Python hooks for Codex and Claude Code. Run it only when setting up or updating hooks, then review new definitions in the host's `/hooks` menu. Add `--with-rex` to also install the optional local Rex integration. The hooks add project context and guard pip mutations in uv projects; they run no checks, install no packages, and grant no permissions. Use `PYTHON_TOOLING_DISABLED=1` in the hook environment for an explicitly requested exceptional workflow. Perform relevant code checks through the user's normal visible terminal workflow.
