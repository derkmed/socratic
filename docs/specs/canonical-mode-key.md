# One mode key below the seam, and one way to spell it out

Issue [#89](https://github.com/derkmed/socratic/issues/89). Same root cause as
[#85](https://github.com/derkmed/socratic/issues/85).

## Goal

`prompting._render_quiz` reads `quiz.mode.value`. `Quiz.mode` is annotated
`DifficultyMode`, but nothing ever converted the caller's mode into one, and the
mode arrives over HTTP as the plain string the request named — `LearnerSettings`
carries it verbatim on purpose ("the mode is *carried, not validated*", CONTEXT).
So every quiz authored through `POST /quizzes` holds `mode='novice'`, a `str`,
and the first prompt assembled for that quiz raises:

```
AttributeError: 'str' object has no attribute 'value'
```

Two live paths hit it and no test did:

- `QuizAuthoring.author_pedagogy` — the second authoring call, fired off the
  render path for *every* quiz the service authors (#89).
- `_grade_by_model` — every Advanced answer submitted through `/answers`, which
  is a 500 (#85).

The domain's own tests miss both because they pass `DifficultyMode` directly.

The same hazard is already worked around twice, in two different spellings:

```python
# service/payloads.py, blank_body
"mode": str(blank.mode.value if hasattr(blank.mode, "value") else blank.mode),
# domain/profile_builder.py, _name
return str(getattr(value, "value", value))
```

The goal is to stop the duality existing, rather than to add a third
workaround at the third reader.

## Seams

- **`ModeRegistry`** — the registry is already "the only place mode is branched
  on" (CONTEXT), which makes it the only place entitled to say which of two
  equal spellings is the canonical one. Directly assertable, no wiring.
- **`QuizAuthoring.author`** — the one entry point that turns a caller-supplied
  mode into a `Quiz`, a `Blank` and a `QuizAttempt`. It already resolves the
  mode's policy here, so the normalisation costs no new lookup and no new
  failure mode.
- **`prompting.assemble`** — returns the layout as a plain value, so the reported
  line is assertable with no service and no SDK.
- **`create_app` + `QuizAuthoring.author_pedagogy`** — the composition the issue
  reproduces against, and the only place that proves the HTTP path is fixed.

## Decisions

1. **Normalise at the domain boundary, do not repeat the defensive read.** The
   `hasattr` guard in `blank_body` is a symptom: a field annotated
   `DifficultyMode` can hold a `str`, so every reader has to ask. Repeating it at
   `prompting.py:304` fixes one line and leaves the next reader to rediscover it
   (which is exactly the history: `blank_body` guarded, `_render_quiz` did not).
   Converting once, where the value enters the domain, makes the annotation true
   and retires both workarounds this change touches.

2. **The registry does the converting, not `DifficultyMode`.** The obvious
   `DifficultyMode(body.mode)` at the HTTP edge is wrong twice over: it makes
   `app.py` a second opinion about mode, which CONTEXT forbids, and it refuses a
   mode registered by anyone else — `ModeKey` is deliberately `str`, and
   `tests/test_registry.py` registers `"expert"` as a plain string. So
   `ModeRegistry.key_for` returns *the key this registry was registered under*,
   for any spelling that compares equal to it. For the two shipped modes that is
   the `DifficultyMode` member; for a third mode registered as `"expert"` it is
   `"expert"`, and no conversion is invented.

3. **`author` normalises; `author_pedagogy` does not have to.** Pedagogy merges
   into the stored attempt, whose quiz `author` wrote — so one normalisation at
   the one constructing entry point covers the whole downstream tree: `Quiz`,
   `Blank`, `QuizAttempt`, and every reader of them.

4. **Rendering a mode key still goes through one function.** Even normalised, a
   mode key is a `ModeKey`, and a registry may hold a non-enum one, so `.value`
   is an unsafe read whoever calls it. `registry.mode_name` is the single place
   that turns a key into its stored spelling; `_render_quiz` and `blank_body`
   both use it. This is not the defensive pattern repeated — it is the defensive
   pattern named, given one home, and deleted from the two places that had
   open-coded it.

5. **An unknown mode keeps failing exactly where it fails today** — `KeyError`
   from the registry on the authoring call, before the model is consulted.
   `key_for` raises the same refusal `policy_for` does, so nothing about the
   unknown-mode path moves.

## Approach

### 1. `registry.py`

- `mode_name(mode: ModeKey) -> str` — the stored spelling of a key, enum or not.
- `ModeRegistry.key_for(mode: ModeKey) -> ModeKey` — the canonical registered
  key, raising the registry's existing `KeyError` for an unregistered mode.
  `policy_for` is re-expressed over it so the refusal is written once.

### 2. `authoring.py`

`QuizAuthoring.author` canonicalises the mode through the registry at the point
it already looks the policy up, before anything is constructed from it.

### 3. The two readers

`prompting._render_quiz` and `payloads.blank_body` render the mode with
`mode_name`. The `hasattr` expression in `blank_body` goes.

## Out of scope

- **`profile_builder._name`.** It normalises `outcome` and `verdict` as well as
  `mode`, off `QuizAttempt` fields that are annotated `str` on purpose. Folding
  it into `mode_name` would be a change to two other fields this issue never
  reported.
- **Refusing an unknown mode with a 422 instead of a 404.** `app.py` deliberately
  leaves the refusal to the registry, and the `KeyError` handler turns it into a
  404 "not found". That is a status-code question about a path this change does
  not move.
- **`LearnerSettings.mode` staying a `str`.** It is the wire's value before any
  registry is in hand; carrying it verbatim is ADR-0010's point.
- **Any change to how modes are registered, or a third mode.**

## Acceptance

1. `ModeRegistry.key_for("novice")` is `DifficultyMode.NOVICE` — the member, not
   an equal string — and `key_for(DifficultyMode.NOVICE)` is the same member.
2. `key_for` on a registry holding a plain `"expert"` key returns `"expert"`,
   and on an unregistered mode raises `KeyError` naming it.
3. `mode_name` renders `DifficultyMode.NOVICE` and `"expert"` as `novice` and
   `expert`.
4. `QuizAuthoring.author(..., mode="novice")` returns a quiz whose `mode`, whose
   blanks' `mode`, and whose stored `QuizAttempt.mode` are all
   `DifficultyMode.NOVICE`.
5. `prompting.assemble(AUTHOR_PEDAGOGY, quiz=<quiz whose mode is "novice">)`
   renders `Mode: novice` instead of raising `AttributeError` — the reported
   line, asserted without the service.
6. **The issue's own reproduction**: author through `create_app`'s `/quizzes`
   with `{"mode": "novice"}`, then run `author_pedagogy` against the stored
   attempt. It completes and the pedagogy merges.
7. An Advanced answer submitted to `/answers` for a quiz authored over HTTP
   grades instead of returning a 500 (#85's reproduction, fixed by the same
   change).
8. `payloads.blank_body` still puts `"novice"` on the wire, and a blank whose
   mode is a plain `"expert"` key still reaches the wire as `"expert"` — the
   case the inline `hasattr` was written for, now `mode_name`'s.
9. An unknown mode is still refused by the registry before any model call.
10. `tests/test_registry.py` and `tests/test_import_hygiene.py` stay green: the
    registry is still the only place mode is branched on, and the domain is
    still stdlib-only.
