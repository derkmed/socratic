"""The quiz service's HTTP API (issue #19, `docs/specs/quiz-service.md`).

The API is deliberately **not** a seam: every route is a translation over
`QuizAuthoring`, `QuizSession`, `TokenMinter` or `submit_rating`, all of which
are tested directly elsewhere. So nothing here re-asserts grading, laddering or
cadence. What is tested here is exactly what only exists once the routes exist:

* **Authorization**, which is a property of the wiring rather than of any
  domain object. In particular `TokenMinter.verify` takes `for_session` as an
  *optional* keyword, so a handler that forgets it accepts any live token for
  any session signed by the same secret. Every capability route is asserted to
  refuse a token minted for a different session — and to refuse it *before* any
  model call, which `RecordingModelClient(fail_if_called=True)` proves.
* **Rotation**, which the iframe depends on to stay alive (D15).
* **What crosses the wire.** The answer key lives in this process and the
  responses are built by whitelist, so a correct option id must not appear in
  any body except the sanctioned rung-three reveal (D3, ADR-0003).
* **CORS**, because the iframe is sandboxed without `allow-same-origin` and its
  requests arrive with `Origin: null` and are preflighted. This was measured in
  a browser before it was coded; these tests hold the server's half of it.

`TestClient` speaks to the ASGI app in-process. No socket is opened.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("fastapi", reason="the service extra is optional")
from fastapi.testclient import TestClient  # noqa: E402

from socratic.domain.authoring import QuizAuthoring  # noqa: E402
from socratic.domain.model_client import (  # noqa: E402
    ModelResponse,
    RecordingModelClient,
)
from socratic.domain import session as session_module  # noqa: E402
from socratic.domain.modes import DifficultyMode, GradingStrategy  # noqa: E402
from socratic.domain.prompting import CallType  # noqa: E402
from socratic.domain.records import Verdict  # noqa: E402
from socratic.domain.repositories import (  # noqa: E402
    InMemoryAttemptRepository,
    InMemoryRatingRepository,
)
from socratic.domain.session import QuizSession  # noqa: E402
from socratic.domain.tokens import TokenMinter  # noqa: E402
from socratic.service import payloads  # noqa: E402
from socratic.service.app import create_app  # noqa: E402
from socratic.service.deps import ServiceDependencies  # noqa: E402

LEARNER = "learner-ada"
SERVICE_TOKEN = "service-token-for-the-pipe"
SECRET = b"0123456789abcdef0123456789abcdef"
OTHER_SESSION = "01J000000000000000000000BB"

FROZEN_MILLIS = 1_760_000_000_000

CORRECT_OPTION_ID = "o1"
SENTINEL_HINT = "SENTINEL-HINT-MUST-NOT-SHIP"
SENTINEL_RUBRIC = "SENTINEL-RUBRIC-MUST-NOT-SHIP"


class Clock:
    """A millisecond clock the tests drive by hand, as in `test_tokens`."""

    def __init__(self, millis: int = FROZEN_MILLIS) -> None:
        self._millis = millis

    def __call__(self) -> int:
        return self._millis

    def advance(self, millis: int) -> None:
        self._millis += millis


# --- Canned model payloads ---------------------------------------------------


def novice_blank_payload(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "options": [
            {"option_id": CORRECT_OPTION_ID, "text": "entropy"},
            {"option_id": "o2", "text": "enthalpy"},
        ],
        "correct_option_id": CORRECT_OPTION_ID,
        "reinforcement": "Entropy is the one that never decreases.",
        "hints": [SENTINEL_HINT, "second hint", "third hint"],
        "rubric": None,
    }


def advanced_blank_payload(blank_id: str = "b1") -> dict:
    return {
        "blank_id": blank_id,
        "options": None,
        "correct_option_id": None,
        "reinforcement": None,
        "hints": None,
        "rubric": SENTINEL_RUBRIC,
    }


def quiz_payload(
    blank_builder=novice_blank_payload, blank_ids: tuple[str, ...] = ("b1",)
) -> dict:
    explanation: list[dict] = [
        {"type": "text", "text": "Heat flows from hot to cold because "}
    ]
    for blank_id in blank_ids:
        explanation.append({"type": "blank", "blank_id": blank_id})
        explanation.append({"type": "text", "text": " rises."})

    return {
        "type": "quiz",
        "topic": "the second law of thermodynamics",
        "explanation": explanation,
        "blanks": [blank_builder(blank_id) for blank_id in blank_ids],
        "recap": "Entropy never decreases in an isolated system.",
        "queued_topics": ["the third law"],
    }


ADVANCED_BLANK_IDS = ("b1", "b2", "b3", "b4")
"""The Advanced policy's `blank_range` admits 4-6, so an Advanced fixture that
reuses the single-blank Novice shape is refused by the validator, not by the
service. Four is the smallest quiz the mode allows."""


DIRECT_ANSWER_PAYLOAD = {
    "type": "direct_answer",
    "topic": "chest pain",
    "answer": "Call emergency services now. This is not a quiz.",
    "queued_topics": ["how the heart's conduction system works"],
}


def skeleton(payload) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(payload),
        message_id="msg_01AUTHORING",
        cache_read_input_tokens=512,
        cache_creation_input_tokens=0,
    )


# --- Wiring ------------------------------------------------------------------


class Harness:
    """The service, its collaborators, and the handles a test needs."""

    def __init__(self, payload=None, *, clock=None, ttl_millis=60_000):
        self.clock = clock or Clock()
        self.model = RecordingModelClient(
            {CallType.AUTHOR_SKELETON: [skeleton(payload or quiz_payload())]}
        )
        self.attempts = InMemoryAttemptRepository()
        self.ratings = InMemoryRatingRepository()
        self.minter = TokenMinter(SECRET, ttl_millis=ttl_millis, clock=self.clock)
        self.deps = ServiceDependencies(
            authoring=QuizAuthoring(
                model_client=self.model,
                attempts=self.attempts,
                clock=self.clock,
            ),
            session=QuizSession(
                model_client=self.model,
                attempts=self.attempts,
                clock=self.clock,
            ),
            attempts=self.attempts,
            ratings=self.ratings,
            minter=self.minter,
            service_token=SERVICE_TOKEN,
            clock=self.clock,
        )
        self.client = TestClient(create_app(self.deps))

    # -- helpers --

    def author(self, **overrides) -> dict:
        body = {
            "learner_id": LEARNER,
            "inquiry": "Why does heat flow?",
            "mode": DifficultyMode.NOVICE.value,
        }
        body.update(overrides)
        response = self.client.post(
            "/quizzes", json=body, headers={"X-Socratic-Service-Token": SERVICE_TOKEN}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def mint_for(self, session: str, learner: str = LEARNER) -> str:
        return self.minter.mint(session, learner)


@pytest.fixture
def harness():
    return Harness()


@pytest.fixture
def authored(harness):
    """An authored quiz, and a token good for its session."""
    body = harness.author()
    return harness, body, body["capability_token"]


CAPABILITY_ROUTES = (
    ("/answers", {"blank_id": "b1", "submitted": CORRECT_OPTION_ID}),
    ("/probes", {"blank_id": "b1", "self_explanation": "because disorder rises"}),
    ("/ratings", {"score": 4}),
)


# --- Authorization -----------------------------------------------------------


class TestTheServiceToken:
    def test_authoring_without_the_service_token_is_refused(self, harness):
        response = harness.client.post(
            "/quizzes",
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "novice"},
        )

        assert response.status_code == 401
        harness.model.assert_never_called()

    def test_authoring_with_the_wrong_service_token_is_refused(self, harness):
        response = harness.client.post(
            "/quizzes",
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "novice"},
            headers={"X-Socratic-Service-Token": "not-the-token"},
        )

        assert response.status_code == 401
        harness.model.assert_never_called()

    def test_a_capability_token_does_not_authorize_authoring(self, harness):
        """The two credentials are not interchangeable: a learner's iframe
        token must not be able to spend model budget authoring quizzes."""
        token = harness.mint_for(OTHER_SESSION)

        response = harness.client.post(
            "/quizzes",
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "novice"},
            headers={"X-Socratic-Service-Token": token},
        )

        assert response.status_code == 401
        harness.model.assert_never_called()


@pytest.mark.parametrize("route,body", CAPABILITY_ROUTES)
class TestTheCapabilityToken:
    """Every one of these runs against every capability route.

    That is the point: the `for_session` mistake is per-handler, so a test that
    covered one route would not catch it in the next one written.
    """

    def _post(self, harness, route, body, session, **kwargs):
        return harness.client.post(route, json={**body, "quiz_session_id": session}, **kwargs)

    def test_a_missing_token_is_refused(self, authored, route, body):
        harness, quiz, _ = authored

        response = self._post(harness, route, body, quiz["quiz_session_id"])

        assert response.status_code == 401

    def test_a_malformed_token_is_refused(self, authored, route, body):
        harness, quiz, _ = authored

        response = self._post(
            harness,
            route,
            body,
            quiz["quiz_session_id"],
            headers={"X-Socratic-Token": "not.a.token"},
        )

        assert response.status_code == 401

    def test_a_token_signed_by_another_secret_is_refused(self, authored, route, body):
        harness, quiz, _ = authored
        forged = TokenMinter(
            b"fedcba9876543210fedcba9876543210", clock=harness.clock
        ).mint(quiz["quiz_session_id"], LEARNER)

        response = self._post(
            harness,
            route,
            body,
            quiz["quiz_session_id"],
            headers={"X-Socratic-Token": forged},
        )

        assert response.status_code == 401

    def test_an_expired_token_is_refused(self, authored, route, body):
        harness, quiz, token = authored
        harness.clock.advance(60_001)

        response = self._post(
            harness,
            route,
            body,
            quiz["quiz_session_id"],
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 401

    def test_a_token_for_another_session_is_refused(self, authored, route, body):
        """The `for_session` keyword is optional on `TokenMinter.verify`, so a
        handler that forgets it accepts any live token for any session signed
        by the same secret (#18). This is that test, on every route."""
        harness, quiz, _ = authored
        other = harness.mint_for(OTHER_SESSION)

        response = self._post(
            harness,
            route,
            body,
            quiz["quiz_session_id"],
            headers={"X-Socratic-Token": other},
        )

        assert response.status_code == 401

    def test_rejection_happens_before_any_model_call(self, authored, route, body):
        harness, quiz, _ = authored
        harness.model.calls_of(CallType.GRADE_ANSWER)  # nothing yet

        self._post(
            harness,
            route,
            body,
            quiz["quiz_session_id"],
            headers={"X-Socratic-Token": "not.a.token"},
        )

        assert harness.model.call_count(CallType.GRADE_ANSWER) == 0
        assert harness.model.call_count(CallType.GRADE_PROBE) == 0


# --- Rotation ----------------------------------------------------------------


class TestRotation:
    def test_a_graded_answer_returns_a_fresh_token(self, authored):
        harness, quiz, token = authored

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 200, response.text
        assert response.json()["capability_token"] != token

    def test_the_superseded_token_stops_working(self, authored):
        """Rotation is supersession, not addition — otherwise the exposure
        window is the life of the quiz rather than minutes (#18, D15)."""
        harness, quiz, token = authored

        harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": token},
        )
        again = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 4},
            headers={"X-Socratic-Token": token},
        )

        assert again.status_code == 401

    def test_a_rating_is_not_a_grading_response_and_rotates_nothing(self, authored):
        harness, quiz, token = authored

        response = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 4},
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 200, response.text
        assert "capability_token" not in response.json()


