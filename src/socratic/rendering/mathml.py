"""LaTeX to MathML, server-side and in-process (ADR-0012, acceptance 31).

Browsers render MathML natively, so this is the one conversion that needs no
client script, no web font and no CDN — the property ADR-0012 chose it for,
since the iframe's capabilities are deployment-dependent and not ours.

**Where this sits.** `domain.types.MathSegment` already holds MathML by its own
docstring ("converted from LaTeX server-side. Opaque to the domain"), so this
module runs *upstream* of the segment array, not inside the renderer: authoring
calls it to build a `MathSegment` from the model's LaTeX, and the grading path
calls it when a blank resolves to a formula. `content.render_explanation`
treats a `MathSegment` as MathML that still has to clear the allowlist — a
segment could have been built by something other than this function.

The output is sanitised here as well as there. `latex2mathml` implements
`\\href`, so `\\href{javascript:alert(1)}{x}` really does emit an `href` on an
`mrow`; a caller who takes this function's word for it and skips the renderer
would ship that.
"""

import latex2mathml.converter
import latex2mathml.exceptions

from socratic.rendering import sanitiser

_LIBRARY_ERRORS = tuple(
    member
    for member in vars(latex2mathml.exceptions).values()
    if isinstance(member, type) and issubclass(member, Exception)
)
"""`latex2mathml`'s parse failures, collected rather than listed.

The library gives its twelve exceptions no common base class, and a version
bump that adds a thirteenth should not turn a malformed formula into a crash
in the caller.
"""

_CONVERSION_ERRORS = _LIBRARY_ERRORS + (IndexError,)
"""Plus `IndexError`, which the tokenizer leaks on some inputs.

`\\genfrac{}{}{}{}{}{}` raises `IndexError: tuple index out of range` rather
than any declared exception. Model-authored LaTeX is untrusted input like any
other, and a renderer that raises an unrelated builtin on it is a renderer that
500s on one bad formula.
"""


class LatexConversionError(ValueError):
    """The LaTeX would not parse. Raised in place of the library's twelve."""


def latex_to_mathml(latex: str, *, display: bool = False) -> str:
    """Convert a LaTeX formula to sanitised MathML.

    Args:
      latex: The formula body, without delimiters. Untrusted.
      display: Render as a block-level formula rather than inline.

    Returns:
      A `<math>` element, already through `sanitiser.sanitise`.

    Raises:
      LatexConversionError: The formula did not parse.
    """
    try:
        converted = latex2mathml.converter.convert(
            latex, display="block" if display else "inline"
        )
    except _CONVERSION_ERRORS as error:
        raise LatexConversionError(f"could not convert LaTeX: {latex!r}") from error
    return sanitiser.sanitise(converted)
