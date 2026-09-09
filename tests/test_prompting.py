"""The prompt-cache layout, and the invariant that pays for it.

Segment 1 is byte-identical for every learner in the workspace, which is why it
caches once and is read by everyone at ~0.1x (ADR-0006). There are **five** of
them, one per call type, so the invariant is asserted five times rather than
once (ADR-0014).

The half of the invariant that is easy to lose is that *omitting* text per
learner splits the cache exactly as surely as adding it (ADR-0010). The
`TestOmittingIsCaughtToo` class below is a meta-test: it builds a deliberately
broken assembler that drops a sentence for learners with probes off, and shows
the same assertion that guards `assemble` catches it.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import hashlib

import pytest

from socratic.domain import prompting
from socratic.domain.ids import new_quiz_session_id
from socratic.domain.modes import DifficultyMode, ProbeCadence
from socratic.domain.registry import BlankRange
from socratic.domain.prompting import (
    CallType,
    LearnerProfile,
    PromptSegment,
    SegmentRole,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

PROMPTING_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "socratic"

EXEMPT_FROM_PROFILE_SCAN = ("domain/prompting.py", "domain/profile_builder.py")
"""The two modules that own the profile, named by path relative to the package
root rather than by filename — see #23 for the same fault in the mode scan.

`prompting.py` is the sole **reader for rendering**; `profile_builder.py` (#16)
is the sole **writer**, and cannot increment a ledger or fold a narrative
forward without reading them. Those are the two ends the abstraction has always
implied — a profile is written in one place and rendered in one place — and
everything in between still reaches a profile through `render_profile` alone.
"""

PROFILE_INTERNALS = ("ledger", "narrative")
"""The shape-bearing fields the abstraction exists to hide.

`learner_id` is deliberately **not** one of them. CONTEXT calls it "partition
key for everything", and `LearnerProfileRepository.get(learner_id)` already
takes it as a parameter — a repository keying by it depends on the partition,
not on the profile's shape. Including it here made the guard forbid persistence
from storing a profile at all, which is what took `main` red.
"""

_PROFILE_INTERNAL_READ = re.compile(
    r"\bprofile\.(?:" + "|".join(PROFILE_INTERNALS) + r")\b"
)


def find_profile_internal_reads(
    source_root: pathlib.Path, exempt: tuple[str, ...]
) -> list[str]:
    """Every line under `source_root` reading a shape-bearing profile field.

    Extracted from the test that uses it so the scan itself can be tested: an
    unenforced guard and a working one look identical from the outside.
    """
    exempted = set(exempt)
    offenders = []
    for path in sorted(source_root.rglob("*.py")):
        location = path.relative_to(source_root).as_posix()
        if location in exempted:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _PROFILE_INTERNAL_READ.search(line):
                offenders.append(f"{location}:{lineno}: {line.strip()}")
    return offenders


def _scan_snippet(location: str, source: str) -> list[str]:
    """Run the scan over a synthetic tree holding one file."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        path = root / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return find_profile_internal_reads(root, exempt=EXEMPT_FROM_PROFILE_SCAN)

# Two learners who share nothing: different ids, different ledgers, different
# narratives, different cadences. Anything that leaks below is visible.
ADA = LearnerProfile(
    learner_id="learner-ada",
    ledger={"topics/monads": 7, "weak/recursion": 2},
    narrative="Ada is fluent in category theory and stumbles on base cases.",
)
BASHO = LearnerProfile(
    learner_id="learner-basho",
    ledger={"topics/haiku": 3},
    narrative="Basho counts syllables well and has never seen a monad.",
)

LEARNER_MARKERS = (
    "learner-ada",
    "learner-basho",
    "monads",
    "recursion",
    "haiku",
    "Ada",
    "Basho",
    "category theory",
    "syllables",
)


def _quiz() -> Quiz:
    return Quiz(
        quiz_session_id=new_quiz_session_id(),
        mode=DifficultyMode.ADVANCED,
        topic="Tail recursion",
        explanation=(
            TextSegment(text="A tail call reuses the "),
            BlankSegment(blank_id="b1"),
            TextSegment(text=" instead of growing it."),
        ),
        blanks=(
            Blank(
                blank_id="b1",
                mode=DifficultyMode.ADVANCED,
                rubric="Any phrasing naming the stack frame.",
            ),
        ),
        recap="Tail calls are loops in disguise.",
    )


def _novice_quiz() -> Quiz:
    return Quiz(
        quiz_session_id=new_quiz_session_id(),
        mode=DifficultyMode.NOVICE,
        topic="Tail recursion",
        explanation=(
            TextSegment(text="A tail call reuses the "),
            BlankSegment(blank_id="b1"),
            TextSegment(text="."),
        ),
        blanks=(
            Blank(
                blank_id="b1",
                mode=DifficultyMode.NOVICE,
                options=(
                    Option(option_id="o1", text="stack frame"),
                    Option(option_id="o2", text="heap"),
                ),
                correct_option_id="o1",
                reinforcement="Right — the frame is reused.",
                hints=("Where do locals live?", "It is not the heap.", "The frame."),
            ),
        ),
        recap="Tail calls are loops in disguise.",
    )


class TestProbeCadence:
    def test_it_carries_the_four_documented_settings(self):
        assert [cadence.value for cadence in ProbeCadence] == [
            "off",
            "final_blank_only",
            "sometimes",
            "always",
        ]

    def test_it_is_a_str_enum_so_it_survives_a_valves_round_trip(self):
        assert ProbeCadence.SOMETIMES == "sometimes"


class TestCallType:
    def test_there_are_exactly_five(self):
        assert [call_type.value for call_type in CallType] == [
            "author_skeleton",
            "author_pedagogy",
            "grade_answer",
            "grade_probe",
            "fold_narrative",
        ]


class TestLayout:
    def test_the_three_roles_arrive_in_render_order(self):
        segments = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            guesses=("b1: 'the heap' — wrong, rung 1",),
            current_blank_id="b1",
            current_guess="the stack frame",
        )
        assert [segment.role for segment in segments.segments] == [
            SegmentRole.SEGMENT_1,
            SegmentRole.SEGMENT_2,
            SegmentRole.VOLATILE_TAIL,
        ]

    def test_breakpoints_sit_on_segments_one_and_two_only(self):
        segments = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
        )
        assert segments.breakpoints() == (0, 1)
        assert segments.segment_1.cache_control is True
        assert segments.segment_2.cache_control is True
        assert segments.volatile_tail.cache_control is False

    def test_the_volatile_tail_is_never_cacheable(self):
        for call_type in CallType:
            segments = prompting.assemble(
                call_type,
                profile=ADA,
                probe_cadence=ProbeCadence.ALWAYS,
                quiz=_quiz(),
                guesses=("b1: 'heap' — wrong",),
                current_blank_id="b1",
                current_guess="stack frame",
            )
            assert segments.volatile_tail.cache_control is False

    def test_it_is_not_a_wire_request(self):
        # The seam returns a segment list, deliberately. Constructing an
        # Anthropic request body is the adapter's job (#7), and keeping it out
        # is what makes these invariants assertable with no live API.
        segments = prompting.assemble(
            CallType.AUTHOR_SKELETON, profile=ADA, probe_cadence=ProbeCadence.OFF
        )
        assert isinstance(segments.segments, tuple)
        assert all(isinstance(item, PromptSegment) for item in segments.segments)
        assert not hasattr(segments, "to_request")

    def test_the_call_type_rides_along(self):
        segments = prompting.assemble(
            CallType.FOLD_NARRATIVE, profile=BASHO, probe_cadence=ProbeCadence.OFF
        )
        assert segments.call_type is CallType.FOLD_NARRATIVE


