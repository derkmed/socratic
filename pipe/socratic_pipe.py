"""The Open WebUI Pipe: the adapter, and nothing else.

Paste this file into Open WebUI's admin panel (Workspace → Functions → new
Pipe). It runs in the **Open WebUI container**, on the far side of the
portability seam, which is why it is a single self-contained file that imports
nothing from `socratic`: that package is not installed there, and by ADR-0015
the seam is a process boundary rather than a convention.

What it does, in full:

    __user__["id"]  ->  LearnerId
    the last user message  ->  inquiry
    POST {service}/overlays  ->  the overlay the quiz service rendered
    that HTML, verbatim, with `Content-Disposition: inline`

Open WebUI displays what a function returns as an `HTMLResponse` in a sandboxed
`srcdoc` iframe. From inside that iframe the overlay's own script `fetch`es the
quiz service directly for every answer, probe and rating — measured in Chrome
and Edge, and preflighted (`docs/research/open-webui-fit.md`). **None of that
traffic passes through here.** The Pipe is called once, to start a quiz.

It **holds no domain logic and makes no model calls** (master spec acceptance
38). Authoring, grading, the hint ladder, the answer key, sanitising and the
capability token all live in the quiz service. Everything below is transport
and two dictionary lookups.

There is no `__event_call__` answer path and there must never be one
(ADR-0015): it is a server-side Socket.IO call that renders a modal in the
*parent* page, which the sandboxed iframe cannot invoke. It is a different
architecture, not a degraded one.

This targets a **default** Open WebUI install (`IFRAME_CSP` unset). Under the
hardening docs' recommended CSP the iframe has no network path to the service
at all — see the README.
"""

import os
from typing import Any

from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

SERVICE_TOKEN_HEADER = "X-Socratic-Service-Token"
"""The Pipe's credential. The learner's capability token rides a different
header and is minted into the overlay by the service — it never passes
through here (`docs/specs/quiz-service.md`)."""

OVERLAY_PATH = "/overlays"

MISSING_LEARNER = (
    "Open WebUI did not identify the learner, so there is nothing to "
    "partition this quiz on."
)

EMPTY_INQUIRY = "Ask a question and you will get a quiz about it."

SERVICE_REFUSED = (
    "The quiz service could not author a quiz (HTTP {status}). Check that it "
    "is running and that this Pipe's service token matches its "
    "SOCRATIC_SERVICE_TOKEN."
)

SERVICE_UNREACHABLE = (
    "The quiz service could not be reached at {url}. Check that the container "
    "is running and that this Pipe's service URL names it."
)

TASK_ACKNOWLEDGED = "Socratic"
"""What Open WebUI's own housekeeping calls get back.

Title and tag generation invoke the selected model, which is this Pipe. Each
one would otherwise author a whole quiz — a model call and a stray attempt for
a string the learner never sees."""


def _learner_id(user: Any) -> str:
    """Map Open WebUI's `__user__` to our `LearnerId`.

    The one crossing of the portability seam (CONTEXT: LearnerId, D3). After
    this line the host is never mentioned again.

    Args:
      user: Open WebUI's `__user__` dict for the authenticated learner.

    Returns:
      The `LearnerId`, stripped.

    Raises:
      ValueError: There is no usable id. Everything downstream is partitioned
        on `LearnerId`, so an absent one is a broken host contract rather than
        a case to invent an anonymous learner for.
    """
    identifier = user.get("id") if isinstance(user, dict) else None
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(MISSING_LEARNER)
    return identifier.strip()


