"""The Anthropic adapter, tested without spending a cent.

Two seams, per `docs/specs/anthropic-adapter.md`:

* `build_request` is a pure function, so every wire invariant ADR-0014 fixes —
  the model id, `cache_control` placement, `effort`, schema-constrained output,
  thinking not disabled — is a property of a dict.
* `AnthropicModelClient` takes its SDK client by injection, so a **fake** with a
  `.messages.create(**kwargs)` stands in for the real one. What is asserted is
  the request the adapter constructs and the `ModelResponse` it maps back.

The live half of the ticket — `cache_read_input_tokens > 0` per call type — is
`tests/test_anthropic_cache_gate.py`, which cannot run without credentials.
"""

import types

import pytest

pytest.importorskip(
    "anthropic",
    reason="the adapter's own tests need the optional `anthropic` extra "
    "installed; the domain is deliberately testable without it",
)

from socratic.adapters import anthropic_client  # noqa: E402
from socratic.domain import model_client  # noqa: E402
from socratic.domain import modes  # noqa: E402
from socratic.domain import prompting  # noqa: E402
from tests.test_output_schemas import object_nodes  # noqa: E402

CallType = prompting.CallType

ADA = prompting.LearnerProfile(
    learner_id="learner-ada",
    ledger={"topics/monads": 3},
    narrative="Ada is fluent in recursion.",
)


def segments_for(call_type: CallType) -> prompting.PromptSegments:
    return prompting.assemble(
        call_type,
        profile=ADA,
        probe_cadence=modes.ProbeCadence.SOMETIMES,
    )


# --- The fake SDK client -----------------------------------------------------


