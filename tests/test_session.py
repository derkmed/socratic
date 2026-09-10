"""Submission — both halves of the seam.

Novice (spec `docs/specs/novice-submit.md`, issue #6) is the hot path: a learner
clicks an option and gets a verdict back with **zero model calls**. Advanced
(spec `docs/specs/advanced-submit.md`, issue #8) takes free text and is graded
on meaning by one `grade_answer` call whose single response carries the verdict,
the probe question and the reactive tutor line **together**.

Everything here runs against `RecordingModelClient` and
`InMemoryAttemptRepository` — those *are* the doubles (master spec seam table),
so there is no fake in this file and no test reaches the network.

Three call-budget assertions, deliberately not collapsed into one:

* `TestZeroModelCalls` is master spec acceptance 6 — one submission against a
  stub configured to fail the moment it is invoked.
* `TestAFullNoviceQuizCostsOneCall` is acceptance 7, the stronger claim — one
  stub instance serves authoring *and* the whole attempt, and its call count is
  still 1 once the attempt seals. It walks paths the per-answer test never
  does: a second blank, the ladder, the reveal, and sealing.
* `TestOneResponseCarriesThreeThings` is acceptance 9, the Advanced ceiling —
  **exactly one** call per submission, and no second one for the tutor line
  (D13: the parallel tutor call is withdrawn and nothing streams).

**What the stub can and cannot prove.** Against `RecordingModelClient` the
Advanced tests assert *plumbing*: that the rubric reaches segment 2, that the
free text reaches the volatile tail after the guesses in order, and that a
`CORRECT` verdict on differently-phrased text flows through to a `Guess` marked
`model_graded`. Whether the model actually judges on meaning rather than wording
is only demonstrable against the live API (#7), so master spec acceptance 3 is
covered here only as far as a stub can cover it.
"""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import datetime, timezone

import pytest

from socratic.domain import session as session_module
from socratic.domain.authoring import QuizAuthoring
from socratic.domain.ids import Ulid, new_quiz_session_id
from socratic.domain.model_client import ModelResponse, RecordingModelClient
from socratic.domain.modes import (
    DifficultyMode,
    GradingStrategy,
    ProbeCadence,
    ProbeFailureBehavior,
    RenderHint,
)
from socratic.domain.prompting import CallType, SegmentRole
from socratic.domain.records import (
    Guess,
    Outcome,
    Probe,
    QuizAttempt,
    Verdict,
)
from socratic.domain.registry import (
    ADVANCED_POLICY,
    BlankRange,
    ModePolicy,
    ModeRegistry,
    NOVICE_POLICY,
    default_registry,
)
from socratic.domain.repositories import InMemoryAttemptRepository
from socratic.domain.session import (
    BlankAlreadyResolved,
    GradingParseError,
    QuizSession,
    ResolvedAnswer,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

LEARNER = "learner-ada"

FROZEN_MILLIS = 1_760_000_000_000


def frozen_clock() -> int:
    """Milliseconds since the epoch, held still, as in `test_authoring`."""
    return FROZEN_MILLIS


FROZEN_AT = datetime.fromtimestamp(FROZEN_MILLIS / 1000, tz=timezone.utc)


# --- Fixtures ----------------------------------------------------------------


def novice_blank(blank_id: str = "b1", *, pedagogy: bool = True) -> Blank:
    """A Novice blank. `pedagogy=False` is the #9 window: the skeleton has
    landed, the hints and reinforcement have not."""
    return Blank(
        blank_id=blank_id,
        mode=DifficultyMode.NOVICE,
        options=(
            Option(option_id=f"{blank_id}-o1", text="entropy"),
            Option(option_id=f"{blank_id}-o2", text="enthalpy"),
        ),
        correct_option_id=f"{blank_id}-o1",
        reinforcement="Entropy is the one that never decreases." if pedagogy else None,
        hints=(
            "Think about disorder.",
            "It is the quantity the second law bounds.",
            "The word is 'entropy'.",
        )
        if pedagogy
        else None,
        probe_question="How did you arrive at that?" if pedagogy else None,
    )


def novice_quiz(blank_ids: tuple[str, ...] = ("b1",), *, pedagogy: bool = True) -> Quiz:
    explanation: list = [TextSegment(text="Heat flows from hot to cold because ")]
    for blank_id in blank_ids:
        explanation.append(BlankSegment(blank_id=blank_id))
        explanation.append(TextSegment(text=" rises."))
    return Quiz(
        quiz_session_id=new_quiz_session_id(clock=frozen_clock),
        mode=DifficultyMode.NOVICE,
        topic="the second law of thermodynamics",
        explanation=tuple(explanation),
        blanks=tuple(
            novice_blank(blank_id, pedagogy=pedagogy) for blank_id in blank_ids
        ),
        recap="Entropy never decreases in an isolated system.",
    )


RUBRIC = (
    "ANSWER-KEY-RUBRIC: any phrasing naming the quantity the second law bounds"
)
"""Distinctive on purpose: several tests assert this string reaches the model
request and reaches no part of what `submit` hands back (D4, ADR-0003)."""


def advanced_blank(blank_id: str = "b1") -> Blank:
    """An Advanced blank: a rubric, and none of the Novice pedagogy.

    The registry's validator forbids `options`, `correct_option_id`,
    `reinforcement` and `hints` on an Advanced blank, so this is the whole of
    what the grader has to work with.
    """
    return Blank(
        blank_id=blank_id,
        mode=DifficultyMode.ADVANCED,
        rubric=f"{RUBRIC} ({blank_id})",
    )


def advanced_quiz(blank_ids: tuple[str, ...] = ("b1",)) -> Quiz:
    explanation: list = [TextSegment(text="Heat flows from hot to cold because ")]
    for blank_id in blank_ids:
        explanation.append(BlankSegment(blank_id=blank_id))
        explanation.append(TextSegment(text=" rises."))
    return Quiz(
        quiz_session_id=new_quiz_session_id(clock=frozen_clock),
        mode=DifficultyMode.ADVANCED,
        topic="the second law of thermodynamics",
        explanation=tuple(explanation),
        blanks=tuple(advanced_blank(blank_id) for blank_id in blank_ids),
        recap="Entropy never decreases in an isolated system.",
    )


def graded(
    verdict: str = "correct",
    *,
    tutor_line: str | None = None,
    probe_question: str | None = None,
    hint: str | None = None,
    revealed_answer: str | None = None,
    message_id: str = "msg_grade_1",
    **counters: int,
) -> ModelResponse:
    """One `grade_answer` response: the verdict and the four nullable fields.

    Optional fields are *omitted* rather than sent as null when they have no
    value — segment 1 tells the model to omit rather than fill with filler, so
    the parser has to tolerate an absent key, not just a null one.
    """
    payload: dict = {"verdict": verdict}
    if tutor_line is not None:
        payload["tutor_line"] = tutor_line
    if probe_question is not None:
        payload["probe_question"] = probe_question
    if hint is not None:
        payload["hint"] = hint
    if revealed_answer is not None:
        payload["revealed_answer"] = revealed_answer
    return ModelResponse(
        content=json.dumps(payload), message_id=message_id, **counters
    )


def grading_stub(*responses: ModelResponse) -> RecordingModelClient:
    """A stub queued to answer `grade_answer` and nothing else."""
    return RecordingModelClient({CallType.GRADE_ANSWER: list(responses) or [graded()]})


def attempt_for(
    quiz: Quiz, *, probe_cadence: ProbeCadence = ProbeCadence.OFF
) -> QuizAttempt:
    return QuizAttempt(
        attempt_id=str(Ulid.mint(clock=frozen_clock)),
        learner_id=LEARNER,
        session_id=quiz.quiz_session_id,
        quiz=quiz,
        mode=quiz.mode,
        topic=quiz.topic,
        created_at=FROZEN_AT,
        probe_cadence_at_authoring=probe_cadence,
        model_id="claude-opus-5",
        effort="low",
        prompt_version="1",
    )


def wire(
    attempt: QuizAttempt,
    *,
    model_client: RecordingModelClient | None = None,
    registry: ModeRegistry | None = None,
) -> "tuple[QuizSession, InMemoryAttemptRepository]":
    """A session over a repository already holding `attempt`.

    The model client defaults to the stub that fails the moment it is invoked,
    because that is the correct default for every Novice test in this file.
    """
    attempts = InMemoryAttemptRepository()
    attempts.save(attempt)
    quiz_session = QuizSession(
        model_client=model_client or RecordingModelClient(fail_if_called=True),
        attempts=attempts,
        registry=registry,
        clock=frozen_clock,
    )
    return quiz_session, attempts


def submit(quiz_session: QuizSession, attempt: QuizAttempt, blank_id: str, option: str):
    return quiz_session.submit(
        learner_id=attempt.learner_id,
        attempt_id=attempt.attempt_id,
        blank_id=blank_id,
        submitted=option,
    )


# --- Acceptance 6 ------------------------------------------------------------


class TestZeroModelCalls:
    """Master spec acceptance 6 — a Novice answer costs no model call, and the
    stub fails the test the moment it is consulted rather than after."""

    def test_a_correct_answer_returns_a_verdict_without_consulting_the_model(self):
        attempt = attempt_for(novice_quiz())
        stub = RecordingModelClient(fail_if_called=True)
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.verdict is Verdict.CORRECT
        stub.assert_never_called()

    def test_a_wrong_answer_returns_a_verdict_without_consulting_the_model(self):
        attempt = attempt_for(novice_quiz())
        stub = RecordingModelClient(fail_if_called=True)
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "b1-o2")

        assert result.verdict is Verdict.INCORRECT
        stub.assert_never_called()


