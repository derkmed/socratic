"""Repository ports, and the in-memory implementations (D5, ADR-0005).

The ports are interfaces in the domain package; nothing above the portability
seam crosses them. **The in-memory implementations are the test doubles** - the
prototype stores everything in memory, so writing separate fakes alongside them
would be two implementations of one contract, drifting apart.

Two of ADR-0005's decisions are structural here rather than documented:

* **Partitioned on `learner_id`.** Every attempt read takes the partition key,
  so "my past quizzes" is a single-partition read and there is no cross-partition
  read path to reach for by accident. Cross-user analytics is an offline job by
  design and does not use this port.
* **Sealed means sealed.** A store that accepts a second write to a sealed
  attempt would make the audit record untrustworthy no matter how careful the
  callers are, so it refuses one.

Records are frozen dataclasses over tuples, so a stored value cannot be mutated
through a reference a caller kept; the implementations hold them directly rather
than copying on read.
"""

from __future__ import annotations

import abc

from socratic.domain.profiles import LearnerProfile
from socratic.domain.records import QuizAttempt, RatingRecord


class AttemptRepository(abc.ABC):
    """One `QuizAttempt` document per quiz session, keyed by ULID."""

    @abc.abstractmethod
    def save(self, attempt: QuizAttempt) -> None:
        """Write the whole document.

        Raises:
            ValueError: if the stored attempt is already sealed.
        """

    @abc.abstractmethod
    def get(self, learner_id: str, attempt_id: str) -> QuizAttempt:
        """Read one attempt from a learner's partition.

        Raises:
            KeyError: if that learner has no such attempt.
        """

    @abc.abstractmethod
    def list_for_learner(self, learner_id: str) -> tuple[QuizAttempt, ...]:
        """Every attempt in a learner's partition, oldest first."""


class RatingRepository(abc.ABC):
    """Optional 1-5 Likert scores, keyed by attempt id and written once."""

    @abc.abstractmethod
    def save(self, rating: RatingRecord) -> None:
        """Write a rating.

        Raises:
            ValueError: if that attempt already has one.
        """

    @abc.abstractmethod
    def get(self, attempt_id: str) -> RatingRecord | None:
        """The attempt's rating, or `None` - rating is optional by design."""


class LearnerProfileRepository(abc.ABC):
    """One profile per learner, rewritten in place by `ProfileBuilder`."""

    @abc.abstractmethod
    def save(self, profile: LearnerProfile) -> None:
        """Write the profile, replacing any previous one."""

    @abc.abstractmethod
    def get(self, learner_id: str) -> LearnerProfile | None:
        """The learner's profile, or `None` before the job has ever run."""


class InMemoryAttemptRepository(AttemptRepository):
    """Attempts held per learner, which is the partition made literal."""

    def __init__(self) -> None:
        self._partitions: dict[str, dict[str, QuizAttempt]] = {}

    def save(self, attempt: QuizAttempt) -> None:
        partition = self._partitions.setdefault(attempt.learner_id, {})
        stored = partition.get(attempt.attempt_id)
        if stored is not None and stored.is_sealed:
            raise ValueError(
                f"attempt {attempt.attempt_id} was sealed at {stored.sealed_at} "
                "and is never written again"
            )
        partition[attempt.attempt_id] = attempt

    def get(self, learner_id: str, attempt_id: str) -> QuizAttempt:
        try:
            return self._partitions[learner_id][attempt_id]
        except KeyError:
            raise KeyError(
                f"no attempt {attempt_id!r} in learner {learner_id!r}'s partition"
            ) from None

    def list_for_learner(self, learner_id: str) -> tuple[QuizAttempt, ...]:
        partition = self._partitions.get(learner_id, {})
        # ULIDs are order-preserving, so lexicographic sort is mint-time sort.
        return tuple(partition[key] for key in sorted(partition))


class InMemoryRatingRepository(RatingRepository):
    """Ratings held by attempt id. Immutable once written."""

    def __init__(self) -> None:
        self._ratings: dict[str, RatingRecord] = {}

    def save(self, rating: RatingRecord) -> None:
        if rating.attempt_id in self._ratings:
            raise ValueError(f"attempt {rating.attempt_id} was already rated")
        self._ratings[rating.attempt_id] = rating

    def get(self, attempt_id: str) -> RatingRecord | None:
        return self._ratings.get(attempt_id)


class InMemoryLearnerProfileRepository(LearnerProfileRepository):
    """Profiles held by learner id."""

    def __init__(self) -> None:
        self._profiles: dict[str, LearnerProfile] = {}

    def save(self, profile: LearnerProfile) -> None:
        self._profiles[profile.learner_id] = profile

    def get(self, learner_id: str) -> LearnerProfile | None:
        return self._profiles.get(learner_id)
