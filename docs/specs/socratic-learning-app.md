# Socratic Learning App — prototype

Sources: [docs/CONTEXT.md](../CONTEXT.md) for vocabulary, [docs/adr/](../adr/)
0001–0015 for decisions, and
[docs/maps/socratic-learning-app.md](../maps/socratic-learning-app.md) for how they
were reached (historical — read the ADRs for what is current). Every assertion
below traces to one of those. Nothing here is a new decision.

## Goal

A learner asks a question inside Open WebUI and gets back a quiz instead of an
answer: a 200–250 word explanation with key terms masked as numbered blanks,
rendered as an interactive overlay. They resolve blanks one at a time against a
three-rung hint ladder, are periodically asked *how did you arrive at that?*, and
finish with an optional rating. The system records every guess and every
self-explanation in the order made, and folds them into a learner profile that
shapes later quizzes.

The prototype runs locally on Mac, Windows and Linux as two containers — Open WebUI
and our own **quiz service** — stores everything in memory behind repository ports,
and is shaped so that swapping in a document store, adding difficulty modes, or
piping the captured data into quiz curation are all additive rather than
migrations.

## Seams

Six. Five carry over from the previous draft; one is new and justified below.

| Seam | Kind | Why it must exist |
|---|---|---|
| `ModelClient` | port | The only non-deterministic dependency. Nothing in the system is testable without a stub here. **Five methods, one per call type:** `author_skeleton`, `author_pedagogy`, `grade_answer`, `grade_probe`, `fold_narrative`. Explicit methods rather than a generic `complete()`, because the seam exists so a stub can assert *this was never called* — and so the call inventory stays visible. [CONTEXT: Call type] |
| `PromptAssembler.assemble(call_type, ...) -> PromptSegments` | pure function | Returns the ordered segment list with breakpoint markers, **not** a wire request. The only place the ADR-0006 invariants are assertable without hitting the live API: segment 1 is byte-identical across two different learners, and no learner-specific text appears above breakpoint 1. Takes `call_type` because there are five segment 1s, not one. |
| `QuizAuthoring.author(inquiry, learner_id) -> AuthoringResult` | service | Single entry to the most complex path. `AuthoringResult` is the `direct_answer \| quiz` union, so the ADR-0004 override branch is testable as a return value rather than a side effect. |
| `QuizSession.submit(...) -> Verdict` and `.answer_probe(...) -> ProbeVerdict` | service | The hot path, and the exact point where "a Novice answer makes zero model calls" is assertable. Probe answering is a separate method because it is a separate call-budget story — the answer is free, the probe reply is not. |
| `AttemptRepository`, `RatingRepository`, `LearnerProfileRepository` | ports | Already decided in [ADR-0005](../adr/0005-persistence-contract.md). The in-memory implementations *are* the test doubles, so this seam costs nothing extra. |
| **`TokenMinter.mint(session, learner) / .verify(token) -> Claims`** | **new** | Justified: expiry, wrong-session rejection and per-response rotation are acceptance conditions, and they cannot be asserted through the HTTP layer without standing a server up. Pure function over a clock, so a frozen clock tests all three. |

Deliberately **not** seams:

- **The HTTP API of the quiz service** — a thin translation over `QuizAuthoring`,
  `QuizSession` and `TokenMinter`, all of which are already seams. Testing it
  separately would duplicate their assertions against a live socket.
- **The Open WebUI Pipe** — thin by construction
  ([ADR-0015](../adr/0015-iframe-pipe-transport.md)), holds no domain logic,
  exercised by hand.
- **The iframe HTML** — rendered from typed data already tested upstream.
- **`ModeRegistry`** — a lookup table, tested through `QuizAuthoring` and
  `QuizSession`.

## Decisions

