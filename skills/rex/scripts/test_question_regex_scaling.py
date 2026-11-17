#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Generated search-start/near-miss tests; all timing is in-process.

The seed tables enumerate alternative starts, not complete adversarial inputs.
The generator exercises overlapping starts, separated starts, and long failed
tails at 200k and 800k characters. Keep these inventories aligned with the audit.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import re
import time
import unittest

import question_guard as guard
import question_redact as redact


@dataclass(frozen=True)
class PatternFamily:
    name: str
    pattern: re.Pattern[str]
    prefixes: tuple[str, ...]


REDACTION_PREFIXES = {
    redact.RedactionRule.URL_USERINFO: ("x", "a-", "a://a:", "git+https://a:"),
    redact.RedactionRule.HIGH_ENTROPY: ("x", " a_", " \t"),
    redact.RedactionRule.PRIVATE_KEY: (
        "-----BEGIN A ",
        "-----BEGIN RSA PRIVATE KEY-----\n-----END A ",
        "-----BEGIN PRIVATE KEY-----\n",
    ),
    redact.RedactionRule.OPENAI_KEY: ("sk-",),
    redact.RedactionRule.GITHUB_TOKEN: (
        "ghp_",
        "gho_",
        "ghu_",
        "ghs_",
        "ghr_",
        "github_pat_",
    ),
    redact.RedactionRule.GITLAB_TOKEN: ("glpat-",),
    redact.RedactionRule.SLACK_TOKEN: ("xoxa-", "xoxb-", "xoxp-", "xoxr-"),
    redact.RedactionRule.AWS_KEY: ("AKIA",),
    redact.RedactionRule.BEARER: ("Bearer ", "Bearer\t"),
    redact.RedactionRule.ASSIGNMENT: (
        " a_",
        "_a",
        "--api-key",
        "password=",
        'password="',
        "password='",
        "passwd=",
        "pwd=",
        "passphrase=",
        "secret=",
        "secret_access_key=",
        "secret_key=",
        "secret_key_base=",
        "token=",
        "token_key=",
        "api-key=",
        "apikey=",
        "private-key=",
        "privatekey=",
        "password_prod_",
        "password_production_",
        "password_staging_",
        "password_stage_",
        "password_dev_",
        "password_development_",
        "password_test_",
        "password_eu_",
        "password_value_",
        "--max_token",
        "password\t",
    ),
    redact.RedactionRule.JWT: ("eyJa-", "eyJa.a-", "eyJa.a.a-"),
}

# Each top-level policy alternative has a seed. Nested alternatives share the
# same start; weaken/rotate also include their bounded filler-word paths.
GUARD_PREFIXES = {
    "merge_or_push.strong": (
        "git ",
        "push ",
        "force-",
        "x--force",
        "merge ",
        "gh ",
        "rebase ",
        "skip ",
    ),
    "security.strong": (
        "credential",
        "signing ",
        "password",
        "passphrase",
        "api ",
        "secret ",
        "private ",
        "ssh ",
        "access ",
        "auth ",
        "disable ",
        "chmod ",
        "permission ",
        "cve-0000-",
    ),
    "security.weaken": (
        "turn off ",
        "disable ",
        "bypass ",
        "weaken ",
        "skip ",
        "remove ",
        "relax ",
        "loosen ",
        "downgrade ",
        "skip the a- a- auth",
        "skip the a- a- rate limit",
    ),
    "security.rotate": (
        "rotate ",
        "revoke ",
        "regenerate ",
        "reissue ",
        "rotate the a- a- ",
    ),
    "money.strong": (
        "payment",
        "pay ",
        "purchase",
        "buy",
        "billing",
        "invoice",
        "subscription",
        "paid ",
        "upgrade ",
        "charge ",
        "credit ",
        "refund",
        "pricing ",
        "$ ",
        "€ ",
        "£ ",
    ),
    "data_loss.strong": (
        "rm ",
        "drop ",
        "truncate ",
        "delete ",
        "wipe",
        "purge",
        "destroy",
        "terraform ",
        "irreversible",
    ),
    "deploy_or_infra.strong": (
        "deploy ",
        "prod ",
        "dns",
        "terraform ",
        "wrangler ",
        "release ",
    ),
    "external_side_effect.strong": (
        "send ",
        "post ",
        "publish",
        "open ",
        "create ",
        "notify ",
    ),
    "scope.strong": (
        "out ",
        "scope ",
        "change ",
        "new ",
        "add ",
        "drop ",
        "requirement ",
        "instead ",
    ),
    "la_cana.strong": ("la ", "la_ca"),
    "merge_or_push.weak": ("push", "merge", "rebase", "tag", "release"),
    "data_loss.weak": ("delete", "remove", "drop", "migrate", "overwrite", "reset"),
    "security.weak": (
        "token",
        "secret",
        "key",
        "auth",
        "login",
        "permission",
        "sandbox",
        "cors",
        "csp",
        "mfa",
        "2fa",
        "otp",
        "tls",
        "ssl",
        "encrypt",
    ),
    "money.weak": ("price", "cost", "budget", "quota", "plan"),
    "deploy_or_infra.weak": (
        "deploy",
        "prod",
        "infra",
        "cloudflare",
        "worker",
        "bucket",
        "ci",
        "workflow",
    ),
}


