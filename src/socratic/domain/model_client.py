"""The `ModelClient` port — the system's only non-deterministic dependency.

Nothing in the system is testable without a double here, so the port lands
before anything that calls it, together with the recording stub that *is* the
double.

**Five methods, one per call type**, rather than a generic `complete()`. Two
reasons, both load-bearing (CONTEXT: Call type, master spec seam table):

* The seam exists so a stub can assert *this was never called* — "submitting a
  Novice answer returns a verdict with zero model calls" (master spec acceptance
  6) is exactly that assertion, and a generic method makes it unwritable at the
  granularity that matters.
* Naming the five keeps the call inventory visible in the type. Each call type
  has its own instructions, its own output schema, its own segment 1 and its own
  cache prefix; a single method would hide all of that behind a string argument.

Every method takes a `PromptSegments` — the laid-out segments, not a wire
request. Translating those into a request body, sending it, and reading the
response back is the adapter's job, and the adapter is the only module in the
system permitted to import the SDK. It is not in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from socratic.domain import records
from socratic.domain.prompting import CallType, PromptSegments


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a call returns.

    `content` is the structured-output payload verbatim; parsing it against the
    call's schema belongs to the caller that knows the schema.

    `message_id` is a **host annotation** (CONTEXT: Host annotations) —
    persisted on the attempt for accountability, never a key, never depended
    on. It is the audit link from a stored quiz back to the exact calls that
    produced it.

    The two cache counters are what the build gate reads: `cache_read_input_
    tokens > 0` asserted independently for each of the five call types is what
    proves each segment 1 cleared the model's minimum cacheable prefix
    (ADR-0014, master spec acceptance 12). Falling under that floor is silent,
    so the counter is the only symptom short of the bill.

    `input_tokens` and `output_tokens` complete the set. All four together are
    the audit link back to the exact API calls behind a stored quiz, and a
    caller holding only the two cache counters has to invent the other two —
    which is how the first caller came to stamp every attempt with `0` input
    and `0` output. The counters stay loose here rather than becoming a single
    `TokenUsage` field: this is the port's value type and `TokenUsage` is the
    persisted one, so `token_usage()` is the one place the two meet.

    Every counter defaults to zero, so a response that does not know its usage
    — the recording stub's canned reply — still constructs.
    """

    content: str
    message_id: str
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def token_usage(self) -> records.TokenUsage:
        """The four counters in the shape that gets persisted.

        Callers stamping a `records.ModelCallRecord` onto an attempt go through
        here rather than reassembling the counters by hand, so there is exactly
        one mapping to get wrong and it has a test.
        """
        return records.TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
        )


@runtime_checkable
class ModelClient(Protocol):
    """The five distinct requests the system makes. No generic method."""

    def author_skeleton(self, segments: PromptSegments) -> ModelResponse:
        """Author the explanation, blanks and options — the blocking call."""
        ...

    def author_pedagogy(self, segments: PromptSegments) -> ModelResponse:
        """Author hints, reinforcements, probe questions and the recap."""
        ...

    def grade_answer(self, segments: PromptSegments) -> ModelResponse:
        """Grade a submitted answer on meaning against the blank's rubric."""
        ...

    def grade_probe(self, segments: PromptSegments) -> ModelResponse:
        """Grade a self-explanation — the richest signal the system collects."""
        ...

    def fold_narrative(self, segments: PromptSegments) -> ModelResponse:
        """Fold new attempts into the learner profile's narrative, offline."""
        ...


class NeverCalled(AssertionError):
    """Raised by a stub configured to fail the moment it is invoked.

    An `AssertionError` subclass so a test that trips it reads as a failed
    assertion rather than as an error in the harness.
    """


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One invocation: which call type, and the segments it was handed."""

    call_type: CallType
    segments: PromptSegments


class RecordingModelClient:
    """The stub. Records every call and serves canned responses.

    Two ways to assert nothing was called, because the two fail differently and
    both are useful:

    * `fail_if_called=True` raises `NeverCalled` at the moment of the call, so
      the traceback names the culprit rather than the test's last line. This is
      the one to reach for when the point of the test is that the model is
      never consulted.
    * `assert_never_called()` checks after the fact, optionally scoped to one
      call type, for tests that let a whole flow run and then inspect it.

    Mismatched segments are rejected: handing `grade_answer` a `PromptSegments`
    assembled for `fold_narrative` is a wiring bug, and a stub that accepted it
    quietly would let that bug through the exact seam built to catch it.
    """

    def __init__(
        self,
        responses: Mapping[CallType, Sequence[ModelResponse]] | None = None,
        *,
        fail_if_called: bool = False,
    ) -> None:
        self._responses: dict[CallType, list[ModelResponse]] = {
            call_type: list(queued) for call_type, queued in (responses or {}).items()
        }
        self._fail_if_called = fail_if_called
        self._calls: list[RecordedCall] = []

    # --- Inspection ----------------------------------------------------------

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """Every call made, in order."""
        return tuple(self._calls)

    def calls_of(self, call_type: CallType) -> tuple[RecordedCall, ...]:
        return tuple(call for call in self._calls if call.call_type is call_type)

    def call_count(self, call_type: CallType | None = None) -> int:
        if call_type is None:
            return len(self._calls)
        return len(self.calls_of(call_type))

    def assert_never_called(self, call_type: CallType | None = None) -> None:
        """Fail if the model was consulted.

        Args:
          call_type: Narrow the assertion to one call type. Omitted, it covers
            all five.

        Raises:
          AssertionError: If any matching call was made.
        """
        made = self._calls if call_type is None else list(self.calls_of(call_type))
        if made:
            names = ", ".join(call.call_type.value for call in made)
            scope = "the model" if call_type is None else call_type.value
            raise AssertionError(f"{scope} was called {len(made)} time(s): {names}")

    # --- The five methods ----------------------------------------------------

    def author_skeleton(self, segments: PromptSegments) -> ModelResponse:
        return self._record(CallType.AUTHOR_SKELETON, segments)

    def author_pedagogy(self, segments: PromptSegments) -> ModelResponse:
        return self._record(CallType.AUTHOR_PEDAGOGY, segments)

    def grade_answer(self, segments: PromptSegments) -> ModelResponse:
        return self._record(CallType.GRADE_ANSWER, segments)

    def grade_probe(self, segments: PromptSegments) -> ModelResponse:
        return self._record(CallType.GRADE_PROBE, segments)

    def fold_narrative(self, segments: PromptSegments) -> ModelResponse:
        return self._record(CallType.FOLD_NARRATIVE, segments)

    # --- Internals -----------------------------------------------------------

    def _record(
        self, call_type: CallType, segments: PromptSegments
    ) -> ModelResponse:
        if segments.call_type is not call_type:
            raise ValueError(
                f"{call_type.value} was handed segments assembled for "
                f"{segments.call_type.value}"
            )

        self._calls.append(RecordedCall(call_type=call_type, segments=segments))

        if self._fail_if_called:
            raise NeverCalled(
                f"{call_type.value} was called, and this stub was told the "
                "model must never be consulted"
            )

        return self._next_response(call_type)

    def _next_response(self, call_type: CallType) -> ModelResponse:
        """The next queued response, the last one repeating once exhausted.

        Repeating rather than raising: a test that queues one verdict and then
        submits three answers is testing the third answer's handling, not the
        stub's bookkeeping.
        """
        queued = self._responses.get(call_type)
        if not queued:
            return ModelResponse(
                content="{}",
                message_id=f"msg_stub_{call_type.value}_{self.call_count(call_type)}",
            )
        if len(queued) > 1:
            return queued.pop(0)
        return queued[0]
