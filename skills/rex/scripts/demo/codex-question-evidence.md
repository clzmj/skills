# Codex question callback evidence

Observed on this Mac with `codex-cli 0.167.2`, November 15, 2026
(November 16 UTC). No inference from documentation or rollout polling.

## Hook probe

A throwaway Codex home and work directory under `/tmp/codex-question-probe`
registered a private JSONL logger for SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse, PermissionRequest, Notification, Stop and SessionEnd.
The TUI ran in a tab in the existing main Rex session with workspace-write and
on-request approvals. Only these locally vetted temporary hooks bypassed hook
trust for that invocation; persistent host trust/settings were unchanged.

Observed payload fields (other lifecycle fields omitted):

```json
{"hook_event_name":"PreToolUse","tool_name":"request_user_input","tool_use_id":"call_6d17ba20bd0d476699892d7648307684","tool_input":{"questions":[{"header":"Probe color","id":"plan_probe_color","question":"Which plan probe color?","options":[{"label":"Blue","description":"Choose Blue."},{"label":"Green","description":"Choose Green."}]}]}}
{"hook_event_name":"PostToolUse","tool_name":"request_user_input","tool_use_id":"call_6d17ba20bd0d476699892d7648307684","tool_response":"{\"answers\":{\"plan_probe_color\":{\"answers\":[\"Blue\"]}}}"}
```

In Default mode the blocking tool returned unavailable; the model then called
`request_user_input_async`, displaying `? 1 question` and `shift+left to answer`:

```json
{"hook_event_name":"PreToolUse","tool_name":"request_user_input_async","tool_use_id":"call_63c014de5ca148f5ac5306dcc30bb362","tool_input":{"questions":[{"title":"Which probe color?","options":["Blue","Green"]}]}}
{"hook_event_name":"PostToolUse","tool_name":"request_user_input_async","tool_use_id":"call_63c014de5ca148f5ac5306dcc30bb362","tool_response":"{\"accepted\":true}"}
```

No Notification or PermissionRequest fired for either question path. In Plan
mode the blocking PreToolUse arrived while the question was awaiting an answer;
PostToolUse arrived after selecting Blue. Async acceptance must not be mistaken
for an answer. Raw payloads remain in the private local `hooks.jsonl` file.

## Live callback delivery

A second isolated Codex home used this branch's installer-generated hook
configuration and a temporary runtime copy. The sole runtime adjustment was
pointing `CONFIG` at a private test config for a loopback HTTP receiver
(`127.0.0.1:18764`); no production webhook or credentials were used. Runtime
state was isolated with `REX_VISIBLE_STATE_DIR`. An opted-in local parent record
allowed the nested CLI to preserve `GROKBOT_PARENT_SESSION` and verify lineage.

The real Plan-mode TUI called `request_user_input` exactly once with
`Which receipt probe color?`. Before answering, the receiver and callback
receipts contained exactly one matching delivery:

```json
{"outcome":"delivered","event":"needs_input","http_status":204,"task_id":"codex-question-live","agent_session_id":"01a1e2a1-e8ea-7601-a6ef-3b7f2acbe653","key":"c58a64c55b80de4a20507284dc88ee8f6c7beffd347119156d9fee8f560e1516"}
```

The received body had `status: waiting`, `kind: question`,
`prompt_text: "Which receipt probe color?"`, requester `question-probe-bot`,
matching Rex session/block IDs, and depth-one parent/root lineage. Its `untrusted`
list included `prompt_text` alongside the existing text/metadata fields.
Selecting Blue produced a PostToolUse answer and a directly observed SQLite
status transition `waiting → running`. Later activity and completion did not
produce another needs_input POST for this call.

Private local artifacts: `posts.jsonl`, `resume.json`, and
`live-state/grokbot-callback-receipts.jsonl` under `/tmp/codex-question-probe`.
Generated evidence and callback state files have mode 0600.

## Regression and suites

The new `test_codex_request_user_input_questions_emit_untrusted_question` run
against the unchanged base runtime failed at `assertIsNotNone(path)` with
`AssertionError: unexpectedly None` (no callback was queued). It passes with this
branch, including delivery to a local HTTP server, duplicate suppression,
untrusted payload checks, lineage and answer-driven resume.

Final checks: `uv run --script scripts/check.py` passed both Python suites
(25 + 111 tests); `bun test` passed 105 tests across seven files (715 assertions).
The intentional baseline failure log and full suite logs remain under the same
local probe directory.

## PR review follow-up: matcher verification

A new isolated real Codex 0.167.2 TUI run registered three PreToolUse groups:
no matcher (control), `^(request_user_input|request_user_input_async)$`, and
`^never_matches_a_real_tool$` (negative control). Default mode exercised the
async question and `pwd`; Plan mode exercised a blocking question and then
`pwd` after selecting Blue. The control captured both question tools and Bash;
the question matcher captured only the two question tools; the negative control
captured zero events. Matching call IDs:

| Tool | Unfiltered control | Question matcher |
| --- | --- | --- |
| request_user_input_async | call_e05ea4656d7c40bf91eee2be78547b5a | same |
| Bash (pwd) | exec-208c7d4e-1b5b-4a87-b82a-b776e2511cf5 | absent |
| request_user_input | call_2edb3a5312d4442fbb175c8aa61cf8e7 | same |

Private raw payloads are `/tmp/codex-question-review/all.jsonl` and
`matched.jsonl`, mode 0600. Codex therefore honors the narrow matcher; no
all-tools import/filter fallback is needed. The existing broad PostToolUse
observer still records answers and later activity.

The read-only Claude `bun run test` denial is separate and predates this PR:
`rex_visible.classify` marks Bun commands containing the `test` argument as test
work, and its PreToolUse guard denies direct shell execution with instructions
to use the visible Rex runner. There is no read-only/review exception. The Claude
lifecycle adapter itself returns no permission decision on PreToolUse. Changing
that visibility policy is outside this question callback fix; it is unchanged.

Review-fix regression coverage now sends real loopback HTTP requests for
blocking-to-async content dedupe, a question repeated with fresh call IDs, a new
turn asking the same question, distinct question/approval deliveries, and async
acceptance remaining waiting after delivery. It also checks first-string field
selection, full-text identity before truncation, ignored Claude question-tool
PreToolUse and callback file modes under umask 000.

Final review-fix suites: **141 Python tests** (25 + 116), **105 Bun tests**
(715 assertions), all passed. Logs are under `/tmp/codex-question-review`.
