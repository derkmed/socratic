# 0016. The Advanced hint rung is authored on the grading response

Date: 2026-09-08
Status: accepted
Amends: [ADR-0013](0013-reactive-tutor-line.md) (one response, three things
becomes four), [ADR-0003](0003-grading-authority-and-key-custody.md) (what a
rung-three reveal consists of, on the model-graded path)

## Context

The **hint ladder** is defined for both modes — "the three escalating responses
to a wrong answer... the third reveals" (CONTEXT: Hint ladder / rung) — but in
Advanced there was nothing to put on any rung.

Three individually-correct pieces left the gap between them
([#56](https://github.com/derkmed/socratic/issues/56)):

- `registry._validate_advanced_blank` rejects `hints` on an Advanced blank
  outright, so nothing is pre-authored.
- `_GRADE_ANSWER_INSTRUCTIONS` told the model *"the client selects which rung of
  the hint ladder to show; you do not choose it and you do not write the hint
  here"*, so nothing is authored at grading time either.
- `session._grade_by_model` therefore called `_hint_for_rung` on a blank whose
  `hints` is always `None`, and returned `hint_rung_shown` of 1, 2 or 3 with
  `feedback=None` every time, and `revealed_option_id=None` on rung three.

The only text a wrong Advanced answer could produce was `ModelGrading.tutor_line`,
which segment 1 tells the model to leave null *"most of the time"*. So the common
case was a learner getting a blank wrong three times, watching a rung counter
climb, and being told nothing before the blank closed.

Three directions were open: author Advanced hints up front the way Novice does;
decide Advanced has no ladder and stop computing a rung for it; or have the model
write the rung's hint at grading time.

## Decision

**The rung's hint is a field on the `grade_answer` response**, alongside the
verdict, the reactive tutor line and the probe question. **The client still
chooses the rung** — `_ladder_rung(prior_wrong)` — and states it in the volatile
tail; the model writes the text for the rung it was given and no other.

Authoring the hints up front was rejected because three hints written before the
learner has said anything is exactly the non-reactive pedagogy Advanced exists to
improve on. Dropping the ladder was rejected because a wrong answer with no
escalation is a worse learner experience than the one being fixed, and because
"a wrong free-text answer walks the same ladder as a wrong click" is a property
worth keeping.

**The rung is passed in prospectively.** It is `min(prior_wrong + 1, 3)`, which
is knowable *before* the call, so it rides the same request that asks for the
verdict. The model is told which rung this answer lands on *if it is wrong* and
writes the hint on that assumption; on a correct verdict it omits the field.
This is what keeps the call count at exactly one per submission — master spec
acceptance 9, and ADR-0013's whole shape.

**The hint reaches the learner through `Submission.feedback`**, the field the
Novice ladder already fills from `blank.hints`. It carries no new wire field and
needs no client change: `payloads.submission_body` already renders
`feedback_html` and `client.js` already shows it on a wrong verdict. The two
routes to a rung's text — pre-authored on the blank, or authored on the response —
converge on one field, exactly as the two routes to a probe question already
converge on `_Grade.probe_question`.

**A rung-three reveal on the Advanced path is the model stating the answer in its
own words**, as the rung-three hint. It is not the rubric.
`revealed_option_id` stays `None`, because an Advanced blank has no option id to
put there — the reveal is prose, not an id.

**The rubric is never handed back, and that is enforced rather than requested.**
Segment 1 now says the rubric is the key and must not be reproduced or quoted;
`_grade_by_model` additionally **drops any hint that contains the blank's rubric
verbatim**, failing closed to no hint at all. ADR-0003's key custody is a
structural claim, and a claim that rests only on the model following an
instruction is not structural.

**What closes an Advanced blank is unchanged**: three wrong answers, the same
`is_blank_resolved` predicate as Novice. The defect was that it closed in
silence, not that it closed. It now closes with the rung-three hint on screen.

## Consequences

**The `grade_answer` cache prefix is invalidated once.** Segment 1 for that call
type is rewritten, so every workspace pays one cache-creation write on the next
`grade_answer` call and reads the new prefix cheaply thereafter (ADR-0014). This
is the accepted cost of the decision. The other four call types' segment 1 texts
are untouched and their prefixes are undisturbed — `_SHARED_STANCE` is
concatenated into five separate strings, and only the `grade_answer` body
changes. A digest tripwire over all five now pins this, so a future edit that
silently invalidates a prefix shows up as a failing test rather than as a bill.

**Segment 1 for `grade_answer` grows**, which is a cost in the right direction:
ADR-0014's risk is falling *under* Opus 5's 512-token floor, not over it.

**An Advanced wrong answer spends roughly 30-60 more output tokens**, on the one
path that was already making a blocking call. By ADR-0011's reasoning latency
tracks output tokens, so this is a real if bounded regression, hidden behind the
same animation as the tutor line.

**"One response, three things" becomes four.** `ModelGrading` carries three
riders, not two; `GRADE_ANSWER` declares four fields, not three. The grouping
still holds: the deterministic strategy builds no `ModelGrading` at all, so
Novice has no route to a model-authored hint rather than a field that is merely
always null.

**Two tests that locked in the old behaviour are rewritten**, not deleted:
`TestAdvancedWalksTheSameLadder::test_an_advanced_blank_has_no_pre_authored_hint_text`
and `::test_nothing_is_revealed_on_rung_three`. They recorded the gap
deliberately; they now assert the intended behaviour.

**The fail-closed rubric guard can cost a legitimate hint** where a rubric is so
short that a natural hint contains it verbatim. That is the cheap direction of
the trade: a lost hint is a bad turn, a published answer key is a broken product.

**Advanced blanks still carry no `hints`.** `_ADVANCED_SCHEMA_FRAGMENT`'s
vestigial `hints: ["array", "null"]` property and the validator that forbids a
non-null value are both left as they are.
