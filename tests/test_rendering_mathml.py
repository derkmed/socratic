"""LaTeX becomes MathML server-side, in-process (master acceptance 31).

`MathSegment.mathml` already holds MathML by its own docstring, so the
conversion this module tests sits **upstream** of the segment array, not inside
the renderer: authoring builds a `MathSegment` with it, and the grading path
builds one with it when a blank resolves to a formula. `content.py` treats a
`MathSegment` as MathML that still has to clear the sanitiser.
"""

import pytest

pytest.importorskip(
    "latex2mathml",
    reason="the renderer's tests need the optional `rendering` extra "
    "installed; the domain is deliberately testable without it",
)

from socratic.rendering import mathml  # noqa: E402
from socratic.rendering import sanitiser  # noqa: E402


class TestConversion:
    def test_latex_becomes_a_math_element(self):
        out = mathml.latex_to_mathml("x^2")
        assert out.startswith("<math")
        assert out.endswith("</math>")

    def test_the_math_element_declares_the_mathml_namespace(self):
        assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in mathml.latex_to_mathml(
            "x"
        )

    def test_a_superscript_becomes_msup(self):
        out = mathml.latex_to_mathml("x^2")
        assert "<msup>" in out and "<mi>x</mi>" in out and "<mn>2</mn>" in out

    def test_a_fraction_becomes_mfrac(self):
        assert "<mfrac>" in mathml.latex_to_mathml(r"\frac{1}{2}")

    def test_an_integral_keeps_its_operator(self):
        out = mathml.latex_to_mathml(r"\int_0^\infty e^{-x}\,dx")
        assert "<msubsup>" in out
        assert "∫" in out or "&#x0222B;" in out

    def test_display_mode_is_requestable(self):
        assert 'display="inline"' in mathml.latex_to_mathml("x")
        assert 'display="block"' in mathml.latex_to_mathml("x", display=True)


class TestNoNetworkAndNoClientDependency:
    def test_conversion_opens_no_connection(self, no_network):
        # Master acceptance 31: no client JS, no font, no CDN. The first thing
        # to prove is that the server side of it is a pure transformation.
        assert mathml.latex_to_mathml(r"\sum_{i=1}^{n} i^2").startswith("<math")

    @pytest.mark.parametrize(
        "latex", ["x^2", r"\frac{a}{b}", r"\sqrt{2}", r"\alpha \le \beta"]
    )
    def test_the_output_references_no_external_resource(self, latex):
        # The `xmlns` declaration is a namespace *name*. It looks like a URL
        # and is never fetched, so it is removed before the scan rather than
        # special-cased inside it.
        out = mathml.latex_to_mathml(latex).replace(
            'xmlns="http://www.w3.org/1998/Math/MathML"', ""
        )
        for marker in ("http://", "https://", "//", "src=", "@import", "url("):
            assert marker not in out, (marker, out)

    def test_the_output_contains_no_script_and_no_link(self):
        out = mathml.latex_to_mathml(r"\int_0^1 x\,dx")
        assert "<script" not in out and "<link" not in out


class TestSanitisation:
    """Everything rendered passes the allowlist, `latex2mathml` included."""

    def test_the_output_is_already_sanitised(self):
        out = mathml.latex_to_mathml(r"\frac{x}{y}")
        assert sanitiser.sanitise(out) == out

    def test_an_href_command_does_not_survive_conversion(self):
        # latex2mathml implements `\href`, which would make a formula a link.
        out = mathml.latex_to_mathml(r"\href{javascript:alert(1)}{x}")
        assert "javascript" not in out
        assert "href" not in out

    def test_malformed_latex_raises_rather_than_emitting_anything(self):
        with pytest.raises(mathml.LatexConversionError):
            mathml.latex_to_mathml(r"\frac{")
