# ADR-0006 — Learner profile lifecycle and prompt placement

Status: accepted
Date: 2026-09-08

## Context

The learner profile is an evolving, LLM-authored summary of a learner's strengths,
weaknesses, recurring topics, and working difficulty level. Two questions: when it
is regenerated, and how it reaches the tutor.

The second question has a caching trap. Anthropic caches are isolated **per
workspace**, not per user, and the documented anti-pattern is interpolating a
user or session id into the system prompt: it gives every learner a unique prefix,
so nothing caches across users. At a million learners that is a million cold cache
writes on instruction text identical for all of them.

## Decision

**Regeneration is an offline job.** A `ProfileBuilder` reads a learner's recent
attempts and rewrites the summary. In the prototype it is triggered manually or on
a timer; in production it becomes a routine batch job. Profile generation never
sits on a learner's critical path.

**The profile is injected as a second cache segment, never interpolated into the
system prompt.** Prompt layout, in render order:

```
[ tools ]
[ system: invariant tutor instructions ]        <-- cache breakpoint 1
[ learner profile + quiz + blank rubrics ]      <-- cache breakpoint 2
[ volatile tail: guesses so far, current blank + guess ]
```

Segment 1 is byte-identical for **every learner in the workspace**, so it is
written to cache once and read at ~0.1x by everyone. Segment 2 is per-learner and
per-session but small. The volatile tail sits after the last breakpoint.

**Invariant:** no learner-specific text may appear above breakpoint 1. Ever. That
is the whole mechanism.

## Consequences

Good:
- Cross-user cache sharing is preserved on the largest, most-repeated segment —
  the lever that matters most at the stated scale.
- Profile work is off the critical path and can be re-run over history whenever the
  profile prompt changes.
- Composes with ADR-0001: the frozen prefix simply gains a second segment.

Costs / risks:
- The profile lags by one job interval; a new learner's first quizzes are
  un-personalised.
- The no-learner-text-above-breakpoint-1 invariant is a discipline nothing
  enforces. Worth a test asserting the segment-1 bytes are constant across two
  different learners.
- We are assuming a profile improves quiz quality. Unmeasured. The rating record
  from ADR-0005 is the signal that could eventually test it.
