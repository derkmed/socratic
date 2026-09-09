"""Rendering — quiz content turned into HTML for the sandboxed iframe.

Like `socratic.adapters`, this package sits on the far side of the ADR-0002
portability seam: `socratic.domain` imports only the standard library and
itself, and the third-party libraries this rendering needs — `nh3`,
`markdown-it-py`, `Pygments`, `latex2mathml` — live here instead. They are the
`rendering` optional extra rather than base dependencies, so the domain still
installs and tests with no third-party package at all
(`tests/test_import_hygiene.py`).

Four modules, composed in one direction:

* `sanitiser` — the allowlist. Everything rendered passes through it,
  model-authored or not (ADR-0012).
* `mathml` — LaTeX to MathML, server-side, in-process. This runs **upstream**
  of `domain.types.MathSegment`, which by its own docstring already holds
  MathML; it is what authoring and the grading path call to build one.
* `markdown` — the restricted subset: inline code, fenced code blocks with
  highlighting, bold, italic, lists, links. Nothing else.
* `content` — the walk over a quiz's flat segment array (ADR-0004). The
  entry point.

Nothing here performs I/O. No client JS, no web font, no CDN: MathML renders
natively and Pygments' colours are inlined from `markdown.highlight_css()`.
"""
