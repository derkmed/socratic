"""`ProfileBuilder` — the offline job that advances a profile (#16, D6/D8).

One run reads the attempts after the stored **watermark**, increments the
**ledger** arithmetically, folds the **narrative** forward with exactly one
`fold_narrative` call, advances the watermark, and writes the profile back.
Incrementally over all history, never a recency window (ADR-0008), so an
infrequent learner is not penalised for the gap — and reading only past the
watermark is what keeps the cost of a run proportional to new activity rather
than to lifetime history.

**Never on a learner's critical path** (ADR-0006). `run` happens because a human
or a timer outside this package called it. Nothing here schedules, sleeps,
threads or queues; the clock is injected and is read exactly once, to stamp
`updated_at`.

**The ledger is authoritative** where the two halves disagree (ADR-0008). That
is a property of this module as much as of the renderer: every figure here is
counted from immutable attempt records, and nothing is ever read back out of the
narrative. The model's prose is stored verbatim and given no arithmetic to do.

The reproducibility claim is the load-bearing one, so it has two
implementations. `run` increments; `recompute_ledger` counts the same history
from scratch. They share `sealed_prefix` and `tally`, which is what makes them
agree by construction rather than by luck, and the suite asserts the agreement
directly (master spec acceptance 30).

**The eligibility rule, in two halves.**

* *The watermark boundary is exclusive.* The stored value names an attempt
  already folded in, so a run takes ids strictly greater than it. The attempt
  whose id equals the watermark is finished business.
* *An unsealed attempt is a barrier, not a skip.* A run advances through the
  contiguous **sealed** prefix of the new attempts and stops at the first
  in-flight one. An in-flight attempt is still accumulating guesses: counting it
  now and never again would put the ledger permanently out of step with its own
  history, and stepping over it would lose it for good, because a watermark only
  moves forward. Holding the line costs one run's delay and keeps the figures
  exact.

Both halves are lexicographic string comparisons on the attempt id, which is
sound only because attempt ids are ULIDs and ULID encoding is order-preserving —
lexicographic sort is mint-time sort (ADR-0007, `ids.py`).

**Mode and outcome are ledger keys, never branches.** The vocabulary below keys
figures *by* mode, which is data; asking which mode it is would be an `if mode
==` outside the registry, which is a bug (CONTEXT: ModeRegistry).

This module reads the profile's ledger and narrative directly, and is exempt
from the scan in `tests/test_prompting.py` for that reason: it is the sole
*writer* of a profile, as `prompting.py` is the sole *reader for rendering*.
Those are the two ends the abstraction has always implied. Everything else in
the system still reaches a profile through `render_profile` alone.
"""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping, Sequence

from socratic.domain import model_client, prompting
from socratic.domain.modes import ProbeCadence
from socratic.domain.profiles import LearnerProfile
from socratic.domain.records import QuizAttempt, Verdict
from socratic.domain.repositories import (
    AttemptRepository,
    LearnerProfileRepository,
)

NARRATIVE_FIELD = "narrative"
"""The key the `fold_narrative` payload carries its prose under.

`ModelResponse.content` is the structured-output payload verbatim and parsing it
against the call's schema belongs to the caller that knows the schema — here.
"""

# --- The ledger's key vocabulary ---------------------------------------------
#
# A flat `Mapping[str, int]`, namespaced with "/" so `render_profile`'s sorted
# render groups related figures together. ADR-0008 names the four families:
# topic counts, mode history, weak-area tallies, and attempt and outcome counts.
# A key is written only once its count is non-zero, because every key present is
# a line in segment 2 and a ledger of zeros is noise.

ATTEMPTS_TOTAL = "attempts/total"
ATTEMPTS_BY_MODE = "attempts/mode/{mode}"
"""Mode history. The mode is part of the key; nothing here branches on it."""

TOPICS = "topics/{topic}"
OUTCOMES = "outcomes/{outcome}"

GUESSES_TOTAL = "guesses/total"
GUESSES_BY_VERDICT = "guesses/{verdict}"

PROBES_ASKED = "probes/asked"
PROBES_BY_VERDICT = "probes/{verdict}"
PROBES_DISMISSED = "probes/dismissed"
"""A probe the learner waved away. It was still asked, so it is still counted
under `probes/asked` — the question having been put is a fact worth keeping."""

