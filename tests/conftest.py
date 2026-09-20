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


def pytest_collection_modifyitems(items):
    """Run the Steam Deck tests before the rest of the suite.

    They are the only tests that drive a real Qt event loop - building
    windows, pumping events, sending key presses. Everything else here
    constructs widgets and never destroys them, so by the time the suite
    reaches them the process holds hundreds of widgets whose Python wrappers
    and C++ objects have come apart. Dispatching events in that state
    segfaults the interpreter inside unrelated code, the same class of
    failure this file's network fixture was written for, and it cannot be
    cleaned up afterwards: touching those widgets to delete them crashes too.

    Nothing depends on the order, so the fix is to let the tests that need a
    healthy process have one.
    """
    items.sort(key=lambda item: "test_steamdeck.py" not in str(item.path))
