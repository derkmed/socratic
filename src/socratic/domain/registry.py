"""The mode registry - the only place difficulty mode is ever branched on.

`DifficultyMode` alone is not the extension point, because behaviour differs per
mode in six places: the authoring schema fragment, the grading strategy, the
validator rule, the render hint, `blank_range`, and `probe_failure_behavior`.
`ModePolicy` bundles all six; `ModeRegistry` maps mode to policy.

**Adding a third mode is one registry entry.** An `if mode ==` anywhere outside
this module is a bug, and `tests/test_registry.py` scans the package for one.

This module declares `validate_blank` as a policy field and populates it per
mode. The `validate_quiz` entry point, and the bounds it enforces (the 20-blank
storage cap, and `blank_range` rejecting first), live in the validator ticket
that follows.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def _validate_novice_blank(blank: Blank) -> tuple[str, ...]:
    _require_mode(blank, DifficultyMode.NOVICE)
    errors: list[str] = []

    options = blank.options or ()
    if len(options) < 2:
        errors.append("a novice blank needs at least two options")
    elif blank.correct_option_id not in {option.option_id for option in options}:
        errors.append("correct_option_id is not one of the options")
    if blank.correct_option_id is None:
        errors.append("correct_option_id is missing")
    if blank.reinforcement is None:
        errors.append("a novice blank needs a reinforcement")
    if blank.hints is None or len(blank.hints) != 3:
        errors.append("a novice blank needs three hints, one per ladder rung")
    if blank.rubric is not None:
        errors.append("a novice blank carries no rubric")

    return tuple(errors)


def _validate_advanced_blank(blank: Blank) -> tuple[str, ...]:
    _require_mode(blank, DifficultyMode.ADVANCED)
    errors: list[str] = []

    if not blank.rubric:
        errors.append("an advanced blank needs a rubric")
    for name in ("options", "correct_option_id", "reinforcement", "hints"):
        if getattr(blank, name) is not None:
            errors.append(f"an advanced blank carries no {name}")

    return tuple(errors)


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


NOVICE_POLICY = ModePolicy(
    authoring_schema_fragment=_NOVICE_SCHEMA_FRAGMENT,
    grading_strategy=GradingStrategy.DETERMINISTIC,
    validate_blank=_validate_novice_blank,
    render_hint=RenderHint.OPTION_BANK,
    blank_range=BlankRange(1, 2),
    probe_failure_behavior=ProbeFailureBehavior.CORRECT_AND_RESOLVE,
)

ADVANCED_POLICY = ModePolicy(
    authoring_schema_fragment=_ADVANCED_SCHEMA_FRAGMENT,
    grading_strategy=GradingStrategy.MODEL_GRADED,
    validate_blank=_validate_advanced_blank,
    render_hint=RenderHint.TEXT_INPUT,
    blank_range=BlankRange(4, 6),
    probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
)


def default_registry() -> ModeRegistry:
    """A fresh registry holding the two shipped modes."""
    return ModeRegistry(
        {
            DifficultyMode.NOVICE: NOVICE_POLICY,
            DifficultyMode.ADVANCED: ADVANCED_POLICY,
        }
    )
