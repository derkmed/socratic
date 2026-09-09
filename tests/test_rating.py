"""Submitting a rating (CONTEXT: RatingRecord; umbrella spec §10, §11).

`RatingRecord` and `RatingRepository` both existed before this ticket, but
nothing composed them, so the quiz service's rating endpoint had no seam to
translate over. This is that seam and nothing more: one function, one `Clock`,
no model call. The rating is deliberately *outside* the attempt — a sealed
attempt still accepts one, because a rating is a human signal about quiz
quality rather than part of the learner's work.
"""

from datetime import datetime, timezone

import pytest

from socratic.domain import rating, repositories
from socratic.domain.modes import DifficultyMode, ProbeCadence
from socratic.domain.records import QuizAttempt
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

LEARNER = "learner-7"
SESSION = "01J000000000000000000000AA"
ATTEMPT = "01J000000000000000000000ZZ"


MILLIS = 1_757_332_800_000
"""2026-09-08T12:00:00Z. The domain's clocks return milliseconds (`ids.Clock`)."""


def _clock() -> int:
    return MILLIS


def _at() -> datetime:
    return datetime.fromtimestamp(MILLIS / 1000, tz=timezone.utc)


def _quiz() -> Quiz:
    return Quiz(
        quiz_session_id=SESSION,
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        explanation=(TextSegment("Heat flows because "), BlankSegment("b1")),
        blanks=(
            Blank(
                blank_id="b1",
                mode=DifficultyMode.NOVICE,
                options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
                correct_option_id="o1",
            ),
        ),
        recap="Entropy never decreases.",
    )


def _attempt(attempt_id: str = ATTEMPT) -> QuizAttempt:
    return QuizAttempt(
        attempt_id=attempt_id,
        learner_id=LEARNER,
        session_id=SESSION,
        quiz=_quiz(),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        created_at=_at(),
        probe_cadence_at_authoring=ProbeCadence.SOMETIMES,
        model_id="claude-opus-5",
        effort="low",
        prompt_version="2026-09-08.1",
    )


def _repos():
    attempts = repositories.InMemoryAttemptRepository()
    ratings = repositories.InMemoryRatingRepository()
    return attempts, ratings


def test_a_rating_is_stored_against_the_attempt():
    attempts, ratings = _repos()
    attempts.save(_attempt())

    record = rating.submit_rating(
        attempts=attempts,
        ratings=ratings,
        learner_id=LEARNER,
        attempt_id=ATTEMPT,
        score=4,
        clock=_clock,
    )

    assert record.score == 4
    assert record.attempt_id == ATTEMPT
    assert record.learner_id == LEARNER
    assert ratings.get(ATTEMPT) == record
    assert record.created_at == _at()


def test_a_sealed_attempt_still_accepts_a_rating():
    attempts, ratings = _repos()
    attempts.save(_attempt().sealed(at=_at()))

    record = rating.submit_rating(
        attempts=attempts,
        ratings=ratings,
        learner_id=LEARNER,
        attempt_id=ATTEMPT,
        score=5,
        clock=_clock,
    )

    assert record.score == 5


def test_a_score_outside_one_to_five_is_refused():
    attempts, ratings = _repos()
    attempts.save(_attempt())

    with pytest.raises(ValueError):
        rating.submit_rating(
            attempts=attempts,
            ratings=ratings,
            learner_id=LEARNER,
            attempt_id=ATTEMPT,
            score=6,
            clock=_clock,
        )

    assert ratings.get(ATTEMPT) is None


def test_rating_an_attempt_that_is_not_the_learners_is_refused():
    attempts, ratings = _repos()
    attempts.save(_attempt())

    with pytest.raises(KeyError):
        rating.submit_rating(
            attempts=attempts,
            ratings=ratings,
            learner_id="someone-else",
            attempt_id=ATTEMPT,
            score=3,
            clock=_clock,
        )

    assert ratings.get(ATTEMPT) is None


def test_a_second_rating_is_refused_and_the_first_stands():
    """`InMemoryRatingRepository` is "immutable once written" — a rating is a
    record of what the learner said at sealing, not a mutable preference. The
    seam does not soften that; it lets the repository's ValueError out, and the
    service turns it into a 409."""
    attempts, ratings = _repos()
    attempts.save(_attempt())

    first = rating.submit_rating(
        attempts=attempts,
        ratings=ratings,
        learner_id=LEARNER,
        attempt_id=ATTEMPT,
        score=2,
        clock=_clock,
    )

    with pytest.raises(ValueError):
        rating.submit_rating(
            attempts=attempts,
            ratings=ratings,
            learner_id=LEARNER,
            attempt_id=ATTEMPT,
            score=5,
            clock=_clock,
        )

    assert ratings.get(ATTEMPT) == first
