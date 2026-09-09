"""Where a learner's inquiry arrives, and what an open quiz does to it.

The single-topic-focus rule with an escape hatch. `QuizAuthoring.author` answers
one question; this module decides whether the question gets asked at all right
now, because the learner may already have a quiz open:

* **Nothing open** — the inquiry is authored, and a `Started` comes back.
* **A quiz open** — the inquiry is **queued** on that attempt and a `Queued`
  comes back. No model call is made: parking a question is free, and the quiz in
  front of the learner is not pulled away from them.
* **"Start this instead"** — the explicit escape. The open attempt is marked
  `abandoned`, the new one is authored, and the rest of the queue carries
  forward onto it (master spec acceptance 26).

**`abandoned` is written here and nowhere else, and only on that explicit
displacement.** There is no sweeper, no timeout and no age heuristic anywhere in
this module: a closed tab tells us nothing, so `in_flight` stays the stored
truth until the learner says otherwise and readers apply their own age threshold
(CONTEXT: Outcome). `sealed_at` stays null on a displaced attempt - it is the
completion stamp, and nothing was completed.

Why this is its own module rather than two more methods on `QuizAuthoring`: the
queue-or-author decision reads the learner's partition, which the authoring path
does not do and should not learn to. `QuizSession` is the same shape on the
submit side - a composition over a collaborator and the repository - and this is
its peer on the way in.

Spec: `docs/specs/queued-inquiries-and-displacement.md`, issue #15.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from typing import Union

from socratic.domain import authoring as authoring_module
from socratic.domain import prompting
from socratic.domain import registry as registry_module
from socratic.domain import repositories
from socratic.domain.modes import ProbeCadence
from socratic.domain.records import Outcome, QuizAttempt
from socratic.domain.types import AuthoringResult, DirectAnswer


@dataclasses.dataclass(frozen=True, slots=True)
class Queued:
    """The inquiry was parked, and the quiz the learner has open carries on.

    Attributes:
      attempt: The live attempt, as stored, with the inquiry on its queue.
      inquiry: What was queued, whitespace-trimmed - which is the form it sits
        in `attempt.queued_topics` under.
    """

    attempt: QuizAttempt
    inquiry: str

    @property
    def others(self) -> tuple[str, ...]:
        """The queue as a reader should show it: everything *but* this inquiry.

        `raise_inquiry` puts the question on the attempt's queue before
        building this, because the queue is what persists it - so
        `attempt.queued_topics` legitimately contains `inquiry`, and a renderer
        that showed the queue whole would list the question it is already
        announcing as a sibling of itself.

        The same exclusion `_start` makes when it carries a queue forward past
        the question being started, for the same reason.
        """
        return _without(self.attempt.queued_topics, self.inquiry)


@dataclasses.dataclass(frozen=True, slots=True)
class Started:
    """The inquiry was answered: a quiz was authored, or answered directly.

    Attributes:
      result: The `Quiz | DirectAnswer` union `QuizAuthoring.author` returned.
      attempt: The in-flight attempt written for it, or `None` for a direct
        answer - which persists no attempt, there being no exercise to record.
      displaced: The attempt this one displaced, already marked `abandoned`,
        or `None` when nothing was displaced.
    """

    result: AuthoringResult
    attempt: QuizAttempt | None
    displaced: QuizAttempt | None


Intake = Union[Queued, Started]
"""What an inquiry became. Returned as a value, like `AuthoringResult`, so the
queued branch is testable rather than inferred from a side effect."""


class InquiryIntake:
    """The door an inquiry arrives at, over authoring and the attempt store."""

    def __init__(
        self,
        *,
        authoring: authoring_module.QuizAuthoring,
        attempts: repositories.AttemptRepository,
    ) -> None:
        self._authoring = authoring
        self._attempts = attempts

    def live_attempt(self, learner_id: str) -> QuizAttempt | None:
        """The quiz this learner has open, if any.

        The latest `in_flight` attempt in their partition. Latest by ULID,
        which is mint order (ADR-0005); `in_flight` rather than "not closed"
        because those are the same set - an attempt is in flight, sealed, or
        abandoned, and the last two are exactly the closed ones.
        """
        open_attempts = [
            attempt
            for attempt in self._attempts.list_for_learner(learner_id)
            if attempt.outcome is Outcome.IN_FLIGHT
        ]
        return open_attempts[-1] if open_attempts else None

    def raise_inquiry(
        self,
        inquiry: str,
        learner_id: str,
        *,
        mode: registry_module.ModeKey,
        probe_cadence: ProbeCadence = ProbeCadence.SOMETIMES,
        profile: prompting.LearnerProfile | None = None,
    ) -> Intake:
        """Take a question from a learner: author it, or queue it.

        Args:
          inquiry: What the learner asked, verbatim.
          learner_id: Whose partition is read and written.
          mode: The learner's difficulty mode, as `QuizAuthoring.author` takes
            it. Ignored on the queued branch - a queued topic is not authored
            here, so it binds no mode (CONTEXT: Mode toggle).
          probe_cadence: The learner's setting, stamped on any attempt written.
          profile: The profile rendered into segment 2.

        Returns:
          `Queued` if a quiz was already open, `Started` otherwise.

        Raises:
          ValueError: If `inquiry` is blank - on either branch, so a stray
            keystroke never lands on the queue.
        """
        asked = _trimmed(inquiry)
        live = self.live_attempt(learner_id)
        if live is None:
            return self._start(
                inquiry,
                learner_id,
                mode=mode,
                probe_cadence=probe_cadence,
                profile=profile,
                displaced=None,
            )

        queued = live.with_queued_topics(
            _distinct((*live.queued_topics, asked))
        )
        self._attempts.save(queued)
        return Queued(attempt=queued, inquiry=asked)

    def start_this_instead(
        self,
        inquiry: str,
        learner_id: str,
        *,
        mode: registry_module.ModeKey,
        probe_cadence: ProbeCadence = ProbeCadence.SOMETIMES,
        profile: prompting.LearnerProfile | None = None,
    ) -> Started:
        """Displace the open quiz and author this inquiry instead.

        The one operation that writes `abandoned`, and it writes it only
        because the learner said so.

        **Authoring happens first.** A displacement that abandoned the open
        attempt and then failed to parse a response would leave the learner
        with nothing at all; this way the quiz they have stays theirs until
        there is one to replace it.

        A `DirectAnswer` therefore displaces nothing: nothing was started, and
        there would be no attempt for the queue to carry forward onto.

        Args:
          inquiry: The question to start on instead. As `raise_inquiry`.
          learner_id: Whose partition is read and written.
          mode: The mode the *new* attempt is authored in - the current one,
            not the displaced attempt's (CONTEXT: Mode toggle).
          probe_cadence: The learner's setting, stamped on the new attempt.
          profile: The profile rendered into segment 2.

        Returns:
          A `Started` naming the new attempt and, when there was one, the
          attempt it displaced, already `abandoned` and stored.

        Raises:
          ValueError: If `inquiry` is blank. Nothing is displaced.
        """
        _trimmed(inquiry)
        return self._start(
            inquiry,
            learner_id,
            mode=mode,
            probe_cadence=probe_cadence,
            profile=profile,
            displaced=self.live_attempt(learner_id),
        )

    def _start(
        self,
        inquiry: str,
        learner_id: str,
        *,
        mode: registry_module.ModeKey,
        probe_cadence: ProbeCadence,
        profile: prompting.LearnerProfile | None,
        displaced: QuizAttempt | None,
    ) -> Started:
        """Author `inquiry`, then close and drain what it displaced."""
        result = self._authoring.author(
            inquiry,
            learner_id,
            mode=mode,
            probe_cadence=probe_cadence,
            profile=profile,
        )
        if isinstance(result, DirectAnswer):
            return Started(result=result, attempt=None, displaced=None)

        attempt = self._attempt_for(result.quiz_session_id, learner_id)
        if displaced is None:
            return Started(result=result, attempt=attempt, displaced=None)

        abandoned = displaced.abandoned()
        self._attempts.save(abandoned)

        carried = _distinct(
            (
                *_without(abandoned.queued_topics, inquiry),
                *attempt.queued_topics,
            )
        )
        attempt = attempt.with_queued_topics(carried)
        self._attempts.save(attempt)
        return Started(result=result, attempt=attempt, displaced=abandoned)

    def _attempt_for(self, session_id: str, learner_id: str) -> QuizAttempt:
        """The attempt authoring just wrote for this session.

        Read back rather than reconstructed: the record is the store's, and
        the queue is written onto the document that is actually there.
        """
        attempt = self._attempts.get_by_session(learner_id, session_id)
        if attempt is None:
            raise KeyError(
                f"no attempt for session {session_id!r} in {learner_id!r}'s "
                "partition"
            )
        return attempt


def _trimmed(inquiry: str) -> str:
    """The inquiry as it is queued, or a refusal.

    The same rejection `QuizAuthoring.author` makes, made here too because the
    queued branch never reaches it.
    """
    asked = inquiry.strip()
    if not asked:
        raise ValueError("an inquiry is required to author against")
    return asked


def _distinct(topics: Iterable[str]) -> tuple[str, ...]:
    """The queue, trimmed, in first-asked order, each question once.

    Distinct is CONTEXT's word for it: the queue is the questions a learner
    raised, not the number of times they raised them. Exact comparison after
    trimming - telling two phrasings of one question apart would take a model
    call, and a duplicate topic costs the learner far less than that.
    """
    seen: list[str] = []
    for topic in topics:
        trimmed = topic.strip()
        if trimmed and trimmed not in seen:
            seen.append(trimmed)
    return tuple(seen)


def _without(topics: Sequence[str], started: str) -> tuple[str, ...]:
    """The queue less the question now being answered.

    A learner who reaches for a queued topic by name would otherwise find it
    still waiting on the quiz that answers it.
    """
    answered = started.strip()
    return tuple(topic for topic in topics if topic.strip() != answered)
