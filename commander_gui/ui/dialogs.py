"""COMMANDER's pop-ups: QMessageBox's API, drawn in the app's own style.

``MessageBox`` is a drop-in for ``QMessageBox``: modules import it under
that name, so the static ``question()``/``warning()``/... calls and the
``box = QMessageBox(...); box.addButton(...); box.exec()`` pattern both
keep working, and tests keep patching ``<module>.QMessageBox``.

Instead of Qt's native box (desktop title bar, icon, Yes/No), ``exec()``
shows a card: the title as a heading, the text, then optional bullet
points and a warning note, with buttons that say what they do.

Text layout: paragraphs are separated by a blank line, and a paragraph
whose lines all start with "• " becomes a bullet list.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr

_SB = QMessageBox.StandardButton
_ROLE = QMessageBox.ButtonRole
#: Roles that go on the right, in the accent colour.
_ACCEPT_ROLES = (_ROLE.AcceptRole, _ROLE.YesRole, _ROLE.ApplyRole, _ROLE.ActionRole)
#: Roles that close the box without acting (Esc and the corner ×).
_REJECT_ROLES = (_ROLE.RejectRole, _ROLE.NoRole)


def _standard_labels() -> dict:
    """Plain labels for Qt's standard buttons, in the app's language (Qt's
    own are English-only here and carry "&" shortcuts)."""
    return {
        _SB.Ok: tr("OK"),
        _SB.Yes: tr("Yes"),
        _SB.No: tr("No"),
        _SB.Cancel: tr("Cancel"),
        _SB.Close: tr("Close"),
        _SB.Save: tr("Save"),
        _SB.Discard: tr("Discard"),
        _SB.Retry: tr("Retry"),
        _SB.Ignore: tr("Ignore"),
        _SB.Abort: tr("Stop"),
        _SB.Open: tr("Open"),
        _SB.Apply: tr("Apply"),
    }


def _label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(True)
    # Errors often hold a path or message worth copying.
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _bullets(points) -> QGridLayout:
    # The bullet in its own column keeps a wrapped line indented.
    grid = QGridLayout()
    grid.setContentsMargins(4, 0, 0, 0)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(6)
    grid.setColumnStretch(1, 1)
    for row, point in enumerate(points):
        dot = QLabel("•")
        dot.setObjectName("accent")
        grid.addWidget(dot, row, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(_label(point, "info"), row, 1)
    return grid


def _add_text(layout: QVBoxLayout, text: str, name: str = "info") -> None:
    for paragraph in text.strip().split("\n\n"):
        lines = [line.strip() for line in paragraph.strip().splitlines() if line.strip()]
        if lines and all(line.startswith(("• ", "- ")) for line in lines):
            layout.addLayout(_bullets([line[2:] for line in lines]))
        elif lines:
            layout.addWidget(_label("\n".join(lines), name))


def _frameless(dialog: QDialog, parent: QWidget | None, on_close) -> bool:
    """Match the main window: with COMMANDER's own title bar on, drop the
    desktop's frame, drag from anywhere empty, close from the corner."""
    # QWidget.window() called directly: pages shadow it with a ``window``
    # attribute holding the MainWindow.
    if parent is None or not getattr(QWidget.window(parent), "custom_title_bar", False):
        return False
    from .title_bar import (
        BUTTON_HEIGHT,
        BUTTON_WIDTH,
        WindowButton,
        WindowDragFilter,
        pin_top_right,
    )

    dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    dialog.custom_title_bar = True
    dialog.toggle_maximized = lambda: None
    dialog.installEventFilter(WindowDragFilter(dialog))
    close = WindowButton("close")
    close.setObjectName("winClose")
    close.setToolTip(tr("Close"))
    close.setFixedSize(BUTTON_WIDTH, BUTTON_HEIGHT)
    close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    close.clicked.connect(on_close)
    pin_top_right(close, dialog)
    return True


def _shell(parent: QWidget | None, title: str) -> tuple[QDialog, QVBoxLayout]:
    """The empty card every pop-up is built in, with its heading."""
    dialog = QDialog(parent)
    dialog.setObjectName("messageDialog")
    dialog.setWindowTitle(title)
    # A fixed width lets every wrapped label work out its real height,
    # so the box is exactly as tall as its text.
    dialog.setFixedWidth(560)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 20, 24, 20)
    layout.setSpacing(10)
    if title:
        layout.addWidget(_label(title, "section1"))
    return dialog, layout


def _fit(layout: QVBoxLayout) -> None:
    """Make the box exactly as tall as its contents at its fixed width."""
    dialog = layout.parentWidget()
    layout.invalidate()
    dialog.setFixedHeight(layout.heightForWidth(dialog.width()))


