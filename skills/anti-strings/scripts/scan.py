#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["tree-sitter-language-pack==0.13.0", "tree-sitter==0.25.2", "pathspec==0.12.1"]
# ///
"""Read-only AST evidence for repeated literals; priority is not a semantic verdict."""
import argparse
import ast
from collections import defaultdict
import fnmatch
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import parse_qsl, urlsplit

from pathspec import GitIgnoreSpec
from tree_sitter_language_pack import get_parser

LANGUAGES = {".py": "python", ".go": "go", ".rs": "rust", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx"}
LITERALS = {"string", "concatenated_string", "template_string", "string_literal", "raw_string_literal", "interpreted_string_literal"}
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor", "dist", "build", "target", "coverage", ".next", ".nuxt", ".output", "__pycache__", ".cache", ".terraform", ".aws", ".ssh", ".gnupg", ".secrets", ".doppler", "generated", "gen"}
SECRET_NAMES = re.compile(r"(^\.env($|\.)|credentials|secrets?($|[._-])|(^|[._-])(id_rsa|id_ed25519)|\.(pem|key|p12|pfx|keystore)$)", re.I)
SECRET_CONTEXT = re.compile(r"password|passwd|secret|token|credential|private_?key|api_?key|access_?key|authorization|connection_?string|(^|_)dsn($|_)", re.I)
SECRET_QUERY = re.compile(r"token|api_?key|signature|credential|password|secret|authorization|access_?key|(^|_)(auth|sig|key)($|_)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOKEN = re.compile(r"(?:\b(?:sk-|ghp_|gho_|github_pat_|AKIA|ASIA)[A-Za-z0-9_/-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN .*PRIVATE KEY-----|://[^/\s:@]+:[^/\s@]+@)")
PRESENTATION = {"class", "classname", "style", "title", "alt", "placeholder", "arialabel", "label", "description", "children", "href", "src"}
QUIET_CALLS = {"print", "println", "printf", "sprintf", "log", "warn", "debug", "info", "error", "clsx", "cn", "classnames", "cva", "tw"}
SEMANTIC = {"state", "status", "kind", "type", "mode", "event", "protocol", "method", "operation", "scope", "role", "provider", "format", "encoding", "config", "key", "name"}


def sensitive(value, context=""):
    reasons = []
    if SECRET_CONTEXT.search(re.sub(r"[^a-zA-Z0-9_]", "", context)):
        reasons.append("sensitive_context")
    if TOKEN.search(value) or re.fullmatch(r"[A-Fa-f0-9]{24,}", value) or (len(value) >= 32 and re.fullmatch(r"[A-Za-z0-9_+/=-]+", value) and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) and re.search(r"\d", value)):
        reasons.append("credential_like")
    if "?" in value:
        try:
            query = urlsplit(value).query
        except ValueError:
            query = value.partition("?")[2].partition("#")[0]
        try:
            parameters = parse_qsl(query, keep_blank_values=True, max_num_fields=256)
            if any(SECRET_QUERY.search(re.sub(r"[^a-zA-Z0-9_]", "_", key)) for key, _ in parameters):
                reasons.append("credential_query")
        except ValueError:
            reasons.append("unparsed_url_query")
    if EMAIL.search(value):
        reasons.append("email")
    if re.fullmatch(r"[+()\d .-]{7,}", value) and len(re.sub(r"\D", "", value)) >= 7:
        reasons.append("phone_like")
    return reasons


def public_path(path):
    return "/".join("<redacted-name>" if sensitive(part) else re.sub(r"[\x00-\x1f\x7f-\x9f]", "<control>", part) for part in str(path).replace(os.sep, "/").split("/"))


def excluded(path, patterns):
    parts = path.parts
    if any(part in SKIP_DIRS for part in parts): return "dependency_or_generated_directory"
    if any(SECRET_NAMES.search(part) for part in parts): return "sensitive_filename"
    if path.name.endswith((".lock", ".min.js", ".min.css", ".map", ".generated.ts", ".generated.js", ".pb.go", ".sum")) or path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "Cargo.lock", "poetry.lock", "uv.lock"}: return "lock_or_generated_file"
    if any(fnmatch.fnmatch(path.as_posix(), pattern) for pattern in patterns): return "configured_exclusion"
    return None


def test_path(path):
    return any(part.lower() in {"test", "tests", "__tests__", "fixtures", "__fixtures__", "snapshots"} for part in path.parts) or bool(re.search(r"(^test_|_test\.|\.(test|spec)\.)", path.name))


def safe_file(root, relative):
    if relative.is_absolute(): return False
    current = root
    for part in relative.parts:
        if part in ("", ".", ".."): return False
        current /= part
        try:
            if current.is_symlink(): return False
        except OSError: return False
    return current.is_file()


def read_source(root, relative, maximum):
    """Open each component without following links; reject devices/FIFOs too."""
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    descriptor = None
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            os.close(directory); directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode): raise OSError("nonregular")
        if metadata.st_size > maximum: return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return stream.read(maximum + 1)
    finally:
        if descriptor is not None: os.close(descriptor)
        os.close(directory)


def inventory(root, patterns, coverage):
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired): result = None
    if result is not None and result.returncode == 0:
        coverage["discovery"] = "git"
        return sorted({Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}, key=lambda path: path.as_posix())
    coverage["discovery"] = "filesystem"
    coverage["warnings"].append("Git inventory unavailable; filesystem traversal honors nested .gitignore files but cannot apply Git global/info excludes.")
    paths = []
    def walk(directory, rules):
        ignore = directory / ".gitignore"
        if ignore.is_file() and not ignore.is_symlink() and ignore.stat().st_size <= 65536:
            try: rules = [*rules, (directory, GitIgnoreSpec.from_lines(ignore.read_text().splitlines()))]
            except (OSError, UnicodeError): coverage["warnings"].append("An unreadable .gitignore reduced filesystem ignore coverage.")
        try: entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            coverage["excluded"].append({"path": public_path(directory.relative_to(root)), "reason": "unreadable_directory"}); return
        for entry in entries:
            relative = entry.relative_to(root)
            reason = excluded(relative, patterns)
            if reason or entry.is_symlink():
                coverage["excluded"].append({"path": public_path(relative), "reason": reason or "symlink"}); continue
            ignored = False
            for base, spec in rules:
                candidate = entry.relative_to(base).as_posix() + ("/" if entry.is_dir() else "")
                for pattern in spec.patterns:
                    if pattern.include is not None and pattern.match_file(candidate): ignored = pattern.include
            if ignored:
                coverage["excluded"].append({"path": public_path(relative), "reason": "gitignored"}); continue
            if entry.is_dir(): walk(entry, rules)
            elif entry.is_file(): paths.append(relative)
    walk(root, [])
    return paths


