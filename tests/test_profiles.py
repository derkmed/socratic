"""The one `LearnerProfile` (D8, ADR-0008; #36).

`main` carried two classes of this name - one persistence round-tripped, one the
assembler rendered - and nothing converted between them. The tests that matter
most here are the ones that cross that old fault line: a profile stored through
`LearnerProfileRepository` renders through `render_profile` with no conversion
step, because there is only one type left.
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from socratic.domain import prompting, profiles, records, repositories
from socratic.domain.profiles import LearnerProfile

AT = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


class TestTheProfileIsOneType:
    def test_the_domain_defines_exactly_one_learner_profile(self):
        # #36: two dataclasses of the same name, from two parallel PRs. The
        # names that survive elsewhere must be the same object, not a copy.
        assert prompting.LearnerProfile is LearnerProfile
        assert not hasattr(records, "LearnerProfile")

    def test_it_carries_the_union_of_what_both_halves_needed(self):
        profile = LearnerProfile(
            learner_id="learner-1",
            ledger={"topics/entropy": 4, "weak/free-energy": 2},
            narrative="Comfortable with entropy, shaky on free energy.",
            watermark="01J000000000000000000000",
            updated_at=AT,
        )
        assert profile.learner_id == "learner-1"
        assert profile.ledger == {"topics/entropy": 4, "weak/free-energy": 2}
        assert profile.narrative.startswith("Comfortable")
        assert profile.watermark == "01J000000000000000000000"
        assert profile.updated_at == AT

    def test_a_fresh_profile_knows_nothing_and_says_so(self):
        profile = LearnerProfile(learner_id="learner-1")
        assert profile.ledger == {}
        assert profile.narrative == ""
        assert profile.watermark is None
        assert profile.updated_at is None

    def test_it_is_frozen(self):
        profile = LearnerProfile(learner_id="learner-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            profile.narrative = "rewritten in place"

    def test_two_profiles_with_the_same_contents_are_equal(self):
        one = LearnerProfile(learner_id="l", ledger={"a": 1}, narrative="n")
        other = LearnerProfile(learner_id="l", ledger={"a": 1}, narrative="n")
        assert one == other

    def test_the_ledger_cannot_be_mutated_through_the_caller_s_dict(self):
        # ADR-0005 / repositories: "a stored value cannot be mutated through a
        # reference a caller kept". A ledger handed in as a dict is no
        # exception, or the exactly-recomputed half of D8 is only exact until
        # somebody keeps the dict.
        ledger = {"topics/entropy": 4}
        profile = LearnerProfile(learner_id="learner-1", ledger=ledger)
        ledger["topics/entropy"] = 99
        assert profile.ledger == {"topics/entropy": 4}

    def test_the_ledger_itself_refuses_writes(self):
        profile = LearnerProfile(learner_id="learner-1", ledger={"a": 1})
        with pytest.raises(TypeError):
            profile.ledger["a"] = 2


class TestTheStoredProfileIsTheRenderedProfile:
    """The defect #36 reported: the repository's profile could not be rendered
    and the assembler's could not be stored."""

    def test_a_profile_round_trips_with_its_ledger_and_watermark(self):
        store = repositories.InMemoryLearnerProfileRepository()
        profile = LearnerProfile(
            learner_id="learner-1",
            ledger={"topics/entropy": 4},
            narrative="Comfortable with entropy.",
            watermark="01J000000000000000000000",
            updated_at=AT,
        )
        store.save(profile)
        assert store.get("learner-1") == profile

    def test_what_the_repository_returns_renders_into_segment_2(self):
        store = repositories.InMemoryLearnerProfileRepository()
        store.save(
            LearnerProfile(
                learner_id="learner-1",
                ledger={"topics/entropy": 4},
                narrative="Comfortable with entropy.",
                watermark="01J000000000000000000000",
            )
        )
        loaded = store.get("learner-1")

        rendered = prompting.render_profile(loaded)

        assert "learner-1" in rendered
        assert "topics/entropy: 4" in rendered
        assert "Comfortable with entropy." in rendered

    def test_the_watermark_is_bookkeeping_and_never_reaches_the_prompt(self):
        # The watermark says how far ProfileBuilder has read. It tells the model
        # nothing about the learner, and putting it in segment 2 would churn the
        # cache entry on every build.
        rendered = prompting.render_profile(
            LearnerProfile(
                learner_id="learner-1", watermark="01J000000000000000000000"
            )
        )
        assert "01J000000000000000000000" not in rendered

    def test_the_profile_module_stays_a_leaf_of_the_domain(self):
        # Why the type lives in its own module: persistence must not import the
        # assembler and the assembler must not import the attempt records.
        source = profiles.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        assert "socratic.domain.prompting" not in text
        assert "socratic.domain.records" not in text
        assert "socratic.domain.repositories" not in text
