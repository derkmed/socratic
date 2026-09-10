# The Advanced hint ladder — the rung's hint rides the grading response

## Goal

Give an Advanced wrong answer something to say. Today the **hint ladder**
(CONTEXT) advances for an Advanced blank — `hint_rung_shown` counts 1, 2, 3 —
with `feedback=None` on every rung and nothing revealed on the third, so a
learner who gets an Advanced blank wrong three times watches a counter climb and
then the blank closes in silence
([#56](https://github.com/derkmed/socratic/issues/56)).

The rung's hint becomes a fourth field on the `grade_answer` response. The
client still selects the rung and states it in the request; the model writes the
text for that rung. Rung three states the answer in the model's own words — never
the rubric — and the blank closes with that text on screen.

## Seams

| Seam | Kind | Why |
|---|---|---|
| `output_schemas.GRADE_ANSWER` | existing | The declared shape of the response. `tests/test_output_schemas.py` already checks each schema against the parser that reads it; a fourth field is one more entry in a table that exists. |
| `prompting.assemble(CallType.GRADE_ANSWER, ...)` | existing | Where the rung reaches the model, in the volatile tail. `tests/test_prompting.py` already asserts what the tail carries and that segment 1 is byte-identical across learners and cadences. |
| `session._parse_grading` → `ModelGrading` | existing | Where a response field becomes a domain value. Reached in tests through `QuizSession.submit` with a stubbed `ModelClient`, which is the master spec's seam. |
| `QuizSession.submit` on an Advanced attempt | existing | The master spec's seam and the one the issue's complaint is stated against: three wrong answers, three rungs, and what the learner is told on each. |
| A digest tripwire over the five segment 1 texts | **new**, small | ADR-0014 makes a segment 1 edit a cache-invalidation event with a cost measured in money and no error to report it. This change deliberately invalidates one prefix; nothing today would notice if it invalidated five. One test pinning five digests turns an invisible cost into a failing test and a line in a diff. |

No new seam in the service, the rendering layer or the client: the hint travels
on `Submission.feedback`, which `payloads.submission_body` already renders as
`feedback_html` and `client.js` already displays through `view.showHint`.

## Decisions

Settled upstream; this spec records them.

- **[ADR-0016](../adr/0016-advanced-hint-rides-the-grading-response.md)** — the
  rung's hint is a field on the grading response, the client chooses the rung
  and passes it in, rung three reveals the answer as prose, the rubric is never
  returned, and what closes an Advanced blank is unchanged.
- **[ADR-0013](../adr/0013-reactive-tutor-line.md)** — one response, several
  things, and exactly one model call per submission. The hint rides the response
  already in flight; a second call would falsify master spec acceptance 9.
- **[ADR-0003](../adr/0003-grading-authority-and-key-custody.md)** — the answer
  key never leaves the backend. An Advanced blank's rubric *is* the key.
- **[ADR-0014](../adr/0014-model-choice-and-effort.md)** — segment 1 is one
  cache prefix per call type. Rewriting the `grade_answer` body invalidates that
  one prefix once; the other four must not move.
- **CONTEXT: Hint ladder / rung** — the client counts attempts and selects the
  rung; the model authors the text. Already the stated division of labour; for
  Advanced it now becomes true.
- **CONTEXT: ModeRegistry** — an `if mode ==` outside the registry is a bug.
  Nothing here branches on mode: `_grade_by_model` is reached through
  `ModePolicy.grading_strategy`.

## Approach

In order, one seam at a time.

1. **Schema.** `GRADE_ANSWER` gains `"hint": NULLABLE_STRING`. Nullable because
   a correct answer has no rung and omits it.
2. **Prompt.** `prompting.assemble` gains a keyword-only `hint_rung: int | None`,
   rendered by `_render_tail` under "Under consideration" and only when supplied,
   so every other call type's tail is byte-identical to what it was.
3. **Segment 1.** Rewrite the `grade_answer` body: ask for the hint on the rung
   the request states, describe what the three rungs escalate through, say that
   rung three states the answer and closes the blank, and forbid reproducing or
   quoting the rubric. Amend the closing "never reveal" paragraph, which rung
   three would otherwise contradict. `_SHARED_STANCE` and the other four bodies
   are untouched.
4. **Parser.** `ModelGrading` gains `hint: str | None`, read by `_parse_grading`
   through the same `_optional_line` as the other riders — absent and null mean
   the same thing.
5. **Strategy.** `_grade_by_model` computes the rung once, passes it into
   `assemble`, and on a wrong verdict routes the returned hint to
   `_Grade.feedback` — falling back to `_hint_for_rung` so a mode whose blanks
   *do* carry pre-authored hints keeps working through this path. It drops the
   hint if it contains the blank's rubric verbatim.
6. **The two locked-in tests** in `TestAdvancedWalksTheSameLadder` are rewritten
   to assert the new behaviour.

## Out of scope

- **Pre-authored Advanced hints.** Option two in the issue, rejected in
  ADR-0016. `registry._validate_advanced_blank` still forbids `hints`, and the
  vestigial `hints` property on `_ADVANCED_SCHEMA_FRAGMENT` stays as it is.
- **A mode with no ladder.** Option three, rejected. No new `ModePolicy` field.
- **Any new wire field.** The hint reaches the client as `feedback_html`; no
  `hint_html`, and no change to `payloads.py`, `content.py`, `overlay.py` or
  `client.js`.
- **What text fills the gap when an Advanced blank closes on rung three.**
  *Resolved by [ADR-0019](../adr/0019-resolved-blank-text-comes-from-the-service.md)
  ([#127](https://github.com/derkmed/socratic/issues/127)).* The
  `revealedOptionId || submitted` this section described did exactly what it
  says — left the learner's wrong answer in the gap. The service now states the
  text as `resolved_html`, and rung three names the answer in a phrase through
  `revealed_answer`.
- **Novice.** Zero model calls, wholly pre-authored feedback, unchanged.
- **`ProbeAnswer.revealed_option_id`** on the "reveals and moves on" path after
  a second failed probe. Same shape, different method; not what #56 reports.
  Still out of scope, and now known to be unreachable: that path needs
  `REOPEN_BLANK`, which only the Advanced policy carries, and an Advanced blank
  is validated to carry no `correct_option_id`.

## Acceptance

1. An Advanced wrong answer returns `feedback` equal to the hint the model
   returned on that grading response.
2. Rung one, two and three each carry the hint the model wrote for that rung;
   none of the three returns `feedback=None` when the model supplied a hint.
3. The rung the client selected appears in the volatile tail of the
   `grade_answer` request, and it is `min(prior_wrong + 1, 3)`.
4. A correct Advanced answer carries no hint: `feedback` is `None` and the
   response's `hint` field is not required.
5. Three wrong Advanced answers still walk rungs 1, 2, 3, still resolve the
   blank, and still cost exactly three model calls.
6. `revealed_option_id` is `None` on all three rungs of an Advanced blank.
7. A hint that contains the blank's rubric verbatim is dropped: nothing `submit`
   returns contains the rubric, whatever the model sends.
8. Segment 1 for `author_skeleton`, `author_pedagogy`, `grade_probe` and
   `fold_narrative` is byte-identical to what it was before this change, pinned
   by digest; `grade_answer`'s is deliberately different.
9. The volatile tail for every call type that supplies no rung is byte-identical
   to what it was before the parameter existed.
10. `GRADE_ANSWER` declares exactly the four fields `_parse_grading` reads.
