"""Prompt layout as a value: ordered cache segments, not a wire request.

`assemble` returns the ordered segment list with its breakpoint markers and
stops there. Building an Anthropic request body is the adapter's job, and the
separation is the whole point of the seam: the cache invariants ADR-0006 rests
on become properties of a returned value, assertable with no live API and no
SDK installed.

Layout, in render order (ADR-0006, master spec section 4)::

    [ segment 1 ] invariant tutor instructions for this call type  <- cache_control
    [ segment 2 ] learner profile + quiz + blank rubrics           <- cache_control
    [ tail      ] the learner's inquiry, guesses so far in order,
                  current blank + guess

**There are five segment 1s, not one.** Each call type has its own instructions,
its own output schema, and therefore its own cache prefix, and each must clear
Opus 5's 512-token minimum on its own (ADR-0014). The invariant is
byte-identity *across learners for a given call type*, so it is asserted five
times.

THE INVARIANT: no learner-specific text may appear in segment 1 — and that
includes *omitting* text per learner, which splits the cache just as surely as
adding it (ADR-0010). Per-learner toggles switch client behaviour; anything that
genuinely must vary goes in segment 2. `assemble` therefore takes a
`probe_cadence` it deliberately never reads into segment 1: the parameter puts
the setting in the one function where somebody would be tempted to branch on it,
and `tests/test_prompting.py` locks that branch out.

**Naming.** `socratic.domain.types` already uses `Segment` for an element of the
explanation token stream (`text | math | blank`). A `PromptSegment` is a
different concept that happens to share the English word. The two must never be
confused, which is why everything here carries the `Prompt` prefix.

**`LearnerProfile` is not defined here.** It lives in `socratic.domain.profiles`
- a leaf module both this assembler and the persistence ports can import without
importing each other (#36). It is re-exported here so that
`socratic.domain.prompting.LearnerProfile` keeps resolving for code written
against the old location; `render_profile` below is still the single point at
which its internals are read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from socratic.domain.modes import ProbeCadence
from socratic.domain.profiles import LearnerProfile
from socratic.domain.registry import BlankRange
from socratic.domain.types import (
    BlankSegment,
    MathSegment,
    Quiz,
    TextSegment,
)


class CallType(str, Enum):
    """The five distinct requests the system makes (CONTEXT: Call type).

    Each is a method on `ModelClient`, each has its own instructions, its own
    output schema, and therefore its own segment 1 and its own cache prefix.
    Naming them is what keeps the inventory visible.
    """

    AUTHOR_SKELETON = "author_skeleton"
    AUTHOR_PEDAGOGY = "author_pedagogy"
    GRADE_ANSWER = "grade_answer"
    GRADE_PROBE = "grade_probe"
    FOLD_NARRATIVE = "fold_narrative"


class SegmentRole(str, Enum):
    """Which of ADR-0006's three positions a segment occupies."""

    SEGMENT_1 = "segment_1"
    """Invariant tutor instructions. Byte-identical for every learner."""

    SEGMENT_2 = "segment_2"
    """Learner profile, quiz, blank rubrics. Per learner, per session."""

    VOLATILE_TAIL = "volatile_tail"
    """The learner's inquiry, guesses so far, current blank and guess.

    Never cached — and the only place a per-request string such as the inquiry
    may appear.
    """


@dataclass(frozen=True, slots=True)
class PromptSegment:
    """One segment of the laid-out prompt.

    `cache_control` is the breakpoint marker, not a request field: the adapter
    translates a marked segment into whatever the wire format calls for.
    """

    role: SegmentRole
    text: str
    cache_control: bool


