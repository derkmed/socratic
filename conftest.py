"""Root `conftest.py`: it registers one plugin and holds nothing else.

`tests/gating.py` reports the test modules an absent optional extra kept out of
the run (#90), and adds `--require-extras`. Both of its hooks have to be
registered before the command line is parsed, which is a thing only the
*initial* conftest — this one, at the rootdir — or a `-p` plugin may do.
`tests/conftest.py` is loaded too late for `pytest_addoption` and is left to
its fixtures.

The hooks live in `tests/gating.py` rather than here so that the same module
can be loaded as a plugin by `tests/test_extras_gate.py`, which runs a real
`pytest` over a scratch directory outside the repository — where this file does
not apply, and so registers nothing twice.
"""

from tests.gating import pytest_addoption, pytest_configure  # noqa: F401