def _text_of(content: Any) -> str:
    """The text of one chat message, whichever shape it arrived in.

    Open WebUI sends `content` as a plain string, or as a list of typed parts
    once anything multimodal is attached. The text parts are fragments of a
    single message, so they concatenate in order; an image part carries no
    inquiry and is dropped.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _inquiry(body: Any) -> str:
    """The learner's question: the last `user` message in the chat body.

    The last one, because that is the turn this invocation is for. Earlier
    turns are history, and any assistant turn is an overlay we returned.

    Raises:
      ValueError: There is no user message with text in it. The service's
        `AuthorRequest` requires a non-empty inquiry, so this would come back
        as a 422 after a round trip; refusing here spends no request and says
        something the learner can act on.
    """
    messages = body.get("messages") if isinstance(body, dict) else None
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text_of(message.get("content")).strip()
        if text:
            return text
    raise ValueError(EMPTY_INQUIRY)


async def _post(url: str, *, json: dict, headers: dict, timeout: float):
    """The real transport: one JSON `POST`, server to server.

    `httpx` is imported here rather than at module scope so the two
    translations above stay importable — and testable — with nothing installed
    but the framework. Open WebUI's own container ships `httpx` and recommends
    it for exactly this.
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=json, headers=headers)


class Pipe:
    """The adapter Open WebUI loads.

    Open WebUI instantiates this with no arguments and calls `pipe` once per
    learner turn. `post` exists only so the suite can drive the adapter
    without a live service; in production it is the module function above.
    """

    class Valves(BaseModel):
        """Admin-level settings, persisted by Open WebUI.

        Defaulted from the environment `compose.yaml` already sets on the
        `open-webui` container, so a default install needs no clicking.

        Per-*learner* settings — difficulty mode and probe cadence — are
        `UserValves` and belong to issue #14. This class is deliberately only
        the admin half.
        """

        service_url: str = Field(
            default_factory=lambda: os.getenv(
                "SOCRATIC_SERVICE_URL", "http://quiz-service:8080"
            ),
            description=(
                "Where the Pipe reaches the quiz service, from inside the "
                "compose network. Not where the learner's browser reaches it: "
                "the overlay is given that origin by the service itself."
            ),
        )
        service_token: str = Field(
            default_factory=lambda: os.getenv("SOCRATIC_SERVICE_TOKEN", ""),
            description=(
                "The Pipe's credential, matching the service's "
                "SOCRATIC_SERVICE_TOKEN. Not a learner's capability token."
            ),
        )
        mode: str = Field(
            default_factory=lambda: os.getenv("SOCRATIC_MODE", "novice"),
            description=(
                "The difficulty mode to author in: novice or advanced. An "
                "install-wide default until per-learner settings land (#14)."
            ),
        )
        timeout_seconds: float = Field(
            default=120.0,
            description=(
                "How long to wait for authoring, which makes a model call."
            ),
        )

    def __init__(self, post=None) -> None:
        self.valves = self.Valves()
        self._post = post or _post

    async def pipe(
        self,
        body: dict,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
    ) -> Any:
        """One learner turn: a question in, an overlay out.

        Returns:
          An `HTMLResponse` carrying the overlay the quiz service rendered,
          which Open WebUI displays in its sandboxed `srcdoc` iframe. On any
          refusal, a plain string, which it renders as chat text instead.
        """
        if (__metadata__ or {}).get("task"):
            return TASK_ACKNOWLEDGED

        try:
            learner_id = _learner_id(__user__)
            inquiry = _inquiry(body)
        except ValueError as error:
            return str(error)

        url = self.valves.service_url.rstrip("/") + OVERLAY_PATH
        try:
            reply = await self._post(
                url,
                json={
                    "learner_id": learner_id,
                    "inquiry": inquiry,
                    "mode": self.valves.mode,
                },
                headers={SERVICE_TOKEN_HEADER: self.valves.service_token},
                timeout=self.valves.timeout_seconds,
            )
        except Exception:
            # Deliberately broad, and deliberately discarding the exception:
            # what a transport error carries is the URL, the headers it tried
            # and sometimes the request itself. This one returns a chat
            # message, and a chat message must not be able to print a
            # credential.
            return SERVICE_UNREACHABLE.format(url=self.valves.service_url)

        if reply.status_code != 200:
            return SERVICE_REFUSED.format(status=reply.status_code)

        # Verbatim. Everything in it was rendered and sanitised in the service
        # before it left (ADR-0012), and the Pipe holds no domain logic to
        # apply to it (acceptance 38).
        return HTMLResponse(
            content=reply.text,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": "inline"},
        )
