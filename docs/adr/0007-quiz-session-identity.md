# 0007. Mint our own QuizSessionId

Date: 2026-09-08
Status: accepted

## Context

A quiz session needs a primary key. Three candidates were considered and two
turned out not to exist in usable form.

The Anthropic **Messages API is stateless** — each request is independent and
there is no server-side conversation object. Responses carry a per-message `id`
(`msg_...`) identifying one API call, not a thread. There is no Claude
conversation id to adopt.

Open WebUI's **`chat_id`** exists but is unreliable: Actions do not receive
`__chat_id__` as a reserved argument and must read `body["chat_id"]`, and it is
[not consistently present in internal/task calls](https://github.com/open-webui/open-webui/issues/20563).
Adopting it would also leak the host through the ADR-0002 portability seam.

## Decision

Mint a **`QuizSessionId` (ULID)** at authoring time and use it as the primary key.

Persist alongside it, as **host annotations** that are never keys and never
depended on: Open WebUI's `chat_id` and `session_id`, and **every Anthropic
response `message.id`** for the calls that produced the quiz and graded its
answers.

## Consequences

The domain owns its identity, so it survives a host swap unchanged and is immune
to the `chat_id` reliability bug. The `message.id` list is a genuine audit trail:
any stored quiz can be traced back to the exact API calls behind it, which is
stronger accountability than a borrowed conversation id would have given.

The cost is one more identifier to carry, and correlating a quiz back to what the
learner sees in Open WebUI's own chat list requires the annotation rather than
being automatic.
