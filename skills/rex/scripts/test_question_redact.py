#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import unittest

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

    def test_url_userinfo_is_removed_without_changing_destination(self):
        result = redact.redact("Fetch https://alice:example-password@example.test/repo")
        self.assertEqual(result.text, "Fetch https://example.test/repo")
        self.assertTrue(result.secret_present)

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
