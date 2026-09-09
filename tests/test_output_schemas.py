"""The composed output schemas, checked against the parsers that read them.

`docs/specs/consolidated-output-schemas.md`. The point of this file is the
**round trip**: a payload is minted *from* a call type's composed schema and
handed to the real parser. Nothing here restates a shape, so a schema and its
parser that disagree fail — which is what #66 was: four hand-written shapes
that had quietly stopped matching four parsers nobody had changed.

Two directions are covered, and they catch different bugs:

* **Mint and parse.** A schema that asks the model for something the parser
  refuses fails here. `GRADE_PROBE` asking for `sound` when `Verdict("sound")`
  raises was this, and it would have raised on every probe reply against the
  live API.
* **Every minted sentinel survives the parse.** A schema that declares a
  property the parser drops, or omits one the parser reads, fails here.
  `GRADE_ANSWER` omitting `tutor_line` and `probe_question` was this, and it
  raised nothing at all — `additionalProperties: false` simply forbade the two
  fields ADR-0013 requires.

The minter is a small JSON-Schema interpreter over the subset the schemas use.
It cannot mint *referential* integrity — a blank segment pointing at a declared
blank — because no JSON schema can say that; `_coherent` supplies exactly that
one fixup, and nothing else.

No `anthropic` import: this file is the reason the composition point lives in
the domain, and it runs in the suite that has no SDK installed.
"""

import itertools
import json
import re

import pytest

from socratic.domain import authoring
from socratic.domain import output_schemas
from socratic.domain import profile_builder
from socratic.domain import prompting
from socratic.domain import registry as registry_module
from socratic.domain import session
from socratic.domain.ids import new_quiz_session_id
from socratic.domain.modes import ProbeCadence
from socratic.domain.records import Verdict
from socratic.domain.types import Blank, BlankSegment, Quiz

CallType = prompting.CallType
AuthoringStage = registry_module.AuthoringStage

MODES = tuple(registry_module.default_registry().modes())

SENTINEL = re.compile(r"^value-\d+$")


# --- Minting a payload from a schema -----------------------------------------


def _counter():
    """Fresh, unique sentinel strings, so coverage is checkable by identity."""
    count = itertools.count()
    return lambda: f"value-{next(count)}"


def _variants(schema):
    """Every payload shape the schema admits, as builders taking a counter.

    One variant per `anyOf` branch, multiplied out through objects and arrays —
    a tagged union is minted once per tag rather than collapsed to its first
    branch, so a branch whose parser disagrees cannot hide behind a sibling.
    """
    if "anyOf" in schema:
        return [
            build for branch in schema["anyOf"] for build in _variants(branch)
        ]
    if "const" in schema:
        return [lambda mint, value=schema["const"]: value]
    if "enum" in schema:
        return [lambda mint, value=schema["enum"][0]: value]

    kinds = schema["type"]
    if isinstance(kinds, str):
        kinds = [kinds]
    kinds = [kind for kind in kinds if kind != "null"]
    kind = kinds[0]

    if kind == "object":
        names = list(schema["properties"])
        per_name = [_variants(schema["properties"][name]) for name in names]
        return [
            lambda mint, names=names, combo=combo: {
                name: build(mint) for name, build in zip(names, combo)
            }
            for combo in itertools.product(*per_name)
        ]
    if kind == "array":
        return [
            lambda mint, build=build: [build(mint)]
            for build in _variants(schema["items"])
        ]
    if kind == "string":
        return [lambda mint: mint()]
    raise AssertionError(f"the minter has no case for {kind!r}")


def mint(schema):
    """Every synthetic payload for `schema`, each with its own sentinels."""
    return [build(_counter()) for build in _variants(schema)]


def sentinels(payload):
    """Every minted string in a payload. Consts and enum tags are not minted."""
    if isinstance(payload, str):
        return {payload} if SENTINEL.match(payload) else set()
    if isinstance(payload, dict):
        return set().union(set(), *(sentinels(v) for v in payload.values()))
    if isinstance(payload, list):
        return set().union(set(), *(sentinels(v) for v in payload))
    return set()


# --- The parsers, and the one thing a schema cannot say ----------------------


