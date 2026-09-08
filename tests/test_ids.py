"""The `QuizSessionId` we mint ourselves (D5 / D7, ADR-0007).

A ULID because the encoding is order-preserving: lexicographic sort *is*
mint-time sort, and mint time is recoverable from the value. Monotonic integer
keys would hot-spot a single shard on write.

A `QuizSessionId` is deliberately **not** a credential — it is timestamp
prefixed and near-monotonic, which is exactly why the capability token exists
separately (CONTEXT: Capability token).
"""

from datetime import datetime, timezone

import pytest

from socratic.domain.ids import CROCKFORD_ALPHABET, Ulid, new_quiz_session_id


def test_ulid_is_26_crockford_base32_characters():
    value = str(Ulid.mint())
    assert len(value) == 26
    assert set(value) <= set(CROCKFORD_ALPHABET)


def test_crockford_alphabet_excludes_the_ambiguous_letters():
    # I, L, O and U are excluded by Crockford's base32 precisely so a
    # transcribed id cannot be confused with 1/0.
    assert len(CROCKFORD_ALPHABET) == 32
    for letter in "ILOU":
        assert letter not in CROCKFORD_ALPHABET


def test_lexicographic_sort_equals_mint_time_order():
    minted = [str(Ulid.mint()) for _ in range(500)]
    assert sorted(minted) == minted


def test_mint_time_is_recoverable():
    before = datetime.now(timezone.utc)
    ulid = Ulid.mint()
    after = datetime.now(timezone.utc)

    recovered = ulid.timestamp
    assert recovered.tzinfo is timezone.utc
    # Recovery is to millisecond precision, so compare against truncated bounds.
    assert _floor_ms(before) <= recovered <= after


def test_mint_time_is_recoverable_from_the_string_alone():
    ulid = Ulid.mint()
    assert Ulid.parse(str(ulid)).timestamp == ulid.timestamp


def test_two_ulids_minted_in_the_same_millisecond_still_sort_by_mint_order():
    clock = _FrozenClock(1_700_000_000_000)
    first = Ulid.mint(clock=clock)
    second = Ulid.mint(clock=clock)
    assert str(first) < str(second)
    assert first.timestamp == second.timestamp


def test_ulids_are_not_sequential_across_milliseconds():
    # Near-monotonic, not guessable-by-increment. The randomness re-seeds
    # whenever the millisecond advances.
    clock = _FrozenClock(1_700_000_000_000)
    first = Ulid.mint(clock=clock)
    clock.advance(1)
    second = Ulid.mint(clock=clock)
    assert str(first)[10:] != str(second)[10:]


def test_parse_rejects_a_value_that_is_not_a_ulid():
    with pytest.raises(ValueError):
        Ulid.parse("not-a-ulid")
    with pytest.raises(ValueError):
        # 'I' is not in the Crockford alphabet.
        Ulid.parse("I" * 26)


def test_new_quiz_session_id_is_a_ulid_string():
    session_id = new_quiz_session_id()
    assert isinstance(session_id, str)
    assert len(session_id) == 26
    assert Ulid.parse(session_id).timestamp is not None


def _floor_ms(moment: datetime) -> datetime:
    return moment.replace(microsecond=(moment.microsecond // 1000) * 1000)


class _FrozenClock:
    """A millisecond clock the tests drive by hand."""

    def __init__(self, millis: int) -> None:
        self._millis = millis

    def __call__(self) -> int:
        return self._millis

    def advance(self, millis: int) -> None:
        self._millis += millis
