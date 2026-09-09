# QuizSession.submit — Novice grading and the three-rung ladder

Issue [#6](https://github.com/derkmed/socratic/issues/6). Narrows the master
spec's [section 8](socratic-learning-app.md) to the **deterministic grading path
only**. Advanced model-graded submission is
[#8](https://github.com/derkmed/socratic/issues/8); probe firing and
`answer_probe` are [#10](https://github.com/derkmed/socratic/issues/10).

## Goal

The hot path. A learner clicks an option and gets a verdict back with **zero
model calls**. `QuizSession.submit(...)` reads the in-flight `QuizAttempt`,
dispatches through `ModePolicy.grading_strategy`, and — for the deterministic
strategy — compares the submission to the stored key and returns pre-authored
feedback. A correct answer returns the blank's reinforcement; a wrong answer
walks the three-rung hint ladder and reveals on rung three. Every submission
appends a `Guess` in the order made, carrying its verdict, the rung showing,
`graded_by` and a timestamp. When every blank is resolved and no probe is
pending, the attempt seals.

The answer key never leaves the backend: `submit` takes an option id and returns
a verdict, and the correct option id appears in the result only once the ladder
has revealed it.

## Seams

One existing seam is implemented; no new seam is introduced.

| Seam | Kind | Status |
|---|---|---|
| `QuizSession.submit(...) -> Submission` | service | **The seam this spec builds.** Already named in the master spec's seam table. Tests attach here. `.answer_probe(...)` is the other half of that seam and belongs to [#10](https://github.com/derkmed/socratic/issues/10). |
| `ModelClient` | port | Existing. Driven through `RecordingModelClient`, which *is* the double. Its `fail_if_called=True` and `assert_never_called()` are exactly what acceptance 6 and 7 assert with; no second stub is written. |
| `AttemptRepository` | port | Existing. Driven through `InMemoryAttemptRepository`, which *is* the double. |
| `ModeRegistry.policy_for(mode).grading_strategy` | lookup | Existing. Dispatch reads it; the session never names a mode. |
| `QuizAuthoring.author` | service | Existing. Acceptance 7 walks a quiz it authored, so the two halves meet at the repository rather than at a fixture. |

Deliberately **not** seams:

- **The grading strategies themselves.** They are module-level functions in a
  table keyed by `GradingStrategy`, exercised through `submit`. A public entry
  point of their own would be a second way into the same code.
- **The seal predicate.** Exposed as a module-level pure function only so its
  two clauses can be read in one place; it is asserted through `submit`.

## Decisions

| # | Decision | Source |
|---|---|---|
| D4 | Novice is pre-authored and graded with **no model call**; the answer key never leaves the backend. | [ADR-0003](../adr/0003-grading-authority-and-key-custody.md) |
| D13 | The reactive tutor line is Advanced-only and rides the grading response. **Novice has none**, which is what makes "zero model calls" true without qualification. | [ADR-0013](../adr/0013-reactive-tutor-line.md) |
| D10 | Sealing is **one unified predicate** — all blanks resolved and no probe pending — never a "last blank resolved" check, because a failed Advanced probe can un-fire completion. | [ADR-0010](../adr/0010-per-learner-instruction-toggles.md) |
| D5 | The attempt is one bounded document; guesses are embedded in order; ≤4 guesses per blank. Records are immutable and accumulate through `with_guess` / `sealed`. | [ADR-0005](../adr/0005-persistence-contract.md) |
| — | The hint ladder is three escalating rungs; the client counts attempts and selects the rung, and the model authored the text. | [CONTEXT: Hint ladder / rung](../CONTEXT.md) |
| — | `ModeRegistry` is the only place mode is branched on; an `if mode ==` elsewhere is a bug. | [CONTEXT: ModeRegistry](../CONTEXT.md) |

## Approach

### 1. Reading the attempt's blank state

Resolution is **derived from the record**, never stored twice. A blank is
resolved when its guesses contain a correct one, or when three incorrect guesses
have exhausted the ladder and the answer was revealed. `attempt.guesses` is
already ordered and already bounded, so the session needs no state of its own —
which is what keeps quiz state client-authoritative (D1) with the attempt as the
single stored truth.

### 2. The seal predicate, both clauses

```
sealed ⇔ every blank resolved  ∧  no probe pending
```

Written as one function reading both clauses off the attempt. The probe clause
is live from the start — an unanswered probe on the attempt blocks sealing —
even though nothing in this ticket appends one; [#10](https://github.com/derkmed/socratic/issues/10)
supplies the firing and the dismissal distinction. Establishing the predicate
now is what stops the no-probe case from being written as "the last blank
resolved" and then having to be unwritten.

### 3. Dispatch on `grading_strategy`

A module-level mapping from `GradingStrategy` to a grader function. `submit`
looks up the policy for the attempt's mode, reads `grading_strategy`, and calls
the grader the table names. The session never mentions `DifficultyMode.NOVICE`.
`MODEL_GRADED` maps to a grader that raises `NotImplementedError` naming #8, so
an Advanced submission fails loudly at the dispatch point rather than being
silently graded by the wrong strategy.

### 4. Deterministic grading and the ladder

Compare `submitted` to `blank.correct_option_id`.

- **Correct** — verdict `CORRECT`, feedback is the blank's `reinforcement`, no
  rung shown, blank resolved.
- **Wrong** — verdict `INCORRECT`, rung = the number of guesses now made against
  this blank (1, 2, 3). Feedback is `blank.hints[rung - 1]`. On rung three the
  answer is revealed: the result carries the correct option id and the blank is
  resolved.

**Absent pedagogy is tolerated, not fatal.** [#9](https://github.com/derkmed/socratic/issues/9)
splits authoring into skeleton and pedagogy payload, so a learner can answer in
the window before `hints` and `reinforcement` have landed (master spec
acceptance 5). A blank whose `hints` is `None` still grades, still advances the
ladder, still reveals on rung three, and returns `feedback=None` rather than
raising. The skeleton carries `correct_option_id`, so grading itself is never
blocked; a blank missing that is a wiring error and raises.

### 5. Appending the guess and sealing

One `Guess` per submission, in the order made: `blank_id`, `submitted`,
`verdict`, `attempt_ordinal` (1-based within the blank), `hint_rung_shown`
(`None` when correct), `created_at` from the injected clock, and `graded_by` set
to the policy's `grading_strategy` — the same enum D4 already named, so the
record says which path graded it without a parallel taxonomy. The attempt is
re-read, extended and saved through the repository; if the predicate now holds,
it is sealed in the same write.

Submitting against a blank that is already resolved raises, and so does
submitting against an attempt that is already sealed — the record refuses the
second one on its own.

## Out of scope

- **Advanced grading.** `MODEL_GRADED` raises `NotImplementedError`; the
  cache-anchored `grade_answer` call is [#8](https://github.com/derkmed/socratic/issues/8).
- **The reactive tutor line.** Advanced-only by D13, and Novice must not have
  one for acceptance 6 to hold.
- **Probes.** No probe is fired, no `answer_probe`, no cadence coin flip, no
  `probe_failure_behavior`. [#10](https://github.com/derkmed/socratic/issues/10).
  The seal predicate's probe clause is live, so an attempt carrying an
  unanswered probe will not seal — but nothing here creates one, and a
  *dismissed* probe is not yet distinguishable from a pending one in the record.
- **The authoring split.** `authoring.py` is untouched; this ticket only makes
  the ladder tolerant of the payload arriving late.
- **Resolving the explanation's segments.** `types.resolve_blank` exists; the
  rendering ticket ([#11](https://github.com/derkmed/socratic/issues/11)) owns
  swapping a resolved blank's node and the MathML that rides the response.
- **The HTTP layer and the capability token.** Not a seam.
- **Ratings.** A separate record on a separate ticket.

## Acceptance

1. Submitting a Novice answer returns a verdict with **zero model calls**,
   asserted by a `RecordingModelClient(fail_if_called=True)` handed to the
   session — the stub fails the test the moment it is invoked (master spec
   acceptance 6).
2. A **full** Novice quiz authored through `QuizAuthoring` with
   `probe_cadence: off`, walked to sealing, makes no model call after the
   authoring call: the same stub instance serves both, its call count is 1
   before the first submission and 1 after the attempt seals, and
   `assert_never_called` holds for every other call type (master spec
   acceptance 7).
3. A wrong Novice answer walks rungs 1, 2, 3 and reveals the correct option on
   rung three (master spec acceptance 2).
4. Each guess persists with its verdict, `hint_rung_shown`, `graded_by` and
   `created_at`, in the order made.
5. Sealing goes through the unified predicate: an attempt with every blank
   resolved but an unanswered probe on it does **not** seal.
6. Grading dispatches through `ModePolicy.grading_strategy` — a registry whose
   policy for the quiz's mode names `MODEL_GRADED` sends the same Novice quiz
   down the model-graded path, proving the strategy and not the mode is what is
   read. `tests/test_registry.py`'s mode-branch scan stays green.
7. A blank whose `hints` and `reinforcement` are absent still grades, still
   reveals on rung three, and returns `feedback=None` rather than raising.
