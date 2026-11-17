#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Redact secret-shaped text before it crosses a question boundary.

This is detection, not proof that arbitrary text contains no secret. Callers
must not apply entropy scanning to identifiers, digests, or protocol tokens.
"""

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
import math
import re
from typing import Final

from question_model import Untrusted, UntrustedSource


class RedactionRule(StrEnum):
    URL_USERINFO = "url_userinfo"
    PRIVATE_KEY = "private_key"
    OPENAI_KEY = "openai_key"
    GITHUB_TOKEN = "github_token"
    GITLAB_TOKEN = "gitlab_token"
    SLACK_TOKEN = "slack_token"
    AWS_KEY = "aws_key"
    BEARER = "bearer"
    ASSIGNMENT = "assignment"
    JWT = "jwt"
    HIGH_ENTROPY = "high_entropy"


REDACTED: Final = "[REDACTED]"
URL_USERINFO: Final = re.compile(r"([a-z][a-z0-9+.-]*://)[^/?#\s]+@", re.I)
TOKEN: Final = re.compile(r"\S{32,}")
PATTERNS: Final = (
    (
        RedactionRule.PRIVATE_KEY,
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)",
            re.S,
        ),
    ),
    (RedactionRule.OPENAI_KEY, re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    (
        RedactionRule.GITHUB_TOKEN,
        re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    (RedactionRule.GITLAB_TOKEN, re.compile(r"glpat-[A-Za-z0-9_-]+")),
    (RedactionRule.SLACK_TOKEN, re.compile(r"xox[abpr]-[A-Za-z0-9-]+")),
    (RedactionRule.AWS_KEY, re.compile(r"AKIA[0-9A-Z]{16}")),
    (RedactionRule.BEARER, re.compile(r"Bearer\s+\S{12,}", re.I)),
    (
        RedactionRule.ASSIGNMENT,
        re.compile(
            r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|private[_-]?key)(?:[_-]\w+)*[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
            re.I,
        ),
    ),
    (
        RedactionRule.JWT,
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    hits: tuple[RedactionRule, ...]

    @property
    def secret_present(self) -> bool:
        return bool(self.hits)


def entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    return (
        -sum(
            (count / len(text)) * math.log2(count / len(text))
            for count in Counter(text).values()
        )
        if text
        else 0.0
    )


def redact(text: str) -> RedactionResult:
    """Remove URL credentials and replace overlapping secret spans once."""
    hits: list[RedactionRule] = []
    if URL_USERINFO.search(text):
        hits.append(RedactionRule.URL_USERINFO)
        text = URL_USERINFO.sub(r"\1", text)
    spans: list[tuple[int, int]] = []
    for rule, pattern in PATTERNS:
        matches = tuple(pattern.finditer(text))
        if matches:
            hits.append(rule)
            spans.extend(match.span() for match in matches)
    for match in TOKEN.finditer(text):
        if entropy(match.group()) > 4.0:
            if RedactionRule.HIGH_ENTROPY not in hits:
                hits.append(RedactionRule.HIGH_ENTROPY)
            spans.append(match.span())
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + REDACTED + text[end:]
    return RedactionResult(text, tuple(hits))


def untrusted(text: str, source: UntrustedSource) -> Untrusted:
    return Untrusted(redact(text).text, source)
