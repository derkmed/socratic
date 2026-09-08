# 0012. Render Markdown and math ourselves, math as server-side MathML

Date: 2026-09-08
Status: accepted
Resolves: the open CSP question carried since
[ADR-0002](0002-open-webui-host-with-portability-seam.md)
Amended by: [ADR-0015](0015-iframe-pipe-transport.md) (hardened deployments leave
the prototype; `fetch` is the only answer path)

## Context

Open WebUI renders Markdown and LaTeX natively in chat — `marked` v9 plus KaTeX
0.16, recognising `$...$`, `$$...$$`, `\(...\)` and `\[...\]`, enabled by default
with no setting. **None of it reaches us.** That pipeline is `Markdown.svelte`, the
chat-message path; our quiz is delivered through `FullHeightIframe.svelte`, a
`srcdoc` sandboxed iframe. Nothing processes the HTML we return.

Researching this also answered the question we had been carrying since ADR-0002.
**`IFRAME_CSP` is an env var, unset by default**, so no CSP is injected and both CDN
scripts and `fetch` work out of the box — Open WebUI's own Rich UI example loads
Chart.js from jsdelivr. But the *hardening* docs recommend
`default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; connect-src 'none'`,
which blocks CDN scripts, web fonts **and** `fetch`. `IFRAME_CSP` is injected as the
first `<meta>` tag, overriding any CSP we ship, and `font-src` is never discussed in
the documentation at all.

So the iframe's capabilities are deployment-dependent and not ours to control. A
hardened deployment is not an edge case; it is what Open WebUI's own docs recommend.

## Decision

**Math is converted to MathML server-side, in the Pipe.** Browsers render MathML
natively: no client JS, no font files, no CDN request. It is the only option that
works under every CSP, including `default-src 'self'`.

**Blanks mask whole formulas only.** (Originally: blanks may sit inside formulas.
**Deferred past the prototype.** Nesting is more tractable than it looks — MathML
Core §2.2.1's own example is a fill-in-the-blank inside a radical, `mtext` is an
HTML text integration point, and a submit `button` there needs no client JS — but
there is *no* ARIA pattern for an unanswered blank, MathML-AAM maps almost nothing
below `math`, and no production system does it. The accessibility story, not the
layout, is what defers it.) When such a blank is filled, the Pipe returns
the re-rendered formula **in the grading response it was already sending**. Every
fill already round-trips to the Pipe, because ADR-0003 keeps the answer key
server-side, so this adds no request and no perceptible latency.

**Markdown is a restricted subset**: inline code, fenced code blocks with
highlighting, bold, italic, lists, links. It covers what a 250-word technical
explanation needs, keeps the iframe renderer small, and narrows what must be
sanitised.

**Everything rendered passes through sanitisation**, model-authored or not. A
prompt-injected explanation is a plausible route to HTML in the page.

**`__event_call__` is the hardened-deployment path, not a hypothetical fallback.**
Under the recommended `IFRAME_CSP`, `connect-src 'none'` blocks the iframe's `fetch`,
so the client must degrade to it. Both paths must work.

**Reversed by [ADR-0015](0015-iframe-pipe-transport.md).** The iframe cannot invoke
`__event_call__` at all — it is a server-side Socket.IO call rendering a
parent-page modal. It is a different architecture, not a degraded path. The
prototype targets a default install, where `IFRAME_CSP` is unset and `fetch`
works.

## Consequences

The quiz renders identically on a default install and a hardened one — no silent
blank formulas, no CDN dependency, nothing to configure. Payload is zero bytes
beyond the content itself, against roughly 500-600KB per render for inlined KaTeX
with fonts, which `srcdoc`'s opaque origin cannot even browser-cache.

The costs are real. Formula rendering is **server-owned**: the iframe cannot
re-render on its own, so a filled formula depends on the Pipe's response, coupling
presentation to the grading path. MathML typography is good but not KaTeX-beautiful,
and coverage of exotic LaTeX depends on the converter. And supporting both `fetch`
and `__event_call__` means two answer-delivery paths to build and test rather than
one.

None of this affects token cost. The rendering payload is browser-side only and
never reaches the model.
