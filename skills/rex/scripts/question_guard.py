#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Shared, fail-closed question policy and decision validation."""

from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
import re
import tomllib
from typing import Final
import unicodedata

from question_model import (
    ASSUMPTION_PREFIX,
    BRIEF_FENCE,
    BRIEF_SCHEMA,
    Answer,
    BriefEscalation,
    BriefProject,
    Category,
    Classification,
    DecisionLevel,
    EscalationLevel,
    GuardHit,
    GuardRule,
    GuardStrength,
    GuardTrigger,
    Question,
    QuestionId,
    RuleId,
    ValidationFailure,
    WeakClearance,
)
from question_redact import redact

GUARD_FILE: Final = Path(__file__).with_suffix(".toml")
POLICY_RULE: Final = RuleId("policy_unavailable.strong")
SECRET_RULE: Final = RuleId("secret_present.strong")
BRIEF_RULE: Final = RuleId("brief_ask_carlos.strong")
LA_CANA_RULE: Final = RuleId("la_cana.strong")
CATEGORY_FLOORS: Final = {
    Category.TRIVIAL_FACTUAL: EscalationLevel.L1_CHEAP,
    Category.REPO_CONVENTION: EscalationLevel.L1_CHEAP,
    Category.DESIGN_CHOICE: EscalationLevel.L2_NORMAL,
    Category.IRREVERSIBLE_OR_COSTLY: EscalationLevel.L4_OWNER,
    Category.PRODUCT_SCOPE: EscalationLevel.L4_OWNER,
}


class BriefInvalid(ValueError):
    """A BRIEF_INVALID notice and POLICY_UNAVAILABLE floor are required."""


def normalize(text: str) -> str:
    # Match-only: Cf stripping also removes ZWJ/ZWNJ; never store this normalized text.
    if not text.isascii():
        text = "".join(
            char
            for char in unicodedata.normalize("NFKC", text)
            if unicodedata.category(char) != "Cf"
        )
    return " ".join(text.lower().split())


