# ADR-0004 — Quiz wire format

Status: accepted
Date: 2026-09-08

## Decision

Schema-constrained structured output (`output_config.format` + `messages.parse()`),
never regex over markdown.

**Top level is a union.** `direct_answer` must be representable, or the supplied
prompt's override (medical, legal, financial, security, active outage, or "I need
this now") either gets violated or produces an unparseable response — on the
highest-stakes question a learner will ever ask.

**The explanation is a segment array, not a string with sentinels.**
`[{type:"text",text}, {type:"blank",blank_id}, ...]`. Rendering walks the list;
filling a blank swaps one element. Sentinel strings reintroduce the parsing
ADR-0001 removed, and character offsets are worse — models count characters badly.

**One `Blank` type with a `mode` enum and nullable mode-specific fields.** Chosen
over mode-split schemas so that further difficulty modes can be added without
forking the type, the store, or the renderer.

**Both Novice and Advanced ship in the prototype.** Advanced (pure recall, no
option bank) is where real domain expertise is built, and shipping it proves the
ADR-0001 cache-anchored grading path is real rather than theoretical.

**Pre-authored fields** (per ADR-0003): Novice blanks carry `options`,
`correct_option_id`, `reinforcement`, and three `hints`; Advanced blanks carry a
`rubric`. The completion `recap` is authored up front too — free, since it rides
the authoring call.

## Consequences

Good:
- No parsing layer; the renderer consumes typed data.
- New difficulty modes are an enum value plus fields, not a new schema.
- The override path is a first-class branch instead of an exception.

Costs / risks:
- Nullable fields mean `strict: true` still holds *structurally* (all keys present,
  `"type": ["array","null"]`) but cannot enforce **conditional** invariants. The
  domain package must therefore validate explicitly:
  `novice ⇒ len(options) ≥ 2 and correct_option_id ∈ options`;
  `advanced ⇒ rubric present`. This validator is load-bearing — without it a
  malformed authoring response reaches the renderer.
- Both modes means two grading paths and two input UIs in the prototype, and a
  later first demo than Novice-only would have given.
