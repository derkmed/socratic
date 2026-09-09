"""The repository ports and their in-memory implementations (D5, ADR-0005).

The in-memory implementations **are** the test doubles - there are no separate
fakes, which is why this seam costs nothing extra (spec seam table).

Two invariants are structural rather than documented: every attempt read is
scoped by `learner_id`, so there is no cross-partition read path to reach for by
accident; and a sealed attempt cannot be written again, which is the half of
acceptance 24 that a separate rating record alone does not prove.
"""

import abc
from datetime import datetime, timezone

import pytest

from socratic.domain.ids import Ulid
from socratic.domain.modes import DifficultyMode, GradingStrategy, ProbeCadence
from socratic.domain.profiles import LearnerProfile
from socratic.domain.records import (
    Guess,
    Outcome,
    QuizAttempt,
    RatingRecord,
    Verdict,
)
from socratic.domain.repositories import (
    AttemptRepository,
    InMemoryAttemptRepository,
    InMemoryLearnerProfileRepository,
    InMemoryRatingRepository,
    LearnerProfileRepository,
    RatingRepository,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

AT = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


def _quiz() -> Quiz:
    blank = Blank(
        blank_id="b1",
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("a", "b", "c"),
    )
    return Quiz(
        quiz_session_id=str(Ulid.mint()),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        explanation=(TextSegment("Heat flows because "), BlankSegment("b1")),
        blanks=(blank,),
        recap="Entropy never decreases.",
    )


def _attempt(learner_id: str = "learner-1", **overrides) -> QuizAttempt:
    fields = dict(
        attempt_id=str(Ulid.mint()),
        learner_id=learner_id,
        session_id=str(Ulid.mint()),
        quiz=_quiz(),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        created_at=AT,
        probe_cadence_at_authoring=ProbeCadence.SOMETIMES,
        model_id="claude-opus-5",
        effort="low",
        prompt_version="2026-09-08.1",
    )
    fields.update(overrides)
    return QuizAttempt(**fields)


def _guess(**overrides) -> Guess:
    fields = dict(
        blank_id="b1",
        submitted="entropy",
        verdict=Verdict.CORRECT,
        attempt_ordinal=1,
        hint_rung_shown=None,
        created_at=AT,
        graded_by=GradingStrategy.DETERMINISTIC,
    )
    fields.update(overrides)
    return Guess(**fields)


class TestThePortsAreAbstract:
    @pytest.mark.parametrize(
        "port, implementation",
        [
            (AttemptRepository, InMemoryAttemptRepository),
            (RatingRepository, InMemoryRatingRepository),
            (LearnerProfileRepository, InMemoryLearnerProfileRepository),
        ],
    )
    def test_each_port_is_an_abstract_base_with_an_in_memory_implementation(
        self, port, implementation
    ):
        assert isinstance(port, abc.ABCMeta)
        assert port.__abstractmethods__
        assert issubclass(implementation, port)
        with pytest.raises(TypeError):
            port()


class TestAttemptRepository:
    def test_an_attempt_round_trips_with_its_quiz_and_guesses_in_order(self):
        # Acceptance 22: the raw unfilled quiz plus every guess in the order
        # made, each with verdict, ladder rung and timestamp.
        repository = InMemoryAttemptRepository()
        attempt = (
            _attempt()
            .with_guess(_guess(verdict=Verdict.INCORRECT, hint_rung_shown=1,
                               submitted="enthalpy"))
            .with_guess(_guess(attempt_ordinal=2))
            .sealed(LATER)
        )
        repository.save(attempt)

        stored = repository.get("learner-1", attempt.attempt_id)
        assert stored.quiz == attempt.quiz
        assert [g.submitted for g in stored.guesses] == ["enthalpy", "entropy"]
        assert stored.guesses[0].hint_rung_shown == 1
        assert stored.guesses[0].created_at == AT
        assert stored.sealed_at == LATER

    def test_an_incomplete_attempt_persists_with_an_unset_sealed_at(self):
        # Acceptance 23.
        repository = InMemoryAttemptRepository()
        attempt = _attempt().with_guess(_guess())
        repository.save(attempt)

        stored = repository.get("learner-1", attempt.attempt_id)
        assert stored.sealed_at is None
        assert stored.outcome is Outcome.IN_FLIGHT
        assert len(stored.guesses) == 1

    def test_attempts_are_partitioned_on_learner_id(self):
        repository = InMemoryAttemptRepository()
        mine = _attempt("learner-1")
        theirs = _attempt("learner-2")
        repository.save(mine)
        repository.save(theirs)

        assert repository.list_for_learner("learner-1") == (mine,)
        assert repository.list_for_learner("learner-2") == (theirs,)

    def test_another_learners_attempt_is_not_reachable_by_id(self):
        repository = InMemoryAttemptRepository()
        theirs = _attempt("learner-2")
        repository.save(theirs)
        with pytest.raises(KeyError):
            repository.get("learner-1", theirs.attempt_id)

    def test_an_unknown_attempt_raises(self):
        with pytest.raises(KeyError):
            InMemoryAttemptRepository().get("learner-1", str(Ulid.mint()))

    def test_a_learner_with_no_attempts_reads_empty(self):
        assert InMemoryAttemptRepository().list_for_learner("nobody") == ()

    def test_attempts_come_back_in_mint_order(self):
        # ULIDs are order-preserving, so lexicographic sort is mint-time sort.
        repository = InMemoryAttemptRepository()
        first = _attempt()
        second = _attempt()
        repository.save(second)
        repository.save(first)
        assert repository.list_for_learner("learner-1") == (first, second)

    def test_saving_again_replaces_the_in_flight_document(self):
        # ADR-0005: every guess is a read-modify-write of the whole document.
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        repository.save(attempt.with_guess(_guess()))
        assert len(repository.get("learner-1", attempt.attempt_id).guesses) == 1

    def test_a_sealed_attempt_is_never_written_again(self):
        # Acceptance 24's other half: the attempt document is unchanged after
        # sealing, enforced by the store rather than trusted to callers.
        repository = InMemoryAttemptRepository()
        sealed = _attempt().sealed(LATER)
        repository.save(sealed)
        with pytest.raises(ValueError, match="sealed"):
            repository.save(sealed)

    def test_a_stored_attempt_is_not_perturbed_by_later_writes(self):
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        attempt.with_guess(_guess())
        assert repository.get("learner-1", attempt.attempt_id).guesses == ()


class TestLookupBySession:
    """`get_by_session`, which every caller holding a capability token needs.

    The token is scoped to a `QuizSessionId` (CONTEXT: Capability token) and the
    attempt is keyed by its own ULID (ADR-0007), so a session cannot be turned
    into an attempt id by construction - the port has to offer the lookup, or
    every caller reads the whole partition on a learner's hot path.

    The contract is **the latest attempt on that session**, not the first and
    not the in-flight one: a lone sealed attempt is still the attempt for its
    session, and where #15 leaves two, mint order names the restart.
    """

    def test_an_attempt_is_found_by_its_session(self):
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)

        assert repository.get_by_session("learner-1", attempt.session_id) == attempt

    def test_an_unknown_session_reads_none(self):
        # `None` rather than `KeyError`: a session with no attempt is what a
        # stale token looks like, and the caller decides what that means.
        repository = InMemoryAttemptRepository()
        repository.save(_attempt())
        assert repository.get_by_session("learner-1", str(Ulid.mint())) is None

    def test_a_session_is_looked_up_inside_the_partition(self):
        # No cross-partition read path, the same as `get`.
        repository = InMemoryAttemptRepository()
        theirs = _attempt("learner-2")
        repository.save(theirs)
        assert repository.get_by_session("learner-1", theirs.session_id) is None

    def test_a_sealed_attempt_is_still_the_attempt_for_its_session(self):
        # The pedagogy payload landing late reads the sealed attempt to decide
        # to drop itself (`authoring.author_pedagogy`); an in-flight-only
        # lookup would turn that into a `KeyError`.
        repository = InMemoryAttemptRepository()
        sealed = _attempt().sealed(LATER)
        repository.save(sealed)

        assert repository.get_by_session("learner-1", sealed.session_id) == sealed

    def test_only_the_named_session_is_returned(self):
        repository = InMemoryAttemptRepository()
        mine = _attempt()
        other = _attempt()
        repository.save(mine)
        repository.save(other)

        assert repository.get_by_session("learner-1", mine.session_id) == mine
        assert repository.get_by_session("learner-1", other.session_id) == other

    def test_a_session_with_an_abandoned_and_an_in_flight_attempt_reads_the_restart(
        self,
    ):
        # The case #15 creates: a displaced session restarted under the same
        # `QuizSessionId`. The restart is minted later and ULIDs are
        # order-preserving, so the latest attempt *is* the live one - no
        # `outcome` filter needed.
        repository = InMemoryAttemptRepository()
        session_id = str(Ulid.mint())
        displaced = _attempt(session_id=session_id).abandoned(LATER)
        restarted = _attempt(session_id=session_id)
        assert displaced.attempt_id < restarted.attempt_id
        repository.save(displaced)
        repository.save(restarted)

        found = repository.get_by_session("learner-1", session_id)
        assert found == restarted
        assert found.outcome is Outcome.IN_FLIGHT

    def test_the_order_the_two_are_written_in_does_not_decide(self):
        repository = InMemoryAttemptRepository()
        session_id = str(Ulid.mint())
        displaced = _attempt(session_id=session_id).abandoned(LATER)
        restarted = _attempt(session_id=session_id)
        repository.save(restarted)
        repository.save(displaced)

        assert repository.get_by_session("learner-1", session_id) == restarted

    def test_re_saving_an_attempt_leaves_the_lookup_reading_the_new_document(self):
        # The hand-maintained index has to stay in step with the partition on
        # every write, not just the first.
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        repository.save(attempt)
        guessed = attempt.with_guess(_guess())
        repository.save(guessed)

        assert repository.get_by_session("learner-1", attempt.session_id) == guessed

    def test_sealing_an_attempt_leaves_the_lookup_reading_the_sealed_document(self):
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        sealed = attempt.sealed(LATER)
        repository.save(sealed)

        found = repository.get_by_session("learner-1", attempt.session_id)
        assert found == sealed
        assert found.is_sealed

    def test_the_lookup_still_agrees_with_the_partition_after_a_re_save(self):
        # Drift is the failure mode of a hand-maintained index, so assert the
        # two read paths against each other rather than only the new one.
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        repository.save(attempt.with_guess(_guess()))

        by_session = repository.get_by_session("learner-1", attempt.session_id)
        assert by_session == repository.get("learner-1", attempt.attempt_id)
        assert repository.list_for_learner("learner-1") == (by_session,)

    def test_an_attempt_re_saved_under_another_session_leaves_no_stale_entry(self):
        # One document, one session: if a caller rewrites the same attempt id
        # under a different session, the entry it left behind must go or the
        # old session keeps resolving to a document that no longer claims it.
        repository = InMemoryAttemptRepository()
        attempt = _attempt()
        repository.save(attempt)
        moved = _attempt(
            attempt_id=attempt.attempt_id, session_id=str(Ulid.mint())
        )
        repository.save(moved)

        assert repository.get_by_session("learner-1", attempt.session_id) is None
        assert repository.get_by_session("learner-1", moved.session_id) == moved


