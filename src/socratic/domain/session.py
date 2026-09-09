"""The hot path: a learner submits an answer and gets a verdict back.

`QuizSession.submit` is a seam in the master spec's table, and it is the exact
point where **"a Novice answer costs zero model calls"** becomes assertable
(master spec acceptance 6 and 7). This module holds **both** halves of that
seam — the deterministic strategy and the model-graded one — and the
self-explanation probe that rides on top of them.

**One response, four things** (D13,
[ADR-0013](../../../docs/adr/0013-reactive-tutor-line.md) as extended by
[ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).
The model-graded strategy makes exactly one `grade_answer` call per submission,
and that single response carries the verdict, the probe question (nullable), the
reactive tutor line (nullable) and the hint ladder's rung text (nullable)
together. There is no second request and nothing streams: the parallel tutor
call ADR-0003 described is withdrawn, and adding one back here would falsify
master spec acceptance 9.

**Asking a probe costs no model call; answering one costs exactly one** (D9,
ADR-0009 as superseded by ADR-0011 and ADR-0013). The question is already in
hand either way — pre-authored on the blank for Novice, riding the one
`grade_answer` response for Advanced — so `submit` fires a probe without a
request. `answer_probe` is a **separate method** for exactly that reason: it
makes one `grade_probe` call, and folding it into `submit` would put a call on
the path master spec acceptance 6 says has none.

**The cadence is client-owned and reproducible** (ADR-0009). A coin flip per
correct answer plus the final blank unconditionally, gated by `probe_cadence`,
drawn from an **injected** `random.Random` so a seeded one makes the sequence
exact in tests. The final blank short-circuits before the draw, which is what
makes acceptance 17's guarantee hold for every seed rather than for the ones
that were tried.

**What a failed probe does is a `ModePolicy` field, never a branch.** Advanced
re-opens the blank with the ladder resuming where it left off, capped at one
re-open; Novice corrects and leaves it resolved. `answer_probe` reads
`probe_failure_behavior` off the policy, so a third mode is a registry entry.

**Dispatch reads `ModePolicy.grading_strategy`, never the mode.** `_GRADERS`
maps a `GradingStrategy` to the function that implements it, and `submit` looks
the policy up through the registry. Nothing in this module names a difficulty
mode — an `if mode ==` outside the registry is a bug (CONTEXT: ModeRegistry),
and `tests/test_registry.py` scans for one.

**Novice has no reactive tutor line** (D13,
[ADR-0013](../../../docs/adr/0013-reactive-tutor-line.md)). Its feedback is
wholly pre-authored — the blank's `reinforcement` on a correct answer, its
`hints` on the way up the ladder — which is what makes the zero-call claim true
without qualification. `Submission` therefore carries no field for one: the
nullable riders live on `ModelGrading`, which the deterministic strategy never
builds, so a Novice result has nowhere for a reactive line to be rather than a
field that is merely always null.

**The ladder has text in both modes** (#56,
[ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).
Novice reads its rung off `blank.hints`, where the pedagogy payload pre-authored
it; Advanced reads it off the grading response it just received, because the
registry forbids an Advanced blank from carrying `hints` at all. Both land in
`Submission.feedback`, the way both routes to a probe question land in
`_Grade.probe_question`, so the client renders one field and the caller cannot
tell which mode wrote it.

**The key never leaves the backend** (D4,
[ADR-0003](../../../docs/adr/0003-grading-authority-and-key-custody.md)). A
Novice submission carries an option id and gets back a verdict; the correct
option id appears in the result on exactly one path, the rung-three reveal. An
Advanced blank's rubric *is* the key: it goes into segment 2 of the grading
request and into nothing that `submit` returns. The Advanced rung-three reveal
is the model stating the answer in prose, not the rubric — `revealed_option_id`
stays null there, because there is no option id on that path — and `_safe_hint`
drops any hint that carries the rubric verbatim, so the custody claim holds
whatever the model sends.

**Blank state is derived, never stored twice.** `attempt.guesses` is already
ordered and already bounded, so resolution and the ladder rung are read off the
record rather than tracked alongside it (D1). The one predicate that decides
sealing — **all blanks resolved and no probe pending** (D10, CONTEXT: Sealed) —
is written here with both clauses live. Probes on, probes off and a **dismissed**
probe are all the same predicate and never a second path: dismissal is a marker
on the record (`records.Probe.dismissed_at`), not a branch in `is_sealable`.

Specs: `docs/specs/novice-submit.md`, `docs/specs/advanced-submit.md`,
`docs/specs/self-explanation-probes.md`.
"""