def character_column(source, offset):
    start = source.rfind(b"\n", 0, offset) + 1
    return len(source[start:offset].decode("utf-8")) + 1


def ancestors(node, limit=9):
    result = []
    while node.parent is not None and len(result) < limit:
        node = node.parent; result.append(node)
    return result


def text(node, source):
    return source[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""


def field(node, name, source):
    return text(node.child_by_field_name(name), source)


def decode_escapes(body, language):
    # Go byte escapes belong to a UTF-8 byte string, unlike Unicode escapes.
    out = bytearray() if language == "go" else []
    def emit(value, byte=False):
        if language == "go": out.extend(bytes([value]) if byte else value.encode("utf-8"))
        else: out.append(value)
    i = 0
    while i < len(body):
        if body[i] != "\\": emit(body[i]); i += 1; continue
        i += 1
        if i >= len(body): raise ValueError("trailing escape")
        char = body[i]; i += 1
        if char in "\r\n":
            if char == "\r" and i < len(body) and body[i] == "\n": i += 1
            if language == "rust":
                while i < len(body) and body[i].isspace(): i += 1
            continue
        escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a", "\\": "\\", '"': '"', "'": "'", "`": "`"}
        if char in escapes: emit(escapes[char]); continue
        if char == "u" and i < len(body) and body[i] == "{":
            end = body.index("}", i); emit(chr(int(body[i+1:end].replace("_", ""), 16))); i = end + 1; continue
        if char in ("x", "u", "U"):
            length = {"x": 2, "u": 4, "U": 8}[char]; code = int(body[i:i+length], 16)
            if len(body[i:i+length]) != length: raise ValueError("short escape")
            emit(code if language == "go" and char == "x" else chr(code), language == "go" and char == "x"); i += length; continue
        if char in "01234567":
            digits = char
            while i < len(body) and len(digits) < 3 and body[i] in "01234567": digits += body[i]; i += 1
            code = int(digits, 8); emit(code if language == "go" else chr(code), language == "go"); continue
        if language in {"javascript", "typescript", "tsx"}: emit(char)
        else: raise ValueError("unsupported escape")
    value = out.decode("utf-8") if language == "go" else "".join(out)
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value): value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    return value


