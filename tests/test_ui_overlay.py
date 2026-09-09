"""The overlay document (issue #13, `docs/specs/quiz-ui.md`).

`render_overlay` is a pure function over the **wire body** `/quizzes` already
returns, so everything the learner's browser receives is assertable here with no
browser: the blanks are inline where the segment walk put them, each has exactly
one control block chosen by its `render_hint`, the document fetches nothing, and
the answer key has no field it could have arrived in.

That last one is why the input is the wire body rather than a `Quiz`.
`payloads.quiz_body` is the whitelist that keeps the key in the service
process, and rendering downstream of it makes "the overlay cannot leak the key"
structural rather than a property of this module's care.
"""

import re

import pytest

pytest.importorskip(
    "nh3",
    reason="the overlay inlines the renderer's highlight CSS, so its tests "
    "need the optional `rendering` extra installed",
)

from socratic.rendering import content  # noqa: E402
from socratic.rendering import mathml  # noqa: E402
from socratic.ui import overlay  # noqa: E402

BASE_URL = "http://localhost:8080"
TOKEN = "eyJzIjoiMDFK-signature"
SESSION = "01J000000000000000000000AA"


def option_bank_blank(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "mode": "novice",
        "render_hint": "option_bank",
        "options": [
            # Inline, the way `payloads.label_html` renders a label (#98): a
            # `<p>` here would be a block box in the button, and a block box in
            # the inline placeholder once the client copies it there.
            {"option_id": "o1", "text_html": "entropy"},
            {"option_id": "o2", "text_html": "<em>enthalpy</em>"},
        ],
    }


def text_input_blank(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "mode": "advanced",
        "render_hint": "text_input",
        "options": None,
    }


def quiz_body(*blanks: dict, recap_html: str = "<p>Entropy never falls.</p>") -> dict:
    """The author response for a quiz whose explanation names every blank."""
    rendered = ["<p>Heat flows because </p>"]
    for blank in blanks:
        rendered.append(
            f'<span class="socratic-blank" '
            f'data-blank-id="{blank["blank_id"]}"></span>'
        )
        rendered.append("<p> rises.</p>")

    return {
        "kind": "quiz",
        "quiz_session_id": SESSION,
        "capability_token": TOKEN,
        "topic": "the second law of thermodynamics",
        "explanation_html": "".join(rendered),
        "recap_html": recap_html,
        "blanks": list(blanks),
        "queued_topics": ["the third law"],
    }


def render(*blanks: dict, **kwargs) -> str:
    body = quiz_body(*(blanks or (option_bank_blank(),)), **kwargs)
    return overlay.render_overlay(body, service_base_url=BASE_URL)


def _order_of(document: str, pattern: str) -> list[str]:
    return re.findall(pattern, document)


class TestTheDocument:
    def test_it_is_a_whole_document_declaring_utf8(self):
        """Rendered MathML is non-ASCII (#53), and a `srcdoc` document that
        does not declare its charset renders every formula as mojibake."""
        document = render()

        assert document.lstrip().lower().startswith("<!doctype html>")
        assert '<meta charset="utf-8">' in document

    def test_the_topic_is_the_document_title_and_is_escaped(self):
        body = quiz_body(option_bank_blank())
        body["topic"] = 'entropy & "order" <script>'

        document = overlay.render_overlay(body, service_base_url=BASE_URL)

        assert "<script>entropy" not in document
        assert "&lt;script&gt;" in document
        assert "&amp;" in document

    def test_it_fetches_nothing(self):
        """ADR-0012's rule, asserted on the bytes: no CDN, no font, no
        stylesheet link. The highlight colours are inlined, and math is MathML
        the browser already knows how to draw."""
        document = render()

        assert "<link" not in document
        assert "@import" not in document
        assert "url(" not in document
        assert not re.search(r"<(?:script|img|iframe)[^>]*\bsrc=", document)

    def test_the_highlight_css_is_inlined(self):
        from socratic.rendering import markdown

        document = render()

        assert markdown.highlight_css() in document

    def test_the_client_script_is_inlined_and_mounted(self):
        document = render()

        assert "SocraticQuiz" in document
        assert "mount(document)" in document

    def test_every_document_reports_its_own_height(self):
        """#116: the frame is sized by the document or not at all.

        Open WebUI's `FullHeightIframe` reads `contentDocument.scrollHeight`
        first, which throws for a `srcdoc` frame sandboxed without
        `allow-same-origin` — every frame ours renders in. Its only other
        source is a `postMessage` from inside, so a document that does not
        send one renders at no height and looks like a failure.
        """
        for document in (
            render(),
            overlay.render_direct_answer(
                {
                    "kind": "direct_answer",
                    "topic": "chest pain",
                    "answer_html": "<p>Call emergency services now.</p>",
                    "queued_topics": [],
                }
            ),
            overlay.render_queued(
                {
                    "kind": "queued",
                    "inquiry": "What is enthalpy?",
                    "quiz_session_id": "01J000000000000000000000AA",
                    "topic": "the second law",
                    "queued_topics": ["What is enthalpy?"],
                }
            ),
        ):
            assert "iframe:height" in document
            assert "postMessage" in document

    def test_the_height_is_measured_from_the_content_not_the_document(self):
        """The naive measure feeds back.

        Once the parent sets the frame to height H, `documentElement`'s own
        `scrollHeight` *is* H, so reporting it re-reports the height we were
        just given and any body padding adds to it on every pass. Observed
        ratcheting without bound before this was written, so the measure is
        over the body's children, whose boxes do not grow when the frame does.
        """
        document = render()

        assert "documentElement.scrollHeight" not in document

    def test_hidden_beats_the_stylesheet(self):
        """Every panel ships present and hidden so the client only toggles
        visibility. A `display:` rule that outranked `hidden` would show all of
        them at once."""
        document = render()

        assert re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important",
                         document)