def _coherent(payload):
    """Point the explanation at the blank the payload declares.

    "Every blank you declare must be referenced, and every reference must
    resolve to a declared blank" is segment 1's rule and `Quiz.__post_init__`'s;
    it is a cross-field invariant, so no JSON schema carries it (ADR-0004 says
    as much, which is why the conditional validator exists). This is the only
    thing the test supplies that the schema did not.
    """
    if payload.get("type") != authoring.QUIZ:
        return payload
    blank_id = payload["blanks"][0]["blank_id"]
    kept = [seg for seg in payload["explanation"] if seg["type"] != "blank"]
    payload = dict(payload)
    payload["explanation"] = kept + [{"type": "blank", "blank_id": blank_id}]
    return payload


def parse_skeleton(payload, mode):
    payload = _coherent(payload)
    if payload.get("type") == authoring.DIRECT_ANSWER:
        return payload, authoring._parse_direct_answer(payload)
    parsed = authoring._parse_quiz(
        payload, quiz_session_id=new_quiz_session_id(), mode=mode
    )
    return payload, parsed


def parse_pedagogy(payload, mode):
    """Merge into a skeleton built from the payload's own blank ids.

    The merge target is not a minted *payload*: `_merge_pedagogy` refuses a
    payload naming a blank the quiz does not declare, which is a fact about two
    calls agreeing with each other rather than about the schema. Its session id
    is minted, because `Quiz` insists on a ULID (ADR-0007).
    """
    ids = [entry["blank_id"] for entry in payload["blanks"]]
    quiz = Quiz(
        quiz_session_id=new_quiz_session_id(),
        mode=mode,
        topic="topic",
        explanation=tuple(BlankSegment(blank_id) for blank_id in ids),
        blanks=tuple(Blank(blank_id=blank_id, mode=mode) for blank_id in ids),
        recap="",
    )
    return payload, authoring._merge_pedagogy(quiz, payload)


def parse_grade_answer(payload, mode):
    return payload, session._parse_grading(json.dumps(payload))


def parse_grade_probe(payload, mode):
    return payload, session._parse_probe_grading(json.dumps(payload))


def parse_narrative(payload, mode):
    return payload, profile_builder._parse_narrative(json.dumps(payload))


PARSERS = {
    CallType.AUTHOR_SKELETON: parse_skeleton,
    CallType.AUTHOR_PEDAGOGY: parse_pedagogy,
    CallType.GRADE_ANSWER: parse_grade_answer,
    CallType.GRADE_PROBE: parse_grade_probe,
    CallType.FOLD_NARRATIVE: parse_narrative,
}
"""Call type to the real parser of its payload.

All five are covered. `GRADE_PROBE` joined when #10 (PR #65) landed
`session._parse_probe_grading`, which is what the tripwire this table used to
carry existed to catch.
"""


def cases():
    for mode in MODES:
        schemas = output_schemas.for_mode(mode)
        for call_type, parse in PARSERS.items():
            for index, payload in enumerate(mint(schemas[call_type])):
                yield pytest.param(
                    parse,
                    payload,
                    mode,
                    id=f"{mode}-{call_type.value}-{index}",
                )


# --- Every object node is strict ---------------------------------------------


def object_nodes(schema):
    """Every object node in a schema, `anyOf` branches included.

    Reads `properties` and `items` defensively. A node missing them is exactly
    the bug this walker exists to find (#73: a bare `{"type": "object"}`), and
    it is worth an assertion naming the node rather than a `KeyError` raised
    mid-walk.
    """
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            yield from object_nodes(branch)
        return
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds or ())
    if "object" in kinds:
        yield schema
        for sub in schema.get("properties", {}).values():
            yield from object_nodes(sub)
    if "array" in kinds:
        items = schema.get("items")
        if items is not None:
            yield from object_nodes(items)


def every_schema():
    for mode in (None,) + MODES:
        for call_type, schema in output_schemas.for_mode(mode).items():
            yield mode, call_type, schema


