"""`ProfileBuilder` — ledger, narrative and watermark (#16, spec section 9).

The three things this suite exists to hold shut, from ADR-0008 and the master
spec's acceptance 28-30:

* **The ledger is arithmetic and reproducible.** Whatever the incremental path
  reaches, a from-scratch recomputation over the same history must reach too —
  which is why `recompute_ledger` is a real function with its own tests rather
  than a comment claiming the property.
* **One fold per advancing run, none on a no-op run**, counted through
  `RecordingModelClient`.
* **The profile never climbs above cache breakpoint 1.** A built profile is
  still just a profile: segment 1 stays byte-identical across learners.
"""

import ast
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from socratic.domain import profile_builder, prompting
from socratic.domain.ids import Ulid
from socratic.domain.model_client import ModelResponse, RecordingModelClient
from socratic.domain.modes import DifficultyMode, GradingStrategy, ProbeCadence
from socratic.domain.profiles import LearnerProfile
from socratic.domain.prompting import CallType
from socratic.domain.records import (
    Guess,
    Outcome,
    Probe,
    QuizAttempt,
    Verdict,
)
from socratic.domain.repositories import (
    InMemoryAttemptRepository,
    InMemoryLearnerProfileRepository,
)
from socratic.domain.types import Blank, BlankSegment, Option, Quiz, TextSegment

AT = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)

LEARNER = "learner-1"


# --- Fixtures ----------------------------------------------------------------


def _quiz() -> Quiz:
    blanks = (
        Blank(
            blank_id="b1",
            mode=DifficultyMode.NOVICE,
            options=(Option("o1", "entropy"), Option("o2", "enthalpy")),
            correct_option_id="o1",
            reinforcement="Entropy is the disorder term.",
            hints=("a", "b", "c"),
        ),
    )
    return Quiz(
        quiz_session_id=str(Ulid.mint()),
        mode=DifficultyMode.NOVICE,
        topic="the second law",
        explanation=(
            TextSegment("Heat flows because "),
            BlankSegment("b1"),
            TextSegment(" rises."),
        ),
        blanks=blanks,
        recap="Entropy never decreases.",
    )


def _guess(verdict: Verdict = Verdict.CORRECT, ordinal: int = 1) -> Guess:
    return Guess(
        blank_id="b1",
        submitted="entropy",
        verdict=verdict,
        attempt_ordinal=ordinal,
        hint_rung_shown=None,
        created_at=AT,
        graded_by=GradingStrategy.DETERMINISTIC,
    )


def _probe(verdict: Verdict | None = Verdict.CORRECT) -> Probe:
    answered = verdict is not None
    return Probe(
        blank_id="b1",
        question="How did you arrive at that?",
        self_explanation="Disorder increases." if answered else None,
        verdict=verdict,
        reopened_blank=False,
        cadence_at_fire=ProbeCadence.SOMETIMES,
        asked_at=AT,
        answered_at=AT if answered else None,
        message_id="msg_1" if answered else None,
    )


def _attempt(
    *,
    attempt_id: str | None = None,
    learner_id: str = LEARNER,
    topic: str = "the second law",
    mode: DifficultyMode = DifficultyMode.NOVICE,
    guesses: tuple[Guess, ...] = (),
    probes: tuple[Probe, ...] = (),
    sealed: bool = True,
    outcome: Outcome = Outcome.RESOLVED,
) -> QuizAttempt:
    """One attempt. Sealed by default — an unsealed one is the barrier case."""
    # The attempt and its quiz name one session, so the quiz's own id is what
    # goes on the record rather than a second minted one.
    quiz = _quiz()
    attempt = QuizAttempt(
        attempt_id=attempt_id or str(Ulid.mint()),
        learner_id=learner_id,
        session_id=quiz.quiz_session_id,
        quiz=quiz,
        mode=mode,
        topic=topic,
        created_at=AT,
        probe_cadence_at_authoring=ProbeCadence.SOMETIMES,
        model_id="claude-opus-5",
        effort="low",
        prompt_version="2026-09-08.1",
        guesses=guesses,
        probes=probes,
    )
    if not sealed:
        return attempt
    if outcome is Outcome.ABANDONED:
        return attempt.abandoned(LATER)
    return attempt.sealed(LATER)