class MessageBox(QMessageBox):
    """``QMessageBox`` with COMMANDER's look. Extra over Qt's API:
    ``points`` (bullet list), ``info`` (dim small print), ``note``
    (warning-coloured line) and
    ``labels`` (``{StandardButton: text}``, e.g. ``{Yes: tr("Delete")}``)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.points: tuple[str, ...] = ()
        self.note = ""
        self._clicked = None

    def clickedButton(self):
        return self._clicked

    def set_labels(self, labels: dict) -> None:
        """Rename standard buttons, e.g. ``{Yes: tr("Delete")}``."""
        for standard, label in labels.items():
            button = self.button(standard)
            if button is not None:
                button.setText(label)
                button.setProperty("customLabel", True)

    # ------------------------------------------------------------- showing
    def exec(self) -> int:
        buttons = self.buttons()
        if not buttons:
            buttons = [self.addButton(_SB.Ok)]
        labels = _standard_labels()
        for button in buttons:
            standard = self.standardButton(button)
            # A label given through ``labels`` wins over the stock one.
            if standard in labels and not button.property("customLabel"):
                button.setText(labels[standard])
        escape = self.escapeButton() or next(
            (b for b in buttons if self.buttonRole(b) in _REJECT_ROLES), None
        ) or (buttons[0] if len(buttons) == 1 else None)
        default = self.defaultButton() or next(
            (b for b in buttons if self.buttonRole(b) in _ACCEPT_ROLES), buttons[-1]
        )

        dialog, layout = _shell(self.parentWidget(), self.windowTitle())
        _add_text(layout, self.text())
        if self.points:
            layout.addLayout(_bullets(self.points))
        if self.informativeText():
            _add_text(layout, self.informativeText(), "dim")
        if self.note:
            _add_text(layout, self.note, "warn")
        if self.detailedText():
            self._add_details(layout)
        check = None
        if self.checkBox() is not None:
            check = QCheckBox(self.checkBox().text())
            check.setChecked(self.checkBox().isChecked())
            layout.addWidget(check)
        layout.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        # Action first, Cancel / Close / Exit always last (right-most).
        exits = {tr("Cancel"), tr("Close"), tr("Quit")}
        ordered = sorted(buttons, key=lambda b: (self.standardButton(b) in (_SB.Cancel, _SB.Close) or b.text().replace("&", "") in exits, b is default, self.buttonRole(b) in _ACCEPT_ROLES or self.buttonRole(b) == _ROLE.DestructiveRole))
        for button in ordered:
            # Qt's own "Show Details..." toggle: _add_details() has ours.
            if button.isHidden() or button.text().replace("&", "").startswith(("Show Details", "Hide Details")):
                continue
            ours = QPushButton(button.text().replace("&&", "\0").replace("&", "").replace("\0", "&"))
            ours.setMinimumSize(110, 34)
            ours.setToolTip(button.toolTip())
            ours.setEnabled(button.isEnabled())
            if self.buttonRole(button) == _ROLE.DestructiveRole or button.objectName() == "danger":
                ours.setObjectName("danger")
            elif button is default:
                ours.setObjectName("primary")
            if button is default:
                ours.setDefault(True)
            ours.clicked.connect(lambda _c=False, b=button: self._pick(dialog, b))
            row.addWidget(ours)
        layout.addLayout(row)
        _frameless(dialog, self.parentWidget(), dialog.reject)
        _fit(layout)

        self._clicked = None
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._clicked = escape
        if check is not None:
            self.checkBox().setChecked(check.isChecked())
        if self._clicked is None:
            return _SB.NoButton
        # Callers may have connected to the button itself.
        self._clicked.clicked.emit()
        standard = self.standardButton(self._clicked)
        if standard != _SB.NoButton:
            return standard
        return buttons.index(self._clicked)

    def _pick(self, dialog: QDialog, button) -> None:
        self._clicked = button
        dialog.accept()

    def _add_details(self, layout: QVBoxLayout) -> None:
        toggle = QPushButton(tr("Show details"))
        toggle.setCheckable(True)
        details = QPlainTextEdit(self.detailedText())
        details.setObjectName("mono")
        details.setReadOnly(True)
        details.setMinimumHeight(140)
        details.hide()
        toggle.toggled.connect(details.setVisible)
        toggle.toggled.connect(lambda _on: _fit(layout))
        toggle.toggled.connect(lambda on: toggle.setText(tr("Hide details") if on else tr("Show details")))
        row = QHBoxLayout()
        row.addWidget(toggle)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(details)

    # ------------------------------------------------------- static helpers
    @classmethod
    def _ask(cls, icon, parent, title, text, buttons, default, points=(), note="", info="", labels=None, details=""):
        box = cls(icon, title, text, buttons, parent)
        if default != _SB.NoButton:
            box.setDefaultButton(default)
        box.set_labels(labels or {})
        box.points = tuple(points)
        box.note = note
        if info:
            box.setInformativeText(info)
        if details:
            box.setDetailedText(details)
        box.exec()
        clicked = box.clickedButton()
        return box.standardButton(clicked) if clicked is not None else _SB.NoButton

    @classmethod
    def question(cls, parent, title, text, buttons=_SB.Yes | _SB.No, defaultButton=_SB.NoButton, **extra):
        return cls._ask(QMessageBox.Icon.Question, parent, title, text, buttons, defaultButton, **extra)

    @classmethod
    def information(cls, parent, title, text, buttons=_SB.Ok, defaultButton=_SB.NoButton, **extra):
        return cls._ask(QMessageBox.Icon.Information, parent, title, text, buttons, defaultButton, **extra)

    @classmethod
    def warning(cls, parent, title, text, buttons=_SB.Ok, defaultButton=_SB.NoButton, **extra):
        return cls._ask(QMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton, **extra)

    @classmethod
    def critical(cls, parent, title, text, buttons=_SB.Ok, defaultButton=_SB.NoButton, **extra):
        return cls._ask(QMessageBox.Icon.Critical, parent, title, text, buttons, defaultButton, **extra)


class InputDialog:
    """``QInputDialog.getText()``/``getItem()`` with the pop-up look.

    Same call signatures and return values, plus ``confirm`` (the OK
    button's label). Modules import it as ``QInputDialog``.
    """

    @staticmethod
    def _ask(parent, title: str, label: str, field: QWidget, confirm: str) -> bool:
        dialog, layout = _shell(parent, title)
        if label:
            layout.addWidget(_label(label, "info"))
        field.setMinimumHeight(34)
        layout.addWidget(field)
        add_buttons(dialog, layout, confirm or tr("OK"))
        _frameless(dialog, parent, dialog.reject)
        _fit(layout)
        field.setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def getText(parent, title, label, echo=QLineEdit.EchoMode.Normal, text="", *, confirm=""):
        edit = QLineEdit(text)
        edit.setEchoMode(echo)
        edit.selectAll()
        accepted = InputDialog._ask(parent, title, label, edit, confirm)
        return edit.text(), accepted

    @staticmethod
    def getItem(parent, title, label, items, current=0, editable=True, *, confirm=""):
        combo = QComboBox()
        combo.addItems(list(items))
        combo.setCurrentIndex(current)
        combo.setEditable(editable)
        accepted = InputDialog._ask(parent, title, label, combo, confirm)
        return combo.currentText(), accepted


def style_window(dialog: QDialog, heading: str = "", *, resizable: bool = True) -> None:
    """Give a full window (Backups, Manage GE-Proton, ...) the pop-up look:
    card background, roomier margins, an optional big heading, and with
    COMMANDER's own title bar on, its frame: drag, corner ×, edge resize.

    Call once the window's layout is built.
    """
    dialog.setObjectName("popupWindow")
    layout = dialog.layout()
    if layout is not None:
        layout.setContentsMargins(24, 20, 24, 20)
        if heading:
            layout.insertWidget(0, _label(heading, "section1"))
    if _frameless(dialog, dialog.parentWidget(), dialog.reject) and resizable:
        from .title_bar import EdgeResizeFilter, attach_resize_filter

        dialog._resize_filter = EdgeResizeFilter(dialog)
        attach_resize_filter(dialog, dialog._resize_filter, True)


class ProgressDialog(QDialog):
    """A themed progress window: heading, status line, bar and Cancel.

    Covers the part of ``QProgressDialog`` COMMANDER uses: ``setValue()``,
    ``setLabelText()`` and the ``canceled`` signal.
    """

    canceled = Signal()

    def __init__(self, title: str, text: str, parent: QWidget | None = None, *, log: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("popupWindow")
        self.setWindowTitle(title)
        self.setFixedWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(_label(title, "section1"))
        self._text = _label(text, "info")
        layout.addWidget(self._text)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)
        self._log = None
        row = QHBoxLayout()
        if log:
            self._log = QPlainTextEdit()
            self._log.setReadOnly(True)
            self._log.setMaximumBlockCount(2000)
            self._log.setFixedHeight(180)
            self._log.hide()
            layout.addWidget(self._log)
            self._toggle = QPushButton(tr("Show details"))
            self._toggle.setMinimumSize(110, 34)
            self._toggle.clicked.connect(self._toggle_log)
            row.addWidget(self._toggle)
        layout.addSpacing(6)
        row.addStretch(1)
        self._cancel = QPushButton(tr("Cancel"))
        self._cancel.setMinimumSize(110, 34)
        self._cancel.clicked.connect(self._on_cancel)
        row.addWidget(self._cancel)
        layout.addLayout(row)
        _frameless(self, parent, self._on_cancel)
        _fit(layout)

    def setValue(self, value: int) -> None:
        self._bar.setValue(int(value))

    def appendLine(self, text: str) -> None:
        if self._log is not None:
            self._log.appendPlainText(text)

    def _toggle_log(self) -> None:
        show = not self._log.isVisible()
        self._log.setVisible(show)
        self._toggle.setText(tr("Hide details") if show else tr("Show details"))
        self.setMinimumHeight(0)
        _fit(self.layout())

    def setLabelText(self, text: str) -> None:
        if text == self._text.text():
            return
        self._text.setText(text)
        # A second line (the download size) would be clipped at the old height.
        _fit(self.layout())

    def _on_cancel(self) -> None:
        if not self._cancel.isEnabled():
            return
        self._cancel.setEnabled(False)
        self._text.setText(tr("Stopping..."))
        self.canceled.emit()

    def resume(self) -> None:
        """Cancel was declined: put the button back."""
        self._cancel.setEnabled(True)

    def reject(self) -> None:
        # Esc cancels like the button; the caller closes the window once
        # the work has actually stopped.
        self._on_cancel()


class UpdateDialog(QDialog):
    """Asks before a COMMANDER self-update: version jump, patch notes and
    where the new AppImage goes. The caller loads the notes in the
    background and passes them to ``set_notes()``; ``folder`` is the
    chosen save folder once accepted."""

    def __init__(
        self, current: str, tag: str, channel: str, folder: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("popupWindow")
        self.setWindowTitle(tr("Update COMMANDER"))
        self.setFixedWidth(600)
        self.folder = folder
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        layout.addWidget(_label(tr("Update COMMANDER"), "section1"))
        versions = QHBoxLayout()
        jump = _label(f"v{current.lstrip('v')}  \u2192  {tag}", "updateVersions")
        versions.addWidget(jump, 1)
        versions.addWidget(_label(tr("Unstable") if channel == "unstable" else tr("Stable"), "info"))
        layout.addLayout(versions)
        layout.addWidget(_label(tr("What's new"), "section3"))
        self.notes = QTextBrowser()
        self.notes.setObjectName("welcomeNotes")
        from .common import open_web_links  # common imports this module

        open_web_links(self.notes)
        self.notes.setFixedHeight(260)
        self.notes.setPlainText(tr("Loading patch notes..."))
        layout.addWidget(self.notes)
        save_row = QHBoxLayout()
        self._folder_label = _label("", "info")
        self._show_folder()
        save_row.addWidget(self._folder_label, 1)
        change = QPushButton(tr("Change..."))
        change.setMinimumSize(110, 34)
        change.clicked.connect(self._change_folder)
        save_row.addWidget(change)
        layout.addLayout(save_row)
        layout.addWidget(_label(tr("COMMANDER restarts when the update is installed."), "info"))
        add_buttons(self, layout, tr("Update now"), cancel=tr("Later")).setFocus()
        _frameless(self, parent, self.reject)

    def set_notes(self, result: object, releases_url: str) -> None:
        """``fetch_latest_release_notes()``'s result, or None on failure."""
        if isinstance(result, tuple) and len(result) == 2 and result[1]:
            # GitHub's feed carries the body already rendered to HTML;
            # QTextBrowser fetches nothing remote and runs no script.
            self.notes.setHtml(str(result[1]))
        else:
            self.notes.setPlainText(
                tr("Couldn't load the patch notes. They are on the Releases page:")
                + "\n"
                + releases_url
            )

    def _show_folder(self) -> None:
        home = str(Path.home())
        shown = "~" + self.folder[len(home):] if self.folder.startswith(home) else self.folder
        self._folder_label.setText(tr("Save to: {folder}", folder=shown))

    def _change_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("Choose where to save the new COMMANDER"), self.folder
        )
        if folder:
            self.folder = folder
            self._show_folder()


def add_close_button(dialog: QDialog, layout) -> None:
    """A right-aligned Close button, for windows that only show things."""
    row = QHBoxLayout()
    row.addStretch(1)
    close = QPushButton(tr("Close"))
    close.setMinimumSize(110, 34)
    close.clicked.connect(dialog.reject)
    row.addWidget(close)
    layout.addLayout(row)


def add_buttons(dialog: QDialog, layout, confirm: str, cancel: str = "") -> QPushButton:
    """The main action plus Cancel (right-most), right-aligned like every pop-up.
    Returns the action button."""
    layout.addSpacing(6)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addStretch(1)
    ok = QPushButton(confirm)
    ok.setObjectName("primary")
    ok.setMinimumSize(110, 34)
    ok.setDefault(True)
    ok.clicked.connect(dialog.accept)
    row.addWidget(ok)
    cancel_button = QPushButton(cancel or tr("Cancel"))
    cancel_button.setMinimumSize(110, 34)
    cancel_button.clicked.connect(dialog.reject)
    row.addWidget(cancel_button)
    layout.addLayout(row)
    return ok
