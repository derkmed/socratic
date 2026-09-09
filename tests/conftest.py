"""Fixtures shared across the suite.

Only one so far: the network guard the rendering tests use. ADR-0012's whole
argument for server-side MathML is that the rendered page needs no CDN, no web
font and no client script, and master spec acceptance 31 turns that into a
condition. A test that merely inspects the output proves the *page* asks for
nothing; this fixture proves the *renderer* does too, by making any attempt to
open a connection an error rather than a slow test.
"""

import socket

import pytest


class NetworkAccessDuringTest(AssertionError):
    """Raised when guarded code tries to open a connection."""


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if anything under it reaches the network.

    Patched at the socket layer rather than at any one client library, so it
    catches an `http.client`, a `urllib`, a `requests` or a transitive
    dependency's own fetch alike.
    """

    def refuse(*args, **kwargs):
        raise NetworkAccessDuringTest(
            "rendering opened a network connection; it must be a pure, "
            "in-process transformation (ADR-0012, acceptance 31)"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return refuse
