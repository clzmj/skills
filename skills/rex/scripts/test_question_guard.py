#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
from dataclasses import replace
from pathlib import Path
import tempfile
import tomllib
import unittest
from uuid import uuid4

import question_guard as guard
import question_model as model


def text(value: str) -> model.Untrusted:
    return model.Untrusted(value, model.UntrustedSource.MODEL)


def question(
    value: str = "Choose a fixture name?", *, multi_select=False
) -> model.Question:
    identity = model.QuestionId(uuid4())
    payload = {
        "id": str(identity),
        "text": value,
        "header": None,
        "options": [
            {"index": 0, "label": "Alpha", "description": None},
            {"index": 1, "label": "Beta", "description": None},
        ],
        "multi_select": multi_select,
        "allows_free_text": False,
    }
    return model.Question(
        identity,
        model.NativeCallId("call"),
        text(value),
        None,
        (
            model.Option(model.OptionIndex(0), text("Alpha")),
            model.Option(model.OptionIndex(1), text("Beta")),
        ),
        multi_select,
        False,
        model.NativeQuestionKey(codex_id="native"),
        None,
        model.digest(payload),
    )


def changed_question(
    item: model.Question, *, header=None, options=None, allows_free_text=None
):
    next_header = item.header if header is None else header
    next_options = item.options if options is None else options
    next_free_text = (
        item.allows_free_text if allows_free_text is None else allows_free_text
    )
    payload = {
        "id": str(item.id),
        "text": item.text.text,
        "header": next_header.text if next_header else None,
        "options": [
            {
                "index": option.index,
                "label": option.label.text,
                "description": option.description.text if option.description else None,
            }
            for option in next_options
        ],
        "multi_select": item.multi_select,
        "allows_free_text": next_free_text,
    }
    return replace(
        item,
        header=next_header,
        options=next_options,
        allows_free_text=next_free_text,
        digest=model.digest(payload),
    )


def answer(
    item: model.Question, rationale: str = "This matches the example."
) -> model.Answer:
    return model.Answer(
        item.id, (model.OptionIndex(0),), None, text(rationale), (), None
    )


AFFIRMATIVE = model.Classification(
    model.Category.REPO_CONVENTION,
    True,
    (),
    (),
    model.EscalationLevel.L1_CHEAP,
    classified=True,
)


