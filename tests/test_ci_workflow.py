"""Something runs the suite on a clean checkout, in both conditions.

Issue [#81]: `.github/` did not exist, so nothing ran `pytest` on a fresh
clone — which is how #79, a guard that fails in any checkout without an
editable install, sat red on `main`. The workflow answers that, and this module
holds it to the two properties that would otherwise drift silently:

* **Both conditions run the whole suite.** *Bare* is an interpreter with the
  test runner and nothing else — no editable install, no extra. It is the only
  thing that keeps `dependencies == []`, the stdlib-only domain and the
  child-interpreter path guard (#79) honest. *Installed* is `.[all]`, and it
  passes `--require-extras` so that an extra which failed to install fails the
  job rather than skipping a module (#90).
* **A new extra cannot go untested.** The installed job installs `all`, and
  `all` must cover every extra `pyproject.toml` declares. An extra whose tests
  nothing runs is #90 happening again.

The workflow is read as text. Parsing it properly would mean a YAML library,
and a third-party import here would not survive the bare condition it exists to
protect — which is the same argument `pyproject.toml` makes about the domain.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

try:  # tomllib is 3.11+; the dev extra carries the backport for 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

BARE = "bare"
INSTALLED = "installed"

JOB_HEADING = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*$")
EXTRAS_IN_A_SPEC = re.compile(r"\[([^\]]+)\]")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    """The body of each job, keyed by name.

    A job opens at two spaces of indentation under `jobs:` and runs until the
    next such line. That is the whole grammar this file needs, and it is the
    shape `.github/workflows/tests.yml` is written in on purpose.
    """
    lines = text.splitlines()
    try:
        start = lines.index("jobs:") + 1
    except ValueError:
        return {}

    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines[start:]:
        heading = JOB_HEADING.match(line)
        if heading:
            current = found.setdefault(heading.group(1), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(body) for name, body in found.items()}


def _declared_extras() -> dict[str, list[str]]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def _extras_installed_by(job: str) -> set[str]:
    """The extra names a job's `pip install` lines name, before expansion."""
    named: set[str] = set()
    for line in job.splitlines():
        if "pip install" not in line:
            continue
        for group in EXTRAS_IN_A_SPEC.findall(line):
            named.update(part.strip() for part in group.split(","))
    return named


def _expand(named: set[str], extras: dict[str, list[str]]) -> set[str]:
    """Follow self-references, so `all` counts as the extras it pulls in.

    `all = ["socratic[anthropic,dev,rendering,service]"]` is a PEP 508
    self-reference: installing it installs the four. Without following it, the
    coverage assertion below would read `all` as one extra among six.
    """
    reached = set(named)
    frontier = list(named)
    while frontier:
        name = frontier.pop()
        for spec in extras.get(name, ()):
            if not spec.startswith("socratic"):
                continue
            for group in EXTRAS_IN_A_SPEC.findall(spec):
                for part in group.split(","):
                    part = part.strip()
                    if part and part not in reached:
                        reached.add(part)
                        frontier.append(part)
    return reached


def test_the_workflow_exists_and_runs_on_push_and_pull_request():
    assert WORKFLOW.exists(), f"no workflow at {WORKFLOW}"
    text = _workflow_text()
    assert "on:" in text
    assert "push:" in text
    assert "pull_request:" in text


def test_it_runs_the_suite_in_both_conditions():
    jobs = _jobs(_workflow_text())
    assert set(jobs) == {BARE, INSTALLED}, sorted(jobs)
    for name, body in jobs.items():
        assert "python -m pytest" in body, f"{name} does not run the suite"


def test_the_bare_job_installs_neither_the_package_nor_an_extra():
    # The point of the job, and the reason it is not `pip install -e ".[dev]"`:
    # an extra of this project installs this project, and #79 was invisible
    # precisely because every developer's interpreter had the editable install.
    bare = _jobs(_workflow_text())[BARE]
    assert "pip install -e" not in bare, bare
    assert _extras_installed_by(bare) == set(), bare


def test_the_installed_job_installs_every_declared_extra():
    extras = _declared_extras()
    installed = _expand(
        _extras_installed_by(_jobs(_workflow_text())[INSTALLED]), extras
    )
    missing = sorted(set(extras) - installed)
    assert missing == [], (
        "an extra nothing in CI installs, so its tests would skip unnoticed "
        f"(#90): {missing}"
    )


def test_the_installed_job_refuses_a_silently_gated_module():
    installed = _jobs(_workflow_text())[INSTALLED]
    assert "--require-extras" in installed, installed


def test_the_expansion_follows_a_self_referencing_extra():
    # Otherwise the coverage assertion above would be satisfied by an `all`
    # that pulls in nothing, which is exactly the failure it guards against.
    extras = {
        "all": ["socratic[one,two]"],
        "one": ["a-package>=1"],
        "two": ["another-package"],
    }
    assert _expand({"all"}, extras) == {"all", "one", "two"}


def test_the_expansion_does_not_invent_extras_from_a_version_specifier():
    extras = {"one": ["a-package[with-its-own-extra]>=1"]}
    assert _expand({"one"}, extras) == {"one"}
