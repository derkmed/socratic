# A compose variable that nothing reads

## Goal

`compose.yaml:40` sets `SOCRATIC_SERVICE_URL_FOR_BROWSER` on the `open-webui`
container. Nothing in the repository reads it, and nothing could: the overlay's
browser-facing origin is the *service's* own `SOCRATIC_PUBLIC_URL`, held as
configuration in the other container and rendered into the document rather than
accepted on a request (#13, D15; `service/config.py`, `PUBLIC_URL_VAR`).

The defect is not the wasted line, it is that the line looks live. Someone
publishing the service on a host port other than 8080 would reasonably change
this one, restart, and find the overlay still fetching `http://localhost:8080` —
a failure that surfaces only in a browser console, inside a `srcdoc` iframe
sandboxed without `allow-same-origin`, which is close to the worst place in this
prototype for a symptom to appear.

Delete the line, repair the comment above it that presents both variables as
live, and leave behind a guard so the next dead compose variable fails a test
instead of misleading a reader. Closes #94.

## Seams

The change to `compose.yaml` has no seam of its own — it is data, not code. The
guard supplies one, and it is the only new code in the diff:

| Seam | Kind | Why it must exist |
|---|---|---|
| `_compose_environment(text) -> dict[str, set[str]]` | new | Maps service name to the `SOCRATIC_*` keys set on it. Pure, takes text rather than a path, so the guard's own parsing is testable against fixtures instead of only against the one file it guards — the shape `test_import_hygiene._imported_roots` and `test_encoding_hygiene._text_reads_without_encoding` already establish. |
| `test_every_socratic_variable_in_compose_is_read` | new | The guard itself, and the reproduction. Against `compose.yaml` at `f1b8a43` it fails naming `SOCRATIC_SERVICE_URL_FOR_BROWSER` on `open-webui`. |

## Decisions

- **Per-container, not repository-wide.** The naive rule — "the name appears
  somewhere in the repo" — would not have caught this cleanly, because a
  reviewer's instinct is that `SOCRATIC_SERVICE_URL_FOR_BROWSER` is *about*
  something real. The rule that catches it is the one the compose file is
  actually asserting: **a variable set on a container is read by the code that
  runs in that container.** `quiz-service` runs `src/socratic/`; `open-webui`
  runs `pipe/`. The dead variable is set on `open-webui`, and `pipe/` does not
  name it. As a bonus the same rule catches a service variable set on the wrong
  container, which is the neighbouring mistake.
- **Only `SOCRATIC_`-prefixed keys.** `ANTHROPIC_API_KEY` is read by the
  Anthropic SDK, not by this repository's source, and would need an allowlist
  entry under any broader rule. Scoping to the prefix makes the rule a
  self-consistency claim about names this project itself coined — no allowlist
  to maintain, and nothing to forget to add.
- **No reverse check.** The guard does not require that every variable the
  source reads is set in `compose.yaml`. `pipe/socratic_pipe.py` reads
  `SOCRATIC_MODE` with a default and compose deliberately leaves it unset;
  demanding the converse would fight that.
- **A hand-rolled scan, not PyYAML.** `pyproject.toml` keeps `dependencies = []`
  as a portability seam (ADR-0002) and the `dev` extra holds three packages. A
  YAML parser added for one hygiene test is a poor trade; the scan reads
  indentation and is itself covered by fixture tests, including the two shapes
  that could fool it — a `ports:` sequence at the same depth as `environment:`,
  and the `${VAR:?...}` host-substitution syntax on the value side, whose names
  must not be mistaken for keys.
- **The comment above the line needs the edit, not just the deletion.** It reads
  "The Pipe reaches the service by its compose DNS name; the iframe reaches it
  from the browser, which is why the two differ" — where "the two" are the two
  variables. Delete one and the sentence is about a variable that is no longer
  there. The replacement has to do the job the dead line pretended to: tell
  someone changing the published port where the browser-facing URL actually
  lives.

## Approach

1. Red: `tests/test_compose_hygiene.py` — the parser's fixture tests and the
   guard. The guard fails on `SOCRATIC_SERVICE_URL_FOR_BROWSER`.
2. Green: delete `compose.yaml:40`; rewrite the comment block above it.
3. Full suite.

## Out of scope

- **Wiring the variable through.** It would mean the host container telling the
  service what origin to render into the overlay — the caller-chosen URL
  `service/config.py` explicitly refuses (#13, D15).
- **`README.md`.** Its Pipe table already states the split correctly ("Not where
  the browser does — the service tells the overlay that itself, from
  `SOCRATIC_PUBLIC_URL`"). It was never wrong; only `compose.yaml` was.
- **Validating the `${VAR:?...}` host-side substitutions**, the published ports,
  or anything else in `compose.yaml`. One rule, one guard.
- **Running the guard in CI.** Nothing runs the suite on a clean checkout at all
  (#81); that is that issue's problem, not this one's.

## Acceptance

1. `grep -rn "SERVICE_URL_FOR_BROWSER" . --exclude-dir=.git` no longer reaches
   `compose.yaml`. It still reaches this spec and the guard's docstring, which
   are prose recording why the variable is gone; the point is that no container
   is configured with it.
2. The comment on the `open-webui` `environment:` block names
   `SOCRATIC_PUBLIC_URL`, on the other container, as the browser-facing origin —
   so the port-changing reader is sent to the right place.
3. `test_every_socratic_variable_in_compose_is_read` passes after the deletion,
   and fails naming the variable and its container when the line is restored.
4. Full suite green, above the 1045-passed / 2-skipped baseline by exactly the
   tests this spec adds.
