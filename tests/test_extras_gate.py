"""The suite says out loud what an absent extra stopped it from running.

Nine modules in this suite open with `pytest.importorskip`, one call for the
whole file (#90). Without the extra, the module is never collected, and the run
reports that as **one skip** — not as the hundred-odd tests inside it. On
`main` @ `f1b8a43` the difference between the bare and the fully installed
interpreter was 316 tests, and the bare run was green.

The gate itself is right: the module genuinely cannot be imported without its
extra, and the domain is deliberately testable with nothing installed
(ADR-0002, `pyproject.toml`, `test_import_hygiene.py`). What was wrong is that
the absence was quiet. `tests/gating.py` makes it loud, and `--require-extras`
makes it fatal for the one caller — CI's installed job — that knows the extras
are supposed to be there.

The end-to-end tests below run a real `pytest` in a child interpreter over a
module gated on a package that cannot exist, because the hook's contract is
what a *run* prints and exits with, not what a function returns.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import types

from tests import gating

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

MISSING_PACKAGE = "socratic_no_such_extra_xyz"
"""A package that cannot be installed, so the gate below always fires."""


# --- Counting what was never collected ---------------------------------------


def test_it_counts_the_test_functions_a_module_declares():
    source = textwrap.dedent(
        """
        import pytest

        def test_one(): pass

        def helper(): pass

        async def test_two(): pass

        class TestSomething:
            def test_three(self): pass
            def not_a_test(self): pass
        """
    )
    assert gating.count_test_functions(source) == 3


def test_the_count_ignores_functions_nested_inside_a_test():
    source = textwrap.dedent(
        """
        def test_outer():
            def test_inner():
                pass
            test_inner()
        """
    )
    assert gating.count_test_functions(source) == 1


def test_a_module_that_cannot_be_parsed_counts_as_nothing():
    # The count exists to size a banner. A module whose source will not parse
    # is a different failure, and one pytest will report on its own.
    assert gating.count_test_functions("def test_(:\n") == 0


# --- Recognising a module that was gated out ---------------------------------


def _collect_report(nodeid: str, path: pathlib.Path, reason: str, outcome="skipped"):
    """A stand-in for the `CollectReport` pytest hands `pytest_collectreport`.

    Its `longrepr` for a collection-time skip is the triple pytest builds:
    absolute path, line number, and the reason with a `Skipped: ` prefix.
    """
    return types.SimpleNamespace(
        nodeid=nodeid,
        outcome=outcome,
        longrepr=(str(path), 2, f"Skipped: {reason}"),
    )


def test_a_module_skipped_at_collection_is_recorded(tmp_path):
    module = tmp_path / "test_gated.py"
    module.write_text("def test_a(): pass\ndef test_b(): pass\n", encoding="utf-8")

    record = gating.gated_module(
        _collect_report("tests/test_gated.py", module, "the service extra is optional")
    )

    assert record == gating.GatedModule(
        nodeid="tests/test_gated.py",
        reason="the service extra is optional",
        test_functions=2,
    )


def test_a_collection_that_did_not_skip_is_not_recorded(tmp_path):
    module = tmp_path / "test_fine.py"
    module.write_text("def test_a(): pass\n", encoding="utf-8")

    report = _collect_report("tests/test_fine.py", module, "n/a", outcome="passed")

    assert gating.gated_module(report) is None


def test_a_skip_without_pytest_s_location_triple_is_not_recorded():
    report = types.SimpleNamespace(
        nodeid="tests/test_odd.py", outcome="skipped", longrepr="something else"
    )
    assert gating.gated_module(report) is None


# --- The banner ---------------------------------------------------------------


def test_nothing_gated_prints_nothing():
    assert gating.summary_lines([]) == []


def test_the_banner_names_the_module_its_reason_its_size_and_the_install():
    lines = gating.summary_lines(
        [
            gating.GatedModule("tests/test_service.py", "the service extra", 76),
            gating.GatedModule("tests/test_rendering_markdown.py", "the rendering extra", 40),
        ]
    )
    text = "\n".join(lines)

    assert "2 test modules were never collected" in text
    assert "116 test functions" in text
    assert "tests/test_service.py" in text
    assert "the service extra" in text
    assert "76" in text
    assert 'pip install -e ".[all]"' in text


# --- End to end, in a real run ------------------------------------------------


def _run_pytest(directory: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess:
    """Run `pytest` over `directory` in a child interpreter, gate loaded.

    `-p tests.gating` is how the plugin is loaded here rather than through the
    repo's own root `conftest.py`: `directory` is outside the repository, so
    that conftest never applies and the plugin is registered exactly once.
    `cwd` is the repository root, which is what puts `tests.gating` within the
    child's reach — `python -m` prepends the working directory to `sys.path`.

    Both ends of the pipe are pinned to UTF-8 for the reason
    `test_import_hygiene._run_in_a_child_interpreter` gives: what is read back
    is prose with em dashes in it, and a locale codec mangles or raises on it.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(directory),
            "-p",
            "tests.gating",
            "-p",
            "no:cacheprovider",
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


def _write_gated_module(directory: pathlib.Path) -> None:
    # A module that does run, alongside the gated one: a run that collects
    # nothing at all exits 5 whatever this plugin decides, which would make
    # the exit status below prove nothing.
    (directory / "test_collected.py").write_text(
        "def test_something_ran(): pass\n", encoding="utf-8"
    )
    (directory / "test_gated_module.py").write_text(
        textwrap.dedent(
            f"""
            import pytest

            pytest.importorskip(
                "{MISSING_PACKAGE}", reason="the imaginary extra is optional"
            )

            def test_one(): pass
            def test_two(): pass
            def test_three(): pass
            """
        ),
        encoding="utf-8",
    )


def test_a_gated_module_makes_a_green_run_say_so(tmp_path):
    _write_gated_module(tmp_path)

    result = _run_pytest(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test module was never collected" in result.stdout, result.stdout
    assert "3 test functions" in result.stdout, result.stdout
    assert "the imaginary extra is optional" in result.stdout, result.stdout
    assert 'pip install -e ".[all]"' in result.stdout, result.stdout


def test_require_extras_turns_that_into_a_failure(tmp_path):
    _write_gated_module(tmp_path)

    result = _run_pytest(tmp_path, "--require-extras")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "--require-extras" in result.stdout, result.stdout


def test_a_run_with_nothing_gated_says_nothing(tmp_path):
    (tmp_path / "test_plain.py").write_text("def test_one(): pass\n", encoding="utf-8")

    result = _run_pytest(tmp_path, "--require-extras")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "never collected" not in result.stdout, result.stdout


def test_the_gate_is_registered_for_this_suite_too():
    # The end-to-end runs above load the plugin with `-p`, which proves the
    # module works but says nothing about whether *this* suite has it. The
    # repo's root `conftest.py` is what wires it in, and the option it adds is
    # the visible edge of that.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    assert "--require-extras" in result.stdout, result.stdout
