"""The write-only JSON trail (#117, ADR-0018, spec: collected-records-on-disk).

The service never reads this directory back, so there is no decoder to round-trip
against - not here either, deliberately. That makes the assertions **structural**:
the encoded attempt is checked against `dataclasses.fields` so a field added to
`QuizAttempt` tomorrow fails this file rather than vanishing from the audit
record, which is the failure mode ADR-0018 rejected a hand-written encoder over.
"""

import dataclasses
import json
import logging
from unittest import mock
from datetime import datetime, timezone

import pytest

from socratic.adapters import collected_records
from socratic.domain.ids import Ulid
from socratic.domain.modes import DifficultyMode, GradingStrategy, ProbeCadence
from socratic.domain.repositories import (
    InMemoryAttemptRepository,
    InMemoryRatingRepository,
)
from socratic.domain.records import (
    Guess,
    QuizAttempt,
    RatingRecord,
    Verdict,
)
from socratic.domain.types import (
    Blank,
    BlankSegment,
    MathSegment,
    Option,
    Quiz,
    TextSegment,
)

AT = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
WRITTEN_AT = 1757332800000


def _quiz(session_id: str | None = None) -> Quiz:
    blank = Blank(
        blank_id="b1",
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("a", "b", "c"),
    )
    return Quiz(
        quiz_session_id=session_id or str(Ulid.mint()),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        explanation=(
            TextSegment("Heat flows because "),
            MathSegment("<math><mi>S</mi></math>"),
            BlankSegment("b1"),
        ),
        blanks=(blank,),
        recap="Entropy never decreases.",
    )


