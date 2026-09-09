# 0015. The iframe↔backend contract: a separate service, fetch only, capability tokens

Date: 2026-09-08
Status: accepted
Resolves: the `fetch`-vs-`__event_call__` question carried since
[ADR-0002](0002-open-webui-host-with-portability-seam.md)
Amends: [ADR-0003](0003-grading-authority-and-key-custody.md) (key custody is a
different process than stated), [ADR-0012](0012-iframe-content-rendering.md)
(hardened deployments leave the prototype)
Verified by: the iframe-`fetch` spike in
[`docs/research/open-webui-fit.md`](../research/open-webui-fit.md) — the `fetch`
path this ADR makes the *only* answer path was measured in Chrome and Edge rather
than reasoned from the source, and its CORS consequences (`*`, no credentials,
preflight answered) are held by `tests/test_service.py::TestCors`

## Context

Every prior ADR says "iframe `fetch` to our API" without saying what serves it,
how the request is authorized, or what happens when `fetch` is unavailable. Reading
the Open WebUI source (v0.11.3) settled the facts.

**`__event_call__` is not a fallback for the same design; it is a different
architecture.** It is a Socket.IO call from the *server*, awaited in the Pipe
coroutine, that renders a modal in the parent page. The iframe cannot invoke it —
there is no client-side API that reaches it. Under the hardening docs'
`IFRAME_CSP` (`connect-src 'none'`) there is no `fetch`, no XHR, no WebSocket and
no EventSource to any origin including the backend. The only sanctioned channel
from a rich-UI iframe is a `postMessage` prompt-submission bridge that creates a
**new chat turn**, requires a confirmation click, and reassigns `srcdoc` — a full
iframe reload that destroys in-frame state.

**`IFRAME_CSP` is genuinely unset by default**, so `fetch` works on a default
install. But the iframe is sandboxed **without `allow-same-origin`**, so it has an
opaque origin and carries no cookies and no `localStorage` token. It cannot
authenticate as the learner by any ambient means, and Open WebUI's CORS default is
`*`. The moment answers arrive by `fetch` there is a network boundary, and it had
no authorization story at all.

## Decision

**The domain package runs as its own service.** A separate process serving its own
HTTP API, a sibling container in compose. The Pipe becomes a thin client: it maps
`__user__` to a `LearnerId`, calls the service, and returns the rendered HTML the
service produced.

This makes the ADR-0002 seam **physical rather than disciplinary**. That ADR named
"an Open WebUI import creeping into the domain package" as the failure mode to
watch and admitted "nothing enforces it but review"; across a process boundary such
an import would not resolve. It also confines ADR-0002's other named cost — Python
pasted into an admin panel with no hot reload — to the thin adapter, instead of to
the surface we iterate on hardest.

**`fetch` is the only answer path. The prototype targets a default Open WebUI
install.** Hardened deployments are a documented constraint, not a supported
configuration. Supporting both would have meant rewriting ADR-0003, ADR-0011 and
ADR-0012 to promise only what the structurally weaker path can deliver — no
in-place update, a confirmation click per answer, and a chat turn per interaction.

**Requests are authorized by a capability token.** HMAC-signed, scoped to one
`QuizSessionId` and its `LearnerId`, short TTL, minted into the `srcdoc` at render
time and sent with every request. **Each grading response returns a fresh token**
which the iframe swaps in — sliding renewal, riding a round trip that already
carries the verdict, the probe question and the reactive line.

The `QuizSessionId` is **not** a credential. It is timestamp-prefixed, near
monotonic, and by [ADR-0007](0007-quiz-session-identity.md) it is a plain key
stamped through the audit trail; treating it as a bearer token would retroactively
poison every place it is printed.

## Consequences

The residual risk ADR-0002 carried from the beginning is closed, and closed in the
favourable direction: the intended overlay UX is the one that ships.

ADR-0003's key-custody claim needs restating. "The key never leaves the Pipe
process" was already the wrong boundary and is now plainly so — the key never
leaves **the backend**, and is never serialized into the iframe. The substance is
unchanged: display fields go to the browser, grading is a round trip.

The exposure window is minutes rather than the life of a quiz, and the token is
scoped to a single session, so a leaked one grades one learner's quiz until it
expires. A learner who abandons a quiz and returns hours later cannot resume — it
fails closed and quietly, which fits ADR-0005's "we never guess that a learner
left" rather than fighting it.

The costs are two processes to run and Docker networking between them, and a
prototype that will not work on a hardened install — an outcome to state plainly in
the README rather than discover in a demo. In-memory repositories (ADR-0005) now
live in the service, so its restart is what loses state, not the host's.
