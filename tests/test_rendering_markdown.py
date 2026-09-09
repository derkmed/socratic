"""The restricted Markdown subset (ADR-0012).

"Inline code, fenced code blocks with highlighting, bold, italic, lists, links.
Nothing else." Both halves are asserted: each construct in the subset produces
its element, and each construct outside it produces literal text rather than
markup. The second half is the one that rots silently — a dependency bump that
turns tables back on would otherwise pass unnoticed.
"""

import re

import pytest

pytest.importorskip(
    "markdown_it",
    reason="the renderer's tests need the optional `rendering` extra "
    "installed; the domain is deliberately testable without it",
)

from socratic.rendering import markdown  # noqa: E402
from socratic.rendering import sanitiser  # noqa: E402


class TestTheSubsetItAllows:
    def test_bold(self):
        assert "<strong>bold</strong>" in markdown.render_markdown("**bold**")

    def test_italic(self):
        assert "<em>italic</em>" in markdown.render_markdown("*italic*")

    def test_inline_code(self):
        assert "<code>x = 1</code>" in markdown.render_markdown("`x = 1`")

    def test_an_unordered_list(self):
        out = markdown.render_markdown("- one\n- two\n")
        assert "<ul>" in out and out.count("<li>") == 2

    def test_an_ordered_list(self):
        out = markdown.render_markdown("1. one\n2. two\n")
        assert "<ol>" in out and out.count("<li>") == 2

    def test_a_link(self):
        out = markdown.render_markdown("[docs](https://example.com/x)")
        assert '<a href="https://example.com/x"' in out
        assert ">docs</a>" in out

    def test_a_fenced_code_block(self):
        out = markdown.render_markdown("```\nplain\n```")
        assert "<pre>" in out and "<code" in out and "plain" in out

    def test_a_fenced_code_block_is_syntax_highlighted(self):
        out = markdown.render_markdown('```python\ndef f():\n    return 1\n```')
        assert '<span class="k">def</span>' in out

    def test_an_unknown_fence_language_falls_back_to_plain_text(self):
        # Not a guess-the-lexer call: guessing is slow and non-deterministic,
        # and a wrong guess colours code misleadingly.
        out = markdown.render_markdown("```notalanguage\nx = 1\n```")
        assert "x = 1" in out
        assert "<pre>" in out

    def test_a_backslash_escape_suppresses_emphasis(self):
        out = markdown.render_markdown(r"a \*literal\* star")
        assert "<em>" not in out
        assert "*literal*" in out


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("source", "element"),
        [
            ("# Heading\n", "<h1"),
            ("Heading\n=======\n", "<h1"),
            ("## Sub\n", "<h2"),
            ("> quoted\n", "<blockquote"),
            ("| a | b |\n|---|---|\n| 1 | 2 |\n", "<table"),
            ("---\n", "<hr"),
            ("***\n", "<hr"),
            ("~~struck~~", "<s"),
            ("![alt](https://evil.example/x.png)", "<img"),
            ("    indented code\n", "<pre"),
            ("term\n: definition\n", "<dl"),
            ("line one  \nline two\n", "<br"),
        ],
    )
    def test_a_construct_outside_the_subset_produces_no_element(
        self, source, element
    ):
        assert element not in markdown.render_markdown(source)

    def test_a_heading_survives_as_literal_text(self):
        # "Stripped or escaped rather than passed through" — the words the
        # author wrote are still there, they just are not markup.
        assert "# Heading" in markdown.render_markdown("# Heading\n")

    def test_a_table_survives_as_literal_text(self):
        out = markdown.render_markdown("| a | b |\n|---|---|\n")
        assert "| a | b |" in out

    def test_an_autolink_is_not_linkified(self):
        out = markdown.render_markdown("<https://evil.example/x>")
        assert "<a " not in out
        assert "evil.example" in out

    def test_a_bare_url_is_not_linkified(self):
        out = markdown.render_markdown("visit https://evil.example/x now")
        assert "<a " not in out

    def test_a_reference_link_is_not_resolved(self):
        out = markdown.render_markdown("[docs][ref]\n\n[ref]: https://evil.example/x\n")
        assert "<a " not in out


class TestRawHtmlAndInjection:
    @pytest.mark.parametrize(
        "source",
        [
            "<script>alert(1)</script>",
            '<img src=x onerror="alert(1)">',
            "<iframe src=https://evil.example></iframe>",
            '<div onclick="alert(1)">x</div>',
            "<style>body{background:url(//evil.example/x)}</style>",
        ],
    )
    def test_raw_html_never_becomes_markup(self, source):
        # The author's characters survive; none of them becomes an element or
        # an attribute. `&lt;script&gt;` in a paragraph is text, and inert.
        out = markdown.render_markdown(source)
        assert _tag_names(out) <= {"p", "em", "strong"}, out
        assert _attribute_names(out) == set(), out
        assert "&lt;" in out

    def test_a_javascript_link_loses_its_href(self):
        out = markdown.render_markdown("[click](javascript:alert(1))")
        assert "javascript:" not in out.lower() or "href" not in out

    def test_a_protocol_relative_link_loses_its_href(self):
        out = markdown.render_markdown("[click](//cdn.example/x.js)")
        assert "href" not in out

    def test_a_fence_info_string_cannot_smuggle_an_attribute(self):
        # The info string becomes `class="language-<info>"`, and it is
        # model-authored.
        out = markdown.render_markdown('```python" onload="alert(1)\nx\n```')
        assert "onload" not in _attribute_names(out)
        assert _tag_names(out) <= {"pre", "code", "span"}, out

    def test_a_blank_placeholder_cannot_be_forged_from_markdown(self):
        # The renderer's own blank element is the one piece of markup the
        # iframe binds behaviour to, so Markdown must not be able to mint one.
        out = markdown.render_markdown(
            '<span class="socratic-blank" data-blank-id="b1"></span>'
        )
        assert sanitiser.BLANK_ID_ATTRIBUTE not in _attribute_names(out)
        assert "span" not in _tag_names(out)

    def test_the_output_is_already_sanitised(self):
        source = "**b** `c` [l](https://ok.example) \n\n- x\n\n```python\nx=1\n```\n"
        out = markdown.render_markdown(source)
        assert sanitiser.sanitise(out) == out


