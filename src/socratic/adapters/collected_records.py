"""Encoding for the collected records trail (#117, ADR-0018).

The trail is **write-only**: this module turns a record into JSON and nothing
turns JSON back into a record, here or anywhere. A reader is what durability
would need - `QuizAttempt.__post_init__` surviving a round trip, a `Segment`
decoder, a corrupt-file policy - and ADR-0018 takes the writer alone, because
the bespoke reader is throwaway code on the path to the document store ADR-0005
already names.

Encoding is a **generic walk over `dataclasses.fields`**, never a hand-written
per-field encoder. The hand-written one's failure mode is silently omitting a
newly added field, which is the worst possible one for an audit record; the walk
picks up a new field the day it is declared. `tests/test_collected_records.py`
pins that against `dataclasses.fields` so a regression fails a test rather than
quietly shortening the record.

One case genuinely loses information under a generic walk. `Segment` is a bare
`Union` whose three members carry no discriminator, so once `TextSegment` is a
dict of its fields there is nothing left to say which member it was - hence the
one injected type tag, using `text`/`math`/`blank`, the vocabulary `authoring.py`
and ADR-0004 already speak.
"""

from __future__ import annotations

import dataclasses
import datetime as datetime_module
import enum
import json
import logging
import os
import pathlib
import urllib.parse
from typing import Any

from socratic.domain import ids
from socratic.domain import records as records_module
from socratic.domain import repositories
from socratic.domain import types as types_module

_log = logging.getLogger(__name__)


class FlushCadence(str, enum.Enum):
    """How often a record is pushed to disk.

    A knob held by the **service**, never by a caller: mode shapes a quiz, but
    an audit trail a caller can reshape per-request is not an audit trail
    (ADR-0018). `compose.yaml` already draws that line for
    `SOCRATIC_PUBLIC_URL`.
    """

    ON_CLOSE = "on_close"
    """The default. `save()` runs on every guess and probe - up to ~120 times
    against a growing document - and only a closed attempt is the immutable
    audit record ADR-0005 describes."""

    EVERY_SAVE = "every_save"
    """The demonstration setting: files appear while a quiz is being taken,
    at the read-modify-write cost ADR-0005 names."""

_SEGMENT_TAGS = {
    types_module.TextSegment: "text",
    types_module.MathSegment: "math",
    types_module.BlankSegment: "blank",
}

_KINDS = {
    records_module.QuizAttempt: "attempt",
    records_module.RatingRecord: "rating",
}

_DIRECTORIES = {"attempt": "attempts", "rating": "ratings"}

# `.` and `..` survive `quote` untouched - both are in its always-safe set - so
# the two path segments that actually traverse are the two the separator
# encoding does not reach. Named here rather than encoded wholesale, because a
# learner id is meant to stay readable in a directory a human opens (#117).
_TRAVERSAL = {".": "%2E", "..": "%2E%2E"}


def _kind_of(record: Any) -> str:
    """The record's tag, or the refusal naming what it was handed."""
    kind = _KINDS.get(type(record))
    if kind is None:
        raise TypeError(
            f"the collected records trail holds attempts and ratings, not "
            f"{type(record).__name__}"
        )
    return kind


def encode(record: Any, *, written_at: int) -> dict[str, Any]:
    """Wrap a record in the envelope every file in the trail carries.

    Args:
      record: A `QuizAttempt` or a `RatingRecord`.
      written_at: Milliseconds since the epoch, from the caller's
        `ids.Clock`. Injected rather than read here so a test can freeze it,
        the same reason `QuizSession` takes a clock.

    Returns:
      `{"schema_version", "kind", "written_at", "record"}`. The envelope exists
      because `QuizAttempt` carries `schema_version` as a field and
      `RatingRecord` does not: a bare walk would leave the rating file the one
      unversioned artifact in a directory whose whole value to an offline reader
      is that the version sits in a uniform place. `datetime` values are left as
      they are and handled by `json.dumps`; the enums and `QuizSessionId` need no
      handling at all, being `str` subclasses and a `str` alias respectively.

    Raises:
      TypeError: if `record` is not one of the two record types the trail holds.
    """
    kind = _kind_of(record)
    return {
        "schema_version": getattr(
            record, "schema_version", records_module.SCHEMA_VERSION
        ),
        "kind": kind,
        "written_at": written_at,
        "record": _walk(record),
    }


