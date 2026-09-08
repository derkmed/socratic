# ADR-0005 — Persistence contract

Status: accepted
Date: 2026-09-08

## Decision

**Partition on `learner_id`.** "My past quizzes" and "my learner profile" are
single-partition reads. Cross-user analytics is a cross-partition scan, which is
acceptable because it is an offline job by design.

**ULIDs, never auto-increment integers.** Monotonic keys hot-spot one shard on
write. A prototype that hands out sequential ids teaches the migration an
expensive lesson.

**One `QuizAttempt` document with an embedded, ordered guess array.** It holds the
raw unfilled quiz exactly as authored plus every guess in the order made. One read
reconstitutes everything ADR-0001's cache-anchored prompt needs.

**The guess array is hard-bounded.** A quiz may have at most **20 blanks**, so the
array is capped at ~60 entries (20 x 3 attempts). Unbounded document growth is the
shape that actually kills document stores at scale, so the cap is enforced, not
assumed. **Raising the 20-blank cap is the trigger to migrate to a guess event
stream** — more blanks than that is cognitive overload for a learner anyway, so
the cap is expected to hold.

**Ratings are separate records keyed by attempt id.** The attempt document is
sealed on completion and never mutated, keeping it a clean immutable audit record.
The rating is an optional human signal outside the LLM conversation, written if and
when offered — a separate artifact matching its separate nature.

**Repositories are interfaces in the domain package**, with in-memory
implementations for the prototype. No Open WebUI types cross this boundary
(ADR-0002).

## Consequences

Good:
- Migration to a real document store is an implementation swap behind the
  repository interfaces; nothing in the domain changes.
- No unbounded arrays, no monotonic keys, no cross-partition read path.
- Attempt records are immutable, so the audit trail is trustworthy.

Costs / risks:
- Every guess is a read-modify-write of the whole attempt document. Fine at
  prototype concurrency (one learner, one quiz); revisit under real load.
- Reading "attempt with its rating" is two lookups.
- The 20-blank cap must be enforced at authoring time, not discovered at write time.
