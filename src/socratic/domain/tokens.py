"""The capability token that authorizes an iframe request (D15, ADR-0015).

The quiz iframe is sandboxed **without `allow-same-origin`**, so it has an
opaque origin, carries no cookie and no Open WebUI token, and cannot
authenticate by any ambient means. The moment an answer arrives by `fetch` there
is a network boundary; this module is its whole authorization story
(CONTEXT: Capability token).

A token is HMAC-signed, scoped to one `QuizSessionId` and its `LearnerId`, and
short-lived. **Every grading response returns a fresh one** which the iframe
swaps in, so an engaged learner's session slides forward while the exposure
window stays at minutes. Rotation retires its predecessor, so a token that
leaked is useful only until the learner's next answer.

The `QuizSessionId` is deliberately **not** a credential: it is timestamp
prefixed, near-monotonic and stamped through the audit trail (D7, ADR-0007),
which is exactly why this seam exists separately from it. A token therefore
carries 128 bits of minted randomness of its own — its `token_id` — and nothing
about it is derived from the session id.

**The signed body is length-prefixed, never delimited.** Every variable-length
field is written as a fixed-width big-endian length followed by its bytes, so no
field value can manufacture a boundary: `("a|b", "c")` and `("a", "b|c")` frame
to different bytes and cannot collide under the MAC. The MAC covers a constant
domain-separation prefix as well as the frame, so a signature is meaningless
outside this use. Only the frame travels; the prefix is knowledge both ends
already have.

Stdlib only, like the rest of the domain (CONTEXT: Portability seam): `hmac`,
`hashlib`, `base64` and `secrets`, and comparisons go through
`hmac.compare_digest` rather than `==`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from socratic.domain.ids import Clock, QuizSessionId, system_clock

LearnerId = str
"""Our identifier for a learner, derived by the adapter from Open WebUI's
authenticated `__user__["id"]` (CONTEXT: LearnerId).

