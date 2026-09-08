"""ULID minting for the identifiers the domain owns (ADR-0007).

We mint our own `QuizSessionId` because there is nothing to borrow: the
Anthropic Messages API is stateless and has no conversation object, and Open
WebUI's `chat_id` is not reliably present in every invocation context and would
leak the host through the portability seam.

A ULID is 128 bits — a 48-bit millisecond timestamp followed by 80 bits of
randomness — rendered as 26 Crockford base32 characters. The encoding is
order-preserving, so lexicographic sort is mint-time sort, and the timestamp is
recoverable from the value alone.

Minting is **monotonic**: two ULIDs minted in the same millisecond increment the
random component rather than re-rolling it, so mint order survives even at
sub-millisecond rates. Across milliseconds the randomness is re-rolled, so the
sequence stays unguessable-by-increment. That is only "near-monotonic", which is
why a `QuizSessionId` is not a credential and the capability token exists
separately.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

# Crockford base32: the digits plus the uppercase alphabet minus I, L, O and U,
# so a transcribed id cannot be confused with 1/0.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_DECODE = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}

ULID_LENGTH = 26
_TIMESTAMP_CHARS = 10
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_MAX_TIMESTAMP = (1 << _TIMESTAMP_BITS) - 1
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1

Clock = Callable[[], int]
"""A source of milliseconds since the Unix epoch. Injected so tests can freeze it."""


def system_clock() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Ulid:
    """A 128-bit ULID, held as its two components."""

    millis: int
    randomness: int

    def __post_init__(self) -> None:
        if not 0 <= self.millis <= _MAX_TIMESTAMP:
            raise ValueError(f"timestamp out of range for a ULID: {self.millis}")
        if not 0 <= self.randomness <= _MAX_RANDOM:
            raise ValueError(f"randomness out of range for a ULID: {self.randomness}")

    @classmethod
    def mint(cls, clock: Clock = system_clock) -> "Ulid":
        return _MINTER.mint(clock)

    @classmethod
    def parse(cls, value: str) -> "Ulid":
        if len(value) != ULID_LENGTH:
            raise ValueError(
                f"a ULID is {ULID_LENGTH} characters, got {len(value)}: {value!r}"
            )
        number = 0
        for char in value:
            try:
                number = (number << 5) | _DECODE[char]
            except KeyError:
                raise ValueError(
                    f"{char!r} is not a Crockford base32 character: {value!r}"
                ) from None
        return cls(millis=number >> _RANDOM_BITS, randomness=number & _MAX_RANDOM)

    @property
    def timestamp(self) -> datetime:
        """The mint time, to millisecond precision, in UTC."""
        return datetime.fromtimestamp(self.millis / 1000, tz=timezone.utc)

    def __str__(self) -> str:
        number = (self.millis << _RANDOM_BITS) | self.randomness
        chars = []
        for _ in range(ULID_LENGTH):
            chars.append(CROCKFORD_ALPHABET[number & 0x1F])
            number >>= 5
        return "".join(reversed(chars))


class _MonotonicMinter:
    """Guarantees mint order survives within a single millisecond."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_millis = -1
        self._last_randomness = 0

    def mint(self, clock: Clock) -> Ulid:
        with self._lock:
            millis = clock()
            if millis == self._last_millis:
                # Same millisecond: increment rather than re-roll, so the
                # lexicographic order still matches the order of minting.
                if self._last_randomness >= _MAX_RANDOM:
                    raise RuntimeError(
                        "exhausted the ULID randomness space within one millisecond"
                    )
                randomness = self._last_randomness + 1
            else:
                randomness = int.from_bytes(os.urandom(10), "big")
            self._last_millis = millis
            self._last_randomness = randomness
            return Ulid(millis=millis, randomness=randomness)


_MINTER = _MonotonicMinter()


QuizSessionId = str
"""A ULID string. The primary key for a quiz session, and **not** a credential.

`chat_id`, `session_id` and every Anthropic `message.id` are host annotations
(CONTEXT: Host annotations) — never keys, never depended on.
"""


def new_quiz_session_id(clock: Clock = system_clock) -> QuizSessionId:
    return str(Ulid.mint(clock=clock))