class GuardTests(unittest.TestCase):
    def test_missing_classification_never_affirms_delegation(self):
        result = guard.derive_floor((question(),))
        self.assertFalse(result.delegable)
        self.assertGreaterEqual(result.floor, model.EscalationLevel.L3_PRINCIPAL)

    def test_missing_classifier_is_distinct_from_a_not_delegable_classification(self):
        missing = guard.derive_floor((question(),))
        refused = guard.derive_floor(
            (question(),), classification=replace(AFFIRMATIVE, delegable=False)
        )
        self.assertFalse(missing.classified)
        self.assertTrue(refused.classified)
        self.assertFalse(missing.delegable)
        self.assertFalse(refused.delegable)
        self.assertFalse(
            guard.derive_floor((question(),), classification=missing).classified
        )

    def test_rule_examples(self):
        for rule in guard.load_guard():
            for value in rule.examples_hit:
                with self.subTest(rule=rule.id, text=value):
                    self.assertRegex(guard.normalize(value), rule.pattern)
            for value in rule.examples_miss:
                with self.subTest(rule=rule.id, text=value):
                    self.assertNotRegex(guard.normalize(value), rule.pattern)

    def test_nfkc_and_unicode_whitespace_and_retired_floor(self):
        retired = replace(
            question("ＧＩＴ\u2003ＰＵＳＨ"), retired_rev=model.BatchRev(2)
        )
        result = guard.derive_floor((retired, question()))
        self.assertEqual(result.floor, model.EscalationLevel.L4_OWNER)
        self.assertEqual(
            guard.derive_floor((question(),), previous_floor=result.floor).floor,
            result.floor,
        )

    def test_unicode_format_characters_cannot_hide_sensitive_actions(self):
        for value in ("git pu\u200bsh to main?", "turn off M\u00adFA?"):
            with self.subTest(value=value):
                result = guard.derive_floor(
                    (question(value),), classification=AFFIRMATIVE
                )
                self.assertEqual(result.floor, model.EscalationLevel.L4_OWNER)

    def test_headers_options_and_context_are_guarded(self):
        base = question()
        variants = (
            changed_question(base, header=text("Change DNS?")),
            changed_question(
                base,
                options=(model.Option(model.OptionIndex(0), text("Buy a license")),),
            ),
            changed_question(
                base,
                options=(
                    model.Option(
                        model.OptionIndex(0), text("Alpha"), text("Deploy to prod")
                    ),
                ),
            ),
        )
        for item in variants:
            self.assertEqual(
                guard.derive_floor((item,)).floor, model.EscalationLevel.L4_OWNER
            )
        self.assertEqual(
            guard.derive_floor((base,), cwd="/work/la_cana").floor,
            model.EscalationLevel.L4_OWNER,
        )

    def test_realistic_metadata_does_not_quarantine_benign_questions(self):
        base = question()
        cwd = "/Users/carlos/Developer/github/clzmj/skills-wt/qe-stack"
        remote = "https://github.com/clzmj/skills.git"
        result = guard.derive_floor(
            (base,), cwd=cwd, remote=remote, classification=AFFIRMATIVE
        )
        self.assertEqual(result.floor, model.EscalationLevel.L1_CHEAP)
        self.assertNotIn(
            model.GuardTrigger.SECRET_PRESENT, [hit.trigger for hit in result.hits]
        )
        sensitive = guard.derive_floor((base,), cwd=cwd + "/la_cana")
        self.assertEqual(sensitive.floor, model.EscalationLevel.L4_OWNER)
        self.assertIn(
            model.GuardTrigger.LA_CANA, [hit.trigger for hit in sensitive.hits]
        )

    def test_category_classifier_and_missing_policy_can_only_raise(self):
        base = question()
        self.assertEqual(
            guard.derive_floor((base,), classification=AFFIRMATIVE).floor,
            model.EscalationLevel.L1_CHEAP,
        )
        classification = model.Classification(
            model.Category.DESIGN_CHOICE, False, (), (), model.EscalationLevel.L2_NORMAL
        )
        self.assertEqual(
            guard.derive_floor((base,), classification=classification).floor,
            model.EscalationLevel.L3_PRINCIPAL,
        )
        low_classification = model.Classification(
            model.Category.REPO_CONVENTION, True, (), (), model.EscalationLevel.L1_CHEAP
        )
        self.assertEqual(
            guard.derive_floor(
                (base,),
                category=model.Category.PRODUCT_SCOPE,
                classification=low_classification,
            ).floor,
            model.EscalationLevel.L4_OWNER,
        )
        result = guard.derive_floor((base,), policy_available=False)
        self.assertEqual(result.floor, model.EscalationLevel.L4_OWNER)
        self.assertIn(
            model.GuardTrigger.POLICY_UNAVAILABLE, [hit.trigger for hit in result.hits]
        )

    def test_raw_secret_is_quarantined_after_redaction(self):
        result = guard.derive_floor(
            (question("Use https://user:fixture@example.test?"),)
        )
        self.assertEqual(result.floor, model.EscalationLevel.L4_OWNER)
        self.assertIn(
            model.GuardTrigger.SECRET_PRESENT, [hit.trigger for hit in result.hits]
        )

    def test_brief_is_strict_and_rejects_guarded_assumptions(self):
        valid = '```toml qe-escalation\nschema="qe.brief/1"\nproject="personal"\nl0_assume=["fixture values"]\nask_carlos=["database schema"]\n```'
        brief = guard.parse_brief(valid)
        self.assertEqual(brief.l0_assume, ("fixture values",))
        self.assertEqual(
            guard.derive_floor(
                (question("Choose database schema?"),), brief=brief
            ).floor,
            model.EscalationLevel.L4_OWNER,
        )
        self.assertEqual(
            guard.derive_floor(
                (question(),), brief=brief, classification=AFFIRMATIVE
            ).floor,
            model.EscalationLevel.L1_CHEAP,
        )
        for invalid in (
            valid.replace("fixture values", "dict key"),
            valid.replace("personal", "unknown"),
            valid[:-3],
            valid + "\n" + valid,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(guard.BriefInvalid):
                    guard.parse_brief(invalid)

    def test_brief_l0_cannot_override_ask_carlos_phrases(self):
        with self.assertRaises(guard.BriefInvalid):
            guard.parse_brief(
                '```toml qe-escalation\nschema="qe.brief/1"\nproject="personal"\nl0_assume=["database schema"]\nask_carlos=["database schema"]\n```'
            )

    def test_no_brief_and_la_cana_detection(self):
        self.assertEqual(guard.parse_brief(None).l0_assume, ())
        self.assertEqual(
            guard.parse_brief("Ordinary brief", cwd="/work/la-caña").project,
            model.BriefProject.LA_CANA,
        )

    def test_assumption_scan_only_accepts_line_start_markers(self):
        self.assertFalse(
            guard.scan_assumptions("I may git push.\n ASSUMPTION: git push")
        )
        self.assertTrue(guard.scan_assumptions("Result\nASSUMPTION: git push"))

    def corpus_floors(self):
        data = tomllib.loads(
            (
                Path(__file__).resolve().parents[3] / "tests/qe/guard_corpus.toml"
            ).read_text()
        )
        floors = {
            group: {
                value: guard.derive_floor(
                    (question(value),), classification=AFFIRMATIVE
                ).floor
                for value in data[group]
            }
            for group in ("sensitive", "adversarial", "benign")
        }
        return data, floors

    @unittest.expectedFailure
    def test_corpus_adversarial_target(self):
        """ADR-0002 5.2.2 corpus gap, pending Carlos decision."""
        _, floors = self.corpus_floors()
        self.assertTrue(all(floor >= 3 for floor in floors["adversarial"].values()))

    @unittest.expectedFailure
    def test_corpus_benign_l4_target(self):
        """ADR-0002 5.2.2 corpus gap, pending Carlos decision."""
        _, floors = self.corpus_floors()
        self.assertLessEqual(
            sum(floor == 4 for floor in floors["benign"].values())
            / len(floors["benign"]),
            0.05,
        )

    def test_corpus_targets_and_exact_current_misses(self):
        data, floors = self.corpus_floors()
        for group in floors:
            self.assertGreaterEqual(len(floors[group]), 30)
        self.assertTrue(all(floor == 4 for floor in floors["sensitive"].values()))
        # Keep measured limitations visible instead of weakening labels or rules.
        self.assertEqual(
            [value for value, floor in floors["adversarial"].items() if floor < 3],
            data["known_adversarial_misses"],
        )
        self.assertEqual(
            [value for value, floor in floors["benign"].items() if floor == 4],
            data["known_benign_quarantines"],
        )
        self.assertLessEqual(
            sum(floor >= 3 for floor in floors["benign"].values())
            / len(floors["benign"]),
            0.30,
        )


class ValidationTests(unittest.TestCase):
    def test_empty_multiselect_is_not_an_answer(self):
        item = question(multi_select=True)
        response = replace(answer(item), choice=())
        self.assertIn(
            model.ValidationFailure.OPTION_NOT_OFFERED,
            self.validate(item, (response,)),
        )

    def test_single_select_free_text_still_requires_one_choice(self):
        item = changed_question(question(), allows_free_text=True)
        response = replace(
            answer(item), choice=(), free_text=text("Neither, use Gamma")
        )
        self.assertIn(
            model.ValidationFailure.OPTION_NOT_OFFERED,
            self.validate(item, (response,), level=model.EscalationLevel.L4_OWNER),
        )

    def test_guard_hits_must_be_supplied_explicitly(self):
        item = question("Use this dict key?")
        with self.assertRaises(TypeError):
            guard.validate_answers(  # ty: ignore[missing-argument]
                (item,),
                (answer(item),),
                level=model.EscalationLevel.L3_PRINCIPAL,
                repo=Path.cwd(),
            )

    def validate(
        self, item, answers, *, level=model.EscalationLevel.L3_PRINCIPAL, **kwargs
    ):
        kwargs.setdefault("hits", ())
        return guard.validate_answers(
            (item,), answers, level=level, repo=Path.cwd(), **kwargs
        )

    def test_current_questions_once_and_offered_single_selection(self):
        item = question()
        response = answer(item)
        self.assertFalse(self.validate(item, (response,)))
        self.assertIn(model.ValidationFailure.MISSING_QUESTION, self.validate(item, ()))
        self.assertIn(
            model.ValidationFailure.UNKNOWN_QUESTION,
            self.validate(item, (response, response)),
        )
        self.assertIn(
            model.ValidationFailure.OPTION_NOT_OFFERED,
            self.validate(item, (replace(response, choice=()),)),
        )
        self.assertIn(
            model.ValidationFailure.OPTION_NOT_OFFERED,
            self.validate(item, (replace(response, choice=(model.OptionIndex(7),)),)),
        )
        self.assertIn(
            model.ValidationFailure.UNKNOWN_QUESTION,
            self.validate(replace(item, retired_rev=model.BatchRev(2)), (response,)),
        )

    def test_free_text_is_only_allowed_at_l3_l4_when_offered(self):
        item = question()
        response = replace(answer(item), free_text=text("A different name"))
        self.assertIn(
            model.ValidationFailure.FREE_TEXT_NOT_ALLOWED,
            self.validate(item, (response,)),
        )
        self.assertNotIn(
            model.ValidationFailure.FREE_TEXT_NOT_ALLOWED,
            self.validate(changed_question(item, allows_free_text=True), (response,)),
        )
        self.assertIn(
            model.ValidationFailure.FREE_TEXT_NOT_ALLOWED,
            self.validate(
                changed_question(item, allows_free_text=True),
                (response,),
                level=model.EscalationLevel.L2_NORMAL,
            ),
        )

    def test_l3_requires_one_written_clearance_per_weak_rule(self):
        item = question("Use this dict key?")
        hits = guard.derive_floor((item,)).hits
        response = answer(item)
        self.assertIn(
            model.ValidationFailure.WEAK_CLEARANCE_MISSING,
            self.validate(item, (response,), hits=hits),
        )
        clearance = model.WeakClearance(
            model.RuleId("security.weak"),
            model.GuardTrigger.SECURITY,
            text("The key is a dictionary field."),
        )
        self.assertFalse(
            self.validate(item, (response,), hits=hits, clearances=(clearance,))
        )
        self.assertIn(
            model.ValidationFailure.WEAK_CLEARANCE_MISSING,
            self.validate(
                item,
                (response,),
                hits=hits,
                clearances=(replace(clearance, reason=text("")),),
            ),
        )

    def test_plain_int_l3_still_requires_written_weak_clearance(self):
        item = question("Use this dictionary key?")
        failures = self.validate(
            item, (answer(item),), level=3, hits=guard.derive_floor((item,)).hits
        )
        self.assertIn(model.ValidationFailure.WEAK_CLEARANCE_MISSING, failures)

    def test_strong_answer_text_is_refused_below_l4_and_secrets_at_all_levels(self):
        item = question()
        response = answer(item, "Run git push")
        self.assertIn(
            model.ValidationFailure.ANSWER_GUARD_HIT, self.validate(item, (response,))
        )
        self.assertFalse(
            self.validate(item, (response,), level=model.EscalationLevel.L4_OWNER)
        )
        for level in (
            model.EscalationLevel.L1_CHEAP,
            model.EscalationLevel.L3_PRINCIPAL,
            model.EscalationLevel.L4_OWNER,
        ):
            self.assertIn(
                model.ValidationFailure.SECRET_IN_ANSWER,
                self.validate(item, (answer(item, "token=fixture"),), level=level),
            )

    def test_evidence_stays_in_repo_and_matches_within_three_lines(self):
        item = question()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "example.txt").write_text(
                "one\ntwo\nthree\nexpected quote\nfive\nsix\nseven\neight\n"
            )
            evidence = model.EvidenceRef("example.txt", 1, text("expected quote"))
            response = replace(answer(item), evidence=(evidence,))

            def validate(value):
                return guard.validate_answers(
                    (item,),
                    (value,),
                    level=model.EscalationLevel.L1_CHEAP,
                    repo=repo,
                    hits=(),
                )

            self.assertFalse(validate(response))
            self.assertIn(
                model.ValidationFailure.NO_EVIDENCE,
                validate(replace(response, evidence=(replace(evidence, line=8),))),
            )
            self.assertIn(
                model.ValidationFailure.NO_EVIDENCE,
                validate(
                    replace(
                        response, evidence=(replace(evidence, path="../outside.txt"),)
                    )
                ),
            )
            self.assertIn(
                model.ValidationFailure.NO_EVIDENCE,
                validate(
                    replace(
                        response,
                        evidence=(evidence, replace(evidence, path="../outside.txt")),
                    )
                ),
            )

    def test_clearance_secret_and_strong_text_are_refused(self):
        item = question("Use a dictionary key?")
        for reason, failure in (
            ("token=fixture", model.ValidationFailure.SECRET_IN_ANSWER),
            ("We can git push.", model.ValidationFailure.ANSWER_GUARD_HIT),
        ):
            clearance = model.WeakClearance(
                model.RuleId("security.weak"), model.GuardTrigger.SECURITY, text(reason)
            )
            self.assertIn(
                failure,
                self.validate(
                    item,
                    (answer(item),),
                    hits=guard.derive_floor((item,)).hits,
                    clearances=(clearance,),
                ),
            )


if __name__ == "__main__":
    unittest.main()
