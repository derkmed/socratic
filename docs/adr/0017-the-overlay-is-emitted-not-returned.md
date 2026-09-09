# 0017. The overlay is emitted as an `embeds` event, not returned

Date: 2026-09-09
Status: accepted
Amends: [ADR-0015](0015-iframe-pipe-transport.md) (the Pipe returns the rendered
HTML — it cannot; it emits it)
Resolves: [#116](https://github.com/derkmed/socratic/issues/116)
Verified by: a spike Pipe run against the real install, and
[`docs/research/open-webui-overlay-delivery.md`](../research/open-webui-overlay-delivery.md),
which reads the mechanism out of the installed 0.11.3 build's own source

## Context

ADR-0015 decided that the Pipe "maps `__user__` to a `LearnerId`, calls the
service, and returns the rendered HTML the service produced". The last clause is
not a thing a Pipe can do.

Open WebUI's Pipe path dispatches a return value on `str`, `dict`, `BaseModel`,
`StreamingResponse`, `Iterator` and `AsyncGenerator` (`functions.py`, 0.11.3).
An `HTMLResponse` matches none of them. It falls through every branch and the
stream emits only its finish chunk, so the learner gets an **empty assistant
message** — no error in the chat, no traceback in either container's log,
nothing to read at all. The overlay had never once rendered; every earlier
failure on this path stopped short of a 200 and hid it.

The `HTMLResponse` → sandboxed-iframe rendering the docs describe is real, but
it belongs to **Tools and Actions**: `middleware.py` tests
`isinstance(tool_result, HTMLResponse)`, on the tool result path.
`docs/research/open-webui-fit.md` recorded this correctly, scoped to
"Tool/Action", and credited Pipes only with importing libraries and making their
own API calls. ADR-0015 generalised that row to a Pipe. The research doc had
even named the shape of the risk — the capabilities were "each documented, just
not documented *composed*" — but the composition it went on to settle by
measurement was iframe-`fetch`, not iframe-render.

## Decision

**The Pipe emits the overlay on `__event_emitter__` as an `embeds` event.**

    await __event_emitter__(
        {"type": "embeds", "data": {"embeds": [html], "replace": True}}
    )

`socket/main.py` persists the payload to the message, and the frontend renders
each entry into a `FullHeightIframe` as `srcdoc` — verbatim, sandboxed,
`allow-same-origin` off, exactly the frame ADR-0015 assumes. `replace` is set
because the host otherwise extends a message's existing embeds, and a re-run
would stack a second quiz under the first.

**The return value becomes the empty string.** It is no longer the payload, but
it still has to be a type the Pipe path understands, and it carries no overlay
text: reprinting the quiz as chat text is what ADR-0001 rules out.

**Every document reports its own height.** `FullHeightIframe` reads
`contentDocument.scrollHeight` first, which throws on an opaque origin, and
falls back to a `postMessage` from inside. A document that sends nothing is
given no height and renders as an empty strip. The measure is over the body's
*children*: `documentElement` and `body` report the height the parent just set,
so reporting either feeds our own output back in and any body padding is re-added
on every pass — observed ratcheting without bound before it was fixed.

**Everything else in ADR-0015 stands.** The service is still a separate process,
`fetch` is still the only answer path, and the capability token is still the
whole authorization story. This changes how one document reaches one frame.

## Consequences

**The seam did not move.** A Tool or an Action would have reached the same
`embeds` event by a longer road — and would have changed the trigger from "the
learner asks a question" to a model tool-call decision or a button press, which
is a product change, not a delivery one. Pipe, Tool and Action all receive
`__user__` and own both `Valves` and `UserValves`, so nothing was gained there
either.

**`__event_emitter__` is now a parameter of `pipe()`, and that is not a crack in
ADR-0015's `__event_call__` prohibition.** The two are different mechanisms.
`__event_call__` blocks the Pipe coroutine on a modal rendered in the *parent*
page, which the sandboxed iframe cannot invoke — a different architecture, as
ADR-0015 says. The emitter is a one-way sink. The prohibition stands as written
and the test that guards it now says which of the two it is about.

**A host that hands us no emitter is told so.** There is no way to show a quiz
without one, so the Pipe refuses before authoring rather than spending a model
call on something with nowhere to go — and says why, rather than reproducing the
silence that made this bug expensive to find.

**~31 KB of HTML crosses a Socket.IO event per turn.** Measured working, with no
chunking. `FullHeightIframe` also accepts a URL as its `src` and uses it as a
real iframe `src` rather than `srcdoc`, which would sidestep the payload
entirely; that is a different security posture from an opaque-origin `srcdoc`
and is not taken here.

**This is still a default-install decision.** Under the hardening docs'
`IFRAME_CSP` the frame has no network path, exactly as ADR-0012 and ADR-0015
already record.