| # | Decision | Source |
|---|---|---|
| D1 | Client-authoritative quiz state; each grading call carries a cache-anchored frozen prefix plus a volatile tail. The interaction log *is* the persisted attempt record. | [ADR-0001](../adr/0001-client-authoritative-quiz-state.md) |
| D2 | Structured output; `direct_answer \| quiz` union; flat segment array of `text \| math \| blank`, never sentinels; one `Blank` type with a mode enum, nullable mode-specific fields and an explicit conditional validator. Both modes ship. | [ADR-0004](../adr/0004-quiz-wire-format.md) |
| D3 | Host on Open WebUI; the domain imports nothing from it; `__user__` maps to our `LearnerId`. | [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md), [research](../research/open-webui-fit.md) |
| D4 | Novice is pre-authored and graded with no model call; Advanced is model-graded per answer; the answer key never leaves the backend. | [ADR-0003](../adr/0003-grading-authority-and-key-custody.md) |
| D5 | Attempt is one bounded document partitioned on `learner_id` with ULID keys; ≤20 blanks; guesses and probes embedded in order; ratings are separate immutable records. | [ADR-0005](../adr/0005-persistence-contract.md) |
| D6 | Profile built by an offline job, injected as cache segment 2, never interpolated into the system prompt. No learner-specific text above breakpoint 1. | [ADR-0006](../adr/0006-learner-profile-lifecycle.md) |
| D7 | We mint our own `QuizSessionId` (ULID); `chat_id`, `session_id` and every Anthropic `message.id` are host annotations, never keys. | [ADR-0007](../adr/0007-quiz-session-identity.md) |
| D8 | The profile is an exactly-recomputed **ledger** plus a folded **narrative**, advanced by a watermark. The ledger is authoritative where they disagree. | [ADR-0008](../adr/0008-learner-profile-composition.md) |
| D9 | The self-explanation probe: client-owned randomised cadence, graded substantively, `probe_failure_behavior` per mode, its own ordered collection. | [ADR-0009](../adr/0009-self-explanation-probe.md) |
| D10 | Per-learner toggles switch **client behaviour, never prompt text**. `probe_cadence` is a `UserValves` field. Sealing is one unified predicate. | [ADR-0010](../adr/0010-per-learner-instruction-toggles.md) |
| D11 | Authoring splits into skeleton + pedagogy payload; asking a probe never costs a call; every interaction is at most one blocking call. No fast mode. | [ADR-0011](../adr/0011-latency-budget.md) |
| D12 | Restricted Markdown subset; math converted to MathML server-side; blanks mask whole formulas only; everything sanitised. | [ADR-0012](../adr/0012-iframe-content-rendering.md) |
| D13 | The reactive tutor line is Advanced-only and rides the grading response. The parallel tutor call is withdrawn; nothing streams. | [ADR-0013](../adr/0013-reactive-tutor-line.md) |
| D14 | `claude-opus-5` by admin `Valve`, effort `low` per call type, thinking never disabled. Five segment-1 prefixes, each of which must clear the 512-token floor. | [ADR-0014](../adr/0014-model-choice-and-effort.md) |
| D15 | The domain runs as its own service; `fetch` is the only answer path; requests carry a rotating capability token; the prototype targets a default install. | [ADR-0015](../adr/0015-iframe-pipe-transport.md) |

## Approach

Built in dependency order; each step is testable before the next begins. The Pipe
comes **last** — it is the thinnest piece and depends on everything.

### 1. Domain types and the mode registry

`DifficultyMode` is an enum (`NOVICE`, `ADVANCED`), but the enum alone is not the
extension point, because behaviour differs per mode in six places. A `ModeRegistry`
maps each mode to a `ModePolicy` bundling all six:

- the JSON schema fragment sent for authoring,
- the grading strategy (`Deterministic` or `ModelGraded`),
- the validator rules (`novice ⇒ len(options) ≥ 2 and correct_option_id ∈ options`;
  `advanced ⇒ rubric present`),
- the render hint the iframe uses to choose an option bank or a text input,
- `blank_range` (Novice 1–2, Advanced 4–6),
- `probe_failure_behavior` (Advanced re-opens the blank; Novice corrects but leaves
  it resolved).

**Adding a third mode is one registry entry.** No branching on mode outside the
registry — an `if mode ==` anywhere else is a bug.

`Quiz`, `Blank`, `Segment` (`text | math | blank`), and the `AuthoringResult` union
per D2.

### 2. Repository ports and in-memory implementations

Per D5: `learner_id` partition, ULID keys, ≤20 blanks enforced at authoring time
rather than discovered at write time. Where `blank_range` and the 20-blank cap
disagree, `blank_range` rejects first — the cap is a storage invariant that should
never fire.

