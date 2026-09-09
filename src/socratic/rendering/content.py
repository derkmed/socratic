"""The walk over a quiz's explanation (ADR-0004, ADR-0012).

The entry point. `render_explanation` folds over the flat segment array one
element at a time and concatenates what each one renders to — it never builds a
string and then substitutes into it. That is not a stylistic preference:
ADR-0004 removed sentinels from the wire format because they reintroduce the
parsing ADR-0001 removed, and a renderer that put them back would quietly
restore the failure mode while the wire format stayed clean.

The same shape is what makes "a blank masks a whole formula, never a term
inside one" true here. A `MathSegment` renders opaquely; nothing descends into
it looking for a blank, and `domain.types` gives a blank nowhere to live inside
one anyway.

Each segment is sanitised as it is rendered, and the finished document is
sanitised again. The second pass is not distrust of the first — it is what
makes ADR-0012's "everything rendered passes through sanitisation" a property
of the bytes that leave this function, checkable on the finished document
rather than only at four boundaries.
"""

import html

from socratic.domain import types
from socratic.rendering import markdown
from socratic.rendering import sanitiser

BLANK_CLASS = "socratic-blank"
"""The hook the iframe binds an answer control to.

A neutral placeholder, deliberately: whether a blank is an option bank or a
text input is `ModePolicy.render_hint`'s decision, and it belongs where the
control is built (issue #13), not in a renderer that would have to branch on
mode to know.
"""


def _render_blank(blank_id: str) -> str:
    """A blank's placeholder element, carrying its id and no answer."""
    return (
        f'<span class="{BLANK_CLASS}" '
        f'{sanitiser.BLANK_ID_ATTRIBUTE}="{html.escape(blank_id, quote=True)}">'
        "</span>"
    )


def render_segment(segment: types.Segment) -> str:
    """Render one segment to sanitised HTML.

    Args:
      segment: One element of a quiz's explanation.

    Returns:
      An HTML fragment. Text is restricted Markdown, math is MathML, a blank
      is a placeholder element.

    Raises:
      TypeError: The value is not a segment. Refused rather than skipped — a
        segment kind the renderer does not know is a wire-format change, and
        dropping it silently would render an explanation with a hole in it.
    """
    if isinstance(segment, types.TextSegment):
        # `render_fragment`, not `render_markdown`: a text segment is a
        # fragment of a sentence with a blank beside it, and a block `<p>`
        # around it turns the blank into a paragraph break (issue #86). A
        # segment that really is block content still gets its blocks.
        return markdown.render_fragment(segment.text)
    if isinstance(segment, types.MathSegment):
        # Sanitised even though `mathml.latex_to_mathml` already did: the
        # field is a plain string on a domain dataclass, and the renderer
        # cannot know what built it.
        return sanitiser.sanitise(segment.mathml)
    if isinstance(segment, types.BlankSegment):
        return _render_blank(segment.blank_id)
    raise TypeError(f"not a segment: {type(segment).__name__}")


def render_explanation(explanation: tuple[types.Segment, ...]) -> str:
    """Render a quiz explanation to sanitised HTML.

    Args:
      explanation: The flat segment array, blanks resolved or not. Untrusted
        throughout — it is model-authored.

    Returns:
      An HTML fragment for the iframe. No script, no stylesheet link, no
      external reference of any kind: math is MathML, which browsers render
      natively, and the code colours are inlined from
      `markdown.highlight_css()` (ADR-0012).
    """
    return sanitiser.sanitise(
        "".join(render_segment(segment) for segment in explanation)
    )
