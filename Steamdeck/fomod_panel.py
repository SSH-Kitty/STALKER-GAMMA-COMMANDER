"""A FOMOD installer for Deck Mode, one step per page.

The same rules as the desktop wizard - both drive
:class:`commander_gui.fomod.FomodWizard` - laid out for a thumbstick: each
option is a full-height row (A picks it), and the preview image and
description on the right follow the focused row, so reading an option costs
nothing more than moving onto it. Back / Next walk the steps the choices so
far leave visible; the last page's button installs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from commander_gui.fomod import (
    GROUP_HINTS,
    NOT_USABLE,
    RECOMMENDED,
    REQUIRED,
    FomodConfig,
    FomodOption,
    FomodWizard,
    resolve_image,
)
from commander_gui.i18n import tr

from . import gamepad as pad
from .scale import px
from .widgets import (
    DeckOverlay,
    DeckRow,
    DeckSmoothScrollArea,
    confirm_overlay,
    deck_label,
    repolish,
)

_IMAGE_H = 260

_TYPE_TAGS = {
    REQUIRED: "Required",
    RECOMMENDED: "Recommended",
    NOT_USABLE: "Not available",
}


class _OptionRow(DeckRow):
    """One option: its name, its type tag, and a radio / check mark."""

    def __init__(self, option: FomodOption, single: bool, on_focus: Callable[[], None]) -> None:
        super().__init__(option.name, "", chevron=False)
        self.option = option
        self._single = single
        self._on_focus = on_focus

    def render(self, selected: bool, kind: str, locked: bool) -> None:
        if self._single:
            mark = "◉" if selected else "○"
        else:
            mark = "☑" if selected else "☐"
        tag = tr(_TYPE_TAGS[kind]) if kind in _TYPE_TAGS else ""
        self.set_value(f"{tag}    {mark}" if tag else mark)
        usable = kind != NOT_USABLE or selected
        self.title_label.setObjectName("deckRowTitle" if usable else "deckRowTitleDim")
        self.value_label.setObjectName("deckRowValueOk" if selected else "deckRowValue")
        repolish(self.title_label)
        repolish(self.value_label)
        # Still a focus stop when locked, so its description can be read.
        self.setProperty("fomodLocked", locked)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._on_focus()


class DeckFomodPanel(QWidget):
    """The overlay body: options on the left, the focused one's details right."""

    def __init__(self, config: FomodConfig, root: Path) -> None:
        super().__init__()
        self.wizard = FomodWizard(config)
        self.config = config
        self.root = root
        self.position = 0
        self._pixmaps: dict[str, QPixmap | None] = {}
        self.rows: list[_OptionRow] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(20))

        left = QVBoxLayout()
        left.setSpacing(px(8))
        self.step_label = deck_label("", role="caption")
        left.addWidget(self.step_label)
        self.scroll = DeckSmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Not a focus stop itself: the rows are, and focusing one scrolls it in.
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setMinimumHeight(px(460))
        self.host = QWidget()
        self.options_layout = QVBoxLayout(self.host)
        self.options_layout.setContentsMargins(0, 0, px(8), 0)
        self.options_layout.setSpacing(px(8))
        self.scroll.setWidget(self.host)
        from .focus import enable_kinetic_scroll

        enable_kinetic_scroll(self.scroll)
        left.addWidget(self.scroll, 1)
        self.error_label = deck_label("", role="body", wrap=True)
        self.error_label.setObjectName("deckBodyWarn")
        self.error_label.hide()
        left.addWidget(self.error_label)
        layout.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(px(10))
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        right.addWidget(self.image)
        self.detail_name = deck_label("", role="section", wrap=True)
        right.addWidget(self.detail_name)
        self.detail_text = deck_label("", role="body", wrap=True)
        self.detail_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        detail_scroll = DeckSmoothScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_scroll.setWidget(self.detail_text)
        right.addWidget(detail_scroll, 1)
        # Not a focus stop (it would sit between the options and the
        # buttons); the right stick scrolls it instead.
        self.detail_scroll = detail_scroll
        #: Called after every toggle - a choice can add or remove later
        #: steps, which changes whether this one ends in Next or Install.
        self.on_changed: Callable[[], None] | None = None
        layout.addLayout(right, 2)

    # -- steps ----------------------------------------------------------------
    def steps(self) -> list[int]:
        return self.wizard.visible_steps()

    def current(self) -> int | None:
        steps = self.steps()
        if not steps:
            return None
        self.position = max(0, min(self.position, len(steps) - 1))
        return steps[self.position]

    def is_last(self) -> bool:
        return self.current() is None or self.position >= len(self.steps()) - 1

    def errors(self) -> list[str]:
        step = self.current()
        return [] if step is None else self.wizard.errors(step)

    def show_step(self) -> None:
        self.error_label.hide()
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = []
        step_index = self.current()
        steps = self.steps()
        if step_index is None:
            self.step_label.setText("")
            self.options_layout.addWidget(
                deck_label(tr("Nothing to choose - press Install to continue."), role="body", wrap=True)
            )
            self.options_layout.addStretch(1)
            self._show_detail(None)
            return
        self.wizard.enter(step_index)
        step = self.config.steps[step_index]
        self.step_label.setText(
            tr(
                "Step {number} of {total}  ·  {name}",
                number=self.position + 1,
                total=len(steps),
                name=step.name,
            )
        )
        for group_index, group in enumerate(step.groups):
            heading = QHBoxLayout()
            heading.addWidget(deck_label(group.name, role="section"), 1)
            heading.addWidget(
                deck_label(tr(GROUP_HINTS.get(group.group_type, "Choose any")), role="caption")
            )
            holder = QWidget()
            holder.setLayout(heading)
            self.options_layout.addWidget(holder)
            single = group.group_type in ("SelectExactlyOne", "SelectAtMostOne")
            for option_index, option in enumerate(group.options):
                row = _OptionRow(option, single, lambda o=option: self._show_detail(o))
                row.activated.connect(
                    lambda g=group_index, o=option_index: self._toggle(g, o)
                )
                row.setProperty("fomodKey", (step_index, group_index, option_index))
                self.options_layout.addWidget(row)
                self.rows.append(row)
        self.options_layout.addStretch(1)
        self._sync()
        self.scroll.verticalScrollBar().setValue(0)
        self._show_detail(self.rows[0].option if self.rows else None)

    def _toggle(self, group_index: int, option_index: int) -> None:
        step_index = self.current()
        if step_index is None:
            return
        self.wizard.toggle(step_index, group_index, option_index)
        self.error_label.hide()
        self._sync()
        if self.on_changed is not None:
            self.on_changed()

    def _sync(self) -> None:
        for row in self.rows:
            step_index, group_index, option_index = row.property("fomodKey")
            group = self.config.steps[step_index].groups[group_index]
            kind = self.wizard.option_type(step_index, group_index, option_index)
            selected = self.wizard.is_selected(step_index, group_index, option_index)
            locked = group.group_type == "SelectAll" or (kind == REQUIRED and selected)
            row.render(selected, kind, locked)

    def show_errors(self, problems: list[str]) -> None:
        self.error_label.setText(tr("Make a choice in: {groups}", groups=", ".join(problems)))
        self.error_label.show()

    # -- details --------------------------------------------------------------
    def _pixmap(self, relative: str) -> QPixmap | None:
        if relative not in self._pixmaps:
            path = resolve_image(self.root, relative)
            pixmap = QPixmap(str(path)) if path is not None else None
            self._pixmaps[relative] = pixmap if pixmap is not None and not pixmap.isNull() else None
        return self._pixmaps[relative]

    def _show_detail(self, option: FomodOption | None) -> None:
        image = option.image if option is not None and option.image else self.config.image
        pixmap = self._pixmap(image) if image else None
        if pixmap is None:
            self.image.clear()
            self.image.hide()
        else:
            self.image.setPixmap(
                pixmap.scaled(
                    max(self.image.width(), px(420)),
                    px(_IMAGE_H),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.image.show()
        if option is None:
            self.detail_name.setText(self.config.name)
            self.detail_text.setText("")
            return
        self.detail_name.setText(option.name)
        text = option.description.replace("\r\n", "\n").replace("\r", "\n").strip()
        self.detail_text.setText(text or tr("No description for this option."))


def show_fomod(
    window,
    config: FomodConfig,
    root: Path,
    *,
    on_install: Callable[[dict[tuple[int, int], list[int]]], None],
    on_cancel: Callable[[], None],
) -> None:
    """Run the installer in an overlay; ``on_install`` gets the selections."""
    panel = DeckFomodPanel(config, root)
    overlay: DeckOverlay | None = None
    #: Set once Install or Cancel has answered, so the overlay's closed
    #: signal (which B also sends) doesn't answer a second time.
    answered = False

    def _label_buttons() -> None:
        overlay.buttons[0].setText(tr("Install") if panel.is_last() else tr("Next"))
        overlay.buttons[1].setEnabled(panel.position > 0)
        overlay.hints = (
            ("A", "Select"),
            ("X", "Install" if panel.is_last() else "Next"),
            ("B", "Back" if panel.position > 0 else "Cancel"),
        )
        updater = getattr(window, "update_hints", None)
        if callable(updater) and window.current_overlay() is overlay:
            updater()

    def _refresh() -> None:
        panel.show_step()
        _label_buttons()
        # Next's click focused a row that show_step() just deleted; the new
        # rows are only shown on the next event-loop pass, and a hidden
        # widget can't take focus - so focus the first one then.
        QTimer.singleShot(0, _focus_first)

    def _focus_first() -> None:
        target = panel.rows[0] if panel.rows else overlay.buttons[0]
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _next() -> None:
        problems = panel.errors()
        if problems:
            panel.show_errors(problems)
            return
        if panel.is_last():
            nonlocal answered
            answered = True
            selections = panel.wizard.selections()
            window.dismiss_overlay()
            on_install(selections)
            return
        panel.position += 1
        _refresh()

    def _back() -> None:
        if panel.position > 0:
            panel.position -= 1
            _refresh()

    def _cancel() -> None:
        window.dismiss_overlay()

    def _on_back() -> bool:
        """B: a step back; on the first step, ask before throwing the
        choices away - it used to cancel the whole installer at once."""
        if panel.position > 0:
            _back()
            return True

        def _really_cancel() -> None:
            window.dismiss_overlay(refocus=False)  # the question
            _cancel()  # the installer

        window.show_overlay(
            confirm_overlay(
                tr("Cancel"),
                tr("Stop installing {name}?", name=config.name),
                on_confirm=_really_cancel,
                on_cancel=window.dismiss_overlay,
                confirm_text=tr("Stop"),
                confirm_role="danger",
            ),
            stacked=True,
        )
        return True

    def _on_action(action: str) -> bool:
        if action == pad.CONTEXT:
            _next()
            return True
        return False

    def _closed() -> None:
        nonlocal answered
        if not answered:
            answered = True
            on_cancel()

    overlay = DeckOverlay(
        config.name,
        panel,
        [
            (tr("Next"), _next, "primary"),
            (tr("Back"), _back, "normal"),
            (tr("Cancel"), _cancel, "normal"),
        ],
        panel_width=1180,
    )
    overlay.closed.connect(_closed)
    overlay.back_handler = _on_back
    overlay.action_handler = _on_action
    overlay.scroll_target = panel.detail_scroll
    panel.on_changed = _label_buttons
    window.show_overlay(overlay)
    _refresh()
