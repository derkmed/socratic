"""The walk over a quiz's flat segment array (ADR-0004, ADR-0012).

The entry point, and where the ticket's structural claims are assertable:
rendering **walks the array** rather than substituting sentinels into a string,
a blank masks a whole formula rather than a term inside one, and a resolved
blank's MathML is in the output of the same in-process call that graded it —
master acceptance 32's "no additional request".
"""

import pathlib
import re

import pytest

pytest.importorskip(
    "nh3",
    reason="the renderer's tests need the optional `rendering` extra "
    "installed; the domain is deliberately testable without it",
)

from socratic.domain import types  # noqa: E402
from socratic.rendering import content  # noqa: E402
from socratic.rendering import mathml  # noqa: E402
from socratic.rendering import sanitiser  # noqa: E402

PYTHAGORAS = r"a^2 + b^2 = c^2"


class TestTheWalk:
    def test_an_empty_explanation_renders_to_nothing(self):
        assert content.render_explanation(()) == ""

    def test_each_segment_kind_contributes_its_own_markup(self):
        out = content.render_explanation(
            (
                types.TextSegment("**bold**"),
                types.MathSegment(mathml.latex_to_mathml("x^2")),
                types.BlankSegment("b1"),
            )
        )
        assert "<strong>bold</strong>" in out
        assert "<math" in out
        assert 'data-blank-id="b1"' in out

    def test_rendering_is_the_concatenation_of_rendering_each_element(self):
        # This is what "walks the segment array" means as a property: the
        # output is a fold over the list, so no element can see another and
        # there is no whole-string pass to substitute anything into.
        segments = (
            types.TextSegment("one"),
            types.MathSegment(mathml.latex_to_mathml("x")),
            types.BlankSegment("b1"),
            types.TextSegment("two"),
        )
        assert content.render_explanation(segments) == "".join(
            content.render_explanation((segment,)) for segment in segments
        )

    def test_segment_order_is_preserved(self):
        out = content.render_explanation(
            (types.TextSegment("first"), types.TextSegment("second"))
        )
        assert out.index("first") < out.index("second")

    def test_an_unknown_segment_kind_is_refused_rather_than_ignored(self):
        with pytest.raises(TypeError):
            content.render_explanation(("just a string",))


class TestNoSentinelSubstitution:
    def test_the_module_contains_no_sentinel_pattern(self):
        # ADR-0004 removed sentinels; a `{{blank_1}}` or `___` creeping back
        # in is the regression this guards. Read as UTF-8 explicitly — the
        # domain's prose is full of em dashes.
        source = pathlib.Path(content.__file__).read_text(encoding="utf-8")
        for sentinel in ("{{", "}}", "___", "%s", "<<", ">>"):
            assert sentinel not in source, sentinel

    def test_text_that_looks_like_a_sentinel_is_rendered_as_text(self):
        out = content.render_explanation(
            (types.TextSegment("the answer is {{blank_1}} here"),)
        )
        assert "{{blank_1}}" in out

    def test_a_text_segment_naming_a_blank_id_does_not_become_a_blank(self):
        out = content.render_explanation(
            (types.TextSegment("b1"), types.BlankSegment("b1"))
        )
        assert out.count('data-blank-id="b1"') == 1


class TestBlanks:
    def test_a_blank_renders_as_a_placeholder_carrying_its_id(self):
        out = content.render_explanation((types.BlankSegment("b7"),))
        assert 'data-blank-id="b7"' in out
        assert content.BLANK_CLASS in out

    def test_a_blank_placeholder_carries_no_answer(self):
        out = content.render_explanation((types.BlankSegment("b7"),))
        assert _text_of(out).strip() == ""

    def test_a_hostile_blank_id_cannot_break_out_of_the_attribute(self):
        out = content.render_explanation(
            (types.BlankSegment('b1" onclick="alert(1)'),)
        )
        assert "onclick" not in _attribute_names(out)
        assert "alert" not in out

    def test_the_placeholder_survives_its_own_sanitiser(self):
        out = content.render_explanation((types.BlankSegment("b1"),))
        assert sanitiser.sanitise(out) == out