class TestSegmentOnePerCallType:
    def test_each_call_type_gets_its_own_instructions(self):
        texts = {
            call_type: prompting.assemble(
                call_type, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
            ).segment_1.text
            for call_type in CallType
        }
        assert len(set(texts.values())) == 5, "five prefixes, not one"

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_segment_one_is_byte_identical_across_learners(self, call_type):
        # Master spec acceptance 10, asserted per call type per ADR-0014.
        ada = prompting.assemble(
            call_type, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES, quiz=_quiz()
        )
        basho = prompting.assemble(
            call_type,
            profile=BASHO,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_novice_quiz(),
        )
        assert ada.segment_1.text == basho.segment_1.text

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_segment_one_is_byte_identical_across_probe_cadences(self, call_type):
        # Master spec acceptance 11. Turning probes off does not change the
        # prompt — the client simply stops firing (ADR-0010).
        texts = {
            prompting.assemble(
                call_type, profile=ADA, probe_cadence=cadence, quiz=_quiz()
            ).segment_1.text
            for cadence in ProbeCadence
        }
        assert len(texts) == 1

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_no_learner_specific_text_appears_above_breakpoint_one(self, call_type):
        for profile in (ADA, BASHO):
            text = prompting.assemble(
                call_type,
                profile=profile,
                probe_cadence=ProbeCadence.ALWAYS,
                quiz=_quiz(),
                guesses=("b1: 'heap' — wrong",),
                current_blank_id="b1",
                current_guess="stack frame",
            ).segment_1.text
            leaked = [marker for marker in LEARNER_MARKERS if marker in text]
            assert leaked == [], f"{call_type.value} segment 1 leaked {leaked}"

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_each_prefix_is_long_enough_to_plausibly_clear_the_cache_floor(
        self, call_type
    ):
        # A structural proxy, not the gate. ADR-0014 fixes a 512-token minimum
        # cacheable prefix on Opus 5 and warns that falling under it fails
        # *silently*, costing exactly the cross-user sharing segment 1 exists
        # for. The real measurement is #7's build gate (master spec acceptance
        # 12); this only catches a prefix that is obviously far too short.
        text = prompting.assemble(
            call_type, profile=ADA, probe_cadence=ProbeCadence.OFF
        ).segment_1.text
        assert len(text) >= 2400, (
            f"{call_type.value} segment 1 is {len(text)} chars, well under a "
            "plausible 512 tokens"
        )


