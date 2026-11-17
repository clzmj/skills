#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare existing redaction inputs against the c112288 baseline."""

from __future__ import annotations

import importlib.util
import argparse
import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType
from typing import Literal, TypedDict

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "skills/rex/scripts/test_question_redact.py"
WORKING = ROOT / "skills/rex/scripts/question_redact.py"
CORPUS = ROOT / "tests/qe/guard_corpus.toml"
KEYWORDS = ("password", "passwd", "pwd", "passphrase", "secret", "token", "apikey")
PREFIXES = ("", "pg", "db", "my", "github", "api", "max_", "not")
SEPARATORS = ("", "_", "-", "__")
SPEC_PATTERN = re.compile(
    r"(password|passwd|pwd|passphrase|secret|token|api[_-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
KEYWORD_SUFFIX_PATTERN = re.compile(
    r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|private[_-]?key)[A-Za-z0-9_-]+(?=\s*[:=])",
    re.IGNORECASE,
)
sys.path.insert(0, str(TESTS.parent))


class DetectionLoss(TypedDict):
    input: str
    baseline_rules: list[str]
    current_rules: list[str]


class DetectionGain(TypedDict):
    input: str
    added_rules: list[str]
    classification: Literal["keyword_suffix"] | None


class GeneratedKeyMiss(TypedDict):
    input: str
    implementation: str


class DifferentialReport(TypedDict):
    unit_test_inputs: int
    guard_corpus_inputs: int
    compared_inputs: int
    baseline_positive_inputs: int
    spec_positive_inputs: int
    spec_positive_misses: list[str]
    generated_key_inputs: int
    generated_key_misses: list[GeneratedKeyMiss]
    unit_test_failures: int
    inputs: list[str]
    unit_test_methods: tuple[str, ...]
    guard_corpus_arrays: tuple[str, ...]
    baseline_positive_losses: list[str]
    rule_losses: list[DetectionLoss]
    detection_gains: list[DetectionGain]
    unclassified_gains: list[str]


def load_module(name: str, source: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def record_test_inputs() -> tuple[set[str], tuple[str, ...], int]:
    """Record inputs from every test, including negative key-table rows."""
    spec = importlib.util.spec_from_file_location("test_question_redact", TESTS)
    assert spec is not None and spec.loader is not None
    test_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_module)
    all_methods = tuple(
        sorted(
            name
            for name in vars(test_module.RedactionTests)
            if name.startswith("test_")
        )
    )
    seen: set[str] = set()
    original = test_module.redact.redact

    def recording_redact(text: str):
        seen.add(text)
        return original(text)

    test_module.redact.redact = recording_redact
    try:
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(
            unittest.TestSuite(test_module.RedactionTests(name) for name in all_methods)
        )
    finally:
        test_module.redact.redact = original
    return seen, all_methods, len(result.failures) + len(result.errors)


