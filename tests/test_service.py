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

import dataclasses
import json
import logging
import re

import pytest

pytest.importorskip("fastapi", reason="the service extra is optional")
from fastapi.testclient import TestClient  # noqa: E402

from socratic.domain.authoring import QuizAuthoring  # noqa: E402
from socratic.domain.inquiry import InquiryIntake  # noqa: E402
from socratic.domain.model_client import (  # noqa: E402
    ModelResponse,
    RecordingModelClient,
)
from socratic.domain import session as session_module  # noqa: E402
from socratic.domain.modes import DifficultyMode, GradingStrategy  # noqa: E402
from socratic.domain.prompting import CallType  # noqa: E402
from socratic.domain.records import Verdict  # noqa: E402
from socratic.domain import types  # noqa: E402
from socratic.domain.registry import (  # noqa: E402
    NOVICE_POLICY,
    default_registry,
)
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

    def __init__(
        self,
        payload=None,
        *,
        clock=None,
        ttl_millis=60_000,
        payloads_=None,
        also=None,
    ):
        """`also` queues responses for the other call types, for the tests that
        follow a quiz past the one blocking call the rest of this file stops
        at."""
        self.clock = clock or Clock()
        queued = payloads_ if payloads_ is not None else [payload or quiz_payload()]
        self.model = RecordingModelClient(
            {
                CallType.AUTHOR_SKELETON: [skeleton(one) for one in queued],
                **(also or {}),
            }
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
    ("/displacements", {"inquiry": "What is enthalpy?"}),
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
                    hint=None,
                )
            ),
            capability_token="rotated",
        )

        assert "You reached for the macro picture." in body["tutor_line_html"]

    def test_a_model_graded_answer_without_one_carries_null(self):
        body = payloads.submission_body(
            self._submission(
                session_module.ModelGrading(
                    tutor_line=None, probe_question=None, hint=None
                )
            ),
            capability_token="rotated",
        )

        assert body["tutor_line_html"] is None

    def test_an_advanced_rung_hint_reaches_the_wire_as_feedback_html(self):
        """#56 / ADR-0016. The rung's text is authored on the grading response
        and routed to `Submission.feedback`, which is the field the Novice
        ladder already fills — so it needs no wire field of its own and the
        overlay needs no change to show it."""
        submission = dataclasses.replace(
            self._submission(
                session_module.ModelGrading(
                    tutor_line=None,
                    probe_question=None,
                    hint="Ask what the second law puts a floor under.",
                )
            ),
            verdict=Verdict.INCORRECT,
            hint_rung_shown=2,
            feedback="Ask what the second law puts a floor under.",
            blank_resolved=False,
        )

        body = payloads.submission_body(submission, capability_token="rotated")

        assert body["hint_rung_shown"] == 2
        assert "second law puts a floor under" in body["feedback_html"]
        assert body["revealed_option_id"] is None

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


# --- Learner settings (#14) --------------------------------------------------
#
# The settings value is tested in `test_learner_settings.py` and the cadence's
# effect on probing in `test_session.py`. What only exists once the routes
# exist is the *path*: how a `UserValves` change reaches a running attempt, and
# what it is not allowed to touch on the way.

from socratic.domain.modes import ProbeCadence  # noqa: E402

SETTINGS_HEADERS = {"X-Socratic-Service-Token": SERVICE_TOKEN}


TWO_BLANKS = ("b1", "b2")
"""The Novice `blank_range` is 1-2, so this is the largest Novice quiz there
is - and the smallest one in which "the next correct answer" is a different
answer from the one just made."""


def two_blank_novice_payload() -> dict:
    return quiz_payload(blank_ids=TWO_BLANKS)


def land_probe_questions(harness, session: str) -> None:
    """Put a pre-authored probe question on every blank of a stored attempt.

    A probe question arrives with the *pedagogy* payload (ADR-0011) and a blank
    without one is never probed, so a cadence test needs one there. It is
    written straight onto the stored attempt rather than fetched through
    `author_pedagogy` — which the routes do fire now (#118) — because these
    tests are about the cadence and not about the second authoring call, and
    queueing a payload for one would put a model call between the request and
    the thing being asserted.
    """
    attempt = harness.attempts.get_by_session(LEARNER, session)
    quiz = attempt.quiz
    harness.attempts.save(
        attempt.with_quiz(
            dataclasses.replace(
                quiz,
                blanks=tuple(
                    dataclasses.replace(
                        blank, probe_question="How did you arrive at that?"
                    )
                    for blank in quiz.blanks
                ),
            )
        )
    )


def advanced_payload() -> dict:
    return quiz_payload(
        blank_builder=advanced_blank_payload, blank_ids=ADVANCED_BLANK_IDS
    )


def set_settings(harness, **body) -> None:
    response = harness.client.post("/settings", json=body, headers=SETTINGS_HEADERS)
    assert response.status_code == 200, response.text


