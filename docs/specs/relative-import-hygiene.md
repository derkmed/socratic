# The import-hygiene scans get a precondition instead of a resolver

Issue [#84](https://github.com/derkmed/socratic/issues/84).

## Goal

Both AST scans in `tests/test_import_hygiene.py` — `_imported_roots()` and
`_imported_submodules()` — count only *absolute* `from` imports:

```python
elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
```

`from ..service import app` parses to `ImportFrom(module="service", level=2)`, so
the clause drops it. The rule that actually loses its teeth is
`test_the_domain_does_not_import_the_service`: a domain module reaching back into
the service *relatively* would put FastAPI below the **portability seam**
(CONTEXT: Portability seam;
[ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md),
[ADR-0015](../adr/0015-iframe-pipe-transport.md)) while every guard stayed green
— the exact failure that test exists to catch.

Latent, not live: no module under `src/socratic/` uses a relative import today.

The goal is to make the `level == 0` clauses *true statements* rather than bugs,
and to guard the new guard — the point of the issue is that a guard was itself
unguarded.

## Seams

| Seam | Kind | Why it must exist |
|---|---|---|
| `_python_sources(root) -> Iterator[tuple[Path, str]]` | new | The one source-reading walk. Extracted from the two scans that each did their own `rglob` + `read_text`, so a guard can be pointed at *synthetic* source instead of the tree. Without this seam the new rule can only ever be tested against a package that satisfies it, which is how the original hole survived. |
| `_relative_import_offenders(sources) -> dict[str, list[str]]` | new | The rule itself, as a pure function of `(path, source)` pairs. This is where the regression test attaches: feed it `from ..service import app` and it must report it. |
| `test_no_module_uses_a_relative_import` | new | The rule applied to the real tree. |
| `_imported_roots`, `_imported_submodules` | existing | Unchanged behaviour. They gain a comment naming the guard above as the precondition that makes their `level == 0` clause sound. |

## Decisions

- **Shape 2, not shape 1.** The issue offers resolving the relative level against
  the file's own package, or asserting the absence of relative imports outright.
  Shape 2 is taken, for three reasons:
  - **It is total; shape 1 is partial.** `from . import service` parses to
    `ImportFrom(module=None, level=1)`, which the existing `and node.module`
    clause drops *independently* of `level`. A resolver would have to handle a
    null module, `__init__.py` anchoring (a package file's anchor is its own
    package, not its parent's), and a `level` deeper than the package — three
    more chances to leave a hole in the very helper whose hole we are closing.
    A ban has no cases.
  - **It fixes both scans at once, and every scan added later.** Shape 1 repairs
    `_imported_submodules()`; `_imported_roots()` keeps the same clause, sound
    today only by the accident that a relative import cannot name a top-level
    third party. Under shape 2 both clauses are correct by construction, and so
    is the next guard someone writes in this file.
  - **It matches the repo's own precedent.** `src/socratic/` contains zero
    relative imports across every package; the ban codifies what the code
    already does rather than legislating a new style. `grep -rEn "^\s*from \.
    " --include="*.py" src/` is empty on `origin/main`.
- **The rule is stated as a *precondition of the scans*, not as a style
  preference.** Its docstring and its failure message say what it protects — the
  `level == 0` clauses, and through them the portability seam — so a future
  reader deleting it as "just a lint rule" is told what breaks. This is the
  answer to the one real cost of shape 2: that the seam guard now leans on a
  second, separately-deletable test.
- **No ADR.** The fifteen ADRs record architectural decisions with live
  trade-offs. "No relative imports under `src/socratic/`" is a technique choice
  inside one test file, fully explained where it bites, and reversible by
  adopting shape 1 later. Recorded here and in the guard's docstring instead.
- **Tests are not in scope of the ban.** `SOURCE_ROOT` is `src/socratic`; the
  scans have never looked at `tests/`, and the seam is a property of the shipped
  package.
- **The regression test is the load-bearing deliverable.** A guard asserting
  `offenders == {}` over a tree that already satisfies it passes whether or not
  it works — vacuous in exactly the way `test_the_domain_module_discovery_covers_the_package`
  and the two-directional SDK assertion already guard against elsewhere in this
  file. The house style here is to pin the mechanism, and this follows it.

## Approach

One file, TDD:

1. **Red** — `test_the_relative_import_scan_reports_a_reach_across_the_seam`:
   hand `_relative_import_offenders` a synthetic `domain/quiz.py` containing
   `from ..service import app`, and a second containing `from . import service`,
   and assert both are reported with the offending statement. Fails today: the
   helper does not exist, and a helper carrying the `level == 0` clause would
   fail it too.
2. **Green** — extract `_python_sources`, add `_relative_import_offenders`.
3. **Red/green** — `test_no_module_uses_a_relative_import` over the real tree,
   plus a non-vacuity assertion that the walk found modules at all.
4. Point the two existing scans at `_python_sources`, and comment their
   `level == 0` clause with the guard that licenses it. No behaviour change.
5. Full suite.

## Out of scope

- **Resolving relative imports (shape 1).** Named above and rejected; the
  migration path if the ban is ever unwanted.
- **`tests/`, `docs/`, `compose.yaml`, `Dockerfile`, and every module under
  `src/socratic/`.** Nothing in the package changes: there is no relative import
  to rewrite.
- **A lint rule.** No `ruff`/`flake8` configuration exists in `pyproject.toml`;
  adding a linter to enforce one rule is a dependency decision, not this fix.
- **The other import-hygiene guards**, the child-interpreter helper, the module
  docstring's "two rules", and the process-boundary comment block.

## Acceptance

1. `_relative_import_offenders` reports a synthetic domain module containing
   `from ..service import app`, naming the file and the statement.
2. It also reports `from . import service` — the `module=None` form the existing
   `and node.module` clause drops regardless of level.
3. `test_no_module_uses_a_relative_import` passes over `src/socratic/` and is
   non-vacuous: the walk it reads is asserted to have found modules.
4. Reintroducing a `node.level == 0` filter into `_relative_import_offenders`
   turns acceptance 1 and 2 red.
5. `_imported_roots()` and `_imported_submodules()` return exactly what they
   returned before.
6. Full suite green with no skips gained: 1024 passed, 2 skipped → 1026 passed,
   2 skipped.
