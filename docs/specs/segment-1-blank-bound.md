# Segment 1 stops delegating the blank bound to a schema that cannot carry it

Issue [#72](https://github.com/derkmed/socratic/issues/72).

## Goal

`_AUTHOR_SKELETON_INSTRUCTIONS` currently tells the model:

> The number of blanks admissible for the difficulty mode is fixed by the schema
> fragment supplied with the request; stay inside it.

The schema fragment cannot fix it. `minItems`/`maxItems` are outside the
JSON-Schema subset `output_config.format` accepts, so neither
`registry._NOVICE_SKELETON_FRAGMENT` / `_ADVANCED_SKELETON_FRAGMENT` (per-*blank*
object schemas, carrying no array bound at all) nor the envelope that wraps them
(`output_schemas._skeleton_envelope`) can express a count.

Two defects fall out of the one sentence:

1. **It points at a constraint that is not there.** The instruction is false
   about the request it rides on.
2. **The bound never reaches the model at all.** The real bound is
   `ModePolicy.blank_range` — Novice 1-2, Advanced 4-6 — and the only thing
   holding it is `validation.ensure_valid_quiz`, *after* the response has been
   paid for. An Advanced quiz authored with two blanks is a
   `QuizValidationError` on the one call a learner blocks on (ADR-0011), and
   nothing in the prompt ever named the number.

The goal is to state the bound where it can honestly be stated, and to stop
promising it where it cannot.

## Seams

- **`prompting.assemble`** — the existing seam. It already returns the layout as
  a value assertable with no API and no SDK, which is where both halves of this
  change are checkable.
- **`prompting._render_segment_2`** — where per-learner, per-session material is
  rendered. The bound varies by mode, so this is the only position ADR-0006
  leaves open to it.
- **`authoring.Authoring.author`** — the caller that already resolves the mode's
  policy (`self._registry.policy_for(mode)`, currently called for its `KeyError`
  and discarded) and so already holds the bound.

## Decisions

1. **The bound goes in segment 2, not segment 1.** ADR-0006 requires segment 1
   to be byte-identical across learners for a given call type. The bound varies
   by mode, so a literal "author between 4 and 6 blanks" in segment 1 would split
   its cache prefix per mode — the expensive half of the prefix, shared across
   the whole workspace, sacrificed to two numbers. Segment 2 is already per
   learner and per session and already renders `Mode:` for a quiz in hand.

2. **`assemble` takes the bound as a value, not the mode.** A `blank_range:
   registry.BlankRange | None` parameter. The registry stays the only place mode
   is branched on (`tests/test_registry.py`); prompting renders whatever data it
   is handed, exactly as `_render_quiz` already renders mode-specific fields "by
   presence rather than by mode". `prompting` importing `registry` is acyclic —
   `registry` imports only `modes` and `types` — and stays inside the domain, so
   `tests/test_import_hygiene.py` is untouched.

3. **Only `AUTHOR_SKELETON` supplies it.** It is the only call that authors
   blanks. `AUTHOR_PEDAGOGY` and the grading calls have the quiz in hand, so
   their blank count is already fixed and restating the bound would be noise.
   Omitting it there is safe: segment 2 is not a cross-learner cache prefix, so
   ADR-0010's "omission splits the cache too" does not bite.

4. **Segment 1 keeps a sentence about the bound, mode-agnostic.** It must still
   tell the model that a bound exists and that it is binding — otherwise the
   model has no reason to look. It names no numbers, so it stays byte-identical
   across learners and across modes.

## Approach

### 1. Segment 1: point at the material, not at the schema

Replace the false sentence in `_AUTHOR_SKELETON_INSTRUCTIONS` with one that
refers the model to the stated range below the instructions. No numbers, no mode
name — nothing that could vary per learner or per mode.

The neighbouring sentence "Every blank carries the mode-specific fields the
supplied schema fragment requires and no others" is *true* — the fragments do
carry per-blank field requirements — and is left alone.

### 2. Segment 2: render the bound when it is supplied

`assemble` gains a keyword-only `blank_range` defaulting to `None`, threaded to
`_render_segment_2`. When supplied it renders a `## Blanks to author` section
naming the inclusive range; when `None` the section is absent entirely.

### 3. `authoring.author`: pass what it already resolves

`self._registry.policy_for(mode)` becomes `policy = self._registry.policy_for(mode)`
and `policy.blank_range` is passed to `assemble`. No new registry lookup, no new
failure mode: the `KeyError` this call already raises is unchanged.

## Out of scope

- **The `COMPLETE`-stage fragment's bare `{"type": "object"}` node** — issue
  [#73](https://github.com/derkmed/socratic/issues/73), a separate defect in a
  fragment this change never reads.
- **Teaching the validator to be lenient.** `ensure_valid_quiz` stays exactly as
  strict as it is. This change makes the rejection rarer, not softer; the
  backstop is the point.
- **A retry on `QuizValidationError`.** Whether a rejected skeleton is re-asked
  is a latency question (ADR-0011) that belongs to whoever owns the authoring
  loop, not to the prompt text.
- **The other four segment 1s.** None of them delegates a numeric bound to the
  schema.

## Acceptance

1. `_AUTHOR_SKELETON_INSTRUCTIONS` no longer claims the schema fragment fixes
   the number of blanks.
2. `assemble(AUTHOR_SKELETON, ..., blank_range=BlankRange(4, 6))` renders `4`
   and `6` into segment 2.
3. That same call leaves segment 1 byte-identical to the call with no
   `blank_range` — asserted for both modes' ranges, which is the ADR-0006 half
   this change could plausibly break.
4. `assemble` with no `blank_range` renders no bound section, and every existing
   caller is untouched.
5. `Authoring.author` sends the mode's own range: a Novice authoring call's
   segment 2 carries 1-2, an Advanced one carries 4-6.
6. The registry remains the only place mode is branched on
   (`tests/test_registry.py` stays green), and the domain remains stdlib-only
   (`tests/test_import_hygiene.py` stays green).