### 3. Data capture — the record shape

Capture is a first-class goal, not a byproduct of grading. The attempt record must
support later curation without a schema migration, so it is stamped and complete
from day one.

**`QuizAttempt`** — `attempt_id` (ULID), `learner_id`, `session_id`, the raw
unfilled quiz exactly as authored, `mode`, `topic`, `created_at`, `sealed_at`,
`outcome` (`in_flight | resolved | abandoned`), `probe_cadence_at_authoring`,
`queued_topics`, `model_id`, `effort`, `schema_version`, `prompt_version`, the
Anthropic `message_id`s for every call, and per-call token usage including
`cache_read_input_tokens`.

**`Guess`** (ordered, embedded) — `blank_id`, `submitted`, `verdict`,
`attempt_ordinal`, `hint_rung_shown`, `created_at`, `graded_by`
(`deterministic | model`).

**`Probe`** (ordered, embedded, peer of `Guess`) — `blank_id`, `question`,
`self_explanation` (nullable; dismissible), `verdict`, `reopened_blank`,
`cadence_at_fire`, `asked_at`, `answered_at`, `message_id`. Separate from `Guess`
because a probe **mutates blank state**: it is an event in the timeline, not a
property of a past guess, and the curation job replaying "what actually happened"
is the consumer that nesting would mislead.

Bounds are enforced, not assumed: ≤20 blanks, ≤4 guesses and ≤2 probes per blank,
so ≤80 guesses and ≤40 probes. The version stamps are what make "without a
migration" true — a curation job reading old records knows which schema and prompt
produced them.

### 4. PromptAssembler

Returns ordered segments with breakpoint markers, **per call type**:

```
[ segment 1 ] invariant tutor instructions for this call type  <- cache_control
[ segment 2 ] learner profile + quiz + blank rubrics           <- cache_control
[ tail      ] guesses so far in order, current blank + guess
```

Segment 2 renders the profile through a single `render_profile(profile) -> str`.
**The domain never reads the profile's internals**, so its shape can evolve without
touching grading, persistence, or the assembler.

There are five segment 1s, one per call type. Each is byte-identical across every
learner in the workspace; none may contain learner-specific text, and that includes
*omitting* text per learner.

### 5. ModelClient port and Anthropic adapter

Five methods per the seam table. `claude-opus-5` from an admin `Valve` — never
`UserValves`, since caches are model-scoped. `output_config.format` with
`strict: true`, `cache_control` on segments 1 and 2, `output_config.effort` fixed at
`low` and **constant per call type** (changing it invalidates that prefix).
Thinking stays adaptive and is never disabled. The adapter is the only module
importing the Anthropic SDK.

**Build gate:** `cache_read_input_tokens > 0` for **each of the five call types
separately**. Each segment 1 must clear the 512-token floor on its own; under it,
breakpoint 1 silently creates no entry, and what is lost is the cross-user sharing
segment 1 exists for.

### 6. QuizAuthoring

Two calls per D11. **Skeleton** — explanation, blanks, options — is the only
blocking one; parse, validate, persist, render. **Pedagogy payload** — hints,
reinforcements, probe questions, recap — is fired immediately after and lands while
the learner reads. Returns the `direct_answer | quiz` union; a `direct_answer` skips
the second call entirely.

### 7. Content rendering

Restricted Markdown subset — inline code, fenced code blocks with highlighting,
bold, italic, lists, links. LaTeX converted to MathML server-side via
`latex2mathml`: no client JS, no fonts, no CDN. A blank masks a **whole formula,
never a term inside one**, so the segment array stays flat; a resolved blank is
swapped for a `text` or `math` node whose MathML arrives in the grading response
already being sent. **Everything rendered passes through sanitisation**,
model-authored or not — a prompt-injected explanation is a plausible route to HTML
in the page.

### 8. QuizSession

`submit()` dispatches through `ModePolicy.grading_strategy`. Novice compares to the
stored key and returns pre-authored feedback with **no model call**. Advanced
assembles the cache-anchored block and calls `grade_answer`, whose response carries
the verdict, the probe question (nullable) and the reactive tutor line (nullable)
together. Both append a `Guess` and advance the ladder.

