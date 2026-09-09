"""The authoring path (spec `docs/specs/quiz-authoring.md`, issue #5).

`QuizAuthoring.author` is the single entry to the most complex path, and the
`direct_answer | quiz` union comes back **as a value** — which is the whole
reason the seam is shaped this way: the ADR-0004 override branch is then
assertable as a return value rather than inferred from a side effect.

Everything here runs against `RecordingModelClient` and
`InMemoryAttemptRepository`. Those *are* the doubles (master spec seam table),
so there is no fake in this file, and no test reaches the network.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from socratic.domain import authoring
from socratic.domain.authoring import AuthoringParseError, QuizAuthoring
from socratic.domain.ids import Ulid
from socratic.domain.model_client import ModelResponse, RecordingModelClient
from socratic.domain.modes import DifficultyMode, ProbeCadence
from socratic.domain.prompting import CallType, LearnerProfile, SegmentRole
from socratic.domain.modes import GradingStrategy
from socratic.domain.records import Guess, Outcome, Verdict
from socratic.domain.registry import (
    AuthoringStage,
    BlankRange,
    ModePolicy,
    ModeRegistry,
    NOVICE_POLICY,
    default_registry,
)
from socratic.domain.repositories import InMemoryAttemptRepository
from socratic.domain.types import (
    BlankSegment,
    DirectAnswer,
    MathSegment,
    Quiz,
    TextSegment,
)
from socratic.domain.validation import QuizValidationError

LEARNER = "learner-ada"

FROZEN_MILLIS = 1_760_000_000_000


def frozen_clock() -> int:
    """Milliseconds since the epoch, held still.

    The minter increments its random component within a millisecond, so a
    frozen clock still yields distinct — and still ordered — ULIDs.
    """
    return FROZEN_MILLIS


# --- Payload builders --------------------------------------------------------
#
# The structured-output payload the model returns, built as plain dicts so a
# test can break exactly one field and leave the rest well-formed.


def novice_blank_payload(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "options": [
            {"option_id": "o1", "text": "entropy"},
            {"option_id": "o2", "text": "enthalpy"},
        ],
        "correct_option_id": "o1",
        "reinforcement": "Entropy is the one that never decreases.",
        "hints": [
            "Think about disorder.",
            "It is the quantity the second law bounds.",
            "The word is 'entropy'.",
        ],
        "rubric": None,
    }


def advanced_blank_payload(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "options": None,
        "correct_option_id": None,
        "reinforcement": None,
        "hints": None,
        "rubric": "Names the quantity the second law bounds.",
    }


def quiz_payload(
    blank_ids: tuple[str, ...] = ("b1",),
    *,
    blank_builder=novice_blank_payload,
    **overrides,
) -> dict:
    explanation: list[dict] = [
        {"type": "text", "text": "Heat flows from hot to cold because "}
    ]
    for blank_id in blank_ids:
        explanation.append({"type": "blank", "blank_id": blank_id})
        explanation.append({"type": "text", "text": " rises."})

    payload = {
        "type": "quiz",
        "topic": "the second law of thermodynamics",
        "explanation": explanation,
        "blanks": [blank_builder(blank_id) for blank_id in blank_ids],
        "recap": "Entropy never decreases in an isolated system.",
        "queued_topics": ["the third law"],
    }
    payload.update(overrides)
    return payload


DIRECT_ANSWER_PAYLOAD = {
    "type": "direct_answer",
    "topic": "chest pain",
    "answer": "Call emergency services now. This is not a quiz.",
    "queued_topics": ["how the heart's conduction system works"],
}


def responses(payload, **overrides) -> dict:
    """One canned `author_skeleton` response carrying `payload`."""
    content = payload if isinstance(payload, str) else json.dumps(payload)
    fields = dict(
        content=content,
        message_id="msg_01AUTHORING",
        cache_read_input_tokens=512,
        cache_creation_input_tokens=0,
    )
    fields.update(overrides)
    return {CallType.AUTHOR_SKELETON: [ModelResponse(**fields)]}


def build(payload, *, registry=None, attempts=None, **overrides):
    """A service wired to a stub serving `payload`, and its collaborators."""
    client = RecordingModelClient(responses(payload, **overrides))
    attempts = attempts if attempts is not None else InMemoryAttemptRepository()
    service = QuizAuthoring(
        model_client=client,
        attempts=attempts,
        registry=registry,
        clock=frozen_clock,
    )
    return service, client, attempts


def author(
    payload,
    *,
    mode=DifficultyMode.NOVICE,
    inquiry="Why does heat flow?",
    **kwargs,
):
    service, client, attempts = build(payload, **kwargs)
    result = service.author(inquiry, LEARNER, mode=mode)
    return result, client, attempts


class TestTheQuizBranch:
    def test_an_ordinary_inquiry_returns_a_validated_quiz(self):
        result, _, _ = author(quiz_payload())

        assert isinstance(result, Quiz)
        assert result.topic == "the second law of thermodynamics"
        assert result.mode is DifficultyMode.NOVICE
        assert result.recap.startswith("Entropy never decreases")
        assert result.queued_topics == ("the third law",)
        # It really went through the registry's validator, not just the parser.
        assert authoring.validation.validate_quiz(result) == ()

    def test_the_explanation_stays_a_flat_segment_array(self):
        result, _, _ = author(
            quiz_payload(
                explanation=[
                    {"type": "text", "text": "Because "},
                    {"type": "blank", "blank_id": "b1"},
                    {"type": "math", "mathml": "<math><mi>S</mi></math>"},
                ]
            )
        )

        assert result.explanation == (
            TextSegment("Because "),
            BlankSegment("b1"),
            MathSegment("<math><mi>S</mi></math>"),
        )

    def test_the_session_id_is_a_ulid_we_minted(self):
        result, _, _ = author(quiz_payload())

        assert Ulid.parse(result.quiz_session_id).millis == FROZEN_MILLIS

    def test_the_skeleton_call_is_made_exactly_once_and_nothing_else_is(self):
        _, client, _ = author(quiz_payload())

        assert client.call_count() == 1
        assert client.calls[0].call_type is CallType.AUTHOR_SKELETON
        for call_type in CallType:
            if call_type is not CallType.AUTHOR_SKELETON:
                client.assert_never_called(call_type)

    def test_an_advanced_quiz_authors_against_the_advanced_range(self):
        result, _, _ = author(
            quiz_payload(
                ("b1", "b2", "b3", "b4"), blank_builder=advanced_blank_payload
            ),
            mode=DifficultyMode.ADVANCED,
        )

        assert isinstance(result, Quiz)
        assert result.mode is DifficultyMode.ADVANCED
        assert tuple(blank.rubric for blank in result.blanks) == (
            "Names the quantity the second law bounds.",
        ) * 4

    def test_the_blanks_are_stamped_with_the_quiz_s_mode(self):
        # One attempt, one mode (CONTEXT: Mode toggle). The response carries no
        # per-blank mode, so a mismatch is not representable.
        result, _, _ = author(quiz_payload(("b1", "b2")))

        assert {blank.mode for blank in result.blanks} == {DifficultyMode.NOVICE}


class TestPersistence:
    def test_it_persists_an_in_flight_attempt_holding_the_raw_unfilled_quiz(self):
        result, _, attempts = author(quiz_payload())

        stored = attempts.list_for_learner(LEARNER)
        assert len(stored) == 1
        attempt = stored[0]
        assert attempt.quiz == result
        assert attempt.outcome is Outcome.IN_FLIGHT
        assert attempt.sealed_at is None
        assert attempt.guesses == ()
        assert attempt.probes == ()

    def test_the_attempt_carries_the_session_id_and_the_learner_partition(self):
        result, _, attempts = author(quiz_payload())

        attempt = attempts.list_for_learner(LEARNER)[0]
        assert attempt.learner_id == LEARNER
        assert attempt.session_id == result.quiz_session_id
        assert attempt.topic == result.topic
        assert attempt.queued_topics == ("the third law",)

    def test_the_attempt_is_keyed_by_its_own_ulid_not_the_session_id(self):
        result, _, attempts = author(quiz_payload())

        attempt = attempts.list_for_learner(LEARNER)[0]
        assert Ulid.parse(attempt.attempt_id)
        assert attempt.attempt_id != result.quiz_session_id

    def test_the_authoring_call_is_stamped_with_its_message_id_and_usage(self):
        _, _, attempts = author(
            quiz_payload(), input_tokens=910, output_tokens=37
        )

        attempt = attempts.list_for_learner(LEARNER)[0]
        assert len(attempt.model_calls) == 1
        call = attempt.model_calls[0]
        assert call.call_type == CallType.AUTHOR_SKELETON.value
        assert call.message_id == "msg_01AUTHORING"
        assert call.usage.cache_read_input_tokens == 512
        assert call.usage.cache_creation_input_tokens == 0
        # #55: the plain input/output counts must survive onto the record
        # too, not just the two cache counters.
        assert call.usage.input_tokens == 910
        assert call.usage.output_tokens == 37

    def test_the_attempt_records_the_model_the_effort_and_the_cadence(self):
        service, _, attempts = build(quiz_payload())
        service.author(
            "Why does heat flow?",
            LEARNER,
            mode=DifficultyMode.NOVICE,
            probe_cadence=ProbeCadence.OFF,
        )

        attempt = attempts.list_for_learner(LEARNER)[0]
        assert attempt.model_id == authoring.MODEL_ID == "claude-opus-5"
        assert attempt.effort == authoring.EFFORT == "low"
        assert attempt.prompt_version == authoring.PROMPT_VERSION
        # Acceptance 21: an attempt authored with probes off is identifiable as
        # suppressed from the record alone.
        assert attempt.probe_cadence_at_authoring is ProbeCadence.OFF

    def test_the_created_at_comes_from_the_injected_clock(self):
        _, _, attempts = author(quiz_payload())

        attempt = attempts.list_for_learner(LEARNER)[0]
        assert attempt.created_at == Ulid.parse(attempt.attempt_id).timestamp
        assert int(attempt.created_at.timestamp() * 1000) == FROZEN_MILLIS


class TestTheDirectAnswerBranch:
    def test_a_safety_relevant_inquiry_returns_a_direct_answer(self):
        # Master spec acceptance 4. The parse succeeds on the direct-answer
        # shape, which carries no blanks at all.
        result, _, _ = author(
            DIRECT_ANSWER_PAYLOAD, inquiry="I have chest pain, what do I do"
        )

        assert isinstance(result, DirectAnswer)
        assert result.answer.startswith("Call emergency services")
        assert result.topic == "chest pain"
        assert result.queued_topics == ("how the heart's conduction system works",)

    def test_a_direct_answer_persists_no_attempt(self):
        _, _, attempts = author(DIRECT_ANSWER_PAYLOAD)

        assert attempts.list_for_learner(LEARNER) == ()

    def test_a_direct_answer_still_costs_exactly_one_call(self):
        _, client, _ = author(DIRECT_ANSWER_PAYLOAD)

        assert client.call_count() == 1

    def test_a_direct_answer_needs_no_queued_topics(self):
        result, _, _ = author(
            {
                "type": "direct_answer",
                "topic": "an outage",
                "answer": "Roll back the deploy.",
            }
        )

        assert result.queued_topics == ()


class TestTheBlankRangeComesFromTheRegistry:
    """Master spec acceptance 25: the range is the mode's, read from the
    registry, and it rejects before the 20-blank storage cap."""

    def test_too_many_blanks_for_novice_is_rejected(self):
        with pytest.raises(QuizValidationError) as caught:
            author(quiz_payload(("b1", "b2", "b3")))

        assert "blank_range 1-2" in str(caught.value)

    def test_too_few_blanks_for_advanced_is_rejected(self):
        with pytest.raises(QuizValidationError) as caught:
            author(
                quiz_payload(("b1",), blank_builder=advanced_blank_payload),
                mode=DifficultyMode.ADVANCED,
            )

        assert "blank_range 4-6" in str(caught.value)

    def test_the_range_is_read_from_the_registry_it_is_handed(self):
        # The number cannot have been hardcoded: this registry says a novice
        # quiz takes three to five blanks, and three is now admissible.
        registry = ModeRegistry(
            {
                DifficultyMode.NOVICE: ModePolicy(
                    authoring_schema_fragment=NOVICE_POLICY.authoring_schema_fragment,
                    grading_strategy=NOVICE_POLICY.grading_strategy,
                    validate_blank=NOVICE_POLICY.validate_blank,
                    render_hint=NOVICE_POLICY.render_hint,
                    blank_range=BlankRange(3, 5),
                    probe_failure_behavior=NOVICE_POLICY.probe_failure_behavior,
                )
            }
        )

        result, _, attempts = author(
            quiz_payload(("b1", "b2", "b3")), registry=registry
        )

        assert isinstance(result, Quiz)
        assert len(attempts.list_for_learner(LEARNER)) == 1

    def test_a_rejected_quiz_persists_nothing(self):
        attempts = InMemoryAttemptRepository()
        with pytest.raises(QuizValidationError):
            author(quiz_payload(("b1", "b2", "b3")), attempts=attempts)

        assert attempts.list_for_learner(LEARNER) == ()

    def test_the_conditional_validator_still_bites(self):
        # Acceptance 27, reached through `author` rather than restated here.
        blank = novice_blank_payload()
        blank["options"] = [{"option_id": "o1", "text": "entropy"}]
        with pytest.raises(QuizValidationError) as caught:
            author(quiz_payload(blanks=[blank]))

        assert "at least two options" in str(caught.value)


class TestMalformedResponses:
    """A malformed response is a clean domain failure, never an exception from
    deep inside a dataclass."""

    def test_content_that_is_not_json(self):
        with pytest.raises(AuthoringParseError) as caught:
            author("not json at all")

        assert "not JSON" in str(caught.value)

    def test_content_that_is_json_but_not_an_object(self):
        with pytest.raises(AuthoringParseError):
            author("[1, 2, 3]")

    def test_a_missing_discriminator(self):
        payload = quiz_payload()
        del payload["type"]
        with pytest.raises(AuthoringParseError) as caught:
            author(payload)

        assert "type" in str(caught.value)

    def test_an_unknown_discriminator(self):
        with pytest.raises(AuthoringParseError) as caught:
            author(quiz_payload(type="essay"))

        assert "essay" in str(caught.value)

    def test_a_missing_required_field(self):
        # The recap is *not* one of these any more - it rides the pedagogy
        # payload (#9), so a skeleton without one is well formed.
        payload = quiz_payload()
        del payload["blanks"]
        with pytest.raises(AuthoringParseError) as caught:
            author(payload)

        assert "blanks" in str(caught.value)

    def test_a_field_of_the_wrong_type(self):
        with pytest.raises(AuthoringParseError) as caught:
            author(quiz_payload(topic=17))

        assert "topic" in str(caught.value)

    def test_an_explanation_that_is_not_a_list(self):
        with pytest.raises(AuthoringParseError) as caught:
            author(quiz_payload(explanation="Heat flows [[b1]] upwards."))

        assert "explanation" in str(caught.value)

    def test_an_unknown_explanation_segment_type(self):
        with pytest.raises(AuthoringParseError) as caught:
            author(
                quiz_payload(
                    explanation=[
                        {"type": "blank", "blank_id": "b1"},
                        {"type": "image", "url": "http://example.invalid/x.png"},
                    ]
                )
            )

        assert "image" in str(caught.value)

    def test_a_blank_referenced_but_never_declared(self):
        # `Quiz.__post_init__` raises a bare ValueError; the caller sees a
        # parse failure, not a dataclass's internals.
        with pytest.raises(AuthoringParseError) as caught:
            author(quiz_payload(blanks=[novice_blank_payload("b9")]))

        assert "b9" in str(caught.value) or "b1" in str(caught.value)

    def test_duplicate_blank_ids(self):
        with pytest.raises(AuthoringParseError):
            author(
                quiz_payload(
                    ("b1",), blanks=[novice_blank_payload(), novice_blank_payload()]
                )
            )

    def test_an_option_of_the_wrong_shape(self):
        blank = novice_blank_payload()
        blank["options"] = ["entropy", "enthalpy"]
        with pytest.raises(AuthoringParseError):
            author(quiz_payload(blanks=[blank]))

    def test_a_direct_answer_missing_its_answer(self):
        with pytest.raises(AuthoringParseError) as caught:
            author({"type": "direct_answer", "topic": "chest pain"})

        assert "answer" in str(caught.value)

    def test_a_malformed_response_persists_nothing(self):
        attempts = InMemoryAttemptRepository()
        with pytest.raises(AuthoringParseError):
            author("not json at all", attempts=attempts)

        assert attempts.list_for_learner(LEARNER) == ()


class TestThePromptItAssembles:
    def test_the_inquiry_rides_the_volatile_tail_and_nothing_above_it(self):
        inquiry = "why does a heat pump beat a resistive heater"
        _, client, _ = author(quiz_payload(), inquiry=inquiry)

        segments = client.calls[0].segments
        assert inquiry in segments.volatile_tail.text
        assert inquiry not in segments.segment_1.text
        assert inquiry not in segments.segment_2.text

    def test_the_cache_breakpoints_survive_the_amended_tail(self):
        _, client, _ = author(quiz_payload())

        segments = client.calls[0].segments
        assert segments.breakpoints() == (0, 1)
        assert segments.volatile_tail.cache_control is False
        assert [segment.role for segment in segments.segments] == [
            SegmentRole.SEGMENT_1,
            SegmentRole.SEGMENT_2,
            SegmentRole.VOLATILE_TAIL,
        ]

    def test_segment_1_is_byte_identical_across_two_learners(self):
        # The invariant that pays for the cache (ADR-0006), asserted through
        # the caller rather than only through `assemble`.
        first, client_one, _ = build(quiz_payload())
        first.author("why does heat flow", "learner-ada", mode=DifficultyMode.NOVICE)
        second, client_two, _ = build(quiz_payload())
        second.author(
            "why does heat flow",
            "learner-basho",
            mode=DifficultyMode.NOVICE,
            profile=LearnerProfile(
                learner_id="learner-basho",
                ledger={"topics/haiku": 3},
                narrative="Basho counts syllables well.",
            ),
        )

        assert (
            client_one.calls[0].segments.segment_1.text
            == client_two.calls[0].segments.segment_1.text
        )

    def test_the_supplied_profile_reaches_segment_2(self):
        service, client, _ = build(quiz_payload())
        service.author(
            "why does heat flow",
            LEARNER,
            mode=DifficultyMode.NOVICE,
            profile=LearnerProfile(
                learner_id=LEARNER,
                ledger={"topics/monads": 7},
                narrative="Ada is fluent in category theory.",
            ),
        )

        segment_2 = client.calls[0].segments.segment_2.text
        assert "topics/monads" in segment_2
        assert "category theory" in segment_2

    def test_no_profile_supplied_still_names_the_learner(self):
        _, client, _ = author(quiz_payload())

        assert LEARNER in client.calls[0].segments.segment_2.text

    def test_the_modes_blank_bound_reaches_segment_2(self):
        # #72: the bound cannot ride the schema fragment — array-count keywords
        # are outside the structured-output subset — so the prompt has to say
        # it, and segment 2 is the only position ADR-0006 leaves open.
        _, novice_client, _ = author(quiz_payload())
        assert "1 and 2" in novice_client.calls[0].segments.segment_2.text

        _, advanced_client, _ = author(
            quiz_payload(
                ("b1", "b2", "b3", "b4"), blank_builder=advanced_blank_payload
            ),
            mode=DifficultyMode.ADVANCED,
        )
        assert "4 and 6" in advanced_client.calls[0].segments.segment_2.text

    def test_the_bound_is_the_registrys_and_not_a_hardcoded_number(self):
        # Same argument as TestTheBlankRangeComesFromTheRegistry: a registry
        # saying 3-5 must produce 3-5 in the prompt, or the number was baked in
        # somewhere other than the mode policy.
        registry = ModeRegistry(
            {
                DifficultyMode.NOVICE: ModePolicy(
                    authoring_schema_fragment=NOVICE_POLICY.authoring_schema_fragment,
                    grading_strategy=NOVICE_POLICY.grading_strategy,
                    validate_blank=NOVICE_POLICY.validate_blank,
                    render_hint=NOVICE_POLICY.render_hint,
                    blank_range=BlankRange(3, 5),
                    probe_failure_behavior=NOVICE_POLICY.probe_failure_behavior,
                )
            }
        )

        _, client, _ = author(quiz_payload(("b1", "b2", "b3")), registry=registry)

        assert "3 and 5" in client.calls[0].segments.segment_2.text

    def test_the_bound_never_reaches_segment_1(self):
        # It varies by mode, and segment 1 is one cache prefix for the whole
        # workspace (ADR-0006).
        _, novice_client, _ = author(quiz_payload())
        _, advanced_client, _ = author(
            quiz_payload(
                ("b1", "b2", "b3", "b4"), blank_builder=advanced_blank_payload
            ),
            mode=DifficultyMode.ADVANCED,
        )

        assert (
            novice_client.calls[0].segments.segment_1.text
            == advanced_client.calls[0].segments.segment_1.text
        )


class TestGuards:
    def test_an_empty_inquiry_is_refused_before_the_model_is_consulted(self):
        service, client, _ = build(quiz_payload())

        with pytest.raises(ValueError):
            service.author("   ", LEARNER, mode=DifficultyMode.NOVICE)

        client.assert_never_called()

    def test_an_unregistered_mode_is_a_wiring_error(self):
        registry = default_registry()
        service, _, _ = build(quiz_payload(), registry=registry)

        with pytest.raises(KeyError):
            service.author("why does heat flow", LEARNER, mode="expert")


# --- The split (issue #9) ----------------------------------------------------
#
# Spec `docs/specs/skeleton-and-pedagogy-split.md`. The skeleton is the only
# blocking call; the pedagogy payload lands while the learner reads.


def novice_skeleton_blank_payload(blank_id: str = "b1") -> dict:
    """A blank as the skeleton call really leaves it: no pedagogy at all."""
    return {
        "blank_id": blank_id,
        "options": [
            {"option_id": "o1", "text": "entropy"},
            {"option_id": "o2", "text": "enthalpy"},
        ],
        "correct_option_id": "o1",
    }


def advanced_skeleton_blank_payload(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "rubric": "Names the quantity the second law bounds.",
    }


def skeleton_payload(blank_ids=("b1",), *, blank_builder=None, **overrides) -> dict:
    """A skeleton response: no reinforcement, no hints, no recap."""
    payload = quiz_payload(
        blank_ids,
        blank_builder=blank_builder or novice_skeleton_blank_payload,
    )
    del payload["recap"]
    payload.update(overrides)
    return payload


def pedagogy_payload(blank_ids=("b1",), **overrides) -> dict:
    payload = {
        "recap": "Entropy never decreases in an isolated system.",
        "blanks": [
            {
                "blank_id": blank_id,
                "reinforcement": "Entropy is the one that never decreases.",
                "probe_question": "How did you arrive at that?",
                "hints": [
                    "Think about disorder.",
                    "It is the quantity the second law bounds.",
                    "The word is entropy.",
                ],
            }
            for blank_id in blank_ids
        ],
    }
    payload.update(overrides)
    return payload


def _content(payload) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload)


def build_both(skeleton, pedagogy, *, registry=None, attempts=None):
    """A service whose stub answers both authoring calls."""
    client = RecordingModelClient(
        {
            CallType.AUTHOR_SKELETON: [
                ModelResponse(
                    content=_content(skeleton),
                    message_id="msg_01SKELETON",
                    cache_read_input_tokens=512,
                    input_tokens=900,
                    output_tokens=310,
                )
            ],
            CallType.AUTHOR_PEDAGOGY: [
                ModelResponse(
                    content=_content(pedagogy),
                    message_id="msg_02PEDAGOGY",
                    cache_read_input_tokens=256,
                    input_tokens=1200,
                    output_tokens=640,
                )
            ],
        }
    )
    attempts = attempts if attempts is not None else InMemoryAttemptRepository()
    service = QuizAuthoring(
        model_client=client,
        attempts=attempts,
        registry=registry,
        clock=frozen_clock,
    )
    return service, client, attempts


def _author(service, mode=DifficultyMode.NOVICE, learner=LEARNER):
    return service.author("Why does heat flow?", learner, mode=mode)


def author_both(skeleton=None, pedagogy=None, *, mode=DifficultyMode.NOVICE, **kwargs):
    """Author, then let the payload land - the two calls, in order."""
    service, client, attempts = build_both(
        skeleton_payload() if skeleton is None else skeleton,
        pedagogy_payload() if pedagogy is None else pedagogy,
        **kwargs,
    )
    quiz = _author(service, mode)
    attempt = service.author_pedagogy(quiz, LEARNER)
    return quiz, attempt, client, attempts


class TestTheSkeletonAlone:
    """Acceptance 5, first half: the quiz is playable before the payload
    arrives. What #32 reported as impossible."""

    def test_a_skeleton_only_payload_authors_a_valid_quiz(self):
        service, _, _ = build_both(skeleton_payload(), pedagogy_payload())

        quiz = _author(service)

        assert isinstance(quiz, Quiz)
        assert (
            authoring.validation.validate_quiz(quiz, stage=AuthoringStage.SKELETON)
            == ()
        )

    def test_a_blank_with_absent_pedagogy_is_a_representable_state(self):
        service, _, _ = build_both(skeleton_payload(), pedagogy_payload())

        quiz = _author(service)

        assert quiz.blanks[0].hints is None
        assert quiz.blanks[0].reinforcement is None
        assert quiz.recap == ""

    def test_the_same_quiz_is_not_yet_complete(self):
        service, _, _ = build_both(skeleton_payload(), pedagogy_payload())

        assert authoring.validation.validate_quiz(_author(service))

    def test_authoring_persists_the_playable_skeleton(self):
        service, _, attempts = build_both(skeleton_payload(), pedagogy_payload())

        quiz = _author(service)

        assert attempts.list_for_learner(LEARNER)[0].quiz == quiz

    def test_authoring_fires_no_pedagogy_call(self):
        # One blocking call, and only one. The follow-up is the caller's to
        # fire off the render path.
        service, client, _ = build_both(skeleton_payload(), pedagogy_payload())

        _author(service)

        assert client.call_count() == 1
        client.assert_never_called(CallType.AUTHOR_PEDAGOGY)

    def test_a_skeleton_still_needs_its_option_bank(self):
        blank = novice_skeleton_blank_payload()
        blank["options"] = [{"option_id": "o1", "text": "entropy"}]
        service, _, _ = build_both(skeleton_payload(blanks=[blank]), pedagogy_payload())

        with pytest.raises(QuizValidationError) as caught:
            _author(service)

        assert "at least two options" in str(caught.value)

    def test_an_advanced_skeleton_still_needs_its_rubric(self):
        blanks = [advanced_skeleton_blank_payload(f"b{n}") for n in range(1, 5)]
        del blanks[2]["rubric"]
        service, _, _ = build_both(
            skeleton_payload(("b1", "b2", "b3", "b4"), blanks=blanks),
            pedagogy_payload(),
        )

        with pytest.raises(QuizValidationError) as caught:
            _author(service, DifficultyMode.ADVANCED)

        assert "rubric" in str(caught.value)

    def test_a_skeleton_that_carries_a_recap_anyway_keeps_it(self):
        # Relaxed, not forbidden: the field is optional at this stage rather
        # than rejected, so a model that sends one costs nothing.
        service, _, _ = build_both(
            skeleton_payload(recap="Entropy never decreases."), pedagogy_payload()
        )

        assert _author(service).recap == "Entropy never decreases."