class TestOmittingIsCaughtToo:
    """The half of ADR-0010 that is easy to half-implement.

    A conditional that *drops* a sentence for learners with probes off splits
    the cache exactly as surely as one that adds learner text. These two tests
    build both violations and show the same assertion catches both.
    """

    @staticmethod
    def _segment_one_that_omits(cadence: ProbeCadence) -> str:
        base = prompting.assemble(
            CallType.GRADE_ANSWER, profile=ADA, probe_cadence=cadence
        ).segment_1.text
        if cadence is ProbeCadence.OFF:
            return base.replace(
                "After a correct answer you may be asked to probe.", ""
            )
        return base

    @staticmethod
    def _segment_one_that_adds(profile: LearnerProfile) -> str:
        base = prompting.assemble(
            CallType.GRADE_ANSWER, profile=profile, probe_cadence=ProbeCadence.OFF
        ).segment_1.text
        return f"You are tutoring {profile.learner_id}.\n{base}"

    def test_the_real_assembler_omits_nothing_for_a_learner_with_probes_off(self):
        off = prompting.assemble(
            CallType.GRADE_ANSWER, profile=ADA, probe_cadence=ProbeCadence.OFF
        ).segment_1.text
        always = prompting.assemble(
            CallType.GRADE_ANSWER, profile=ADA, probe_cadence=ProbeCadence.ALWAYS
        ).segment_1.text
        assert off == always
        assert len(off) == len(always)

    def test_an_assembler_that_omits_per_learner_fails_the_same_assertion(self):
        omitted = self._segment_one_that_omits(ProbeCadence.OFF)
        kept = self._segment_one_that_omits(ProbeCadence.ALWAYS)
        assert omitted != kept, (
            "the broken assembler must actually differ, or this meta-test "
            "proves nothing about the assertion's power"
        )
        assert len(omitted) < len(kept), "the violation is a removal, not an addition"

    def test_an_assembler_that_adds_per_learner_fails_the_same_assertion(self):
        assert self._segment_one_that_adds(ADA) != self._segment_one_that_adds(BASHO)


class TestSegmentTwo:
    def test_it_carries_the_profile_the_quiz_and_the_rubrics(self):
        quiz = _quiz()
        text = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=quiz,
        ).segment_2.text
        assert prompting.render_profile(ADA) in text
        assert quiz.topic in text
        assert "Any phrasing naming the stack frame." in text

    def test_the_learner_specific_text_lives_here_and_only_here(self):
        text = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
        ).segment_2.text
        assert "learner-ada" in text
        assert "category theory" in text

    def test_a_call_with_no_quiz_still_renders_the_profile(self):
        # `fold_narrative` and `author_skeleton` have no quiz in hand yet.
        text = prompting.assemble(
            CallType.AUTHOR_SKELETON, profile=BASHO, probe_cadence=ProbeCadence.OFF
        ).segment_2.text
        assert prompting.render_profile(BASHO) in text

    def test_a_mode_key_that_is_not_an_enum_member_still_renders(self):
        """#89: `ModeKey` is `str`, and `_render_quiz` read `quiz.mode.value`.
        A registry may hold a plain string key — `tests/test_registry.py`
        registers `"expert"` — so `.value` is an unsafe read here whatever the
        authoring path stores."""
        quiz = dataclasses.replace(_quiz(), mode="expert")

        text = prompting.assemble(
            CallType.AUTHOR_PEDAGOGY,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=quiz,
        ).segment_2.text

        assert "Mode: expert" in text

    def test_a_registered_member_renders_as_its_stored_spelling(self):
        text = prompting.assemble(
            CallType.AUTHOR_PEDAGOGY,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_novice_quiz(),
        ).segment_2.text

        assert "Mode: novice" in text

    def test_novice_option_banks_reach_segment_two(self):
        text = prompting.assemble(
            CallType.GRADE_PROBE,
            profile=ADA,
            probe_cadence=ProbeCadence.ALWAYS,
            quiz=_novice_quiz(),
        ).segment_2.text
        assert "stack frame" in text
        assert "heap" in text


