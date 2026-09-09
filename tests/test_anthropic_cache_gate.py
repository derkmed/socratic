"""The build gate: five segment-1 prefixes, each proven to cache on its own.

**These tests are UNRUN.** They make live, paid Anthropic API calls, so they
skip unless `ANTHROPIC_API_KEY` is set. They are written, committed and ready:
running them is a spending decision, and it belongs to whoever owns the key.

    ANTHROPIC_API_KEY=sk-... pytest tests/test_anthropic_cache_gate.py -v -s

What they discharge, and nothing else does:

* **Acceptance 12** — `cache_read_input_tokens > 0` on the second call of a
  session, asserted **independently for each of the five call types**. One
  aggregate check does not discharge it: each call type has its own segment 1
  and therefore its own prefix, and any one of them can silently fail to cache
  while the other four succeed.
* **The 512-token floor** (ADR-0014) — the measured token length of each of the
  five segment 1s. `tests/test_anthropic_adapter.py` carries a character-count
  proxy for this; a proxy is not a measurement, and only `count_tokens` on the
  real tokenizer settles it.

Falling under the floor is not an ordinary cache miss. Breakpoint 1 silently
creates no entry while breakpoint 2 still caches — but that entry is per-learner
and per-session, so what is lost is precisely the cross-user sharing segment 1
exists for. The symptom is a bill, not an error. That is why this is a gate.

A failure here sends work back into #4's assembler rather than being tuned
around in this module.
"""

import os

import pytest

pytest.importorskip(
    "anthropic",
    reason="the live cache gate needs the optional `anthropic` extra installed",
)

from socratic.adapters import anthropic_client  # noqa: E402
from socratic.domain import modes  # noqa: E402
from socratic.domain import prompting  # noqa: E402

CallType = prompting.CallType

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason=(
        "UNRUN: the per-call-type cache gate makes live, paid Anthropic API "
        "calls. Set ANTHROPIC_API_KEY and re-run to discharge acceptance 12 "
        "and the 512-token floor measurement — both are undischarged until "
        "then."
    ),
)

ADA = prompting.LearnerProfile(
    learner_id="learner-cache-gate",
    ledger={"topics/monads": 3},
    narrative="Fluent in recursion, shaky on laziness.",
)


def segments_for(call_type: CallType) -> prompting.PromptSegments:
    """One assembled prompt, reused byte-identically across both calls.

    Byte-identical is the whole point: the cache is a prefix match, so anything
    that varies between the two calls — a timestamp, a fresh id, a reordered
    ledger — would make a miss indistinguishable from a prefix under the floor.
    """
    return prompting.assemble(
        call_type,
        profile=ADA,
        probe_cadence=modes.ProbeCadence.SOMETIMES,
    )


@pytest.fixture(scope="module")
def live_client():
    import anthropic

    return anthropic_client.AnthropicModelClient(client=anthropic.Anthropic())


@pytest.mark.parametrize("call_type", list(CallType), ids=lambda c: c.value)
def test_the_second_call_of_a_session_reads_the_cache(live_client, call_type):
    """Acceptance 12, once per call type. Two calls; the second must hit."""
    segments = segments_for(call_type)
    method = getattr(live_client, call_type.value)

    first = method(segments)
    second = method(segments)

    print(
        f"\n{call_type.value}: "
        f"first(create={first.cache_creation_input_tokens}, "
        f"read={first.cache_read_input_tokens}) "
        f"second(create={second.cache_creation_input_tokens}, "
        f"read={second.cache_read_input_tokens})"
    )

    assert second.cache_read_input_tokens > 0, (
        f"{call_type.value}'s prefix did not cache. Its segment 1 is most "
        "likely under the "
        f"{anthropic_client.MINIMUM_CACHEABLE_PREFIX_TOKENS}-token floor, in "
        "which case breakpoint 1 created no entry and cross-user sharing is "
        "silently lost for this call type. Take it back to the assembler (#4)."
    )


@pytest.mark.parametrize("call_type", list(CallType), ids=lambda c: c.value)
def test_each_segment_1_clears_the_512_token_floor(call_type):
    """The measurement to record on #7, one line per call type.

    `count_tokens` is itself a live API call, which is why this sits behind the
    same credential gate as the rest of the file.
    """
    import anthropic

    client = anthropic.Anthropic()
    segment_1 = segments_for(call_type).segment_1.text
    floor = anthropic_client.MINIMUM_CACHEABLE_PREFIX_TOKENS

    counted = client.messages.count_tokens(
        model=anthropic_client.MODEL,
        system=[{"type": "text", "text": segment_1}],
        messages=[{"role": "user", "content": "."}],
    )

    print(f"\n{call_type.value} segment 1: {counted.input_tokens} tokens (floor {floor})")

    assert counted.input_tokens >= floor, (
        f"{call_type.value}'s segment 1 measures {counted.input_tokens} tokens, "
        f"under the {floor}-token floor. This is the assembler's problem, not "
        "the adapter's: say so on #7 rather than padding the prompt here."
    )
