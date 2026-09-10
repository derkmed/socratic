# ADR-0019 — Resolved blank text comes from the service, not the DOM

Status: accepted
Date: 2026-09-09
Amends: [ADR-0004](0004-quiz-wire-format.md) (a field on the graded submission),
[ADR-0016](0016-advanced-hint-rides-the-grading-response.md) (a second nullable
rider on the same grading response)

## Context

[ADR-0004](0004-quiz-wire-format.md) says a resolved blank "is swapped for a
`text` or `math` node", and CONTEXT.md says a formula answer resolves to a `math`
node "whose MathML the Pipe returns in the grading response it was already
sending". Neither was implemented: `submission_body` carried no such field.

The client filled the hole itself, by scavenging the rendered document for a
label to paste into the gap — `root.querySelector('.socratic-option[data-option-id=…]')`.
Option ids are **blank-scoped**, so the first match in document order is usually
another blank's option, and the learner sees the wrong answer in the gap
([#127](https://github.com/derkmed/socratic/issues/127)).

The same issue exposed a second hole with the same symptom. On the Advanced path
a rung-three close returns `revealed_option_id=None` — correctly, since
[ADR-0016](0016-advanced-hint-rides-the-grading-response.md) makes that reveal
prose — while `is_blank_resolved` still closes the blank. The client's
`revealedOptionId || submitted` then fills the gap with the learner's third
**wrong** answer. Neither half is wrong on its own; the gap between them is.

Both holes are the same shape: **the client reconstructing an answer nobody
told it.**

## Decision

**The graded submission carries the resolved text.** `resolved_html` on the
submission body, rendered by `payloads.label_html` — an
inline fragment, not a segment node, so text and MathML arrive through one field
and `createDomView` stays a `setHtml` with no type dispatch. Both DOM lookups —
the gap fill and the rung-three reveal line — are deleted.

**Advanced gets a short-form reveal.** A nullable `revealed_answer` on the
`grade_answer` response, riding the call already in flight exactly as ADR-0016's
`hint` does. The prose hint explains; this names. A gap is a noun-phrase-shaped
hole and the prose rung-three hint is a sentence.

**The domain carries the text, `payloads` renders it.** `Submission` gains a
plain `resolved_answer`; `label_html` turns it into `resolved_html` at the edge.
This keeps `html_of` and `label_html` the only two ways a string becomes HTML in
that package, and keeps `payloads` a projection rather than a second place that
knows the answer key.

**A correct Advanced answer resolves to the learner's own words**, sanitised
like everything else. Canonising it would silently rewrite what they wrote and
would need a third field to carry the canonical form.

**A null `resolved_html` renders nothing.** The client never falls back to
`submitted` — that fallback *is* the second defect. The only case that can reach
it is an Advanced rung-three close where the model omitted `revealed_answer`; an
empty gap is a cosmetic failure, the learner's wrong answer presented as the
resolved text is a pedagogical one.

**Segment 1 asks for it.** Declaring `revealed_answer` in the schema only makes
room for it; a field the instruction never mentions is a field the model omits,
which would have left the gap empty on every rung-three close and the fix inert
on the one path it was written for. So the `grade_answer` cache prefix is
rewritten, and its pinned digest moves — the same cost ADR-0016 paid for `hint`,
for the same reason.

**The probe path carries nothing.** It can close a blank — "reveals and moves
on", ADR-0009 — but it cannot change what belongs in the gap, because a probe
only fires after a correct answer and the gap already holds it. An earlier cut
of this ADR gave `ProbeAnswer` a `resolved_answer` for symmetry; the field was
unreachable under every registered policy, and the client call it justified
fired on every probe answer with nothing to write, erasing the gap. Symmetry was
the wrong instinct: the two paths are not symmetric, because only one of them
decides what the answer was.

**`revealed_option_id` stays.** It is the sanctioned disclosure
[ADR-0003](0003-grading-authority-and-key-custody.md) accounts for, and it
remains the audit marker for what was disclosed. What changes is that no text is
derived from it any more.

## Consequences

Good:
- The client cannot desync from the answer key, because it no longer holds a
  second, inferred copy of it.
- The `math`-resolved blank that ADR-0004 and CONTEXT.md both promised becomes
  implementable — the field it needed now exists.
- The assertion moves to a layer that has tests. `createDomView` is untested by
  construction ("thin by construction"); "the service sent the right
  `resolved_html`" is a Python test, so the fix is guarded without inventing a
  DOM harness.
- An Advanced blank that closes after three misses stops presenting the
  learner's wrong answer as the resolved text.

Costs / risks:
- A wider wire. Two more fields on the submission path and one more on the
  grading schema.
- A second model-authored string that approaches the answer key, so the key
  custody guard of ADR-0003 has to cover `revealed_answer` as well as `hint` —
  and it cannot be the same guard, because a short reveal is *meant* to state
  what the rubric encodes.
- The `grade_answer` cache prefix is invalidated workspace-wide (ADR-0014).
  Unavoidable: the model cannot fill a field it was never asked for.
- A close that does not repaint is now a rule the client has to keep, and
  nothing in the type system enforces it. The guard is a test
  (`test_answering_a_probe_never_repaints_the_gap`), because the alternative —
  a field that says "paint nothing" — is what caused the regression.
- The thin-view claim is now load-bearing rather than incidental. This bug is
  the proof that "too thin to hold a bug" was not true; keeping it true is a
  standing cost of the design, not a fact about it.
