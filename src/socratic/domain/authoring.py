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

**One blocking call, and only one** (D11,
[ADR-0011](../../../docs/adr/0011-latency-budget.md)). `author` makes the
skeleton call and returns a quiz that is already playable; `author_pedagogy`
makes the second call and merges reinforcements, hint rungs and the recap into
the **in-flight attempt**, which is where the learner's guesses are. Merging
into the stored attempt rather than into the quiz the caller is holding is the
guard the ADR asks for: a learner who answers inside the window loses nothing.
A `direct_answer` fires no second call - there is no exercise to add pedagogy
to, and no attempt to add it to.

Spec: `docs/specs/skeleton-and-pedagogy-split.md`, after
`docs/specs/quiz-authoring.md`.
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

SKELETON_STAGE = registry_module.AuthoringStage.SKELETON
COMPLETE_STAGE = registry_module.AuthoringStage.COMPLETE

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
        # The mode arrives from the wire as whatever string the request named,
        # and everything below stamps it on a `Quiz`, a `Blank` and a
        # `QuizAttempt`. Canonicalising it here - through the registry, which
        # is the only thing entitled to an opinion about mode - is what keeps
        # one type below the seam, instead of every reader of `.mode` having to
        # ask whether it got the member or the string (#89, #85).
        mode = self._registry.key_for(mode)
        policy = self._registry.policy_for(mode)

        segments = prompting.assemble(
            prompting.CallType.AUTHOR_SKELETON,
            profile=profile or prompting.LearnerProfile(learner_id=learner_id),
            probe_cadence=probe_cadence,
            quiz=None,
            inquiry=inquiry,
            # The bound the model is about to be judged against, said out loud
            # (#72). `ensure_valid_quiz` below still enforces it — this only
            # moves the mode's `blank_range` from a post-hoc rejection on the
            # one call a learner blocks on (ADR-0011) into the prompt that call
            # reads.
            blank_range=policy.blank_range,
            # The same mode, said to the *schema* rather than to the prose.
            # Segment 1 tells the model it is answering "against a schema
            # supplied with the request" and no prompt names the mode at all,
            # so this is what makes an Advanced call produce Advanced blanks
            # rather than novice-shaped ones the validator then refuses (#114).
            mode=mode,
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
        validation.ensure_valid_quiz(quiz, self._registry, stage=SKELETON_STAGE)
        self._persist(
            quiz,
            learner_id=learner_id,
            mode=mode,
            probe_cadence=probe_cadence,
            response=response,
        )
        return quiz

    def author_pedagogy(
        self,
        quiz: Quiz,
        learner_id: str,
        *,
        probe_cadence: ProbeCadence = ProbeCadence.SOMETIMES,
        profile: prompting.LearnerProfile | None = None,
    ) -> records.QuizAttempt:
        """Fetch the pedagogy payload and merge it into the in-flight attempt.

        The second of the two authoring calls (D11, ADR-0011). It is **not**
        blocking: the caller fires it off the render path, and it lands while
        the learner spends 30-60 seconds reading the explanation. Nothing here
        is on the learner's critical path, which is why it is a separate entry
        point rather than a tail of `author`.

        **The merge target is the stored attempt, re-read after the call.**
        That is the guard ADR-0011 asks for. A learner can answer inside the
        window, and their guess is appended to the attempt by the submit path;
        merging into the quiz this method was handed would write that guess
        away again. Re-reading is also what makes the sealed case visible.

        Args:
          quiz: The quiz `author` returned. Rendered into segment 2, and its
            session names the attempt to merge into.
          learner_id: Whose partition the attempt lives in.
          probe_cadence: The learner's setting. Never reaches the prompt
            (ADR-0010); taken so that stays assertable.
          profile: The profile rendered into segment 2, as for `author`.

        Returns:
          The attempt with the payload merged in - or unchanged, if it had
          already closed - sealed, or abandoned by displacement.

        Raises:
          KeyError: If the learner has no attempt for this quiz's session.
          AuthoringParseError: If the payload cannot be read, or names a blank
            the quiz does not declare.
          QuizValidationError: If the merged quiz is still not a complete one.
            Nothing is written, so the learner keeps the playable skeleton.
        """
        segments = prompting.assemble(
            prompting.CallType.AUTHOR_PEDAGOGY,
            profile=profile or prompting.LearnerProfile(learner_id=learner_id),
            probe_cadence=probe_cadence,
            quiz=quiz,
            # Through `key_for` so the schema is looked up under the key the
            # registry holds, whichever spelling the quiz was stored with (#89).
            mode=self._registry.key_for(quiz.mode),
        )
        response = self._model_client.author_pedagogy(segments)

        attempt = self._attempt_for(quiz, learner_id)
        if attempt.is_closed:
            # Closed means never written again, and pedagogy the learner will
            # not see has no reader. Dropping it is the whole handling: a late
            # payload is not an error. Both closed states land here - sealed,
            # where every blank is already resolved (CONTEXT: Sealed), and
            # abandoned, where the learner displaced the quiz inside the
            # window (#15).
            return attempt

        merged = _merge_pedagogy(attempt.quiz, _decode(response.content))
        validation.ensure_valid_quiz(merged, self._registry, stage=COMPLETE_STAGE)
        merged_attempt = attempt.with_model_call(
            _call_record(prompting.CallType.AUTHOR_PEDAGOGY, response)
        ).with_quiz(merged)
        self._attempts.save(merged_attempt)
        return merged_attempt

    def _attempt_for(self, quiz: Quiz, learner_id: str) -> records.QuizAttempt:
        """The attempt for this quiz's session.

        The session is deliberately a different key from the attempt id
        (ADR-0007), so the lookup belongs to the port rather than to a scan
        written out here — this path is one of several arriving holding a
        `QuizSessionId`, and against a store partitioned on `learner_id` the
        scan is a full partition read on the learner's hot path (#47).

        `get_by_session` reads the *latest* attempt on the session, which where
        [#15](https://github.com/derkmed/socratic/issues/15) has left a
        displaced one behind is the restart rather than the attempt it
        displaced. A sealed attempt still comes back: dropping a late payload
        is this method's caller's business, not the lookup's.
        """
        attempt = self._attempts.get_by_session(learner_id, quiz.quiz_session_id)
        if attempt is None:
            raise KeyError(
                f"no attempt for session {quiz.quiz_session_id!r} in "
                f"{learner_id!r}'s partition"
            )
        return attempt

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
        ).with_model_call(_call_record(prompting.CallType.AUTHOR_SKELETON, response))
        self._attempts.save(attempt)


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

    The **recap is optional here** and defaults to `""`: it rides the pedagogy
    payload (ADR-0011), so a skeleton response is not malformed for omitting
    one. Optional rather than forbidden - a skeleton that carries one anyway is
    kept, and the empty recap is what the complete-stage validator refuses.
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
            recap=_optional(payload, "recap", str, where) or "",
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


