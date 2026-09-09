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
        payload, quiz_session_id="quiz-session", mode=mode
    )
    return payload, parsed


def parse_pedagogy(payload, mode):
    """Merge into a skeleton built from the payload's own blank ids.

    The merge target is not minted: `_merge_pedagogy` refuses a payload naming a
    blank the quiz does not declare, which is a fact about two calls agreeing
    with each other rather than about the schema.
    """
    ids = [entry["blank_id"] for entry in payload["blanks"]]
    quiz = Quiz(
        quiz_session_id="quiz-session",
        mode=mode,
        topic="topic",
        explanation=tuple(BlankSegment(blank_id) for blank_id in ids),
        blanks=tuple(Blank(blank_id=blank_id, mode=mode) for blank_id in ids),
        recap="",
    )
    return payload, authoring._merge_pedagogy(quiz, payload)


def parse_grade_answer(payload, mode):
    return payload, session._parse_grading(json.dumps(payload))


def parse_narrative(payload, mode):
    return payload, profile_builder._parse_narrative(json.dumps(payload))


PARSERS = {
    CallType.AUTHOR_SKELETON: parse_skeleton,
    CallType.AUTHOR_PEDAGOGY: parse_pedagogy,
    CallType.GRADE_ANSWER: parse_grade_answer,
    CallType.FOLD_NARRATIVE: parse_narrative,
}
"""Call type to the real parser of its payload.

`GRADE_PROBE` is absent because no probe parser exists on `main` yet — it
arrives with #10 (PR #65). `test_grade_probe_has_no_parser_to_check_it_against`
fails the moment one does, which is the reconciliation this table needs.
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
    """Every object node in a schema, `anyOf` branches included."""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            yield from object_nodes(branch)
        return
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds or ())
    if "object" in kinds:
        yield schema
        for sub in schema["properties"].values():
            yield from object_nodes(sub)
    if "array" in kinds:
        yield from object_nodes(schema["items"])


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

    def test_grade_probe_has_no_parser_to_check_it_against(self):
        assert CallType.GRADE_PROBE not in PARSERS, (
            "a probe parser now exists (#10 / PR #65): add it to PARSERS so the "
            "round trip covers GRADE_PROBE, and re-verify the key names in "
            "output_schemas.GRADE_PROBE against it. This test is the "
            "reconciliation tripwire #66 left behind on purpose."
        )
        assert not hasattr(session, "_parse_probe_grading"), (
            "session._parse_probe_grading has landed; see the message above"
        )