# --- What crosses the wire ---------------------------------------------------


class TestTheAnswerKeyStaysHere:
    def test_the_authored_quiz_names_no_correct_option(self, harness):
        """The correct option's *id* is one of the options, so its absence
        cannot be asserted directly. What must be absent is any field that
        says which one it is — a `correct_option_id` key, an `is_correct`
        flag, anything. So: the word does not occur in the body at all."""
        body = harness.author()

        assert "correct" not in _blob(body).lower()

    def test_the_authored_quiz_carries_no_hints_or_reinforcement(self, harness):
        body = harness.author()

        assert SENTINEL_HINT not in _blob(body)
        assert "never decreases" not in _blob(body["blanks"])

    def test_an_advanced_quiz_carries_no_rubric(self):
        harness = Harness(
            quiz_payload(
                blank_builder=advanced_blank_payload, blank_ids=ADVANCED_BLANK_IDS
            )
        )

        body = harness.author(mode=DifficultyMode.ADVANCED.value)

        assert SENTINEL_RUBRIC not in _blob(body)

    def test_the_options_still_reach_the_learner_by_id_and_text(self, harness):
        body = harness.author()

        options = body["blanks"][0]["options"]
        assert [option["option_id"] for option in options] == ["o1", "o2"]
        assert "entropy" in options[0]["text_html"]

    def test_a_blank_carries_its_render_hint_so_the_ui_never_branches_on_mode(
        self, harness
    ):
        body = harness.author()

        assert body["blanks"][0]["render_hint"] == "option_bank"


