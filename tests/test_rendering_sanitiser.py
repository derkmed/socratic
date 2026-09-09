"""The last line before model-authored bytes become HTML in the page.

ADR-0012: "everything rendered passes through sanitisation, model-authored or
not. A prompt-injected explanation is a plausible route to HTML in the page."
ADR-0015 makes that sharper — the prototype targets a **default** Open WebUI
install, where `IFRAME_CSP` is unset, so no CSP catches what this module lets
through.

The vectors asserted here are deliberately not only `<script>`. A sanitiser
that drops script tags and keeps `onerror=` or a `javascript:` href has not
sanitised anything; those are the cases that actually ship. They are tested
against `sanitise` directly rather than through `render_explanation`, because
the Markdown layer escapes raw HTML on its own — a `<script>` smuggled through
Markdown would prove the Markdown layer works and say nothing about the last
line.
"""

import re

import pytest

pytest.importorskip(
    "nh3",
    reason="the renderer's tests need the optional `rendering` extra "
    "installed; the domain is deliberately testable without it",
)

from socratic.rendering import sanitiser  # noqa: E402


class TestScriptBearingElements:
    """Master spec acceptance 33."""

    def test_a_script_tag_renders_inert(self):
        assert sanitiser.sanitise("<script>alert(1)</script>") == ""

    def test_a_script_tag_amid_prose_leaves_only_the_prose(self):
        cleaned = sanitiser.sanitise("<p>before<script>alert(1)</script>after</p>")
        assert "script" not in cleaned
        assert "alert" not in cleaned
        assert "before" in cleaned and "after" in cleaned

    @pytest.mark.parametrize(
        "html",
        [
            "<iframe src=https://evil.example></iframe>",
            "<object data=x></object>",
            "<embed src=x>",
            "<form action=x><input name=y></form>",
            "<style>body{background:url(//evil.example/x)}</style>",
            "<link rel=stylesheet href=https://evil.example/x.css>",
            "<base href=https://evil.example/>",
            "<meta http-equiv=refresh content=0;url=https://evil.example>",
            "<svg><use href=//evil.example /></svg>",
            "<template><script>alert(1)</script></template>",
        ],
    )
    def test_no_element_outside_the_allowlist_survives(self, html):
        cleaned = sanitiser.sanitise(html)
        assert "<" not in cleaned, cleaned

    def test_a_comment_hiding_a_script_is_dropped_whole(self):
        # html5ever's tokenizer resolves the comment the way a browser does;
        # a regex sanitiser is where this one gets through.
        cleaned = sanitiser.sanitise("<p>a<!--<script>alert(1)</script>-->b</p>")
        assert cleaned == "<p>ab</p>"


class TestAttributeLevelVectors:
    """The half a tag-name allowlist does not cover."""

    @pytest.mark.parametrize(
        "html",
        [
            '<img src=x onerror="alert(1)">',
            '<p onclick="alert(1)">x</p>',
            '<span class="k" onmouseover="alert(1)">x</span>',
            '<a href="https://ok.example" onfocus="alert(1)">x</a>',
            '<li onload="alert(1)">x</li>',
            '<math><mo onclick="alert(1)">x</mo></math>',
        ],
    )
    def test_an_event_handler_attribute_never_survives(self, html):
        cleaned = sanitiser.sanitise(html)
        assert "alert" not in cleaned, cleaned
        handlers = [
            name for name in _attribute_names(cleaned) if name.startswith("on")
        ]
        assert handlers == [], cleaned

    @pytest.mark.parametrize(
        "href",
        [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "  javascript:alert(1)",
            "java\tscript:alert(1)",
            "&#x6a;avascript:alert(1)",
            "vbscript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "file:///etc/passwd",
        ],
    )
    def test_a_dangerous_href_scheme_is_stripped_from_the_link(self, href):
        cleaned = sanitiser.sanitise(f'<a href="{href}">click</a>')
        assert "href" not in cleaned, cleaned
        assert "click" in cleaned

    @pytest.mark.parametrize(
        "href", ["https://ok.example/x", "http://ok.example/x", "mailto:a@b.example"]
    )
    def test_an_author_written_link_survives_with_its_href(self, href):
        cleaned = sanitiser.sanitise(f'<a href="{href}">click</a>')
        assert f'href="{href}"' in cleaned

    @pytest.mark.parametrize("href", ["//cdn.example/x.js", "/local/path", "../up"])
    def test_a_relative_or_protocol_relative_href_is_denied(self, href):
        # `//cdn.example/x` is how a CDN reference sneaks past a scheme
        # allowlist: it has no scheme at all (ADR-0012, no CDN request).
        cleaned = sanitiser.sanitise(f'<a href="{href}">click</a>')
        assert "href" not in cleaned, cleaned

    def test_an_outbound_link_carries_noopener_noreferrer(self):
        cleaned = sanitiser.sanitise('<a href="https://ok.example">x</a>')
        assert 'rel="noopener noreferrer"' in cleaned

    def test_a_style_attribute_is_dropped(self):
        cleaned = sanitiser.sanitise(
            '<p style="background:url(//evil.example/x)">y</p>'
        )
        assert cleaned == "<p>y</p>"

    @pytest.mark.parametrize(
        "attribute", ["id", "srcset", "formaction", "background", "xlink:href"]
    )
    def test_an_unlisted_attribute_is_dropped(self, attribute):
        cleaned = sanitiser.sanitise(f'<p {attribute}="x">y</p>')
        assert cleaned == "<p>y</p>"

    def test_a_class_value_is_constrained_to_a_safe_charset(self):
        # Class survives because Pygments and markdown-it both use it. The
        # value is model-influenced (a fence's info string becomes
        # `language-<info>`), so it is filtered rather than trusted.
        assert 'class="k"' in sanitiser.sanitise('<span class="k">x</span>')
        assert "class" not in sanitiser.sanitise('<span class="a{}<>b">x</span>')