def _fold_response(narrative: str) -> ModelResponse:
    return ModelResponse(
        content=json.dumps({profile_builder.NARRATIVE_FIELD: narrative}),
        message_id="msg_fold",
    )


def _model(*narratives: str) -> RecordingModelClient:
    return RecordingModelClient(
        {CallType.FOLD_NARRATIVE: [_fold_response(text) for text in narratives]}
        if narratives
        else {CallType.FOLD_NARRATIVE: [_fold_response("A learner in progress.")]}
    )


def _clock(at: datetime = LATER):
    return lambda: at


def _builder(
    attempts: InMemoryAttemptRepository,
    profiles: InMemoryLearnerProfileRepository,
    model: RecordingModelClient,
    at: datetime = LATER,
) -> profile_builder.ProfileBuilder:
    return profile_builder.ProfileBuilder(
        attempts=attempts, profiles=profiles, model=model, clock=_clock(at)
    )


def _wired(*attempts: QuizAttempt, narratives: tuple[str, ...] = ()):
    """The three repositories, the stub, and a builder over them."""
    attempt_repo = InMemoryAttemptRepository()
    for attempt in attempts:
        attempt_repo.save(attempt)
    profiles = InMemoryLearnerProfileRepository()
    model = _model(*narratives)
    return attempt_repo, profiles, model, _builder(attempt_repo, profiles, model)


# --- The eligibility rule ----------------------------------------------------


class TestSealedPrefix:
    """Which attempts a run is allowed to fold in.

    Exclusive of the watermark, and stopping at the first unsealed attempt —
    the two halves of the rule the whole reproducibility property rests on.
    """

    def test_with_no_watermark_every_sealed_attempt_is_eligible(self):
        one, two = _attempt(), _attempt()
        assert profile_builder.sealed_prefix((one, two)) == (one, two)

    def test_the_boundary_attempt_itself_is_not_reprocessed(self):
        # The watermark names an attempt *already folded in*, so the boundary
        # is exclusive. The attempt whose id equals it must not come back.
        one, two = _attempt(), _attempt()
        eligible = profile_builder.sealed_prefix(
            (one, two), after=one.attempt_id
        )
        assert eligible == (two,)

    def test_the_attempt_after_the_boundary_is_processed(self):
        one, two = _attempt(), _attempt()
        eligible = profile_builder.sealed_prefix(
            (one, two), after=two.attempt_id
        )
        assert eligible == ()

    def test_an_unsealed_attempt_is_a_barrier_not_a_skip(self):
        # An in-flight attempt still accumulates guesses. Counting it now
        # would make the ledger unreproducible; stepping over it would lose it
        # for good, since the watermark only moves forward.
        first = _attempt()
        in_flight = _attempt(sealed=False)
        later = _attempt()
        ordered = tuple(sorted((first, in_flight, later), key=lambda a: a.attempt_id))
        assert profile_builder.sealed_prefix(ordered) == (first,)

    def test_it_sorts_by_ulid_rather_than_trusting_the_caller(self):
        one, two = _attempt(), _attempt()
        assert profile_builder.sealed_prefix((two, one)) == (one, two)


# --- The ledger --------------------------------------------------------------


