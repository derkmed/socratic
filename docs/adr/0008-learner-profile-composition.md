# 0008. Split the learner profile into a ledger and a narrative

Date: 2026-09-08
Status: accepted

## Context

The profile is built **incrementally over all history** rather than from a
recency window, so that infrequent learners are not penalised by a time cutoff.

Incremental folding has a failure mode: each build summarises the previous
summary, so an early wrong read of a learner compounds indefinitely with no pass
that ever re-grounds it. The obvious correction — periodically rebuilding from
full history — gets more expensive the longer someone has been learning, which
penalises exactly the committed learners the incremental approach was protecting.

## Decision

The `LearnerProfile` has two parts:

- **Ledger** — structured and exactly recomputed: topic counts, mode history,
  weak-area tallies, attempt and outcome counts. Updated by incrementing from the
  attempts since the last watermark. Attempt records are immutable and complete,
  so these figures are arithmetic and cannot drift.
- **Narrative** — the LLM-authored prose summary. Folded forward incrementally,
  informed by the ledger.

`ProfileBuilder` advances a **watermark** (last processed attempt) so each run
reads only new attempts, never full history.

Both parts render into cache segment 2 through the single `render_profile()` call;
the domain still never reads the profile's internals (ADR-0006).

## Consequences

Drift is confined to the prose. Every figure a curation job or a future
personalisation feature would want is exact, and stays exact for a learner with
ten years of history. Cost per build is proportional to new activity, not to
lifetime history, so a returning learner is cheap to update.

The costs: two things to keep in step rather than one, a watermark that must
advance correctly or work is silently reprocessed or skipped, and a narrative that
can still contradict its own ledger — the ledger is authoritative when they
disagree.
