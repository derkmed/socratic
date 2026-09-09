"""The Open WebUI Pipe (issue #12, `docs/specs/open-webui-pipe.md`).

The Pipe is **deliberately not a seam** (master spec, "Seams"): it is thin by
construction, holds no domain logic and makes no model calls. So nothing here
re-asserts authoring, grading or rendering — all of that happens on the far
side of a process boundary and is tested there.

What is tested here is exactly what only exists once the adapter exists:

* **The one crossing of the portability seam** — `__user__["id"]` becoming a
  `LearnerId` (CONTEXT: LearnerId, D3).
* **The translation either way** — an Open WebUI chat body becoming an
  `inquiry`, and the service's rendered overlay coming back out unchanged with
  `Content-Disposition: inline` (master spec §12).
* **Two absence claims**, which are only checkable by scanning the source: that
  the Pipe holds no domain logic and makes no model calls (acceptance 38), and
  that no `__event_call__` answer path exists (ADR-0015).

The Pipe lives outside `src/socratic/` because it runs in the *other*
container — it is pasted Python in Open WebUI's admin panel and cannot import
this package. That is why it is loaded here by path rather than imported.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re

import pytest

pytest.importorskip("fastapi", reason="the service extra is optional")

PIPE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[1] / "pipe" / "socratic_pipe.py"
)


def _load_pipe_module():
    """Load the Pipe from its path, the way Open WebUI loads pasted source.

    Not an import: `pipe/` is not a package and is deliberately outside the
    wheel, because shipping Open WebUI's adapter inside the quiz service's
    image is the boundary ADR-0015 exists to make physical.
    """
    spec = importlib.util.spec_from_file_location("socratic_pipe", PIPE_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipe_module():
    return _load_pipe_module()


class TestTheLearnerId:
    """`__user__` maps to a `LearnerId` at the Pipe boundary, and that is the
    only thing that crosses the portability seam (CONTEXT, D3)."""

    def test_the_learner_id_is_the_open_webui_user_id(self, pipe_module):
        assert pipe_module._learner_id({"id": "u-ada", "name": "Ada"}) == "u-ada"

    @pytest.mark.parametrize(
        "user", [None, {}, {"id": ""}, {"id": "   "}, {"name": "Ada"}, "u-ada"]
    )
    def test_a_missing_learner_id_is_refused(self, pipe_module, user):
        # Everything is partitioned on `LearnerId` (CONTEXT), so an absent one
        # is a broken host contract, not a case to invent an anonymous learner
        # for.
        with pytest.raises(ValueError):
            pipe_module._learner_id(user)

    def test_the_learner_id_is_stripped_of_surrounding_whitespace(
        self, pipe_module
    ):
        assert pipe_module._learner_id({"id": " u-ada "}) == "u-ada"


class TestTheInquiry:
    """The learner's question, taken out of an Open WebUI chat body.

    The last *user* message, because that is the turn the Pipe was invoked
    for; earlier turns are history and any assistant turn is ours.
    """

    def test_the_inquiry_is_the_last_user_message(self, pipe_module):
        body = {
            "messages": [
                {"role": "user", "content": "Why is the sky blue?"},
                {"role": "assistant", "content": "<the overlay>"},
                {"role": "user", "content": "Why does heat flow?"},
            ]
        }

        assert pipe_module._inquiry(body) == "Why does heat flow?"

    def test_typed_content_parts_are_joined_in_order(self, pipe_module):
        # Open WebUI sends `content` either as a string or as a list of typed
        # parts once anything multimodal is attached. The text parts are
        # fragments of one message, so they concatenate; everything that is
        # not text is not an inquiry and is dropped.
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Why does "},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                        {"type": "text", "text": "heat flow?"},
                    ],
                }
            ]
        }

        assert pipe_module._inquiry(body) == "Why does heat flow?"

    def test_the_inquiry_is_stripped(self, pipe_module):
        body = {"messages": [{"role": "user", "content": "  Why?  "}]}

        assert pipe_module._inquiry(body) == "Why?"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"messages": []},
            {"messages": [{"role": "assistant", "content": "hello"}]},
            {"messages": [{"role": "user", "content": "   "}]},
            {"messages": [{"role": "user", "content": []}]},
            {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
        ],
    )
    def test_an_empty_inquiry_is_refused(self, pipe_module, body):
        # `AuthorRequest.inquiry` is `min_length=1`, so this would come back as
        # a 422 from a round trip. Refusing locally spends no request and says
        # something the learner can act on.
        with pytest.raises(ValueError):
            pipe_module._inquiry(body)


# --- The round trip ----------------------------------------------------------

SERVICE_URL = "http://quiz-service:8080"
SERVICE_TOKEN = "service-token-for-the-pipe"
OVERLAY = "<!doctype html><title>quiz</title><p>Heat flows because ∫</p>"

BODY = {"messages": [{"role": "user", "content": "Why does heat flow?"}]}
USER = {"id": "u-ada", "name": "Ada", "role": "user"}


class Reply:
    """What the injected transport returns, shaped like `httpx.Response`."""

    def __init__(self, status_code: int = 200, text: str = OVERLAY) -> None:
        self.status_code = status_code
        self.text = text


class RecordingTransport:
    """The one injected collaborator: every call recorded, nothing sent.

    `Pipe()` takes no arguments in Open WebUI, so this hangs off a keyword
    that defaults to the real client — invisible in production, and the only
    thing that makes the adapter assertable without a live service.
    """

    def __init__(self, reply=None, raises: Exception | None = None) -> None:
        self.reply = reply or Reply()
        self.raises = raises
        self.calls: list[dict] = []

    async def __call__(self, url, *, json, headers, timeout):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return self.reply

    def assert_never_called(self) -> None:
        assert self.calls == [], f"the service was called: {self.calls}"


class RecordingEmitter:
    """Open WebUI's `__event_emitter__`, recorded rather than dispatched."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.events: list[dict] = []

    async def __call__(self, event):
        self.events.append(event)
        if self.raises is not None:
            raise self.raises


