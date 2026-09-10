# socratic

A learner asks a question inside Open WebUI and gets back **a quiz instead of
an answer**: a 200–250 word explanation with key terms masked as numbered
blanks, played as an interactive overlay. They resolve blanks one at a time
against a three-rung hint ladder, are periodically asked *how did you arrive at
that?*, and finish with an optional rating. Every guess and every
self-explanation is recorded in the order made and folded into a learner
profile that shapes later quizzes.

A prototype. It runs locally on Mac, Windows and Linux as two containers and
stores everything in memory behind repository ports.

**New here? Go to [Quickstart](#quickstart).** Then, when you want the *why*:

- `docs/CONTEXT.md` — the vocabulary. Terms mean exactly what it says.
- `docs/adr/` — the decisions, and why.
- `docs/specs/socratic-learning-app.md` — the master spec.

## Quickstart

Roughly fifteen minutes, most of it image pulls. Four steps, in order — the
Pipe is pasted into a *running* Open WebUI, so Open WebUI has to exist first.

### 0. What you need first

| | |
|---|---|
| **Docker** with Compose v2 | `docker compose version` should print `v2.x`. Docker Desktop on Mac/Windows, Docker Engine on Linux. |
| **An Anthropic API key** | Authoring a quiz makes a live model call. There is no offline or stub mode. Get one at <https://console.anthropic.com/>. |
| **Open WebUI** | The host application. You do **not** have to install it separately if you use the compose path below — it comes up as the second container. |
| Python 3.10+ | Only if you want to run the test suite. Not needed to use the app. |

**Setting up Open WebUI itself** — installing it, running it from source,
upgrading it, or getting it working outside this compose file — is documented
in its own repository: **<https://github.com/derkmed/open-webui-socratic>**, a
fork of Open WebUI that carries this repository as a `socratic` submodule.
Start there if you are new to Open WebUI, if you already run an Open WebUI you
want to keep, or if `docker compose up` below does not get you a working
Open WebUI. Get Open WebUI up and reachable in a browser *before* you come back
here to import the Pipe.

### 1. Get the code and set two secrets and a key

Write them into a `.env` file beside `compose.yaml`. Compose reads it
automatically, it is already gitignored, and — unlike a shell export — it is
still there tomorrow:

```sh
git clone https://github.com/derkmed/socratic.git
cd socratic

cat >> .env <<'EOF'
SOCRATIC_TOKEN_SECRET=replace-me
SOCRATIC_SERVICE_TOKEN=replace-me
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

To generate the two secrets, run this **once** and paste the output into the
file above:

```sh
python -c "import secrets;print(secrets.token_urlsafe(32));print(secrets.token_urlsafe(32))"
```

> **Generate them once, not once per session.** Both secrets are values that two
> parties have to agree on, so minting fresh ones on an install that already
> works is what breaks it — and neither failure names its cause:
>
> - A new `SOCRATIC_SERVICE_TOKEN` no longer matches the one saved in the Pipe's
>   valves, and authoring fails with **HTTP 401**. Open WebUI stores a valve once
>   it is saved, and a stored valve outranks the environment from then on, so
>   fixing the container's variable alone does not fix this — see step 3.
> - A new `SOCRATIC_TOKEN_SECRET` invalidates every capability token already
>   minted, so quizzes *author* fine and then reject every **answer**.
>
> If you export either variable in your shell, that export **overrides `.env`**
> for as long as the shell lives. That is the usual way an install that worked
> yesterday returns a 401 today. `unset SOCRATIC_SERVICE_TOKEN` before
> `docker compose up` to put `.env` back in charge.

Neither secret has a default: a missing one fails at startup rather than
silently weakening. On Windows PowerShell, use `$env:NAME = "value"` if you
export rather than using `.env`.

Three values, and they are not interchangeable:

| Value | Who holds it | What it is for |
|---|---|---|
| `ANTHROPIC_API_KEY` | quiz service only | Authoring a quiz. The Pipe never calls a model. |
| `SOCRATIC_SERVICE_TOKEN` | the Pipe **and** the service | The Pipe's credential for calling the service. The two must match. |
| `SOCRATIC_TOKEN_SECRET` | quiz service only | Signs the short-lived **capability token** the overlay carries. Never leaves the service's process. |

### 2. Start both containers

```sh
docker compose up
```

Open WebUI comes up on <http://localhost:3000> and the quiz service on
<http://localhost:8080>. Open <http://localhost:3000>, create the first
account — it becomes the **admin**, which the next step needs — and leave it
running.

> Already running your own Open WebUI, and only want the quiz service from
> here? Start it alone with `docker compose up quiz-service`, then give your
> Open WebUI `SOCRATIC_SERVICE_URL` (where *its* process reaches the service)
> and the same `SOCRATIC_SERVICE_TOKEN`, or set both by hand as Valves in
> step 3. If the service is not on the same host as the browser, set
> `SOCRATIC_PUBLIC_URL` too — see the table in step 3.

### 3. Install the Pipe

The Pipe is pasted, not installed — it runs inside the Open WebUI container,
which does not have this package.

1. Open WebUI → **Bottom Left User Bubble → Admin Panel → Functions →
   Create**, which is `/admin/functions`. Functions used to sit under Workspace
   and older guides still say so; as of 0.11.3 the Workspace tabs are Models,
   Knowledge, Prompts, Skills and Tools. The page is admin-only — a
   non-admin account is redirected away rather than shown an empty list.
2. Paste the whole of `pipe/socratic_pipe.py`, save, and enable it.
3. Check its **Valves**. They default from the environment `compose.yaml`
   already sets, so on a *first* `docker compose up` there is nothing to
   change:

   | Valve | Default | What it is |
   |---|---|---|
   | `service_url` | `$SOCRATIC_SERVICE_URL`, else `http://quiz-service:8080` | Where the *Pipe* reaches the service, inside the compose network. Not where the browser does — the service tells the overlay that itself, from `SOCRATIC_PUBLIC_URL`. |
   | `service_token` | `$SOCRATIC_SERVICE_TOKEN` | The Pipe's credential. Must match the service's. Not a learner's capability token. |
   | `mode` | `novice` | The difficulty mode to author in, install-wide. |
   | `timeout_seconds` | `120` | Authoring makes a model call. |

   Those are **defaults, not bindings**. Open WebUI stores a valve the moment
   the Function is saved, and from then on the stored value wins and the
   environment is ignored. So if you change `SOCRATIC_SERVICE_TOKEN` later,
   recreating the containers updates the *service* and leaves the *Pipe* still
   presenting the old one — the 401 in the table below. Change it here too.

### 4. Ask a question

Start a new chat, pick **socratic** as the model, and ask something. A quiz
overlay should appear and accept an answer. That round trip — Pipe → service →
model → overlay → service — is the whole system, and it is also the manual
smoke test the automated suite cannot perform (see [Tests](#tests)).

### When it does not work

| Symptom | Likely cause |
|---|---|
| An **empty reply**, with no error anywhere | The Pipe you pasted is out of date. The overlay travels on an `embeds` event; a Pipe that *returns* it renders nothing at all, and says nothing about why ([ADR-0017](docs/adr/0017-the-overlay-is-emitted-not-returned.md)). Re-paste `pipe/socratic_pipe.py`. |
| `docker compose up` exits complaining about a variable | One of the three values in step 1 is unset in this shell. Export it or put it in `.env`. |
| No **socratic** model in the chat picker | The Function was saved but not *enabled*, or you are signed in as a non-admin who cannot see `/admin/functions`. |
| The overlay renders but answering does nothing | The browser cannot reach the service. Open <http://localhost:8080> directly; if that works but answering does not, `SOCRATIC_PUBLIC_URL` is wrong for where the *browser* is. |
| `401` when **authoring** | The Pipe's `service_token` Valve and the service's `SOCRATIC_SERVICE_TOKEN` differ. Usually because the secret was regenerated, or a shell `export` is shadowing `.env`. Recreating the container is not enough: the Pipe's valve is *stored*, so update it in step 3 as well. Compare without printing either: `docker compose exec quiz-service sh -c 'printf %s "$SOCRATIC_SERVICE_TOKEN" \| sha256sum \| cut -c1-12'`. |
| Quizzes author, then every **answer** is refused | `SOCRATIC_TOKEN_SECRET` changed. It signs capability tokens, so a new value invalidates every overlay already on screen. Ask a fresh question. |
| A changed key or secret has no effect | Compose fixed the environment when the container was **created** — recreate rather than restart, below. |
| Nothing renders and the console shows a blocked `fetch` | `IFRAME_CSP` is set on your Open WebUI. That is out of scope, structurally — see [the constraint](#constraint-a-default-open-webui-install-only). |

### Changing one of those values later

`compose.yaml` interpolates `SOCRATIC_TOKEN_SECRET`, `SOCRATIC_SERVICE_TOKEN`,
`ANTHROPIC_API_KEY` and `SOCRATIC_PUBLIC_URL` from your environment or from a
`.env` file beside it, and a container's environment is fixed when the container
is **created**. `docker compose restart` reuses the containers it already has,
so it restarts the process with the environment it was born with: edit
`ANTHROPIC_API_KEY`, restart, and the service keeps serving the old key with no
symptom other than behaviour that matches the value you just replaced. Recreate
rather than restart, from the directory holding `compose.yaml`:

```sh
docker compose up -d --force-recreate
```

Naming a service — `docker compose up -d --force-recreate quiz-service` — limits
the blast radius to the container whose value changed and leaves the other one
up. From anywhere else, point compose at the checkout, which is also where it
looks for `.env`:

```sh
docker compose --project-directory <path-to-checkout> up -d --force-recreate
```

## The two containers

```
  Open WebUI  ──(the Pipe: POST /overlays, server to server)──▶  quiz service
      │                                                              ▲
      │  renders the overlay in a sandboxed srcdoc iframe            │
      └──────────(the learner's browser: fetch, per answer)──────────┘
```

**quiz service** (`src/socratic/`) — the domain package in its own process. It
owns the answer key, the repositories, the renderer and every model call.

**The Pipe** (`pipe/socratic_pipe.py`) — Open WebUI's adapter, and nothing
else. It maps `__user__` to a `LearnerId`, calls the service, and returns the
overlay the service rendered. It holds no domain logic and makes no model
calls.

The split is not tidiness: it makes the **portability seam** a process boundary
rather than a convention ([ADR-0015](docs/adr/0015-iframe-pipe-transport.md)).
Open WebUI is not installed in the service's image, so an import of it below
the seam would not resolve.

Answers do **not** travel back through the Pipe. The overlay `fetch`es the quiz
service directly from inside the iframe, carrying a short-lived **capability
token** that the service rotates on every graded response. The Pipe is called
once, to start a quiz.

## The collected records

`docker compose up` collects data. Every quiz that finishes — or that a learner
abandons by starting something else — is written to `./data/` as JSON, along
with any rating it was given:

```
data/
├── attempts/
│   └── derek%40example.com/
│       └── 01K4S9F2Q7X8M3N6P0R5T2V9WZ.json
└── ratings/
    └── derek%40example.com/
        └── 01K4S9F2Q7X8M3N6P0R5T2V9WZ.json
```

One file per record, in a tree that mirrors the `learner_id` partition
([ADR-0005](docs/adr/0005-persistence-contract.md)); the partition key is
percent-encoded, because it is a learner id that became a path segment. Each
file is a self-describing envelope around the record exactly as it was stored:

```json
{
  "schema_version": "1",
  "kind": "attempt",
  "written_at": 1757332800000,
  "record": {
    "attempt_id": "01K4S9F2Q7X8M3N6P0R5T2V9WZ",
    "learner_id": "derek@example.com",
    "mode": "novice",
    "topic": "the second law",
    "outcome": "resolved",
    "created_at": "2026-09-08T12:00:00+00:00",
    "sealed_at": "2026-09-08T12:07:31+00:00",
    "quiz": { "explanation": [ { "kind": "text", "text": "Heat flows because " } ] },
    "guesses": [ { "blank_id": "b1", "verdict": "correct", "graded_by": "deterministic" } ]
  }
}
```

**It is written, never read.** The service boots with empty repositories every
time, so a restart still loses any quiz that was in flight — these files look
like durable state and are not. They are the audit trail, and the corpus the
offline profile job scans
([ADR-0018](docs/adr/0018-collected-records-on-disk.md)).

Two knobs, both on the `quiz-service` container:

| Variable | Default | What it does |
| --- | --- | --- |
| `SOCRATIC_DATA_DIR` | `/data` | Where the trail goes. **Unset it and nothing is collected at all** — the service runs exactly as it did before this existed. An unwritable path refuses the boot rather than collecting nothing quietly. |
| `SOCRATIC_DATA_FLUSH` | `on_close` | `on_close` writes an attempt once, when it seals or is abandoned. `every_save` writes on every guess and probe, so you can watch files appear while you take a quiz — useful for a demo, at the cost of rewriting a growing document up to ~120 times. |

Neither is a learner setting, and neither is accepted from a caller: the
difficulty mode shapes a quiz, but an audit trail a caller can reshape
per-request is not an audit trail.

`./data/` is gitignored. The whole record goes to disk, **answer keys
included** — [ADR-0003](docs/adr/0003-grading-authority-and-key-custody.md)'s key
custody governs what crosses to the browser, and a redacted attempt would be
worthless to the profile job — so being untracked is the only control on it.

## Constraint: a default Open WebUI install only

The prototype targets a **default install**, meaning `IFRAME_CSP` is unset,
which is what Open WebUI ships. **Hardened deployments are out of scope, and
will not work** — not degrade, not work.

The reason is structural. Open WebUI renders a rich-UI iframe sandboxed
*without* `allow-same-origin`, so the overlay has an opaque origin, carries no
cookie and no host token, and reaches the quiz service by `fetch` with
`Origin: null`. That works on a default install: measured in Chrome and Edge,
preflighted, with `Access-Control-Allow-Origin: *` and no credentials
(`docs/research/open-webui-fit.md`). Under the hardening docs' recommended
`IFRAME_CSP` (`connect-src 'none'`) there is no `fetch`, no XHR, no WebSocket
and no EventSource to any origin — including ours. The iframe then has **no
network path to the service at all**.

There is no fallback, by decision. The one sanctioned channel out of a hardened
iframe is a `postMessage` prompt-submission bridge that starts a new chat turn,
requires a confirmation click, and reassigns `srcdoc` — a full reload that
destroys in-frame state. `__event_call__` is not an alternative either: it is a
server-side Socket.IO call that renders a modal in the *parent* page, which the
iframe cannot invoke. Either would be a different architecture, not a degraded
path, and supporting it would mean promising only what the structurally weaker
one can deliver. See [ADR-0015](docs/adr/0015-iframe-pipe-transport.md).

## Tests

```sh
pip install -e ".[all]"
pytest
```

The domain package has **no base dependencies** and is testable with no
third-party package installed at all; the Anthropic SDK, the renderer and the
service are optional extras, and the tests for each skip without theirs. No
test opens a socket or calls the model — so no API key, and no running
container, is needed to run the suite.

`all` is the install that leaves nothing gated. Each extra's tests are gated
by a single `pytest.importorskip` for the whole module, so a missing extra
costs a *module*, not a test, and the run stays green — 316 tests apart, on the
same commit. Any run that had a module gated out ends with a summary naming it,
the extra it wanted and how big it was; `pytest --require-extras` turns that
summary into a failure, which is what CI's installed job uses.

Both conditions run in CI on every push and pull request
([`.github/workflows/tests.yml`](.github/workflows/tests.yml)): `bare` is an
interpreter with `pytest` alone — no editable install, no extra — because that
is the condition the import-hygiene guards are about and the one nobody
develops in.

What the suite cannot cover is the outermost ring — this Pipe, inside a real
Open WebUI, reaching a real service. That needs a container runtime and an API
key. The manual smoke test is [step 4](#4-ask-a-question): ask a question, and a
quiz overlay should appear and accept an answer.
