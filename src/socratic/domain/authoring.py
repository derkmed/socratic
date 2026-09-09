"""The authoring path: an inquiry in, a quiz or a direct answer out.

`QuizAuthoring.author` is the single entry to the most complex path in the
system (master spec seam table). It assembles the prompt, makes the one
blocking `author_skeleton` call, parses the structured response, validates the
quiz through the mode registry, persists an in-flight `QuizAttempt` stamped
with the call's `message_id` and token usage, and returns the result.

**The union comes back as a value.** `AuthoringResult` is `Quiz | DirectAnswer`
(D2, [ADR-0004](../../../docs/adr/0004-quiz-wire-format.md)), so the override
branch — medical, legal, financial, security, an active outage, or a learner
who has said plainly that they need the answer now — is testable as a return
value rather than inferred from a side effect. A direct answer parses on its
own shape and never reads blanks that are not there, and it persists no
attempt: there is no quiz session to record.

**Nothing here validates.** `validation.ensure_valid_quiz` is the gate, and it
reaches the mode's `blank_range` through the registry, so the pedagogical bound
is the registry's number rather than one restated here. An `if mode ==` outside
the registry is a bug (CONTEXT: ModeRegistry).

**One call, and only one** (D11,
[ADR-0011](../../../docs/adr/0011-latency-budget.md)). The skeleton is the
blocking call; the pedagogy payload that rides behind it is
[#9](https://github.com/derkmed/socratic/issues/9) and is deliberately absent,
so the skeleton response carries the whole blank shape the registry's
`authoring_schema_fragment` already declares.

Spec: `docs/specs/quiz-authoring.md`.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable, Mapping, Sequence

from socratic.domain import ids
from socratic.domain import model_client as model_client_module
from socratic.domain import prompting
from socratic.domain import records
from socratic.domain import registry as registry_module
from socratic.domain import repositories
from socratic.domain import validation
from socratic.domain.modes import ProbeCadence
from socratic.domain.types import (
    AuthoringResult,
    Blank,
    BlankSegment,
    DirectAnswer,
    MathSegment,
    Option,
    Quiz,
    Segment,
    TextSegment,
)

MODEL_ID = "claude-opus-5"
"""ADR-0014. An admin-level `Valve`, never `UserValves` — caches are
model-scoped, so a per-learner choice would fragment every segment 1."""

EFFORT = "low"
"""ADR-0014. Uniform across call types until measurement moves it; changing it
invalidates that call type's cache prefix."""

PROMPT_VERSION = "2026-09-08.1"
"""Stamped on the attempt so a curation job reading an old record knows which
instructions produced it. A constant for now, overridable at construction;
deriving it from the assembled prompt is not this ticket's decision."""

DIRECT_ANSWER = "direct_answer"
QUIZ = "quiz"

_TEXT = "text"
_MATH = "math"
_BLANK = "blank"


class AuthoringParseError(ValueError):
    """A structured response the authoring path cannot read.

    Its own type because a malformed response is an expected failure of an
    external system, not a bug in a dataclass: the caller needs to be able to
    tell "the model returned nonsense" from "we built the wrong object", and a
    `TypeError` escaping from inside `Quiz.__post_init__` tells it neither.
    """


