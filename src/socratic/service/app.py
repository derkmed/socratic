"""The routes: four translations, and nothing else.

Each handler authorizes, calls exactly one seam, and shapes the result. There
is no grading, no laddering, no cadence and no key handling here — those live
in `QuizSession` and `QuizAuthoring`, which is what makes this layer safe to
leave untested as a unit and what the ticket means by "the HTTP API is
deliberately not a seam".

Two properties are *only* true here, so they are asserted in `test_service.py`:
authorization runs before any work, and the response carries a rotated token.
"""

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from socratic.domain import inquiry as inquiry_module
from socratic.domain import rating as rating_module
from socratic.domain import types
from socratic.domain.modes import ProbeCadence
from socratic.domain.records import QuizAttempt
from socratic.domain.settings import LearnerSettings
from socratic.domain.tokens import Claims
from socratic.service import payloads, security
from socratic.service.deps import ServiceDependencies
from socratic.ui import overlay

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

    def _current_cadence(learner_id: str) -> ProbeCadence | None:
        """The learner's cadence right now, or `None` for "they never said".

        `None` is not the same as `sometimes`: it lets `QuizSession.submit`
        fall back to the attempt's `probe_cadence_at_authoring`, so a learner
        who has never touched their valves keeps playing the quiz they were
        given. The mode is deliberately not read here - one attempt, one mode
        (CONTEXT: Mode toggle).
        """
        recorded = deps.settings.get(learner_id)
        return None if recorded is None else recorded.probe_cadence

    def _authored(body: payloads.AuthorRequest) -> dict[str, Any]:
        """Take the inquiry in, and shape whatever became of it.

        Shared by `/quizzes` and `/overlays` so neither grows a second copy of
        the authoring translation — and so the overlay is rendered from exactly
        the body the JSON route returns, which is what keeps the answer key's
        whitelist the only thing standing between the key and the browser.

        **`InquiryIntake`, not `QuizAuthoring`** (#92). The single-topic-focus
        rule is a decision about the learner's partition, which authoring does
        not read; going straight to `author` authored a second quiz for a
        learner who already had one open and left the first in flight.
        """
        settings = _settings(
            body.learner_id, mode=body.mode, probe_cadence=body.probe_cadence
        )
        # Authoring is itself a sync point, so the common case - a learner who
        # changes a valve and then asks a question - needs no separate call.
        # It syncs on the queued branch too: the Pipe pushes current valves
        # with every turn, and whether we author is beside the point.
        deps.settings.save(settings)

        intake = deps.intake.raise_inquiry(
            body.inquiry,
            body.learner_id,
            mode=settings.mode,
            probe_cadence=settings.probe_cadence,
        )
        if isinstance(intake, inquiry_module.Queued):
            return payloads.queued_body(intake)
        return _started(intake.result, body.learner_id)

    def _started(
        result: types.AuthoringResult, learner_id: str
    ) -> dict[str, Any]:
        """An authored result as its wire body, with a token if it has a
        session to scope one to."""
        if isinstance(result, types.DirectAnswer):
            return payloads.direct_answer_body(result)

        token = deps.minter.mint(result.quiz_session_id, learner_id)
        return payloads.quiz_body(
            result, registry=deps.registry, capability_token=token
        )

    @app.post("/quizzes")
    def author_quiz(
        body: payloads.AuthorRequest,
        presented: str | None = Depends(security.service_header),
    ) -> JSONResponse:
        security.require_service_token(deps, presented)

        return _json(_authored(body))

    @app.post("/overlays")
    def author_overlay(
        body: payloads.AuthorRequest,
        presented: str | None = Depends(security.service_header),
    ) -> HTMLResponse:
        """The overlay document, for the Pipe to return unchanged (#13, §11).

        The Pipe cannot render this itself — it is pasted Python in the Open
        WebUI container and cannot import `socratic.ui` — so ADR-0015's "the
        Pipe returns the rendered HTML the service produced" has to mean a route
        here. Same credential as `/quizzes`, because it is the same call with a
        different representation: it is the Pipe asking, server to server,
        before any session exists for a capability token to be scoped to.
        """
        security.require_service_token(deps, presented)

        authored = _authored(body)
        # Three branches, one per `kind` the authoring body can carry. Named
        # exhaustively rather than as an if/else pair, so a fourth kind fails
        # here and loudly: the previous `else` sent anything that was not a
        # quiz to the direct-answer renderer, which is how a queued inquiry
        # reached the Pipe as a 404 instead of a document.
        renderers = {
            "quiz": lambda: overlay.render_overlay(
                authored, service_base_url=deps.public_base_url
            ),
            "direct_answer": lambda: overlay.render_direct_answer(authored),
            "queued": lambda: overlay.render_queued(authored),
        }
        document = renderers[authored["kind"]]()

        return HTMLResponse(
            content=document,
            media_type="text/html; charset=utf-8",
            # The learner's browser renders this inside a `srcdoc` iframe; it is
            # never a file to save (master spec §12).
            headers={"Content-Disposition": "inline"},
        )

    @app.post("/displacements")
    def displace_quiz(
        body: payloads.DisplaceRequest,
        token: str | None = Depends(security.capability_header),
    ) -> JSONResponse:
        """"Start this instead": abandon the open quiz, author this one.

        The capability token for the attempt being displaced is the whole
        authorization, and it is the right one: it asserts "the bearer is the
        learner who has this session open", which is exactly the authority the
        gesture needs. The service token is deliberately *not* accepted — it is
        held install-wide by the Pipe, and `abandoned` is the only destructive
        write in the domain.

        The response carries the newly authored quiz, with a token for its
        session, because the one the caller presented now belongs to a closed
        attempt and every write to it is refused.

        What is displaced is the learner's latest `in_flight` attempt, which
        `InquiryIntake` chooses — and which, now that a second inquiry is
        queued rather than authored, is the attempt the presented token names.
        """
        claims, _ = _authorized(token, body.quiz_session_id)

        # The iframe cannot see `UserValves`, so mode and cadence come from
        # what the Pipe last pushed rather than from the request. Absent
        # settings are the documented defaults, not a refusal: a learner can
        # reach this route without ever having touched a valve.
        settings = deps.settings.get(claims.learner) or LearnerSettings(
            claims.learner
        )

        started = deps.intake.start_this_instead(
            body.inquiry,
            claims.learner,
            mode=settings.mode,
            probe_cadence=settings.probe_cadence,
        )

        return _json(
            payloads.displaced_body(
                _started(started.result, claims.learner),
                displaced_session_id=(
                    started.displaced.session_id if started.displaced else None
                ),
            )
        )

    @app.post("/settings")
    def record_settings(
        body: payloads.SettingsRequest,
        presented: str | None = Depends(security.service_header),
    ) -> JSONResponse:
        """The learner's `UserValves`, pushed by the Pipe (#14, ADR-0010).

        The route exists because "mid-quiz" would otherwise have no meaning
        above the domain: between authoring and sealing the only actor talking
        to this service is the iframe, and the iframe cannot see `UserValves`.
        So a change has to arrive here, from the Pipe, server to server - which
        is why it carries the service token rather than a capability one. A
        learner's iframe naming another learner in the body must not be able to
        rewrite their settings.

        **A replace, not a patch.** `UserValves` is read whole on the Pipe's
        side, so a setting the body omits is one the learner has not set — and
        writing the whole document keeps the store a copy of the valves rather
        than a merge of everything ever sent.
        """
        security.require_service_token(deps, presented)

        deps.settings.save(
            _settings(
                body.learner_id, mode=body.mode, probe_cadence=body.probe_cadence
            )
        )
        return _json({"ok": True})

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
            probe_cadence=_current_cadence(claims.learner),
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


def _settings(
    learner_id: str, *, mode: str | None, probe_cadence: str | None
) -> LearnerSettings:
    """The wire's strings as the domain's settings value, or a 422.

    The four cadence values and the defaults are written down once, in
    `LearnerSettings`; this is only the translation of its refusal into a
    status code. An unknown *mode* is deliberately not refused here - the
    registry refuses it on the authoring call, before the model is consulted,
    and staying out of that keeps `ModeRegistry` the only place mode is
    branched on (CONTEXT).
    """
    try:
        return LearnerSettings.parse(
            learner_id, mode=mode, probe_cadence=probe_cadence
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
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
