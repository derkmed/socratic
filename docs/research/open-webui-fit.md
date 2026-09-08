# Research — Open WebUI as host platform

Date: 2026-09-08. Question: can Open WebUI host a non-chat interactive quiz
surface backed by our own Anthropic request path (per ADR-0001)?

## Findings (all from docs.openwebui.com)

| Need | Mechanism | Status |
|---|---|---|
| Render a rich quiz overlay | Tool/Action returns `HTMLResponse` with `Content-Disposition: inline` → sandboxed iframe | Documented |
| Clean click-back, no chat pollution | `__event_call__` with `{"type": "input"}` / `{"type": "confirmation"}` — **blocks server-side until the user answers**, returns a dict | Documented |
| Click *inside* the rich overlay | iframe `fetch` to our own API | **Undocumented — residual risk** |
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

Not verified here (deferred, cheap to settle when building): whether Open WebUI's
CSP permits outbound `fetch` from a rich-UI iframe to a local origin.

## Sources

- https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/
- https://docs.openwebui.com/features/extensibility/plugin/functions/action/
- https://docs.openwebui.com/features/extensibility/plugin/functions/pipe/
