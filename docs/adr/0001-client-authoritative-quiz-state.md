# ADR-0001 — Client-authoritative quiz state with a cache-anchored session block

Status: accepted
Date: 2026-09-08
Supersedes: the "reprint the explanation every reply" instruction in the
originally-supplied Socratic system prompt.

## Context

The supplied tutor prompt instructs the model to reprint the full ~250-word
explanation, with solved blanks filled and unsolved ones blank, on every reply.
That is a workaround for a plaintext chat window that cannot re-render state.

We want clickable quiz overlays and a persisted record of every guess in order.
Both goals want a machine-readable quiz object, not prose to be re-parsed each turn.

Reprinting is also the worst available token pattern: a conversation of N turns
re-emits the explanation N times, so *output* tokens — the expensive direction —
grow quadratically in turns. At the stated target of millions of users this is
the dominant cost line, and it buys nothing a client-side renderer can't do free.

## Decision

**The client holds authoritative quiz state.** The model authors a quiz once as a
structured payload. Solved/unsolved blanks, per-blank attempt counts, and guess
order live in application state and are rendered by the UI. The model never
reprints the explanation.

**Each grading call is a fresh request assembled from persisted state**, shaped in
two parts:

1. **Frozen prefix** (`cache_control` breakpoint at its end) — tutor system
   instructions, the model-authored explanation, and every blank's definition and
   grading rubric. Byte-identical for the life of the session.
2. **Volatile tail** — a compact log of every guess so far across all blanks in
   order, then the blank currently being answered and the learner's guess.

**The interaction log is the persisted attempt record.** Tutor memory, the audit
trail, and the "raw unfilled quiz + responses in guess order" storage requirement
are one artifact, not three. Anything the tutor is allowed to remember is by
construction something we have already durably written down.

## Consequences

Good:
- The frozen prefix clears the 512-token minimum cacheable prefix on
  `claude-opus-5` and reads at ~0.1× input price. 5-minute TTL breaks even at two
  requests; a quiz makes many, and each read refreshes the timer for free — so a
  learner answering within five minutes keeps the session warm at negligible cost.
- Rendering never waits on the model to re-describe state it already sent.
- The tutor still sees cross-blank patterns, so coaching stays personal.

Costs / risks:
- Prompt assembly becomes our code, and cache hit rate becomes a thing we must
  measure (`usage.cache_read_input_tokens`). Any accidental variance in the frozen
  prefix — a timestamp, unsorted JSON keys — silently drops us to full price with
  no error.
- We diverge from the supplied prompt, which must be rewritten to emit a quiz
  object and to grade one blank at a time rather than restate the whole board.
- Attempt counting moves to the client, so the hint-ladder rung is an input to the
  model rather than something it infers.
