"""The hot path: a learner submits an answer and gets a verdict back.

`QuizSession.submit` is a seam in the master spec's table, and it is the exact
point where **"a Novice answer costs zero model calls"** becomes assertable
(master spec acceptance 6 and 7). This module holds the deterministic half of
that seam; the model-graded half is
[#8](https://github.com/derkmed/socratic/issues/8) and probes are
[#10](https://github.com/derkmed/socratic/issues/10).

**Dispatch reads `ModePolicy.grading_strategy`, never the mode.** `_GRADERS`
maps a `GradingStrategy` to the function that implements it, and `submit` looks
the policy up through the registry. Nothing in this module names a difficulty
mode — an `if mode ==` outside the registry is a bug (CONTEXT: ModeRegistry),
and `tests/test_registry.py` scans for one.

**Novice has no reactive tutor line** (D13,
[ADR-0013](../../../docs/adr/0013-reactive-tutor-line.md)). Its feedback is
wholly pre-authored — the blank's `reinforcement` on a correct answer, its
`hints` on the way up the ladder — which is what makes the zero-call claim true
without qualification. `Submission` therefore carries no field for one.

**The key never leaves the backend** (D4,
[ADR-0003](../../../docs/adr/0003-grading-authority-and-key-custody.md)). A
submission carries an option id and gets back a verdict; the correct option id
appears in the result on exactly one path, the rung-three reveal.

**Blank state is derived, never stored twice.** `attempt.guesses` is already
ordered and already bounded, so resolution and the ladder rung are read off the
record rather than tracked alongside it (D1). The one predicate that decides
sealing — **all blanks resolved and no probe pending** (D10, CONTEXT: Sealed) —
is written here with both clauses live, so the no-probe case is a special case
of the predicate rather than a "last blank resolved" check that #10 would have
to unwrite.

Spec: `docs/specs/novice-submit.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from socratic.domain import ids
from socratic.domain import model_client as model_client_module
from socratic.domain import records
from socratic.domain import registry as registry_module
from socratic.domain import repositories
from socratic.domain.modes import GradingStrategy
from socratic.domain.records import QuizAttempt, Verdict
from socratic.domain.types import Blank

HINT_LADDER_RUNGS = records.HINT_LADDER_RUNGS
"""Three escalating responses to a wrong answer, and the third reveals."""


class BlankAlreadyResolved(ValueError):
    """A submission arrived for a blank that is finished.

    A `ValueError` rather than a silent no-op: the client is authoritative for
    quiz state, so a submission against a resolved blank means the client's
    view and the record have diverged, and swallowing it would hide that.
    """


@dataclass(frozen=True, slots=True)
class Submission:
    """What one submission returns.

    `revealed_option_id` is non-null only on the rung-three reveal, which is
    the single route by which the answer key reaches a caller.

    There is deliberately **no** reactive tutor line here. Advanced carries one
    as a nullable field on its grading response (D13); Novice has none, and a
    field that is always null on this path would suggest otherwise.
    """

    verdict: Verdict
    graded_by: GradingStrategy
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None
    blank_resolved: bool
    attempt_sealed: bool


@dataclass(frozen=True, slots=True)
class _Grade:
    """What a grading strategy decided, before any of it is persisted.

    Separate from `Submission` because a grade is about the blank and the
    submission alone, while a `Submission` also reports what happened to the
    attempt — and the strategies have no business knowing about sealing.
    """

    verdict: Verdict
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None


Grader = Callable[[Blank, str, int], _Grade]
"""A grading strategy: the blank, what was submitted, and which attempt at this
blank this is (1-based). Returns the grade; persistence is the session's."""


def _hint_for_rung(blank: Blank, rung: int) -> str | None:
    """The ladder rung's pre-authored text, or `None` if it has not landed.

    [#9](https://github.com/derkmed/socratic/issues/9) splits authoring into a
    skeleton and a pedagogy payload, so a learner can answer in the window
    before `hints` arrives (master spec acceptance 5). Missing pedagogy costs
    the learner the hint text, not the verdict — the ladder still advances and
    still reveals, and the hot path does not raise over a payload that is
    merely late.
    """
    hints = blank.hints or ()
    if rung <= len(hints):
        return hints[rung - 1]
    return None