def normalize(node, source, language):
    literal = text(node, source)
    if any(child.type in {"interpolation", "template_substitution", "string_interpolation", "interpolation_expression"} for child in descendants(node)): return None
    if language == "python":
        value = ast.literal_eval(literal)
        return value if isinstance(value, str) else None
    if language == "rust":
        raw = re.fullmatch(r'r(#{0,255})"([\s\S]*)"\1', literal)
        if raw: return raw[2]
        if not literal.startswith('"'): return None  # Byte/C strings have different semantics.
    if language == "go" and literal.startswith("`"): return literal[1:-1].replace("\r", "")
    if literal[:1] in {'"', "'", "`"} and literal[-1:] == literal[:1]: return decode_escapes(literal[1:-1], language)
    return None


def descendants(node):
    stack = list(reversed(node.named_children))
    while stack:
        item = stack.pop(); yield item; stack.extend(reversed(item.named_children))


def direct_value(owner, node):
    value = owner.child_by_field_name("value") or owner.child_by_field_name("right")
    while value is not None and value.type in {"expression_list", "parenthesized_expression", "as_expression", "satisfies_expression", "literal_type"} and value.named_children:
        value = value.named_children[0]
    return value == node


def evidence(node, source, language):
    chain = ancestors(node)
    parent = node.parent
    kinds = {item.type for item in chain}
    if any(item.type in {"import_statement", "export_statement"} and item.child_by_field_name("source") == node for item in chain): return None
    if kinds & {"use_declaration", "extern_crate_declaration"}: return None
    if language == "python" and parent:
        expression = parent if parent.type == "expression_statement" else node
        container = expression.parent
        if container and container.type in {"module", "block"} and container.named_children and container.named_children[0] == expression and (container.type == "module" or container.parent and container.parent.type in {"function_definition", "class_definition"}): return None
    key = ""; candidate = None; contexts = []
    for item in chain:
        if item.type in {"pair", "key_value", "field_initializer", "jsx_attribute"}:
            key = field(item, "key", source) or field(item, "name", source) or text(item.named_children[0], source)
            contexts.append(key)
            if re.sub(r"[^a-z]", "", key.lower()) in PRESENTATION: return None
        if item.type in {"call_expression", "call"}:
            function = field(item, "function", source).lower()
            if function.rsplit(".", 1)[-1].replace("!", "") in QUIET_CALLS or function in {"require", "import"}: return None
            contexts.append(function)
            key = key or function
        if item.type in {"assignment", "annotated_assignment", "variable_declarator", "const_spec", "const_item", "static_item", "enum_assignment"}:
            name = field(item, "left", source) or field(item, "name", source)
            contexts.append(name)
            key = key or name
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                if direct_value(item, node) and (item.type in {"const_spec", "const_item", "static_item", "enum_assignment"} or re.fullmatch(r"[A-Z][A-Z0-9_]+", name)):
                    candidate = {"name": name, "style": "enum_member" if item.type == "enum_assignment" else "constant"}
    comparison = bool(kinds & {"comparison_operator", "case_clause", "switch_case", "match_arm", "literal_pattern"}) or any(item.type == "binary_expression" and field(item, "operator", source) in {"==", "!=", "===", "!==", "<", ">", "<=", ">=", "in", "instanceof"} for item in chain)
    role = "constant_definition" if candidate else "comparison" if comparison else "mapping_key" if parent and parent.type in {"pair", "key_value"} and parent.child_by_field_name("key") == node else "configuration" if re.sub(r"[^a-z]", "", key.lower()) in SEMANTIC else "argument" if kinds & {"arguments", "argument_list"} else "assignment" if key else "literal"
    inline_test = any(item.type in {"function_definition", "function_item", "mod_item"} and (field(item, "name", source).startswith("test_") or field(item, "name", source) == "tests") for item in chain)
    return role, parent.type if parent else "root", " ".join(contexts), candidate, inline_test


