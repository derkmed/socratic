# Content rendering — sanitised Markdown and server-side MathML

## Goal

Turn a quiz's `explanation` — the flat `text | math | blank` segment array from
[ADR-0004](../adr/0004-quiz-wire-format.md) — into HTML the sandboxed iframe can
display on a default Open WebUI install: a restricted Markdown subset, math as
MathML converted **server-side**, and every content byte through a sanitiser
first. This is master spec §7 and issue #11, and it delivers acceptance 31, 32
and 33.

The security half is the load-bearing half. Explanations are model-authored, a
prompt-injected explanation is a plausible route to HTML in the page
([ADR-0012](../adr/0012-iframe-content-rendering.md)), and the iframe is
`srcdoc` with no CSP on a default install
([ADR-0015](../adr/0015-iframe-pipe-transport.md)) — so nothing downstream
catches what the renderer lets through.

## Seams

Rendering is not in the master spec's seam table, so all three seams here are
new. They are three because the acceptance conditions attach at three different
places and collapsing them would make two of the three unassertable.

| Seam | Kind | Why it must exist |
|---|---|---|
| `content.render_explanation(explanation) -> str` | pure function | The segment-array walk. Acceptance 31 (formulas render), 32 (a resolved blank's MathML is in the output) and "no sentinel substitution" are all properties of this one function's output over a `tuple[Segment, ...]`. One entry point, so there is one place that can forget to sanitise. |
| `sanitiser.sanitise(html) -> str` | pure function | Acceptance 33 and the attribute-level vectors (`javascript:` hrefs, `onerror=`, `annotation-xml` integration points) are properties of the **sanitiser alone**. They cannot be asserted through `render_explanation`, because the Markdown layer already escapes raw HTML — a test that smuggled `<script>` in through Markdown would prove the Markdown layer works and say nothing about the sanitiser. The sanitiser is the last line, so it gets tested as the last line. |
| `mathml.latex_to_mathml(latex) -> str` | pure function | The LaTeX→MathML boundary, which is **upstream of `MathSegment`**: `MathSegment.mathml` already holds MathML by its own docstring, so conversion happens where model-authored LaTeX becomes a segment, not where a segment becomes HTML. Acceptance 32 needs to call this directly — the grading path resolves a blank to a `MathSegment` and must have somewhere to build it. |

`markdown.render_markdown` is deliberately **not** a fourth public seam in the
acceptance sense; it is exercised through `render_explanation`, and the subset
is asserted there. It is a separate module only because the Markdown
configuration is bulky.

## Decisions

| # | Decision | Source |
|---|---|---|
| D12 | Restricted Markdown subset — inline code, fenced code blocks with highlighting, bold, italic, lists, links, **nothing else**; math converted to MathML server-side via `latex2mathml`; blanks mask whole formulas only; everything sanitised, model-authored or not. | [ADR-0012](../adr/0012-iframe-content-rendering.md) |
| D2 | The explanation is a flat segment array walked element by element, never a string with sentinels; a blank resolves by swapping one element for a `text` or `math` node (`domain.types.resolve_blank`). | [ADR-0004](../adr/0004-quiz-wire-format.md), `domain/types.py` |
| D15 | The prototype targets a **default** install, where `IFRAME_CSP` is unset. Nothing downstream strips what we emit. | [ADR-0015](../adr/0015-iframe-pipe-transport.md) |
| — | Third-party libraries live **outside** `socratic.domain`; the domain stays stdlib-only and installs with no third-party package at all. `socratic.adapters.anthropic_client` is the precedent, `tests/test_import_hygiene.py` is the enforcement. | ADR-0002, `tests/test_import_hygiene.py` |

Nested blanks (a blank inside a formula) are settled as **out of scope** by
ADR-0012 and by `domain/types.py`, which makes them structurally impossible —
no segment type has a field that can hold another segment. Nothing here
reopens that.

## Approach

A new package `src/socratic/rendering/`, outside the domain, in four modules
built in this order. Each is red-green-refactored before the next begins.

1. **`sanitiser.py`** — the allowlist and `sanitise(html)`. Built first because
   everything else composes onto it, and because it is the piece whose tests
   are the point of the ticket.
2. **`mathml.py`** — `latex_to_mathml(latex, display=...)`, `latex2mathml`
   converted and then sanitised.
3. **`markdown.py`** — `render_markdown(text)`, a `markdown-it-py` instance
   built from the `zero` preset with exactly six rules enabled, Pygments for
   fence highlighting, then sanitised. Plus `highlight_css()`, the Pygments
   style definitions, so the iframe can inline them instead of fetching a
   stylesheet.
4. **`content.py`** — `render_explanation(explanation)`, the walk: `TextSegment`
   to `render_markdown`, `MathSegment` to `sanitise`, `BlankSegment` to a
   constant placeholder element carrying the blank id.

`pyproject.toml` gains a **`rendering` optional extra**, not base dependencies.
This is forced, not chosen: `tests/test_import_hygiene.py::test_the_package_has_no_base_dependencies`
asserts `project.dependencies == []`, and that test is not ours to edit. It is
also right — the domain must install and test with no third-party package —
and it matches the `anthropic` extra exactly. Rendering tests therefore open
with `pytest.importorskip`, as `tests/test_anthropic_adapter.py` does.

### The Markdown subset, concretely

`markdown-it-py`'s `zero` preset enables nothing but paragraphs and text. Six
rules are enabled on top: `emphasis` (bold and italic), `backticks` (inline
code), `fence` (fenced code blocks), `list`, `link`, `escape`. `html` is off,
so raw HTML is escaped rather than parsed.

Everything else therefore renders as literal text rather than markup: headings,
blockquotes, tables, horizontal rules, setext headings, reference links,
autolinks, images, hard line breaks, HTML blocks. That is the "stripped or
escaped rather than passed through" half of the acceptance criteria, and it is
asserted construct by construct.

### The sanitiser

**`nh3`.** `bleach` is deprecated and its own README points at `nh3`; `nh3` is
the maintained binding to Rust's `ammonia`, which sanitises by parsing with
`html5ever` — the same HTML5 tokenizer a browser uses. Parsing with the
browser's own grammar is the property that matters, because the sanitiser bugs
that get exploited are parser differentials, not missing tag names. A
hand-rolled sanitiser is not an option.

The allowlist is by tag, with attributes named per tag; everything unnamed is
dropped. URL schemes are `http`, `https`, `mailto`, and relative URLs are
**denied** — which is what closes protocol-relative `//cdn.example/x` as well as
root-relative paths.

MathML elements are allowlisted by name. `annotation` and `annotation-xml` are
deliberately **excluded**: `annotation-xml` is an HTML integration point inside
foreign content and therefore a classic mXSS surface, and `latex2mathml` does
not emit either.

## Out of scope

- **The iframe document itself** — the `<html>` shell, the `srcdoc`, the CSS,
  the answer controls. Issue #19 owns the UI; this ticket produces HTML
  fragments. `BlankSegment` renders to a neutral placeholder element carrying
  the blank id, not to an option bank or a text input, because choosing between
  those is a `ModePolicy.render_hint` decision and belongs where the control is
  built.
- **`QuizSession` and the grading response.** Acceptance 32's "with no
  additional request" is asserted here as *the renderer performs no I/O and the
  MathML for a resolved blank is produced synchronously in-process*. Wiring that
  MathML into a `Verdict` is issue #6's.
- **Parsing LaTeX out of model output.** Where the model's `$...$` becomes a
  `MathSegment` is authoring's business (issue #9); this ticket provides the
  conversion function it calls.
- **Nested blanks.** Deferred past the prototype by ADR-0012 on the
  accessibility story, and structurally impossible in `domain/types.py`.
- **A Pygments colour scheme choice.** `highlight_css()` returns the default
  style's definitions; picking a theme is a UI decision.
- **`__event_call__` / hardened deployments.** ADR-0015 put these outside the
  prototype.

## Acceptance

1. An explanation containing `<script>alert(1)</script>` renders inert — the
   tag is gone, not merely escaped-and-hoped-for (master acceptance 33).
2. Attribute-level vectors are neutralised, each asserted separately: an
   `on*` handler, a `javascript:` href, a mixed-case `JaVaScRiPt:` href, an
   entity-encoded `&#x6a;avascript:` href, a `data:text/html` href, a `style`
   attribute, a protocol-relative `//host` href.
3. Every Markdown construct outside the subset — heading, blockquote, table,
   horizontal rule, image, autolink, reference link, raw HTML — appears as
   literal text in the output, and none of them produces an element.
4. Each construct **inside** the subset produces its element: `<strong>`,
   `<em>`, `<code>`, `<pre><code>` with Pygments spans, `<ul>`/`<ol>`/`<li>`,
   `<a href>`.
5. LaTeX becomes MathML with a `<math>` root, in-process, with no network call
   — asserted by a socket guard that raises if anything opens one (master
   acceptance 31).
6. The rendered output of a full explanation contains no `<script>`, no
   `<link>`, no `src=`, no `@import`, no `url(`, and no absolute or
   protocol-relative URL other than in an `<a href>` the author wrote — no CDN
   or font request is possible from it (master acceptance 31).
7. `highlight_css()` contains no `url(` and no `@import`, so inlining it fetches
   nothing.
8. Resolving a `BlankSegment` whose answer is a formula, via
   `domain.types.resolve_blank` with a `MathSegment` built by
   `latex_to_mathml`, puts that formula's MathML into the rendered output — with
   no network call and no second pass over the model (master acceptance 32).
9. Rendering walks the segment array: rendering `[a, b, c]` equals rendering
   each element in order and concatenating, and no sentinel string appears
   anywhere in the module.
10. A blank masks a whole formula: `resolve_blank` rejects a `BlankSegment` as a
    resolution (already true in the domain), and the renderer has no path that
    descends into a `MathSegment` looking for a blank.
11. `sanitise` is idempotent on the renderer's own output — sanitising a
    rendered explanation a second time changes nothing, which is what makes
    "everything rendered passed the allowlist" checkable on the finished
    document rather than only at each boundary.
12. The full suite stays green with the `rendering` extra **absent**: the new
    tests skip, they do not fail.
