"""The persisted record shape (D5, D7, D9; ADR-0005, ADR-0007, ADR-0009).

Capture is a first-class goal, not a byproduct of grading. A later curation job
must be able to replay *what actually happened* from these records alone, so the
attempt is stamped and complete from day one: `model_id`, `effort`,
`schema_version` and `prompt_version` are what make "without a migration" true,
and the Anthropic `message_id`s plus per-call token usage are the audit link
back to the exact API calls behind a stored quiz.

**Records are immutable.** An attempt accumulates by returning a new value -
`with_guess`, `with_probe`, `with_model_call`, `sealed`, `abandoned` - which is
the read-modify-write ADR-0005 already describes and makes "the attempt document
is unchanged after sealing" (acceptance 24) structural rather than a convention.

**Probes are a peer of guesses, never nested on one.** A probe mutates blank
state, so it is an event in the timeline; modelling it as a property of a past
guess would misrepresent the timeline to exactly the consumer that wants to
replay it (ADR-0009).

**The bounds are enforced, not assumed.** At most 20 blanks, at most 4 guesses
and 2 probes per blank, so at most 80 guesses and 40 probes. Unbounded document
growth is the shape that kills document stores, so every construction checks -
and since every write goes through a record, construction *is* write time.

**The learner profile is not one of these records.** It is a rewritten-in-place
aggregate rather than an immutable event, and the assembler needs it too, so it
lives in `socratic.domain.profiles` - a leaf neither this module nor
`prompting.py` has to import through the other (D8, ADR-0008; #36).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from socratic.domain.ids import QuizSessionId, Ulid
from socratic.domain.modes import GradingStrategy, ProbeCadence
from socratic.domain.types import Quiz

SCHEMA_VERSION = "1"
"""The shape of the records in this module. Stamped on every attempt so a
curation job reading an old one knows what it is reading."""

MAX_BLANKS = 20
"""ADR-0005. Raising this is the trigger to migrate to a guess event stream."""

MAX_GUESSES_PER_BLANK = 4
"""One correct, then up to three more as the ladder resumes (ADR-0009)."""

MAX_PROBES_PER_BLANK = 2
"""The probe, and the one after a re-open (ADR-0009)."""

MAX_GUESSES = MAX_BLANKS * MAX_GUESSES_PER_BLANK
MAX_PROBES = MAX_BLANKS * MAX_PROBES_PER_BLANK

HINT_LADDER_RUNGS = 3
"""The three escalating responses to a wrong answer (CONTEXT: Hint ladder)."""


class Verdict(str, Enum):
    """The graded outcome of a submission, as persisted."""

    CORRECT = "correct"
    INCORRECT = "incorrect"


class Outcome(str, Enum):
    """How an attempt ended (CONTEXT: Outcome).

    `IN_FLIGHT` is the stored truth for an unfinished quiz; `ABANDONED` is
    written only on displacement, when the learner explicitly starts something
    else. **We never guess that a learner left** - a closed tab tells us
    nothing, so readers apply their own age threshold.
    """

    IN_FLIGHT = "in_flight"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Per-call token usage. `cache_read_input_tokens` is the one the caching
    build gate reads (spec Approach section 5, acceptance 12)."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    """One Anthropic call this attempt made.

    `call_type` is held as a plain string rather than an enum: the call-type
    inventory belongs to `ModelClient`, and a record that outlives the code that
    wrote it should not be able to fail to load because a name was retired.
    """

    call_type: str
    message_id: str
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class Guess:
    """One submission (CONTEXT: Guess).

    `graded_by` reuses `GradingStrategy` rather than minting a parallel
    `deterministic | model` enum - D4 already named that distinction.
    """

    blank_id: str
    submitted: str
    verdict: Verdict
    attempt_ordinal: int
    hint_rung_shown: int | None
    created_at: datetime
    graded_by: GradingStrategy

    def __post_init__(self) -> None:
        if not 1 <= self.attempt_ordinal <= MAX_GUESSES_PER_BLANK:
            raise ValueError(
                f"attempt ordinal runs 1-{MAX_GUESSES_PER_BLANK}, got "
                f"{self.attempt_ordinal}"
            )
        if self.hint_rung_shown is not None and not (
            1 <= self.hint_rung_shown <= HINT_LADDER_RUNGS
        ):
            raise ValueError(
                f"the hint rung runs 1-{HINT_LADDER_RUNGS}, got "
                f"{self.hint_rung_shown}"
            )


@dataclass(frozen=True, slots=True)
class Probe:
    """One self-explanation probe (CONTEXT: Probe, ADR-0009).

    Nullable from `self_explanation` onwards because a probe is dismissible: the
    question having been asked is still a fact worth keeping, and a dismissed
    probe persists with a null self-explanation without blocking sealing
    (acceptance 18).

    `cadence_at_fire` is recorded alongside the attempt's
    `probe_cadence_at_authoring` so a mid-quiz change to the setting stays exact.

    `dismissed_at` is what separates a dismissed probe from one still waiting on
    the learner. Both persist with a null `self_explanation`, so without the
    marker the two states are identical from the record alone - and one seal
    predicate cannot then honour both "a pending probe blocks sealing"
    (acceptance 16) and "a dismissed one does not" (acceptance 18). It is a
    marker on the probe rather than a second clause in the predicate, so sealing
    stays a single question asked of a single collection.
    """

    blank_id: str
    question: str
    self_explanation: str | None
    verdict: Verdict | None
    reopened_blank: bool
    cadence_at_fire: ProbeCadence
    asked_at: datetime
    answered_at: datetime | None
    message_id: str | None
    dismissed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.is_answered and self.verdict is None:
            raise ValueError("an answered probe carries a verdict")
        if self.is_answered and self.is_dismissed:
            raise ValueError("a probe is answered or dismissed, never both")
        if not self.is_answered:
            if self.verdict is not None:
                raise ValueError("an unanswered probe carries no verdict")
            if self.reopened_blank:
                raise ValueError("an unanswered probe cannot have re-opened a blank")

    @property
    def is_answered(self) -> bool:
        return self.self_explanation is not None

    @property
    def is_dismissed(self) -> bool:
        """The learner waved the question away (ADR-0010).

        The escape that lets a probe already on screen *stand* when
        `probe_cadence` is turned off mid-quiz: the question is not withdrawn,
        the learner declines it, and the attempt is free to seal.
        """
        return self.dismissed_at is not None


@dataclass(frozen=True, slots=True)
class QuizAttempt:
    """One document per quiz session, partitioned on `learner_id`.

    Holds the raw unfilled quiz exactly as authored plus two ordered
    collections, `guesses` and `probes`, which a reader merges by timestamp when
    it wants a timeline.

    **Write through the `with_*` methods, never `dataclasses.replace`.** Each
    one refuses a closed attempt - sealed or abandoned (CONTEXT: Closed);
    `dataclasses.replace` goes straight to `__init__` and so goes round that
    guard without complaining.
    The guard cannot be moved into `__post_init__`, because reconstructing a
    sealed attempt field-for-field is exactly what a repository does when it
    materialises a stored document, and `__post_init__` sees only the value it
    is handed - it has no way to tell that apart from a caller rebuilding a
    sealed attempt with new content. So `dataclasses.replace` on this record is
    discouraged here rather than blocked there.
    """

    attempt_id: str
    learner_id: str
    session_id: QuizSessionId
    quiz: Quiz
    mode: str
    topic: str
    created_at: datetime
    probe_cadence_at_authoring: ProbeCadence
    model_id: str
    effort: str
    prompt_version: str
    schema_version: str = SCHEMA_VERSION
    sealed_at: datetime | None = None
    outcome: Outcome = Outcome.IN_FLIGHT
    queued_topics: tuple[str, ...] = ()
    model_calls: tuple[ModelCallRecord, ...] = ()
    guesses: tuple[Guess, ...] = ()
    probes: tuple[Probe, ...] = ()

    def __post_init__(self) -> None:
        try:
            Ulid.parse(self.attempt_id)
        except ValueError as error:
            raise ValueError(
                f"an attempt is keyed by a ULID: {self.attempt_id!r} ({error})"
            ) from None

        if self.session_id != self.quiz.quiz_session_id:
            raise ValueError(
                f"an attempt and its quiz name one session: "
                f"{self.session_id!r} against {self.quiz.quiz_session_id!r}"
            )

        if len(self.quiz.blanks) > MAX_BLANKS:
            raise ValueError(
                f"a quiz carries at most {MAX_BLANKS} blanks, got "
                f"{len(self.quiz.blanks)}"
            )

        known = {blank.blank_id for blank in self.quiz.blanks}
        for event in (*self.guesses, *self.probes):
            if event.blank_id not in known:
                raise ValueError(f"unknown blank on this quiz: {event.blank_id!r}")

        self._check_per_blank_bound(
            [guess.blank_id for guess in self.guesses],
            MAX_GUESSES_PER_BLANK,
            "guesses",
        )
        self._check_per_blank_bound(
            [probe.blank_id for probe in self.probes], MAX_PROBES_PER_BLANK, "probes"
        )

        # `sealed_at` is the completion stamp, so it pairs with `resolved`
        # alone (#15). An abandoned attempt was left, not finished: it is
        # closed against further writes, but it carries no sealed_at, and an
        # in-flight one never did.
        if self.outcome is Outcome.RESOLVED and self.sealed_at is None:
            raise ValueError("a resolved attempt needs a sealed_at")
        if self.outcome is not Outcome.RESOLVED and self.sealed_at is not None:
            raise ValueError(f"a {self.outcome.value} attempt has no sealed_at")

    @staticmethod
    def _check_per_blank_bound(
        blank_ids: list[str], bound: int, noun: str
    ) -> None:
        for blank_id in set(blank_ids):
            count = blank_ids.count(blank_id)
            if count > bound:
                raise ValueError(
                    f"a blank carries at most {bound} {noun}, {blank_id!r} has "
                    f"{count}"
                )

    @property
    def is_sealed(self) -> bool:
        """A completed attempt, stamped on sealing (CONTEXT: Sealed)."""
        return self.sealed_at is not None

    @property
    def is_closed(self) -> bool:
        """An attempt that will never be written again.

        Sealed *or* abandoned. Sealing is one of two ways an attempt stops
        being written to, and since #15 it is the only one that stamps a time:
        a displaced attempt is closed with `sealed_at` still null, because
        nothing about it was completed.
        """
        return self.is_sealed or self.outcome is Outcome.ABANDONED

    def with_guess(self, guess: Guess) -> "QuizAttempt":
        self._refuse_if_closed("record a guess against")
        return dataclasses.replace(self, guesses=(*self.guesses, guess))

    def with_probe(self, probe: Probe) -> "QuizAttempt":
        self._refuse_if_closed("record a probe against")
        return dataclasses.replace(self, probes=(*self.probes, probe))

    def with_probe_resolved(self, position: int, probe: Probe) -> "QuizAttempt":
        """Swap a pending probe for its answered or dismissed self, in place.

        The reply to a probe is the second half of the same event, not a new
        one, so it replaces the pending record rather than appending beside it -
        a curation job replaying the timeline would otherwise see the tutor ask
        the same question twice. `with_probe` stays the append; this is the only
        write on an attempt that is not one, and it is still a new value.

        Addressed by position rather than by value because two pending probes on
        the same blank can compare equal under a frozen clock, and the caller -
        which found the probe by scanning `probes` - already knows which one it
        means.

        Raises:
          IndexError: If there is no probe at `position`.
          ValueError: If the attempt is closed.
        """
        self._refuse_if_closed("resolve a probe against")
        probes = list(self.probes)
        probes[position] = probe
        return dataclasses.replace(self, probes=tuple(probes))

    def with_model_call(self, call: ModelCallRecord) -> "QuizAttempt":
        self._refuse_if_closed("record a model call against")
        return dataclasses.replace(self, model_calls=(*self.model_calls, call))

    def with_queued_topics(self, topics: Sequence[str]) -> "QuizAttempt":
        """Replace the queue of questions the learner raised but parked.

        The whole queue, not one topic: displacement rewrites it wholesale when
        it carries the remainder forward onto the restart (#15), and appending
        one inquiry is the same write with one more element. Which topics, in
        what order, and whether a repeat counts twice are the caller's
        business - `socratic.domain.inquiry` holds that - so the record keeps
        only the guard and the immutability.

        Raises:
          ValueError: If the attempt is closed.
        """
        self._refuse_if_closed("queue a topic on")
        return dataclasses.replace(self, queued_topics=tuple(topics))

    def with_quiz(self, quiz: Quiz) -> "QuizAttempt":
        """Swap in a quiz the authoring path has merged into (D11, ADR-0011).

        The pedagogy payload lands on the quiz rather than on the guess or
        probe collections, so it is the one write with no natural home among
        the three above. The record's invariants run again on the swap, so a
        merge that dropped a blank a guess names is rejected here.
        """
        self._refuse_if_closed("replace the quiz on")
        return dataclasses.replace(self, quiz=quiz)

    def sealed(self, at: datetime) -> "QuizAttempt":
        """Close the attempt as resolved.

        The predicate that decides *when* this is called - all blanks resolved
        and no probe pending - belongs to the session, not the record.
        """
        self._refuse_if_closed("seal")
        return dataclasses.replace(self, sealed_at=at, outcome=Outcome.RESOLVED)

    def abandoned(self) -> "QuizAttempt":
        """Close the attempt as displaced by "start this instead".

        Written only on displacement - never by a sweeper, a timeout, or any
        other inference that the learner left (CONTEXT: Outcome). Takes no
        time because it stamps none: `sealed_at` stays null on an abandoned
        attempt (#15), and when the displacement happened is legible from the
        `created_at` of the attempt that displaced it.

        The attempt is closed all the same - `outcome` is what says so, and
        `is_closed` is what the guard reads.
        """
        self._refuse_if_closed("abandon")
        return dataclasses.replace(self, outcome=Outcome.ABANDONED)

    def _refuse_if_closed(self, action: str) -> None:
        if self.is_sealed:
            raise ValueError(
                f"cannot {action} an attempt sealed at {self.sealed_at}"
            )
        if self.outcome is Outcome.ABANDONED:
            raise ValueError(f"cannot {action} an abandoned attempt")


@dataclass(frozen=True, slots=True)
class RatingRecord:
    """An optional 1-5 Likert score keyed by attempt id (CONTEXT: RatingRecord).

    A separate record so the attempt stays sealed: a human signal about quiz
    quality, outside the LLM conversation entirely.
    """

    attempt_id: str
    learner_id: str
    score: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not 1 <= self.score <= 5:
            raise ValueError(f"a Likert score runs 1-5, got {self.score}")