def scan(root, options):
    coverage = {"discovery": "", "scanned": [], "excluded": [], "unsupported": [], "parse_errors": [], "warnings": [], "literal_skips": {}}
    groups = defaultdict(list); parsers = {}
    for relative in inventory(root, options["exclude"], coverage):
        location = public_path(relative)
        reason = excluded(relative, options["exclude"])
        if reason or not safe_file(root, relative):
            coverage["excluded"].append({"path": location, "reason": reason or "symlink_or_nonregular"}); continue
        is_test = test_path(relative)
        if is_test and not options["include_tests"]:
            coverage["excluded"].append({"path": location, "reason": "test_file"}); continue
        language = options["extensions"].get(relative.suffix.lower())
        if not language:
            coverage["unsupported"].append({"path": location, "reason": "unmapped_extension"}); continue
        try:
            source = read_source(root, relative, options["max_file_bytes"])
            if source is None:
                coverage["excluded"].append({"path": location, "reason": "file_too_large"}); continue
            if len(source) > options["max_file_bytes"]: raise ValueError("size changed")
            if b"\0" in source: raise UnicodeError("binary")
            source.decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            coverage["excluded"].append({"path": location, "reason": "binary_unreadable_or_nonutf8"}); continue
        if re.search(rb'(?im)^\s*(?://|#|/\*|\*)[^\n]*(?:@generated|code generated|auto.generated|do not edit)', source[:4096]):
            coverage["excluded"].append({"path": location, "reason": "generated_header"}); continue
        try:
            if language not in parsers: parsers[language] = get_parser(language)
            tree = parsers[language].parse(source)
        except Exception:
            coverage["unsupported"].append({"path": location, "reason": "parser_unavailable"}); continue
        if tree.root_node.has_error:
            error = next((item for item in descendants(tree.root_node) if item.type == "ERROR" or item.is_missing), tree.root_node)
            coverage["parse_errors"].append({"path": location, "language": language, "line": error.start_point.row + 1, "column": character_column(source, error.start_byte)}); continue
        literals = 0; stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if "comment" in node.type: continue
            if node.type not in LITERALS:
                stack.extend(reversed(node.named_children)); continue
            try: value = normalize(node, source, language)
            except (ValueError, SyntaxError, UnicodeError): value = None
            if value is None:
                coverage["literal_skips"]["interpolated_or_unsupported"] = coverage["literal_skips"].get("interpolated_or_unsupported", 0) + 1; continue
            item = evidence(node, source, language)
            if item is None: continue
            role, context, key, candidate, inline_test = item
            occurrence_test = is_test or inline_test
            if occurrence_test and not options["include_tests"]: continue
            reasons = sensitive(value, key)
            if not reasons and (len(value) < 3 or len(value) > 160 or "\n" in value or "\r" in value or len(value.split()) > 4): continue
            if not value.strip(): continue
            column = character_column(source, node.start_byte)
            entry = {"path": location, "line": node.start_point.row + 1, "column": column, "language": language, "role": role, "context": context}
            groups[value].append((entry, occurrence_test, reasons, candidate)); literals += 1
        coverage["scanned"].append({"path": location, "language": language, "test": is_test, "literals": literals})
    findings = []
    for value, occurrences in groups.items():
        if len(occurrences) < options["min_occurrences"]: continue
        locations = sorted([item[0] for item in occurrences], key=lambda item: (item["path"], item["line"], item["column"]))
        production = sum(not item[1] for item in occurrences); files = len({item["path"] for item in locations})
        reasons = sorted({reason for item in occurrences for reason in item[2]})
        semantic = sum(item[0]["role"] in {"comparison", "configuration", "mapping_key"} for item in occurrences)
        priority = "high" if production >= 3 and files >= 2 and semantic >= 2 else "medium" if production >= 3 and semantic else "low"
        candidates = [{**item[0], **item[3]} for item in occurrences if item[3]]
        findings.append({"value": "<redacted>" if reasons else value, "redacted": bool(reasons), "redaction_reasons": reasons, "priority": priority,
                         "counts": {"occurrences": len(occurrences), "files": files, "production": production, "tests": len(occurrences) - production},
                         "locations": locations, "constant_candidates": candidates})
    findings.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item["priority"]], item["locations"][0]["path"], item["locations"][0]["line"], item["locations"][0]["column"], item["value"]))
    for key in ("scanned", "excluded", "unsupported", "parse_errors"): coverage[key].sort(key=lambda item: (item["path"], item.get("reason", "")))
    coverage["warnings"] = sorted(set(coverage["warnings"]))
    return {"schema_version": 1, "root": public_path(root), "heuristic_notice": "Repeated syntax is evidence for review, not proof of shared domain meaning or a mandate to create constants.", "coverage": coverage, "findings": findings}