def _grade_against_the_key(blank: Blank, submitted: str, ordinal: int) -> _Grade:
    """The deterministic strategy (D4): compare to the stored key, no model.

    Raises:
      ValueError: If the blank carries no `correct_option_id`. That field rides
        the skeleton call, so its absence is not the late-pedagogy window — it
        is a malformed quiz, and there is nothing to grade against.
    """
    key = blank.correct_option_id
    if key is None:
        raise ValueError(
            f"blank {blank.blank_id!r} carries no answer key to grade against"
        )

    if submitted == key:
        return _Grade(
            verdict=Verdict.CORRECT,
            hint_rung_shown=None,
            feedback=blank.reinforcement,
            revealed_option_id=None,
        )

    # Every prior guess on an unresolved blank was wrong — a correct one would
    # have resolved it — so the attempt ordinal *is* the rung.
    rung = min(ordinal, HINT_LADDER_RUNGS)
    return _Grade(
        verdict=Verdict.INCORRECT,
        hint_rung_shown=rung,
        feedback=_hint_for_rung(blank, rung),
        revealed_option_id=key if rung >= HINT_LADDER_RUNGS else None,
    )


def _grade_by_model(blank: Blank, submitted: str, ordinal: int) -> _Grade:
    """The model-graded strategy: free recall against the blank's rubric.

    Not built here. It needs the cache-anchored block, the `grade_answer` call,
    and the probe question and reactive tutor line that ride its response —
    [#8](https://github.com/derkmed/socratic/issues/8). Raising rather than
    falling back to the deterministic path, because an Advanced answer graded
    by string equality would look like a working feature.
    """
    raise NotImplementedError(
        "model-graded submission is issue #8; this build implements the "
        "deterministic strategy only"
    )


_GRADERS: Mapping[GradingStrategy, Grader] = {
    GradingStrategy.DETERMINISTIC: _grade_against_the_key,
    GradingStrategy.MODEL_GRADED: _grade_by_model,
}
"""Strategy to implementation. This table is why the session never branches on
a difficulty mode: the registry names a strategy and the table names the code
that runs it, so a third mode reusing either strategy costs nothing here."""


# --- Reading blank state off the record --------------------------------------


def guesses_for(attempt: QuizAttempt, blank_id: str) -> tuple[records.Guess, ...]:
    """Every guess made against one blank, in the order made."""
    return tuple(guess for guess in attempt.guesses if guess.blank_id == blank_id)


def is_blank_resolved(attempt: QuizAttempt, blank_id: str) -> bool:
    """Whether a blank is finished: answered correctly, or revealed.

    Derived rather than stored, so there is no second copy of the truth to
    disagree with `attempt.guesses`.
    """
    guesses = guesses_for(attempt, blank_id)
    if any(guess.verdict is Verdict.CORRECT for guess in guesses):
        return True
    wrong = sum(1 for guess in guesses if guess.verdict is Verdict.INCORRECT)
    return wrong >= HINT_LADDER_RUNGS


def has_pending_probe(attempt: QuizAttempt) -> bool:
    """Whether a probe is still waiting on the learner.

    The second clause of the seal predicate. Nothing in this build fires a
    probe, so today this is always false for an attempt the session created —
    but the clause is live, and an attempt carrying an unanswered probe will
    not seal.

    A **dismissed** probe is not yet distinguishable from a pending one: both
    persist with a null `self_explanation` (`records.Probe`), and the record
    has no dismissal marker. Blocking is the conservative reading — an attempt
    that has not sealed can still seal later, while one sealed early can never
    be written again. [#10](https://github.com/derkmed/socratic/issues/10),
    which owns probe firing and dismissal, completes the distinction.
    """
    return any(not probe.is_answered for probe in attempt.probes)


