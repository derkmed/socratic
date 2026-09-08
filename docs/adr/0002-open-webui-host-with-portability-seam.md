# ADR-0002 — Host on Open WebUI behind a portability seam

Status: accepted
Date: 2026-09-08

## Context

The product's differentiator is an interactive quiz surface, not a chat window,
which argued for a purpose-built app. Research (`docs/research/open-webui-fit.md`)
showed Open WebUI can host it: rich HTML embeds for rendering, `__event_call__`
for blocking bidirectional input, `__user__` for authenticated identity, and
arbitrary Python libraries for our own Anthropic request path.

Adopting it donates authentication, user accounts, admin config, cross-platform
Docker packaging, and a conversation list — all real requirements we would
otherwise write. The stated priority is a fast prototype and demo. Long-term
swappability is desirable insurance, not a constraint.

## Decision

Build on Open WebUI, subject to one hard rule:

**All domain logic lives in a plain Python package with zero Open WebUI imports.**
Quiz schema, prompt assembly, grading, repositories, and the learner profile know
nothing about the host. The Pipe/Action is a thin adapter that maps `__user__` to
our own `LearnerId` and calls into that package.

The quiz UI talks to our own code, never to Open WebUI's chat pipeline. We do not
use `input:prompt:submit`; answers arrive via iframe `fetch` to our API if the CSP
permits it, otherwise via `__event_call__`.

## Consequences

Good:
- Auth, accounts, packaging, and identity (D5) are solved by adoption rather than
  invented.
- Swapping hosts later is an adapter rewrite plus an HTML shell, not a rewrite of
  the system.
- ADR-0001 holds under either click-back mechanism.

Costs / risks:
- Whether a rich-UI iframe may `fetch` our API is undocumented. If blocked, the
  UX degrades to `__event_call__` modals — clunkier than the intended overlay.
- Plugin authoring is Python pasted into an admin panel, not files with hot
  reload. The surface we will iterate on hardest lives in that loop.
- The seam is a discipline, not a mechanism; nothing enforces it but review. An
  Open WebUI import creeping into the domain package is the failure mode to watch.
