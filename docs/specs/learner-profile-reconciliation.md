# One LearnerProfile — reconciling the records and prompting duplicates

## Goal

`main` carries two frozen dataclasses named `LearnerProfile` in
`socratic.domain`, from two PRs that ran in parallel and never met:
`records.LearnerProfile` (`learner_id`, `narrative`, `watermark`, `updated_at`)
is what `LearnerProfileRepository` round-trips, and
`prompting.LearnerProfile` (`learner_id`, `ledger`, `narrative`) is what
`render_profile` renders into segment 2. Nothing converts between them, so the
profile the repository returns cannot be rendered and the profile the assembler
renders cannot be stored (#36).

This slice collapses them into **one** type carrying the union of the fields,
living in a new `socratic.domain.profiles` module that both persistence and the
assembler import. It unifies the **type** and its round-trip; it does not build
`ProfileBuilder`.

## Seams

Three already exist; one module is new, and it is where the type lives rather
than a new behaviour seam.

| Seam | Kind | Why it must exist |
|---|---|---|
| `prompting.render_profile(profile) -> str` | existing | ADR-0006/ADR-0008: the single point at which profile internals are read. The unified type has to render, and rendering has to stay deterministic over the ledger. |
| `LearnerProfileRepository.save / .get` | existing (ADR-0005) | The round-trip is half of the reported defect: what is stored must be what renders. |
| `find_profile_internal_reads` in `tests/test_prompting.py` | existing (#30, #31) | The guard is the only thing holding the profile abstraction shut. Adding fields to the type is exactly when it can quietly stop meaning anything. |
| `socratic.domain.profiles.LearnerProfile` | new module, no new behaviour seam | The type needs one home that neither `records.py` nor `prompting.py` has to import from the other to reach. Its tests attach to construction, defaults and immutability. |

The acceptance seam that proves #36 is closed is the composition of the first
two: save a profile, read it back, render it — one test, no conversion function
in between.

## Decisions

- **[ADR-0008](../adr/0008-learner-profile-composition.md)**, CONTEXT:
  *LearnerProfile* — a profile is a **ledger** (structured, exactly recomputed)
  plus a **narrative** (LLM-authored prose, folded forward). Where they
  disagree, the ledger is authoritative, which is why `render_profile` puts it
  first. The union of the two current classes is therefore
  `learner_id`, `ledger`, `narrative`, `watermark`, `updated_at`.
- **ADR-0008**, CONTEXT: *Watermark* — the watermark is the last attempt
  `ProfileBuilder` processed; it belongs to the profile, not to a separate
  record.
- **[ADR-0006](../adr/0006-learner-profile-lifecycle.md)**, CONTEXT:
  *LearnerProfile* — the domain never reads the profile's internals; it reaches
  a prompt only through `render_profile`. The shape is therefore free to change,
  and the guard in `tests/test_prompting.py` is what keeps that true.
- **#30 / #31** — `learner_id` is CONTEXT's "partition key for everything" and
  is deliberately **not** a profile internal. Treating it as one took `main`
  red.
- **ADR-0005**, `repositories.py` module docstring — records are frozen
  dataclasses over immutable containers, so a stored value cannot be mutated
  through a reference a caller kept.
- **CONTEXT: Portability seam** — stdlib only below the seam; frozen
  dataclasses with `slots=True`, matching `records.py` and `prompting.py`.

## Approach

1. **New module `src/socratic/domain/profiles.py`** holding the single
   `LearnerProfile`: `learner_id`, `ledger: Mapping[str, int]`,
   `narrative: str = ""`, `watermark: str | None = None`,
   `updated_at: datetime | None = None`. Frozen, `slots=True`. The ledger is
   sealed into a read-only mapping at construction so a caller's dict cannot be
   mutated after `save`.

   Why a third module rather than either existing one: `records.py` is the
   persisted *attempt* record shape and `prompting.py` is the prompt
   assembler. Putting the profile in `records.py` makes the assembler import
   the attempt records; putting it in `prompting.py` makes the persistence
   ports import the assembler, which is backwards. A module of its own leaves
   both dependencies pointing at a leaf.

2. **`narrative` is `str`, not `str | None`.** The two classes disagreed;
   `""` is chosen because one representation of "nothing folded yet" is
   cheaper than two, `render_profile` already tests it for truthiness, and it
   is what persistence round-trips today.

3. **`records.py` loses its `LearnerProfile`** entirely — no alias. Nothing on
   `main` or on the open PRs imports it from there.

4. **`prompting.py` re-exports** `LearnerProfile` from `profiles`, so
   `socratic.domain.prompting.LearnerProfile` keeps working for PR #32
   (`authoring.py`) and PR #35. `render_profile` renders the unified type.

5. **`repositories.py`** imports the profile from `profiles` and the attempt
   records from `records`.

6. **The guard keeps its meaning.** `PROFILE_INTERNALS` stays
   `("ledger", "narrative")` — the shape-bearing fields `render_profile` reads.
   `watermark` and `updated_at` join `learner_id` as fields the guard does not
   forbid: they are `ProfileBuilder`'s bookkeeping, and #16 must read the
   watermark to advance it. A test records that, mirroring the `learner_id`
   test, so the exclusion is deliberate rather than an oversight.

7. **Tests move with the type**: `tests/test_profiles.py` for construction,
   defaults, immutability and the store-then-render acceptance;
   `tests/test_records.py` drops its `TestLearnerProfile`;
   `tests/test_repositories.py` round-trips the unified type including the
   ledger.

## Out of scope

- **`ProfileBuilder` (#16).** No folding, no watermark advancement, no
  incremental recomputation, no `with_*` accumulator on the profile. The type
  is deliberately left thin enough that #16 owns how it changes.
- **Ledger key vocabulary.** ADR-0008 names the categories (topic counts, mode
  history, weak-area tallies, outcome counts); nothing here fixes a key format
  or a schema for them. `Mapping[str, int]` stays as loose as it is today.
- **A `LearnerId` type.** `learner_id` stays `str`, as both classes have it.
- **`updated_at` semantics.** Carried and round-tripped, never set or read by
  the domain; who stamps it is #16's.
- **Repository storage changes.** No new port method, no query by watermark.
- **`assemble` and the volatile tail** — being edited concurrently for #34.
  Changes here stay inside `LearnerProfile`, `render_profile` and imports.
- **Removing the `prompting.LearnerProfile` alias.** It stays until #32 and
  #35 land and can import from `profiles` directly.

## Acceptance

1. `socratic.domain` defines exactly one class named `LearnerProfile`.
2. It carries `learner_id`, `ledger`, `narrative`, `watermark`, `updated_at`,
   is frozen, and uses `slots=True`.
3. A profile saved through `InMemoryLearnerProfileRepository` and read back
   compares equal, ledger and watermark included.
4. A profile read back from the repository renders through
   `prompting.render_profile` — the defect #36 reports — with no conversion.
5. `render_profile` is still deterministic over ledger insertion order, still
   puts the ledger before the narrative, and still handles an empty profile.
6. `socratic.domain.prompting.LearnerProfile` still resolves to that one class.
7. Mutating the dict a caller passed as the ledger does not change the profile.
8. `find_profile_internal_reads` still reports `profile.ledger` /
   `profile.narrative` read anywhere but `domain/prompting.py`, and the full
   suite is green.