class TestThePedagogyPayloadLanding:
    def test_it_merges_into_the_in_flight_attempt(self):
        _, attempt, _, attempts = author_both()

        assert attempt.quiz.recap.startswith("Entropy never decreases")
        assert attempt.quiz.blanks[0].hints == (
            "Think about disorder.",
            "It is the quantity the second law bounds.",
            "The word is entropy.",
        )
        assert attempt.quiz.blanks[0].reinforcement
        assert attempts.list_for_learner(LEARNER)[0] == attempt

    def test_the_pre_authored_probe_question_rides_the_payload(self):
        # ADR-0011: a Novice probe question is pre-authored per blank exactly
        # as the hint rungs are, which is the whole of "asking a probe costs no
        # model call" on the deterministic path (#10).
        _, attempt, _, _ = author_both()

        assert attempt.quiz.blanks[0].probe_question == "How did you arrive at that?"

    def test_a_payload_with_no_probe_question_is_tolerated(self):
        # The #9 window again: a late or partial payload costs the learner the
        # probe, not the verdict.
        payload = pedagogy_payload()
        del payload["blanks"][0]["probe_question"]
        _, attempt, _, _ = author_both(pedagogy=payload)

        assert attempt.quiz.blanks[0].probe_question is None
        assert authoring.validation.validate_quiz(attempt.quiz) == ()

    def test_the_merged_quiz_validates_as_complete(self):
        _, attempt, _, _ = author_both()

        assert authoring.validation.validate_quiz(attempt.quiz) == ()

    def test_it_is_the_second_call_and_the_only_other_one(self):
        _, _, client, _ = author_both()

        assert [call.call_type for call in client.calls] == [
            CallType.AUTHOR_SKELETON,
            CallType.AUTHOR_PEDAGOGY,
        ]

    def test_both_calls_message_ids_and_usage_persist_on_the_attempt(self):
        _, attempt, _, _ = author_both()

        calls = {call.call_type: call for call in attempt.model_calls}
        assert set(calls) == {
            CallType.AUTHOR_SKELETON.value,
            CallType.AUTHOR_PEDAGOGY.value,
        }
        skeleton = calls[CallType.AUTHOR_SKELETON.value]
        pedagogy = calls[CallType.AUTHOR_PEDAGOGY.value]
        assert skeleton.message_id == "msg_01SKELETON"
        assert pedagogy.message_id == "msg_02PEDAGOGY"
        assert skeleton.usage.input_tokens == 900
        assert skeleton.usage.output_tokens == 310
        assert pedagogy.usage.output_tokens == 640
        assert pedagogy.usage.cache_read_input_tokens == 256

    def test_an_advanced_payload_carries_only_the_recap(self):
        # ADR-0013: Advanced feedback is the reactive tutor line on the grading
        # response, so there is no per-blank pedagogy to merge.
        _, attempt, _, _ = author_both(
            skeleton=skeleton_payload(
                ("b1", "b2", "b3", "b4"),
                blank_builder=advanced_skeleton_blank_payload,
            ),
            pedagogy={"recap": "Entropy never decreases in an isolated system."},
            mode=DifficultyMode.ADVANCED,
        )

        assert attempt.quiz.recap.startswith("Entropy never decreases")
        assert authoring.validation.validate_quiz(attempt.quiz) == ()

    def test_the_prompt_carries_the_quiz_in_segment_2(self):
        _, _, client, _ = author_both()

        segments = client.calls_of(CallType.AUTHOR_PEDAGOGY)[0].segments
        assert segments.call_type is CallType.AUTHOR_PEDAGOGY
        assert "b1" in segments.segment_2.text
        assert segments.breakpoints() == (0, 1)


