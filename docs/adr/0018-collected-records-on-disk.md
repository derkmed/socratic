# ADR-0018 — Collected records on disk are written, never read

Status: accepted
Date: 2026-09-09
Extends: [ADR-0005](0005-persistence-contract.md)

## Context

[ADR-0005](0005-persistence-contract.md) settled the persistence *contract* —
partition on `learner_id`, ULID keys, one `QuizAttempt` document, ratings
separate — and left the *implementation* as in-memory dictionaries, naming the
migration to a real document store "an implementation swap behind the repository
interfaces". Nothing has ever been written down (#117).

Two different things wanted that swap, and they pull apart:

- **Durability** — a restart should not lose a learner's in-flight quiz. That
  needs a reader, which means `QuizAttempt.__post_init__` validation surviving a
  round trip, a segment decoder, and a policy for a corrupt file.
- **Data collection** — the offline profile job ([ADR-0006](0006-learner-profile-lifecycle.md))
  needs attempts it can scan, and a prototype needs to show that the records it
  claims to keep actually exist.

## Decision

**Write-only. The service never reads the directory back.** It boots with empty
repositories exactly as it does today; the files are an audit trail and the
substrate for ADR-0006's offline job, not process state. A bespoke JSON reader
is throwaway code on the path to the document store ADR-0005 already names,
whereas the written records outlive the prototype.

**Attempts and ratings only.** They are the audit trail ADR-0005 describes and
the two repositories the service actually constructs. `LearnerProfile` and
`LearnerSettings` are derived or re-enterable, and the profile repository is not
reachable from the running service at all — persisting it would be dead code.

**The implementation lives in `adapters/`, not `domain/`.** The domain package
declares the repository interfaces and stays free of `open()` and `pathlib`;
`adapters/anthropic_client.py` already establishes that I/O-touching
implementations of domain interfaces belong there. The in-memory repositories
stay in `domain/` as the exception they are — test doubles in production
clothing.

**A bind mount to a gitignored `./data/`, located by `SOCRATIC_DATA_DIR`.**
A named volume would survive `docker compose down`, but reading it needs
`docker cp`, and a demonstration of collected data that you cannot see is not
one. `compose.yaml`'s standing objection to bind mounts is about *build output*
differing across Mac, Windows and Linux; a data directory does not have that
problem.

**The whole record goes to disk, answer keys included.** [ADR-0003](0003-grading-authority-and-key-custody.md)'s
key custody governs what crosses to the *browser*; a service-side file is a
different threat model. A redacted attempt cannot reconstitute a session and is
worthless to the profile job — fidelity is the entire point of an audit record.
The control is that the directory is untracked.

**Written when the attempt closes, not on every `save()`.** `save()` is called
on every guess and every probe — up to ~120 times against a growing document,
which ADR-0005 already names as the read-modify-write cost. CONTEXT (Closed)
already nominates `is_closed` as what "the repository and the profile job's
watermark all read", and a closed attempt is exactly the immutable audit record
ADR-0005 describes. A `RatingRecord` has no closed state and is immutable on
arrival, so it is written on `save()`.

**A wrapper, not a subclass.** `FileWriting*Repository` holds any repository of
its interface and writes on the way through, rather than deriving from the
in-memory one. It is the shape that still works when ADR-0005's document store
arrives — you wrap that instead of re-deriving from it — and it keeps file I/O
from welding itself to `InMemoryAttemptRepository`'s session bookkeeping.

**Encoded by a generic `dataclasses.asdict` walk**, with `datetime`, the enums
and `QuizSessionId` handled in a `json.dumps` default, and a type tag injected
for `Segment` only. `Segment` is a bare `Union` whose members carry no
discriminator, so that one case genuinely loses information; the tags are
`text`/`math`/`blank`, the vocabulary `authoring.py` and ADR-0004 already use.
A hand-written encoder was rejected because its failure mode — silently omitting
a newly added field — is the worst possible one for an audit record.

**A failed write is logged, not raised; the directory is checked at startup.**
Because the write lands on the request that seals the quiz, raising would cost a
learner their last answer to protect a demo artifact, inverting ADR-0005's
judgement about what is valuable. A writability check at startup catches every
realistic cause before a learner is involved.

**`SOCRATIC_DATA_DIR` is optional; unset means no persistence.** The wrapper is
simply not applied, and the service behaves exactly as it does today. It matches
the additive, reversible nature of a write-only trail, and spares every existing
test a temporary directory it does not care about.

## Consequences

Good:
- A fraction of the work of a durable store, with no read path and so no
  corrupt-file, schema-drift or partial-write recovery policy to design.
- ADR-0006's offline job gets a real corpus to scan instead of a live process.
- The eventual document store swaps in behind the same interfaces, unchanged.

Costs / risks:
- **A restart still loses every in-flight quiz.** The files look like durable
  state and are not, which is exactly the sort of thing a future reader assumes
  wrongly — hence this ADR.
- `schema_version` is written and read by nobody, so drift is only detectable
  offline.
- Answer keys sit in the working tree, protected by `.gitignore` alone.
