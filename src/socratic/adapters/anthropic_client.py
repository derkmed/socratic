"""The Anthropic adapter — the only module that imports the SDK.

`assemble` stops at a `PromptSegments`: the ordered segments with breakpoint
markers, deliberately not a wire request, so that ADR-0006's invariants stay
assertable without the network. This module is where that value becomes a
request.

The translation is one pure function, `build_request`, kept separate from the
sending so every wire invariant ADR-0014 fixes is a property of a dict:

```
system   = [ segment 1 ]                          <- cache_control if marked
messages = [ user: [ segment 2, volatile tail ] ]  <- cache_control if marked
output_config = { effort, format: json_schema }
thinking = { type: "adaptive" }
```

Segment 1 goes in `system` and the rest in the one user turn because the API
renders `tools -> system -> messages`; that ordering *is* ADR-0006's prefix
order. The `cache_control` marker is read off each `PromptSegment` rather than
hardcoded here, so the assembler stays the single authority on where the
breakpoints are.

**Model and effort are admin configuration, never per-learner and never
per-request** (ADR-0014). Caches are model-scoped, so a per-learner model would
fragment every segment 1; and changing a call type's effort invalidates that
call type's prefix. Both live on `Valves`, which is admin-level by
construction — there is no `UserValves` here, and none of the five methods
takes anything but the assembled segments.

**Thinking is never disabled.** On Opus 5, disabling it has two documented
failure modes — tool calls written into visible text, and `<thinking>` tag
leakage. Effort is the latency lever instead.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping

import anthropic

from socratic.domain import model_client
from socratic.domain import output_schemas
from socratic.domain import prompting

MODEL = "claude-opus-5"
"""ADR-0014. Chosen over `claude-sonnet-5` for its 512-token minimum cacheable
prefix against Sonnet 5's 1024: five segment-1 prefixes are each smaller than
one combined prefix would be, so the floor is the binding constraint."""

EFFORT = "low"
"""ADR-0014. Uniform across the five call types until measurement moves one."""

MAX_TOKENS = 16000

CACHE_CONTROL = {"type": "ephemeral"}

MINIMUM_CACHEABLE_PREFIX_TOKENS = 512
"""Opus 5's floor. Under it, breakpoint 1 silently creates no entry and the
cross-user sharing segment 1 exists for is lost. The symptom is a bill."""


class ModelRefused(RuntimeError):
    """The model declined the request (`stop_reason: "refusal"`).

    Raised rather than returned because a refusal is not schema-constrained
    output: handing it back as `content` would give the caller a parse error
    with no way to tell a refusal from a malformed quiz.
    """


def _uniform_effort() -> dict[prompting.CallType, str]:
    return {call_type: EFFORT for call_type in prompting.CallType}


@dataclasses.dataclass(frozen=True, slots=True)
class Valves:
    """Admin-level settings, in the sense Open WebUI gives the word.

    Never `UserValves` (CONTEXT: Model). A per-learner model would fragment
    every segment 1's cache entry and make curation data non-comparable across
    learners; a per-learner effort would do the same to one call type's prefix.

    Held here as a plain frozen dataclass rather than as an Open WebUI type: the
    Pipe owns the host's `Valves` class and cannot be imported below the
    portability seam, so it passes the values down.
    """

    model: str = MODEL
    effort: Mapping[prompting.CallType, str] = dataclasses.field(
        default_factory=_uniform_effort
    )
    max_tokens: int = MAX_TOKENS


DEFAULT_OUTPUT_SCHEMAS: Mapping[prompting.CallType, dict[str, Any]] = (
    output_schemas.for_mode()
)
"""One schema per call type, composed from the registry and the parsers (#66).

Not written here any more. They were provisional shapes from #4, to be replaced
through the `schemas` argument by the tickets that owned the payloads (#5
authoring, #8/#9 grading); those tickets landed and none of them did, and by
then every one of the four disagreed with what the domain actually parses.
Nothing failed, because every test runs against `RecordingModelClient`, which
serves canned content and never looks at a schema.

`output_schemas.for_mode` is the single composition point, and it lives in the
domain because that is the direction the dependency runs: the domain may not
import the adapter, and a schema is a statement about what the domain parses -
this module only carries it to the wire. The default is the mode-agnostic
composition, which admits every mode the registry holds; a caller that knows
which mode it is authoring for passes `schemas=output_schemas.for_mode(mode)`
through the constructor argument that has existed for this since #7."""


def _block(segment: prompting.PromptSegment) -> dict[str, Any]:
    """One text block, carrying the breakpoint the assembler marked."""
    block: dict[str, Any] = {"type": "text", "text": segment.text}
    if segment.cache_control:
        block["cache_control"] = dict(CACHE_CONTROL)
    return block


def build_request(
    segments: prompting.PromptSegments,
    *,
    valves: Valves | None = None,
    schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate assembled segments into a Messages API request body.

    Pure: no client, no clock, no network. Every invariant ADR-0014 fixes is a
    property of the returned dict, which is what makes them testable at all
    without spending money.

    Args:
      segments: The assembled prompt. Its `call_type` selects the effort level
        and the output schema.
      valves: Admin settings. Defaults to `claude-opus-5` at `low` effort.
      schema: The call's output schema, overriding the provisional default.

    Returns:
      Keyword arguments for `client.messages.create`.
    """
    valves = valves or Valves()
    call_type = segments.call_type
    if schema is None:
        schema = DEFAULT_OUTPUT_SCHEMAS[call_type]

    return {
        "model": valves.model,
        "max_tokens": valves.max_tokens,
        "system": [_block(segments.segment_1)],
        "messages": [
            {
                "role": "user",
                "content": [
                    _block(segments.segment_2),
                    _block(segments.volatile_tail),
                ],
            }
        ],
        "output_config": {
            "effort": valves.effort[call_type],
            "format": {"type": "json_schema", "schema": dict(schema)},
        },
        # Never `{"type": "disabled"}` — ADR-0014, and the module docstring.
        "thinking": {"type": "adaptive"},
    }


class AnthropicModelClient:
    """`ModelClient` over the live API.

    The SDK client is injected rather than constructed here so that the whole
    adapter is testable against a fake with a `.messages.create(**kwargs)` —
    the seam that keeps this ticket from being untestable without a bill.
    """

    def __init__(
        self,
        *,
        client: anthropic.Anthropic | Any | None = None,
        valves: Valves | None = None,
        schemas: Mapping[prompting.CallType, Mapping[str, Any]] | None = None,
    ) -> None:
        """Wire the adapter up.

        Args:
          client: The SDK client, or a double. Omitted, a real
            `anthropic.Anthropic()` is constructed, which resolves credentials
            from the environment and will make paid calls.
          valves: Admin settings (model, per-call-type effort, `max_tokens`).
          schemas: Output schemas by call type, overriding the provisional
            defaults.
        """
        self._client = client if client is not None else anthropic.Anthropic()
        self._valves = valves or Valves()
        self._schemas = dict(DEFAULT_OUTPUT_SCHEMAS)
        if schemas:
            self._schemas.update(schemas)

    @property
    def valves(self) -> Valves:
        return self._valves

    # --- The five methods ----------------------------------------------------

    def author_skeleton(
        self, segments: prompting.PromptSegments
    ) -> model_client.ModelResponse:
        return self._send(prompting.CallType.AUTHOR_SKELETON, segments)

    def author_pedagogy(
        self, segments: prompting.PromptSegments
    ) -> model_client.ModelResponse:
        return self._send(prompting.CallType.AUTHOR_PEDAGOGY, segments)

    def grade_answer(
        self, segments: prompting.PromptSegments
    ) -> model_client.ModelResponse:
        return self._send(prompting.CallType.GRADE_ANSWER, segments)

    def grade_probe(
        self, segments: prompting.PromptSegments
    ) -> model_client.ModelResponse:
        return self._send(prompting.CallType.GRADE_PROBE, segments)

    def fold_narrative(
        self, segments: prompting.PromptSegments
    ) -> model_client.ModelResponse:
        return self._send(prompting.CallType.FOLD_NARRATIVE, segments)

    # --- Internals -----------------------------------------------------------

    def _send(
        self,
        call_type: prompting.CallType,
        segments: prompting.PromptSegments,
    ) -> model_client.ModelResponse:
        if segments.call_type is not call_type:
            raise ValueError(
                f"{call_type.value} was handed segments assembled for "
                f"{segments.call_type.value}"
            )
        request = build_request(
            segments, valves=self._valves, schema=self._schemas[call_type]
        )
        return _to_model_response(self._client.messages.create(**request))


def _to_model_response(message: Any) -> model_client.ModelResponse:
    """Map an SDK `Message` onto the domain's `ModelResponse`.

    The usage counters are read defensively: a response that omits them is a
    cache miss reported as zero, which is exactly what the gate should see.
    """
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        raise ModelRefused(f"the model declined the request (category: {category})")

    usage = getattr(message, "usage", None)
    return model_client.ModelResponse(
        content=_first_text(message),
        message_id=message.id,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
    )


def _first_text(message: Any) -> str:
    """The structured-output payload, verbatim.

    `output_config.format` guarantees the first text block is valid JSON
    against the schema; parsing it belongs to the caller that knows the schema
    (`model_client.ModelResponse`). Thinking blocks are skipped rather than
    assumed absent — thinking is never disabled here.
    """
    for block in getattr(message, "content", ()):
        if getattr(block, "type", None) == "text":
            return block.text
    raise ModelRefused("the response carried no text block")
