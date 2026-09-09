"""The quiz UI: the overlay document the learner plays a quiz in (#13, §11).

A package **outside** `socratic.domain`, for the same reason `socratic.service`
is one: it reaches `socratic.rendering` for the highlight CSS the iframe has to
inline, and the domain is stdlib-only (`tests/test_import_hygiene.py`).

It declares no extra of its own. The service already carries the `rendering`
one, and the overlay is only ever rendered by the service — the Pipe cannot
import this package, it is pasted Python in another container.
"""
