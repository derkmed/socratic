"""The overlay document, built from the wire body and nothing else.

`render_overlay` takes the JSON body `/quizzes` already returns — not a `Quiz`.
That is the load-bearing choice in this module: `payloads.quiz_body` is the
whitelist that keeps the answer key in the service process (D3, ADR-0003), and
rendering *downstream* of it means the key has no field here it could have
arrived in. A renderer handed a `Quiz` would hold the key and be one attribute
away from shipping it.

Two other rules the tests hold this module to:

* **The input control comes from `render_hint`.** A lookup table, not a chain of
  conditions, and certainly not a mode check — an `if mode ==` outside the
  registry is a bug (CONTEXT: ModeRegistry), and a mode check in the view is
  that bug moved across a network boundary where the scan cannot see it.
* **Every panel ships present and hidden.** The client toggles visibility; it
  never builds markup out of model-authored data. That keeps ADR-0012's "the
  iframe renders no model-authored text itself" true of the client too — every
  fragment it inserts was sanitised in this process before it left.

Nothing here re-sanitises: `explanation_html`, `recap_html`, `text_html` and
`answer_html` all left `payloads.html_of`, `payloads.label_html` or
`content.render_explanation` already sanitised, and a second pass over the
finished document would strip the buttons, the inputs and the script this
module exists to add.
"""

import html
import pathlib
from typing import Any, Callable, Mapping, Sequence

from socratic.rendering import markdown

_HERE = pathlib.Path(__file__).resolve().parent

OPTION_BANK = "option_bank"
TEXT_INPUT = "text_input"
"""The two `RenderHint` values that exist today (`domain/modes.py`).

Named as strings rather than imported as the enum because this module renders
the *wire* body, where a hint is the string the registry put on it. Importing
the enum would let the renderer be right about a hint the service never sent.
"""


def _asset(name: str) -> str:
    """One of the package's inlined assets.

    Read as UTF-8 explicitly: the machine's locale codec has no business
    deciding what the learner's stylesheet says, and the prototype targets Mac,
    Windows and Linux alike (#25).
    """
    return (_HERE / name).read_text(encoding="utf-8")


def _attr(value: object) -> str:
    """One attribute value, escaped.

    Quotes included. The capability token is opaque bytes as far as this module
    is concerned, and a `"` in any interpolated value would otherwise close the
    attribute and open the document.
    """
    return html.escape(str(value), quote=True)


def _option_bank(blank: Mapping[str, Any]) -> str:
    """One button per option, each carrying the option's rendered text.

    Raises:
      ValueError: The blank declares no options. An option bank with nothing to
        click is a blank the learner cannot answer, and the conditional
        validator already refuses one upstream (acceptance 27) — so reaching
        here means the wire format changed, not that a learner got unlucky.
    """
    options: Sequence[Mapping[str, Any]] = blank.get("options") or ()
    if not options:
        raise ValueError(
            f"blank {blank.get('blank_id')!r} renders an option bank but "
            "declares no options"
        )
    buttons = "".join(
        f'<button type="button" class="socratic-option" '
        f'data-option-id="{_attr(option["option_id"])}">'
        f'{option["text_html"]}</button>'
        for option in options
    )
    return f'<div class="socratic-options">{buttons}</div>'


def _text_input(blank: Mapping[str, Any]) -> str:
    """A free-text field and its submit button.

    The label is the overlay's own words, not the model's: an Advanced blank
    carries a rubric and no prompt, and the rubric never leaves the service.
    """
    field_id = f"socratic-answer-{_attr(blank['blank_id'])}"
    return (
        f'<label class="socratic-answer-label" for="{field_id}">'
        "Your answer</label>"
        f'<input type="text" id="{field_id}" class="socratic-answer" '
        'autocomplete="off" spellcheck="false">'
        '<button type="button" class="socratic-submit">Submit</button>'
    )


_CONTROLS: Mapping[str, Callable[[Mapping[str, Any]], str]] = {
    OPTION_BANK: _option_bank,
    TEXT_INPUT: _text_input,
}
"""Render hint to control. The only dispatch in the view, and it is over the
hint the mode's policy chose — never over the mode."""