@dataclass(frozen=True, slots=True)
class PromptSegments:
    """An assembled prompt: the ordered segments and the call type they serve.

    Deliberately not a request body. See the module docstring.
    """

    call_type: CallType
    segments: tuple[PromptSegment, ...]

    def _of(self, role: SegmentRole) -> PromptSegment:
        for segment in self.segments:
            if segment.role is role:
                return segment
        raise KeyError(role)

    @property
    def segment_1(self) -> PromptSegment:
        return self._of(SegmentRole.SEGMENT_1)

    @property
    def segment_2(self) -> PromptSegment:
        return self._of(SegmentRole.SEGMENT_2)

    @property
    def volatile_tail(self) -> PromptSegment:
        return self._of(SegmentRole.VOLATILE_TAIL)

    def breakpoints(self) -> tuple[int, ...]:
        """The indices carrying a cache breakpoint, in order."""
        return tuple(
            index
            for index, segment in enumerate(self.segments)
            if segment.cache_control
        )


def render_profile(profile: LearnerProfile) -> str:
    """Render a learner profile for segment 2.

    The single point at which profile internals are read. Deterministic: the
    ledger is sorted, so two profiles differing only in insertion order render
    identically and cannot split segment 2's cache entry needlessly.
    """
    lines = [f"Learner: {profile.learner_id}"]

    lines.append("Ledger (exactly recomputed; authoritative where the")
    lines.append("narrative disagrees):")
    if profile.ledger:
        for key in sorted(profile.ledger):
            lines.append(f"  {key}: {profile.ledger[key]}")
    else:
        lines.append("  (no attempts recorded yet)")

    lines.append("Narrative summary:")
    if profile.narrative:
        lines.append(f"  {profile.narrative}")
    else:
        lines.append("  (none yet; this learner is new or unprocessed)")

    return "\n".join(lines)


def assemble(
    call_type: CallType,
    *,
    profile: LearnerProfile,
    probe_cadence: ProbeCadence,
    quiz: Quiz | None = None,
    inquiry: str | None = None,
    guesses: Sequence[str] = (),
    current_blank_id: str | None = None,
    current_guess: str | None = None,
    blank_range: BlankRange | None = None,
) -> PromptSegments:
    """Lay a request out as ordered cache segments.

    Args:
      call_type: Which of the five requests this is. Selects segment 1, of
        which there are five — one per call type, each its own cache prefix.
      profile: The learner profile. Reaches the prompt only through
        `render_profile`, and only in segment 2.
      probe_cadence: The learner's `UserValves` setting. **Read by nothing
        here**, deliberately: turning probes off does not change the prompt
        (ADR-0010), and taking the parameter is what makes that assertable.
      quiz: The quiz in hand, when there is one. `author_skeleton` and
        `fold_narrative` have none.
      inquiry: What the learner actually asked, for the calls that carry it —
        `author_skeleton` above all. Rendered at the head of the volatile
        tail, which is the only position open to it: it is the most
        learner-specific string in the system, so anywhere above the last
        breakpoint would give every learner their own cache prefix (ADR-0006).
      guesses: The guesses so far, in order, already rendered to one line each
        by the caller that owns the `Guess` record.
      current_blank_id: The blank being answered, when there is one.
      current_guess: The answer under consideration, when there is one.
      blank_range: How many blanks the mode admits, for the call that authors
        them. Supplied as a value rather than derived from a mode here: the
        registry stays the only place mode is branched on, and this renders
        whatever it is handed. It lands in segment 2 because it varies by mode
        and segment 1 may not (ADR-0006) — the schema fragment cannot carry it
        either, since array-count keywords are outside the subset structured
        outputs accept (#72).

    Returns:
      The three segments in render order, with breakpoints on the first two.

    Raises:
      KeyError: If `call_type` is not one of the five.
    """
    del probe_cadence  # See the docstring: never reaches the prompt.

    instructions = _SEGMENT_1[call_type]

    return PromptSegments(
        call_type=call_type,
        segments=(
            PromptSegment(
                role=SegmentRole.SEGMENT_1,
                text=instructions,
                cache_control=True,
            ),
            PromptSegment(
                role=SegmentRole.SEGMENT_2,
                text=_render_segment_2(
                    profile=profile, quiz=quiz, blank_range=blank_range
                ),
                cache_control=True,
            ),
            PromptSegment(
                role=SegmentRole.VOLATILE_TAIL,
                text=_render_tail(
                    inquiry=inquiry,
                    guesses=guesses,
                    current_blank_id=current_blank_id,
                    current_guess=current_guess,
                ),
                cache_control=False,
            ),
        ),
    )