class TestTheCompositionPoint:
    def test_it_covers_every_call_type(self):
        for mode in (None,) + MODES:
            assert set(output_schemas.for_mode(mode)) == set(CallType)

    def test_the_authoring_schemas_are_the_registry_s_fragments(self):
        # The whole point of `StageRules`: the schema half and the validator
        # half travel together. This is what finally reads the schema half.
        for mode in MODES:
            policy = registry_module.default_registry().policy_for(mode)
            schemas = output_schemas.for_mode(mode)
            for call_type, stage in (
                (CallType.AUTHOR_SKELETON, AuthoringStage.SKELETON),
                (CallType.AUTHOR_PEDAGOGY, AuthoringStage.PEDAGOGY),
            ):
                fragment = policy.rules_for(stage).schema_fragment
                assert fragment in _blank_items(schemas[call_type]), (
                    f"{mode}/{call_type.value} does not carry the registry's "
                    f"{stage.value} fragment"
                )

    def test_every_object_node_is_strict(self):
        # The SDK spells "strict" structurally: `additionalProperties: false`
        # plus a complete `required` list, at every object node rather than
        # only the top one. See the PR's note on ADR-0014's `strict: true`
        # wording (#41).
        for mode, call_type, schema in every_schema():
            for node in object_nodes(schema):
                where = f"{mode}/{call_type.value}"
                assert node["additionalProperties"] is False, where
                assert sorted(node["required"]) == sorted(node["properties"]), (
                    where
                )

    def test_every_registry_fragment_is_strict(self):
        # The walk above follows what is *composed* today - `SKELETON` and
        # `PEDAGOGY`. A fragment the registry declares but nothing sends yet is
        # a trap that springs on whoever wires it (#73: Advanced's `COMPLETE`
        # fragment had a bare `{"type": "object"}` under `options`). Going
        # through `rules_for` rather than the module constants covers the
        # fallback by construction, and covers a mode that declares its own
        # rules for a stage just the same.
        registry = registry_module.default_registry()
        for mode in registry.modes():
            policy = registry.policy_for(mode)
            for stage in AuthoringStage:
                fragment = policy.rules_for(stage).schema_fragment
                for node in object_nodes(fragment):
                    where = f"{mode}/{stage.value}: {node}"
                    assert node.get("additionalProperties") is False, where
                    assert "properties" in node, where
                    assert sorted(node.get("required", ())) == sorted(
                        node["properties"]
                    ), where

    def test_every_verdict_enum_is_the_verdict_enum(self):
        # `GRADE_PROBE` asked for `sound`/`unsound`, which `Verdict` has never
        # had. Deriving the values makes that unrepresentable.
        known = sorted(member.value for member in Verdict)
        found = 0
        for _, _, schema in every_schema():
            for node in object_nodes(schema):
                verdict = node["properties"].get("verdict")
                if verdict is not None:
                    assert sorted(verdict["enum"]) == known
                    found += 1
        assert found > 0, "no schema declares a verdict at all"

    def test_the_five_call_types_have_five_different_schemas(self):
        rendered = {
            call_type: repr(schema)
            for call_type, schema in output_schemas.for_mode().items()
        }
        assert len(set(rendered.values())) == len(CallType)


def _blank_items(schema):
    """The `blanks` array's item schemas, across every branch of the envelope."""
    items = []
    for node in object_nodes(schema):
        blanks = node["properties"].get("blanks")
        if blanks is None:
            continue
        item = blanks["items"]
        items.extend(item.get("anyOf", [item]))
    return items


class TestTheSchemasAgreeWithTheParsers:
    @pytest.mark.parametrize("parse,payload,mode", list(cases()))
    def test_a_minted_payload_parses(self, parse, payload, mode):
        parse(payload, mode)

    @pytest.mark.parametrize("parse,payload,mode", list(cases()))
    def test_the_parse_keeps_every_field_the_schema_declares(
        self, parse, payload, mode
    ):
        used, parsed = parse(payload, mode)
        missing = sorted(
            value for value in sentinels(used) if repr(value) not in repr(parsed)
        )
        assert missing == [], (
            f"the schema declares fields the parser drops: {missing} is in the "
            f"minted payload and not in {parsed!r}"
        )


class TestGradeProbe:
    def test_its_schema_asks_for_the_verdict_and_a_correction(self):
        # Segment 1 for this call type says "Return the verdict and a short
        # correction addressing the specific misunderstanding the reply
        # revealed", and #10 (PR #65) parses `{verdict, correction}`. The
        # correction is not nullable: segment 1 asks for one every time, and a
        # schema stricter than its parser is safe where the reverse is the bug
        # this ticket is about.
        schema = output_schemas.for_mode()[CallType.GRADE_PROBE]
        assert sorted(schema["properties"]) == ["correction", "verdict"]
        assert schema["properties"]["correction"] == {"type": "string"}

    def test_the_probe_parser_is_covered_by_the_round_trip(self):
        # This replaces the tripwire #66 left for #10's parser. The keys were
        # re-verified against `session._parse_probe_grading` when PR #65
        # landed: it reads `verdict` and `correction`, which is what
        # `output_schemas.GRADE_PROBE` declares.
        assert CallType.GRADE_PROBE in PARSERS

    def test_the_schema_is_stricter_than_the_parser_not_looser(self):
        # `_parse_probe_grading` tolerates a null correction; the schema does
        # not offer one, because segment 1 asks for a correction on every
        # reply. Stricter-than-the-parser is the safe direction — the reverse
        # is the drift this module exists to stop.
        assert (
            output_schemas.GRADE_PROBE["properties"]["correction"]
            == output_schemas.STRING
        )
        assert session._parse_probe_grading(
            json.dumps({"verdict": "incorrect", "correction": None})
        ) == (Verdict.INCORRECT, None)


