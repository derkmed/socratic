"""The capability token (D15, ADR-0015; CONTEXT: Capability token).

The iframe is sandboxed without `allow-same-origin`, so it has an opaque origin
and carries no cookie and no Open WebUI token. The token is the whole
authorization story for an answer arriving by `fetch`, which is why expiry,
wrong-session rejection and per-response rotation are asserted here — against a
frozen clock, with no HTTP server anywhere (spec acceptance 35 and 36).
"""

import pytest

from socratic.domain import tokens

SECRET = b"0123456789abcdef0123456789abcdef"
OTHER_SECRET = b"fedcba9876543210fedcba9876543210"

SESSION = "01J000000000000000000000AA"
OTHER_SESSION = "01J000000000000000000000BB"
LEARNER = "learner-7"


class _FrozenClock:
    """A millisecond clock the tests drive by hand, as in `test_ids`."""

    def __init__(self, millis: int = 1_700_000_000_000) -> None:
        self._millis = millis

    def __call__(self) -> int:
        return self._millis

    def advance(self, millis: int) -> None:
        self._millis += millis


def _minter(clock=None, **kwargs) -> tokens.TokenMinter:
    return tokens.TokenMinter(SECRET, clock=clock or _FrozenClock(), **kwargs)


def test_a_minted_token_verifies_and_names_its_session_and_learner():
    clock = _FrozenClock()
    minter = _minter(clock)

    claims = minter.verify(minter.mint(SESSION, LEARNER))

    assert claims.session == SESSION
    assert claims.learner == LEARNER
    assert claims.issued_at_millis == clock()
    assert claims.expires_at_millis == clock() + tokens.DEFAULT_TTL_MILLIS


def test_a_token_is_opaque_text_that_does_not_carry_the_secret():
    token = _minter().mint(SESSION, LEARNER)
    assert isinstance(token, str)
    assert token.count(".") == 1
    assert "0123456789abcdef" not in token


def test_a_token_past_its_ttl_is_rejected():
    # Acceptance 35, asserted against a frozen clock rather than a sleep.
    clock = _FrozenClock()
    minter = _minter(clock, ttl_millis=60_000)
    token = minter.mint(SESSION, LEARNER)

    clock.advance(59_999)
    assert minter.verify(token).session == SESSION

    clock.advance(1)
    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(token)


def test_a_token_scoped_to_a_different_session_is_rejected():
    # Acceptance 35: the service passes the session the request is addressed to.
    minter = _minter()
    token = minter.mint(SESSION, LEARNER)

    assert minter.verify(token, for_session=SESSION).learner == LEARNER
    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(token, for_session=OTHER_SESSION)


def test_a_tampered_token_is_rejected():
    minter = _minter()
    body, signature = minter.mint(SESSION, LEARNER).split(".")

    flipped = "B" if body[0] != "B" else "C"
    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(f"{flipped}{body[1:]}.{signature}")
    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(f"{body}.{flipped}{signature[1:]}")


def test_a_token_signed_with_a_different_secret_is_rejected():
    clock = _FrozenClock()
    forger = tokens.TokenMinter(OTHER_SECRET, clock=clock)
    forged = forger.mint(SESSION, LEARNER)

    with pytest.raises(tokens.CapabilityTokenRejected):
        tokens.TokenMinter(SECRET, clock=clock).verify(forged)


def test_a_token_body_re_signed_over_edited_claims_is_rejected():
    # The forgery route worth naming: keep the signature, swap the learner.
    minter = _minter()
    minter.mint(SESSION, LEARNER)
    victim = minter.mint(SESSION, "victim")
    body, _ = victim.split(".")
    _, attacker_signature = minter.mint(SESSION, "attacker").split(".")

    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(f"{body}.{attacker_signature}")


def test_malformed_input_is_rejected_rather_than_raising_something_else():
    minter = _minter()
    for junk in ["", ".", "not-a-token", "a.b.c", "!!!.###", "x." + "y" * 43]:
        with pytest.raises(tokens.CapabilityTokenRejected):
            minter.verify(junk)


def test_rejection_does_not_say_which_check_failed():
    # One message for every rejection: expiry, scope, tampering and junk are
    # indistinguishable to a caller, so none of them is an oracle.
    clock = _FrozenClock()
    minter = _minter(clock, ttl_millis=1_000)
    expired = minter.mint(SESSION, LEARNER)
    clock.advance(2_000)

    messages = set()
    for bad in [expired, "not-a-token"]:
        with pytest.raises(tokens.CapabilityTokenRejected) as caught:
            minter.verify(bad)
        messages.add(str(caught.value))
    with pytest.raises(tokens.CapabilityTokenRejected) as caught:
        minter.verify(minter.mint(SESSION, LEARNER), for_session=OTHER_SESSION)
    messages.add(str(caught.value))

    assert len(messages) == 1