def _render_segment_2(
    *,
    profile: LearnerProfile,
    quiz: Quiz | None,
    blank_range: BlankRange | None = None,
) -> str:
    """Learner profile, quiz, blank rubrics, and the mode's blank bound.

    Per learner, per session — which is what lets the blank bound live here at
    all. It varies by mode, so segment 1 is closed to it (ADR-0006), and the
    schema fragment cannot express an array count (#72). Segment 2 is not a
    cross-learner cache prefix, so a call that supplies no bound simply renders
    no bound section: ADR-0010's "omitting splits the cache too" is about
    segment 1 and does not bite here.
    """
    parts = ["## Learner profile", render_profile(profile)]
    if quiz is not None:
        parts.append("## Quiz")
        parts.append(_render_quiz(quiz))
    else:
        parts.append("## Quiz")
        parts.append("(none yet; this call authors or summarises rather than grades)")
    if blank_range is not None:
        parts.append("## Blanks to author")
        parts.append(_render_blank_range(blank_range))
    return "\n\n".join(parts)


def _render_blank_range(blank_range: BlankRange) -> str:
    """State the mode's blank bound as a sentence the model can act on.

    The bound is inclusive at both ends. A degenerate range — a future mode
    admitting exactly one count — reads as a count rather than as a range,
    because "between 1 and 1" invites the model to wonder what was meant.
    """
    if blank_range.minimum == blank_range.maximum:
        return (
            f"Author exactly {blank_range.minimum} blanks. A quiz with any "
            "other number is discarded before the learner sees it."
        )
    return (
        f"Author between {blank_range.minimum} and {blank_range.maximum} "
        "blanks, inclusive. A quiz outside that range is discarded before the "
        "learner sees it."
    )


def _render_quiz(quiz: Quiz) -> str:
    """Render the quiz and its blanks.

    Every mode-specific field is rendered by presence rather than by mode: an
    `if mode ==` outside the registry is a bug (CONTEXT: ModeRegistry), and
    rendering has no reason to hold an opinion about mode beyond naming it.
    """
    lines = [
        f"Session: {quiz.quiz_session_id}",
        f"Mode: {quiz.mode.value}",
        f"Topic: {quiz.topic}",
        "Explanation:",
        f"  {_render_explanation(quiz.explanation)}",
        f"Recap: {quiz.recap}",
    ]
    if quiz.queued_topics:
        lines.append(f"Queued topics: {', '.join(quiz.queued_topics)}")

    lines.append("Blanks:")
    for blank in quiz.blanks:
        lines.append(f"  [{blank.blank_id}]")
        if blank.rubric is not None:
            lines.append(f"    Rubric: {blank.rubric}")
        if blank.options is not None:
            for option in blank.options:
                marker = "*" if option.option_id == blank.correct_option_id else "-"
                lines.append(f"    {marker} {option.option_id}: {option.text}")
        if blank.reinforcement is not None:
            lines.append(f"    Reinforcement: {blank.reinforcement}")
        if blank.hints is not None:
            for rung, hint in enumerate(blank.hints, start=1):
                lines.append(f"    Hint (rung {rung}): {hint}")

    return "\n".join(lines)


def _render_explanation(explanation: tuple[object, ...]) -> str:
    """Flatten the segment array back to readable text, blanks left masked."""
    pieces = []
    for segment in explanation:
        if isinstance(segment, TextSegment):
            pieces.append(segment.text)
        elif isinstance(segment, MathSegment):
            pieces.append(segment.mathml)
        elif isinstance(segment, BlankSegment):
            pieces.append(f"[[{segment.blank_id}]]")
        else:
            raise TypeError(f"not an explanation segment: {type(segment).__name__}")
    return "".join(pieces)


