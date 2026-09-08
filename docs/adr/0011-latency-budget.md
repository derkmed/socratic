# 0011. Split the authoring call; never spend a call to ask a probe

Date: 2026-09-08
Status: accepted
Amends: [ADR-0009](0009-self-explanation-probe.md) (call-count claim, again)

## Context

Latency is a primary product concern, and the constraint is that no single
interaction should cost more than one or two model calls.

Auditing the design against that found two problems.

**ADR-0009's call count was wrong a second time.** It said the probe takes a Novice
quiz from ~1 call to "~3". A probe is **two** calls — one to ask the question, one
to grade the reply — and only the ask was counted. With the guaranteed final-blank
probe plus a ~50% coin flip, a 2-blank Novice quiz is really **3-5 calls**.

**Call count was never the bottleneck.** Latency tracks output tokens, and the
authoring call emits everything at once: a 250-word explanation, plus per blank two
options, a reinforcement, three hint rungs and a rubric, plus the recap — roughly
700-1100 output tokens in one blocking call before the learner sees anything.

## Decision

**Split authoring into two calls.**

1. **Skeleton** — explanation, blanks, options. Roughly a third of the tokens, so
   it paints fast. This is the only blocking call.
2. **Pedagogy payload** — hints, reinforcements, probe questions, recap. Fired
   immediately after and fetched while the learner spends 30-60 seconds reading 250
   words. It lands long before it is needed.

The client guards the case of a learner who answers before the payload arrives.

**Asking a probe never costs a call.**

- **Novice** — probe questions are pre-authored per blank in the pedagogy payload,
  exactly as the hint rungs already are. The same reasoning as ADR-0003.
- **Advanced** — the probe question is a nullable field on the grading response the
  client is already making.

Either way the probe drops from two calls to one, and the question appears instantly
rather than after a round trip.

**No fast mode.** Opus 5's `speed: "fast"` (up to 2.5x output tokens/sec) targets
exactly this bottleneck, but the structural fixes above buy more on the paths that
matter and cost nothing. It also doubles token price to $10/$50 per MTok, is a
research preview with its own rate limit, and — decisively — **switching speed
invalidates the prompt cache**, so mixing speeds across authoring and grading would
split segment 1 into two cache namespaces and undo ADR-0006. Revisit against
measurements, and only ever all-or-nothing.

## Consequences

Every interaction now costs at most one blocking call:

| Interaction | Blocking calls |
|---|---|
| Ask a question | 1 (light) + 1 hidden behind reading |
| Novice answer, right or wrong | 0 |
| Advanced answer | 1 |
| Probe question appears | 0 |
| Probe reply | 1 |

Costs: a quiz is briefly playable without its feedback payload, so the client needs
a guard — the first real race condition in the design. Novice probe questions are
written before the model sees the learner's answer, so they are generic to the blank
rather than reactive to the guess; with a two-option bank the loss is small, and it
is the same trade ADR-0003 already accepted for hint rungs.