def is_sealable(attempt: QuizAttempt) -> bool:
    """The unified predicate: **all blanks resolved and no probe pending**.

    One predicate, both clauses, evaluated after every submission — never "the
    last blank resolved". Because a failed Advanced probe can re-open a blank,
    `resolved` is not terminal and completion can fire and un-fire (D10,
    CONTEXT: Sealed); a completion check written around the last blank would be
    correct today and wrong the moment #10 lands.
    """
    every_blank_resolved = all(
        is_blank_resolved(attempt, blank.blank_id) for blank in attempt.quiz.blanks
    )
    return every_blank_resolved and not has_pending_probe(attempt)


class QuizSession:
    """Grades submissions against one learner's in-flight attempt."""

    def __init__(
        self,
        *,
        model_client: model_client_module.ModelClient,
        attempts: repositories.AttemptRepository,
        registry: registry_module.ModeRegistry | None = None,
        clock: ids.Clock = ids.system_clock,
    ) -> None:
        """Wire the service.

        Args:
          model_client: The `ModelClient` port. Held for the model-graded
            strategy (#8) and, on the deterministic path, **never consulted** —
            which is the whole of master spec acceptance 6: hand it a
            `RecordingModelClient(fail_if_called=True)` and a Novice attempt
            runs to sealing without tripping it.
          attempts: Where the in-flight attempt is read and written. The
            in-memory implementation *is* the test double.
          registry: Where `grading_strategy` comes from. Defaults to
            `default_registry()`.
          clock: Milliseconds since the epoch. Injected so a frozen clock makes
            each guess's `created_at` and the attempt's `sealed_at`
            deterministic.
        """
        self._model_client = model_client
        self._attempts = attempts
        self._registry = registry or registry_module.default_registry()
        self._clock = clock

    def submit(
        self,
        *,
        learner_id: str,
        attempt_id: str,
        blank_id: str,
        submitted: str,
    ) -> Submission:
        """Grade one submission, record it, and seal the attempt if it is done.

        Args:
          learner_id: Whose partition the attempt lives in.
          attempt_id: The attempt being worked through.
          blank_id: Which blank was answered.
          submitted: What the learner submitted — an option id under the
            deterministic strategy, free text under the model-graded one.

        Returns:
          The verdict, the pre-authored feedback that goes with it, the ladder
          rung showing, and what happened to the blank and the attempt.

        Raises:
          KeyError: If there is no such attempt, no such blank on its quiz, or
            no policy registered for its mode.
          BlankAlreadyResolved: If the blank is already answered or revealed.
          NotImplementedError: If the mode's strategy is model-graded (#8).
          ValueError: If the blank carries no answer key, or the attempt is
            already sealed.
        """
        attempt = self._attempts.get(learner_id, attempt_id)
        blank = attempt.quiz.blank(blank_id)

        if is_blank_resolved(attempt, blank_id):
            raise BlankAlreadyResolved(
                f"blank {blank_id!r} on attempt {attempt_id} is already resolved"
            )

        policy = self._registry.policy_for(attempt.mode)
        grader = _GRADERS[policy.grading_strategy]
        ordinal = len(guesses_for(attempt, blank_id)) + 1
        grade = grader(blank, submitted, ordinal)

        now = self._now()
        attempt = attempt.with_guess(
            records.Guess(
                blank_id=blank_id,
                submitted=submitted,
                verdict=grade.verdict,
                attempt_ordinal=ordinal,
                hint_rung_shown=grade.hint_rung_shown,
                created_at=now,
                graded_by=policy.grading_strategy,
            )
        )

        sealed = is_sealable(attempt)
        if sealed:
            attempt = attempt.sealed(now)
        self._attempts.save(attempt)

        return Submission(
            verdict=grade.verdict,
            graded_by=policy.grading_strategy,
            hint_rung_shown=grade.hint_rung_shown,
            feedback=grade.feedback,
            revealed_option_id=grade.revealed_option_id,
            blank_resolved=is_blank_resolved(attempt, blank_id),
            attempt_sealed=sealed,
        )

    def _now(self) -> datetime:
        """The injected clock as an aware UTC datetime, matching `Ulid`."""
        return datetime.fromtimestamp(self._clock() / 1000, tz=timezone.utc)