def _control(blank: Mapping[str, Any], ordinal: int, of: int) -> str:
    """One blank's control block, hidden until the client activates it.

    Raises:
      ValueError: The render hint is one this module does not know. Refused
        loudly rather than skipped, exactly as `content.render_segment` refuses
        a segment kind it has never heard of: a blank drawn without a control
        is a quiz that cannot be finished, and silence would ship it.
    """
    hint = blank.get("render_hint")
    build = _CONTROLS.get(str(hint))
    if build is None:
        raise ValueError(
            f"no control for render hint {hint!r} on blank "
            f"{blank.get('blank_id')!r}"
        )
    return (
        f'<section class="socratic-control" '
        f'data-blank-id="{_attr(blank["blank_id"])}" '
        f'data-render-hint="{_attr(hint)}" hidden>'
        f'<p class="socratic-control-prompt">Blank {ordinal} of {of}</p>'
        f"{build(blank)}"
        "</section>"
    )


def _require_absolute(service_base_url: str) -> str:
    """The service's browser-facing origin, checked and trimmed.

    Raises:
      ValueError: The URL is not absolute. A `srcdoc` document inherits its
        parent's base URL, so a relative path resolves against Open WebUI's
        origin rather than the service's — every answer would leave for the
        wrong host, and the learner would see a network error instead of a
        verdict (D15, ADR-0015).
    """
    trimmed = service_base_url.rstrip("/")
    if not trimmed.startswith(("http://", "https://")):
        raise ValueError(
            "the overlay needs the service's absolute browser-facing URL; "
            f"got {service_base_url!r}"
        )
    return trimmed


def _panels() -> str:
    """The verdict, probe and rating panels: present, empty and hidden.

    All three are authored here rather than built by the client, so that the
    only strings the client ever inserts are fragments this process already
    sanitised.
    """
    return (
        '<div class="socratic-verdict" role="status" aria-live="polite" hidden>'
        '<p class="socratic-verdict-line"></p>'
        '<div class="socratic-feedback"></div>'
        '<div class="socratic-tutor-line"></div>'
        '<p class="socratic-reveal" hidden></p>'
        '<p class="socratic-pending socratic-pedagogy-pending" hidden>'
        "The tutor is still writing this part — your answer counted all the "
        "same.</p>"
        "</div>"
        '<section class="socratic-probe" aria-label="Self-explanation" hidden>'
        '<div class="socratic-probe-question"></div>'
        '<label class="socratic-probe-label" for="socratic-probe-answer">'
        "How did you arrive at that?</label>"
        '<textarea id="socratic-probe-answer" class="socratic-probe-answer" '
        'rows="3"></textarea>'
        '<button type="button" class="socratic-probe-submit">Send</button>'
        '<button type="button" class="socratic-probe-dismiss">Not now'
        "</button>"
        "</section>"
        '<section class="socratic-rating" aria-label="Rate this quiz" hidden>'
        "<p>How useful was this quiz?</p>"
        + "".join(
            f'<button type="button" class="socratic-score" '
            f'data-score="{score}">{score}</button>'
            for score in range(1, 6)
        )
        + '<button type="button" class="socratic-rating-dismiss">No thanks'
        "</button>"
        "</section>"
    )


def _queued(topics: Sequence[str]) -> str:
    """The topics the learner raised and the tutor set aside.

    Listed, never answered — the single-topic-focus rule (CONTEXT: Queued
    topics). Nothing in this ticket acts on them; #15 owns displacement.
    """
    if not topics:
        return ""
    items = "".join(f"<li>{html.escape(topic)}</li>" for topic in topics)
    return (
        '<section class="socratic-queued">'
        "<p>Also asked, saved for later:</p>"
        f"<ul>{items}</ul>"
        "</section>"
    )