def _attempt(learner_id: str = "learner-1", **overrides) -> QuizAttempt:
    quiz = overrides.pop("quiz", None) or _quiz(overrides.pop("session_id", None))
    fields = dict(
        attempt_id=str(Ulid.mint()),
        learner_id=learner_id,
        session_id=quiz.quiz_session_id,
        quiz=quiz,
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


class TestTheEnvelope:
    """Every file is self-describing, a rating as much as an attempt."""

    def test_an_attempt_is_wrapped_with_its_kind_version_and_write_time(self):
        attempt = _attempt()

        envelope = collected_records.encode(attempt, written_at=WRITTEN_AT)

        assert set(envelope) == {
            "schema_version",
            "kind",
            "written_at",
            "record",
        }
        assert envelope["kind"] == "attempt"
        assert envelope["schema_version"] == attempt.schema_version
        assert envelope["written_at"] == WRITTEN_AT

    def test_a_rating_is_wrapped_the_same_way(self):
        """`RatingRecord` carries no `schema_version` field of its own.

        Which is the whole reason for the envelope: a bare `asdict` walk would
        leave the rating file the one unversioned artifact in the directory.
        """
        rating = RatingRecord(
            attempt_id=str(Ulid.mint()),
            learner_id="learner-1",
            score=4,
            created_at=AT,
        )

        envelope = collected_records.encode(rating, written_at=WRITTEN_AT)

        assert set(envelope) == {
            "schema_version",
            "kind",
            "written_at",
            "record",
        }
        assert envelope["kind"] == "rating"
        assert envelope["schema_version"]
        assert envelope["written_at"] == WRITTEN_AT


class TestTheWalkIsGeneric:
    """A field added to a record tomorrow must not vanish from the trail."""

    def test_every_declared_field_of_the_attempt_reaches_the_record(self):
        """Asserted against `dataclasses.fields`, not a hand-written list.

        A hand-written encoder's failure mode is omitting a new field silently
        (ADR-0018), so the test that guards it cannot itself enumerate the
        fields by hand.
        """
        attempt = _attempt(guesses=(_guess(),))

        encoded = collected_records.encode(attempt, written_at=WRITTEN_AT)["record"]

        expected = {field.name for field in dataclasses.fields(QuizAttempt)}
        assert set(encoded) == expected

    def test_the_walk_reaches_nested_records_too(self):
        attempt = _attempt(guesses=(_guess(),))

        encoded = collected_records.encode(attempt, written_at=WRITTEN_AT)["record"]

        assert len(encoded["guesses"]) == 1
        expected = {field.name for field in dataclasses.fields(Guess)}
        assert set(encoded["guesses"][0]) == expected

    def test_a_refused_record_type_is_named_rather_than_half_encoded(self):
        with pytest.raises(TypeError, match="attempts and ratings"):
            collected_records.encode(_quiz(), written_at=WRITTEN_AT)


class TestTheSegmentTag:
    """The one place a generic walk genuinely loses information."""

    def test_each_segment_carries_the_member_it_was(self):
        """`Segment` is a bare `Union`; its members carry no discriminator.

        Once `TextSegment` is `{"text": ...}` there is nothing left in the JSON
        to say which of the three it was, so the tag is injected.
        """
        attempt = _attempt()

        encoded = collected_records.encode(attempt, written_at=WRITTEN_AT)["record"]

        segments = encoded["quiz"]["explanation"]
        assert [segment["kind"] for segment in segments] == [
            "text",
            "math",
            "blank",
        ]

    def test_nothing_else_is_tagged(self):
        """The tag is for `Segment` alone - a `Blank` is not a union member."""
        attempt = _attempt()

        encoded = collected_records.encode(attempt, written_at=WRITTEN_AT)["record"]

        assert "kind" not in encoded["quiz"]["blanks"][0]


class TestItSerialises:
    def test_the_whole_envelope_dumps_and_parses(self):
        attempt = _attempt(guesses=(_guess(),))

        text = json.dumps(
            collected_records.encode(attempt, written_at=WRITTEN_AT),
            default=collected_records.json_default,
        )

        parsed = json.loads(text)
        assert parsed["kind"] == "attempt"
        assert parsed["record"]["attempt_id"] == attempt.attempt_id

    def test_a_datetime_becomes_an_iso_8601_string(self):
        attempt = _attempt()

        text = json.dumps(
            collected_records.encode(attempt, written_at=WRITTEN_AT),
            default=collected_records.json_default,
        )

        assert json.loads(text)["record"]["created_at"] == AT.isoformat()

    def test_an_enum_becomes_its_value_not_its_repr(self):
        """The enums are `str` subclasses, so `json` needs no help with them.

        Pinned because the day one of them stops being a `str` subclass, the
        trail would silently start recording `DifficultyMode.NOVICE`.
        """
        attempt = _attempt()

        text = json.dumps(
            collected_records.encode(attempt, written_at=WRITTEN_AT),
            default=collected_records.json_default,
        )

        record = json.loads(text)["record"]
        assert record["mode"] == "novice"
        assert record["probe_cadence_at_authoring"] == "sometimes"
        assert record["outcome"] == "in_flight"


class TestThePaths:
    """One file per record, in a tree that mirrors ADR-0005's partition."""

    def test_an_attempt_lands_under_its_learner(self, tmp_path):
        attempt = _attempt(learner_id="learner-1")

        path = collected_records.record_path(tmp_path, attempt)

        assert path == (
            tmp_path / "attempts" / "learner-1" / f"{attempt.attempt_id}.json"
        )

    def test_a_rating_partitions_the_same_way(self, tmp_path):
        """Even though `RatingRepository.get` takes no partition key.

        The flat lookup is a prototype convenience; the trail shows the shape
        ADR-0005's document store will enforce.
        """
        rating = RatingRecord(
            attempt_id="01J000000000000000000ABCD",
            learner_id="learner-1",
            score=4,
            created_at=AT,
        )

        path = collected_records.record_path(tmp_path, rating)

        assert path == (
            tmp_path / "ratings" / "learner-1" / "01J000000000000000000ABCD.json"
        )

    @pytest.mark.parametrize(
        "learner_id",
        ["../../etc", "a/b", "..", ".", "learner/../..", "\windows"],
    )
    def test_a_learner_id_cannot_escape_the_data_directory(
        self, tmp_path, learner_id
    ):
        """The partition key is a path segment, so it is attacker-shaped input.

        Percent-encoding settles the separators; `.` and `..` need naming
        separately, because both are already percent-encoding-safe characters
        and so survive `quote` untouched.
        """
        attempt = _attempt(learner_id=learner_id)

        path = collected_records.record_path(tmp_path, attempt)

        resolved = path.resolve()
        assert resolved.is_relative_to(tmp_path.resolve())
        assert resolved.parent.parent == (tmp_path / "attempts").resolve()

    def test_an_ordinary_learner_id_stays_readable(self, tmp_path):
        """The directory is meant to be opened and read by a human (#117)."""
        attempt = _attempt(learner_id="derek@example.com")

        path = collected_records.record_path(tmp_path, attempt)

        assert path.parent.name == "derek%40example.com"


def _writing_attempts(tmp_path, **overrides):
    delegate = overrides.pop("delegate", None) or InMemoryAttemptRepository()
    return collected_records.FileWritingAttemptRepository(
        delegate, tmp_path, clock=lambda: WRITTEN_AT, **overrides
    )


class TestTheAttemptWrapperDelegates:
    """A wrapper, not a subclass - so the port contract is the delegate's."""

    def test_reads_come_straight_back_from_the_delegate(self, tmp_path):
        repository = _writing_attempts(tmp_path)
        attempt = _attempt()

        repository.save(attempt)

        assert repository.get(attempt.learner_id, attempt.attempt_id) == attempt
        assert repository.get_by_session(
            attempt.learner_id, attempt.session_id
        ) == attempt
        assert repository.list_for_learner(attempt.learner_id) == (attempt,)

    def test_it_delegates_before_it_writes(self, tmp_path):
        """The delegate is the authority on whether the write is legal.

        Writing first would leave a file on disk for a write the store rejected.
        """
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().sealed(AT)
        repository.save(attempt)
        written = collected_records.record_path(tmp_path, attempt)
        written.unlink()

        with pytest.raises(ValueError, match="never written again"):
            repository.save(attempt)

        assert not written.exists()


class TestWhenTheAttemptIsWritten:
    def test_a_sealed_attempt_lands_on_disk(self, tmp_path):
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().sealed(AT)

        repository.save(attempt)

        path = collected_records.record_path(tmp_path, attempt)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["kind"] == "attempt"
        assert envelope["written_at"] == WRITTEN_AT
        assert envelope["record"]["attempt_id"] == attempt.attempt_id

    def test_an_abandoned_attempt_lands_too(self, tmp_path):
        """The predicate is `is_closed`, not `sealed_at` (CONTEXT: Closed).

        A displaced attempt keeps its guesses and leaves `sealed_at` null, and
        it is exactly as much of an audit record as a finished one.
        """
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().abandoned()

        repository.save(attempt)

        assert collected_records.record_path(tmp_path, attempt).exists()

    def test_an_in_flight_attempt_does_not(self, tmp_path):
        """`save()` runs on every guess and probe - up to ~120 times."""
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().with_guess(_guess())

        repository.save(attempt)

        assert not collected_records.record_path(tmp_path, attempt).exists()

    def test_every_save_writes_the_in_flight_attempt_too(self, tmp_path):
        """The demonstration knob: files appear while a quiz is being taken."""
        repository = _writing_attempts(
            tmp_path, flush=collected_records.FlushCadence.EVERY_SAVE
        )
        attempt = _attempt()

        repository.save(attempt)
        path = collected_records.record_path(tmp_path, attempt)
        assert path.exists()

        repository.save(attempt.with_guess(_guess()))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert len(envelope["record"]["guesses"]) == 1

    def test_nothing_is_left_behind_by_the_atomic_write(self, tmp_path):
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().sealed(AT)

        repository.save(attempt)

        directory = collected_records.record_path(tmp_path, attempt).parent
        assert [path.name for path in directory.iterdir()] == [
            f"{attempt.attempt_id}.json"
        ]


class TestAFailedWriteIsSwallowed:
    def test_the_learners_answer_survives_a_broken_data_directory(
        self, tmp_path, caplog
    ):
        """Raising here would cost a learner their last answer.

        The write lands on the request that seals the quiz, so ADR-0018 logs and
        carries on: the demo artifact is not worth the answer. The startup check
        is what catches this before a learner is ever involved.
        """
        (tmp_path / "attempts").write_text("not a directory", encoding="utf-8")
        repository = _writing_attempts(tmp_path)
        attempt = _attempt().sealed(AT)

        with caplog.at_level(logging.WARNING):
            repository.save(attempt)

        assert repository.get(attempt.learner_id, attempt.attempt_id) == attempt
        assert attempt.attempt_id in caplog.text


def _rating(**overrides) -> RatingRecord:
    fields = dict(
        attempt_id=str(Ulid.mint()),
        learner_id="learner-1",
        score=4,
        created_at=AT,
    )
    fields.update(overrides)
    return RatingRecord(**fields)


def _writing_ratings(tmp_path, **overrides):
    delegate = overrides.pop("delegate", None) or InMemoryRatingRepository()
    return collected_records.FileWritingRatingRepository(
        delegate, tmp_path, clock=lambda: WRITTEN_AT, **overrides
    )


class TestTheRatingWrapper:
    def test_a_rating_is_written_on_save(self, tmp_path):
        """A `RatingRecord` has no closed state - it is immutable on arrival.

        So there is no cadence to wait for, under either flush setting.
        """
        repository = _writing_ratings(tmp_path)
        rating = _rating()

        repository.save(rating)

        path = collected_records.record_path(tmp_path, rating)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["kind"] == "rating"
        assert envelope["record"]["score"] == 4
        assert envelope["schema_version"]

    def test_reads_come_straight_back_from_the_delegate(self, tmp_path):
        repository = _writing_ratings(tmp_path)
        rating = _rating()

        repository.save(rating)

        assert repository.get(rating.attempt_id) == rating
        assert repository.get("no such attempt") is None

    def test_a_second_rating_is_refused_and_writes_nothing(self, tmp_path):
        repository = _writing_ratings(tmp_path)
        rating = _rating()
        repository.save(rating)
        written = collected_records.record_path(tmp_path, rating)
        written.unlink()

        with pytest.raises(ValueError, match="already rated"):
            repository.save(rating)

        assert not written.exists()

    def test_a_failed_write_is_swallowed_here_too(self, tmp_path, caplog):
        (tmp_path / "ratings").write_text("not a directory", encoding="utf-8")
        repository = _writing_ratings(tmp_path)
        rating = _rating()

        with caplog.at_level(logging.WARNING):
            repository.save(rating)

        assert repository.get(rating.attempt_id) == rating
        assert rating.attempt_id in caplog.text


class TestTheWiring:
    """`build_app`'s conditional, lifted out so it needs no Anthropic SDK.

    Not one of the spec's seams: `socratic.service.__main__` imports the adapter
    at module level, so the only test of the wiring would otherwise be gated
    behind the optional `anthropic` extra and never run below the seam.
    """

    def test_no_data_dir_leaves_the_repositories_exactly_as_they_were(self):
        attempts = InMemoryAttemptRepository()
        ratings = InMemoryRatingRepository()

        wrapped = collected_records.wrap(attempts, ratings, data_dir=None)

        assert wrapped == (attempts, ratings)

    def test_a_data_dir_wraps_both(self, tmp_path):
        attempts = InMemoryAttemptRepository()
        ratings = InMemoryRatingRepository()

        wrapped_attempts, wrapped_ratings = collected_records.wrap(
            attempts, ratings, data_dir=tmp_path
        )

        assert isinstance(
            wrapped_attempts, collected_records.FileWritingAttemptRepository
        )
        assert isinstance(
            wrapped_ratings, collected_records.FileWritingRatingRepository
        )
        attempt = _attempt().sealed(AT)
        wrapped_attempts.save(attempt)
        assert collected_records.record_path(tmp_path, attempt).exists()

    def test_the_flush_cadence_reaches_the_wrapper(self, tmp_path):
        attempts, _ = collected_records.wrap(
            InMemoryAttemptRepository(),
            InMemoryRatingRepository(),
            data_dir=tmp_path,
            flush=collected_records.FlushCadence.EVERY_SAVE,
        )
        attempt = _attempt()

        attempts.save(attempt)

        assert collected_records.record_path(tmp_path, attempt).exists()


class TestTheCollectedRecordsConfiguration:
    """`SOCRATIC_DATA_DIR` and `SOCRATIC_DATA_FLUSH` (#117, ADR-0018).

    Read here rather than in `build_app` where the repositories are constructed,
    so one place answers what the service reads from the environment - and so
    the writability check is the same shape of startup refusal the other four
    variables already get.
    """

    ENV = {
        "SOCRATIC_TOKEN_SECRET": "0123456789abcdef0123456789abcdef",
        "SOCRATIC_SERVICE_TOKEN": "service-token-for-the-pipe",
    }

    def test_unset_means_no_persistence(self):
        """Additive and reversible: the service behaves exactly as before.

        Which is also why no existing test needs a temporary directory.
        """
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(dict(self.ENV))

        assert config.data_dir is None

    def test_the_directory_is_created_if_it_is_not_there(self, tmp_path):
        from socratic.service.config import ServiceConfig

        target = tmp_path / "data"
        config = ServiceConfig.from_env(
            {**self.ENV, "SOCRATIC_DATA_DIR": str(target)}
        )

        assert config.data_dir == target
        assert target.is_dir()

    def test_a_data_dir_that_is_a_file_refuses_the_boot(self, tmp_path):
        """Setting the variable is a statement that the records are wanted.

        Silently not collecting them is the failure #117 exists to prevent, so
        this is the one persistence failure that raises rather than logging.
        """
        from socratic.service.config import ConfigError, ServiceConfig

        target = tmp_path / "data"
        target.write_text("not a directory", encoding="utf-8")

        with pytest.raises(ConfigError, match="SOCRATIC_DATA_DIR"):
            ServiceConfig.from_env(
                {**self.ENV, "SOCRATIC_DATA_DIR": str(target)}
            )

    def test_an_unwritable_data_dir_refuses_the_boot(self, tmp_path):
        """The probe is a real write, not a permission bit.

        `os.access` and the mode bits both lie under enough circumstances -
        Windows ACLs, a read-only mount, a container running as a user the host
        directory does not know - and the check is worth having only if it
        catches those.
        """
        from socratic.service.config import ConfigError, ServiceConfig

        target = tmp_path / "data"
        target.mkdir()

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        with mock.patch("pathlib.Path.write_text", refuse):
            with pytest.raises(ConfigError, match="SOCRATIC_DATA_DIR"):
                ServiceConfig.from_env(
                    {**self.ENV, "SOCRATIC_DATA_DIR": str(target)}
                )

    def test_the_flush_cadence_defaults_to_on_close(self):
        from socratic.adapters.collected_records import FlushCadence
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(dict(self.ENV))

        assert config.data_flush is FlushCadence.ON_CLOSE

    def test_the_flush_cadence_is_read_from_the_environment(self, tmp_path):
        from socratic.adapters.collected_records import FlushCadence
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(
            {
                **self.ENV,
                "SOCRATIC_DATA_DIR": str(tmp_path / "data"),
                "SOCRATIC_DATA_FLUSH": "every_save",
            }
        )

        assert config.data_flush is FlushCadence.EVERY_SAVE

    def test_an_unknown_flush_cadence_is_refused_at_startup(self):
        """Falling back to the default would hide the typo that caused it."""
        from socratic.service.config import ConfigError, ServiceConfig

        with pytest.raises(ConfigError, match="SOCRATIC_DATA_FLUSH"):
            ServiceConfig.from_env(
                {**self.ENV, "SOCRATIC_DATA_FLUSH": "always"}
            )