class TestABlankIsAGapInASentence:
    """Issue #86.

    A text segment either side of a blank is a fragment of a sentence, so the
    three of them must render as one flowing line — no block wrapper, and the
    spaces that hold the words apart from the blank still there. Before this,
    each text segment was its own `<p>` and the overlay's stylesheet flattened
    them presentationally, at the cost of a real paragraph break inside one
    segment.
    """

    SENTENCE = (
        types.TextSegment("Heat flows because "),
        types.BlankSegment("b1"),
        types.TextSegment(" rises."),
    )

    def test_the_sentence_carries_no_paragraph_wrapper(self):
        assert "<p>" not in content.render_explanation(self.SENTENCE)

    def test_the_words_keep_their_distance_from_the_blank(self):
        out = content.render_explanation(self.SENTENCE)
        assert out.startswith("Heat flows because <span")
        assert out.endswith("</span> rises.")

    def test_the_rendered_text_reads_as_one_line(self):
        assert _text_of(content.render_explanation(self.SENTENCE)) == (
            "Heat flows because  rises."
        )

    def test_two_adjacent_blanks_stay_apart(self):
        out = content.render_explanation(
            (
                types.BlankSegment("b1"),
                types.TextSegment(" "),
                types.BlankSegment("b2"),
            )
        )
        assert "</span> <span" in out

    def test_a_segment_that_is_block_content_keeps_its_blocks(self):
        # The fenced block and the list are not clauses; flattening them was
        # never the point.
        out = content.render_explanation(
            (
                types.TextSegment("Consider:\n\n```python\nx = 1\n```"),
                types.TextSegment("- one\n- two\n"),
            )
        )
        assert "<pre" in out
        assert "<li>one</li>" in out
        assert "<p>Consider:</p>" in out

    def test_a_paragraph_break_inside_one_segment_survives(self):
        # What the CSS workaround could not preserve.
        out = content.render_explanation(
            (types.TextSegment("first\n\nsecond"),)
        )
        assert out.count("<p>") == 2


class TestAResolvedFormula:
    """Master acceptance 32."""

    def test_resolving_a_blank_to_a_formula_renders_that_formulas_mathml(
        self, no_network
    ):
        explanation = (
            types.TextSegment("The theorem states "),
            types.BlankSegment("b1"),
            types.TextSegment(" for right triangles."),
        )
        answer = types.MathSegment(mathml.latex_to_mathml(PYTHAGORAS))
        resolved = types.resolve_blank(explanation, "b1", answer)

        out = content.render_explanation(resolved)

        assert "<math" in out
        assert "<msup>" in out
        assert 'data-blank-id="b1"' not in out
        assert "right triangles" in out

    def test_the_mathml_is_produced_in_process_with_no_connection(
        self, no_network
    ):
        # "With no additional request" (acceptance 32) is asserted here as the
        # server-side half: converting and rendering the answer opens nothing.
        # Carrying it in the grading response is issue #6's wiring.
        answer = types.MathSegment(mathml.latex_to_mathml(PYTHAGORAS))
        assert "<math" in content.render_explanation((answer,))

    def test_the_array_stays_the_same_length(self):
        explanation = (types.TextSegment("a"), types.BlankSegment("b1"))
        resolved = types.resolve_blank(
            explanation, "b1", types.MathSegment(mathml.latex_to_mathml("x"))
        )
        assert len(resolved) == len(explanation)


class TestABlankMasksAWholeFormula:
    def test_a_blank_cannot_resolve_to_another_blank(self):
        explanation = (types.BlankSegment("b1"),)
        with pytest.raises(TypeError):
            types.resolve_blank(explanation, "b1", types.BlankSegment("b2"))

    def test_math_is_rendered_opaquely_and_never_searched_for_blanks(self):
        # There is no path that descends into a MathSegment looking for a
        # blank, so MathML that merely *mentions* a blank id is just MathML.
        formula = mathml.latex_to_mathml("b1")
        out = content.render_explanation((types.MathSegment(formula),))
        assert content.BLANK_CLASS not in out
        assert "data-blank-id" not in _attribute_names(out)