from __future__ import annotations

import dataclasses
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from socratic.domain import ids
from socratic.domain import model_client as model_client_module
from socratic.domain import prompting
from socratic.domain import records
from socratic.domain import registry as registry_module
from socratic.domain import repositories
from socratic.domain.modes import (
    GradingStrategy,
    ProbeCadence,
    ProbeFailureBehavior,
)
from socratic.domain.records import QuizAttempt, Verdict
from socratic.domain.types import Blank

HINT_LADDER_RUNGS = records.HINT_LADDER_RUNGS
"""Three escalating responses to a wrong answer, and the third reveals."""

_VERDICT = "verdict"
_TUTOR_LINE = "tutor_line"
_PROBE_QUESTION = "probe_question"
_HINT = "hint"
_REVEALED_ANSWER = "revealed_answer"
_CORRECTION = "correction"

MAX_REOPENS_PER_BLANK = 1
"""A blank re-opens at most once; a second failed probe reveals and moves on
(ADR-0009). Named rather than inlined because it is the same kind of bound as
`HINT_LADDER_RUNGS` — a pedagogy decision, not an implementation detail."""

_COIN = 0.5
"""The coin flip behind `sometimes`: roughly every second correct answer
(ADR-0009). A learner cannot game a rhythm that does not exist."""


class BlankAlreadyResolved(ValueError):
    """A submission arrived for a blank that is finished.

    A `ValueError` rather than a silent no-op: the client is authoritative for
    quiz state, so a submission against a resolved blank means the client's
    view and the record have diverged, and swallowing it would hide that.
    """


