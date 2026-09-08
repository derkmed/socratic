"""The quiz wire format (D2, ADR-0004, amended by ADR-0012).

The explanation is a **flat segment array** of `text | math | blank`, never a
string with sentinels: rendering walks the list, and resolving a blank swaps one
element for a `text` or `math` node. Sentinel strings reintroduce the parsing
ADR-0001 removed, and character offsets are worse — models count characters
badly.

A blank masks a **whole formula, never a term inside one**. That is what keeps
the array flat, and it is enforced structurally: no segment type has a field
that can hold another segment, so there is nowhere for a nested blank to live.
Nested blanks are out of scope for the prototype, deferred on the accessibility
story — there is no ARIA pattern for an unanswered blank and MathML-AAM maps
almost nothing below `math`.

There is **one `Blank` type** with a mode enum and nullable mode-specific
fields, chosen over mode-split schemas so a further difficulty mode is an enum
value plus fields rather than a forked schema, store and renderer. `strict: true`
still holds structurally, but conditional invariants
(`novice ⇒ len(options) >= 2 and correct_option_id in options`;
`advanced ⇒ rubric present`) cannot be expressed in the schema and are validated
explicitly — see `ModePolicy.validate_blank` in `registry.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from socratic.domain.ids import QuizSessionId
from socratic.domain.modes import DifficultyMode


@dataclass(frozen=True, slots=True)
class TextSegment:
    """Restricted Markdown. Sanitised before rendering, model-authored or not."""

    text: str


@dataclass(frozen=True, slots=True)
class MathSegment:
    """MathML, converted from LaTeX server-side. Opaque to the domain."""

    mathml: str


@dataclass(frozen=True, slots=True)
class BlankSegment:
    """A reference to a `Blank` by id. Carries no content of its own."""

    blank_id: str


Segment = Union[TextSegment, MathSegment, BlankSegment]

ResolvedSegment = Union[TextSegment, MathSegment]
"""What a blank may resolve to. A blank never resolves to another blank."""


@dataclass(frozen=True, slots=True)
class Option:
    """One entry in a Novice blank's option bank."""

    option_id: str
    text: str


@dataclass(frozen=True, slots=True)
class Blank:
    """One masked element.

    Novice carries `options`, `correct_option_id`, `reinforcement` and three
    `hints`; Advanced carries a `rubric`. Both sets are nullable on the one
    type — which is why the conditional validator is load-bearing.
    """

    blank_id: str
    mode: DifficultyMode

    # Novice.
    options: tuple[Option, ...] | None = None
    correct_option_id: str | None = None
    reinforcement: str | None = None
    hints: tuple[str, str, str] | None = None

    # Advanced.
    rubric: str | None = None


@dataclass(frozen=True, slots=True)
class Quiz:
    """One authored exercise, produced by a single authoring call."""

    quiz_session_id: QuizSessionId
    mode: DifficultyMode
    topic: str
    explanation: tuple[Segment, ...]
    blanks: tuple[Blank, ...]
    recap: str
    queued_topics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [blank.blank_id for blank in self.blanks]
        duplicates = {id_ for id_ in ids if ids.count(id_) > 1}
        if duplicates:
            raise ValueError(f"duplicate blank ids: {sorted(duplicates)}")

        declared = set(ids)
        referenced = {
            segment.blank_id
            for segment in self.explanation
            if isinstance(segment, BlankSegment)
        }
        if referenced - declared:
            raise ValueError(
                f"explanation references undeclared blanks: {sorted(referenced - declared)}"
            )
        if declared - referenced:
            raise ValueError(
                f"blanks with no segment referencing them: {sorted(declared - referenced)}"
            )

    def blank(self, blank_id: str) -> Blank:
        """Blanks are addressable by id — the renderer and the grader both
        arrive holding an id rather than an index."""
        for candidate in self.blanks:
            if candidate.blank_id == blank_id:
                return candidate
        raise KeyError(blank_id)


@dataclass(frozen=True, slots=True)
class DirectAnswer:
    """The override branch: medical, legal, financial, security, active outage,
    or "I need this now". A first-class member of the union rather than an
    exception, so the highest-stakes question a learner asks still parses."""

    answer: str
    topic: str
    queued_topics: tuple[str, ...] = ()


AuthoringResult = Union[Quiz, DirectAnswer]
"""Returned as a value so the override branch is testable rather than a side
effect (spec seam table, `QuizAuthoring.author`)."""


def resolve_blank(
    explanation: tuple[Segment, ...],
    blank_id: str,
    resolved: ResolvedSegment,
) -> tuple[Segment, ...]:
    """Swap the blank's element for the node it resolved to.

    One element in, one element out — the array stays flat and the same length.
    """
    if not isinstance(resolved, (TextSegment, MathSegment)):
        raise TypeError(
            f"a blank resolves to a text or math node, got {type(resolved).__name__}"
        )

    swapped = tuple(
        resolved
        if isinstance(segment, BlankSegment) and segment.blank_id == blank_id
        else segment
        for segment in explanation
    )
    if swapped == explanation:
        raise KeyError(blank_id)
    return swapped
