# 0013. The reactive tutor line rides the grading response; no streaming

Date: 2026-09-08
Status: accepted
Amends: [ADR-0003](0003-grading-authority-and-key-custody.md) (the parallel tutor
call is withdrawn), [ADR-0009](0009-self-explanation-probe.md) (the probe question
no longer rides that call)

## Context

[ADR-0003](0003-grading-authority-and-key-custody.md) decided that a richer
reactive tutor line would be fired concurrently with the celebration animation and
streamed into the panel, so model latency hid behind UI that was going to run
anyway. [ADR-0009](0009-self-explanation-probe.md) then justified the probe
question by having it ride that same call — "asking costs nothing new".

[ADR-0011](0011-latency-budget.md) removed that payload. Novice probe questions
became pre-authored in the pedagogy payload; Advanced probe questions became a
nullable field on the grading response the client was already making. The parallel
call was left with nothing to carry, and nobody said so.

That mattered because it was silently load-bearing in two directions. If the call
still fires on a Novice answer, then "a Novice answer costs zero model calls" — the
headline economic claim of ADR-0003 and acceptance criterion 2 — is false. If it
does not fire, ADR-0003's decision is dead text that a reader would still build.

## Decision

**The reactive tutor line exists in Advanced only, as a nullable field on the
grading response.** Advanced is already making a model call to grade the answer;
the line rides that response alongside the probe question ADR-0011 put there.

**Novice has no reactive tutor line at all.** Its feedback is wholly pre-authored —
reinforcement and hint rungs from the pedagogy payload. A Novice answer costs zero
model calls, without qualification.

**The parallel tutor call is withdrawn, and streaming is out of the prototype.**
Nothing is fired concurrently with the celebration any more. The only streaming
that remains in the design is none.

**ADR-0003's insight survives the mechanism that carried it.** Latency still hides
behind the celebration animation — it is simply one response arriving during the
animation rather than a stream catching up after it.

## Consequences

An entire subsystem leaves the prototype. There is no concurrent request to
orchestrate, no stream to multiplex into the iframe, and no partial-state path
where a verdict has landed and its commentary has not.

The cost is that an Advanced verdict no longer returns ahead of its tutor line: the
response waits on roughly 30-40 additional output tokens. By ADR-0011's own
reasoning latency tracks output tokens, so this is a real regression — but a
bounded one, on the one path that was already spending a blocking call, and well
inside the animation it hides behind.

Novice feedback is now unambiguously non-reactive. That was already true in
substance — ADR-0003 conceded that a two-option click has no phrasing worth
reacting to — but it was previously obscured by a call that appeared to add
reactivity and did not.

Acceptance criterion 2 becomes literally assertable: a `ModelClient` stub that
fails on any invocation survives a Novice answer.