def _contains_phrase(text: str, phrase: str) -> bool:
    return (
        re.search(
            r"(?<!\w)" + re.escape(normalize(phrase)) + r"(?!\w)", normalize(text)
        )
        is not None
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected a list of strings")
    return tuple(value)


def load_guard(path: Path = GUARD_FILE) -> tuple[GuardRule, ...]:
    """Parse the policy boundary; malformed or incomplete rules never run."""
    data = tomllib.loads(path.read_text())
    entries = data.get("rules")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Guard rules are missing")
    rules: list[GuardRule] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Guard rule must be a table")
        rule_id, trigger, strength, pattern = (
            entry.get(key) for key in ("id", "trigger", "strength", "pattern")
        )
        if not all(
            isinstance(value, str) and value
            for value in (rule_id, trigger, strength, pattern)
        ):
            raise ValueError("Guard rule fields must be nonempty strings")
        if (
            not isinstance(rule_id, str)
            or not isinstance(trigger, str)
            or not isinstance(strength, str)
            or not isinstance(pattern, str)
        ):
            raise ValueError("Invalid guard fields")
        rules.append(
            GuardRule(
                RuleId(rule_id),
                GuardTrigger(trigger),
                GuardStrength(strength),
                re.compile(pattern, re.I),
                _strings(entry.get("examples_hit", [])),
                _strings(entry.get("examples_miss", [])),
            )
        )
    if len({rule.id for rule in rules}) != len(rules):
        raise ValueError("Duplicate guard rule")
    return tuple(rules)


def _policy_hits(
    text: str, *, question: QuestionId | None = None, rules: Sequence[GuardRule]
) -> tuple[GuardHit, ...]:
    normalized = normalize(text)
    return tuple(
        GuardHit(rule.trigger, rule.strength, rule.id, question)
        for rule in rules
        if rule.pattern.search(normalized)
    )


def scan(
    text: str,
    *,
    question: QuestionId | None = None,
    rules: Sequence[GuardRule] | None = None,
) -> tuple[GuardHit, ...]:
    """Scan content only; raw secret hits survive removal of their values."""
    redacted = redact(text)
    hits = list(
        _policy_hits(
            redacted.text,
            question=question,
            rules=load_guard() if rules is None else rules,
        )
    )
    if redacted.secret_present:
        hits.append(
            GuardHit(
                GuardTrigger.SECRET_PRESENT, GuardStrength.STRONG, SECRET_RULE, question
            )
        )
    return tuple(hits)


def parse_brief(
    text: str | None,
    *,
    cwd: str = "",
    remote: str = "",
    rules: Sequence[GuardRule] | None = None,
) -> BriefEscalation:
    """Parse the optional policy fence, refusing guarded L0 assumptions."""
    try:
        policy = load_guard() if rules is None else rules
        project = (
            BriefProject.LA_CANA
            if re.search(r"\bla[\s_-]?ca(n|ñ)a\b", normalize(cwd + " " + remote))
            else BriefProject.PERSONAL
        )
        if text is None:
            return BriefEscalation(project, (), ())
        lines = text.splitlines()
        starts = [
            index
            for index, line in enumerate(lines)
            if line.strip() == "```" + BRIEF_FENCE
        ]
        if not starts:
            return BriefEscalation(project, (), ())
        if len(starts) != 1:
            raise ValueError("Multiple escalation blocks")
        start = starts[0] + 1
        end = next(
            (
                index
                for index in range(start, len(lines))
                if lines[index].strip() == "```"
            ),
            None,
        )
        if end is None:
            raise ValueError("Unterminated escalation block")
        data = tomllib.loads("\n".join(lines[start:end]))
        if (
            set(data) != {"schema", "project", "l0_assume", "ask_carlos"}
            or data.get("schema") != BRIEF_SCHEMA
        ):
            raise ValueError("Invalid brief schema or fields")
        project_value = data["project"]
        if not isinstance(project_value, str):
            raise ValueError("Invalid brief project")
        assumptions = _strings(data["l0_assume"])
        asks = _strings(data["ask_carlos"])
        if any(not normalize(item) for item in (*assumptions, *asks)):
            raise ValueError("Brief entries must be nonempty")
        if any(
            scan(item, rules=policy)
            or any(_contains_phrase(item, phrase) for phrase in asks)
            for item in assumptions
        ):
            raise ValueError("L0 assumption hits a guard rule")
        return BriefEscalation(BriefProject(project_value), assumptions, asks)
    except (OSError, ValueError, re.error) as exc:
        raise BriefInvalid(str(exc)) from exc


def _question_texts(question: Question) -> Iterable[str]:
    yield question.text.text
    if question.header:
        yield question.header.text
    for option in question.options:
        yield option.label.text
        if option.description:
            yield option.description.text


def derive_floor(
    questions: Sequence[Question],
    *,
    category: Category = Category.REPO_CONVENTION,
    classification: Classification | None = None,
    brief: BriefEscalation | None = None,
    cwd: str = "",
    remote: str = "",
    previous_floor: EscalationLevel = EscalationLevel.L0_SELF,
    secret_present: bool = False,
    policy_available: bool = True,
    rules: Sequence[GuardRule] | None = None,
    require_classification: bool = True,
) -> Classification:
    """Compute a monotone batch floor, retaining retired question policy."""
    hits: list[GuardHit] = []
    category_floor = CATEGORY_FLOORS[category]
    try:
        if not policy_available:
            raise ValueError("Policy is unavailable")
        policy = load_guard() if rules is None else rules
        for question in questions:
            texts = tuple(_question_texts(question))
            for text in texts:
                hits.extend(scan(text, question=question.id, rules=policy))
            if brief:
                for phrase in brief.ask_carlos:
                    if any(_contains_phrase(text, phrase) for text in texts):
                        hits.append(
                            GuardHit(
                                GuardTrigger.BRIEF_ASK_CARLOS,
                                GuardStrength.STRONG,
                                BRIEF_RULE,
                                question.id,
                            )
                        )
        # Paths and remotes are identifiers: entropy is meaningful for content,
        # while a normal long path must still expose policy words like la_cana.
        for text in (cwd, remote):
            hits.extend(_policy_hits(text, rules=policy))
        for text in brief.ask_carlos if brief else ():
            hits.extend(scan(text, rules=policy))
        if brief and brief.project == BriefProject.LA_CANA:
            hits.append(
                GuardHit(GuardTrigger.LA_CANA, GuardStrength.STRONG, LA_CANA_RULE, None)
            )
    except (OSError, ValueError, re.error):
        hits.append(
            GuardHit(
                GuardTrigger.POLICY_UNAVAILABLE, GuardStrength.STRONG, POLICY_RULE, None
            )
        )
    if secret_present:
        hits.append(
            GuardHit(
                GuardTrigger.SECRET_PRESENT, GuardStrength.STRONG, SECRET_RULE, None
            )
        )
    if classification:
        category = classification.category
        hits.extend(classification.hits)
    floor = max(
        previous_floor,
        category_floor,
        CATEGORY_FLOORS[category],
        classification.floor if classification else EscalationLevel.L0_SELF,
    )
    if any(hit.strength == GuardStrength.STRONG for hit in hits):
        floor = EscalationLevel.L4_OWNER
    elif hits:
        floor = max(floor, EscalationLevel.L3_PRINCIPAL)
    if require_classification and (
        classification is None
        or (
            not classification.classified
            or not classification.delegable
            or classification.sensitive_topics
        )
    ):
        floor = max(floor, EscalationLevel.L3_PRINCIPAL)
    return Classification(
        category,
        classification.delegable if classification else False,
        classification.sensitive_topics if classification else (),
        tuple(dict.fromkeys(hits)),
        floor,
        classified=classification.classified if classification else False,
    )


def assess_guard(
    questions: Sequence[Question],
    *,
    brief: BriefEscalation | None = None,
    cwd: str = "",
    remote: str = "",
    previous_floor: EscalationLevel = EscalationLevel.L0_SELF,
    secret_present: bool = False,
    policy_available: bool = True,
    rules: Sequence[GuardRule] | None = None,
) -> Classification:
    """Deterministic pre-classification policy, never proof of local admission."""
    return derive_floor(
        questions,
        brief=brief,
        cwd=cwd,
        remote=remote,
        previous_floor=previous_floor,
        secret_present=secret_present,
        policy_available=policy_available,
        rules=rules,
        require_classification=False,
    )


def scan_assumptions(
    text: str, *, rules: Sequence[GuardRule] | None = None
) -> tuple[GuardHit, ...]:
    """Return guard hits only for the explicit line-start L0 marker."""
    try:
        policy = load_guard() if rules is None else rules
        return tuple(
            dict.fromkeys(
                hit
                for line in text.splitlines()
                if line.startswith(ASSUMPTION_PREFIX)
                for hit in scan(line[len(ASSUMPTION_PREFIX) :], rules=policy)
            )
        )
    except (OSError, ValueError, re.error):
        return (
            GuardHit(
                GuardTrigger.POLICY_UNAVAILABLE, GuardStrength.STRONG, POLICY_RULE, None
            ),
        )


def _has_evidence(answer: Answer, repo: Path) -> bool:
    if not answer.evidence:
        return False
    for evidence in answer.evidence:
        try:
            path = (repo / evidence.path).resolve()
            if not path.is_relative_to(repo.resolve()) or not evidence.quote.text:
                return False
            lines = path.read_text().splitlines()
            if evidence.line is None or evidence.line < 1 or evidence.line > len(lines):
                return False
            if evidence.quote.text not in "\n".join(
                lines[max(0, evidence.line - 4) : evidence.line + 3]
            ):
                return False
        except (OSError, UnicodeError):
            return False
    return True


def validate_answers(
    questions: Sequence[Question],
    answers: Sequence[Answer],
    *,
    level: EscalationLevel | DecisionLevel,
    repo: Path | str,
    clearances: Sequence[WeakClearance] = (),
    hits: Sequence[GuardHit],
    rules: Sequence[GuardRule] | None = None,
) -> tuple[ValidationFailure, ...]:
    """Validate a complete current-revision answer before any delivery."""
    level = EscalationLevel(level)
    failures: list[ValidationFailure] = []
    current = {
        question.id: question for question in questions if question.retired_rev is None
    }
    counts = Counter(answer.question for answer in answers)
    if any(counts[question] == 0 for question in current):
        failures.append(ValidationFailure.MISSING_QUESTION)
    if any(question not in current or count != 1 for question, count in counts.items()):
        failures.append(ValidationFailure.UNKNOWN_QUESTION)
    try:
        policy = load_guard() if rules is None else rules
        answer_texts = [answer.rationale.text for answer in answers]
        answer_texts.extend(
            answer.free_text.text for answer in answers if answer.free_text is not None
        )
        answer_texts.extend(clearance.reason.text for clearance in clearances)
        answer_hits = tuple(
            hit for text in answer_texts for hit in scan(text, rules=policy)
        )
        if any(hit.trigger == GuardTrigger.SECRET_PRESENT for hit in answer_hits):
            failures.append(ValidationFailure.SECRET_IN_ANSWER)
        if level < EscalationLevel.L4_OWNER and any(
            hit.strength == GuardStrength.STRONG for hit in answer_hits
        ):
            failures.append(ValidationFailure.ANSWER_GUARD_HIT)
    except (OSError, ValueError, re.error):
        failures.append(ValidationFailure.ANSWER_GUARD_HIT)
    if level == EscalationLevel.L3_PRINCIPAL:
        required = {
            (hit.rule_id, hit.trigger)
            for hit in hits
            if hit.strength == GuardStrength.WEAK
        }
        provided = Counter(
            (clearance.rule_id, clearance.trigger)
            for clearance in clearances
            if clearance.reason.text.strip()
        )
        if any(provided[key] != 1 for key in required):
            failures.append(ValidationFailure.WEAK_CLEARANCE_MISSING)
    for answer in answers:
        question = current.get(answer.question)
        if question is None:
            continue
        offered = {option.index for option in question.options}
        if (
            any(choice not in offered for choice in answer.choice)
            or len(set(answer.choice)) != len(answer.choice)
            or (not question.multi_select and len(answer.choice) != 1)
            or (
                question.multi_select
                and not answer.choice
                and not (answer.free_text and answer.free_text.text.strip())
            )
        ):
            failures.append(ValidationFailure.OPTION_NOT_OFFERED)
        if answer.free_text is not None and (
            not question.allows_free_text or level < EscalationLevel.L3_PRINCIPAL
        ):
            failures.append(ValidationFailure.FREE_TEXT_NOT_ALLOWED)
        if level <= EscalationLevel.L2_NORMAL and not _has_evidence(answer, Path(repo)):
            failures.append(ValidationFailure.NO_EVIDENCE)
    return tuple(dict.fromkeys(failures))
