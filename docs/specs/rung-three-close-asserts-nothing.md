# A rung-three close asserts nothing in the gap

## Goal

Stop the overlay writing an answer into a gap the learner did not earn. Today
`client.js` fills a resolved gap with `revealedOptionId || submitted`, and on a
**rung-three close** — the blank closing on a wrong answer (CONTEXT: Hint ladder
/ rung) — what the learner submitted was by definition wrong. In Novice the
fallback never fires, because a rung-three Novice reveal returns
`revealed_option_id`; in Advanced there is no option id at all
([ADR-0003](../adr/0003-grading-authority-and-key-custody.md) — the rubric is
the answer key and must not be returned), so the learner's own wrong words land
in the sentence they are about to read back
([#112](https://github.com/derkmed/socratic/issues/112)).

The fix is the cheaper of the two directions the issue offered, and it applies
to **both modes**: on a rung-three close the gap asserts nothing. It renders
closed-but-unrevealed — a fixed marker pointing at the verdict panel — and the
**reveal** (CONTEXT) is what names the answer, as it already does. Novice
therefore stops putting the revealed option in the gap too: a blank that closed
on three wrong answers should read the same way in both modes, and the answer
belongs in the note rather than in the prose the learner failed to complete.

The note becomes the whole disclosure. It shows on a rung-three close in both
modes — today it is shown only when there is an option id, so an Advanced close
leaves the reveal element hidden and the prose reveal sits in the feedback
block alone — and it closes by telling the learner they can carry on.

## Seams

| Seam | Kind | Why |
|---|---|---|
| `applyGrade` in `client.js` | existing | The state machine that decides what the view is told. `tests/test_ui_client.py` drives it in a child Node interpreter with a recording view, which is where every other client decision is asserted. |
| The grade event handed to `celebrate` / `showHint` | existing | Already carries `rung`, `revealedOptionId` and `pedagogyPending`. Whether this response is the reveal is one more decision that belongs here rather than in the DOM half. |
| `view.resolveBlank({blankId, answer})` | existing | Already the one call that says what goes in a gap. `answer: null` is the new "nothing goes in it", and the tests read the argument. |
| `revealNote(optionHtml, hasFeedback)` on the module | **new**, small | Composing the note is a decision — whether the answer is named, and what is said when nothing names it — and `createDomView` has no tests to hold one. A pure function beside `createFetchTransport`, which the module already exports for exactly this reason, puts it back in the Node harness. |
| The `closedText` on the `resolveBlank` event | existing | What a closed gap says is the other half of #112's remedy, and master acceptance 31 rests on it being non-empty. Chosen in the state machine and handed over, so a test can see it. |
| `createDomView` and `overlay.css` | existing, untested by design | Binding only, and now genuinely so: it renders the marker it is handed and the note the function composed. The client's tests run in Node with no DOM and no dependency to give it one, so the closed state's styling and the note's placement are still asserted nowhere. |

No new wire field, no schema change, no model output: the service already sends
everything this needs.

## Decisions

Settled upstream; this spec records them.

- **[ADR-0003](../adr/0003-grading-authority-and-key-custody.md)** — the answer
  key never leaves the backend, and an Advanced blank's rubric *is* the key.
  Nothing here asks the backend for gap text, so no new path carries the key.
- **[ADR-0009](../adr/0009-self-explanation-probe.md)** — rung three reveals and
  closes the blank. Unchanged: the blank still closes, and the reveal still
  happens. Only where it is displayed moves.
- **CONTEXT: Reveal** — in Novice the reveal is `revealed_option_id`; in
  Advanced it is prose in the rung-three hint, and `revealed_option_id` stays
  null. Both reach the note; neither reaches the gap.
- **CONTEXT: ModeRegistry** — an `if mode ==` outside the registry is a bug.
  Nothing here branches on mode. The client cannot see the mode and does not
  learn to: the condition is the verdict, which is the same in both.
- **Master spec acceptance 31** — no silent blanks. A gap that asserts nothing
  is not a gap that shows nothing; it carries a marker and the note carries the
  answer.
- **`client.js` header** — every decision lives in the state machine and the
  DOM half is thin. The decision is `answer: null`; the marker's wording is
  rendering.

## Approach

In order, one seam at a time.

1. **The event says whether this response is the reveal.** `applyGrade` computes
   `reveal` — a wrong verdict that nonetheless resolved the blank — and puts it
   on the event next to `rung` and `revealedOptionId`. It is derived from the
   verdict rather than from `hint_rung_shown === 3` because what matters is that
   the blank closed without the learner getting it right.
2. **The gap stops asserting.** `resolveBlank` is passed `submitted` on a
   correct close and `null` on a reveal, replacing `revealedOptionId ||
   submitted`. The `revealedOptionId` fallback disappears with it.
3. **The view renders the closed state.** A `null` answer sets
   `data-state="closed"` and writes the event's `closedText`; `overlay.css`
   gains a rule for it, muted against the resolved state's accent.
4. **The note shows on every reveal.** `paint` shows `.socratic-reveal` when
   `event.reveal`, not only when there is an option id, and fills it from
   `revealNote`. With an option id the note names the option — punctuated, so
   the clause and the line that follows it are two sentences — and without one
   the answer is in the feedback prose directly above. Where there is neither,
   the note says the answer was not named rather than telling the learner to
   carry on with one they never got.
5. **A probe correction clears what it does not replace.** `probeGraded` writes
   the feedback block only, so the reveal, the tutor line and the pending
   notice from whatever was graded last are hidden alongside it. A probe can be
   answered after the learner has moved on to another blank.

## Out of scope

- **A `resolved_text` on the wire.** The issue's first direction — a backend
  field carrying short answer text for the gap. It needs a schema field, a
  prompting change and a new route for the answer to travel; this needs none of
  them, and the issue itself called it the more expensive option.
- **The second-failed-probe close.** [ADR-0009](../adr/0009-self-explanation-probe.md)
  makes a second failed probe a reveal, "mirroring rung 3", and
  `createDomView.probeGraded` renders no reveal for it: `probeGraded` is handed
  `blankResolved` and `revealedOptionId` and reads neither. The principle this
  change establishes — a reveal is disclosed in the note — is not applied to
  that path. Same family, different method, and not what #112 reports. Filed
  separately.
- **Testing the DOM half.** Node with no DOM is the deliberate arrangement of
  `test_ui_client.py`; adding a DOM would add the first test-time JS dependency
  this repo does not have. The two decisions are pulled out into the state
  machine and a pure function instead; what stays untested is placement and
  styling, which is where the seam was already drawn.
- **The service, the renderer, the domain.** Untouched.

## Acceptance

1. A correct answer that resolves its blank still fills the gap with what the
   learner submitted.
2. A rung-three Novice close emits `resolveBlank` with `answer` null — the
   revealed option id no longer reaches the gap.
3. A rung-three Advanced close, which carries no option id, emits `resolveBlank`
   with `answer` null: what the learner submitted never reaches the gap.
4. A rung-three close still resolves the blank and still advances the learner —
   `resolveBlank` is called, and the next blank is activated or the recap shown.
5. The grade event carries `reveal` true on a rung-three close in both modes,
   and false on a correct answer and on a wrong answer that leaves the blank
   open.
6. A wrong answer on rungs one and two emits no `resolveBlank` at all.
7. `showHint` still receives the revealed option id in Novice, so the note can
   name the option.
8. The gap a rung-three close leaves behind is not empty: `resolveBlank`
   carries a non-empty `closedText`, and a correct close carries none. This is
   master acceptance 31 held by a test rather than by a string in an untested
   file — the check that fails if the marker is ever emptied.
9. The note names the option where there is one, as a finished sentence: the
   option clause is terminated, so it and the line after it do not run
   together.
10. On a reveal carrying no option id the note still shows, and does not claim
    to name an answer — the reveal is prose and it is in the feedback block
    above.
11. A reveal where nothing names the answer anywhere — no option id and no
    feedback, which is what a hint dropped by `_safe_hint` leaves — says so,
    and says something different from the case where the feedback does carry
    the reveal.
12. Not covered by a test, and named here so the gap is on the record: that the
    note and the marker are *placed* where the spec says, that the closed
    state's styling distinguishes it from the resolved one, and that
    `probeGraded` clears the panel it does not rewrite. All three are
    `createDomView` and `overlay.css`, which have no tests; they are verified
    by inspection.