WEAK_AREAS = "weak/{topic}"
"""The weak-area tally: wrong guesses and failed probes, counted by topic.

Deliberately not a rate. A ledger holds counts, so a reader that wants a rate
divides by `topics/<topic>` and knows what it divided by; a stored ratio would
be a derived figure that no longer reconciles once the next run increments.
"""

Clock = Callable[[], datetime]
"""When the profile was written. Injected so a test can freeze it, and so the
job holds no opinion about wall time."""


def _name(value: object) -> str:
    """The stored spelling of an enum-or-string field.

    Attempt fields such as `mode` and `outcome` are typed as plain strings but
    are often handed the `str` enum member, so a key built by interpolation
    would read the member's repr for one caller and its stored spelling for
    another.
    Normalising is not a branch on the value — it never asks *which* one it is.
    """
    return str(getattr(value, "value", value))


def sealed_prefix(
    attempts: Iterable[QuizAttempt], after: str | None = None
) -> tuple[QuizAttempt, ...]:
    """The attempts a run may fold in: past the watermark, up to the barrier.

    Args:
      attempts: A learner's attempts, in any order — they are sorted here
        rather than trusted, since the ordering is what the boundary means.
      after: The stored watermark, or `None` before the job has ever run. The
        boundary is **exclusive**: the attempt named by it is already folded in.

    Returns:
      The contiguous run of sealed attempts after the watermark, oldest first,
      stopping at the first attempt still in flight.
    """
    ordered = sorted(attempts, key=lambda attempt: attempt.attempt_id)
    eligible: list[QuizAttempt] = []
    for attempt in ordered:
        if after is not None and attempt.attempt_id <= after:
            continue
        if not attempt.is_sealed:
            break
        eligible.append(attempt)
    return tuple(eligible)


def tally(attempts: Sequence[QuizAttempt]) -> dict[str, int]:
    """The ledger figures for one batch of attempts.

    Pure arithmetic over immutable records, which is what makes the ledger
    incapable of drifting: the same attempts counted twice give the same
    answer, whether they are counted a batch at a time or all at once.
    """
    figures: collections.Counter[str] = collections.Counter()
    for attempt in attempts:
        figures[ATTEMPTS_TOTAL] += 1
        figures[TOPICS.format(topic=attempt.topic)] += 1
        figures[ATTEMPTS_BY_MODE.format(mode=_name(attempt.mode))] += 1
        figures[OUTCOMES.format(outcome=_name(attempt.outcome))] += 1

        weak = 0
        for guess in attempt.guesses:
            figures[GUESSES_TOTAL] += 1
            figures[GUESSES_BY_VERDICT.format(verdict=_name(guess.verdict))] += 1
            if guess.verdict is Verdict.INCORRECT:
                weak += 1

        for probe in attempt.probes:
            figures[PROBES_ASKED] += 1
            if probe.verdict is None:
                figures[PROBES_DISMISSED] += 1
                continue
            figures[PROBES_BY_VERDICT.format(verdict=_name(probe.verdict))] += 1
            if probe.verdict is Verdict.INCORRECT:
                weak += 1

        if weak:
            figures[WEAK_AREAS.format(topic=attempt.topic)] += weak

    return dict(figures)


def merge_ledgers(
    base: Mapping[str, int], delta: Mapping[str, int]
) -> dict[str, int]:
    """`base` incremented by `delta`, as a new dict.

    A stored profile's ledger arrives sealed in a `MappingProxyType`, so this
    builds a new mapping rather than adding in place.
    """
    merged = dict(base)
    for key, count in delta.items():
        merged[key] = merged.get(key, 0) + count
    return merged


def recompute_ledger(attempts: Iterable[QuizAttempt]) -> dict[str, int]:
    """The ledger for a learner's whole history, counted from scratch.

    The second of the two code paths acceptance 30 compares. It applies the
    same eligibility rule as `run` — via `sealed_prefix` — because a
    reproducibility claim that held only when nobody was mid-quiz would not be
    worth making.
    """
    return tally(sealed_prefix(attempts))


