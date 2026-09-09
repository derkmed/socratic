# ProfileBuilder — ledger, narrative and watermark

Issue #16. Spec section 9, D6/D8, ADR-0006, ADR-0008. Acceptance 28, 29, 30.

## Goal

The offline job that advances a `LearnerProfile`. One run reads the attempts
after the stored **watermark**, increments the **ledger** arithmetically, folds
the **narrative** forward with exactly one `fold_narrative` call, advances the
watermark, and writes the profile back. Built incrementally over all history,
never a recency window, and never on a learner's critical path. Nothing here is
wired to a scheduler: a run happens because somebody, or a timer, called `run`.

## Seams

All existing. No new port, no new repository, no new test double.

- **`AttemptRepository.list_for_learner`** — the only read path. It already
  returns a learner's partition oldest-first by ULID, which is the ordering the
  watermark comparison depends on.
- **`LearnerProfileRepository.get` / `save`** — read the stored profile, write
  the advanced one. `InMemoryLearnerProfileRepository` is the double.
- **`ModelClient.fold_narrative`**, exercised through `RecordingModelClient` —
  `call_count(CallType.FOLD_NARRATIVE)` is how "exactly one call per run" and
  "zero calls on a no-op run" become assertions rather than intentions.
- **`prompting.assemble` / `render_profile`** — the profile reaches a prompt
  through these and nowhere else, which is what keeps segment 1 byte-identical.

Two new *functions* (not new seams — they are the unit under test):
`recompute_ledger`, so acceptance 30 has a second, independent code path to
compare against, and `sealed_prefix`, which both paths share so the comparison
is meaningful rather than tautological.

## Decisions

Settled upstream; restated here because the code depends on them.

- The profile has two parts and **the ledger is authoritative** where they
  disagree (ADR-0008, CONTEXT: LearnerProfile). The builder therefore never
  derives a figure from the narrative, in either direction.
- The **watermark is the last attempt processed** (CONTEXT: Watermark), held as
  the attempt's ULID. ULIDs are order-preserving (ADR-0007, `ids.py`), so
  "attempts after the watermark" is a lexicographic comparison.
- The profile is injected as **segment 2 only** (ADR-0006). Segment 1 is
  byte-identical across learners; a built profile changes nothing about that.
- The narrative fold is **one call** (spec section 9), offline (ADR-0006).
- `LearnerProfile` is frozen with a sealed `MappingProxyType` ledger, so the
  builder constructs a new profile rather than advancing one in place.

Decided here, and flagged in the PR:

- **The watermark boundary is exclusive.** The stored value names an attempt
  already folded in; a run processes ids strictly greater than it. The
  alternative — storing the first *unprocessed* id — has no value to store
  before the first run and after the last one.
- **An unsealed attempt is a barrier.** A run advances through the *contiguous
  sealed prefix* of the new attempts and stops at the first in-flight one. An
  in-flight attempt still accumulates guesses, so counting it now and never
  again would make the ledger unreproducible; skipping past it would lose it
  forever, since the watermark only moves forward. `sealed_prefix` is the single
  implementation of this rule, used by both the incremental and the from-scratch
  path.
- **A no-op run writes nothing at all** — not the profile, not `updated_at`.
  Acceptance 29 asks for the watermark unmoved and the figures unchanged; not
  writing is the strongest way to deliver that.

## Approach

`src/socratic/domain/profile_builder.py`, stdlib only.

1. **Ledger key vocabulary.** Flat `Mapping[str, int]`, namespaced with `/`:
   `attempts/total`, `attempts/mode/<mode>`, `topics/<topic>`,
   `outcomes/<outcome>`, `guesses/total`, `guesses/correct`,
   `guesses/incorrect`, `probes/asked`, `probes/correct`, `probes/incorrect`,
   `probes/dismissed`, `weak/<topic>` (incorrect guesses and failed probes,
   tallied by topic — the weak-area tally). Mode and outcome are **keys, never
   branches**: nothing here asks which mode it is.
2. **`sealed_prefix(attempts, after=None)`** — the eligibility rule above.
3. **`tally(attempts)`** — the figures for one batch of attempts.
4. **`merge_ledgers(base, delta)`** — arithmetic increment, returning a new
   dict.
5. **`recompute_ledger(attempts)`** — from scratch over all history, via
   `sealed_prefix` + `tally`. The thing that makes "the ledger is authoritative"
   checkable.
6. **`ProfileBuilder(attempts, profiles, model, clock)`** with one method,
   `run(learner_id) -> LearnerProfile`: read, prefix, increment, fold once,
   advance, save.
7. **The fold.** `prompting.assemble(CallType.FOLD_NARRATIVE, ...)` with the
   *already-incremented* ledger and the *previous* narrative, and the new
   attempts rendered one line each into the volatile tail. The response's
   `content` is the call's structured payload; the builder parses it as a JSON
   object and reads `narrative`.

## Out of scope

- **Any scheduler, cron entry, queue or timer.** `run` is called by a human or
  by something outside this package. Nothing in the module starts a thread,
  sleeps, or schedules.
- **Recording the fold's `ModelCallRecord`.** Model calls are stamped on a
  `QuizAttempt`, the processed attempts are sealed, and a sealed attempt is
  never written again. Where offline call accounting lands is a separate
  question.
- **Changing `profiles.py`.** The shape it shipped with is sufficient.
- **Changing `prompting.py`.** The fold rides the existing `assemble`
  signature.
- **A periodic re-grounding rebuild of the narrative.** ADR-0008 names the
  drift and accepts it; `recompute_ledger` re-grounds the figures only.
- **Cross-learner batch runs**, backfill tooling, and any HTTP surface.

## Acceptance

1. A run by hand rewrites the profile, and a subsequent `assemble` call carries
   its ledger and narrative in segment 2 (acceptance 28).
2. Two runs with no new attempts in between: the second changes no ledger
   figure, leaves the watermark where it was, and makes **zero**
   `fold_narrative` calls (acceptance 29).
3. `recompute_ledger` over all of a learner's attempts equals the ledger reached
   by several incremental runs — including when an in-flight attempt sat in the
   middle of the history (acceptance 30).
4. Exactly one `fold_narrative` call per advancing run, asserted by count.
5. The attempt whose id *equals* the watermark is not reprocessed; the next one
   is.
6. A narrative that contradicts the ledger changes no figure.
7. Segment 1 stays byte-identical for two learners with different built
   profiles, per call type.
8. The module imports no scheduler, no timer, no queue, and no third-party
   package.
