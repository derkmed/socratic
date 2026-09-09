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
    result = subprocess.run(
        [sys.executable, "-c", guard],
        capture_output=True,
        text=True,
        cwd=str(SOURCE_ROOT.parents[1]),
    )
    assert result.returncode == 0, result.stderr
