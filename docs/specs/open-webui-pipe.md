# The Open WebUI Pipe — the adapter, and nothing else

## Goal

The **Pipe** (CONTEXT): Open WebUI's adapter to the **quiz service**, and the
last piece of the prototype. A learner asks a question in Open WebUI; the Pipe
maps `__user__` to a **LearnerId**, calls the service's `POST /overlays` server
to server, and returns the **overlay** the service rendered as an
`HTMLResponse` with `Content-Disposition: inline`, which Open WebUI displays in
its sandboxed `srcdoc` iframe (master spec §12; D3, D15;
[ADR-0015](../adr/0015-iframe-pipe-transport.md); acceptance 1, 34, 38).

It **holds no domain logic and makes no model calls**. Thin by construction —
that is why it comes last and why the master spec lists it as deliberately not
a seam. Everything it could plausibly do has already been done by the time it
runs: authoring, rendering, sanitising, key custody and token minting all live
on the other side of the process boundary.

The **portability seam** (CONTEXT) is a process boundary, and the Pipe is on
the far side of it. It cannot import `socratic` — it is pasted Python running
in the Open WebUI container, which does not have this package installed. It is
therefore a **single self-contained file**, and that constraint is what shapes
everything below.

## Seams

The master spec settles this: the Pipe is **deliberately not a seam** — "thin
by construction, holds no domain logic, exercised by hand". Nothing here
contradicts that. What follows is the minimum needed for the file to be
testable at all in a repository whose suite has no Open WebUI and no service
running.

