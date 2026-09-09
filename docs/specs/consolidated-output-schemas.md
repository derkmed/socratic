# Consolidated model output schemas

## Goal

Every call type's `output_config.format` schema is **derived, in one place, from
the thing that parses the payload**. Today `DEFAULT_OUTPUT_SCHEMAS` in the
Anthropic adapter still holds #4's placeholder shapes and all four disagree with
what the domain reads back (#66), while the registry's real fragments —
`ModePolicy.authoring_schema_fragment` and `StageRules.schema_fragment` — are
declared, tested, and read by nothing outside `registry.py`. Nothing fails
because every test runs against `RecordingModelClient`, which never looks at a
schema.

Four hand-patched dicts would fix today and reproduce the bug in a year. The
deliverable is therefore **one composition point** — `domain/output_schemas.py`
— plus a test that mints a synthetic payload *from* each composed schema and
runs it through the **real parser**. A schema and its parser that disagree fail
that test; there is no table of shapes that merely happens to match.

## Seams

Existing, preferred:

1. **`ModePolicy.rules_for(stage).schema_fragment`** — the per-mode, per-stage
   blank shape. `StageRules` exists so a stage's schema and its validator cannot
   drift; this spec is what finally reads the schema half. Reached through the
   registry, never by branching on a mode.
2. **`AnthropicModelClient(schemas=...)` / `build_request(schema=...)`** — the
   injection point the adapter already has. Untouched; only what fills it in by
   default changes.
3. **The parsers themselves**, unchanged and unmoved:
   `authoring._parse_quiz` / `_parse_direct_answer` / `_merge_pedagogy`,
   `session._parse_grading`, `profile_builder._parse_narrative`.

New, one, justified:

4. **`socratic.domain.output_schemas.for_mode(mode=None, registry=None)`** — the
   single composition point, returning a schema per `CallType`. New because
   there is nowhere existing that may depend on both the registry and the
   parsers' payload shapes *and* be reachable from the adapter: the domain may
   not import the adapter, and the adapter may not be the home of a rule the
   no-SDK suite has to be able to check.

## Decisions

- **The composition point lives in the domain, not the adapter.** The dependency
  runs adapter → domain (`tests/test_import_hygiene.py`), so a domain module is
  reachable from the adapter and the reverse is not. It also means the
  schema/parser agreement test runs in the suite that has no `anthropic`
  installed — the half of the suite that matters most here, since the schemas
  describe what the *domain* parses and the adapter is only the transport.
- **The two authoring schemas come from the registry.** `rules_for(SKELETON)`
  and `rules_for(PEDAGOGY)` supply `blanks.items`; the composition point wraps
  them in the per-quiz envelope. Per mode *via the registry* — never
  `if mode ==` (CONTEXT: ModeRegistry, `tests/test_registry.py`).
- **`GRADE_ANSWER`, `GRADE_PROBE` and `FOLD_NARRATIVE` are composed in the same
  module, not added to the registry.** They do not vary by mode, and the
  registry's job is mode variance; putting a mode-invariant shape behind
  `policy_for(mode)` would say it varies. What keeps them honest is not their
  address but the round-trip test.
- **Strictness is structural** — `additionalProperties: false` plus a complete
  `required` list, recursively, at every object node. `strict: true` is not a
  field on `output_config.format`; that discrepancy with ADR-0014's wording is
  tracked in #41 and is not re-filed here.
- **Nullable is `"type": ["string", "null"]` with the key still in `required`**,
  the idiom the registry fragments already use.
- **A genuine tagged union is `anyOf` of strict branches**, not one object with
  everything nullable. ADR-0004 puts a union at the top level
  (`quiz | direct_answer`) and makes the explanation a segment array
  (`text | math | blank`); `anyOf` is in the documented structured-output schema
  subset, and it is what lets the model be *unable* to emit a quiz with no
  explanation. It is also what makes a minted payload exact, which is what the
  agreement test is built on.
- **`GRADE_ANSWER` carries verdict + nullable `tutor_line` + nullable
  `probe_question`** — ADR-0013's one response, three things, and exactly what
  `session._parse_grading` reads.
- **Verdict enums come from `records.Verdict`**, not from a literal list. The
  sharpest bug in #66 — `GRADE_PROBE` asking for `sound`/`unsound` when
  `Verdict("sound")` raises — becomes structurally unrepresentable.
- **Parser behaviour does not change.** Where a parser and the intended schema
  disagreed, the schema was wrong; the parsers were each written by the ticket
  that owns their payload.

## Approach

1. `src/socratic/domain/output_schemas.py`.
   - `_strict(**properties)` — an object node with a complete `required` list
     and `additionalProperties: false`.
   - `_SEGMENT` — `anyOf` of the three explanation segment shapes.
   - `_skeleton_envelope(fragment)` — `anyOf[quiz, direct_answer]`, the quiz
     branch carrying `topic`, `explanation`, `blanks` (items = `fragment`),
     nullable `recap` (it rides the pedagogy payload, ADR-0011) and
     `queued_topics`.
   - `_pedagogy_envelope(fragment)` — `recap` plus `blanks` (items =
     `fragment`).
   - `GRADE_ANSWER`, `GRADE_PROBE`, `FOLD_NARRATIVE` constants.
   - `for_mode(mode=None, *, registry=None)` — the five schemas. `mode=None`
     means "any registered mode": the two authoring `blanks.items` become
     `anyOf` over every mode the registry holds, which is the honest shape for a
     caller that has not yet said which mode it is authoring for.
2. `anthropic_client.DEFAULT_OUTPUT_SCHEMAS = dict(output_schemas.for_mode())`,
   with the "provisional shapes" docstring replaced by what they now are.
   `build_request` and the constructor are untouched.
3. `tests/test_output_schemas.py` — a JSON-Schema payload minter, then, per call
   type with a parser: mint every variant, parse it with the real parser, and
   assert every minted string reaches the parsed object.
4. `tests/test_anthropic_adapter.py` — the two structured-output tests
   generalised from "the top node is a strict object" to "every object node in
   the schema is strict", which is what the SDK actually requires.

## Out of scope

- **Wiring `for_mode(mode)` into `QuizAuthoring`.** Nothing in the repo
  constructs `AnthropicModelClient` outside the tests and the unrun cache gate;
  the Pipe that will is not built. The mode-agnostic default is correct for
  every registered mode, and narrowing it is one `schemas=` argument at the
  construction site when there is one.
- **Changing any parser.** Including `session._parse_grading`'s treatment of an
  absent key as a null.
- **`minItems` / `maxItems` from `blank_range`.** Numeric and array-count
  constraints are outside the structured-output schema subset, so the count
  stays the validator's job (ADR-0004's stated consequence). Segment 1 says the
  admissible blank count "is fixed by the schema fragment supplied with the
  request", which is not achievable; `prompting.py` is out of this lane and the
  mismatch is filed instead.
