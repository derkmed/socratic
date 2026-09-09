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

from socratic.domain.ids import QuizSessionId
from socratic.domain.profiles import LearnerProfile
from socratic.domain.records import QuizAttempt, RatingRecord
from socratic.domain.settings import LearnerSettings


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
    def get_by_session(
        self, learner_id: str, session_id: QuizSessionId
    ) -> QuizAttempt | None:
        """The latest attempt on a session, or `None` if there is none.

        The lookup every caller arriving with a capability token needs: the
        token is scoped to a `QuizSessionId` (CONTEXT: Capability token) and the
        attempt is keyed by its own ULID (ADR-0007), so a session cannot be
        turned into an attempt id by construction. Without this on the port each
        such caller reads the whole partition on the learner's hot path.

        **The latest, by attempt id.** A session holds one attempt today; once
        [#15](https://github.com/derkmed/socratic/issues/15) lets a displaced
        session be restarted it holds the abandoned one too, and ULIDs are
        order-preserving, so the latest is the restart. Filtering on
        `Outcome.IN_FLIGHT` instead would read the same attempt in that case and
        lose a sealed one when it is the only attempt there is - which is
        exactly what the late pedagogy payload reads to decide to drop itself.

        `None` rather than `KeyError`: a session with no attempt is what a stale
        token looks like, not a wiring mistake, and the caller decides.
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



class LearnerSettingsRepository(abc.ABC):
    """One `LearnerSettings` document per learner, rewritten in place.

    The settings of record, so a request carrying none — which is every request
    the iframe makes, since the iframe cannot see `UserValves` — can still be
    served the learner's current ones (#14, ADR-0010).
    """

    @abc.abstractmethod
    def save(self, settings: LearnerSettings) -> None:
        """Write the learner's settings, replacing any previous ones."""

    @abc.abstractmethod
    def get(self, learner_id: str) -> LearnerSettings | None:
        """The learner's settings, or `None` if they have never been recorded.

        `None` rather than the defaults: "never set" and "set to the defaults"
        are the same behaviour but not the same fact, and the answering path
        needs to tell them apart to know whether to fall back to the attempt's
        `probe_cadence_at_authoring`.
        """


class InMemoryAttemptRepository(AttemptRepository):
    """Attempts held per learner, which is the partition made literal."""

    def __init__(self) -> None:
        self._partitions: dict[str, dict[str, QuizAttempt]] = {}
        # The session index: learner -> session -> the attempt ids on it. It
        # holds ids rather than documents, so there is one copy of every
        # attempt and a re-save cannot leave a stale one behind here.
        self._sessions: dict[str, dict[QuizSessionId, set[str]]] = {}

    def save(self, attempt: QuizAttempt) -> None:
        partition = self._partitions.setdefault(attempt.learner_id, {})
        stored = partition.get(attempt.attempt_id)
        if stored is not None and stored.is_sealed:
            raise ValueError(
                f"attempt {attempt.attempt_id} was sealed at {stored.sealed_at} "
                "and is never written again"
            )
        partition[attempt.attempt_id] = attempt

        sessions = self._sessions.setdefault(attempt.learner_id, {})
        if stored is not None and stored.session_id != attempt.session_id:
            # One document, one session: the entry the previous write left on
            # the old session would otherwise resolve to a document that no
            # longer claims it.
            self._forget(sessions, stored)
        sessions.setdefault(attempt.session_id, set()).add(attempt.attempt_id)

    @staticmethod
    def _forget(
        sessions: dict[QuizSessionId, set[str]], stored: QuizAttempt
    ) -> None:
        attempt_ids = sessions.get(stored.session_id)
        if attempt_ids is None:
            return
        attempt_ids.discard(stored.attempt_id)
        if not attempt_ids:
            del sessions[stored.session_id]

    def get(self, learner_id: str, attempt_id: str) -> QuizAttempt:
        try:
            return self._partitions[learner_id][attempt_id]
        except KeyError:
            raise KeyError(
                f"no attempt {attempt_id!r} in learner {learner_id!r}'s partition"
            ) from None

    def get_by_session(
        self, learner_id: str, session_id: QuizSessionId
    ) -> QuizAttempt | None:
        attempt_ids = self._sessions.get(learner_id, {}).get(session_id)
        if not attempt_ids:
            return None
        # ULIDs are order-preserving, so the largest id is the latest attempt.
        # The document comes from the partition, never from the index, so what
        # this returns is by construction what `get` would return.
        return self._partitions[learner_id][max(attempt_ids)]

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


class InMemoryLearnerSettingsRepository(LearnerSettingsRepository):
    """Settings held by learner id."""

    def __init__(self) -> None:
        self._settings: dict[str, LearnerSettings] = {}

    def save(self, settings: LearnerSettings) -> None:
        self._settings[settings.learner_id] = settings

    def get(self, learner_id: str) -> LearnerSettings | None:
        return self._settings.get(learner_id)
