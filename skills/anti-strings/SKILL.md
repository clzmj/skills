---
name: anti-strings
description: Find and review repeated string literals that may represent shared domain constants, closed values, or typo risks. Use when auditing a codebase for duplicated strings or consolidating repeated domain values; not for routine edits to ordinary copy, styles, or unrelated literals.
---

# Anti Strings

Use the scanner to surface repeated string literals, then decide from code context whether they represent the same concept. A matching spelling is a lead, not proof of shared meaning.

Default tested mappings cover Go, Rust, Python, JavaScript, TypeScript, and TSX. Other grammars may be added with `--extension .java=java` or the JSON config's `extensions`; fixture-check their literal forms before relying on them, since grammar support does not guarantee equivalent coverage.

## Workflow

1. Resolve `scripts/scan.py` relative to this skill's installed directory, then pass the target repository as an absolute path. For a global install, run `uv run --script ~/.agents/skills/anti-strings/scripts/scan.py /absolute/path/to/repo`; from this source checkout, use `uv run --script /absolute/path/to/skills/anti-strings/scripts/scan.py /absolute/path/to/repo`. Markdown is the default; use `--format json` for structured results. Add `--include-tests` when test literals are in scope. To save a report, pass `--output PATH` outside the target root.
2. Check `coverage.scanned`, `coverage.unsupported`, `coverage.excluded`, `coverage.parse_errors`, and `coverage.warnings` in JSON (or their Markdown summary). State any gaps; parse errors skip the affected file, and a clean report is not evidence about files the scanner did not parse.
3. Inspect each candidate at its reported locations and nearby definitions/usages. Group only literals with the same domain meaning; rank findings by exact file and line, impact, and confidence.
4. Recommend the narrowest owner: an existing domain module, type, or schema. Refactor only when the task asks for changes. Preserve public, serialized, API, and wire values exactly.

See [remediation guidance](references/remediation.md) for language-shaped options and cases to leave alone.

Treat scanner matches as heuristics, not semantic findings. Do not use regex autofixes or blind global replacement; edit and verify each chosen value in context.
