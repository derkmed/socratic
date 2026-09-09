"""What an absent extra stopped the suite from running, said out loud.

Nine modules in this suite open with `pytest.importorskip` — the service, the
renderer, the overlay, the adapter, the Pipe. One call gates the whole file, so
without the extra the module is never collected and the run reports it as a
single skip. Nine skips, and 316 tests that did not run (#90). The gate is
correct: the domain installs with `dependencies == []` on purpose (ADR-0002,
`test_import_hygiene.py`) and those modules genuinely cannot be imported
without their extra. Only the silence was wrong.

This module is a `pytest` plugin, registered by the repository's root
`conftest.py`. It does two things:

* **Always** — print a summary naming every module that was never collected,
  the reason it gave, how many test functions it declares, and the install that
  would run them.
* **On `--require-extras`** — fail the run instead. That is for a caller who
  knows the extras are supposed to be installed and wants "the extras did not
  install" to look different from "the suite passed". CI's installed job passes
  it; the bare job does not, because a gated skip is the correct outcome there
  and is itself under test.

The size of a module is counted from its source by AST, because a module that
was never imported has no other way to say how big it is. It counts *declared*
`test_` functions, so a parametrised module runs more cases than it reports —
the wording says "test functions" and means exactly that.
"""

from __future__ import annotations

import ast
import pathlib
from typing import NamedTuple, Sequence

REQUIRE_EXTRAS = "--require-extras"

FULL_INSTALL = 'pip install -e ".[all]"'
"""The one install that leaves nothing gated (`pyproject.toml`, `all`)."""

PLUGIN_NAME = "socratic-extras-gate"


class GatedModule(NamedTuple):
    """A test module that an absent optional dependency kept out of the run."""

    nodeid: str
    reason: str
    test_functions: int


def count_test_functions(source: str) -> int:
    """How many `test_` functions `source` declares, at module or class level.

    Deliberately not an `ast.walk`: a helper defined *inside* a test is not a
    test, and counting one would overstate the very number this exists to stop
    understating. Unparseable source counts as nothing — that is a different
    failure, and one pytest reports on its own.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    def _tests(body) -> int:
        found = 0
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found += node.name.startswith("test")
            elif isinstance(node, ast.ClassDef):
                found += _tests(node.body)
        return found

    return _tests(tree.body)


def gated_module(report) -> GatedModule | None:
    """The record of a collection that was skipped, or `None` for anything else.

    A collection-time skip carries pytest's location triple as its `longrepr`:
    the absolute path, the line, and the reason behind a `Skipped: ` prefix.
    Anything else — a passing collection, a collection error, a skip reported
    in some other shape — is not this plugin's business.
    """
    if getattr(report, "outcome", None) != "skipped":
        return None
    longrepr = getattr(report, "longrepr", None)
    if not (isinstance(longrepr, tuple) and len(longrepr) == 3):
        return None

    path, _line, message = longrepr
    reason = str(message).split("Skipped: ", 1)[-1].strip()

    source = pathlib.Path(str(path))
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        text = ""

    return GatedModule(
        nodeid=report.nodeid,
        reason=reason,
        test_functions=count_test_functions(text),
    )


def summary_lines(gated: Sequence[GatedModule], *, required: bool = False) -> list[str]:
    """The banner, or nothing at all when the whole suite was collected."""
    if not gated:
        return []

    modules = len(gated)
    tests = sum(record.test_functions for record in gated)
    plural = "s were" if modules != 1 else " was"
    width = max(len(record.nodeid) for record in gated)

    lines = [
        f"{modules} test module{plural} never collected, because an optional "
        f"extra is not installed.",
        f"That is {tests} test functions, counted from their source, that the "
        f"line above reports as {modules} skipped.",
        "",
    ]
    lines += [
        f"  {record.nodeid:<{width}}  {record.test_functions:>4} test functions"
        f"  {record.reason}"
        for record in gated
    ]
    lines += ["", f"  Run all of them:  {FULL_INSTALL}"]
    if required:
        lines += [
            "",
            f"  {REQUIRE_EXTRAS} was given, so this is a failure: the caller "
            f"asserted the extras were installed.",
        ]
    return lines


class ExtrasGate:
    """The plugin instance: it records the gated modules, then reports them."""

    def __init__(self, required: bool) -> None:
        self.required = required
        self.gated: list[GatedModule] = []

    def pytest_collectreport(self, report) -> None:
        record = gated_module(report)
        if record is not None:
            self.gated.append(record)

    def pytest_terminal_summary(self, terminalreporter) -> None:
        lines = summary_lines(self.gated, required=self.required)
        if not lines:
            return
        terminalreporter.write_sep("=", "optional extras", red=self.required)
        for line in lines:
            terminalreporter.write_line(line)

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        # Set on the session rather than raised, because a gated module is not
        # a test failure: nothing ran badly, something did not run at all.
        if self.required and self.gated:
            session.exitstatus = 1


def pytest_addoption(parser) -> None:
    parser.addoption(
        REQUIRE_EXTRAS,
        action="store_true",
        default=False,
        help=(
            "fail if any test module was skipped for want of an optional "
            f"extra. For a caller that installed them all ({FULL_INSTALL}) "
            "and needs a failed install to look different from a pass."
        ),
    )


def pytest_configure(config) -> None:
    config.pluginmanager.register(
        ExtrasGate(required=config.getoption(REQUIRE_EXTRAS)), PLUGIN_NAME
    )
