# Splitting authoring into skeleton plus pedagogy payload

Issue [#9](https://github.com/derkmed/socratic/issues/9). Completes the master
spec's [section 6](socratic-learning-app.md), which
[`quiz-authoring.md`](quiz-authoring.md) deliberately narrowed to the skeleton
call while there was only one call to make.

## Goal

Authoring becomes two calls (D11,
[ADR-0011](../adr/0011-latency-budget.md)). **`author_skeleton`** — explanation,
blanks, options and rubrics — is the only blocking one: parse, validate,
persist, return a playable quiz. **`author_pedagogy`** — reinforcements, hint
rungs and the recap — is fired immediately after and lands while the learner
spends 30-60 seconds reading 250 words; it merges into the **in-flight attempt**
rather than into a copy of the quiz held in memory, so a learner who answers
inside the window loses nothing. A `direct_answer` fires no second call at all.

The obstacle this spec exists to remove was reported by
[#32](https://github.com/derkmed/socratic/pull/32): a true skeleton-only payload
could not pass the gate, because `ModePolicy.authoring_schema_fragment` marks
every pedagogy field required and `validate_blank` refuses a Novice blank
without three hints. So the schema fragments and the conditional validator are
relaxed **together**, and both are relaxed by **decomposition rather than
deletion**.

## Seams

No new seam class. One existing seam gains a second method, and two existing
pure-function seams gain a stage.

| Seam | Kind | Status |
|---|---|---|
| `QuizAuthoring.author(inquiry, learner_id, ...) -> AuthoringResult` | service | Existing (master spec seam table). Unchanged signature; it now makes the skeleton call only and validates at the skeleton stage. |
| `QuizAuthoring.author_pedagogy(quiz, learner_id) -> QuizAttempt` | service | **New method on the existing seam.** Not a new seam: it is the second half of the same service, and giving the follow-up call its own entry point is what makes "a `direct_answer` fires no pedagogy call" assertable as a *call that was never made*. |
| `validate_quiz(quiz, registry, *, stage)` / `ensure_valid_quiz` | pure function | Existing. Gains `stage`, defaulting to `COMPLETE`, so every caller that does not know about stages is unaffected. |
| `ModePolicy.rules_for(stage) -> StageRules` | registry | Existing seam extended. The one place a stage's schema fragment and blank validator are chosen, and it is inside the registry — where per-mode rules are required to live. |
| `ModelClient.author_pedagogy(segments)` | port | Existing, never yet called. Driven through `RecordingModelClient`; no new double. |
| `AttemptRepository` | port | Existing. The merge re-reads through `list_for_learner` and writes back through `save`. |

## Decisions

| # | Decision | Source |
|---|---|---|
| D11 | Split authoring into two calls; the skeleton is the only blocking one; the client guards the case of a learner who answers before the payload arrives. | [ADR-0011](../adr/0011-latency-budget.md) |
| D2 | One `Blank` type with nullable mode-specific fields plus an explicit conditional validator; `strict: true` cannot express the conditional invariants. | [ADR-0004](../adr/0004-quiz-wire-format.md) |
| D5 | The attempt is the bounded document holding the raw unfilled quiz. | [ADR-0005](../adr/0005-persistence-contract.md) |
| D7 | Every `message.id` is a host annotation persisted for accountability — **both** calls'. | [ADR-0007](../adr/0007-quiz-session-identity.md), CONTEXT: Host annotations |
| — | Per-mode behaviour lives in `ModePolicy` and nowhere else; an `if mode ==` outside the registry is a bug. | CONTEXT: ModeRegistry |
| — | An attempt that is **sealed** will never be written again. | CONTEXT: Sealed |

## Approach

### 1. `AuthoringStage`, and stage rules in the registry

`AuthoringStage` is `SKELETON | PEDAGOGY | COMPLETE`. The first two name the two
authoring calls; `COMPLETE` names the merged result and is what every existing
caller means.

`StageRules` bundles what one stage asks of a blank: a `schema_fragment` (the
call's output schema) and a `validate_blank`. `ModePolicy` gains one field,
`stage_rules: Mapping[AuthoringStage, StageRules]`, and one method,
`rules_for(stage)`. The six existing fields are untouched: they *are* the
`COMPLETE` rules, which is what they already were.

**The relaxation is a decomposition.** For each shipped mode the complete
validator is defined as its skeleton rules followed by its pedagogy rules, so
`COMPLETE` is not a re-derivation of the old rules — it is the old rules,
partitioned:

- **Novice skeleton** — at least two options, a `correct_option_id` naming one
  of them, no rubric.
- **Novice pedagogy** — a reinforcement, and three hints, one per ladder rung.
- **Advanced skeleton** — a rubric, and no option bank.
- **Advanced pedagogy** — no reinforcement and no hints; Advanced feedback is
  the reactive tutor line on the grading response
  ([ADR-0013](../adr/0013-reactive-tutor-line.md)), so Advanced has no
  *per-blank* pedagogy and its payload carries only the recap.

A mode whose policy declares no `stage_rules` — a third mode registered with the
six fields alone — is validated by its complete rules at **every** stage. The
fallback is the strict direction on purpose: a policy that predates staged
authoring cannot be silently relaxed by code it has never heard of.

The **rubric is a skeleton field**, not a pedagogy one. ADR-0011 lists it in
neither payload, and the skeleton must be *playable*: an Advanced blank with no
rubric cannot be graded, so putting it behind the second call would make the
window the ADR requires to be answerable exactly the window in which an Advanced
answer cannot be graded.

### 2. Stage on the validator

`validate_quiz(quiz, registry, *, stage=AuthoringStage.COMPLETE)` looks the
per-blank rules up through `policy.rules_for(stage)`. The two bounds —
`blank_range` and `STORAGE_BLANK_CAP` — apply at every stage, because the
skeleton already declares every blank.

One quiz-level rule is added, and it fires at `COMPLETE` only: a completed quiz
needs a **recap**. Without it a mode with no per-blank pedagogy (Advanced) would
have nothing at all that `COMPLETE` checks, and the pedagogy call could return
an empty payload and be called complete.

### 3. Skeleton authoring

`author` is unchanged except that it validates at `SKELETON` and parses `recap`
as optional, defaulting to `""` — the recap is a pedagogy field, and a skeleton
response is not malformed for omitting it. A skeleton that carries one anyway is
kept.

The hand-rolled `_with_inquiry` helper is dropped in favour of
`assemble(..., inquiry=...)`, the slot `prompting.py` now provides for exactly
this. The inquiry stays in the volatile tail either way.

Both calls stamp a `records.ModelCallRecord` on the attempt, built from
`ModelResponse.token_usage()` rather than by reassembling the four counters by
hand.

### 4. The pedagogy payload

`author_pedagogy(quiz, learner_id)`:

1. Re-reads the in-flight attempt for that quiz's session from the repository.
   **Re-reading is the race guard**: guesses appended while the payload was in
   flight are in the stored attempt and would be discarded by a merge into a
   stale in-memory copy.
2. Assembles `CallType.AUTHOR_PEDAGOGY` with the quiz in segment 2 and calls
   `ModelClient.author_pedagogy`.
3. Merges the payload into the attempt's quiz: the recap, and per blank the
   reinforcement and the three hints.
4. Validates the merged quiz at `COMPLETE` and saves the attempt, stamped with
   the second `ModelCallRecord`.

A payload that names a blank the quiz does not declare is an
`AuthoringParseError`; a merged quiz that is still incomplete is a
`QuizValidationError`. Neither is written, so a failed pedagogy call leaves the
learner with the playable skeleton that was already persisted.

If the attempt has **sealed** before the payload lands — a short Novice quiz
answered inside the window — the payload is dropped and the attempt is returned
unchanged. A sealed attempt is never written again (CONTEXT: Sealed), and
pedagogy for blanks that are all resolved has no reader.

## Out of scope

- **The submit path, and `QuizSession`.** [#6](https://github.com/derkmed/socratic/issues/6).
  Acceptance 5 has two halves; this spec discharges the first — the quiz is
  valid and playable on the skeleton alone, the merge preserves an in-flight
  attempt's guesses, and a blank with absent pedagogy is a representable,
  tested state. The second half — a *verdict* returned when the hint text for a
  blank has not arrived — is #6's ladder, and this spec does not claim it.
- **Firing the second call concurrently.** The domain is synchronous and
  stdlib-only; `author_pedagogy` is a method the quiz service
  ([#10](https://github.com/derkmed/socratic/issues/10)) calls off the render
  path. Choosing the concurrency mechanism is that ticket's, not this one's.
- **Pre-authored Novice probe questions.** ADR-0011 puts them in this payload,
  but `types.Blank` has no field to hold one and `types.py` belongs to the
  tickets that own the probe machinery. Filed separately.
- **Rendering the skeleton, and rendering a blank whose hints are absent.**
  [#11](https://github.com/derkmed/socratic/issues/11).
- **A repository lookup by session id.** `author_pedagogy` scans
  `list_for_learner`, which is what the port offers; adding an index is
  [ADR-0005](../adr/0005-persistence-contract.md)'s successor's problem.
- **Retries, fallbacks, and what happens if the pedagogy call never returns.**
  Master spec out-of-scope. The quiz stays playable without it, which is the
  point of the split.

## Acceptance

1. `author` makes exactly one call, `author_skeleton`, and `author_pedagogy` is
   never called from it.
2. A skeleton payload carrying **no** reinforcement, hints or recap authors a
   valid, persisted, playable quiz; the same payload fails `validate_quiz` at
   `COMPLETE`.
3. A fully-populated Novice blank still requires two options, a
   `correct_option_id` among them, a reinforcement and three hints at
   `COMPLETE`; a fully-populated Advanced blank still requires a rubric.
   Acceptance 27 is unchanged.
4. A Novice skeleton blank with fewer than two options is rejected at
   `SKELETON`; an Advanced skeleton blank with no rubric is rejected at
   `SKELETON`.
5. `author_pedagogy` merges the recap and per-blank pedagogy into the stored
   attempt, and the merged quiz validates at `COMPLETE`.
6. Guesses appended to the attempt while the payload was in flight survive the
   merge.
7. A `direct_answer` returns without any `author_pedagogy` call being recorded.
8. The attempt carries two `ModelCallRecord`s — `author_skeleton` and
   `author_pedagogy` — each with its own `message_id` and its own token usage.
9. A mode registered with the six fields alone is validated by its complete
   rules at every stage.
10. A pedagogy payload naming an unknown blank raises `AuthoringParseError` and
    writes nothing; an incomplete merge raises `QuizValidationError` and writes
    nothing.
11. A sealed attempt drops a late payload without error and is returned
    unchanged.
