# Recorded PR-0 hook fixtures

Captured on 2026-11-16 with the versions and commands in
`docs/qe/question-delivery-evidence.md`. These are recorded real CLI events,
not synthesized question events. Recorder wrappers contain a `payload`; Claude
also records actual `response` and elapsed seconds. Codex hook denials do not
produce a PostToolUse record, and neither child produced a question event.

Credentials, URL userinfo, and native child IDs are redacted. Claude session IDs
are replaced with consistent `test-identity-NNN` labels; Codex UUIDs were masked
at capture. Call IDs are retained for binding. An opaque Codex collaborator
message is omitted while its tool envelope and agent_id field presence remain.
Paths and harmless model usage fields are evidence, not instructions.

`claude-p4-print-unavailable.jsonl` is the failed print-mode setup, distinct from
the successful interactive `claude-p4.jsonl`. P5's answer and pending cases are
separate fresh contexts. `codex-observations.json` contains CLI route receipts and
answerer JSON/usage, not merely a claim of accepted delivery. See the evidence
doc for the captured consumption and marker observations.
