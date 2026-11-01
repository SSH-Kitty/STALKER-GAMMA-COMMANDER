"""Right-click menus in COMMANDER's style.

Two parts, both driven from the app-wide filter in :mod:`pointer_cursor`:

* ``polish_menu``: every ``QMenu`` gets a see-through window, so the
  theme's rounded card shows with real round corners.
* ``show_text_menu``: text boxes and selectable labels get COMMANDER's own
  Undo/Cut/Copy/Paste menu instead of Qt's: translated, with line icons,
  and without Qt's "&" shortcut marks or its Unicode control-character
  submenu.
"""

from __future__ import annotations

from PySide6 import QtGui
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QTextDocumentFragment
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from ..i18n import tr
from .brand_icons import line_icon


def _labels() -> dict[str, tuple[str, str]]:
    """Qt's standard action name -> (our label, icon)."""
    return {
        "edit-undo": (tr("Undo"), "undo"),
        "edit-redo": (tr("Redo"), "redo"),
        "edit-cut": (tr("Cut"), "cut"),
        "edit-copy": (tr("Copy"), "copy"),
        "edit-paste": (tr("Paste"), "paste"),
        "edit-delete": (tr("Delete"), "delete"),
        "select-all": (tr("Select all"), "select"),
        "link-copy": (tr("Copy link"), "link"),
    }


def menu_icon(name: str) -> QIcon:
    """A line icon for a menu item, dimmed when the item is greyed out."""
    icon = QIcon(line_icon(name, "text"))
    icon.addPixmap(line_icon(name, "text_disabled").pixmap(36, 36), QIcon.Mode.Disabled)
    return icon


def polish_menu(menu: QMenu) -> None:
    """Round corners: the window must be see-through before it exists."""
    if not menu.internalWinId():
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def _editor(obj) -> QWidget | None:
    """The text box or label a right-click on ``obj`` belongs to, if it
    uses the default menu."""
    widget = obj
    # QPlainTextEdit/QTextEdit get the click on their viewport.
    if not isinstance(widget, (QLineEdit, QLabel)) and isinstance(obj.parent(), (QPlainTextEdit, QTextEdit)):
        widget = obj.parent()
    if not isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit, QLabel)):
        return None
    if widget.contextMenuPolicy() != Qt.ContextMenuPolicy.DefaultContextMenu:
        return None
    # A spin box adds Step up/down to its box's menu: left to Qt.
    if isinstance(widget, QLineEdit) and isinstance(widget.parent(), QAbstractSpinBox):
        return None
    if isinstance(widget, QLabel) and not (
        widget.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
    ):
        return None
    return widget


def _visible_length(label: QLabel) -> int:
    """Characters shown: a selection past the end selects nothing at all."""
    text = label.text()
    rich = label.textFormat() == Qt.TextFormat.RichText or (
        label.textFormat() == Qt.TextFormat.AutoText and QtGui.Qt.mightBeRichText(text)
    )
    return len(QTextDocumentFragment.fromHtml(text).toPlainText()) if rich else len(text)


def _label_menu(label: QLabel) -> QMenu:
    menu = QMenu(label)
    copy = menu.addAction(tr("Copy"))
    copy.setObjectName("edit-copy")
    copy.setEnabled(label.hasSelectedText())
    copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(label.selectedText()))
    menu.addSeparator()
    select = menu.addAction(tr("Select all"))
    select.setObjectName("select-all")
    select.triggered.connect(lambda: label.setSelection(0, _visible_length(label)))
    return menu


def build_text_menu(widget: QWidget, pos: QPoint) -> QMenu:
    """COMMANDER's menu for ``widget``, built from Qt's own so every
    action keeps working (and greyed out) exactly as Qt decides."""
    if isinstance(widget, QLabel):
        menu = _label_menu(widget)
    elif isinstance(widget, QLineEdit):
        menu = widget.createStandardContextMenu()
    else:
        menu = widget.createStandardContextMenu(pos)
    labels = _labels()
    for action in menu.actions():
        if action.isSeparator():
            continue
        name = action.objectName()
        if name not in labels:
            # Qt's "Insert Unicode control character" and the like.
            menu.removeAction(action)
            continue
        text, icon = labels[name]
        shortcut = action.text().partition("\t")[2]
        action.setText(f"{text}\t{shortcut}" if shortcut else text)
        action.setIcon(menu_icon(icon))
    # No separator left first, last or twice in a row after the removals.
    previous = None
    for action in menu.actions():
        if action.isSeparator() and (previous is None or previous.isSeparator()):
            menu.removeAction(action)
        else:
            previous = action
    actions = menu.actions()
    if actions and actions[-1].isSeparator():
        menu.removeAction(actions[-1])
    return menu


def show_text_menu(obj, event) -> bool:
    """Show our menu for a right-click on a text box; False if not one."""
    widget = _editor(obj)
    if widget is None:
        return False
    # Viewport coordinates for a text box, as createStandardContextMenu wants.
    menu = build_text_menu(widget, event.pos())
    menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    menu.popup(event.globalPos())
    return True