def gain_classification(
    value: str, added_rules: list[str]
) -> Literal["keyword_suffix"] | None:
    if added_rules == ["assignment"] and KEYWORD_SUFFIX_PATTERN.search(value):
        return "keyword_suffix"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="also write a JSON report to this path"
    )
    args = parser.parse_args()
    baseline_source = subprocess.run(
        ["git", "show", "c112288:skills/rex/scripts/question_redact.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    baseline = load_module("baseline_question_redact", baseline_source)
    working = load_module("working_question_redact", WORKING.read_text())
    inputs, methods, unit_test_failures = record_test_inputs()
    unit_test_inputs = len(inputs)
    corpus = tomllib.loads(CORPUS.read_text())
    prompt_arrays = tuple(
        sorted(key for key, value in corpus.items() if isinstance(value, list))
    )
    corpus_inputs = {
        value
        for key in prompt_arrays
        for value in corpus[key]
        if isinstance(value, str)
    }
    inputs.update(corpus_inputs)
    inputs.update(
        (
            "x-eyJa.b.c",
            "x-eyJ" + "a" * 20 + ".b.c",
            "auth-eyJ" + "a" * 20 + ".b.c",
            "Token:\nThe answer",
            "password\n=hunter2",
            "PGPASSWORD=hunter2",
            "dbpassword=hunter2",
            "githubtoken=hunter2",
            "apitoken=hunter2",
            "mysecret=hunter2",
            "mytoken=x",
            "notpassword=x",
            "max_token=5",
            "--max_token=5",
            "MAX-TOKEN=5",
            "MAX-TOKEN=hunter2",
            "my__password=x",
            "my--password=x",
            "x-_password=x",
            "Database__Password=x",
            "API_KEY_PROD=hunter2",
            "GITHUB_TOKEN_PROD=hunter2",
            "password_prod_eu=hunter2",
            "password\t=\tfixture",
        )
    )
    generated_keys = {
        f"{(prefix + separator + keyword).lower()}=hunter2"
        for keyword in KEYWORDS
        for prefix in PREFIXES
        for separator in SEPARATORS
    }
    generated_keys |= {
        value.partition("=")[0].upper() + "=hunter2" for value in generated_keys
    }
    inputs.update(generated_keys)
    inputs.update(("PGPASSWORD=hunter2", "dbpassword=hunter2"))
    baseline_positive = sum(baseline.redact(value).secret_present for value in inputs)
    boolean_losses: list[str] = []
    rule_losses: list[DetectionLoss] = []
    gains: list[DetectionGain] = []
    for value in sorted(inputs):
        old, new = baseline.redact(value), working.redact(value)
        old_rules = sorted({hit.value for hit in old.hits})
        new_rules = sorted({hit.value for hit in new.hits})
        if old.secret_present and not new.secret_present:
            boolean_losses.append(value)
        lost_rules = sorted(set(old_rules) - set(new_rules))
        if lost_rules:
            rule_losses.append(
                {
                    "input": value,
                    "baseline_rules": old_rules,
                    "current_rules": new_rules,
                }
            )
        added_rules = sorted(set(new_rules) - set(old_rules))
        if added_rules:
            gains.append(
                {
                    "input": value,
                    "added_rules": added_rules,
                    "classification": gain_classification(value, added_rules),
                }
            )
    spec_positive = {value for value in inputs if SPEC_PATTERN.search(value)}
    spec_misses = sorted(
        value for value in spec_positive if not working.redact(value).secret_present
    )
    generated_misses = [
        GeneratedKeyMiss(input=value, implementation=label)
        for value in sorted(generated_keys)
        for label, module in (("baseline", baseline), ("current", working))
        if not module.redact(value).secret_present
    ]
    print(
        f"Recorded unit-test inputs: {unit_test_inputs}; guard-corpus inputs: {len(corpus_inputs)}"
    )
    print(f"Test methods exercised ({len(methods)}): {', '.join(methods)}")
    print(f"Unit test assertion failures/errors: {unit_test_failures}")
    print(f"Guard corpus arrays included: {', '.join(prompt_arrays)}")
    print(
        f"Compared {len(inputs)} unique inputs, {baseline_positive} baseline-positive"
    )
    print(
        f"Baseline-positive boolean losses: {len(boolean_losses)}; rule losses: {len(rule_losses)}"
    )
    print(
        f"Detection gains: {len(gains)}; unclassified gains: {sum(gain['classification'] is None for gain in gains)}"
    )
    print(
        f"Spec-positive inputs: {len(spec_positive)}; current misses: {len(spec_misses)}"
    )
    print(
        f"Generated key inputs: {len(generated_keys)}; baseline/current misses: {len(generated_misses)}"
    )
    for value in boolean_losses:
        print(f"BASELINE POSITIVE LOSS {value!r}")
    for loss in rule_losses:
        print(
            f"RULE LOSS {loss['input']!r}: {loss['baseline_rules']} => {loss['current_rules']}"
        )
    for gain in gains:
        print(
            f"GAIN ({gain['classification'] or 'unclassified'}): {gain['input']!r} +{gain['added_rules']}"
        )
    for value in spec_misses:
        print(f"SPEC FLOOR MISS {value!r}")
    for miss in generated_misses:
        print(f"GENERATED KEY MISS ({miss['implementation']}): {miss['input']!r}")
    if args.output:
        report: DifferentialReport = {
            "unit_test_inputs": unit_test_inputs,
            "guard_corpus_inputs": len(corpus_inputs),
            "compared_inputs": len(inputs),
            "baseline_positive_inputs": baseline_positive,
            "spec_positive_inputs": len(spec_positive),
            "spec_positive_misses": spec_misses,
            "generated_key_inputs": len(generated_keys),
            "generated_key_misses": generated_misses,
            "unit_test_failures": unit_test_failures,
            "inputs": sorted(inputs),
            "unit_test_methods": methods,
            "guard_corpus_arrays": prompt_arrays,
            "baseline_positive_losses": boolean_losses,
            "rule_losses": rule_losses,
            "detection_gains": gains,
            "unclassified_gains": [
                gain["input"] for gain in gains if gain["classification"] is None
            ],
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return (
        1
        if boolean_losses
        or rule_losses
        or spec_misses
        or generated_misses
        or any(gain["classification"] is None for gain in gains)
        or unit_test_failures
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