class TestTheSessionAndItsToken:
    def test_the_document_carries_the_session_the_token_and_the_service_url(self):
        document = render()

        assert f'data-quiz-session-id="{SESSION}"' in document
        assert f'data-capability-token="{TOKEN}"' in document
        assert f'data-service-base-url="{BASE_URL}"' in document

    def test_the_token_is_attribute_escaped(self):
        """The token is opaque bytes as far as this module is concerned, and a
        `"` in one would otherwise close the attribute and open the document."""
        body = quiz_body(option_bank_blank())
        body["capability_token"] = 'a"><script>alert(1)</script>'

        document = overlay.render_overlay(body, service_base_url=BASE_URL)

        assert "<script>alert(1)</script>" not in document
        assert "&quot;" in document

    def test_the_service_url_must_be_absolute(self):
        """A `srcdoc` document inherits its parent's base URL, so a relative
        path would resolve against Open WebUI's origin instead of ours — the
        answers would leave for the wrong host and the learner would see a
        network error, not a wrong answer."""
        with pytest.raises(ValueError):
            overlay.render_overlay(quiz_body(option_bank_blank()),
                                   service_base_url="/quiz")


class TestTheBlanks:
    def test_the_explanation_is_placed_verbatim_with_its_blanks_inline(self):
        document = render(option_bank_blank("b1"), option_bank_blank("b2"))

        assert quiz_body(
            option_bank_blank("b1"), option_bank_blank("b2")
        )["explanation_html"] in document

    def test_a_formula_survives_into_the_document(self):
        body = quiz_body(option_bank_blank())
        rendered = mathml.latex_to_mathml("a^2 + b^2 = c^2")
        body["explanation_html"] = body["explanation_html"] + rendered

        document = overlay.render_overlay(body, service_base_url=BASE_URL)

        assert rendered in document

    def test_every_blank_gets_exactly_one_control_block_in_declared_order(self):
        document = render(
            option_bank_blank("b1"), text_input_blank("b2"), option_bank_blank("b3")
        )

        controls = _order_of(
            document, r'class="socratic-control"[^>]*data-blank-id="(\w+)"'
        )
        assert controls == ["b1", "b2", "b3"]

    def test_the_placeholder_and_its_control_agree_on_the_blank_id(self):
        """The client pairs the two by this id; a control the walk cannot find
        a placeholder for is a blank the learner can never reach."""
        document = render(option_bank_blank("b1"), text_input_blank("b2"))

        placeholders = _order_of(
            document,
            rf'class="{content.BLANK_CLASS}" {re.escape("data-blank-id")}="(\w+)"',
        )
        controls = _order_of(
            document, r'class="socratic-control"[^>]*data-blank-id="(\w+)"'
        )
        assert placeholders == controls


class TestTheControlComesFromTheRenderHint:
    """The view never branches on mode; it reads the hint the registry put on
    the wire (CONTEXT: ModeRegistry, `quiz-service.md`)."""

    def test_an_option_bank_renders_one_button_per_option(self):
        document = render(option_bank_blank())

        assert _order_of(document, r'class="socratic-option"\s+data-option-id="(\w+)"') \
            == ["o1", "o2"]
        assert ">entropy</button>" in document
        assert "<em>enthalpy</em>" in document

    def test_a_text_input_renders_a_field_and_no_buttons_of_its_own(self):
        document = render(text_input_blank())

        assert 'class="socratic-answer"' in document
        assert 'class="socratic-option"' not in document

    def test_the_hint_travels_onto_the_control_so_the_client_need_not_re_derive_it(
        self,
    ):
        document = render(option_bank_blank("b1"), text_input_blank("b2"))

        assert _order_of(document, r'data-render-hint="(\w+)"') == [
            "option_bank",
            "text_input",
        ]

    def test_an_unknown_render_hint_is_refused(self):
        """A hint this module does not know is a wire-format change, and a
        blank rendered without a control is a blank the learner cannot answer.
        Refused loudly rather than skipped silently, exactly as
        `content.render_segment` refuses a segment kind it does not know."""
        blank = option_bank_blank()
        blank["render_hint"] = "voice_note"

        with pytest.raises(ValueError, match="voice_note"):
            overlay.render_overlay(
                quiz_body(blank), service_base_url=BASE_URL
            )

    def test_an_option_bank_with_no_options_is_refused(self):
        blank = option_bank_blank()
        blank["options"] = None

        with pytest.raises(ValueError):
            overlay.render_overlay(quiz_body(blank), service_base_url=BASE_URL)