- **`GRADE_PROBE`'s parser.** It arrives with #10/PR #65. This spec encodes the
  shape segment 1 already asks for and leaves a test that fails the moment a
  probe parser exists, so the reconciliation cannot be missed.
- **Running the live cache gate.** No credits (#7). The gate now sends real
  schemas rather than placeholders; what it asserts is unchanged.

## Acceptance

1. `output_schemas.for_mode(mode)["author_skeleton"]` embeds
   `registry.policy_for(mode).rules_for(SKELETON).schema_fragment`, and likewise
   for `PEDAGOGY` — asserted by equality, so a registry change flows through.
2. For every call type with a parser, a payload minted from its composed schema
   parses without raising.
3. For every such payload, every string the schema caused to be minted is
   present in the parsed domain object — so a property the schema declares and
   the parser drops, or a field the parser reads and the schema omits, fails.
4. `GRADE_ANSWER`'s schema requires `verdict`, `tutor_line` and
   `probe_question`, the last two nullable (ADR-0013).
5. Every verdict enum in every composed schema equals the values of
   `records.Verdict`.
6. Every object node in every composed schema has `additionalProperties: false`
   and a `required` list equal to its properties.
7. The full suite passes with and without the `anthropic` extra; the adapter
   tests still skip cleanly when it is absent.
