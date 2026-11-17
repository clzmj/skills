#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare existing redaction inputs against the c112288 baseline."""

from __future__ import annotations

import importlib.util
import argparse
import ast
import json
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "skills/rex/scripts/test_question_redact.py"
WORKING = ROOT / "skills/rex/scripts/question_redact.py"
CORPUS = ROOT / "tests/qe/guard_corpus.toml"
BENIGN = ("max_token=5", "--max_token=5", "mytoken=x", "notpassword=x")
sys.path.insert(0, str(TESTS.parent))


class BenignCorrection(TypedDict):
    input: str
    baseline_hits: list[str]
    baseline_text: str
    current_hits: list[str]
    current_text: str


class DifferentialReport(TypedDict):
    unit_test_inputs: int
    guard_corpus_inputs: int
    compared_inputs: int
    baseline_positive_inputs: int
    inputs: list[str]
    test_methods: tuple[str, ...]
    classified_methods_not_recorded_wholesale: tuple[str, ...]
    guard_corpus_arrays: tuple[str, ...]
    baseline_positive_losses: list[str]
    jwt_rule_losses: list[str]
    benign_corrections: list[BenignCorrection]


def load_module(name: str, source: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def record_test_inputs() -> tuple[set[str], tuple[str, ...], tuple[str, ...]]:
    """Record secret-positive cases; benign-only tests are explicitly inventoried."""
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
    positive_methods = (
        "test_untrusted_factory_redacts_text_and_preserves_source",
        "test_path_text_is_entropy_scanned_under_spec",
        "test_quoted_values_do_not_cross_newlines",
        "test_truncated_private_key_is_removed",
        "test_url_userinfo_with_at_in_password_is_removed",
        "test_secret_assignments_in_environment_and_json_are_removed",
        "test_url_userinfo_is_removed_without_changing_destination",
        "test_quoted_password_with_spaces_is_fully_redacted",
        "test_every_secret_shape_and_entropy_is_removed",
        "test_overlapping_matches_redact_once_and_preserve_context",
        "test_case_and_url_protocol_variants",
    )
    skipped_methods = (
        "test_compound_secret_keys_are_redacted_without_matching_token_limits",
        "test_secret_and_nonsecret_key_variants",
        "test_assignment_separator_does_not_cross_newlines",
        "test_at_in_query_or_fragment_is_not_url_userinfo",
        "test_jwt_starts_after_hyphens_and_overlapping_dotted_segments",
        "test_normal_text_and_low_entropy_strings_survive",
    )
    if set(all_methods) != set(positive_methods) | set(skipped_methods):
        raise RuntimeError(
            f"Unclassified redaction tests: {sorted(set(all_methods) - set(positive_methods) - set(skipped_methods))}"
        )
    seen: set[str] = set()
    original = test_module.redact.redact

    def recording_redact(text: str):
        seen.add(text)
        return original(text)

    test_module.redact.redact = recording_redact
    try:
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(
            unittest.TestSuite(
                test_module.RedactionTests(name) for name in positive_methods
            )
        )
    finally:
        test_module.redact.redact = original
    if not result.wasSuccessful():
        raise RuntimeError(
            "Existing redaction tests failed; refusing incomplete corpus"
        )
    tree = ast.parse(TESTS.read_text())
    # Explicit table positives, every configured prefix; evaluate literals only.
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.FunctionDef)
            or node.name != "test_secret_and_nonsecret_key_variants"
        ):
            continue
        prefix_loop = next(item for item in node.body if isinstance(item, ast.For))
        prefixes = ast.literal_eval(prefix_loop.iter)
        for assignment in ast.walk(node):
            if not isinstance(assignment, ast.Assign) or not any(
                isinstance(t, ast.Name) and t.id == "cases" for t in assignment.targets
            ):
                continue
            cases = ast.literal_eval(assignment.value)
            for key, expected in cases:
                if expected:
                    seen.update(f"{prefix}{key}=hunter2" for prefix in prefixes)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name
            == "test_compound_secret_keys_are_redacted_without_matching_token_limits"
        ):
            loop = next(item for item in node.body if isinstance(item, ast.For))
            seen.update(ast.literal_eval(loop.iter))
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "test_assignment_separator_does_not_cross_newlines"
        ):
            seen.add("password\t=\tfixture")
    # Capture positive loop inputs from the JWT test, excluding its explicitly listed negative tuple.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name
            == "test_jwt_starts_after_hyphens_and_overlapping_dotted_segments"
        ):
            token_loop = next(item for item in node.body if isinstance(item, ast.For))
            prefix_loop = next(
                item for item in token_loop.body if isinstance(item, ast.For)
            )
            for token in ast.literal_eval(token_loop.iter):
                for prefix in ast.literal_eval(prefix_loop.iter):
                    seen.add(prefix + token)
    return seen, positive_methods, skipped_methods


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
    inputs, methods, skipped_methods = record_test_inputs()
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
            "my__password=x",
            "my--password=x",
            "x-_password=x",
            "Database__Password=x",
        )
    )
    baseline_positive = sum(baseline.redact(value).secret_present for value in inputs)
    lost = sorted(
        value
        for value in inputs
        if baseline.redact(value).secret_present
        and not working.redact(value).secret_present
    )
    jwt_lost = sorted(
        value
        for value in inputs
        if "jwt" in {hit.value for hit in baseline.redact(value).hits}
        and "jwt" not in {hit.value for hit in working.redact(value).hits}
    )
    print(
        f"Recorded unit-test inputs: {len(inputs) - len(corpus_inputs)}; guard-corpus inputs: {len(corpus_inputs)}"
    )
    print(f"Test methods exercised ({len(methods)}): {', '.join(methods)}")
    print(
        f"Explicitly classified benign/mixed methods not recorded wholesale ({len(skipped_methods)}): {', '.join(skipped_methods)}"
    )
    print(f"Guard corpus arrays included: {', '.join(prompt_arrays)}")
    print(
        f"Compared {len(inputs)} unique inputs, {baseline_positive} baseline-positive"
    )
    print(f"Baseline-positive losses: {len(lost)}; JWT rule losses: {len(jwt_lost)}")
    for value in lost:
        print(f"LOST {value!r}")
    for value in jwt_lost:
        print(f"JWT LOST {value!r}")
    print("\nBenign corrections (baseline => current):")
    for value in BENIGN:
        old, new = baseline.redact(value), working.redact(value)
        print(f"{value!r}: {old.hits!r} {old.text!r} => {new.hits!r} {new.text!r}")
    if args.output:
        report: DifferentialReport = {
            "unit_test_inputs": len(inputs - corpus_inputs),
            "guard_corpus_inputs": len(corpus_inputs),
            "compared_inputs": len(inputs),
            "baseline_positive_inputs": baseline_positive,
            "inputs": sorted(inputs),
            "test_methods": methods,
            "classified_methods_not_recorded_wholesale": skipped_methods,
            "guard_corpus_arrays": prompt_arrays,
            "baseline_positive_losses": lost,
            "jwt_rule_losses": jwt_lost,
            "benign_corrections": [
                {
                    "input": value,
                    "baseline_hits": [hit.value for hit in baseline.redact(value).hits],
                    "baseline_text": baseline.redact(value).text,
                    "current_hits": [hit.value for hit in working.redact(value).hits],
                    "current_text": working.redact(value).text,
                }
                for value in BENIGN
            ],
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 1 if lost or jwt_lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
