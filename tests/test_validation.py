"""The conditional quiz validator (spec Approach sections 1-2, D2, D5).

`strict: true` guarantees every key is present; it cannot guarantee that the
*right* ones are populated for the mode in play. That gap is what
`validate_quiz` closes, and it closes it by dispatching to
`ModePolicy.validate_blank` through the registry rather than branching on mode
itself.

Two bounds, two independent rejections (acceptance 25): the mode's
`blank_range` rejects first, and the 20-blank storage cap from ADR-0005 rejects
on its own even when a mode's range would admit the count.
"""

import dataclasses

import pytest

from socratic.domain import validation
from socratic.domain.ids import new_quiz_session_id
from socratic.domain.modes import DifficultyMode
from socratic.domain.registry import (
    AuthoringStage,
    BlankRange,
    ModePolicy,
    ModeRegistry,
    default_registry,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment


def _novice_blank(blank_id: str = "b1", **overrides) -> Blank:
    fields = dict(
        blank_id=blank_id,
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("Think about disorder.", "It is the S term.", "It is entropy."),
    )
    fields.update(overrides)
    return Blank(**fields)


def _advanced_blank(blank_id: str = "b1", **overrides) -> Blank:
    fields = dict(
        blank_id=blank_id,
        mode=DifficultyMode.ADVANCED,
        rubric="Any phrasing naming the disorder term.",
    )
    fields.update(overrides)
    return Blank(**fields)


def _quiz(blanks: tuple[Blank, ...], mode) -> Quiz:
    """A quiz whose explanation references every blank exactly once."""
    explanation: list = [TextSegment("The second law says ")]
    for blank in blanks:
        explanation.append(BlankSegment(blank.blank_id))
        explanation.append(TextSegment(" always increases. "))
    return Quiz(
        quiz_session_id=new_quiz_session_id(),
        mode=mode,
        topic="the second law of thermodynamics",
        explanation=tuple(explanation),
        blanks=blanks,
        recap="Entropy never decreases in an isolated system.",
    )


def _novice_quiz(count: int = 2, **blank_overrides) -> Quiz:
    blanks = tuple(
        _novice_blank(f"b{n}", **blank_overrides) for n in range(1, count + 1)
    )
    return _quiz(blanks, DifficultyMode.NOVICE)


def _advanced_quiz(count: int = 4, **blank_overrides) -> Quiz:
    blanks = tuple(
        _advanced_blank(f"b{n}", **blank_overrides) for n in range(1, count + 1)
    )
    return _quiz(blanks, DifficultyMode.ADVANCED)


def _policy_with(base: ModePolicy, **overrides) -> ModePolicy:
    fields = dict(
        authoring_schema_fragment=base.authoring_schema_fragment,
        grading_strategy=base.grading_strategy,
        validate_blank=base.validate_blank,
        render_hint=base.render_hint,
        blank_range=base.blank_range,
        probe_failure_behavior=base.probe_failure_behavior,
    )
    fields.update(overrides)
    return ModePolicy(**fields)


def _wide_registry() -> ModeRegistry:
    """A hypothetical third mode whose `blank_range` admits more than the
    storage cap, so the two bounds can be told apart."""
    return ModeRegistry(
        {
            "expert": _policy_with(
                default_registry().policy_for(DifficultyMode.ADVANCED),
                validate_blank=lambda blank: (),
                blank_range=BlankRange(8, 25),
            )
        }
    )


def _expert_quiz(count: int) -> Quiz:
    blanks = tuple(
        Blank(blank_id=f"b{n}", mode="expert", rubric="Any phrasing.")
        for n in range(1, count + 1)
    )
    return _quiz(blanks, "expert")


class TestAWellFormedQuiz:
    def test_a_well_formed_novice_quiz_validates(self):
        assert validation.validate_quiz(_novice_quiz(), default_registry()) == ()

    def test_a_well_formed_advanced_quiz_validates(self):
        assert validation.validate_quiz(_advanced_quiz(), default_registry()) == ()

    def test_the_default_registry_is_the_default_argument(self):
        # A caller with no registry of its own still dispatches through one -
        # never through a branch on mode.
        assert validation.validate_quiz(_novice_quiz()) == ()

    def test_ensuring_a_well_formed_quiz_returns_it_unchanged(self):
        quiz = _advanced_quiz()
        assert validation.ensure_valid_quiz(quiz, default_registry()) is quiz


class TestTheConditionalBlankRules:
    """Acceptance 27, dispatched entirely through `ModePolicy.validate_blank`."""

    def test_a_novice_blank_with_fewer_than_two_options_is_rejected(self):
        quiz = _novice_quiz(options=(Option("o1", "entropy"),))
        errors = validation.validate_quiz(quiz, default_registry())
        assert errors
        assert any("option" in error for error in errors)

    def test_a_novice_blank_whose_correct_option_is_not_in_the_bank_is_rejected(self):
        quiz = _novice_quiz(correct_option_id="o9")
        errors = validation.validate_quiz(quiz, default_registry())
        assert any("correct_option_id" in error for error in errors)

    def test_an_advanced_blank_with_no_rubric_is_rejected(self):
        quiz = _advanced_quiz(rubric=None)
        errors = validation.validate_quiz(quiz, default_registry())
        assert any("rubric" in error for error in errors)

    def test_a_blank_error_names_the_blank_it_came_from(self):
        blanks = (
            _advanced_blank("b1"),
            _advanced_blank("b2"),
            _advanced_blank("b3", rubric=None),
            _advanced_blank("b4"),
        )
        errors = validation.validate_quiz(
            _quiz(blanks, DifficultyMode.ADVANCED), default_registry()
        )
        assert [error for error in errors if error.startswith("b3:")]
        assert not [error for error in errors if error.startswith("b1:")]

    def test_the_rules_are_the_registrys_and_not_a_second_copy(self):
        # If the validator carried its own per-mode rules, swapping the
        # policy's `validate_blank` would not change the verdict.
        seen: list[Blank] = []

        def validate(blank: Blank) -> tuple[str, ...]:
            seen.append(blank)
            return ("the policy said no",)

        registry = ModeRegistry(
            {
                DifficultyMode.NOVICE: _policy_with(
                    default_registry().policy_for(DifficultyMode.NOVICE),
                    validate_blank=validate,
                )
            }
        )
        errors = validation.validate_quiz(_novice_quiz(count=1), registry)
        assert errors == ("b1: the policy said no",)
        assert len(seen) == 1


class TestTheTwoBounds:
    """Acceptance 25: two bounds, two independent rejections."""

    def test_more_blanks_than_the_modes_range_is_rejected(self):
        errors = validation.validate_quiz(_novice_quiz(count=3), default_registry())
        assert any("blank_range" in error for error in errors)

    def test_fewer_blanks_than_the_modes_range_is_rejected(self):
        errors = validation.validate_quiz(_advanced_quiz(count=3), default_registry())
        assert any("blank_range" in error for error in errors)

    def test_the_storage_cap_rejects_independently_of_the_blank_range(self):
        # A mode whose range admits 25 blanks still cannot store 21: the cap is
        # a storage invariant, not a restatement of `blank_range`.
        errors = validation.validate_quiz(_expert_quiz(21), _wide_registry())
        assert errors
        assert any(str(validation.STORAGE_BLANK_CAP) in error for error in errors)
        assert not any("blank_range" in error for error in errors)

    def test_exactly_the_cap_is_admissible(self):
        assert validation.validate_quiz(_expert_quiz(20), _wide_registry()) == ()

    def test_the_range_rejects_first_when_both_bounds_fire(self):
        # Spec Approach section 2: where they disagree, `blank_range` rejects
        # first - the cap is the one that should never fire in practice.
        errors = validation.validate_quiz(_novice_quiz(count=21), default_registry())
        assert "blank_range" in errors[0]
        assert any(str(validation.STORAGE_BLANK_CAP) in error for error in errors)

    def test_the_cap_is_the_twenty_of_adr_0005(self):
        assert validation.STORAGE_BLANK_CAP == 20


class TestReportingEveryProblem:
    def test_every_problem_is_reported_not_just_the_first(self):
        quiz = _novice_quiz(count=3, options=(Option("o1", "entropy"),))
        errors = validation.validate_quiz(quiz, default_registry())
        assert any("blank_range" in error for error in errors)
        assert len([error for error in errors if "option" in error]) == 3

    def test_the_raising_entry_point_carries_every_error(self):
        quiz = _novice_quiz(count=3, options=(Option("o1", "entropy"),))
        with pytest.raises(validation.QuizValidationError) as caught:
            validation.ensure_valid_quiz(quiz, default_registry())
        assert caught.value.errors == validation.validate_quiz(
            quiz, default_registry()
        )
        assert len(caught.value.errors) > 1


class TestModeAgreement:
    def test_a_blank_of_another_mode_is_reported_not_raised(self):
        # One attempt, one mode (CONTEXT: Mode toggle). A mismatched blank
        # makes the policy's own validator raise, so the quiz-level check
        # catches it first and reports it alongside everything else.
        quiz = _quiz((_advanced_blank("b1"),), DifficultyMode.NOVICE)
        errors = validation.validate_quiz(quiz, default_registry())
        assert any("mode" in error for error in errors)

    def test_a_mismatched_blank_does_not_suppress_the_other_errors(self):
        quiz = _quiz(
            (_advanced_blank("b1"), _novice_blank("b2", options=())),
            DifficultyMode.NOVICE,
        )
        errors = validation.validate_quiz(quiz, default_registry())
        assert any(error.startswith("b1:") and "mode" in error for error in errors)
        assert any(error.startswith("b2:") and "option" in error for error in errors)

    def test_a_quiz_in_an_unregistered_mode_is_an_error(self):
        with pytest.raises(KeyError):
            validation.validate_quiz(_novice_quiz(), ModeRegistry())


class TestValidatingAtAStage(object):
    """Issue #9. Authoring is two calls, so "valid" needs to say *when*.

    `stage` defaults to `COMPLETE`, which is what every test above means and
    what every caller that has never heard of the split already meant.
    """

    def _skeleton_novice_quiz(self, count: int = 2) -> Quiz:
        return _novice_quiz(count, reinforcement=None, hints=None)

    def test_a_skeleton_only_novice_quiz_is_valid_at_the_skeleton_stage(self):
        # The thing PR #32 reported as impossible.
        assert (
            validation.validate_quiz(
                self._skeleton_novice_quiz(),
                default_registry(),
                stage=AuthoringStage.SKELETON,
            )
            == ()
        )

    def test_the_same_quiz_is_invalid_at_the_complete_stage(self):
        errors = validation.validate_quiz(
            self._skeleton_novice_quiz(), default_registry()
        )
        assert any("hint" in error for error in errors)
        assert any("reinforcement" in error for error in errors)

    def test_complete_is_the_default_stage(self):
        assert validation.validate_quiz(
            self._skeleton_novice_quiz(), default_registry()
        ) == validation.validate_quiz(
            self._skeleton_novice_quiz(),
            default_registry(),
            stage=AuthoringStage.COMPLETE,
        )

    def test_the_conditional_rules_still_bite_at_the_skeleton_stage(self):
        # Relaxed is not disabled: the option bank is a skeleton field.
        errors = validation.validate_quiz(
            _novice_quiz(options=(Option("o1", "entropy"),), hints=None),
            default_registry(),
            stage=AuthoringStage.SKELETON,
        )
        assert any("option" in error for error in errors)

    def test_an_advanced_skeleton_still_needs_its_rubric(self):
        errors = validation.validate_quiz(
            _advanced_quiz(rubric=None),
            default_registry(),
            stage=AuthoringStage.SKELETON,
        )
        assert any("rubric" in error for error in errors)

    def test_the_blank_range_binds_at_every_stage(self):
        # The skeleton declares every blank, so the pedagogical bound is
        # knowable from it alone.
        for stage in AuthoringStage:
            errors = validation.validate_quiz(
                self._skeleton_novice_quiz(count=3),
                default_registry(),
                stage=stage,
            )
            assert any("blank_range" in error for error in errors)

    def test_a_completed_quiz_needs_a_recap(self):
        # The recap is the only pedagogy an Advanced quiz has, so without this
        # an empty payload would merge and be called complete.
        quiz = dataclasses.replace(_advanced_quiz(), recap="   ")
        assert any(
            "recap" in error
            for error in validation.validate_quiz(quiz, default_registry())
        )

    def test_a_skeleton_needs_no_recap(self):
        quiz = dataclasses.replace(self._skeleton_novice_quiz(), recap="")
        assert (
            validation.validate_quiz(
                quiz, default_registry(), stage=AuthoringStage.SKELETON
            )
            == ()
        )

    def test_ensure_valid_quiz_takes_the_same_stage(self):
        quiz = self._skeleton_novice_quiz()
        assert (
            validation.ensure_valid_quiz(
                quiz, default_registry(), stage=AuthoringStage.SKELETON
            )
            is quiz
        )
        with pytest.raises(validation.QuizValidationError):
            validation.ensure_valid_quiz(quiz, default_registry())

    def test_a_mode_that_declares_no_stage_rules_is_validated_as_complete(self):
        # `_wide_registry`'s expert policy carries the six fields and nothing
        # else, so every stage is its complete validator.
        for stage in AuthoringStage:
            assert (
                validation.validate_quiz(_expert_quiz(20), _wide_registry(), stage=stage)
                == ()
            )