def _render_tail(
    *,
    inquiry: str | None,
    guesses: Sequence[str],
    current_blank_id: str | None,
    current_guess: str | None,
) -> str:
    """The volatile tail: the inquiry, the guesses in order, the current pair.

    Sits after the last breakpoint and is never marked cacheable — it changes
    on every single request, so caching it would write a fresh entry each time
    and read none of them. That is exactly why the inquiry belongs here: a
    per-request string above a breakpoint splits the segment it lands in.

    A call with no inquiry renders no inquiry heading. Omission is safe here
    and only here — the tail is not cached, so there is no entry for a missing
    section to split (contrast ADR-0010, which is about segment 1).
    """
    lines: list[str] = []
    if inquiry is not None:
        lines.append("## The learner's inquiry")
        lines.append(f"  {inquiry}")

    lines.append("## Guesses so far, in order")
    if guesses:
        for position, guess in enumerate(guesses, start=1):
            lines.append(f"  {position}. {guess}")
    else:
        lines.append("  (none yet)")

    lines.append("## Under consideration")
    if current_blank_id is None:
        lines.append("  Blank: (none)")
    else:
        lines.append(f"  Blank: {current_blank_id}")
    if current_guess is None:
        lines.append("  Guess: (none)")
    else:
        lines.append(f"  Guess: {current_guess}")

    return "\n".join(lines)


# --- The five segment 1s -----------------------------------------------------
#
# One per call type. Each is byte-identical for every learner in the workspace,
# each is its own cache prefix, and each must clear Opus 5's 512-token minimum
# on its own (ADR-0014) — falling under it is silent, and what is lost is
# precisely the cross-user sharing segment 1 exists for.
#
# Nothing below may vary per learner. Not by interpolation, and not by
# omission: a conditional that drops a sentence for learners with probes off
# gives segment 1 two variants and so two cache entries, and the precedent is
# worse than the entry (ADR-0010).

_SHARED_STANCE = """\
You are the tutor in a Socratic learning app. A learner asks a question and
receives a short explanation with key terms masked as numbered blanks, which
they resolve one at a time. You never simply hand over an answer that the
learner is in the middle of working out, and you never lecture past the point
that the question asked for.

Hold to one topic. If the learner raises further distinct questions, they are
recorded as queued topics and answered later; naming them is useful, chasing
them is not. Keep the register plain, warm and unhurried. Do not congratulate
reflexively, do not pad, and do not moralise. Prefer the concrete instance to
the general claim: a learner who has seen one worked case will generalise
faster than one who has been handed the generalisation.

Everything you produce is structured output against a schema supplied with the
request. Emit the schema's fields and nothing else: no preamble, no closing
remark, no restatement of the instructions, no markdown fences around the whole
payload. Where a field is optional and you have nothing worth saying, omit it
rather than filling it with filler.

Text fields accept a restricted Markdown subset: inline code, fenced code blocks
with a language tag, bold, italic, lists and links. Anything else is stripped
before it reaches a learner, so reaching for it wastes tokens. Mathematics is
written as LaTeX and converted to MathML before rendering; a masked element is
always a whole formula and never a term inside one, so do not attempt to mask
part of an expression.

You are never told which learner you are serving beyond the profile supplied
below the instructions, and you should not ask. Treat the profile as evidence
about what this learner has already met, not as a licence to flatter or to
diagnose them.
"""

