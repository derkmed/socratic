"""The `ModelClient` port and its recording stub.

The port exists for one reason that a generic `complete()` would destroy: a
stub must be able to assert *this was never called*. Master spec acceptance 6
("a Novice answer returns a verdict with zero model calls") and 7 ("a full
Novice quiz with probes off makes no model calls at all after authoring") are
both that assertion, so it is tested here at the seam before anything depends
on it.

The five explicit methods are the second reason: the call inventory stays
visible in the type (CONTEXT: Call type).
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from socratic.domain import model_client as mc
from socratic.domain.model_client import (
    ModelClient,
    ModelResponse,
    NeverCalled,
    RecordingModelClient,
)
from socratic.domain import records
from socratic.domain.modes import ProbeCadence
from socratic.domain.prompting import CallType, LearnerProfile, assemble

PROFILE = LearnerProfile(learner_id="learner-1", narrative="Knows some Python.")

METHOD_NAMES = (
    "author_skeleton",
    "author_pedagogy",
    "grade_answer",
    "grade_probe",
    "fold_narrative",
)


def _segments(call_type: CallType):
    return assemble(call_type, profile=PROFILE, probe_cadence=ProbeCadence.SOMETIMES)


class TestThePort:
    def test_it_exposes_exactly_the_five_call_type_methods(self):
        exposed = {
            name
            for name in dir(ModelClient)
            if not name.startswith("_") and callable(getattr(ModelClient, name, None))
        }
        assert exposed == set(METHOD_NAMES)

    def test_there_is_no_generic_complete(self):
        # A generic method would hide the call inventory and destroy the
        # "this was never called" assertion the seam exists for.
        assert not hasattr(ModelClient, "complete")
        assert not hasattr(RecordingModelClient, "complete")

    def test_every_call_type_has_a_method_of_the_same_name(self):
        for call_type in CallType:
            assert call_type.value in METHOD_NAMES
            assert hasattr(ModelClient, call_type.value)

    @pytest.mark.parametrize("name", METHOD_NAMES)
    def test_each_method_takes_prompt_segments_and_returns_a_model_response(self, name):
        signature = inspect.signature(getattr(ModelClient, name))
        assert list(signature.parameters) == ["self", "segments"]

    def test_the_stub_satisfies_the_port(self):
        assert isinstance(RecordingModelClient(), ModelClient)

    def test_the_only_implementation_shipped_is_the_stub(self):
        # No real API implementation lives here; the Anthropic adapter is #7,
        # and it is the only module allowed to import the SDK
        # (tests/test_import_hygiene.py holds that line).
        concrete = [
            name
            for name, member in vars(mc).items()
            if inspect.isclass(member)
            and member is not ModelClient
            and not issubclass(member, BaseException)
            and all(hasattr(member, method) for method in METHOD_NAMES)
        ]
        assert concrete == ["RecordingModelClient"]


class TestRecording:
    def test_it_records_every_call_in_order(self):
        client = RecordingModelClient()
        client.author_skeleton(_segments(CallType.AUTHOR_SKELETON))
        client.grade_answer(_segments(CallType.GRADE_ANSWER))
        client.grade_answer(_segments(CallType.GRADE_ANSWER))
        assert [call.call_type for call in client.calls] == [
            CallType.AUTHOR_SKELETON,
            CallType.GRADE_ANSWER,
            CallType.GRADE_ANSWER,
        ]

    def test_it_records_the_segments_it_was_handed(self):
        client = RecordingModelClient()
        segments = _segments(CallType.GRADE_PROBE)
        client.grade_probe(segments)
        assert client.calls[0].segments is segments

    def test_calls_of_filters_by_call_type(self):
        client = RecordingModelClient()
        client.grade_answer(_segments(CallType.GRADE_ANSWER))
        client.fold_narrative(_segments(CallType.FOLD_NARRATIVE))
        client.grade_answer(_segments(CallType.GRADE_ANSWER))
        assert len(client.calls_of(CallType.GRADE_ANSWER)) == 2
        assert len(client.calls_of(CallType.AUTHOR_PEDAGOGY)) == 0

    def test_call_count_counts_everything_by_default(self):
        client = RecordingModelClient()
        client.author_skeleton(_segments(CallType.AUTHOR_SKELETON))
        client.author_pedagogy(_segments(CallType.AUTHOR_PEDAGOGY))
        assert client.call_count() == 2
        assert client.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_a_call_is_rejected_if_the_segments_serve_another_call_type(self):
        # A stub that quietly accepts mismatched segments would let a wiring
        # bug through the exact seam that exists to catch it.
        client = RecordingModelClient()
        with pytest.raises(ValueError):
            client.grade_answer(_segments(CallType.FOLD_NARRATIVE))


class TestCannedResponses:
    def test_it_serves_the_queued_response_for_the_call_type(self):
        response = ModelResponse(content='{"verdict": "correct"}', message_id="msg_1")
        client = RecordingModelClient(responses={CallType.GRADE_ANSWER: [response]})
        assert client.grade_answer(_segments(CallType.GRADE_ANSWER)) is response

    def test_it_serves_queued_responses_in_order_then_repeats_the_last(self):
        first = ModelResponse(content="1", message_id="msg_1")
        second = ModelResponse(content="2", message_id="msg_2")
        client = RecordingModelClient(
            responses={CallType.GRADE_ANSWER: [first, second]}
        )
        segments = _segments(CallType.GRADE_ANSWER)
        assert client.grade_answer(segments) is first
        assert client.grade_answer(segments) is second
        assert client.grade_answer(segments) is second

    def test_an_unqueued_call_type_gets_a_placeholder_rather_than_an_error(self):
        client = RecordingModelClient()
        response = client.fold_narrative(_segments(CallType.FOLD_NARRATIVE))
        assert isinstance(response, ModelResponse)
        assert response.message_id


class TestNeverCalled:
    """The assertion the seam exists for (master spec acceptance 6 and 7)."""

    def test_assert_never_called_passes_on_an_untouched_stub(self):
        RecordingModelClient().assert_never_called()

    def test_assert_never_called_fails_after_one_call(self):
        client = RecordingModelClient()
        client.grade_answer(_segments(CallType.GRADE_ANSWER))
        with pytest.raises(AssertionError) as caught:
            client.assert_never_called()
        assert "grade_answer" in str(caught.value)

    def test_assert_never_called_can_be_scoped_to_one_call_type(self):
        client = RecordingModelClient()
        client.author_skeleton(_segments(CallType.AUTHOR_SKELETON))
        client.assert_never_called(CallType.GRADE_ANSWER)
        with pytest.raises(AssertionError):
            client.assert_never_called(CallType.AUTHOR_SKELETON)

    def test_fail_if_called_raises_at_the_moment_of_the_call(self):
        # Raising at the call site rather than after the fact is the point:
        # the traceback names the culprit instead of the test's last line.
        client = RecordingModelClient(fail_if_called=True)
        with pytest.raises(NeverCalled) as caught:
            client.grade_answer(_segments(CallType.GRADE_ANSWER))
        assert "grade_answer" in str(caught.value)

    def test_never_called_is_an_assertion_error_so_pytest_reports_it_as_one(self):
        assert issubclass(NeverCalled, AssertionError)

    @pytest.mark.parametrize("name", METHOD_NAMES)
    def test_every_one_of_the_five_methods_honours_fail_if_called(self, name):
        client = RecordingModelClient(fail_if_called=True)
        call_type = CallType(name)
        with pytest.raises(NeverCalled):
            getattr(client, name)(_segments(call_type))

    def test_a_failed_call_is_still_recorded(self):
        client = RecordingModelClient(fail_if_called=True)
        with pytest.raises(NeverCalled):
            client.grade_probe(_segments(CallType.GRADE_PROBE))
        assert client.call_count() == 1


class TestModelResponse:
    def test_it_carries_the_payload_and_the_host_annotation(self):
        # CONTEXT: Host annotations — the `message.id` is the audit link from a
        # stored quiz back to the exact API calls that produced it.
        response = ModelResponse(content="{}", message_id="msg_abc")
        assert response.content == "{}"
        assert response.message_id == "msg_abc"

    def test_the_cache_counters_default_to_zero(self):
        # #7's build gate reads these: `cache_read_input_tokens > 0` per call
        # type is what proves each segment 1 cleared the 512-token floor.
        response = ModelResponse(content="{}", message_id="msg_abc")
        assert response.cache_read_input_tokens == 0
        assert response.cache_creation_input_tokens == 0

    def test_the_plain_counters_default_to_zero(self):
        # Additive with defaults so every existing construction — the stub's
        # canned response included — keeps working untouched (#33).
        response = ModelResponse(content="{}", message_id="msg_abc")
        assert response.input_tokens == 0
        assert response.output_tokens == 0

    def test_it_carries_all_four_counters(self):
        response = ModelResponse(
            content="{}",
            message_id="msg_abc",
            input_tokens=1200,
            output_tokens=340,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=64,
        )
        assert response.input_tokens == 1200
        assert response.output_tokens == 340
        assert response.cache_read_input_tokens == 900
        assert response.cache_creation_input_tokens == 64

    def test_the_existing_fields_keep_their_positions(self):
        # #32 and #35 both build `ModelResponse` on unmerged branches; the new
        # fields go on the end so a positional or keyword construction written
        # against `main` today still means the same thing after a rebase.
        names = [field.name for field in dataclasses.fields(ModelResponse)]
        assert names[:4] == [
            "content",
            "message_id",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ]

    def test_it_is_frozen(self):
        response = ModelResponse(content="{}", message_id="msg_abc")
        with pytest.raises(Exception):
            response.content = "other"  # type: ignore[misc]


class TestTokenUsageMapping:
    def test_it_maps_all_four_counters_onto_a_token_usage(self):
        # #33: without this the caller has to reassemble four loose counters,
        # and `QuizAuthoring` was inventing two of them as zeros.
        response = ModelResponse(
            content="{}",
            message_id="msg_abc",
            input_tokens=1200,
            output_tokens=340,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=64,
        )
        assert response.token_usage() == records.TokenUsage(
            input_tokens=1200,
            output_tokens=340,
            cache_creation_input_tokens=64,
            cache_read_input_tokens=900,
        )

    def test_an_unpopulated_response_maps_to_all_zeros(self):
        assert ModelResponse(
            content="{}", message_id="msg_abc"
        ).token_usage() == records.TokenUsage(input_tokens=0, output_tokens=0)