class FakeMessages:
    """Stands in for `client.messages`. Records what it was handed."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return self.response


class FakeAnthropic:
    """An SDK client with the one method the adapter uses."""

    def __init__(
        self,
        *,
        message_id: str = "msg_fake_01",
        content: str = '{"ok": true}',
        cache_read: int = 4096,
        cache_creation: int = 0,
    ) -> None:
        response = types.SimpleNamespace(
            id=message_id,
            stop_reason="end_turn",
            content=[types.SimpleNamespace(type="text", text=content)],
            usage=types.SimpleNamespace(
                input_tokens=12,
                output_tokens=34,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
            ),
        )
        self.messages = FakeMessages(response)

    @property
    def last_request(self) -> dict:
        return self.messages.requests[-1]


def build(call_type: CallType = CallType.GRADE_ANSWER, **kwargs) -> dict:
    return anthropic_client.build_request(segments_for(call_type), **kwargs)


# --- The wire request --------------------------------------------------------


class TestTheModelComesFromAnAdminValve:
    def test_it_defaults_to_claude_opus_5(self):
        # ADR-0014: chosen for its 512-token minimum cacheable prefix.
        assert anthropic_client.Valves().model == "claude-opus-5"
        assert build()["model"] == "claude-opus-5"

    def test_an_admin_can_change_it_for_the_whole_workspace(self):
        valves = anthropic_client.Valves(model="claude-sonnet-5")
        assert build(valves=valves)["model"] == "claude-sonnet-5"

    def test_no_per_learner_seam_exists_to_change_it(self):
        # CONTEXT: Model — "never `UserValves` (caches are model-scoped, so a
        # per-learner choice would fragment every segment 1)". The assertion is
        # structural: there is no UserValves here, and none of the five methods
        # takes anything but the assembled segments.
        assert not hasattr(anthropic_client, "UserValves")
        client = anthropic_client.AnthropicModelClient(client=FakeAnthropic())
        for call_type in CallType:
            method = getattr(client, call_type.value)
            names = list(inspect_parameters(method))
            assert names == ["segments"], (call_type, names)


def inspect_parameters(method):
    import inspect

    return [
        name
        for name in inspect.signature(method).parameters
        if name not in ("self",)
    ]


class TestEffort:
    def test_it_is_low_for_every_call_type(self):
        # ADR-0014: "Effort is `low` everywhere for now."
        for call_type in CallType:
            assert build(call_type)["output_config"]["effort"] == "low"

    def test_it_is_a_per_call_type_knob(self):
        # ADR-0014: a per-call-type knob set uniformly "until measurement
        # justifies raising one; `author_skeleton` is the likeliest first
        # candidate". Raising one must not touch the other four.
        valves = anthropic_client.Valves(
            effort={
                **anthropic_client.Valves().effort,
                CallType.AUTHOR_SKELETON: "high",
            }
        )
        assert build(CallType.AUTHOR_SKELETON, valves=valves)["output_config"][
            "effort"
        ] == "high"
        assert build(CallType.GRADE_ANSWER, valves=valves)["output_config"][
            "effort"
        ] == "low"

    def test_it_is_constant_across_requests_of_one_call_type(self):
        # ADR-0014: "changing it invalidates that call type's cache prefix, so
        # it is configuration, never a per-request decision". Nothing about a
        # request can move it, so two requests differing in everything else
        # still agree on effort.
        one = build(CallType.GRADE_ANSWER)
        other = anthropic_client.build_request(
            prompting.assemble(
                CallType.GRADE_ANSWER,
                profile=prompting.LearnerProfile(learner_id="someone-else"),
                probe_cadence=modes.ProbeCadence.ALWAYS,
                guesses=("blank 1: monad", "blank 1: functor"),
                current_blank_id="b2",
                current_guess="endofunctor",
            )
        )
        assert one["output_config"]["effort"] == other["output_config"]["effort"]


class TestThinking:
    def test_it_is_never_disabled(self):
        # ADR-0014: disabling it on Opus 5 leaks `<thinking>` tags and writes
        # tool calls into visible text. Lowering effort is the lever instead.
        for call_type in CallType:
            thinking = build(call_type)["thinking"]
            assert thinking["type"] == "adaptive"
            assert thinking["type"] != "disabled"

    def test_no_valve_can_disable_it(self):
        assert not any(
            "thinking" in field for field in anthropic_client.Valves.__annotations__
        )


class TestCacheControl:
    def test_it_lands_on_segments_1_and_2_and_not_the_tail(self):
        request = build()
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
        blocks = request["messages"][0]["content"]
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in blocks[1]

    def test_the_marker_is_read_off_the_assembled_segment(self):
        # The assembler owns where the breakpoints are; the adapter must not
        # hardcode them, or the two can silently disagree.
        segments = segments_for(CallType.GRADE_ANSWER)
        unmarked = prompting.PromptSegments(
            call_type=segments.call_type,
            segments=tuple(
                prompting.PromptSegment(
                    role=segment.role, text=segment.text, cache_control=False
                )
                for segment in segments.segments
            ),
        )
        request = anthropic_client.build_request(unmarked)
        assert "cache_control" not in request["system"][0]
        assert all(
            "cache_control" not in block
            for block in request["messages"][0]["content"]
        )

    def test_segment_1_leads_the_prefix_and_the_tail_comes_last(self):
        # Render order is tools -> system -> messages, so segment 1 in `system`
        # and the tail last is exactly ADR-0006's prefix order.
        segments = segments_for(CallType.GRADE_ANSWER)
        request = anthropic_client.build_request(segments)
        assert request["system"][0]["text"] == segments.segment_1.text
        blocks = request["messages"][0]["content"]
        assert blocks[0]["text"] == segments.segment_2.text
        assert blocks[1]["text"] == segments.volatile_tail.text
        assert request["messages"][0]["role"] == "user"


class TestStructuredOutput:
    def test_every_call_type_constrains_its_output_to_a_json_schema(self):
        # An object or a union of them: ADR-0004 puts a union at the top level
        # of the authoring response, so "the top node is an object" was never
        # the rule. What every call must have is a schema at all — a request
        # with no `output_config.format` is an unconstrained request.
        for call_type in CallType:
            fmt = build(call_type)["output_config"]["format"]
            assert fmt["type"] == "json_schema"
            assert list(object_nodes(fmt["schema"])), call_type

    def test_every_schema_is_strict(self):
        # The SDK spells "strict" structurally on `output_config.format`:
        # `additionalProperties: false` plus a complete `required` list. See the
        # PR's note on where ADR-0014's `strict: true` wording lands.
        #
        # Asserted at *every* object node rather than only the top one, which
        # is what the API asks for and what the top-only version silently
        # stopped covering once the schemas grew nested objects.
        for call_type, schema in anthropic_client.DEFAULT_OUTPUT_SCHEMAS.items():
            for node in object_nodes(schema):
                assert node["additionalProperties"] is False, call_type
                assert sorted(node["required"]) == sorted(node["properties"]), (
                    call_type
                )

    def test_the_five_call_types_have_five_different_schemas(self):
        rendered = {
            call_type: repr(schema)
            for call_type, schema in anthropic_client.DEFAULT_OUTPUT_SCHEMAS.items()
        }
        assert set(rendered) == set(CallType)
        assert len(set(rendered.values())) == len(CallType)

    def test_the_schema_can_be_overridden_per_call_type(self):
        # The injection point stays. What it overrides is now a composed
        # default rather than a placeholder — a caller that knows its mode
        # passes `output_schemas.for_mode(mode)` through here (#66).
        mine = {
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
            "additionalProperties": False,
        }
        request = build(CallType.GRADE_ANSWER, schema=mine)
        assert request["output_config"]["format"]["schema"] == mine


class TestTheFloorProxy:
    @pytest.mark.parametrize("call_type", list(CallType))
    def test_each_segment_1_clears_a_proxy_for_the_512_token_floor(
        self, call_type
    ):
        # NOT the measurement acceptance criterion 2 asks for — that needs
        # `count_tokens`, which is a live call. This is a lower-bound proxy:
        # assuming at most 4 characters per token, 2048 characters cannot be
        # fewer than 512 tokens. Cheap, offline, and enough to catch a segment 1
        # that has been trimmed into the silent-no-cache zone.
        assumed_chars_per_token = 4
        text = segments_for(call_type).segment_1.text
        floor_in_chars = 512 * assumed_chars_per_token
        assert len(text) >= floor_in_chars, (
            f"{call_type.value}'s segment 1 is {len(text)} characters, under the "
            f"{floor_in_chars}-character proxy for the 512-token floor"
        )


# --- The adapter ------------------------------------------------------------


class TestTheFiveMethods:
    @pytest.mark.parametrize("call_type", list(CallType))
    def test_each_one_sends_its_own_call_type(self, call_type):
        fake = FakeAnthropic()
        client = anthropic_client.AnthropicModelClient(client=fake)
        getattr(client, call_type.value)(segments_for(call_type))
        sent = fake.last_request
        assert sent["system"][0]["text"] == segments_for(call_type).segment_1.text
        assert (
            sent["output_config"]["format"]["schema"]
            == anthropic_client.DEFAULT_OUTPUT_SCHEMAS[call_type]
        )

    def test_it_satisfies_the_model_client_protocol(self):
        client = anthropic_client.AnthropicModelClient(client=FakeAnthropic())
        assert isinstance(client, model_client.ModelClient)

    def test_mismatched_segments_are_rejected(self):
        # The same wiring check `RecordingModelClient` makes: handing
        # `grade_answer` segments assembled for `fold_narrative` is a bug, and
        # the seam exists to catch it.
        client = anthropic_client.AnthropicModelClient(client=FakeAnthropic())
        with pytest.raises(ValueError):
            client.grade_answer(segments_for(CallType.FOLD_NARRATIVE))


class TestTheResponse:
    def test_the_message_id_comes_back_for_persistence(self):
        # CONTEXT: Host annotations — the audit link from a stored quiz back to
        # the exact API calls that produced it.
        fake = FakeAnthropic(message_id="msg_01ABC")
        client = anthropic_client.AnthropicModelClient(client=fake)
        assert client.grade_answer(segments_for(CallType.GRADE_ANSWER)).message_id == (
            "msg_01ABC"
        )

    def test_per_call_cache_usage_comes_back(self):
        fake = FakeAnthropic(cache_read=1234, cache_creation=567)
        client = anthropic_client.AnthropicModelClient(client=fake)
        response = client.grade_answer(segments_for(CallType.GRADE_ANSWER))
        assert response.cache_read_input_tokens == 1234
        assert response.cache_creation_input_tokens == 567

    def test_usage_counters_absent_from_the_response_read_as_zero(self):
        fake = FakeAnthropic()
        fake.messages.response.usage = types.SimpleNamespace(input_tokens=1)
        client = anthropic_client.AnthropicModelClient(client=fake)
        response = client.grade_answer(segments_for(CallType.GRADE_ANSWER))
        assert response.cache_read_input_tokens == 0
        assert response.cache_creation_input_tokens == 0

    def test_the_content_is_the_structured_payload_verbatim(self):
        # Parsing it against the call's schema belongs to the caller that knows
        # the schema (`model_client.ModelResponse`).
        fake = FakeAnthropic(content='{"blanks": []}')
        client = anthropic_client.AnthropicModelClient(client=fake)
        response = client.grade_answer(segments_for(CallType.GRADE_ANSWER))
        assert response.content == '{"blanks": []}'

    def test_a_refusal_is_not_read_as_content(self):
        # `stop_reason: "refusal"` means the output may not match the schema, so
        # returning it as though it did would hand a parse error to a caller
        # with no way to tell the two apart.
        fake = FakeAnthropic(content="")
        fake.messages.response.stop_reason = "refusal"
        fake.messages.response.content = []
        client = anthropic_client.AnthropicModelClient(client=fake)
        with pytest.raises(anthropic_client.ModelRefused):
            client.grade_answer(segments_for(CallType.GRADE_ANSWER))


class TestMaxTokens:
    def test_it_is_set_and_admin_configurable(self):
        assert build()["max_tokens"] == anthropic_client.Valves().max_tokens
        valves = anthropic_client.Valves(max_tokens=2048)
        assert build(valves=valves)["max_tokens"] == 2048