def answer(harness, token: str, session: str, blank_id: str) -> dict:
    response = harness.client.post(
        "/answers",
        json={
            "quiz_session_id": session,
            "blank_id": blank_id,
            "submitted": CORRECT_OPTION_ID,
        },
        headers={"X-Socratic-Token": token},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTheSettingsRoute:
    def test_it_records_the_learners_settings(self, harness):
        set_settings(harness, learner_id=LEARNER, probe_cadence="off", mode="advanced")

        stored = harness.deps.settings.get(LEARNER)
        assert stored.probe_cadence is ProbeCadence.OFF
        assert stored.mode == "advanced"

    def test_it_needs_the_service_token(self, harness):
        # The learner's own iframe must not be able to rewrite the settings of
        # a learner it names: the two credentials are not interchangeable.
        response = harness.client.post(
            "/settings", json={"learner_id": LEARNER, "probe_cadence": "off"}
        )

        assert response.status_code == 401
        assert harness.deps.settings.get(LEARNER) is None

    def test_an_unknown_cadence_is_refused(self, harness):
        response = harness.client.post(
            "/settings",
            json={"learner_id": LEARNER, "probe_cadence": "never"},
            headers=SETTINGS_HEADERS,
        )

        assert response.status_code == 422
        assert harness.deps.settings.get(LEARNER) is None

    def test_it_replaces_the_document_rather_than_patching_it(self, harness):
        # `UserValves` is read whole on the Pipe's side, so a setting the body
        # omits is one the learner has not set. Merging instead would leave a
        # learner who cleared a valve still carrying its old value.
        set_settings(harness, learner_id=LEARNER, mode="advanced")
        set_settings(harness, learner_id=LEARNER, probe_cadence="off")

        stored = harness.deps.settings.get(LEARNER)
        assert stored.probe_cadence is ProbeCadence.OFF
        assert stored.mode == DifficultyMode.NOVICE

    def test_authoring_records_the_settings_it_was_given(self, harness):
        harness.author(probe_cadence="off")

        assert harness.deps.settings.get(LEARNER).probe_cadence is ProbeCadence.OFF

    def test_the_cadence_authored_under_is_stamped_on_the_attempt(self, harness):
        body = harness.author(probe_cadence="off")

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert attempt.probe_cadence_at_authoring is ProbeCadence.OFF


class TestChangingTheCadenceMidQuiz:
    """Master spec acceptance 20, through the HTTP surface.

    `QuizSession.submit` has taken a live cadence since #10; until the route
    passed one the override was unreachable, so a learner's change could not
    take effect until their next quiz.
    """

    @pytest.fixture
    def playing(self):
        """An attempt authored under `always`, with its pedagogy landed.

        The pedagogy call is made through the domain rather than a route
        because there is no route for it: it is fired off the render path and
        is nobody's critical path (ADR-0011). It is here only because a probe
        needs a pre-authored question to ask.
        """
        harness = Harness(two_blank_novice_payload())
        body = harness.author(probe_cadence="always")
        session = body["quiz_session_id"]
        land_probe_questions(harness, session)
        return harness, session, body["capability_token"]

    def test_it_takes_effect_on_the_next_correct_answer(self, playing):
        harness, session, token = playing
        first = answer(harness, token, session, "b1")
        assert first["probe"] is not None, "authored under `always`"

        # The learner dismisses it, then turns probing off. Each response
        # carries the token the next request must use: a rotated one stops
        # verifying (D15).
        dismissed = harness.client.post(
            "/probes",
            json={"quiz_session_id": session, "blank_id": "b1"},
            headers={"X-Socratic-Token": first["capability_token"]},
        )
        assert dismissed.status_code == 200, dismissed.text
        set_settings(harness, learner_id=LEARNER, probe_cadence="off")

        second = answer(
            harness, dismissed.json()["capability_token"], session, "b2"
        )
        assert second["probe"] is None

    def test_a_probe_already_pending_is_unaffected(self, playing):
        harness, session, token = playing
        first = answer(harness, token, session, "b1")
        assert first["probe"] is not None

        set_settings(harness, learner_id=LEARNER, probe_cadence="off")

        attempt = harness.attempts.get_by_session(LEARNER, session)
        pending = [probe for probe in attempt.probes if not probe.is_answered]
        assert len(pending) == 1
        assert not pending[0].is_dismissed
        assert not attempt.is_sealed, "a pending probe holds the attempt open"

    def test_with_nothing_recorded_the_attempts_own_stamp_still_governs(self):
        # A learner who has never touched their valves — and the whole of the
        # behaviour before this ticket. The route must not substitute the
        # defaults for the cadence the quiz was authored under, so the attempt
        # here is made through the domain, leaving the settings unrecorded.
        harness = Harness(two_blank_novice_payload())
        quiz = harness.deps.authoring.author(
            "Why does heat flow?",
            LEARNER,
            mode=DifficultyMode.NOVICE.value,
            probe_cadence=ProbeCadence.OFF,
        )
        land_probe_questions(harness, quiz.quiz_session_id)
        assert harness.deps.settings.get(LEARNER) is None

        token = harness.mint_for(quiz.quiz_session_id)
        assert answer(harness, token, quiz.quiz_session_id, "b1")["probe"] is None


class TestTheModeToggleAppliesFromTheNextAuthoringCall:
    """Master spec acceptance 40. One attempt, one mode."""

    @pytest.fixture
    def playing(self):
        harness = Harness(payloads_=[two_blank_novice_payload(), advanced_payload()])
        body = harness.author(mode=DifficultyMode.NOVICE.value)
        return harness, body

    def test_the_in_flight_attempts_mode_is_unchanged(self, playing):
        harness, body = playing
        before = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])

        set_settings(harness, learner_id=LEARNER, mode=DifficultyMode.ADVANCED.value)
        answer(harness, body["capability_token"], body["quiz_session_id"], "b1")

        after = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert after.mode == before.mode == DifficultyMode.NOVICE
        assert after.quiz.blanks == before.quiz.blanks

    def test_the_next_authoring_call_is_authored_in_the_new_mode(self):
        """The *next* call, which since #92 means the next one that authors.

        The first quiz is finished before the second inquiry is raised, because
        a learner with one still open has their second question queued rather
        than authored — `InquiryIntake` decides that, not the mode toggle.
        """
        harness = Harness(payloads_=[two_blank_novice_payload(), advanced_payload()])
        first = harness.author(
            mode=DifficultyMode.NOVICE.value, probe_cadence=ProbeCadence.OFF.value
        )
        token = first["capability_token"]
        for blank_id in TWO_BLANKS:
            token = answer(harness, token, first["quiz_session_id"], blank_id)[
                "capability_token"
            ]
        assert harness.attempts.get_by_session(
            LEARNER, first["quiz_session_id"]
        ).is_sealed

        set_settings(harness, learner_id=LEARNER, mode=DifficultyMode.ADVANCED.value)
        second = harness.author(mode=DifficultyMode.ADVANCED.value)

        attempt = harness.attempts.get_by_session(LEARNER, second["quiz_session_id"])
        assert attempt.mode == DifficultyMode.ADVANCED

    def test_the_answering_route_reads_no_mode_at_all(self):
        # The structural half of "one attempt, one mode": there is no field on
        # `AnswerRequest` for a mode to arrive in, and the request model
        # forbids unknown fields, so a caller that tries is refused rather than
        # having it silently dropped.
        assert "mode" not in payloads.AnswerRequest.model_fields
        assert payloads.AnswerRequest.model_config["extra"] == "forbid"


