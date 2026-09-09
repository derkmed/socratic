# Queued inquiries and start-this-instead displacement

Issue [#15](https://github.com/derkmed/socratic/issues/15). Master spec
`docs/specs/socratic-learning-app.md`, D5 and acceptance 26.

## Goal

A learner with a quiz open asks a second question. The single-topic-focus rule
says we do not answer it now, and the quiz in front of them does not get pulled
away: the new inquiry is **queued** on the live attempt and the attempt carries
on. The escape is explicit — **start this instead** — which marks the displaced
attempt `abandoned`, authors the new quiz, and carries the remaining queue
forward onto it.

`abandoned` is written **only** on that explicit displacement. There is no
sweeper and no timeout, so `in_flight` stays the stored truth until the learner
moves on themselves, and `sealed_at` stays **null** on an abandoned attempt —
it was never completed, only left.

## Seams

Existing, and where the tests attach:

- **`QuizAttempt`** (`src/socratic/domain/records.py`, `tests/test_records.py`) —
  `abandoned()` already exists but writes a `sealed_at`, which acceptance 3 of
  the issue forbids. The `outcome`/`sealed_at` invariant in `__post_init__` and
  the closed-write guard move with it. `queued_topics` is already a field; what
  it lacks is a `with_*` write, which is how every other write on this record
  reaches it.
- **`InMemoryAttemptRepository.save`**
  (`src/socratic/domain/repositories.py`, `tests/test_repositories.py`) — refuses
  a second write to a sealed attempt today, and must refuse one to an abandoned
  attempt for the same reason once `sealed_at` no longer marks it.
- **`profile_builder`** (`src/socratic/domain/profile_builder.py`,
  `tests/test_profile_builder.py`) — `_eligible` walks the contiguous run of
  *closed* attempts, spelled `is_sealed` today. An abandoned attempt with no
  `sealed_at` would stop that walk forever, so the predicate moves with the
  record's.
- **`QuizAuthoring.author`** (`src/socratic/domain/authoring.py`) — unchanged.
  Displacement composes it rather than reaching inside it.

New, one of them:

- **`socratic.domain.inquiry.InquiryIntake`** (`tests/test_inquiry.py`) — the
  door a learner's inquiry arrives at, composing `QuizAuthoring` and the
  `AttemptRepository`. It exists because the queue-or-author decision reads the
  learner's partition, which authoring does not do and should not learn to; and
  it mirrors `QuizSession`, which is the same shape (a composition over the
  model client and the repository) for the submit path.

## Decisions

- **Queued topics** (CONTEXT: Queued topics) — additional distinct questions the
  learner raised, listed rather than answered, persisted on the attempt. On
  displacement the remaining queue carries forward and the displaced attempt is
  marked `abandoned`.
- **Outcome** (CONTEXT: Outcome, ADR-0005) — `abandoned` is written only on
  displacement. We never guess that a learner left; readers apply their own age
  threshold.
- **`sealed_at` stays null on an abandoned attempt** (issue #15). This corrects
  the record as built: `abandoned()` set `sealed_at`, and `__post_init__`
  required it for any non-`in_flight` outcome.
- **Sealed** (CONTEXT: Sealed) keeps its meaning — all blanks resolved and no
  probe pending — so it is not the predicate for "never written again" any more.
  That becomes **closed**: sealed *or* abandoned.
- **Writes go through the `with_*` methods** (records.py docstring, #46), so
  queuing a topic is `with_queued_topics`, not `dataclasses.replace`.
- **One attempt, one mode** (CONTEXT: Mode toggle) — the new attempt is authored
  in whatever mode is current, not in the displaced attempt's mode.
- **Session identity** (ADR-0007, #58) — the restart is a new attempt on a new
  `QuizSessionId`. `get_by_session` returning the latest attempt is what already
  reads the restart rather than the attempt it displaced.

## Approach

1. **The record.** `abandoned()` loses its `at` argument and sets `outcome` only.
   `__post_init__` pairs `sealed_at` with `resolved` alone: `in_flight` and
   `abandoned` both carry none. `is_closed` joins `is_sealed`, and the write
   guard becomes `_refuse_if_closed`, so an abandoned attempt still refuses a
   guess, a probe, a model call, a quiz swap and a seal. `with_queued_topics`
   is added alongside the other writes, under the same guard.
2. **The repository and the profile builder.** `save` refuses a second write to
   a closed attempt; `_eligible` folds closed attempts, so an abandoned one no
   longer blocks the profile job's watermark forever.
3. **`InquiryIntake`.** Two entry points over the same helper:
   - `raise_inquiry(inquiry, learner_id, ...)` — with a live attempt in the
     learner's partition, appends the inquiry to its queue and returns
     `Queued`; with none, authors and returns `Authored`.
   - `start_this_instead(inquiry, learner_id, ...)` — authors first, then
     abandons the live attempt, then writes the displaced attempt's remaining
     queue onto the new one, ahead of whatever the new quiz brought with it.
     A `DirectAnswer` displaces nothing: there is no new attempt for the queue
     to land on, so the live attempt carries on.

   The live attempt is the latest `in_flight` attempt in the partition.
   Queuing is distinct-preserving: an inquiry already on the queue is not
   queued twice, compared as stripped strings.

## Out of scope

- **Any sweeper, timeout or heuristic** that infers a learner has left. Forbidden
  by the issue and by CONTEXT: Outcome, and this spec adds none.
- **An `abandoned_at` timestamp.** The record has no field for one and the master
  spec's field list does not name one; the successor attempt's `created_at` is
  when the displacement happened.
- **HTTP endpoints and the Pipe.** `InquiryIntake` is a domain seam; wiring it
  behind `/author` (and deciding what the client renders for a queued inquiry)
  belongs with the service and the Open WebUI adapter, which are live work
  elsewhere.
- **Rendering the growing queue in the overlay.** The overlay renders
  `quiz.queued_topics` as authored; the attempt is the store of record and the
  live document is not re-rendered mid-quiz.
- **A bound on queue length.** No decision names one; the record's other bounds
  all come from ADR-0009, which is silent here.
- **Deduplicating near-duplicate inquiries.** Exact string comparison only; a
  model call to tell two phrasings apart is not warranted.

## Acceptance

1. A second inquiry raised while an attempt is `in_flight` is appended to that
   attempt's `queued_topics`, no model call is made, and the attempt stays
   `in_flight` with its guesses and probes intact.
2. `start_this_instead` marks the displaced attempt `abandoned` and the new
   attempt's `queued_topics` begins with the displaced attempt's remaining queue
   (master acceptance 26).
3. The inquiry being started is not carried forward as a queued topic on the
   attempt that answers it.
4. An abandoned attempt keeps its guesses and probes and has `sealed_at is None`.
5. An abandoned attempt is closed: every `with_*` write, and `sealed()`, refuse
   it, and the repository refuses a second write to it.
6. Nothing in the domain writes `abandoned` except an explicit displacement.
7. `queued_topics` persists on the attempt record across a save and a read.
8. The profile builder counts an abandoned attempt under `outcomes/abandoned`
   and does not stall on it.
