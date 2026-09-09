"""Per-learner settings, and the invariant they must never break (#14).

Two settings ride `UserValves`: the **mode toggle** and **`probe_cadence`**.
`LearnerSettings` is their domain-side shape — the one value the Open WebUI
adapter maps valves into, and the one value the cache assertion varies.

The assertion is the point of the ticket. Segment 1 is byte-identical for every
learner in the workspace, per call type (ADR-0006, ADR-0014), and ADR-0010's
rule is that a per-learner toggle switches *client behaviour* and never prompt
text — including by **omission**, which splits the cache exactly as surely as an
addition. So `TestSettingsNeverReachSegmentOne` walks the whole settings
cross-product rather than one setting at a time, and
`TestTheAssertionHasPower` builds assemblers that violate the rule in both
directions to show the same assertion catches both. An unenforced guard and a
working one look identical from the outside.
"""

from __future__ import annotations

import itertools

import pytest

from socratic.domain import prompting
from socratic.domain.modes import DifficultyMode, ProbeCadence
from socratic.domain.profiles import LearnerProfile
from socratic.domain.prompting import CallType
from socratic.domain.registry import default_registry
from socratic.domain.repositories import InMemoryLearnerSettingsRepository
from socratic.domain.settings import LearnerSettings

ADA = LearnerProfile(
    learner_id="learner-ada",
    ledger={"topics/monads": 7},
    narrative="Ada is fluent in category theory.",
)
BASHO = LearnerProfile(
    learner_id="learner-basho",
    ledger={"topics/haiku": 3},
    narrative="Basho counts syllables well.",
)

EVERY_SETTING = tuple(
    LearnerSettings(learner_id=learner, mode=mode, probe_cadence=cadence)
    for learner, mode, cadence in itertools.product(
        (ADA.learner_id, BASHO.learner_id),
        (DifficultyMode.NOVICE, DifficultyMode.ADVANCED),
        tuple(ProbeCadence),
    )
)
"""Every combination the product admits today: two learners x two modes x four
cadences. Sixteen settings, and one segment 1 per call type."""


def _profile_for(settings: LearnerSettings) -> LearnerProfile:
    return ADA if settings.learner_id == ADA.learner_id else BASHO


def _assemble(call_type: CallType, settings: LearnerSettings):
    """Assemble the way a caller holding settings would.

    The mode reaches the prompt only as the registry's `blank_range`, and only
    in segment 2 — which is exactly the shape the authoring path uses.
    """
    return prompting.assemble(
        call_type,
        profile=_profile_for(settings),
        probe_cadence=settings.probe_cadence,
        blank_range=default_registry().policy_for(settings.mode).blank_range,
    )


class TestTheSettingsValue:
    def test_the_cadence_defaults_to_sometimes(self):
        # ADR-0010: opt-out, not opt-in.
        assert LearnerSettings(learner_id="l").probe_cadence is ProbeCadence.SOMETIMES

    def test_the_mode_defaults_to_novice(self):
        assert LearnerSettings(learner_id="l").mode == DifficultyMode.NOVICE

    def test_it_is_frozen_so_a_stored_value_cannot_be_rewritten(self):
        settings = LearnerSettings(learner_id="l")
        with pytest.raises(Exception):
            settings.probe_cadence = ProbeCadence.OFF  # type: ignore[misc]

    def test_it_carries_no_model_and_no_effort(self):
        # ADR-0014: the model is an admin `Valve`, never `UserValves` — a
        # per-learner model would fragment every segment 1. Structural: there
        # is no field for one to arrive in.
        fields = set(LearnerSettings.__dataclass_fields__)
        assert fields == {"learner_id", "mode", "probe_cadence"}


class TestParsingTheValvesStrings:
    def test_absent_values_take_the_documented_defaults(self):
        settings = LearnerSettings.parse("l")

        assert settings.mode == DifficultyMode.NOVICE
        assert settings.probe_cadence is ProbeCadence.SOMETIMES

    @pytest.mark.parametrize("cadence", [c.value for c in ProbeCadence])
    def test_it_accepts_exactly_the_four_documented_cadences(self, cadence):
        settings = LearnerSettings.parse("l", probe_cadence=cadence)

        assert settings.probe_cadence is ProbeCadence(cadence)

    @pytest.mark.parametrize("bogus", ["never", "OFF", "", "sometimes "])
    def test_an_unknown_cadence_is_refused_not_defaulted(self, bogus):
        # Silently falling back to `sometimes` would probe a learner who asked
        # not to be, and would hide the typo that caused it.
        with pytest.raises(ValueError, match="probe_cadence"):
            LearnerSettings.parse("l", probe_cadence=bogus)

    def test_the_mode_is_carried_not_validated_here(self):
        # CONTEXT: ModeRegistry — the only place mode is branched on. A mode
        # this module has never heard of is the registry's refusal on the
        # authoring call, not a second opinion here.
        assert LearnerSettings.parse("l", mode="expert").mode == "expert"