class TestMathML:
    """MathML is foreign content, and foreign content has its own vectors."""

    def test_presentation_mathml_survives_intact(self):
        html = (
            '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
            "<mrow><msup><mi>x</mi><mn>2</mn></msup></mrow></math>"
        )
        assert sanitiser.sanitise(html) == html

    def test_annotation_xml_is_not_allowlisted(self):
        # `annotation-xml` with an HTML encoding is an HTML integration point
        # inside foreign content — the classic mXSS surface. latex2mathml
        # emits neither annotation element, so excluding both costs nothing.
        cleaned = sanitiser.sanitise(
            '<math><annotation-xml encoding="text/html">'
            "<script>alert(1)</script></annotation-xml></math>"
        )
        assert cleaned == "<math></math>"

    def test_annotation_is_not_allowlisted(self):
        cleaned = sanitiser.sanitise(
            '<math><semantics><mi>x</mi>'
            '<annotation encoding="application/x-tex">x</annotation>'
            "</semantics></math>"
        )
        assert "annotation" not in cleaned

    def test_an_href_inside_math_is_dropped(self):
        # latex2mathml supports `\href`, so a formula is a link vector too.
        cleaned = sanitiser.sanitise(
            '<math><mi href="javascript:alert(1)">x</mi></math>'
        )
        assert cleaned == "<math><mi>x</mi></math>"

    def test_html_smuggled_through_mtext_is_sanitised(self):
        cleaned = sanitiser.sanitise(
            "<math><mtext><script>alert(1)</script></mtext></math>"
        )
        assert "alert" not in cleaned


class TestTheAllowlistItself:
    def test_the_allowlist_names_no_script_bearing_element(self):
        forbidden = {"script", "style", "iframe", "object", "embed", "svg", "form"}
        assert sanitiser.ALLOWED_TAGS & forbidden == set()

    def test_no_tag_may_carry_an_event_handler_or_style(self):
        for tag, attributes in sanitiser.ALLOWED_ATTRIBUTES.items():
            assert "style" not in attributes, tag
            assert not any(name.startswith("on") for name in attributes), tag

    def test_every_attribute_entry_names_an_allowed_tag(self):
        strays = set(sanitiser.ALLOWED_ATTRIBUTES) - sanitiser.ALLOWED_TAGS
        assert strays == set()

    def test_only_href_bearing_tags_may_carry_a_url(self):
        url_bearing = {
            tag
            for tag, attributes in sanitiser.ALLOWED_ATTRIBUTES.items()
            if attributes & {"href", "src", "action", "data"}
        }
        assert url_bearing == {"a"}

    def test_the_url_schemes_are_the_three_safe_ones(self):
        assert sanitiser.ALLOWED_URL_SCHEMES == frozenset(
            {"http", "https", "mailto"}
        )


class TestIdempotence:
    @pytest.mark.parametrize(
        "html",
        [
            "<p>plain</p>",
            '<p>an <a href="https://ok.example">author link</a></p>',
            '<pre><code class="language-python">'
            '<span class="k">def</span></code></pre>',
            '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>',
            "<p>&lt;script&gt; as literal text</p>",
            "<script>alert(1)</script>",
        ],
    )
    def test_sanitising_twice_changes_nothing_the_first_pass_left(self, html):
        once = sanitiser.sanitise(html)
        assert sanitiser.sanitise(once) == once

    def test_escaped_text_is_not_double_escaped(self):
        assert sanitiser.sanitise("<p>&lt;b&gt;</p>") == "<p>&lt;b&gt;</p>"


_TAG = re.compile(r"<([A-Za-z][\w:-]*)((?:\s[^<>]*)?)/?>")


def _attribute_names(html: str) -> set[str]:
    """Attribute names on real elements, for asserting one is gone.

    Scoped to tags rather than matched across the whole string: escaped text
    still reads `onerror="alert(1)"` to a naive regex while being inert on the
    page, and conflating the two would make these assertions fail for the
    wrong reason — or pass for it.
    """
    names: set[str] = set()
    for match in _TAG.finditer(html):
        names.update(re.findall(r"[\s\"']([A-Za-z:-]+)=", match.group(2)))
    return names