def _blob(payload) -> str:
    return json.dumps(payload)


# --- CORS --------------------------------------------------------------------


class TestCors:
    """The iframe's opaque origin sends `Origin: null` and preflights any
    request carrying a JSON content type or the token header. Measured in
    Chrome and Edge before this was written; this is the server's half."""

    @pytest.mark.parametrize("route", ["/quizzes", "/answers", "/probes", "/ratings"])
    def test_preflight_is_answered_for_a_null_origin(self, harness, route):
        response = harness.client.options(
            route,
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-socratic-token",
            },
        )

        assert response.status_code in (200, 204)
        assert response.headers["access-control-allow-origin"] == "*"
        allowed = response.headers["access-control-allow-headers"].lower()
        assert "x-socratic-token" in allowed
        assert "content-type" in allowed

    def test_credentials_are_never_allowed(self, harness):
        """`Access-Control-Allow-Credentials` is illegal beside `*` and
        meaningless from an opaque origin — the token is the whole story."""
        response = harness.client.options(
            "/answers",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-socratic-token",
            },
        )

        assert "access-control-allow-credentials" not in response.headers


# --- The routes themselves ---------------------------------------------------


class TestAuthoring:
    def test_a_quiz_comes_back_rendered_with_its_session_and_token(self, harness):
        body = harness.author()

        assert body["kind"] == "quiz"
        assert body["topic"] == "the second law of thermodynamics"
        assert 'data-blank-id="b1"' in body["explanation_html"]
        assert harness.minter.verify(
            body["capability_token"], for_session=body["quiz_session_id"]
        )

    def test_a_direct_answer_comes_back_with_no_session_and_no_token(self):
        harness = Harness(DIRECT_ANSWER_PAYLOAD)

        body = harness.author()

        assert body["kind"] == "direct_answer"
        assert "emergency services" in body["answer_html"]
        assert body.get("capability_token") is None

    def test_the_response_declares_utf8(self, harness):
        """Rendered MathML is non-ASCII (#53); a response that does not say so
        breaks every formula."""
        response = harness.client.post(
            "/quizzes",
            json={
                "learner_id": LEARNER,
                "inquiry": "Why does heat flow?",
                "mode": "novice",
            },
            headers={"X-Socratic-Service-Token": SERVICE_TOKEN},
        )

        assert "utf-8" in response.headers["content-type"].lower()