# --- A quiz authored over HTTP (#89, #85) ------------------------------------


def pedagogy_payload(blank_ids: tuple[str, ...] = ("b1",)) -> dict:
    """The second authoring call's payload, as `_merge_pedagogy` reads it."""
    return {
        "recap": "Entropy never decreases in an isolated system.",
        "blanks": [
            {
                "blank_id": blank_id,
                "reinforcement": "Entropy is the one that never decreases.",
                "probe_question": "How did you arrive at that?",
                "hints": [SENTINEL_HINT, "second hint", "third hint"],
            }
            for blank_id in blank_ids
        ],
    }


def canned(payload: dict, message_id: str) -> ModelResponse:
    return ModelResponse(content=json.dumps(payload), message_id=message_id)


class TestAQuizAuthoredOverHttp:
    """The mode arrives as the plain string the request named, and the domain
    stamps it on the quiz. Until the registry canonicalised it on the way in,
    every prompt assembled for such a quiz died on `quiz.mode.value` — which is
    the pedagogy call for every quiz (#89) and every Advanced grading call
    (#85). Nothing in this file reached either, because both happen after the
    one blocking call the rest of these tests stop at."""

    def test_the_stored_quiz_carries_the_registered_mode_key(self, harness):
        harness.author()

        attempt = harness.attempts.list_for_learner(LEARNER)[0]
        assert attempt.quiz.mode is DifficultyMode.NOVICE
        assert attempt.mode is DifficultyMode.NOVICE

    def test_the_pedagogy_call_lands_on_it(self):
        # #89's own reproduction: author through `/quizzes`, then call
        # `author_pedagogy` directly on the stored quiz. The route fires it
        # too now (#118); calling it by hand is what keeps this a test of the
        # mode key surviving the wire rather than of the wiring.
        harness = Harness(
            also={
                CallType.AUTHOR_PEDAGOGY: [
                    canned(pedagogy_payload(), "msg_02PEDAGOGY")
                ]
            }
        )
        body = harness.author()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        merged = harness.deps.authoring.author_pedagogy(attempt.quiz, LEARNER)

        assert merged.quiz.recap == "Entropy never decreases in an isolated system."
        assert merged.quiz.blanks[0].probe_question == "How did you arrive at that?"

    def test_an_advanced_answer_is_graded_rather_than_a_500(self):
        # #85, fixed by the same normalisation: Advanced is the model-graded
        # mode, so `/answers` assembles a prompt from the stored quiz.
        harness = Harness(
            payload=advanced_payload(),
            also={CallType.GRADE_ANSWER: [canned({"verdict": "correct"}, "msg_grade")]},
        )
        set_settings(harness, learner_id=LEARNER, mode="advanced")
        body = harness.author(mode="advanced")

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": body["quiz_session_id"],
                "blank_id": "b1",
                "submitted": "entropy",
            },
            headers={"X-Socratic-Token": body["capability_token"]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["verdict"] == "correct"

    def test_the_wire_still_names_the_mode_as_a_plain_string(self, harness):
        body = harness.author()

        assert body["blanks"][0]["mode"] == "novice"

    def test_a_third_modes_key_reaches_the_wire_intact(self):
        """`blank_body` used to spell the enum-or-string guard out inline. It
        renders through `registry.mode_name` now, which has to keep working for
        a mode registered as a plain string — the case the inline guard was
        written for."""
        registry = default_registry()
        registry.register("expert", NOVICE_POLICY)
        blank = types.Blank(blank_id="b1", mode="expert", rubric="Name it.")

        assert payloads.blank_body(blank, registry)["mode"] == "expert"


# --- The pedagogy call is fired by the service (#118) -------------------------
#
# `docs/specs/fire-the-pedagogy-call.md`. `author_pedagogy` was implemented,
# tested and never called from `src/` or `pipe/`, so every quiz a learner ever
# played had empty hint rungs - rung three included, the one that reveals - no
# reinforcements, no probe questions and `recap_html == ""`. What only exists
# once the routes exist is the *firing*: which routes do it, which do not, and
# that a failed second call still leaves a playable quiz behind.


def skeleton_only_blank_payload(blank_id: str = "b1") -> dict:
    """A true skeleton blank: options and a key, and no pedagogy at all.

    The Novice skeleton rules ask for two options and a `correct_option_id`
    among them and nothing else, so this is what the first call is entitled to
    return - and it makes every pedagogy field an assertion about the merge
    rather than about the fixture.
    """
    return {
        "blank_id": blank_id,
        "options": [
            {"option_id": CORRECT_OPTION_ID, "text": "entropy"},
            {"option_id": "o2", "text": "enthalpy"},
        ],
        "correct_option_id": CORRECT_OPTION_ID,
        "reinforcement": None,
        "hints": None,
        "rubric": None,
    }


def skeleton_only_payload(blank_ids: tuple[str, ...] = ("b1",)) -> dict:
    payload = quiz_payload(
        blank_builder=skeleton_only_blank_payload, blank_ids=blank_ids
    )
    payload["recap"] = ""
    return payload


def with_pedagogy(*, payloads_=None, blank_ids: tuple[str, ...] = ("b1",)) -> Harness:
    """A harness with a pedagogy payload waiting for the second call."""
    return Harness(
        payloads_=payloads_ if payloads_ is not None else [skeleton_only_payload()],
        also={
            CallType.AUTHOR_PEDAGOGY: [
                canned(pedagogy_payload(blank_ids), "msg_02PEDAGOGY")
            ]
        },
    )


def overlay_of(harness, **overrides):
    body = {
        "learner_id": LEARNER,
        "inquiry": "Why does heat flow?",
        "mode": DifficultyMode.NOVICE.value,
    }
    body.update(overrides)
    return harness.client.post(
        "/overlays", json=body, headers={"X-Socratic-Service-Token": SERVICE_TOKEN}
    )


class TestTheQuizRouteFiresThePedagogyCall:
    def test_it_makes_exactly_one_pedagogy_call(self):
        harness = with_pedagogy()

        harness.author()

        assert harness.model.call_count(CallType.AUTHOR_SKELETON) == 1
        assert harness.model.call_count(CallType.AUTHOR_PEDAGOGY) == 1

    def test_the_skeleton_call_is_made_first(self):
        """The payload is fetched *after* the body the learner reads is built
        (ADR-0011: the skeleton is the only blocking call)."""
        harness = with_pedagogy()

        harness.author()

        assert [call.call_type for call in harness.model.calls] == [
            CallType.AUTHOR_SKELETON,
            CallType.AUTHOR_PEDAGOGY,
        ]

    def test_the_stored_quiz_gains_hints_a_reinforcement_a_probe_and_a_recap(self):
        """The issue's own reproduction, from the HTTP side. Every one of these
        was empty for every quiz ever played."""
        harness = with_pedagogy()

        body = harness.author()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        blank = attempt.quiz.blanks[0]
        assert attempt.quiz.recap == "Entropy never decreases in an isolated system."
        assert blank.hints == (SENTINEL_HINT, "second hint", "third hint")
        assert blank.reinforcement == "Entropy is the one that never decreases."
        assert blank.probe_question == "How did you arrive at that?"

    def test_both_calls_are_stamped_on_the_attempt(self):
        harness = with_pedagogy()

        body = harness.author()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert [call.call_type for call in attempt.model_calls] == [
            "author_skeleton",
            "author_pedagogy",
        ]

    def test_the_response_still_carries_no_hint_text(self):
        """The fire changes nothing about what crosses the wire: the ladder is
        merged into the stored quiz and stays in this process (D3, ADR-0003).
        `TestTheAnswerKeyStaysHere` holds the key's half of that."""
        harness = with_pedagogy()

        rendered = json.dumps(harness.author())

        assert SENTINEL_HINT not in rendered
        assert "correct_option_id" not in rendered


class TestTheOverlayRouteFiresItToo:
    """The Pipe's route, and therefore the one a real learner actually goes
    through (ADR-0015)."""

    def test_it_fires_the_pedagogy_call(self):
        harness = with_pedagogy()

        response = overlay_of(harness)

        assert response.status_code == 200, response.text
        assert harness.model.call_count(CallType.AUTHOR_PEDAGOGY) == 1

    def test_the_document_is_rendered_from_the_skeleton(self):
        """The overlay must not wait on the payload - it is rendered from the
        body the JSON route returns, which is the skeleton."""
        harness = with_pedagogy()

        document = overlay_of(harness).text

        assert SENTINEL_HINT not in document
        assert "the second law of thermodynamics" in document


class TestDisplacementFiresItForTheNewQuiz:
    """Start-this-instead authors a quiz by the same call and hands it to the
    same learner. A restart that came back without hints is #118 again by a
    different door."""

    def test_the_restarted_quiz_gets_its_payload(self):
        harness = with_pedagogy(
            payloads_=[skeleton_only_payload(), skeleton_only_payload()]
        )
        first = harness.author()

        response = harness.client.post(
            "/displacements",
            json={
                "quiz_session_id": first["quiz_session_id"],
                "inquiry": SECOND_INQUIRY,
            },
            headers={"X-Socratic-Token": first["capability_token"]},
        )

        assert response.status_code == 200, response.text
        restarted = harness.attempts.get_by_session(
            LEARNER, response.json()["quiz_session_id"]
        )
        assert restarted.quiz.blanks[0].hints == (
            SENTINEL_HINT,
            "second hint",
            "third hint",
        )


class TestTheBranchesThatFireNothing:
    def test_a_direct_answer_fires_no_pedagogy_call(self):
        """There is no quiz and no session, so there is nothing to author
        pedagogy for (`skeleton-and-pedagogy-split.md`, acceptance 7)."""
        harness = Harness(DIRECT_ANSWER_PAYLOAD)

        body = harness.author(inquiry="I have chest pain, what is happening?")

        assert body["kind"] == "direct_answer"
        harness.model.assert_never_called(CallType.AUTHOR_PEDAGOGY)

    def test_a_queued_inquiry_fires_no_pedagogy_call(self):
        """The learner already has a quiz, and the attempt they have was given
        its payload when it was authored."""
        harness = with_pedagogy(
            payloads_=[skeleton_only_payload(), skeleton_only_payload()]
        )
        harness.author()

        second = harness.author(inquiry=SECOND_INQUIRY)

        assert second["kind"] == "queued"
        assert harness.model.call_count(CallType.AUTHOR_PEDAGOGY) == 1


class TestAFailedPedagogyCall:
    """The response has already been sent, so nothing the second call does may
    reach the learner. The stub serves `{}` for a call type nothing was queued
    for, which `_merge_pedagogy` refuses for want of a recap - a malformed
    payload, arriving the way a real one would."""

    def test_the_learner_still_gets_a_playable_quiz(self):
        harness = Harness(skeleton_only_payload())

        body = harness.author()

        assert body["kind"] == "quiz"
        assert body["blanks"][0]["options"]

    def test_nothing_is_written_to_the_attempt(self):
        harness = Harness(skeleton_only_payload())

        body = harness.author()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert [call.call_type for call in attempt.model_calls] == ["author_skeleton"]
        assert attempt.quiz.blanks[0].hints is None

    def test_it_is_logged_rather_than_swallowed_in_silence(self, caplog):
        """The symptom was reported as "hint generation is slow". It was not
        slow; nothing said anything. A dropped payload has to be audible."""
        harness = Harness(skeleton_only_payload())

        with caplog.at_level(logging.WARNING, logger="socratic.service.app"):
            body = harness.author()

        assert body["quiz_session_id"] in caplog.text


# --- An unregistered mode (#101) ---------------------------------------------


class TestAnUnregisteredMode:
    """A mode no registry entry exists for is a **422 naming the field**, not a
    404 (#101).

    The refusal itself was always right and always in the right place — the
    registry raises before any model call — but "not found" is what a stale
    session says, and a Pipe could not tell the two apart. Only the status
    changed: the sentence is the registry's, in the shape the neighbouring
    `probe_cadence` refusal already uses.
    """

    def author_expert(self, harness, route="/quizzes"):
        return harness.client.post(
            route,
            json={"learner_id": LEARNER, "inquiry": "why?", "mode": "expert"},
            headers={"X-Socratic-Service-Token": SERVICE_TOKEN},
        )

    def test_it_names_the_mode_and_the_modes_there_are(self, harness):
        response = self.author_expert(harness)

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "unknown mode: 'expert'; expected one of 'novice', 'advanced'"
        )

    def test_it_is_refused_before_any_model_call(self, harness):
        self.author_expert(harness)

        harness.model.assert_never_called()

    def test_the_overlay_route_refuses_it_the_same_way(self, harness):
        response = self.author_expert(harness, route="/overlays")

        assert response.status_code == 422
        assert "expert" in response.json()["detail"]

    def test_a_missing_thing_is_still_a_404(self, authored):
        # The other half of the distinction: a `KeyError` that really is "no
        # such thing" — here a blank the quiz does not have — keeps the status
        # it had. Neither refusal is readable if both are 404.
        harness, quiz, token = authored

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": quiz["quiz_session_id"],
                "blank_id": "b99",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": token},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}

    def test_the_settings_route_still_carries_a_mode_it_has_not_heard_of(
        self, harness
    ):
        # The seam is unchanged: mode is carried, not validated, above the
        # registry (CONTEXT: LearnerSettings). The 422 is the *authoring*
        # call's refusal translated at the edge, not a second opinion here.
        set_settings(harness, learner_id=LEARNER, mode="expert")

        assert harness.deps.settings.get(LEARNER).mode == "expert"


