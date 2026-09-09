"""The per-learner settings, as one value (#14, D10, ADR-0010).

Two settings ride Open WebUI's `UserValves`: the **mode toggle** and
**`probe_cadence`** (CONTEXT). `LearnerSettings` is what the adapter maps them
into, and what everything below the portability seam takes instead of a loose
pair of parameters.

**One value, not two parameters**, for the reason ADR-0010 exists. The rule is
that a per-learner toggle switches *client behaviour* and never prompt text,
including by omission — and the assertion that guards it has to vary
*something*. Varying a value the product keeps growing means the next toggle is
swept into `tests/test_learner_settings.py` without the tests being edited;
varying two hand-written parameters means the next toggle is invisible to them
until somebody remembers.

**Its own module**, importing only `modes`, for the reason `profiles.py` is its
own: the repository ports and the service both need this shape, and neither
should have to import the other to get it.

**No model, no effort, no "fast mode".** Those are admin-level `Valve`s
(ADR-0014, CONTEXT: Model) because caches are model-scoped, and speed switching
would split segment 1 into two namespaces (master spec, out of scope). There is
no field here for one to arrive in, which is the enforcement.

**The mode is carried, not validated.** `ModeRegistry` is the only place mode is
branched on (CONTEXT), so a mode this module has never heard of is the
registry's refusal on the authoring call — a `KeyError` before the model is
consulted — rather than a second, competing opinion here. The cadence *is*
validated, because its four values are its whole definition and nothing
downstream would catch a fifth.
"""

from __future__ import annotations

from dataclasses import dataclass

from socratic.domain.modes import DifficultyMode, ProbeCadence


@dataclass(frozen=True, slots=True)
class LearnerSettings:
    """One learner's current settings.

    Attributes:
      learner_id: Whose settings these are. CONTEXT's partition key, and the
        key `LearnerSettingsRepository` stores them under.
      mode: The difficulty mode the **next authoring call** uses. One attempt,
        one mode (CONTEXT: Mode toggle) — a quiz in flight finishes in the mode
        it was born in, which is a property of the answering path never reading
        this field.
      probe_cadence: How often the self-explanation probe fires. Applies
        **immediately**, from the next correct answer; a probe already pending
        stands (ADR-0010). Defaults to `sometimes` — opt-out, not opt-in.
    """

    learner_id: str
    mode: str = DifficultyMode.NOVICE
    probe_cadence: ProbeCadence = ProbeCadence.SOMETIMES

    @classmethod
    def parse(
        cls,
        learner_id: str,
        *,
        mode: str | None = None,
        probe_cadence: str | None = None,
    ) -> "LearnerSettings":
        """Read the valves' strings, with `None` meaning "never set".

        Args:
          learner_id: Whose settings these are.
          mode: The mode's key, or `None` for the documented default. Carried
            verbatim; the registry is what refuses an unknown one.
          probe_cadence: One of the four documented values, or `None` for
            `sometimes`.

        Returns:
          The settings.

        Raises:
          ValueError: If `probe_cadence` is not one of the four. Naming the
            field, because the caller translating this to a 4xx has no other
            way to say which value was wrong — and because silently falling
            back to `sometimes` would probe a learner who asked not to be, and
            would hide the typo that did it.
        """
        try:
            cadence = (
                ProbeCadence.SOMETIMES
                if probe_cadence is None
                else ProbeCadence(probe_cadence)
            )
        except ValueError:
            raise ValueError(
                f"unknown probe_cadence: {probe_cadence!r}; expected one of "
                + ", ".join(repr(value.value) for value in ProbeCadence)
            ) from None

        return cls(
            learner_id=learner_id,
            mode=DifficultyMode.NOVICE if mode is None else mode,
            probe_cadence=cadence,
        )
