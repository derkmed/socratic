"""Submitting a rating (CONTEXT: RatingRecord; umbrella spec §10, §11).

The one operation the quiz service's four endpoints needed and did not have.
`RatingRecord` carries the invariant (a Likert score runs 1-5) and
`RatingRepository` carries the storage; what was missing was the composition —
check the attempt is this learner's, stamp the clock, store it. Without it the
service's rating handler would have had to do all three, and a handler holding
domain logic is what this ticket forbids.

A rating is **not** part of the attempt. It is keyed by attempt id and stored
separately, so it can arrive after sealing without disturbing a sealed record
(CONTEXT: RatingRecord) — sealing is precisely when the learner is asked.
"""

from datetime import datetime, timezone

from socratic.domain import ids, repositories
from socratic.domain.records import RatingRecord

__all__ = ["submit_rating"]


def submit_rating(
    *,
    attempts: repositories.AttemptRepository,
    ratings: repositories.RatingRepository,
    learner_id: str,
    attempt_id: str,
    score: int,
    clock: ids.Clock = ids.system_clock,
) -> RatingRecord:
    """Store one learner's Likert score for one attempt.

    The attempt is fetched before anything is written, so a score for an
    attempt that is not this learner's raises `KeyError` from the repository
    and stores nothing. `RatingRecord` refuses a score outside 1-5, which is
    why this function does not check the range itself.

    A second rating for the same attempt is refused by the repository, which
    holds ratings "immutable once written". That `ValueError` is allowed out
    rather than swallowed: the caller decides what a re-rating means, and the
    service answers it with a 409.
    """
    attempts.get(learner_id, attempt_id)

    record = RatingRecord(
        attempt_id=attempt_id,
        learner_id=learner_id,
        score=score,
        created_at=_now(clock),
    )
    ratings.save(record)
    return record


def _now(clock: ids.Clock) -> datetime:
    """The domain's clocks return milliseconds; records carry datetimes."""
    return datetime.fromtimestamp(clock() / 1000, tz=timezone.utc)