def repeat_to_size(unit: str, size: int) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def adversarial_inputs(prefixes: tuple[str, ...], size: int) -> Iterator[str]:
    for prefix in prefixes:
        # No terminating delimiter: repeated starts inside the same long run.
        yield repeat_to_size(prefix, size - 1) + "!"
        # Failed suffix at each start, with a boundary permitting the next one.
        yield repeat_to_size(prefix + "! ", size - 1) + "!"
        # Only one start but an unbounded near-miss tail.
        yield prefix + "a" * (size - len(prefix) - 1) + "!"


def families() -> tuple[PatternFamily, ...]:
    patterns = (
        (redact.RedactionRule.URL_USERINFO, redact.URL_USERINFO),
        (redact.RedactionRule.HIGH_ENTROPY, redact.TOKEN),
        *redact.PATTERNS,
    )
    return tuple(
        PatternFamily(rule.value, pattern, REDACTION_PREFIXES[rule])
        for rule, pattern in patterns
    ) + tuple(
        PatternFamily(rule.id, rule.pattern, GUARD_PREFIXES[rule.id])
        for rule in guard.load_guard()
    )


class RegexScalingTests(unittest.TestCase):
    def assert_linear(
        self,
        name: str,
        operation: Callable[[str], object],
        inputs: Callable[[int], Iterator[str]],
    ) -> None:
        timings = []
        for size in (200_000, 800_000):
            # Generate outside the timing; two trials reduce scheduler noise.
            values = tuple(inputs(size))
            trials = []
            for _ in range(2):
                started = time.perf_counter()
                for value in values:
                    operation(value)
                trials.append(time.perf_counter() - started)
            timings.append(min(trials))
            self.assertLess(sum(trials), 30.0, (name, size, trials))
        ratio = timings[1] / timings[0]
        print(f"scaling {name}: {timings[0]:.4f}s -> {timings[1]:.4f}s ({ratio:.2f}x)")
        self.assertLess(ratio, 6.0, (name, timings))

    def test_inventory_covers_every_compiled_pattern(self):
        compiled = {
            pattern
            for pattern in vars(redact).values()
            if isinstance(pattern, re.Pattern)
        } | {pattern for _, pattern in redact.PATTERNS}
        covered = {
            family.pattern
            for family in families()
            if family.name in set(redact.RedactionRule)
        }
        self.assertEqual(compiled, covered)
        self.assertEqual(set(GUARD_PREFIXES), {rule.id for rule in guard.load_guard()})
        self.assertEqual(set(REDACTION_PREFIXES), set(redact.RedactionRule))

    def test_every_pattern_scales_on_generated_adversarial_inputs(self):
        for family in families():
            with self.subTest(pattern=family.name):
                self.assert_linear(
                    family.name,
                    lambda text: sum(1 for _ in family.pattern.finditer(text)),
                    lambda size: adversarial_inputs(family.prefixes, size),
                )

    def test_guard_scan_scales_on_generated_alternative_starts(self):
        # Exercise the real path including normalization, policy loading,
        # entropy, span merging and output construction, once per family.
        for family in families():
            with self.subTest(pattern=family.name):
                self.assert_linear(
                    "scan/" + family.name,
                    guard.scan,
                    lambda size: iter(
                        (
                            repeat_to_size("! ".join(family.prefixes) + "! ", size),
                            repeat_to_size(family.prefixes[0], size - 1) + "!",
                        )
                    ),
                )


if __name__ == "__main__":
    unittest.main()
