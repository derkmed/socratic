# TokenMinter — HMAC capability tokens with TTL and rotation

## Goal

The **capability token** (CONTEXT) that authorizes a request from the quiz
iframe to the **quiz service**. The iframe is sandboxed without
`allow-same-origin`, so it has an opaque origin, carries no cookie and no Open
WebUI token, and cannot authenticate by any ambient means (D15,
[ADR-0015](../adr/0015-iframe-pipe-transport.md)). This slice builds the minter
and its verification only: `TokenMinter.mint(session, learner)` and
`.verify(token) -> Claims`, HMAC-signed, scoped to one `QuizSessionId` and its
`LearnerId`, with a short TTL and per-response rotation.

## Seams

One, already named in the umbrella spec's Seams table:

| Seam | Kind | Why it must exist |
|---|---|---|
| `TokenMinter.mint(session, learner) / .verify(token) -> Claims` | existing (spec Seams table) | Expiry, wrong-session rejection and per-response rotation are acceptance conditions (35, 36) and cannot be asserted through the HTTP layer without standing a server up. A pure function over a `Clock`, so a frozen clock tests all three. |

No new seam is introduced. The `Clock` seam is the one already in
`socratic.domain.ids` — the TTL is measured with it rather than with a second
clock abstraction.

## Decisions

- **D15 / [ADR-0015](../adr/0015-iframe-pipe-transport.md)** — requests from the
  iframe are authorized by an HMAC capability token, scoped to one
  `QuizSessionId` and its `LearnerId`, short TTL, minted into the `srcdoc` at
  render time; **every grading response returns a fresh token** which the iframe
  swaps in. Sliding renewal, exposure window of minutes.
- **D7 / [ADR-0007](../adr/0007-quiz-session-identity.md)**, CONTEXT:
  *QuizSessionId* — the session id is **not** a credential: timestamp-prefixed,
  near-monotonic, stamped through the audit trail. That is why this seam exists
  separately from it, and why the token carries unguessable randomness of its
  own.
- **CONTEXT: Portability seam** — stdlib only. `hmac`, `hashlib`, `base64`,
  `secrets`; no third-party crypto, nothing from the host.
- **Umbrella spec, Out of scope** — the token authorizes iframe→service
  requests; it is **not a login**. Authentication stays with Open WebUI.

## Approach

`src/socratic/domain/tokens.py`, one module:

1. `LearnerId = str` — provisional, defined here because nothing on `main`
   defines it yet, following the `QuizSessionId` precedent in `ids.py`.
2. `Claims` — a frozen dataclass: `session`, `learner`, `issued_at_millis`,
   `expires_at_millis`, `token_id`.
3. `CapabilityTokenRejected` — one exception, one constant message. Which check
   failed is not part of the caller's contract.
4. `TokenMinter(secret, *, ttl_millis=DEFAULT_TTL_MILLIS, clock=system_clock)`:
   - `mint(session, learner) -> str` — mints a fresh `token_id` and records it
     as the session's current one, superseding any predecessor.
   - `verify(token, *, for_session=None) -> Claims` — signature, then expiry,
     then supersession, then the optional session scope.
   - `rotate(token) -> str` — verify, then mint for the same session and
     learner. What a grading response calls.
5. **Serialization.** The signed body is a length-prefixed frame, not a
   delimited string: each variable-length field is written as a fixed-width
   2-byte big-endian length followed by its bytes, and the two timestamps are
   fixed 8-byte fields. No field value can manufacture a boundary, so
   `("a|b", "c")` and `("a", "b|c")` cannot collide. The MAC is taken over a
   constant domain-separation prefix concatenated with that frame. The wire
   form is `base64url(body) + "." + base64url(mac)`, and `.` is outside the
   base64url alphabet.

## Out of scope

- **Wiring into request handling.** The quiz service's HTTP layer is its own
  ticket; nothing here parses a request or reads a header.
- **Rendering the token into the `srcdoc`.** Belongs to the renderer.
- **Explicit revocation / logout.** Rotation supersedes; there is no `revoke`.
- **Cross-process token validity.** Supersession state is in-process, alongside
  the in-memory repositories of ADR-0005: a service restart invalidates
  outstanding tokens, which fails closed.
- **Key rotation of the HMAC secret itself,** and any key-management story. The
  secret arrives as a parameter.
- **A `LearnerId` that is more than a `str` alias.** Provisional until a ticket
  owns learner identity.

## Acceptance

Restating the issue's criteria (spec acceptance 35 and 36):

1. A minted token verifies and yields `Claims` naming its `QuizSessionId` and
   `LearnerId`.
2. A token past its TTL is rejected, against a frozen clock (35).
3. A token scoped to a different session is rejected (35).
4. A tampered token, and one signed with a different secret, are rejected.
5. Rotation issues a fresh token and the previous one stops verifying (36).
6. The TTL defaults to minutes, is configurable, and a TTL beyond the
   documented ceiling is refused.
7. No test needs a running HTTP server.
