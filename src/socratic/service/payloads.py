"""The wire format — a whitelist, never a serialization.

Every response here is built field by field out of a domain object. Nothing
serializes a `Quiz`, a `Blank` or a `QuizAttempt`, and that is the mechanism
by which the answer key stays in this process (D3, ADR-0003): `correct_option_id`,
`rubric`, `hints` and `reinforcement` have no field to travel in, so adding one
would be a visible edit to this module rather than a field quietly appearing
because someone added it to a dataclass.

The one sanctioned disclosure is `revealed_option_id` on a graded submission —
the rung-three reveal, per blank, after the ladder is spent (`Submission`, ADR-0009).

Everything that reaches a browser leaves here already rendered and sanitised
(ADR-0012): the iframe renders no model-authored text itself.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from socratic.domain import inquiry as inquiry_module
from socratic.domain import session as session_module
from socratic.domain import types
from socratic.domain.registry import ModeRegistry, mode_name
from socratic.rendering import content, markdown, sanitiser


class _Strict(BaseModel):
    """Unknown fields are an error, not something to ignore.

    A request that names an `attempt_id` or a `learner_id` is refused rather
    than silently having it dropped: those are resolved from the token, and a
    caller that thinks it can choose them should be told it cannot.
    """

    model_config = ConfigDict(extra="forbid")


class AuthorRequest(_Strict):
    learner_id: str = Field(min_length=1)
    inquiry: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    probe_cadence: Optional[str] = None


class SettingsRequest(_Strict):
    """A learner's `UserValves`, as the Pipe reads them (#14, ADR-0010).

    Both settings are optional, and `None` means "this learner has never set
    it" rather than "set it back to the default" - the same distinction
    `LearnerSettings.parse` makes. There is no model and no effort field:
    those are admin-level `Valve`s (ADR-0014), and `extra="forbid"` means a
    request that names one is refused rather than having it dropped.
    """

    learner_id: str = Field(min_length=1)
    mode: Optional[str] = None
    probe_cadence: Optional[str] = None


class _SessionScoped(_Strict):
    """Every iframe request names the session it addresses.

    Not as a credential — the session id is a plain key (D7, ADR-0007) — but
    as the thing the capability token is checked *against*. Without it the
    `for_session` check would have nothing to compare to but the token's own
    claims, which is no check at all.
    """

    quiz_session_id: str = Field(min_length=1)


class DisplaceRequest(_SessionScoped):
    """"Start this instead" (#92, master acceptance 26).

    No `learner_id` and no `mode`. The learner comes from the capability
    token's own claims, and the mode from the settings the Pipe pushed - the
    iframe cannot see `UserValves`, and a mode it could name would be a mode a
    learner could choose for someone else's next quiz.
    """

    inquiry: str = Field(min_length=1)


class AnswerRequest(_SessionScoped):
    blank_id: str = Field(min_length=1)
    submitted: str


class ProbeRequest(_SessionScoped):
    blank_id: str = Field(min_length=1)
    self_explanation: Optional[str] = None
    """Null is a dismissal. The probe is dismissible (spec §11), and the
    ticket names four endpoints, so dismissal is a value here rather than a
    fifth route."""


class RatingRequest(_SessionScoped):
    score: int = Field(ge=1, le=5)


def html_of(text: str) -> str:
    """Restricted Markdown to sanitised HTML.

    Model-authored or not: ADR-0012's rule is that everything rendered passes
    through sanitisation, and this function and `label_html` below are the only
    two ways a string becomes HTML in this package — the block render and the
    label render, and nothing hand-rolls a third.
    """
    return sanitiser.sanitise(markdown.render_markdown(text))


def label_html(text: str) -> str:
    """One label — an option, a button's worth of words — to sanitised HTML.

    The same restricted subset and the same sanitiser pass as `html_of`, and
    the same untrusted input; what differs is the box. A label is a phrase, not
    a document: `html_of("entropy")` is `<p>entropy</p>`, and that `<p>` is a
    block the option's own chrome never asked for. The client copies an
    option's markup into the inline placeholder when the blank resolves, so the
    block travels into the middle of a sentence (#98).

    `markdown.render_fragment` renders a label that is a phrase inline, with no
    wrapper, and falls back to the block render for a label that carries real
    block content — an author who writes a fenced block as an option gets a
    fenced block, because inline-rendering one would mangle it.
    """
    return sanitiser.sanitise(markdown.render_fragment(text))


def _option(option: types.Option) -> dict[str, Any]:
    return {"option_id": option.option_id, "text_html": label_html(option.text)}


def blank_body(blank: types.Blank, registry: ModeRegistry) -> dict[str, Any]:
    """One blank, as the UI needs it and no more.

    `render_hint` travels so the UI picks its input control from data. The
    alternative — the UI knowing that Novice means buttons — is the `if mode ==`
    that `tests/test_registry.py` forbids, moved across a network boundary
    where the guard could not see it.
    """
    policy = registry.policy_for(blank.mode)
    return {
        "blank_id": blank.blank_id,
        "mode": mode_name(blank.mode),
        "render_hint": policy.render_hint.value,
        "options": (
            [_option(option) for option in blank.options] if blank.options else None
        ),
    }


def quiz_body(
    quiz: types.Quiz, *, registry: ModeRegistry, capability_token: str
) -> dict[str, Any]:
    return {
        "kind": "quiz",
        "quiz_session_id": quiz.quiz_session_id,
        "capability_token": capability_token,
        "topic": quiz.topic,
        "explanation_html": content.render_explanation(quiz.explanation),
        "recap_html": html_of(quiz.recap),
        "blanks": [blank_body(blank, registry) for blank in quiz.blanks],
        "queued_topics": list(quiz.queued_topics),
    }


def direct_answer_body(answer: types.DirectAnswer) -> dict[str, Any]:
    """The override branch (ADR-0004). No session was created, so there is no
    session for a token to be scoped to and none is minted."""
    return {
        "kind": "direct_answer",
        "topic": answer.topic,
        "answer_html": html_of(answer.answer),
        "queued_topics": list(answer.queued_topics),
    }


def queued_body(queued: inquiry_module.Queued) -> dict[str, Any]:
    """The third authoring branch: the inquiry was parked (#92, #15).

    **No `capability_token`, and that is forced rather than tidy.**
    `TokenMinter.mint` retires whichever token preceded it for a session, so
    minting one for the attempt the learner already has open would invalidate
    the token their live overlay is holding and 401 their next answer. The
    session id travels as what it has always been — an identifier, not a
    credential (D7, ADR-0007) — so the caller can say which quiz is in the way.

    Nothing model-authored crosses here unrendered: `topic` is plain text, as
    it is on `quiz_body`, and the queued topics are the learner's own words.

    `queued.others` rather than `attempt.queued_topics`: this inquiry is on
    that queue - `raise_inquiry` put it there - and `inquiry` above already
    names it, so sending the queue whole would render the question twice, once
    as the subject and once as a sibling of itself.
    """
    attempt = queued.attempt
    return {
        "kind": "queued",
        "inquiry": queued.inquiry,
        "quiz_session_id": attempt.session_id,
        "topic": attempt.topic,
        "queued_topics": list(queued.others),
    }


def displaced_body(
    body: dict[str, Any], *, displaced_session_id: Optional[str]
) -> dict[str, Any]:
    """An authored body, plus what starting it displaced.

    Null when nothing was: a `DirectAnswer` starts no attempt, so there is
    nothing for the displaced attempt's queue to carry forward onto and the
    domain leaves the open quiz alone.
    """
    return {**body, "displaced_session_id": displaced_session_id}


def _probe_body(probe) -> Optional[dict[str, Any]]:
    if probe is None:
        return None
    return {"blank_id": probe.blank_id, "question_html": html_of(probe.question)}


def _tutor_line_html(submission: session_module.Submission) -> Optional[str]:
    """The reactive tutor line, rendered, or null (D13, ADR-0013).

    Read off `model_grading` rather than off the submission, because that is
    where the field lives and why: the deterministic strategy never builds a
    `ModelGrading`, so Novice has no route to a tutor line at all rather than a
    field that happens always to be null. Reaching through the group is what
    keeps that structural on the wire too.
    """
    grading = submission.model_grading
    if grading is None or not grading.tutor_line:
        return None
    return html_of(grading.tutor_line)


def _resolved_html(resolved_answer: str | None) -> str | None:
    """What goes in the gap, rendered — or `None` when there is nothing to put
    there.

    `label_html` rather than `html_of`, for the reason `label_html` exists: the
    fragment lands inline in the middle of a sentence, and the block render's
    `<p>` would carry paragraph margins in with it (#98).

    This is the field that replaced the client scanning the rendered document
    for an option id
    ([ADR-0019](../../../docs/adr/0019-resolved-blank-text-comes-from-the-service.md),
    [#127](https://github.com/derkmed/socratic/issues/127)). A null here means
    the gap stays empty; it never means "fall back to what the learner typed",
    which is the defect that made an Advanced blank close showing the wrong
    answer.
    """
    if resolved_answer is None:
        return None
    return label_html(resolved_answer)


def submission_body(
    submission: session_module.Submission, *, capability_token: str
) -> dict[str, Any]:
    return {
        "verdict": submission.verdict.value,
        "graded_by": submission.graded_by.value,
        "hint_rung_shown": submission.hint_rung_shown,
        "feedback_html": (
            html_of(submission.feedback) if submission.feedback else None
        ),
        "tutor_line_html": _tutor_line_html(submission),
        "revealed_option_id": submission.revealed_option_id,
        "resolved_html": _resolved_html(submission.resolved_answer),
        "blank_resolved": submission.blank_resolved,
        "attempt_sealed": submission.attempt_sealed,
        "probe": _probe_body(submission.probe_asked),
        "capability_token": capability_token,
    }


def probe_answer_body(
    answer: session_module.ProbeAnswer, *, capability_token: str
) -> dict[str, Any]:
    return {
        "verdict": answer.verdict.value if answer.verdict else None,
        "correction_html": (
            html_of(answer.correction) if answer.correction else None
        ),
        "blank_reopened": answer.blank_reopened,
        "blank_resolved": answer.blank_resolved,
        "revealed_option_id": answer.revealed_option_id,
        "attempt_sealed": answer.attempt_sealed,
        "capability_token": capability_token,
    }


def probe_dismissal_body(dismissal, *, capability_token: str) -> dict[str, Any]:
    return {
        "dismissed": True,
        "attempt_sealed": getattr(dismissal, "attempt_sealed", False),
        "capability_token": capability_token,
    }
