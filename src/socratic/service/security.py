"""The two credentials, in one place each.

`TokenMinter.verify` takes `for_session` as an **optional** keyword. A handler
that omits it verifies only the signature and the expiry, and so accepts any
live token, for any session, signed by the same secret — a learner's token
would grade another learner's quiz. #18's agent flagged this explicitly.

`authorize_capability` is the only place in the service that calls `verify`,
and it cannot be called without naming the session the request is addressed
to. That is the whole reason it exists: the mistake is per-handler, so the fix
has to be a single function that every handler must go through.

The session id in the request body is **not** a credential (D7, ADR-0007) —
it is timestamp-prefixed and stamped through the audit trail. It is here as
the thing the token is checked *against*, so that a token minted for session A
is refused on a request addressed to session B. Verifying a token against the
session named in its own claims would be circular and prove nothing.
"""

import hmac

from fastapi import Header, HTTPException, status

from socratic.domain.ids import QuizSessionId
from socratic.domain.tokens import CapabilityTokenRejected, Claims
from socratic.service.deps import ServiceDependencies

UNAUTHORIZED = "unauthorized"
"""One constant body. Which check failed is not the caller's business — it
would tell a prober whether a session exists, whether a token is merely
expired, or whether the secret is right."""


def _refuse() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHORIZED
    )


def authorize_capability(
    deps: ServiceDependencies,
    token: str | None,
    for_session: QuizSessionId,
) -> Claims:
    """Verify an iframe's capability token against the session it addresses.

    `for_session` is positional-or-keyword but has no default, so it cannot be
    forgotten the way `verify`'s can.
    """
    if not token:
        raise _refuse()
    try:
        return deps.minter.verify(token, for_session=for_session)
    except CapabilityTokenRejected:
        raise _refuse() from None


def require_service_token(
    deps: ServiceDependencies,
    presented: str | None,
) -> None:
    """The Pipe's credential for the authoring route.

    Compared with `hmac.compare_digest` so the comparison does not leak the
    secret's prefix by timing. A capability token presented here fails this
    check like any other wrong string: the two credentials are not
    interchangeable, and a learner's token must not spend model budget.
    """
    if not presented or not hmac.compare_digest(presented, deps.service_token):
        raise _refuse()


def capability_header(
    x_socratic_token: str | None = Header(default=None),
) -> str | None:
    """The iframe's token. A header, not a cookie: the iframe is sandboxed
    without `allow-same-origin`, so it has an opaque origin and carries no
    ambient credential at all (D15)."""
    return x_socratic_token


def service_header(
    x_socratic_service_token: str | None = Header(default=None),
) -> str | None:
    """The Pipe's token, on a different header from the learner's so neither
    can be presented where the other is expected."""
    return x_socratic_service_token
