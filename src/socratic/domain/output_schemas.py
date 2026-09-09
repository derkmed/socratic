"""The one place a call type's output schema is composed.

Every call constrains its output (`output_config.format`), and the shape it
asks for has to be the shape the domain reads back. Before this module those
were two independent statements: the adapter carried five hand-written shapes
and the parsers carried five expectations, and four of the five pairs had
drifted apart without anything failing — every test runs against
`RecordingModelClient`, which serves canned content and never looks at a schema
(#66).

**The composition point lives in the domain rather than in the adapter**, for
two reasons. The dependency only runs one way: the domain may not import the
adapter (`tests/test_import_hygiene.py`), and a schema is a statement about what
the *domain* parses, with the adapter only carrying it to the wire. And the
agreement between a schema and its parser is checkable without an SDK, which is
the half of the suite that runs when the optional `anthropic` extra is absent.
`AnthropicModelClient(schemas=...)` stays the injection point; this module is
what fills it.

Where each shape comes from:

* **The two authoring calls** take their per-blank shape from the registry -
  `ModePolicy.rules_for(stage).schema_fragment`. `StageRules` exists precisely
  so a stage's output schema and its validator cannot drift apart, and this
  module is what finally reads the schema half. Per mode **through the
  registry**: an `if mode ==` out here would be the bug the registry exists to
  prevent (CONTEXT: ModeRegistry).
* **`grade_answer`, `grade_probe` and `fold_narrative`** are composed here
  rather than added to the registry. They do not vary by mode, and the
  registry's job is mode variance - putting a mode-invariant shape behind
  `policy_for(mode)` would claim it varies. What keeps them honest is
  `tests/test_output_schemas.py`, which mints a payload from each schema and
  runs it through the real parser.

Two conventions, both the repo's own. **Strictness is structural**:
`additionalProperties: false` plus a complete `required` list at every object
node is how the Messages API spells a strict structured-output format - there is
no `strict: true` field on `output_config.format` (#41). **A nullable field is
`"type": ["string", "null"]` with the key still in `required`**, which is how the
registry's fragments already say it.

A genuine tagged union - ADR-0004's `quiz | direct_answer`, and the explanation's
`text | math | blank` segments - is `anyOf` over strict branches instead. That is
what makes a quiz with no explanation unrepresentable rather than merely invalid,
and it is what lets a test mint one exact payload per branch.

Spec: `docs/specs/consolidated-output-schemas.md`.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from socratic.domain import authoring
from socratic.domain import profile_builder
from socratic.domain import prompting
from socratic.domain import registry as registry_module
from socratic.domain.records import Verdict

Schema = dict[str, Any]

CallType = prompting.CallType

STRING: Schema = {"type": "string"}
NULLABLE_STRING: Schema = {"type": ["string", "null"]}

VERDICT: Schema = {
    "type": "string",
    "enum": [member.value for member in Verdict],
}
"""Derived from `records.Verdict`, never restated.

The sharpest failure #66 found was `grade_probe` asking the model for
`sound`/`unsound` while `Verdict("sound")` raises - a `GradingParseError` on
every probe reply, against a schema that made any other answer impossible.
Reading the enum off the type makes that unrepresentable."""


def _strict(properties: Mapping[str, Schema]) -> Schema:
    """One object node: every property required, nothing extra.

    A nullable property is still required - the key is always present and its
    value may be `null`, which is the only way the schema subset has of saying
    "optional" and the way the registry's fragments already say it.
    """
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _tagged(tag: str, properties: Mapping[str, Schema]) -> Schema:
    """A union branch, pinned to its tag by a one-value enum."""
    return _strict({"type": {"type": "string", "enum": [tag]}, **properties})


def _one_of(branches: Sequence[Schema]) -> Schema:
    """A union - or the branch itself, when a mode leaves only one."""
    return dict(branches[0]) if len(branches) == 1 else {"anyOf": list(branches)}


# --- The authoring envelopes -------------------------------------------------

_SEGMENT: Schema = _one_of(
    [
        _tagged("text", {"text": STRING}),
        _tagged("math", {"mathml": STRING}),
        _tagged("blank", {"blank_id": STRING}),
    ]
)
"""The explanation's flat segment array (ADR-0004, amended by ADR-0012).

