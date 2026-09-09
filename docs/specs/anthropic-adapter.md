# The Anthropic adapter and the per-call-type cache gate

Ticket: [#7](https://github.com/derkmed/socratic/issues/7). The adapter half of
slice 5 of [the master spec](socratic-learning-app.md) — the port half landed in
[#4](prompt-assembler-and-model-client.md). Sources:
[CONTEXT](../CONTEXT.md) (**Model**, **Call type**, **Segment 1**, **Portability
seam**), [ADR-0014](../adr/0014-model-choice-and-effort.md),
[ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md). Nothing here is
a new decision.

## Goal

The one module in the codebase that imports the Anthropic SDK: it turns a
`PromptSegments` — the laid-out value `assemble` deliberately stops at — into a
wire request, sends it, and maps the response back to a `ModelResponse` carrying
the `message_id` and the per-call cache usage the attempt persists.

And the **gate**: `cache_read_input_tokens > 0` asserted independently for each
of the five call types, with each segment 1 clearing the 512-token floor on its
own.

## Seams

| Seam | Kind | Tests attach at |
|---|---|---|
| `build_request(segments, valves, schema) -> dict` | pure function | Its return value. Every wire invariant — model id, `cache_control` placement, `effort`, `strict` structured output, thinking not disabled — is a property of a dict, assertable with no client and no network. |
| `AnthropicModelClient(client, ...)` | adapter | A **fake SDK client** injected at construction: an object with `.messages.create(**kwargs)` that records the request and returns a canned response. Proves the five methods dispatch to the right call type and that the response maps back to `message_id` + usage. |
| The live cache gate | integration, credential-gated | Two real calls per call type against the real API. `skipif` on `ANTHROPIC_API_KEY`. |

The first two seams are why the adapter is testable at all without spending
money. The third cannot be, and the spec says so rather than pretending.

## Decisions

| # | Decision | Source |
|---|---|---|
| D14 | `claude-opus-5` from an **admin-level** `Valve`, never `UserValves` — caches are model-scoped. | [ADR-0014](../adr/0014-model-choice-and-effort.md) |
| D14 | `effort` is `low`, a **per-call-type** knob set uniformly, constant for a given call type because changing it invalidates that prefix. | ADR-0014 |
| D14 | **Thinking is never disabled.** Adaptive thinking, effort as the lever. | ADR-0014 |
| D14 | Structured output constrained by a JSON schema per call type; `cache_control` on segments 1 and 2. | ADR-0014, master spec §5 |
| D2 | The adapter lives **outside** `socratic.domain`, in `socratic.adapters`, and is the only module permitted to import the SDK. The SDK stays an optional extra so the domain installs and tests without it. | [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md), CONTEXT: Portability seam |

## Approach

### 1. `src/socratic/adapters/anthropic_client.py`

`Valves` — the admin-level settings object. `model` defaults to
`claude-opus-5`; `effort` is a mapping from `CallType` to effort level, every
entry `low`. Frozen, and holding nothing learner-scoped: there is no
`UserValves` here and no per-call model or effort argument anywhere on the
adapter's surface, which is how "not settable per learner" is asserted.

`build_request(segments, valves, schema) -> dict` — the pure translation:

```
system   = [ segment 1 ]                         <- cache_control if marked
messages = [ user: [ segment 2, volatile tail ] ] <- cache_control if marked
output_config = { effort, format: json_schema }
thinking = { type: "adaptive" }
```

Segment 1 goes in `system` and the rest in the single user turn because the API
renders `tools -> system -> messages`, so that ordering is exactly ADR-0006's
prefix order. The `cache_control` marker is read off the `PromptSegment`, never
hardcoded — the assembler stays authoritative about where the breakpoints are.

`AnthropicModelClient` — implements the `ModelClient` Protocol. Takes the SDK
client by injection so the fake can stand in. Each of the five methods rejects
`PromptSegments` assembled for a different call type, the same wiring check
`RecordingModelClient` makes.

### 2. Import hygiene, narrowed to say what it meant

`tests/test_import_hygiene.py` scanned all of `src/socratic/` for third-party
imports, which forbade the module this ticket exists to create. Two surgical
changes:

- the stdlib-only scan is scoped to `src/socratic/domain/` — the domain is what
  the rule was ever about;
- `test_no_module_imports_the_anthropic_sdk` becomes
  `test_the_anthropic_sdk_is_imported_only_by_the_adapter`, naming the one
  module allowed to. That is stronger than the blanket ban: a blanket ban goes
  away the moment an adapter exists, an exclusive-import assertion does not.

The Open WebUI check keeps scanning the whole package, unchanged.

### 3. The gate, written and skipped

`tests/test_anthropic_cache_gate.py`. Every test in it is
`skipif`-ed on `ANTHROPIC_API_KEY`, with a skip reason that says these are live,
paid calls. Parametrized over the five call types: assemble, call twice, assert
`cache_read_input_tokens > 0` on the second. A second test records each segment
1's measured token count against the 512 floor via `count_tokens`.

An **offline proxy** for the floor lives with the unit tests: each segment 1 is
at least `512 x 4` characters, under an assumed ceiling of 4 characters per
token. It is a lower-bound sanity check, clearly labelled, and it is **not** the
measurement acceptance criterion 2 asks for.

## Acceptance

Discharged by this slice:

- The model id comes from the admin `Valve`, defaults to `claude-opus-5`, and
  cannot be set per learner.
- `effort` is `low`, per call type, and constant across requests for a call type.
- Thinking is never disabled.
- Requests set schema-constrained structured output and `cache_control` on
  segments 1 and 2.
- `message_id` and per-call usage are returned for persistence.
- The Anthropic SDK is imported from this adapter module and nowhere else.

**Not discharged, and cannot be here:**

- `cache_read_input_tokens > 0` per call type (master spec acceptance 12).
- The measured token length of the five segment 1s against the 512 floor.

Both need live, paid API calls. The tests exist and are skipped for want of a
credential; running them is a spending decision that belongs to the user.

## Out of scope

- The five output schemas' real shapes. The adapter ships provisional schemas
  keyed by call type and takes an override at construction; the authoring and
  grading tickets own the content.
- Retry, timeout and error-mapping policy beyond the SDK's defaults.
- Streaming — nothing in this system streams (ADR-0013).
- The Open WebUI `Valves` class itself, which lives in the Pipe (#12) and cannot
  be imported below the seam.
