#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Black-box scanner tests; keep standalone and development parser pins aligned."""

import json
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

    def test_scanner_source_has_no_findings_and_is_fully_parsed(self):
        self.write("scanner.py", SCANNER.read_text())
        report = self.run_scan()
        self.assertEqual(report["findings"], [])
        coverage = report["coverage"]
        for category in ("excluded", "unsupported", "parse_errors"):
            self.assertEqual(coverage[category], [])
        self.assertEqual(len(coverage["scanned"]), 1)
        entry = coverage["scanned"][0]
        self.assertEqual(entry["path"], "scanner.py")
        self.assertEqual(entry["language"], "python")
        self.assertFalse(entry["test"])
        self.assertGreater(entry["literals"], 0)
        self.assertEqual(set(entry), {"path", "language", "test", "literals"})

    def test_skill_directory_self_scan_at_default_threshold(self):
        report = self.run_scan(root=SCANNER.parents[1])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["root"], str(SCANNER.parents[1]))
        coverage = report["coverage"]
        self.assertEqual(coverage["parse_errors"], [])
        self.assertEqual(len(coverage["scanned"]), 1)
        self.assertEqual(coverage["scanned"][0]["path"], "scripts/scan.py")
        self.assertEqual(coverage["scanned"][0]["language"], "python")
        self.assertGreater(coverage["scanned"][0]["literals"], 0)

    def test_installed_extension_grammars_parse_and_extract_static_strings(self):
        fixtures = {
            "shell.custom": ("bash", 'state="queued"\n' * 3),
            "native.custom": ("c", 'const char *a="queued", *b="queued", *c="queued";'),
            "python.custom": ("python", 'state="queued"\n' * 3),
        }
        for filename, (language, source) in fixtures.items():
            with self.subTest(language=language):
                self.write(f"{language}/{filename}", source)
                report = self.run_scan("--extension", f".custom={language}", root=self.root / language)
                coverage = report["coverage"]
                self.assertEqual(coverage["unsupported"], [])
                self.assertEqual(coverage["parse_errors"], [])
                self.assertEqual(coverage["scanned"][0]["language"], language)
                finding = self.finding(report, "queued")
                self.assertEqual(finding["counts"]["occurrences"], 3)
                self.assertEqual({item["language"] for item in finding["locations"]}, {language})

    def test_cli_root_coverage_directory_and_output_format(self):
        self.write("coverage/hidden.py", 'state="hidden"\n' * 3)
        self.write("live.py", 'state="queued"\n' * 3)
        report = self.run_scan()
        self.assertEqual(report["root"], str(self.root.resolve()))
        self.assertEqual([item["path"] for item in report["coverage"]["scanned"]], ["live.py"])
        self.assertEqual(report["coverage"]["excluded"], [{
            "path": "coverage", "reason": "dependency_or_generated_directory",
        }])
        rendered = self.run_scan(json_output=False).stdout
        self.assertIn(f"Root: {self.root.resolve()}", rendered)
        self.assertIn("Coverage: 1 parsed files; 1 excluded", rendered)
        invalid = self.run_scan("--format", "yaml", check=False)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("--format", invalid.stderr)
        self.assertIn("yaml", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)

    def test_lower_threshold_reveals_scanner_candidates(self):
        report = self.run_scan("--min-occurrences", "2", root=SCANNER.parents[1])
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertTrue(report["findings"])

    def test_imported_environment_lookup_is_not_resolved(self):
        self.write("environment.py", 'from os import environ\n' + 'value=environ["SERVICE_MODE"]\n' * 3)
        report = self.run_scan()
        indexed = [item for item in report["findings"] if item["kind"] == "repeated_string_key"]
        self.assertEqual([item["value"] for item in indexed], ["SERVICE_MODE"])
        self.assertEqual(report["coverage"]["parse_errors"], [])

    def test_sql_dollar_quote_parse_gap_is_reported(self):
        self.write("body.sql", "DO $$ BEGIN RAISE NOTICE 'queued'; END $$;")
        report = self.run_scan()
        self.assertEqual(report["coverage"]["scanned"], [])
        self.assertEqual([item["path"] for item in report["coverage"]["parse_errors"]], ["body.sql"])
        rendered = self.run_scan(json_output=False).stdout
        self.assertIn("parse_error", rendered)

    def test_java_kotlin_and_csharp_file_fixtures_parse(self):
        fixture_dir = Path(__file__).parent / "fixtures/anti-strings-realistic"
        fixtures = {"Job.java": "java", "Job.kt": "kotlin", "Job.cs": "csharp"}
        for name in fixtures:
            self.write(name, (fixture_dir / name).read_text())
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(report["coverage"]["unsupported"], [])
        self.assertEqual({item["path"]: item["language"] for item in report["coverage"]["scanned"]}, fixtures)
        self.assertEqual(self.finding(report, "queued")["counts"]["occurrences"], 9)

    def test_quiet_calls_preserve_indexed_keys_but_skip_display_text(self):
        self.write(
            "state.py",
            'print(row["state"])\nlog.info(row["state"])\nprint(row["state"])\n'
            + 'print("display text")\n' * 3,
        )
        self.write(
            "state.js",
            'console.log(row["state"]); logger.info(row["state"]); '
            'console.warn(row["state"]); console.log("display text");'
            'console.log("display text"); console.log("display text");',
        )
        report = self.run_scan()
        indexed = [item for item in report["findings"]
                   if item["kind"] == "repeated_string_key"]
        self.assertEqual({item["index_evidence"]["path"] for item in indexed},
                         {"state.py", "state.js"})
        self.assertTrue(all(item["counts"]["occurrences"] == 3 for item in indexed))
        self.assertNotIn("display text", {item["value"] for item in report["findings"]})

    def test_sql_numeric_literals_do_not_count_as_skipped_strings(self):
        self.write("state.sql", "SELECT 1, 2.5, -3, TRUE, NULL, 'queued', 'queued', 'queued';")
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(report["coverage"]["literal_skips"], {})
        self.assertEqual(self.finding(report, "queued")["counts"]["occurrences"], 3)

    def test_kotlin_when_branches_parse_and_extract_strings(self):
        fixtures = {
            "enum.kt": (
                'enum class State { QUEUED, RUNNING, DONE }\n'
                'fun label(state: State): String = when (state) {\n'
                '    State.QUEUED -> "queued"\n'
                '    State.RUNNING -> "queued"\n'
                '    State.DONE -> "queued"\n'
                '}\n'
            ),
            "compact.kt": 'fun next(s: String) = when (s) { "queued" -> "running"; "running" -> "done"; else -> "queued" }',
            "multiline.kt": 'fun next(s: String) = when (s) {\n "queued" -> "running"\n "running" -> "done"\n else -> "queued"\n}',
            "subject.kt": 'fun next(s: String) = when (val state = s) { "queued" -> "running"; else -> "queued" }',
            "condition.kt": 'fun next(s: String) = when { s == "queued" -> "running"; else -> "queued" }',
        }
        for path, source in fixtures.items():
            self.write(path, source)
        report = self.run_scan()
        coverage = report["coverage"]
        self.assertEqual(coverage["parse_errors"], [])
        self.assertEqual(coverage["unsupported"], [])
        self.assertEqual({item["path"] for item in coverage["scanned"]}, set(fixtures))
        self.assertTrue(all(item["language"] == "kotlin" for item in coverage["scanned"]))
        self.assertEqual(self.finding(report, "queued")["counts"]["occurrences"], 11)

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

    def test_realistic_swift_elixir_and_sql_fixtures(self):
        fixture_dir = Path(__file__).parent / "fixtures/anti-strings-realistic"
        for filename, language, queued_count in (
            ("job.swift", "swift", 3),
            ("job.ex", "elixir", 3),
            ("jobs.sql", "sql", 4),
        ):
            with self.subTest(language=language):
                self.write(f"{language}/{filename}", (fixture_dir / filename).read_text())
                report = self.run_scan(root=self.root / language)
                coverage = report["coverage"]
                self.assertEqual(coverage["parse_errors"], [])
                self.assertEqual(coverage["unsupported"], [])
                self.assertEqual(coverage["excluded"], [])
                self.assertEqual(len(coverage["scanned"]), 1)
                self.assertEqual(coverage["scanned"][0]["language"], language)
                finding = self.finding(report, "queued")
                self.assertEqual(finding["counts"]["occurrences"], queued_count)
                self.assertEqual({item["path"] for item in finding["locations"]}, {filename})
                if language == "sql":
                    self.assertEqual(coverage["literal_skips"], {})

    def test_repeated_key_index_is_an_explicit_per_file_heuristic(self):
        fixtures = {
            "state.py": 'a=obj["state"]\nb=other["state"]\nc=obj["state"]',
            "state.js": 'a=obj["state"]; b=other[`state`]; c=obj["st\\x61te"];',
            "state.ts": 'const a=obj["state"]; const b=other["state"]; const c=obj["state"];',
            "state.go": 'package state\nfunc f(){a:=obj["state"]; b:=other["state"]; c:=obj["state"]}',
            "state.rb": 'a=obj["state"]\nb=other[\'state\']\nc=obj["state"]',
            "state.php": '<?php $a=$obj["state"]; $b=$other[\'state\']; $c=$obj["state"];',
        }
        for name, source in fixtures.items(): self.write(name, source)
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

    def test_nonrecord_receivers_and_counter_targets_are_not_findings(self):
        fixtures = {
            "counter.py": (
                'a=os.environ["environment_name"]\n'
                'b=sys.modules["module_registry"]\n'
                'counts["counter_total"] += 1\n'
                'counts["counter_group"]["counter_nested"] -= 1\n'
            ) * 3,
            "counter.ts": (
                'const a=process.env["environment_name"]; '
                'counts["counter_total"] += 1; counts["counter_increment"]++; '
                '--counts["counter_decrement"]; '
                'counts["counter_group"]["counter_nested"] *= 2;\n'
            ) * 3,
            "counter.go": 'package state\nfunc f(){\n' + (
                'counts["counter_total"] += 1; counts["counter_increment"]++; '
                'counts["counter_decrement"]--; '
                'counts["counter_group"]["counter_nested"] *= 2\n'
            ) * 3 + '}\n',
        }
        for path, source in fixtures.items():
            self.write(path, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertEqual(report["findings"], [])

    def test_counter_exclusions_preserve_rhs_and_computed_key_record_reads(self):
        fixtures = {
            "counter.py": (
                'counts["counter_total"] += row["record_state"]\n'
                'counts[row["record_field"]] += 1\n'
                'a=custom.env["custom_field"]\n'
                'counts["ordinary_field"] = row["ordinary_field"]\n'
            ) * 3,
            "counter.ts": (
                'counts["counter_total"] += row["record_state"]; '
                'counts[row["record_field"]]++; '
                'const a=custom.env["custom_field"]; '
                'counts["ordinary_field"] = row["ordinary_field"];\n'
            ) * 3,
            "counter.go": 'package state\nfunc f(){\n' + (
                'counts["counter_total"] += row["record_state"]; '
                'counts[row["record_field"]]++; '
                'a:=custom.env["custom_field"]; '
                'counts["ordinary_field"] = row["ordinary_field"]\n'
            ) * 3 + '}\n',
        }
        for path, source in fixtures.items():
            self.write(path, source)
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        indexed = [item for item in report["findings"]
                   if item["kind"] == "repeated_string_key"]
        self.assertEqual(len(indexed), 12)
        self.assertEqual({item["value"] for item in indexed},
                         {"record_state", "record_field", "custom_field", "ordinary_field"})
        self.assertTrue(all(item["counts"]["occurrences"] ==
                            (6 if item["value"] == "ordinary_field" else 3)
                            for item in indexed))
        self.assertNotIn("counter_total", {item["value"] for item in report["findings"]})

    def test_typescript_type_indexes_do_not_count_as_literal_or_runtime_keys(self):
        self.write(
            "types.ts",
            'type Fields = [Foo["type_only_field"], Foo["type_only_field"], Foo["type_only_field"]];\n'
            'type Mixed = [Foo["runtime_field"], Foo["runtime_field"], Foo["runtime_field"]];\n'
            + 'const value = row["runtime_field"];\n' * 3,
        )
        report = self.run_scan()
        self.assertEqual(report["coverage"]["parse_errors"], [])
        self.assertNotIn("type_only_field", {item["value"] for item in report["findings"]})
        findings = [item for item in report["findings"] if item["value"] == "runtime_field"]
        self.assertEqual({item["kind"] for item in findings},
                         {"repeated_literal", "repeated_string_key"})
        self.assertTrue(all(item["counts"]["occurrences"] == 3 for item in findings))

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
        for name, source in fixtures.items(): self.write(name, source)
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

    def test_line_block_and_multiline_comments_do_not_supply_occurrences(self):
        fixtures = {
            "state.py": '# "queued" "queued" "queued"\nstate="queued"\n',
            "state.java": 'class State { // "queued" "queued"\n/* "queued" "queued" */ String state="queued"; }',
            "state.kt": '// "queued" "queued"\n/* "queued"\n"queued" */\nval state="queued"',
            "state.swift": '// "queued" "queued"\n/* "queued"\n"queued" */\nlet state="queued"',
            "state.sql": "-- 'queued' 'queued'\n/* 'queued'\n'queued' */\nSELECT 'queued';",
        }
        for filename, source in fixtures.items():
            with self.subTest(filename=filename):
                self.write(f"{filename}/{filename}", source)
                report = self.run_scan(root=self.root / filename)
                self.assertEqual(report["coverage"]["parse_errors"], [])
                self.assertEqual(report["coverage"]["unsupported"], [])
                self.assertEqual(report["coverage"]["scanned"][0]["literals"], 1)
                self.assertEqual(report["findings"], [])

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