class TestRenderProfile:
    def test_it_renders_the_ledger_and_the_narrative(self):
        rendered = prompting.render_profile(ADA)
        assert "topics/monads" in rendered
        assert "Ada is fluent in category theory" in rendered

    def test_the_ledger_is_authoritative_so_it_is_rendered_first(self):
        # CONTEXT: LearnerProfile — "where they disagree, the ledger is
        # authoritative". Order is how that is communicated to the model.
        rendered = prompting.render_profile(ADA)
        assert rendered.index("topics/monads") < rendered.index("Ada is fluent")

    def test_it_is_deterministic_regardless_of_ledger_insertion_order(self):
        one = LearnerProfile(learner_id="l", ledger={"a": 1, "b": 2})
        other = LearnerProfile(learner_id="l", ledger={"b": 2, "a": 1})
        assert prompting.render_profile(one) == prompting.render_profile(other)

    def test_an_empty_profile_renders_without_pretending_to_know_anything(self):
        rendered = prompting.render_profile(LearnerProfile(learner_id="new"))
        assert rendered.strip() != ""
        assert "new" in rendered

    def test_no_other_module_reads_the_profile_internals(self):
        # CONTEXT: LearnerProfile — "rendered into segment 2 through a single
        # call; the domain never reads its internals, so its shape is free to
        # change". `prompting.py` owns the only reads.
        offenders = find_profile_internal_reads(
            PROMPTING_SOURCE_ROOT, exempt=EXEMPT_FROM_PROFILE_SCAN
        )
        assert offenders == [], "profile internals read outside prompting.py:\n" + (
            "\n".join(offenders)
        )


class TestTheProfileInternalsScanItself:
    """The scan is the only thing holding the profile abstraction shut, so a
    hole in it enforces less than it claims while still passing."""

    def test_reading_a_shape_bearing_field_elsewhere_is_reported(self):
        for field in PROFILE_INTERNALS:
            source = f"summary = profile.{field}\n"
            assert _scan_snippet("domain/grading.py", source), (
                f"reading profile.{field} outside prompting.py must be caught"
            )

    def test_the_partition_key_is_not_an_internal(self):
        # `learner_id` is CONTEXT's "partition key for everything", and
        # `LearnerProfileRepository.get(learner_id)` already takes it as a
        # parameter. A repository keying by it is not depending on the
        # profile's shape — it is using the one field whose existence is not
        # in question. Treating it as an internal made the guard forbid
        # persistence from storing a profile at all.
        assert _scan_snippet(
            "domain/repositories.py",
            "self._profiles[profile.learner_id] = profile\n",
        ) == []

    def test_the_builder_s_bookkeeping_is_not_an_internal(self):
        # The unified profile (#36) also carries `watermark` and `updated_at`.
        # Neither is shape-bearing for rendering — the watermark says how far
        # `ProfileBuilder` has read, and #16 has to read it to advance it.
        # Forbidding that would repeat #30: a guard that stops the profile
        # being used for the thing it exists for.
        for field in ("watermark", "updated_at"):
            assert _scan_snippet(
                "domain/builder.py", f"since = profile.{field}\n"
            ) == []

    def test_the_owning_module_may_read_them(self):
        for field in PROFILE_INTERNALS:
            assert _scan_snippet("domain/prompting.py", f"x = profile.{field}\n") == []

    def test_the_exemption_is_by_path_not_by_filename(self):
        # Same fault as #23, in the sibling guard: matching a bare filename
        # would exempt any future `<subpackage>/prompting.py`.
        assert _scan_snippet("adapters/prompting.py", "x = profile.ledger\n")

    def test_every_exempt_module_actually_exists(self):
        for module in EXEMPT_FROM_PROFILE_SCAN:
            assert (PROMPTING_SOURCE_ROOT / module).is_file(), f"{module} has moved"