class TestTheLatePedagogyWindow:
    """The skeleton call renders a playable quiz before the pedagogy payload
    lands (master spec acceptance 5, D11). The recap is a pedagogy field, so at
    render time it is routinely absent."""

    def test_an_absent_recap_renders_no_recap_section(self):
        document = render(recap_html="")

        assert '<section class="socratic-recap"' not in document

    def test_a_present_recap_is_placed(self):
        document = render(recap_html="<p>Entropy never falls.</p>")

        assert '<section class="socratic-recap"' in document
        assert "<p>Entropy never falls.</p>" in document

    def test_a_quiz_is_still_playable_with_the_recap_missing_entirely(self):
        body = quiz_body(option_bank_blank())
        del body["recap_html"]

        document = overlay.render_overlay(body, service_base_url=BASE_URL)

        assert 'class="socratic-control"' in document


class TestTheAnswerKeyIsNotHere:
    """The whitelist is upstream, in `payloads`; this asserts the overlay adds
    nothing back.

    Stated as sentinels smuggled onto the blank rather than as a scan for the
    word "correct": the document legitimately contains that word, because the
    client compares a verdict against it. What must not appear is a *value* —
    which is the leak, and which a word scan over a document carrying its own
    code cannot distinguish.
    """

    KEY_FIELDS = {
        "correct_option_id": "SENTINEL-KEY",
        "rubric": "SENTINEL-RUBRIC",
        "reinforcement": "SENTINEL-REINFORCEMENT",
        "hints": ["SENTINEL-HINT-1", "SENTINEL-HINT-2", "SENTINEL-HINT-3"],
        "probe_question": "SENTINEL-PROBE",
    }

    def test_a_key_field_on_the_wire_body_still_does_not_reach_the_document(self):
        blank = {**option_bank_blank("b1"), **self.KEY_FIELDS}

        document = overlay.render_overlay(
            quiz_body(blank), service_base_url=BASE_URL
        )

        assert "SENTINEL" not in document

    def test_no_option_is_marked_out_from_the_others(self):
        """The correct option's id is one of the two on the wire, so its
        absence cannot be asserted directly. What must be absent is anything
        that says which one it is."""
        document = render(option_bank_blank())

        buttons = re.findall(r"<button[^>]*socratic-option[^>]*>", document)
        assert len(buttons) == 2
        assert buttons[0].replace("o1", "OPT") == buttons[1].replace("o2", "OPT")


class TestTheDirectAnswerBranch:
    def test_the_prose_is_rendered_with_no_quiz_machinery(self):
        document = overlay.render_direct_answer(
            {
                "kind": "direct_answer",
                "topic": "chest pain",
                "answer_html": "<p>Call emergency services now.</p>",
                "queued_topics": ["how the heart's conduction system works"],
            }
        )

        assert "<p>Call emergency services now.</p>" in document
        assert '<meta charset="utf-8">' in document
        assert '<section class="socratic-control"' not in document
        assert "SocraticQuiz" not in document


class TestTheQueuedBranch:
    """The third document (#92).

    The Pipe returns `/overlays`' bytes verbatim, so a queued inquiry has to
    become a document here or it becomes a `KeyError` there.
    """

    def _queued(self, **overrides) -> str:
        body = {
            "kind": "queued",
            "inquiry": "What is enthalpy?",
            "quiz_session_id": "01J000000000000000000000AA",
            "topic": "the second law of thermodynamics",
            "queued_topics": ["the third law", "What is enthalpy?"],
        }
        body.update(overrides)
        return overlay.render_queued(body)

    def test_it_names_the_quiz_still_open_and_the_question_just_saved(self):
        document = self._queued()

        assert "the second law of thermodynamics" in document
        assert "What is enthalpy?" in document
        assert "the third law" in document
        assert '<meta charset="utf-8">' in document

    def test_it_carries_no_client_and_nothing_to_submit(self):
        """No token was minted for it, so there is nothing for a script to
        authorize with and no control that could spend one.

        The height reporter is the one script every document carries (#116)
        and is deliberately not covered here: it makes no request, reads no
        token and offers the learner nothing to press. The claim is that this
        document cannot *talk to the service*, which is asserted on the client,
        the controls and the two data attributes below rather than on the
        presence of a `<script>` tag.
        """
        document = self._queued()

        assert "SocraticQuiz" not in document
        assert "fetch(" not in document
        assert '<section class="socratic-control"' not in document
        assert "data-capability-token" not in document
        assert "data-service-base-url" not in document

    def test_the_session_id_is_not_written_into_the_document(self):
        """It is an identifier the *caller* was given, not something the
        learner's page needs: there is no request this document can make."""
        document = self._queued()

        assert "01J000000000000000000000AA" not in document

    def test_the_learners_own_words_are_escaped(self):
        document = self._queued(
            inquiry="<script>alert(1)</script>",
            queued_topics=["<script>alert(1)</script>"],
        )

        assert "<script>alert(1)</script>" not in document
        assert "&lt;script&gt;" in document
