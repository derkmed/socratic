# ADR-0003 — Grading authority, key custody, and the parallel tutor call

Status: accepted
Date: 2026-09-08
Amended by: [ADR-0009](0009-self-explanation-probe.md) (call-count claim),
[ADR-0013](0013-reactive-tutor-line.md) (the parallel tutor call is withdrawn),
[ADR-0015](0015-iframe-pipe-transport.md) (key custody is a different process than
stated here)

## Context

Every answer could be graded by the model, but most answers don't need it. In
Novice mode the option bank has **two** options, so the wrong answer is knowable
at authoring time — the model can pre-write the correct-answer reinforcement and
all three hint-ladder rungs in the same call that authors the quiz.

Separately, we want feedback to feel instant. Shipping the answer key to the
browser would achieve that, but under a pre-authored design the page would then
carry every blank's answer *and* every hint rung — and the learner most likely to
open devtools is one who is stuck, which is exactly when the retrieval effort is
doing its work.

## Decision

**Novice is pre-authored and graded without a model call.** The authoring response
carries, per blank: the correct option, the reinforcing sentence, and the three
hint rungs. A Novice quiz costs ~1 model call end-to-end instead of ~8.

**Advanced is model-graded per answer**, using the ADR-0001 cache-anchored block.
Free recall means unbounded answers, so "grade on meaning, not wording" requires
the model.

**The key never leaves the backend.** (Stated here as "the Pipe process"; per
[ADR-0015](0015-iframe-pipe-transport.md) the domain runs as its own service, so
the Pipe is not the boundary. The substance is unchanged.) The iframe receives
display fields only
— explanation, blank positions, option labels. Grading is a round trip
(~1–5ms locally) that returns a verdict plus pre-authored feedback.

**The tutor call runs in parallel with the celebration.** The verdict returns
immediately; any richer reactive tutor line is fired concurrently and streamed
into the panel as the feedback animation plays. Model latency hides behind UI
that was going to run anyway.

**Withdrawn by [ADR-0013](0013-reactive-tutor-line.md).** Once ADR-0011 made the
probe question free, this call had no payload left to carry. The reactive line is
now a nullable field on the Advanced grading response, Novice has none, and nothing
streams. The insight survives its mechanism: latency still hides behind the
animation.

## Consequences

Good:
- ~8x fewer model calls per Novice quiz; the dominant mode is nearly free to serve.
- Feedback is instant without the page holding a single answer.
- Latency budget is spent on animation we wanted, not on waiting.

Costs / risks:
- Two grading paths (deterministic and model-graded) to build and test.
- Novice feedback is pre-written, so it cannot react to *how* a learner phrased a
  wrong answer — acceptable, since a 2-option click has no phrasing.
- The UI needs an optimistic/streaming path, not just request-response.
- Process boundary, not network boundary, protects the key: a determined local
  user can read the store. That is the accepted threat model — friction against
  casual peeking, not defence against an attacker.