# --- The intake reaches HTTP (#92) -------------------------------------------


SECOND_INQUIRY = "What is enthalpy?"
THIRD_INQUIRY = "What is absolute zero?"


def two_quizzes(harness_kwargs=None) -> Harness:
    """A harness with two authoring responses queued.

    Two, so that a route that wrongly authors a second quiz gets one rather
    than a `StopIteration` — the test then fails on the behaviour it is about
    instead of on the stub running dry.
    """
    return Harness(
        payloads_=[quiz_payload(), quiz_payload(blank_ids=("b1",))],
        **(harness_kwargs or {}),
    )


class TestASecondInquiryMidQuiz:
    """Issue #92: the door an inquiry arrives at is `InquiryIntake`, not
    `QuizAuthoring`.

    Reproduced on `origin/main` @ `f1b8a43`: two `/quizzes` calls returned two
    `quiz_session_id`s, left two `in_flight` attempts in one learner's
    partition and made two authoring calls.
    """

    def test_the_second_inquiry_is_queued_rather_than_authored(self):
        harness = two_quizzes()
        first = harness.author()

        second = harness.author(inquiry=SECOND_INQUIRY)

        assert second["kind"] == "queued"
        assert harness.model.call_count(CallType.AUTHOR_SKELETON) == 1
        attempts = harness.attempts.list_for_learner(LEARNER)
        assert [attempt.session_id for attempt in attempts] == [
            first["quiz_session_id"]
        ]

    def test_the_queued_body_names_the_open_quiz_and_the_rest_of_the_queue(self):
        harness = two_quizzes()
        first = harness.author()
        harness.author(inquiry=SECOND_INQUIRY)

        third = harness.author(inquiry=THIRD_INQUIRY)

        assert third["quiz_session_id"] == first["quiz_session_id"]
        assert third["topic"] == first["topic"]
        assert third["inquiry"] == THIRD_INQUIRY
        # The rest of the queue - and not this question, which `inquiry`
        # already names. Listing it under "also asked" would render it as a
        # sibling of itself.
        assert third["queued_topics"] == [SECOND_INQUIRY]

    def test_the_queued_body_mints_no_capability_token(self):
        """`TokenMinter.mint` retires the session's previous token, so a token
        minted here would log the learner's open overlay out of its own quiz."""
        harness = two_quizzes()
        first = harness.author()

        second = harness.author(inquiry=SECOND_INQUIRY)

        assert "capability_token" not in second
        assert harness.minter.verify(
            first["capability_token"], for_session=first["quiz_session_id"]
        )

    def test_the_open_attempt_is_untouched_apart_from_its_queue(self):
        harness = two_quizzes()
        harness.author()

        harness.author(inquiry=SECOND_INQUIRY)

        attempt = harness.attempts.list_for_learner(LEARNER)[0]
        assert attempt.outcome.value == "in_flight"
        assert attempt.sealed_at is None

    def test_a_first_inquiry_still_authors(self, harness):
        body = harness.author()

        assert body["kind"] == "quiz"
        assert body["capability_token"]
        assert harness.model.call_count(CallType.AUTHOR_SKELETON) == 1


