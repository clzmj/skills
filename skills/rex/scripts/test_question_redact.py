#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import subprocess
import unittest
from pathlib import Path

import question_redact as redact
from question_model import UntrustedSource


class RedactionTests(unittest.TestCase):
    def test_untrusted_factory_redacts_text_and_preserves_source(self):
        result = redact.untrusted("password=fixture", UntrustedSource.AGENT)
        self.assertEqual(result.text, "[REDACTED]")
        self.assertEqual(result.source, UntrustedSource.AGENT)

    def test_path_text_is_entropy_scanned_under_spec(self):
        for path in (
            "skills/rex/scripts/question_ledger.py",
            "src/components/UserProfileCard.tsx",
        ):
            with self.subTest(path=path):
                self.assertIn(
                    redact.RedactionRule.HIGH_ENTROPY, redact.redact(path).hits
                )

    def test_truncated_private_key_is_removed(self):
        result = redact.redact("Before -----BEGIN RSA PRIVATE KEY-----\nfixture")
        self.assertEqual(result.text, "Before [REDACTED]")
        self.assertTrue(result.secret_present)

    def test_url_userinfo_with_at_in_password_is_removed(self):
        result = redact.redact("Fetch https://u:p@ss@host.test/repo")
        self.assertEqual(result.text, "Fetch https://host.test/repo")
        self.assertTrue(result.secret_present)

    def test_secret_assignments_in_environment_and_json_are_removed(self):
        for text in (
            "DB_PASSWORD=hunter2",
            "OPENAI_API_KEY=fixture",
            "GITHUB_TOKEN: fixture",
            '"password": "hunter2"',
        ):
            with self.subTest(text=text):
                result = redact.redact(text)
                self.assertTrue(result.secret_present)
                self.assertNotIn("hunter2", result.text)
                self.assertNotIn("fixture", result.text)

    def test_compound_secret_keys_are_redacted_without_matching_token_limits(self):
        for value in (
            "AWS_SECRET_ACCESS_KEY=hunter2",
            "secret_key_base: abc",
            "private_key: abc",
            "api_key: abc",
            "PASSWORD_PROD=x",
        ):
            with self.subTest(value=value):
                result = redact.redact(value)
                self.assertTrue(result.secret_present)
                self.assertNotIn(value.split()[-1].split("=")[-1], result.text)
        for value in (
            "max_tokens=1000",
            "tokenizer=fast",
            "tokens_per_second: 50",
            "token_count=5",
            "token_limit: 4096",
            "password_min_length=8",
            "secret_santa_count=3",
            "max_token_length=512",
        ):
            with self.subTest(value=value):
                self.assertEqual(redact.redact(value).text, value)
                self.assertFalse(redact.redact(value).secret_present)

    def test_url_userinfo_is_removed_without_changing_destination(self):
        result = redact.redact("Fetch https://alice:example-password@example.test/repo")
        self.assertEqual(result.text, "Fetch https://example.test/repo")
        self.assertTrue(result.secret_present)

    def test_at_in_query_or_fragment_is_not_url_userinfo(self):
        for value in (
            "https://example.com?to=a@b.c",
            "https://example.com#to=a@b.c",
        ):
            with self.subTest(value=value):
                result = redact.redact(value)
                self.assertEqual(result.text, value)
                self.assertFalse(result.secret_present)

    def test_quoted_password_with_spaces_is_fully_redacted(self):
        for value in ('password = "hunter two"', "password = 'hunter two'"):
            with self.subTest(value=value):
                self.assertEqual(redact.redact(value).text, "[REDACTED]")

    def test_quoted_values_do_not_cross_newlines(self):
        for quote in ('"', "'"):
            for newline in ("\n", "\r"):
                with self.subTest(quote=quote, newline=repr(newline)):
                    value = f"password = {quote}first line{newline}second line{quote}"
                    self.assertEqual(
                        redact.redact(value).text,
                        f"[REDACTED]{newline}second line{quote}",
                    )

    def test_compound_key_regex_finishes_quickly_on_nonmatching_tail(self):
        script = """
import sys, time
sys.path.insert(0, sys.argv[1])
import question_redact
value = 'token' + '_a' * 40 + '!'
start = time.perf_counter()
question_redact.redact(value)
print(time.perf_counter() - start)
"""
        try:
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "--no-project",
                    "python",
                    "-c",
                    script,
                    str(Path(__file__).parent),
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
        except subprocess.TimeoutExpired:
            self.fail("redaction regex hung on 40 nonmatching suffix segments")
        self.assertLess(float(result.stdout), 0.1)

    def test_every_secret_shape_and_entropy_is_removed(self):
        samples = (
            "-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----",
            "sk-" + "a" * 24,
            "ghp_" + "b" * 24,
            "github_pat_" + "c" * 24,
            "glpat-fixture-value",
            "xoxb-fixture-value",
            "AKIA" + "A" * 16,
            "Bearer " + "z" * 15,
            "password=fixture",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.fixtureSignature",
            "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv",
            "é1!ø2@ñ3#å4$ç5%ß6^中7&文8*字9(語0)にあいうえお",
        )
        for sample in samples:
            with self.subTest(sample=sample[:12]):
                result = redact.redact("Before " + sample + " after")
                self.assertTrue(result.secret_present)
                self.assertNotIn(sample, result.text)
                self.assertIn(redact.REDACTED, result.text)

    def test_normal_text_and_low_entropy_strings_survive(self):
        text = "Choose a fixture name " + "a" * 48
        self.assertEqual(redact.redact(text).text, text)
        self.assertFalse(redact.redact(text).secret_present)

    def test_overlapping_matches_redact_once_and_preserve_context(self):
        result = redact.redact("Use token=sk-" + "a" * 24 + " safely")
        self.assertEqual(result.text, "Use [REDACTED] safely")

    def test_case_and_url_protocol_variants(self):
        result = redact.redact("GIT+HTTPS://user%40name:p%40ss@host.test/repo")
        self.assertEqual(result.text, "GIT+HTTPS://host.test/repo")


if __name__ == "__main__":
    unittest.main()
