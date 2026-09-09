# Quiz service — an HTTP API over the domain, in its own process

## Goal

The **quiz service** (CONTEXT): the domain package running as its own process,
serving its own HTTP API, a sibling container to Open WebUI. It owns the answer
key, the repositories, the renderer and every model call. The **portability
seam** (CONTEXT) stops being a convention and becomes a process boundary — an
Open WebUI import below it would not resolve (D15,
[ADR-0015](../adr/0015-iframe-pipe-transport.md); umbrella spec §10; acceptance
34, 37).

Four endpoints: author a quiz, submit an answer, answer a probe, submit a
rating. (A fifth, `POST /settings`, was added by
[#14](https://github.com/derkmed/socratic/issues/14) —
`learner-settings.md` — on the same service credential and the same
"translation over a seam" rule. A sixth, `POST /displacements`, was added by
[#92](https://github.com/derkmed/socratic/issues/92) —
`inquiry-intake-over-http.md` — on a *capability* token, because it is a
learner's gesture about their own open attempt.) Every request is authorized before any work happens, and every *grading*
response carries a freshly rotated **capability token** (CONTEXT).

## Seams

The HTTP API is **deliberately not a seam**. It is a thin translation over seams
that already exist and are already tested; asserting them again through a live
socket would duplicate their coverage and buy nothing.

| Seam | Kind | Why it must exist |
|---|---|---|
| `QuizAuthoring.author(inquiry, learner_id, *, mode, …) -> Quiz \| DirectAnswer` | existing | The author endpoint's whole body. |
| `QuizSession.submit(...) -> Submission` | existing | The answer endpoint's whole body. |
| `QuizSession.answer_probe(...) -> ProbeAnswer` / `.dismiss_probe(...)` | existing | The probe endpoint's whole body. |
| `TokenMinter.verify(token, *, for_session=…)` / `.rotate(token)` | existing | Authorization and sliding renewal. |
| `AttemptRepository.get_by_session(learner_id, session_id)` | existing | Resolves the token's session to the attempt, so no attempt id ever crosses the wire. |
| `submit_rating(...) -> RatingRecord` | **new**, `socratic.domain.rating` | The one endpoint with no seam under it. `RatingRecord` and `RatingRepository` exist, but nothing composes them; without this the handler would hold domain logic, which acceptance forbids. Smallest possible: one function, one `Clock`. |
| `create_app(deps) -> ASGI app` | **new**, `socratic.service.app` | An app *factory* over an explicit dependency record, so tests drive the real routes in-process with a stub model client and no network, no env and no uvicorn. |

The service's own tests attach to `create_app` through the framework's test
client. That is one seam, not a per-route one, and it is what makes the
authorization rules assertable — they are properties of the wiring, not of any
domain object.

## Decisions

- **D15 / [ADR-0015](../adr/0015-iframe-pipe-transport.md)** — separate process,
  `fetch` only, capability tokens. The Pipe is a thin client that maps
  `__user__` to a `LearnerId` and calls this service.
- **CORS is load-bearing and was measured, not assumed.** A spike run for this
  ticket confirmed `fetch` reaches the service from a `srcdoc` iframe sandboxed
  without `allow-same-origin`, in Chrome and Edge, for a simple `GET`, a JSON
  `POST` and a `POST` carrying a custom token header. Requests arrive with
  `Origin: null`, and the two `POST`s are **preflighted**. So the service must
  answer `OPTIONS` and return `Access-Control-Allow-Origin: *`, and must **not**
  send `Access-Control-Allow-Credentials` — illegal beside `*`, and meaningless
  from an opaque origin. This is exactly why authorization is a bearer token in
  a header rather than anything ambient.
- **D3 / [ADR-0003](../adr/0003-grading-authority-and-key-custody.md)** — the
  answer key stays server-side. The wire format is built by *whitelisting*
  fields onto response models, never by serializing a `Quiz` or a `Blank`.
- **CONTEXT: Render hint** — a blank's payload carries `render_hint`, so the UI
  picks its input control from data rather than branching on mode
  (`tests/test_registry.py` forbids `if mode ==` outside the registry).
- **#53 / ADR-0012** — rendered MathML is non-ASCII. Every response declares
  `charset=utf-8`, and every string that reaches a browser leaves here already
  rendered and sanitised. The iframe renders no model-authored text itself.

## Approach

`src/socratic/service/`, a new package **outside** `socratic.domain` so it may
import third-party code (`tests/test_import_hygiene.py` keeps the domain
stdlib-only). A new `service` extra carries FastAPI and uvicorn, by the same
rule that put `anthropic` and `rendering` behind extras — base dependencies stay
`[]`.

| Module | Holds |
|---|---|
| `config.py` | `ServiceConfig.from_env()` — HMAC secret (≥32 bytes, never logged), service token, TTL, CORS origin. |
| `deps.py` | `ServiceDependencies` — the wired repositories, registry, minter, model client. Built once, injected into `create_app`. |
| `payloads.py` | Request and response models. The whitelist that keeps the key server-side. |
| `security.py` | `require_capability(...)` and `require_service_token(...)`. |
| `app.py` | `create_app(deps)` — routes, CORS, error mapping. |
| `__main__.py` | uvicorn entry point. Imported by nothing else. |

### Authorization

Two credentials, because the four endpoints have two different callers:

- **Author** is called by the **Pipe**, server to server, *before* a session
  exists — the session id and its token are created by this call, so a
  session-scoped token cannot authorize it. It takes a **service token**: a
  deployment-config shared secret compared with `hmac.compare_digest`.
- **Answer, probe and rating** are called by the **iframe** and take a
  capability token in `X-Socratic-Token`, verified with **`for_session=` always
  passed**. `TokenMinter.verify` takes `for_session` as an optional keyword; a
  handler that omits it accepts any live token for any session signed by the
  same secret. One helper does this verification for all three routes so there
  is a single place for it to be right, and a test asserts a token for session A
  is refused on session B for every one of them.

Verification happens in a dependency that runs **before** the handler body, so a
rejected request does no work. Rejection is `401` with a constant body; which
check failed is not the caller's business.

### Endpoints

All `POST`, all JSON, all `charset=utf-8`.

| Route | Auth | Body | Returns |
|---|---|---|---|
| `/quizzes` | service token | `learner_id`, `inquiry`, `mode`, `probe_cadence?` | `kind: "quiz"` with `quiz_session_id`, `capability_token`, `topic`, `explanation_html`, `recap`, `blanks[]`, `queued_topics`; or `kind: "direct_answer"` with `answer_html` and no token — a direct answer has no session to scope one to. |
| `/answers` | capability | `blank_id`, `submitted` | The `Submission` fields, rendered, plus a **rotated** `capability_token`. |
| `/probes` | capability | `blank_id`, `self_explanation` (nullable) | `ProbeAnswer` fields, rendered, plus a **rotated** token. A null `self_explanation` is a dismissal and routes to `dismiss_probe` — the probe is dismissible (§11) and the ticket names four endpoints, so dismissal is a value here rather than a fifth route. |
| `/ratings` | capability | `score` (1–5) | `{"ok": true}`. Not a grading response, so no rotation. |

The attempt is resolved from the token's own claims via `get_by_session`, so no
`attempt_id` or `learner_id` is ever accepted from the iframe. A learner cannot
name someone else's attempt because the request never gets to name one.

### What crosses the wire

`payloads.py` builds every response field by field. A blank becomes
`{blank_id, mode, render_hint, options: [{option_id, text_html}] | null}` —
`correct_option_id`, `rubric`, `hints`, `reinforcement` and `probe_question`
have no field to travel in. `revealed_option_id` on a submission is the one
sanctioned disclosure: the rung-three reveal, per blank, after the ladder is
spent (`Submission` docstring, ADR-0009). A test asserts that authoring a quiz
whose correct option is a known sentinel never puts that sentinel in the author
response body.

### Containers

`Dockerfile` (service, `python:3.10-slim`, installs `.[service,anthropic,rendering]`)
and `compose.yaml` bringing up Open WebUI and the quiz service on one network,
with the secrets as environment variables. Composed so that `docker compose up`
works unchanged on Mac, Windows and Linux — no host networking, no bind mounts
of build output.

## Acceptance

1. The service runs as its own process and serves all four endpoints.
2. Each of the three iframe endpoints rejects a missing, malformed, expired and
   **wrong-session** token with `401`, before any domain call happens.
3. Every grading response carries a token that differs from the one sent.
4. No response body contains a correct option id, rubric, hint or reinforcement,
   except the sanctioned rung-three `revealed_option_id`.
5. Preflight: `OPTIONS` on each route answers with `Access-Control-Allow-Origin: *`,
   the token header allowed, and no `Access-Control-Allow-Credentials`.
6. `docker compose config` validates and the service image builds.
7. No module under `socratic.domain` imports Open WebUI or the service package.
8. Handlers hold no domain logic: each route body is a translation over one seam.

## Out of scope

- **The quiz UI** (#13). No HTML page, no `srcdoc`, no client script. This
  ticket returns JSON with already-rendered, already-sanitised fragments; the
  segment-walking renderer that consumes them is #13's.
- **The Open WebUI Pipe** (#12). Nothing here imports or knows about Open WebUI.
- **Persistence.** In-memory repositories (ADR-0005). A restart loses attempts
  and, per #18, outstanding tokens — it fails closed.
- **Multiple workers.** Token supersession is in-process; one worker only.
- **Learner settings and queued inquiries** (#14, #15).
- **Authentication.** Inherited from Open WebUI. The capability token is not a
  login; the service token is not a user identity.
- **A real deployment.** Two local containers, secrets in environment variables.
