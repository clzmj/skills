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
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import TypeGuard, get_args
from urllib.parse import parse_qsl, urlsplit

from pathspec import GitIgnoreSpec
from tree_sitter_language_pack import SupportedLanguage, get_parser


class NodeType(StrEnum):
    ERROR = 'ERROR'
    COMMENT = 'comment'
    ANNOTATED_ASSIGNMENT = 'annotated_assignment'
    ARGUMENT_LIST = 'argument_list'
    ARGUMENTS = 'arguments'
    AS_EXPRESSION = 'as_expression'
    ASSIGNMENT = 'assignment'
    ASSIGNMENT_EXPRESSION = 'assignment_expression'
    ASSIGNMENT_STATEMENT = 'assignment_statement'
    AUGMENTED_ASSIGNMENT = 'augmented_assignment'
    AUGMENTED_ASSIGNMENT_EXPRESSION = 'augmented_assignment_expression'
    BINARY_EXPRESSION = 'binary_expression'
    BINARY_OPERATOR = 'binary_operator'
    BLOCK = 'block'
    CALL = 'call'
    CALL_EXPRESSION = 'call_expression'
    CASE_CLAUSE = 'case_clause'
    CLASS_DEFINITION = 'class_definition'
    COMPARISON_OPERATOR = 'comparison_operator'
    CONCATENATED_STRING = 'concatenated_string'
    CONST_ITEM = 'const_item'
    CONST_SPEC = 'const_spec'
    DEC_STATEMENT = 'dec_statement'
    ELEMENT_REFERENCE = 'element_reference'
    ENCAPSED_STRING = 'encapsed_string'
    ENUM_ASSIGNMENT = 'enum_assignment'
    EXPORT_STATEMENT = 'export_statement'
    EXPRESSION_LIST = 'expression_list'
    EXPRESSION_STATEMENT = 'expression_statement'
    EXTERN_CRATE_DECLARATION = 'extern_crate_declaration'
    FIELD_INITIALIZER = 'field_initializer'
    FUNCTION_DEFINITION = 'function_definition'
    FUNCTION_ITEM = 'function_item'
    IMPORT_STATEMENT = 'import_statement'
    INC_STATEMENT = 'inc_statement'
    INDEX_EXPRESSION = 'index_expression'
    INTERPOLATED_EXPRESSION = 'interpolated_expression'
    INTERPOLATION = 'interpolation'
    INTERPOLATION_EXPRESSION = 'interpolation_expression'
    INTERPRETED_STRING_LITERAL = 'interpreted_string_literal'
    JSX_ATTRIBUTE = 'jsx_attribute'
    KEY_VALUE = 'key_value'
    KEYWORD_FALSE = 'keyword_false'
    KEYWORD_NULL = 'keyword_null'
    KEYWORD_TRUE = 'keyword_true'
    LINE_STRING_LITERAL = 'line_string_literal'
    LITERAL = 'literal'
    LITERAL_PATTERN = 'literal_pattern'
    LITERAL_TYPE = 'literal_type'
    LOOKUP_TYPE = 'lookup_type'
    MATCH_ARM = 'match_arm'
    MOD_ITEM = 'mod_item'
    MODULE = 'module'
    NAVIGATION_EXPRESSION = 'navigation_expression'
    PAIR = 'pair'
    PARENTHESIZED_EXPRESSION = 'parenthesized_expression'
    PROPERTY_DECLARATION = 'property_declaration'
    RAW_STRING_LITERAL = 'raw_string_literal'
    SATISFIES_EXPRESSION = 'satisfies_expression'
    STATIC_ITEM = 'static_item'
    STRING = 'string'
    STRING_INTERPOLATION = 'string_interpolation'
    STRING_LITERAL = 'string_literal'
    SUBSCRIPT = 'subscript'
    SUBSCRIPT_EXPRESSION = 'subscript_expression'
    SWITCH_CASE = 'switch_case'
    TEMPLATE_STRING = 'template_string'
    TEMPLATE_SUBSTITUTION = 'template_substitution'
    UPDATE_EXPRESSION = 'update_expression'
    USE_DECLARATION = 'use_declaration'
    VARIABLE_DECLARATION = 'variable_declaration'
    VARIABLE_DECLARATOR = 'variable_declarator'
    VARIABLE_NAME = 'variable_name'


class FieldName(StrEnum):
    FUNCTION = 'function'
    INDEX = 'index'
    KEY = 'key'
    LEFT = 'left'
    NAME = 'name'
    OBJECT = 'object'
    OPERAND = 'operand'
    OPERATOR = 'operator'
    RIGHT = 'right'
    SOURCE = 'source'
    SUBSCRIPT = 'subscript'
    VALUE = 'value'


class ReportKey(StrEnum):
    COLUMN = 'column'
    CONSTANT_CANDIDATES = 'constant_candidates'
    CONTEXT = 'context'
    COUNTS = 'counts'
    COVERAGE = 'coverage'
    DISCOVERY = 'discovery'
    EXCLUDED = 'excluded'
    FILES = 'files'
    FINDINGS = 'findings'
    HEURISTIC = 'heuristic'
    HEURISTIC_NOTICE = 'heuristic_notice'
    INDEX_EVIDENCE = 'index_evidence'
    KIND = 'kind'
    LANGUAGE = 'language'
    LINE = 'line'
    LITERAL_SKIPS = 'literal_skips'
    LITERALS = 'literals'
    LOCATIONS = 'locations'
    NAME = 'name'
    NOTICE = 'notice'
    OCCURRENCES = 'occurrences'
    PARSE_ERRORS = 'parse_errors'
    PATH = 'path'
    PRIORITY = 'priority'
    PRODUCTION = 'production'
    REASON = 'reason'
    REDACTED = 'redacted'
    REDACTION_REASONS = 'redaction_reasons'
    ROLE = 'role'
    ROOT = 'root'
    SCANNED = 'scanned'
    SCHEMA_VERSION = 'schema_version'
    SEVERITY = 'severity'
    STYLE = 'style'
    TEST = 'test'
    TESTS = 'tests'
    UNSUPPORTED = 'unsupported'
    VALUE = 'value'
    WARNINGS = 'warnings'


