"""Staged authoring rules in the registry (issue #9, spec
`docs/specs/skeleton-and-pedagogy-split.md`).

Authoring is two calls (D11, ADR-0011), so a blank is admissible at two
different moments: once the **skeleton** has landed, and once the **pedagogy
payload** has merged. `ModePolicy.rules_for(stage)` is the one place that choice
is made, and it is inside the registry — which is where per-mode rules are
required to live (CONTEXT: ModeRegistry).

The load-bearing property asserted here is that the relaxation is a
**decomposition, not a deletion**: for each shipped mode the complete rules are
exactly its skeleton rules plus its pedagogy rules, so nothing the conditional
validator refused before is admissible now at `COMPLETE`.

`tests/test_registry.py` keeps the six-field contract and the `if mode ==` scan;
this file only adds the stage.
"""

from __future__ import annotations

import pytest

from socratic.domain.modes import (
    DifficultyMode,
    GradingStrategy,
    ProbeFailureBehavior,
    RenderHint,
)
from socratic.domain.registry import (
    ADVANCED_POLICY,
    AuthoringStage,
    BlankRange,
    ModePolicy,
    NOVICE_POLICY,
    StageRules,
    default_registry,
)
from socratic.domain.types import Blank, Option

OPTIONS = (Option("o1", "entropy"), Option("o2", "enthalpy"))
HINTS = ("Think about disorder.", "The second law bounds it.", "It is entropy.")


def novice_blank(**overrides) -> Blank:
    """A **complete** Novice blank - every field the finished quiz needs."""
    fields = dict(
        blank_id="b1",
        mode=DifficultyMode.NOVICE,
        options=OPTIONS,
        correct_option_id="o1",
        reinforcement="Entropy is the one that never decreases.",
        hints=HINTS,
    )
    fields.update(overrides)
    return Blank(**fields)


def novice_skeleton_blank(**overrides) -> Blank:
    """A Novice blank as the **skeleton** call leaves it: no pedagogy at all."""
    return novice_blank(reinforcement=None, hints=None, **overrides)


def advanced_blank(**overrides) -> Blank:
    fields = dict(
        blank_id="b1",
        mode=DifficultyMode.ADVANCED,
        rubric="Names the quantity the second law bounds.",
    )
    fields.update(overrides)
    return Blank(**fields)


def validate(policy: ModePolicy, stage: AuthoringStage, blank: Blank):
    return policy.rules_for(stage).validate_blank(blank)


class TestTheStages:
    def test_the_two_calls_and_the_merged_result_each_name_a_stage(self):
        assert {stage.value for stage in AuthoringStage} == {
            "skeleton",
            "pedagogy",
            "complete",
        }

    def test_complete_is_the_six_field_pair_the_policy_already_carried(self):
        # The existing fields *are* the complete rules. Nothing moved.
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        rules = policy.rules_for(AuthoringStage.COMPLETE)

        assert rules.validate_blank is policy.validate_blank
        assert rules.schema_fragment is policy.authoring_schema_fragment


class TestTheNoviceStages:
    def test_a_skeleton_blank_with_no_pedagogy_is_admissible_at_skeleton(self):
        # This is the whole ticket: PR #32 could not ship a skeleton-only
        # payload because the validator refused a Novice blank without hints.
        assert (
            validate(NOVICE_POLICY, AuthoringStage.SKELETON, novice_skeleton_blank())
            == ()
        )

    def test_the_same_blank_is_inadmissible_at_complete(self):
        errors = validate(NOVICE_POLICY, AuthoringStage.COMPLETE, novice_skeleton_blank())

        assert any("hint" in error for error in errors)
        assert any("reinforcement" in error for error in errors)

    def test_the_skeleton_stage_still_demands_two_options(self):
        errors = validate(
            NOVICE_POLICY,
            AuthoringStage.SKELETON,
            novice_skeleton_blank(options=(Option("o1", "entropy"),)),
        )

        assert any("two options" in error for error in errors)

    def test_the_skeleton_stage_still_demands_a_correct_option_in_the_bank(self):
        errors = validate(
            NOVICE_POLICY,
            AuthoringStage.SKELETON,
            novice_skeleton_blank(correct_option_id="o9"),
        )

        assert any("correct_option_id" in error for error in errors)

    def test_the_skeleton_stage_still_refuses_a_rubric(self):
        errors = validate(
            NOVICE_POLICY,
            AuthoringStage.SKELETON,
            novice_skeleton_blank(rubric="not a novice field"),
        )

        assert any("rubric" in error for error in errors)

    def test_the_pedagogy_stage_owns_the_reinforcement_and_the_three_hints(self):
        assert validate(NOVICE_POLICY, AuthoringStage.PEDAGOGY, novice_blank()) == ()
        assert validate(
            NOVICE_POLICY, AuthoringStage.PEDAGOGY, novice_blank(hints=None)
        )
        assert validate(
            NOVICE_POLICY, AuthoringStage.PEDAGOGY, novice_blank(reinforcement=None)
        )

    def test_the_pedagogy_stage_has_no_opinion_about_the_option_bank(self):
        # The skeleton already settled the bank; asking twice would report the
        # same problem twice on a merged quiz.
        assert (
            validate(
                NOVICE_POLICY,
                AuthoringStage.PEDAGOGY,
                novice_blank(options=None, correct_option_id=None),
            )
            == ()
        )


