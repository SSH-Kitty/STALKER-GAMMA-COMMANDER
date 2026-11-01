"""COMMANDER's own file and folder picker (commander_gui/ui/file_dialog.py)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from commander_gui.ui import file_dialog
from commander_gui.ui.dialogs import MessageBox
from commander_gui.ui.file_dialog import FileDialog, _Picker, parse_filters

APP = QApplication.instance() or QApplication([])
ARCHIVES = "Mod archives (*.zip *.7z *.rar *.fomod);;All files (*)"


class FileDialogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "Games" / "Anomaly").mkdir(parents=True)
        (self.tmp / ".wine").mkdir()
        (self.tmp / "mod.ZIP").touch()
        (self.tmp / "notes.txt").touch()

    def picker(self, start, mode="dir", filters=""):
        picker = _Picker(None, "Pick", str(start), mode, filters)
        self.addCleanup(picker.deleteLater)
        picker.show()
        APP.processEvents()
        return picker

    @staticmethod
    def names(picker):
        return [picker.listing.item(i).text() for i in range(picker.listing.count())]

    def test_filters_parse_and_match_any_case(self):
        self.assertEqual(parse_filters("Zips (*.zip *.7z);;All (*)"), [("Zips (*.zip *.7z)", ["*.zip", "*.7z"]), ("All (*)", ["*"])])
        self.assertEqual(parse_filters(""), [])
        picker = self.picker(self.tmp, "open", ARCHIVES)
        self.assertEqual(self.names(picker), ["Games", "mod.ZIP"])
        picker.filter_combo.setCurrentIndex(1)
        self.assertEqual(self.names(picker), ["Games", "mod.ZIP", "notes.txt"])

    def test_folder_mode_lists_only_folders_and_uses_the_open_one(self):
        picker = self.picker(self.tmp)
        self.assertEqual(self.names(picker), ["Games"])
        # Nothing preselected: the button means the folder you're in.
        self.assertEqual(picker._chosen(), str(self.tmp))
        picker.listing.setCurrentRow(0)
        self.assertEqual(picker._chosen(), str(self.tmp / "Games"))

    def test_hidden_folders_toggle_with_h_and_ctrl_h(self):
        picker = self.picker(self.tmp)
        picker.listing.setFocus()
        QTest.keyClick(picker.listing, Qt.Key.Key_H)
        self.assertIn(".wine", self.names(picker))
        QTest.keyClick(picker.listing, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
        self.assertNotIn(".wine", self.names(picker))
        # Typed into the filter box, H is just a letter.
        picker.filter_edit.setFocus()
        QTest.keyClick(picker.filter_edit, Qt.Key.Key_H)
        self.assertEqual(picker.filter_edit.text(), "h")
        self.assertFalse(picker.show_hidden)

    def test_backspace_goes_up_but_still_edits_the_filter(self):
        picker = self.picker(self.tmp / "Games" / "Anomaly")
        picker.filter_edit.setFocus()
        QTest.keyClicks(picker.filter_edit, "ab")
        QTest.keyClick(picker.filter_edit, Qt.Key.Key_Backspace)
        self.assertEqual(picker.filter_edit.text(), "a")
        self.assertEqual(picker.current, self.tmp / "Games" / "Anomaly")
        picker.listing.setFocus()
        QTest.keyClick(picker.listing, Qt.Key.Key_Backspace)
        self.assertEqual(picker.current, self.tmp / "Games")
        # Lands on the folder it came from.
        self.assertEqual(picker.listing.currentItem().text(), "Anomaly")

    def test_double_click_opens_a_folder_once(self):
        picker = self.picker(self.tmp)
        rect = picker.listing.visualItemRect(picker.listing.item(0))
        QTest.mouseClick(picker.listing.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QTest.mouseDClick(picker.listing.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        APP.processEvents()
        self.assertEqual(picker.current, self.tmp / "Games")

    def test_sidebar_headings_and_rows_never_raise(self):
        picker = self.picker(self.tmp)
        for row in range(picker.sidebar.count()):
            picker.sidebar.itemClicked.emit(picker.sidebar.item(row))

    def test_start_folder_falls_back_sensibly(self):
        # Gone folder (an unplugged drive): its nearest parent left.
        self.assertEqual(self.picker(self.tmp / "gone" / "deeper").current, self.tmp)
        self.assertEqual(self.picker("").current, Path.home())
        # A bare save name (Export Diagnostics) starts in Home with the name filled in.
        save = self.picker("commander-diagnostics.txt", "save")
        self.assertEqual((save.current, save.name_edit.text()), (Path.home(), "commander-diagnostics.txt"))
        # Picking a file again: the old one starts selected.
        again = self.picker(self.tmp / "notes.txt", "open", "Text (*.txt)")
        self.assertEqual(again._chosen(), str(self.tmp / "notes.txt"))

    def test_pasted_path_goes_there(self):
        picker = self.picker(self.tmp)
        picker.filter_edit.setText(str(self.tmp / "Games"))
        picker.filter_edit.returnPressed.emit()
        self.assertEqual(picker.current, self.tmp / "Games")
        opener = self.picker(self.tmp, "open")
        opener.filter_edit.setText(str(self.tmp / "notes.txt"))
        opener.filter_edit.returnPressed.emit()
        self.assertEqual(opener.result_path, str(self.tmp / "notes.txt"))

    def test_save_asks_before_replacing_and_refuses_slashes(self):
        picker = self.picker(self.tmp / "notes.txt", "save")
        with patch.object(MessageBox, "question", return_value=MessageBox.StandardButton.No) as ask:
            picker._confirm()
        ask.assert_called_once()
        self.assertEqual(picker.result_path, "")
        with patch.object(MessageBox, "question", return_value=MessageBox.StandardButton.Yes):
            picker._confirm()
        self.assertEqual(picker.result_path, str(self.tmp / "notes.txt"))
        picker.name_edit.setText("a/b.txt")
        self.assertFalse(picker.ok.isEnabled())

    def test_unreadable_folder_says_so(self):
        with patch.object(file_dialog.os, "scandir", side_effect=PermissionError):
            picker = self.picker(self.tmp)
        self.assertEqual(self.names(picker), ["This folder can't be opened"])

    def test_static_helpers_match_qfiledialog(self):
        def accept(picker):
            picker._finish(str(self.tmp / "mod.ZIP"))
            return QDialog.DialogCode.Accepted

        with patch.object(_Picker, "exec", accept):
            self.assertEqual(FileDialog.getOpenFileName(None, "t", str(self.tmp), ARCHIVES), (str(self.tmp / "mod.ZIP"), "Mod archives (*.zip *.7z *.rar *.fomod)"))
            self.assertEqual(FileDialog.getExistingDirectory(None, "t", str(self.tmp)), str(self.tmp / "mod.ZIP"))
            self.assertEqual(FileDialog.getOpenFileName(None, "t", "")[1], "")
        with patch.object(_Picker, "exec", return_value=QDialog.DialogCode.Rejected):
            self.assertEqual(FileDialog.getExistingDirectory(None, "t", ""), "")
            self.assertEqual(FileDialog.getOpenFileName(None, "t", "", ARCHIVES), ("", ""))
            self.assertEqual(FileDialog.getSaveFileName(None, "t", "x.zip", "Zip (*.zip)"), ("", ""))

    def test_system_picker_hands_back_its_choice(self):
        picker = self.picker(self.tmp, "open", ARCHIVES)
        with patch.object(file_dialog.QFileDialog, "getOpenFileName", return_value=("/x/y.zip", "")) as native:
            picker._system_picker()
        native.assert_called_once_with(picker, "Pick", str(self.tmp), ARCHIVES)
        self.assertEqual(picker.result_path, "/x/y.zip")

    def test_framed_popup_waits_for_show_before_making_its_window(self):
        # winId() before show turned the page behind into native windows on
        # Wayland, which broke every right-click menu on that page.
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from commander_gui.ui.dialogs import style_window

        main = QWidget()
        main.custom_title_bar = True
        self.addCleanup(main.deleteLater)
        page = QWidget(main)
        dialog = QDialog(page)
        QVBoxLayout(dialog)
        with patch("commander_gui.ui.title_bar.attach_resize_filter") as attach:
            style_window(dialog, "Heading")
            self.assertEqual(dialog.internalWinId(), 0)
            attach.assert_not_called()
            dialog.show()
            APP.processEvents()
            attach.assert_called_once_with(dialog, dialog._resize_filter, True)
        dialog.close()

    def test_every_page_uses_the_themed_picker(self):
        root = Path(file_dialog.__file__).parent
        for source in root.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            if source.name == "file_dialog.py" or "QFileDialog." not in text:
                continue
            self.assertIn("from .file_dialog import FileDialog as QFileDialog", text, source.name)


if __name__ == "__main__":
    unittest.main()