class OutputFormat(StrEnum):
    JSON = "json"
    MARKDOWN = "markdown"


class ConfigKey(StrEnum):
    EXCLUDE = 'exclude'
    EXTENSIONS = 'extensions'
    INCLUDE_TESTS = 'include_tests'
    MAX_FILE_BYTES = 'max_file_bytes'
    MIN_OCCURRENCES = 'min_occurrences'


INTERPOLATED_OR_UNSUPPORTED = "interpolated_or_unsupported"
UTF8 = "utf-8"


class Language(StrEnum):
    PYTHON = "python"
    GO = "go"
    RUST = "rust"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    JAVA = "java"
    KOTLIN = "kotlin"
    CSHARP = "csharp"
    SWIFT = "swift"
    RUBY = "ruby"
    PHP = "php"
    ELIXIR = "elixir"
    SQL = "sql"


class ExclusionReason(StrEnum):
    DEPENDENCY_OR_GENERATED_DIRECTORY = "dependency_or_generated_directory"
    SENSITIVE_FILENAME = "sensitive_filename"
    LOCK_OR_GENERATED_FILE = "lock_or_generated_file"
    CONFIGURED_EXCLUSION = "configured_exclusion"
    UNREADABLE_DIRECTORY = "unreadable_directory"
    SYMLINK = "symlink"
    GITIGNORED = "gitignored"
    SYMLINK_OR_NONREGULAR = "symlink_or_nonregular"
    TEST_FILE = "test_file"
    UNMAPPED_EXTENSION = "unmapped_extension"
    FILE_TOO_LARGE = "file_too_large"
    BINARY_UNREADABLE_OR_NONUTF8 = "binary_unreadable_or_nonutf8"
    GENERATED_HEADER = "generated_header"
    PARSER_UNAVAILABLE = "parser_unavailable"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    PARSE_ERROR = "parse_error"


class Role(StrEnum):
    CONSTANT_DEFINITION = "constant_definition"
    COMPARISON = "comparison"
    MAPPING_KEY = "mapping_key"
    CONFIGURATION = "configuration"
    ARGUMENT = "argument"
    ASSIGNMENT = "assignment"
    LITERAL = "literal"


class CandidateStyle(StrEnum):
    ENUM_MEMBER = "enum_member"
    CONSTANT = "constant"


class RedactionReason(StrEnum):
    SENSITIVE_CONTEXT = "sensitive_context"
    CREDENTIAL_LIKE = "credential_like"
    CREDENTIAL_QUERY = "credential_query"
    UNPARSED_URL_QUERY = "unparsed_url_query"
    EMAIL = "email"
    PHONE_LIKE = "phone_like"


class FindingKind(StrEnum):
    REPEATED_LITERAL = "repeated_literal"
    STRING_KEY_INDEX = "repeated_string_key"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return (Severity.HIGH, Severity.MEDIUM, Severity.LOW).index(self)


LANGUAGES = {
    suffix: language
    for language, suffixes in {
        Language.PYTHON: (".py",),
        Language.GO: (".go",),
        Language.RUST: (".rs",),
        Language.JAVASCRIPT: (".js", ".jsx", ".mjs", ".cjs"),
        Language.TYPESCRIPT: (".ts", ".mts", ".cts"),
        Language.TSX: (".tsx",),
        Language.JAVA: (".java",),
        Language.KOTLIN: (".kt", ".kts"),
        Language.CSHARP: (".cs",),
        Language.SWIFT: (".swift",),
        Language.RUBY: (".rb",),
        Language.PHP: (".php",),
        Language.ELIXIR: (".ex", ".exs"),
        Language.SQL: (".sql",),
    }.items()
    for suffix in suffixes
}
LITERALS = {
    NodeType.STRING,
    NodeType.CONCATENATED_STRING,
    NodeType.TEMPLATE_STRING,
    NodeType.STRING_LITERAL,
    NodeType.RAW_STRING_LITERAL,
    NodeType.INTERPRETED_STRING_LITERAL,
    NodeType.LINE_STRING_LITERAL,
    NodeType.ENCAPSED_STRING,
    NodeType.LITERAL,
}


@dataclass
class ScanOptions:
    exclude: list[str] = dataclass_field(default_factory=list)
    include_tests: bool = False
    min_occurrences: int = 3
    max_file_bytes: int = 1024 * 1024
    extensions: dict[str, str] = dataclass_field(
        default_factory=lambda: dict(LANGUAGES)
    )


@dataclass(frozen=True)
class IndexEvidence:
    path: str
    occurrences: int
    lines: list[int]
    heuristic: bool = True
    notice: str = "Repeated literal keys may indicate an implicit record shape; receiver types and shared domain meaning are not inferred."


@dataclass(frozen=True)
class Occurrence:
    location: "Location"
    test: bool
    redaction_reasons: list[RedactionReason]
    candidate: "DefinitionCandidate | None"
    string_key_index: bool


@dataclass(frozen=True)
class Location:
    path: str
    line: int
    column: int
    language: Language
    role: Role
    context: str


@dataclass(frozen=True)
class DefinitionCandidate:
    name: str
    style: CandidateStyle


@dataclass(frozen=True)
class LocatedCandidate(Location):
    name: str
    style: CandidateStyle