class TestTally:
    def test_it_counts_attempts_topics_modes_and_outcomes(self):
        figures = profile_builder.tally(
            (
                _attempt(topic="the second law"),
                _attempt(topic="tail recursion", mode=DifficultyMode.ADVANCED),
            )
        )
        assert figures["attempts/total"] == 2
        assert figures["topics/the second law"] == 1
        assert figures["topics/tail recursion"] == 1
        assert figures["attempts/mode/novice"] == 1
        assert figures["attempts/mode/advanced"] == 1
        assert figures["outcomes/resolved"] == 2

    def test_an_abandoned_attempt_is_counted_under_its_own_outcome(self):
        figures = profile_builder.tally(
            (_attempt(outcome=Outcome.ABANDONED),)
        )
        assert figures["outcomes/abandoned"] == 1
        assert "outcomes/resolved" not in figures

    def test_it_counts_guesses_by_verdict(self):
        figures = profile_builder.tally(
            (
                _attempt(
                    guesses=(
                        _guess(Verdict.INCORRECT, ordinal=1),
                        _guess(Verdict.CORRECT, ordinal=2),
                    )
                ),
            )
        )
        assert figures["guesses/total"] == 2
        assert figures["guesses/correct"] == 1
        assert figures["guesses/incorrect"] == 1

    def test_it_counts_probes_including_the_dismissed_ones(self):
        figures = profile_builder.tally(
            (
                _attempt(
                    guesses=(_guess(),),
                    probes=(_probe(Verdict.CORRECT), _probe(None)),
                ),
            )
        )
        assert figures["probes/asked"] == 2
        assert figures["probes/correct"] == 1
        assert figures["probes/dismissed"] == 1

    def test_a_weak_area_is_tallied_by_topic(self):
        figures = profile_builder.tally(
            (
                _attempt(
                    topic="tail recursion",
                    guesses=(_guess(Verdict.INCORRECT),),
                    probes=(),
                ),
            )
        )
        assert figures["weak/tail recursion"] == 1

    def test_a_clean_attempt_leaves_no_weak_area_key(self):
        figures = profile_builder.tally((_attempt(guesses=(_guess(),)),))
        assert "weak/the second law" not in figures

    def test_no_figure_is_ever_zero(self):
        # A ledger of zeros is noise in segment 2, and `render_profile` prints
        # every key it is given.
        figures = profile_builder.tally((_attempt(),))
        assert all(value > 0 for value in figures.values()), figures


class TestMergeLedgers:
    def test_it_adds_shared_keys_and_carries_the_rest(self):
        merged = profile_builder.merge_ledgers(
            {"attempts/total": 2, "topics/a": 2}, {"attempts/total": 1, "topics/b": 1}
        )
        assert merged == {"attempts/total": 3, "topics/a": 2, "topics/b": 1}

    def test_it_does_not_mutate_its_arguments(self):
        base = {"attempts/total": 1}
        profile_builder.merge_ledgers(base, {"attempts/total": 1})
        assert base == {"attempts/total": 1}

    def test_it_accepts_the_sealed_mapping_a_stored_profile_carries(self):
        stored = LearnerProfile(learner_id=LEARNER, ledger={"attempts/total": 1})
        merged = profile_builder.merge_ledgers(
            stored.ledger, {"attempts/total": 1}
        )
        assert merged["attempts/total"] == 2


# --- The builder -------------------------------------------------------------