class QuizAuthoring:
    """Authors a quiz, or answers directly, for one inquiry at a time."""

    def __init__(
        self,
        *,
        model_client: model_client_module.ModelClient,
        attempts: repositories.AttemptRepository,
        registry: registry_module.ModeRegistry | None = None,
        clock: ids.Clock = ids.system_clock,
        model_id: str = MODEL_ID,
        effort: str = EFFORT,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        """Wire the service.

        Args:
          model_client: The `ModelClient` port. In tests this is
            `RecordingModelClient`, which *is* the double.
          attempts: Where the in-flight attempt is written.
          registry: Where `blank_range` and the per-mode validator come from.
            Defaults to `default_registry()`.
          clock: Milliseconds since the epoch. Injected so a frozen clock makes
            the minted ids and the attempt's `created_at` deterministic.
          model_id: Stamped on the attempt (ADR-0014).
          effort: Stamped on the attempt (ADR-0014).
          prompt_version: Stamped on the attempt.
        """
        self._model_client = model_client
        self._attempts = attempts
        self._registry = registry or registry_module.default_registry()
        self._clock = clock
        self._model_id = model_id
        self._effort = effort
        self._prompt_version = prompt_version

    def author(
        self,
        inquiry: str,
        learner_id: str,
        *,
        mode: registry_module.ModeKey,
        probe_cadence: ProbeCadence = ProbeCadence.SOMETIMES,
        profile: prompting.LearnerProfile | None = None,
    ) -> AuthoringResult:
        """Author a quiz for `inquiry`, or answer it directly.

        Args:
          inquiry: What the learner asked, verbatim. Rides the volatile tail —
            it is per-request, so caching it would write a fresh entry on every
            call and read none of them.
          learner_id: Whose partition the attempt is written to.
          mode: The learner's difficulty mode, which the mode toggle fixes at
            authoring time — one attempt, one mode (CONTEXT: Mode toggle).
            Where the setting comes from belongs to
            [#14](https://github.com/derkmed/socratic/issues/14).
          probe_cadence: The learner's `UserValves` setting, recorded on the
            attempt as `probe_cadence_at_authoring` so `off` stays visible even
            when zero probes fire (acceptance 21). It never reaches the prompt.
          profile: The learner profile rendered into segment 2. Defaults to an
            empty profile for this learner; reading a stored one is
            [#16](https://github.com/derkmed/socratic/issues/16).

        Returns:
          A validated `Quiz`, or a `DirectAnswer` when the model took the
          override branch. The union is the return value, not a side effect.

        Raises:
          ValueError: If `inquiry` is blank.
          KeyError: If no policy is registered for `mode` — a wiring mistake,
            caught before the model is consulted rather than after.
          AuthoringParseError: If the response cannot be read as the ADR-0004
            union.
          QuizValidationError: If the authored quiz is inadmissible — the
            mode's `blank_range` and the conditional blank rules.
        """
        if not inquiry.strip():
            raise ValueError("an inquiry is required to author against")
        self._registry.policy_for(mode)

        segments = _with_inquiry(
            prompting.assemble(
                prompting.CallType.AUTHOR_SKELETON,
                profile=profile or prompting.LearnerProfile(learner_id=learner_id),
                probe_cadence=probe_cadence,
                quiz=None,
            ),
            inquiry,
        )
        response = self._model_client.author_skeleton(segments)

        payload = _decode(response.content)
        kind = _require(payload, "type", str, "the authoring response")
        if kind == DIRECT_ANSWER:
            # No quiz session, so nothing to persist: a direct answer is an
            # answer, not an exercise the learner is working through.
            return _parse_direct_answer(payload)
        if kind != QUIZ:
            raise AuthoringParseError(
                f"unknown authoring result type {kind!r}; expected "
                f"{QUIZ!r} or {DIRECT_ANSWER!r}"
            )

        quiz = _parse_quiz(
            payload,
            quiz_session_id=ids.new_quiz_session_id(clock=self._clock),
            mode=mode,
        )
        validation.ensure_valid_quiz(quiz, self._registry)
        self._persist(
            quiz,
            learner_id=learner_id,
            mode=mode,
            probe_cadence=probe_cadence,
            response=response,
        )
        return quiz

    def _persist(
        self,
        quiz: Quiz,
        *,
        learner_id: str,
        mode: registry_module.ModeKey,
        probe_cadence: ProbeCadence,
        response: model_client_module.ModelResponse,
    ) -> None:
        """Write the in-flight attempt, stamped with the call that made it.

        The attempt is keyed by its own ULID rather than by the
        `QuizSessionId`: both are minted here, and keeping them distinct is
        what lets a later displaced-and-restarted session
        ([#15](https://github.com/derkmed/socratic/issues/15)) hold more than
        one attempt without either key having to mean two things.
        """
        attempt_id = ids.Ulid.mint(clock=self._clock)
        attempt = records.QuizAttempt(
            attempt_id=str(attempt_id),
            learner_id=learner_id,
            session_id=quiz.quiz_session_id,
            quiz=quiz,
            mode=mode,
            topic=quiz.topic,
            created_at=attempt_id.timestamp,
            probe_cadence_at_authoring=probe_cadence,
            model_id=self._model_id,
            effort=self._effort,
            prompt_version=self._prompt_version,
            queued_topics=quiz.queued_topics,
        ).with_model_call(
            records.ModelCallRecord(
                call_type=prompting.CallType.AUTHOR_SKELETON.value,
                message_id=response.message_id,
                usage=_usage(response),
            )
        )
        self._attempts.save(attempt)


def _usage(response: model_client_module.ModelResponse) -> records.TokenUsage:
    """The call's token usage, as far as the port reports it.

    `ModelResponse` carries the two cache counters and nothing else today, so
    the plain input and output counts are zero until the Anthropic adapter
    ([#7](https://github.com/derkmed/socratic/issues/7)) has somewhere to put
    them. The cache counters are the ones the build gate reads, so what matters
    for acceptance 12 is already here.
    """
    return records.TokenUsage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=response.cache_creation_input_tokens,
        cache_read_input_tokens=response.cache_read_input_tokens,
    )


