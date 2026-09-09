# Self-explanation probes: cadence, grading, re-open, sealing gate

Issue [#10](https://github.com/derkmed/socratic/issues/10). Master spec
`docs/specs/socratic-learning-app.md` section 8, acceptance 8 and 13-21.
D9, D10. [ADR-0009](../adr/0009-self-explanation-probe.md),
[ADR-0010](../adr/0010-per-learner-instruction-toggles.md),
[ADR-0013](../adr/0013-reactive-tutor-line.md).

## Goal

Make the **Probe** (CONTEXT: Probe) a real event in the loop: fire it after a
correct answer on a client-owned cadence, grade the learner's
**Self-explanation** with exactly one `grade_probe` call, apply the mode's
`probe_failure_behavior` to what a failed probe does, and let it hold the
attempt open. Asking stays free in both modes — Novice probe questions are
pre-authored in the pedagogy payload, Advanced ones ride the `grade_answer`
response that `ModelGrading.probe_question` already carries — so the call
budget grows only where a learner actually replies.

Two record-shape gaps block this and are closed here. `records.Probe` cannot
tell a **dismissed** probe from a pending one, so the one seal predicate cannot
serve acceptance 16 and 18 at once. And `types.Blank` has nowhere to hold a
pre-authored probe question, so the Novice pedagogy payload cannot carry one.

## Seams

All existing, bar two additions to types the seams already run through.

- **`session.QuizSession.submit`** (existing) — where the cadence coin flip
  happens and a `records.Probe` is appended. Already the seam the master spec's
  table names, and already the place the seal predicate is evaluated.
- **`session.QuizSession.answer_probe`** (new method on an existing class) —
  the one `grade_probe` call. A separate method from `submit` precisely because
  it is a separate call-budget story (issue #10: "the answer is free, the probe
  reply is not"). It cannot be folded into `submit`: no submission is happening.
- **`session.QuizSession.dismiss_probe`** (new method, same class) — the
  learner's escape (ADR-0010). No model call.
- **`session.is_sealable` / `session.has_pending_probe` /
  `session.is_blank_resolved`** (existing module functions) — the unified
  predicate and the derived blank state a re-open has to move.
- **`registry.ModePolicy.probe_failure_behavior`** (existing field) — read, not
  branched on. `tests/test_registry.py`'s package scan for `if mode ==` is the
  standing assertion.
- **`registry.NOVICE_POLICY.rules_for(AuthoringStage.PEDAGOGY)`** (existing) —
  where the pre-authored probe question is asked for.
- **`model_client.RecordingModelClient`** (existing) — every call assertion in
  this spec is a `call_count()` / `assert_never_called` against it.
- **New surface, justified:** `records.Probe.dismissed_at` and
  `types.Blank.probe_question`. Both are nullable fields with defaults on
  existing frozen records; neither adds a seam, and both are the minimum that
  makes an already-agreed behaviour representable.

## Decisions

Every one of these is settled upstream; none is made here.

- **Cadence is client-owned and randomised** — a coin flip per correct answer
  plus the final blank unconditionally, gated by `probe_cadence`
  (ADR-0009; CONTEXT: Probe, `probe_cadence`). Seeded RNG in tests.
- **Asking never costs a call.** Novice questions are pre-authored in the
  pedagogy payload; an Advanced one is a nullable field on the grading response
  (ADR-0009 as superseded by ADR-0011 and ADR-0013; CONTEXT: Probe).
- **Answering costs exactly one call**, `grade_probe`, in both modes (master
  spec acceptance 8).
- **`probe_failure_behavior` is a `ModePolicy` field** (D9, ADR-0009;
  CONTEXT: `probe_failure_behavior`). Advanced re-opens the blank; Novice
  corrects the misconception and leaves it resolved.
- **On re-open the ladder resumes where it left off**, and re-opening is capped
  at one per blank; a second failed probe reveals and moves on (ADR-0009;
  CONTEXT: Hint ladder / rung).
- **Sealing is one unified predicate: all blanks resolved and no probe
  pending** (D10, ADR-0010; CONTEXT: Sealed). Probes on, off and dismissed are
  all the same predicate, never a second path.
- **A probe already on screen stands** when `probe_cadence` changes mid-quiz;
  the setting applies from the next correct answer, and the learner's escape is
  to dismiss (ADR-0010, master spec acceptance 20).
- **The cadence is recorded twice** — `probe_cadence_at_authoring` on the
  attempt and `cadence_at_fire` on each probe (ADR-0010, acceptance 21).
- **Probes are their own ordered collection**, a peer of guesses, never nested
  on a `Guess` (ADR-0009; `records.QuizAttempt.probes` already).
- **Turning probes off does not change the prompt** — segment 1 stays
  byte-identical and the client simply stops firing (ADR-0010). Nothing in this
  work touches `prompting.py`.

## Approach

Built in this order, red → green → refactor at the seams above.

### 1. `records.Probe` learns dismissal

Append `dismissed_at: datetime | None = None`. `__post_init__` refuses a probe
that is both answered and dismissed. An `is_dismissed` property joins
`is_answered`. `QuizAttempt.with_probe_resolved(position, probe)` swaps a
pending probe for its answered or dismissed self in place — the one write that
is not an append, because a probe's later half is the same event, not a new one.

`session.has_pending_probe` narrows to **unanswered and not dismissed**. That
is the whole of acceptance 18: the seal predicate is untouched, and dismissal
is expressed in the record rather than in a second code path.

### 2. `types.Blank` learns a pre-authored probe question

A nullable `probe_question` in the Novice field group.
`_NOVICE_PEDAGOGY_FRAGMENT` and `_NOVICE_SCHEMA_FRAGMENT` declare it,
required-and-nullable in the house style of the other nullable schema fields.
`_validate_advanced_pedagogy` adds it to the fields Advanced refuses.
`authoring._with_pedagogy` reads it off the entry.

The Novice validator does **not** hard-require it, for the same reason
`_hint_for_rung` tolerates missing hints: a blank whose pedagogy has not landed
(master spec acceptance 5) costs the learner the probe, not the verdict.

### 3. The ladder rung becomes a function of wrong answers

`_ladder_rung` today reads the attempt ordinal, which is the same number only
while every guess on an unresolved blank is wrong. Once a probe re-opens a
blank that ordinal includes a correct answer. Recompute the rung from the count
of prior **incorrect** guesses: `min(prior_wrong + 1, HINT_LADDER_RUNGS)`. On a
never-probed blank this is identical to today, which is why no existing ladder
test moves.

### 4. Firing a probe in `submit`

`QuizSession` takes an injected `rng: random.Random`. `submit` takes an
optional `probe_cadence`, defaulting to the attempt's
`probe_cadence_at_authoring`, so a mid-quiz change is the caller passing a
different value.

On a `CORRECT` verdict and only then: `_should_probe(cadence, is_final_blank)`
—

- `OFF` → never, and no draw;
- `FINAL_BLANK_ONLY` → the last blank in the quiz's declared order, and no
  draw;
- `ALWAYS` → always, and no draw;
- `SOMETIMES` → the final blank unconditionally (short-circuit, no draw), else
  one `rng.random() < 0.5` coin flip.

The final-blank short-circuit is what makes the guarantee hold **regardless of
seed**: the RNG is never consulted on that blank.

The question comes from `_Grade.probe_question`, which the deterministic
strategy fills from `blank.probe_question` and the model-graded one from
`ModelGrading.probe_question` — so `submit` reads one field and never learns
which mode it is in. No question, no probe. The per-blank bound
(`MAX_PROBES_PER_BLANK`) is checked before appending.

The appended probe rides back on `Submission.probe_asked`.

### 5. `answer_probe`

Finds the pending probe for the blank, assembles `CallType.GRADE_PROBE` through
`prompting.assemble` — the self-explanation and the question it answers in the
volatile tail's "under consideration" slot — and makes exactly one
`grade_probe` call, stamped on the attempt as a `ModelCallRecord`.

On `INCORRECT`, `policy.probe_failure_behavior` decides, read from the
registry:

- `REOPEN_BLANK` with no prior re-open on this blank → the stored probe carries
  `reopened_blank=True`;
- `REOPEN_BLANK` with the cap already spent → no re-open; reveal and move on;
- `CORRECT_AND_RESOLVE` → nothing moves; the blank stays resolved.

`is_blank_resolved` becomes `corrects > reopens or wrongs >=
HINT_LADDER_RUNGS`, so a re-open voids exactly the one correct answer it
followed. `reopens` is zero everywhere today, so every existing resolution test
is unmoved.

Then the unified predicate runs, and the attempt seals if it now holds.

### 6. `dismiss_probe`

Stamps `dismissed_at`, no model call, then the same predicate. A dismissed
probe persists with a null `self_explanation` and stops blocking.

## Out of scope

- **`prompting.py`, `model_client.py` and the adapters** are untouched.
  `assemble` already takes `CallType.GRADE_PROBE` and a `probe_cadence` it
  deliberately never reads; `ModelClient.grade_probe` already exists. Widening
  the port for probes is exactly what ADR-0010 and ADR-0013 rule out.
- **The `UserValves` wiring** that supplies a live `probe_cadence` per request.
  This spec takes the setting as a parameter; where it comes from is the Open
  WebUI adapter's business (master spec section 12).
- **The client-side probe UI** — the question card, the dismiss control, the
  celebration animation it interrupts. Section 11.
- **Curation and normalisation for probe density** (ADR-0010's consequence).
  Probes are captured; nothing reads them back yet.
- **Live-API evidence that a probe is graded substantively.** Against
  `RecordingModelClient` what is assertable is plumbing and call counts. Master
  spec acceptance 3's sibling claim for probes needs #7.
- **Any second seal path.** Nothing here may add a "last blank resolved" check,
  a probes-off shortcut, or a dismissed-probe branch in sealing.
- **Reading a stored `LearnerProfile`** — `answer_probe` defaults an empty one
  exactly as `submit` and `author` do (#16).

## Acceptance

Numbers are the master spec's.

1. **(8)** Answering a probe makes exactly one model call, and it is
   `grade_probe` — asserted in **both** modes against `RecordingModelClient`.
2. Asking a probe makes **no** model call, in both modes: a Novice probe fires
   against a stub that raises on contact, and an Advanced one adds nothing to
   the single `grade_answer` call of the submission it rode.
3. `TestZeroModelCalls` and `TestAFullNoviceQuizCostsOneCall` in
   `tests/test_session.py` stay green **unedited**; a full Novice quiz with
   `probe_cadence: off` still makes no call after authoring.
4. **(13)** A failed Advanced probe re-opens the blank, and the next wrong
   answer shows the rung after the one the learner had reached — not rung 1.
5. **(14)** A failed Novice probe leaves the blank resolved.
6. **(15)** A blank re-opens at most once; a second failed probe does not
   re-open, and the blank stays resolved.
7. **(16)** A pending probe blocks sealing though every blank is resolved, and
   answering it seals the attempt — through `is_sealable`, with no second path.
8. **(17)** Under a seeded RNG the fired/not-fired sequence is reproducible
   across two runs of the same seed, and across a spread of seeds the final
   blank is probed every time.
9. **(18)** A dismissed probe persists with a null `self_explanation`, a
   non-null `dismissed_at`, and does not block sealing.
10. **(19)** `final_blank_only` probes the last blank and no other.
11. **(20)** Changing `probe_cadence` mid-quiz takes effect from the next
    correct answer, and a probe already pending is untouched.
12. **(21)** An attempt authored under `probe_cadence: off` carries
    `probe_cadence_at_authoring is ProbeCadence.OFF` and zero probes — readable
    from the record alone.
13. `probe_failure_behavior` is read from the registry; `tests/test_registry.py`'s
    scan for `if mode ==` outside the registry stays green.
14. A pre-authored Novice `probe_question` survives the pedagogy payload into
    the `Blank`, and an Advanced blank carrying one is refused by the Advanced
    pedagogy validator.
15. The full suite is green.
