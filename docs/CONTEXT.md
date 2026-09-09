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

**Segment** — an element of the explanation's token stream: `text`, `math`, or
`blank`. The explanation is a segment array, never a string with sentinels.

**`text` segment** — a **restricted Markdown** subset: inline code, fenced code
blocks with highlighting, bold, italic, lists, links. Sanitised before rendering,
model-authored or not.

**`math` segment** — LaTeX converted to **MathML server-side** by the Pipe. No
client JS, no fonts, no CDN, and zero payload beyond the content itself, against
~500-600KB per render for inlined KaTeX that `srcdoc`'s opaque origin cannot even
browser-cache. A blank **masks a whole formula, never a term inside one** — nesting
a blank inside MathML is deferred past the prototype. A blank whose answer is a
formula resolves to a `math` node, whose MathML the Pipe returns in the grading
response it was already sending.

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
no state. **Asking never costs a call**: Novice probe questions are pre-authored in
the pedagogy payload, and an Advanced one is a nullable field on the grading
response already in flight. A probe **mutates blank state**, so it is an event and a peer of
`Guess`, not an annotation on one.

**`probe_cadence`** — a `UserValves` setting:
`off | final_blank_only | sometimes | always`, default `sometimes`. Flipping it
applies **immediately**, from the next correct answer — unlike the mode toggle, no
pre-authored content is bound to it. A probe already on screen **stands**; the
learner's escape is to dismiss it. Recorded twice: `probe_cadence_at_authoring` on
the attempt (so `off` is visible even when zero probes fire) and `cadence_at_fire`
on each probe (so mid-quiz changes stay exact). **Turning probes off does not change the
prompt**: segment 1 stays byte-identical and the client simply stops firing the
probe. See [ADR-0010](adr/0010-per-learner-instruction-toggles.md).

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

**LearnerSettings** — the two per-learner settings as one value: the mode toggle
and `probe_cadence`. What the Pipe maps `UserValves` into, and the value the
byte-identity assertion varies, so the next toggle is swept into it rather than
needing its own test. Carries **no model and no effort** — those are admin
`Valve`s (ADR-0014) and there is no field for them. The mode is *carried, not
validated*: `ModeRegistry` stays the only place mode is branched on. Held per
learner by `LearnerSettingsRepository`, written by the Pipe through the quiz
service's settings route, and read on the answering path for the cadence alone —
never for the mode.

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

**Sealed** — an attempt that will never be written again. One predicate: **all
blanks resolved and no probe pending**. Because a failed probe can re-open a blank
in Advanced, `resolved` is not a terminal state and completion can fire and un-fire;
the same predicate covers probes-off and dismissed probes without a branch.

## The prompt

**Model** — `claude-opus-5`, set by an admin-level `Valve`, never `UserValves`
(caches are model-scoped, so a per-learner choice would fragment every segment 1).
Chosen over `claude-sonnet-5` for its **512-token** minimum cacheable prefix against
Sonnet 5's 1024: with five prefixes each smaller than one combined prefix would be,
falling under the floor silently costs the cross-user sharing that is the whole
point of segment 1. **Effort is `low` everywhere for now** — a per-call-type knob
set uniformly until measurement moves it, and constant per call type because
changing it invalidates that prefix. Thinking is never disabled.

**Segment 1** — invariant tutor instructions, **one per call type**. Byte-identical
for every learner in the workspace, so each caches once and everyone reads it
cheaply. Five call types means five shared prefixes, not one — the invariant is
"byte-identical across learners *for a given call type*", and the model's minimum
cacheable prefix must be cleared by **each** of them independently. **No
learner-specific text may ever appear here** — and that includes *omitting* text
per learner, which splits the cache just as surely as adding it. Per-learner
toggles switch client behaviour; instructions that genuinely must vary go in
segment 2.

**Segment 2** — learner profile, quiz, and blank rubrics. Per learner, per session.
Also carries anything that varies by *mode*: the mode's `blank_range` is stated
here on the authoring call, because segment 1 is one prefix for the whole
workspace and the schema fragment cannot express an array count.

**Volatile tail** — guesses so far in order, then the current blank and guess.
Never cached.

**Call type** — one of the five distinct requests the system makes:
`author_skeleton`, `author_pedagogy`, `grade_answer`, `grade_probe`,
`fold_narrative`. Each is a method on `ModelClient`, each has its own instructions,
its own output schema, and therefore its own segment 1 and its own cache prefix.
Naming them is what keeps the inventory visible; a generic `complete()` would hide
it and destroy the "this was never called" assertion the seam exists for.

**Reactive tutor line** — a sentence responding to *how* a learner phrased a wrong
answer. **Advanced only**, and a nullable field on the grading response, never a
call of its own. Novice has none: its feedback is wholly pre-authored, so a Novice
answer costs zero model calls without qualification. Nothing streams.

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
Nothing below it imports Open WebUI. **A process boundary, not a convention:** the
domain runs as its own service, so an Open WebUI import below the seam would not
resolve. The import test remains as a fast check.

**Quiz service** — the separate process running the domain package and serving its
own HTTP API. Owns the answer key, the repositories, the renderer and every model
call. A sibling container to Open WebUI.

**Pipe** — the Open WebUI adapter, and nothing else. Maps `__user__` to a
`LearnerId`, calls the quiz service, returns the HTML the service rendered. It
holds no domain logic and makes no model calls.

**Overlay** — the document the learner plays a quiz in: one self-contained HTML
page the quiz service renders and the Pipe returns, displayed in Open WebUI's
sandboxed `srcdoc` iframe. It carries the explanation with its blanks drawn
inline, one input control per blank chosen by the mode's **render hint**, and the
capability token. It fetches nothing — no CDN, no font, no stylesheet — and
renders no model-authored text itself: every fragment in it was sanitised in the
service before it left. A `direct_answer` gets an overlay too, with the prose and
no quiz machinery.

**Capability token** — how a request from the iframe is authorized. HMAC-signed,
scoped to one `QuizSessionId` and its `LearnerId`, short TTL, minted into the
`srcdoc` at render time. The iframe is sandboxed without `allow-same-origin`, so it
has an opaque origin and carries no cookie and no Open WebUI token — it cannot
authenticate by any ambient means. **Each grading response returns a fresh token**
which the iframe swaps in, so an engaged learner's session slides forward while the
exposure window stays at minutes. The `QuizSessionId` is **not** a credential: it is
timestamp-prefixed, near monotonic, and stamped through the audit trail.

**Default install** — an Open WebUI deployment with `IFRAME_CSP` unset, which is the
shipped default. The prototype targets this and only this. Under the hardening
docs' recommended CSP an iframe has no `fetch`, no WebSocket and no way to reach the
Pipe at all, which is a different architecture rather than a degraded path.