def _walk(value: Any) -> Any:
    """`dataclasses.asdict`, plus the one tag a bare `Union` cannot carry."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        encoded = {
            field.name: _walk(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        tag = _SEGMENT_TAGS.get(type(value))
        if tag is not None:
            encoded["kind"] = tag
        return encoded
    if isinstance(value, (list, tuple)):
        return [_walk(item) for item in value]
    if isinstance(value, dict):
        return {key: _walk(item) for key, item in value.items()}
    return value


def json_default(value: Any) -> str:
    """`json.dumps`'s `default`: the one type the walk leaves unencodable.

    The enums are `str` subclasses and `QuizSessionId` is a `str` alias, so both
    encode as strings with no help. `datetime` does not, and every record has at
    least one.
    """
    if isinstance(value, datetime_module.datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__} for the trail")


def record_path(data_dir: pathlib.Path, record: Any) -> pathlib.Path:
    """Where one record's file goes.

    `<data_dir>/attempts/<learner_id>/<attempt_id>.json`, and the same shape
    under `ratings/` - ratings partition on `learner_id` too, despite
    `RatingRepository.get` taking no partition key, because the trail shows the
    shape ADR-0005's document store will enforce rather than the prototype's
    convenience.

    Args:
      data_dir: The root, from `SOCRATIC_DATA_DIR`.
      record: A `QuizAttempt` or a `RatingRecord`.

    Returns:
      The file's path. The partition key is percent-encoded: it is a learner id
      that became a path segment, which makes it attacker-shaped input.

    Raises:
      TypeError: if `record` is not one of the two record types the trail holds.
    """
    kind = _kind_of(record)
    partition = urllib.parse.quote(record.learner_id, safe="")
    partition = _TRAVERSAL.get(partition, partition)
    return data_dir / _DIRECTORIES[kind] / partition / f"{record.attempt_id}.json"


def write_record(
    data_dir: pathlib.Path, record: Any, *, written_at: int
) -> None:
    """Write one record to its file, atomically.

    A temp file in the target directory plus `os.replace`, so a reader - the
    offline job, or a human with the directory open - never sees half a
    document. Same directory as the target, because `replace` is only atomic
    within a filesystem.

    Args:
      data_dir: The trail's root.
      record: A `QuizAttempt` or a `RatingRecord`.
      written_at: Milliseconds since the epoch.
    """
    path = record_path(data_dir, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    text = json.dumps(
        encode(record, written_at=written_at),
        default=json_default,
        indent=2,
    )
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


class _FileWriting:
    """What the two wrappers share: where to write, when, and by whose clock."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        flush: FlushCadence = FlushCadence.ON_CLOSE,
        clock: ids.Clock = ids.system_clock,
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._flush = flush
        self._clock = clock

    def _write(self, record: Any) -> None:
        """Write, or log why it did not.

        Never raises. The write lands on the request that seals the quiz, so
        raising would cost a learner their last answer to protect a demo
        artifact - inverting ADR-0005's judgement about what is valuable. The
        startup writability check is what catches a broken directory before a
        learner is involved.
        """
        try:
            write_record(self._data_dir, record, written_at=self._clock())
        except OSError as error:
            _log.warning(
                "could not write %s %s to the collected records trail: %s",
                _KINDS.get(type(record), "record"),
                record.attempt_id,
                error,
            )


class FileWritingAttemptRepository(_FileWriting, repositories.AttemptRepository):
    """An `AttemptRepository` that writes what passes through it.

    A **wrapper, not a subclass**: it holds any repository of the interface, so
    it still works when ADR-0005's document store arrives - you wrap that
    instead of re-deriving from it - and file I/O never welds itself to
    `InMemoryAttemptRepository`'s session bookkeeping.
    """

    def __init__(
        self,
        delegate: repositories.AttemptRepository,
        data_dir: pathlib.Path,
        *,
        flush: FlushCadence = FlushCadence.ON_CLOSE,
        clock: ids.Clock = ids.system_clock,
    ) -> None:
        super().__init__(data_dir, flush=flush, clock=clock)
        self._delegate = delegate

    def save(self, attempt: records_module.QuizAttempt) -> None:
        """Delegate, then write if this attempt is due.

        Delegating first is what keeps a rejected write off the disk: the
        delegate refuses a second write to a closed attempt, and writing first
        would leave a file for a write the store never accepted.
        """
        self._delegate.save(attempt)
        if self._flush is FlushCadence.EVERY_SAVE or attempt.is_closed:
            self._write(attempt)

    def get(
        self, learner_id: str, attempt_id: str
    ) -> records_module.QuizAttempt:
        return self._delegate.get(learner_id, attempt_id)

    def get_by_session(
        self, learner_id: str, session_id: ids.QuizSessionId
    ) -> records_module.QuizAttempt | None:
        return self._delegate.get_by_session(learner_id, session_id)

    def list_for_learner(
        self, learner_id: str
    ) -> tuple[records_module.QuizAttempt, ...]:
        return self._delegate.list_for_learner(learner_id)


class FileWritingRatingRepository(_FileWriting, repositories.RatingRepository):
    """A `RatingRepository` that writes what passes through it.

    Written on `save()` under either flush cadence: a rating has no closed state
    to wait for and is immutable on arrival, so the batching that spares the
    attempt ~120 writes has nothing to spare here.
    """

    def __init__(
        self,
        delegate: repositories.RatingRepository,
        data_dir: pathlib.Path,
        *,
        flush: FlushCadence = FlushCadence.ON_CLOSE,
        clock: ids.Clock = ids.system_clock,
    ) -> None:
        super().__init__(data_dir, flush=flush, clock=clock)
        self._delegate = delegate

    def save(self, rating: records_module.RatingRecord) -> None:
        """Delegate, then write. The delegate refuses a second rating."""
        self._delegate.save(rating)
        self._write(rating)

    def get(self, attempt_id: str) -> records_module.RatingRecord | None:
        return self._delegate.get(attempt_id)


def wrap(
    attempts: repositories.AttemptRepository,
    ratings: repositories.RatingRepository,
    *,
    data_dir: pathlib.Path | None,
    flush: FlushCadence = FlushCadence.ON_CLOSE,
    clock: ids.Clock = ids.system_clock,
) -> tuple[repositories.AttemptRepository, repositories.RatingRepository]:
    """The two repositories, wrapped for the trail or handed straight back.

    `data_dir` of `None` - `SOCRATIC_DATA_DIR` unset - returns exactly what it
    was given, so the service behaves precisely as it did before the trail
    existed. That is what makes this change additive and reversible.

    Args:
      attempts: The attempt repository to wrap.
      ratings: The rating repository to wrap.
      data_dir: The trail's root, or `None` for no persistence.
      flush: How often an attempt is pushed to disk.
      clock: Milliseconds since the epoch, for `written_at`.

    Returns:
      The pair, wrapped or not.
    """
    if data_dir is None:
        return attempts, ratings
    return (
        FileWritingAttemptRepository(
            attempts, data_dir, flush=flush, clock=clock
        ),
        FileWritingRatingRepository(ratings, data_dir, flush=flush, clock=clock),
    )