@dataclass(frozen=True)
class CoverageEntry:
    path: str
    reason: ExclusionReason | None = None
    language: Language | None = None
    line: int | None = None
    column: int | None = None
    test: bool | None = None
    literals: int | None = None


@dataclass
class Coverage:
    discovery: str = ""
    scanned: list[CoverageEntry] = dataclass_field(default_factory=list)
    excluded: list[CoverageEntry] = dataclass_field(default_factory=list)
    unsupported: list[CoverageEntry] = dataclass_field(default_factory=list)
    parse_errors: list[CoverageEntry] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)
    literal_skips: dict[str, int] = dataclass_field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self, dict_factory=lambda pairs: {
            key: value for key, value in pairs if value is not None
        })


@dataclass(frozen=True)
class FindingCounts:
    occurrences: int
    files: int
    production: int
    tests: int


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    severity: Severity
    value: str
    redacted: bool
    redaction_reasons: list[RedactionReason]
    counts: FindingCounts
    locations: list[Location]
    constant_candidates: list[LocatedCandidate]
    index_evidence: IndexEvidence | None = None
    heuristic: bool = True

    def to_json(self) -> dict:
        result = asdict(self)
        result[ReportKey.PRIORITY] = self.severity  # Stable v1 compatibility alias.
        if self.index_evidence is None:
            del result[ReportKey.INDEX_EVIDENCE]
        return result

    def sort_key(self) -> tuple[int, str, int, int, str, str]:
        first = self.locations[0]
        return (
            self.severity.rank,
            first.path,
            first.line,
            first.column,
            self.value,
            self.kind,
        )


SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor", "dist", "build", "target", "coverage", ".next", ".nuxt", ".output", "__pycache__", ".cache", ".terraform", ".aws", ".ssh", ".gnupg", ".secrets", ".doppler", "generated", "gen"}
SECRET_NAMES = re.compile(r"(^\.env($|\.)|credentials|secrets?($|[._-])|(^|[._-])(id_rsa|id_ed25519)|\.(pem|key|p12|pfx|keystore)$)", re.I)
SECRET_CONTEXT = re.compile(r"password|passwd|secret|token|credential|private_?key|api_?key|access_?key|authorization|connection_?string|(^|_)dsn($|_)", re.I)
SECRET_QUERY = re.compile(r"token|api_?key|signature|credential|password|secret|authorization|access_?key|(^|_)(auth|sig|key)($|_)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOKEN = re.compile(r"(?:\b(?:sk-|ghp_|gho_|github_pat_|AKIA|ASIA)[A-Za-z0-9_/-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN .*PRIVATE KEY-----|://[^/\s:@]+:[^/\s@]+@)")
PRESENTATION = {"class", "classname", "style", "title", "alt", "placeholder", "arialabel", "label", "description", "children", "href", "src"}
QUIET_CALLS = {"print", "println", "printf", "sprintf", "log", "warn", "debug", "info", "error", "clsx", "cn", "classnames", "cva", "tw"}
SEMANTIC = {"state", "status", "kind", "type", "mode", "event", "protocol", "method", "operation", "scope", "role", "provider", "format", "encoding", "config", FieldName.KEY, FieldName.NAME}


def sensitive(value, context=""):
    reasons = []
    if SECRET_CONTEXT.search(re.sub(r"[^a-zA-Z0-9_]", "", context)):
        reasons.append(RedactionReason.SENSITIVE_CONTEXT)
    if TOKEN.search(value) or re.fullmatch(r"[A-Fa-f0-9]{24,}", value) or (len(value) >= 32 and re.fullmatch(r"[A-Za-z0-9_+/=-]+", value) and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) and re.search(r"\d", value)):
        reasons.append(RedactionReason.CREDENTIAL_LIKE)
    if "?" in value:
        try:
            query = urlsplit(value).query
        except ValueError:
            query = value.partition("?")[2].partition("#")[0]
        try:
            parameters = parse_qsl(query, keep_blank_values=True, max_num_fields=256)
            if any(SECRET_QUERY.search(re.sub(r"[^a-zA-Z0-9_]", "_", key)) for key, _ in parameters):
                reasons.append(RedactionReason.CREDENTIAL_QUERY)
        except ValueError:
            reasons.append(RedactionReason.UNPARSED_URL_QUERY)
    if EMAIL.search(value):
        reasons.append(RedactionReason.EMAIL)
    if re.fullmatch(r"[+()\d .-]{7,}", value) and len(re.sub(r"\D", "", value)) >= 7:
        reasons.append(RedactionReason.PHONE_LIKE)
    return reasons


def public_path(path):
    return "/".join("<redacted-name>" if sensitive(part) else re.sub(r"[\x00-\x1f\x7f-\x9f]", "<control>", part) for part in str(path).replace(os.sep, "/").split("/"))


def excluded(path, patterns):
    parts = path.parts
    if any(part in SKIP_DIRS for part in parts): return ExclusionReason.DEPENDENCY_OR_GENERATED_DIRECTORY
    if any(SECRET_NAMES.search(part) for part in parts): return ExclusionReason.SENSITIVE_FILENAME
    if path.name.endswith((".lock", ".min.js", ".min.css", ".map", ".generated.ts", ".generated.js", ".pb.go", ".sum")) or path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "Cargo.lock", "poetry.lock", "uv.lock"}: return ExclusionReason.LOCK_OR_GENERATED_FILE
    if any(fnmatch.fnmatch(path.as_posix(), pattern) for pattern in patterns): return ExclusionReason.CONFIGURED_EXCLUSION
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
        coverage.discovery = "git"
        return sorted({Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}, key=lambda path: path.as_posix())
    coverage.discovery = "filesystem"
    coverage.warnings.append(
        "Git inventory unavailable; filesystem traversal honors nested .gitignore files but cannot apply Git global/info excludes."
    )
    paths = []

    def walk(directory, rules):
        ignore = directory / ".gitignore"
        if ignore.is_file() and not ignore.is_symlink() and ignore.stat().st_size <= 65536:
            try:
                rules = [
                    *rules,
                    (
                        directory,
                        GitIgnoreSpec.from_lines(ignore.read_text().splitlines()),
                    ),
                ]
            except (OSError, UnicodeError):
                coverage.warnings.append(
                    "An unreadable .gitignore reduced filesystem ignore coverage."
                )
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            coverage.excluded.append(
                CoverageEntry(path=public_path(directory.relative_to(root)), reason=ExclusionReason.UNREADABLE_DIRECTORY)
            )
            return
        for entry in entries:
            relative = entry.relative_to(root)
            reason = excluded(relative, patterns)
            if reason or entry.is_symlink():
                coverage.excluded.append(
                    CoverageEntry(path=public_path(relative), reason=reason or ExclusionReason.SYMLINK)
                )
                continue
            ignored = False
            for base, spec in rules:
                candidate = entry.relative_to(base).as_posix() + ("/" if entry.is_dir() else "")
                for pattern in spec.patterns:
                    if pattern.include is not None and pattern.match_file(candidate): ignored = pattern.include
            if ignored:
                coverage.excluded.append(
                    CoverageEntry(path=public_path(relative), reason=ExclusionReason.GITIGNORED)
                )
                continue
            if entry.is_dir(): walk(entry, rules)
            elif entry.is_file(): paths.append(relative)

    walk(root, [])
    return paths


def character_column(source, offset):
    start = source.rfind(b"\n", 0, offset) + 1
    return len(source[start:offset].decode(UTF8)) + 1


def ancestors(node, limit=9):
    result = []
    while node.parent is not None and len(result) < limit:
        node = node.parent; result.append(node)
    return result


def text(node, source):
    return source[node.start_byte:node.end_byte].decode(UTF8) if node is not None else ""


def field(node, name, source):
    return text(node.child_by_field_name(name), source)


def decode_escapes(body, language: Language):
    # Go byte escapes belong to a UTF-8 byte string, unlike Unicode escapes.
    byte_output = bytearray()
    characters: list[str] = []

    def emit(value, byte=False):
        if language == Language.GO:
            byte_output.extend(bytes([value]) if byte else value.encode(UTF8))
        else:
            characters.append(value)

    i = 0
    while i < len(body):
        if body[i] != "\\": emit(body[i]); i += 1; continue
        i += 1
        if i >= len(body): raise ValueError("trailing escape")
        char = body[i]; i += 1
        if char in "\r\n":
            if char == "\r" and i < len(body) and body[i] == "\n": i += 1
            if language == Language.RUST:
                while i < len(body) and body[i].isspace(): i += 1
            continue
        escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "a": "\a", "\\": "\\", '"': '"', "'": "'", "`": "`"}
        if char in escapes: emit(escapes[char]); continue
        if char == "u" and i < len(body) and body[i] == "{":
            end = body.index("}", i); emit(chr(int(body[i+1:end].replace("_", ""), 16))); i = end + 1; continue
        if char in ("x", "u", "U"):
            length = {"x": 2, "u": 4, "U": 8}[char]; code = int(body[i:i+length], 16)
            if len(body[i:i+length]) != length: raise ValueError("short escape")
            emit(code if language == Language.GO and char == "x" else chr(code), language == Language.GO and char == "x"); i += length; continue
        if char in "01234567":
            digits = char
            while i < len(body) and len(digits) < 3 and body[i] in "01234567": digits += body[i]; i += 1
            code = int(digits, 8); emit(code if language == Language.GO else chr(code), language == Language.GO); continue
        if language in {Language.JAVASCRIPT, Language.TYPESCRIPT, Language.TSX}: emit(char)
        else: raise ValueError("unsupported escape")
    value = byte_output.decode(UTF8) if language == Language.GO else "".join(characters)
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value): value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    return value


