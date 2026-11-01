"""COMMANDER's right-click menus (commander_gui/ui/context_menu.py)."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QSpinBox,
    QTreeWidget,
)

from commander_gui.ui import context_menu
from commander_gui.ui.context_menu import build_text_menu, polish_menu, show_text_menu

APP = QApplication.instance() or QApplication([])


def texts(menu):
    return [action.text() for action in menu.actions()]


class ContextMenuTests(unittest.TestCase):
    def right_click(self, widget):
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(2, 2), QPoint(2, 2))
        with patch.object(QMenu, "popup") as popup:
            shown = show_text_menu(widget, event)
        return shown, popup

    def test_text_box_menu_is_translated_without_qt_marks(self):
        edit = QLineEdit("hello")
        edit.selectAll()
        with patch.object(context_menu, "tr", side_effect=lambda text: f"[{text}]"):
            menu = build_text_menu(edit, QPoint())
        labels = [text.partition("\t")[0] for text in texts(menu) if text]
        self.assertEqual(labels, ["[Undo]", "[Redo]", "[Cut]", "[Copy]", "[Paste]", "[Delete]", "[Select all]"])
        self.assertFalse(any("&" in text for text in texts(menu)))
        # Qt's shortcut hints stay; every item has an icon.
        self.assertIn("\t", texts(menu)[menu.actions().index(next(a for a in menu.actions() if a.objectName() == "edit-copy"))])
        self.assertTrue(all(not a.icon().isNull() for a in menu.actions() if not a.isSeparator()))

    def test_unknown_qt_items_and_stray_separators_go(self):
        edit = QLineEdit()
        menu = QMenu()
        menu.addSeparator()
        menu.addAction("Insert Unicode control character")
        menu.addSeparator()
        copy = menu.addAction("&Copy\tCtrl+C")
        copy.setObjectName("edit-copy")
        menu.addSeparator()
        with patch.object(QLineEdit, "createStandardContextMenu", return_value=menu):
            built = build_text_menu(edit, QPoint())
        self.assertEqual(texts(built), ["Copy\tCtrl+C"])

    def test_plain_text_box_takes_the_click_on_its_viewport(self):
        box = QPlainTextEdit("abc")
        shown, popup = self.right_click(box.viewport())
        self.assertTrue(shown)
        popup.assert_called_once()

    def test_selectable_label_copies_and_selects(self):
        label = QLabel("<b>Path:</b> /home/x")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        menu = build_text_menu(label, QPoint())
        copy, select = (a for a in menu.actions() if not a.isSeparator())
        self.assertFalse(copy.isEnabled())
        select.trigger()
        self.assertEqual(label.selectedText(), "Path: /home/x")
        menu = build_text_menu(label, QPoint())
        next(a for a in menu.actions() if a.objectName() == "edit-copy").trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), "Path: /home/x")

    def test_left_alone(self):
        plain = QLabel("not selectable")
        custom = QLineEdit()
        custom.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        spin = QSpinBox()
        for widget in (plain, custom, spin.findChild(QLineEdit), QTreeWidget()):
            self.assertFalse(self.right_click(widget)[0], type(widget).__name__)

    def test_every_menu_gets_a_see_through_window(self):
        menu = QMenu()
        polish_menu(menu)
        self.assertTrue(menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))


if __name__ == "__main__":
    unittest.main()