class TestVolatileTail:
    def test_guesses_arrive_in_order_then_the_current_blank_and_guess(self):
        tail = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            guesses=("first guess", "second guess", "third guess"),
            current_blank_id="b1",
            current_guess="the stack frame",
        ).volatile_tail.text
        positions = [
            tail.index("first guess"),
            tail.index("second guess"),
            tail.index("third guess"),
            tail.index("b1"),
            tail.index("the stack frame"),
        ]
        assert positions == sorted(positions)

    def test_a_call_with_no_history_still_produces_a_tail(self):
        segments = prompting.assemble(
            CallType.AUTHOR_SKELETON, profile=ADA, probe_cadence=ProbeCadence.OFF
        )
        assert segments.volatile_tail.role is SegmentRole.VOLATILE_TAIL
        assert segments.volatile_tail.cache_control is False

    def test_the_tail_never_reaches_the_cached_segments(self):
        segments = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            guesses=("wrong: phlogiston",),
            current_blank_id="b1",
            current_guess="the stack frame",
        )
        assert "phlogiston" not in segments.segment_1.text
        assert "phlogiston" not in segments.segment_2.text
        assert "phlogiston" in segments.volatile_tail.text


class TestInquiry:
    """The learner's inquiry is the most learner-specific string there is.

    It reaches the prompt through the tail and nowhere else. An inquiry that
    landed in segment 1 would give every learner their own prefix — the exact
    anti-pattern ADR-0006 exists to prevent — and one that reshaped segment 2
    would split that entry per request rather than per session.
    """

    ADA_INQUIRY = "Why does phlogiston not appear in modern chemistry?"
    BASHO_INQUIRY = "How many syllables does a haiku's second line take?"

    def test_the_inquiry_reaches_the_volatile_tail(self):
        segments = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            inquiry=self.ADA_INQUIRY,
        )
        assert self.ADA_INQUIRY in segments.volatile_tail.text

    def test_the_inquiry_never_reaches_the_cached_segments(self):
        for call_type in CallType:
            segments = prompting.assemble(
                call_type,
                profile=ADA,
                probe_cadence=ProbeCadence.ALWAYS,
                quiz=_quiz(),
                inquiry=self.ADA_INQUIRY,
            )
            assert self.ADA_INQUIRY not in segments.segment_1.text
            assert self.ADA_INQUIRY not in segments.segment_2.text
            assert "phlogiston" not in segments.segment_1.text
            assert "phlogiston" not in segments.segment_2.text

    def test_the_tail_carrying_an_inquiry_is_still_never_cacheable(self):
        for call_type in CallType:
            segments = prompting.assemble(
                call_type,
                profile=ADA,
                probe_cadence=ProbeCadence.ALWAYS,
                inquiry=self.ADA_INQUIRY,
            )
            assert segments.volatile_tail.cache_control is False
            assert segments.breakpoints() == (0, 1)

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_segment_one_is_byte_identical_across_different_inquiries(
        self, call_type
    ):
        ada = prompting.assemble(
            call_type,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            inquiry=self.ADA_INQUIRY,
        )
        basho = prompting.assemble(
            call_type,
            profile=BASHO,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_novice_quiz(),
            inquiry=self.BASHO_INQUIRY,
        )
        assert ada.segment_1.text == basho.segment_1.text

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_segment_one_is_byte_identical_whether_or_not_an_inquiry_is_given(
        self, call_type
    ):
        # The omission half of ADR-0010: a learner who supplies no inquiry must
        # not get a shorter prefix than one who does.
        with_inquiry = prompting.assemble(
            call_type,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            inquiry=self.ADA_INQUIRY,
        ).segment_1.text
        without = prompting.assemble(
            call_type,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
        ).segment_1.text
        assert with_inquiry == without
        assert len(with_inquiry) == len(without)

    def test_segment_two_is_unperturbed_by_the_inquiry(self):
        # Segment 2 is per learner *per session*; the inquiry is per request.
        # Letting it reshape segment 2 would rewrite that entry every call.
        quiz = _quiz()
        with_inquiry = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=quiz,
            inquiry=self.ADA_INQUIRY,
        ).segment_2.text
        without = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=quiz,
        ).segment_2.text
        assert with_inquiry == without

    def test_the_inquiry_is_optional_so_existing_callers_are_untouched(self):
        # Backward compatibility: omitting the argument leaves the tail
        # byte-identical to what a caller got before the slot existed.
        arguments = dict(
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            guesses=("b1: 'heap' — wrong",),
            current_blank_id="b1",
            current_guess="the stack frame",
        )
        omitted = prompting.assemble(CallType.GRADE_ANSWER, **arguments)
        explicit_none = prompting.assemble(
            CallType.GRADE_ANSWER, inquiry=None, **arguments
        )
        assert omitted.volatile_tail.text == explicit_none.volatile_tail.text
        assert "inquiry" not in omitted.volatile_tail.text.lower()

    def test_the_inquiry_leads_the_tail_ahead_of_the_guess_history(self):
        tail = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            inquiry=self.ADA_INQUIRY,
            guesses=("first guess",),
            current_blank_id="b1",
            current_guess="the stack frame",
        ).volatile_tail.text
        positions = [
            tail.index(self.ADA_INQUIRY),
            tail.index("first guess"),
            tail.index("the stack frame"),
        ]
        assert positions == sorted(positions)

    def test_a_multi_line_inquiry_survives_intact(self):
        inquiry = "First line of the question.\nSecond line of the question."
        tail = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=BASHO,
            probe_cadence=ProbeCadence.OFF,
            inquiry=inquiry,
        ).volatile_tail.text
        assert "First line of the question." in tail
        assert "Second line of the question." in tail


