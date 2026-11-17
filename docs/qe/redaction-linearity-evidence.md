# PR #12 redaction coverage and linearity

The fix6b clarification permits four explicitly named benign corrections to
`c112288`; secret-positive cases must retain detection. Frozen spec §5.2.2
still makes every raw redaction hit a `SECRET_PRESENT` guard hit.

## Search starts and coverage

| Pattern | Linear scan argument | Coverage |
| --- | --- | --- |
| `JWT` | Start only at the edge of a base64url run. Consume the first segment and look ahead across two more; each run participates in at most three segment scans. Search its first segment once for `eyJ` at offset zero or after `-`. | Restores `x-eyJ…`, `auth-eyJ…`, `tok-eyJ…` and short `x-eyJa.b.c`. Redaction starts at `eyJ`. Consuming only one segment also retains overlapping candidates such as `a.b.eyJa.b.c`. Requires at least one character after `eyJ` and the existing final word boundary. |
| `ASSIGNMENT` | One start per key run; alphanumeric chunks and separator runs are disjoint. Secret names and allowed suffix alternatives are bounded. Quoted values stop at a quote, newline or end. | Includes leading CLI/environment separators and doubled/mixed interior separators, including `Database__Password`. The `max_token`/`max-token` exemption applies at the start after leading separators; `a__max_token` remains secret. |
| `URL_USERINFO` | One start per scheme-character run; the credential scan excludes URL path/query/fragment delimiters. | Removes userinfo, including passwords containing `@`, while preserving destination and query/fragment text. |
| `PRIVATE_KEY` | A recognized opening consumes through its closing marker or end of text. Marker words use disjoint word/space runs. | Complete and truncated private keys remain redacted. |
| Provider keys, bearer, entropy token | Fixed prefixes or one non-whitespace run; no overlapping unbounded search starts. | Existing provider-key, bearer and Unicode entropy cases remain covered. |
| Guard policy patterns | Fixed or bounded alternatives; generated inventory checks every compiled policy pattern and the real normalized guard path. | Frozen §5.2.2 policy remains unchanged. |

The generated suite retains its 200,000 and 800,000 character inputs, every
existing family, ratio below 6, and 30-second trial bound. It uses one small
trial and two large trials instead of two at both sizes. Added seeds cover
`a__`, `a_-`, `a-_password_`, `x-eyJa-`, and dotted overlapping candidates.
This reduces trial work while extending input coverage.

## Reproduction

```sh
uv run python -m unittest discover -s skills/rex/scripts -p test_question_redact.py -v
uv run python -m unittest discover -s skills/rex/scripts -p test_question_regex_scaling.py -v
uv run --script tests/qe/check_redaction_differential.py --output /tmp/pr12-fix6b-differential.json
bun run test
```

Red-first verification at `70c6511` ran 17 tests and failed 36 subcases for
the new JWT and doubled-separator expectations. After the fix all 17 pass.
The expanded scaling suite passes all three tests in 37.547 seconds:

| Operation | 200k | 800k | Ratio |
| --- | --- | --- | --- |
| Assignment pattern | 0.4635s | 1.8521s | 4.00× |
| JWT pattern | 0.0545s | 0.2189s | 4.02× |
| Guard scan / assignment seeds | 0.1077s | 0.4241s | 3.94× |
| Guard scan / JWT seeds | 0.1305s | 0.5218s | 4.00× |

These are local measurements, not guarantees for every host or arbitrary
input. The suite also checks full guard scanning, entropy, span merging and
output construction. Redaction remains heuristic detection.

## Benign corrections

Only these four inputs are designated benign corrections by the clarification.
The differential prints both versions' hits and redacted output for each.

| Input | `c112288` | Current |
| --- | --- | --- |
| `max_token=5` | assignment; `max_[REDACTED]` | no hit; unchanged |
| `--max_token=5` | assignment; `--max_[REDACTED]` | no hit; unchanged |
| `mytoken=x` | assignment; `my[REDACTED]` | no hit; unchanged |
| `notpassword=x` | assignment; `not[REDACTED]` | no hit; unchanged |

## Differential and full gates

The reproducible [differential report](redaction-differential.json) compares
344 unique inputs: 243 secret-positive test/review inputs and all 101 unique
guard-corpus prompts (all five arrays, including known misses and quarantines).
Of these, 154 are detected at `c112288`. **Zero baseline-positive detections
and zero JWT rule detections are lost.** The four benign corrections above
are compared separately, with their full before/after output.

Secret cases are selected from test expectations, independently of current
detection. The checker records all inputs from eleven positive-only methods;
extracts the explicit positive halves of mixed key, compound and JWT loops;
and includes the tab assignment. It inventories every test method and fails
if a new method has not been classified. Benign-only methods and the negative
halves of mixed tests are identified in the script and report rather than
used to define secret positives. No guard-corpus string is filtered out.

`bun run test` passed: 75 general Python tests, 232 Rex Python tests (two
previously documented expected failures for frozen guard corpus targets),
and 138 Bun tests with 850 assertions. There are no unexpected failures.
Touched-file `ruff format --check`, `ruff check`, `ty check` with the Rex
scripts search path, and `git diff --check` also passed. No dependency,
credential, guard policy, or hook installation changes were needed.
