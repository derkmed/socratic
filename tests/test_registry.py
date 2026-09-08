"""The mode registry (spec Approach section 1, CONTEXT: ModePolicy / ModeRegistry).

`DifficultyMode` alone is not the extension point, because behaviour differs per
mode in six places. `ModePolicy` bundles all six and `ModeRegistry` maps mode to
policy. **An `if mode ==` anywhere outside the registry is a bug** - asserted by
`test_no_module_branches_on_mode_outside_the_registry`, which is the half of
acceptance 39 that a registry entry alone cannot prove.
"""

import dataclasses
import pathlib
import re

import pytest

from socratic.domain.modes import (
    DifficultyMode,
    GradingStrategy,
    ProbeFailureBehavior,
    RenderHint,
)
from socratic.domain.registry import (
    BlankRange,
    ModePolicy,
    ModeRegistry,
    default_registry,
)
from socratic.domain.types import Blank, Option

THE_SIX = {
    "authoring_schema_fragment",
    "grading_strategy",
    "validate_blank",
    "render_hint",
    "blank_range",
    "probe_failure_behavior",
}


def _novice_blank(**overrides) -> Blank:
    fields = dict(
        blank_id="b1",
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("a", "b", "c"),
    )
    fields.update(overrides)
    return Blank(**fields)


def _advanced_blank(**overrides) -> Blank:
    fields = dict(blank_id="b1", mode=DifficultyMode.ADVANCED, rubric="Any phrasing.")
    fields.update(overrides)
    return Blank(**fields)


class TestModePolicy:
    def test_a_policy_bundles_all_six_per_mode_behaviours(self):
        assert {f.name for f in dataclasses.fields(ModePolicy)} >= THE_SIX

    @pytest.mark.parametrize("mode", [DifficultyMode.NOVICE, DifficultyMode.ADVANCED])
    def test_both_shipped_modes_populate_all_six(self, mode):
        policy = default_registry().policy_for(mode)
        for name in THE_SIX:
            assert getattr(policy, name) is not None, f"{mode} left {name} unset"


class TestTheShippedModes:
    def test_novice_is_graded_with_no_model_call(self):
        # D4: the Novice answer path costs zero model calls, without
        # qualification. The registry is where that is decided.
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        assert policy.grading_strategy is GradingStrategy.DETERMINISTIC

    def test_advanced_is_model_graded(self):
        policy = default_registry().policy_for(DifficultyMode.ADVANCED)
        assert policy.grading_strategy is GradingStrategy.MODEL_GRADED

    def test_novice_renders_an_option_bank_and_advanced_a_text_input(self):
        registry = default_registry()
        novice = registry.policy_for(DifficultyMode.NOVICE)
        advanced = registry.policy_for(DifficultyMode.ADVANCED)
        assert novice.render_hint is RenderHint.OPTION_BANK
        assert advanced.render_hint is RenderHint.TEXT_INPUT

    def test_blank_ranges_are_one_to_two_and_four_to_six(self):
        registry = default_registry()
        assert registry.policy_for(DifficultyMode.NOVICE).blank_range == BlankRange(1, 2)
        assert registry.policy_for(DifficultyMode.ADVANCED).blank_range == BlankRange(4, 6)

    def test_a_failed_probe_reopens_in_advanced_and_not_in_novice(self):
        registry = default_registry()
        assert (
            registry.policy_for(DifficultyMode.ADVANCED).probe_failure_behavior
            is ProbeFailureBehavior.REOPEN_BLANK
        )
        assert (
            registry.policy_for(DifficultyMode.NOVICE).probe_failure_behavior
            is ProbeFailureBehavior.CORRECT_AND_RESOLVE
        )

    def test_the_authoring_schema_fragment_is_a_json_schema_object(self):
        for mode in (DifficultyMode.NOVICE, DifficultyMode.ADVANCED):
            fragment = default_registry().policy_for(mode).authoring_schema_fragment
            assert isinstance(fragment, dict)
            assert "properties" in fragment


class TestBlankRange:
    def test_a_range_knows_whether_a_count_is_admissible(self):
        assert not BlankRange(1, 2).admits(0)
        assert BlankRange(1, 2).admits(1)
        assert BlankRange(1, 2).admits(2)
        assert not BlankRange(1, 2).admits(3)

    def test_an_inverted_range_is_rejected(self):
        with pytest.raises(ValueError):
            BlankRange(6, 4)


