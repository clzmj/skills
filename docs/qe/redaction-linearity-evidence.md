# PR #12 redaction coverage and linearity

Frozen spec §8.4 is a detection floor. Its assignment regex has no left
anchor, and Python `\s` includes newlines. Keys ending in a secret keyword
must be detected, including `PGPASSWORD`, `dbpassword`, `mytoken`,
`notpassword`, `max_token`, and `MAX-TOKEN`. There are no benign key exemptions.
The earlier newline exclusion contradicted §8.4 and has been rolled back:
`Token:\nThe answer` and `password\n=hunter2` (actual newlines) are detected.

Dinesh's fix7 decision permits only keyword-suffix assignment gains over
`c112288`, retaining existing cases such as `API_KEY_PROD`,
`GITHUB_TOKEN_PROD`, and `password_prod_eu`. Every baseline-positive detection
and every baseline rule hit must survive. Frozen §5.2.2 still makes every raw
redaction hit a `SECRET_PRESENT` guard hit.

## Search starts and coverage

| Pattern | Linear scan argument | Coverage |
| --- | --- | --- |
| `ASSIGNMENT` | Start once per word/hyphen key run. Consume the prefix and backtrack over it once to locate a secret keyword. Keyword alternatives are bounded; suffix tokens cannot contain another complete secret keyword, so failed suffix scans cannot repeatedly traverse a long tail. Whitespace scans occur only at the key's end. | Glued prefixes, leading CLI/environment separators, doubled/mixed separators, existing secret suffixes, and all spec whitespace. Quoted values still stop at a quote, newline or end. |
| `JWT` | Start at a base64url run edge, consume one segment and look ahead across two more. Each run participates in at most three segment scans. The final word boundary can backtrack over trailing hyphens in one additional linear pass. | Retains hyphen-prefixed and overlapping dotted candidates, the minimum first-segment length and final word boundary. |
| `URL_USERINFO` | Start once per scheme-character run; exclude URL path/query/fragment delimiters from the credential scan. | Removes userinfo, including passwords containing `@`, while preserving the destination. |
| `PRIVATE_KEY` | Consume a recognized opening through its closing marker or end; marker words use disjoint word/space runs. | Complete and truncated private keys. |
| Provider keys, bearer, entropy token | Fixed prefixes or one non-whitespace run, without overlapping unbounded search starts. | Existing provider-key, bearer and Unicode entropy cases. |
| Guard policy patterns | Fixed or bounded alternatives; generated inventory checks every compiled policy pattern and the real normalized guard path. | Frozen §5.2.2 policy remains unchanged. |

The scaling suite retains all seed families, 200,000 and 800,000 character
inputs, a ratio below 6 and the existing 30-second bound. It now takes the
minimum of two 200k trials and runs one 800k trial. Scheduler noise in the
denominator therefore errs toward a stricter ratio, with less total work.

## Reproduction and red-first evidence

```sh
uv run python -m unittest discover -s skills/rex/scripts -p test_question_redact.py -v
uv run python -m unittest discover -s skills/rex/scripts -p test_question_regex_scaling.py -v
uv run --script tests/qe/check_redaction_differential.py --output /tmp/pr12-fix7-differential.json
bun run test
```

Before editing the production detector at `ad88319`, the glued-key regression
suite failed 72 subcases. Adding the restored newline expectations increased
this to 78 failing subcases across 17 tests. The decision-aligned differential
also failed: 159 boolean/rule losses, 159 spec-floor misses and 86 generated
current-key misses (zero generated baseline misses). Its 91 gains were already
confined to the allowed suffix class. After the fix, all 17 tests pass.

## Differential

The [committed report](redaction-differential.json) records every input and
lists every gain individually with class `keyword_suffix`. It compares
**922 unique inputs**, including all 384 recorded unit-test inputs and all
101 unique guard-corpus strings across all five arrays. All 17 test methods
run, including negative key-table rows; no inputs are filtered by whether
the current detector accepts them.

An independent generator builds 448 assignments from seven base keywords ×
eight prefixes (`""`, `pg`, `db`, `my`, `github`, `api`, `max_`, `not`) × four
separators (`""`, `_`, `-`, `__`) × lower/UPPER keys, with value `hunter2`.
Explicit `PGPASSWORD=hunter2`, `dbpassword=hunter2`, prior exempted literals,
and newline cases are included too.

| Check | Result |
| --- | --- |
| Baseline-positive inputs | 661 |
| Baseline-positive boolean losses | 0 |
| Baseline rule losses | 0 |
| Direct §8.4 regex positives / current misses | 588 / 0 |
| Generated inputs / baseline misses / current misses | 448 / 0 / 0 |
| Gains, each listed with its class in the report | 91 keyword-suffix assignments |
| Unclassified gains | 0 |

The checker fails on any baseline boolean/rule loss, spec-floor miss,
generated-key miss, failed unit assertion, or gain outside the allowed class.
Only an added `assignment` rule can qualify for that class; it includes the
baseline's existing private-key keyword and hyphenated suffix spellings.
Changes to redacted span width are not detection changes.

## Full gates

`bun run test` passes: 75 general Python tests, 232 Rex Python tests (including
all three scaling tests and two previously documented expected failures for
frozen guard-corpus targets), and 138 Bun tests with 850 assertions. There are
no unexpected failures. Focused Ruff format/check, ty with the Rex scripts
search path and repository configuration, and `git diff --check` also pass.

| Operation | 200k | 800k | Ratio |
| --- | --- | --- | --- |
| Assignment pattern | 0.5903s | 2.3758s | 4.02× |
| JWT pattern | 0.0538s | 0.2162s | 4.02× |
| Guard scan / assignment seeds | 0.1031s | 0.4159s | 4.03× |
| Guard scan / JWT seeds | 0.1329s | 0.5305s | 3.99× |

Measurements are host-specific evidence, not guarantees for arbitrary inputs.
Redaction remains heuristic detection.
