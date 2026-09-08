# 0014. Claude Opus 5, and effort is the latency lever

Date: 2026-09-08
Status: accepted
Amends: [ADR-0001](0001-client-authoritative-quiz-state.md) (cache-floor
arithmetic), [ADR-0011](0011-latency-budget.md) (the fast-mode rejection now
applies to a model that actually offers it)

## Context

The model was never recorded in an ADR. It sat in a bullet in the spec, despite
being the single choice that fixes the minimum cacheable prefix, the token price,
and whether fast mode exists at all. It had also drifted: ADR-0001 reasoned about
`claude-opus-5` and a 512-token floor, the spec had since settled on
`claude-sonnet-5` whose floor is 1024, and nothing reconciled them.

[ADR-0010](0010-per-learner-instruction-toggles.md) made this sharper than it
looks. Its claim of "one segment-1 cache entry for the whole workspace" is really
**one per call type**, and there are five call types. Five prefixes are each
*smaller* than the single combined one the earlier ADRs pictured, so each is closer
to the floor.

Falling under the floor is not an ordinary cache miss. Breakpoint 1 silently
creates no entry, breakpoint 2 still caches — but that entry is per-learner and
per-session. What is lost is precisely the **cross-user sharing** that ADR-0006
exists to protect, and the symptom is a bill, not an error.

Verified floors: **Claude Opus 5 — 512 tokens**, $5/$25 per MTok. **Claude
Sonnet 5 — 1024 tokens**, $2/$10.

## Decision

**`claude-opus-5`, set by an admin-level `Valve`.** Not `UserValves`: caches are
model-scoped, so a per-learner choice would fragment every segment 1 and make
curation data non-comparable across learners.

**Effort is `low` everywhere for now.** Thinking is adaptive and on by default on
Opus 5, and thinking tokens are output tokens — which by ADR-0011's reasoning is
exactly what latency tracks. `output_config.effort` is the lever. It is a
**per-call-type knob** set to the same value across all five until measurement
justifies raising one; `author_skeleton` is the likeliest first candidate, being
the only blocking call whose output quality is the product.

**Effort must stay constant per call type.** Changing it invalidates that call
type's cache prefix, so it is configuration, never a per-request decision.

**Thinking is never disabled.** Disabling it on Opus 5 has two documented failure
modes — tool calls written into visible text rather than emitted as `tool_use`
blocks, and `<thinking>` tag leakage into the response. Lowering effort achieves
the same cost and latency goal without either.

## Consequences

The 512-token floor roughly halves the risk that any of the five prefixes silently
fails to cache. That risk still has to be measured per call type rather than once —
acceptance criterion 7 becomes five assertions, not one — but the margin is real
where on Sonnet 5 the smallest prefixes would have needed padding to clear 1024.

ADR-0011's fast-mode rejection becomes a live decision instead of a moot one. Fast
mode is offered on Opus 5 and not on Sonnet 5, so on the chosen model there is
something to decline, and the reasoning — that switching speed invalidates the
prompt cache and would split segment 1 into two namespaces — now bites.

The cost is 2.5x per token against Sonnet 5. At prototype volume that is
immaterial, and the choice is a one-line Valve change. The asymmetry is the point:
paying 2.5x on a prototype is recoverable, and discovering at scale that the
prefixes never cached is not.

Uniform `low` effort is a deliberate under-tuning. Authoring quality is the
product, and `low` is the setting least likely to serve it — this is accepted as a
starting point to be moved by measurement, not as a claim that authoring does not
need the headroom.