Flat by construction - no segment holds another - so the schema needs no depth
limit, and a union rather than one node with three nullable fields so that a
`text` segment carrying a `mathml` is not something the model can emit."""

_DIRECT_ANSWER: Schema = _tagged(
    authoring.DIRECT_ANSWER,
    {"answer": STRING, "topic": STRING},
)
"""The override branch (ADR-0004): medical, legal, financial, security, an
active outage, or a learner who needs the answer now.

A first-class branch of the union rather than an absence, so the highest-stakes
question a learner asks cannot come back as a quiz-shaped response with every
quiz field null."""


def _skeleton_envelope(fragment: Mapping[str, object]) -> Schema:
    """The first authoring call: `quiz | direct_answer`, blanks from `fragment`.

    `recap` is nullable because it rides the *pedagogy* payload (ADR-0011) - a
    skeleton is not malformed for omitting one, and one that carries it anyway
    is kept, which is exactly what `authoring._parse_quiz` does.
    """
    return _one_of(
        [
            _tagged(
                authoring.QUIZ,
                {
                    "topic": STRING,
                    "explanation": {"type": "array", "items": _SEGMENT},
                    "blanks": {"type": "array", "items": dict(fragment)},
                    "recap": NULLABLE_STRING,
                },
            ),
            _DIRECT_ANSWER,
        ]
    )


def _pedagogy_envelope(fragment: Mapping[str, object]) -> Schema:
    """The second authoring call: the recap, and per-blank pedagogy.

    `recap` is required and not nullable here - this is the call that owes it,
    and `authoring._merge_pedagogy` refuses a payload without one.
    """
    return _strict(
        {
            "recap": STRING,
            "blanks": {"type": "array", "items": dict(fragment)},
        }
    )


# --- The three that do not vary by mode --------------------------------------

GRADE_ANSWER: Schema = _strict(
    {
        "verdict": VERDICT,
        "tutor_line": NULLABLE_STRING,
        "probe_question": NULLABLE_STRING,
        "hint": NULLABLE_STRING,
    }
)
"""One response, four things (ADR-0013 as extended by
[ADR-0016](../../../docs/adr/0016-advanced-hint-rides-the-grading-response.md)).

The reactive tutor line, the probe question and the hint ladder's rung text ride
the grading response as nullable fields rather than as calls of their own, and
`session._parse_grading` reads all four. The placeholder this replaces declared
neither rider, so `additionalProperties: false` forbade exactly the two fields
ADR-0013 requires - and nothing raised, because both parse as absent.