| Seam | Kind | Why it must exist |
|---|---|---|
| `create_app(deps)` → `POST /overlays` | existing | The route the Pipe calls, added by [#13](https://github.com/derkmed/socratic/issues/13) precisely so ADR-0015's "the Pipe returns the rendered HTML the service produced" could be true without the Pipe importing `socratic.ui`. The Pipe adds no route and changes none. |
| `Pipe.pipe(body, __user__, __metadata__)` with an injected `post` | **new**, small | One constructor argument, defaulting to `None` → a real `httpx.AsyncClient`. Open WebUI constructs `Pipe()` with no arguments, so the injection point is invisible in production and is the only thing that makes the adapter assertable without a live service. Without it, "maps `__user__` to a `LearnerId`" and "makes no model calls" are claims nobody can check until a demo. |
| A structural scan of the Pipe source | **new**, follows `tests/test_import_hygiene.py` | Two of the acceptance criteria are *absence* claims — no domain logic, no `__event_call__` answer path — and absence is only checkable by scanning. The repo already enforces its seam this way; this is the same test, pointed at the file on the other side of the boundary. |

The pure helpers inside the file (`_learner_id`, `_inquiry`) are tested
directly. They are not seams in the architectural sense; they are the two
translations the Pipe exists to perform, and they are pure functions over
dictionaries.

## Decisions

Every one of these is settled upstream. This spec records them; it decides
nothing.

- **D15 / [ADR-0015](../adr/0015-iframe-pipe-transport.md)** — the domain runs
  as its own service; the Pipe is a thin client that maps `__user__` to a
  `LearnerId`, calls the service, and returns the rendered HTML the service
  produced. `fetch` is the only answer path.
- **D3 / [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md)** —
  `__user__` maps to our `LearnerId` at the adapter, which then never mentions
  the host again. `LearnerId` is `__user__["id"]` (CONTEXT: LearnerId).
- **No `__event_call__` answer path** (ADR-0015, master spec Out of scope). It
  is a server-side Socket.IO call rendering a parent-page modal; the iframe
  cannot invoke it. A different architecture, not a degraded one. The Pipe must
  not contain one, and the scan enforces that.
- **Default install only** (CONTEXT: Default install; master spec Out of
  scope). `IFRAME_CSP` unset. Under the hardening docs' recommended CSP the
  iframe has no network path to the service at all. A README constraint, not a
  configuration to support.
- **The overlay is rendered by the service, never by the Pipe** (ADR-0015;
  `docs/specs/quiz-ui.md`). The Pipe returns bytes it did not author and does
  not inspect.
- **Two credentials, not one** (`docs/specs/quiz-service.md`). The Pipe holds
  the *service* token, on `X-Socratic-Service-Token`; the learner's capability
  token is minted into the overlay by the service and never passes through the
  Pipe.

### The composition was measured before it was built

The master spec's §12 said the remaining unknown was "the composition — this
Pipe, inside a real default install, reaching the real quiz service". The
transport half was already measured ([#77](https://github.com/derkmed/socratic/issues/77),
`docs/research/open-webui-fit.md`). This ticket measured the rest that is
measurable without a live Open WebUI container:

**This Pipe** called the **real service** over real HTTP with its own service
token, and returned the **real overlay document** — the whole chain in this
spec, only with a stubbed model client where the API key would be. That
document was then embedded in a `srcdoc` iframe with `sandbox="allow-scripts
allow-downloads"` and no `allow-same-origin` — the sandbox Open WebUI applies —
inside an HTTP-served parent page, and a click on a real option button went
through the real `client.js`. Run in **headless Chrome 152 and headless Edge
152**; identical in both.

The Pipe returned an `HTMLResponse`, `Content-Disposition: inline`,
`text/html; charset=utf-8`. Then, from inside the iframe:

| The service saw | Chrome | Edge |
|---|---|---|
| `OPTIONS /answers`, `Origin: null`, `Access-Control-Request-Headers: content-type,x-socratic-token` → `200`, `Access-Control-Allow-Origin: *`, no `-Allow-Credentials` | yes | yes |
| `POST /answers`, `Origin: null`, carrying `X-Socratic-Token` → `200` | yes | yes |

So the `fetch` reaches the quiz service and returns a verdict. What is still
unobserved is only the outermost ring: Open WebUI itself as the parent page,
which needs a container runtime and an API key. See "Out of scope".

## Approach

One new file, `pipe/socratic_pipe.py`, plus its tests and a README. Built in
this order.

1. **`_learner_id(__user__)` → `str`.** `__user__["id"]`, required and
   non-empty. This is the whole of the portability seam's one crossing.
   Refuses rather than inventing an anonymous learner: everything is
   partitioned on `LearnerId`, so a missing one is a bug in the host contract,
   not a case to paper over.
2. **`_inquiry(body)` → `str`.** The last `user` message in `body["messages"]`.
   Open WebUI sends `content` either as a string or as a list of typed parts;
   both are handled, the text parts joined. An empty inquiry is refused — the
   service's `AuthorRequest` requires `min_length=1`, and a 422 from a round
   trip is a worse error message than a local one.
3. **`Pipe.pipe(...)`.** Compose the two, `POST` to
   `{service_url}/overlays` with `X-Socratic-Service-Token`, and return
   `HTMLResponse(content=<the service's body>, media_type="text/html;
   charset=utf-8", headers={"Content-Disposition": "inline"})`.
4. **`Valves`.** Admin-level and persisted, defaulted from the environment
   variables `compose.yaml` already sets on the `open-webui` container:
   `SOCRATIC_SERVICE_URL`, `SOCRATIC_SERVICE_TOKEN`. Plus a request timeout and
   the difficulty mode to author in.
5. **The failure paths.** A refusal, a timeout, or any non-200 from the service
   returns a short plain string, which Open WebUI renders as chat text. Never a
   traceback, and never the service token — the Pipe holds a credential, and a
   Pipe that prints its own exception into a chat window prints its Valves.
6. **The structural scan** (`tests/test_pipe.py`), and the **README**.

### What the Pipe sends

`POST /overlays` takes `learner_id`, `inquiry`, `mode` and an optional
`probe_cadence` (`socratic/service/payloads.py::AuthorRequest`, `extra=forbid`).
The Pipe sends the first three and omits `probe_cadence`, so the domain's own
default stays the single place it is written down — the same reason
`app.py::_cadence` omits it.

`mode` has no default in the domain and the request requires one, so it is an
admin `Valve`, defaulting to `novice`. **Per-learner** mode and cadence are
[#14](https://github.com/derkmed/socratic/issues/14)'s `UserValves`, which this
ticket does not touch.

## Out of scope

- **`UserValves`** — per-learner mode and probe cadence are
  [#14](https://github.com/derkmed/socratic/issues/14), in flight in parallel.
  This file defines admin `Valves` only, so the two do not collide: #14 adds the
  `UserValves` class and reads it where this spec reads the admin default.
- **Queued inquiries and "start this instead"** —
  [#15](https://github.com/derkmed/socratic/issues/15). A second inquiry
  mid-quiz is that ticket's, and it is domain behaviour, not adapter behaviour.
- **The `QuizAttempt` record.** Untouched. Both parallel tickets extend it;
  this one has no reason to.
- **An `__event_call__` answer path.** Ruled out by ADR-0015, and asserted
  absent rather than merely not written.
- **Hardened Open WebUI deployments** (`IFRAME_CSP` set). Documented in the
  README as a constraint with its reason. Not supported, not detected, not
  worked around.
- **Streaming.** Withdrawn with the parallel tutor call (D13). The Pipe returns
  one response.
- **Anything under `src/socratic/`.** The Pipe lives outside the package
  because it runs in the other container; adding it to the wheel would ship
  Open WebUI's adapter into the quiz service's image, which is the boundary
  ADR-0015 exists to make physical.
- **An end-to-end test against a live Open WebUI.** It needs a container
  runtime and an `ANTHROPIC_API_KEY`; neither is available to the suite, and a
  test that silently skips everywhere is worse than a documented manual step.
  The README carries the manual smoke test instead.

## Acceptance

1. `__user__["id"]` becomes the `learner_id` on the request to the service; a
   missing or empty one is refused before any request is made.
2. The last user message becomes the `inquiry`, for both the string and the
   typed-parts shapes of `content`.
3. The Pipe returns an `HTMLResponse` whose body is **byte-identical** to what
   the service returned, with `Content-Disposition: inline`.
4. The Pipe makes no model calls and holds no domain logic: its source imports
   nothing from `socratic`, `anthropic` or `open_webui`, and it names no
   grading, laddering, cadence or key vocabulary (acceptance 38).
5. The string `__event_call__` does not appear in the Pipe's source, and `pipe`
   does not accept it as a parameter.
6. A non-200 from the service, and a transport failure, each return a plain
   string that contains neither the service token nor a traceback.
7. The Pipe is one self-contained file, importable with only `pydantic`,
   `fastapi` and `httpx` — all of which the Open WebUI container already has.
8. The README states that hardened deployments (`IFRAME_CSP` set) are out of
   scope, and why (acceptance 1, 34 context).