class TestNoReactiveTutorLine:
    """D13: Novice feedback is wholly pre-authored, which is what makes
    "zero model calls" true without qualification."""

    def test_the_correct_verdict_carries_the_pre_authored_reinforcement(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.feedback == "Entropy is the one that never decreases."
        assert result.hint_rung_shown is None

    def test_the_result_has_no_field_for_a_reactive_line(self):
        assert not hasattr(
            session_module.Submission(
                verdict=Verdict.CORRECT,
                graded_by=GradingStrategy.DETERMINISTIC,
                hint_rung_shown=None,
                feedback=None,
                revealed_option_id=None,
                blank_resolved=True,
                attempt_sealed=False,
            ),
            "tutor_line",
        )

    def test_no_novice_path_reaches_a_reactive_line(self):
        # Asserted rather than assumed (D13). The two nullable fields ride
        # `model_grading`, which the deterministic strategy never populates —
        # so every Novice route, correct or up the ladder or revealed, has
        # nowhere for a reactive line to be.
        attempt = attempt_for(novice_quiz(("b1", "b2")))
        quiz_session, _ = wire(attempt)

        walked = [submit(quiz_session, attempt, "b1", "b1-o2") for _ in range(3)]
        walked.append(submit(quiz_session, attempt, "b2", "b2-o1"))

        assert [step.hint_rung_shown for step in walked] == [1, 2, 3, None]
        assert all(step.model_grading is None for step in walked)


# --- Acceptance 2: the ladder ------------------------------------------------


class TestTheThreeRungLadder:
    def test_a_wrong_answer_walks_the_rungs_and_reveals_on_the_third(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)

        first = submit(quiz_session, attempt, "b1", "b1-o2")
        second = submit(quiz_session, attempt, "b1", "b1-o2")
        third = submit(quiz_session, attempt, "b1", "b1-o2")

        assert [step.hint_rung_shown for step in (first, second, third)] == [1, 2, 3]
        assert [step.feedback for step in (first, second, third)] == [
            "Think about disorder.",
            "It is the quantity the second law bounds.",
            "The word is 'entropy'.",
        ]
        assert first.revealed_option_id is None
        assert second.revealed_option_id is None
        assert third.revealed_option_id == "b1-o1"
        assert third.blank_resolved is True

    def test_the_answer_key_stays_in_the_backend_until_the_reveal(self):
        # ADR-0003: the key never leaves the backend. The only route by which
        # `submit` returns it is the rung-three reveal.
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        assert submit(quiz_session, attempt, "b1", "b1-o1").revealed_option_id is None

    def test_a_correct_answer_after_a_wrong_one_resumes_the_ordinal(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)

        submit(quiz_session, attempt, "b1", "b1-o2")
        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.verdict is Verdict.CORRECT
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert [guess.attempt_ordinal for guess in stored.guesses] == [1, 2]

    def test_a_resolved_blank_refuses_a_further_submission(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)
        submit(quiz_session, attempt, "b1", "b1-o1")

        with pytest.raises(BlankAlreadyResolved):
            submit(quiz_session, attempt, "b1", "b1-o2")

    def test_a_revealed_blank_refuses_a_further_submission(self):
        attempt = attempt_for(novice_quiz(("b1", "b2")))
        quiz_session, _ = wire(attempt)
        for _ in range(3):
            submit(quiz_session, attempt, "b1", "b1-o2")

        with pytest.raises(BlankAlreadyResolved):
            submit(quiz_session, attempt, "b1", "b1-o1")

    def test_an_unknown_blank_is_rejected(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        with pytest.raises(KeyError):
            submit(quiz_session, attempt, "nope", "b1-o1")


# --- The persisted guesses ---------------------------------------------------


class TestGuessesPersistInTheOrderMade:
    def test_every_guess_is_stamped_and_ordered(self):
        attempt = attempt_for(novice_quiz(("b1", "b2")))
        quiz_session, attempts = wire(attempt)

        submit(quiz_session, attempt, "b1", "b1-o2")
        submit(quiz_session, attempt, "b1", "b1-o1")
        submit(quiz_session, attempt, "b2", "b2-o1")

        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert [(guess.blank_id, guess.submitted) for guess in stored.guesses] == [
            ("b1", "b1-o2"),
            ("b1", "b1-o1"),
            ("b2", "b2-o1"),
        ]
        assert [guess.verdict for guess in stored.guesses] == [
            Verdict.INCORRECT,
            Verdict.CORRECT,
            Verdict.CORRECT,
        ]
        assert [guess.hint_rung_shown for guess in stored.guesses] == [1, None, None]
        assert all(
            guess.graded_by is GradingStrategy.DETERMINISTIC
            for guess in stored.guesses
        )
        assert all(guess.created_at == FROZEN_AT for guess in stored.guesses)


# --- Acceptance: the unified seal predicate ----------------------------------


class TestADisplacedAttemptTakesNoMoreAnswers:
    """#15: displacement closes the attempt, so the submit path refuses it.

    The learner said "start this instead"; a guess arriving from the tab they
    left behind is not a verdict to grade, it is a write to a closed record.
    """

    def test_a_submission_against_an_abandoned_attempt_is_refused(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)
        attempts.save(attempt.abandoned())

        with pytest.raises(ValueError, match="abandoned"):
            submit(quiz_session, attempt, "b1", "b1-o1")

    def test_the_abandoned_record_is_left_exactly_as_it_was(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)
        abandoned = attempt.abandoned()
        attempts.save(abandoned)

        with pytest.raises(ValueError):
            submit(quiz_session, attempt, "b1", "b1-o1")

        assert attempts.get(LEARNER, attempt.attempt_id) == abandoned


class TestSealingGoesThroughTheUnifiedPredicate:
    def test_the_attempt_seals_when_every_blank_is_resolved(self):
        attempt = attempt_for(novice_quiz(("b1", "b2")))
        quiz_session, attempts = wire(attempt)

        first = submit(quiz_session, attempt, "b1", "b1-o1")
        assert first.attempt_sealed is False

        second = submit(quiz_session, attempt, "b2", "b2-o1")
        assert second.attempt_sealed is True

        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert stored.outcome is Outcome.RESOLVED
        assert stored.sealed_at == FROZEN_AT

    def test_a_revealed_blank_counts_as_resolved_for_the_predicate(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)

        for _ in range(2):
            submit(quiz_session, attempt, "b1", "b1-o2")
        assert submit(quiz_session, attempt, "b1", "b1-o2").attempt_sealed is True

    def test_a_pending_probe_blocks_sealing_though_every_blank_is_resolved(self):
        # The clause that a "last blank resolved" check would not have. Nothing
        # in this ticket fires a probe, so one is put on the attempt directly;
        # #10 supplies the firing.
        quiz = novice_quiz()
        attempt = attempt_for(quiz).with_probe(
            Probe(
                blank_id="b1",
                question="How did you arrive at that?",
                self_explanation=None,
                verdict=None,
                reopened_blank=False,
                cadence_at_fire=ProbeCadence.ALWAYS,
                asked_at=FROZEN_AT,
                answered_at=None,
                message_id=None,
            )
        )
        quiz_session, attempts = wire(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.blank_resolved is True
        assert result.attempt_sealed is False
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert stored.outcome is Outcome.IN_FLIGHT
        assert stored.sealed_at is None

    def test_the_predicate_reads_both_clauses(self):
        quiz = novice_quiz()
        resolved = attempt_for(quiz).with_guess(
            Guess(
                blank_id="b1",
                submitted="b1-o1",
                verdict=Verdict.CORRECT,
                attempt_ordinal=1,
                hint_rung_shown=None,
                created_at=FROZEN_AT,
                graded_by=GradingStrategy.DETERMINISTIC,
            )
        )
        assert session_module.is_sealable(resolved) is True
        assert session_module.is_sealable(attempt_for(quiz)) is False


# --- Dispatch ----------------------------------------------------------------


class TestDispatchGoesThroughTheGradingStrategy:
    def test_the_same_quiz_follows_the_registry_and_not_its_mode(self):
        # The proof that `grading_strategy` is what is read: a registry whose
        # policy for this mode says MODEL_GRADED sends an otherwise ordinary
        # Novice quiz down the model-graded path.
        attempt = attempt_for(novice_quiz())
        model_graded = ModeRegistry(
            {
                DifficultyMode.NOVICE: ModePolicy(
                    authoring_schema_fragment=NOVICE_POLICY.authoring_schema_fragment,
                    grading_strategy=GradingStrategy.MODEL_GRADED,
                    validate_blank=NOVICE_POLICY.validate_blank,
                    render_hint=RenderHint.OPTION_BANK,
                    blank_range=BlankRange(1, 2),
                    probe_failure_behavior=ProbeFailureBehavior.CORRECT_AND_RESOLVE,
                )
            }
        )
        stub = grading_stub(graded("correct"))
        quiz_session, _ = wire(attempt, model_client=stub, registry=model_graded)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.graded_by is GradingStrategy.MODEL_GRADED
        assert stub.call_count(CallType.GRADE_ANSWER) == 1

    def test_an_unregistered_mode_is_a_wiring_error(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt, registry=ModeRegistry())

        with pytest.raises(KeyError):
            submit(quiz_session, attempt, "b1", "b1-o1")

    def test_the_default_registry_grades_novice_deterministically(self):
        assert (
            default_registry().policy_for(DifficultyMode.NOVICE).grading_strategy
            is GradingStrategy.DETERMINISTIC
        )


# --- Pedagogy that has not landed yet (#9) -----------------------------------


class TestAbsentPedagogyIsToleratedNotFatal:
    """The skeleton is playable before the pedagogy payload arrives (#9, master
    spec acceptance 5). A blank with no hints still grades."""

    def test_a_correct_answer_with_no_reinforcement_returns_a_verdict(self):
        attempt = attempt_for(novice_quiz(pedagogy=False))
        quiz_session, _ = wire(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.verdict is Verdict.CORRECT
        assert result.feedback is None
        assert result.blank_resolved is True

    def test_the_ladder_still_walks_and_still_reveals_with_no_hints(self):
        attempt = attempt_for(novice_quiz(pedagogy=False))
        quiz_session, attempts = wire(attempt)

        rungs = [submit(quiz_session, attempt, "b1", "b1-o2") for _ in range(3)]

        assert [step.hint_rung_shown for step in rungs] == [1, 2, 3]
        assert [step.feedback for step in rungs] == [None, None, None]
        assert rungs[-1].revealed_option_id == "b1-o1"
        assert attempts.get(LEARNER, attempt.attempt_id).is_sealed

    def test_a_short_hint_ladder_does_not_raise(self):
        # A half-written payload is not the same as an absent one, and neither
        # is worth a 500 on the hot path.
        quiz = novice_quiz()
        blank = quiz.blanks[0]
        attempt = attempt_for(
            Quiz(
                quiz_session_id=quiz.quiz_session_id,
                mode=quiz.mode,
                topic=quiz.topic,
                explanation=quiz.explanation,
                blanks=(dataclasses.replace(blank, hints=("only one",)),),
                recap=quiz.recap,
            )
        )
        quiz_session, _ = wire(attempt)

        assert submit(quiz_session, attempt, "b1", "b1-o2").feedback == "only one"
        assert submit(quiz_session, attempt, "b1", "b1-o2").feedback is None

    def test_a_blank_with_no_answer_key_is_a_wiring_error(self):
        # `correct_option_id` rides the skeleton, so its absence is not the #9
        # window — it is a malformed quiz, and grading cannot be faked.
        quiz = novice_quiz()
        attempt = attempt_for(
            Quiz(
                quiz_session_id=quiz.quiz_session_id,
                mode=quiz.mode,
                topic=quiz.topic,
                explanation=quiz.explanation,
                blanks=(
                    dataclasses.replace(quiz.blanks[0], correct_option_id=None),
                ),
                recap=quiz.recap,
            )
        )
        quiz_session, _ = wire(attempt)

        with pytest.raises(ValueError):
            submit(quiz_session, attempt, "b1", "b1-o1")


# --- Acceptance 7 ------------------------------------------------------------


class TestAFullNoviceQuizCostsOneCall:
    """Master spec acceptance 7, and deliberately not a restatement of 6.

    Acceptance 6 is one submission against a stub that raises on contact.
    This walks a quiz that `QuizAuthoring` actually authored, with
    `probe_cadence: off`, through every blank — wrong answers, the ladder, the
    reveal, a correct answer, and sealing — with **one** stub instance serving
    both the authoring call and the whole attempt. The count that was 1 after
    authoring is still 1 after the attempt seals.
    """

    def _authored(self):
        """Author a two-blank Novice quiz, and hand back the stub that did it."""
        stub = RecordingModelClient(
            {
                CallType.AUTHOR_SKELETON: [
                    ModelResponse(content=_SKELETON, message_id="msg_1")
                ]
            }
        )
        attempts = InMemoryAttemptRepository()
        QuizAuthoring(
            model_client=stub,
            attempts=attempts,
            clock=frozen_clock,
        ).author(
            "why does heat flow one way?",
            LEARNER,
            mode=DifficultyMode.NOVICE,
            probe_cadence=ProbeCadence.OFF,
        )
        (attempt,) = attempts.list_for_learner(LEARNER)
        return stub, attempts, attempt

    def test_the_whole_attempt_adds_no_call_to_the_authoring_one(self):
        stub, attempts, attempt = self._authored()
        assert stub.call_count() == 1, "authoring is one call (#9 splits it in two)"

        quiz_session = QuizSession(
            model_client=stub, attempts=attempts, clock=frozen_clock
        )

        def answer(blank_id: str, option: str):
            return quiz_session.submit(
                learner_id=LEARNER,
                attempt_id=attempt.attempt_id,
                blank_id=blank_id,
                submitted=option,
            )

        # b1 the hard way: all three rungs, revealed.
        for _ in range(3):
            answer("b1", "o2")
        # b2 wrong once, then right.
        answer("b2", "o1")
        last = answer("b2", "o2")

        assert last.attempt_sealed is True
        assert attempts.get(LEARNER, attempt.attempt_id).outcome is Outcome.RESOLVED

        assert stub.call_count() == 1
        assert stub.calls[0].call_type is CallType.AUTHOR_SKELETON
        for call_type in (
            CallType.AUTHOR_PEDAGOGY,
            CallType.GRADE_ANSWER,
            CallType.GRADE_PROBE,
            CallType.FOLD_NARRATIVE,
        ):
            stub.assert_never_called(call_type)

    def test_a_stub_that_raises_on_contact_survives_the_whole_attempt(self):
        # The same walk, but with the session holding a stub that fails at the
        # moment of a call rather than after the fact — so a stray call names
        # its own culprit in the traceback.
        _, attempts, attempt = self._authored()
        never = RecordingModelClient(fail_if_called=True)
        quiz_session = QuizSession(
            model_client=never, attempts=attempts, clock=frozen_clock
        )

        for blank_id, options in (("b1", ("o2", "o2", "o2")), ("b2", ("o1", "o2"))):
            for option in options:
                quiz_session.submit(
                    learner_id=LEARNER,
                    attempt_id=attempt.attempt_id,
                    blank_id=blank_id,
                    submitted=option,
                )

        assert attempts.get(LEARNER, attempt.attempt_id).is_sealed
        never.assert_never_called()


# --- Advanced: model-graded free text (#8) -----------------------------------


class TestAdvancedFreeTextIsGradedOnMeaning:
    """Master spec acceptance 3, as far as a stub can carry it.

    What is asserted here is plumbing: differently-phrased free text reaches
    the model, a `CORRECT` verdict comes back, and it flows through to a guess
    marked `model_graded`. The judgement itself belongs to the live API (#7).
    """

    def test_a_correct_answer_phrased_differently_from_the_rubric_passes(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("correct"))
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(
            quiz_session, attempt, "b1", "the messiness of the system goes up"
        )

        assert result.verdict is Verdict.CORRECT
        assert result.blank_resolved is True
        assert result.hint_rung_shown is None
        tail = stub.calls[0].segments.volatile_tail.text
        assert "the messiness of the system goes up" in tail

    def test_the_guess_persists_as_model_graded(self):
        attempt = attempt_for(advanced_quiz())
        quiz_session, attempts = wire(attempt, model_client=grading_stub())

        submit(quiz_session, attempt, "b1", "disorder rises")

        (guess,) = attempts.get(LEARNER, attempt.attempt_id).guesses
        assert guess.graded_by is GradingStrategy.MODEL_GRADED
        assert guess.submitted == "disorder rises"
        assert guess.verdict is Verdict.CORRECT
        assert guess.created_at == FROZEN_AT

    def test_a_wrong_answer_is_the_models_call_not_a_string_comparison(self):
        # The submitted text is word-for-word the rubric's own wording, and it
        # still fails, because the verdict is the model's and nothing here
        # compares strings.
        attempt = attempt_for(advanced_quiz())
        quiz_session, _ = wire(attempt, model_client=grading_stub(graded("incorrect")))

        result = submit(quiz_session, attempt, "b1", RUBRIC)

        assert result.verdict is Verdict.INCORRECT


class TestOneResponseCarriesThreeThings:
    """Master spec acceptance 9, and D13.

    The parallel tutor call is withdrawn. One request, three things, nothing
    streamed — so the count is asserted as *exactly* one rather than "at least
    the grading call".
    """

    def test_exactly_one_call_per_submission_and_it_is_grade_answer(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("correct", tutor_line="Close on the wording."))
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        assert stub.call_count() == 1
        assert stub.call_count(CallType.GRADE_ANSWER) == 1
        for call_type in (
            CallType.AUTHOR_SKELETON,
            CallType.AUTHOR_PEDAGOGY,
            CallType.GRADE_PROBE,
            CallType.FOLD_NARRATIVE,
        ):
            stub.assert_never_called(call_type)

    def test_the_verdict_probe_question_and_tutor_line_arrive_together(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            graded(
                "correct",
                tutor_line="You reached for the macroscopic picture first.",
                probe_question="How did you arrive at that?",
            )
        )
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "entropy climbs")

        assert result.verdict is Verdict.CORRECT
        assert result.model_grading is not None
        assert (
            result.model_grading.tutor_line
            == "You reached for the macroscopic picture first."
        )
        assert result.model_grading.probe_question == "How did you arrive at that?"
        # All three off one response: still one call, after reading all three.
        assert stub.call_count() == 1

    def test_both_riders_are_nullable_and_a_bare_verdict_is_enough(self):
        attempt = attempt_for(advanced_quiz())
        quiz_session, _ = wire(attempt, model_client=grading_stub(graded("correct")))

        result = submit(quiz_session, attempt, "b1", "entropy climbs")

        assert result.model_grading is not None
        assert result.model_grading.tutor_line is None
        assert result.model_grading.probe_question is None

    def test_a_null_rider_is_read_the_same_as_an_absent_one(self):
        attempt = attempt_for(advanced_quiz())
        explicit_nulls = RecordingModelClient(
            {
                CallType.GRADE_ANSWER: [
                    ModelResponse(
                        content=json.dumps(
                            {
                                "verdict": "correct",
                                "tutor_line": None,
                                "probe_question": None,
                            }
                        ),
                        message_id="msg_grade_1",
                    )
                ]
            }
        )
        quiz_session, _ = wire(attempt, model_client=explicit_nulls)

        result = submit(quiz_session, attempt, "b1", "entropy climbs")

        assert result.model_grading is not None
        assert result.model_grading.tutor_line is None

    def test_a_multi_blank_advanced_attempt_costs_one_call_per_submission(self):
        attempt = attempt_for(advanced_quiz(("b1", "b2")))
        stub = grading_stub(graded("incorrect"), graded("correct"), graded("correct"))
        quiz_session, attempts = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "heat goes up")
        submit(quiz_session, attempt, "b1", "disorder rises")
        last = submit(quiz_session, attempt, "b2", "disorder rises")

        assert last.attempt_sealed is True
        assert attempts.get(LEARNER, attempt.attempt_id).outcome is Outcome.RESOLVED
        assert stub.call_count() == 3


class TestAdvancedWalksTheSameLadder:
    def test_three_wrong_answers_walk_the_rungs_and_resolve_the_blank(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("incorrect"))
        quiz_session, attempts = wire(attempt, model_client=stub)

        rungs = [
            submit(quiz_session, attempt, "b1", f"try {n}") for n in range(1, 4)
        ]

        assert [step.hint_rung_shown for step in rungs] == [1, 2, 3]
        assert rungs[-1].blank_resolved is True
        assert attempts.get(LEARNER, attempt.attempt_id).is_sealed
        assert stub.call_count() == 3, "one call per wrong answer, and no more"

    def test_rung_three_reveals_in_prose_and_carries_no_option_id(self):
        # ADR-0016. An Advanced blank has no option id to reveal, and its
        # rubric is the answer key (D4), so the reveal is what the model wrote
        # for rung three — prose, in `feedback` — and `revealed_option_id`
        # stays null on every rung. Rewritten from
        # `test_nothing_is_revealed_on_rung_three`, which locked in the silence
        # #56 reports rather than endorsing it.
        attempt = attempt_for(advanced_quiz())
        quiz_session, _ = wire(
            attempt,
            model_client=grading_stub(
                graded("incorrect", hint="Think about what is conserved."),
                graded("incorrect", hint="It is the quantity that never falls."),
                graded("incorrect", hint="The answer is entropy: it never falls."),
            ),
        )

        rungs = [
            submit(quiz_session, attempt, "b1", f"try {n}") for n in range(1, 4)
        ]

        assert [step.revealed_option_id for step in rungs] == [None, None, None]
        assert rungs[-1].hint_rung_shown == 3
        assert rungs[-1].feedback == "The answer is entropy: it never falls."
        assert rungs[-1].blank_resolved is True, "rung three still closes it"

    def test_an_advanced_hint_is_authored_on_the_grading_response(self):
        # #56 / ADR-0016. The rung's text rides the response that carried the
        # verdict, reaching the learner through `feedback` — the same field the
        # Novice ladder fills from `blank.hints`. Rewritten from
        # `test_an_advanced_blank_has_no_pre_authored_hint_text`, which
        # asserted `feedback is None` on every rung.
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            graded(
                "incorrect",
                tutor_line="Heat is not disorder.",
                hint="Ask what the second law puts a floor under.",
            )
        )
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "heat goes up")

        assert result.feedback == "Ask what the second law puts a floor under."
        assert result.hint_rung_shown == 1
        assert result.model_grading is not None
        assert result.model_grading.hint == (
            "Ask what the second law puts a floor under."
        )
        assert result.model_grading.tutor_line == "Heat is not disorder."
        assert stub.call_count() == 1, "the hint costs no second call"

    def test_each_rung_carries_the_hint_written_for_that_rung(self):
        attempt = attempt_for(advanced_quiz())
        quiz_session, _ = wire(
            attempt,
            model_client=grading_stub(
                graded("incorrect", hint="rung one"),
                graded("incorrect", hint="rung two"),
                graded("incorrect", hint="rung three"),
            ),
        )

        rungs = [
            submit(quiz_session, attempt, "b1", f"try {n}") for n in range(1, 4)
        ]

        assert [step.hint_rung_shown for step in rungs] == [1, 2, 3]
        assert [step.feedback for step in rungs] == [
            "rung one",
            "rung two",
            "rung three",
        ]

    def test_a_correct_advanced_answer_carries_no_hint(self):
        attempt = attempt_for(advanced_quiz())
        quiz_session, _ = wire(attempt, model_client=grading_stub(graded("correct")))

        result = submit(quiz_session, attempt, "b1", "entropy climbs")

        assert result.hint_rung_shown is None
        assert result.feedback is None

    def test_the_client_states_the_rung_it_selected_in_the_request(self):
        # ADR-0016: the client still chooses the rung and passes it in. It is
        # `min(prior_wrong + 1, 3)`, knowable before the call, which is what
        # keeps the hint on the one response already in flight.
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(*(graded("incorrect", hint="h") for _ in range(3)))
        quiz_session, _ = wire(attempt, model_client=stub)

        for n in range(1, 4):
            submit(quiz_session, attempt, "b1", f"try {n}")

        tails = [call.segments.volatile_tail.text for call in stub.calls]
        assert "Hint rung if wrong: 1 of 3" in tails[0]
        assert "Hint rung if wrong: 2 of 3" in tails[1]
        assert "Hint rung if wrong: 3 of 3" in tails[2]

    def test_a_hint_that_reproduces_the_rubric_is_dropped(self):
        # D4 / ADR-0003 is a structural claim, so it may not rest on the model
        # obeying an instruction. A hint carrying the rubric verbatim fails
        # closed to no hint at all (ADR-0016).
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            graded("incorrect", hint=f"The rubric says: {RUBRIC} (b1)")
        )
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "heat goes up")

        assert result.feedback is None
        assert RUBRIC not in repr(dataclasses.astuple(result))

    def test_a_resolved_advanced_blank_refuses_a_further_submission(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("correct"))
        quiz_session, _ = wire(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "entropy climbs")

        with pytest.raises(BlankAlreadyResolved):
            submit(quiz_session, attempt, "b1", "second thoughts")
        assert stub.call_count() == 1, "a refused submission consults no model"