class TestAFragmentIsAClauseUnlessItIsNot:
    """`render_fragment` — the text-segment render (issue #86).

    A text segment is usually a fragment of a sentence with a blank on one or
    both sides, so it must not become a `<p>`, and it must keep the spaces
    that hold it apart from the blank. A segment that carries genuine block
    content is a block, and renders exactly as `render_markdown` renders it.
    """

    def test_a_clause_gets_no_paragraph_wrapper(self):
        assert markdown.render_fragment("Heat rises") == "Heat rises"

    def test_a_clause_keeps_the_space_that_precedes_the_next_blank(self):
        assert markdown.render_fragment("Heat flows because ") == (
            "Heat flows because "
        )

    def test_a_clause_keeps_the_space_that_follows_the_previous_blank(self):
        assert markdown.render_fragment(" rises.") == " rises."

    def test_a_whitespace_only_segment_survives_as_a_gap(self):
        # The separator between two adjacent blanks. Block-rendered it is the
        # empty string, which fuses the two blanks into one.
        assert markdown.render_fragment(" ") == " "

    def test_an_empty_segment_renders_to_nothing(self):
        assert markdown.render_fragment("") == ""

    def test_the_inline_subset_still_renders(self):
        out = markdown.render_fragment("a **bold** and `code` bit")
        assert out == "a <strong>bold</strong> and <code>code</code> bit"

    def test_a_fenced_block_renders_as_a_block(self):
        source = "```python\ndef f():\n    return 1\n```"
        out = markdown.render_fragment(source)
        assert out == markdown.render_markdown(source)
        assert "<pre" in out
        assert 'class="language-python"' in out

    def test_a_list_renders_as_a_block(self):
        source = "- one\n- two\n"
        out = markdown.render_fragment(source)
        assert out == markdown.render_markdown(source)
        assert "<li>one</li>" in out

    def test_two_paragraphs_stay_two_paragraphs(self):
        # The thing the CSS workaround could not preserve: a real paragraph
        # break *inside* one text segment.
        source = "first paragraph\n\nsecond paragraph"
        out = markdown.render_fragment(source)
        assert out == markdown.render_markdown(source)
        assert out.count("<p>") == 2

    def test_a_soft_line_break_is_still_one_clause(self):
        assert markdown.render_fragment("a\nb") == "a\nb"

    def test_raw_html_in_a_clause_is_escaped(self):
        out = markdown.render_fragment("<script>alert(1)</script>")
        assert "<script" not in out
        assert "&lt;script&gt;" in out

    def test_a_javascript_href_does_not_survive_a_clause(self):
        out = markdown.render_fragment("[click](javascript:alert(1))")
        assert "<a" not in out

    def test_a_clause_is_sanitised(self):
        out = markdown.render_fragment("an *emphasised* clause")
        assert out == sanitiser.sanitise(out)


class TestHighlightingNeedsNoNetwork:
    def test_rendering_opens_no_connection(self, no_network):
        out = markdown.render_markdown("```python\nimport os\n```")
        assert "<pre" in out

    def test_the_output_references_no_external_resource(self):
        out = markdown.render_markdown(
            "**b** `c`\n\n- x\n\n```python\nimport os\n```\n"
        )
        for marker in ("<script", "<link", "src=", "@import", "url(", "//"):
            assert marker not in out, (marker, out)

    def test_the_highlight_stylesheet_fetches_nothing(self):
        # It is meant to be inlined into the iframe. A `url(` or an `@import`
        # in it would be exactly the CDN or font request ADR-0012 rules out.
        css = markdown.highlight_css()
        assert ".k" in css
        for marker in ("url(", "@import", "http://", "https://", "//"):
            assert marker not in css, (marker, css[:200])


_TAG = re.compile(r"<([A-Za-z][\w:-]*)((?:\s[^<>]*)?)/?>")
"""Real markup only.

Escaped text carries `&lt;script&gt;`, not `<script>`, so scanning for tags
this way is what distinguishes "the parser produced an element" from "the
author's characters are still on the page, inert" — which is the whole
distinction these tests are about.
"""


def _tag_names(html: str) -> set[str]:
    return {match.group(1).lower() for match in _TAG.finditer(html)}


def _attribute_names(html: str) -> set[str]:
    names: set[str] = set()
    for match in _TAG.finditer(html):
        names.update(re.findall(r"[\s\"']([A-Za-z:-]+)=", match.group(2)))
    return names