class TestARunAdvancesTheProfile:
    def test_a_first_run_writes_ledger_narrative_and_watermark(self):
        attempt = _attempt(guesses=(_guess(),))
        attempts, profiles, model, builder = _wired(
            attempt, narratives=("Works steadily through thermodynamics.",)
        )

        built = builder.run(LEARNER)

        assert built.ledger["attempts/total"] == 1
        assert built.narrative == "Works steadily through thermodynamics."
        assert built.watermark == attempt.attempt_id
        assert built.updated_at == LATER
        assert profiles.get(LEARNER) == built

    def test_the_fold_costs_exactly_one_call(self):
        attempts, profiles, model, builder = _wired(_attempt(), _attempt())

        builder.run(LEARNER)

        assert model.call_count(CallType.FOLD_NARRATIVE) == 1
        assert model.call_count() == 1

    def test_a_second_run_folds_only_what_is_new(self):
        first, second = _attempt(), _attempt()
        attempt_repo, profiles, model, builder = _wired(
            first, narratives=("First pass.", "Second pass.")
        )

        builder.run(LEARNER)
        attempt_repo.save(second)
        built = builder.run(LEARNER)

        assert built.ledger["attempts/total"] == 2
        assert built.narrative == "Second pass."
        assert built.watermark == second.attempt_id
        assert model.call_count(CallType.FOLD_NARRATIVE) == 2

    def test_the_previous_narrative_and_the_new_ledger_reach_the_fold(self):
        first, second = _attempt(), _attempt()
        attempt_repo, profiles, model, builder = _wired(
            first, narratives=("First pass.", "Second pass.")
        )
        builder.run(LEARNER)
        attempt_repo.save(second)
        builder.run(LEARNER)

        segment_2 = model.calls_of(CallType.FOLD_NARRATIVE)[1].segments.segment_2.text
        # The narrative it is folding *forward*, and the figures it must not
        # contradict — the incremented ones, not the stale ones.
        assert "First pass." in segment_2
        assert "attempts/total: 2" in segment_2

    def test_the_new_attempts_reach_the_volatile_tail(self):
        attempt = _attempt(topic="tail recursion")
        attempts, profiles, model, builder = _wired(attempt)

        builder.run(LEARNER)

        tail = model.calls_of(CallType.FOLD_NARRATIVE)[0].segments.volatile_tail.text
        assert attempt.attempt_id in tail
        assert "tail recursion" in tail

    def test_a_learner_with_no_attempts_at_all_is_left_alone(self):
        attempts, profiles, model, builder = _wired()

        built = builder.run("nobody")

        assert built.ledger == {}
        assert built.watermark is None
        assert profiles.get("nobody") is None
        model.assert_never_called()

    def test_it_reads_only_its_own_learner_s_partition(self):
        mine = _attempt(learner_id=LEARNER)
        theirs = _attempt(learner_id="learner-2")
        attempts, profiles, model, builder = _wired(mine, theirs)

        built = builder.run(LEARNER)

        assert built.ledger["attempts/total"] == 1

    def test_an_in_flight_attempt_holds_the_watermark_where_it_is(self):
        first = _attempt()
        in_flight = _attempt(sealed=False)
        attempts, profiles, model, builder = _wired(
            *sorted((first, in_flight), key=lambda a: a.attempt_id)
        )

        built = builder.run(LEARNER)

        assert built.watermark == first.attempt_id
        assert built.ledger["attempts/total"] == 1


class TestANoOpRun:
    """Acceptance 29 — running twice with no new attempts changes nothing."""

    def test_the_figures_and_the_watermark_do_not_move(self):
        attempts, profiles, model, builder = _wired(_attempt(), _attempt())

        once = builder.run(LEARNER)
        twice = builder.run(LEARNER)

        assert twice.ledger == once.ledger
        assert twice.watermark == once.watermark
        assert twice.narrative == once.narrative
        assert twice.updated_at == once.updated_at

    def test_it_does_not_consult_the_model(self):
        attempts, profiles, model, builder = _wired(_attempt())
        builder.run(LEARNER)

        builder.run(LEARNER)

        assert model.call_count(CallType.FOLD_NARRATIVE) == 1

    def test_a_run_with_nothing_new_is_refused_the_model_outright(self):
        # The strong form: a stub that raises the moment it is touched.
        attempt_repo = InMemoryAttemptRepository()
        attempt_repo.save(_attempt())
        profiles = InMemoryLearnerProfileRepository()
        builder = _builder(attempt_repo, profiles, _model())
        stored = builder.run(LEARNER)

        refusing = RecordingModelClient(fail_if_called=True)
        again = _builder(attempt_repo, profiles, refusing).run(LEARNER)

        assert again == stored
        refusing.assert_never_called()