def markdown(report):
    def safe(value): return html.escape(str(value)).replace("|", "&#124;").replace("`", "&#96;")
    coverage = report["coverage"]
    lines = ["# Repeated literal evidence", "", report["heuristic_notice"], "", f"Root: {safe(report['root'])}", f"Coverage: {len(coverage['scanned'])} parsed files; {len(coverage['excluded'])} excluded; {len(coverage['unsupported'])} unsupported; {len(coverage['parse_errors'])} parse errors.", ""]
    for finding in report["findings"]:
        lines += [f"## {finding['priority'].title()} · {safe(json.dumps(finding['value'], ensure_ascii=True))}", f"{finding['counts']['occurrences']} occurrences in {finding['counts']['files']} files; {finding['counts']['production']} production / {finding['counts']['tests']} test locations.", ""]
        for location in finding["locations"]: lines.append(f"- {safe(location['path'])}:{location['line']}:{location['column']} — {location['role']} ({location['context']})")
        for candidate in finding["constant_candidates"]: lines.append(f"- Existing definition candidate: {safe(candidate['name'])} at {safe(candidate['path'])}:{candidate['line']} ({candidate['style']})")
        if finding["redacted"]: lines.append("- Value redacted: " + ", ".join(finding["redaction_reasons"]))
        lines.append("")
    if not report["findings"]: lines += ["No repeated candidates met the configured threshold.", ""]
    lines += ["## Coverage details", ""]
    for kind in ("excluded", "unsupported", "parse_errors"):
        for entry in coverage[kind]: lines.append(f"- {kind}: {safe(entry['path'])} — {entry.get('reason', 'parse_error')}")
    lines += [f"- {safe(warning)}" for warning in coverage["warnings"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path, help="Write a report outside the scanned root; stdout is the default")
    parser.add_argument("--include-tests", action="store_true", default=None)
    parser.add_argument("--config", type=Path, help="JSON: exclude, include_tests, min_occurrences, max_file_bytes, extensions")
    parser.add_argument("--min-occurrences", type=int)
    parser.add_argument("--extension", action="append", default=[], metavar=".EXT=GRAMMAR")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir(): parser.error("ROOT must be an existing directory")
    options = {"exclude": [], "include_tests": False, "min_occurrences": 3, "max_file_bytes": 1024 * 1024, "extensions": dict(LANGUAGES)}
    if args.config:
        try: config = json.loads(args.config.read_text())
        except (OSError, ValueError): parser.error("Cannot read JSON configuration")
        if not isinstance(config, dict) or set(config) - set(options): parser.error("Unknown configuration keys")
        options.update({key: value for key, value in config.items() if key != "extensions"})
        if "extensions" in config:
            if not isinstance(config["extensions"], dict): parser.error("extensions must be an object")
            options["extensions"].update(config["extensions"])
    for item in args.extension:
        if "=" not in item: parser.error("--extension expects .EXT=GRAMMAR")
        suffix, language = item.split("=", 1); options["extensions"][suffix] = language
    if args.include_tests is not None: options["include_tests"] = args.include_tests
    if args.min_occurrences is not None: options["min_occurrences"] = args.min_occurrences
    if not isinstance(options["include_tests"], bool) or not isinstance(options["exclude"], list) or not all(isinstance(item, str) for item in options["exclude"]): parser.error("Invalid exclusion/test configuration")
    if any(not isinstance(options[key], int) or isinstance(options[key], bool) or options[key] < (2 if key == "min_occurrences" else 1) for key in ("min_occurrences", "max_file_bytes")): parser.error("Invalid occurrence threshold or file size")
    if not all(isinstance(key, str) and key.startswith(".") and isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value) for key, value in options["extensions"].items()): parser.error("Invalid extension/parser mapping")
    output = args.output.expanduser().resolve() if args.output else None
    if output and output.is_relative_to(root): parser.error("Reports must be written outside ROOT to keep the target read-only")
    report = scan(root, options)
    rendered = json.dumps(report, indent=2, ensure_ascii=True) + "\n" if args.format == "json" else markdown(report)
    if output:
        with output.open("w", encoding="utf-8") as stream: stream.write(rendered)
    else: sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