class TestSubmittingAnAnswer:
    def test_a_correct_novice_answer_is_graded_with_no_model_call(self, authored):
        harness, quiz, token = authored

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": token},
        )

        body = response.json()
        assert body["verdict"] == "correct"
        assert body["blank_resolved"] is True
        assert harness.model.call_count(CallType.GRADE_ANSWER) == 0

    def test_the_iframe_never_names_an_attempt(self, authored):
        """The attempt is resolved from the token's own learner claim and the
        addressed session, so there is no field in which to name someone
        else's attempt."""
        harness, quiz, token = authored

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
                "attempt_id": "01J000000000000000000000XX",
                "learner_id": "someone-else",
            },
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 422


class TestRating:
    def test_a_score_is_stored_against_the_attempt(self, authored):
        harness, quiz, token = authored

        response = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 4},
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 200, response.text
        attempt = harness.attempts.get_by_session(LEARNER, quiz["quiz_session_id"])
        stored = harness.ratings.get(attempt.attempt_id)
        assert stored is not None and stored.score == 4

    def test_a_score_outside_one_to_five_is_refused(self, authored):
        harness, quiz, token = authored

        response = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 9},
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 422

    def test_a_second_rating_is_a_conflict(self, authored):
        harness, quiz, token = authored

        first = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 4},
            headers={"X-Socratic-Token": token},
        )
        assert first.status_code == 200, first.text

        second = harness.client.post(
            "/ratings",
            json={"quiz_session_id": quiz["quiz_session_id"], "score": 5},
            headers={"X-Socratic-Token": token},
        )

        assert second.status_code == 409