def _a_quiz(mode="novice"):
    """One minimal quiz, for the call types that render an existing one."""
    return Quiz(
        quiz_session_id=new_quiz_session_id(),
        mode=mode,
        topic="topic",
        explanation=(BlankSegment("b1"),),
        blanks=(Blank(blank_id="b1", mode=mode),),
        recap="",
    )


class TestTheSchemaFollowsTheModeTheCallIsFor:
    """#114. The schema *is* the contract.

    Segment 1 tells the model "structured output against a schema supplied with
    the request", and nothing anywhere names the difficulty mode in prose — so
    the only thing that makes an Advanced call produce Advanced blanks is the
    schema it is sent with. Sent the mode-agnostic union instead, the model may
    pick either branch, and Advanced validation refuses everything the Novice
    branch admits.

    `for_segments` is the selection, and it lives here rather than in the
    adapter for the reason the module docstring gives: a schema is a statement
    about what the domain parses, and this half of the suite runs without the
    SDK installed.
    """

    def _authoring(self, call_type, mode):
        return prompting.assemble(
            call_type,
            profile=prompting.LearnerProfile(learner_id="L"),
            probe_cadence=ProbeCadence.SOMETIMES,
            inquiry="How does an LRU cache work?",
            quiz=_a_quiz() if call_type is prompting.CallType.AUTHOR_PEDAGOGY else None,
            blank_range=registry_module.BlankRange(4, 6),
            mode=mode,
        )

    def test_an_advanced_skeleton_asks_for_a_rubric_and_forbids_options(self):
        schema = output_schemas.for_segments(
            self._authoring(prompting.CallType.AUTHOR_SKELETON, "advanced")
        )

        blanks = _blank_items(schema)
        assert blanks, "the envelope declares no blanks array"
        for blank in blanks:
            assert set(blank["required"]) == {"blank_id", "rubric"}
            assert "options" not in blank["properties"]
            assert "correct_option_id" not in blank["properties"]

    def test_a_novice_skeleton_asks_for_an_option_bank(self):
        schema = output_schemas.for_segments(
            self._authoring(prompting.CallType.AUTHOR_SKELETON, "novice")
        )

        blanks = _blank_items(schema)
        assert blanks
        for blank in blanks:
            assert "options" in blank["properties"]
            assert "correct_option_id" in blank["properties"]

    def test_neither_mode_is_sent_the_other_modes_shape(self):
        """The union is what #114 was: both branches offered, either accepted."""
        novice = _blank_items(
            output_schemas.for_segments(
                self._authoring(prompting.CallType.AUTHOR_SKELETON, "novice")
            )
        )
        advanced = _blank_items(
            output_schemas.for_segments(
                self._authoring(prompting.CallType.AUTHOR_SKELETON, "advanced")
            )
        )

        assert novice != advanced
        assert len(novice) == 1, "a mode-bound call offers exactly one blank shape"
        assert len(advanced) == 1

    def test_an_authoring_call_with_no_mode_is_refused(self):
        """Silence here is the bug returning: an authoring call that forgot its
        mode would fall back to the union and be judged by one mode's rules."""
        segments = self._authoring(prompting.CallType.AUTHOR_SKELETON, None)

        with pytest.raises(ValueError, match="mode"):
            output_schemas.for_segments(segments)

    def test_a_mode_invariant_call_needs_no_mode(self):
        """`grade_answer`, `grade_probe` and `fold_narrative` do not vary by
        mode, so they are not made to carry one."""
        segments = prompting.assemble(
            prompting.CallType.GRADE_ANSWER,
            profile=prompting.LearnerProfile(learner_id="L"),
            probe_cadence=ProbeCadence.SOMETIMES,
            quiz=_a_quiz(),
            current_blank_id="b1",
            current_guess="entropy",
        )

        assert output_schemas.for_segments(segments) == output_schemas.GRADE_ANSWER
