# QuizSession.submit — Advanced grading: model-graded free text

Issue [#8](https://github.com/derkmed/socratic/issues/8). The other half of the
master spec's [section 8](socratic-learning-app.md) seam, whose deterministic
half is [`novice-submit.md`](novice-submit.md) (#6). Probe firing, cadence and
`answer_probe` remain [#10](https://github.com/derkmed/socratic/issues/10).

> **Extended by [`advanced-hint-ladder.md`](advanced-hint-ladder.md)** (#56,
> [ADR-0016](../adr/0016-advanced-hint-rides-the-grading-response.md)). The
> grading response now carries a **fourth** nullable field, the hint ladder's
> rung text, and the client states the rung it selected in the volatile tail.
> Read "one response, three things" below as four. Everything else here stands:
> still one call, still nothing streaming, still no Novice route to any of it.

## Goal

An Advanced blank takes free text and is judged on **meaning, not wording**.
`QuizSession.submit` dispatches through `ModePolicy.grading_strategy` exactly as
it does today; `GradingStrategy.MODEL_GRADED` stops raising and instead
assembles the cache-anchored block through `prompting.assemble` — segment 1,
segment 2 carrying the quiz and its rubrics, and the volatile tail carrying
every guess so far in order then the current blank and guess — and makes
**one** `ModelClient.grade_answer` call.

**One response, three things** (D13,
[ADR-0013](../adr/0013-reactive-tutor-line.md)). That single response carries
the verdict, the probe question (nullable) and the reactive tutor line
(nullable) together. There is no second request and nothing streams. The
parallel tutor call ADR-0003 described is withdrawn.

**Novice is untouched and still costs zero model calls.** Nothing on the
deterministic path gains a model client consultation, and Novice gains no
reactive tutor line on any path — the three things ride a value that is `None`
for every deterministic submission, so there is no Novice route to one at all.

The answer key and the rubric stay in the backend
([ADR-0003](../adr/0003-grading-authority-and-key-custody.md)): the rubric goes
into the model request and never appears in what `submit` returns.

## Seams

No new seam. Everything attaches to seams that already exist.

| Seam | Kind | Status |
|---|---|---|
| `QuizSession.submit(...) -> Submission` | service | Existing (#6). The one entry point; the model-graded strategy is exercised only through it, never called directly. |
| `ModelClient.grade_answer(segments)` | port | Existing. Driven through `RecordingModelClient`, which *is* the double — its `call_count` is what proves "exactly one call", and its recorded `PromptSegments` is what proves the rubric reached the request and the free text reached the tail. |
| `prompting.assemble(CallType.GRADE_ANSWER, ...)` | assembler | Existing. The request is assembled through it, never built by hand, so the cache invariants stay properties of a returned value. |
| `ModeRegistry.policy_for(mode).grading_strategy` | lookup | Existing. Dispatch still reads it; the session still names no mode. |
| `AttemptRepository` | port | Existing. `InMemoryAttemptRepository` *is* the double. |

Deliberately **not** seams:

- **`_grade_by_model` itself.** A module-level function in the `_GRADERS`
  table, like its deterministic sibling. A public entry of its own would be a
  second way into the same code.
- **Response parsing.** A private helper, asserted through `submit`, mirroring
  how `authoring.py` parses its own structured response.

## Decisions

| # | Decision | Source |
|---|---|---|
| D4 | Advanced is model-graded per answer, because free recall means unbounded answers. **The key never leaves the backend.** | [ADR-0003](../adr/0003-grading-authority-and-key-custody.md) |
| D13 | The reactive tutor line is Advanced-only and is a **nullable field on the grading response**, never a call of its own. Novice has none. The parallel tutor call is withdrawn and nothing streams. | [ADR-0013](../adr/0013-reactive-tutor-line.md) |
| D1 | Each grading call is a fresh request: frozen prefix with a `cache_control` breakpoint, then a volatile tail holding every guess so far across all blanks in order, then the current blank and guess. | [ADR-0001](../adr/0001-client-authoritative-quiz-state.md) |
| D5 | The attempt is one bounded immutable document; the call is stamped on it through `with_model_call` with its `message_id` and token usage. | [ADR-0005](../adr/0005-persistence-contract.md) |
| D10 | Sealing stays the one unified predicate — all blanks resolved and no probe pending. Unchanged by this ticket. | [ADR-0010](../adr/0010-per-learner-instruction-toggles.md) |
| — | The hint ladder is three escalating rungs and the client selects the rung; the model is told explicitly not to choose it and not to write the hint. | [CONTEXT: Hint ladder / rung](../CONTEXT.md), `prompting._GRADE_ANSWER_INSTRUCTIONS` |
| — | `ModeRegistry` is the only place mode is branched on. | [CONTEXT: ModeRegistry](../CONTEXT.md) |

## Approach

### 1. Widening the grader contract, once

`Grader` is `Callable[[Blank, str, int], _Grade]` today, which is everything the
deterministic strategy needs and less than the model-graded one needs: it also
needs the attempt (for the guesses in order), the learner profile (for segment
2), and the `ModelClient`. The table stays homogeneous by passing a single
frozen `_GradingContext` to both graders rather than forking the signature.

`_Grade` grows three fields, all `None` on the deterministic path:
`tutor_line`, `probe_question`, and `model_call` (a `records.ModelCallRecord`
the session stamps on the attempt — strategies still do no persistence).

### 2. `ModelGrading`: the three things that arrived together

```python
@dataclass(frozen=True, slots=True)
class ModelGrading:
    tutor_line: str | None
    probe_question: str | None
```

`Submission` gains `model_grading: ModelGrading | None`. It is `None` for every
deterministic submission, so **`Submission` still has no `tutor_line`
attribute** and a Novice result has no route to one — the existing structural
assertion in `tests/test_session.py` stands unchanged. Grouping the two
nullable fields into one value is what makes "one response, three things"
readable in the return type: the verdict, and the object that rode back with
it.

### 3. Assembling the request

`prompting.assemble(CallType.GRADE_ANSWER, profile=…, probe_cadence=…,
quiz=attempt.quiz, guesses=…, current_blank_id=blank_id,
current_guess=submitted)`. No `inquiry` — that is the authoring call's field.

`guesses` is every guess on the attempt so far, **across all blanks, in the
order made** (D1), rendered to one line each by the session, which is the module
that owns the `Guess` record. The line names the blank, the submission and the
verdict; it is the tutor's memory of the attempt.

`profile` is a keyword argument on `submit` defaulting to an empty
`LearnerProfile` for the learner, exactly as `QuizAuthoring.author` already does
— reading a stored one is [#16](https://github.com/derkmed/socratic/issues/16).
`probe_cadence` is the attempt's `probe_cadence_at_authoring`; `assemble`
deliberately never reads it (ADR-0010), and the live setting belongs to #10.

### 4. Parsing the response

`ModelResponse.content` is the structured payload verbatim and parsing it
against the call's schema belongs to the caller that knows the schema — so the
port is **not** widened, and the session parses JSON the way `authoring.py`
already does. Shape:

```json
{"verdict": "correct" | "incorrect",
 "tutor_line": "…" | null,
 "probe_question": "…" | null}
```

`verdict` is required and maps onto `records.Verdict`. Both other fields are
optional and default to `None` — the instructions tell the model to omit an
optional field rather than fill it with filler. A response that cannot be read
raises `GradingParseError`, a `ValueError` subclass mirroring
`AuthoringParseError`: "the model returned nonsense" must be distinguishable
from "we built the wrong object".

### 5. The ladder, shared with Novice

A wrong Advanced answer walks the same three rungs: `rung = min(ordinal, 3)`,
and the blank resolves once three wrong guesses have exhausted the ladder —
`is_blank_resolved` already reads that off the record and is not touched.

Hint text comes from `_hint_for_rung`, which returns `None` when the blank
carries no `hints`. An Advanced blank never carries any (the registry's
validator forbids them), so `feedback` is `None` on the Advanced path and the
reactive tutor line is what the learner actually reads. That is the model's own
instruction: *"you do not choose it and you do not write the hint here."*

**`revealed_option_id` is never set on rung three in Advanced.** It names an
option id and an Advanced blank has none; the rubric is the answer key and stays
in the backend (D4). The blank still resolves.

*Amended by [ADR-0016](../adr/0016-advanced-hint-rides-the-grading-response.md)
and [ADR-0019](../adr/0019-resolved-blank-text-comes-from-the-service.md).*
"Nothing is revealed", as this section originally read, was true of the field
and false of the learner's experience, and the gap between those two is
[#127](https://github.com/derkmed/socratic/issues/127). The blank resolves with
`revealed_option_id` null, and the client — having no text to put in the gap —
fell back to the learner's third **wrong** guess and planted it in the finished
explanation. Rung three now reveals in prose through `hint` (ADR-0016) and
names the answer in a phrase through `revealed_answer` (ADR-0019), which is
what fills the gap. `revealed_option_id` stays null on this path throughout.

### 6. Persisting the guess and the call

The `Guess` appends exactly as it does today, with `graded_by` taken from the
policy — `GradingStrategy.MODEL_GRADED` on this path. The attempt additionally
takes a `ModelCallRecord` through `with_model_call`, carrying
`CallType.GRADE_ANSWER.value`, the response's `message_id`, and
`ModelResponse.token_usage()` — the port's one mapping into the persisted shape,
rather than the counters reassembled by hand. Both writes land in the single
`save`, and sealing is unchanged.

## Out of scope

- **Probe firing and cadence.** A probe question arriving on the response is
  parsed and carried; **deciding when to fire one, appending a `records.Probe`,
  the coin flip, `probe_cadence` gating, `answer_probe` and
  `probe_failure_behavior`** are all
  [#10](https://github.com/derkmed/socratic/issues/10). This ticket appends no
  probe and the seal predicate's probe clause is untouched.
- **Probe grading.** `grade_probe` is not called here.
- **Streaming, and any second request.** Withdrawn by D13. A second call on this
  path would falsify acceptance 9, so the tests assert the count is exactly one.
- **Real semantic judgement.** Against `RecordingModelClient` what is asserted is
  *plumbing* — that the rubric reaches the request, the free text reaches the
  tail in order, and a `CORRECT` verdict on differently-phrased text flows
  through to a `Guess` with `graded_by: model_graded`. Whether the model actually
  grades on meaning is only demonstrable against the live API, which is
  [#7](https://github.com/derkmed/socratic/issues/7) and gated behind billing.
- **Widening `ModelResponse`.** The port stays as it is; `content` is the
  structured payload and its schema is the caller's business.
- **Reading a stored learner profile.** `submit` takes one and defaults to an
  empty profile, as authoring does. [#16](https://github.com/derkmed/socratic/issues/16).
- **Rendering, MathML, and the resolved segment.** [#11](https://github.com/derkmed/socratic/issues/11).
- **The Anthropic adapter and the live cache counters.** [#7](https://github.com/derkmed/socratic/issues/7).
- **Retry, timeout and fallback on a failed grading call.** A raising client
  raises; there is no deterministic fallback, because an Advanced answer graded
  by string equality would look like a working feature.

## Acceptance

1. An Advanced blank accepts free text, and a correct answer phrased differently
   from the rubric returns `Verdict.CORRECT` and persists a `Guess` with
   `graded_by: MODEL_GRADED` (master spec acceptance 3, at the plumbing level the
   stub can prove).
2. **Exactly one** model call per Advanced submission, and it is
   `grade_answer`: `call_count() == 1` and `call_count(GRADE_ANSWER) == 1`.
   That one response carries verdict, probe question and reactive tutor line
   together (master spec acceptance 9).
3. Novice still costs zero model calls — both the per-answer assertion and the
   whole-attempt one, with one shared `RecordingModelClient`, stay green — and a
   Novice `Submission` carries `model_grading is None` on every path: correct,
   each ladder rung, and the reveal.
4. A wrong free-text answer walks rungs 1, 2, 3 and the blank resolves on the
   third, with nothing revealed.
5. The request carries the frozen prefix plus the volatile tail: segment 2
   contains the blank's rubric, the tail contains every prior guess in the order
   made and then the current blank id and the submitted free text.
6. The guess persists with `graded_by: MODEL_GRADED`, and the attempt carries a
   `ModelCallRecord` with the call's `message_id` and its token usage.
7. The rubric and the answer key never appear in the returned `Submission`.
8. Dispatch is still through `ModePolicy.grading_strategy`; `tests/test_registry.py`'s
   mode-branch scan stays green and the session names no `DifficultyMode`.
9. A malformed grading response raises `GradingParseError` rather than being
   silently graded.