def normalize(node, source, language: Language):
    literal = text(node, source)
    if any(
        child.type
        in {
            NodeType.INTERPOLATION,
            NodeType.TEMPLATE_SUBSTITUTION,
            NodeType.STRING_INTERPOLATION,
            NodeType.INTERPOLATION_EXPRESSION,
            NodeType.INTERPOLATED_EXPRESSION,
            NodeType.NAVIGATION_EXPRESSION,
            NodeType.VARIABLE_NAME,
        }
        for child in descendants(node)
    ):
        return None
    if language == Language.PYTHON:
        value = ast.literal_eval(literal)
        return value if isinstance(value, str) else None
    if language == Language.RUST:
        raw = re.fullmatch(r'r(#{0,255})"([\s\S]*)"\1', literal)
        if raw: return raw[2]
        if not literal.startswith('"'):
            return None  # Byte/C strings have different semantics.
    if language == Language.GO and literal.startswith("`"): return literal[1:-1].replace("\r", "")
    if language == Language.SQL:
        # SQL dialect escape modes vary; only portable quoted strings are decoded.
        if literal.startswith("'") and literal.endswith("'") and "\\" not in literal:
            return literal[1:-1].replace("''", "'")
        return None
    if language in {Language.PHP, Language.RUBY} and literal.startswith("'"):
        return re.sub(r"\\([\\'])", lambda match: match[1], literal[1:-1])
    if language == Language.CSHARP and literal.startswith('@"'):
        return literal[2:-1].replace('""', '"')
    if language in {
        Language.JAVA,
        Language.KOTLIN,
        Language.CSHARP,
        Language.SWIFT,
        Language.RUBY,
        Language.PHP,
        Language.ELIXIR,
    }:
        # These languages disagree on raw/multiline forms and several escapes.
        # Keep simple static forms and common escapes, conservatively skip others.
        if literal.startswith(('"""', "'''", "`", "%", "~")):
            return None
        if re.search(r"\\[^nrtbf\\\"']", literal):
            return None
    if literal[:1] in {'"', "'", "`"} and literal[-1:] == literal[:1]: return decode_escapes(literal[1:-1], language)
    return None


