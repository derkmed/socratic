"""The domain package's dependencies, enforced structurally.

Two rules, both load-bearing from the first commit rather than asserted later:

* **No third-party SDK.** `anthropic` is an optional extra, so the domain is
  testable with no SDK installed. The Anthropic adapter is the only module that
  may import it, and it is not in this package yet.
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


def _imported_roots() -> dict[pathlib.Path, set[str]]:
    """The top-level package each module imports, per file."""
    per_file: dict[pathlib.Path, set[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
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
        if roots - allowed
    }
    assert offenders == {}, f"third-party imports in the domain package: {offenders}"


def test_no_module_imports_the_anthropic_sdk():
    offenders = [
        str(path) for path, roots in _imported_roots().items() if "anthropic" in roots
    ]
    assert offenders == [], f"the SDK is an optional extra, imported by: {offenders}"


def test_no_module_imports_open_webui():
    offenders = [
        str(path)
        for path, roots in _imported_roots().items()
        if roots & FORBIDDEN_HOST_PACKAGES
    ]
    assert offenders == [], f"the host leaked below the portability seam: {offenders}"


def test_the_package_has_no_base_dependencies():
    config = tomllib.loads(PYPROJECT.read_text())
    assert config["project"]["dependencies"] == []


def test_anthropic_is_declared_as_an_optional_extra():
    config = tomllib.loads(PYPROJECT.read_text())
    extras = config["project"]["optional-dependencies"]
    assert any(spec.startswith("anthropic") for spec in extras["anthropic"])


def test_the_domain_imports_with_no_sdk_installed():
    # Belt and braces over the AST scan: import the package in a subprocess
    # whose meta path refuses `anthropic` outright, so a lazy or conditional
    # import would still be caught.
    guard = (
        "import sys\n"
        "class Refuse:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] == 'anthropic':\n"
        "            raise AssertionError('the domain imported the anthropic SDK')\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        return self.find_module(name, path)\n"
        "sys.meta_path.insert(0, Refuse())\n"
        "import socratic.domain.ids, socratic.domain.modes\n"
        "import socratic.domain.types, socratic.domain.registry\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", guard],
        capture_output=True,
        text=True,
        cwd=str(SOURCE_ROOT.parents[1]),
    )
    assert result.returncode == 0, result.stderr
