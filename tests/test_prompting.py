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

import pathlib
import re

import pytest

from socratic.domain import prompting
from socratic.domain.ids import new_quiz_session_id
from socratic.domain.modes import DifficultyMode, ProbeCadence
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
