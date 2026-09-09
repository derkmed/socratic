"""Queued inquiries and start-this-instead displacement (#15, acceptance 26).

The single-topic-focus rule with an escape hatch. A second question asked with
a quiz open is parked on the attempt rather than answered; the learner's way
out is to say so explicitly, which abandons the quiz in front of them and
carries the rest of the queue onto the one that replaces it.

`abandoned` is written **only** there. Nothing in this module infers that a
learner has left, and `sealed_at` stays null on an attempt they walked away
from - it is the completion stamp, and nothing completed.

Spec: `docs/specs/queued-inquiries-and-displacement.md`.
"""

import json

import pytest

from socratic.domain import inquiry as inquiry_module
from socratic.domain.authoring import QuizAuthoring
from socratic.domain.ids import Ulid
from socratic.domain.model_client import ModelResponse, RecordingModelClient
from socratic.domain.modes import DifficultyMode, ProbeCadence
from socratic.domain.prompting import CallType
from socratic.domain.records import Guess, Outcome, Verdict
from socratic.domain.repositories import InMemoryAttemptRepository
from socratic.domain.types import DirectAnswer, Quiz

LEARNER = "learner-ada"

FROZEN_MILLIS = 1_760_000_000_000


def frozen_clock() -> int:
    return FROZEN_MILLIS


def quiz_payload(
    *,
    topic: str = "the second law of thermodynamics",
    queued_topics: tuple[str, ...] = (),
) -> dict:
    return {
        "type": "quiz",
        "topic": topic,
        "explanation": [
            {"type": "text", "text": "Heat flows from hot to cold because "},
            {"type": "blank", "blank_id": "b1"},
            {"type": "text", "text": " rises."},
        ],
        "blanks": [
            {
                "blank_id": "b1",
                "options": [
                    {"option_id": "o1", "text": "entropy"},
                    {"option_id": "o2", "text": "enthalpy"},
                ],
                "correct_option_id": "o1",
                "reinforcement": "Entropy is the one that never decreases.",
                "hints": ["Think about disorder.", "The bound.", "Entropy."],
                "rubric": None,
            }
        ],
        "recap": "Entropy never decreases in an isolated system.",
        "queued_topics": list(queued_topics),
    }


DIRECT_ANSWER_PAYLOAD = {
    "type": "direct_answer",
    "topic": "chest pain",
    "answer": "Call emergency services now. This is not a quiz.",
    "queued_topics": [],
}


def _responses(*payloads) -> dict:
    return {
        CallType.AUTHOR_SKELETON: [
            ModelResponse(
                content=json.dumps(payload),
                message_id=f"msg_01AUTHORING{index}",
                cache_read_input_tokens=512,
            )
            for index, payload in enumerate(payloads)
        ]
    }


def build(*payloads):
    """An intake wired to a stub serving `payloads`, one per authoring call."""
    client = RecordingModelClient(_responses(*payloads))
    attempts = InMemoryAttemptRepository()
    intake = inquiry_module.InquiryIntake(
        authoring=QuizAuthoring(
            model_client=client, attempts=attempts, clock=frozen_clock
        ),
        attempts=attempts,
    )
    return intake, client, attempts


def _raise(intake, inquiry: str, **kwargs):
    return intake.raise_inquiry(
        inquiry, LEARNER, mode=DifficultyMode.NOVICE, **kwargs
    )


def _instead(intake, inquiry: str, **kwargs):
    return intake.start_this_instead(
        inquiry, LEARNER, mode=DifficultyMode.NOVICE, **kwargs
    )


def _a_guess(attempt) -> Guess:
    return Guess(
        blank_id=attempt.quiz.blanks[0].blank_id,
        submitted="o1",
        verdict=Verdict.CORRECT,
        attempt_ordinal=1,
        hint_rung_shown=None,
        created_at=attempt.created_at,
        graded_by="deterministic",
    )