def make_pipe(pipe_module, transport):
    pipe = pipe_module.Pipe(post=transport)
    pipe.valves.service_url = SERVICE_URL
    pipe.valves.service_token = SERVICE_TOKEN
    return pipe


def run(pipe, **kwargs):
    import asyncio

    # The host hands every real turn an emitter, so the default belongs here
    # rather than in each test. Pass one explicitly to assert on it.
    kwargs.setdefault("__event_emitter__", RecordingEmitter())
    return asyncio.run(pipe.pipe(body=BODY, __user__=USER, **kwargs))


class TestTheRoundTrip:
    def test_the_request_names_the_learner_the_inquiry_and_the_mode(
        self, pipe_module
    ):
        transport = RecordingTransport()

        run(make_pipe(pipe_module, transport))

        call = transport.calls[0]
        assert call["url"] == f"{SERVICE_URL}/overlays"
        assert call["json"] == {
            "learner_id": "u-ada",
            "inquiry": "Why does heat flow?",
            "mode": "novice",
        }

    def test_the_cadence_is_omitted_so_the_domain_keeps_its_default(
        self, pipe_module
    ):
        # `AuthorRequest.probe_cadence` is optional and the domain owns the
        # default, exactly as `app.py::_cadence` has it. Per-learner cadence is
        # #14's `UserValves`, not this ticket's.
        transport = RecordingTransport()

        run(make_pipe(pipe_module, transport))

        assert "probe_cadence" not in transport.calls[0]["json"]

    def test_the_request_carries_the_service_token_and_not_a_learner_one(
        self, pipe_module
    ):
        # Two credentials, deliberately on different headers
        # (`docs/specs/quiz-service.md`): the Pipe holds the service token, and
        # the learner's capability token is minted into the overlay by the
        # service and never passes through here.
        transport = RecordingTransport()

        run(make_pipe(pipe_module, transport))

        headers = transport.calls[0]["headers"]
        assert headers["X-Socratic-Service-Token"] == SERVICE_TOKEN
        assert "X-Socratic-Token" not in headers


