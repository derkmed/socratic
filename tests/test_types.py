"""The quiz wire format (D2, ADR-0004).

Two properties carry the ADR and are asserted here rather than assumed:

* the explanation is a **flat** segment array of `text | math | blank`, never a
  string with sentinels — rendering walks the list, and resolving a blank swaps
  one element for a `text` or `math` node;
* a blank masks a **whole formula, never a term inside one**, so no type in the
  module permits a blank nested inside another segment.
"""

import dataclasses

import pytest

from socratic.domain.modes import DifficultyMode
from socratic.domain.types import (
    Blank,
    BlankSegment,
    DirectAnswer,
    MathSegment,
    Option,
    Quiz,
    Segment,
    TextSegment,
    resolve_blank,
)


def _novice_blank(blank_id: str = "b1") -> Blank:
    return Blank(
        blank_id=blank_id,
        mode=DifficultyMode.NOVICE,
        options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
        correct_option_id="o1",
        reinforcement="Entropy is the disorder term.",
        hints=("Think about disorder.", "It is the S term.", "The answer is entropy."),
    )


def _advanced_blank(blank_id: str = "b1") -> Blank:
    return Blank(
        blank_id=blank_id,
        mode=DifficultyMode.ADVANCED,
        rubric="Accept any phrasing meaning the disorder of the system.",
    )


def _quiz(*blanks: Blank, explanation: tuple[Segment, ...] | None = None) -> Quiz:
    blanks = blanks or (_novice_blank(),)
    if explanation is None:
        explanation = (TextSegment("Free energy falls when "),)
        for blank in blanks:
            explanation += (BlankSegment(blank.blank_id), TextSegment(" rises."))
    return Quiz(
        quiz_session_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mode=blanks[0].mode,
        topic="thermodynamics",
        explanation=explanation,
        blanks=blanks,
        recap="Free energy trades enthalpy against entropy.",
    )


class TestSegments:
    def test_the_three_segment_types_are_text_math_and_blank(self):
        assert set(Segment.__args__) == {TextSegment, MathSegment, BlankSegment}

    def test_no_segment_type_can_nest_another_segment(self):
        # This is the structural guarantee behind "a blank masks a whole
        # formula, never a term inside one". A `math` segment carries opaque
        # MathML and a `blank` segment carries only an id, so there is nowhere
        # for a nested blank to live.
        for segment_type in Segment.__args__:
            for field in dataclasses.fields(segment_type):
                assert field.type in ("str",), (
                    f"{segment_type.__name__}.{field.name} is {field.type!r}; "
                    "segment fields must be scalars or the array stops being flat"
                )

    def test_the_explanation_is_a_flat_array_not_a_string_with_sentinels(self):
        quiz = _quiz()
        assert isinstance(quiz.explanation, tuple)
        assert [type(segment) for segment in quiz.explanation] == [
            TextSegment,
            BlankSegment,
            TextSegment,
        ]

    def test_segments_are_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            TextSegment("hello").text = "goodbye"


class TestResolvingABlank:
    def test_resolving_swaps_one_element_for_a_text_node(self):
        quiz = _quiz()
        resolved = resolve_blank(quiz.explanation, "b1", TextSegment("entropy"))
        assert resolved == (
            TextSegment("Free energy falls when "),
            TextSegment("entropy"),
            TextSegment(" rises."),
        )

    def test_resolving_swaps_one_element_for_a_math_node(self):
        quiz = _quiz()
        mathml = "<math><mi>S</mi></math>"
        resolved = resolve_blank(quiz.explanation, "b1", MathSegment(mathml))
        assert resolved[1] == MathSegment(mathml)
        assert len(resolved) == len(quiz.explanation)

    def test_resolving_leaves_the_original_untouched(self):
        quiz = _quiz()
        resolve_blank(quiz.explanation, "b1", TextSegment("entropy"))
        assert quiz.explanation[1] == BlankSegment("b1")

    def test_a_blank_may_only_resolve_to_a_text_or_math_node(self):
        quiz = _quiz()
        with pytest.raises(TypeError):
            resolve_blank(quiz.explanation, "b1", BlankSegment("b2"))

    def test_resolving_an_absent_blank_is_an_error(self):
        quiz = _quiz()
        with pytest.raises(KeyError):
            resolve_blank(quiz.explanation, "nope", TextSegment("entropy"))


class TestBlank:
    def test_one_blank_type_carries_both_modes_nullable_fields(self):
        field_names = {field.name for field in dataclasses.fields(Blank)}
        assert {"options", "correct_option_id", "reinforcement", "hints"} <= field_names
        assert "rubric" in field_names

    def test_a_novice_blank_leaves_the_advanced_field_unset(self):
        assert _novice_blank().rubric is None

    def test_an_advanced_blank_leaves_the_novice_fields_unset(self):
        blank = _advanced_blank()
        assert blank.options is None
        assert blank.correct_option_id is None
        assert blank.reinforcement is None
        assert blank.hints is None


class TestQuiz:
    def test_blanks_are_addressable_by_id(self):
        quiz = _quiz(_novice_blank("b1"), _novice_blank("b2"))
        assert quiz.blank("b2").blank_id == "b2"

    def test_an_unknown_blank_id_is_an_error(self):
        with pytest.raises(KeyError):
            _quiz().blank("b9")

    def test_duplicate_blank_ids_are_rejected(self):
        with pytest.raises(ValueError):
            _quiz(_novice_blank("b1"), _novice_blank("b1"))

    def test_a_blank_segment_with_no_matching_blank_is_rejected(self):
        with pytest.raises(ValueError):
            _quiz(explanation=(BlankSegment("ghost"),))

    def test_a_blank_with_no_segment_referencing_it_is_rejected(self):
        with pytest.raises(ValueError):
            _quiz(
                _novice_blank("b1"),
                _novice_blank("b2"),
                explanation=(BlankSegment("b1"),),
            )

    def test_queued_topics_default_to_empty(self):
        assert _quiz().queued_topics == ()


class TestAuthoringResult:
    def test_authoring_returns_a_union_so_the_override_branch_is_a_value(self):
        # ADR-0004: the safety/urgency override must be representable, and the
        # spec's seam table wants it testable as a return value rather than as
        # a side effect.
        from socratic.domain.types import AuthoringResult

        assert set(AuthoringResult.__args__) == {Quiz, DirectAnswer}

    def test_a_direct_answer_carries_prose_and_no_blanks(self):
        answer = DirectAnswer(answer="Call a doctor.", topic="chest pain")
        assert not hasattr(answer, "blanks")
        assert answer.answer == "Call a doctor."