class TestTheFirstInquiry:
    """With nothing open, an inquiry is simply authored."""

    def test_it_authors_a_quiz(self):
        intake, client, attempts = build(quiz_payload())

        started = _raise(intake, "Why does heat flow?")

        assert isinstance(started, inquiry_module.Started)
        assert isinstance(started.result, Quiz)
        assert started.attempt.outcome is Outcome.IN_FLIGHT
        assert started.displaced is None
        assert client.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_the_attempt_it_returns_is_the_stored_one(self):
        intake, _, attempts = build(quiz_payload())

        started = _raise(intake, "Why does heat flow?")

        stored = attempts.get(LEARNER, started.attempt.attempt_id)
        assert stored == started.attempt

    def test_a_blank_inquiry_is_refused_before_the_model_is_consulted(self):
        intake, client, _ = build(quiz_payload())

        with pytest.raises(ValueError, match="inquiry"):
            _raise(intake, "   ")

        client.assert_never_called()

    def test_a_direct_answer_creates_no_attempt(self):
        intake, _, attempts = build(DIRECT_ANSWER_PAYLOAD)

        started = _raise(intake, "I have chest pain")

        assert isinstance(started.result, DirectAnswer)
        assert started.attempt is None
        assert attempts.list_for_learner(LEARNER) == ()


class TestASecondInquiryMidQuiz:
    """Acceptance 1: queued, and the quiz in front of the learner carries on."""

    def test_it_is_queued_rather_than_authored(self):
        intake, client, _ = build(quiz_payload())
        _raise(intake, "Why does heat flow?")

        queued = _raise(intake, "What is the third law?")

        assert isinstance(queued, inquiry_module.Queued)
        assert queued.inquiry == "What is the third law?"
        assert queued.attempt.queued_topics == ("What is the third law?",)
        # The whole point: parking a question costs no model call.
        assert client.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_the_live_attempt_carries_on(self):
        intake, _, attempts = build(quiz_payload())
        started = _raise(intake, "Why does heat flow?")
        attempts.save(started.attempt.with_guess(_a_guess(started.attempt)))

        queued = _raise(intake, "What is the third law?")

        assert queued.attempt.attempt_id == started.attempt.attempt_id
        assert queued.attempt.outcome is Outcome.IN_FLIGHT
        assert queued.attempt.sealed_at is None
        assert len(queued.attempt.guesses) == 1

    def test_the_queue_is_persisted_on_the_attempt(self):
        intake, _, attempts = build(quiz_payload())
        started = _raise(intake, "Why does heat flow?")

        _raise(intake, "What is the third law?")

        stored = attempts.get(LEARNER, started.attempt.attempt_id)
        assert stored.queued_topics == ("What is the third law?",)

    def test_the_queue_keeps_the_order_the_questions_were_asked_in(self):
        intake, _, _ = build(quiz_payload())
        _raise(intake, "Why does heat flow?")

        _raise(intake, "What is the third law?")
        queued = _raise(intake, "What is absolute zero?")

        assert queued.attempt.queued_topics == (
            "What is the third law?",
            "What is absolute zero?",
        )

    def test_the_same_question_asked_twice_is_queued_once(self):
        # CONTEXT: Queued topics - *distinct* questions the learner raised.
        intake, _, _ = build(quiz_payload())
        _raise(intake, "Why does heat flow?")

        _raise(intake, "What is the third law?")
        queued = _raise(intake, "  What is the third law?  ")

        assert queued.attempt.queued_topics == ("What is the third law?",)

    def test_a_blank_inquiry_never_reaches_the_queue(self):
        intake, _, attempts = build(quiz_payload())
        started = _raise(intake, "Why does heat flow?")

        with pytest.raises(ValueError, match="inquiry"):
            _raise(intake, "   ")

        stored = attempts.get(LEARNER, started.attempt.attempt_id)
        assert stored.queued_topics == ()

    def test_it_joins_the_queue_the_quiz_was_authored_with(self):
        intake, _, _ = build(quiz_payload(queued_topics=("the third law",)))
        _raise(intake, "Why does heat flow?")

        queued = _raise(intake, "What is absolute zero?")

        assert queued.attempt.queued_topics == (
            "the third law",
            "What is absolute zero?",
        )

    def test_nothing_here_abandons_anything(self):
        # Acceptance 6: `abandoned` is written only on explicit displacement.
        intake, _, attempts = build(quiz_payload())
        _raise(intake, "Why does heat flow?")

        _raise(intake, "What is the third law?")

        outcomes = {a.outcome for a in attempts.list_for_learner(LEARNER)}
        assert outcomes == {Outcome.IN_FLIGHT}

    def test_a_sealed_attempt_is_not_live_so_the_inquiry_is_authored(self):
        # Nothing is open, so there is nothing to defer to: the learner
        # finished their quiz and asked the next question.
        intake, client, attempts = build(quiz_payload(), quiz_payload())
        started = _raise(intake, "Why does heat flow?")
        attempts.save(started.attempt.sealed(started.attempt.created_at))

        second = _raise(intake, "What is the third law?")

        assert isinstance(second, inquiry_module.Started)
        assert client.call_count(CallType.AUTHOR_SKELETON) == 2

    def test_an_abandoned_attempt_is_not_live_either(self):
        intake, client, attempts = build(quiz_payload(), quiz_payload())
        started = _raise(intake, "Why does heat flow?")
        attempts.save(started.attempt.abandoned())

        second = _raise(intake, "What is the third law?")

        assert isinstance(second, inquiry_module.Started)
        assert client.call_count(CallType.AUTHOR_SKELETON) == 2