class TestTheAdvancedStages:
    def test_the_rubric_is_a_skeleton_field(self):
        # An Advanced blank is graded against its rubric, so a quiz that is
        # playable on the skeleton alone must already carry one.
        assert (
            validate(ADVANCED_POLICY, AuthoringStage.SKELETON, advanced_blank()) == ()
        )
        errors = validate(
            ADVANCED_POLICY, AuthoringStage.SKELETON, advanced_blank(rubric=None)
        )
        assert any("rubric" in error for error in errors)

    def test_advanced_carries_no_per_blank_pedagogy(self):
        # ADR-0013: Advanced feedback is the reactive tutor line on the grading
        # response, so the payload has nothing per blank to add.
        assert (
            validate(ADVANCED_POLICY, AuthoringStage.PEDAGOGY, advanced_blank()) == ()
        )
        errors = validate(
            ADVANCED_POLICY, AuthoringStage.PEDAGOGY, advanced_blank(hints=HINTS)
        )
        assert any("hints" in error for error in errors)


class TestTheDecomposition:
    """`COMPLETE` is not a re-derivation of the old rules - it *is* the old
    rules, partitioned. Asserted rather than asserted-about, because this is
    what guarantees the relaxation weakened nothing."""

    @pytest.mark.parametrize(
        "policy,blank",
        [
            (NOVICE_POLICY, novice_blank()),
            (NOVICE_POLICY, novice_skeleton_blank()),
            (NOVICE_POLICY, novice_blank(options=(), correct_option_id=None)),
            (ADVANCED_POLICY, advanced_blank()),
            (ADVANCED_POLICY, advanced_blank(rubric=None, hints=HINTS)),
        ],
    )
    def test_complete_reports_exactly_skeleton_then_pedagogy(self, policy, blank):
        assert validate(policy, AuthoringStage.COMPLETE, blank) == (
            validate(policy, AuthoringStage.SKELETON, blank)
            + validate(policy, AuthoringStage.PEDAGOGY, blank)
        )

    def test_a_finished_novice_blank_still_passes_every_stage(self):
        for stage in AuthoringStage:
            assert validate(NOVICE_POLICY, stage, novice_blank()) == ()

    def test_a_finished_advanced_blank_still_passes_every_stage(self):
        for stage in AuthoringStage:
            assert validate(ADVANCED_POLICY, stage, advanced_blank()) == ()

    @pytest.mark.parametrize("stage", list(AuthoringStage))
    def test_a_blank_of_another_mode_is_an_error_at_every_stage(self, stage):
        with pytest.raises(ValueError):
            validate(NOVICE_POLICY, stage, advanced_blank())


class TestTheSchemaFragments:
    def test_the_skeleton_fragment_does_not_require_pedagogy_fields(self):
        required = set(
            NOVICE_POLICY.rules_for(AuthoringStage.SKELETON).schema_fragment[
                "required"
            ]
        )

        assert {"blank_id", "options", "correct_option_id"} <= required
        assert not required & {"hints", "reinforcement"}

    def test_the_pedagogy_fragment_requires_exactly_the_pedagogy_fields(self):
        required = set(
            NOVICE_POLICY.rules_for(AuthoringStage.PEDAGOGY).schema_fragment[
                "required"
            ]
        )

        assert required == {"blank_id", "reinforcement", "hints"}

    def test_the_advanced_skeleton_fragment_requires_the_rubric(self):
        required = set(
            ADVANCED_POLICY.rules_for(AuthoringStage.SKELETON).schema_fragment[
                "required"
            ]
        )

        assert required == {"blank_id", "rubric"}

    def test_every_stage_fragment_is_a_json_schema_object(self):
        for policy in (NOVICE_POLICY, ADVANCED_POLICY):
            for stage in AuthoringStage:
                fragment = policy.rules_for(stage).schema_fragment
                assert isinstance(fragment, dict)
                assert "properties" in fragment


class TestAPolicyThatPredatesStages:
    """A third mode is still one registry entry. One that declares no
    `stage_rules` is validated by its complete rules at *every* stage - the
    strict direction, so code it has never heard of cannot relax it."""

    def _six_field_policy(self) -> ModePolicy:
        def validate_blank(blank: Blank) -> tuple[str, ...]:
            return () if blank.rubric else ("expert blanks need a rubric",)

        return ModePolicy(
            authoring_schema_fragment={"properties": {"rubric": {"type": "string"}}},
            grading_strategy=GradingStrategy.MODEL_GRADED,
            validate_blank=validate_blank,
            render_hint=RenderHint.TEXT_INPUT,
            blank_range=BlankRange(8, 12),
            probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
        )

    @pytest.mark.parametrize("stage", list(AuthoringStage))
    def test_every_stage_falls_back_to_the_complete_rules(self, stage):
        policy = self._six_field_policy()
        rules = policy.rules_for(stage)

        assert rules.validate_blank is policy.validate_blank
        assert rules.schema_fragment is policy.authoring_schema_fragment

    def test_a_mode_may_declare_stage_rules_of_its_own(self):
        permissive = StageRules(
            schema_fragment={"properties": {}},
            validate_blank=lambda blank: (),
        )
        policy = ModePolicy(
            authoring_schema_fragment={"properties": {"rubric": {"type": "string"}}},
            grading_strategy=GradingStrategy.MODEL_GRADED,
            validate_blank=lambda blank: ("expert blanks need a rubric",),
            render_hint=RenderHint.TEXT_INPUT,
            blank_range=BlankRange(8, 12),
            probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
            stage_rules={AuthoringStage.SKELETON: permissive},
        )
        blank = Blank(blank_id="b1", mode="expert")

        assert policy.rules_for(AuthoringStage.SKELETON).validate_blank(blank) == ()
        assert policy.rules_for(AuthoringStage.COMPLETE).validate_blank(blank)