class TestAnswersInTheWindow:
    """Acceptance 5, second half, as far as this ticket owns it: the merge
    lands on the *stored* attempt, so anything appended while the payload was
    in flight is still there afterwards. Returning a verdict from a blank whose
    hints have not arrived is #6's ladder."""

    def test_a_guess_made_in_the_window_survives_the_merge(self):
        service, _, attempts = build_both(skeleton_payload(), pedagogy_payload())
        quiz = _author(service)

        # The learner answers before the payload lands. #6 owns the submit
        # path; this stands in for it at the repository, which is the only
        # place the race is visible from here.
        stored = attempts.list_for_learner(LEARNER)[0]
        attempts.save(stored.with_guess(_a_guess(stored)))

        attempt = service.author_pedagogy(quiz, LEARNER)

        assert len(attempt.guesses) == 1
        assert attempt.quiz.blanks[0].hints is not None

    def test_a_payload_landing_on_an_abandoned_attempt_is_dropped(self):
        # #15: the learner displaced the quiz inside the pedagogy window. The
        # attempt is closed with no `sealed_at`, so a guard reading only that
        # stamp would try to write to it and raise on the learner's behalf.
        service, client, attempts = build_both(skeleton_payload(), pedagogy_payload())
        quiz = _author(service)
        stored = attempts.list_for_learner(LEARNER)[0]
        attempts.save(stored.abandoned())

        attempt = service.author_pedagogy(quiz, LEARNER)

        assert attempt.outcome is Outcome.ABANDONED
        assert attempt.quiz.blanks[0].hints is None
        assert client.call_count(CallType.AUTHOR_PEDAGOGY) == 1

    def test_a_payload_landing_on_a_sealed_attempt_is_dropped(self):
        service, client, attempts = build_both(skeleton_payload(), pedagogy_payload())
        quiz = _author(service)
        stored = attempts.list_for_learner(LEARNER)[0]
        attempts.save(stored.sealed(stored.created_at))

        attempt = service.author_pedagogy(quiz, LEARNER)

        # Sealed means never written again (CONTEXT: Sealed), and pedagogy for
        # blanks that are all resolved has no reader.
        assert attempt.is_sealed
        assert attempt.quiz.blanks[0].hints is None
        assert client.call_count(CallType.AUTHOR_PEDAGOGY) == 1

    def test_a_payload_lands_on_the_restart_not_the_attempt_it_displaced(self):
        # The case #15 creates: the session was displaced and started again, so
        # it holds an abandoned attempt as well as the live one. The payload is
        # for the live one - the abandoned attempt is closed, so landing on it
        # would silently drop pedagogy the learner is waiting for.
        service, _, attempts = build_both(skeleton_payload(), pedagogy_payload())
        quiz = _author(service)
        live = attempts.list_for_learner(LEARNER)[0]
        earlier = Ulid.mint(clock=lambda: FROZEN_MILLIS - 60_000)
        displaced = dataclasses.replace(
            live, attempt_id=str(earlier), created_at=earlier.timestamp
        ).abandoned()
        assert displaced.session_id == live.session_id
        attempts.save(displaced)

        attempt = service.author_pedagogy(quiz, LEARNER)

        assert attempt.attempt_id == live.attempt_id
        assert attempt.quiz.blanks[0].hints is not None
        # And the abandoned record it displaced is exactly as it was.
        assert attempts.get(LEARNER, displaced.attempt_id) == displaced


