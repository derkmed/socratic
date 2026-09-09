# Research — how a rich HTML overlay can actually be delivered in Open WebUI 0.11.3

Date: 2026-09-09. Question: [issue #116](https://github.com/derkmed/socratic/issues/116)
established that a Pipe returning an `HTMLResponse` renders nothing. Given that,
**what delivery mechanisms for a rich HTML overlay do exist in the version we run**,
what does each cost, and which of them a Pipe can reach on its own.

This is an investigation. Nothing here is a decision.

## How this was established

Both containers were found **stopped** (`socratic-open-webui-1`, exit 137;
`socratic-quiz-service-1`, exit 0) and the brief forbade starting them, so the
install was read with `docker cp` out of the stopped container — read-only, and it
leaves the containers untouched. No Function, Tool, model or setting was created or
changed; nothing was posted anywhere.

Two provenance notes, because they change how much the citations below are worth:

- **Backend** line numbers are the real files at `/app/backend/open_webui/…` in the
  image the running install uses (`ghcr.io/open-webui/open-webui:main`).
- **Frontend** citations are *not* guesses from minified JS. The image ships
  `.map` files next to every bundle (`/app/build/_app/immutable/chunks/*.js.map`,
  725 of them) and they carry `sourcesContent` — the complete original Svelte and
  TypeScript for the exact build that is installed. Paths and line numbers below
  (`src/lib/components/…`) are that embedded original source, extracted from those
  maps. It is source of the shipped build, not of some other release.

## Findings

| Question | Answer | Established by |
|---|---|---|
| A Pipe's `HTMLResponse` matches no dispatch branch | True — `StreamingResponse`, `dict`, `str`, `Iterator`, `AsyncGenerator`, `BaseModel` are all `False` for it | **Measured** (isinstance run against `fastapi.responses.HTMLResponse`) |
| Non-streaming Pipe + `HTMLResponse` | Worse than blank: `choices[0]` gets **no `message` key at all** | Read from source |
| A fenced ` ```html ` block in a Pipe's `str` renders richly | **Yes** — auto-opens the Artifacts side panel, no setting to turn on | Read from source |
| …and the raw HTML is *also* printed into the chat as a code block | Yes, expanded, unless the learner sets `collapseCodeBlocks` | Read from source |
| …and the overlay is **re-wrapped**, not rendered verbatim | Yes — pasted into a generated `<!DOCTYPE html>…<body>` template | Read from source |
| …and a full standalone document survives that wrapping | **Yes**: inner `<style>` applies, inner `<script>` runs, inner `<title>` wins | **Measured** (spike below) |
| Tool/Action `HTMLResponse` path requires `Content-Disposition: inline` | Yes, and it is a substring test on the header | Read from source |
| Actions have the same `HTMLResponse` path as Tools | Yes — `utils/actions.py` calls the same `process_tool_result` | Read from source |
| That path's real mechanism is an `embeds` **event**, not a return value | Yes — `{'type': 'embeds', 'data': {'embeds': [html]}}` | Read from source |
| **A Pipe can emit that event itself** | Yes — `__event_emitter__` is handed to Pipes and the handler is type-agnostic | Read from source |
| …and embeds are persisted to the chat, with a `replace` flag | Yes, `upsert_message_to_chat_by_id_and_message_id(..., {'embeds': …})` | Read from source |
| …and rendered **verbatim** as `srcdoc`, inline in the message | Yes, via `FullHeightIframe` | Read from source |
| Any `dict` / `BaseModel` return shape that carries HTML to a rich render | **No.** The chunk consumer understands only `error`, `sources`, `selected_model_id`, `usage`, `choices[0].delta.content` | Read from source |
| Tools receive `__user__`, own `Valves`, own `UserValves` | Yes, all three | Read from source |
| Actions receive `__user__`, own `Valves`, own `UserValves` | Yes, all three — but no `__metadata__`, `__files__`, `__task__`, `__oauth_token__` | Read from source |
| `IFRAME_CSP` unset ⇒ `injectCsp` is a no-op on both paths | Yes | Read from source |
| Sandbox on both paths: `allow-scripts`, no `allow-same-origin` | Yes. `allow-forms` differs by surface: `?? true` on the message-level `message.embeds` frame (the Pipe path) and on CodeBlock/artifact; `?? false` only on the tool-call collapsible | Read from source |
| An embed frame may drive the prompt box; an artifact frame may not | Yes — only `FullHeightIframe` registers its window as trusted | Read from source |

## Q1 — does a fenced ` ```html ` block in a `str` render richly?

**Yes, on a default install, with no setting to switch on.** Two independent
surfaces react to it, and they behave differently.

**1. The Artifacts side panel, opened automatically.**
`src/lib/components/chat/Messages/ContentRenderer.svelte:139-166` runs on every
code-block token update:

```js
const isArtifact =
    ['html', 'svg'].includes(normalizedLang) ||
    (normalizedLang === 'xml' && code.toLowerCase().includes('<svg'));
…
if (
    ($settings?.detectArtifacts ?? true) &&
    !compactPreview && isArtifact && hasClosingCodeFence(raw) &&
    !autoOpenedArtifactIds.has(artifactId) && !$mobile && $currentChatId
) { … showArtifacts.set(true); showControls.set(true); }
```

`detectArtifacts` defaults to **true** (`?? true`), so the panel opens by itself.
It does not open on mobile (`!$mobile`), and it needs a complete block —
`hasClosingCodeFence` (L137) is `/(?:^|\n)```[ \t]*$/`, which a Pipe returning its
whole `str` at once satisfies trivially.

**2. A `Preview` button on the code block.**
`src/lib/components/chat/Messages/CodeBlock.svelte:522`:
`{#if preview && ['html', 'svg'].includes(lang)}`. `preview` is passed as
`preview={!readOnly}` from `ResponseMessage.svelte:837`, so in a live chat it is on.
Note this comparison is **not** lowercased, unlike the artifact check — ` ```HTML `
gets the auto-opening panel but no button.

**What actually goes in the iframe is not our document.** The panel's contents are
built in `src/lib/components/chat/Chat.svelte:1927-1979` (`getContents`, re-run on
every history change, L1872-1884). It walks every non-user message, extracts code
blocks with `getCodeBlockContents` (`src/lib/utils/index.ts:2202`), and then
**generates a host document around them**:

```js
const renderedContent = `
    <!DOCTYPE html>
    <html lang="en">
    <head> … <style> body { background-color: white; } ${group.css} </style> </head>
    <body>
        ${group.html}
        <script> ${group.js} </script>
    </body>
    </html>`;
contents = [...contents, { type: 'iframe', content: renderedContent }];
```

Our 31 KB standalone document would be interpolated at `${group.html}` — a whole
`<!DOCTYPE html><html><head>…` nested inside another document's `<body>`. That it
survives is measured, not assumed; see the spike below.

The frame itself, `Artifacts.svelte:243-264`:

```svelte
srcdoc={injectCsp(contents[selectedContentIdx].content, $config?.ui?.iframe_csp ?? '')}
sandbox="{($settings?.iframeSandboxAllowScripts ?? true) ? 'allow-scripts' : ''}…"
```

`IFRAME_CSP` defaults to `''` (`config.py:1745`, surfaced at `main.py:2403`) and
`injectCsp` returns the HTML unchanged when the CSP is empty (`src/lib/utils/csp.ts`).
Sandbox defaults: `allow-scripts` on, `allow-downloads` on, `allow-same-origin`
off. **`allow-forms` is not uniform across surfaces**: the message-level
`message.embeds` frame - the one a Pipe-emitted `embeds` event reaches - takes
`$settings?.iframeSandboxAllowForms ?? true`, as does the CodeBlock/artifact
frame. Only the tool-call collapsible's embeds take `?? false`. So the frame
option 3 uses has forms **on** by default.

**The costs of this route are not in the rendering, they are in the chat.** The
fenced block is still a code block: `MarkdownTokens.svelte:196` passes
`collapsed={$settings?.collapseCodeBlocks ?? false}`, so by default the learner sees
31 KB of syntax-highlighted HTML printed into the transcript, with the overlay in a
side panel beside it. And the panel is chat-wide, not message-bound — it collects
every artifact in the conversation and paginates them as "Version *n* of *m*".

## Q2 — what the Tool/Action `HTMLResponse` path actually is

`/app/backend/open_webui/utils/middleware.py:1006` `process_tool_result`. The
decisive lines, L1018-1032:

```python
result_context = None
if isinstance(tool_result, tuple) and len(tool_result) == 2 and isinstance(tool_result[0], HTMLResponse):
    tool_result, result_context = tool_result

if isinstance(tool_result, HTMLResponse):
    content_disposition = tool_result.headers.get('Content-Disposition', '')
    if 'inline' in content_disposition:
        content = tool_result.body.decode('utf-8', 'replace')
        tool_result_embeds.append(content)
```

- **`Content-Disposition: inline` is genuinely checked**, as a substring. Without it
  (L1068-1069) the branch falls through to
  `tool_result = tool_result.body.decode(...)` — the HTML becomes plain text fed to
  the model. `HTMLResponse` does not set the header itself; the author must.
- **The tuple form** `(HTMLResponse, result_context)` exists so the *model* gets
  something useful to reason about instead of the generic stand-in. When the second
  element is absent and the status is 2xx, the tool result the LLM sees is
  `{'status': 'success', 'code': 'ui_component', 'message': '<name>: Embedded UI result is active and visible to the user.'}`
  (L1035-1041); 4xx and 5xx map to error dicts of the same shape.
- **How it reaches the chat: an event, not a return value.** `middleware.py:1442-1449`:
  `await event_emitter({'type': 'embeds', 'data': {'embeds': tool_result_embeds}})`.
  This is the whole mechanism, and it is the reason Q3 has an answer.

**Actions have the identical path.** `/app/backend/open_webui/utils/actions.py:130-148`
calls the same `process_tool_result` with `tool_type='action'` and emits the same
event. So the return contract for an Action is exactly the Tool's: an `HTMLResponse`
with `Content-Disposition: inline`, optionally as a `(response, context)` tuple.

What a Tool must look like to hit it: a Tool function whose spec the model chooses to
call, returning that `HTMLResponse`. An Action instead appears as a **button under a
message** and is invoked by the learner, not the model.

## Q3 — can a Pipe reach a rich render by any other supported return type?

**By return type: no. By event emission: yes, and it is the same code path the Tool
path uses.**

**The negative half, first.** `functions.py:313-341` (streaming) and L352-358
(non-streaming) are the only branches. Measured, against real objects:

```
isinstance(res, StreamingResponse) = False   isinstance(res, Iterator)       = False
isinstance(res, dict)              = False   isinstance(res, AsyncGenerator) = False
isinstance(res, str)               = False   isinstance(res, BaseModel)      = False
```

so an `HTMLResponse` falls through to the empty finish chunk (L338-341) — the blank
message of #116. The non-streaming case is quietly worse: `get_message_content`
(L163-169) has no `Response` branch and returns `None`, and
`openai_chat_completion_message_template` (`utils/misc.py:808-825`) only sets
`choices[0]['message']` `if message is not None`. The completion comes back with a
choice that has no message at all.

Neither `dict` nor `BaseModel` helps, because nothing downstream reads HTML out of a
chunk. `src/lib/apis/streaming/index.ts` understands exactly five things —
`parsedData.error`, `.sources`, `.selected_model_id`, `.usage`, and
`choices?.[0]?.delta?.content ?? ''` — and there is no `embeds` handling anywhere in
`streaming_chat_response_handler` (`middleware.py:4217`+) outside the tool-call
block. A `dict` return can put text in the message, raise an error, or attach
citations. That is the whole vocabulary.

**The positive half.** Pipes are given `__event_emitter__`
(`functions.py:238-242`, `266-281`) whenever the metadata carries
`session_id`/`chat_id`/`message_id` — i.e. in any normal chat turn — and it is
injected if the `pipe` signature asks for it (`get_function_params`, L195-212). The
emitter, `/app/backend/open_webui/socket/main.py:1057`, dispatches on the event type
**with no notion of who emitted it**, and `embeds` is a first-class type there
(L1125-1141):

```python
elif event_type == 'embeds':
    event_payload = event_data.get('data', {})
    embeds = event_payload.get('embeds', [])
    if not event_payload.get('replace', False):
        existing_embeds = await Chats.get_message_metadata(chat_id, message_id, 'embeds')
        if isinstance(existing_embeds, list):
            embeds.extend(existing_embeds)
    await Chats.upsert_message_to_chat_by_id_and_message_id(
        chat_id, message_id, {'embeds': embeds}, touch=False)
```

The frontend half is equally agnostic: `Chat.svelte:1258-1268` handles
`chat:message:embeds` / `embeds` by setting `message.embeds`, and
`ResponseMessage.svelte:714-731` renders each entry **inline in the message**:

```svelte
<FullHeightIframe
    src={embed}
    allowScripts={true}
    allowForms={$settings?.iframeSandboxAllowForms ?? true}
    allowSameOrigin={$settings?.iframeSandboxAllowSameOrigin ?? false}
    allowPopups={true} />
```

`FullHeightIframe.svelte:53-67` auto-detects URL vs raw HTML and puts raw HTML into
`srcdoc` **verbatim** — no wrapper, unlike Q1. So concretely, a Pipe that keeps its
`HTMLResponse` body but does this instead:

```python
await __event_emitter__({'type': 'embeds', 'data': {'embeds': [html], 'replace': True}})
return ''
```

gets the overlay inline under its own message, persisted in the chat, with the same
sandbox as every other path. Three details that matter for our overlay:

- **Height.** `ResponseMessage` passes no `initialHeight`, so the frame has no height
  of its own until the content posts `{type: 'iframe:height', height: n}` to the
  parent (`FullHeightIframe.svelte:157-163`). Our overlay would have to do that.
- **It is trusted for the prompt bridge.** Embed frames register their window in
  `embedWindows` (`FullHeightIframe.svelte:3-5`), and `Chat.svelte:1407-1424` accepts
  `input:prompt` / `input:prompt:submit` / `action:submit` from them (behind a
  confirmation modal). The Artifacts panel builds a bare `<iframe>` and is **not**
  registered, so the same message from an artifact frame is silently dropped.
- **Persistence is skipped for unsaved chats** — `save_to_chat` requires
  `is_saved_chat_id` (`socket/main.py:1067`, `utils/chat_id.py:15`). The embed still
  renders live; it just is not there after a reload of a temporary chat.

## Q4 — learner identity and valves, per option

| | `__user__` | own admin `Valves` | own `UserValves` | invoked by |
|---|---|---|---|---|
| **Pipe** (today) | yes, `functions.py:277` | yes, L58-71 | yes, L205-211 (as `__user__['valves']`) | the learner's turn, as a model |
| **Tool** | yes, `utils/tools.py:311-345` | yes, `Tools.get_tool_valves_by_id`, L316-318 | yes, L319-322 | the **model deciding to call it** |
| **Action** | yes, `utils/actions.py:111-122` | yes, L85-87 | yes, L115-118 | the **learner clicking a button** under a message |

So the `__user__` → `LearnerId` mapping (`pipe/socratic_pipe.py:77-90`) and the
`Valves` block (L162-201) port across to either without loss of information; both
also get `__event_emitter__`, `__event_call__` and `__request__`. What does not port
is *what triggers the code*. A Pipe is a model the learner selects and its turn is
the learner's message. A Tool needs a real model turn that chooses to call it — an
LLM in the loop, and a tool-call decision we do not control. An Action needs the
learner to click a button attached to an existing message. Actions additionally do
not receive `__metadata__`, `__files__`, `__task__` or `__oauth_token__`; the Pipe
currently uses `__metadata__` to short-circuit title-generation tasks
(`socratic_pipe.py:221-222`), which would need another home.

## Spike — a standalone document inside the artifact wrapper

Date: 2026-09-09. Q1's one genuinely uncertain step is whether our full document
survives being interpolated into `${group.html}` inside another `<!DOCTYPE html>`.
This was **measured in the browser rather than reasoned about**, in a served page
(not `file://`) with the wrapper reproduced character-for-character from
`Chat.svelte:1941-1965` and the sandbox reproduced from `Artifacts.svelte`
(`allow-scripts allow-downloads allow-forms`, **no** `allow-same-origin`). The inner
document reported on itself by `postMessage`:

| Probe | Result |
|---|---|
| Children of the outer `<body>` | `META,TITLE,STYLE,DIV,SCRIPT` — inner `<html>`/`<head>`/`<body>` dropped, children hoisted |
| Inner `<script>` | ran (`INNER-SCRIPT-RAN`) |
| Inner `<style>` | applied (`rgb(0, 128, 0)`) |
| Inner `<title>` | won — `document.title === 'Quiz overlay'` |
| `document.querySelectorAll('html').length` | `1` |

**The nesting is survivable.** The HTML parser flattens the inner document rather
than rejecting it, and scripts and styles keep working. One consequence to keep:
the wrapper's `body { background-color: white; }` is emitted *before* our styles, so
ours win on equal specificity — but the wrapper is the outer `body`, and our own
`body` rules now apply to that same element, which is a coupling that did not exist
when we served the document whole.

Not measured: any of this inside the real install. The stack was down and the brief
forbade starting it.

## Assessment

Four candidate mechanisms; three are real.

**1. Pipe returning `HTMLResponse` — not viable.** Measured dead. Nothing about it
is a configuration problem; the branch does not exist.

**2. Pipe returning a `str` with a fenced ` ```html ` block — viable, cheap,
and it costs ADR-0001.** No Function changes beyond the return statement, no new
plugin, identity and valves untouched, and it works on a default install with no
setting. What it buys is not the surface we specified: the overlay lands in a
chat-wide side panel, not in the turn; the raw 31 KB is printed into the transcript
as an expanded code block by default; the document is re-wrapped rather than served
whole; nothing renders on mobile; and the frame is **not** trusted for the prompt
bridge. ADR-0001's "no reprinting, no chat-shaped turns" is exactly what this
violates. ADR-0015 is untouched — the sandbox and CSP defaults are the same, so the
`fetch` answer path still works.

**3. Pipe emitting an `embeds` event — viable, and it preserves the design.**
The overlay is inline in the assistant's own message, `srcdoc` verbatim, persisted,
updatable in place with `replace`, sandboxed identically, and trusted for the prompt
bridge as a bonus we do not need. It costs: adding `__event_emitter__` to the `pipe`
signature, returning a `str` instead of the response, and teaching the overlay to
post `iframe:height` — otherwise it has no height. `LearnerId`, `Valves` and the
`__metadata__` task short-circuit all stay where they are. ADR-0015 is unaffected in
substance; its "the Pipe returns the rendered HTML the service produced" wording
becomes "emits", and re-emitting `replace: True` reloads `srcdoc` and destroys
in-frame state — the same trap that ADR ascribed to the `postMessage` bridge, so it
must not be used for per-answer updates.

**4. Moving to a Tool or an Action — viable, and the most expensive.** Both reach
the identical embed, and both keep `__user__`, `Valves` and `UserValves`. The cost is
not plumbing, it is the trigger: a Tool puts an LLM's tool-call decision between the
learner and the quiz, and an Action puts a button click there. Either replaces "the
learner's turn *is* the quiz" with something else, which is a product change, not a
delivery change. A Tool or Action is what the upstream docs describe, and option 3
reaches the same code path without paying for it.

Worth recording independently of the choice: `open-webui-fit.md` says `allow-forms`
is opt-in. In 0.11.3 that is true only of the tool-call collapsible's embeds
(`?? false`). The message-level `message.embeds` frame and the CodeBlock/artifact
frame both default it **on** (`?? true`) - the default depends on which surface
renders, which the old wording flattened. `allow-same-origin` is off by default
everywhere (`?? false`), so ADR-0015's opaque-origin reasoning stands.

## Open questions

- **Does the `embeds` route work end to end in the real install?** Everything above
  is source and one browser measurement; nothing was exercised against the running
  Open WebUI, because the containers were stopped and starting them was out of
  scope. The experiment that settles it: bring the stack up, add
  `__event_emitter__` to the Pipe signature in a scratch copy of the Function, emit
  one embed, and confirm the iframe appears in the message and survives a reload.
  That requires creating or editing a Function, which this brief forbade.
- **How large an embed the socket will carry.** No `max_http_buffer_size` is set on
  the `AsyncServer` (`socket/main.py:89-116`), and that limit governs inbound
  payloads rather than server→client emits, so 31 KB looks unproblematic — but this
  is inference, not a measurement. Emitting a document of the real size would settle
  it in one turn.
- **Whether `iframe:height` is enough for our overlay's layout**, or whether the
  inline frame needs an explicit `initialHeight` that `ResponseMessage` never passes.
  Settled by rendering the real overlay in an embed and watching it resize.
- **What the artifact panel does with two overlays in one conversation.** It
  paginates them as versions of one artifact (`Artifacts.svelte:94-121`) and jumps to
  the newest. Whether a second quiz reads as "Version 2 of 2" of the first is a UX
  question this reading cannot answer; it needs two real turns.

## Sources

All inside the running image `ghcr.io/open-webui/open-webui:main` (0.11.3):

- `/app/backend/open_webui/functions.py`
- `/app/backend/open_webui/utils/middleware.py`
- `/app/backend/open_webui/utils/actions.py`
- `/app/backend/open_webui/utils/tools.py`
- `/app/backend/open_webui/utils/misc.py`, `utils/chat_id.py`
- `/app/backend/open_webui/socket/main.py`
- `/app/backend/open_webui/config.py`, `main.py`
- `/app/build/_app/immutable/chunks/*.js.map` → `src/lib/components/chat/{Chat,Artifacts}.svelte`,
  `src/lib/components/chat/Messages/{ContentRenderer,CodeBlock,ResponseMessage}.svelte`,
  `src/lib/components/chat/Messages/Markdown/MarkdownTokens.svelte`,
  `src/lib/components/common/FullHeightIframe.svelte`,
  `src/lib/apis/streaming/index.ts`, `src/lib/utils/index.ts`, `src/lib/utils/csp.ts`
