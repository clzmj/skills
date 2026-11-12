#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Black-box scanner tests; dependency pins belong only to the standalone tool."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCANNER = Path(__file__).resolve().parents[1] / "skills/anti-strings/scripts/scan.py"


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anti-strings-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()

    def write(self, path, contents):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        return target

    def run_scan(self, *args, root=None, json_output=True, check=True):
        command = ["uv", "run", "--script", str(SCANNER), str(root or self.root), "--format", "json" if json_output else "markdown", *args]
        result = subprocess.run(command, text=True, capture_output=True, timeout=90)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if json_output and check else result

    def finding(self, report, value):
        return next(item for item in report["findings"] if item["value"] == value)

    def test_named_language_grammars_normalize_escapes_raw_and_static_templates(self):
        fixtures = {
            "state.py": 'QUEUED = "queued"\nif state == r"queued": pass\nif other == "q\\u0075eued": pass\n',
            "state.go": 'package state\nconst Queued = "queued"\nfunc next(s string) { if s == `queued` {} ; if s == "q\\u0075eued" {} }\n',
            "state.rs": 'const QUEUED: &str = r#"queued"#; fn next(s: &str) { if s == "queued" {} if s == "q\\u{75}eued" {} }\n',
            "state.js": 'const QUEUED="queued"; if (state === `queued`) {}; if (other === "q\\u0075eued") {};',
            "state.ts": 'const QUEUED: string="queued"; if (state === `queued`) {}; if (other === "q\\x75eued") {};',
            "state.tsx": 'const QUEUED="queued"; if (state === "queued") {}; const el=<div data-state="queued"/>;',
        }
        for name, source in fixtures.items(): self.write(name, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        found = self.finding(report, "queued")
        self.assertEqual(found["counts"], {"occurrences": 18, "files": 6, "production": 18, "tests": 0})
        self.assertEqual({item["language"] for item in found["locations"]}, {"python", "go", "rust", "javascript", "typescript", "tsx"})
        self.assertTrue({"QUEUED", "Queued"}.issubset({item["name"] for item in found["constant_candidates"]}))
        self.assertEqual(found["priority"], "high")

    def test_go_utf8_byte_and_unicode_escapes_have_the_same_value(self):
        self.write("unicode.go", 'package state\nconst Queued="équeued"\nfunc next(s string){if s == "\\xc3\\xa9queued"{};if s == "\\u00e9queued"{}}')
        report = self.run_scan()
        self.assertEqual(self.finding(report, "équeued")["counts"]["occurrences"], 3)

    def test_exported_implementation_is_counted_but_module_specifiers_are_not(self):
        self.write("options.ts", 'export const GLOBAL="global"; export function mode(x:string){ return x === "global"; } export default {scope:"global"}; export {x} from "module-path"; export * from "module-path"; import x from "module-path";')
        report = self.run_scan()
        found = self.finding(report, "global")
        self.assertEqual(found["counts"]["occurrences"], 3)
        self.assertEqual([item["name"] for item in found["constant_candidates"]], ["GLOBAL"])
        self.assertFalse(any(item["value"] == "module-path" for item in report["findings"]))

    def test_concat_keeps_occurrences_without_comparison_role_or_priority_boost(self):
        self.write("state.js", 'export function join(x){return "concat_state" + x}; export function eq(x){return x === "equal_state"};')
        self.write("state.go", 'package state\nfunc Join(x string) string {return "concat_state" + x}; func Eq(x string) bool {return x == "equal_state"}')
        self.write("state.rs", 'pub fn join(x:&str)->String {"concat_state".to_owned() + x} pub fn eq(x:&str)->bool {x == "equal_state"}')
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        concat = self.finding(report, "concat_state")
        equality = self.finding(report, "equal_state")
        self.assertEqual(concat["counts"]["occurrences"], 3)
        self.assertTrue(all(item["role"] != "comparison" for item in concat["locations"]))
        self.assertEqual(concat["priority"], "low")
        self.assertTrue(all(item["role"] == "comparison" for item in equality["locations"]))
        self.assertEqual(equality["priority"], "high")

    def test_comments_docstrings_interpolation_ui_and_css_do_not_supply_fake_occurrences(self):
        self.write("noise.py", '"queued"\n# "queued" "queued"\ntext=f"queued{value}"\nvalue="queued"\n')
        self.write("noise.tsx", '// "queued" "queued"\nconst text=`queued${value}`; const node=<div className="queued" title="queued">queued</div>; cn("queued"); const value="queued";')
        report = self.run_scan()
        self.assertEqual(report["findings"], [])
        self.assertGreaterEqual(report["coverage"]["literal_skips"].get("interpolated_or_unsupported", 0), 2)

    def test_redacts_context_credentials_contact_details_and_no_raw_snippets(self):
        values = ["private-context-value", "sk-syntheticCredentialValue12345", "alice.private@example.invalid", "+52 55 1234 5678"]
        for index, value in enumerate(values):
            key = "API_KEY" if index == 0 else "value"
            self.write(f"private{index}.py", "\n".join(f'{key} = {value!r}' for _ in range(3)))
        raw = self.run_scan(json_output=False).stdout
        report = self.run_scan()
        serialized = json.dumps(report)
        for value in values:
            self.assertNotIn(value, raw)
            self.assertNotIn(value, serialized)
        self.assertEqual(len(report["findings"]), 4)
        self.assertTrue(all(item["redacted"] and item["value"] == "<redacted>" for item in report["findings"]))
        self.assertNotIn("snippet", serialized)

    def test_redacts_credential_url_queries_but_preserves_ordinary_query_parameters(self):
        secret = "syntheticQuerySecret"
        urls = [f"https://api.example.invalid/resource?{key}={secret}" for key in ("token", "api_key", "signature", "%74oken")]
        for index, url in enumerate(urls): self.write(f"query{index}.py", f"url={url!r}\n" * 3)
        ordinary = "https://images.example.invalid/image?width=800&page=2&quality=90"
        self.write("ordinary.py", f"url={ordinary!r}\n" * 3)
        report = self.run_scan()
        rendered = self.run_scan(json_output=False).stdout
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, rendered)
        self.assertEqual(len([item for item in report["findings"] if item["redacted"]]), 4)
        self.assertEqual(self.finding(report, ordinary)["counts"]["occurrences"], 3)
        self.assertIn("width=800", rendered)
        self.assertIn("quality=90", rendered)

    def test_safe_fallback_honors_nested_gitignore_skips_links_secrets_generated_and_binary(self):
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/hidden.py", 'VALUE="excluded"\n' * 3)
        self.write("nested/.gitignore", "hidden.py\n")
        self.write("nested/hidden.py", 'VALUE="excluded"\n' * 3)
        self.write("vendor/third.py", 'VALUE="excluded"\n' * 3)
        self.write("generated/code.py", 'VALUE="excluded"\n' * 3)
        self.write("credentials.py", 'VALUE="excluded"\n' * 3)
        self.write("broken.py", 'VALUE="excluded"\x00')
        outside = Path(self.temp.name) / "outside.py"; outside.write_text('VALUE="excluded"\n' * 3)
        (self.root / "linked.py").symlink_to(outside)
        (self.root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
        self.write("safe.py", 'STATUS="ready"\nif value == "ready": pass\nif other == "ready": pass\n')
        report = self.run_scan()
        self.assertEqual([item["path"] for item in report["coverage"]["scanned"]], ["safe.py"])
        self.assertEqual([item["value"] for item in report["findings"]], ["ready"])
        reasons = {item["reason"] for item in report["coverage"]["excluded"]}
        self.assertTrue({"gitignored", "sensitive_filename", "symlink", "dependency_or_generated_directory", "binary_unreadable_or_nonutf8"}.issubset(reasons))

    def test_git_inventory_includes_untracked_nonignored_and_reports_parse_and_unsupported_coverage(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.write(".gitignore", "ignored.py\n")
        self.write("ignored.py", 'VALUE="ignored"\n' * 3)
        self.write("untracked.ts", 'if(x === "ready") f("ready"); const READY="ready";')
        self.write("invalid.py", 'if value == "ready": (\n')
        self.write("infra.tf", 'value="ready"\n' * 3)
        self.write("config.json", '{"value":"ready"}')
        report = self.run_scan()
        self.assertEqual(report["coverage"]["discovery"], "git")
        self.assertEqual(len(report["coverage"]["parse_errors"]), 1)
        self.assertEqual(report["coverage"]["parse_errors"][0]["path"], "invalid.py")
        self.assertTrue({"infra.tf", "config.json"}.issubset({item["path"] for item in report["coverage"]["unsupported"]}))
        self.assertEqual(self.finding(report, "ready")["counts"]["occurrences"], 3)

    def test_parse_error_columns_use_unicode_characters_not_utf8_bytes(self):
        self.write("ascii.ts", 'export const text = "x"; ???\n')
        self.write("unicode.ts", 'export const text = "é"; ???\n')
        report = self.run_scan()
        errors = {item["path"]: item for item in report["coverage"]["parse_errors"]}
        self.assertEqual(set(errors), {"ascii.ts", "unicode.ts"})
        self.assertGreater(errors["ascii.ts"]["column"], 1)
        self.assertEqual(errors["ascii.ts"]["column"], errors["unicode.ts"]["column"])

    def test_test_locations_are_opt_in_and_output_is_deterministic_and_read_only(self):
        self.write("state.py", 'QUEUED="queued"\nif state == "queued": pass\nif other == "queued": pass\n')
        self.write("tests/test_state.py", 'EXPECTED="queued"\n' * 3)
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        first = self.run_scan()
        second = self.run_scan()
        self.assertEqual(first, second)
        self.assertEqual(self.finding(first, "queued")["counts"]["tests"], 0)
        included = self.run_scan("--include-tests")
        self.assertEqual(self.finding(included, "queued")["counts"]["tests"], 3)
        after = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        denied = self.run_scan("--output", str(self.root / "report.json"), check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse((self.root / "report.json").exists())

    def test_config_controls_threshold_exclusions_and_explicit_parser_mapping(self):
        self.write("state.custom", 'STATUS="queued"\nif value == "queued": pass\n')
        self.write("omit.py", 'VALUE="omit-value"\n' * 3)
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"exclude": ["omit.py"], "min_occurrences": 2, "extensions": {".custom": "python"}}))
        report = self.run_scan("--config", str(config))
        self.assertEqual(self.finding(report, "queued")["counts"]["occurrences"], 2)
        self.assertEqual(report["coverage"]["excluded"], [{"path": "omit.py", "reason": "configured_exclusion"}])
        output = Path(self.temp.name) / "report.md"
        self.run_scan("--config", str(config), "--output", str(output), json_output=False)
        self.assertIn("Repeated literal evidence", output.read_text())


if __name__ == "__main__":
    unittest.main()
