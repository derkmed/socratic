"""The restricted Markdown subset (ADR-0012).

"Inline code, fenced code blocks with highlighting, bold, italic, lists,
links." Nothing else — the subset covers what a 250-word technical explanation
needs, keeps the iframe renderer small, and narrows what must be sanitised.

The subset is expressed as a **denial by default**: `markdown-it-py`'s `zero`
preset enables nothing but paragraphs and text, and exactly six rules are
enabled on top of it. Configuring the other way round — the full parser minus
a disable list — would silently readmit every construct the next release adds.

Everything the parser does not recognise stays as the author's literal
characters, which is the "stripped or escaped rather than passed through" half
of the ticket: a `# heading` renders as the text `# heading`, a table as its
own pipes. Raw HTML is escaped rather than parsed (`html: False`), so a
prompt-injected `<script>` never reaches the sanitiser as markup at all — the
sanitiser is the second line here, not the first.

Highlighting is **Pygments, inline**. The colours come from
`highlight_css()`, which the iframe embeds in a `<style>` element; there is no
stylesheet to fetch, which is the same constraint that put math in MathML.
"""

import markdown_it
import pygments
import pygments.formatters
import pygments.lexers
import pygments.util

from socratic.rendering import sanitiser

ENABLED_RULES = (
    "emphasis",
    "backticks",
    "fence",
    "list",
    "link",
    "escape",
)
"""The whole subset, one rule each.

`emphasis` is bold and italic together; `backticks` is inline code; `escape` is
`\\*` and its friends — not a construct of its own but the only way an author
writes a literal asterisk, and the subset is otherwise inescapable.

Left off, and therefore literal text: headings (`heading`, `lheading`),
blockquotes, tables, horizontal rules (`hr`), indented code (`code`),
reference definitions (`reference`), images (`image`), autolinks (`autolink`),
hard line breaks (`newline`), entities (`entity`), and every HTML rule.
"""

_PYGMENTS_STYLE = "default"

_FORMATTER = pygments.formatters.HtmlFormatter(nowrap=True, style=_PYGMENTS_STYLE)
"""`nowrap` because markdown-it supplies the `<pre><code>` wrapper itself."""


def _highlight(code: str, language: str, _attrs: str) -> str:
    """Pygments-highlight a fenced block, falling back to plain text.

    An unrecognised language becomes `TextLexer` rather than a `guess_lexer`
    call: guessing is slow, non-deterministic across Pygments versions, and a
    wrong guess colours code misleadingly. The fence's info string is
    model-authored, so an unrecognised one is the expected case, not an error.
    """
    try:
        lexer = pygments.lexers.get_lexer_by_name(language, stripall=False)
    except pygments.util.ClassNotFound:
        lexer = pygments.lexers.get_lexer_by_name("text", stripall=False)
    return pygments.highlight(code, lexer, _FORMATTER)


_PARSER = (
    markdown_it.MarkdownIt(
        "zero",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
            "breaks": False,
            "highlight": _highlight,
        },
    )
    .enable(list(ENABLED_RULES))
)


def render_markdown(text: str) -> str:
    """Render restricted Markdown to sanitised HTML.

    Args:
      text: Model-authored Markdown. Untrusted.

    Returns:
      HTML containing only the subset's elements, already through
      `sanitiser.sanitise`.
    """
    return sanitiser.sanitise(_PARSER.render(text))


_ONE_PARAGRAPH = ("paragraph_open", "inline", "paragraph_close")
"""The block token stream of a text that is a single flowing paragraph.

An empty stream — whitespace only, or nothing — counts as well: it is a text
with no block content at all, which is the other case that must flow.
"""


def render_fragment(text: str) -> str:
    """Render one `text` segment of an explanation.

    A text segment is usually a **fragment of a sentence** with a blank on one
    or both sides (ADR-0004), so `render_markdown` is the wrong render for it:
    it wraps the fragment in a block `<p>` and drops the leading and trailing
    spaces, and the explanation reads as a stack of blocks rather than as a
    sentence with holes in it.

    So a segment the parser sees as **at most one paragraph and nothing else**
    is rendered inline — no wrapper, whitespace intact — and everything else is
    rendered exactly as `render_markdown` renders it. A fenced code block or a
    list is not a clause; it is a block, and it keeps its block layout.

    The parser makes that call from its own block token stream rather than a
    regex guessing at block markers, and both paths share one parser, so the
    restricted subset, the escaping of raw HTML and the sanitiser pass are the
    same either way.

    Args:
      text: Model-authored Markdown. Untrusted.

    Returns:
      HTML containing only the subset's elements, already through
      `sanitiser.sanitise`. No `<p>` unless the text asked for one.
    """
    tokens = tuple(token.type for token in _PARSER.parse(text, {}))
    if tokens and tokens != _ONE_PARAGRAPH:
        return render_markdown(text)
    return sanitiser.sanitise(_PARSER.renderInline(text))


def highlight_css() -> str:
    """The Pygments colour definitions, for inlining into the iframe.

    Returned rather than linked: ADR-0012 rules out a CDN or font request, and
    a `<link rel=stylesheet>` is one. The definitions reference no external
    resource, which the tests assert rather than assume.
    """
    return _FORMATTER.get_style_defs(".highlight")