class TestRatingRepository:
    def test_a_rating_is_a_separate_record_leaving_the_attempt_untouched(self):
        # Acceptance 24.
        attempts = InMemoryAttemptRepository()
        ratings = InMemoryRatingRepository()
        sealed = _attempt().with_guess(_guess()).sealed(LATER)
        attempts.save(sealed)

        ratings.save(
            RatingRecord(
                attempt_id=sealed.attempt_id,
                learner_id="learner-1",
                score=4,
                created_at=LATER,
            )
        )

        assert attempts.get("learner-1", sealed.attempt_id) == sealed
        assert ratings.get(sealed.attempt_id).score == 4

    def test_an_unrated_attempt_has_no_rating(self):
        assert InMemoryRatingRepository().get(str(Ulid.mint())) is None

    def test_a_rating_is_written_once(self):
        ratings = InMemoryRatingRepository()
        attempt_id = str(Ulid.mint())
        rating = RatingRecord(
            attempt_id=attempt_id,
            learner_id="learner-1",
            score=4,
            created_at=LATER,
        )
        ratings.save(rating)
        with pytest.raises(ValueError, match="already rated"):
            ratings.save(rating)


class TestLearnerProfileRepository:
    def test_a_profile_round_trips(self):
        profiles = InMemoryLearnerProfileRepository()
        profile = LearnerProfile(
            learner_id="learner-1",
            ledger={"topics/entropy": 4, "weak/free-energy": 2},
            narrative="Comfortable with entropy.",
            watermark=str(Ulid.mint()),
            updated_at=LATER,
        )
        profiles.save(profile)
        stored = profiles.get("learner-1")
        assert stored == profile
        # Both halves of D8 survive the round trip: before #36 the store held a
        # profile with no ledger at all.
        assert stored.ledger == {"topics/entropy": 4, "weak/free-energy": 2}

    def test_a_learner_with_no_profile_reads_none(self):
        assert InMemoryLearnerProfileRepository().get("learner-1") is None

    def test_the_profile_is_rewritten_in_place(self):
        # D8: ProfileBuilder rewrites the profile and advances the watermark.
        profiles = InMemoryLearnerProfileRepository()
        profiles.save(LearnerProfile(learner_id="learner-1", narrative="first"))
        profiles.save(LearnerProfile(learner_id="learner-1", narrative="second"))
        assert profiles.get("learner-1").narrative == "second"