class NoPendingProbe(ValueError):
    """A probe reply or dismissal arrived for a blank with no probe waiting.

    A `ValueError` for the reason `BlankAlreadyResolved` is one: the client is
    authoritative for quiz state, so a reply to a probe the record does not
    have means the two views have diverged, and swallowing it hides that.
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
    """The three nullable riders that came back **with** the verdict (D13).

    One response, four things: the verdict is on `Submission`, and these three
    rode the same response. Grouping them rather than flattening them onto
    `Submission` is what keeps the deterministic path structurally free of a
    reactive tutor line — `Submission.model_grading` is `None` there, so a
    Novice result has no route to one at all, rather than a field that happens
    always to be null.

    `probe_question` is carried, not acted on. Whether to ask it is
    [#10](https://github.com/derkmed/socratic/issues/10).

    `hint` is the hint ladder's rung text, authored on this response because an
    Advanced blank carries no pre-authored `hints` for `_hint_for_rung` to read
    ([#56](https://github.com/derkmed/socratic/issues/56),
    [ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).
    It is carried here *and* routed to `Submission.feedback`, which is the field
    the Novice ladder already fills: the two routes to a rung's text converge on
    one field, exactly as the two routes to a probe question converge on
    `_Grade.probe_question`. It is null on a correct verdict, which has no rung.
    """

    tutor_line: str | None
    probe_question: str | None
    hint: str | None
    revealed_answer: str | None = None


@dataclass(frozen=True, slots=True)
class Submission:
    """What one submission returns.

    `revealed_option_id` is non-null only on the rung-three reveal of a blank
    that *has* an option id, which is the single route by which the answer key
    reaches a caller. It is the **disclosure marker** and nothing else: no
    displayable text is derived from it any more (ADR-0019). What goes in the
    gap is `resolved_answer`, in words, and it is null whenever the blank did
    not resolve or nothing safe was available - never the learner's guess.

    `model_grading` is non-null only on the model-graded path. There is
    deliberately no `tutor_line` attribute here (D13).

    `probe_asked` is the `records.Probe` this submission fired, already
    appended to the attempt, or `None` when the cadence said no. Handing the
    record back rather than a bare question is what lets a caller tell "the
    tutor asked" from "the tutor had a question and did not ask it" — and the
    probe carries the `cadence_at_fire` that decided it.
    """

    verdict: Verdict
    graded_by: GradingStrategy
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None
    blank_resolved: bool
    attempt_sealed: bool
    model_grading: ModelGrading | None = None
    probe_asked: records.Probe | None = None
    resolved_answer: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeAnswer:
    """What one graded self-explanation did.

    `blank_reopened` and `blank_resolved` are separate because they are not
    each other's negation: under `CORRECT_AND_RESOLVE` a failed probe re-opens
    nothing and the blank stays resolved, and under `REOPEN_BLANK` with the cap
    already spent the same pair holds for a different reason.

    `revealed_option_id` is non-null only on that second case — the "reveals
    and moves on" of ADR-0009, mirroring rung three — and only for a blank that
    has an option id at all. An Advanced blank has none, and its rubric is not
    one (D4).
    """

    verdict: Verdict
    correction: str | None
    blank_reopened: bool
    blank_resolved: bool
    revealed_option_id: str | None
    attempt_sealed: bool
    resolved_answer: str | None = None
    """What goes in the gap on the close this path can make, in words
    (ADR-0019). Null when the blank re-opened, and null on an Advanced blank,
    which has no option to name."""


@dataclass(frozen=True, slots=True)
class ProbeDismissal:
    """What waving a probe away did. No verdict, because nothing was graded."""

    blank_resolved: bool
    attempt_sealed: bool


@dataclass(frozen=True, slots=True)
class _Grade:
    """What a grading strategy decided, before any of it is persisted.

    Separate from `Submission` because a grade is about the blank and the
    submission alone, while a `Submission` also reports what happened to the
    attempt — and the strategies have no business knowing about sealing.

    `model_call` is what a strategy that consulted the model wants stamped on
    the attempt. The strategy builds the record and the session writes it, so
    persistence stays in one place.

    `probe_question` is the question this strategy has available to ask, filled
    only on a correct verdict. It is the one field that erases the two routes a
    probe question travels: the deterministic strategy reads it off the blank
    where the pedagogy payload pre-authored it, the model-graded one off the
    response it just received, and `submit` reads neither — it reads this.
    """

    verdict: Verdict
    hint_rung_shown: int | None
    feedback: str | None
    revealed_option_id: str | None
    model_grading: ModelGrading | None = None
    model_call: records.ModelCallRecord | None = None
    probe_question: str | None = None
    resolved_answer: str | None = None
    """What goes in the gap, as plain text, or `None` if the blank did not
    resolve or nothing safe was available to fill it (ADR-0019). Never the
    learner's guess on a wrong answer - that was #127."""


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

    prior_wrong: int
    """How many guesses at this blank were wrong. The ladder rung, not the
    ordinal: after a probe re-opens a blank the ordinal counts a correct answer
    too, and the ladder must resume where it left off rather than one rung on
    from a right answer."""

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


def _safe_hint(blank: Blank, hint: str | None) -> str | None:
    """A model-authored hint, or `None` if it reproduces the answer key.

    **Key custody is structural, not advisory** (D4,
    [ADR-0003](../../../docs/adr/0003-grading-authority-and-key-custody.md)).
    An Advanced blank's rubric *is* the answer key: it goes into segment 2 of
    the grading request and into nothing `submit` returns. Segment 1 now asks
    the model for a hint, and tells it never to reproduce or quote the rubric —
    but an instruction is a request, and the claim ADR-0003 makes is not the
    kind that a request can support. So the rubric coming back is checked for
    here, and the hint dropped whole when it is found.

    **Fails closed.** A dropped hint costs the learner one bad turn; a
    published rubric costs the exercise. Whitespace is normalised before the
    comparison because a rubric rewrapped into a paragraph is the same
    disclosure, and the match is case-sensitive substring rather than fuzzy:
    the point is to catch a copy, and a paraphrase is what was asked for.

    A rubric short enough to appear verbatim inside a legitimate hint is the
    known false positive, and it is the cheap direction of the trade
    ([ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).
    """
    if hint is None or not blank.rubric:
        return hint
    if " ".join(blank.rubric.split()) in " ".join(hint.split()):
        return None
    return hint


def _option_text(blank: Blank, option_id: str | None) -> str | None:
    """The words behind an option id, or `None` if nothing carries it.

    Option ids are **blank-scoped** (CONTEXT: Option id), so this takes the
    blank that scopes the id rather than searching the quiz. That is the whole
    of [#127](https://github.com/derkmed/socratic/issues/127): the client used
    to do this lookup against the rendered document, where the first match in
    order is usually some other blank's option.
    """
    if option_id is None:
        return None
    for option in blank.options or ():
        if option.option_id == option_id:
            return option.text
    return None


REVEAL_MAX_CHARS = 120
"""How long a short-form reveal may be before it stops being one.

A gap is a noun-phrase-shaped hole (ADR-0019). This is the width of the widest
phrase that still reads as one, and it is a **custody** bound as much as a
cosmetic one — grading criteria are prose, and prose does not fit here.
"""


def _safe_revealed_answer(blank: Blank, revealed: str | None) -> str | None:
    """A model-authored short-form reveal, or `None` if it leaks the key.

    **Deliberately not `_safe_hint`, and not a caller of it.**
    [ADR-0019](../../../docs/adr/0019-resolved-blank-text-comes-from-the-service.md).
    The two fields have opposite relationships to the answer key: a hint must
    approach it without arriving, so any hint carrying the rubric is dropped;
    this field's entire job is to *name* the answer, and `_safe_hint`'s rule
    would fire on exactly the use it was added for. A rubric short enough to be
    quoted inside a legitimate hint is a documented false positive there; here
    it would be the common case.

    So the threshold moves rather than the posture. Two bounds, both
    fail-closed:

    1. **Length.** Anything longer than `REVEAL_MAX_CHARS` is not a phrase, and
       a reveal that is not a phrase is not what was asked for. This is what
       stops a paragraph of grading criteria whatever words it uses, and it is
       the bound that does the structural work.
    2. **Verbatim rubric.** Under that cap a short rubric can still be copied
       out whole, so the `_safe_hint` containment check is repeated here for
       the case the cap cannot see. Whitespace is normalised first, for the
       same reason and with the same case-sensitivity: the point is to catch a
       copy, and a paraphrase is what was asked for.

    The false positive that remains is a rubric that *is* the bare answer, which
    would drop a correct reveal of it. That is the cheap direction of the trade
    (ADR-0003): a dropped reveal costs a gap, a published rubric costs the
    exercise. It closes the blank with nothing in it, which is
    [#130](https://github.com/derkmed/socratic/issues/130) and not this.
    """
    if revealed is None or not blank.rubric:
        return revealed
    normalised = " ".join(revealed.split())
    if len(normalised) > REVEAL_MAX_CHARS:
        return None
    if " ".join(blank.rubric.split()) in normalised:
        return None
    return revealed


def _ladder_rung(prior_wrong: int) -> int:
    """Which rung a wrong answer lands on: the next one, capped at three.

    Counted from **wrong** answers rather than from the attempt ordinal. On a
    blank that has never been probed the two are the same number — every prior
    guess on an unresolved blank was wrong, because a correct one would have
    resolved it — which is why no ladder behaviour moves.

    They part company once a failed probe re-opens a blank, and that is the
    whole point: the guesses then include the correct answer that fired the
    probe, and counting it as a rung would skip one. **The ladder resumes where
    it left off** (ADR-0009, CONTEXT: Hint ladder / rung) — a failed probe is
    evidence the learner needed more help, not less.

    Shared by both strategies: a wrong free-text answer walks the same ladder
    as a wrong click.
    """
    return min(prior_wrong + 1, HINT_LADDER_RUNGS)


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
            probe_question=blank.probe_question,
            resolved_answer=_option_text(blank, key),
        )

    rung = _ladder_rung(context.prior_wrong)
    revealed = key if rung >= HINT_LADDER_RUNGS else None
    return _Grade(
        verdict=Verdict.INCORRECT,
        hint_rung_shown=rung,
        feedback=_hint_for_rung(blank, rung),
        revealed_option_id=revealed,
        resolved_answer=_option_text(blank, revealed),
    )


def _grade_by_model(context: _GradingContext) -> _Grade:
    """The model-graded strategy: free recall judged on meaning (D4).

    Assembles the cache-anchored block through `prompting.assemble` — frozen
    prefix, then the volatile tail holding every guess so far across all blanks
    in order and then the blank and free text under consideration (D1) — and
    makes **one** `grade_answer` call. The verdict, the probe question and the
    reactive tutor line all come back on that one response (D13); a second call
    for any of them would falsify master spec acceptance 9.

    A wrong answer walks the same three-rung ladder as a wrong click, and
    **the rung's text is authored on this response** (#56,
    [ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).
    An Advanced blank carries no pre-authored `hints` — the registry forbids
    them — so `_hint_for_rung` has nothing to read, and before ADR-0016 a wrong
    Advanced answer returned a rung number and no text at all.

    The rung is chosen **here**, before the call, as `_ladder_rung` of the wrong
    guesses so far, and stated in the volatile tail. It is knowable in advance
    precisely because it does not depend on the verdict — it is the rung a
    wrong answer *would* land on — which is what lets the hint ride the one
    response already in flight rather than costing a second call.

    Rung three reveals in prose: the model states the answer, and
    `revealed_option_id` stays `None` because there is no option id on this
    path. **The rubric is not the reveal.** Segment 1 forbids reproducing it,
    and a hint that does so anyway is dropped here — key custody (D4,
    ADR-0003) is a structural claim, so it may not rest on the model obeying an
    instruction.

    Raises:
      GradingParseError: If the response cannot be read as a verdict and its
        three nullable riders.
    """
    rung = _ladder_rung(context.prior_wrong)
    segments = prompting.assemble(
        prompting.CallType.GRADE_ANSWER,
        profile=context.profile,
        probe_cadence=context.attempt.probe_cadence_at_authoring,
        quiz=context.attempt.quiz,
        guesses=_render_guesses(context.attempt.guesses),
        current_blank_id=context.blank.blank_id,
        current_guess=context.submitted,
        hint_rung=rung,
    )
    response = context.model_client.grade_answer(segments)
    verdict, grading = _parse_grading(response.content)
    grading = dataclasses.replace(
        grading,
        hint=_safe_hint(context.blank, grading.hint),
        revealed_answer=_safe_revealed_answer(
            context.blank, grading.revealed_answer
        ),
    )

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
            probe_question=grading.probe_question,
            resolved_answer=context.submitted,
        )

    return _Grade(
        verdict=verdict,
        hint_rung_shown=rung,
        feedback=grading.hint or _hint_for_rung(context.blank, rung),
        revealed_option_id=None,
        model_grading=grading,
        model_call=call,
        resolved_answer=(
            grading.revealed_answer if rung >= HINT_LADDER_RUNGS else None
        ),
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
    """Read a `grade_answer` response: the verdict and its three riders.

    The port is deliberately not widened for this. `ModelResponse.content` is
    the structured payload verbatim and parsing it against the call's schema
    belongs to the caller that knows the schema — which is how `authoring.py`
    reads its own response, and the reason a probe question landing here costs
    no change to the five-method `ModelClient`.

    All four riders are optional *and* nullable: segment 1 tells the model to
    omit an optional field rather than fill it with filler, so an absent key and
    an explicit null mean the same thing.

    Raises:
      GradingParseError: On anything that is not an object carrying a known
        verdict and, at most, four string-or-null riders.
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
        hint=_optional_line(payload, _HINT),
        revealed_answer=_optional_line(payload, _REVEALED_ANSWER),
    )


def _parse_probe_grading(content: str) -> "tuple[Verdict, str | None]":
    """Read a `grade_probe` response: the verdict and a nullable correction.

    The same shape as `_parse_grading` and deliberately its own function rather
    than a parameterised one: the two calls have two output schemas, and a
    shared parser that tolerated either would accept a `tutor_line` here, where
    segment 1 asks for a correction addressing what this learner actually said.

    Raises:
      GradingParseError: On anything that is not an object carrying a known
        verdict and, at most, a string-or-null correction.
    """
    try:
        payload = json.loads(content)
    except ValueError as error:
        raise GradingParseError(
            f"the probe grading response is not JSON: {error}"
        ) from None
    if not isinstance(payload, dict):
        raise GradingParseError(
            f"the probe grading response is not an object: "
            f"{type(payload).__name__}"
        )

    raw = payload.get(_VERDICT)
    if not isinstance(raw, str):
        raise GradingParseError(
            f"the probe grading response carries no {_VERDICT!r} string"
        )
    try:
        verdict = Verdict(raw)
    except ValueError:
        known = ", ".join(sorted(member.value for member in Verdict))
        raise GradingParseError(
            f"unknown probe verdict {raw!r}; expected one of {known}"
        ) from None

    return verdict, _optional_line(payload, _CORRECTION)


def _render_self_explanation(probe: records.Probe, self_explanation: str) -> str:
    """The reply, carrying the question it answers, for the volatile tail.

    `prompting.assemble` takes a single "under consideration" string, and for
    this call the pair is what the model needs: the question was chosen by the
    client — pre-authored for Novice, model-authored a call ago for Advanced —
    so the request has to say which one is being answered. Rendered here for
    the reason `_render_guesses` is: the assembler has no business knowing the
    `Probe` shape.
    """
    return (
        f"self-explanation {self_explanation!r} in reply to the probe "
        f"{probe.question!r}"
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


def probes_for(attempt: QuizAttempt, blank_id: str) -> tuple[records.Probe, ...]:
    """Every probe fired against one blank, in the order fired."""
    return tuple(probe for probe in attempt.probes if probe.blank_id == blank_id)


def reopens_of(attempt: QuizAttempt, blank_id: str) -> int:
    """How many times a failed probe has re-opened this blank. At most one."""
    return sum(1 for probe in probes_for(attempt, blank_id) if probe.reopened_blank)


def is_blank_resolved(attempt: QuizAttempt, blank_id: str) -> bool:
    """Whether a blank is finished: answered correctly, or revealed.

    Derived rather than stored, so there is no second copy of the truth to
    disagree with `attempt.guesses` and `attempt.probes`.

    `resolved` is **not terminal** (D10, CONTEXT: Sealed). A failed Advanced
    probe re-opens the blank, so a correct answer resolves it only until a
    re-open voids that answer: each re-open consumes exactly the one correct
    guess it followed, which is why the first clause is a comparison rather
    than an `any`. Re-opens are zero on every blank that has never been probed,
    where the clause reads exactly as `any(... is CORRECT)` did before.

    A revealed blank stays resolved regardless — three wrong answers is the end
    of the ladder, and no probe can fire on one, because probes follow correct
    answers only.
    """
    guesses = guesses_for(attempt, blank_id)
    correct = sum(1 for guess in guesses if guess.verdict is Verdict.CORRECT)
    if correct > reopens_of(attempt, blank_id):
        return True
    wrong = sum(1 for guess in guesses if guess.verdict is Verdict.INCORRECT)
    return wrong >= HINT_LADDER_RUNGS


def has_pending_probe(attempt: QuizAttempt) -> bool:
    """Whether a probe is still waiting on the learner.

    The second clause of the seal predicate: **unanswered and not dismissed**.

    Dismissal is read off the record (`records.Probe.dismissed_at`) rather than
    handled by a second sealing path. That is the whole of acceptance 18 — a
    dismissed probe persists with a null `self_explanation`, exactly as a
    pending one does, and stops blocking because the marker says the learner
    declined it. `is_sealable` is unchanged, and one predicate still serves
    probes on, off and dismissed alike (D10, ADR-0010).
    """
    return any(
        not probe.is_answered and not probe.is_dismissed
        for probe in attempt.probes
    )


def is_sealable(attempt: QuizAttempt) -> bool:
    """The unified predicate: **all blanks resolved and no probe pending**.

    One predicate, both clauses, evaluated after every submission — never "the
    last blank resolved". Because a failed Advanced probe can re-open a blank,
    `resolved` is not terminal and completion can fire and un-fire (D10,
    CONTEXT: Sealed); a completion check written around the last blank would be
    correct until the first probe failed.
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
        rng: random.Random | None = None,
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
          rng: The source of the probe cadence's coin flip. Injected for the
            same reason `clock` is: a seeded `random.Random` makes the fired /
            not-fired sequence reproducible in tests (acceptance 17), and the
            module-level `random` functions would make it global state instead.
            Defaults to a fresh unseeded `random.Random`.
        """
        self._model_client = model_client
        self._attempts = attempts
        self._registry = registry or registry_module.default_registry()
        self._clock = clock
        self._rng = rng or random.Random()

    def submit(
        self,
        *,
        learner_id: str,
        attempt_id: str,
        blank_id: str,
        submitted: str,
        profile: prompting.LearnerProfile | None = None,
        probe_cadence: ProbeCadence | None = None,
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
          probe_cadence: The learner's **current** `UserValves` setting, which
            is the caller's to supply because flipping it applies immediately
            (ADR-0010). Defaults to the attempt's
            `probe_cadence_at_authoring`, so a caller that never touches the
            setting behaves as the attempt was authored. A change takes effect
            from this submission on; a probe already pending is not revisited,
            which is master spec acceptance 20.

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
        prior = guesses_for(attempt, blank_id)
        ordinal = len(prior) + 1
        grade = grader(
            _GradingContext(
                blank=blank,
                submitted=submitted,
                ordinal=ordinal,
                prior_wrong=sum(
                    1 for guess in prior if guess.verdict is Verdict.INCORRECT
                ),
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

        cadence = (
            attempt.probe_cadence_at_authoring
            if probe_cadence is None
            else probe_cadence
        )
        probe = self._probe_to_fire(attempt, blank_id, grade, cadence, now)
        if probe is not None:
            attempt = attempt.with_probe(probe)

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
            probe_asked=probe,
            resolved_answer=grade.resolved_answer,
        )

    # --- Probes ---------------------------------------------------------------

    def answer_probe(
        self,
        *,
        learner_id: str,
        attempt_id: str,
        blank_id: str,
        self_explanation: str,
        profile: prompting.LearnerProfile | None = None,
    ) -> ProbeAnswer:
        """Grade one self-explanation, and apply what a failure does.

        **Exactly one model call**, `grade_probe`, in both modes (master spec
        acceptance 8). This is a separate method from `submit` for precisely
        that reason: a Novice *answer* is free and a Novice probe *reply* is
        not, and one method could not carry both claims to a stub.

        What a failed probe does is `ModePolicy.probe_failure_behavior`, read
        from the registry rather than branched on (D9, ADR-0009). Advanced
        re-opens the blank with the ladder resuming where it left off, capped at
        one re-open per blank; Novice corrects the misconception and leaves the
        blank resolved, because re-opening a two-option bank whose answer the
        learner was just told is degenerate.

        Args:
          learner_id: Whose partition the attempt lives in.
          attempt_id: The attempt being worked through.
          blank_id: Which blank's pending probe is being answered.
          self_explanation: The learner's free-text reply. The richest signal
            the system collects, and captured verbatim.
          profile: As `submit`; defaulted to an empty profile for this learner.

        Returns:
          The verdict, the correction that goes with it, and what happened to
          the blank and the attempt.

        Raises:
          KeyError: If there is no such attempt, or no policy for its mode.
          NoPendingProbe: If no probe on that blank is waiting on the learner.
          GradingParseError: If the response cannot be read.
          ValueError: If the attempt is already sealed.
        """
        attempt = self._attempts.get(learner_id, attempt_id)
        position, probe = self._pending_probe(attempt, blank_id)
        blank = attempt.quiz.blank(blank_id)
        policy = self._registry.policy_for(attempt.mode)

        segments = prompting.assemble(
            prompting.CallType.GRADE_PROBE,
            profile=profile or prompting.LearnerProfile(learner_id=learner_id),
            probe_cadence=attempt.probe_cadence_at_authoring,
            quiz=attempt.quiz,
            guesses=_render_guesses(attempt.guesses),
            current_blank_id=blank_id,
            current_guess=_render_self_explanation(probe, self_explanation),
        )
        response = self._model_client.grade_probe(segments)
        verdict, correction = _parse_probe_grading(response.content)

        reopen = (
            verdict is Verdict.INCORRECT
            and policy.probe_failure_behavior is ProbeFailureBehavior.REOPEN_BLANK
            and reopens_of(attempt, blank_id) < MAX_REOPENS_PER_BLANK
        )
        revealed = (
            blank.correct_option_id
            if verdict is Verdict.INCORRECT
            and policy.probe_failure_behavior is ProbeFailureBehavior.REOPEN_BLANK
            and not reopen
            else None
        )

        now = self._now()
        attempt = attempt.with_probe_resolved(
            position,
            dataclasses.replace(
                probe,
                self_explanation=self_explanation,
                verdict=verdict,
                reopened_blank=reopen,
                answered_at=now,
                message_id=response.message_id,
            ),
        ).with_model_call(
            records.ModelCallRecord(
                call_type=prompting.CallType.GRADE_PROBE.value,
                message_id=response.message_id,
                usage=response.token_usage(),
            )
        )

        sealed = is_sealable(attempt)
        if sealed:
            attempt = attempt.sealed(now)
        self._attempts.save(attempt)

        return ProbeAnswer(
            verdict=verdict,
            correction=correction,
            blank_reopened=reopen,
            blank_resolved=is_blank_resolved(attempt, blank_id),
            revealed_option_id=revealed,
            attempt_sealed=sealed,
            resolved_answer=_option_text(blank, revealed),
        )

    def dismiss_probe(
        self, *, learner_id: str, attempt_id: str, blank_id: str
    ) -> ProbeDismissal:
        """Wave a pending probe away. **No model call.**

        The learner's escape, and the reason a probe already on screen stands
        when `probe_cadence` is turned off mid-quiz: the question is never
        withdrawn — withdrawing one while someone is typing an answer to it is
        worse than one more probe — so declining it is the only cancellation
        path, and it has no partial state (ADR-0010).

        The probe persists with a null `self_explanation` and a stamped
        `dismissed_at`, and stops blocking the seal predicate (acceptance 18).

        Raises:
          KeyError: If there is no such attempt.
          NoPendingProbe: If no probe on that blank is waiting on the learner.
          ValueError: If the attempt is already sealed.
        """
        attempt = self._attempts.get(learner_id, attempt_id)
        position, probe = self._pending_probe(attempt, blank_id)

        now = self._now()
        attempt = attempt.with_probe_resolved(
            position, dataclasses.replace(probe, dismissed_at=now)
        )

        sealed = is_sealable(attempt)
        if sealed:
            attempt = attempt.sealed(now)
        self._attempts.save(attempt)

        return ProbeDismissal(
            blank_resolved=is_blank_resolved(attempt, blank_id),
            attempt_sealed=sealed,
        )

    # --- Internals ------------------------------------------------------------

    def _probe_to_fire(
        self,
        attempt: QuizAttempt,
        blank_id: str,
        grade: _Grade,
        cadence: ProbeCadence,
        now: datetime,
    ) -> records.Probe | None:
        """The probe this submission fires, or `None`.

        Three gates, cheapest and most decisive first, so the RNG is drawn on
        as few paths as possible and the seeded sequence stays a function of
        correct answers alone:

        1. Only a correct answer is ever probed.
        2. The cadence decides, and only `sometimes` draws a coin.
        3. There has to be a question to ask, and room for it on the blank.

        Gate 3 is last because it is the one that can be absent for a reason
        that has nothing to do with the learner — the pedagogy payload landing
        after the skeleton (master spec acceptance 5). A blank in that window
        costs the learner the probe, not the verdict, and it does not perturb
        the coin.
        """
        if grade.verdict is not Verdict.CORRECT:
            return None
        if not self._cadence_says_probe(attempt, blank_id, cadence):
            return None
        if grade.probe_question is None:
            return None
        if len(probes_for(attempt, blank_id)) >= records.MAX_PROBES_PER_BLANK:
            return None

        return records.Probe(
            blank_id=blank_id,
            question=grade.probe_question,
            self_explanation=None,
            verdict=None,
            reopened_blank=False,
            cadence_at_fire=cadence,
            asked_at=now,
            answered_at=None,
            message_id=None,
        )

    def _cadence_says_probe(
        self, attempt: QuizAttempt, blank_id: str, cadence: ProbeCadence
    ) -> bool:
        """The client-owned cadence: a coin flip, plus the final blank.

        **The final blank short-circuits before the draw**, which is what makes
        "the final blank is always probed regardless of the seed" (acceptance
        17) structural rather than lucky: on that blank the RNG is not consulted
        at all, so no seed can decide otherwise.

        The final blank is the last one the quiz *declares*, not the last one
        the learner happens to resolve. Declared order is the same thing when a
        learner works through the explanation in order, and unlike resolution
        order it is fixed before the attempt starts — which is what acceptance
        19's "the last blank and no other" is checkable against.
        """
        is_final = attempt.quiz.blanks[-1].blank_id == blank_id

        if cadence is ProbeCadence.OFF:
            return False
        if cadence is ProbeCadence.ALWAYS:
            return True
        if cadence is ProbeCadence.FINAL_BLANK_ONLY:
            return is_final
        return is_final or self._rng.random() < _COIN

    def _pending_probe(
        self, attempt: QuizAttempt, blank_id: str
    ) -> "tuple[int, records.Probe]":
        """The blank's probe still waiting on the learner, and where it sits.

        The position rather than the probe alone, because two probes on one
        blank can compare equal under a frozen clock and the record is written
        back by position.

        Raises:
          NoPendingProbe: If there is none.
        """
        for position, probe in enumerate(attempt.probes):
            if (
                probe.blank_id == blank_id
                and not probe.is_answered
                and not probe.is_dismissed
            ):
                return position, probe
        raise NoPendingProbe(
            f"no probe on blank {blank_id!r} of attempt {attempt.attempt_id} is "
            "waiting on the learner"
        )

    def _now(self) -> datetime:
        """The injected clock as an aware UTC datetime, matching `Ulid`."""
        return datetime.fromtimestamp(self._clock() / 1000, tz=timezone.utc)
