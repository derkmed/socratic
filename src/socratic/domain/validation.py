"""Quiz validation - the gate a quiz passes before it is rendered or stored.

Structured output with `strict: true` can guarantee that every key is present.
It cannot guarantee that the *right* keys are populated for the mode in play: a
Novice blank needs at least two options and a `correct_option_id` naming one of
them, an Advanced blank needs a rubric, and one `Blank` type with nullable
mode-specific fields makes both shapes representable either way (D2,
[ADR-0004](../../../docs/adr/0004-quiz-wire-format.md)). Closing that gap is
what this module is for, which is why the rules are conditional rather than a
flat schema.

**The per-mode rules are not here.** They live in `ModePolicy.validate_blank`,
and `validate_quiz` reaches them through the registry, so validation branches on
mode in exactly one place and a hypothetical third difficulty mode is still one
registry entry (spec acceptance 39).

**Two bounds, two independent rejections** (spec acceptance 25). The mode's
`blank_range` rejects first - it is the pedagogical bound, Novice 1-2 and
Advanced 4-6, and it is the one an author should ever meet. `STORAGE_BLANK_CAP`
is the separate storage invariant from
[ADR-0005](../../../docs/adr/0005-persistence-contract.md): the attempt document
embeds its guesses, so unbounded blanks means an unbounded document. Because it
is a different invariant it is checked independently rather than being subsumed
by whatever range the mode in play happens to declare.

Every problem found is reported, never just the first: a malformed authoring
response usually has several, and a reviewer wants all of them at once.
"""

from __future__ import annotations

from socratic.domain import registry as registry_module
from socratic.domain.types import Quiz

STORAGE_BLANK_CAP = 20
"""The hard ceiling on blanks per quiz (ADR-0005, D5).

Enforced at authoring time rather than discovered at write time. Raising it is
the trigger to migrate the embedded guess array to an event stream, so it is a
constant here and not a per-mode setting.
"""


class QuizValidationError(ValueError):
    """A quiz that may not be rendered or persisted.

    Carries **every** problem found, not just the one that raised.
    """

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_quiz(
    quiz: Quiz,
    registry: registry_module.ModeRegistry | None = None,
) -> tuple[str, ...]:
    """Report everything inadmissible about `quiz` - empty when it is fine.

    Args:
      quiz: The quiz to check. Structural invariants that `Quiz.__post_init__`
        already enforces (duplicate ids, dangling blank segments) are not
        re-checked here.
      registry: Where the per-mode rules come from. Defaults to
        `default_registry()`.

    Returns:
      The reasons the quiz is inadmissible, in order: the two bounds first,
      then one entry per problem per blank, each prefixed with its `blank_id`.

    Raises:
      KeyError: If no policy is registered for the quiz's mode. An unregistered
        mode is a wiring mistake rather than a malformed quiz, so it is not
        reported alongside the content errors.
    """
    if registry is None:
        registry = registry_module.default_registry()
    policy = registry.policy_for(quiz.mode)

    errors: list[str] = []
    errors.extend(_bound_errors(quiz, policy.blank_range))
    for blank in quiz.blanks:
        for error in _blank_errors(blank, quiz, policy):
            errors.append(f"{blank.blank_id}: {error}")
    return tuple(errors)


def ensure_valid_quiz(
    quiz: Quiz,
    registry: registry_module.ModeRegistry | None = None,
) -> Quiz:
    """Return `quiz` if it is admissible, else raise.

    The rejecting face of `validate_quiz`, for callers on the authoring path
    that have nothing useful to do with a list of problems.

    Raises:
      QuizValidationError: Carrying every problem found.
    """
    errors = validate_quiz(quiz, registry)
    if errors:
        raise QuizValidationError(errors)
    return quiz


def _bound_errors(
    quiz: Quiz,
    blank_range: registry_module.BlankRange,
) -> tuple[str, ...]:
    """The two bounds, checked independently and reported range-first."""
    errors: list[str] = []
    count = len(quiz.blanks)
    if not blank_range.admits(count):
        errors.append(
            f"{count} blanks is outside the mode's blank_range "
            f"{blank_range.minimum}-{blank_range.maximum}"
        )
    if count > STORAGE_BLANK_CAP:
        errors.append(
            f"{count} blanks exceeds the storage cap of {STORAGE_BLANK_CAP}"
        )
    return tuple(errors)


def _blank_errors(
    blank,
    quiz: Quiz,
    policy: registry_module.ModePolicy,
) -> tuple[str, ...]:
    """One blank's problems, from the policy - or the mode disagreement that
    stops the policy being asked at all.

    One attempt, one mode (CONTEXT: Mode toggle), so a blank carrying a
    different mode from its quiz is inadmissible. It is caught here rather than
    left to the policy, whose validator raises on a foreign blank and would
    take the rest of the report down with it.
    """
    if blank.mode != quiz.mode:
        return (f"mode {blank.mode!r} does not match the quiz's {quiz.mode!r}",)
    return policy.validate_blank(blank)