def _with_inquiry(
    segments: prompting.PromptSegments, inquiry: str
) -> prompting.PromptSegments:
    """Put the inquiry at the head of the volatile tail.

    `assemble` takes no `inquiry` parameter — its tail is shaped for the
    grading calls, which carry guesses rather than a question — so the
    authoring path adds it here rather than reaching into `prompting.py`. The
    tail is the right place regardless: the inquiry is per-request, so it must
    sit below the last cache breakpoint.
    """
    tail = segments.volatile_tail
    amended = dataclasses.replace(
        tail, text=f"## The learner's inquiry\n  {inquiry}\n{tail.text}"
    )
    return dataclasses.replace(
        segments,
        segments=tuple(
            amended
            if segment.role is prompting.SegmentRole.VOLATILE_TAIL
            else segment
            for segment in segments.segments
        ),
    )


# --- Parsing -----------------------------------------------------------------
#
# `ModelResponse.content` is the structured-output payload verbatim, and the
# schema is ADR-0004's tagged union. Every failure below is an
# `AuthoringParseError` carrying the field that went wrong, because a
# structured-output response that does not match its own schema is the symptom
# of a prompt or schema problem and the field name is the whole diagnosis.


def _decode(content: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise AuthoringParseError(
            f"the authoring response is not JSON: {error}"
        ) from None
    if not isinstance(payload, dict):
        raise AuthoringParseError(
            "the authoring response is not a JSON object, but a "
            f"{type(payload).__name__}"
        )
    return payload


def _require(payload: Mapping[str, Any], key: str, kind: type, where: str):
    """One required field of a known type, or a parse error naming it."""
    if key not in payload:
        raise AuthoringParseError(f"{where} has no {key!r}")
    value = payload[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise AuthoringParseError(
            f"{where}: {key!r} should be a {kind.__name__}, got "
            f"{type(value).__name__}"
        )
    return value


def _optional(payload: Mapping[str, Any], key: str, kind: type, where: str):
    """A nullable field: absent and `null` both mean nothing was authored."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, kind) or isinstance(value, bool):
        raise AuthoringParseError(
            f"{where}: {key!r} should be a {kind.__name__} or null, got "
            f"{type(value).__name__}"
        )
    return value


def _strings(payload: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """A list of strings, absent meaning empty."""
    values = _optional(payload, key, list, where)
    if values is None:
        return ()
    for value in values:
        if not isinstance(value, str):
            raise AuthoringParseError(
                f"{where}: {key!r} holds a {type(value).__name__}, not a string"
            )
    return tuple(values)


def _parse_direct_answer(payload: Mapping[str, Any]) -> DirectAnswer:
    """The override branch, parsed on its own shape.

    It reads no blanks, no explanation and no recap — the fields are not there,
    and a parser that went looking for them would turn the highest-stakes
    question a learner asks into an exception (ADR-0004).
    """
    where = "the direct answer"
    return DirectAnswer(
        answer=_require(payload, "answer", str, where),
        topic=_require(payload, "topic", str, where),
        queued_topics=_strings(payload, "queued_topics", where),
    )


def _parse_quiz(
    payload: Mapping[str, Any],
    *,
    quiz_session_id: str,
    mode: registry_module.ModeKey,
) -> Quiz:
    """Build the quiz, turning structural refusals into parse errors.

    `Quiz.__post_init__` refuses duplicate blank ids and blanks that no segment
    references. Those are malformed authoring responses like any other, so they
    surface as `AuthoringParseError` rather than as a `ValueError` from inside
    a dataclass the caller never named.
    """
    where = "the authored quiz"
    explanation = _parse_explanation(
        _require(payload, "explanation", list, where)
    )
    blanks = tuple(
        _parse_blank(entry, mode=mode)
        for entry in _require(payload, "blanks", list, where)
    )
    try:
        return Quiz(
            quiz_session_id=quiz_session_id,
            mode=mode,
            topic=_require(payload, "topic", str, where),
            explanation=explanation,
            blanks=blanks,
            recap=_require(payload, "recap", str, where),
            queued_topics=_strings(payload, "queued_topics", where),
        )
    except ValueError as error:
        raise AuthoringParseError(f"{where} is malformed: {error}") from None


def _parse_explanation(entries: Sequence[Any]) -> tuple[Segment, ...]:
    """The flat segment array: `text | math | blank`, in reading order.

    Flat by construction — no segment type holds another — so a nested blank
    has nowhere to live and the parser needs no depth limit.
    """
    return tuple(_parse_segment(entry) for entry in entries)


_SEGMENTS: Mapping[str, tuple[str, Callable[[str], Segment]]] = {
    _TEXT: ("text", TextSegment),
    _MATH: ("mathml", MathSegment),
    _BLANK: ("blank_id", BlankSegment),
}


def _parse_segment(entry: Any) -> Segment:
    where = "an explanation segment"
    if not isinstance(entry, dict):
        raise AuthoringParseError(
            f"{where} is a {type(entry).__name__}, not an object"
        )
    kind = _require(entry, "type", str, where)
    try:
        field, build = _SEGMENTS[kind]
    except KeyError:
        raise AuthoringParseError(
            f"unknown explanation segment type {kind!r}; expected one of "
            f"{', '.join(sorted(_SEGMENTS))}"
        ) from None
    return build(_require(entry, field, str, f"a {kind!r} segment"))


def _parse_blank(entry: Any, *, mode: registry_module.ModeKey) -> Blank:
    """One blank, stamped with the quiz's mode.

    The response carries no per-blank mode: one attempt, one mode (CONTEXT:
    Mode toggle), so stamping it here makes a mismatch unrepresentable rather
    than something the validator has to catch. Which fields must be populated
    is the registry's business, not this parser's — a blank that is empty for
    its mode parses fine here and is rejected by `validate_quiz`.
    """
    where = "a blank"
    if not isinstance(entry, dict):
        raise AuthoringParseError(f"{where} is a {type(entry).__name__}")
    blank_id = _require(entry, "blank_id", str, where)
    where = f"blank {blank_id!r}"
    hints = _optional(entry, "hints", list, where)
    return Blank(
        blank_id=blank_id,
        mode=mode,
        options=_parse_options(entry, where),
        correct_option_id=_optional(entry, "correct_option_id", str, where),
        reinforcement=_optional(entry, "reinforcement", str, where),
        hints=None if hints is None else tuple(_hint(hint, where) for hint in hints),
        rubric=_optional(entry, "rubric", str, where),
    )


def _hint(hint: Any, where: str) -> str:
    if not isinstance(hint, str):
        raise AuthoringParseError(
            f"{where}: a hint rung is a {type(hint).__name__}, not a string"
        )
    return hint


def _parse_options(
    entry: Mapping[str, Any], where: str
) -> tuple[Option, ...] | None:
    """The option bank, or `None` where the mode authors none.

    An empty list is kept as an empty tuple rather than folded to `None`: the
    two mean different things to the conditional validator, and quietly
    rewriting one into the other would hide a malformed response behind the
    wrong error message.
    """
    options = _optional(entry, "options", list, where)
    if options is None:
        return None
    parsed = []
    for option in options:
        if not isinstance(option, dict):
            raise AuthoringParseError(
                f"{where}: an option is a {type(option).__name__}, not an object"
            )
        parsed.append(
            Option(
                option_id=_require(option, "option_id", str, f"{where}: an option"),
                text=_require(option, "text", str, f"{where}: an option"),
            )
        )
    return tuple(parsed)
