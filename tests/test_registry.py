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

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "socratic"

EXEMPT_FROM_MODE_SCAN = ("domain/modes.py", "domain/registry.py")
"""The two modules that ARE the extension point, named by their path relative to
the package root rather than by filename.

By name, a future `src/socratic/adapters/registry.py` — an adapter package is
already planned in ADR-0002 — would be silently exempt, and the scan would keep
passing while enforcing less than it claims.
"""

_MODE_BRANCH = re.compile(
    r"(?:if|elif|assert|while|case)\b[^\n]*"
    r"\b(?:DifficultyMode\.\w+|NOVICE|ADVANCED)\b"
)


def find_mode_branches(
    source_root: pathlib.Path, exempt: "tuple[str, ...]"
) -> "list[str]":
    """Every line under `source_root` that branches on a difficulty mode.

    Returns `path:lineno: source` strings, so a failure names the offender
    rather than just its count. Extracted from the test that uses it so the
    scan itself can be tested against a synthetic tree — an unenforced guard
    and a passing one look identical from the outside.
    """
    exempted = set(exempt)
    offenders = []
    for path in sorted(source_root.rglob("*.py")):
        location = path.relative_to(source_root).as_posix()
        if location in exempted:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _MODE_BRANCH.search(line):
                offenders.append(f"{location}:{lineno}: {line.strip()}")
    return offenders


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
        offenders = find_mode_branches(SOURCE_ROOT, exempt=EXEMPT_FROM_MODE_SCAN)
        assert offenders == [], "branching on mode outside the registry:\n" + "\n".join(
            offenders
        )


class TestTheModeBranchScanItself:
    """The scan is the only thing enforcing "no `if mode ==` outside the
    registry", so a hole in it enforces less than it claims while still
    passing. These are the scan's own guard."""

    def test_a_branch_outside_the_exempt_modules_is_reported(self, tmp_path):
        _write(tmp_path / "domain" / "grading.py", "if mode == DifficultyMode.NOVICE:\n")
        offenders = find_mode_branches(tmp_path, exempt=EXEMPT_FROM_MODE_SCAN)
        assert [offender.split(":")[0] for offender in offenders] == [
            "domain/grading.py"
        ]

    def test_the_exempt_modules_may_branch(self, tmp_path):
        for module in EXEMPT_FROM_MODE_SCAN:
            _write(tmp_path / module, "if mode == DifficultyMode.ADVANCED:\n")
        assert find_mode_branches(tmp_path, exempt=EXEMPT_FROM_MODE_SCAN) == []

    def test_the_exemption_is_by_path_not_by_filename(self, tmp_path):
        # The bug this class was written for. An adapter package is already
        # planned (ADR-0002), and a `src/socratic/adapters/registry.py` would
        # be silently exempt if the scan matched a filename anywhere in the
        # tree. The exemption names two specific modules, not two names.
        _write(
            tmp_path / "adapters" / "registry.py",
            "if mode == DifficultyMode.NOVICE:\n",
        )
        _write(
            tmp_path / "adapters" / "modes.py",
            "if mode == DifficultyMode.NOVICE:\n",
        )
        offenders = find_mode_branches(tmp_path, exempt=EXEMPT_FROM_MODE_SCAN)
        assert sorted(offender.split(":")[0] for offender in offenders) == [
            "adapters/modes.py",
            "adapters/registry.py",
        ]

    def test_a_line_that_merely_mentions_a_mode_is_not_a_branch(self, tmp_path):
        _write(tmp_path / "domain" / "quiet.py", "mode = DifficultyMode.NOVICE\n")
        assert find_mode_branches(tmp_path, exempt=EXEMPT_FROM_MODE_SCAN) == []

    def test_the_report_names_the_file_the_line_and_the_source(self, tmp_path):
        _write(
            tmp_path / "domain" / "grading.py",
            "x = 1\nif mode == DifficultyMode.NOVICE:\n",
        )
        assert find_mode_branches(tmp_path, exempt=EXEMPT_FROM_MODE_SCAN) == [
            "domain/grading.py:2: if mode == DifficultyMode.NOVICE:"
        ]

    def test_every_exempt_module_actually_exists(self):
        # An exemption naming a module that has moved would silently stop
        # exempting anything, which is the same class of bug in reverse.
        for module in EXEMPT_FROM_MODE_SCAN:
            assert (SOURCE_ROOT / module).is_file(), f"{module} is no longer there"


def _write(path: pathlib.Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


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
