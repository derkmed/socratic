# Inquiry intake over HTTP

Issue [#92](https://github.com/derkmed/socratic/issues/92). Builds on
`docs/specs/queued-inquiries-and-displacement.md` (issue #15), which built
`InquiryIntake` but deliberately stopped below HTTP. Master spec
`docs/specs/socratic-learning-app.md`, D5 and acceptance 26.

## Goal

`InquiryIntake` decides whether an inquiry is authored now or queued onto the
quiz the learner already has open. Nothing above the domain calls it, so
`POST /quizzes` and `POST /overlays` still go straight to
`QuizAuthoring.author`: a second question asked mid-quiz authors a second quiz,
spends a second model call, and leaves the first attempt in flight, unqueued
and undisplaced.

Reproduced on `origin/main` @ `f1b8a43`: two `/quizzes` calls for one learner
returned two distinct `quiz_session_id`s, left two `in_flight` attempts in the
learner's partition, and made two authoring calls.

This spec routes both authoring doors through the intake, gives the response a
**queued** branch on the wire and in the overlay, and adds the endpoint the
learner's "start this instead" gesture will call.

## What the clients actually are

Settled by reading the code rather than the issue, because the Pipe landed
after #92 was filed and the issue's premise is out of date on two points:

- **The Pipe does not parse the authoring body.** `pipe/socratic_pipe.py`
  calls `POST /overlays` and returns `reply.text` verbatim as an
  `HTMLResponse`. It never looks inside. So the queued branch reaches the Pipe
  as *a document to render*, not as a shape to parse — and `/overlays` must
  grow a queued document or the Pipe breaks outright on the first queued
  inquiry.
- **`client.js` does not parse the authoring body either.** It is mounted into
  an already-rendered document and parses only the `/answers`, `/probes` and
  `/ratings` responses. The authoring body is consumed by
  `ui/overlay.py::render_overlay`, in this process.

`POST /quizzes` therefore has **no production client today** — only the tests.
It is kept, and kept in step with `/overlays`, because it is the JSON face of
the same call and the suite drives it.

## Seams

Existing, and where the tests attach:

- **`socratic.domain.inquiry.InquiryIntake`** (`tests/test_inquiry.py`) —
  unchanged. This spec composes it; it reaches inside nothing.
- **`ServiceDependencies`** (`src/socratic/service/deps.py`,
  `tests/test_service.py`) — grows an `intake`, defaulted from `authoring` and
  `attempts` in `__post_init__` exactly as `registry` and `settings` already
  are, so `__main__.build_app` and every existing construction are untouched.
- **The routes** (`src/socratic/service/app.py`, `tests/test_service.py`) —
  `_authored` calls the intake instead of authoring, and gains a third branch.
  A new `POST /displacements` joins the capability routes.
- **`payloads`** (`src/socratic/service/payloads.py`, `tests/test_service.py`) —
  the whitelist stays the only way a domain object becomes wire bytes, so the
  queued branch is a new function there and not a dict built in a handler.
- **`ui.overlay`** (`src/socratic/ui/overlay.py`, `tests/test_ui_overlay.py`) —
  `render_queued`, alongside `render_overlay` and `render_direct_answer`, over
  the wire body rather than over a `QuizAttempt`, for the reason that module's
  docstring gives.

No new seam. Everything here is translation over a domain seam that already
exists, which is what the quiz service's spec means by "the HTTP API is
deliberately not a seam".

## Decisions

Each of these was settled here rather than by the issue; the reasoning is the
point.

- **The queued branch mints no capability token, and this is forced.**
  `TokenMinter.mint` retires the session's previous token
  (`domain/tokens.py:153-178`, `self._current[session] = token_id`). Minting
  one for the learner's live session on the queued branch would silently
  invalidate the token their open overlay is holding, and their next answer
  would come back 401. The queued body therefore carries the live
  `quiz_session_id` as an **identifier only** — which it already is, and not a
  credential (D7, ADR-0007) — and no token.
- **The gesture endpoint is `POST /displacements`, authorized by the
  capability token for the attempt being displaced.** Not a `displace` flag on
  `AuthorRequest`: that would put a destructive write (`abandoned`, which the
  domain writes nowhere else) behind the *service* token, which the Pipe holds
  install-wide for every learner. Displacement is a learner's gesture about
  their own open attempt, and the capability token is precisely the authority
  "the bearer is the learner who has this session open". It reuses
  `app._authorized` unchanged, so the learner comes from the verified claims
  and never from the body.
- **`/quizzes` and `/overlays` keep their names.** The issue floated
  `POST /inquiries`. Renaming would break the Pipe's only route for no
  behavioural gain; these two *are* the door an inquiry arrives at, they simply
  skipped the intake.
- **`/displacements` returns the newly-authored body plus
  `displaced_session_id`.** The caller needs a capability token for the new
  session, and the one it presented is now attached to an abandoned attempt;
  returning the full authored body is the only way to hand it one without a
  second round trip. `displaced_session_id` is null when nothing was displaced,
  which is the `DirectAnswer` case the domain already defines.
- **The displaced attempt's old token is not revoked.** The domain already
  refuses every write to a closed attempt, so an `/answers` call on it is a 422
  rather than a leak. Revoking would mean a new `TokenMinter` operation for no
  additional safety.
- **Settings still sync on the queued branch.** `_authored` saves the learner's
  settings before it does anything else, because the Pipe pushes current valves
  with every turn. That is independent of whether the inquiry is authored.
  `raise_inquiry` ignores `mode` on the queued branch by design (CONTEXT: Mode
  toggle) — a queued topic binds no mode.
- **`/displacements` reads the learner's stored settings** rather than taking
  mode and cadence on the request: the iframe cannot see `UserValves`, and the
  attempt being displaced was authored from a settings save that already
  happened. Absent settings fall back to `LearnerSettings`' documented
  defaults.
- **The queued document ships no interactive control.** See "Out of scope".

## Approach

1. **`payloads`.** `queued_body(queued)` — `kind: "queued"`, the inquiry as
   trimmed, and the live attempt's `quiz_session_id`, `topic` and
   `queued_topics`. `DisplaceRequest(_SessionScoped)` with an `inquiry`.
   `displaced_body(body, displaced_session_id=...)`.
2. **`deps`.** `intake: InquiryIntake = None`, built in `__post_init__` from
   the `authoring` and `attempts` already on the record.
3. **`app`.** `_authored` calls `deps.intake.raise_inquiry(...)` and returns
   `queued_body` for `Queued`, the existing two bodies for `Started`.
   `/overlays` dispatches on `kind` across three branches.
   `POST /displacements` authorizes with `_authorized`, calls
   `deps.intake.start_this_instead(...)` with the stored settings, and shapes
   the result.
4. **`ui.overlay`.** `render_queued(body)` — the topic still open, the question
   just saved, and the rest of the queue. No script, no token, no controls.

## Out of scope

- **The "start this instead" control in the browser.** Filed as its own issue.
  The endpoint ships and is tested; what is missing is the button and the
  swap. It cannot honestly be built here for two reasons: the control belongs
  on the *live* quiz overlay (that is the document holding a valid token), and
  that document is rendered once and never re-rendered mid-quiz, so it does not
  know a question was queued after it was drawn — closing that needs the queue
  to ride the grading response or a poll, which is a design decision of its
  own. Doing it from the queued document instead would need a whole-document
  swap (`document.open()`/`write`) inside the sandboxed `srcdoc` frame, and
  this repo has no browser harness to verify that against — `test_ui_client.py`
  drives `client.js` in a bare Node interpreter with no DOM.
- **Re-rendering the live overlay when its queue grows.** Already out of scope
  in #15's spec, and unchanged here.
- **Revoking the displaced attempt's capability token.** Above.
- **Any sweeper, timeout or heuristic** that decides a learner has left. Still
  forbidden (CONTEXT: Outcome).
- **A queued-branch `/quizzes` client.** There is none today; this spec adds
  no consumer.

## Acceptance

1. A second `POST /quizzes` for a learner with an `in_flight` attempt returns
   `kind: "queued"`, makes **no** authoring call, and leaves exactly one
   attempt in the learner's partition — the defect this issue reports.
2. That queued response names the live attempt's `quiz_session_id` and topic,
   lists the queue including the new inquiry, and carries **no**
   `capability_token`.
3. The token the learner's open overlay holds still verifies after a queued
   inquiry — nothing was retired.
4. `POST /overlays` for a queued inquiry returns an HTML document naming the
   open topic and the queued questions, with no `<script>` and no capability
   token in it.
5. The first inquiry for a learner with nothing open is authored exactly as
   before: same body, same token, same overlay.
6. `POST /displacements` with a valid capability token for the live session
   abandons that attempt, authors the new inquiry, returns a `kind: "quiz"`
   body with a **different** `quiz_session_id` and a fresh token, and names the
   displaced session.
7. The new attempt's `queued_topics` carries the displaced attempt's remaining
   queue and does not contain the inquiry now being answered (master
   acceptance 26).
8. `POST /displacements` with a token for another session is refused 401
   before any model call, like every other capability route.
9. After a displacement, an `/answers` call against the displaced session is
   refused rather than accepted.
10. `ServiceDependencies` constructed without an `intake` builds one; nothing
    in `__main__.build_app` changes.
