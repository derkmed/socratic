"""The quiz service: an HTTP API over the domain, in its own process.

The domain package running as its own process, a sibling container to Open
WebUI, owning the answer key, the repositories, the renderer and every model
call (D15, `docs/adr/0015-iframe-pipe-transport.md`, master spec §10).

Nothing under `socratic.domain` may import this package, and nothing here is a
seam: every route is a translation over `QuizAuthoring`, `QuizSession`,
`TokenMinter` or `submit_rating`. The **portability seam** is a process
boundary here rather than a convention — an Open WebUI import below it would
not resolve.
"""