Probe cadence is client-owned and randomised — a coin flip per correct answer plus
the final blank unconditionally, seeded RNG in tests — and gated by `probe_cadence`.
`answer_probe()` calls `grade_probe`. On failure, `probe_failure_behavior` decides:
Advanced re-opens the blank with the ladder resuming where it left off, capped at
one re-open per blank; Novice corrects the misconception and leaves it resolved.

Seal on the unified predicate — **all blanks resolved and no probe pending** —
never on the last blank resolving, since a failed Advanced probe can un-fire
completion.

### 9. ProfileBuilder

Reads attempts after the stored watermark, increments the ledger arithmetically,
folds the narrative forward with one `fold_narrative` call, advances the watermark,
writes back. Invoked manually or on a timer; not wired to any scheduler.

### 10. The quiz service

An HTTP API over the domain package, in its own process. Owns the answer key, the
repositories, the renderer and every model call. Endpoints: author a quiz, submit an
answer, answer a probe, submit a rating.

`TokenMinter` issues an HMAC capability token scoped to one `QuizSessionId` and its
`LearnerId`, with a short TTL, minted into the `srcdoc` at render time. Every
request carries it; **every grading response returns a fresh one** which the iframe
swaps in. Sliding renewal keeps an engaged learner's session alive while the
exposure window stays at minutes. The `QuizSessionId` is not a credential.

### 11. Quiz UI

Segment-walking renderer; option buttons for Novice, text input for Advanced, chosen
by the mode's render hint. Answers go by `fetch` to the quiz service carrying the
capability token. The verdict starts the celebration; in Advanced the reactive tutor
line arrives in the same response, and Novice has none. **Nothing streams.** The
client guards the window where the quiz is playable but the pedagogy payload has not
landed. Probe prompt on a correct answer per cadence, dismissible. Rating prompt on
sealing — optional, dismissible, never entering the conversation.

### 12. Open WebUI adapter

A Pipe mapping `__user__` → `LearnerId`, calling the quiz service, returning the
`HTMLResponse` it rendered with `Content-Disposition: inline`. It holds no domain
logic and makes no model calls. The `fetch` this depends on **has been observed**,
not merely read: a spike reached a stub service from a `srcdoc` iframe sandboxed
without `allow-same-origin`, in Chrome and Edge, with `Origin: null` and both
`POST`s preflighted (`docs/research/open-webui-fit.md`). What is still unobserved is
the composition — this Pipe, inside a real default install, reaching the real quiz
service, which is acceptance 34.

## Out of scope

- **A real database.** In-memory only. The repository ports are the migration path.
- **Multi-tenant or hosted deployment.** Two local containers.
- **Authentication and account management.** Inherited from Open WebUI entirely.
  The capability token authorizes iframe→service requests; it is not a login.
- **A scheduler for the profile job.** Manual or timer trigger; no cron, no queue.
- **The curation pipeline itself.** This spec makes the data *sufficient* for
  curation; it builds no consumer of it.
- **Difficulty modes beyond Novice and Advanced.** The registry makes them cheap;
  none are added here.
- **Hardened Open WebUI deployments.** The prototype targets a default install
  (`IFRAME_CSP` unset). Under the hardening docs' recommended CSP the iframe has no
  network path to the service at all — a different architecture, not a degraded one.
  A documented constraint for the README.
- **`__event_call__` as an answer path.** The iframe cannot invoke it; it is a
  server-side call rendering a parent-page modal.
- **Blanks nested inside a formula.** Whole-formula blanks only. Deferred on the
  accessibility story, not the layout: there is no ARIA pattern for an unanswered
  blank, MathML-AAM maps almost nothing below `math`, and no production system does
  it.
- **Streaming.** Withdrawn with the parallel tutor call.
- **Fast mode.** Switching speed invalidates the prompt cache, which would split
  segment 1 into two namespaces.
- **Cross-learner analytics, dashboards, leaderboards, spaced repetition.**
- **Model fallbacks, retries, rate-limit handling** beyond SDK defaults.

## Acceptance

**Core loop**