def string_key_index(node, language: Language) -> bool:
    """Syntax only: even Go index expressions need not have a map receiver."""
    parent = node.parent
    if parent is None:
        return False
    if language == Language.PYTHON:
        return (
            parent.type == NodeType.SUBSCRIPT
            and parent.child_by_field_name(FieldName.SUBSCRIPT) == node
        )
    if language in {Language.JAVASCRIPT, Language.TYPESCRIPT, Language.TSX}:
        return (
            parent.type == NodeType.SUBSCRIPT_EXPRESSION
            and parent.child_by_field_name(FieldName.INDEX) == node
        )
    if language == Language.GO:
        return (
            parent.type == NodeType.INDEX_EXPRESSION
            and parent.child_by_field_name(FieldName.INDEX) == node
        )
    if language == Language.RUBY:
        return (
            parent.type == NodeType.ELEMENT_REFERENCE
            and len(parent.named_children) == 2
            and parent.named_children[-1] == node
        )
    if language == Language.PHP:
        return (
            parent.type == NodeType.SUBSCRIPT_EXPRESSION
            and len(parent.named_children) == 2
            and parent.named_children[-1] == node
        )
    return False


def excluded_index_literal(node, source, language: Language) -> bool:
    """Exclude known non-record lookups and counter targets from both findings."""
    if language in {Language.TYPESCRIPT, Language.TSX} and any(
        owner.type == NodeType.LOOKUP_TYPE for owner in ancestors(node)
    ):
        return True
    if not string_key_index(node, language):
        return False
    index = node.parent
    receiver = (
        index.child_by_field_name(FieldName.VALUE)
        or index.child_by_field_name(FieldName.OBJECT)
        or index.child_by_field_name(FieldName.OPERAND)
    )
    receiver_name = re.sub(r"\s+", "", text(receiver, source))
    if language == Language.PYTHON and receiver_name in {"os.environ", "sys.modules"}:
        return True
    if language in {Language.JAVASCRIPT, Language.TYPESCRIPT, Language.TSX} and receiver_name == "process.env":
        return True
    # Follow only the target's receiver chain, never a computed key or RHS read.
    target = index
    while target.parent is not None:
        owner = target.parent
        if owner.type in {NodeType.PARENTHESIZED_EXPRESSION, NodeType.EXPRESSION_LIST}:
            target = owner
            continue
        if owner.type in {NodeType.SUBSCRIPT, NodeType.SUBSCRIPT_EXPRESSION, NodeType.INDEX_EXPRESSION} and (
            owner.child_by_field_name(FieldName.VALUE) == target
            or owner.child_by_field_name(FieldName.OBJECT) == target
            or owner.child_by_field_name(FieldName.OPERAND) == target
        ):
            target = owner
            continue
        if owner.type in {NodeType.AUGMENTED_ASSIGNMENT, NodeType.AUGMENTED_ASSIGNMENT_EXPRESSION}:
            return owner.child_by_field_name(FieldName.LEFT) == target
        if owner.type == NodeType.ASSIGNMENT_STATEMENT:
            return (
                owner.child_by_field_name(FieldName.LEFT) == target
                and field(owner, FieldName.OPERATOR, source) not in {"", "="}
            )
        return owner.type in {NodeType.UPDATE_EXPRESSION, NodeType.INC_STATEMENT, NodeType.DEC_STATEMENT}
    return False


def supported_parser(parser_name: str) -> TypeGuard[SupportedLanguage]:
    """Validate extension overrides against this pinned pack's grammar registry."""
    return parser_name in get_args(SupportedLanguage)


def descendants(node):
    stack = list(reversed(node.named_children))
    while stack:
        item = stack.pop(); yield item; stack.extend(reversed(item.named_children))


def direct_value(owner, node):
    value = owner.child_by_field_name(FieldName.VALUE) or owner.child_by_field_name(FieldName.RIGHT)
    while value is not None and value.type in {NodeType.EXPRESSION_LIST, NodeType.PARENTHESIZED_EXPRESSION, NodeType.AS_EXPRESSION, NodeType.SATISFIES_EXPRESSION, NodeType.LITERAL_TYPE} and value.named_children:
        value = value.named_children[0]
    return value == node