_AUTHOR_SKELETON_INSTRUCTIONS = (
    _SHARED_STANCE
    + """
## This call: author the skeleton

Produce either a direct answer or a quiz, as a tagged union.

Return a direct answer when the inquiry is medical, legal, financial or
security-sensitive, when it concerns an active outage or another situation in
which someone needs the fact immediately, or when the learner has said plainly
that they need the answer rather than the exercise. This branch is a first-class
outcome, not a failure: the highest-stakes question a learner asks must still
get a straight response. Do not quiz someone who is mid-incident.

Otherwise author a quiz. Write a 200 to 250 word explanation of the topic that
would stand on its own as prose, then mask the key terms as blanks. The
explanation is a flat array of segments — text, math, or a blank reference — in
reading order. Never write a sentinel string into a text segment to indicate a
blank, and never nest one segment inside another.

Choose the blanks so that each one is the load-bearing word or phrase of its
sentence: the term a learner who understood the passage could supply and one who
did not could not. Do not mask a term the passage never explains, do not mask
the same term twice, and do not mask articles, connectives or units. How many
blanks the difficulty mode admits is stated as an inclusive range with the
learner material below these instructions; author a number inside it. That
range is the mode's pedagogical bound, and it is checked after you answer, so a
quiz outside it is discarded before the learner sees any of it.

Every blank carries the mode-specific fields the supplied schema fragment
requires and no others. Give each blank a short stable identifier and reference
it from exactly one position in the explanation array. Every blank you declare
must be referenced, and every reference must resolve to a declared blank.

Also return the topic as a short noun phrase, and any further distinct questions
the inquiry raised as queued topics. Do not answer the queued topics.

This is the only call a learner waits on, so favour a clean first pass over an
elaborate one. The hints, reinforcements and probe questions are authored by a
separate call that lands while the learner is still reading; do not produce them
here even if the schema would tolerate them.
"""
)

_AUTHOR_PEDAGOGY_INSTRUCTIONS = (
    _SHARED_STANCE
    + """
## This call: author the pedagogy payload

The skeleton has already been authored, validated and shown to the learner. It
is supplied below. Your job is the teaching material that hangs off it: the hint
ladder, the reinforcements, the probe questions and the closing recap. The
learner is reading the explanation right now, so this payload lands into a live
quiz and must be consistent with the skeleton exactly as authored. Do not
restate, rewrite, re-mask or re-order any part of it.

For each blank, write three hints forming an escalating ladder. Rung one
re-frames the surrounding idea without naming the answer or any of its
synonyms. Rung two narrows the field — a category, a contrast with a near miss,
a property the answer must have. Rung three states the answer plainly and
explains why it is the answer, because a learner who has reached rung three is
better served by understanding than by another turn of the screw. A ladder whose
first rung gives the answer away is the most common failure here; check each
rung against the one below it before you emit it.

Where the schema asks for a reinforcement, write one or two sentences that a
learner sees after answering correctly. Say why the answer is right, not that it
is right. A reinforcement that only congratulates has spent the learner's
attention and taught nothing.

Where the schema asks for a probe question, write the question the tutor asks
after a correct answer: how did you arrive at that? Phrase it so that a learner
who guessed and a learner who reasoned give visibly different replies. Avoid
questions answerable with yes, no, or a restatement of the answer just given.
Ask about the route, not the destination.

Finally write the recap: a short closing summary of the whole explanation with
nothing masked, which the learner reads once every blank is resolved. It should
be readable by someone who has forgotten the individual blanks.
"""
)

_GRADE_ANSWER_INSTRUCTIONS = (
    _SHARED_STANCE
    + """
## This call: grade an answer

A learner has submitted an answer to one blank. The quiz, the blank's rubric and
every guess made so far are supplied below. Judge the submission on meaning
against the rubric, not on wording: an answer that is correct but phrased
differently from the rubric passes, and one that matches the rubric's words
while plainly missing the idea does not. Spelling, capitalisation, word order
and reasonable synonyms are never grounds for rejection on their own.

Return the verdict and nothing that pre-empts the next step. If the answer is
wrong, the client selects which rung of the hint ladder to show; you do not
choose it and you do not write the hint here. If the answer is right, say so and
stop. After a correct answer you may be asked to probe. The probe question, when
one is requested, rides on this same response — asking never costs a further
call — and a separate call grades the learner's reply to it.

Where the supplied schema includes a reactive tutor line, use it for one
sentence responding to *how* the learner phrased the answer they gave: the
misconception the wording reveals, the near miss worth naming, the distinction
they have not yet drawn. It is a nullable field. Leave it null when the wording
carries nothing worth remarking on, which is most of the time. A reactive line
that restates the verdict is noise.

Read the guesses so far before judging. A learner on their third attempt at a
blank is in a different position from one on their first, and a response that
ignores the two attempts already made will read as though nobody was listening.
Do not, however, let the history change the verdict itself: a correct answer on
the fourth attempt is correct.

Never reveal the answer to a blank the learner has not resolved, and never
reveal the answer to a different blank. The answer key stays in the backend, and
what you are shown of it is scoped to the blank in front of you.
"""
)

