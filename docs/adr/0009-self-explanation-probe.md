# 0009. The self-explanation probe

Date: 2026-09-08
Status: accepted
Amended by: [ADR-0011](0011-latency-budget.md) (call count corrected again: a probe
is two calls, not one, so a 2-blank Novice quiz is 3-5, not ~3)
Amends: [ADR-0003](0003-grading-authority-and-key-custody.md) (call-count claim),
[ADR-0005](0005-persistence-contract.md) (document bound)

## Context

The tutor prompt gained an instruction after the ADRs were written: on roughly
every second correct answer, and always on the final blank, ask the learner *how
did you arrive at that?* before moving on.

It is the strongest addition to the prompt — a click tells you someone was right,
and only a self-explanation tells you whether they knew — and it breaks two things
that were already written down.

**It breaks ADR-0003's headline figure.** A self-explanation is unbounded prose. It
cannot be pre-authored, so responding to it needs a model call even in Novice.

**The model cannot own the cadence.** "Roughly every second" requires counting
correct answers, and under ADR-0001 the model holds no state.

## Decision

**Cadence is client-owned and randomised.** A coin flip per correct answer, plus
the final blank unconditionally. Tests use a seeded RNG. A learner cannot game a
rhythm that does not exist.

**The probe is graded substantively**, and what a failed probe does is a
`ModePolicy` field, `probe_failure_behavior`:

- **Advanced** — re-opens the blank. Free recall is genuinely re-answerable.
- **Novice** — the tutor corrects the misconception but the blank stays resolved.
  Re-opening a two-option bank whose answer the learner was *just told* is
  degenerate: nothing remains to retrieve, and pretending otherwise signals that
  the system is not paying attention.

**On re-open the hint ladder resumes where it left off**, and re-opening is capped
at **one per blank**; a second failed probe reveals and moves on, mirroring rung 3.

**Sealing is gated on the final probe resolving**, not on the last blank resolving.
`resolved` is no longer a terminal state.

**Probes are their own ordered collection** on the attempt, a peer of `guesses`.
A probe mutates blank state, so it is an event, not an annotation on a prior guess;
modelling it as a property of a past guess would misrepresent the timeline to
exactly the curation job that wants to replay it.

**The probe question rides the existing parallel tutor call** from ADR-0003 — the
call already fires during the celebration animation. Asking costs nothing new; only
the reply path is new.

## Consequences

The system can now tell solid recall from a lucky guess, which is the difference
between measuring completion and measuring learning. The self-explanation is the
richest curation signal available, and it is captured verbatim.

**ADR-0003's "~1 model call per Novice quiz" is superseded: the real figure is ~3.**
Still well below the ~8 of per-answer grading, and the decision would be unchanged,
but the number was wrong.

**ADR-0005's "~60 guesses" bound is superseded.** Restated: at most 20 blanks; at
most 4 guesses per blank (one correct, then up to three more as the ladder resumes);
at most 2 probes per blank. So at most 80 guesses and 40 probes — still hard-bounded,
still a safe document, but the arithmetic changed and the cap must be enforced.

Costs: `resolved` un-firing means completion logic must handle a quiz that was
finished and is not any more. Two ordered collections must be merged by timestamp to
reconstruct a timeline, and nothing structural stops them drifting. And a learner who
is honest about shaky reasoning is, in Advanced, given more work — the intended
outcome, but it must feel like consolidation rather than a trap, which is what the
prompt's tone paragraph is carrying.
