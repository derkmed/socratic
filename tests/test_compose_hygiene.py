"""Every `SOCRATIC_*` variable in `compose.yaml` is read by the container it is set on.

A compose variable nothing reads is worse than clutter, because it does not look
like clutter. It looks like the knob you turn. `SOCRATIC_SERVICE_URL_FOR_BROWSER`
sat on the `open-webui` service and had no effect at all (#94): the overlay's
browser-facing origin is the *service's* own `SOCRATIC_PUBLIC_URL`, in the other
container, rendered into the document rather than accepted on a request (#13,
D15 — `socratic.service.config.PUBLIC_URL_VAR`). Someone republishing the
service on a different host port would have changed the dead one and found the
overlay still fetching `http://localhost:8080`, with the only symptom in a
browser console inside a sandboxed iframe.

The rule here is the one the compose file is already implicitly asserting: a
variable set on a container is read by the code that runs in that container.
`quiz-service` runs the image built from the `Dockerfile`, which is
`src/socratic/`; `open-webui` runs the Pipe, which is `pipe/`. That per-container
form also catches the neighbouring mistake — a service variable set on the host
container, where it would be equally inert.

Scoped to the `SOCRATIC_` prefix on purpose. `ANTHROPIC_API_KEY` is read by the
SDK rather than by anything here, and would need an allowlist entry under a
broader rule; confined to the names this project coined, the guard is a
self-consistency claim with no allowlist to keep up to date.

The scan reads indentation rather than importing a YAML parser: the package
keeps `dependencies = []` as a portability seam (ADR-0002) and the `dev` extra
is three packages, so a parser earned by one hygiene test is a poor trade. The
scan is a seam of its own for that reason — `_compose_environment` takes text,
and the fixtures below cover the two shapes that could fool it.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

COMPOSE = REPO_ROOT / "compose.yaml"

PREFIX = "SOCRATIC_"
"""The names this project coined, and so the names it owes a reader."""

READERS = {
    "quiz-service": ("src/socratic",),
    "open-webui": ("pipe",),
}
"""Which source tree runs in which container.

`quiz-service` is the `Dockerfile` image, which installs `src/socratic` and runs
`python -m socratic.service`. `open-webui` is the stock upstream image with the
Pipe loaded into it; Open WebUI's own configuration is not this project's to
police, which is what the `SOCRATIC_` prefix confines the guard to.
"""


def _compose_environment(text: str) -> dict[str, set[str]]:
    """Map each service to the `SOCRATIC_*` variable names set on it.

    Indentation-driven, and deliberately narrow: it collects keys only inside an
    `environment:` mapping inside a service inside `services:`. Two shapes have
    to be got right or the guard reports nonsense. A `ports:` sequence sits at
    the same depth as `environment:` and its entries at the same depth as the
    variables, so leaving the environment mapping has to be detected by the key
    that opens the next block rather than by depth alone. And a value written
    `${SOCRATIC_TOKEN_SECRET:?...}` names a *host* variable compose substitutes
    itself, on the right of the colon; only the left side is a key this guard
    has any claim over.
    """
    found: dict[str, set[str]] = {}
    in_services = False
    services_indent = 0
    service: str | None = None
    service_indent = 0
    env_indent: int | None = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if not in_services:
            if stripped == "services:":
                in_services = True
                services_indent = indent
            continue

        if indent <= services_indent:
            # A sibling of `services:` — the top-level `volumes:`, say.
            in_services = False
            service = None
            env_indent = None
            continue

        if service is None or indent <= service_indent:
            if stripped.endswith(":"):
                service = stripped[:-1]
                service_indent = indent
                found.setdefault(service, set())
                env_indent = None
            continue

        if env_indent is None or indent <= env_indent:
            # A key of the service itself: `environment:` opens the block this
            # guard reads, anything else closes it.
            env_indent = indent if stripped == "environment:" else None
            continue

        key = stripped.split(":", 1)[0].strip()
        if key.startswith(PREFIX):
            found[service].add(key)

    return found


FIXTURE = """\
services:
  quiz-service:
    build: .
    environment:
      SOCRATIC_TOKEN_SECRET: ${SOCRATIC_FROM_THE_HOST:?a message with: a colon}
      SOCRATIC_PUBLIC_URL: http://localhost:8080
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:?set it}
    ports:
      - "8080:8080"

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    depends_on:
      - quiz-service
    environment:
      SOCRATIC_SERVICE_URL: http://quiz-service:8080

volumes:
  open-webui-data:
"""


def test_the_scan_reads_the_variables_of_each_service():
    assert _compose_environment(FIXTURE) == {
        "quiz-service": {"SOCRATIC_TOKEN_SECRET", "SOCRATIC_PUBLIC_URL"},
        "open-webui": {"SOCRATIC_SERVICE_URL"},
    }


def test_the_scan_does_not_mistake_a_host_substitution_for_a_key():
    # `${SOCRATIC_FROM_THE_HOST:?...}` is compose's own substitution, on the
    # value side. Reading it as a key would have the guard demand that the
    # source read a name that is not set on any container at all.
    variables = _compose_environment(FIXTURE)

    assert "SOCRATIC_FROM_THE_HOST" not in variables["quiz-service"]


def test_the_scan_stops_at_the_end_of_the_environment_block():
    # `ports:` and its entries sit at exactly the depths `environment:` and its
    # variables do; only the key that opens the block distinguishes them.
    text = FIXTURE.replace(
        '      - "8080:8080"',
        '      - "8080:8080"\n      - "SOCRATIC_NOT_A_VARIABLE:1"',
    )

    assert _compose_environment(text)["quiz-service"] == {
        "SOCRATIC_TOKEN_SECRET",
        "SOCRATIC_PUBLIC_URL",
    }


def _names_read_under(*relative_roots: str) -> str:
    """Every byte of Python under the given roots, concatenated.

    A textual scan rather than an AST one: a variable name reaches the
    environment as a string literal wherever it is read, and this guard asks
    only whether the name is named at all. Somewhere between "not mentioned in
    the container's own source" and "genuinely read" there is room for a false
    pass; there is none for a false failure, which is the direction that
    matters for a guard nobody is watching.
    """
    chunks = []
    for relative in relative_roots:
        root = REPO_ROOT / relative
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_every_socratic_variable_in_compose_is_read_by_its_own_container():
    variables = _compose_environment(COMPOSE.read_text(encoding="utf-8"))

    assert set(variables) == set(READERS), (
        "compose.yaml's services and this guard's map of which source tree runs "
        f"in each have drifted apart: {sorted(variables)} vs {sorted(READERS)}"
    )

    dead: dict[str, list[str]] = {}
    for service, names in sorted(variables.items()):
        source = _names_read_under(*READERS[service])
        missing = sorted(name for name in names if name not in source)
        if missing:
            dead[service] = missing

    assert dead == {}, (
        "these variables are set on a container whose own source never names "
        f"them, so setting them has no effect: {dead}. A variable that looks "
        "live and is not is worse than no variable at all (#94) — either the "
        "code that should read it is missing, or the line is."
    )