def evidence(node, source, language):
    chain = ancestors(node)
    parent = node.parent
    kinds = {item.type for item in chain}
    if any(item.type in {NodeType.IMPORT_STATEMENT, NodeType.EXPORT_STATEMENT} and item.child_by_field_name(FieldName.SOURCE) == node for item in chain): return None
    if kinds & {NodeType.USE_DECLARATION, NodeType.EXTERN_CRATE_DECLARATION}: return None
    if language == Language.PYTHON and parent:
        expression = parent if parent.type == NodeType.EXPRESSION_STATEMENT else node
        container = expression.parent
        if container and container.type in {NodeType.MODULE, NodeType.BLOCK} and container.named_children and container.named_children[0] == expression and (container.type == NodeType.MODULE or container.parent and container.parent.type in {NodeType.FUNCTION_DEFINITION, NodeType.CLASS_DEFINITION}): return None
    key = ""
    candidate = None
    contexts = []
    for item in chain:
        if item.type in {NodeType.PAIR, NodeType.KEY_VALUE, NodeType.FIELD_INITIALIZER, NodeType.JSX_ATTRIBUTE}:
            key = field(item, FieldName.KEY, source) or field(item, FieldName.NAME, source) or text(item.named_children[0], source)
            contexts.append(key)
            if re.sub(r"[^a-z]", "", key.lower()) in PRESENTATION: return None
        if item.type in {NodeType.CALL_EXPRESSION, NodeType.CALL}:
            function = field(item, FieldName.FUNCTION, source).lower()
            if (
                function.rsplit(".", 1)[-1].replace("!", "") in QUIET_CALLS
                and not string_key_index(node, language)
            ) or function in {"require", "import"}:
                return None
            contexts.append(function)
            key = key or function
        if item.type in {
            NodeType.ASSIGNMENT,
            NodeType.ASSIGNMENT_EXPRESSION,
            NodeType.ANNOTATED_ASSIGNMENT,
            NodeType.PROPERTY_DECLARATION,
            NodeType.BINARY_OPERATOR,
            NodeType.VARIABLE_DECLARATOR,
            NodeType.CONST_SPEC,
            NodeType.CONST_ITEM,
            NodeType.STATIC_ITEM,
            NodeType.ENUM_ASSIGNMENT,
        }:
            name = field(item, FieldName.LEFT, source) or field(item, FieldName.NAME, source)
            if not name and item.type == NodeType.PROPERTY_DECLARATION:
                binding = next(
                    (
                        child
                        for child in item.named_children
                        if child.type == NodeType.VARIABLE_DECLARATION
                    ),
                    None,
                )
                name = text(binding, source)
            contexts.append(name)
            key = key or name
            if (
                re.fullmatch(r"[A-Za-z_$][\w$]*", name)
                and direct_value(item, node)
                and (
                    item.type
                    in {NodeType.CONST_SPEC, NodeType.CONST_ITEM, NodeType.STATIC_ITEM, NodeType.ENUM_ASSIGNMENT}
                    or re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
                )
            ):
                candidate = DefinitionCandidate(
                    name,
                    CandidateStyle.ENUM_MEMBER
                    if item.type == NodeType.ENUM_ASSIGNMENT
                    else CandidateStyle.CONSTANT,
                )
    comparison = bool(kinds & {NodeType.COMPARISON_OPERATOR, NodeType.CASE_CLAUSE, NodeType.SWITCH_CASE, NodeType.MATCH_ARM, NodeType.LITERAL_PATTERN}) or any(item.type == NodeType.BINARY_EXPRESSION and field(item, FieldName.OPERATOR, source) in {"==", "!=", "===", "!==", "<", ">", "<=", ">=", "in", "instanceof"} for item in chain)
    role = Role.CONSTANT_DEFINITION if candidate else Role.COMPARISON if comparison else Role.MAPPING_KEY if parent and parent.type in {NodeType.PAIR, NodeType.KEY_VALUE} and parent.child_by_field_name(FieldName.KEY) == node else Role.CONFIGURATION if re.sub(r"[^a-z]", "", key.lower()) in SEMANTIC else Role.ARGUMENT if kinds & {NodeType.ARGUMENTS, NodeType.ARGUMENT_LIST} else Role.ASSIGNMENT if key else Role.LITERAL
    inline_test = any(item.type in {NodeType.FUNCTION_DEFINITION, NodeType.FUNCTION_ITEM, NodeType.MOD_ITEM} and (field(item, FieldName.NAME, source).startswith("test_") or field(item, FieldName.NAME, source) == "tests") for item in chain)
    return role, parent.type if parent else ReportKey.ROOT, " ".join(contexts), candidate, inline_test