class TestTheRequestIsTheCacheAnchoredBlock:
    """D1/ADR-0001: frozen prefix with breakpoints, then the volatile tail."""

    def test_the_request_is_assembled_for_grade_answer_with_both_breakpoints(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub()
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        (call,) = stub.calls
        assert call.call_type is CallType.GRADE_ANSWER
        assert call.segments.call_type is CallType.GRADE_ANSWER
        assert [segment.role for segment in call.segments.segments] == [
            SegmentRole.SEGMENT_1,
            SegmentRole.SEGMENT_2,
            SegmentRole.VOLATILE_TAIL,
        ]
        assert call.segments.breakpoints() == (0, 1)
        assert call.segments.volatile_tail.cache_control is False

    def test_segment_2_carries_the_blank_rubric(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub()
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        assert RUBRIC in stub.calls[0].segments.segment_2.text

    def test_the_tail_carries_every_guess_so_far_in_order_then_the_current_one(self):
        attempt = attempt_for(advanced_quiz(("b1", "b2")))
        stub = grading_stub(graded("incorrect"), graded("correct"), graded("correct"))
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "first swing")
        submit(quiz_session, attempt, "b1", "second swing")
        submit(quiz_session, attempt, "b2", "third swing")

        tail = stub.calls[-1].segments.volatile_tail.text
        assert (
            tail.index("first swing")
            < tail.index("second swing")
            < tail.index("third swing")
        )
        assert tail.index("## Under consideration") < tail.index("third swing")
        assert "Blank: b2" in tail

    def test_the_first_submission_carries_an_empty_guess_log(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub()
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        assert "(none yet)" in stub.calls[0].segments.volatile_tail.text

    def test_the_grading_request_carries_no_inquiry(self):
        # The inquiry is the authoring call's field; a grading tail that
        # reprinted it would be per-request text nobody asked for.
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub()
        quiz_session, _ = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        assert "## The learner's inquiry" not in (
            stub.calls[0].segments.volatile_tail.text
        )


class TestTheAnswerKeyStaysInTheBackend:
    """D4/ADR-0003. The rubric goes into the request and comes out of nothing."""

    def test_the_rubric_never_appears_in_what_submit_returns(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("incorrect"))
        quiz_session, _ = wire(attempt, model_client=stub)

        results = [
            submit(quiz_session, attempt, "b1", f"try {n}") for n in range(1, 4)
        ]

        assert RUBRIC in stub.calls[0].segments.segment_2.text
        for result in results:
            rendered = repr(dataclasses.astuple(result))
            assert RUBRIC not in rendered
            assert "ANSWER-KEY" not in rendered


class TestTheModelCallIsStampedOnTheAttempt:
    def test_the_message_id_and_token_usage_persist(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            graded(
                "correct",
                message_id="msg_grade_ada_1",
                input_tokens=910,
                output_tokens=37,
                cache_read_input_tokens=612,
                cache_creation_input_tokens=4,
            )
        )
        quiz_session, attempts = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "entropy climbs")

        (call,) = attempts.get(LEARNER, attempt.attempt_id).model_calls
        assert call.call_type == CallType.GRADE_ANSWER.value
        assert call.message_id == "msg_grade_ada_1"
        assert call.usage.input_tokens == 910
        assert call.usage.output_tokens == 37
        assert call.usage.cache_read_input_tokens == 612
        assert call.usage.cache_creation_input_tokens == 4

    def test_every_submission_stamps_its_own_call(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            graded("incorrect", message_id="msg_a"),
            graded("correct", message_id="msg_b"),
        )
        quiz_session, attempts = wire(attempt, model_client=stub)

        submit(quiz_session, attempt, "b1", "first swing")
        submit(quiz_session, attempt, "b1", "second swing")

        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert [call.message_id for call in stored.model_calls] == ["msg_a", "msg_b"]

    def test_the_deterministic_path_stamps_no_call(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, attempts = wire(attempt)

        submit(quiz_session, attempt, "b1", "b1-o1")

        assert attempts.get(LEARNER, attempt.attempt_id).model_calls == ()


class TestAMalformedGradingResponseIsNotSilentlyGraded:
    """`GradingParseError` so "the model returned nonsense" is distinguishable
    from "we built the wrong object" — the same reasoning as
    `AuthoringParseError`."""

    @pytest.mark.parametrize(
        "content",
        [
            "not json at all",
            "[]",
            "null",
            json.dumps({"tutor_line": "no verdict here"}),
            json.dumps({"verdict": "maybe"}),
            json.dumps({"verdict": 3}),
            json.dumps({"verdict": "correct", "tutor_line": 7}),
        ],
    )
    def test_an_unreadable_response_raises(self, content):
        attempt = attempt_for(advanced_quiz())
        stub = RecordingModelClient(
            {CallType.GRADE_ANSWER: [ModelResponse(content=content, message_id="m")]}
        )
        quiz_session, attempts = wire(attempt, model_client=stub)

        with pytest.raises(GradingParseError):
            submit(quiz_session, attempt, "b1", "entropy climbs")

        assert attempts.get(LEARNER, attempt.attempt_id).guesses == ()

    def test_there_is_no_fallback_to_string_comparison(self):
        # An Advanced answer graded by string equality would look like a
        # working feature, which is the one failure mode worth raising over.
        assert issubclass(GradingParseError, ValueError)


def _blank_payload(blank_id: str) -> dict:
    return {
        "blank_id": blank_id,
        "options": [
            {"option_id": "o1", "text": "entropy"},
            {"option_id": "o2", "text": "enthalpy"},
        ],
        "correct_option_id": "o1" if blank_id == "b1" else "o2",
        "reinforcement": "That is the one the second law bounds.",
        "hints": ["Think about disorder.", "The second law bounds it.", "Entropy."],
        "rubric": None,
    }


_SKELETON = json.dumps(
    {
        "type": "quiz",
        "topic": "the second law of thermodynamics",
        "explanation": [
            {"type": "text", "text": "Heat flows one way because "},
            {"type": "blank", "blank_id": "b1"},
            {"type": "text", "text": " rises, and "},
            {"type": "blank", "blank_id": "b2"},
            {"type": "text", "text": " does not."},
        ],
        "blanks": [_blank_payload("b1"), _blank_payload("b2")],
        "recap": "Entropy never decreases in an isolated system.",
        "queued_topics": [],
    }
)


# --- Probes (#10) ------------------------------------------------------------
#
# Master spec acceptance 8 and 13-21. The invariant that shapes every test
# below: **asking a probe costs no model call and answering one costs exactly
# one**, so nearly every test here is also a `call_count()` assertion.


PROBE_QUESTION = "How did you arrive at that?"


def probed(
    verdict: str = "correct",
    *,
    correction: str | None = None,
    message_id: str = "msg_probe_1",
    **counters: int,
) -> ModelResponse:
    """One `grade_probe` response: the verdict and a nullable correction."""
    payload: dict = {"verdict": verdict}
    if correction is not None:
        payload["correction"] = correction
    return ModelResponse(
        content=json.dumps(payload), message_id=message_id, **counters
    )


def pending_probe(blank_id: str = "b1", **overrides) -> Probe:
    fields = dict(
        blank_id=blank_id,
        question=PROBE_QUESTION,
        self_explanation=None,
        verdict=None,
        reopened_blank=False,
        cadence_at_fire=ProbeCadence.ALWAYS,
        asked_at=FROZEN_AT,
        answered_at=None,
        message_id=None,
    )
    fields.update(overrides)
    return Probe(**fields)


def probing_session(
    attempt: QuizAttempt,
    *,
    model_client: RecordingModelClient | None = None,
    seed: int = 0,
):
    """A session with a seeded RNG, so the coin flip is reproducible."""
    attempts = InMemoryAttemptRepository()
    attempts.save(attempt)
    quiz_session = QuizSession(
        model_client=model_client or RecordingModelClient(fail_if_called=True),
        attempts=attempts,
        clock=frozen_clock,
        rng=random.Random(seed),
    )
    return quiz_session, attempts


def answer_probe(quiz_session, attempt, blank_id, self_explanation):
    return quiz_session.answer_probe(
        learner_id=LEARNER,
        attempt_id=attempt.attempt_id,
        blank_id=blank_id,
        self_explanation=self_explanation,
    )


class TestAskingAProbeCostsNoModelCall:
    """The half of the call budget that does not move (ADR-0011, ADR-0013).

    Novice asks from `Blank.probe_question`, pre-authored in the pedagogy
    payload; Advanced asks from `ModelGrading.probe_question`, which rode the
    one `grade_answer` call the submission was already making. Neither route
    adds a request.
    """

    def test_a_novice_probe_fires_against_a_stub_that_raises_on_contact(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        never = RecordingModelClient(fail_if_called=True)
        quiz_session, attempts = probing_session(attempt, model_client=never)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.probe_asked is not None
        assert result.probe_asked.question == PROBE_QUESTION
        never.assert_never_called()
        (probe,) = attempts.get(LEARNER, attempt.attempt_id).probes
        assert probe.is_answered is False

    def test_an_advanced_probe_adds_nothing_to_the_one_grading_call(self):
        attempt = attempt_for(advanced_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = grading_stub(graded("correct", probe_question=PROBE_QUESTION))
        quiz_session, _ = probing_session(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "the disorder term")

        assert result.probe_asked is not None
        assert result.probe_asked.question == PROBE_QUESTION
        assert stub.call_count() == 1
        assert stub.calls[0].call_type is CallType.GRADE_ANSWER

    def test_no_probe_question_means_no_probe(self):
        # The #9 window: the skeleton has landed and the pedagogy has not, so
        # there is no question to ask. It costs the learner the probe, not the
        # verdict.
        attempt = attempt_for(
            novice_quiz(pedagogy=False), probe_cadence=ProbeCadence.ALWAYS
        )
        quiz_session, attempts = probing_session(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.probe_asked is None
        assert attempts.get(LEARNER, attempt.attempt_id).probes == ()
        assert result.attempt_sealed is True

    def test_a_wrong_answer_never_probes(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        quiz_session, attempts = probing_session(attempt)

        assert submit(quiz_session, attempt, "b1", "b1-o2").probe_asked is None
        assert attempts.get(LEARNER, attempt.attempt_id).probes == ()


class TestAnsweringAProbeCostsExactlyOneCall:
    """Master spec acceptance 8, asserted in **both** modes.

    This is the whole reason `answer_probe` is a separate method from `submit`:
    a Novice answer is free and a Novice probe reply is not, and one method
    could not carry both claims.
    """

    def test_a_novice_probe_reply_is_one_grade_probe_call(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient({CallType.GRADE_PROBE: [probed("correct")]})
        quiz_session, _ = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")
        assert stub.call_count() == 0, "asking is free"

        answer = answer_probe(
            quiz_session, attempt, "b1", "Disorder increases, so entropy it is."
        )

        assert answer.verdict is Verdict.CORRECT
        assert stub.call_count() == 1
        assert stub.calls[0].call_type is CallType.GRADE_PROBE

    def test_an_advanced_probe_reply_is_one_grade_probe_call(self):
        attempt = attempt_for(advanced_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {
                CallType.GRADE_ANSWER: [
                    graded("correct", probe_question=PROBE_QUESTION)
                ],
                CallType.GRADE_PROBE: [probed("correct")],
            }
        )
        quiz_session, _ = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "the disorder term")

        answer_probe(
            quiz_session, attempt, "b1", "The second law bounds it, nothing else."
        )

        assert stub.call_count() == 2
        assert [call.call_type for call in stub.calls] == [
            CallType.GRADE_ANSWER,
            CallType.GRADE_PROBE,
        ]

    def test_the_reply_and_the_question_reach_the_volatile_tail(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient({CallType.GRADE_PROBE: [probed("correct")]})
        quiz_session, _ = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")

        answer_probe(quiz_session, attempt, "b1", "Disorder increases.")

        (call,) = stub.calls
        assert call.segments.call_type is CallType.GRADE_PROBE
        tail = call.segments.volatile_tail
        assert "Disorder increases." in tail.text
        assert PROBE_QUESTION in tail.text
        assert tail.cache_control is False
        assert call.segments.breakpoints() == (0, 1)

    def test_the_probe_call_is_stamped_on_the_attempt(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {
                CallType.GRADE_PROBE: [
                    probed(
                        "correct",
                        message_id="msg_01probe",
                        input_tokens=800,
                        output_tokens=90,
                        cache_read_input_tokens=512,
                    )
                ]
            }
        )
        quiz_session, attempts = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")

        answer_probe(quiz_session, attempt, "b1", "Disorder increases.")

        (call,) = attempts.get(LEARNER, attempt.attempt_id).model_calls
        assert call.call_type == CallType.GRADE_PROBE.value
        assert call.message_id == "msg_01probe"
        assert call.usage.cache_read_input_tokens == 512

    def test_the_answered_probe_replaces_the_pending_one(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {CallType.GRADE_PROBE: [probed("correct", message_id="msg_01probe")]}
        )
        quiz_session, attempts = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")

        answer_probe(quiz_session, attempt, "b1", "Disorder increases.")

        (probe,) = attempts.get(LEARNER, attempt.attempt_id).probes
        assert probe.self_explanation == "Disorder increases."
        assert probe.verdict is Verdict.CORRECT
        assert probe.answered_at == FROZEN_AT
        assert probe.message_id == "msg_01probe"

    def test_answering_a_probe_that_is_not_pending_is_a_wiring_error(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = probing_session(attempt)

        with pytest.raises(session_module.NoPendingProbe):
            answer_probe(quiz_session, attempt, "b1", "Disorder increases.")

    @pytest.mark.parametrize(
        "content", ["not json", '{"verdict": "maybe"}', '{"correction": "x"}']
    )
    def test_an_unreadable_probe_response_raises(self, content):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {
                CallType.GRADE_PROBE: [
                    ModelResponse(content=content, message_id="msg_bad")
                ]
            }
        )
        quiz_session, _ = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")

        with pytest.raises(GradingParseError):
            answer_probe(quiz_session, attempt, "b1", "Disorder increases.")


class TestWhatAFailedProbeDoesComesFromTheRegistry:
    """D9 / ADR-0009. `probe_failure_behavior` is read, never branched on."""

    def _advanced(self, *probe_responses, grading=None):
        attempt = attempt_for(advanced_quiz(("b1",)), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {
                CallType.GRADE_ANSWER: list(grading)
                if grading
                else [
                    graded("incorrect"),
                    graded("correct", probe_question=PROBE_QUESTION),
                ],
                CallType.GRADE_PROBE: list(probe_responses),
            }
        )
        quiz_session, attempts = probing_session(attempt, model_client=stub)
        return quiz_session, attempts, attempt

    def test_a_failed_advanced_probe_reopens_the_blank(self):
        # Acceptance 13, first half.
        quiz_session, attempts, attempt = self._advanced(probed("incorrect"))
        submit(quiz_session, attempt, "b1", "heat")
        submit(quiz_session, attempt, "b1", "the disorder term")

        answer = answer_probe(quiz_session, attempt, "b1", "It just looked right.")

        assert answer.verdict is Verdict.INCORRECT
        assert answer.blank_reopened is True
        assert answer.blank_resolved is False
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert session_module.is_blank_resolved(stored, "b1") is False
        assert stored.probes[0].reopened_blank is True

    def test_the_ladder_resumes_at_the_next_rung_not_at_rung_one(self):
        # Acceptance 13, the half that matters. The learner had reached rung 1
        # before answering correctly, so the re-opened blank shows rung **2** —
        # a failed probe is evidence they needed more help, not less.
        quiz_session, _, attempt = self._advanced(
            probed("incorrect"),
            grading=[
                graded("incorrect"),
                graded("correct", probe_question=PROBE_QUESTION),
                graded("incorrect"),
            ],
        )
        first = submit(quiz_session, attempt, "b1", "heat")
        assert first.hint_rung_shown == 1
        submit(quiz_session, attempt, "b1", "the disorder term")
        answer_probe(quiz_session, attempt, "b1", "It just looked right.")

        again = submit(quiz_session, attempt, "b1", "heat again")

        assert again.hint_rung_shown == 2

    def test_a_failed_novice_probe_leaves_the_blank_resolved(self):
        # Acceptance 14. Re-opening a two-option bank whose answer the learner
        # was just told is degenerate, so Novice corrects and moves on.
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {CallType.GRADE_PROBE: [probed("incorrect", correction="Not quite.")]}
        )
        quiz_session, _ = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "b1-o1")

        answer = answer_probe(quiz_session, attempt, "b1", "I guessed.")

        assert answer.verdict is Verdict.INCORRECT
        assert answer.correction == "Not quite."
        assert answer.blank_reopened is False
        assert answer.blank_resolved is True
        assert answer.attempt_sealed is True

    def test_a_blank_reopens_at_most_once(self):
        # Acceptance 15: the second failed probe reveals and moves on.
        quiz_session, attempts, attempt = self._advanced(
            probed("incorrect"), probed("incorrect")
        )
        submit(quiz_session, attempt, "b1", "heat")
        submit(quiz_session, attempt, "b1", "the disorder term")
        answer_probe(quiz_session, attempt, "b1", "It just looked right.")
        submit(quiz_session, attempt, "b1", "the disorder term")

        second = answer_probe(quiz_session, attempt, "b1", "Still guessing.")

        assert second.blank_reopened is False
        assert second.blank_resolved is True
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert [probe.reopened_blank for probe in stored.probes] == [True, False]
        assert session_module.is_blank_resolved(stored, "b1") is True

    def test_the_behaviour_comes_from_the_policy_and_not_from_the_mode(self):
        # The proof that the registry is what is read: a Novice attempt whose
        # registered policy says REOPEN_BLANK re-opens, mode notwithstanding.
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        reopening = ModeRegistry(
            {
                DifficultyMode.NOVICE: dataclasses.replace(
                    NOVICE_POLICY,
                    probe_failure_behavior=ProbeFailureBehavior.REOPEN_BLANK,
                )
            }
        )
        attempts = InMemoryAttemptRepository()
        attempts.save(attempt)
        quiz_session = QuizSession(
            model_client=RecordingModelClient(
                {CallType.GRADE_PROBE: [probed("incorrect")]}
            ),
            attempts=attempts,
            registry=reopening,
            clock=frozen_clock,
            rng=random.Random(0),
        )
        submit(quiz_session, attempt, "b1", "b1-o1")

        answer = answer_probe(quiz_session, attempt, "b1", "I guessed.")

        assert answer.blank_reopened is True


class TestCadenceIsClientOwnedAndReproducible:
    """Acceptance 17, 19 and 21. Asking is free, so the whole class runs
    against a stub that raises the moment it is consulted."""

    FIVE = ("b1", "b2", "b3", "b4", "b5")

    def _walk(self, cadence: ProbeCadence, *, seed: int):
        """Answer all five blanks correctly; report which ones were probed."""
        attempt = attempt_for(novice_quiz(self.FIVE), probe_cadence=cadence)
        quiz_session, attempts = probing_session(attempt, seed=seed)
        for blank_id in self.FIVE:
            submit(quiz_session, attempt, blank_id, f"{blank_id}-o1")
        stored = attempts.get(LEARNER, attempt.attempt_id)
        return tuple(probe.blank_id for probe in stored.probes)

    def test_the_same_seed_produces_the_same_sequence(self):
        assert self._walk(ProbeCadence.SOMETIMES, seed=7) == self._walk(
            ProbeCadence.SOMETIMES, seed=7
        )

    def test_different_seeds_do_produce_different_sequences(self):
        # Otherwise the reproducibility assertion above is vacuously true.
        sequences = {
            self._walk(ProbeCadence.SOMETIMES, seed=seed) for seed in range(12)
        }
        assert len(sequences) > 1

    def test_the_final_blank_is_probed_whatever_the_seed(self):
        # Acceptance 17's second half. `sometimes` short-circuits on the final
        # blank without drawing at all, so the guarantee is structural rather
        # than a property of the seeds that happen to be tried here.
        for seed in range(40):
            assert "b5" in self._walk(
                ProbeCadence.SOMETIMES, seed=seed
            ), f"seed {seed} skipped the final blank"

    def test_always_probes_every_correct_answer(self):
        assert self._walk(ProbeCadence.ALWAYS, seed=3) == self.FIVE

    def test_final_blank_only_probes_the_last_blank_and_no_other(self):
        # Acceptance 19.
        for seed in range(10):
            assert self._walk(ProbeCadence.FINAL_BLANK_ONLY, seed=seed) == ("b5",)

    def test_off_fires_nothing(self):
        for seed in range(10):
            assert self._walk(ProbeCadence.OFF, seed=seed) == ()

    def test_an_attempt_authored_with_probes_off_is_readable_as_suppressed(self):
        # Acceptance 21: from the record alone, with zero probes present.
        attempt = attempt_for(
            novice_quiz(("b1", "b2")), probe_cadence=ProbeCadence.OFF
        )
        quiz_session, attempts = probing_session(attempt, seed=1)
        submit(quiz_session, attempt, "b1", "b1-o1")
        submit(quiz_session, attempt, "b2", "b2-o1")

        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert stored.probe_cadence_at_authoring is ProbeCadence.OFF
        assert stored.probes == ()
        assert stored.outcome is Outcome.RESOLVED

    def test_the_cadence_at_fire_is_stamped_on_each_probe(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        quiz_session, attempts = probing_session(attempt)
        submit(quiz_session, attempt, "b1", "b1-o1")

        (probe,) = attempts.get(LEARNER, attempt.attempt_id).probes
        assert probe.cadence_at_fire is ProbeCadence.ALWAYS


class TestChangingTheCadenceMidQuiz:
    """Acceptance 20 / ADR-0010: it applies from the next correct answer, and a
    probe already on screen stands."""

    def test_it_takes_effect_on_the_next_correct_answer(self):
        attempt = attempt_for(
            novice_quiz(("b1", "b2")), probe_cadence=ProbeCadence.OFF
        )
        quiz_session, _ = probing_session(attempt)

        first = quiz_session.submit(
            learner_id=LEARNER,
            attempt_id=attempt.attempt_id,
            blank_id="b1",
            submitted="b1-o1",
            probe_cadence=ProbeCadence.OFF,
        )
        second = quiz_session.submit(
            learner_id=LEARNER,
            attempt_id=attempt.attempt_id,
            blank_id="b2",
            submitted="b2-o1",
            probe_cadence=ProbeCadence.ALWAYS,
        )

        assert first.probe_asked is None
        assert second.probe_asked is not None
        assert second.probe_asked.cadence_at_fire is ProbeCadence.ALWAYS

    def test_a_probe_already_pending_is_unaffected(self):
        attempt = attempt_for(
            novice_quiz(("b1", "b2")), probe_cadence=ProbeCadence.ALWAYS
        )
        quiz_session, attempts = probing_session(attempt)
        first = quiz_session.submit(
            learner_id=LEARNER,
            attempt_id=attempt.attempt_id,
            blank_id="b1",
            submitted="b1-o1",
            probe_cadence=ProbeCadence.ALWAYS,
        )
        assert first.probe_asked is not None

        second = quiz_session.submit(
            learner_id=LEARNER,
            attempt_id=attempt.attempt_id,
            blank_id="b2",
            submitted="b2-o1",
            probe_cadence=ProbeCadence.OFF,
        )

        assert second.probe_asked is None
        assert second.attempt_sealed is False, "b1's probe still stands"
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert len(stored.probes) == 1
        assert stored.probes[0].is_answered is False
        assert stored.probes[0].is_dismissed is False


class TestOneSealPredicateServesEveryProbeCase:
    """Acceptance 16 and 18. Probes on, off and dismissed — every case below
    goes through `is_sealable`, and none of them adds a second path."""

    def test_a_pending_probe_holds_the_attempt_open_and_answering_seals_it(self):
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient({CallType.GRADE_PROBE: [probed("correct")]})
        quiz_session, attempts = probing_session(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "b1-o1")
        assert result.blank_resolved is True
        assert result.attempt_sealed is False

        answer = answer_probe(quiz_session, attempt, "b1", "Disorder increases.")

        assert answer.attempt_sealed is True
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert stored.outcome is Outcome.RESOLVED
        assert stored.sealed_at == FROZEN_AT

    def test_a_dismissed_probe_does_not_block_sealing(self):
        # Acceptance 18, and the reason `dismissed_at` had to exist: without it
        # this attempt and the one above are the same record.
        attempt = attempt_for(novice_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        never = RecordingModelClient(fail_if_called=True)
        quiz_session, attempts = probing_session(attempt, model_client=never)
        submit(quiz_session, attempt, "b1", "b1-o1")

        dismissal = quiz_session.dismiss_probe(
            learner_id=LEARNER, attempt_id=attempt.attempt_id, blank_id="b1"
        )

        assert dismissal.attempt_sealed is True
        never.assert_never_called()
        stored = attempts.get(LEARNER, attempt.attempt_id)
        (probe,) = stored.probes
        assert probe.self_explanation is None
        assert probe.dismissed_at == FROZEN_AT
        assert probe.is_answered is False
        assert stored.outcome is Outcome.RESOLVED

    def test_the_predicate_reads_dismissal_rather_than_a_second_path(self):
        quiz = novice_quiz()
        answered = attempt_for(quiz).with_guess(
            Guess(
                blank_id="b1",
                submitted="b1-o1",
                verdict=Verdict.CORRECT,
                attempt_ordinal=1,
                hint_rung_shown=None,
                created_at=FROZEN_AT,
                graded_by=GradingStrategy.DETERMINISTIC,
            )
        )
        pending = answered.with_probe(pending_probe())
        dismissed = answered.with_probe(
            dataclasses.replace(pending_probe(), dismissed_at=FROZEN_AT)
        )

        assert session_module.has_pending_probe(pending) is True
        assert session_module.has_pending_probe(dismissed) is False
        assert session_module.is_sealable(pending) is False
        assert session_module.is_sealable(dismissed) is True

    def test_a_failed_advanced_probe_un_fires_completion(self):
        # The reason the predicate is never "the last blank resolved": this
        # attempt was complete a moment ago and is not any more.
        attempt = attempt_for(advanced_quiz(), probe_cadence=ProbeCadence.ALWAYS)
        stub = RecordingModelClient(
            {
                CallType.GRADE_ANSWER: [
                    graded("correct", probe_question=PROBE_QUESTION)
                ],
                CallType.GRADE_PROBE: [probed("incorrect")],
            }
        )
        quiz_session, attempts = probing_session(attempt, model_client=stub)
        submit(quiz_session, attempt, "b1", "the disorder term")

        answer = answer_probe(quiz_session, attempt, "b1", "It looked right.")

        assert answer.attempt_sealed is False
        stored = attempts.get(LEARNER, attempt.attempt_id)
        assert stored.outcome is Outcome.IN_FLIGHT
        assert session_module.is_sealable(stored) is False

    def test_dismissing_a_probe_that_is_not_pending_is_a_wiring_error(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = probing_session(attempt)

        with pytest.raises(session_module.NoPendingProbe):
            quiz_session.dismiss_probe(
                learner_id=LEARNER, attempt_id=attempt.attempt_id, blank_id="b1"
            )


class TestTheShortFormRevealIsGuardedOnItsOwnTerms:
    """[ADR-0019](../docs/adr/0019-resolved-blank-text-comes-from-the-service.md).

    `revealed_answer` approaches the answer key by design — naming the answer
    is the whole job of the field — so it cannot wear `_safe_hint`, which drops
    anything carrying the rubric. Same fail-closed posture, different
    threshold: a phrase that names the answer is the point, a body of text that
    hands over the grading criteria is the leak.
    """

    def test_a_phrase_that_names_the_answer_survives(self):
        """The case `_safe_hint` would have destroyed. A rubric short enough to
        be quoted inside a legitimate reveal is a known false positive there,
        and here it would fire on the field's intended use."""
        blank = advanced_blank()

        assert session_module._safe_revealed_answer(blank, "entropy") == "entropy"

    def test_a_reveal_reproducing_the_rubric_is_dropped(self):
        """Custody is structural, not advisory (D4, ADR-0003). The rubric
        fixture is well under the length cap, so this is the check earning its
        keep rather than the cap catching it by accident."""
        blank = advanced_blank()

        revealed = session_module._safe_revealed_answer(
            blank, f"The answer: {RUBRIC} (b1)"
        )

        assert revealed is None

    def test_whitespace_does_not_launder_the_rubric(self):
        """A rubric rewrapped is the same disclosure, exactly as in
        `_safe_hint`."""
        blank = advanced_blank()
        rewrapped = f"{RUBRIC} (b1)".replace(" ", "\n  ")

        assert session_module._safe_revealed_answer(blank, rewrapped) is None

    def test_a_reveal_longer_than_a_phrase_is_dropped(self):
        """A gap is a noun-phrase-shaped hole. Anything paragraph-sized is not
        a reveal, whatever it contains — the cap is what makes this guard
        narrower than `_safe_hint` rather than merely different."""
        blank = advanced_blank()
        essay = "the quantity that never decreases, " * 20

        assert session_module._safe_revealed_answer(blank, essay) is None

    def test_a_blank_with_no_rubric_is_left_alone(self):
        assert (
            session_module._safe_revealed_answer(novice_blank(), "entropy") == "entropy"
        )

    def test_nothing_revealed_stays_nothing(self):
        assert session_module._safe_revealed_answer(advanced_blank(), None) is None


class TestWhatFillsTheGapWhenABlankResolves:
    """[ADR-0019](../docs/adr/0019-resolved-blank-text-comes-from-the-service.md)
    and [#127](https://github.com/derkmed/socratic/issues/127).

    The resolved text is the service's to state. Before this the client
    reconstructed it by scanning the rendered document for an option id — which
    are blank-scoped, so it usually found another blank's option — and on the
    Advanced path fell back to the learner's own wrong answer.
    """

    def test_a_correct_click_resolves_to_the_option_text_not_its_id(self):
        """The id is `b1-o1`; the gap gets the words."""
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o1")

        assert result.resolved_answer == ResolvedAnswer("entropy")

    def test_a_novice_rung_three_resolves_to_the_revealed_option_text(self):
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        for _ in range(3):
            result = submit(quiz_session, attempt, "b1", "b1-o2")

        assert result.blank_resolved
        assert result.resolved_answer == ResolvedAnswer("entropy")

    def test_an_unresolved_blank_has_nothing_to_put_in_the_gap(self):
        """Rungs one and two leave the blank open, so there is no gap to fill
        and no reveal to make."""
        attempt = attempt_for(novice_quiz())
        quiz_session, _ = wire(attempt)

        result = submit(quiz_session, attempt, "b1", "b1-o2")

        assert not result.blank_resolved
        assert result.resolved_answer is None

    def test_a_correct_advanced_answer_resolves_to_the_learners_own_words(self):
        """Their sentence now. Canonising it would silently rewrite what they
        wrote, and would need a field nobody added."""
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(graded("correct"))
        quiz_session, _ = wire(attempt, model_client=stub)

        result = submit(quiz_session, attempt, "b1", "entropy climbs")

        assert result.resolved_answer == ResolvedAnswer(
            "entropy climbs", learner_authored=True
        ), "the learner's own words must be marked as theirs, not rendered as markup"

    def test_an_advanced_rung_three_resolves_to_the_short_form_reveal(self):
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            *(
                graded("incorrect", hint="h", revealed_answer="entropy")
                for _ in range(3)
            )
        )
        quiz_session, _ = wire(attempt, model_client=stub)

        for _ in range(3):
            result = submit(quiz_session, attempt, "b1", "heat")

        assert result.blank_resolved
        assert result.resolved_answer == ResolvedAnswer("entropy")
        assert not result.resolved_answer.learner_authored

    def test_an_advanced_rung_three_never_resolves_to_the_wrong_answer(self):
        """The defect itself. The model omitted `revealed_answer`, the blank
        closes anyway, and what must NOT appear in the gap is `heat` — the
        learner's third wrong guess, which is what shipped before #127."""
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(*(graded("incorrect", hint="h") for _ in range(3)))
        quiz_session, _ = wire(attempt, model_client=stub)

        for _ in range(3):
            result = submit(quiz_session, attempt, "b1", "heat")

        assert result.blank_resolved
        assert result.resolved_answer is None

    def test_a_reveal_that_leaks_the_rubric_leaves_the_gap_empty(self):
        """Fails closed all the way to the caller: the guard drops it and
        nothing downstream substitutes the guess (D4, ADR-0003)."""
        attempt = attempt_for(advanced_quiz())
        stub = grading_stub(
            *(
                graded("incorrect", hint="h", revealed_answer=f"{RUBRIC} (b1)")
                for _ in range(3)
            )
        )
        quiz_session, _ = wire(attempt, model_client=stub)

        for _ in range(3):
            result = submit(quiz_session, attempt, "b1", "heat")

        assert result.resolved_answer is None
        assert RUBRIC not in repr(dataclasses.astuple(result))


class TestTheProbePathHasNothingToPutInTheGap:
    """The #131 review's second blocking finding.

    An earlier cut gave `ProbeAnswer` a `resolved_answer`. It could never carry
    one: the reveal branch needs `REOPEN_BLANK`, which only the Advanced policy
    has, and an Advanced blank is validated to carry no `correct_option_id`. The
    tests that "covered" it built a Novice policy mutated to `REOPEN_BLANK` — a
    mode the validators forbid — so they were green against a shape the service
    cannot emit, while the reachable shape had no test at all.

    That is not a hole to fill. A probe only fires after a correct answer, so
    the gap already holds it; this path closes a blank without changing what
    belongs in it.
    """

    def _answer(self):
        return session_module.ProbeAnswer(
            verdict=Verdict.CORRECT,
            correction=None,
            blank_reopened=False,
            blank_resolved=True,
            revealed_option_id=None,
            attempt_sealed=False,
        )

    def test_the_probe_answer_carries_no_resolved_text(self):
        assert not hasattr(self._answer(), "resolved_answer")

    def test_only_advanced_re_opens_and_advanced_has_no_option_to_reveal(self):
        """Why the field had nothing to carry, pinned in both halves so a
        future policy pairing `REOPEN_BLANK` with an option bank fails here
        rather than silently reviving a dead branch."""
        reopening = {
            mode
            for mode, policy in (
                (DifficultyMode.NOVICE, NOVICE_POLICY),
                (DifficultyMode.ADVANCED, ADVANCED_POLICY),
            )
            if policy.probe_failure_behavior is ProbeFailureBehavior.REOPEN_BLANK
        }
        assert reopening == {DifficultyMode.ADVANCED}

        errors = ADVANCED_POLICY.validate_blank(
            Blank(
                blank_id="b1",
                mode=DifficultyMode.ADVANCED,
                rubric="a rubric",
                correct_option_id="b1-o1",
            )
        )
        assert any("correct_option_id" in error for error in errors)
