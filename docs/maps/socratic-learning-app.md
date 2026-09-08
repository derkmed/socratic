# Map: Socratic Learning Web App

> **Historical record.** This map is how the decisions were reached, not what they
> currently are. Several were later revised — the reactive tutor line no longer
> streams and is Advanced-only ([ADR-0013](../adr/0013-reactive-tutor-line.md)), and
> `__event_call__` is not a fallback for the iframe `fetch` path
> ([ADR-0015](../adr/0015-iframe-pipe-transport.md)). For the current design read
> [CONTEXT.md](../CONTEXT.md), the ADRs, and
> [the spec](../specs/socratic-learning-app.md).

## Destination

We can start building when all of the following are decided and written down:

1. **The quiz wire format** — a machine-parseable contract the model emits that the
   UI can render as interactive overlays (clickable options, blanks, hint ladder),
   and which the persistence layer can store verbatim as the "raw unfilled quiz".
2. **Where quiz state lives** — model-reprints-everything (chat-shaped) vs.
   client/server holds authoritative state and the model is a stateless quiz service.
3. **The host platform** — Open WebUI extension vs. purpose-built app — decided
   against the real requirements of custom interactive UI, not by default.
4. **Grading authority** — who decides an answer is correct, and where the answer
   key lives (never trusting the browser with it prematurely).
5. **The persistence contract** — repository interfaces + document shapes for
   `QuizAttempt`, `Rating`, `LearnerProfile`, keyed to an account, with an
   in-memory implementation for the prototype and no partition-hostile shapes.
6. **Identity & API-key handling** — who the "user account" is in a locally-run
   app, and where the Anthropic key lives.
7. **Learner profile lifecycle** — what generates it, what it contains, how (and
   whether) it is injected back into the prompt.

Explicitly *not* required to start: multi-tenant infra, a real database,
horizontal scaling. Those are constrained by decision 5's abstraction, not built.

## Decisions so far

- **D1 — Client-authoritative quiz state.** The model authors a quiz once as a
  structured payload; the client/server holds the canonical state (solved blanks,
  attempt counts, guess order) and renders it. Grading turns are near-stateless
  calls, not an accumulating chat transcript. Rejected the supplied prompt's
  "reprint everything every turn" pattern as quadratic in tokens and fragile to
  parse. Opens D1a. → [ADR-0001](../adr/0001-client-authoritative-quiz-state.md)

- **D1a — Cache-anchored session block.** Each grading call carries a frozen,
  cache-marked prefix (instructions + explanation + blank rubrics) plus a volatile
  tail (all guesses so far in order, then the current blank + guess). The
  interaction log *is* the persisted attempt record — memory, audit, and replay
  are one artifact. Grounded in measured API facts: 512-token minimum cacheable
  prefix on `claude-opus-5`, ~0.1× read price, break-even at two requests.
  → [ADR-0001](../adr/0001-client-authoritative-quiz-state.md)

- **D3 — Host on Open WebUI, behind a portability seam.** It can render rich HTML
  embeds, has a blocking bidirectional channel (`__event_call__`), exposes the
  authenticated `__user__`, and permits arbitrary Python — so our own request path
  survives. Hard rule: domain logic imports nothing from Open WebUI; the plugin is
  a thin adapter. → [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md),
  [research](../research/open-webui-fit.md)

- **D5 — Identity comes from the host.** Open WebUI's authenticated `__user__` is
  the account; the adapter maps it to our own `LearnerId` so the domain never
  depends on the host's user model. Folded into D3.

- **D4 - Pre-author Novice, model-grade Advanced; key stays server-side.** A
  2-option bank makes the wrong answer knowable at authoring time, so correct-answer
  reinforcement and all three hint rungs are emitted up front and graded with no
  model call (~1 call per Novice quiz, not ~8). Advanced free recall is model-graded
  per answer. The iframe gets display fields only; the reactive tutor call runs in
  parallel with the celebration animation so its latency is hidden.
  -> [ADR-0003](../adr/0003-grading-authority-and-key-custody.md)

- **D2 - Structured output; union top level; segment array; one Blank type with a
  mode enum.** `direct_answer` is a first-class branch so the safety override can't
  break the parse. Explanation is a segment list, not sentinels. Nullable
  mode-specific fields keep the type open to future difficulty modes, at the price
  of an explicit conditional validator. Both Novice and Advanced ship.
  -> [ADR-0004](../adr/0004-quiz-wire-format.md)

- **D6 - Attempt as one bounded document; ratings separate.** Partition on
  `learner_id`, ULID keys, guesses embedded in order, quiz capped at 20 blanks
  (~60 guesses) with the cap as the migration trigger to an event stream. Ratings
  are separate immutable records keyed by attempt id, keeping the attempt sealed.
  Repository interfaces live in the domain package with in-memory implementations.
  -> [ADR-0005](../adr/0005-persistence-contract.md)

- **D7 - Profile built by an offline job; injected as a second cache segment.**
  Never interpolated into the system prompt - that is a documented anti-pattern
  that destroys cross-user cache sharing. Invariant tutor instructions sit above
  breakpoint 1 and cache once workspace-wide; profile + quiz sit above breakpoint 2.
  -> [ADR-0006](../adr/0006-learner-profile-lifecycle.md)

## Fog of war

_Clear._ Items that burned off as decisions landed:

- *Hint ladder ownership* - settled by D4: the client counts attempts; Novice hint
  rungs are pre-authored, Advanced rungs are an input to the grading call.
- *Cost/latency envelope* - settled by D3/D4: ~1 model call per Novice quiz,
  per-answer calls only in Advanced, tutor latency hidden behind the celebration.
- *Advanced free-text vs. the clickable premise* - settled by D2: both modes ship,
  one schema, mode enum; Advanced renders a text input instead of a bank.
- *Override path* - settled by D2: `direct_answer` is a first-class union branch.
- *Prompt-injection surface* - largely structural. Schema-constrained output means
  the model cannot emit free prose to satisfy a reframing request; the only escape
  is the `direct_answer` branch, which is the intended, honoured surrender path.
- *Streaming* - settled by D4: verdict returns immediately, the richer tutor line
  streams in behind the animation.
- *Profile privacy* - a local, per-learner-partitioned record under D5/D6; revisit
  before any hosted multi-tenant deployment.

Carried into build as tasks, not open decisions:

- Assert `usage.cache_read_input_tokens > 0` in a test, so a silent prefix
  invalidator cannot quietly triple the bill.
- Assert cache segment 1 is byte-identical across two different learners (ADR-0006).
- Validate mode-conditional schema invariants explicitly (ADR-0004).
- Confirm a rich-UI iframe may `fetch` our API under Open WebUI's CSP; documented
  fallback is `__event_call__` (ADR-0002).

## Frontier

_Empty._ Every decision in the Destination is resolved.

## Handoff

The route is clear. Next step:

    /spec docs/maps/socratic-learning-app.md

Build order implied by the decisions: domain package first (schema + validator +
repositories + prompt assembly), then the Open WebUI adapter, then the quiz UI,
then the rating record, then the profile job.