def scan(root: Path, options: ScanOptions):
    coverage = Coverage()
    groups: dict[str, list[Occurrence]] = defaultdict(list)
    parsers = {}
    for relative in inventory(root, options.exclude, coverage):
        location = public_path(relative)
        reason = excluded(relative, options.exclude)
        if reason or not safe_file(root, relative):
            coverage.excluded.append(
                CoverageEntry(path=location, reason=reason or ExclusionReason.SYMLINK_OR_NONREGULAR)
            )
            continue
        is_test = test_path(relative)
        if is_test and not options.include_tests:
            coverage.excluded.append(CoverageEntry(path=location, reason=ExclusionReason.TEST_FILE))
            continue
        parser_name = options.extensions.get(relative.suffix.lower())
        if not parser_name:
            coverage.unsupported.append(
                CoverageEntry(path=location, reason=ExclusionReason.UNMAPPED_EXTENSION)
            )
            continue
        try:
            source = read_source(root, relative, options.max_file_bytes)
            if source is None:
                coverage.excluded.append(
                    CoverageEntry(path=location, reason=ExclusionReason.FILE_TOO_LARGE)
                )
                continue
            if len(source) > options.max_file_bytes:
                raise ValueError("size changed")
            if b"\0" in source: raise UnicodeError("binary")
            source.decode(UTF8)
        except (OSError, ValueError, UnicodeError):
            coverage.excluded.append(
                CoverageEntry(path=location, reason=ExclusionReason.BINARY_UNREADABLE_OR_NONUTF8)
            )
            continue
        if re.search(rb'(?im)^\s*(?://|#|/\*|\*)[^\n]*(?:@generated|code generated|auto.generated|do not edit)', source[:4096]):
            coverage.excluded.append(
                CoverageEntry(path=location, reason=ExclusionReason.GENERATED_HEADER)
            )
            continue
        try:
            if not supported_parser(parser_name):
                coverage.unsupported.append(
                    CoverageEntry(path=location, reason=ExclusionReason.PARSER_UNAVAILABLE)
                )
                continue
            try:
                language = Language(parser_name)
            except ValueError:
                coverage.unsupported.append(
                    CoverageEntry(path=location, reason=ExclusionReason.UNSUPPORTED_LANGUAGE)
                )
                continue
            if language not in parsers: parsers[language] = get_parser(parser_name)
            tree = parsers[language].parse(source)
        except (
            LookupError,
            RuntimeError,
            ValueError,
            TypeError,
            OSError,
            AttributeError,
        ):
            coverage.unsupported.append(
                CoverageEntry(path=location, reason=ExclusionReason.PARSER_UNAVAILABLE)
            )
            continue
        if tree.root_node.has_error:
            error = next((item for item in descendants(tree.root_node) if item.type == NodeType.ERROR or item.is_missing), tree.root_node)
            coverage.parse_errors.append(
                CoverageEntry(path=location, language=language, line=error.start_point.row + 1, column=character_column(source, error.start_byte))
            )
            continue
        literals = 0
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if NodeType.COMMENT in node.type: continue
            if node.type not in LITERALS:
                stack.extend(reversed(node.named_children)); continue
            if excluded_index_literal(node, source, language):
                continue
            if language == Language.SQL and (
                re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text(node, source))
                or any(child.type in {NodeType.KEYWORD_TRUE, NodeType.KEYWORD_FALSE, NodeType.KEYWORD_NULL}
                       for child in node.named_children)
            ):
                continue  # SQL uses the same literal node for strings and scalars.
            try: value = normalize(node, source, language)
            except (ValueError, SyntaxError, UnicodeError): value = None
            if value is None:
                coverage.literal_skips[INTERPOLATED_OR_UNSUPPORTED] = (
                    coverage.literal_skips.get(INTERPOLATED_OR_UNSUPPORTED, 0) + 1
                )
                continue
            item = evidence(node, source, language)
            if item is None: continue
            role, context, key, candidate, inline_test = item
            occurrence_test = is_test or inline_test
            if occurrence_test and not options.include_tests:
                continue
            reasons = sensitive(value, key)
            indexed = string_key_index(node, language)
            if not reasons and (
                (len(value) < 3 and not indexed)
                or len(value) > 160
                or "\n" in value
                or "\r" in value
                or len(value.split()) > 4
            ):
                continue
            if not value.strip(): continue
            column = character_column(source, node.start_byte)
            entry = Location(
                location, node.start_point.row + 1, column, language, role, context
            )
            groups[value].append(
                Occurrence(entry, occurrence_test, reasons, candidate, indexed)
            )
            literals += 1
        coverage.scanned.append(
            CoverageEntry(path=location, language=language, test=is_test, literals=literals)
        )
    findings: list[Finding] = []
    for value, occurrences in groups.items():
        if len(occurrences) < options.min_occurrences:
            continue
        locations = sorted(
            [item.location for item in occurrences],
            key=lambda item: (item.path, item.line, item.column),
        )
        production = sum(not item.test for item in occurrences)
        files = len({item.path for item in locations})
        reasons = sorted(
            {reason for item in occurrences for reason in item.redaction_reasons}
        )
        semantic = sum(
            item.location.role in {Role.COMPARISON, Role.CONFIGURATION, Role.MAPPING_KEY}
            for item in occurrences
        )
        priority = Severity.HIGH if production >= 3 and files >= 2 and semantic >= 2 else Severity.MEDIUM if production >= 3 and semantic else Severity.LOW
        candidates: list[LocatedCandidate] = [
            LocatedCandidate(**asdict(item.location), **asdict(item.candidate))
            for item in occurrences
            if item.candidate
        ]
        findings.append(
            Finding(
                FindingKind.REPEATED_LITERAL,
                priority,
                "<redacted>" if reasons else value,
                bool(reasons),
                reasons,
                FindingCounts(
                    len(occurrences), files, production, len(occurrences) - production
                ),
                locations,
                candidates,
            )
        )
        by_file: dict[str, list[Occurrence]] = defaultdict(list)
        for item in occurrences:
            if item.string_key_index:
                by_file[item.location.path].append(item)
        for path, indexed in sorted(by_file.items()):
            if len(indexed) < options.min_occurrences:
                continue
            index_locations = sorted(
                [item.location for item in indexed],
                key=lambda item: (item.line, item.column),
            )
            index_production = sum(not item.test for item in indexed)
            record = IndexEvidence(
                path, len(indexed), [item.line for item in index_locations]
            )
            findings.append(
                Finding(
                    FindingKind.STRING_KEY_INDEX,
                    Severity.LOW,
                    "<redacted>" if reasons else value,
                    bool(reasons),
                    reasons,
                    FindingCounts(
                        len(indexed),
                        1,
                        index_production,
                        len(indexed) - index_production,
                    ),
                    index_locations,
                    [],
                    record,
                )
            )
    findings.sort(key=Finding.sort_key)
    for entries in (coverage.scanned, coverage.excluded, coverage.unsupported, coverage.parse_errors):
        entries.sort(key=lambda item: (item.path, item.reason or ""))
    coverage.warnings = sorted(set(coverage.warnings))
    return {
        ReportKey.SCHEMA_VERSION: 1,
        ReportKey.ROOT: public_path(root),
        ReportKey.HEURISTIC_NOTICE: "Repeated syntax is evidence for review, not proof of shared domain meaning or a mandate to create constants.",
        ReportKey.COVERAGE: coverage.to_json(),
        ReportKey.FINDINGS: [item.to_json() for item in findings],
    }