class TestStartThisInstead:
    """Acceptance 26: the displaced attempt is abandoned, the queue moves."""

    def test_the_displaced_attempt_is_marked_abandoned(self):
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        first = _raise(intake, "Why does heat flow?")

        started = _instead(intake, "What is the third law?")

        assert started.displaced.attempt_id == first.attempt.attempt_id
        assert started.displaced.outcome is Outcome.ABANDONED
        stored = attempts.get(LEARNER, first.attempt.attempt_id)
        assert stored.outcome is Outcome.ABANDONED

    def test_the_displaced_attempt_keeps_its_work_and_seals_nothing(self):
        # Acceptance 4: partial work is the curation signal, and `sealed_at`
        # stays null because nothing was completed.
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        first = _raise(intake, "Why does heat flow?")
        attempts.save(first.attempt.with_guess(_a_guess(first.attempt)))

        started = _instead(intake, "What is the third law?")

        assert started.displaced.sealed_at is None
        assert started.displaced.is_sealed is False
        assert len(started.displaced.guesses) == 1

    def test_the_new_attempt_is_in_flight_on_its_own_session(self):
        intake, _, _ = build(quiz_payload(), quiz_payload())
        first = _raise(intake, "Why does heat flow?")

        started = _instead(intake, "What is the third law?")

        assert started.attempt.attempt_id != first.attempt.attempt_id
        assert started.attempt.session_id != first.attempt.session_id
        assert started.attempt.outcome is Outcome.IN_FLIGHT

    def test_the_remaining_queue_carries_forward(self):
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        _raise(intake, "Why does heat flow?")
        _raise(intake, "What is the third law?")
        _raise(intake, "What is absolute zero?")

        started = _instead(intake, "What is the third law?")

        # The one being started is no longer queued; the other still is.
        assert started.attempt.queued_topics == ("What is absolute zero?",)

    def test_the_carried_queue_is_persisted_on_the_new_attempt(self):
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        _raise(intake, "Why does heat flow?")
        _raise(intake, "What is absolute zero?")

        started = _instead(intake, "What is the third law?")

        stored = attempts.get(LEARNER, started.attempt.attempt_id)
        assert stored.queued_topics == ("What is absolute zero?",)

    def test_the_inherited_queue_leads_the_new_quizs_own(self):
        intake, _, _ = build(
            quiz_payload(),
            quiz_payload(queued_topics=("what a Carnot cycle is",)),
        )
        _raise(intake, "Why does heat flow?")
        _raise(intake, "What is absolute zero?")

        started = _instead(intake, "What is the third law?")

        assert started.attempt.queued_topics == (
            "What is absolute zero?",
            "what a Carnot cycle is",
        )

    def test_a_topic_on_both_queues_is_carried_once(self):
        intake, _, _ = build(
            quiz_payload(),
            quiz_payload(queued_topics=("What is absolute zero?",)),
        )
        _raise(intake, "Why does heat flow?")
        _raise(intake, "What is absolute zero?")

        started = _instead(intake, "What is the third law?")

        assert started.attempt.queued_topics == ("What is absolute zero?",)

    def test_with_nothing_open_it_is_an_ordinary_authoring(self):
        intake, client, _ = build(quiz_payload())

        started = _instead(intake, "Why does heat flow?")

        assert started.displaced is None
        assert started.attempt.outcome is Outcome.IN_FLIGHT
        assert client.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_a_direct_answer_displaces_nothing(self):
        # Nothing was started, so nothing was displaced - and there would be
        # no attempt for the queue to carry forward onto.
        intake, _, attempts = build(quiz_payload(), DIRECT_ANSWER_PAYLOAD)
        first = _raise(intake, "Why does heat flow?")
        _raise(intake, "What is absolute zero?")

        started = _instead(intake, "I have chest pain")

        assert isinstance(started.result, DirectAnswer)
        assert started.displaced is None
        assert started.attempt is None
        stored = attempts.get(LEARNER, first.attempt.attempt_id)
        assert stored.outcome is Outcome.IN_FLIGHT
        assert stored.queued_topics == ("What is absolute zero?",)

    def test_a_blank_inquiry_displaces_nothing_either(self):
        intake, client, attempts = build(quiz_payload())
        first = _raise(intake, "Why does heat flow?")

        with pytest.raises(ValueError, match="inquiry"):
            _instead(intake, "   ")

        stored = attempts.get(LEARNER, first.attempt.attempt_id)
        assert stored.outcome is Outcome.IN_FLIGHT
        assert client.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_the_new_attempt_is_stamped_with_the_current_cadence(self):
        # One attempt, one authoring-time cadence (CONTEXT: probe_cadence):
        # the restart is authored fresh, not cloned from what it displaced.
        intake, _, _ = build(quiz_payload(), quiz_payload())
        _raise(intake, "Why does heat flow?", probe_cadence=ProbeCadence.OFF)

        started = _instead(
            intake, "What is the third law?", probe_cadence=ProbeCadence.ALWAYS
        )

        assert started.attempt.probe_cadence_at_authoring is ProbeCadence.ALWAYS


