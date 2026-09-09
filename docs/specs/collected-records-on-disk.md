# Collected records on disk — the write-only JSON trail

## Goal

The prototype claims to keep an audit record of every quiz and has never written
one down. [#117](https://github.com/derkmed/socratic/issues/117) asks the
repository to justify its working status by actually collecting data, in JSON,
in a dedicated directory.

This builds the **collected records** trail (CONTEXT):
[ADR-0005](../adr/0005-persistence-contract.md) settled the persistence
*contract* and left the implementation as in-memory dictionaries;
[ADR-0018](../adr/0018-collected-records-on-disk.md) takes half of the migration
that contract names — the **writer, with no reader**. Closed attempts and
ratings are written to `SOCRATIC_DATA_DIR` as one JSON file per record, in a
tree that mirrors ADR-0005's `learner_id` partition. The service never reads the
directory back and still boots with empty repositories, so a restart still loses
every in-flight quiz. The files are an audit trail and the corpus
[ADR-0006](../adr/0006-learner-profile-lifecycle.md)'s offline job scans, not
process state.

The reason durability is *not* in here: it needs a reader, which means
`QuizAttempt.__post_init__` validation surviving a round trip, a `Segment`
decoder and a corrupt-file policy. Data collection needs only the writer, and
the bespoke JSON reader is throwaway code on the path to the document store
ADR-0005 already names.

## Seams

Three existing seams carry almost all of this. One new seam is proposed, and it
is a pure function.

**Existing — `AttemptRepository` / `RatingRepository` and their tests**
(`tests/test_repositories.py`). The wrapper is another implementation of the
same two ports, so the port-contract tests already there apply to it unchanged:
closed-means-closed, partitioned reads, ratings written once. Attaching here is
what makes "a wrapper, not a subclass" testable — the wrapper holds *any*
repository of its interface, so the in-memory one goes in as the delegate and
the contract tests run against the wrapped pair.

**Existing — `ServiceConfig.from_env(env=...)`** (`src/socratic/service/config.py:49`,
covered in `tests/test_service.py`). It already takes an injectable `env` dict
and already raises `ConfigError` for every other variable, so `data_dir` and the
flush cadence attach with a `tmp_path` and no new machinery. This is the seam
for: unset means no persistence, a path that is a file is refused, an
unwritable directory is refused.

**Existing — `_compose_environment`** (`tests/test_compose_hygiene.py`). Its
standing rule is that every `SOCRATIC_*` variable is read by the container it is
set on. Setting the two new variables on `quiz-service` is checked by that test
automatically; nothing new is needed, and putting either of them on the
`open-webui` container would fail it — which is the same line the design draws
by hand.

**New — `encode(record) -> dict`**, a pure function in `adapters/`, separate
from the writer. Justified because the encoding is the part with a real failure
mode (a newly added dataclass field silently omitted, a `Segment` losing its
type) and it is the part that must be asserted structurally, since no decoder
exists anywhere to round-trip against. Keeping it pure means the field-coverage
assertions need no filesystem, and the writer's own tests can be about paths,
atomicity and swallowed failures instead.

The wrapper additionally takes an `ids.Clock`, following `QuizSession`'s
existing injection convention (`src/socratic/domain/session.py:704`), so
`written_at` is deterministic under test. That is a constructor parameter, not a
seam of its own.

## Decisions

Every one of these is recorded in
[ADR-0018](../adr/0018-collected-records-on-disk.md); the CONTEXT term is
**Collected records**.

**Write-only.** The service never reads the directory back. No corrupt-file,
schema-drift or partial-write recovery policy exists, because there is no read
path to need one.

**Attempts and ratings only.** `LearnerProfile` and `LearnerSettings` are
derived or re-enterable, and the profile repository is not reachable from the
running service at all.

**The implementation lives in `adapters/`.** The domain package declares the
ports and stays free of `open()` and `pathlib`; `adapters/anthropic_client.py`
already establishes where I/O-touching implementations of domain interfaces go.
The in-memory repositories stay in `domain/` as the exception they are.

**A wrapper, not a subclass.** `FileWriting*Repository` holds any repository of
its interface and writes on the way through. It is the shape that still works
when ADR-0005's document store arrives, and it keeps file I/O from welding
itself to `InMemoryAttemptRepository`'s session bookkeeping.

**The wrapper delegates first, then writes.** The delegate is the authority on
whether the write is legal — `InMemoryAttemptRepository.save` refuses a second
write to a closed attempt — so writing first would leave a file for a write the
store rejected.

**One JSON file per record, in a tree mirroring the partition.**
`data/attempts/<learner_id>/<attempt_id>.json` and
`data/ratings/<learner_id>/<attempt_id>.json`, with `learner_id` percent-encoded
so an id containing `/` or `..` cannot escape the directory. Ratings partition
this way despite `RatingRepository.get` taking no partition key: the flat lookup
is a prototype convenience, and the trail shows the shape ADR-0005's document
store will enforce. Written via a temp file plus rename within the target
directory, which is genuinely atomic.

**A self-describing envelope**, `{"schema_version", "kind", "written_at",
"record"}`. `QuizAttempt` carries `schema_version` as a field and `RatingRecord`
does not, so a bare walk would yield a versioned attempt beside an unversioned
rating; the whole value of a version nobody reads is that an offline reader
finds it in one uniform place. `written_at` is epoch milliseconds from the
injected `ids.Clock`; the records' own `datetime` fields stay ISO-8601 through
the encoder's `json.dumps` default.

**Encoded by a generic `dataclasses.asdict` walk**, with `datetime`, the enums
and `QuizSessionId` handled in a `json.dumps` default and a type tag injected
for `Segment` only. `Segment` is a bare `Union` whose members carry no
discriminator; the tags are `text`/`math`/`blank`, the vocabulary `authoring.py`
and [ADR-0004](../adr/0004-quiz-wire-format.md) already use. A hand-written
encoder is rejected: its failure mode is silently omitting a newly added field,
which is the worst possible one for an audit record.

**The whole record, answer keys included.**
[ADR-0003](../adr/0003-grading-authority-and-key-custody.md)'s key custody
governs what crosses to the *browser*; a service-side file is a different threat
model. A redacted attempt cannot reconstitute a session and is worthless to the
profile job. The control is that the directory is untracked.

**Written when the attempt closes**, not on every `save()` — `save()` is called
on every guess and probe, up to ~120 times against a growing document.
`is_closed` is already what CONTEXT nominates as the shared predicate, and a
closed attempt is exactly ADR-0005's immutable audit record. A `RatingRecord`
has no closed state and is immutable on arrival, so it is written on `save()`.

**Flush cadence is a knob held by the service**, `SOCRATIC_DATA_FLUSH`, values
`on_close` (default) and `every_save`. Writing only on close is a weak
demonstration of collection, so the batching can be overridden to write on every
`save()`. It is install-wide and env-defaulted, in the same class as
`SOCRATIC_MODE`. It is deliberately **not** a Pipe `Valves` field beside `mode`,
and not a per-learner setting: mode shapes a quiz, and an audit trail a caller
can reshape per-request — or a learner can opt out of — is not an audit trail.
`compose.yaml` already draws that line for `SOCRATIC_PUBLIC_URL`, held by the
service and never accepted from a caller. The Pipe holds no domain logic and no
disk, so it does not decide how the disk is written.

**`SOCRATIC_DATA_DIR` is read by `ServiceConfig.from_env`** as an optional
`data_dir`, not by `build_app` where the repositories are constructed. One place
answers what the service reads from the environment.

**Unset means no persistence.** The wrapper is simply not applied and the
service behaves exactly as it does today, sparing every existing test a
temporary directory it does not care about.

**An unwritable directory refuses the boot**, `ConfigError`. A *runtime* write
failure is logged and never raised, because it lands on the request that seals
the quiz and raising would cost a learner their last answer to protect a demo
artifact. A startup failure has no learner to protect, and setting the variable
is a deliberate statement that the records are wanted — so silently not
collecting them is the failure #117 exists to prevent.

**The write is synchronous** on the request that seals.
[ADR-0011](../adr/0011-latency-budget.md) makes latency a primary concern, but a
few KB to local disk is noise against the model call in the same request, and a
background write would be untestable without synchronisation and losable to an
at-exit race.

**Tests assert on the JSON structurally. No decoder, not even in tests.** A
round-trip test would prove fidelity best, but a test-only decoder is the read
path this design declines, and it is precisely what someone promotes to
production the day durability is wanted, in place of the document store ADR-0005
names.

**`compose.yaml` turns collection on by default**, bind-mounting `./data` and
setting `SOCRATIC_DATA_DIR`. A demonstration of data collection that collects
nothing until a second variable is set demonstrates the opposite. A bind mount
rather than a named volume because reading a named volume needs `docker cp`, and
a demonstration of collected data you cannot see is not one; `compose.yaml`'s
standing objection to bind mounts is about *build output* differing across Mac,
Windows and Linux, which a data directory does not.

**Visibility is a README section, not a tool.** The bind mount already puts the
files under the reader's cursor in `./data/`; a `dump` command would be the read
path this design declines, wearing a different hat.

## Approach

1. **`encode`** — the pure encoder in `adapters/`: the `asdict` walk, the
   `json.dumps` default for `datetime`, the enums and `QuizSessionId`, the
   `Segment` type tag, and the envelope. Built first because everything else
   consumes it and it is the part with the real failure mode.
2. **The path function** — `learner_id` percent-encoding and the two path
   shapes. Pure, and the place the directory-escape case is pinned.
3. **`FileWritingAttemptRepository`** — delegate first, then write when
   `is_closed` (or on every `save()` under `every_save`); temp file plus rename;
   failure logged through `logging.getLogger(__name__)`, never raised.
4. **`FileWritingRatingRepository`** — the same, writing on every `save()` since
   a rating is immutable on arrival.
5. **`ServiceConfig`** — optional `data_dir`, the flush cadence, and the startup
   writability check raising `ConfigError`.
6. **`build_app`** — wrap `attempts` and the rating repository when `data_dir`
   is set; leave them bare when it is not.
7. **`.gitignore`** — `/data/`.
8. **`compose.yaml`** — the `./data` bind mount and the two variables on
   `quiz-service`, which `test_compose_hygiene.py` then holds in place.
9. **README** — the section showing the tree and one pretty-printed record.

## Out of scope

**Durability.** A restart still loses every in-flight quiz. The files look like
durable state and are not — the single most important thing a future reader
could assume wrongly here.

**Any read path.** No loader, no CLI, no `dump` tool, no test decoder. The
service boots with empty repositories exactly as today.

**`LearnerProfile` and `LearnerSettings` persistence.** Derived or re-enterable,
and the profile repository is not reachable from the running service.

**The offline profile job itself** (ADR-0006). This produces the corpus it will
scan; it does not scan it.

**The document store migration** (ADR-0005). That remains an implementation swap
behind the same ports, and this wrapper is the shape that survives it.

**Redaction of answer keys.** Deliberately not done; the control is that the
directory is untracked.

**Any per-learner control over collection.** Not a `UserValves` field, not on
`LearnerSettings`, not accepted from a caller. Consent-shaped collection is a
different design with a different threat model.

**Retention, rotation, or size limits.** ADR-0005's bounds cap a single
document; nothing here caps the directory.

**`schema_version` enforcement.** It is written and read by nobody, so drift is
only detectable offline. No migration machinery.

## Acceptance

1. With `SOCRATIC_DATA_DIR` unset, no file is written and the service behaves
   exactly as it does today; no existing test needs a temporary directory.
2. Sealing an attempt writes exactly one file at
   `<data_dir>/attempts/<learner_id>/<attempt_id>.json`.
3. Abandoning an attempt by displacement writes it too — the predicate is
   `is_closed`, not `sealed_at`.
4. Under `on_close`, no file exists for an attempt after a guess or a probe;
   under `every_save`, a file exists after the first `save()` and is rewritten
   on each subsequent one.
5. Saving a `RatingRecord` writes
   `<data_dir>/ratings/<learner_id>/<attempt_id>.json` immediately.
6. A `learner_id` containing `/` or `..` lands inside the data directory, not
   outside it.
7. The encoded attempt contains a key for **every** field of `QuizAttempt`,
   asserted against `dataclasses.fields` so a newly added field fails the test
   rather than vanishing.
8. Every `Segment` in the encoded quiz carries its `text` / `math` / `blank`
   tag.
9. The file parses as JSON and its top level is
   `{"schema_version", "kind", "written_at", "record"}`, for a rating as well as
   an attempt.
10. A save the delegate refuses — a second write to a closed attempt — raises
    and writes no file.
11. A failing file write is logged and does **not** propagate; the delegate's
    write still stands.
12. `SOCRATIC_DATA_DIR` pointing at a file, or at an unwritable directory,
    raises `ConfigError` from `ServiceConfig.from_env`.
13. Both new variables are set on `quiz-service` in `compose.yaml`, and
    `test_compose_hygiene.py` passes — which is the assertion that neither sits
    on the `open-webui` container.
14. `/data/` is gitignored, so `git status` is clean after a quiz is collected.