**Provisional.** Nothing else in the domain defines it yet; this is the
`QuizSessionId` precedent in `ids.py` applied to the other half of a token's
scope, and it moves to its own module when a ticket owns learner identity.
"""

DEFAULT_TTL_MILLIS = 5 * 60_000
"""Five minutes. Short enough that a leaked token is worth minutes, long enough
that a learner reading a paragraph before answering does not fall off the end —
and any engaged learner slides forward on the next grading response anyway."""

MAX_TTL_MILLIS = 15 * 60_000
"""The ceiling on a configured TTL. "The exposure window stays at minutes" is a
property of the design, not a default someone can quietly widen to a day."""

MIN_SECRET_BYTES = 32
"""A full SHA-256 block of key material. Shorter secrets are refused rather than
stretched, because the caller supplying one has made a mistake worth hearing
about at construction time."""

_VERSION = 1
_DOMAIN_PREFIX = b"socratic/capability-token/v1"
_TOKEN_ID_BYTES = 16
_LENGTH_BYTES = 2
_MAX_FIELD_BYTES = (1 << (8 * _LENGTH_BYTES)) - 1
_TIMESTAMP_BYTES = 8
_MAX_TIMESTAMP = (1 << (8 * _TIMESTAMP_BYTES)) - 1
_SEPARATOR = "."
_REJECTED = "capability token rejected"


class CapabilityTokenRejected(Exception):
    """A token was not accepted. **Which check failed is not in the contract.**

    Expiry, wrong session, a bad signature, a retired token and outright junk
    all raise this with the same message, so a caller — and anything a caller
    forwards to the iframe — cannot use rejection as an oracle.
    """

    def __init__(self) -> None:
        super().__init__(_REJECTED)


@dataclass(frozen=True, slots=True)
class Claims:
    """What a verified token asserts."""

    session: QuizSessionId
    learner: LearnerId
    issued_at_millis: int
    expires_at_millis: int
    token_id: str
    """The token's own randomness, hex-encoded. Identifies this token among the
    others minted for the same session; it is what rotation retires."""


class TokenMinter:
    """Mints and verifies capability tokens for one quiz service process.

    Supersession state is in-process, alongside the in-memory repositories of
    ADR-0005: a restart invalidates outstanding tokens, which fails closed and
    quietly — the behaviour ADR-0015 already accepts for a learner who returns
    hours later.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_millis: int = DEFAULT_TTL_MILLIS,
        clock: Clock = system_clock,
    ) -> None:
        """Args:
            secret: The signing key, at least `MIN_SECRET_BYTES` of it. Supplied
              by the caller — this module has no default and no fallback.
            ttl_millis: How long a minted token stays valid, in milliseconds.
            clock: Milliseconds since the epoch; injected so tests can freeze it
              (`socratic.domain.ids.Clock`).

        Raises:
            TypeError: If `secret` is not `bytes`.
            ValueError: If `secret` is too short, or `ttl_millis` is outside
              `1..MAX_TTL_MILLIS`.
        """
        if not isinstance(secret, (bytes, bytearray)):
            raise TypeError("the signing secret must be bytes")
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(
                f"the signing secret must be at least {MIN_SECRET_BYTES} bytes, "
                f"got {len(secret)}"
            )
        if not 0 < ttl_millis <= MAX_TTL_MILLIS:
            raise ValueError(
                f"a capability token's TTL must be within "
                f"1..{MAX_TTL_MILLIS} ms, got {ttl_millis}"
            )
        self._secret = bytes(secret)
        self._ttl_millis = ttl_millis
        self._clock = clock
        self._current: dict[QuizSessionId, bytes] = {}

    @property
    def ttl_millis(self) -> int:
        return self._ttl_millis

    def mint(self, session: QuizSessionId, learner: LearnerId) -> str:
        """Mints a token scoped to `session` and `learner`.

        The new token becomes the session's current one, retiring whichever
        token preceded it.
        """
        session_bytes = _field(session, "session id")
        learner_bytes = _field(learner, "learner id")
        issued_at = self._clock()
        expires_at = issued_at + self._ttl_millis
        if not 0 <= issued_at <= expires_at <= _MAX_TIMESTAMP:
            raise ValueError(f"clock reading out of range for a token: {issued_at}")
        token_id = secrets.token_bytes(_TOKEN_ID_BYTES)

        body = b"".join(
            [
                bytes([_VERSION]),
                _framed(session_bytes),
                _framed(learner_bytes),
                _framed(token_id),
                issued_at.to_bytes(_TIMESTAMP_BYTES, "big"),
                expires_at.to_bytes(_TIMESTAMP_BYTES, "big"),
            ]
        )
        self._current[session] = token_id
        return f"{_b64encode(body)}{_SEPARATOR}{_b64encode(self._sign(body))}"

    def verify(
        self, token: str, *, for_session: QuizSessionId | None = None
    ) -> Claims:
        """Verifies a token and returns what it asserts.

        Args:
            token: The token as it came off the wire.
            for_session: The session the request is addressed to, when the
              caller knows it. A token scoped to a different session is rejected
              (spec acceptance 35).

        Returns:
            The `Claims` the token carries.

        Raises:
            CapabilityTokenRejected: For every failure, with one message.
        """
        body = self._authentic_body(token)
        claims = _parse(body)
        if self._clock() >= claims.expires_at_millis:
            raise CapabilityTokenRejected()
        current = self._current.get(claims.session)
        if current is None or not hmac.compare_digest(
            current, bytes.fromhex(claims.token_id)
        ):
            raise CapabilityTokenRejected()
        if for_session is not None and not hmac.compare_digest(
            for_session.encode("utf-8"), claims.session.encode("utf-8")
        ):
            raise CapabilityTokenRejected()
        return claims

    def rotate(self, token: str) -> str:
        """Verifies `token` and issues its replacement for the same scope.

        What a grading response calls: sliding renewal riding a round trip that
        already carries the verdict. The rotated token stops verifying
        (spec acceptance 36).
        """
        claims = self.verify(token)
        return self.mint(claims.session, claims.learner)

    def _sign(self, body: bytes) -> bytes:
        return hmac.new(
            self._secret, _DOMAIN_PREFIX + body, hashlib.sha256
        ).digest()

    def _authentic_body(self, token: str) -> bytes:
        """The token's body, once its signature is proven — never before."""
        if not isinstance(token, str) or token.count(_SEPARATOR) != 1:
            raise CapabilityTokenRejected()
        encoded_body, encoded_signature = token.split(_SEPARATOR)
        body = _b64decode(encoded_body)
        signature = _b64decode(encoded_signature)
        if not hmac.compare_digest(signature, self._sign(body)):
            raise CapabilityTokenRejected()
        return body


def _field(value: str, what: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"a capability token needs a non-empty {what}")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_FIELD_BYTES:
        raise ValueError(f"{what} is too long to frame: {len(encoded)} bytes")
    return encoded


def _framed(value: bytes) -> bytes:
    """One length-prefixed field. The prefix is fixed-width, so a value cannot
    counterfeit a boundary the way a delimiter it contains would."""
    return len(value).to_bytes(_LENGTH_BYTES, "big") + value


def _parse(body: bytes) -> Claims:
    """Reads an authenticated body. Anything unexpected is still a rejection —
    the signature proves the bytes are ours, not that they are well-formed."""
    try:
        if not body or body[0] != _VERSION:
            raise CapabilityTokenRejected()
        at = 1
        fields = []
        for _ in range(3):
            length = int.from_bytes(body[at : at + _LENGTH_BYTES], "big")
            at += _LENGTH_BYTES
            field = body[at : at + length]
            if len(field) != length:
                raise CapabilityTokenRejected()
            fields.append(field)
            at += length
        session, learner, token_id = fields
        stamps = body[at : at + 2 * _TIMESTAMP_BYTES]
        if len(stamps) != 2 * _TIMESTAMP_BYTES or at + len(stamps) != len(body):
            raise CapabilityTokenRejected()
        return Claims(
            session=session.decode("utf-8"),
            learner=learner.decode("utf-8"),
            issued_at_millis=int.from_bytes(stamps[:_TIMESTAMP_BYTES], "big"),
            expires_at_millis=int.from_bytes(stamps[_TIMESTAMP_BYTES:], "big"),
            token_id=token_id.hex(),
        )
    except UnicodeDecodeError:
        raise CapabilityTokenRejected() from None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    """Strict base64url. `validate=True` so that a token with stray characters
    is rejected outright rather than silently decoding to the same bytes as
    some other token."""
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise CapabilityTokenRejected() from None
