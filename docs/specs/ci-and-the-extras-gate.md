# The suite says what it did not run, and something runs it

Issues [#90](https://github.com/derkmed/socratic/issues/90) and
[#81](https://github.com/derkmed/socratic/issues/81), taken together because
they are one problem seen twice: **nothing in this repo can tell you whether the
suite actually ran.**

## Goal

Nine test modules are gated behind `pytest.importorskip`, one call per module.
Without the optional extras installed, those nine modules are never collected
and the run says so as nine skipped *modules* — not as the hundreds of tests
inside them. Measured on `main` @ `f1b8a43`, same checkout, same interpreter:

```
# pytest and the tomli backport only — no editable install, no extras
762 passed, 9 skipped in 5.37s

# pip install -e ".[dev,service,anthropic,rendering]"
1078 passed, 10 skipped in 16.19s
```

316 tests separate those two lines, and the first line is green. The `README`'s
own **Tests** section names `pip install -e ".[service,rendering,dev]"`, which
omits `anthropic` — so following the repo's instructions to the letter still
leaves `tests/test_anthropic_adapter.py` silently uncollected. That is #90.

Nothing anywhere runs either line on a clean checkout. `.github/` does not
exist, which is how #79 — a guard that failed in any checkout without an
editable install — sat red on `main`. That is #81.

The extras are not the defect. `pyproject.toml` explains why the domain must
install with `dependencies == []`, `test_import_hygiene.py` enforces it, and
ADR-0002's portability seam is the reason. The defect is that the *absence* of
the extras is quiet, and that no machine ever tries either condition.

The goal is two things that fix each other:

1. A run that is honest about what it skipped, and a way to demand it skipped
   nothing.
2. A CI workflow that runs the suite in **both** conditions on every push and
   pull request — bare, and fully installed — so each condition is a standing
   claim rather than a thing someone once tried.

The second is what makes the first safe to rely on: the bare job is the only
thing that keeps `dependencies == []` and the stdlib-only domain honest once a
contributor's default install stops being bare.

## Seams

| Seam | Contract | Tested by |
|---|---|---|
| `tests/gating.py::count_test_functions` | source text → the number of `test_*` functions declared in it, at module level or in a class | `tests/test_extras_gate.py` |
| `tests/gating.py::gated_module` | a skipped `CollectReport` → the record of what was gated out, or `None` for anything else | `tests/test_extras_gate.py` |
| `tests/gating.py::summary_lines` | the records → the banner text, empty when nothing was gated | `tests/test_extras_gate.py` |
| `tests/gating.py` **as a pytest plugin** | a real `pytest` run over a gated module prints the banner; with `--require-extras` it also fails | `tests/test_extras_gate.py`, end to end in a child interpreter |
| `.github/workflows/tests.yml` | the two conditions, and that the installed one installs every extra `pyproject.toml` declares | `tests/test_ci_workflow.py` |

`tests/conftest.py` is deliberately *not* a seam: it imports the hook functions
from `tests/gating.py` and adds nothing, so that the same module can be loaded
as a plugin with `-p tests.gating` by the end-to-end test without the repo's own
`conftest.py` registering the hooks a second time.

## Decisions

- **An `all` extra, rather than folding the extras into `dev`.** #90 offers
  three remedies, cheapest first, and the cheapest is to make `pip install -e
  ".[dev]"` pull everything. It is rejected here: `anthropic` is a large SDK
  that a contributor working on the domain has no use for, and `dev` honestly
  means *the test runner*. `all = ["socratic[anthropic,rendering,service,dev]"]`
  is purely additive — no existing install changes — and the banner below is
  what removes the invisibility that made the quiet default dangerous. The
  alternative is one line of `pyproject.toml` if a maintainer disagrees.
- **The banner prints by default; failing is opt-in.** A gated skip is correct
  behaviour in the bare condition and must stay correct — that condition is a CI
  job. So the default is a loud, unmissable summary naming each uncollected
  module, the extra it wants, the number of test functions inside it, and the
  install that would run them. `--require-extras` turns the same finding into a
  failing exit status, and the installed CI job passes it. Without that flag CI
  could not tell "the extras are installed" from "the extras quietly failed to
  install".
- **Test functions counted from source by AST, not estimated.** The modules were
  never imported, so pytest cannot know their size; the AST can. The count is of
  declared `test_*` functions, so a parametrised module reports fewer cases than
  it would run — the banner says "test functions" and means it.
- **Two named jobs, not a matrix.** #81 suggests a matrix of bare and installed.
  The two conditions share no install step at all, so a matrix would be one
  `if:` per step masquerading as symmetry. Two jobs read as what they are.
- **The whole suite is gated in both conditions.** #81 was unsure how much to
  gate. Gating anything less would reintroduce the class of defect it was filed
  about — a green signal that does not mean the suite passed. The suite is six
  seconds bare and sixteen installed; there is no cost to argue about.
- **Python 3.10 only, Ubuntu only.** `requires-python = ">=3.10"` and the
  `Dockerfile` runs `python:3.10-slim`, so 3.10 is both the floor and what the
  prototype actually ships on. A version matrix is a decision about supported
  platforms that no document in this repo has made.
- **The bare job installs the package not at all**, and names `pytest` and
  `tomli` directly rather than through an extra — because an extra of this
  project installs this project. That is the whole point of the job: #79 was
  invisible precisely because every developer's interpreter had an editable
  install, and `test_the_child_interpreter_can_reach_the_package_source` is the
  guard that only means something in an interpreter that does not.
- **The workflow is checked by a test, at the one place it can drift.**
  `tests/test_ci_workflow.py` asserts that the extras the installed job installs
  cover every extra `pyproject.toml` declares, following self-references such as
  `all`. A new extra whose tests nothing runs is exactly this pair of issues
  happening again. The workflow is read as text — no YAML parser is added, since
  that would be a third-party dependency in the bare condition.

## Approach

1. **Red** — `tests/test_extras_gate.py` against `tests/gating.py`, which does
   not exist: the counter, the record, the banner text, and an end-to-end run of
   `python -m pytest -p tests.gating` over a temporary module whose
   `importorskip` names a package that cannot exist, asserting the banner in
   stdout and, with `--require-extras`, a non-zero exit status.
2. **Green** — write `tests/gating.py`; wire the hooks into `tests/conftest.py`.
3. **Red** — `tests/test_ci_workflow.py`: the workflow file exists, runs the
   suite in both conditions, and its installed job covers every declared extra.
4. **Green** — `pyproject.toml` gains the `all` extra;
   `.github/workflows/tests.yml` is written.
5. Both conditions are then re-measured locally, by hand, in the two
   interpreters used for the baseline above — a workflow file is configuration,
   and the only honest verification is running its commands.
6. `README`'s **Tests** section is corrected to name `.[all]` and to say what
   the bare condition is for.

## Out of scope

- **Changing what any gate asserts, or removing any `importorskip`.** Every one
  of the nine is correct: the module genuinely cannot be imported without its
  extra.
- **Making `dev` heavier**, per the decision above.
- **A Python or OS matrix**, linting, type checking, coverage, or a release job.
  #81 asked for the suite to run; anything else is a policy decision.
- **The two `UNRUN` skips in `tests/test_anthropic_cache_gate.py`.** They are
  deliberate: they make live, paid API calls and are gated on
  `ANTHROPIC_API_KEY`. `--require-extras` is about uncollected *modules*, not
  about these, and CI holds no API key.
- **Caching pip downloads in CI.** A first workflow should be obvious; sixteen
  seconds of tests do not need a cache in front of them.

## Acceptance

1. In an interpreter with `pytest` and `tomli` and nothing else, `python -m
   pytest` passes and prints a banner naming all nine gated modules, the extras
   they need, and `pip install -e ".[all]"`.
2. In the same interpreter, `python -m pytest --require-extras` exits non-zero
   and says why.
3. In an interpreter with `pip install -e ".[all]"`, `python -m pytest
   --require-extras` passes and prints no banner.
4. `pip install -e ".[all]"` installs every extra `pyproject.toml` declares.
5. `.github/workflows/tests.yml` runs on push and pull request, with a job for
   each condition, and the installed job passes `--require-extras`.
6. `tests/test_ci_workflow.py` fails if a new extra is declared that the
   installed job does not install.
7. The full suite is green in both conditions, and the numbers are recorded in
   the pull request.
