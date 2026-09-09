"""Novice submission (spec `docs/specs/novice-submit.md`, issue #6).

The hot path: a learner clicks an option and gets a verdict back with **zero
model calls**. Everything here runs against `RecordingModelClient` and
`InMemoryAttemptRepository` — those *are* the doubles (master spec seam table),
so there is no fake in this file and no test reaches the network.

Two call-budget assertions, deliberately not collapsed into one:

* `TestZeroModelCalls` is master spec acceptance 6 — one submission against a
  stub configured to fail the moment it is invoked.
* `TestAFullNoviceQuizCostsOneCall` is acceptance 7, the stronger claim — one
  stub instance serves authoring *and* the whole attempt, and its call count is
  still 1 once the attempt seals. It walks paths the per-answer test never
  does: a second blank, the ladder, the reveal, and sealing.
"""

from __future__ import annotations

import dataclasses
import json
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
from socratic.domain.prompting import CallType
from socratic.domain.records import (
    Guess,
    Outcome,
    Probe,
    QuizAttempt,
    Verdict,
)
from socratic.domain.registry import (
    BlankRange,
    ModePolicy,
    ModeRegistry,
    NOVICE_POLICY,
    default_registry,
)
from socratic.domain.repositories import InMemoryAttemptRepository
from socratic.domain.session import BlankAlreadyResolved, QuizSession
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
        quiz_session, _ = wire(attempt, registry=model_graded)

        with pytest.raises(NotImplementedError):
            submit(quiz_session, attempt, "b1", "b1-o1")

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
