"""The routes: four translations, and nothing else.

Each handler authorizes, calls exactly one seam, and shapes the result. There
is no grading, no laddering, no cadence and no key handling here — those live
in `QuizSession` and `QuizAuthoring`, which is what makes this layer safe to
leave untested as a unit and what the ticket means by "the HTTP API is
deliberately not a seam".

Two properties are *only* true here, so they are asserted in `test_service.py`:
authorization runs before any work, and the response carries a rotated token.
"""

from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from socratic.domain import rating as rating_module
from socratic.domain import types
from socratic.domain.records import QuizAttempt
from socratic.domain.tokens import Claims
from socratic.service import payloads, security
from socratic.service.deps import ServiceDependencies

TOKEN_HEADER = "X-Socratic-Token"
SERVICE_TOKEN_HEADER = "X-Socratic-Service-Token"


def create_app(deps: ServiceDependencies) -> FastAPI:
    """Build the ASGI app over an explicit dependency record."""
    app = FastAPI(title="socratic quiz service", docs_url=None, redoc_url=None)

    # The iframe is sandboxed without `allow-same-origin`, so its requests
    # arrive with `Origin: null` and any of ours is preflighted. Measured in
    # Chrome and Edge before this was written.
    #
    # `allow_origins=["*"]` with `allow_credentials=False`: credentials are
    # illegal beside `*` and meaningless from an opaque origin anyway. The
    # capability token is the entire authorization story, which is exactly why
    # it is a header the iframe sets rather than anything ambient.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["content-type", TOKEN_HEADER, SERVICE_TOKEN_HEADER],
        max_age=600,
    )

    def _authorized(token: str | None, session: str) -> tuple[Claims, QuizAttempt]:
        """Verify, then resolve the attempt from the token's own claims.

        The learner is taken from the verified token, never from the request,
        so an attempt belonging to someone else cannot be named — there is no
        field in which to name one.
        """
        claims = security.authorize_capability(deps, token, session)
        attempt = deps.attempts.get_by_session(claims.learner, session)
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no such session"
            )
        return claims, attempt

    @app.post("/quizzes")
    def author_quiz(
        body: payloads.AuthorRequest,
        presented: str | None = Depends(security.service_header),
    ) -> JSONResponse:
        security.require_service_token(deps, presented)

        result = deps.authoring.author(
            body.inquiry,
            body.learner_id,
            mode=body.mode,
            **_cadence(body.probe_cadence),
        )

        if isinstance(result, types.DirectAnswer):
            return _json(payloads.direct_answer_body(result))

        token = deps.minter.mint(result.quiz_session_id, body.learner_id)
        return _json(
            payloads.quiz_body(
                result, registry=deps.registry, capability_token=token
            )
        )

    @app.post("/answers")
    def submit_answer(
        body: payloads.AnswerRequest,
        token: str | None = Depends(security.capability_header),
    ) -> JSONResponse:
        claims, attempt = _authorized(token, body.quiz_session_id)

        submission = deps.session.submit(
            learner_id=claims.learner,
            attempt_id=attempt.attempt_id,
            blank_id=body.blank_id,
            submitted=body.submitted,
        )

        return _json(
            payloads.submission_body(
                submission, capability_token=deps.minter.rotate(token)
            )
        )

    @app.post("/probes")
    def answer_probe(
        body: payloads.ProbeRequest,
        token: str | None = Depends(security.capability_header),
    ) -> JSONResponse:
        claims, attempt = _authorized(token, body.quiz_session_id)

        if body.self_explanation is None:
            dismissal = deps.session.dismiss_probe(
                learner_id=claims.learner,
                attempt_id=attempt.attempt_id,
                blank_id=body.blank_id,
            )
            return _json(
                payloads.probe_dismissal_body(
                    dismissal, capability_token=deps.minter.rotate(token)
                )
            )

        answer = deps.session.answer_probe(
            learner_id=claims.learner,
            attempt_id=attempt.attempt_id,
            blank_id=body.blank_id,
            self_explanation=body.self_explanation,
        )
        return _json(
            payloads.probe_answer_body(
                answer, capability_token=deps.minter.rotate(token)
            )
        )

    @app.post("/ratings")
    def submit_rating(
        body: payloads.RatingRequest,
        token: str | None = Depends(security.capability_header),
    ) -> JSONResponse:
        claims, attempt = _authorized(token, body.quiz_session_id)

        try:
            rating_module.submit_rating(
                attempts=deps.attempts,
                ratings=deps.ratings,
                learner_id=claims.learner,
                attempt_id=attempt.attempt_id,
                score=body.score,
                clock=deps.clock,
            )
        except ValueError as error:
            # The repository holds ratings immutable once written. A second
            # one is the caller's conflict to resolve, not an error here.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from None

        # Not a grading response, so nothing rotates: rotation rides the round
        # trip that already carries a verdict (D15).
        return _json({"ok": True})

    _install_domain_error_handling(app)
    return app


def _cadence(value: str | None) -> dict[str, Any]:
    """Pass the cadence only when the caller named one, so the domain's own
    default stays the single place it is written down."""
    from socratic.domain.modes import ProbeCadence

    if value is None:
        return {}
    try:
        return {"probe_cadence": ProbeCadence(value)}
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown probe cadence: {value!r}",
        ) from None


def _json(body: dict[str, Any]) -> JSONResponse:
    """Every response says UTF-8 explicitly.

    Rendered MathML is non-ASCII — nh3 resolves `&#x0222B;` to `∫` (#53) — so
    a response that does not declare its charset breaks every formula.
    """
    return JSONResponse(content=body, media_type="application/json; charset=utf-8")


def _install_domain_error_handling(app: FastAPI) -> None:
    """Domain refusals become 4xx, not 500s.

    `KeyError` is how the repositories and `Quiz.blank` say "no such thing for
    this learner", and `ValueError` is how the records refuse an inadmissible
    operation. Neither is a bug in the service, and neither should return a
    stack trace.
    """

    @app.exception_handler(KeyError)
    def _not_found(_: Request, error: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "not found"})

    @app.exception_handler(ValueError)
    def _unprocessable(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})