class TestTheAdvancedTutorLine:
    """The reactive tutor line rides the grading response (D13, ADR-0013).

    `Submission.model_grading.tutor_line` has existed since #8 and is tested in
    the domain, but until #13 it had no field to travel in — so the overlay had
    no way to show the one piece of feedback Advanced has. It is rendered like
    every other string that reaches a browser.

    Driven against `payloads.submission_body` rather than through `/answers`,
    because an Advanced answer cannot currently be submitted through the
    service at all: `/quizzes` hands `QuizAuthoring` the mode as a plain
    string, so `Quiz.mode` is a `str` and segment 2's `quiz.mode.value` raises.
    That is a bug in the route, not in this field, and it is filed rather than
    fixed here.
    """

    def _submission(self, model_grading):
        return session_module.Submission(
            verdict=Verdict.CORRECT,
            graded_by=GradingStrategy.MODEL_GRADED,
            hint_rung_shown=None,
            feedback=None,
            revealed_option_id=None,
            blank_resolved=True,
            attempt_sealed=False,
            model_grading=model_grading,
        )

    def test_the_tutor_line_rides_the_grading_response(self):
        body = payloads.submission_body(
            self._submission(
                session_module.ModelGrading(
                    tutor_line="You reached for the macro picture.",
                    probe_question=None,
                )
            ),
            capability_token="rotated",
        )

        assert "You reached for the macro picture." in body["tutor_line_html"]

    def test_a_model_graded_answer_without_one_carries_null(self):
        body = payloads.submission_body(
            self._submission(
                session_module.ModelGrading(tutor_line=None, probe_question=None)
            ),
            capability_token="rotated",
        )

        assert body["tutor_line_html"] is None

    def test_the_deterministic_path_has_no_tutor_line_at_all(self):
        """Not merely null: the deterministic strategy never builds a
        `ModelGrading`, so there is nothing for the field to be filled from."""
        body = payloads.submission_body(
            self._submission(None), capability_token="rotated"
        )

        assert body["tutor_line_html"] is None

    def test_the_field_is_on_the_wire_for_a_novice_answer(self, authored):
        """The overlay reads this key unconditionally, so it has to be present
        on the path that never fills it."""
        harness, quiz, token = authored

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": token},
        )

        assert response.json()["tutor_line_html"] is None
        harness.model.assert_never_called(CallType.GRADE_ANSWER)


class TestTheOverlayRoute:
    """The service serves the document; the Pipe only forwards it.

    ADR-0015: the Pipe "returns the rendered HTML the service produced". It has
    no other option — it is pasted Python in the Open WebUI container and cannot
    import `socratic.ui` at all. So the overlay is a route here, authorized by
    the same service token as `/quizzes`, and the browser-facing URL is this
    process's configuration rather than a field the caller can choose.
    """

    def _overlay(self, harness, **overrides):
        body = {
            "learner_id": LEARNER,
            "inquiry": "Why does heat flow?",
            "mode": DifficultyMode.NOVICE.value,
        }
        body.update(overrides)
        return harness.client.post(
            "/overlays",
            json=body,
            headers={"X-Socratic-Service-Token": SERVICE_TOKEN},
        )

    def test_it_returns_a_document_declaring_utf8(self, harness):
        response = self._overlay(harness)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html")
        assert "utf-8" in response.headers["content-type"].lower()
        assert response.text.lstrip().lower().startswith("<!doctype html>")

    def test_the_document_carries_the_quiz_and_a_token_good_for_its_session(
        self, harness
    ):
        document = self._overlay(harness).text

        assert 'data-blank-id="b1"' in document
        assert "entropy" in document

        session = re.search(r'data-quiz-session-id="([^"]+)"', document).group(1)
        token = re.search(r'data-capability-token="([^"]+)"', document).group(1)
        assert harness.minter.verify(token, for_session=session)

    def test_the_browser_facing_url_comes_from_configuration(self, harness):
        document = self._overlay(harness).text

        assert f'data-service-base-url="{harness.deps.public_base_url}"' in document

    def test_a_caller_cannot_choose_the_url_the_answers_go_to(self, harness):
        """A URL from the request body would let whoever holds the service
        token point every learner's answers at another origin."""
        response = self._overlay(harness, service_base_url="http://evil.example")

        assert response.status_code == 422

    def test_without_the_service_token_it_is_refused_before_any_model_call(
        self, harness
    ):
        response = harness.client.post(
            "/overlays",
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "novice"},
        )

        assert response.status_code == 401
        harness.model.assert_never_called()

    def test_a_capability_token_does_not_authorize_it(self, harness):
        response = harness.client.post(
            "/overlays",
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "novice"},
            headers={"X-Socratic-Service-Token": harness.mint_for(OTHER_SESSION)},
        )

        assert response.status_code == 401
        harness.model.assert_never_called()

    def test_a_direct_answer_is_prose_with_no_token(self):
        """The override branch created no session, so there is nothing for a
        token to be scoped to and nothing to submit (ADR-0004)."""
        harness = Harness(DIRECT_ANSWER_PAYLOAD)

        document = self._overlay(harness).text

        assert "emergency services" in document
        assert "data-capability-token" not in document
        assert "SocraticQuiz" not in document

    def test_the_answer_key_does_not_reach_the_document(self, harness):
        """The overlay renders downstream of `payloads.quiz_body`, so this is
        the end-to-end form of the whitelist: the fixture's hints and its
        reinforcement are in this process and neither may leave it."""
        document = self._overlay(harness).text

        assert SENTINEL_HINT not in document
        assert "Entropy is the one that never decreases" not in document

    def test_the_recap_ships_hidden_rather_than_withheld(self, harness):
        """The recap is pedagogy, not key: the learner reads it once the attempt
        seals. It travels in the document and starts hidden, so showing it costs
        no request (CONTEXT: Sealed)."""
        document = self._overlay(harness).text

        assert '<section class="socratic-recap" hidden>' in document
        assert "never decreases in an isolated system" in document