class TestTheOverlayRouteQueuesToo:
    """The Pipe's route, which returns these bytes verbatim (#92).

    Without a queued branch here the Pipe does not degrade — it 500s on the
    first second question any learner asks.
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

    def test_a_second_inquiry_returns_a_document_rather_than_a_quiz(self):
        harness = two_quizzes()
        self._overlay(harness)

        response = self._overlay(harness, inquiry=SECOND_INQUIRY)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html")
        assert SECOND_INQUIRY in response.text
        assert "the second law of thermodynamics" in response.text
        assert harness.model.call_count(CallType.AUTHOR_SKELETON) == 1

    def test_the_queued_document_carries_no_token_and_no_client(self):
        harness = two_quizzes()
        first_document = self._overlay(harness).text
        first_token = re.search(
            r'data-capability-token="([^"]+)"', first_document
        ).group(1)

        queued_document = self._overlay(harness, inquiry=SECOND_INQUIRY).text

        assert "data-capability-token" not in queued_document
        assert "SocraticQuiz" not in queued_document
        # And the token the learner's open overlay is holding still works.
        assert harness.minter.verify(first_token)


class TestDisplacement:
    """"Start this instead", as a route (#92, master acceptance 26).

    Authorized by the capability token for the attempt being displaced, not by
    the service token: `abandoned` is the one destructive write in the domain,
    it is a learner's gesture about their own open quiz, and the service token
    is held install-wide for every learner at once.
    """

    def _displace(self, harness, token, session, inquiry=SECOND_INQUIRY):
        return harness.client.post(
            "/displacements",
            json={"quiz_session_id": session, "inquiry": inquiry},
            headers={"X-Socratic-Token": token},
        )

    def test_it_abandons_the_open_attempt_and_authors_the_new_inquiry(self):
        harness = two_quizzes()
        first = harness.author()

        response = self._displace(
            harness, first["capability_token"], first["quiz_session_id"]
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "quiz"
        assert body["quiz_session_id"] != first["quiz_session_id"]
        assert body["displaced_session_id"] == first["quiz_session_id"]

        displaced = harness.attempts.get_by_session(
            LEARNER, first["quiz_session_id"]
        )
        assert displaced.outcome.value == "abandoned"
        assert displaced.sealed_at is None

    def test_the_new_body_carries_a_token_good_for_the_new_session(self):
        harness = two_quizzes()
        first = harness.author()

        body = self._displace(
            harness, first["capability_token"], first["quiz_session_id"]
        ).json()

        assert harness.minter.verify(
            body["capability_token"], for_session=body["quiz_session_id"]
        )

    def test_the_remaining_queue_carries_forward_without_the_question_answered(self):
        """Master acceptance 26. The second question was queued first, so it is
        on the displaced attempt's queue when it is chosen — and must not
        remain there once it is the thing being answered."""
        harness = two_quizzes()
        first = harness.author()
        harness.author(inquiry=SECOND_INQUIRY)
        harness.author(inquiry=THIRD_INQUIRY)

        body = self._displace(
            harness, first["capability_token"], first["quiz_session_id"]
        ).json()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert SECOND_INQUIRY not in attempt.queued_topics
        assert THIRD_INQUIRY in attempt.queued_topics

    def test_the_service_token_does_not_authorize_a_displacement(self):
        harness = two_quizzes()
        first = harness.author()

        response = harness.client.post(
            "/displacements",
            json={
                "quiz_session_id": first["quiz_session_id"],
                "inquiry": SECOND_INQUIRY,
            },
            headers={"X-Socratic-Service-Token": SERVICE_TOKEN},
        )

        assert response.status_code == 401

    def test_the_displaced_session_stops_accepting_answers(self):
        harness = two_quizzes()
        first = harness.author()

        self._displace(harness, first["capability_token"], first["quiz_session_id"])

        response = harness.client.post(
            "/answers",
            json={
                "quiz_session_id": first["quiz_session_id"],
                "blank_id": "b1",
                "submitted": CORRECT_OPTION_ID,
            },
            headers={"X-Socratic-Token": first["capability_token"]},
        )

        assert response.status_code == 422

    def test_it_authors_in_the_learners_current_mode(self):
        """The iframe cannot see `UserValves`, so the mode is read from the
        settings the Pipe pushed, not from the request."""
        harness = Harness(payloads_=[two_blank_novice_payload(), advanced_payload()])
        first = harness.author(mode=DifficultyMode.NOVICE.value)
        set_settings(harness, learner_id=LEARNER, mode=DifficultyMode.ADVANCED.value)

        body = self._displace(
            harness, first["capability_token"], first["quiz_session_id"]
        ).json()

        attempt = harness.attempts.get_by_session(LEARNER, body["quiz_session_id"])
        assert attempt.mode == DifficultyMode.ADVANCED

    def test_a_direct_answer_displaces_nothing(self):
        """The domain's rule: nothing was started, so there is no attempt for
        the queue to carry forward onto and the open quiz stays the
        learner's."""
        harness = Harness(payloads_=[quiz_payload(), DIRECT_ANSWER_PAYLOAD])
        first = harness.author()

        body = self._displace(
            harness,
            first["capability_token"],
            first["quiz_session_id"],
            inquiry="chest pain",
        ).json()

        assert body["kind"] == "direct_answer"
        assert body["displaced_session_id"] is None
        still_open = harness.attempts.get_by_session(
            LEARNER, first["quiz_session_id"]
        )
        assert still_open.outcome.value == "in_flight"

    def test_it_names_no_learner_and_no_attempt(self):
        assert set(payloads.DisplaceRequest.model_fields) == {
            "quiz_session_id",
            "inquiry",
        }
        assert payloads.DisplaceRequest.model_config["extra"] == "forbid"


class TestTheIntakeIsWiredWithoutBeingAskedFor:
    def test_dependencies_build_their_own_intake(self, harness):
        """`__main__.build_app` names no intake, and does not have to: the
        record derives one from the authoring and attempts it already holds,
        exactly as it derives its registry and its settings store."""
        rebuilt = dataclasses.replace(harness.deps, intake=None)

        assert rebuilt.intake is not None
        assert isinstance(rebuilt.intake, InquiryIntake)

    def test_the_answer_key_does_not_travel_on_the_queued_branch(self):
        harness = two_quizzes()
        harness.author()

        queued = harness.author(inquiry=SECOND_INQUIRY)

        rendered = json.dumps(queued)
        assert SENTINEL_HINT not in rendered
        assert CORRECT_OPTION_ID not in rendered


# --- Option labels (#98) -----------------------------------------------------


class TestAnOptionLabelIsInline:
    """An option's `text_html` is a label, not a document (#98).

    `html_of` is the block render: `html_of("entropy")` is `<p>entropy</p>`. The
    client copies an option's markup into the blank's placeholder when the blank
    resolves, and the placeholder is an inline-block span sitting in the middle
    of a sentence — so that `<p>`, and the UA's paragraph margins with it, used
    to land there. `label_html` renders a label that is a phrase inline; a label
    carrying real block content still block-renders, because inline-rendering a
    fenced block would mangle it into a single line.
    """

    def _labels(self, *texts: str) -> list[str]:
        blank = types.Blank(
            blank_id="b1",
            mode=DifficultyMode.NOVICE,
            options=tuple(
                types.Option(option_id=f"o{index}", text=text)
                for index, text in enumerate(texts, start=1)
            ),
            correct_option_id="o1",
        )
        options = payloads.blank_body(blank, default_registry())["options"]
        return [option["text_html"] for option in options]

    def test_a_one_word_label_carries_no_paragraph_wrapper(self):
        assert self._labels("entropy") == ["entropy"]

    def test_a_label_keeps_the_inline_subset(self):
        assert self._labels("the **second** law") == [
            "the <strong>second</strong> law"
        ]

    def test_a_label_that_is_a_block_stays_a_block(self):
        source = "```python\nreturn 1\n```"

        label = self._labels(source)[0]

        assert label == payloads.html_of(source)
        assert "<pre" in label

    def test_a_label_is_sanitised_like_everything_else_on_the_wire(self):
        label = self._labels("<script>alert(1)</script>")[0]

        assert "<script" not in label
        assert "&lt;script&gt;" in label

    def test_every_option_of_an_authored_quiz_reaches_the_wire_unwrapped(
        self, harness
    ):
        """The whole path, not just the helper: what `/quizzes` hands the
        overlay is what the client copies into the placeholder."""
        body = harness.author()

        for option in body["blanks"][0]["options"]:
            assert "<p>" not in option["text_html"], option


class TestTheResolvedTextReachesTheWire:
    """[ADR-0019](../docs/adr/0019-resolved-blank-text-comes-from-the-service.md)
    and [#127](https://github.com/derkmed/socratic/issues/127).

    The client used to reconstruct the gap's text by scanning the rendered
    document for an option id. Option ids are blank-scoped, so it usually found
    a different blank's option and pasted that. The service states it instead.
    """

    def _submission(self, **overrides):
        fields = dict(
            verdict=Verdict.CORRECT,
            graded_by=GradingStrategy.DETERMINISTIC,
            hint_rung_shown=None,
            feedback=None,
            revealed_option_id=None,
            blank_resolved=True,
            attempt_sealed=False,
        )
        fields.update(overrides)
        return session_module.Submission(**fields)

    def test_the_resolved_text_travels_as_inline_html(self):
        """`label_html`, not `html_of`: the fragment lands in the middle of a
        sentence, and a `<p>` wrapper would carry block margins into it (#98)."""
        body = payloads.submission_body(
            self._submission(resolved_answer="entropy"),
            capability_token="rotated",
        )

        assert body["resolved_html"] == "entropy"

    def test_markup_in_the_resolved_text_is_sanitised_like_everything_else(self):
        body = payloads.submission_body(
            self._submission(resolved_answer="`OrderedDict`"),
            capability_token="rotated",
        )

        assert body["resolved_html"] == "<code>OrderedDict</code>"

    def test_a_blank_that_did_not_resolve_sends_null(self):
        body = payloads.submission_body(
            self._submission(blank_resolved=False, resolved_answer=None),
            capability_token="rotated",
        )

        assert body["resolved_html"] is None

    def test_the_disclosure_marker_still_travels_beside_it(self):
        """`revealed_option_id` stays: it is what ADR-0003 accounts for. What
        changed is that no displayable text is derived from it."""
        body = payloads.submission_body(
            self._submission(
                verdict=Verdict.INCORRECT,
                hint_rung_shown=3,
                revealed_option_id="b1-o1",
                resolved_answer="entropy",
            ),
            capability_token="rotated",
        )

        assert body["revealed_option_id"] == "b1-o1"
        assert body["resolved_html"] == "entropy"

    def test_the_probe_path_carries_it_too(self):
        body = payloads.probe_answer_body(
            session_module.ProbeAnswer(
                verdict=Verdict.INCORRECT,
                correction="Not quite.",
                blank_reopened=False,
                blank_resolved=True,
                revealed_option_id="b1-o1",
                attempt_sealed=False,
                resolved_answer="entropy",
            ),
            capability_token="rotated",
        )

        assert body["resolved_html"] == "entropy"
