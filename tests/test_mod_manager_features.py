"""Drag-and-drop install, Update from Archive, per-mod conflicts, update
notifications."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from commander_gui.modlist import mod_conflicts
from commander_gui.ui.dialogs import MessageBox as QMessageBox
from commander_gui.ui.mod_manager_page import _mod_name


def _write(root: Path, relative: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


class ModConflictsTest(unittest.TestCase):
    """Priority is file order: earlier in modlist.txt wins."""

    def setUp(self):
        self.mods = Path(tempfile.mkdtemp())
        _write(self.mods, "High/gamedata/scripts/a.script")
        _write(self.mods, "Mine/gamedata/scripts/a.script")
        _write(self.mods, "Mine/gamedata/Configs/b.ltx")
        _write(self.mods, "Mine/meta.ini")  # not a game file
        _write(self.mods, "Low/gamedata/configs/B.ltx")
        _write(self.mods, "Low/meta.ini")
        _write(self.mods, "Off/gamedata/scripts/a.script")
        self.lines = ["+High", "+Mine", "+Low", "-Off", "+Cat_separator"]

    def test_overrides_and_overridden_by_follow_the_list_order(self):
        overrides, overridden_by = mod_conflicts(self.lines, self.mods, "Mine")
        self.assertEqual(overridden_by, {"High": ["gamedata/scripts/a.script"]})
        # Case-insensitive, like the game and MO2's virtual filesystem.
        self.assertEqual(overrides, {"Low": ["gamedata/Configs/b.ltx"]})

    def test_disabled_mods_are_left_out_unless_asked_about(self):
        _o, overridden_by = mod_conflicts(self.lines, self.mods, "Mine")
        self.assertNotIn("Off", overridden_by)
        overrides, _b = mod_conflicts(self.lines, self.mods, "Off")
        self.assertEqual(set(overrides), set())  # Off is last: everyone beats it
        _o, beaten_by = mod_conflicts(self.lines, self.mods, "Off")
        self.assertEqual(set(beaten_by), {"High", "Mine"})

    def test_unknown_mod(self):
        self.assertEqual(mod_conflicts(self.lines, self.mods, "Nope"), ({}, {}))


class _Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _page(self):
        from PySide6.QtWidgets import QStatusBar

        from commander_gui.settings import CliProfile, CliSettings
        from commander_gui.ui.mod_manager_page import ModManagerPage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}),
            patch("commander_gui.ui.mod_manager_page.mo2_running", return_value=False),
            patch("commander_gui.ui.mod_manager_page.ModManagerPage.refresh"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        class FakeWindow:
            settings = CliSettings(
                profiles=[CliProfile(active=True, profile_name="M", gamma=self.tmp.name)]
            )
            install_busy = False
            install_operation = None

            def set_install_busy(self, busy, operation=None):
                self.install_busy = busy
                self.install_operation = operation if busy else None

            def refresh_settings(self):
                pass

            def update_mod_counter(self):
                pass

            def statusBar(self):
                return QStatusBar()

        return ModManagerPage(FakeWindow())


class DragDropTest(_Page):
    def test_only_local_mod_archives_are_taken(self):
        from commander_gui.ui.mod_manager_page import dropped_archives

        tmp = Path(tempfile.mkdtemp())
        for name in ("a.zip", "b.7Z", "c.txt"):
            (tmp / name).write_text("x", encoding="utf-8")
        mime = QMimeData()
        mime.setUrls(
            [QUrl.fromLocalFile(str(tmp / n)) for n in ("a.zip", "b.7Z", "c.txt", "gone.zip")]
            + [QUrl("https://example.com/x.zip")]
        )
        self.assertEqual(dropped_archives(mime), [tmp / "a.zip", tmp / "b.7Z"])
        self.assertEqual(dropped_archives(QMimeData()), [])

    def test_several_archives_install_one_after_another(self):
        page = self._page()
        started = []

        def fake_install(archive):
            started.append(archive.name)
            # The first name prompt is cancelled: no install starts.
            page._install_active = archive.name != "one.zip"

        with patch.object(page, "_install_archive", side_effect=fake_install):
            page._install_dropped([Path("one.zip"), Path("two.zip"), Path("three.zip")])
            self.assertEqual(started, ["one.zip", "two.zip"])
            page._install_active = False
            page._install_next_dropped()
        self.assertEqual(started, ["one.zip", "two.zip", "three.zip"])


class RealDropEventTest(_Page):
    def test_a_file_dropped_on_the_list_starts_an_install(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QDragEnterEvent, QDropEvent

        page = self._page()
        archive = Path(self.tmp.name) / "New Mod.zip"
        archive.write_text("x", encoding="utf-8")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(archive))])
        viewport = page.tree
        enter = QDragEnterEvent(
            QPointF(5, 5).toPoint(), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        viewport.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(
            QPointF(5, 5), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        with patch.object(page, "_install_archive") as install:
            viewport.dropEvent(drop)
        install.assert_called_once_with(archive)


class UpdateFromArchiveTest(_Page):
    def test_update_reinstalls_in_place_with_its_own_message(self):
        page = self._page()
        with (
            patch(
                "commander_gui.ui.mod_manager_page.QFileDialog.getOpenFileName",
                return_value=("/downloads/Mine-v2.zip", ""),
            ),
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            patch.object(page, "_reinstall_from") as reinstall,
        ):
            page._update_mod_from_archive("Mine")
        archive, name, message = reinstall.call_args.args
        self.assertEqual((archive, name), (Path("/downloads/Mine-v2.zip"), "Mine"))
        self.assertIn("updated", message)

    def test_reinstall_keeps_the_old_folder_until_the_new_one_lands(self):
        import time

        page = self._page()
        mods = Path(self.tmp.name) / "mods"
        _write(mods, "Mine/gamedata/old.ltx")
        # The real _start_mod_install marks the install active as it starts.
        with patch.object(
            page,
            "_start_mod_install",
            side_effect=lambda *_a: setattr(page, "_install_active", True),
        ) as start:
            page._reinstall_from(Path("/x/Mine.zip"), "Mine", "done")
            # Moving the existing folder aside now runs on a
            # BackgroundTask (real QThread) instead of blocking the
            # caller.
            app = QApplication.instance()
            deadline = time.monotonic() + 5
            while not start.called and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            for _ in range(20):
                app.processEvents()
        start.assert_called_once()
        destination, backup = page._reinstall_backup
        self.assertFalse(destination.exists())
        self.assertTrue((backup / "gamedata" / "old.ltx").is_file())
        # The install failed: _finish_install() puts the old files back.
        page._finish_install()
        deadline = time.monotonic() + 5
        while not (mods / "Mine" / "gamedata" / "old.ltx").exists() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            app.processEvents()
        self.assertTrue((mods / "Mine" / "gamedata" / "old.ltx").is_file())
        self.assertIsNone(page._reinstall_done_message)


class DragReorderWarningTest(_Page):
    def _drop(self, page, answer):
        with (
            patch.object(QMessageBox, "question", return_value=answer) as question,
            patch.object(page, "_write_lines", return_value=False) as write,
        ):
            page._on_tree_drop("A", "B", "Cat", True)
            for _ in range(5):
                QApplication.instance().processEvents()
        return question, write

    def test_first_drag_asks_and_cancel_moves_nothing(self):
        page = self._page()
        page._lines = ["+A", "+B", "-Cat_separator"]
        question, write = self._drop(page, QMessageBox.StandardButton.No)
        question.assert_called_once()
        write.assert_not_called()
        question, write = self._drop(page, QMessageBox.StandardButton.Yes)
        question.assert_called_once()
        write.assert_called_once()
        # Asked once per session, like Move Up/Down.
        question, write = self._drop(page, QMessageBox.StandardButton.Yes)
        question.assert_not_called()
        write.assert_called_once()


class ModNicknameTest(_Page):
    def _page_with_mod(self):
        page = self._page()
        mods = Path(self.tmp.name) / "mods"
        _write(mods, "Real Name/gamedata/a.ltx")
        page._lines = ["+Real Name", "+Other"]
        page._populate_tree()
        return page, mods

    def _rename(self, page, nickname):
        with (
            patch.object(page, "_selected_mod_names", return_value=["Real Name"]),
            patch(
                "commander_gui.ui.mod_manager_page.QInputDialog.getText",
                return_value=(nickname, True),
            ),
        ):
            page._rename_selected_mod()

    def _texts(self, page):
        header = page.tree.topLevelItem(0)
        return {
            _mod_name(header.child(i)): header.child(i).text(0)
            for i in range(header.childCount())
        }

    def test_nickname_changes_only_what_commander_shows(self):
        from commander_gui import gui_settings

        page, mods = self._page_with_mod()
        self._rename(page, "Nice Name")
        self.assertEqual(self._texts(page)["Real Name"], "Nice Name")
        # Nothing Mod Organizer reads is touched.
        self.assertEqual(page._lines, ["+Real Name", "+Other"])
        self.assertTrue((mods / "Real Name" / "gamedata" / "a.ltx").is_file())
        self.assertEqual(page._selected_mod_names(), ["Real Name"])
        # It survives a reload: stored per GAMMA folder.
        stored = gui_settings.load_gui_settings()["mod_nicknames"]
        self.assertEqual(stored[str(Path(self.tmp.name))], {"Real Name": "Nice Name"})

    def test_search_finds_both_names(self):
        page, _mods = self._page_with_mod()
        self._rename(page, "Nice Name")
        header = page.tree.topLevelItem(0)
        for needle in ("nice", "real"):
            page.search.setText(needle)
            visible = [_mod_name(header.child(i)) for i in range(header.childCount()) if not header.child(i).isHidden()]
            self.assertEqual(visible, ["Real Name"])

    def test_empty_or_real_name_resets_the_nickname(self):
        page, _mods = self._page_with_mod()
        self._rename(page, "Nice Name")
        self._rename(page, "")
        self.assertEqual(self._texts(page)["Real Name"], "Real Name")
        self.assertEqual(page._nicknames(), {})


class UpdateNotificationTest(unittest.TestCase):
    """Once per version, and never when turned off."""

    def _window(self):
        from commander_gui.ui.main_window import MainWindow

        return MainWindow.__new__(MainWindow)

    def test_gamma_update_is_notified_once_per_version(self):
        from commander_gui import gui_settings

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.main_window.notify_desktop") as notify,
        ):
            window = self._window()
            status = SimpleNamespace(update_available=True, latest="930")
            window._on_scheduled_update_checked(status)
            window._on_scheduled_update_checked(status)
            self.assertEqual(notify.call_count, 1)
            window._on_scheduled_update_checked(SimpleNamespace(update_available=True, latest="931"))
            self.assertEqual(notify.call_count, 2)
            gui_settings.save_gui_settings(update_notifications=False)
            window._on_scheduled_update_checked(SimpleNamespace(update_available=True, latest="932"))
            self.assertEqual(notify.call_count, 2)

    def test_commander_update_is_notified_once_per_tag(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}),
            patch("commander_gui.ui.main_window.notify_desktop") as notify,
        ):
            window = self._window()
            window._notify_commander_update("v1.3.1")
            window._notify_commander_update("v1.3.1")
            window._notify_commander_update("v1.3.2")
        self.assertEqual(notify.call_count, 2)

    def test_the_periodic_check_rechecks_commander_after_the_interval(self):
        import time

        window = self._window()
        window.install_busy = False
        window._commander_checked_at = time.monotonic()
        with (
            patch.object(type(window), "_maybe_check_for_updates_in_background") as gamma,
            patch.object(type(window), "_check_commander_update_status") as commander,
        ):
            window._periodic_update_check()
            gamma.assert_called_once()
            commander.assert_not_called()
            window._commander_checked_at -= window._COMMANDER_CHECK_INTERVAL_S
            window._periodic_update_check()
            commander.assert_called_once()


if __name__ == "__main__":
    unittest.main()