def _call_record(
    call_type: prompting.CallType,
    response: model_client_module.ModelResponse,
) -> records.ModelCallRecord:
    """One call, stamped for the attempt.

    Both authoring calls go through here, so both `message_id`s and both token
    counts land on the attempt the same way. The usage comes from
    `ModelResponse.token_usage()` rather than being reassembled counter by
    counter - there is one mapping to get wrong and it belongs to the port.
    """
    return records.ModelCallRecord(
        call_type=call_type.value,
        message_id=response.message_id,
        usage=response.token_usage(),
    )


def _merge_pedagogy(quiz: Quiz, payload: Mapping[str, Any]) -> Quiz:
    """Fold the pedagogy payload into the skeleton.

    Additive by construction: the payload carries only fields the skeleton
    left empty, so nothing the learner is already looking at moves. A blank
    the payload says nothing about keeps its absent pedagogy and is caught by
    the complete-stage validator, which is where that judgement belongs.

    Args:
      quiz: The skeleton as stored on the attempt.
      payload: The decoded `author_pedagogy` response.

    Returns:
      The merged quiz. Not validated here - the caller does that at
      `COMPLETE`, in one place, through the registry.

    Raises:
      AuthoringParseError: If the payload is malformed, or names a blank the
        quiz does not declare. An unknown blank id is the symptom of the two
        calls having drifted apart, and merging around it silently would leave
        a blank with no hints and no explanation of why.
    """
    where = "the pedagogy payload"
    entries = _pedagogy_entries(payload, quiz, where)
    return dataclasses.replace(
        quiz,
        recap=_require(payload, "recap", str, where),
        blanks=tuple(
            _with_pedagogy(blank, entries.get(blank.blank_id)) for blank in quiz.blanks
        ),
    )


def _pedagogy_entries(
    payload: Mapping[str, Any],
    quiz: Quiz,
    where: str,
) -> dict[str, Mapping[str, Any]]:
    """The payload's per-blank entries, keyed by blank id."""
    declared = {blank.blank_id for blank in quiz.blanks}
    entries: dict[str, Mapping[str, Any]] = {}
    for entry in _optional(payload, "blanks", list, where) or ():
        if not isinstance(entry, dict):
            raise AuthoringParseError(
                f"{where}: a blank is a {type(entry).__name__}, not an object"
            )
        blank_id = _require(entry, "blank_id", str, where)
        if blank_id not in declared:
            raise AuthoringParseError(
                f"{where} names blank {blank_id!r}, which the quiz does not "
                f"declare; it has {sorted(declared)}"
            )
        entries[blank_id] = entry
    return entries


def _with_pedagogy(blank: Blank, entry: Mapping[str, Any] | None) -> Blank:
    """One blank, with its reinforcement, hint ladder and probe question in.

    The probe question is merged on the same terms as the hints: pre-authored
    per blank (ADR-0011), and absent rather than fatal when the payload does
    not carry one.
    """
    if entry is None:
        return blank
    where = f"the pedagogy for blank {blank.blank_id!r}"
    hints = _optional(entry, "hints", list, where)
    return dataclasses.replace(
        blank,
        reinforcement=_optional(entry, "reinforcement", str, where),
        hints=None if hints is None else tuple(_hint(hint, where) for hint in hints),
        probe_question=_optional(entry, "probe_question", str, where),
    )