def _document(*, title: str, body: str, script: str = "") -> str:
    """The shared chrome. One document, no external reference of any kind."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>\n{markdown.highlight_css()}\n{_asset('overlay.css')}</style>\n"
        "</head>\n"
        f"<body>\n{body}\n{script}</body>\n"
        "</html>\n"
    )


def render_overlay(body: Mapping[str, Any], *, service_base_url: str) -> str:
    """Render a quiz's overlay document.

    Args:
      body: The `kind: "quiz"` author response — `payloads.quiz_body`'s output.
        Its HTML fields are already rendered and sanitised.
      service_base_url: The service's absolute browser-facing origin. The
        service's own configuration, never a caller's request field: a URL a
        caller could choose would point every learner's answers at whatever
        origin the caller named.

    Returns:
      One self-contained HTML document, for a `srcdoc` iframe on a default
      Open WebUI install.

    Raises:
      ValueError: The base URL is relative, a blank's render hint is unknown,
        or an option bank declares no options.
    """
    base = _require_absolute(service_base_url)
    blanks: Sequence[Mapping[str, Any]] = body.get("blanks") or ()
    recap_html = body.get("recap_html") or ""

    controls = "".join(
        _control(blank, ordinal, len(blanks))
        for ordinal, blank in enumerate(blanks, start=1)
    )
    recap = (
        f'<section class="socratic-recap" hidden>{recap_html}</section>'
        if recap_html
        else ""
    )

    page = (
        '<main class="socratic-overlay"'
        f' data-quiz-session-id="{_attr(body["quiz_session_id"])}"'
        f' data-capability-token="{_attr(body["capability_token"])}"'
        f' data-service-base-url="{_attr(base)}">\n'
        f'<h1 class="socratic-topic">{html.escape(str(body.get("topic", "")))}'
        "</h1>\n"
        f'<div class="socratic-explanation">{body["explanation_html"]}</div>\n'
        f'<div class="socratic-controls">{controls}</div>\n'
        f"{_panels()}\n"
        f"{recap}"
        f"{_queued(body.get('queued_topics') or ())}\n"
        "</main>"
    )

    script = (
        f"<script>\n{_asset('client.js')}\n</script>\n"
        "<script>SocraticQuiz.mount(document);</script>\n"
    )
    return _document(title=str(body.get("topic", "quiz")), body=page, script=script)


def render_direct_answer(body: Mapping[str, Any]) -> str:
    """Render the override branch (ADR-0004) as a document.

    A `direct_answer` created no session, so there is no capability token, no
    blank and nothing to submit — and therefore no client script. The learner
    gets the prose and the queued topics.

    Args:
      body: The `kind: "direct_answer"` author response.

    Returns:
      One self-contained HTML document.
    """
    page = (
        '<main class="socratic-overlay socratic-direct-answer">\n'
        f'<h1 class="socratic-topic">{html.escape(str(body.get("topic", "")))}'
        "</h1>\n"
        f'<div class="socratic-answer-prose">{body["answer_html"]}</div>\n'
        f"{_queued(body.get('queued_topics') or ())}\n"
        "</main>"
    )
    return _document(title=str(body.get("topic", "answer")), body=page)


def render_queued(body: Mapping[str, Any]) -> str:
    """Render the queued branch (#92, #15) as a document.

    The learner asked a second question with a quiz already open. The
    single-topic-focus rule parked it, and this document is what says so: the
    quiz still waiting for them, the question just saved, and the rest of the
    queue.

    **No script, no token, no control.** No capability token was minted for
    this branch — `payloads.queued_body` explains why one cannot be — so there
    is nothing here for a script to authorize with, and a control that could
    not reach the service would be a button that does nothing. The learner's
    live quiz is still the previous message in their chat, and still theirs.
    The "start this instead" control belongs on *that* document, and is its own
    issue.

    Everything interpolated here is escaped rather than rendered: the topic is
    plain text, as it is on `quiz_body`, and the queued topics are the
    learner's own typed words, which have been through no sanitiser.

    Args:
      body: The `kind: "queued"` author response.

    Returns:
      One self-contained HTML document.
    """
    topic = html.escape(str(body.get("topic", "")))
    inquiry = html.escape(str(body.get("inquiry", "")))
    page = (
        '<main class="socratic-overlay socratic-queued-notice">\n'
        '<h1 class="socratic-topic">Saved for later</h1>\n'
        '<div class="socratic-answer-prose">'
        f"<p>You asked: <em>{inquiry}</em></p>"
        f"<p>It is waiting on the quiz you have open — <strong>{topic}"
        "</strong> — which is the message just above this one. Finish that "
        "one and this question is still here.</p>"
        "</div>\n"
        f"{_queued(body.get('queued_topics') or ())}\n"
        "</main>"
    )
    return _document(title="saved for later", body=page)
