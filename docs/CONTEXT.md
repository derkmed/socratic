# CONTEXT — shared language

The vocabulary of this project. When a term here appears in code, docs or
conversation, it means exactly this.

## People and identity

**Learner** — a person using the app. Not "user"; the word carries the product's
intent, and Open WebUI already owns the word "user".

**LearnerId** — our identifier for a learner. Derived from Open WebUI's
authenticated `__user__["id"]` by the adapter, then never mentioning the host
again. Partition key for everything.

**QuizSessionId** — a ULID we mint at authoring time. Primary key for a quiz
session. **We mint it because there is nothing to borrow:** the Anthropic Messages
API is stateless and has no conversation object, and Open WebUI's `chat_id` is not
reliably present in every invocation context (open issue #20563) and would leak the
host through the portability seam.

**Host annotations** — `chat_id`, `session_id` and each Anthropic response
`message.id`, persisted on the attempt for accountability. Never keys, never
depended on. The `message.id` values are the audit link from a stored quiz back to
the exact API calls that produced it.

## The quiz

**Quiz** — one authored exercise: an explanation with blanks, produced by a single
authoring call.

**Segment** — an element of the explanation's token stream, either `text` or
`blank`. The explanation is a segment array, never a string with sentinels.

**Blank** — one masked element. Carries mode-specific fields: Novice has `options`,
`correct_option_id`, `reinforcement` and three `hints`; Advanced has a `rubric`.

**Answer key** — the correct answers and pre-authored feedback. Lives in the store,
inside the backend process. Never serialized into the iframe.

**Hint ladder / rung** — the three escalating responses to a wrong answer. The
client counts attempts and selects the rung; the model authors the text. On a
re-opened blank the ladder **resumes where it left off** — a failed probe is
evidence the learner needed more help, not less.

**Probe** — the tutor asking *how did you arrive at that?* after a correct answer,
and the learner's free-text reply. Fires on a coin flip per correct answer plus the
final blank unconditionally; **the client owns the cadence**, since the model holds
no state. The question rides the parallel tutor call that already fires during the
celebration. A probe **mutates blank state**, so it is an event and a peer of
`Guess`, not an annotation on one.

**Self-explanation** — the learner's reply to a probe. The richest curation signal
in the system: a click shows someone was right, this shows whether they knew.

**Queued topics** — additional distinct questions the learner raised, listed rather
than answered, per the single-topic-focus rule. Persisted on the attempt. When a
learner uses **start this instead** to displace the current quiz, the remaining
queue carries forward to the new attempt and the displaced one is marked
`abandoned`.

## Difficulty

**DifficultyMode** — an enum. `NOVICE` and `ADVANCED` today; more are expected.

**ModePolicy** — everything that varies by mode, bundled: authoring schema
fragment, grading strategy, validator rules, render hint, `blank_range`
(Novice 1–2, Advanced 4–6), and `probe_failure_behavior`.

**`probe_failure_behavior`** — what a failed probe does. **Advanced** re-opens the
blank; **Novice** corrects the misconception but leaves it resolved, because
re-opening a two-option bank whose answer the learner was just told is degenerate.
Capped at one re-open per blank; a second failed probe reveals and moves on.

**ModeRegistry** — maps mode to policy. **The only place mode is branched on.** An
`if mode ==` anywhere else is a bug.

**Mode toggle** — a per-learner setting held in Open WebUI's `UserValves`.
Applies from the **next authoring call**; a quiz in flight finishes in the mode it
was born in. One attempt, one mode — a Novice quiz has no rubrics and an Advanced
one has no option banks, so switching mid-quiz would mean re-authoring and
discarding retrieval work already done.

## Records

**QuizAttempt** — one document per quiz session. Holds the raw unfilled quiz as
authored plus two ordered collections, `guesses` and `probes`, merged by timestamp
when a timeline is needed. Bound: ≤20 blanks, ≤4 guesses and ≤2 probes per blank —
so ≤80 guesses and ≤40 probes. Raising the blank cap is the trigger to migrate to a
single event stream.

**Guess** — one submission: blank, value, verdict, ladder rung, timestamp, and
whether it was graded deterministically or by the model.

**Outcome** — `in_flight` | `resolved` | `abandoned`. `in_flight` is the stored
truth for an unfinished quiz; `abandoned` is written only on displacement, when the
learner explicitly starts something else. **We never guess that a learner left** —
a closed tab tells us nothing, so readers apply their own age threshold.

**RatingRecord** — an optional 1–5 Likert score keyed by attempt id. A human signal
about quiz quality, outside the LLM conversation entirely. Separate record, so the
attempt stays sealed.

**Sealed** — an attempt that will never be written again. **Gated on the final
probe resolving**, not on the last blank resolving: because a failed probe can
re-open a blank in Advanced, `resolved` is not a terminal state and completion can
fire and un-fire.

## The prompt

**Segment 1** — invariant tutor instructions. Byte-identical for every learner in
the workspace, so it caches once and everyone reads it cheaply. **No
learner-specific text may ever appear here.**

**Segment 2** — learner profile, quiz, and blank rubrics. Per learner, per session.

**Volatile tail** — guesses so far in order, then the current blank and guess.
Never cached.

**LearnerProfile** — two parts. The **ledger** is structured and exactly
recomputed: topic counts, mode history, weak-area tallies, outcome counts,
incremented from new attempts. The **narrative** is the LLM-authored prose summary,
folded forward incrementally. Where they disagree, **the ledger is authoritative**.
Rendered into segment 2 through a single call; the domain never reads its
internals, so its shape is free to change.

**ProfileBuilder** — the offline job that advances the profile. Built
**incrementally over all history**, never a recency window, so infrequent learners
are not penalised. Never on a learner's critical path.

**Watermark** — the last attempt `ProfileBuilder` processed. Each run reads only
attempts after it, so build cost tracks new activity rather than lifetime history.

## Boundaries

**Portability seam** — the line between the Open WebUI adapter and the domain
package. Crossed by exactly one thing: the adapter calling down with a `LearnerId`.
Nothing below it imports Open WebUI. Enforced by review and an import test.
