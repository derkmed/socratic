"""The difficulty-mode vocabulary.

`DifficultyMode` alone is not the extension point, because behaviour differs per
mode in six places; the bundle that *is* the extension point lives in
`registry.py`. This module holds only the enums that both the types and the
registry need, so the import graph stays one-directional.

`DifficultyMode` is a `str` enum deliberately: the registry is keyed by the
mode's value, so a further mode is a registry entry rather than a change to
every module that names the type (spec acceptance 39).
"""

from __future__ import annotations

from enum import Enum


class DifficultyMode(str, Enum):
    """`NOVICE` and `ADVANCED` today; more are expected."""

    NOVICE = "novice"
    ADVANCED = "advanced"


class GradingStrategy(str, Enum):
    """How a submitted answer is graded (D4, ADR-0003)."""

    DETERMINISTIC = "deterministic"
    """Compared to the stored key with **no model call**."""

    MODEL_GRADED = "model_graded"
    """Graded on meaning by `ModelClient.grade_answer`."""


class RenderHint(str, Enum):
    """What input control the iframe puts under a blank."""

    OPTION_BANK = "option_bank"
    TEXT_INPUT = "text_input"


class ProbeFailureBehavior(str, Enum):
    """What a failed self-explanation probe does (D9, ADR-0009)."""

    REOPEN_BLANK = "reopen_blank"
    """Advanced: re-open the blank, ladder resuming where it left off."""

    CORRECT_AND_RESOLVE = "correct_and_resolve"
    """Novice: correct the misconception but leave the blank resolved —
    re-opening a two-option bank whose answer the learner was just told is
    degenerate."""
