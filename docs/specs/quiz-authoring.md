# QuizAuthoring — a quiz from an inquiry, against a stubbed model

Issue [#5](https://github.com/derkmed/socratic/issues/5). Narrows the master
spec's [section 6](socratic-learning-app.md) to the **skeleton call only**;
splitting authoring into skeleton plus pedagogy payload is
[#9](https://github.com/derkmed/socratic/issues/9).

**Superseded in part by**
[`skeleton-and-pedagogy-split.md`](skeleton-and-pedagogy-split.md), which adds
the second call. The out-of-scope note below - that the skeleton response has
to carry the full blank shape because the conditional validator would otherwise
refuse it - is exactly what that spec removes.

## Goal

The single entry point to the authoring path: a learner's inquiry goes in, and
either a validated `Quiz` or a `DirectAnswer` comes out **as a value**.
`QuizAuthoring.author` assembles the prompt, makes the one blocking
`author_skeleton` call, parses the structured response, validates the quiz
through the mode registry, persists an in-flight `QuizAttempt` stamped with the
call's `message_id` and token usage, and returns the `direct_answer | quiz`
union. The whole path runs against a `ModelClient` stub, so it is testable with
no network and no SDK.

## Seams

One existing seam is implemented; no new seam is introduced.

| Seam | Kind | Status |
|---|---|---|
| `QuizAuthoring.author(inquiry, learner_id, ...) -> AuthoringResult` | service | **The seam this spec builds.** Already named in the master spec's seam table. Tests attach here. |
| `ModelClient` | port | Existing. Driven through `RecordingModelClient`, which *is* the double — no new stub. |
| `AttemptRepository` | port | Existing. Driven through `InMemoryAttemptRepository`, which *is* the double. |
| `PromptAssembler.assemble` | pure function | Existing. Called, not re-tested here. |
| `validate_quiz` / `ensure_valid_quiz` | pure function | Existing. Called, not re-tested here; the blank-range rejection is asserted **through** `author`, which is what proves the range is read from the registry. |

Deliberately not seams: the parser. It is an internal of `authoring.py`,
exercised entirely through `author` by feeding the stub a malformed payload.
Giving it its own public entry point would be a second way into the same code.

## Decisions

| # | Decision | Source |
|---|---|---|
| D2 | Structured output; `direct_answer \| quiz` union at the top level; flat segment array of `text \| math \| blank`; one `Blank` type with nullable mode-specific fields plus an explicit conditional validator. | [ADR-0004](../adr/0004-quiz-wire-format.md) |
| D5 | The attempt is one bounded document partitioned on `learner_id`, ULID-keyed, holding the raw unfilled quiz. | [ADR-0005](../adr/0005-persistence-contract.md) |
| D7 | We mint the `QuizSessionId` at authoring time; every `message.id` is a host annotation persisted for accountability, never a key. | [ADR-0007](../adr/0007-quiz-session-identity.md), CONTEXT: Host annotations |
| D11 | The skeleton call is the only blocking one. A `direct_answer` skips the second call entirely — and at this stage there is no second call at all. | [ADR-0011](../adr/0011-latency-budget.md) |
| D14 | `claude-opus-5`, effort `low`, stamped on the attempt. | [ADR-0014](../adr/0014-model-choice-and-effort.md) |
| — | `blank_range` is Novice 1–2 and Advanced 4–6, read from `ModePolicy`, and rejects independently of `STORAGE_BLANK_CAP`. **An `if mode ==` outside the registry is a bug.** | CONTEXT: ModePolicy / ModeRegistry, master spec acceptance 25 |

## Approach

One new module, `src/socratic/domain/authoring.py`, and its tests.

1. **Parse.** `ModelResponse.content` is the structured-output payload verbatim,
   so it is JSON against the ADR-0004 union: a `"type"` discriminator of
   `"direct_answer"` or `"quiz"`. A `direct_answer` parses on its own shape and
   never reads blanks that are not there. Every malformed shape — bad JSON, a
   missing or unknown discriminator, a wrong field type, an unknown explanation
   segment type — raises `AuthoringParseError`, a clean domain failure rather
   than a `TypeError` from inside a dataclass. Structural failures raised by
   `Quiz.__post_init__` (duplicate blank ids, a dangling blank reference) are
   caught and re-raised the same way.
2. **Assemble.** `assemble(CallType.AUTHOR_SKELETON, profile=…,
   probe_cadence=…, quiz=None)`, with the inquiry appended to the **volatile
   tail** — it is per-request and must never be cached.
3. **Call.** `ModelClient.author_skeleton`, exactly once.
4. **Validate.** `ensure_valid_quiz` through the registry. No validation is
   written here.
5. **Persist.** An in-flight `QuizAttempt` holding the raw unfilled quiz,
   stamped with a `ModelCallRecord` carrying the call's `message_id` and token
   usage, saved through `AttemptRepository`. A `direct_answer` persists nothing.
6. **Return** the union as a value.

## Out of scope

- **The pedagogy payload and the second call.** #9. This spec makes exactly one
  model call, and the skeleton response therefore carries the full blank shape
  the registry's `authoring_schema_fragment` already declares (options,
  `correct_option_id`, `reinforcement`, `hints`, `rubric`) — otherwise the
  conditional validator could not pass.
- **The Anthropic adapter.** #7. No SDK import, no network; the path is
  exercised through `RecordingModelClient`.
- **Reading the learner profile from `LearnerProfileRepository`.** The profile
  is passed in. Two `LearnerProfile` classes currently exist on `main`
  (`prompting.LearnerProfile` and `records.LearnerProfile`); reconciling them is
  #16's decision, not this spec's, so this path touches only the one the seam it
  crosses owns — `prompting.LearnerProfile`, for segment 2.
- **The learner's mode and cadence settings.** #14 owns where they come from;
  they are parameters here.
- **Grading, probes, sealing, rendering, queued-topic displacement.** #6, #8,
  #10, #11, #15.
- **Retries, fallbacks and rate-limit handling.** Master spec out-of-scope.
- **Deriving `prompt_version` from the assembled prompt.** A constant for now,
  overridable at construction.

## Acceptance

Restating the issue's criteria as checkable conditions.

1. An ordinary inquiry returns a validated `Quiz`, and an in-flight
   `QuizAttempt` carrying the raw unfilled quiz is in the repository afterwards.
2. A safety-relevant or "I need this now" inquiry returns a `DirectAnswer`, and
   the parse succeeds without reading blanks that are not there (master spec
   acceptance 4).
3. A `direct_answer` persists no attempt: the learner's partition is empty.
4. A quiz whose blank count falls outside the mode's `blank_range` is rejected
   with `QuizValidationError`, with the range read from the registry — asserted
   for both shipped modes and through a registry carrying a *custom* range, so
   the number cannot have been hardcoded (master spec acceptance 25).
5. The persisted attempt carries one `ModelCallRecord` for `author_skeleton`
   with the response's `message_id` and its token usage.
6. `RecordingModelClient` records exactly one call, of type `author_skeleton`,
   and no other call type is ever made.
7. Malformed content — bad JSON, missing discriminator, unknown discriminator,
   unknown segment type, non-list explanation — raises `AuthoringParseError`,
   not an arbitrary exception, and persists nothing.
8. The prompt handed to the model carries the inquiry in the volatile tail and
   nowhere above a cache breakpoint.