class TestTheLiveAttempt:
    def test_it_is_the_latest_in_flight_attempt_in_the_partition(self):
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        first = _raise(intake, "Why does heat flow?")
        attempts.save(first.attempt.sealed(first.attempt.created_at))
        second = _raise(intake, "What is the third law?")

        assert (
            intake.live_attempt(LEARNER).attempt_id == second.attempt.attempt_id
        )

    def test_a_partition_with_nothing_open_has_none(self):
        intake, _, attempts = build(quiz_payload())
        started = _raise(intake, "Why does heat flow?")
        attempts.save(started.attempt.abandoned())

        assert intake.live_attempt(LEARNER) is None

    def test_another_learners_attempt_is_not_live_here(self):
        # The partition is the boundary (ADR-0005), and displacement reads it.
        intake, client, _ = build(quiz_payload(), quiz_payload())
        _raise(intake, "Why does heat flow?")

        other = intake.raise_inquiry(
            "What is the third law?",
            "learner-grace",
            mode=DifficultyMode.NOVICE,
        )

        assert isinstance(other, inquiry_module.Started)
        assert client.call_count(CallType.AUTHOR_SKELETON) == 2


class TestNothingInfersThatALearnerLeft:
    """The hard constraint of #15, asserted rather than trusted."""

    def test_the_module_has_no_sweeper_or_timeout(self):
        names = dir(inquiry_module)
        assert not [
            name
            for name in names
            if any(
                word in name.lower()
                for word in ("sweep", "timeout", "expire", "stale", "reap")
            )
        ]

    def test_an_old_in_flight_attempt_is_still_in_flight(self):
        # Age is a reader's judgement, never a writer's (CONTEXT: Outcome).
        intake, _, attempts = build(quiz_payload(), quiz_payload())
        started = _raise(intake, "Why does heat flow?")

        _raise(intake, "What is the third law?")

        stored = attempts.get(LEARNER, started.attempt.attempt_id)
        assert stored.outcome is Outcome.IN_FLIGHT
        assert stored.sealed_at is None
        assert Ulid.parse(stored.attempt_id).millis == FROZEN_MILLIS
