# Research — Open WebUI as host platform

Date: 2026-09-08. Question: can Open WebUI host a non-chat interactive quiz
surface backed by our own Anthropic request path (per ADR-0001)?

## Findings (all from docs.openwebui.com)

| Need | Mechanism | Status |
|---|---|---|
| Render a rich quiz overlay | Tool/Action returns `HTMLResponse` with `Content-Disposition: inline` → sandboxed iframe | Documented |
| Clean click-back, no chat pollution | `__event_call__` with `{"type": "input"}` / `{"type": "confirmation"}` — **blocks server-side until the user answers**, returns a dict | Documented |
| Click *inside* the rich overlay | iframe `fetch` to our own API | **Undocumented, but measured** — see the spike below |
| Chat-based click-back (fallback we reject) | `input:prompt:submit` postMessage — fills and submits a prompt into the chat | Documented |
| Authenticated user identity | `__user__` dict; `Users.get_user_by_id(__user__["id"])` | Documented |
| Own libraries / own API calls / own store | Pipes import external libs; `httpx.AsyncClient` recommended | Documented |
| Secrets & config | `Valves` (admin-level, persisted); `UserValves` per-user | Documented |

## Sandbox specifics

Rich UI iframes are sandboxed and **fully isolated from the parent page** by
default: no cookies, no `localStorage`, no parent DOM. `allow-scripts` and
`allow-downloads` are always on. `allowSameOrigin` and `allowForms` are opt-in
toggles. Without `allowSameOrigin` the iframe has an opaque origin, so any call
to our API arrives with `Origin: null` and cannot carry cookie auth.

## Assessment

The two capabilities we need are each documented, just not documented *composed*.
The worst case is therefore **ugly, not architecture-breaking**: if the iframe
`fetch` is blocked, render the quiz as rich HTML and collect answers via
`__event_call__` modals. ADR-0001 survives either way — no reprinting, no
chat-shaped turns.

## Spike — iframe `fetch` from an opaque origin

Date: 2026-09-08. The one thing above that the docs do not cover was deferred as
"cheap to settle when building", and has now been settled by measurement rather
than by reading: **outbound `fetch` from a sandboxed, opaque-origin frame to a
local service works, and it is preflighted.**

Setup: a `srcdoc` iframe with `sandbox="allow-scripts allow-downloads"` and **no**
`allow-same-origin` — the sandbox Open WebUI applies — inside an HTTP-served
parent, calling a stub service on a second port. Run in headless Chrome 152 and in
Edge; complete in both.

| Case | The client saw | The server saw |
|---|---|---|
| Simple `GET` | `200`, body parsed in-frame | `Origin: null` |
| `POST` `application/json` | `200`, body parsed in-frame | `OPTIONS` → `POST`, `Origin: null` |
| `POST` + `X-Socratic-Token` | `200`, token echoed back | preflight requesting `content-type,x-socratic-token`, then `POST` |

The frame reports `location.origin === "null"` and `isSecureContext === true`.

Two consequences are load-bearing rather than trivia:

1. **The requests are preflighted.** Both `POST`s trigger `OPTIONS`. A service that
   does not answer preflight fails, and the failure is invisible from the server
   side — nothing but the `OPTIONS` ever arrives.
2. **`Access-Control-Allow-Origin` must be `*`, and
   `Access-Control-Allow-Credentials` must be absent** — it is illegal beside `*`
   and meaningless from an opaque origin. This is the concrete reason the
   capability token is a header rather than anything ambient. The server's half is
   held by `tests/test_service.py::TestCors`.

**A trap, worth recording next to the result.** The same page fails completely
inside the Claude Code in-app browser pane: all three cases return `Failed to
fetch` with *zero* requests reaching the server — no preflight, and `no-cors`,
`<img>` and XHR are blocked too — while a non-sandboxed frame and an
`allow-same-origin` frame both succeed from that same page. That is an artifact of
the pane, not of Chrome. Anyone re-testing there will get a convincing false
negative.

What this spike does **not** show is the composition end to end: this Pipe, inside
a real default install, reaching the real quiz service. That stays an acceptance
criterion (master spec, "Transport and authorization" 34).

One correction to the Assessment above, for anyone reading it as current: the
fallback it names is not available. Reading the Open WebUI source later showed the
iframe cannot invoke `__event_call__` at all — it is a server-side Socket.IO call
that renders a parent-page modal — so `fetch` is not the preferred path of two but
the only one. See [ADR-0015](../adr/0015-iframe-pipe-transport.md).

## Sources

- https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/
- https://docs.openwebui.com/features/extensibility/plugin/functions/action/
- https://docs.openwebui.com/features/extensibility/plugin/functions/pipe/