class TestTheOverlayReachesTheChatAsAnEmbed:
    """#116: a Pipe's *return value* cannot carry an overlay.

    Open WebUI's Pipe path dispatches on `str`, `dict`, `BaseModel`,
    `StreamingResponse`, `Iterator` and `AsyncGenerator` — an `HTMLResponse`
    matches none of them, so it fell through every branch and the learner got
    an empty message with nothing logged anywhere. The iframe render lives on
    the `embeds` event instead, which a Pipe reaches through
    `__event_emitter__`, and which the frontend renders verbatim as `srcdoc`.
    """

    def test_the_overlay_is_emitted_as_an_embeds_event_verbatim(
        self, pipe_module
    ):
        transport = RecordingTransport()
        emitter = RecordingEmitter()

        run(make_pipe(pipe_module, transport), __event_emitter__=emitter)

        assert len(emitter.events) == 1
        event = emitter.events[0]
        assert event["type"] == "embeds"
        # Byte-identical, for the same reason the return value used to be:
        # the service sanitised it and the Pipe holds no domain logic.
        assert event["data"]["embeds"] == [OVERLAY]

    def test_the_embed_replaces_rather_than_accumulates(self, pipe_module):
        # Without `replace`, `socket/main.py` extends the message's existing
        # embeds, so a re-run would stack a second quiz under the first.
        transport = RecordingTransport()
        emitter = RecordingEmitter()

        run(make_pipe(pipe_module, transport), __event_emitter__=emitter)

        assert emitter.events[0]["data"]["replace"] is True

    def test_it_returns_a_string_so_the_stream_terminates(self, pipe_module):
        # The return value is no longer the payload, but it still has to be a
        # type the Pipe path understands. It carries no overlay text: the
        # quiz is in the frame, and reprinting it in the transcript is what
        # ADR-0001 rules out.
        transport = RecordingTransport()
        emitter = RecordingEmitter()

        returned = run(
            make_pipe(pipe_module, transport), __event_emitter__=emitter
        )

        assert isinstance(returned, str)
        assert OVERLAY not in returned

    def test_a_host_without_an_emitter_is_told_so_and_not_left_blank(
        self, pipe_module
    ):
        # An empty message with no explanation is the exact failure #116
        # describes. If the host hands us no emitter there is no way to show
        # a quiz, so say that rather than reproduce the silence.
        import asyncio

        transport = RecordingTransport()
        pipe = make_pipe(pipe_module, transport)

        message = asyncio.run(pipe.pipe(body=BODY, __user__=USER))

        assert isinstance(message, str)
        assert message.strip() != ""
        assert SERVICE_TOKEN not in message
        transport.assert_never_called()

    def test_an_emitter_that_raises_becomes_a_plain_message(self, pipe_module):
        transport = RecordingTransport()
        emitter = RecordingEmitter(raises=RuntimeError(SERVICE_TOKEN))

        message = run(
            make_pipe(pipe_module, transport), __event_emitter__=emitter
        )

        assert isinstance(message, str)
        assert SERVICE_TOKEN not in message
        assert "Traceback" not in message


