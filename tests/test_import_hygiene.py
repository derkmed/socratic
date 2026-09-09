"""The domain package's dependencies, enforced structurally.

Two rules, both load-bearing from the first commit rather than asserted later:

* **No third-party SDK in the domain.** `anthropic` is an optional extra, so
  `socratic.domain` is testable with no SDK installed. The Anthropic adapter is
  the only module that may import it — and now that the adapter exists, the rule
  is stated as *exclusivity* rather than as a blanket ban. A blanket ban has to
  be deleted the first time an adapter lands; "imported here and nowhere else"
  survives, and is the stronger of the two.
* **Nothing from Open WebUI** (spec acceptance 37, CONTEXT: Portability seam).
  The seam is a process boundary, so an Open WebUI import below it would not
  resolve at all; this test is the fast check that catches it at commit time.
"""

import ast
import os
import pathlib
import subprocess
import sys

try:  # tomllib is 3.11+; the dev extra carries the backport for 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "socratic"
PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"

FORBIDDEN_HOST_PACKAGES = {"open_webui", "openwebui"}

DOMAIN_PACKAGE = "domain"
"""The stdlib-only rule scopes here, and only here (CONTEXT: Portability seam).

It was written against the whole of `src/socratic/`, which over-reached: the
seam it protects is the domain's, and `socratic.adapters` exists precisely to
hold the imports the domain may not have. The Open WebUI rule below keeps
scanning everything, because *nothing* in the package may reach the host."""

ANTHROPIC_ADAPTER = pathlib.Path("adapters") / "anthropic_client.py"
"""The one module allowed to import the SDK (master spec §5, ticket #7)."""