class TestTheLedgerIsReproducible:
    """Acceptance 30 — two code paths, one answer."""

    def test_incremental_equals_from_scratch(self):
        history = [_attempt() for _ in range(3)]
        attempt_repo, profiles, model, builder = _wired()

        for attempt in history:
            attempt_repo.save(attempt)
            builder.run(LEARNER)

        incremental = builder.run(LEARNER).ledger
        from_scratch = profile_builder.recompute_ledger(
            attempt_repo.list_for_learner(LEARNER)
        )
        assert dict(incremental) == dict(from_scratch)

    def test_they_agree_across_a_varied_history(self):
        attempt_repo, profiles, model, builder = _wired()
        history = (
            _attempt(topic="a", guesses=(_guess(Verdict.INCORRECT),)),
            _attempt(topic="b", mode=DifficultyMode.ADVANCED),
            _attempt(topic="a", outcome=Outcome.ABANDONED),
            _attempt(
                topic="b",
                guesses=(_guess(),),
                probes=(_probe(Verdict.INCORRECT), _probe(None)),
            ),
        )
        for attempt in history:
            attempt_repo.save(attempt)
            builder.run(LEARNER)

        incremental = profiles.get(LEARNER).ledger
        from_scratch = profile_builder.recompute_ledger(
            attempt_repo.list_for_learner(LEARNER)
        )
        assert dict(incremental) == dict(from_scratch)
        assert from_scratch["attempts/total"] == 4

    def test_they_agree_when_an_in_flight_attempt_sat_in_the_middle(self):
        # The case the barrier rule exists for: the two paths must apply the
        # same eligibility rule, or "reproducible" is only true when nobody is
        # mid-quiz.
        first = _attempt()
        in_flight = _attempt(sealed=False)
        later = _attempt()
        ordered = sorted((first, in_flight, later), key=lambda a: a.attempt_id)
        attempt_repo, profiles, model, builder = _wired(*ordered)

        incremental = builder.run(LEARNER).ledger
        from_scratch = profile_builder.recompute_ledger(
            attempt_repo.list_for_learner(LEARNER)
        )
        assert dict(incremental) == dict(from_scratch)
        assert from_scratch["attempts/total"] == 1

    def test_recomputing_an_empty_history_is_an_empty_ledger(self):
        assert dict(profile_builder.recompute_ledger(())) == {}


class TestTheLedgerIsAuthoritative:
    def test_a_narrative_that_contradicts_the_figures_changes_none_of_them(self):
        attempts, profiles, model, builder = _wired(
            _attempt(),
            narratives=("This learner has attempted ninety-one quizzes.",),
        )

        built = builder.run(LEARNER)

        assert built.ledger["attempts/total"] == 1
        assert "ninety-one" in built.narrative

    def test_the_ledger_renders_ahead_of_the_narrative(self):
        attempts, profiles, model, builder = _wired(
            _attempt(), narratives=("Prose about the learner.",)
        )
        built = builder.run(LEARNER)

        rendered = prompting.render_profile(built)

        assert rendered.index("attempts/total") < rendered.index("Prose about")


class TestTheFoldResponse:
    def test_a_payload_without_the_narrative_field_is_refused(self):
        attempt_repo = InMemoryAttemptRepository()
        attempt_repo.save(_attempt())
        model = RecordingModelClient(
            {CallType.FOLD_NARRATIVE: [ModelResponse(content="{}", message_id="m")]}
        )
        builder = _builder(attempt_repo, InMemoryLearnerProfileRepository(), model)

        with pytest.raises(ValueError, match="narrative"):
            builder.run(LEARNER)

    def test_a_payload_that_is_not_an_object_is_refused(self):
        attempt_repo = InMemoryAttemptRepository()
        attempt_repo.save(_attempt())
        model = RecordingModelClient(
            {
                CallType.FOLD_NARRATIVE: [
                    ModelResponse(content="not json at all", message_id="m")
                ]
            }
        )
        builder = _builder(attempt_repo, InMemoryLearnerProfileRepository(), model)

        with pytest.raises(ValueError):
            builder.run(LEARNER)

    def test_a_refused_payload_leaves_the_stored_profile_untouched(self):
        attempt_repo = InMemoryAttemptRepository()
        attempt_repo.save(_attempt())
        profiles = InMemoryLearnerProfileRepository()
        model = RecordingModelClient(
            {CallType.FOLD_NARRATIVE: [ModelResponse(content="{}", message_id="m")]}
        )
        with pytest.raises(ValueError):
            _builder(attempt_repo, profiles, model).run(LEARNER)

        assert profiles.get(LEARNER) is None


