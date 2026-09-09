"""Adapters — the modules allowed to know about the outside world.

Everything under `socratic.domain` imports only the standard library and
itself; this package is the other side of that line (ADR-0002, CONTEXT:
Portability seam). `anthropic_client` is the one module in the codebase
permitted to import the Anthropic SDK, which is why the SDK is an optional
extra rather than a base dependency: the domain installs and tests without it.
"""
