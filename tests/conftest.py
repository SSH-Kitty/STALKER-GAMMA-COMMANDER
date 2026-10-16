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

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# No real windows: same as CI, and nothing pops up on the desktop mid-run.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _network_disabled_in_tests(*_args, **_kwargs):
    raise OSError("network access is disabled during tests")


@pytest.fixture(autouse=True)
def _block_real_network_calls():
    # Also skip urlopen_with_retry's backoff (patching only network's own
    # ``time`` name, not the global module): a page's background fetch sat
    # ~2s in those sleeps, outlived its test and this patch, made its last
    # attempt for real, then delivered the result to a destroyed page during
    # a later test's processEvents() - a segfault in CI.
    with (
        patch("urllib.request.urlopen", side_effect=_network_disabled_in_tests),
        patch("commander_gui.network.time"),
    ):
        yield


def _import_every_ui_module():
    import importlib
    import pkgutil

    import commander_gui.ui
    import Steamdeck
    import Steamdeck.screens

    for package in (commander_gui.ui, Steamdeck, Steamdeck.screens):
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            importlib.import_module(info.name)


@pytest.fixture(autouse=True)
def _never_run_the_real_cli():
    """CLI commands fail at spawn instead of running the bundled binary.

    Building a MainWindow or Dashboard starts background CLI queries. The
    real ``stalker-gamma`` writes its log under ``$XDG_CONFIG_HOME``, and
    tests that point that at their own ``TemporaryDirectory`` delete it at
    the end of the test body - before ``_stop_background_work_after_each_test``
    has waited for those queries. A CLI process still running then wrote its
    log mid-delete: ``OSError: Directory not empty`` in a random one of ~30
    tests. Only ``cli_runner``'s own lookup is replaced: ``mod_install``
    still finds the bundled 7zz next to it. Tests that need CLI output patch
    ``run_sync``/``CommandRunner`` themselves, as before.
    """
    with patch(
        "commander_gui.cli_runner.cli_binary_path",
        return_value=Path("/nonexistent/stalker-gamma-disabled-in-tests"),
    ):
        yield


@pytest.fixture(autouse=True)
def _ignore_the_real_game_and_mo2():
    """Answer "not running" wherever the UI asks about MO2 or the game.

    Those checks look at the real machine: with GAMMA running on it (a
    developer playing while the suite runs), the UI refused the action in a
    modal box, which never closes offscreen - the suite hung for good. Every
    module is imported first so none keeps the real check. ``common``'s own
    copies stay real for their own tests, which fake ``subprocess.run``.
    Tests that need "running" still patch these themselves, on top.
    """
    from commander_gui.ui import common

    _import_every_ui_module()
    real = {common.mo2_running, common.game_running}
    patches = [
        patch.object(module, name, return_value=False)
        for module_name, module in list(sys.modules.items())
        if module_name.startswith(("commander_gui.", "Steamdeck"))
        and module is not common
        for name in ("mo2_running", "game_running")
        if getattr(module, name, None) in real
    ]
    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


@pytest.fixture(autouse=True)
def _stop_background_work_after_each_test():
    """Join every BackgroundTask/CommandRunner a test left running, with
    result delivery suppressed, then re-arm it - the same teardown a live
    language switch uses. Otherwise a task finishes during a *later* test's
    processEvents() and its handler touches a page that is already gone,
    segfaulting the suite (seen intermittently in CI).

    Then delete every top-level widget the test created. Tests build pages
    and windows and drop them; left alive, hundreds piled up and a later
    test's processEvents() fired a timer on one whose C++ side was already
    half gone - a segfault ~30% into a serial run. Widgets from setUpClass
    exist before this fixture starts, so class-shared windows survive."""
    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    app = qtwidgets.QApplication.instance() if qtwidgets else None
    before = set(map(id, app.topLevelWidgets())) if app else set()
    yield
    common = sys.modules.get("commander_gui.ui.common")
    if common is not None:
        common.shutdown_active_runners(timeout_ms=10000)
        common.resume_after_shutdown()
    qtwidgets = sys.modules.get("PySide6.QtWidgets")
    app = qtwidgets.QApplication.instance() if qtwidgets else None
    if app is None:  # test never touched Qt
        return
    from PySide6.QtCore import QEvent

    for widget in app.topLevelWidgets():
        if id(widget) not in before:
            widget.hide()
            widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _private_data_home(tmp_path, monkeypatch):
    """Keep save/settings backups (commander_gui.game_backup) out of the real
    ``~/.local/share``: installs, updates and repairs now back up settings
    first, and many tests drive those flows against temporary installs."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    # Same for settings.json / gui-settings.json: tests that save GUI state
    # (an install's resume point, window size...) otherwise wrote it into
    # the developer's real ~/.config/stalker-gamma.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def _no_unmocked_popups():
    """A themed pop-up (commander_gui.ui.dialogs) that a test forgot to
    mock would wait for a click forever; fail the test instead."""
    from PySide6.QtWidgets import QDialog

    real_exec = QDialog.exec

    def guarded(dialog):
        if dialog.objectName() == "messageDialog":
            raise AssertionError(f"unmocked pop-up: {dialog.windowTitle()!r}")
        return real_exec(dialog)

    with patch.object(QDialog, "exec", guarded):
        yield


def pytest_collection_modifyitems(items):
    """Run the Steam Deck (and desktop event-loop) tests before the rest.

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
    early = ("test_steamdeck.py", "test_desktop_event_loop.py")
    # Single tests elsewhere that show a window and pump its events: run
    # late, this one segfaulted the suite intermittently.
    early_tests = ("test_profiles_page_refills_a_long_compare_table_quickly",)
    items.sort(
        key=lambda item: item.path.name not in early and item.name not in early_tests
    )


def pytest_sessionfinish(session, exitstatus):
    """Stop every background thread the suite left running before Python
    exits. Tests build pages that start QThreads (dependency probes, size
    scans) and never wait for them; when interpreter teardown then destroys a
    QThread that is still running, Qt aborts the process ("QThread:
    Destroyed while thread is still running") and the run ends in a core
    dump even though every test passed - which a CI job reads as failure."""
    try:
        from commander_gui.ui.common import shutdown_active_runners
    except Exception:  # noqa: BLE001 - never mask the test results
        return
    shutdown_active_runners(timeout_ms=15000)