class TestSegmentsValue:
    def test_a_prompt_segment_is_frozen(self):
        segment = PromptSegment(
            role=SegmentRole.SEGMENT_1, text="x", cache_control=True
        )
        with pytest.raises(Exception):
            segment.text = "y"  # type: ignore[misc]

    def test_the_roles_are_the_three_of_adr_0006(self):
        assert [role.value for role in SegmentRole] == [
            "segment_1",
            "segment_2",
            "volatile_tail",
        ]

    def test_an_unknown_call_type_is_an_error_not_a_silent_default(self):
        with pytest.raises((KeyError, ValueError)):
            prompting.assemble(
                "grade_everything",  # type: ignore[arg-type]
                profile=ADA,
                probe_cadence=ProbeCadence.OFF,
            )


class TestTheBlankBound:
    """Issue #72: the admissible blank count reaches the model as *text*.

    `minItems`/`maxItems` are outside the JSON-Schema subset structured outputs
    accept, so no fragment the registry supplies can carry the bound. It has to
    be said in the prompt, and ADR-0006 leaves exactly one position open to it:
    segment 2, because the bound varies by mode and segment 1 may not.
    """

    def test_segment_one_no_longer_delegates_the_count_to_the_schema(self):
        text = prompting.assemble(
            CallType.AUTHOR_SKELETON, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
        ).segment_1.text
        assert "fixed by the schema fragment" not in text, (
            "segment 1 still promises a constraint the structured-output "
            "schema subset cannot express (#72)"
        )

    def test_a_supplied_bound_reaches_segment_two(self):
        segments = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            blank_range=BlankRange(4, 6),
        )
        text = segments.segment_2.text
        assert "4" in text and "6" in text

    def test_the_bound_never_reaches_segment_one(self):
        # The ADR-0006 half this change could plausibly break: segment 1 is one
        # cache prefix for the whole workspace, and the bound varies by mode.
        prefixes = {
            prompting.assemble(
                CallType.AUTHOR_SKELETON,
                profile=ADA,
                probe_cadence=ProbeCadence.SOMETIMES,
                blank_range=blank_range,
            ).segment_1.text
            for blank_range in (None, BlankRange(1, 2), BlankRange(4, 6))
        }
        assert len(prefixes) == 1, "the blank bound split segment 1's cache prefix"

    def test_two_modes_bounds_produce_different_segment_twos(self):
        novice = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            blank_range=BlankRange(1, 2),
        ).segment_2.text
        advanced = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            blank_range=BlankRange(4, 6),
        ).segment_2.text
        assert novice != advanced

    def test_no_bound_renders_no_bound_section(self):
        without = prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
        ).segment_2.text
        assert "Blanks to author" not in without

    def test_the_bound_is_optional_so_existing_callers_are_untouched(self):
        # Same shape as TestInquiry's equivalent: a new keyword must not
        # perturb a call that does not pass it.
        explicit = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            blank_range=None,
        )
        implicit = prompting.assemble(
            CallType.AUTHOR_SKELETON, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
        )
        assert explicit == implicit


