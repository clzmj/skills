---
name: anti-strings
description: Audit code for repeated or closed string values and untyped records that should become closed sets or typed records; use this rubric for every coding brief or review in Codex or Claude. Not for ordinary copy or mechanical replacement.
---

# Anti Strings

Review domain values as typed values, not scattered strings or ad hoc dictionaries. Flag repeated domain literals, finite choices represented by strings, string-key field access such as `d["status"]` or `obj["kind"]`, and untyped dict/map/object records. A dictionary with genuinely dynamic runtime keys is appropriate.

The scanner has fixture-tested mappings for Python, JavaScript, TypeScript/TSX, Go, Rust, Java, Kotlin, C#, Swift, Ruby, PHP, Elixir, and SQL. Repeated-key detection supports only Python, JavaScript, TypeScript/TSX, Go, Ruby, and PHP, and groups accesses per file. It cannot infer receiver types or prove a record is untyped. Review boundary parsing and record shapes manually, even when the scan is clean. Unfamiliar literal forms are skipped; SQL coverage is grammar/dialect dependent. Custom mappings use `--extension .ext=grammar` or JSON config `extensions` and may name any grammar in the pinned parser pack, including grammars outside the built-in language enum. Missing grammars use `parser_unavailable`. Custom grammars retain their previous behavior, but successful parsing alone does not prove literal coverage: fixture-check the forms you rely on.

The scanner reports zero findings at the default `--min-occurrences 3` when scanning its own source. A stricter `--min-occurrences 2` scan can report real repeated implementation values; the zero-findings claim does not cover that threshold.

Known coverage limits: PostgreSQL dollar-quoted bodies (`$$...$$` and `$tag$...$tag$`) are outside verified literal coverage. The pinned SQL grammar rejects forms such as `DO $$ BEGIN ... END $$;`; those files appear in `coverage.parse_errors` and are skipped entirely. Other forms can parse, but some forms parse without yielding literals (for example, `SELECT $$queued$$`), so a successful parse does not establish dollar-quote extraction support. No dialect preprocessor or additional parser is applied.

The repeated-key heuristic excludes syntactic `os.environ`, `sys.modules`, and `process.env` receivers, but does not resolve imported bindings or aliases. Thus `from os import environ` followed by repeated `environ["KEY"]` accesses remains a possible false positive: the name can also be rebound to an ordinary record. Review those receivers manually before recommending typed records. The import statement itself is not a repeated-key finding.

## Repo-local measurement

At `clzmj/skills` commit `7b66a95`, a default scan (tests excluded, threshold 3) yielded 38 repeated-key groups. Sort by `(first location path, value)`, then select 20 with `random.Random(0).sample(pool, 20)`. One agent judged every access in each selected group: **4/20 groups (20%) were false positives**, all four in `skills/rex/scripts/grokbot_callback.py` on already-typed state/view fields. The lenient rule counted a mixed typed/untyped group as a true positive whenever any access used an untyped fixed record; that can hide false positives within mixed groups. This is a single-judge, repo-local sample, not a general accuracy estimate.

For a second check outside that file, the same judge sampled five previously unselected `codex_sessions.py` groups with seed 1: `mode`, `parent`, `parent_source`, `session`, `thread`. All used untyped coordination/view records: **0/5 false positives**. This small, deliberately file-specific check is separate from the original 4/20 result, not independent validation.

## Audit mode

1. Resolve `scripts/scan.py` relative to this skill's installed directory, then pass the target repository as an absolute path. For a global install, run `uv run --script ~/.agents/skills/anti-strings/scripts/scan.py /absolute/path/to/repo`; from this source checkout, use `uv run --script /absolute/path/to/skills/anti-strings/scripts/scan.py /absolute/path/to/repo`. Markdown is the default; use `--format json` for structured results. Add `--include-tests` when test literals are in scope. To save a report, pass `--output PATH` outside the target root.
2. Check `coverage.scanned`, `coverage.unsupported`, `coverage.excluded`, `coverage.parse_errors`, and `coverage.warnings` in JSON (or their Markdown summary). State any gaps; parse errors skip the affected file, and a clean report is not evidence about files the scanner did not parse.
3. Inspect candidates at their reported locations and nearby definitions/usages. Group only values with the same domain meaning; rank findings by exact file and line, impact, and confidence. Report findings and recommendations without editing.

## Brief and review mode

For every coding brief or review in Codex or Claude, apply this rubric to every affected value:

> Give repeated domain values typed constants, model finite choices as exhaustive closed sets, and model structured data as typed records (structs/classes); parse and validate JSON, environment, CLI, HTTP, and database input once at its boundary, then use typed models inward; preserve exact public, serialized, API, and wire values; leave ordinary UI copy, import specifiers, independent test assertions, and genuinely dynamic-key collections in place.

Recommend the narrowest existing type, module, or schema owner. Read [remediation guidance](references/remediation.md) for language idioms and caveats before recommending a representation.

Treat scanner matches as heuristics, not semantic findings. Do not use regex autofixes or blind global replacement; verify each chosen value in context. State scanner coverage gaps and do not infer unsupported grammar coverage from added extensions.
