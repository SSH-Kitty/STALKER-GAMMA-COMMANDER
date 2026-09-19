"""Test-wide fixtures.

Blocks real outbound network access for every test by default. Without
this, constructing a MainWindow/Dashboard (many tests do, incidentally,
just to exercise unrelated UI) schedules a real "check for updates" HTTP
request on a background QThread. In a sandboxed/offline environment that
request doesn't fail fast - it blocks for many seconds inside the TLS
handshake - and if the test (and its MainWindow) finishes and gets
garbage-collected before that thread does, the still-running background
thread can crash the interpreter (observed: a segfault inside
ssl.do_handshake, in a completely unrelated, later test) rather than a
clean, contained failure.

Every test that actually exercises network code already mocks
``urllib.request.urlopen`` itself (directly or via
``commander_gui.network``/``commander_gui.updates``, both of which are
the exact same underlying attribute) - this autouse fixture's own patch
simply becomes the "real" function those inner ``patch()`` calls save
and restore around, so it changes nothing for them.
"""

from unittest.mock import patch

import pytest


def _network_disabled_in_tests(*_args, **_kwargs):
    raise OSError("network access is disabled during tests")


@pytest.fixture(autouse=True)
def _block_real_network_calls():
    with patch("urllib.request.urlopen", side_effect=_network_disabled_in_tests):
        yield
