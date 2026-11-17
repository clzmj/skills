#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import unittest

import question_redact as redact


class RedactionTests(unittest.TestCase):
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