def _imported_roots() -> dict[pathlib.Path, set[str]]:
    """The top-level package each module imports, per file."""
    per_file: dict[pathlib.Path, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        per_file[path.relative_to(SOURCE_ROOT)] = roots
    return per_file


def test_the_domain_imports_only_the_standard_library_and_itself():
    allowed = set(sys.stdlib_module_names) | {"socratic"}
    offenders = {
        str(path): sorted(roots - allowed)
        for path, roots in _imported_roots().items()
        if path.parts[0] == DOMAIN_PACKAGE and roots - allowed
    }
    assert offenders == {}, f"third-party imports in the domain package: {offenders}"


def test_the_anthropic_sdk_is_imported_only_by_the_adapter():
    # Master spec §5 and CONTEXT: Portability seam — "the adapter is the only
    # module that imports the Anthropic SDK". Asserted in both directions: the
    # adapter does import it, and nothing else does. The second half is the
    # rule; the first is what stops the rule from being vacuously satisfied by
    # an adapter that quietly stopped calling the API.
    importers = {
        path for path, roots in _imported_roots().items() if "anthropic" in roots
    }
    assert ANTHROPIC_ADAPTER in importers, (
        f"{ANTHROPIC_ADAPTER} is the adapter and must import the SDK; "
        f"found importers: {sorted(str(path) for path in importers)}"
    )
    strays = sorted(str(path) for path in importers - {ANTHROPIC_ADAPTER})
    assert strays == [], f"the SDK leaked outside the adapter, imported by: {strays}"


def test_no_module_imports_open_webui():
    offenders = [
        str(path)
        for path, roots in _imported_roots().items()
        if roots & FORBIDDEN_HOST_PACKAGES
    ]
    assert offenders == [], f"the host leaked below the portability seam: {offenders}"


def _load_pyproject() -> dict:
    """The parsed contents of `pyproject.toml`.

    Read as bytes: TOML is defined to be UTF-8, and `tomllib.load` decodes it
    as such, so a binary handle keeps the machine's locale codec out of it.
    """
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_the_package_has_no_base_dependencies():
    config = _load_pyproject()
    assert config["project"]["dependencies"] == []


def test_anthropic_is_declared_as_an_optional_extra():
    config = _load_pyproject()
    extras = config["project"]["optional-dependencies"]
    assert any(spec.startswith("anthropic") for spec in extras["anthropic"])


def _domain_module_names() -> list[str]:
    """Every importable module in the domain package, discovered not listed.

    Walks `SOURCE_ROOT / DOMAIN_PACKAGE` instead of naming modules, so the
    guard below grows with the package the way the AST scans already do. The
    hardcoded list it replaces had gone stale three modules behind (#28).

    Confining the walk to the domain package is also what keeps the rest of
    `socratic` out of it: `socratic.adapters` legitimately imports the SDK
    (see `ANTHROPIC_ADAPTER`), and sibling packages outside the seam may carry
    third-party dependencies the guard has no business importing.

    `__pycache__` entries and stems that are not Python identifiers are not
    importable modules and are skipped; `__init__.py` names its package.
    """
    names: set[str] = set()
    for path in (SOURCE_ROOT / DOMAIN_PACKAGE).rglob("*.py"):
        parts = path.relative_to(SOURCE_ROOT).with_suffix("").parts
        if "__pycache__" in parts:
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not all(part.isidentifier() for part in parts):
            continue
        names.add(".".join(("socratic",) + parts))
    return sorted(names)


def test_the_domain_module_discovery_covers_the_package():
    # The guard below is worth exactly what this walk returns, and a walk that
    # quietly returned nothing would make it vacuous — so pin the shape here:
    # the package itself, a module known to exist, and nothing outside the
    # domain, which is where the SDK is allowed to live.
    domain = f"socratic.{DOMAIN_PACKAGE}"
    names = _domain_module_names()
    assert domain in names
    assert f"{domain}.ids" in names
    assert len(names) > 1, names
    strays = [
        name
        for name in names
        if name != domain and not name.startswith(f"{domain}.")
    ]
    assert strays == [], f"the walk escaped the domain package: {strays}"


def _run_in_a_child_interpreter(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a child interpreter rooted at the project.

    Both ends of the pipe are pinned to UTF-8, because the value this returns
    is read only when the guard fails, and what it holds then is a traceback
    quoting em-dashed domain source (#43). `encoding` settles the parent's
    decode; `PYTHONIOENCODING` settles the child's encode, which would
    otherwise be the locale's codec or whatever the ambient environment last
    said. Pinning one end alone is worse than pinning neither: the two
    locale-derived defaults at least agree with each other, whereas a UTF-8
    decoder over cp1252 bytes raises inside subprocess's reader thread and
    leaves `stderr` as `None`. `errors="replace"` is the same argument once
    more — for a string read only to explain a failure, mangled beats absent.
    """
    environ = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(SOURCE_ROOT.parents[1]),
        env=environ,
    )


def test_the_domain_imports_with_no_sdk_installed():
    # Belt and braces over the AST scan: import the domain in a subprocess
    # whose meta path refuses `anthropic` outright, so a lazy, conditional or
    # dynamically named import would still be caught. Every module in the
    # package is imported — the check the AST cannot do is worth little if it
    # covers only the modules someone remembered to list.
    modules = _domain_module_names()
    assert modules, "no domain modules discovered; the guard would be vacuous"
    guard = (
        "import importlib\n"
        "import sys\n"
        "class Refuse:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] == 'anthropic':\n"
        "            raise AssertionError('the domain imported the anthropic SDK')\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        return self.find_module(name, path)\n"
        "sys.meta_path.insert(0, Refuse())\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
    )
    result = _run_in_a_child_interpreter(guard)
    assert result.returncode == 0, result.stderr


def test_the_child_interpreter_reports_non_ascii_diagnostics_intact(monkeypatch):
    # The guard above reads `stderr` only when it fails, and what it reads
    # then is a traceback printing the *source lines* of the frames it walked
    # — domain source, which is full of em dashes (#43).
    #
    # Both ends of that pipe pick a codec, and left alone both pick the
    # locale's, which is why the mangling does not show up on an untouched
    # machine. `PYTHONIOENCODING` moves the child's end and not the parent's,
    # and it is exactly the variable people set on Windows to get UTF-8 out of
    # Python. A codec that agrees with no locale makes the mismatch visible
    # wherever the suite runs.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")

    result = _run_in_a_child_interpreter(
        "raise AssertionError('the domain \\u2014 imported the SDK')"
    )

    assert result.returncode != 0
    assert "the domain — imported the SDK" in result.stderr


# --- The process boundary (issue #19, acceptance 37) -------------------------
#
# ADR-0002 named "an Open WebUI import creeping into the domain package" as the
# failure mode to watch, and admitted that nothing enforced it but review.
# ADR-0015 makes the seam physical: the domain runs in its own process, in an
# image that does not have Open WebUI installed, so such an import would not
# resolve. These two keep the *source* honest as well, because a grep test is
# what fails on the pull request rather than in the container.

SERVICE_PACKAGE = "service"

OPEN_WEBUI_ROOTS = frozenset({"open_webui", "openwebui"})


def test_nothing_in_the_package_imports_open_webui():
    offenders = {
        str(path): sorted(roots & OPEN_WEBUI_ROOTS)
        for path, roots in _imported_roots().items()
        if roots & OPEN_WEBUI_ROOTS
    }
    assert offenders == {}, (
        "Open WebUI is the host, not a dependency (ADR-0002, ADR-0015): "
        f"{offenders}"
    )


def test_the_domain_does_not_import_the_service():
    """The dependency runs one way.

    `test_the_domain_imports_only_the_standard_library_and_itself` allows the
    whole `socratic` root, so it would not catch this: the service is a
    `socratic` module too. But the service is the shell around the domain, and
    a domain module reaching back into it would put FastAPI below the seam
    while every existing guard stayed green.
    """
    offenders = {
        str(path): sorted(modules)
        for path, modules in _imported_submodules().items()
        if path.parts[0] == DOMAIN_PACKAGE and SERVICE_PACKAGE in modules
    }
    assert offenders == {}, f"domain modules importing the service: {offenders}"


def _imported_submodules() -> dict[pathlib.Path, set[str]]:
    """The `socratic.<name>` subpackages each module imports.

    A second scan rather than a widening of `_imported_roots`, which reports
    only top-level roots and is depended on by the guards above.
    """
    per_file: dict[pathlib.Path, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.update(_socratic_child(alias.name))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.update(_socratic_child(node.module))
        per_file[path.relative_to(SOURCE_ROOT)] = found
    return per_file


def _socratic_child(dotted: str) -> set[str]:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == "socratic":
        return {parts[1]}
    return set()