class TestTheFailurePaths:
    """Every refusal is a plain string, which Open WebUI renders as chat text.

    Never a traceback, and never the service token: the Pipe holds a
    credential, and a Pipe that prints its own exception into a chat window
    prints its Valves.
    """

    def test_an_unidentified_learner_is_refused_before_any_request(
        self, pipe_module
    ):
        transport = RecordingTransport()
        pipe = make_pipe(pipe_module, transport)

        import asyncio

        message = asyncio.run(pipe.pipe(body=BODY, __user__={}))

        assert isinstance(message, str)
        assert SERVICE_TOKEN not in message
        transport.assert_never_called()

    def test_an_empty_inquiry_is_refused_before_any_request(self, pipe_module):
        transport = RecordingTransport()
        pipe = make_pipe(pipe_module, transport)

        import asyncio

        message = asyncio.run(pipe.pipe(body={"messages": []}, __user__=USER))

        assert isinstance(message, str)
        transport.assert_never_called()

    @pytest.mark.parametrize("status", [401, 422, 500])
    def test_a_refusal_from_the_service_becomes_a_plain_message(
        self, pipe_module, status
    ):
        transport = RecordingTransport(
            reply=Reply(status_code=status, text="<html>details</html>")
        )

        message = run(make_pipe(pipe_module, transport))

        assert isinstance(message, str)
        assert str(status) in message
        assert SERVICE_TOKEN not in message

    def test_a_transport_failure_becomes_a_plain_message(self, pipe_module):
        transport = RecordingTransport(
            raises=RuntimeError(f"connect failed to {SERVICE_URL} {SERVICE_TOKEN}")
        )

        message = run(make_pipe(pipe_module, transport))

        assert isinstance(message, str)
        assert SERVICE_TOKEN not in message
        assert "Traceback" not in message


class TestOpenWebUIsOwnTaskCalls:
    """Open WebUI calls the selected model for its own housekeeping — title
    and tag generation — naming the task in the request metadata. Authoring a
    quiz for one would spend a model call and leave a stray attempt behind
    (D11: every interaction is at most one blocking call), so a named task is
    answered locally and never reaches the service."""

    def test_a_named_task_is_answered_without_calling_the_service(
        self, pipe_module
    ):
        transport = RecordingTransport()

        result = run(
            make_pipe(pipe_module, transport),
            __metadata__={"task": "title_generation"},
        )

        assert isinstance(result, str)
        transport.assert_never_called()

    def test_an_ordinary_turn_names_no_task_and_is_authored(self, pipe_module):
        transport = RecordingTransport()
        emitter = RecordingEmitter()

        run(
            make_pipe(pipe_module, transport),
            __metadata__={"chat_id": "c-1"},
            __event_emitter__=emitter,
        )

        assert emitter.events[0]["data"]["embeds"] == [OVERLAY]
        assert len(transport.calls) == 1


# --- The absence claims ------------------------------------------------------
#
# "Thin by construction" and "no `__event_call__` answer path" are claims about
# what is *not* in the file, and absence is only checkable by scanning. This
# follows `test_import_hygiene.py`, which enforces the seam from the other
# side of the process boundary.


@pytest.fixture(scope="module")
def pipe_source() -> str:
    return PIPE_SOURCE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pipe_tree(pipe_source: str) -> ast.Module:
    return ast.parse(pipe_source)