def markdown(report):
    def safe(value): return html.escape(str(value)).replace("|", "&#124;").replace("`", "&#96;")

    coverage = report[ReportKey.COVERAGE]
    lines = ["# Repeated literal evidence", "", report[ReportKey.HEURISTIC_NOTICE], "", f"Root: {safe(report[ReportKey.ROOT])}", f"Coverage: {len(coverage[ReportKey.SCANNED])} parsed files; {len(coverage[ReportKey.EXCLUDED])} excluded; {len(coverage[ReportKey.UNSUPPORTED])} unsupported; {len(coverage[ReportKey.PARSE_ERRORS])} parse errors.", ""]
    for finding in report[ReportKey.FINDINGS]:
        lines += [
            f"## {finding[ReportKey.PRIORITY].title()} · {safe(json.dumps(finding[ReportKey.VALUE], ensure_ascii=True))}",
            f"Kind: {finding[ReportKey.KIND]} (heuristic)",
            f"{finding[ReportKey.COUNTS][ReportKey.OCCURRENCES]} occurrences in {finding[ReportKey.COUNTS][ReportKey.FILES]} files; {finding[ReportKey.COUNTS][ReportKey.PRODUCTION]} production / {finding[ReportKey.COUNTS][ReportKey.TESTS]} test locations.",
            "",
        ]
        if ReportKey.INDEX_EVIDENCE in finding:
            lines.append(finding[ReportKey.INDEX_EVIDENCE][ReportKey.NOTICE])
        for location in finding[ReportKey.LOCATIONS]: lines.append(f"- {safe(location[ReportKey.PATH])}:{location[ReportKey.LINE]}:{location[ReportKey.COLUMN]} — {location[ReportKey.ROLE]} ({location[ReportKey.CONTEXT]})")
        for candidate in finding[ReportKey.CONSTANT_CANDIDATES]: lines.append(f"- Existing definition candidate: {safe(candidate[ReportKey.NAME])} at {safe(candidate[ReportKey.PATH])}:{candidate[ReportKey.LINE]} ({candidate[ReportKey.STYLE]})")
        if finding[ReportKey.REDACTED]: lines.append("- Value redacted: " + ", ".join(finding[ReportKey.REDACTION_REASONS]))
        lines.append("")
    if not report[ReportKey.FINDINGS]: lines += ["No repeated candidates met the configured threshold.", ""]
    lines += ["## Coverage details", ""]
    for kind in (ReportKey.EXCLUDED, ReportKey.UNSUPPORTED, ReportKey.PARSE_ERRORS):
        for entry in coverage[kind]: lines.append(f"- {kind}: {safe(entry[ReportKey.PATH])} — {entry.get(ReportKey.REASON, ExclusionReason.PARSE_ERROR)}")
    lines += [f"- {safe(warning)}" for warning in coverage[ReportKey.WARNINGS]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--format", type=OutputFormat, choices=tuple(OutputFormat), default=OutputFormat.MARKDOWN)
    parser.add_argument("--output", type=Path, help="Write a report outside the scanned root; stdout is the default")
    parser.add_argument("--include-tests", action="store_true", default=None)
    parser.add_argument("--config", type=Path, help="JSON: exclude, include_tests, min_occurrences, max_file_bytes, extensions")
    parser.add_argument("--min-occurrences", type=int)
    parser.add_argument("--extension", action="append", default=[], metavar=".EXT=GRAMMAR")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir(): parser.error("ROOT must be an existing directory")
    options = ScanOptions()
    if args.config:
        try: config = json.loads(args.config.read_text())
        except (OSError, ValueError): parser.error("Cannot read JSON configuration")
        if not isinstance(config, dict) or set(config) - set(
            ScanOptions.__dataclass_fields__
        ):
            parser.error("Unknown configuration keys")
        options.exclude = config.get(ConfigKey.EXCLUDE, options.exclude)
        options.include_tests = config.get(ConfigKey.INCLUDE_TESTS, options.include_tests)
        options.min_occurrences = config.get(ConfigKey.MIN_OCCURRENCES, options.min_occurrences)
        options.max_file_bytes = config.get(ConfigKey.MAX_FILE_BYTES, options.max_file_bytes)
        if ConfigKey.EXTENSIONS in config:
            if not isinstance(config[ConfigKey.EXTENSIONS], dict): parser.error("extensions must be an object")
            options.extensions.update(config[ConfigKey.EXTENSIONS])
    for item in args.extension:
        if "=" not in item: parser.error("--extension expects .EXT=GRAMMAR")
        suffix, language = item.split("=", 1)
        options.extensions[suffix] = language
    if args.include_tests is not None:
        options.include_tests = args.include_tests
    if args.min_occurrences is not None:
        options.min_occurrences = args.min_occurrences
    if (
        not isinstance(options.include_tests, bool)
        or not isinstance(options.exclude, list)
        or not all(isinstance(item, str) for item in options.exclude)
    ):
        parser.error("Invalid exclusion/test configuration")
    if (
        not isinstance(options.min_occurrences, int)
        or isinstance(options.min_occurrences, bool)
        or options.min_occurrences < 2
    ):
        parser.error("Invalid occurrence threshold")
    if (
        not isinstance(options.max_file_bytes, int)
        or isinstance(options.max_file_bytes, bool)
        or options.max_file_bytes < 1
    ):
        parser.error("Invalid file size")
    if not all(
        isinstance(key, str)
        and key.startswith(".")
        and isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]*", value)
        for key, value in options.extensions.items()
    ):
        parser.error("Invalid extension/parser mapping")
    output = args.output.expanduser().resolve() if args.output else None
    if output and output.is_relative_to(root): parser.error("Reports must be written outside ROOT to keep the target read-only")
    report = scan(root, options)
    rendered = json.dumps(report, indent=2, ensure_ascii=True) + "\n" if args.format == OutputFormat.JSON else markdown(report)
    if output:
        with output.open("w", encoding=UTF8) as stream: stream.write(rendered)
    else: sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