class TestTheConditionalValidator:
    """ADR-0004: `strict: true` holds structurally but cannot enforce the
    conditional invariants, so the domain validates them explicitly. This
    module declares `validate_blank` and populates it per mode; the
    `validate_quiz` entry point and the bounds it enforces are exercised in
    `tests/test_validation.py`."""

    def test_a_well_formed_novice_blank_passes(self):
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        assert policy.validate_blank(_novice_blank()) == ()

    def test_a_novice_blank_with_fewer_than_two_options_is_rejected(self):
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        errors = policy.validate_blank(_novice_blank(options=(Option("o1", "entropy"),)))
        assert errors
        assert any("option" in error for error in errors)

    def test_a_novice_blank_whose_correct_option_is_not_in_the_bank_is_rejected(self):
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        errors = policy.validate_blank(_novice_blank(correct_option_id="o9"))
        assert any("correct_option_id" in error for error in errors)

    def test_a_novice_blank_needs_three_hints_and_a_reinforcement(self):
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        assert policy.validate_blank(_novice_blank(hints=None))
        assert policy.validate_blank(_novice_blank(reinforcement=None))

    def test_a_well_formed_advanced_blank_passes(self):
        policy = default_registry().policy_for(DifficultyMode.ADVANCED)
        assert policy.validate_blank(_advanced_blank()) == ()

    def test_an_advanced_blank_with_no_rubric_is_rejected(self):
        policy = default_registry().policy_for(DifficultyMode.ADVANCED)
        errors = policy.validate_blank(_advanced_blank(rubric=None))
        assert any("rubric" in error for error in errors)

    def test_an_advanced_blank_carrying_novice_fields_is_rejected(self):
        # One type with nullable fields means the wrong mode's fields are
        # *representable*; the validator is what makes them inadmissible.
        policy = default_registry().policy_for(DifficultyMode.ADVANCED)
        errors = policy.validate_blank(
            _advanced_blank(options=(Option("o1", "entropy"), Option("o2", "enthalpy")))
        )
        assert errors

    def test_validating_a_blank_of_another_mode_is_an_error(self):
        policy = default_registry().policy_for(DifficultyMode.NOVICE)
        with pytest.raises(ValueError):
            policy.validate_blank(_advanced_blank())


class TestRegistryLookup:
    def test_an_unregistered_mode_is_an_error(self):
        with pytest.raises(KeyError):
            default_registry().policy_for("expert")

    def test_the_registry_lists_the_modes_it_knows(self):
        assert set(default_registry().modes()) == {
            DifficultyMode.NOVICE,
            DifficultyMode.ADVANCED,
        }

    def test_the_default_registry_is_not_shared_mutable_state(self):
        first, second = default_registry(), default_registry()
        first.register("expert", _stub_policy())
        assert "expert" not in second.modes()

    def test_registering_over_an_existing_mode_is_an_error(self):
        with pytest.raises(ValueError):
            default_registry().register(DifficultyMode.NOVICE, _stub_policy())

    def test_a_registry_can_be_built_from_nothing(self):
        assert ModeRegistry().modes() == ()


class TestAddingAThirdMode:
    """Spec acceptance 39: adding a hypothetical third difficulty mode requires
    editing only the registry."""

    def test_a_third_mode_is_one_registry_entry(self):
        registry = default_registry()
        registry.register("expert", _stub_policy())

        policy = registry.policy_for("expert")
        assert policy.grading_strategy is GradingStrategy.MODEL_GRADED
        assert policy.blank_range == BlankRange(8, 12)
        assert "expert" in registry.modes()

    def test_the_third_mode_flows_through_every_one_of_the_six_fields(self):
        registry = default_registry()
        registry.register("expert", _stub_policy())
        policy = registry.policy_for("expert")
        for name in THE_SIX:
            assert getattr(policy, name) is not None

    def test_the_third_modes_validator_is_reached_through_the_registry(self):
        registry = default_registry()
        registry.register("expert", _stub_policy())
        blank = Blank(blank_id="b1", mode="expert", rubric=None)
        assert registry.policy_for("expert").validate_blank(blank) == (
            "expert blanks need a rubric",
        )

    def test_no_module_branches_on_mode_outside_the_registry(self):
        # The other half of acceptance 39: a registry entry only makes a third
        # mode cheap if nothing else has an opinion about mode. Anything that
        # compares against a DifficultyMode member, or matches on one, is a bug.
        offenders = []
        source_root = pathlib.Path(__file__).resolve().parents[1] / "src" / "socratic"
        branch = re.compile(
            r"(?:if|elif|assert|while|case)\b[^\n]*"
            r"\b(?:DifficultyMode\.\w+|NOVICE|ADVANCED)\b"
        )
        for path in sorted(source_root.rglob("*.py")):
            if path.name in {"registry.py", "modes.py"}:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if branch.search(line):
                    location = path.relative_to(source_root)
                    offenders.append(f"{location}:{lineno}: {line.strip()}")
        assert offenders == [], "branching on mode outside the registry:\n" + "\n".join(
            offenders
        )


def _stub_policy() -> ModePolicy:
    def validate(blank: Blank) -> tuple[str, ...]:
        return () if blank.rubric else ("expert blanks need a rubric",)

    return ModePolicy(
        authoring_schema_fragment={"properties": {"rubric": {"type": "string"}}},
        grading_strategy=GradingStrategy.MODEL_GRADED,
        validate_blank=validate,
        render_hint=RenderHint.TEXT_INPUT,
        blank_range=BlankRange(8, 12),
        probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
    )
