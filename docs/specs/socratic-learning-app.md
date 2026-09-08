# Socratic Learning App — prototype

Source: [docs/maps/socratic-learning-app.md](../maps/socratic-learning-app.md).
Every assertion below traces to a decision recorded there or to an ADR.

## Goal

A learner asks a question inside Open WebUI and gets back a quiz instead of an
answer: a 200–250 word explanation with key terms masked as numbered blanks,
rendered as an interactive overlay. They resolve blanks one at a time against a
three-rung hint ladder; the system records every guess in the order made, invites
an optional rating at the end, and accumulates a learner profile that shapes later
quizzes. The prototype runs locally on Mac, Windows and Linux via Open WebUI's
container, stores everything in memory behind repository ports, and is shaped so
that swapping in a document store, adding difficulty modes, or piping the captured
data into quiz curation are all additive rather than migrations.

## Seams

Greenfield, so every seam is new. Five, each justified by a property that cannot
be tested without it.

| Seam | Kind | Why it must exist |
|---|---|---|
| `ModelClient` | port | The only non-deterministic dependency. Nothing in the system is testable without a stub here. Two methods: `author(request)` and `grade(request)`. |
| `PromptAssembler.assemble(...) -> PromptSegments` | pure function | Returns the ordered segment list with breakpoint markers, **not** a wire request. This is the only place the two ADR-0006 invariants can be asserted without hitting the live API: segment 1 is byte-identical across two different learners, and no learner-specific text appears above breakpoint 1. |
| `QuizAuthoring.author(inquiry, learner_id) -> AuthoringResult` | service | Single entry to the most complex path. `AuthoringResult` is the `direct_answer \| quiz` union, so the ADR-0004 override branch is testable as a return value rather than a side effect. |
| `QuizSession.submit(blank_id, response) -> Verdict` | service | The hot path, and the exact point where "Novice makes zero model calls" is assertable — a Novice test passes a `ModelClient` stub that fails the test if called at all. |
| `AttemptRepository`, `RatingRepository`, `LearnerProfileRepository` | ports | Already decided in ADR-0005. The in-memory implementations *are* the test doubles, so this seam costs nothing extra. |

Deliberately **not** seams: the Open WebUI adapter (thin, exercised by hand), the
iframe HTML (rendered from typed data that is already tested upstream), and the
`ModeRegistry` (a lookup table, tested through `QuizAuthoring` and `QuizSession`).

## Decisions

| # | Decision | Source |
|---|---|---|
| D1 / D1a | Client-authoritative quiz state; each grading call carries a cache-anchored frozen prefix plus a volatile tail. The interaction log *is* the persisted attempt record. | [ADR-0001](../adr/0001-client-authoritative-quiz-state.md) |
| D3 / D5 | Host on Open WebUI; domain package imports nothing from it; `__user__` maps to our `LearnerId`. | [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md), [research](../research/open-webui-fit.md) |
| D4 | Novice pre-authored and graded with no model call; Advanced model-graded per answer; answer key never leaves the process; reactive tutor call runs parallel to the celebration. | [ADR-0003](../adr/0003-grading-authority-and-key-custody.md) |
| D2 | Structured output; `direct_answer \| quiz` union; segment array not sentinels; one `Blank` type with a mode enum and nullable mode-specific fields, plus an explicit conditional validator. Both modes ship. | [ADR-0004](../adr/0004-quiz-wire-format.md) |
| D6 | Attempt is one bounded document partitioned on `learner_id` with ULID keys; ≤20 blanks; guesses embedded in order; ratings are separate immutable records. | [ADR-0005](../adr/0005-persistence-contract.md) |
| D7 | Profile built by an offline job, injected as cache segment 2, never interpolated into the system prompt. | [ADR-0006](../adr/0006-learner-profile-lifecycle.md) |

## Approach

Built in dependency order; each step is testable before the next begins.

### 1. Domain types and the mode registry

`DifficultyMode` is an enum (`NOVICE`, `ADVANCED`) — but the enum alone is not the
extension point, because behaviour differs per mode in four places. A
`ModeRegistry` maps each mode to a `ModePolicy` bundling all four:

- the JSON schema fragment sent for authoring,
- the grading strategy (`Deterministic` or `ModelGraded`),
- the validator rules (`novice ⇒ len(options) ≥ 2 and correct_option_id ∈ options`;
  `advanced ⇒ rubric present`),
- the render hint the iframe uses to choose an option bank or a text input.

**Adding a third mode is one registry entry.** No branching on mode outside the
registry — this is the invariant that keeps D2a's promise of extensibility real.

`Quiz`, `Blank`, `Segment`, and the `AuthoringResult` union per ADR-0004.

### 2. Repository ports and in-memory implementations

Per ADR-0005: `learner_id` partition, ULID keys, ≤20 blanks enforced at authoring
time rather than discovered at write time.

### 3. Data capture — the record shape

Capture is a first-class goal, not a byproduct of grading. The attempt record must
support later quiz curation without a schema migration, so it is stamped and
complete from day one:

**`QuizAttempt`** — `attempt_id` (ULID), `learner_id`, `session_id`, the raw
unfilled quiz exactly as authored, `mode`, `topic`, `created_at`, `sealed_at`,
`outcome` (resolved / abandoned), `model_id`, `schema_version`, `prompt_version`,
and per-call token usage including `cache_read_input_tokens`.

**`Guess`** (ordered, embedded) — `blank_id`, `submitted` (option id or free text),
`verdict`, `attempt_ordinal` (which rung of the ladder this was), `hint_rung_shown`,
`created_at`, and `graded_by` (`deterministic` or `model`).

The version stamps are what make "without a migration" true: a curation job reading
old records knows which schema and prompt produced them, so changes are additive
rather than rewrites. `sealed_at` and `outcome` exist so abandoned quizzes are
distinguishable from completed ones — the strongest curation signal in the set.

### 4. PromptAssembler

Returns ordered segments with breakpoint markers:

```
[ segment 1 ] invariant tutor instructions          <- cache_control
[ segment 2 ] learner profile + quiz + blank rubrics <- cache_control
[ tail      ] guesses so far in order, current blank + guess
```

Segment 2 renders the profile through a single `render_profile(profile) -> str`
call. **The domain never reads the profile's internals**, so its shape can evolve
without touching grading, persistence, or the assembler's structure. That is the
abstraction that matters for D7, not the storage type.

### 5. ModelClient port and Anthropic adapter

`claude-opus-5`, `output_config.format` with `strict: true`, `cache_control` on
segments 1 and 2. The adapter is the only module importing the Anthropic SDK.

### 6. QuizAuthoring

Assemble → call → parse → **run the conditional validator** → persist the unfilled
quiz. Returns the `direct_answer | quiz` union.

### 7. QuizSession

`submit()` dispatches through `ModePolicy.grading_strategy`. Novice compares to the
stored key and returns pre-authored feedback with no model call. Advanced assembles
the cache-anchored block and calls `ModelClient.grade`. Both append a `Guess` and
advance the ladder. On the last blank resolving, seal the attempt.

### 8. Open WebUI adapter

Pipe/Action mapping `__user__` → `LearnerId`, returning `HTMLResponse` with
`Content-Disposition: inline`. Answer events arrive by iframe `fetch`; if the CSP
blocks it, fall back to `__event_call__` — same topology either way.

### 9. Quiz UI

Segment-walking renderer; option buttons for Novice, text input for Advanced,
chosen by the mode's render hint. Verdict returns immediately and starts the
celebration; the reactive tutor line streams in behind it. Rating prompt on
completion — optional, dismissible, never entering the conversation.

### 10. ProfileBuilder

Reads attempts after the stored watermark, increments the ledger arithmetically,
folds the narrative forward with one model call, advances the watermark, writes
back. Invoked manually or on a timer; not wired to any scheduler.

## Out of scope

- **A real database.** In-memory only. The repository ports are the migration path.
- **Multi-tenant or hosted deployment.** Local Open WebUI container.
- **Authentication and account management.** Inherited from Open WebUI entirely.
- **A scheduler for the profile job.** Manual or timer trigger; no cron, no queue.
- **The curation pipeline itself.** This spec makes the data *sufficient* for
  curation; it does not build any consumer of it.
