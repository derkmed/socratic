"""The mode registry - the only place difficulty mode is ever branched on.

`DifficultyMode` alone is not the extension point, because behaviour differs per
mode in six places: the authoring schema fragment, the grading strategy, the
validator rule, the render hint, `blank_range`, and `probe_failure_behavior`.
`ModePolicy` bundles all six; `ModeRegistry` maps mode to policy.

**Adding a third mode is one registry entry.** An `if mode ==` anywhere outside
this module is a bug, and `tests/test_registry.py` scans the package for one.

This module declares `validate_blank` as a policy field and populates it per
mode. The `validate_quiz` entry point, and the bounds it enforces (the 20-blank
storage cap, and `blank_range` rejecting first), live in `validation.py`, which
reaches these rules through the registry rather than restating them.

**Rules also vary by authoring stage** (issue #9, D11, ADR-0011). Authoring is
two calls, so a mode asks less of a blank once the *skeleton* has landed than
it does of a finished one. `AuthoringStage` names the two calls and the merged
result; `StageRules` bundles a stage's output schema with its validator; and
`ModePolicy.rules_for` is the one place a stage's rules are chosen. The six
fields are unchanged and are the `COMPLETE` rules - and each mode's complete
validator is *composed* from its per-stage ones, so the stage is a
decomposition of the conditional validator rather than a relaxation of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from socratic.domain.modes import (
    DifficultyMode,
    GradingStrategy,
    ProbeFailureBehavior,
    RenderHint,
)
from socratic.domain.types import Blank

ModeKey = str
"""What the registry is keyed by. `DifficultyMode` is a `str` enum, so a further
mode is a new key rather than a change to every module that names the type."""

BlankValidator = Callable[[Blank], "tuple[str, ...]"]
"""Returns the reasons a blank is inadmissible - empty when it is fine.

