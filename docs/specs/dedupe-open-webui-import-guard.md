# One guard for the Open WebUI rule, not two

## Goal

`tests/test_import_hygiene.py` asserts the **portability seam**'s Open WebUI rule
(CONTEXT: Portability seam; [ADR-0002](../adr/0002-open-webui-host-with-portability-seam.md),
[ADR-0015](../adr/0015-iframe-pipe-transport.md)) twice, over two copies of the
same two-member set. `test_no_module_imports_open_webui` and
`test_nothing_in_the_package_imports_open_webui` scan the same files, through the
same helper, with the same predicate; whichever fails, the other fails
identically. Leave one guard and one constant, keeping the copy whose assertion
message names the two decisions the rule rests on. Closes #82.

## Seams

No new seam. The rule's seam already exists and stays exactly where it is:

| Seam | Kind | Why it must exist |
|---|---|---|
| `_imported_roots() -> dict[Path, set[str]]` | existing | The AST scan every import-hygiene guard reads. Both Open WebUI guards already attach here, and the surviving one keeps attaching here unchanged. |
| `test_nothing_in_the_package_imports_open_webui` | existing | The surviving guard. It is itself the assertion of the rule; the change is that it becomes the only one. |

Because the change *removes* a test, the red-green loop has nothing to attach a
new failing test to. What stands in for it is an **equivalence demonstration**:
inject an `import open_webui` under `src/socratic/`, observe both guards fail on
the same offender, then confirm the surviving guard alone still fails on it.
That is the evidence the deletion loses no coverage, and it belongs in the PR
rather than in the tree.

## Decisions

- **The two guards are genuinely identical in scope.** Both iterate all of
  `_imported_roots()`, which walks `SOURCE_ROOT.rglob("*.py")` with no filter,
  and both intersect the roots with `{"open_webui", "openwebui"}`. Neither
  restricts to `DOMAIN_PACKAGE`, neither excludes anything the other includes.
  They differ only in the shape of `offenders` (list of paths vs. dict of path to
  matched roots) and in the assertion message.
- **The duplication was accidental, not two deliberate perspectives.** The
  second guard arrived with the quiz service (#19, `920d430`), whose message
  reads "Two new guards keep the seam physical in the source as well as in the
  image: nothing imports Open WebUI, and no domain module imports the service" —
  written as if the first guard (`9c153c8`, the scaffolding commit) were not
  already there. The other guard in that pair,
  `test_the_domain_does_not_import_the_service`, was new; this one was not.
- **The module docstring's "two rules" survive the deletion intact.** The
  docstring counts *rules* — no third-party SDK in the domain, nothing from Open
  WebUI — not tests. One guard per rule is what it describes; two guards for one
  rule is what contradicts it.
- **Keep the later guard.** Its message cites ADR-0002 and ADR-0015, and its
  `offenders` dict reports *which* forbidden root matched rather than only the
  file. It also sits in the process-boundary section whose comment block explains
  why a source-level grep test exists at all when the seam is physical.
- **Nothing outside the file references either constant.** `graft grep
  "FORBIDDEN_HOST_PACKAGES"` and `graft grep "OPEN_WEBUI_ROOTS"` each report hits
  in one file only, and `graft callers` finds no in-edges for the deleted test.

## Approach

One file, one commit:

1. Delete `test_no_module_imports_open_webui` and, with it,
   `FORBIDDEN_HOST_PACKAGES` — its only reader.
2. Leave `OPEN_WEBUI_ROOTS`, `test_nothing_in_the_package_imports_open_webui`,
   `_imported_roots`, and every other guard byte-identical.
3. Run the full suite: one fewer test, no other movement.

## Out of scope

- **Making the two guards genuinely differ.** The issue floats this as the
  alternative to deleting one — a domain-seam view and a process-boundary view
  that scan different things. There is nothing for a second view to scan: the rule
  is "*nothing* in the package may reach the host", the existing scan already
  covers the whole package, and any narrower second scan would be a strict subset
  of it. Inventing a distinction to justify keeping a duplicate is worse than the
  duplicate.
- **`FORBIDDEN_HOST_PACKAGES`' name.** `OPEN_WEBUI_ROOTS` survives as-is; no
  rename to inherit the other name's phrasing.
- **The other import-hygiene guards**, the child-interpreter helper and its tests
  (#80, `f2c0d13`), the module docstring, and the process-boundary comment block.
  All untouched.
- **Enforcing the rule anywhere new** — CI, packaging, or the image. ADR-0015
  already makes the seam physical; this test is the fast check and stays the only
  one.

## Acceptance

1. `tests/test_import_hygiene.py` contains exactly one guard asserting the Open
   WebUI rule, and exactly one set of forbidden host roots.
2. `grep -n "FORBIDDEN_HOST_PACKAGES\|open_webui" tests/test_import_hygiene.py`
   shows the constant gone and `OPEN_WEBUI_ROOTS` intact.
3. An injected `import open_webui` under `src/socratic/` fails
   `test_nothing_in_the_package_imports_open_webui`, with the offending path and
   the matched root in the message.
4. Full suite green, with exactly one test fewer than the baseline
   (623 passed, 7 skipped → 622 passed, 7 skipped) and no skips gained.
