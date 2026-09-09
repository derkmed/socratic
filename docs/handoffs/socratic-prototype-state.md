# Handoff — Socratic prototype, state of the build

Written 2026-09-08 from the repository itself, not from a working session.
**Revised the same day** against `main` @ `5ef5ae6`: the first draft was written
from a checkout seven commits behind `origin/main` and understated what had
landed. Corrections are marked below. Nothing here is a new decision; every claim below was checked
against the tree, the test suite, or the GitHub issue list on the day it was
written. Re-verify anything load-bearing before you rely on it.

## Goal

Finish the prototype described in [docs/specs/socratic-learning-app.md](../specs/socratic-learning-app.md):
a learner asks a question inside Open WebUI and gets back an interactive quiz
overlay instead of prose. The domain, the quiz service and the browser overlay
are built, **and so is the Open WebUI Pipe** — issue #12 is closed and
[PR #93](https://github.com/derkmed/socratic/pull/93) merged at `5ef5ae6`. Every
structural piece of the prototype now exists. What remains is a backlog of open
issues, several of which are live bugs on the path a real learner takes, plus
the two acceptance conditions that can only be met by observation on a real
install.

## Where the decisions already live

Do not re-open these. Each is written down with its reasoning:

- **[docs/CONTEXT.md](../CONTEXT.md)** — the vocabulary. Read it first; terms
  like *blank*, *rung*, *segment*, *call type*, *probe*, *attempt* are precise
  here and used precisely in the code.
- **[docs/adr/0001–0015](../adr/)** — the fifteen decisions and their reasons.
- **[docs/specs/](../specs/)** — one spec per shipped unit, plus the master
  spec above. The master spec's **Seams** table (line 25) is the testing
  contract; its **Acceptance** list (line 267, items 1–40) is the definition of
  done for the whole prototype.
- **[docs/maps/socratic-learning-app.md](../maps/socratic-learning-app.md)** —
  *historical only*. It records how the decisions were reached, and it says so
  in its own header; two of its statements were later revised. Read the ADRs
  for what is current.

The four decisions most likely to be accidentally relitigated, with the reason
that settles them:

| Decision | Reason it is not up for debate |
|---|---|
| Client holds authoritative quiz state; the model is near-stateless (ADR-0001) | The "reprint the whole quiz every turn" alternative is quadratic in tokens and fragile to parse. |
| Novice is pre-authored and graded with **zero** model calls; only Advanced is model-graded (ADR-0003) | A 2-option bank makes the wrong answer knowable at authoring time. ~1 call per Novice quiz instead of ~8. |
| Nothing streams (ADR-0013) | The reactive tutor line rides back in the same grading response, hidden behind the celebration animation. `client.js` has a test asserting no `EventSource`, no `WebSocket`, no `postMessage`. |
| The domain package imports nothing from Open WebUI (ADR-0002) | Portability seam. Enforced by an import-hygiene test, not by convention. |

## State

**Green.** `python -m pytest` on `main` @ `5ef5ae6`: **1024 passed, 2 skipped**
in ~6s. (The 890 figure in the first draft was the count at `a04c159`.)

Built and on `main`:

- `src/socratic/domain/` — types, mode registry, validation, output schemas,
  prompt assembly, `ModelClient` port, authoring, session, repositories,
  records, rating, tokens, profiles, profile builder.
- `src/socratic/adapters/anthropic_client.py` — the one real `ModelClient`.
- `src/socratic/rendering/` — Markdown, MathML, sanitiser, content assembly.
- `src/socratic/service/` — FastAPI quiz service: `app.py`, `security.py`,
  `deps.py`, `payloads.py`, `config.py`.
- `src/socratic/ui/` — `overlay.py` (the document), `client.js`, `overlay.css`.
  The most recent commits (`a04c159`, `bbf2967`, `cfdfa90`, `af4fc72`) landed
  the segment walk, one-request-per-interaction with a sliding capability
  token, and the resolved-blank gap fill.
- `compose.yaml` + `Dockerfile` — the two-container local setup.

**The Pipe — built, contrary to this document's first draft.**
`pipe/socratic_pipe.py`, with `tests/test_pipe.py` and
[docs/specs/open-webui-pipe.md](../specs/open-webui-pipe.md). It sits outside
`src/socratic/` because it runs in the other container and cannot import the
package. `__user__["id"]` → `LearnerId`, last user message → inquiry,
`POST /overlays` with the service token, and the returned document handed back
verbatim as an `HTMLResponse` with `Content-Disposition: inline`. It holds no
domain logic and makes no model calls (acceptance 38).

Also landed in the same stretch: `domain/inquiry.py` (queue the second question
or displace on request), `domain/settings.py` (the two per-learner settings),
and a `README.md`.

**Still unobserved:** acceptance 1 and 34 end to end in a real default Open
WebUI install — that needs Docker and an `ANTHROPIC_API_KEY`. The composition
one ring in has been measured: the real Pipe called the real quiz service, and
the overlay it returned reached that service by `fetch` from a `srcdoc` iframe
sandboxed without `allow-same-origin`, in headless Chrome and Edge, `Origin:
null`, preflighted. The README carries the manual smoke test.

**Untracked in the working tree** (uncommitted as of writing):
`CLAUDE.md`, `docs/agents/{domain,issue-tracker,triage-labels}.md`, and
`.claude/worktrees/`. The first two are the agent-skill conventions this repo
now runs on and are worth committing; `.claude/worktrees/` is scratch.

## Next actions

1. **Fix the bugs on the real learner path.** The Pipe now sits on top of this
   HTTP path, so each of these is reachable by a learner rather than latent. In
   rough dependency order:
   - #89 — `author_pedagogy` raises `AttributeError` for *any* quiz authored
     over HTTP.
   - #85 — an Advanced answer through the service is a 500: `/quizzes` hands
     the domain a `str` where a `DifficultyMode` is annotated.
   - #86 — every text segment renders as its own paragraph, so a blank reads
     as a block break rather than a gap in a sentence.
   - #56 — an Advanced wrong answer has no hint text on any rung, and rung
     three reveals nothing.
2. **~~Then build the Pipe~~ — done at `5ef5ae6`.** What is left of that thread
   is acceptance 34 on a real default install, which needs Docker and an API
   key rather than more code.
3. **Fix the test-suite honesty issues before trusting green.** #90 — a green
   local suite silently skips ~270 tests when the extras are not installed.
   #81 — nothing runs the suite on a clean checkout, which is how #79 stayed
   red on `main`.

`gh issue list` for the full backlog (16 open when first written; #12 and
#53 have closed since). Issue workflow,
triage labels and domain-doc conventions are in
[docs/agents/](../agents/) and summarised in `CLAUDE.md`.

## Open questions

- **#40 — segment 2 carries the whole answer key on every call type**, which
  contradicts the prompt's own scoping claim. This is a correctness question
  about ADR-0006's cache layout, not a typo; it may need an ADR amendment
  rather than a patch.
- **#41 — ADR-0014 and the spec say `output_config.format` takes
  `strict: true`; the SDK has no such field.** Documentation is ahead of
  reality; decide whether the docs or the intent is wrong.
- **#51 — offline model calls have nowhere to be accounted**: `ModelCallRecord`
  attaches only to an unsealed `QuizAttempt`, so the profile job's calls are
  unrecorded.

## Explicitly out of scope

From the master spec, and settled — do not build these:

- A real database. In-memory behind the repository ports; the ports *are* the
  migration path.
- Multi-tenant or hosted deployment; authentication (inherited entirely from
  Open WebUI — the capability token authorizes iframe→service calls, it is not
  a login); a scheduler for the profile job; the curation pipeline itself.
- Difficulty modes beyond Novice and Advanced. The registry makes them cheap;
  none are added here.
- Streaming (withdrawn with the parallel tutor call); Fast mode (it invalidates
  the prompt cache); `__event_call__` as an answer path (the iframe cannot
  invoke it); blanks nested inside a formula.
- Hardened Open WebUI deployments. The prototype targets a default install with
  `IFRAME_CSP` unset. Under the hardening docs' recommended CSP the iframe has
  no network path to the service at all — a different architecture, not a
  degraded one. This belongs in the README as a stated constraint.