- **Difficulty modes beyond Novice and Advanced.** The registry makes them cheap;
  none are added here.
- **Resolving the iframe-`fetch` CSP question.** Settled at build time by trying it;
  `__event_call__` is the documented fallback and no other design changes.
- **Cross-learner analytics, dashboards, leaderboards, spaced repetition.**
- **Model fallbacks, retries, rate-limit handling** beyond SDK defaults.
- **Streaming the authoring call.** Single-shot; only the reactive tutor line streams.

## Late decisions folded in

Settled by grill after the first draft. Terms in [CONTEXT.md](../CONTEXT.md).

- **Rating is a 5-point Likert scale.** `RatingRecord.score` is an integer 1-5,
  optional, dismissible, never entering the LLM conversation.
- **Abandoned quizzes are persisted.** `in_flight` is the stored truth; `abandoned`
  is written **only on displacement**, when the learner explicitly starts something
  else. No sweeper job - we never guess that a learner left, and readers apply
  their own age threshold. `sealed_at` stays null until completion.
- **Identity is ours.** `QuizSessionId` (ULID) is the primary key; `chat_id`,
  `session_id` and every Anthropic `message.id` are persisted as host annotations
  for audit. There is no Claude conversation id to borrow - the Messages API is
  stateless. -> [ADR-0007](../adr/0007-quiz-session-identity.md)
- **Mode toggle lives in `UserValves`** and applies from the next authoring call.
  One attempt, one mode.
- **Second inquiry mid-quiz is queued**, with an explicit "start this instead"
  escape that abandons the current attempt and carries the remaining queue forward.
- **Profile is incremental over all history**, split into an exactly-recomputed
  ledger and a drifting narrative, advanced by a watermark.
  -> [ADR-0008](../adr/0008-learner-profile-composition.md)
- **`blank_range` is a `ModePolicy` field** (Novice 1-2, Advanced 4-6). The
  20-blank cap is a storage invariant that should never fire.

## Acceptance

1. A learner asks a question in Open WebUI and gets an interactive quiz overlay,
   not prose.
2. Clicking a Novice option returns a verdict **with zero model calls** — asserted
   by a `ModelClient` stub that fails the test if invoked.
3. A wrong Novice answer walks the three-rung ladder and reveals on rung three.
4. An Advanced blank accepts free text and is graded on meaning: a correct answer
   phrased differently from the rubric passes.
5. A safety-relevant or "I need this now" inquiry returns `direct_answer`, and the
   parse succeeds.
6. `PromptAssembler` produces a byte-identical segment 1 for two different learners.
7. `usage.cache_read_input_tokens > 0` on the second call of a session.
8. The conditional validator rejects a Novice blank with fewer than two options and
   an Advanced blank with no rubric.
9. A completed attempt persists the raw unfilled quiz plus every guess in the order
   made, with verdict, ladder rung and timestamp on each.
10. A 1-5 Likert rating is written as a separate record; the attempt document is
    unchanged after sealing.
10b. An attempt left incomplete persists with its guesses and an unset `sealed_at`.
11. Authoring rejects a quiz with more than 20 blanks.
12. `ProfileBuilder` run by hand rewrites the profile, and the next authoring call
    carries it in segment 2.
13. No module under the domain package imports Open WebUI — assertable by a grep
    test over imports.
14. Adding a hypothetical third difficulty mode requires editing only the registry:
    no `if mode ==` outside it.
15. Flipping the mode toggle mid-quiz leaves the in-flight attempt's mode unchanged
    and applies to the next authoring call.
16. "Start this instead" marks the displaced attempt `abandoned` and the new
    attempt inherits the remaining queued topics.
17. Running `ProfileBuilder` twice with no new attempts in between is a no-op: the
    ledger figures are unchanged and the watermark does not move.
18. Ledger figures are reproducible - recomputing from all attempts equals the
    incrementally-advanced values.