_GRADE_PROBE_INSTRUCTIONS = (
    _SHARED_STANCE
    + """
## This call: grade a self-explanation

After answering a blank correctly the learner was asked how they arrived at
their answer. Their reply is supplied below, with the blank, the rubric and the
guesses that preceded it. Judge whether the reply shows the learner actually
understood, or whether they arrived at a correct answer by elimination, by
pattern-matching the surrounding sentence, or by chance.

This is the single richest signal the system collects. A click shows that
somebody was right; this shows whether they knew. Grade it substantively, which
means neither of the two easy failures: do not pass every reply that is
non-empty and polite, and do not fail a reply that is correct but inarticulate.
A learner reasoning correctly in clumsy words has understood. A learner
restating the answer in confident words has not.

A reply that names the mechanism, the constraint, or the elimination that ruled
out the alternatives passes. A reply that restates the answer, appeals to it
looking right, or reproduces the sentence around the blank does not. A reply
that is honest about guessing does not pass, and should be met without reproach:
saying so is more useful than a confident fabrication and should not be
punished.

Return the verdict and a short correction addressing the specific
misunderstanding the reply revealed. Address what this learner actually said.
Generic encouragement is worse than silence here, because it spends the one
moment where the learner is already thinking about their own reasoning.

What follows a failed probe is not yours to decide. Depending on the difficulty
mode the blank may re-open for another attempt, or it may stay resolved while
the misconception is corrected in passing. The client applies whichever the mode
specifies, and it caps re-opening, so do not instruct the learner to try again
and do not assume they will get another turn.
"""
)

_FOLD_NARRATIVE_INSTRUCTIONS = (
    _SHARED_STANCE
    + """
## This call: fold the narrative summary forward

You are not tutoring on this call. You are maintaining the prose half of a
learner profile, offline, between sessions. Supplied below are the existing
narrative summary and the attempts recorded since it was last advanced. Return
the updated narrative.

The profile has two halves. The ledger — topic counts, mode history, weak-area
tallies, outcome counts — is computed exactly and is authoritative wherever the
two disagree. You are writing the other half: the short prose account of how
this learner works that the numbers cannot carry. Never contradict the ledger,
never restate it, and never invent a count. If the numbers say a topic was
attempted seven times, the narrative's job is to say what happened across those
seven, not to say seven.

Fold forward incrementally. The summary is built over the learner's whole
history rather than a recent window, so an infrequent learner is not penalised
for the gap. Carry forward what still holds, revise what the new attempts
contradict, and drop what has been superseded — a weakness the learner has since
demonstrated they no longer have should leave the summary rather than linger as
a stale note that shapes every future quiz.

Write a few sentences, not an essay. Favour what would change how the next quiz
is written: the topics they return to, the level they work comfortably at, the
kinds of mistake that recur, whether their self-explanations show reasoning or
recall, and whether they push into harder material or consolidate. Omit
anything a quiz author could not act on.

Do not diagnose, do not speculate about the learner as a person, and do not
record anything about them beyond how they engage with the material here. Write
in the third person, plainly, in a register the learner could read without
offence — because they may.
"""
)

_SEGMENT_1: Mapping[CallType, str] = {
    CallType.AUTHOR_SKELETON: _AUTHOR_SKELETON_INSTRUCTIONS,
    CallType.AUTHOR_PEDAGOGY: _AUTHOR_PEDAGOGY_INSTRUCTIONS,
    CallType.GRADE_ANSWER: _GRADE_ANSWER_INSTRUCTIONS,
    CallType.GRADE_PROBE: _GRADE_PROBE_INSTRUCTIONS,
    CallType.FOLD_NARRATIVE: _FOLD_NARRATIVE_INSTRUCTIONS,
}
