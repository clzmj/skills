#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Black-box scanner tests; dependency pins belong only to the standalone tool."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        command = [
            "uv",
            "run",
            "--script",
            str(SCANNER),
            str(root or self.root),
            "--format",
            "json" if json_output else "markdown",
            *args,
        ]
        result = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=90
        )
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
        for name, source in fixtures.items():
            self.write(name, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        found = self.finding(report, "queued")
        self.assertEqual(
            found["counts"],
            {"occurrences": 18, "files": 6, "production": 18, "tests": 0},
        )
        self.assertEqual(
            {item["language"] for item in found["locations"]},
            {"python", "go", "rust", "javascript", "typescript", "tsx"},
        )
        self.assertTrue(
            {"QUEUED", "Queued"}.issubset(
                {item["name"] for item in found["constant_candidates"]}
            )
        )
        self.assertEqual(found["priority"], "high")

    def test_go_utf8_byte_and_unicode_escapes_have_the_same_value(self):
        self.write(
            "unicode.go",
            'package state\nconst Queued="équeued"\nfunc next(s string){if s == "\\xc3\\xa9queued"{};if s == "\\u00e9queued"{}}',
        )
        report = self.run_scan()
        self.assertEqual(self.finding(report, "équeued")["counts"]["occurrences"], 3)

    def test_additional_pinned_grammars_extract_static_strings(self):
        fixtures = {
            "state.java": (
                "java",
                'class State { String a="queued"; String b="queued"; String c="queued"; }',
            ),
            "state.kt": ("kotlin", 'val a="queued"\nval b="queued"\nval c="queued"'),
            "state.cs": (
                "csharp",
                'class State { string a="queued"; string b="queued"; string c="queued"; }',
            ),
            "state.swift": ("swift", 'let a="queued"\nlet b="queued"\nlet c="queued"'),
            "state.rb": ("ruby", 'a="queued"\nb="queued"\nc="queued"'),
            "state.php": ("php", '<?php $a="queued"; $b="queued"; $c="queued";'),
            "state.ex": ("elixir", 'a="queued"\nb="queued"\nc="queued"'),
            "state.sql": ("sql", "SELECT 'queued', 'queued', 'queued';"),
        }
        for name, (_, source) in fixtures.items():
            self.write(name, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(report["coverage"]["unsupported"], [])
        found = self.finding(report, "queued")
        self.assertEqual(found["counts"]["occurrences"], 24)
        self.assertEqual(
            {item["language"] for item in found["locations"]},
            {value[0] for value in fixtures.values()},
        )

    def test_repeated_key_index_is_an_explicit_per_file_heuristic(self):
        fixtures = {
            "state.py": 'a=obj["state"]\nb=other["state"]\nc=obj["state"]',
            "state.js": 'a=obj["state"]; b=other[`state`]; c=obj["st\\x61te"];',
            "state.ts": 'const a=obj["state"]; const b=other["state"]; const c=obj["state"];',
            "state.go": 'package state\nfunc f(){a:=obj["state"]; b:=other["state"]; c:=obj["state"]}',
            "state.rb": 'a=obj["state"]\nb=other[\'state\']\nc=obj["state"]',
            "state.php": '<?php $a=$obj["state"]; $b=$other[\'state\']; $c=$obj["state"];',
        }
        for name, source in fixtures.items():
            self.write(name, source)
        self.write("single.py", 'a=obj["state"]\n')
        self.write("ordinary.py", 'a="state"\nb="state"\nc="state"\n')
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        indexed = [
            item for item in report["findings"] if item["kind"] == "repeated_string_key"
        ]
        self.assertEqual(len(indexed), len(fixtures))
        self.assertEqual(
            {item["index_evidence"]["path"] for item in indexed}, set(fixtures)
        )
        self.assertTrue(
            all(
                item["heuristic"]
                and item["index_evidence"]["heuristic"]
                and item["counts"]["occurrences"] == 3
                for item in indexed
            )
        )
        self.assertTrue(
            all(
                "receiver types" in item["index_evidence"]["notice"] for item in indexed
            )
        )

    def test_index_heuristic_excludes_interpolation_slices_and_redacts_keys(self):
        self.write(
            "state.rb",
            'a=obj["sta#{value}te"]\nb=obj["sta#{value}te"]\nc=obj["sta#{value}te"]',
        )
        self.write(
            "state.php",
            '<?php $a=$obj["sta{$value}te"]; $b=$obj["sta{$value}te"]; $c=$obj["sta{$value}te"];',
        )
        self.write("slice.py", 'a=obj["state":]\nb=obj["state":]\nc=obj["state":]')
        secret = "private.person@example.invalid"
        self.write("private.py", f"a=obj[{secret!r}]\n" * 3)
        report = self.run_scan()
        indexed = [
            item for item in report["findings"] if item["kind"] == "repeated_string_key"
        ]
        self.assertEqual(len(indexed), 1)
        self.assertTrue(indexed[0]["redacted"])
        self.assertNotIn(secret, json.dumps(report))

    def test_short_index_fields_and_configured_threshold_are_per_file(self):
        self.write(
            "state.tsx", 'const a=obj["id"]; const b=other["id"]; const c=<div/>;'
        )
        self.write("other.py", 'a=obj["id"]\n')
        self.assertFalse(
            any(
                item["kind"] == "repeated_string_key"
                for item in self.run_scan()["findings"]
            )
        )
        report = self.run_scan("--min-occurrences", "2")
        indexed = [
            item for item in report["findings"] if item["kind"] == "repeated_string_key"
        ]
        self.assertEqual(len(indexed), 1)
        self.assertEqual(indexed[0]["index_evidence"]["path"], "state.tsx")
        self.assertEqual(indexed[0]["value"], "id")
        self.assertEqual(indexed[0]["counts"]["occurrences"], 2)

    def test_additional_grammars_skip_interpolation_and_unsupported_escape_forms(self):
        fixtures = {
            "state.java": 'class S { String a="\\u0071ueued"; String b="\\u0071ueued"; String c="\\u0071ueued"; }',
            "state.kt": 'val a="sta${value}te"\nval b="sta${value}te"\nval c="sta${value}te"',
            "state.cs": 'class S { string a=$"sta{value}te"; string b=$"sta{value}te"; string c=$"sta{value}te"; }',
            "state.swift": 'let a="sta\\(value)te"\nlet b="sta\\(value)te"\nlet c="sta\\(value)te"',
            "state.ex": 'a="sta#{value}te"\nb="sta#{value}te"\nc="sta#{value}te"',
            "state.sql": "SELECT 'sta\\nte', 'sta\\nte', 'sta\\nte';",
        }
        for name, source in fixtures.items():
            self.write(name, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(report["findings"], [])
        self.assertGreaterEqual(
            report["coverage"]["literal_skips"]["interpolated_or_unsupported"], 15
        )

    def test_sql_quotes_and_ruby_php_single_quote_escapes_are_not_js_escapes(self):
        self.write("state.sql", "SELECT 'it''s ready', 'it''s ready', 'it''s ready';")
        for extension, prefix in (("rb", ""), ("php", "<?php ")):
            self.write(
                f"state.{extension}",
                prefix
                + "a='literal\\nkey'; b='literal\\nkey'; c='literal\\nkey';".replace(
                    "a=", "$a=" if extension == "php" else "a="
                )
                .replace("b=", "$b=" if extension == "php" else "b=")
                .replace("c=", "$c=" if extension == "php" else "c="),
            )
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(self.finding(report, "it's ready")["counts"]["occurrences"], 3)
        self.assertEqual(
            self.finding(report, "literal\\nkey")["counts"]["occurrences"], 6
        )

    def test_unknown_explicit_grammar_is_reported_as_unavailable(self):
        self.write("state.custom", 'a="queued"\n' * 3)
        report = self.run_scan("--extension", ".custom=not_a_real_grammar")
        self.assertEqual(
            report["coverage"]["unsupported"],
            [{"path": "state.custom", "reason": "parser_unavailable"}],
        )

    def test_added_languages_preserve_sensitive_assignment_redaction(self):
        secret = "context-private-value"
        fixtures = {
            "state.java": f'class S {{ String api_key="{secret}"; }}',
            "state.kt": f'val api_key="{secret}"',
            "state.cs": f'class S {{ string api_key="{secret}"; }}',
            "state.swift": f'let api_key="{secret}"',
            "state.rb": f'api_key="{secret}"',
            "state.php": f'<?php $api_key="{secret}";',
            "state.ex": f'api_key="{secret}"',
        }
        for name, source in fixtures.items():
            value = secret + Path(name).suffix
            for index in range(3):
                self.write(f"part{index}/{name}", source.replace(secret, value))
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(len(report["findings"]), len(fixtures))
        self.assertTrue(all(item["redacted"] for item in report["findings"]))
        self.assertNotIn(secret, json.dumps(report))

    def test_exported_implementation_is_counted_but_module_specifiers_are_not(self):
        self.write(
            "options.ts",
            'export const GLOBAL="global"; export function mode(x:string){ return x === "global"; } export default {scope:"global"}; export {x} from "module-path"; export * from "module-path"; import x from "module-path";',
        )
        report = self.run_scan()
        found = self.finding(report, "global")
        self.assertEqual(found["counts"]["occurrences"], 3)
        self.assertEqual(
            [item["name"] for item in found["constant_candidates"]], ["GLOBAL"]
        )
        self.assertFalse(
            any(item["value"] == "module-path" for item in report["findings"])
        )

    def test_concat_keeps_occurrences_without_comparison_role_or_priority_boost(self):
        self.write(
            "state.js",
            'export function join(x){return "concat_state" + x}; export function eq(x){return x === "equal_state"};',
        )
        self.write(
            "state.go",
            'package state\nfunc Join(x string) string {return "concat_state" + x}; func Eq(x string) bool {return x == "equal_state"}',
        )
        self.write(
            "state.rs",
            'pub fn join(x:&str)->String {"concat_state".to_owned() + x} pub fn eq(x:&str)->bool {x == "equal_state"}',
        )
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        concat = self.finding(report, "concat_state")
        equality = self.finding(report, "equal_state")
        self.assertEqual(concat["counts"]["occurrences"], 3)
        self.assertTrue(
            all(item["role"] != "comparison" for item in concat["locations"])
        )
        self.assertEqual(concat["priority"], "low")
        self.assertTrue(
            all(item["role"] == "comparison" for item in equality["locations"])
        )
        self.assertEqual(equality["priority"], "high")

    def test_comments_docstrings_interpolation_ui_and_css_do_not_supply_fake_occurrences(
        self,
    ):
        self.write(
            "noise.py",
            '"queued"\n# "queued" "queued"\ntext=f"queued{value}"\nvalue="queued"\n',
        )
        self.write(
            "noise.tsx",
            '// "queued" "queued"\nconst text=`queued${value}`; const node=<div className="queued" title="queued">queued</div>; cn("queued"); const value="queued";',
        )
        report = self.run_scan()
        self.assertEqual(report["findings"], [])
        self.assertGreaterEqual(
            report["coverage"]["literal_skips"].get("interpolated_or_unsupported", 0), 2
        )

    def test_redacts_context_credentials_contact_details_and_no_raw_snippets(self):
        values = [
            "private-context-value",
            "sk-syntheticCredentialValue12345",
            "alice.private@example.invalid",
            "+52 55 1234 5678",
        ]
        for index, value in enumerate(values):
            key = "API_KEY" if index == 0 else "value"
            self.write(
                f"private{index}.py", "\n".join(f"{key} = {value!r}" for _ in range(3))
            )
        raw = self.run_scan(json_output=False).stdout
        report = self.run_scan()
        serialized = json.dumps(report)
        for value in values:
            self.assertNotIn(value, raw)
            self.assertNotIn(value, serialized)
        self.assertEqual(len(report["findings"]), 4)
        self.assertTrue(
            all(
                item["redacted"] and item["value"] == "<redacted>"
                for item in report["findings"]
            )
        )
        self.assertNotIn("snippet", serialized)

    def test_redacts_credential_url_queries_but_preserves_ordinary_query_parameters(
        self,
    ):
        secret = "syntheticQuerySecret"
        urls = [
            f"https://api.example.invalid/resource?{key}={secret}"
            for key in ("token", "api_key", "signature", "%74oken")
        ]
        for index, url in enumerate(urls):
            self.write(f"query{index}.py", f"url={url!r}\n" * 3)
        ordinary = "https://images.example.invalid/image?width=800&page=2&quality=90"
        self.write("ordinary.py", f"url={ordinary!r}\n" * 3)
        report = self.run_scan()
        rendered = self.run_scan(json_output=False).stdout
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, rendered)
        self.assertEqual(
            len([item for item in report["findings"] if item["redacted"]]), 4
        )
        self.assertEqual(self.finding(report, ordinary)["counts"]["occurrences"], 3)
        self.assertIn("width=800", rendered)
        self.assertIn("quality=90", rendered)

    def test_safe_fallback_honors_nested_gitignore_skips_links_secrets_generated_and_binary(
        self,
    ):
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/hidden.py", 'VALUE="excluded"\n' * 3)
        self.write("nested/.gitignore", "hidden.py\n")
        self.write("nested/hidden.py", 'VALUE="excluded"\n' * 3)
        self.write("vendor/third.py", 'VALUE="excluded"\n' * 3)
        self.write("generated/code.py", 'VALUE="excluded"\n' * 3)
        self.write("credentials.py", 'VALUE="excluded"\n' * 3)
        self.write("broken.py", 'VALUE="excluded"\x00')
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text('VALUE="excluded"\n' * 3)
        (self.root / "linked.py").symlink_to(outside)
        (self.root / "linked-dir").symlink_to(outside.parent, target_is_directory=True)
        self.write(
            "safe.py",
            'STATUS="ready"\nif value == "ready": pass\nif other == "ready": pass\n',
        )
        report = self.run_scan()
        self.assertEqual(
            [item["path"] for item in report["coverage"]["scanned"]], ["safe.py"]
        )
        self.assertEqual([item["value"] for item in report["findings"]], ["ready"])
        reasons = {item["reason"] for item in report["coverage"]["excluded"]}
        self.assertTrue(
            {
                "gitignored",
                "sensitive_filename",
                "symlink",
                "dependency_or_generated_directory",
                "binary_unreadable_or_nonutf8",
            }.issubset(reasons)
        )

    def test_git_inventory_includes_untracked_nonignored_and_reports_parse_and_unsupported_coverage(
        self,
    ):
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
        self.assertTrue(
            {"infra.tf", "config.json"}.issubset(
                {item["path"] for item in report["coverage"]["unsupported"]}
            )
        )
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
        self.write(
            "state.py",
            'QUEUED="queued"\nif state == "queued": pass\nif other == "queued": pass\n',
        )
        self.write("tests/test_state.py", 'EXPECTED="queued"\n' * 3)
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        first = self.run_scan()
        second = self.run_scan()
        self.assertEqual(first, second)
        self.assertEqual(self.finding(first, "queued")["counts"]["tests"], 0)
        included = self.run_scan("--include-tests")
        self.assertEqual(self.finding(included, "queued")["counts"]["tests"], 3)
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        denied = self.run_scan("--output", str(self.root / "report.json"), check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse((self.root / "report.json").exists())

    def test_config_controls_threshold_exclusions_and_explicit_parser_mapping(self):
        self.write("state.custom", 'STATUS="queued"\nif value == "queued": pass\n')
        self.write("omit.py", 'VALUE="omit-value"\n' * 3)
        config = Path(self.temp.name) / "config.json"
        config.write_text(
            json.dumps(
                {
                    "exclude": ["omit.py"],
                    "min_occurrences": 2,
                    "extensions": {".custom": "python"},
                }
            )
        )
        report = self.run_scan("--config", str(config))
        self.assertEqual(self.finding(report, "queued")["counts"]["occurrences"], 2)
        self.assertEqual(
            report["coverage"]["excluded"],
            [{"path": "omit.py", "reason": "configured_exclusion"}],
        )
        output = Path(self.temp.name) / "report.md"
        self.run_scan(
            "--config", str(config), "--output", str(output), json_output=False
        )
        self.assertIn("Repeated literal evidence", output.read_text())


if __name__ == "__main__":
    unittest.main()
