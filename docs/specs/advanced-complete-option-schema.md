# The Advanced COMPLETE-stage fragment's option object

Closes #73.

## The complaint

`_ADVANCED_SCHEMA_FRAGMENT` in `src/socratic/domain/registry.py` declares

```python
"options": {"type": ["array", "null"], "items": {"type": "object"}},
```

The `items` node is a bare `{"type": "object"}`: no `properties`, no `required`,
no `additionalProperties`. Structured outputs require every object node to be
strict, so that node would be rejected by the API the moment the fragment
reached a request.

It does not reach one today. `ModePolicy.rules_for` falls back to
`authoring_schema_fragment` for any stage a policy does not declare, and neither
shipped policy declares `AuthoringStage.COMPLETE` — so `_ADVANCED_SCHEMA_FRAGMENT`
*is* the Advanced COMPLETE-stage rules, and `output_schemas.for_mode` composes
`SKELETON` and `PEDAGOGY` only. That is what makes this worth fixing now rather
than later: it is a trap that springs on whoever first wires the complete stage,
at which point it looks like their bug.

The Novice sibling spells the option object out in full (`option_id`, `text`,
both required, `additionalProperties: False`), so the two fragments disagree
about the same thing.

## What guards it today, and why neither catches it

* `tests/test_registry.py::test_the_authoring_schema_fragment_is_a_json_schema_object`
  checks only that the fragment's top node is an object schema.
* `tests/test_output_schemas.py::TestTheCompositionPoint::test_every_object_node_is_strict`
  walks every object node of every **composed** schema — `SKELETON` and
  `PEDAGOGY`. The COMPLETE fragment is not composed into anything, so the walk
  never reaches it.

The gap is not the strictness rule; it is the rule's *reach*. It follows what is
currently sent rather than what the registry declares.

## Seams

1. **`tests/test_output_schemas.py` — the strictness walk's reach.** Extend it
   from the composed schemas to every fragment the registry declares:
   every mode in `default_registry().modes()` × every member of
   `AuthoringStage`, taken through `ModePolicy.rules_for(stage).schema_fragment`.
   Going through `rules_for` rather than the module-level constants is what makes
   the COMPLETE fallback covered by construction — a future policy that declares
   its own COMPLETE rules is checked too, and so is a third mode registered with
   the six fields alone.

   `object_nodes`, the existing walker, indexes `schema["properties"]` and
   `schema["items"]` directly, so a bare `{"type": "object"}` raises `KeyError`
   before the assertion can name it. It becomes `.get`-based, and the strictness
   assertion reads the three keys defensively, so the failure says *which*
   fragment and *which* node — which is the whole value of the guard.

2. **`_ADVANCED_SCHEMA_FRAGMENT` in `src/socratic/domain/registry.py`.** Its
   `options.items` becomes the same fully-specified option object Novice uses.
   The option-object literal currently appears twice (in
   `_NOVICE_SCHEMA_FRAGMENT` and in `_OPTIONS`); it is lifted to one
   `_OPTION_ITEM` constant that all three sites reference, because a third copy
   is exactly how these two fragments came to disagree.

   `options` stays `{"type": ["array", "null"]}`. Advanced authors no options —
   `_validate_advanced_blank` never requires them — so nullable is the honest
   shape; only the item node changes.

## Acceptance

* The strictness walk covers the Advanced COMPLETE fragment, and fails on the
  bare `{"type": "object"}` node before the fix (naming that node), passes after.
* `registry.default_registry().policy_for(ADVANCED).rules_for(COMPLETE).schema_fragment`
  has an `options.items` object with `option_id` and `text`, both required, and
  `additionalProperties: False` — the same item schema Novice's fragment carries.
* Full suite green.

## Not included

* **Composing a COMPLETE-stage output schema.** No call type asks for
  `COMPLETE`; `output_schemas.for_mode` is untouched. Wiring the complete stage
  is the work this fix exists to keep clear of a trap, not part of it.
* **`minItems` / `maxItems` from `blank_range`** on the options or blanks arrays
  — outside the structured-output subset, already tracked separately.
* **Any validator change.** `_validate_advanced_blank` is correct as it stands;
  the schema half was the half that was wrong.
