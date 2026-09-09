"""The allowlist every rendered byte passes through.

ADR-0012 makes sanitisation unconditional — "everything rendered passes through
sanitisation, model-authored or not" — and ADR-0015 says why it is the only
line: the prototype targets a default Open WebUI install, where `IFRAME_CSP` is
unset, so nothing downstream strips what this module lets through.

**`nh3`**, not `bleach`. `bleach` is deprecated and its own README points here.
`nh3` binds Rust's `ammonia`, which sanitises by *parsing* with `html5ever` —
the tokenizer a browser uses — rather than by matching patterns over a string.
That is the property worth paying for: the sanitiser bugs that get exploited
are parser differentials (a comment that is not a comment, a `<mtext>` that is
an HTML integration point), not forgotten tag names.

The allowlist is stated as data rather than buried in the call, because "what
may appear in the page" is the reviewable artefact of this ticket. Three rules
run over it:

* **Tags by name.** Anything unnamed is dropped, contents and all for
  script-bearing elements.
* **Attributes per tag**, never globally. Only `<a>` may carry a URL. No tag
  may carry `style` or an `on*` handler — asserted in the tests, not merely
  intended.
* **URLs must be absolute** and `http`/`https`/`mailto`. Relative URLs are
  denied, which is what closes protocol-relative `//cdn.example/x`: it names a
  CDN while carrying no scheme for a scheme allowlist to reject.
"""

import re

import nh3

_HTML_TAGS = frozenset(
    {
        "p",
        "br",
        "strong",
        "em",
        "code",
        "pre",
        "ul",
        "ol",
        "li",
        "a",
        "span",
    }
)
"""The restricted Markdown subset's output, and nothing more (ADR-0012)."""

_MATHML_TAGS = frozenset(
    {
        "math",
        "mrow",
        "mi",
        "mn",
        "mo",
        "ms",
        "mtext",
        "mspace",
        "msup",
        "msub",
        "msubsup",
        "mfrac",
        "msqrt",
        "mroot",
        "munder",
        "mover",
        "munderover",
        "mmultiscripts",
        "mprescripts",
        "mtable",
        "mtr",
        "mtd",
        "mstyle",
        "mpadded",
        "mphantom",
        "menclose",
        "merror",
    }
)
"""Presentation MathML, matched to what `latex2mathml` emits.

`annotation` and `annotation-xml` are deliberately absent. `annotation-xml` is
an HTML integration point inside foreign content and therefore the classic mXSS
surface in MathML; `latex2mathml` emits neither, so excluding both costs
nothing and closes the vector by construction rather than by filtering.
`maction` is absent for the same reason — it is the interactive one.
"""

ALLOWED_TAGS = _HTML_TAGS | _MATHML_TAGS

_MATHML_ATTRIBUTES = frozenset(
    {
        "accent",
        "columnalign",
        "columnspacing",
        "depth",
        "displaystyle",
        "fence",
        "form",
        "height",
        "linebreak",
        "linethickness",
        "lspace",
        "mathbackground",
        "mathcolor",
        "mathsize",
        "mathvariant",
        "maxsize",
        "minsize",
        "movablelimits",
        "notation",
        "rowlines",
        "rowspacing",
        "rspace",
        "scriptlevel",
        "separator",
        "stretchy",
        "voffset",
        "width",
    }
)
"""Presentational only. `href` is not here: `latex2mathml` supports `\\href`,
which would make a formula a link vector."""

BLANK_ID_ATTRIBUTE = "data-blank-id"
"""How a rendered blank carries the id the answer control binds to."""

ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "code": frozenset({"class"}),
    "pre": frozenset({"class"}),
    "span": frozenset({"class", BLANK_ID_ATTRIBUTE}),
    "ol": frozenset({"start"}),
    "math": _MATHML_ATTRIBUTES | {"xmlns", "display"},
    **{tag: _MATHML_ATTRIBUTES for tag in _MATHML_TAGS - {"math"}},
}

ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

_SAFE_TOKEN = re.compile(r"\A[A-Za-z0-9 _-]+\Z")
"""What a `class` or blank id may contain.

Both are model-influenced — a fence's info string becomes
`class="language-<info>"`, and a blank id arrives in the authoring payload. The
serialiser escapes attribute values, so this is not what stops an injection; it
stops a crafted value from reaching for the iframe's own stylesheet or its
`querySelector` calls.
"""


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    """Drop allowlisted attributes whose *value* is out of bounds."""
    if attribute in {"class", BLANK_ID_ATTRIBUTE} and not _SAFE_TOKEN.match(value):
        return None
    return value


def sanitise(html: str) -> str:
    """Reduce `html` to the allowlist above.

    Args:
      html: A fragment. Untrusted — the caller need not know where it came
        from, which is the point of running everything through one function.

    Returns:
      The fragment with every element, attribute and URL scheme outside the
      allowlist removed. Idempotent: sanitising the result changes nothing.
    """
    return nh3.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes={tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()},
        attribute_filter=_filter_attribute,
        url_schemes=set(ALLOWED_URL_SCHEMES),
        url_relative="deny",
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