class TestTheRungTheClientSelected:
    """ADR-0016 / [#56](https://github.com/derkmed/socratic/issues/56).

    The client counts the wrong answers and picks the rung (CONTEXT: Hint
    ladder / rung); the model writes the text for it. So the rung has to reach
    the model, and the volatile tail is the only place it can go — it changes
    on every request, and anything above the last breakpoint would split a
    cache prefix.
    """

    ARGUMENTS = dict(
        profile=ADA,
        probe_cadence=ProbeCadence.SOMETIMES,
        quiz=_quiz(),
        guesses=("b1: 'heap' — wrong",),
        current_blank_id="b1",
        current_guess="the stack frame",
    )

    @pytest.mark.parametrize("rung", [1, 2, 3])
    def test_the_tail_states_the_rung_and_the_ladder_it_sits_on(self, rung):
        tail = prompting.assemble(
            CallType.GRADE_ANSWER, hint_rung=rung, **self.ARGUMENTS
        ).volatile_tail.text
        assert f"Hint rung if wrong: {rung} of 3" in tail

    def test_the_rung_sits_with_the_blank_under_consideration(self):
        # It qualifies the guess being judged, not the history above it.
        tail = prompting.assemble(
            CallType.GRADE_ANSWER, hint_rung=2, **self.ARGUMENTS
        ).volatile_tail.text
        assert tail.index("## Under consideration") < tail.index("Hint rung")

    def test_the_rung_is_optional_so_existing_callers_are_untouched(self):
        # Backward compatibility: omitting the argument leaves the tail
        # byte-identical to what a caller got before the slot existed.
        omitted = prompting.assemble(CallType.GRADE_ANSWER, **self.ARGUMENTS)
        explicit_none = prompting.assemble(
            CallType.GRADE_ANSWER, hint_rung=None, **self.ARGUMENTS
        )
        assert omitted.volatile_tail.text == explicit_none.volatile_tail.text
        assert "Hint rung" not in omitted.volatile_tail.text

    def test_the_rung_never_reaches_a_cached_segment(self):
        # It is per request. Above the last breakpoint it would write a fresh
        # cache entry on every single answer (ADR-0006).
        with_rung = prompting.assemble(
            CallType.GRADE_ANSWER, hint_rung=3, **self.ARGUMENTS
        )
        without = prompting.assemble(CallType.GRADE_ANSWER, **self.ARGUMENTS)
        assert with_rung.segment_1.text == without.segment_1.text
        assert with_rung.segment_2.text == without.segment_2.text


class TestTheFiveCachePrefixesArePinned:
    """A tripwire over segment 1, one digest per call type (ADR-0014).

    Editing a segment 1 invalidates that call type's cache prefix for the whole
    workspace. The cost is real, it is paid once, and **nothing reports it** —
    breakpoint 1 simply writes a new entry and the symptom is a bill rather
    than an error. So an edit that was meant to touch one prefix and touched
    three looks exactly like an edit that touched one.

    These digests make that visible. A deliberate rewrite updates exactly the
    lines it meant to and the diff says which prefixes were spent; an
    accidental one fails a test. The five bodies share `_SHARED_STANCE` by
    concatenation, not by reference at render time, so editing the stance is
    correctly five failures rather than one.

    **Updating a digest is a decision, not a chore.** If a change here was not
    argued for in the pull request that makes it, it is a bug.
    """

    DIGESTS = {
        CallType.AUTHOR_SKELETON: (
            "26a6007c9bfa01460cc750df2e5795f549a5beaa657d1147a3b08754686782a6"
        ),
        CallType.AUTHOR_PEDAGOGY: (
            "9eb883fd09f0364a55f902f4bf69eca53246c28800bd2763ad287d07df0a411c"
        ),
        # Rewritten twice. #56 / ADR-0016 asked for the hint ladder's rung
        # text; #127 / ADR-0019 asks for `revealed_answer`, the phrase that
        # goes in the gap when rung three closes the blank. Declaring the field
        # in the schema alone would have left it forever null, so the prefix
        # had to move — the accepted cost of the decision, both times.
        CallType.GRADE_ANSWER: (
            "7d65300eb39e3146df345081f8a5a403fb180825e7f95f8de41b7181fb1b8e41"
        ),
        CallType.GRADE_PROBE: (
            "b2fddf2971da9da7bfd6a191d897429b79ab15bd80058fc32215f3fbd1451941"
        ),
        CallType.FOLD_NARRATIVE: (
            "fc89b70c6369baf427ca69f2288571bdb1de0ecf1a68baba4f10d85f6c003ff2"
        ),
    }

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_the_prefix_is_the_one_that_was_last_paid_for(self, call_type):
        text = prompting.assemble(
            call_type, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
        ).segment_1.text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == self.DIGESTS[call_type], (
            f"segment 1 for {call_type.value} changed. That invalidates its "
            "cache prefix for the whole workspace (ADR-0014). If the change "
            "was intended, update the digest and say so in the PR."
        )

    def test_every_call_type_is_pinned(self):
        assert set(self.DIGESTS) == set(CallType), "a prefix nobody is watching"


