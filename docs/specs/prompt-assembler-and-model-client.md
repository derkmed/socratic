# PromptAssembler and the ModelClient port

Ticket: [#4](https://github.com/derkmed/socratic/issues/4). Slice 4 and the port
half of slice 5 of [the master spec](socratic-learning-app.md). Sources:
[CONTEXT](../CONTEXT.md) (**Call type**, **Segment 1**, **Segment 2**, **Volatile
tail**, **LearnerProfile**), [ADR-0006](../adr/0006-learner-profile-lifecycle.md),
[ADR-0010](../adr/0010-per-learner-instruction-toggles.md),
[ADR-0014](../adr/0014-model-choice-and-effort.md). Nothing here is a new
decision.

## Goal

Land the two seams the rest of the system is untestable without: the
`ModelClient` **port** — the only non-deterministic dependency, five explicit
methods, one per call type — with a recording stub that can fail a test if
invoked at all; and `PromptAssembler.assemble(call_type, ...) -> PromptSegments`,
which lays a request out as an ordered list of cache segments with breakpoint
markers rather than as a wire request. Laying it out as a value is what makes
ADR-0006's invariant — no learner-specific text above breakpoint 1 — assertable
without touching the live API.

No real API implementation. The Anthropic adapter is #7.

## Seams

Both are named in the master spec's seam table; neither is new.

| Seam | Kind | Tests attach at |
|---|---|---|
| `ModelClient` | port (`Protocol`) | `RecordingModelClient` — the in-package stub. Records every call; `assert_never_called()` and `fail_if_called=True` are the two ways acceptance 6 and 7 get their "zero model calls". |
| `PromptAssembler.assemble(call_type, ...) -> PromptSegments` | pure function | Its return value. Every cache invariant is a property of the returned segment list, so no double, no clock, no network. |

One supporting seam, already required by the master spec §4:
`render_profile(profile) -> str` — the single point at which a `LearnerProfile`
reaches a prompt. The domain reads no profile internals anywhere else, so the
profile's shape is free to change (CONTEXT: LearnerProfile).

## Decisions

| # | Decision | Source |
|---|---|---|
| D6 | Profile injected as cache segment 2, never interpolated into the system prompt. No learner-specific text above breakpoint 1. | [ADR-0006](../adr/0006-learner-profile-lifecycle.md) |
| D10 | Per-learner toggles switch **client behaviour, never prompt text**. Omitting text per learner splits the cache exactly as adding it does. | [ADR-0010](../adr/0010-per-learner-instruction-toggles.md) |
| D14 | Five call types means five segment-1 prefixes, each of which must clear the 512-token floor **independently**; the invariant is asserted per call type, not once. | [ADR-0014](../adr/0014-model-choice-and-effort.md) |
| — | Five explicit `ModelClient` methods rather than a generic `complete()`: the seam exists so a stub can assert *this was never called*, and naming them keeps the call inventory visible. | CONTEXT: Call type; master spec seam table |

## Approach

Built in three steps, each red-green before the next.

### 1. `ProbeCadence`

Added to `src/socratic/domain/modes.py` — the `off | final_blank_only |
sometimes | always` `UserValves` setting from ADR-0010. It lives here rather than
in the new modules because #3 needs it too, and an identical addition on both
branches merges cleanly.

### 2. `src/socratic/domain/prompting.py`

`CallType` — the five-member enum that is the call inventory.

`PromptSegment` / `PromptSegments` — **named unambiguously**, because
`types.Segment` already means an element of the explanation token stream
(`text | math | blank`). Same English word, different concept; they must never be
confused. A `PromptSegment` carries a `SegmentRole`, its rendered `text`, and a
`cache_control` flag — the breakpoint marker.

`LearnerProfile` — minimal here, the ledger/narrative pair of ADR-0008 with
nothing else. #16 fleshes it out.

`render_profile(profile) -> str` — the single reader of profile internals.

`assemble(call_type, ...) -> PromptSegments` — returns, in order:

```
[ segment 1 ] invariant tutor instructions for this call type  <- cache_control
[ segment 2 ] learner profile + quiz + blank rubrics           <- cache_control
[ tail      ] guesses so far in order, current blank + guess
```

There are **five segment 1s**, one per call type, each its own module-level
constant. Segment 2 renders the profile through `render_profile`, then the quiz
and its blanks. The tail is never marked cacheable.

`assemble` accepts `probe_cadence` and deliberately does nothing with it. The
parameter is there so acceptance 11 asserts something real: it puts the setting
in the one function where somebody would be tempted to branch on it, and the test
locks that branch out.

### 3. `src/socratic/domain/model_client.py`

`ModelClient` — a `Protocol` with exactly five methods, one per `CallType`, each
taking a `PromptSegments` and returning a `ModelResponse` (the structured-output
payload verbatim, the Anthropic `message.id` as a host annotation, and the cache
token counters #7's build gate reads).

`RecordingModelClient` — the stub. Records `(call_type, segments)` per call,
serves canned responses per call type, and offers two ways to assert nothing was
called: `fail_if_called=True` (raises at the moment of the call, so the traceback
points at the culprit) and `assert_never_called()` (checked after the fact).

## Out of scope

- **Any real API implementation.** No `anthropic` import, no HTTP, no request
  body construction. `assemble` returns segments, *not* a wire request — that
  translation is #7's, and keeping it out is what makes this seam assertable.
- **The live cache gate** (acceptance 12, master spec §5). `cache_read_input_tokens
  > 0` per call type needs a real call. A character-count floor stands in here as
  a cheap structural proxy for the 512-token floor; it is not the gate.
- **`validate_quiz`** — #2's entry point.
- **`QuizAttempt`, `Guess`, `Probe`, and the repository ports** — #3's. The
  volatile tail therefore takes guesses as already-rendered strings in order; #3's
  `Guess` will supply them.
- **The full `LearnerProfile`** — #16. Minimal here, and #3 introduces a minimal
  one of its own for its repository port; the two reconcile at merge time.
- **`LearnerId`** as a named alias — #3's `ids.py` addition. Annotated `str` here
  to avoid defining the same alias twice.
- **Prompt text quality.** The five segment-1 bodies are real instructions, but
  tuning them is measurement work, not this ticket.

## Acceptance

1. `ModelClient` exposes exactly the five call-type methods — `author_skeleton`,
   `author_pedagogy`, `grade_answer`, `grade_probe`, `fold_narrative` — and no
   generic `complete()`. Asserted against the Protocol's member set.
2. The stub records every call, and can fail a test if invoked at all.
3. `assemble` produces a **distinct** segment 1 per call type — five different
   texts, five different prefixes.
4. For each of the five call types, segment 1 is byte-identical for two
   different learners (master spec acceptance 10).
5. For each of the five call types, segment 1 is byte-identical for two learners
   with different `probe_cadence` settings (master spec acceptance 11).
6. A test demonstrates that *omitting* text per learner is caught, not just
   adding it — a deliberately-broken assembler that drops a sentence for
   `probe_cadence: off` fails the same assertion.
7. No learner-specific text appears above breakpoint 1: the learner id, the
   ledger's contents and the narrative are all absent from segment 1, asserted
   per call type.
8. The profile reaches segment 2 only through `render_profile`; no other module
   in `src/socratic` reads a profile field, asserted by a source scan.
9. The volatile tail carries the guesses in order, then the current blank and
   guess, and is never marked cacheable.
