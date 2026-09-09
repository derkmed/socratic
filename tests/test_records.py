"""The persisted record shape (spec Approach section 3, D5/D7/D9, ADR-0005).

Capture is a first-class goal, not a byproduct of grading: the attempt record is
stamped and complete from day one so a later curation job can replay what
actually happened without a schema migration.

The bounds ADR-0009 restated - at most 20 blanks, at most 4 guesses and 2 probes
per blank - are enforced at construction, which is what "enforced, not assumed"
means once every write goes through a record.
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from socratic.domain.ids import Ulid
from socratic.domain.modes import DifficultyMode, GradingStrategy, ProbeCadence
from socratic.domain.records import (
    MAX_BLANKS,
    MAX_GUESSES,
    MAX_GUESSES_PER_BLANK,
    MAX_PROBES,
    MAX_PROBES_PER_BLANK,
    SCHEMA_VERSION,
    Guess,
    ModelCallRecord,
    Outcome,
    Probe,
    QuizAttempt,
    RatingRecord,
    TokenUsage,
    Verdict,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

AT = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


def _blank(index: int) -> Blank:
    return Blank(
        blank_id=f"b{index}",
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("a", "b", "c"),
    )


def _quiz(blank_count: int = 2) -> Quiz:
    blanks = tuple(_blank(index) for index in range(1, blank_count + 1))
    explanation: list = [TextSegment("Heat flows because ")]
    for blank in blanks:
        explanation.append(BlankSegment(blank.blank_id))
        explanation.append(TextSegment(" rises."))
    return Quiz(
        quiz_session_id=str(Ulid.mint()),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        explanation=tuple(explanation),
        blanks=blanks,
        recap="Entropy never decreases.",
    )


def _attempt(**overrides) -> QuizAttempt:
    fields = dict(
        attempt_id=str(Ulid.mint()),
        learner_id="learner-1",
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


def _guess(blank_id: str = "b1", ordinal: int = 1, **overrides) -> Guess:
    fields = dict(
        blank_id=blank_id,
        submitted="entropy",
        verdict=Verdict.CORRECT,
        attempt_ordinal=ordinal,
        hint_rung_shown=None,
        created_at=AT,
        graded_by=GradingStrategy.DETERMINISTIC,
    )
    fields.update(overrides)
    return Guess(**fields)


def _probe(blank_id: str = "b1", **overrides) -> Probe:
    fields = dict(
        blank_id=blank_id,
        question="How did you arrive at that?",
        self_explanation="Disorder increases, so entropy is the term.",
        verdict=Verdict.CORRECT,
        reopened_blank=False,
        cadence_at_fire=ProbeCadence.SOMETIMES,
        asked_at=AT,
        answered_at=LATER,
        message_id="msg_01probe",
    )
    fields.update(overrides)
    return Probe(**fields)


class TestGuess:
    def test_a_guess_carries_everything_acceptance_22_asks_for(self):
        guess = _guess(verdict=Verdict.INCORRECT, hint_rung_shown=1)
        assert guess.blank_id == "b1"
        assert guess.submitted == "entropy"
        assert guess.verdict is Verdict.INCORRECT
        assert guess.attempt_ordinal == 1
        assert guess.hint_rung_shown == 1
        assert guess.created_at == AT
        assert guess.graded_by is GradingStrategy.DETERMINISTIC

    def test_graded_by_reuses_the_grading_strategy_vocabulary(self):
        # D4: "deterministic | model" is already named by GradingStrategy, so
        # the record borrows it rather than minting a parallel enum.
        assert _guess().graded_by in tuple(GradingStrategy)

    def test_a_guess_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            _guess().submitted = "enthalpy"

    def test_the_ladder_only_has_three_rungs(self):
        with pytest.raises(ValueError, match="hint rung"):
            _guess(hint_rung_shown=4)

    def test_a_blank_is_attempted_at_most_four_times(self):
        with pytest.raises(ValueError, match="ordinal"):
            _guess(ordinal=MAX_GUESSES_PER_BLANK + 1)


class TestProbe:
    def test_a_probe_carries_everything_the_curation_job_replays(self):
        probe = _probe(reopened_blank=True)
        assert probe.blank_id == "b1"
        assert probe.question == "How did you arrive at that?"
        assert probe.self_explanation.startswith("Disorder")
        assert probe.verdict is Verdict.CORRECT
        assert probe.reopened_blank is True
        assert probe.cadence_at_fire is ProbeCadence.SOMETIMES
        assert probe.asked_at == AT
        assert probe.answered_at == LATER
        assert probe.message_id == "msg_01probe"

    def test_a_dismissed_probe_persists_with_a_null_self_explanation(self):
        # Acceptance 18: dismissible, so everything downstream of the reply is
        # unset - but the question having been asked is still a fact.
        probe = _probe(
            self_explanation=None, verdict=None, answered_at=None, message_id=None
        )
        assert probe.self_explanation is None
        assert probe.answered_at is None
        assert probe.is_answered is False

    def test_an_answered_probe_knows_it(self):
        assert _probe().is_answered is True

    def test_a_probe_that_was_answered_needs_a_verdict(self):
        with pytest.raises(ValueError, match="verdict"):
            _probe(verdict=None)

    def test_an_unanswered_probe_cannot_have_re_opened_a_blank(self):
        with pytest.raises(ValueError, match="re-open"):
            _probe(
                self_explanation=None,
                verdict=None,
                answered_at=None,
                message_id=None,
                reopened_blank=True,
            )

    def test_a_probe_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            _probe().self_explanation = "something else"


class TestQuizAttempt:
    def test_an_attempt_opens_in_flight_with_an_unset_sealed_at(self):
        attempt = _attempt()
        assert attempt.outcome is Outcome.IN_FLIGHT
        assert attempt.sealed_at is None
        assert attempt.is_sealed is False

    def test_the_attempt_id_is_a_ulid(self):
        attempt = _attempt()
        assert Ulid.parse(attempt.attempt_id).timestamp is not None

    def test_a_non_ulid_attempt_id_is_rejected(self):
        with pytest.raises(ValueError, match="ULID"):
            _attempt(attempt_id="attempt-1")

    def test_it_holds_the_raw_unfilled_quiz_exactly_as_authored(self):
        # Acceptance 22, first half: the Quiz is stored as-is, not flattened.
        quiz = _quiz()
        assert _attempt(quiz=quiz).quiz is quiz

    def test_the_version_stamps_are_present_on_every_attempt(self):
        attempt = _attempt()
        assert attempt.model_id == "claude-opus-5"
        assert attempt.effort == "low"
        assert attempt.prompt_version == "2026-09-08.1"
        assert attempt.schema_version == SCHEMA_VERSION

    def test_the_authoring_cadence_is_recorded_even_when_probes_are_off(self):
        # Acceptance 21: `off` must be visible from the record alone, which is
        # why the cadence is stamped rather than inferred from zero probes.
        attempt = _attempt(probe_cadence_at_authoring=ProbeCadence.OFF)
        assert attempt.probe_cadence_at_authoring is ProbeCadence.OFF
        assert attempt.probes == ()

    def test_guesses_accumulate_in_the_order_they_were_made(self):
        attempt = (
            _attempt()
            .with_guess(_guess("b1", 1, submitted="enthalpy", verdict=Verdict.INCORRECT,
                               hint_rung_shown=1))
            .with_guess(_guess("b1", 2))
            .with_guess(_guess("b2", 1))
        )
        assert [g.submitted for g in attempt.guesses] == [
            "enthalpy",
            "entropy",
            "entropy",
        ]
        assert [g.blank_id for g in attempt.guesses] == ["b1", "b1", "b2"]

    def test_probes_are_their_own_ordered_collection_peer_of_guesses(self):
        # D9 / ADR-0009: a probe mutates blank state, so it is an event in the
        # timeline, never an annotation nested on a past guess.
        attempt = _attempt().with_guess(_guess()).with_probe(_probe())
        assert attempt.probes == (_probe(),)
        assert not any(hasattr(g, "probe") for g in attempt.guesses)
        assert {f.name for f in dataclasses.fields(QuizAttempt)} >= {
            "guesses",
            "probes",
        }

    def test_with_guess_leaves_the_previous_record_untouched(self):
        attempt = _attempt()
        assert attempt.with_guess(_guess()).guesses != attempt.guesses
        assert attempt.guesses == ()

    def test_a_guess_against_an_unknown_blank_is_rejected(self):
        with pytest.raises(ValueError, match="unknown blank"):
            _attempt().with_guess(_guess("b99"))

    def test_a_probe_against_an_unknown_blank_is_rejected(self):
        with pytest.raises(ValueError, match="unknown blank"):
            _attempt().with_probe(_probe("b99"))

    def test_with_quiz_swaps_the_quiz_and_leaves_the_previous_record_untouched(
        self,
    ):
        # The pedagogy merge (#9) needs this write; before #46 it reached for
        # `dataclasses.replace` and so went round `_refuse_if_sealed`.
        attempt = _attempt()
        merged = _quiz()
        swapped = attempt.with_quiz(merged)
        assert swapped.quiz is merged
        assert attempt.quiz is not merged

    def test_with_quiz_keeps_the_guesses_already_recorded(self):
        attempt = _attempt().with_guess(_guess())
        assert attempt.with_quiz(_quiz()).guesses == attempt.guesses

    def test_with_quiz_rejects_a_quiz_that_drops_a_guessed_blank(self):
        # The record's own invariants run again on the swap, so a merge that
        # loses a blank cannot orphan the guesses that name it.
        attempt = _attempt().with_guess(_guess("b2"))
        with pytest.raises(ValueError, match="unknown blank"):
            attempt.with_quiz(_quiz(1))

    def test_the_anthropic_message_ids_and_usage_are_kept_per_call(self):
        # D7 / ADR-0007: the message.id list is the audit link from a stored
        # quiz back to the exact API calls behind it.
        call = ModelCallRecord(
            call_type="author_skeleton",
            message_id="msg_01skeleton",
            usage=TokenUsage(
                input_tokens=900,
                output_tokens=400,
                cache_creation_input_tokens=512,
                cache_read_input_tokens=0,
            ),
        )
        attempt = _attempt().with_model_call(call)
        assert [c.message_id for c in attempt.model_calls] == ["msg_01skeleton"]
        assert attempt.model_calls[0].usage.cache_read_input_tokens == 0


class TestSealing:
    def test_sealing_stamps_the_time_and_resolves_the_outcome(self):
        sealed = _attempt().with_guess(_guess()).sealed(LATER)
        assert sealed.sealed_at == LATER
        assert sealed.outcome is Outcome.RESOLVED
        assert sealed.is_sealed is True

    def test_an_incomplete_attempt_persists_its_guesses_with_no_sealed_at(self):
        # Acceptance 23, and the distinguishing test for it: same guesses,
        # different terminal state.
        incomplete = _attempt().with_guess(_guess())
        complete = incomplete.sealed(LATER)
        assert incomplete.sealed_at is None
        assert incomplete.outcome is Outcome.IN_FLIGHT
        assert incomplete.guesses == complete.guesses

    def test_a_sealed_attempt_cannot_be_sealed_again(self):
        with pytest.raises(ValueError, match="sealed"):
            _attempt().sealed(LATER).sealed(LATER)

    def test_a_sealed_attempt_accepts_no_further_guesses_or_probes(self):
        sealed = _attempt().sealed(LATER)
        with pytest.raises(ValueError, match="sealed"):
            sealed.with_guess(_guess())
        with pytest.raises(ValueError, match="sealed"):
            sealed.with_probe(_probe())

    def test_a_sealed_attempt_cannot_have_its_quiz_replaced(self):
        # #46: the write the pedagogy merge needs, refused like every other.
        sealed = _attempt().sealed(LATER)
        with pytest.raises(ValueError, match="sealed"):
            sealed.with_quiz(_quiz())

    def test_every_with_method_on_the_record_refuses_a_sealed_attempt(self):
        # The structural half of #46. A `with_*` sibling added later without
        # `_refuse_if_sealed` fails here; one added without an entry in this
        # table fails here too, so the guard cannot be forgotten quietly.
        arguments = {
            "with_guess": _guess(),
            "with_probe": _probe(),
            "with_model_call": ModelCallRecord(
                call_type="author_pedagogy",
                message_id="msg_01pedagogy",
                usage=TokenUsage(
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            ),
            "with_quiz": _quiz(),
        }
        mutators = {
            name
            for name in dir(QuizAttempt)
            if name.startswith("with_") and callable(getattr(QuizAttempt, name))
        }
        assert mutators == set(arguments)

        sealed = _attempt().sealed(LATER)
        for name, argument in arguments.items():
            with pytest.raises(ValueError, match="sealed"):
                getattr(sealed, name)(argument)

    def test_a_sealed_attempt_is_still_reconstructible_field_for_field(self):
        # Deliberate, and the reason the `dataclasses.replace` route stays
        # advisory rather than structural (#46): a repository materialising a
        # stored document rebuilds a sealed attempt through `__init__`, and
        # `__post_init__` cannot tell that apart from a caller rebuilding one
        # with fresh content. Refusing sealed reconstruction would break
        # persistence, `sealed()` and `abandoned()` alike.
        sealed = _attempt().with_guess(_guess()).sealed(LATER)
        rebuilt = QuizAttempt(
            **{
                field.name: getattr(sealed, field.name)
                for field in dataclasses.fields(sealed)
            }
        )
        assert rebuilt == sealed

    def test_displacement_marks_the_attempt_abandoned(self):
        # ADR-0005 / CONTEXT Outcome: `abandoned` is written only on
        # displacement. We never guess that a learner left.
        abandoned = _attempt().abandoned(LATER)
        assert abandoned.outcome is Outcome.ABANDONED
        assert abandoned.sealed_at == LATER

    def test_an_outcome_other_than_in_flight_needs_a_sealed_at(self):
        with pytest.raises(ValueError, match="sealed_at"):
            _attempt(outcome=Outcome.RESOLVED)

    def test_an_in_flight_attempt_cannot_carry_a_sealed_at(self):
        with pytest.raises(ValueError, match="sealed_at"):
            _attempt(sealed_at=LATER)


class TestBounds:
    def test_the_twenty_blank_cap_is_enforced_at_write_time(self):
        with pytest.raises(ValueError, match="20 blanks"):
            _attempt(quiz=_quiz(MAX_BLANKS + 1))

    def test_a_quiz_at_the_cap_is_admitted(self):
        assert len(_attempt(quiz=_quiz(MAX_BLANKS)).quiz.blanks) == MAX_BLANKS

    def test_at_most_four_guesses_per_blank(self):
        attempt = _attempt()
        for ordinal in range(1, MAX_GUESSES_PER_BLANK + 1):
            attempt = attempt.with_guess(_guess("b1", ordinal))
        with pytest.raises(ValueError, match="guesses"):
            attempt.with_guess(_guess("b1", 1))

    def test_at_most_two_probes_per_blank(self):
        attempt = _attempt().with_probe(_probe()).with_probe(_probe())
        with pytest.raises(ValueError, match="probes"):
            attempt.with_probe(_probe())

    def test_the_document_bound_follows_from_the_per_blank_bounds(self):
        # ADR-0009's restated arithmetic: 20 blanks x 4 guesses and x 2 probes.
        assert MAX_GUESSES == MAX_BLANKS * MAX_GUESSES_PER_BLANK == 80
        assert MAX_PROBES == MAX_BLANKS * MAX_PROBES_PER_BLANK == 40

    def test_a_full_document_stays_inside_the_bound(self):
        attempt = _attempt(quiz=_quiz(MAX_BLANKS))
        for index in range(1, MAX_BLANKS + 1):
            for ordinal in range(1, MAX_GUESSES_PER_BLANK + 1):
                attempt = attempt.with_guess(_guess(f"b{index}", ordinal))
            for _ in range(MAX_PROBES_PER_BLANK):
                attempt = attempt.with_probe(_probe(f"b{index}"))
        assert len(attempt.guesses) == MAX_GUESSES
        assert len(attempt.probes) == MAX_PROBES


class TestRatingRecord:
    def test_a_rating_is_keyed_by_attempt_id(self):
        rating = RatingRecord(
            attempt_id="01J000000000000000000000",
            learner_id="learner-1",
            score=4,
            created_at=LATER,
        )
        assert rating.attempt_id == "01J000000000000000000000"
        assert rating.score == 4

    @pytest.mark.parametrize("score", [0, 6, -1])
    def test_the_likert_scale_runs_one_to_five(self, score):
        with pytest.raises(ValueError, match="1-5"):
            RatingRecord(
                attempt_id="01J000000000000000000000",
                learner_id="learner-1",
                score=score,
                created_at=LATER,
            )

    def test_a_rating_is_immutable(self):
        rating = RatingRecord(
            attempt_id="01J000000000000000000000",
            learner_id="learner-1",
            score=5,
            created_at=LATER,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rating.score = 1


# The learner profile is not a record in this module - it is a
# rewritten-in-place aggregate, and it lives in `socratic.domain.profiles`
# (#36). Its tests moved with it, to `tests/test_profiles.py`.
