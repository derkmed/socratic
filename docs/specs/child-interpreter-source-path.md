# The import-hygiene child interpreter gets its own path to `src`

Issue [#79](https://github.com/derkmed/socratic/issues/79).

## Goal

`tests/test_import_hygiene.py::test_the_domain_imports_with_no_sdk_installed`
spawns a child interpreter to prove the domain imports with the Anthropic SDK
refused outright — the check the AST scans cannot do. Nothing puts `src` on that
child's import path, so in a checkout with no editable install the child dies on
`import socratic` before it can prove anything:

```
$ python -m pytest
FAILED tests/test_import_hygiene.py::test_the_domain_imports_with_no_sdk_installed
E         ModuleNotFoundError: No module named 'socratic'
1 failed, 621 passed, 7 skipped
```

`[tool.pytest.ini_options] pythonpath = ["src"]` configures the *parent* process;
it does not reach a subprocess. The child runs with `-c`, so its `sys.path[0]` is
the cwd — the repo root — and the package lives at `src/socratic`, not
`socratic`. The only thing that has ever made this guard pass is an installed
distribution, and no document in the repo asks anyone to install one.

The goal is to make the guard carry its own path to the package source, and to
pin that property with a test that does not depend on whether the ambient
interpreter happens to have the package installed.

## Seams

- **`tests/test_import_hygiene.py::_run_in_a_child_interpreter`** — the existing
  seam, and the only one. Every child interpreter in the file goes through this
  one helper, so the environment it builds is exactly the contract under test.
  No new seam is introduced.

## Decisions

- **`PYTHONPATH` in the `environ` dict, not a moved `cwd`.** The helper's
  docstring says the child is "rooted at the project" and the `environ` copy
  exists precisely to layer settings onto the ambient environment
  (`PYTHONIOENCODING`, per #43). Adding one more entry to that dict extends a
  mechanism the function already documents; repointing `cwd` at `src` would
  falsify the docstring and quietly move the working directory that the
  non-ASCII-diagnostics test's tracebacks are reported relative to. The issue
  offered both and preferred this one; the docstring is the reason it is right,
  not the preference.
- **The ambient `PYTHONPATH` is preserved, not replaced.** The `environ` dict is
  built as a copy-plus-overrides of `os.environ` for exactly this reason. `src`
  is prepended, joined with `os.pathsep` — `;` on Windows, `:` elsewhere — so a
  developer's own `PYTHONPATH` still reaches the child.
- **Diff confined to `tests/test_import_hygiene.py`.** No change to
  `pyproject.toml` or `conftest.py`: the defect is one subprocess's environment,
  and shared config is where concurrent work collides.
- The domain's stdlib-only rule and the SDK's exclusivity to
  `adapters/anthropic_client.py` are settled (ADR-0002 portability seam, master
  spec §5, the module docstring of the test file). This change alters nothing
  about what the guard asserts, only whether it can run.

## Approach

1. **Red** — add a test asserting the property directly: that
   `_run_in_a_child_interpreter` hands the child a `sys.path` containing
   `str(SOURCE_ROOT.parent)`. The child prints its resolved `sys.path` and the
   parent asserts membership. This fails today on every machine, installed or
   not, because nothing puts `src` there; the pre-existing
   `test_the_domain_imports_with_no_sdk_installed` failure is the reported
   symptom but is only red on a machine without the install, so it cannot be the
   test that pins the fix.
2. **Green** — prepend `str(SOURCE_ROOT.parent)` to `PYTHONPATH` in the
   `environ` dict `_run_in_a_child_interpreter` already builds, and extend its
   docstring to say why that entry is there.
3. Run the file, then the full suite.

## Out of scope

- **Documenting or adding an install step.** `pip install -e .` may still be
  useful, but the guard must not depend on one, and nothing else in the suite
  does.
- **`pyproject.toml` and `conftest.py`.** A `pythonpath`/`conftest` change would
  reach every test rather than the one subprocess that is broken, and would
  collide with concurrent branches.
- **The assertion message.** The issue notes the failure reads as a packaging
  break; once the path is right the failure mode is gone, and rewording
  `assert result.returncode == 0, result.stderr` to explain a condition that can
  no longer arise is noise.
- **The other subprocess-free guards in the file.** The AST scans read source
  from disk and never needed an import path.
- **Anything about the SDK exclusivity rule itself.**

## Acceptance

1. `python -m pytest tests/test_import_hygiene.py` passes in an interpreter with
   no editable install of `socratic` — the exact condition of the report.
2. A test in `tests/test_import_hygiene.py` fails if `src` is not on the child
   interpreter's `sys.path`, whether or not `socratic` is installed.
3. `test_the_child_interpreter_reports_non_ascii_diagnostics_intact` still
   passes: the added entry does not disturb the `PYTHONIOENCODING` override or
   the ambient environment the helper copies.
4. The full suite is green, and the diff touches only
   `tests/test_import_hygiene.py` plus this spec.