class TestThePedagogyPayloadFailing:
    def test_a_payload_naming_an_unknown_blank_is_a_parse_error(self):
        service, _, attempts = build_both(skeleton_payload(), pedagogy_payload(("b9",)))
        quiz = _author(service)

        with pytest.raises(AuthoringParseError) as caught:
            service.author_pedagogy(quiz, LEARNER)

        assert "b9" in str(caught.value)
        assert attempts.list_for_learner(LEARNER)[0].quiz == quiz

    def test_a_payload_that_is_not_json_is_a_parse_error(self):
        service, _, _ = build_both(skeleton_payload(), "not json at all")
        quiz = _author(service)

        with pytest.raises(AuthoringParseError):
            service.author_pedagogy(quiz, LEARNER)

    def test_an_incomplete_merge_is_a_validation_error_and_writes_nothing(self):
        # The learner keeps the playable skeleton that was already persisted.
        service, _, attempts = build_both(
            skeleton_payload(),
            pedagogy_payload(blanks=[{"blank_id": "b1", "hints": ["one", "two"]}]),
        )
        quiz = _author(service)

        with pytest.raises(QuizValidationError) as caught:
            service.author_pedagogy(quiz, LEARNER)

        assert "three hints" in str(caught.value)
        assert attempts.list_for_learner(LEARNER)[0].quiz == quiz

    def test_a_payload_with_no_recap_is_a_validation_error(self):
        service, _, _ = build_both(skeleton_payload(), pedagogy_payload(recap="   "))
        quiz = _author(service)

        with pytest.raises(QuizValidationError) as caught:
            service.author_pedagogy(quiz, LEARNER)

        assert "recap" in str(caught.value)

    def test_a_quiz_with_no_attempt_of_its_own_is_a_key_error(self):
        service, _, _ = build_both(skeleton_payload(), pedagogy_payload())
        quiz = _author(service)

        with pytest.raises(KeyError):
            service.author_pedagogy(quiz, "learner-basho")


class TestADirectAnswerFiresNoPedagogyCall:
    def test_the_second_call_is_never_made(self):
        service, client, _ = build_both(DIRECT_ANSWER_PAYLOAD, pedagogy_payload())

        result = service.author(
            "I have chest pain, what do I do", LEARNER, mode=DifficultyMode.NOVICE
        )

        assert isinstance(result, DirectAnswer)
        assert client.call_count() == 1
        client.assert_never_called(CallType.AUTHOR_PEDAGOGY)

    def test_there_is_no_attempt_to_land_a_payload_on(self):
        service, _, attempts = build_both(DIRECT_ANSWER_PAYLOAD, pedagogy_payload())
        service.author(
            "I have chest pain, what do I do", LEARNER, mode=DifficultyMode.NOVICE
        )

        assert attempts.list_for_learner(LEARNER) == ()


def _a_guess(attempt):
    """One submitted answer, as the submit path (#6) will append it."""
    return Guess(
        blank_id=attempt.quiz.blanks[0].blank_id,
        submitted="o1",
        verdict=Verdict.CORRECT,
        attempt_ordinal=1,
        hint_rung_shown=None,
        created_at=attempt.created_at,
        graded_by=GradingStrategy.DETERMINISTIC,
    )