# --- The prompt invariants ---------------------------------------------------


class TestTheBuiltProfileReachesSegmentTwoAndNoHigher:
    """Acceptance 28, and the ADR-0006 invariant it must not break."""

    def test_the_next_authoring_call_carries_the_profile_in_segment_2(self):
        attempts, profiles, model, builder = _wired(
            _attempt(topic="tail recursion"),
            narratives=("Comfortable with base cases.",),
        )
        built = builder.run(LEARNER)

        segments = prompting.assemble(
            CallType.AUTHOR_SKELETON,
            profile=built,
            probe_cadence=ProbeCadence.SOMETIMES,
            inquiry="What is a tail call?",
        )

        assert "topics/tail recursion" in segments.segment_2.text
        assert "Comfortable with base cases." in segments.segment_2.text

    def test_segment_1_stays_byte_identical_across_two_built_profiles(self):
        one = _built_profile("learner-ada", "monads", "Ada is fluent.")
        other = _built_profile("learner-basho", "haiku", "Basho counts well.")

        for call_type in CallType:
            first = prompting.assemble(
                call_type, profile=one, probe_cadence=ProbeCadence.ALWAYS
            ).segment_1.text
            second = prompting.assemble(
                call_type, profile=other, probe_cadence=ProbeCadence.OFF
            ).segment_1.text
            assert first == second, call_type

    def test_no_trace_of_the_built_profile_appears_in_segment_1(self):
        built = _built_profile("learner-ada", "monads", "Ada is fluent.")
        for call_type in CallType:
            segment_1 = prompting.assemble(
                call_type, profile=built, probe_cadence=ProbeCadence.ALWAYS
            ).segment_1.text
            for marker in ("learner-ada", "monads", "Ada is fluent"):
                assert marker not in segment_1


def _built_profile(learner_id: str, topic: str, narrative: str) -> LearnerProfile:
    attempts, profiles, model, builder = _wired(
        _attempt(learner_id=learner_id, topic=topic), narratives=(narrative,)
    )
    return builder.run(learner_id)


# --- The job is not wired to anything ----------------------------------------


SCHEDULING_MODULES = frozenset(
    {"sched", "asyncio", "threading", "queue", "multiprocessing", "signal", "time"}
)


class TestNothingSchedulesTheJob:
    """Acceptance: "no scheduler is wired; manual or timer-triggered only".

    The module takes a clock so a caller can freeze it, and does nothing else
    with time. Anything that could make a run happen on its own would put the
    job on somebody's critical path, which is the one thing ADR-0006 forbids.
    """

    def test_the_module_imports_nothing_that_could_schedule_a_run(self):
        source = pathlib.Path(profile_builder.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        assert imported & SCHEDULING_MODULES == set(), sorted(imported)

    def test_the_clock_is_injected_rather_than_read_from_the_wall(self):
        frozen = datetime(2030, 1, 1, tzinfo=timezone.utc)
        attempt_repo = InMemoryAttemptRepository()
        attempt_repo.save(_attempt())
        profiles = InMemoryLearnerProfileRepository()
        builder = _builder(attempt_repo, profiles, _model(), at=frozen)

        assert builder.run(LEARNER).updated_at == frozen

    def test_running_is_the_only_public_verb(self):
        # The dependencies are public attributes; `run` is the only verb, so
        # there is no `start`, `schedule`, `every` or `loop` to reach for.
        methods = sorted(
            name
            for name, value in vars(profile_builder.ProfileBuilder).items()
            if not name.startswith("_") and callable(value)
        )
        assert methods == ["run"]


def test_a_ulid_watermark_orders_the_way_the_boundary_assumes():
    # The whole boundary is a string comparison, which is only sound because
    # ULIDs are order-preserving (ADR-0007). Pinned here so a change to
    # `ids.py` fails this suite rather than silently reordering history.
    early = Ulid.mint(clock=lambda: int(AT.timestamp() * 1000))
    late = Ulid.mint(clock=lambda: int((AT + timedelta(days=1)).timestamp() * 1000))
    assert str(early) < str(late)
