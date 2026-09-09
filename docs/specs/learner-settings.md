# Learner settings — the mode toggle and the probe cadence

## Goal

Two per-learner settings, held in Open WebUI's `UserValves` and carried down to
the quiz service as one named value: the **mode toggle** (CONTEXT) and
**`probe_cadence`** (CONTEXT). Issue
[#14](https://github.com/derkmed/socratic/issues/14); D10,
[ADR-0010](../adr/0010-per-learner-instruction-toggles.md); master spec
acceptance 11, 20, 21, 40.

The invariant the whole ticket exists to protect: **toggles switch client
behaviour, never prompt text.** Segment 1 stays byte-identical however many
settings the product grows — and that includes *omitting* instructions for a
learner, which splits the cache exactly as badly as adding them.

Almost all the *behaviour* is already built. `ProbeCadence` exists, `assemble`
takes a cadence it deliberately never reads, `QuizSession.submit` takes a live
`probe_cadence` overriding the attempt's stamp, `QuizAuthoring.author` takes a
`mode` and stamps `probe_cadence_at_authoring`. What does not exist is the
**settings value itself** and the path by which a learner's current settings
reach a running attempt: the service drops the cadence on `/answers`, so the
domain's mid-quiz override is unreachable through the HTTP surface, and
acceptance 20 is untestable above the domain.

This ticket builds that path, and pins the cache invariant against the settings
value rather than against a loose parameter.

## Seams

| Seam | Kind | Why it must exist |
|---|---|---|
| `LearnerSettings(learner_id, mode, probe_cadence)` | **new**, `socratic.domain.settings` | The domain-side shape of `UserValves`. The four cadence values and the `sometimes` default have to be written down exactly once, and the byte-identity assertion needs *one object* to vary — asserting invariance against two loose parameters proves less than asserting it against the settings value the product will keep growing. |
| `LearnerSettings.parse(learner_id, *, mode=None, probe_cadence=None)` | **new**, same module | The untrusted-string boundary. A cadence outside the four documented values is a refusal, not a silent fallback to the default; the service turns that refusal into a 422. |
| `LearnerSettingsRepository.save(settings) / .get(learner_id)` | **new**, `socratic.domain.repositories` | The settings of record for a learner, so a request that carries no settings — every iframe request — can still be served the learner's current ones. Shaped exactly like `LearnerProfileRepository`: one document per learner, rewritten in place. |
| `POST /settings` | **new**, `socratic.service.app` | How a valves change reaches the service **without** authoring a quiz. Without it "mid-quiz" has no meaning above the domain: between authoring and sealing, the only actor talking to the service is the iframe, and the iframe cannot see `UserValves`. |
| `QuizSession.submit(..., probe_cadence=…)` | existing | Already applies a live cadence from this submission on and leaves a pending probe alone. `/answers` simply has to supply it. |
| `QuizAuthoring.author(..., mode=…, probe_cadence=…)` | existing | Already fixes the mode at authoring time and stamps the cadence on the attempt. |
| `prompting.assemble(call_type, …, probe_cadence=…)` | existing | Already reads the cadence into nothing. The new tests vary the whole settings cross-product against it. |

The HTTP API stays **deliberately not a seam** (`quiz-service.md`): the new
route is a translation over the repository port, and it is tested through
`create_app` like the other four.

## Decisions

- **D10 / ADR-0010** — segment 1 is byte-identical for every learner, per call
  type. No setting adds or removes a byte of prompt text. Where instructions
  genuinely must vary they go in segment 2; nothing here needs to.
- **CONTEXT: Mode toggle** — applies from the **next authoring call**. One
  attempt, one mode. The attempt already carries `mode`, so this is a property
  of *not* reading the setting anywhere on the answering path, and it is
  asserted as such.
- **CONTEXT: `probe_cadence`** — applies **immediately**, from the next correct
  answer. A probe already pending **stands**; the learner's escape is to dismiss
  it.
- **ADR-0014 / CONTEXT: Model** — the model is an admin `Valve`, never
  `UserValves`. `LearnerSettings` carries no model, no effort and no "fast
  mode", and there is no field for one.
- **CONTEXT: ModeRegistry** — the registry stays the only place mode is branched
  on, so `LearnerSettings` does **not** validate the mode against an enum. An
  unknown mode is the registry's refusal on the authoring call, exactly as it is
  today; the settings value carries the string.

## Approach

### 1. `socratic.domain.settings` — a leaf module

`LearnerSettings` is a frozen dataclass with `learner_id`, `mode` (defaulting to
`DifficultyMode.NOVICE`) and `probe_cadence` (defaulting to
`ProbeCadence.SOMETIMES`). A module of its own, importing only `modes`, for the
same reason `profiles.py` is its own module: the repository ports and the
service both need it, and neither should have to import the other.

`parse` takes the two values as optional strings — `None` meaning "the learner
never set this one" — and returns a `LearnerSettings`, raising `ValueError`
naming the field for a cadence outside the four documented values.

### 2. The repository port

`LearnerSettingsRepository` beside `LearnerProfileRepository`, with
`InMemoryLearnerSettingsRepository` as both the prototype's store and the test
double (ADR-0005: the in-memory implementations *are* the doubles).

### 3. The service

`ServiceDependencies` grows a `settings` field, defaulted in `__post_init__` the
way `registry` already is, so existing wiring is untouched.

- `POST /settings` — service-token authorized, body `{learner_id, mode?,
  probe_cadence?}`, records the learner's settings and returns `{"ok": true}`.
  The Pipe (#12) calls it whenever it observes `UserValves`; a bad cadence is a
  422 by the same translation `/quizzes` already uses.
- `/quizzes` and `/overlays` — record the settings the request names before
  authoring with them, so authoring is itself a sync point and the common case
  needs no extra call.
- `/answers` — read the learner's recorded settings (the learner comes from the
  verified token, never from the request) and pass `probe_cadence` to
  `QuizSession.submit`. With nothing recorded, pass nothing: the domain falls
  back to `probe_cadence_at_authoring` and behaves exactly as it does today.
- `/answers` reads **no mode**. That omission is what makes "one attempt, one
  mode" true, and it is asserted rather than left to inspection.

### 4. The cache invariant, asserted against settings

`tests/test_learner_settings.py` walks the full settings cross-product — every
mode × every cadence — through `assemble` for **each of the five call types**,
and asserts one segment-1 text per call type. Two learner ids are used, so a
leak of the learner's own name fails it too. A deliberately broken assembler
that branches on settings is built in the same file, to show the assertion has
power — an unenforced guard and a working one look identical from the outside.

## Acceptance

Numbers are the master spec's; the bullets are issue #14's.

- **40** — flipping the mode toggle mid-quiz leaves the in-flight attempt's
  `mode` unchanged, and the next authoring call is authored in the new mode.
- **20** — changing `probe_cadence` mid-quiz takes effect on the next correct
  answer, through the HTTP surface; a probe already pending is unaffected.
- **10, 11** — segment 1 is byte-identical across learners with different
  settings, for every call type; and the same assertion catches an assembler
  that *omits* text for a learner as well as one that adds it.
- `probe_cadence` defaults to `sometimes` and accepts exactly the four
  documented values; anything else is refused, not defaulted.
- **21** — `probe_cadence_at_authoring` is stamped on the attempt from the
  learner's settings, and an attempt authored under `off` stays identifiable.

## Out of scope

- **The Pipe itself** — the `UserValves` Pydantic class, mapping `__user__` to a
  `LearnerId`, and the decision of when to call `POST /settings`. That is
  [#12](https://github.com/derkmed/socratic/issues/12), in flight in parallel.
  Nothing here imports Open WebUI.
- **The overlay's own cadence awareness.** The iframe renders the probe it is
  handed; it holds no cadence and asks for none.
- **A settings UI outside `UserValves`.** Open WebUI owns the control surface.
- **Any new field on `QuizAttempt`.** `probe_cadence_at_authoring` and `mode`
  are already there; this ticket adds nothing to the record.
- **Persisting settings history.** One document per learner, rewritten in place.
  What a quiz was authored under is already on the attempt.
- **Reading a stored `LearnerProfile`** — still #16.