def _code_strings(tree: ast.Module) -> list[str]:
    """Every identifier and string literal in the file, documentation aside.

    Documentation is excluded on purpose: the Pipe's prose necessarily *names*
    the things it must not do — the answer key it never sees, the
    `__event_call__` path it must never grow — and a scan over the raw text
    would either fail on the explanation or force the explanation out. What is
    scanned is what executes.

    "Documentation" is a docstring, or a bare string statement, which is how
    this file annotates its module constants.
    """
    documentation = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            documentation.add(node.value.value)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            found.append(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in documentation:
                found.append(node.value)
    return found


def _imported_roots(tree: ast.Module) -> dict[str, bool]:
    """Top-level package each import names, and whether it is at module scope."""
    module_scope = {node for node in tree.body}
    found: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            found[root] = found.get(root, False) or node in module_scope
    return found


class TestTheFileIsSelfContained:
    def test_it_imports_nothing_but_the_standard_library_and_three_packages(
        self, pipe_tree
    ):
        # It is pasted Python in the Open WebUI container: `socratic` is not
        # installed there, and by ADR-0015 the seam is a process boundary
        # rather than a convention. `fastapi`, `pydantic` and `httpx` are what
        # that container already has.
        import sys

        allowed = set(sys.stdlib_module_names) | {"fastapi", "pydantic", "httpx"}
        strays = sorted(set(_imported_roots(pipe_tree)) - allowed)

        assert strays == [], f"the Pipe reached outside its container: {strays}"

    @pytest.mark.parametrize("forbidden", ["socratic", "anthropic", "open_webui"])
    def test_it_imports_neither_the_domain_nor_the_sdk_nor_the_host(
        self, pipe_tree, forbidden
    ):
        assert forbidden not in _imported_roots(pipe_tree)

    def test_httpx_is_imported_lazily(self, pipe_tree):
        # So the two translations stay importable, and testable, with nothing
        # installed but the framework.
        assert _imported_roots(pipe_tree).get("httpx") is False


class TestItHoldsNoDomainLogicAndMakesNoModelCalls:
    """Master spec acceptance 38.

    Scanned over identifiers and string literals rather than over the whole
    text, so the prose above — which necessarily *names* the things the Pipe
    does not do — cannot make the test vacuous or make it fail.
    """

    KEY_VOCABULARY = (
        "rubric",
        "correct_option",
        "reinforcement",
        "hint",
        "ladder",
        "verdict",
        "grade",
        "grading",
        "probe",
        "seal",
        "attempt",
    )

    ANSWER_PATHS = ("/answers", "/probes", "/ratings")

    def test_the_answer_key_vocabulary_appears_nowhere_in_its_code(
        self, pipe_tree
    ):
        # The key never leaves the backend (D3, ADR-0003) and the ladder,
        # cadence and sealing are the domain's. A Pipe that so much as names
        # them has started to hold logic.
        strings = [text.lower() for text in _code_strings(pipe_tree)]
        offenders = sorted(
            {
                term
                for term in self.KEY_VOCABULARY
                for text in strings
                if term in text
            }
        )

        assert offenders == [], f"the Pipe named domain vocabulary: {offenders}"

    def test_it_calls_no_route_but_the_overlay_one(self, pipe_tree):
        # Answers, probes and ratings go from the *iframe* straight to the
        # service (D15). The Pipe is called once, to start a quiz.
        strings = _code_strings(pipe_tree)
        offenders = sorted(
            {path for path in self.ANSWER_PATHS for text in strings if path in text}
        )

        assert offenders == [], f"the Pipe reached an answer route: {offenders}"
        assert any("/overlays" in text for text in strings)

    def test_it_names_no_model_and_no_model_api(self, pipe_source):
        lowered = pipe_source.lower()
        for term in ("api.anthropic.com", "messages.create", "claude-"):
            assert term not in lowered, term


class TestThereIsNoEventCallAnswerPath:
    """ADR-0015: `__event_call__` is a server-side Socket.IO call rendering a
    modal in the parent page. The sandboxed iframe cannot invoke it, so it is
    a different architecture rather than a degraded path — and the issue is
    explicit that one must not be built."""

    def test_no_executing_line_names_it(self, pipe_tree):
        # The file's prose says at length why this path must never be built,
        # which is documentation rather than an answer path — so the scan is
        # over what executes, as above.
        assert "__event_call__" not in _code_strings(pipe_tree)

    def test_pipe_does_not_accept_it_as_a_parameter(self, pipe_module):
        # `__event_emitter__` is deliberately *not* covered by this rule. The
        # two are different mechanisms: `__event_call__` blocks the coroutine
        # on a modal in the parent page, which is the architecture ADR-0015
        # rules out, while the emitter is a one-way sink the overlay rides to
        # the frame it renders in (#116).
        import inspect

        parameters = inspect.signature(pipe_module.Pipe.pipe).parameters

        assert "__event_call__" not in parameters
        assert "__event_emitter__" in parameters


class TestTheReadmeStatesTheConstraint:
    """The last acceptance criterion: hardened deployments are out of scope,
    and the README says so and says why (CONTEXT: Default install)."""

    def test_the_readme_names_the_constraint_and_its_reason(self):
        readme = (
            PIPE_SOURCE.parents[1] / "README.md"
        ).read_text(encoding="utf-8")

        assert "IFRAME_CSP" in readme
        assert re.search(r"hardened", readme, re.IGNORECASE)
        assert re.search(r"default install", readme, re.IGNORECASE)