def test_rotation_issues_a_fresh_token_and_retires_the_previous_one():
    # Acceptance 36: every grading response returns a fresh token, which the
    # iframe swaps in; the one it replaced stops being accepted.
    minter = _minter()
    first = minter.mint(SESSION, LEARNER)

    second = minter.rotate(first)

    assert second != first
    claims = minter.verify(second)
    assert claims.session == SESSION
    assert claims.learner == LEARNER
    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.verify(first)


def test_rotation_slides_the_expiry_forward():
    clock = _FrozenClock()
    minter = _minter(clock, ttl_millis=60_000)
    first = minter.mint(SESSION, LEARNER)

    clock.advance(30_000)
    second = minter.rotate(first)

    assert minter.verify(second).expires_at_millis == clock() + 60_000
    clock.advance(59_999)
    assert minter.verify(second).session == SESSION


def test_a_retired_token_cannot_be_rotated():
    minter = _minter()
    first = minter.mint(SESSION, LEARNER)
    minter.rotate(first)

    with pytest.raises(tokens.CapabilityTokenRejected):
        minter.rotate(first)


def test_rotating_one_session_does_not_retire_another():
    minter = _minter()
    theirs = minter.mint(OTHER_SESSION, "someone-else")
    mine = minter.mint(SESSION, LEARNER)

    minter.rotate(mine)

    assert minter.verify(theirs).session == OTHER_SESSION


def test_two_tokens_for_the_same_session_and_millisecond_still_differ():
    # The token id is minted randomness, not derived from the session id, which
    # is timestamp-prefixed and near-monotonic and therefore not a credential
    # (D7, ADR-0007).
    minter = _minter()
    first = minter.mint(SESSION, LEARNER)
    second = minter.mint(SESSION, LEARNER)
    assert first != second


def test_a_token_from_another_minters_state_is_rejected():
    # Supersession state is in-process; a restart fails closed (ADR-0015).
    clock = _FrozenClock()
    token = tokens.TokenMinter(SECRET, clock=clock).mint(SESSION, LEARNER)

    with pytest.raises(tokens.CapabilityTokenRejected):
        tokens.TokenMinter(SECRET, clock=clock).verify(token)


def test_the_default_ttl_is_minutes():
    # "The exposure window stays at minutes" (CONTEXT: Capability token).
    assert 60_000 <= tokens.DEFAULT_TTL_MILLIS <= tokens.MAX_TTL_MILLIS
    assert tokens.MAX_TTL_MILLIS <= 30 * 60_000


def test_the_ttl_is_configurable_within_the_ceiling():
    clock = _FrozenClock()
    minter = tokens.TokenMinter(SECRET, ttl_millis=120_000, clock=clock)
    claims = minter.verify(minter.mint(SESSION, LEARNER))
    assert claims.expires_at_millis - claims.issued_at_millis == 120_000


def test_an_unusable_ttl_is_refused_at_construction():
    for ttl in [0, -1, tokens.MAX_TTL_MILLIS + 1]:
        with pytest.raises(ValueError):
            tokens.TokenMinter(SECRET, ttl_millis=ttl, clock=_FrozenClock())


def test_the_secret_must_be_supplied_and_must_be_strong_bytes():
    with pytest.raises(TypeError):
        tokens.TokenMinter("a-string-secret" * 3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        tokens.TokenMinter(b"")
    with pytest.raises(ValueError):
        tokens.TokenMinter(b"too-short")


def test_the_session_and_learner_round_trip_delimiter_bearing_values():
    # The signed body is length-prefixed, so no field value can manufacture a
    # boundary: these two mint distinguishable tokens rather than colliding.
    minter = _minter()
    left = minter.verify(minter.mint("a.b|c", "d"))
    right = minter.verify(minter.mint("a", "b|c.d"))
    assert (left.session, left.learner) == ("a.b|c", "d")
    assert (right.session, right.learner) == ("a", "b|c.d")


def test_a_clock_reading_outside_the_timestamp_field_is_refused():
    # The stamps are fixed 8-byte fields; a reading that would overflow them is
    # a broken clock, and it is better heard than silently truncated.
    for millis in [-1, 1 << 64]:
        minter = _minter(_FrozenClock(millis))
        with pytest.raises(ValueError):
            minter.mint(SESSION, LEARNER)
