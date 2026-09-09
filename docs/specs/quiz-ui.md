# The quiz UI — the overlay the learner actually uses

Issue [#13](https://github.com/derkmed/socratic/issues/13). Completes the master
spec's [section 11](socratic-learning-app.md), which
[`quiz-service.md`](quiz-service.md) deliberately left empty: that ticket
returns JSON with already-rendered, already-sanitised fragments, and "the
segment-walking renderer that consumes them" is this one's.

## Goal

The **overlay** (CONTEXT): the document the learner plays a quiz in, rendered by
the quiz service and served inside Open WebUI's sandboxed `srcdoc` iframe. The
explanation appears with its blanks drawn inline where the segment walk put
them, formulas included. Under each blank the learner gets the input control the
mode's **render hint** names — an option bank for Novice, a text input for
Advanced — chosen from data, never from a mode check in the view.

Answers leave by `fetch` carrying the **capability token**, and every grading
response returns a fresh token the client swaps in. **Nothing streams**: the
verdict starts the celebration, the Advanced **reactive tutor line** is already
in that same response, and Novice has none at all (D13,
[ADR-0013](../adr/0013-reactive-tutor-line.md)). A **probe** prompt appears when
the response carries one, and is dismissible. On **sealing** a 1–5 rating prompt
appears, is dismissible, and never enters the conversation.

And the client guards the window the split authoring call opens: the quiz is
playable on the skeleton alone, so a learner can answer before the **pedagogy
payload** lands, and the reinforcement, the hint text and the recap can all be
absent without an error (master spec acceptance 5).

## Seams

Two new, two existing. The new ones are where the ticket's behaviour actually
lives; the existing ones carry the two fields it needs that do not travel today.

| Seam | Kind | Why it must exist |
|---|---|---|
| `ui.overlay.render_overlay(body, *, service_base_url) -> str` | **new**, pure function | The only place a `render_hint` becomes an input control and a capability token becomes markup. Pure over the **wire body** `/quizzes` already returns → a string, so the whole document is assertable with no browser: the blanks are inline in declared order, each has exactly one control block, and the answer key has no field to have arrived in. Taking the wire body rather than a `Quiz` is what makes the last of those structural — `payloads.quiz_body` is the whitelist, and the overlay renders downstream of it. |
| `SocraticQuiz.createQuizClient({...})` in `ui/client.js` | **new** | Token rotation, verdict-to-celebration, probe display and dismissal, the rating prompt on sealing and the late-pedagogy guard are true only in the client. Written as a state machine over two injected ports — a `transport` and a `view` — so all of it is drivable with no DOM and no network. Driven from pytest through a child **Node** interpreter, the way `test_import_hygiene` already drives a child Python one. |
| `create_app(deps)` → `POST /overlays` | existing | ADR-0015 says the Pipe "returns the rendered HTML the service produced", and the Pipe cannot import this package — it is pasted Python in another container. So the service serves the document, and it is asserted through the `TestClient` seam that already exists. |
| `payloads.submission_body` | existing | Gains `tutor_line_html`. `Submission.model_grading.tutor_line` exists and is tested in the domain, but has no field on the wire — so criterion "the Advanced tutor line is already in the response" is unmeetable until it does. |

Deliberately **not** seams:

- **The DOM view** (`createDomView`, `mount`). Thin by construction, in the same
  sense the Pipe is: it moves a control into a placeholder and sets
  `innerHTML` from strings the service already sanitised. Every decision it
  could make has been taken out of it and put in the state machine, which is
  where the tests are.
- **The stylesheet.** No behaviour. Its one load-bearing property — that it
  fetches nothing — is asserted on the document, not on the CSS.

## Decisions

| # | Decision | Source |
|---|---|---|
| D15 | The domain runs as its own service; `fetch` is the only answer path; requests carry a rotating capability token; the prototype targets a default install. | [ADR-0015](../adr/0015-iframe-pipe-transport.md) |
| D13 | The reactive tutor line is Advanced-only and rides the grading response. Nothing streams. | [ADR-0013](../adr/0013-reactive-tutor-line.md) |
| D12 | Restricted Markdown, MathML server-side, everything sanitised, no CDN and no font fetch. | [ADR-0012](../adr/0012-iframe-content-rendering.md) |
| D11 | Authoring splits into skeleton plus pedagogy payload, and **the client guards** the window before the payload lands. | [ADR-0011](../adr/0011-latency-budget.md) |
| D9 | The probe is graded substantively, `probe_failure_behavior` per mode, and a dismissal is a persisted probe with a null self-explanation. | [ADR-0009](../adr/0009-self-explanation-probe.md) |
| D4 | The answer key never leaves the backend; the wire format is a whitelist. | [ADR-0003](../adr/0003-grading-authority-and-key-custody.md), [ADR-0004](../adr/0004-quiz-wire-format.md) |
| — | **The render hint is the only thing that chooses a control.** An `if mode ==` outside the registry is a bug, and a mode check in the view is that bug moved across a network boundary. | CONTEXT: ModeRegistry, ModePolicy; `quiz-service.md` |
| — | A rating is a separate record outside the LLM conversation entirely. | CONTEXT: RatingRecord; acceptance 24 |
| — | `markdown.highlight_css()` is "for inlining into the iframe" and `content.BLANK_CLASS` is "the hook the iframe binds an answer control to". Both were written for this ticket to consume. | `rendering/markdown.py`, `rendering/content.py` |

## Approach

### 1. `tutor_line_html` on the wire

One field on `payloads.submission_body`, rendered through `html_of` like every
other string that reaches a browser. Null on the deterministic path, because
`Submission.model_grading` is `None` there — Novice has no route to a tutor line
rather than a field that happens always to be null (D13).

### 2. `socratic.ui`, and the overlay document

A new package **outside** `socratic.domain`, for the same reason
`socratic.service` is: it imports `socratic.rendering`, and the domain is
stdlib-only. It imports no third-party package directly and declares no extra of
its own.

`render_overlay(body, *, service_base_url)` builds one self-contained document:

- `<meta charset="utf-8">`, because rendered MathML is non-ASCII
  ([#53](https://github.com/derkmed/socratic/issues/53)) and a `srcdoc` document
  that does not say so renders every formula as mojibake.
- An inline `<style>` carrying `markdown.highlight_css()` and the overlay's own
  rules. No `<link>`, no `@import`, no `url(`, no font — asserted, not intended.
- The explanation, `body["explanation_html"]` verbatim. It is already walked,
  already sanitised, and re-sanitising it here would be the second pass
  `content.render_explanation` already ran.
- One **control block** per entry in `body["blanks"]`, in declared order, each
  carrying `data-blank-id`. The block's contents come from `render_hint`
  through a lookup table: `option_bank` renders one button per option with the
  option's `text_html` inside it, `text_input` renders a text field and a
  submit button. A hint the table does not know is a `ValueError` — an
  unrenderable blank is a wire-format change, not something to skip silently.
- The verdict panel, the probe prompt and the rating prompt, all present and
  hidden, so the client only ever toggles visibility and never builds markup
  from model-authored data.
- The session id, the current capability token and `service_base_url` as data
  attributes on the root element, HTML-escaped. The token is the one credential
  the document holds; the HMAC secret is not in this process's output at all.
- `client.js` inlined, then one call to `SocraticQuiz.mount(document)`.

**Why the controls are a panel rather than markup inside the placeholder span.**
An option's `text_html` is block-level — `render_markdown("entropy")` is
`<p>entropy</p>` — and a block box inside the inline placeholder splits the very
line it was supposed to sit in. So the placeholder keeps its inline gap and the
control sits in a block panel under the explanation, one blank at a time. The
blank is still drawn inline where the walk put it, which is what the criterion
asks; the control is under it rather than in it. When a blank resolves the
client fills its gap, so a finished quiz reads as a complete sentence rather
than as a page of holes.

The same block-level wrapping is why the overlay's stylesheet flows the
explanation's direct-child paragraphs inline: `render_markdown` wraps every text
segment in its own `<p>`, so left alone each blank would read as a paragraph
break rather than as a gap in a sentence. The cost is that a paragraph break
*inside* one text segment flattens too — a presentational workaround for
something better fixed in the content renderer, filed separately.

`render_direct_answer(body)` renders the override branch (ADR-0004) as the same
chrome with the prose and no client script: there is no session, no token and
nothing to submit.

### 3. `client.js` — the state machine and its two ports

One classic script assigning a single global, so the browser gets
`window.SocraticQuiz` from an inline `<script>` and a child Node interpreter can
evaluate the same bytes and pull the factory out. Nothing runs at load time;
`mount` is called explicitly by the document.

`createQuizClient({sessionId, token, transport, view})` holds exactly one piece
of mutable state — the current token — and exposes:

- `start()`, `submitAnswer(blankId, submitted)`, `answerProbe(blankId, text)`,
  `dismissProbe(blankId)`, `submitRating(score)`, `dismissRating()`.
- **The walk over the blanks.** The client is given the blank ids in declared
  order — the order the segment walk drew them, and the order the server's
  cadence called the last one final — and activates one at a time. It re-scans
  on every resolution rather than holding a cursor, because a failed probe can
  re-open a blank behind the one the learner is on: `resolved` is not a terminal
  state (CONTEXT: Sealed).
- Every request carries `quiz_session_id` and the current token. Every response
  that carries `capability_token` replaces it; one that does not — the rating —
  leaves it alone, rather than swapping in `undefined` and locking the learner
  out of their own quiz.
- A **correct** verdict calls `view.celebrate(...)`; a wrong one calls
  `view.showHint(...)` with the rung and the revealed option id when the ladder
  is spent. The Advanced tutor line is passed straight from the same response —
  there is no second call, and the client makes exactly one request per
  submission.
- `probe` non-null on a response shows the prompt; the learner answers it or
  dismisses it, and a dismissal is `self_explanation: null` to the same
  endpoint (`quiz-service.md`: "dismissal is a value here rather than a fifth
  route").
- `attempt_sealed` on any grading response shows the rating prompt.
  `dismissRating()` hides it and sends nothing. A score sends one request to
  `/ratings` and nothing else — no `postMessage`, which is the only mechanism by
  which anything in this document could enter the conversation (ADR-0015).
- **The late-pedagogy guard.** A null `feedback_html` is the window, not an
  error: the verdict, the rung and the reveal are all still acted on, and the
  view is told the pedagogy is pending instead of being handed a null to
  render. An empty `recap_html` at render time omits the recap section rather
  than rendering an empty one.

`createFetchTransport(fetchImpl)` is the only place `fetch` is named: `POST`,
`content-type: application/json`, `X-Socratic-Token`, absolute URL. **Absolute**
because a `srcdoc` document inherits its parent's base URL, so a relative path
would resolve against Open WebUI's origin instead of the service's.

`createDomView(document)` and `mount(document)` bind the state machine to the
document: they move each control into its placeholder, activate one blank at a
time in declared order, and toggle the panels.

### 4. `POST /overlays`, and the browser-facing URL

One route, service-token authorized like `/quizzes`, same request body, returning
`text/html; charset=utf-8`. It authors, mints, and renders — the two halves of
`/quizzes`' body factored into a helper both routes call, so neither grows
domain logic.

`/quizzes` is left exactly as it is. Folding the document into its JSON body
would have been smaller, but the document quotes the word `correct` in its
verdict handling and `test_the_authored_quiz_names_no_correct_option` scans the
whole author body for it — a key-custody guard that should not be weakened to
make room for a UI.

The URL the browser must use is **the service's own configuration**, not a
request field: `SOCRATIC_PUBLIC_URL`, on `ServiceConfig` and
`ServiceDependencies`, defaulting to `http://localhost:8080` — the port
`compose.yaml` already publishes. A caller-supplied URL rendered into the
document would let whoever holds the service token point every learner's answers
at another origin.

### 5. Testing the client

`tests/test_ui_client.py` runs each case in a child Node interpreter, one
process per case, reading the shipped `client.js` and driving it over a
recording `fetch` and a recording view. The module skips wholesale when `node`
is not on `PATH`, exactly as the adapter's tests skip without the `anthropic`
extra and the service's without `fastapi`. Node is a test-time tool only: no
`package.json`, no dependency, nothing installed, and nothing in the image.

## Out of scope

- **The Open WebUI Pipe** ([#12](https://github.com/derkmed/socratic/issues/12)).
  This ticket makes the document the Pipe will fetch and return; it does not
  write the Pipe, and nothing here imports or knows about Open WebUI.
- **Learner settings** ([#14](https://github.com/derkmed/socratic/issues/14)) and
  **queued topics / start-this-instead**
  ([#15](https://github.com/derkmed/socratic/issues/15)). `queued_topics` is
  listed in the document because it already crosses the wire; nothing acts on it.
- **Firing `author_pedagogy` off the render path.** Still nobody's: the client
  guard is what this ticket owns, not the concurrency that opens the window.
- **The DOM view's own tests.** Deliberately, per the seam table. A browser
  harness is a toolchain this prototype does not have and does not need to
  assert anything the state machine does not already assert.
- **Resolving a blank whose answer is a formula** (acceptance 32). `Submission`
  has no field for the resolved segment's MathML, so there is nothing for a
  client to render; adding one is the submit path's, not the view's.
- **Advanced hint text** ([#56](https://github.com/derkmed/socratic/issues/56)).
  An Advanced wrong answer has no rung text today, and the client's
  late-pedagogy guard renders that absence the same way it renders a late
  payload — it does not invent a hint.
- **Accessibility beyond the basics.** Labels, focus order and `aria-live` on
  the verdict panel are in; a blank nested inside a formula is out by
  ADR-0012, and there is no ARIA pattern for an unanswered blank to adopt.
- **`localStorage`, resume, and offline play.** The token expires in minutes and
  the iframe has an opaque origin; a learner who returns hours later fails
  closed, which is D15's intent.

## Acceptance

1. The overlay places `explanation_html` inline, blanks in declared order, and a
   formula's MathML survives into the document unaltered.
2. Each blank gets exactly one control block, keyed by `data-blank-id`, and the
   control is chosen by `render_hint`: buttons carrying each option's
   `text_html` for `option_bank`, a text field for `text_input`.
3. No module under `src/socratic/ui/` branches on a difficulty mode — the
   existing scan covers the Python, and a new one covers `client.js`, which the
   Python scan cannot see.
4. The document declares `charset=utf-8`, inlines the highlight CSS, and
   contains no `<link>`, no `@import`, no `url(` and no external `src`.
5. The document carries the session id, the capability token and the absolute
   service URL, and carries no correct option id, rubric, hint or reinforcement.
6. An empty `recap_html` renders no recap section; a document is still produced.
7. `submitAnswer` issues exactly one `POST` to `<base>/answers`, with
   `X-Socratic-Token` set to the current token and `quiz_session_id` in the body.
8. A grading response's `capability_token` replaces the previous one, and the
   next request carries the new one and never the old.
9. A `/ratings` response carries no token and the client's token is unchanged
   after it.
10. A correct verdict calls `view.celebrate`; an Advanced response's
    `tutor_line_html` reaches the view from that same response, and exactly one
    request was made.
11. A wrong answer calls `view.showHint` with the rung, and with the revealed
    option id when the response carries one.
12. A response with `feedback_html: null` resolves without throwing, still
    reports the verdict and the rung, and tells the view the pedagogy is
    pending. No view call receives the string `"null"` or `"undefined"`.
13. A response carrying a `probe` shows the prompt; answering posts the text to
    `/probes`; dismissing posts `self_explanation: null` and makes no other
    request.
14. `attempt_sealed` shows the rating prompt. `dismissRating()` sends nothing.
    Submitting a score sends exactly one request, to `/ratings`.
15. `client.js` contains no `postMessage`, no `EventSource`, no `WebSocket` and
    no read of a response body stream — nothing streams and nothing reaches the
    conversation.
16. `POST /overlays` returns `text/html; charset=utf-8` containing the authored
    quiz and a token that verifies for the session; without the service token it
    is `401` before any model call; a `direct_answer` inquiry returns the prose
    document and no token.
17. `submission_body` carries `tutor_line_html`, non-null only on the
    model-graded path.
18. The learner walks the blanks in declared order: the first is active on
    mount, resolving one activates the next, a wrong answer activates nothing,
    and the last one resolving reports the end rather than activating a blank
    that is not there.
19. A re-opened blank becomes active again, behind the one the learner had
    moved on to.
20. A resolved blank is told what to put in its gap — what the learner got
    right, or the revealed option when the ladder was spent.