class TestTheSettingsRepository:
    def test_a_learner_with_no_recorded_settings_reads_as_none(self):
        # Not as defaults: "never set" and "set to the defaults" are the same
        # behaviour but not the same fact, and the answering path needs to tell
        # them apart to fall back to the attempt's stamp.
        assert InMemoryLearnerSettingsRepository().get("learner-ada") is None

    def test_it_holds_one_document_per_learner_rewritten_in_place(self):
        repository = InMemoryLearnerSettingsRepository()
        repository.save(LearnerSettings(learner_id="learner-ada"))
        repository.save(
            LearnerSettings(learner_id="learner-ada", probe_cadence=ProbeCadence.OFF)
        )

        assert repository.get("learner-ada").probe_cadence is ProbeCadence.OFF

    def test_learners_do_not_share_a_document(self):
        repository = InMemoryLearnerSettingsRepository()
        repository.save(
            LearnerSettings(learner_id="learner-ada", probe_cadence=ProbeCadence.OFF)
        )

        assert repository.get("learner-basho") is None


class TestSettingsNeverReachSegmentOne:
    """Master spec acceptance 10 and 11, asserted against the settings value.

    Varying one setting at a time proves less than varying the value the
    product will keep growing: the next toggle lands in `LearnerSettings` and
    is swept up by these tests without them being edited.
    """

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_one_segment_one_per_call_type_across_every_setting(self, call_type):
        texts = {
            _assemble(call_type, settings).segment_1.text
            for settings in EVERY_SETTING
        }

        assert len(texts) == 1, (
            f"{call_type.value} segment 1 has {len(texts)} variants across "
            f"{len(EVERY_SETTING)} settings; the workspace cache is split"
        )

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_it_is_byte_identical_and_not_merely_equal_in_length(self, call_type):
        # Spelled out because the failure this guards is a *substitution* of
        # equal length — a mode name swapped for another — which a length
        # check alone would wave through.
        first = _assemble(call_type, EVERY_SETTING[0]).segment_1.text
        for settings in EVERY_SETTING[1:]:
            other = _assemble(call_type, settings).segment_1.text
            assert other == first
            assert other.encode("utf-8") == first.encode("utf-8")

    @pytest.mark.parametrize("call_type", list(CallType))
    def test_no_learner_identifier_appears_in_segment_one(self, call_type):
        # The cadence *values* are deliberately not searched for: "off" and
        # "always" are ordinary English and appear in instructions that are
        # the same for everyone. Byte-identity across the cross-product is
        # what proves the cadence did not reach the prompt; this catches the
        # other leak, a learner naming themselves in the shared prefix.
        text = _assemble(call_type, EVERY_SETTING[0]).segment_1.text
        leaked = [
            value
            for value in (ADA.learner_id, BASHO.learner_id, "Ada", "Basho", "monads")
            if value in text
        ]

        assert leaked == [], f"{call_type.value} segment 1 names {leaked}"

    def test_turning_probes_off_removes_no_text_at_all(self):
        # The omission half of ADR-0010, stated as a length comparison so a
        # dropped sentence cannot pass as a rewrite.
        off = _assemble(
            CallType.GRADE_ANSWER,
            LearnerSettings(learner_id="l", probe_cadence=ProbeCadence.OFF),
        ).segment_1.text
        always = _assemble(
            CallType.GRADE_ANSWER,
            LearnerSettings(learner_id="l", probe_cadence=ProbeCadence.ALWAYS),
        ).segment_1.text

        assert off == always
        assert len(off) == len(always)


class TestTheAssertionHasPower:
    """Meta-tests: two broken assemblers, one assertion, both caught.

    Without these, `test_one_segment_one_per_call_type_across_every_setting`
    would pass just as happily if `assemble` returned the empty string.
    """

    @staticmethod
    def _variants(broken) -> int:
        return len({broken(settings) for settings in EVERY_SETTING})

    def test_an_assembler_that_adds_text_per_setting_is_caught(self):
        def adds(settings: LearnerSettings) -> str:
            base = _assemble(CallType.GRADE_ANSWER, settings).segment_1.text
            if settings.probe_cadence is ProbeCadence.OFF:
                return base + "\nDo not ask the learner to explain themselves."
            return base

        assert self._variants(adds) > 1

    def test_an_assembler_that_omits_text_per_setting_is_caught(self):
        def omits(settings: LearnerSettings) -> str:
            base = _assemble(CallType.GRADE_ANSWER, settings).segment_1.text
            if settings.probe_cadence is ProbeCadence.OFF:
                return base.replace(
                    "After a correct answer you may be asked to probe.", ""
                )
            return base

        assert self._variants(omits) > 1

    def test_the_real_assembler_is_the_one_that_passes(self):
        assert (
            self._variants(
                lambda settings: _assemble(
                    CallType.GRADE_ANSWER, settings
                ).segment_1.text
            )
            == 1
        )