class TestSanitisationOfTheWholeDocument:
    def test_a_prompt_injected_explanation_renders_inert(self):
        # Master acceptance 33, at the entry point rather than at the
        # sanitiser: this is the call the quiz service actually makes.
        out = content.render_explanation(
            (
                types.TextSegment("Consider <script>alert(1)</script> this."),
                types.MathSegment(
                    '<math><mo onclick="alert(1)">x</mo>'
                    '<annotation-xml encoding="text/html">'
                    "<script>alert(1)</script></annotation-xml></math>"
                ),
            )
        )
        # The words survive as text — that is what "inert" means here, and it
        # is the honest outcome: the learner sees what the model wrote, the
        # browser executes none of it.
        # No `p`: the text segment is one clause and renders inline (#86).
        assert _tag_names(out) == {"math", "mo"}, out
        assert _attribute_names(out) == set(), out
        assert "&lt;script&gt;" in out
        assert "annotation" not in out

    def test_a_math_segment_built_by_something_other_than_us_is_sanitised(self):
        # `MathSegment.mathml` is a plain string on a domain dataclass; the
        # renderer cannot know it came from `latex_to_mathml`, so it does not
        # take its word for it.
        out = content.render_explanation(
            (types.MathSegment('<math><mi href="javascript:alert(1)">x</mi></math>'),)
        )
        assert "javascript" not in out
        assert "href" not in _attribute_names(out)

    def test_the_rendered_document_is_idempotent_under_the_sanitiser(self):
        # What makes "everything rendered passed the allowlist" checkable on
        # the finished document, not only at each boundary.
        out = content.render_explanation(_a_full_explanation())
        assert sanitiser.sanitise(out) == out

    def test_the_rendered_document_requests_nothing_external(self):
        # Master acceptance 31: no client JS, no font, no CDN.
        out = content.render_explanation(_a_full_explanation()).replace(
            'xmlns="http://www.w3.org/1998/Math/MathML"', ""
        )
        for marker in (
            "<script",
            "<link",
            "<style",
            "src=",
            "@import",
            "url(",
            "//",
            "http://",
            "https://",
        ):
            assert marker not in out, (marker, out)

    def test_rendering_a_full_explanation_opens_no_connection(self, no_network):
        assert content.render_explanation(_a_full_explanation()) != ""


class TestThePackagingContract:
    """Rendering's libraries are an extra, and this is where that is stated.

    `test_import_hygiene.py` owns the other half — the domain imports only the
    standard library, and `dependencies == []`. That test is what *forces* the
    extra; this one is what stops the four libraries from quietly migrating
    into the base dependency list and taking the domain's independence with
    them.
    """

    def test_the_rendering_libraries_are_declared_as_an_optional_extra(self):
        extras = _pyproject()["project"]["optional-dependencies"]["rendering"]
        declared = {spec.split(">")[0].split("=")[0].strip().lower() for spec in extras}
        assert declared == {"nh3", "markdown-it-py", "pygments", "latex2mathml"}

    def test_they_are_not_base_dependencies(self):
        assert _pyproject()["project"]["dependencies"] == []


def _pyproject() -> dict:
    """Parsed as bytes — TOML is defined to be UTF-8, so the locale codec has
    no business in it (see `tests/test_encoding_hygiene.py`)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib
    root = pathlib.Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _a_full_explanation() -> tuple[types.Segment, ...]:
    return (
        types.TextSegment(
            "A **right** triangle obeys `pythagoras`:\n\n"
            "- the legs are `a` and `b`\n"
            "- the hypotenuse is `c`\n\n"
            "```python\ndef hypotenuse(a, b):\n    return (a * a + b * b) ** 0.5\n```\n"
        ),
        types.MathSegment(mathml.latex_to_mathml(PYTHAGORAS, display=True)),
        types.BlankSegment("b1"),
        types.TextSegment("which is *why* it works."),
    )


_TAG = re.compile(r"<([A-Za-z][\w:-]*)((?:\s[^<>]*)?)/?>")


def _tag_names(html: str) -> set[str]:
    return {match.group(1).lower() for match in _TAG.finditer(html)}


def _attribute_names(html: str) -> set[str]:
    names: set[str] = set()
    for match in _TAG.finditer(html):
        names.update(re.findall(r"[\s\"']([A-Za-z:-]+)=", match.group(2)))
    return names


_ANY_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")


def _text_of(html: str) -> str:
    """The document's text nodes, for asserting an element is empty."""
    return _ANY_TAG.sub("", html)