Errors rather than an exception, because a malformed authoring response usually
has several problems and a reviewer wants all of them at once.
"""


class AuthoringStage(str, Enum):
    """How far through authoring a blank is (D11, ADR-0011).

    Authoring is two calls, so a blank is admissible at two different moments
    and "valid" alone does not say which is meant. `SKELETON` and `PEDAGOGY`
    name the two calls; `COMPLETE` names the merged result, and is what every
    caller that has never heard of the split already means.
    """

    SKELETON = "skeleton"
    PEDAGOGY = "pedagogy"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class StageRules:
    """What one authoring stage asks of a blank.

    The call's output schema and the conditional validator travel together
    because they are two statements of the same rule: relaxing one without the
    other is exactly the trap #9 was filed to get out of - a schema that lets a
    skeleton-only payload through and a validator that then refuses it.
    """

    schema_fragment: Mapping[str, object]
    validate_blank: BlankValidator


@dataclass(frozen=True, slots=True)
class BlankRange:
    """How many blanks a quiz in this mode may carry. Inclusive at both ends."""

    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if self.minimum < 1:
            raise ValueError(f"a quiz needs at least one blank, got {self.minimum}")
        if self.maximum < self.minimum:
            raise ValueError(f"inverted blank range: {self.minimum}-{self.maximum}")

    def admits(self, count: int) -> bool:
        return self.minimum <= count <= self.maximum


@dataclass(frozen=True, slots=True)
class ModePolicy:
    """Everything that varies by difficulty mode, bundled."""

    authoring_schema_fragment: Mapping[str, object]
    grading_strategy: GradingStrategy
    validate_blank: BlankValidator
    render_hint: RenderHint
    blank_range: BlankRange
    probe_failure_behavior: ProbeFailureBehavior
    stage_rules: Mapping[AuthoringStage, StageRules] = field(default_factory=dict)
    """What the mode asks at each authoring stage, where it asks less than it
    asks of a finished quiz.

    The six fields above are unchanged and are the `COMPLETE` rules - which is
    what they always were. A mode declares `stage_rules` only to say that some
    of those rules belong to the second call.
    """

    def rules_for(self, stage: AuthoringStage) -> StageRules:
        """The schema fragment and validator this stage asks for.

        A stage the policy does not declare falls back to the complete rules,
        so a mode registered with the six fields alone - a third difficulty
        mode is still one registry entry - is validated exactly as strictly as
        it was before staged authoring existed. The fallback is deliberately
        the strict direction: a policy cannot be silently relaxed by code it
        has never heard of.
        """
        declared = self.stage_rules.get(stage)
        if declared is not None:
            return declared
        return StageRules(
            schema_fragment=self.authoring_schema_fragment,
            validate_blank=self.validate_blank,
        )


class ModeRegistry:
    """Maps mode to policy. Instances are independent - `default_registry()`
    hands back a fresh one, so a caller registering an extra mode cannot
    perturb anyone else."""

    def __init__(self, policies: Mapping[ModeKey, ModePolicy] | None = None) -> None:
        self._policies: dict[ModeKey, ModePolicy] = dict(policies or {})

    def register(self, mode: ModeKey, policy: ModePolicy) -> None:
        if mode in self._policies:
            raise ValueError(f"{mode!r} already has a policy")
        self._policies[mode] = policy

    def policy_for(self, mode: ModeKey) -> ModePolicy:
        try:
            return self._policies[mode]
        except KeyError:
            raise KeyError(f"no policy registered for mode {mode!r}") from None

    def modes(self) -> tuple[ModeKey, ...]:
        return tuple(self._policies)


# --- The shipped modes -------------------------------------------------------
#
# The validators below are the explicit conditional checks ADR-0004 requires:
# `strict: true` keeps all keys present, but it cannot say that a Novice blank
# needs two options and an Advanced one needs a rubric.


def _require_mode(blank: Blank, expected: ModeKey) -> None:
    if blank.mode != expected:
        raise ValueError(
            f"a {expected!r} policy cannot validate a {blank.mode!r} blank"
        )


def _validate_novice_skeleton(blank: Blank) -> tuple[str, ...]:
    """What the skeleton call owes a Novice blank: the option bank.

    A quiz has to be *playable* on the skeleton alone (ADR-0011), and for
    Novice that means a bank of at least two options with a key naming one of
    them. No pedagogy is asked for: hints and the reinforcement ride the second
    call, and demanding them here is exactly what made a skeleton-only payload
    unrepresentable before #9.
    """
    _require_mode(blank, DifficultyMode.NOVICE)
    errors: list[str] = []

    options = blank.options or ()
    if len(options) < 2:
        errors.append("a novice blank needs at least two options")
    elif blank.correct_option_id not in {option.option_id for option in options}:
        errors.append("correct_option_id is not one of the options")
    if blank.correct_option_id is None:
        errors.append("correct_option_id is missing")
    if blank.rubric is not None:
        errors.append("a novice blank carries no rubric")

    return tuple(errors)


def _validate_novice_pedagogy(blank: Blank) -> tuple[str, ...]:
    """What the pedagogy payload owes a Novice blank.

    It has no opinion about the option bank - the skeleton already settled it,
    and asking twice would report one problem twice on a merged quiz.
    """
    _require_mode(blank, DifficultyMode.NOVICE)
    errors: list[str] = []

    if blank.reinforcement is None:
        errors.append("a novice blank needs a reinforcement")
    if blank.hints is None or len(blank.hints) != 3:
        errors.append("a novice blank needs three hints, one per ladder rung")

    return tuple(errors)


def _validate_advanced_skeleton(blank: Blank) -> tuple[str, ...]:
    """What the skeleton call owes an Advanced blank: the rubric.

    The rubric is a **skeleton** field. ADR-0011 lists it in neither payload,
    and an Advanced blank is graded against it - so deferring it to the second
    call would make the window the ADR requires to be answerable precisely the
    window in which an Advanced answer cannot be graded.
    """
    _require_mode(blank, DifficultyMode.ADVANCED)
    errors: list[str] = []

    if not blank.rubric:
        errors.append("an advanced blank needs a rubric")
    for name in ("options", "correct_option_id"):
        if getattr(blank, name) is not None:
            errors.append(f"an advanced blank carries no {name}")

    return tuple(errors)


def _validate_advanced_pedagogy(blank: Blank) -> tuple[str, ...]:
    """Advanced has no per-blank pedagogy at all.

    Its feedback is the reactive tutor line on the grading response and its
    probe question a nullable field on the same (ADR-0013, ADR-0011), so the
    payload carries only the quiz-level recap. The stage still exists, and
    still refuses fields Advanced does not own.
    """
    _require_mode(blank, DifficultyMode.ADVANCED)
    return tuple(
        f"an advanced blank carries no {name}"
        for name in ("reinforcement", "hints")
        if getattr(blank, name) is not None
    )


def _both(first: BlankValidator, second: BlankValidator) -> BlankValidator:
    """The complete rules: one stage's, then the other's.

    Composition rather than a third hand-written validator, so `COMPLETE` is
    not a re-derivation of what the conditional validator refused before #9 -
    it *is* those rules, partitioned. Nothing can be relaxed at `COMPLETE`
    without being relaxed at a stage, where the omission is visible.
    """

    def validate(blank: Blank) -> tuple[str, ...]:
        return first(blank) + second(blank)

    return validate


_validate_novice_blank = _both(_validate_novice_skeleton, _validate_novice_pedagogy)
_validate_advanced_blank = _both(
    _validate_advanced_skeleton, _validate_advanced_pedagogy
)


_NOVICE_SCHEMA_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "blank_id": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "option_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["option_id", "text"],
                "additionalProperties": False,
            },
        },
        "correct_option_id": {"type": "string"},
        "reinforcement": {"type": "string"},
        "hints": {"type": "array", "items": {"type": "string"}},
        "rubric": {"type": ["string", "null"]},
    },
    "required": [
        "blank_id",
        "options",
        "correct_option_id",
        "reinforcement",
        "hints",
        "rubric",
    ],
    "additionalProperties": False,
}

_ADVANCED_SCHEMA_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "blank_id": {"type": "string"},
        "options": {"type": ["array", "null"], "items": {"type": "object"}},
        "correct_option_id": {"type": ["string", "null"]},
        "reinforcement": {"type": ["string", "null"]},
        "hints": {"type": ["array", "null"], "items": {"type": "string"}},
        "rubric": {"type": "string"},
    },
    "required": [
        "blank_id",
        "options",
        "correct_option_id",
        "reinforcement",
        "hints",
        "rubric",
    ],
    "additionalProperties": False,
}


_BLANK_ID = {"blank_id": {"type": "string"}}

_OPTIONS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "option_id": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["option_id", "text"],
        "additionalProperties": False,
    },
}

_NOVICE_SKELETON_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": {
        **_BLANK_ID,
        "options": _OPTIONS,
        "correct_option_id": {"type": "string"},
    },
    "required": ["blank_id", "options", "correct_option_id"],
    "additionalProperties": False,
}

_NOVICE_PEDAGOGY_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": {
        **_BLANK_ID,
        "reinforcement": {"type": "string"},
        "hints": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["blank_id", "reinforcement", "hints"],
    "additionalProperties": False,
}

_ADVANCED_SKELETON_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": {**_BLANK_ID, "rubric": {"type": "string"}},
    "required": ["blank_id", "rubric"],
    "additionalProperties": False,
}

_ADVANCED_PEDAGOGY_FRAGMENT: Mapping[str, object] = {
    "type": "object",
    "properties": dict(_BLANK_ID),
    "required": ["blank_id"],
    "additionalProperties": False,
}
"""Advanced adds nothing per blank. The fragment says so explicitly rather than
being absent, because `additionalProperties: False` is what stops the model
inventing a hint ladder Advanced would then refuse."""


NOVICE_POLICY = ModePolicy(
    authoring_schema_fragment=_NOVICE_SCHEMA_FRAGMENT,
    grading_strategy=GradingStrategy.DETERMINISTIC,
    validate_blank=_validate_novice_blank,
    render_hint=RenderHint.OPTION_BANK,
    blank_range=BlankRange(1, 2),
    probe_failure_behavior=ProbeFailureBehavior.CORRECT_AND_RESOLVE,
    stage_rules={
        AuthoringStage.SKELETON: StageRules(
            schema_fragment=_NOVICE_SKELETON_FRAGMENT,
            validate_blank=_validate_novice_skeleton,
        ),
        AuthoringStage.PEDAGOGY: StageRules(
            schema_fragment=_NOVICE_PEDAGOGY_FRAGMENT,
            validate_blank=_validate_novice_pedagogy,
        ),
    },
)

ADVANCED_POLICY = ModePolicy(
    authoring_schema_fragment=_ADVANCED_SCHEMA_FRAGMENT,
    grading_strategy=GradingStrategy.MODEL_GRADED,
    validate_blank=_validate_advanced_blank,
    render_hint=RenderHint.TEXT_INPUT,
    blank_range=BlankRange(4, 6),
    probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
    stage_rules={
        AuthoringStage.SKELETON: StageRules(
            schema_fragment=_ADVANCED_SKELETON_FRAGMENT,
            validate_blank=_validate_advanced_skeleton,
        ),
        AuthoringStage.PEDAGOGY: StageRules(
            schema_fragment=_ADVANCED_PEDAGOGY_FRAGMENT,
            validate_blank=_validate_advanced_pedagogy,
        ),
    },
)


def default_registry() -> ModeRegistry:
    """A fresh registry holding the two shipped modes."""
    return ModeRegistry(
        {
            DifficultyMode.NOVICE: NOVICE_POLICY,
            DifficultyMode.ADVANCED: ADVANCED_POLICY,
        }
    )