class TestSegmentOneAsksForTheRungsText:
    """The sentence #56 named, and what replaced it (ADR-0016)."""

    def segment_1(self):
        """The text with its line wrapping flattened.

        These assertions are about what the instructions *say*; where the
        paragraph happens to break is not part of the claim, and an assertion
        that broke when a sentence rewrapped would be a test of the formatter.
        """
        text = prompting.assemble(
            CallType.GRADE_ANSWER, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
        ).segment_1.text
        return " ".join(text.split())

    def test_the_model_is_no_longer_told_not_to_write_the_hint(self):
        assert "you do not write the hint here" not in self.segment_1()

    def test_the_model_is_asked_for_the_hint_on_the_rung_it_is_given(self):
        text = self.segment_1()
        assert "Write the hint for the rung you are given" in text

    def test_the_client_still_owns_the_choice_of_rung(self):
        assert "you do not choose it" in self.segment_1()

    def test_rung_three_is_told_to_state_the_answer_and_close_the_blank(self):
        text = self.segment_1()
        assert "Rung three is the last" in text
        assert "state the answer plainly" in text

    def test_the_rubric_is_named_as_the_key_and_forbidden_as_a_hint(self):
        # D4 / ADR-0003. Instruction is the first line of defence; the second
        # is `session._grade_by_model`, which drops a hint that carries it.
        text = self.segment_1()
        assert "never reproduce it" in text
        assert "repeats the rubric has published the key" in text

    def test_only_grade_answer_learned_to_write_a_rung(self):
        # The other four prefixes are undisturbed, which the digest tripwire
        # also pins. This says it in the vocabulary rather than in a hash.
        # `author_pedagogy` still mentions the hint ladder — it is the call
        # that pre-authors the Novice one — so the marker is the new
        # instruction, not the term.
        for call_type in CallType:
            if call_type is CallType.GRADE_ANSWER:
                continue
            text = " ".join(
                prompting.assemble(
                    call_type, profile=ADA, probe_cadence=ProbeCadence.SOMETIMES
                ).segment_1.text.split()
            )
            assert "Write the hint for the rung you are given" not in text
            assert "Rung three is the last one" not in text


class TestSegmentOneAsksForTheShortFormReveal:
    """[ADR-0019](../docs/adr/0019-resolved-blank-text-comes-from-the-service.md).

    Declaring `revealed_answer` in the schema only makes room for it. A field
    segment 1 never mentions is a field the model omits, and the gap it exists
    to fill would have stayed empty on every rung-three close — the fix inert
    on exactly the path it was written for. ADR-0016 had to do the same for
    `hint`.
    """

    def _instruction(self) -> str:
        return prompting.assemble(
            CallType.GRADE_ANSWER,
            profile=ADA,
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_quiz(),
            current_blank_id="b1",
            current_guess="the stack frame",
            hint_rung=3,
        ).segment_1.text

    def test_the_instruction_names_the_field(self):
        assert "revealed_answer" in self._instruction()

    def test_the_instruction_asks_for_a_phrase_and_not_a_sentence(self):
        """The whole point of the field: the hint explains, this one names, and
        what fits in a gap is a noun phrase."""
        instruction = self._instruction().lower()

        assert "phrase" in instruction

    def test_the_instruction_still_forbids_handing_back_the_rubric(self):
        """The guard is structural (`_safe_revealed_answer`), but segment 1
        asking for a short reveal must not read as permission to paste the
        rubric into it."""
        instruction = self._instruction()

        assert "rubric" in instruction