1. A learner asks a question in Open WebUI and gets an interactive quiz overlay,
   not prose.
2. A wrong Novice answer walks the three-rung ladder and reveals on rung three.
3. An Advanced blank accepts free text and is graded on meaning: a correct answer
   phrased differently from the rubric passes.
4. A safety-relevant or "I need this now" inquiry returns `direct_answer`, and the
   parse succeeds.
5. The skeleton call renders a playable quiz before the pedagogy payload arrives,
   and a learner answering in that window is handled without error.

**Call budget**

6. Submitting a Novice **answer** returns a verdict with zero model calls —
   asserted by a `ModelClient` stub that fails the test if invoked.
7. A full Novice quiz with `probe_cadence: off` makes no model calls at all after
   authoring; the same stub survives the whole attempt.
8. Grading a probe reply calls the model exactly once, in both modes.
9. The Advanced grading response carries the verdict, the probe question and the
   reactive tutor line together — no second request, and nothing streams.

**Caching and prompt invariants**

10. `PromptAssembler` produces a byte-identical segment 1 for two different
    learners — asserted **per call type**.
11. Segment 1 is byte-identical for two learners with different `probe_cadence`
    settings. Same assertion as 10, which is the point.
12. `cache_read_input_tokens > 0` on the second call of a session, asserted
    independently **for each of the five call types**.

**Probes**

13. A failed probe in Advanced re-opens the blank and the ladder resumes at the next
    rung, not at rung 1.
14. A failed probe in Novice leaves the blank resolved.
15. A blank re-opens at most once; a second failed probe reveals and moves on.
16. An attempt does not seal while a probe is pending, and seals by the same
    predicate whether probes are on, off or dismissed.
17. Probe cadence is reproducible under a seeded RNG, and the final blank is always
    probed regardless of the seed.
18. A dismissed probe persists with a null `self_explanation` and does not block
    sealing.
19. `final_blank_only` probes the last blank and no other.
20. Changing `probe_cadence` mid-quiz takes effect on the next correct answer; a
    probe already pending is unaffected.
21. An attempt authored under `probe_cadence: off` is identifiable as suppressed
    from the record alone, with zero probes present.

**Persistence and capture**

22. A completed attempt persists the raw unfilled quiz plus every guess in the order
    made, with verdict, ladder rung and timestamp on each.
23. An attempt left incomplete persists with its guesses and an unset `sealed_at`.
24. A 1–5 Likert rating is written as a separate record; the attempt document is
    unchanged after sealing.
25. Authoring rejects a quiz with more blanks than the mode's `blank_range`, and the
    20-blank storage cap rejects independently of it.
26. "Start this instead" marks the displaced attempt `abandoned` and the new attempt
    inherits the remaining queued topics.
27. The conditional validator rejects a Novice blank with fewer than two options and
    an Advanced blank with no rubric.

**Profile**

28. `ProfileBuilder` run by hand rewrites the profile, and the next authoring call
    carries it in segment 2.
29. Running `ProfileBuilder` twice with no new attempts is a no-op: ledger figures
    unchanged, watermark unmoved.
30. Ledger figures are reproducible — recomputing from all attempts equals the
    incrementally-advanced values.

**Rendering**

31. A quiz renders correctly on a default install — formulas included, no silent
    blanks, no CDN request and no font fetch.
32. Resolving a blank whose answer is a formula returns that formula's MathML in the
    grading response, with no additional request.
33. Rendered Markdown is sanitised: an explanation containing a script tag renders
    inert.

**Transport and authorization**

34. An answer submitted by `fetch` from the sandboxed iframe reaches the quiz
    service on a default install and returns a verdict.
35. A request carrying an expired token, or one scoped to a different session, is
    rejected.
36. Every grading response carries a fresh token, and the previous one stops being
    accepted once rotated.

**Extensibility**

37. No module in the domain package imports Open WebUI — assertable by a grep test
    over imports, and structurally true across the process boundary.
38. The Pipe makes no model calls and holds no domain logic.
39. Adding a hypothetical third difficulty mode requires editing only the registry:
    no `if mode ==` outside it.
40. Flipping the mode toggle mid-quiz leaves the in-flight attempt's mode unchanged
    and applies to the next authoring call.
