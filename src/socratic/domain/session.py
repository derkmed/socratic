"""The hot path: a learner submits an answer and gets a verdict back.

`QuizSession.submit` is a seam in the master spec's table, and it is the exact
point where **"a Novice answer costs zero model calls"** becomes assertable
(master spec acceptance 6 and 7). This module holds **both** halves of that
seam — the deterministic strategy and the model-graded one; probes are
[#10](https://github.com/derkmed/socratic/issues/10).

**One response, three things** (D13,
[ADR-0013](../../../docs/adr/0013-reactive-tutor-line.md)). The model-graded
strategy makes exactly one `grade_answer` call per submission, and that single
response carries the verdict, the probe question (nullable) and the reactive
tutor line (nullable) together. There is no second request and nothing streams:
the parallel tutor call ADR-0003 described is withdrawn, and adding one back
here would falsify master spec acceptance 9.

**A probe question arriving is not a probe firing.** The question rides the
response and is handed to the caller on `ModelGrading`; deciding *whether* to
ask it, appending a `records.Probe`, the cadence coin flip and `answer_probe`
are all [#10](https://github.com/derkmed/socratic/issues/10). Nothing here
appends a probe, so the seal predicate's probe clause is as inert — and as live
— as it was before.

**Dispatch reads `ModePolicy.grading_strategy`, never the mode.** `_GRADERS`
maps a `GradingStrategy` to the function that implements it, and `submit` looks
the policy up through the registry. Nothing in this module names a difficulty
mode — an `if mode ==` outside the registry is a bug (CONTEXT: ModeRegistry),
and `tests/test_registry.py` scans for one.

**Novice has no reactive tutor line** (D13,
[ADR-0013](../../../docs/adr/0013-reactive-tutor-line.md)). Its feedback is
wholly pre-authored — the blank's `reinforcement` on a correct answer, its
`hints` on the way up the ladder — which is what makes the zero-call claim true
without qualification. `Submission` therefore carries no field for one: the two
nullable riders live on `ModelGrading`, which the deterministic strategy never
builds, so a Novice result has nowhere for a reactive line to be rather than a
field that is merely always null.

**The key never leaves the backend** (D4,
[ADR-0003](../../../docs/adr/0003-grading-authority-and-key-custody.md)). A
Novice submission carries an option id and gets back a verdict; the correct
option id appears in the result on exactly one path, the rung-three reveal. An
Advanced blank's rubric *is* the key: it goes into segment 2 of the grading
request and into nothing that `submit` returns, and the rung-three reveal has
nothing to reveal on that path — there is no option id, and the rubric is not
one.

**Blank state is derived, never stored twice.** `attempt.guesses` is already
ordered and already bounded, so resolution and the ladder rung are read off the
record rather than tracked alongside it (D1). The one predicate that decides
sealing — **all blanks resolved and no probe pending** (D10, CONTEXT: Sealed) —
is written here with both clauses live, so the no-probe case is a special case
of the predicate rather than a "last blank resolved" check that #10 would have
to unwrite.

Specs: `docs/specs/novice-submit.md`, `docs/specs/advanced-submit.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from socratic.domain import ids
from socratic.domain import model_client as model_client_module
from socratic.domain import prompting
from socratic.domain import records
from socratic.domain import registry as registry_module
from socratic.domain import repositories
from socratic.domain.modes import GradingStrategy
from socratic.domain.records import QuizAttempt, Verdict
from socratic.domain.types import Blank

HINT_LADDER_RUNGS = records.HINT_LADDER_RUNGS
"""Three escalating responses to a wrong answer, and the third reveals."""

_VERDICT = "verdict"
_TUTOR_LINE = "tutor_line"
_PROBE_QUESTION = "probe_question"


class BlankAlreadyResolved(ValueError):
    """A submission arrived for a blank that is finished.

    A `ValueError` rather than a silent no-op: the client is authoritative for
    quiz state, so a submission against a resolved blank means the client's
    view and the record have diverged, and swallowing it would hide that.
    """


class GradingParseError(ValueError):
    """A `grade_answer` response the session cannot read.

    Its own type, for the reason `authoring.AuthoringParseError` has one: the
    caller needs to tell "the model returned nonsense" from "we built the wrong
    object", and a `KeyError` escaping from inside a dict lookup tells it
    neither. Raising rather than falling back to a string comparison, because
    an Advanced answer graded by string equality would look like a working
    feature.
    """


@dataclass(frozen=True, slots=True)
class ModelGrading:
    """The two nullable riders that came back **with** the verdict (D13).

    One response, three things: the verdict is on `Submission`, and these two
    rode the same response. Grouping them rather than flattening them onto
    `Submission` is what keeps the deterministic path structurally free of a
    reactive tutor line — `Submission.model_grading` is `None` there, so a
    Novice result has no route to one at all, rather than a field that happens
    always to be null.

    `probe_question` is carried, not acted on. Whether to ask it is
    [#10](https://github.com/derkmed/socratic/issues/10).
    """

    tutor_line: str | None
    probe_question: str | None


@dataclass(frozen=True, slots=True)
class Submission:
    """What one submission returns.

    `revealed_option_id` is non-null only on the rung-three reveal of a blank
    that *has* an option id, which is the single route by which the answer key
    reaches a caller.

    `model_grading` is non-null only on the model-graded path. There is
    deliberately no `tutor_line` attribute here (D13).
    """

    verdict: Verdict
    graded_by: GradingStrategy
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None
    blank_resolved: bool
    attempt_sealed: bool
    model_grading: ModelGrading | None = None


@dataclass(frozen=True, slots=True)
class _Grade:
    """What a grading strategy decided, before any of it is persisted.

    Separate from `Submission` because a grade is about the blank and the
    submission alone, while a `Submission` also reports what happened to the
    attempt — and the strategies have no business knowing about sealing.

    `model_call` is what a strategy that consulted the model wants stamped on
    the attempt. The strategy builds the record and the session writes it, so
    persistence stays in one place.
    """

    verdict: Verdict
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None
    model_grading: ModelGrading | None = None
    model_call: records.ModelCallRecord | None = None


@dataclass(frozen=True, slots=True)
class _GradingContext:
    """Everything a strategy may need, so both entries in `_GRADERS` share one
    signature.

    The deterministic strategy reads only the first three fields and is handed
    the rest anyway. That is the cheaper of the two asymmetries: a table whose
    two entries had different signatures would put an `if` back on the dispatch
    path that `_GRADERS` exists to remove.
    """

    blank: Blank
    submitted: str
    ordinal: int
    """Which attempt at this blank this is, 1-based."""

    attempt: QuizAttempt
    profile: prompting.LearnerProfile
    model_client: model_client_module.ModelClient


Grader = Callable[[_GradingContext], _Grade]
"""A grading strategy. Returns the grade; persistence is the session's."""


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


def _ladder_rung(ordinal: int) -> int:
    """Which rung a wrong answer lands on.

    Every prior guess on an unresolved blank was wrong — a correct one would
    have resolved it — so the attempt ordinal *is* the rung, capped at three.
    Shared by both strategies: a wrong free-text answer walks the same ladder
    as a wrong click.
    """
    return min(ordinal, HINT_LADDER_RUNGS)


def _grade_against_the_key(context: _GradingContext) -> _Grade:
    """The deterministic strategy (D4): compare to the stored key, no model.

    Raises:
      ValueError: If the blank carries no `correct_option_id`. That field rides
        the skeleton call, so its absence is not the late-pedagogy window — it
        is a malformed quiz, and there is nothing to grade against.
    """
    blank = context.blank
    key = blank.correct_option_id
    if key is None:
        raise ValueError(
            f"blank {blank.blank_id!r} carries no answer key to grade against"
        )

    if context.submitted == key:
        return _Grade(
            verdict=Verdict.CORRECT,
            hint_rung_shown=None,
            feedback=blank.reinforcement,
            revealed_option_id=None,
        )

    rung = _ladder_rung(context.ordinal)
    return _Grade(
        verdict=Verdict.INCORRECT,
        hint_rung_shown=rung,
        feedback=_hint_for_rung(blank, rung),
        revealed_option_id=key if rung >= HINT_LADDER_RUNGS else None,
    )


def _grade_by_model(context: _GradingContext) -> _Grade:
    """The model-graded strategy: free recall judged on meaning (D4).

    Assembles the cache-anchored block through `prompting.assemble` — frozen
    prefix, then the volatile tail holding every guess so far across all blanks
    in order and then the blank and free text under consideration (D1) — and
    makes **one** `grade_answer` call. The verdict, the probe question and the
    reactive tutor line all come back on that one response (D13); a second call
    for any of them would falsify master spec acceptance 9.

    A wrong answer walks the same three-rung ladder as a wrong click. The rung
    text comes from `_hint_for_rung`, which an Advanced blank never satisfies —
    the registry forbids an Advanced blank from carrying `hints`, and segment 1
    tells the model not to write one — so the reactive tutor line is what the
    learner actually reads. Nothing is revealed on rung three: there is no
    option id on this path, and the rubric is the key.

    Raises:
      GradingParseError: If the response cannot be read as a verdict and its
        two nullable riders.
    """
    segments = prompting.assemble(
        prompting.CallType.GRADE_ANSWER,
        profile=context.profile,
        probe_cadence=context.attempt.probe_cadence_at_authoring,
        quiz=context.attempt.quiz,
        guesses=_render_guesses(context.attempt.guesses),
        current_blank_id=context.blank.blank_id,
        current_guess=context.submitted,
    )
    response = context.model_client.grade_answer(segments)
    verdict, grading = _parse_grading(response.content)

    call = records.ModelCallRecord(
        call_type=prompting.CallType.GRADE_ANSWER.value,
        message_id=response.message_id,
        usage=response.token_usage(),
    )

    if verdict is Verdict.CORRECT:
        return _Grade(
            verdict=verdict,
            hint_rung_shown=None,
            feedback=None,
            revealed_option_id=None,
            model_grading=grading,
            model_call=call,
        )

    rung = _ladder_rung(context.ordinal)
    return _Grade(
        verdict=verdict,
        hint_rung_shown=rung,
        feedback=_hint_for_rung(context.blank, rung),
        revealed_option_id=None,
        model_grading=grading,
        model_call=call,
    )


def _render_guesses(guesses: Sequence[records.Guess]) -> tuple[str, ...]:
    """The volatile tail's guess log: every guess, all blanks, in order (D1).

    Rendered here because `prompting.assemble` takes lines rather than records
    — the assembler has no business knowing the `Guess` shape, and this module
    is the one that owns it.
    """
    lines = []
    for guess in guesses:
        verdict = guess.verdict.value
        line = f"[{guess.blank_id}] {guess.submitted!r} -> {verdict}"
        if guess.hint_rung_shown is not None:
            line += f" (hint rung {guess.hint_rung_shown})"
        lines.append(line)
    return tuple(lines)


def _parse_grading(content: str) -> "tuple[Verdict, ModelGrading]":
    """Read a `grade_answer` response: the verdict and its two riders.

    The port is deliberately not widened for this. `ModelResponse.content` is
    the structured payload verbatim and parsing it against the call's schema
    belongs to the caller that knows the schema — which is how `authoring.py`
    reads its own response, and the reason a probe question landing here costs
    no change to the five-method `ModelClient`.

    Both riders are optional *and* nullable: segment 1 tells the model to omit
    an optional field rather than fill it with filler, so an absent key and an
    explicit null mean the same thing.

    Raises:
      GradingParseError: On anything that is not an object carrying a known
        verdict and, at most, two string-or-null riders.
    """
    try:
        payload = json.loads(content)
    except ValueError as error:
        raise GradingParseError(
            f"the grading response is not JSON: {error}"
        ) from None
    if not isinstance(payload, dict):
        raise GradingParseError(
            f"the grading response is not an object: {type(payload).__name__}"
        )

    raw = payload.get(_VERDICT)
    if not isinstance(raw, str):
        raise GradingParseError(
            f"the grading response carries no {_VERDICT!r} string"
        )
    try:
        verdict = Verdict(raw)
    except ValueError:
        known = ", ".join(sorted(member.value for member in Verdict))
        raise GradingParseError(
            f"unknown verdict {raw!r}; expected one of {known}"
        ) from None

    return verdict, ModelGrading(
        tutor_line=_optional_line(payload, _TUTOR_LINE),
        probe_question=_optional_line(payload, _PROBE_QUESTION),
    )


def _optional_line(payload: Mapping[str, Any], name: str) -> str | None:
    """One nullable string rider, absent and null read the same way."""
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GradingParseError(
            f"{name!r} on the grading response is a string or null, got "
            f"{type(value).__name__}"
        )
    return value


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
          model_client: The `ModelClient` port. Consulted exactly once per
            submission by the model-graded strategy, and on the deterministic
            path **never consulted** — which is the whole of master spec
            acceptance 6: hand it a `RecordingModelClient(fail_if_called=True)`
            and a Novice attempt runs to sealing without tripping it.
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
        profile: prompting.LearnerProfile | None = None,
    ) -> Submission:
        """Grade one submission, record it, and seal the attempt if it is done.

        Args:
          learner_id: Whose partition the attempt lives in.
          attempt_id: The attempt being worked through.
          blank_id: Which blank was answered.
          submitted: What the learner submitted — an option id under the
            deterministic strategy, free text under the model-graded one.
          profile: The learner profile rendered into segment 2 of a grading
            request. Read only by the model-graded strategy, and defaulted to
            an empty profile for this learner exactly as
            `QuizAuthoring.author` does; reading a stored one is
            [#16](https://github.com/derkmed/socratic/issues/16).

        Returns:
          The verdict, the feedback that goes with it, the ladder rung showing,
          what happened to the blank and the attempt, and — on the model-graded
          path only — the two nullable riders that came back on the same
          response as the verdict.

        Raises:
          KeyError: If there is no such attempt, no such blank on its quiz, or
            no policy registered for its mode.
          BlankAlreadyResolved: If the blank is already answered or revealed.
          GradingParseError: If a grading response cannot be read.
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
        grade = grader(
            _GradingContext(
                blank=blank,
                submitted=submitted,
                ordinal=ordinal,
                attempt=attempt,
                profile=profile
                or prompting.LearnerProfile(learner_id=learner_id),
                model_client=self._model_client,
            )
        )

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
        if grade.model_call is not None:
            attempt = attempt.with_model_call(grade.model_call)

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
            model_grading=grade.model_grading,
        )

    def _now(self) -> datetime:
        """The injected clock as an aware UTC datetime, matching `Ulid`."""
        return datetime.fromtimestamp(self._clock() / 1000, tz=timezone.utc)
