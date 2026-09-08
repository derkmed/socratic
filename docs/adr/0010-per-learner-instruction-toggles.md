# 0010. Per-learner toggles switch behaviour, not prompt text

Date: 2026-09-08
Status: accepted
Extends: [ADR-0006](0006-learner-profile-lifecycle.md),
[ADR-0009](0009-self-explanation-probe.md)
Amended by: [ADR-0011](0011-latency-budget.md) and
[ADR-0013](0013-reactive-tutor-line.md) (the call counts below predate asking a
probe becoming free), [ADR-0014](0014-model-choice-and-effort.md) ("one segment-1
cache entry" is one **per call type**, and there are five)

## Context

A learner setting was proposed to turn the self-explanation probe off, with the
probe instructions omitted from the system instruction when disabled.

Omitting them collides with ADR-0006's invariant: **no learner-specific text may
ever appear above breakpoint 1.** Segment 1 is byte-identical for every learner in
the workspace, which is why it is written to cache once and read by everyone at
~0.1x. A per-learner omission gives segment 1 two variants, so two cache entries.
Two is survivable; the precedent is not. Each further toggle doubles the count, so
three toggles means eight segment-1 entries.

It is also unnecessary. Under ADR-0009 the **client already owns probe cadence**.
With probes off, the client simply never fires the parallel tutor call, and the
model is never asked to probe. The instruction text is inert — roughly 80 tokens in
a segment cached at ~0.1x and shared across every learner.

## Decision

**Segment 1 stays byte-identical. Per-learner toggles govern client behaviour, not
prompt text.** This is the general rule, not a probe-specific exception.

**Where instruction text genuinely must vary per learner, it goes in segment 2**,
which is already per-learner and per-session. Never segment 1.

The setting is a `UserValves` field — Open WebUI valves are Pydantic models and
support `Literal[...]`, which renders as a dropdown and costs no more than a bool:

```
probe_cadence: Literal["off", "final_blank_only", "sometimes", "always"] = "sometimes"
```

Defaults **on at `sometimes`** — opt-out, not opt-in. `final_blank_only` exists so a
learner who finds probing chatty keeps the capstone probe rather than losing the
signal entirely.

**Flipping applies immediately, from the next correct answer.** Unlike the mode
toggle, no pre-authored content is bound to it.

**A probe already on screen stands.** The setting applies from the next correct
answer, not to the question in front of the learner. The escape already exists: a
probe is dismissible, and a dismissed probe persists with a null
`self_explanation` and does not block sealing. Withdrawing a question while someone
is typing an answer to it is worse than one more probe, and it avoids a
cancellation path with its own partial state.

**The cadence is recorded twice, deliberately.** `probe_cadence_at_authoring` is
snapshotted on the attempt, and `cadence_at_fire` is stamped on each `Probe`. The
per-probe field alone is insufficient: with `off`, no probes fire, so nothing is
recorded and "probes were disabled" becomes indistinguishable from "this learner
never got one right". The attempt field makes a suppressed learner visible in the
data; the per-probe field stays exact across mid-quiz changes.

**Sealing uses one unified predicate: all blanks resolved and no probe pending.**
It covers probes-on, probes-off and dismissed probes without a branch — ADR-0009's
"gated on the final probe" was a special case of this.

## Consequences

One segment-1 cache entry per call type for the whole workspace, permanently,
however many toggles the product grows. (Written here as "one entry" — there are
five, one per `ModelClient` method; see
[ADR-0014](0014-model-choice-and-effort.md). The invariant is unchanged, but it is
asserted per call type.) Future settings inherit the rule for free, and the
question "does this change the prompt?" has a standing answer: no.

The cost is roughly 80 inert tokens in the prompt of a learner who has probes off.
At ~0.1x on a shared cached segment this is close to unmeasurable, and it is the
right trade against a combinatorial cache split.

Other effects: `probe_failure_behavior` (ADR-0009) is unreachable when probes are
off, so the Advanced re-open path goes dormant. Probe density now varies across
learners, so the curation job must normalise for it rather than comparing raw
counts. And ADR-0009's "~3 model calls per Novice quiz" assumed `sometimes` — at
`always` a 2-blank Novice quiz reaches ~4, and an Advanced quiz at `always` runs
roughly 10-12. **These figures are stale:** they predate ADR-0011 making the probe
*ask* free and ADR-0013 withdrawing the parallel tutor call. Recount before quoting
them.