@dataclass(frozen=True, slots=True)
class ProfileBuilder:
    """The offline job. One public verb, called by hand or by a timer.

    Attributes:
      attempts: Where the history is read from, one learner partition at a
        time. There is no cross-partition read path and this job does not want
        one.
      profiles: Where the profile is read and rewritten in place.
      model: The `ModelClient` the narrative fold goes through. Exactly one
        `fold_narrative` call per advancing run, and none at all on a no-op.
      clock: Read once per advancing run, to stamp `updated_at`.
    """

    attempts: AttemptRepository
    profiles: LearnerProfileRepository
    model: model_client.ModelClient
    clock: Clock

    def run(self, learner_id: str) -> LearnerProfile:
        """Advance one learner's profile, and return what it now says.

        A run with nothing new to fold writes nothing at all — not the ledger,
        not the narrative, not `updated_at` — and does not consult the model
        (master spec acceptance 29).

        Args:
          learner_id: Whose profile to advance. The partition key.

        Returns:
          The stored profile: the advanced one, or the untouched previous one
          when there was nothing new. A learner with no profile and no eligible
          attempts gets an empty profile back without one being written.

        Raises:
          ValueError: If the fold's payload is not a JSON object carrying a
            string narrative. The stored profile is left as it was.
        """
        profile = self.profiles.get(learner_id) or LearnerProfile(
            learner_id=learner_id
        )
        folding = sealed_prefix(
            self.attempts.list_for_learner(learner_id), after=profile.watermark
        )
        if not folding:
            return profile

        ledger = merge_ledgers(profile.ledger, tally(folding))
        advanced = LearnerProfile(
            learner_id=learner_id,
            ledger=ledger,
            narrative=self._fold(profile, ledger=ledger, folding=folding),
            watermark=folding[-1].attempt_id,
            updated_at=self.clock(),
        )
        self.profiles.save(advanced)
        return advanced

    def _fold(
        self,
        profile: LearnerProfile,
        *,
        ledger: Mapping[str, int],
        folding: Sequence[QuizAttempt],
    ) -> str:
        """The one `fold_narrative` call, and the parse of what it returns.

        The call is handed the *already-incremented* ledger and the *previous*
        narrative: the figures it must not contradict, and the prose it is
        folding forward. Both ride in segment 2 through `render_profile`, which
        keeps this module out of the business of laying a profile out.
        """
        segments = prompting.assemble(
            prompting.CallType.FOLD_NARRATIVE,
            profile=LearnerProfile(
                learner_id=profile.learner_id,
                ledger=ledger,
                narrative=profile.narrative,
            ),
            # Never read into the prompt (ADR-0010), and there is no live
            # session here to read it from; the parameter is not optional.
            probe_cadence=ProbeCadence.OFF,
            # The volatile tail is the only position open to per-run text.
            guesses=_render_new_attempts(folding),
        )
        return _parse_narrative(self.model.fold_narrative(segments).content)


def _render_new_attempts(attempts: Sequence[QuizAttempt]) -> tuple[str, ...]:
    """One line per attempt folded in, for the volatile tail.

    Enough for prose about *what happened*, and nothing the ledger already
    states exactly: the fold's instructions tell the model never to restate a
    count, so handing it the raw events rather than a second set of totals is
    what keeps it out of arithmetic it would get wrong.
    """
    lines = []
    for attempt in attempts:
        wrong = sum(
            1 for guess in attempt.guesses if guess.verdict is Verdict.INCORRECT
        )
        answered = [probe for probe in attempt.probes if probe.self_explanation]
        lines.append(
            f"attempt {attempt.attempt_id} [{_name(attempt.mode)}] "
            f"topic {attempt.topic!r}: {_name(attempt.outcome)}, "
            f"{len(attempt.guesses)} guesses ({wrong} wrong), "
            f"{len(answered)} self-explanations of {len(attempt.probes)} probes"
        )
    return tuple(lines)


def _parse_narrative(content: str) -> str:
    """The prose out of the fold's structured payload.

    Refuses anything else rather than storing it: a profile whose narrative is
    a stray JSON fragment would be rendered into segment 2 of every subsequent
    call for that learner, and would be folded forward from there.
    """
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"the fold_narrative payload is not JSON: {error}"
        ) from None
    if not isinstance(payload, dict):
        raise ValueError(
            f"the fold_narrative payload is not an object: {type(payload).__name__}"
        )
    narrative = payload.get(NARRATIVE_FIELD)
    if not isinstance(narrative, str):
        raise ValueError(
            f"the fold_narrative payload carries no {NARRATIVE_FIELD!r} string: "
            f"{sorted(payload)}"
        )
    return narrative