`hint` is the fourth, and the reason it is here rather than pre-authored on the
blank: an Advanced blank carries no `hints` (the registry forbids them), so
without this field a wrong Advanced answer had no rung text at all (#56). The
**client** still selects the rung and states it in the volatile tail; this field
is the text for that rung. Nullable because a correct answer has no rung."""

GRADE_PROBE: Schema = _strict({"verdict": VERDICT, "correction": STRING})
"""The verdict on a self-explanation, and the correction that goes with it.

Segment 1 for this call type asks the model to "return the verdict and a short
correction addressing the specific misunderstanding the reply revealed", and
[#10](https://github.com/derkmed/socratic/issues/10) parses `{verdict,
correction}`. The correction is not nullable: segment 1 asks for one on every
reply, and a schema stricter than its parser is safe where the reverse is the
drift this module exists to stop.

**This is the one shape here with no parser to check it against** - the probe
parser arrives with #10. `tests/test_output_schemas.py` fails the moment one
does, so the key names get re-verified rather than assumed."""

FOLD_NARRATIVE: Schema = _strict({profile_builder.NARRATIVE_FIELD: STRING})
"""The prose half of the profile, under the key `profile_builder` reads."""


# --- The composition point ---------------------------------------------------


def for_mode(
    mode: registry_module.ModeKey | None = None,
    *,
    registry: registry_module.ModeRegistry | None = None,
) -> dict[CallType, Schema]:
    """The output schema for each of the five call types.

    Args:
      mode: The difficulty mode being authored for, which fixes the per-blank
        shape both authoring calls ask for. `None` means "any mode the registry
        holds", and the two authoring schemas admit every registered mode's
        blank - the honest shape for a caller that has not yet said which mode
        it is authoring, and what the adapter defaults to.
      registry: Where the per-mode, per-stage fragments come from. Defaults to
        `default_registry()`.

    Returns:
      A fresh dict, one schema per `CallType`. Fresh because the caller hands it
      to the adapter, which copies it into a request body - nothing here is
      shared mutable state.

    Raises:
      KeyError: If no policy is registered for `mode`.
    """
    registry = registry or registry_module.default_registry()
    modes = registry.modes() if mode is None else (mode,)
    policies = [registry.policy_for(key) for key in modes]

    def fragments(stage: registry_module.AuthoringStage) -> list[Schema]:
        return [
            dict(policy.rules_for(stage).schema_fragment)
            for policy in policies
        ]

    return {
        CallType.AUTHOR_SKELETON: _skeleton_envelope(
            _one_of(fragments(registry_module.AuthoringStage.SKELETON))
        ),
        CallType.AUTHOR_PEDAGOGY: _pedagogy_envelope(
            _one_of(fragments(registry_module.AuthoringStage.PEDAGOGY))
        ),
        CallType.GRADE_ANSWER: dict(GRADE_ANSWER),
        CallType.GRADE_PROBE: dict(GRADE_PROBE),
        CallType.FOLD_NARRATIVE: dict(FOLD_NARRATIVE),
    }
MODE_BOUND_CALLS: frozenset[CallType] = frozenset(
    {CallType.AUTHOR_SKELETON, CallType.AUTHOR_PEDAGOGY}
)
"""The call types whose schema depends on the mode being authored for.

The other three are composed here precisely because they do not vary
(`GRADE_ANSWER`, `GRADE_PROBE`, `FOLD_NARRATIVE` above), so they are not made
to carry a mode they would ignore."""


def for_segments(
    segments: prompting.PromptSegments,
    *,
    registry: registry_module.ModeRegistry | None = None,
) -> Schema:
    """The schema one assembled call should be sent with.

    The schema is the contract. Segment 1 tells the model it is producing
    "structured output against a schema supplied with the request", and no
    prompt anywhere names the difficulty mode in prose - so for the two
    authoring calls, the schema is the *only* thing that makes an Advanced call
    produce Advanced blanks. Sent `for_mode()`'s mode-agnostic union instead,
    the model may satisfy it from either branch, and the mode's own validator
    then refuses everything the other branch admits. That was #114: every
    Advanced quiz came back novice-shaped and 422'd.

    Args:
      segments: The assembled call, which carries both its `call_type` and -
        for the two that vary - the `mode` it is for.
      registry: Where the per-mode fragments come from. Defaults to
        `default_registry()`.

    Returns:
      One schema, fresh, for `segments.call_type`.

    Raises:
      ValueError: If an authoring call carries no mode. Refused rather than
        defaulted, because the default that would apply is exactly the union
        this function exists to stop being sent.
      KeyError: If no policy is registered for the mode.
    """
    call_type = segments.call_type
    if call_type not in MODE_BOUND_CALLS:
        return for_mode(registry=registry)[call_type]

    if segments.mode is None:
        raise ValueError(
            f"{call_type.value} carries no mode, so there is no blank shape "
            "to ask the model for"
        )
    return for_mode(segments.mode, registry=registry)[call_type]