# --- Configuration -----------------------------------------------------------


class TestServiceConfig:
    """Startup refuses a weak or missing secret rather than serving with one.

    The failure a prototype is most likely to ship with is a short secret in a
    compose file, which looks like configuration and is not (#18).
    """

    ENV = {
        "SOCRATIC_TOKEN_SECRET": "0123456789abcdef0123456789abcdef",
        "SOCRATIC_SERVICE_TOKEN": "service-token-for-the-pipe",
    }

    def test_a_well_formed_environment_is_accepted(self):
        from socratic.service.config import DEFAULT_TTL_MILLIS, ServiceConfig

        config = ServiceConfig.from_env(dict(self.ENV))

        assert config.service_token == "service-token-for-the-pipe"
        assert config.ttl_millis == DEFAULT_TTL_MILLIS

    @pytest.mark.parametrize(
        "override",
        [
            {"SOCRATIC_TOKEN_SECRET": "too-short"},
            {"SOCRATIC_TOKEN_SECRET": ""},
            {"SOCRATIC_SERVICE_TOKEN": ""},
            {"SOCRATIC_TOKEN_TTL_MILLIS": "0"},
            {"SOCRATIC_TOKEN_TTL_MILLIS": "not-a-number"},
        ],
    )
    def test_a_bad_environment_is_refused_at_startup(self, override):
        from socratic.service.config import ConfigError, ServiceConfig

        env = {**self.ENV, **override}

        with pytest.raises(ConfigError):
            ServiceConfig.from_env(env)

    def test_the_public_url_defaults_to_the_published_compose_port(self):
        """The overlay needs the service's *browser-facing* origin, which is
        not the compose DNS name the Pipe uses. `compose.yaml` publishes 8080
        to the host, so that is the default."""
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(dict(self.ENV))

        assert config.public_base_url == "http://localhost:8080"

    def test_the_public_url_is_read_from_the_environment(self):
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(
            {**self.ENV, "SOCRATIC_PUBLIC_URL": "https://quiz.example:9443/"}
        )

        assert config.public_base_url == "https://quiz.example:9443"

    def test_a_relative_public_url_is_refused_at_startup(self):
        """A `srcdoc` document inherits its parent's base URL, so a relative
        one would send every answer to Open WebUI instead of here — a failure
        that shows up as a network error in a demo, not as a bad config."""
        from socratic.service.config import ConfigError, ServiceConfig

        with pytest.raises(ConfigError):
            ServiceConfig.from_env({**self.ENV, "SOCRATIC_PUBLIC_URL": "/quiz"})

    def test_the_secret_is_never_rendered(self):
        """A traceback or a debug log must not be how the secret escapes."""
        from socratic.service.config import ServiceConfig

        config = ServiceConfig.from_env(dict(self.ENV))

        assert "0123456789abcdef" not in repr(config)
        assert "redacted" in repr(config)
