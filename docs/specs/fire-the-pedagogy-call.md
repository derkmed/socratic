# Firing the pedagogy call from the service

Issue [#118](https://github.com/derkmed/socratic/issues/118). Discharges the
one thing
[`skeleton-and-pedagogy-split.md`](skeleton-and-pedagogy-split.md) put out of
scope and handed to [#10](https://github.com/derkmed/socratic/issues/10): *"the
quiz service calls `author_pedagogy` off the render path."*

## Goal

`QuizAuthoring.author_pedagogy` is implemented, tested and never called outside
`tests/`. Every quiz a learner plays therefore ships with no hint rungs, no
rung-three reveal, no reinforcements, no probe questions and `recap_html == ""`.
The fix is one wire: **when a route authors a quiz, the second authoring call of
[ADR-0011](../adr/0011-latency-budget.md) is fired after the response is
handed back.**

Nothing about the response changes. The skeleton is still the only blocking
call, still what the overlay is rendered from, and still what a learner who
answers inside the window plays against.

## Seams

No new seam. One existing service seam gains its first production caller, and
the routes gain the framework's own after-the-response hook.

| Seam | Kind | Status |
|---|---|---|
| `QuizAuthoring.author_pedagogy(quiz, learner_id, *, probe_cadence, profile)` | service | **Existing, unchanged.** Its contract — re-read the attempt, drop a payload that lands on a closed one, write nothing on failure — is already specified and tested in `skeleton-and-pedagogy-split.md`. This spec only calls it. |
| `_started(result, learner_id, ...)` in `service/app.py` | internal | Existing. The single point where an `AuthoringResult` becomes a wire body, reached by `/quizzes`, `/overlays` **and** `/displacements`. The fire goes here, so one wire covers all three rather than three copies of it. |
| `BackgroundTasks` | framework | Starlette's, injected by FastAPI. This is what "off the render path" means for a synchronous service: the task runs after the response body is sent, and — because the handlers are `def` rather than `async def` — in the threadpool, so it blocks no request. `TestClient` runs background tasks in-request, which is what makes the whole wire assertable through HTTP. |
| `RecordingModelClient` | test double | Existing. `CallType.AUTHOR_PEDAGOGY` is queued through `Harness(also=...)`, exactly as `test_the_pedagogy_call_lands_on_it` already does by hand. |

## Decisions

| # | Decision | Source |
|---|---|---|
| D11 | Two authoring calls; the skeleton is the only blocking one; the pedagogy payload is "fired immediately after and fetched while the learner spends 30-60 seconds reading 250 words". | [ADR-0011](../adr/0011-latency-budget.md) |
| — | A failed pedagogy call writes nothing and leaves the learner with the playable skeleton. | `skeleton-and-pedagogy-split.md` §4 |
| — | A `direct_answer` fires no second call at all. | `skeleton-and-pedagogy-split.md`, acceptance 7 |
| — | A sealed or abandoned attempt drops a late payload without error. | `skeleton-and-pedagogy-split.md`, acceptance 11 |
| — | The API layer is a translation over the domain seams and holds no policy. | `service/app.py` module docstring |

## Approach

### 1. Where the call is fired

`_started` is the one place a `types.AuthoringResult` becomes a body, and it
already branches on `DirectAnswer` — which is precisely the branch that must
fire nothing. It takes one more argument, the `BackgroundTasks` the route was
handed, and on the quiz branch schedules the pedagogy call before returning the
body.

`/quizzes`, `/overlays` and `/displacements` each declare
`background: BackgroundTasks`. `/settings`, `/answers`, `/probes` and
`/ratings` author nothing and are untouched.

`/displacements` fires it too, and deliberately: **"start this instead" authors
a quiz by the same call and hands it to the same learner**, so a displaced
restart that came back without hints would be the reported bug again by a
different door.

The queued branch returns before `_started` is reached, so a queued inquiry
fires nothing without any extra guard — the learner already has a quiz, and the
attempt they have is the one that was already given a payload.

### 2. Why `BackgroundTasks` and not a thread

The domain is synchronous and stdlib-only, and the service already owns
FastAPI. Starlette runs the task after the response is written, so the learner's
`/overlays` round trip is unchanged; because the handlers are `def`, FastAPI
runs both handler and task in its threadpool, so the pedagogy call blocks no
event loop.

The alternative — a `ThreadPoolExecutor` owned by `ServiceDependencies` — buys
nothing here and costs a lifecycle to manage and a shutdown to get right. The
smaller, more reversible choice is the framework's. If the prototype ever needs
retries, a queue or a shutdown drain, that is where a real dispatcher gets
introduced, and this wire is the thing it would replace.

### 3. Failure is swallowed, and said out loud

`author_pedagogy` can raise `AuthoringParseError`, `QuizValidationError`,
`KeyError` or whatever the model client raises. **None of those may reach the
learner**: the response has already been sent, and the point of the split is
that the quiz is playable without the payload.

So the scheduled callable wraps the call in `except Exception` and logs a
warning naming the session. Swallowing without logging would turn a broken
second call back into exactly the invisible failure this issue is about — the
symptom was reported as "hint generation is slow" precisely because nothing said
anything.

`logging.getLogger(__name__)` at module scope; the service configures no
handlers, so this rides whatever uvicorn has set up.

### 4. The learner's cadence

`author_pedagogy` takes `probe_cadence`. Both call sites have already resolved
the learner's settings — `_authored` saves them, `/displacements` reads them —
so the resolved value is passed through rather than re-read. It never reaches
the prompt ([ADR-0010](../adr/0010-per-learner-instruction-toggles.md)); it is
passed because the seam takes it and a caller that dropped it would make that
un-assertable.

No `LearnerProfile` is passed, matching `_authored`'s existing skeleton call,
which passes none either.

## Out of scope

- **The 12.4 s skeleton call.** The issue names it and excludes it; wiring the
  second call does not change it.
- **Retries, backoff, or what happens when the pedagogy call never returns.**
  Master spec out-of-scope, restated by `skeleton-and-pedagogy-split.md`. The
  quiz stays playable without it.
- **Inferring "pedagogy pending" from empty feedback** —
  [#113](https://github.com/derkmed/socratic/issues/113).
- **The Pipe.** `pipe/socratic_pipe.py` calls `/overlays` and gets a document
  back; the fire is entirely on the service's side of that call and the Pipe
  needs no change.
- **Removing `land_probe_questions` from `tests/test_service.py`.** It writes
  probe questions straight onto the stored attempt to work around this bug. It
  is still the honest fixture for the cadence tests, which want a probe question
  on a blank without also wanting a second model call in the way; its comment is
  corrected to say the call is now wired, rather than unreachable.

## Acceptance

1. `POST /quizzes` that authors a quiz makes exactly one
   `AUTHOR_PEDAGOGY` call, and exactly one `AUTHOR_SKELETON` call.
2. After that request, the stored attempt's quiz carries the recap, and per
   blank the reinforcement, three hints and the probe question — the issue's
   own reproduction, from the HTTP side.
3. The `/quizzes` response body is byte-identical to what it was before: the
   skeleton, with no hint text and no key.
4. `POST /overlays` fires the call too.
5. `POST /displacements` fires the call for the newly authored quiz.
6. A `direct_answer` fires no `AUTHOR_PEDAGOGY` call.
7. A queued inquiry fires no `AUTHOR_PEDAGOGY` call.
8. A pedagogy call that raises leaves the route's response a 200 with the
   playable skeleton, writes nothing to the attempt, and logs a warning naming
   the session.
9. The pedagogy call is made **after** the response is produced, not before it —
   asserted by the route returning a body even when the call raises, and by the
   skeleton call being recorded first.
