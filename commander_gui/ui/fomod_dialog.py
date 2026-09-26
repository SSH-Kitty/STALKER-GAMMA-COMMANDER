"""The desktop FOMOD installer: one step at a time, like Mod Organizer 2's.

Left, the current step's option groups (radio buttons where the group takes
one, check boxes where it takes several); right, the preview image and
description of the option under the mouse or keyboard focus. Back / Next
walk the steps the choices so far leave visible, and the last one installs.
The rules - defaults, Required / NotUsable options, condition flags - are
:class:`commander_gui.fomod.FomodWizard`'s, shared with Deck Mode.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..fomod import (
    GROUP_HINTS,
    NOT_USABLE,
    RECOMMENDED,
    REQUIRED,
    FomodConfig,
    FomodOption,
    FomodWizard,
    resolve_image,
)
from ..i18n import tr
from .common import clear_layout, info_label, make_card, section_label

_IMAGE_MAX_H = 300

#: Small label beside an option whose type is worth knowing up front.
_TYPE_TAGS = {
    REQUIRED: "Required",
    RECOMMENDED: "Recommended",
    NOT_USABLE: "Not available",
}


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class FomodWizardDialog(QDialog):
    def __init__(self, config: FomodConfig, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.wizard = FomodWizard(config)
        self._config = config
        self._root = root
        self._pixmaps: dict[str, QPixmap | None] = {}
        self._controls: dict[tuple[int, int], list[QAbstractButton]] = {}
        self._hover_sources: dict[QObject, FomodOption] = {}
        self._position = 0
        self.setWindowTitle(tr("Install {name}", name=config.name))
        self.resize(1080, 720)
        self.setMinimumSize(820, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(section_label(config.name))
        if config.author:
            author = QLabel(tr("by {author}", author=config.author))
            author.setObjectName("dim")
            titles.addWidget(author)
        header.addLayout(titles, 1)
        self.step_label = QLabel("")
        self.step_label.setObjectName("dim")
        header.addWidget(self.step_label, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.options_scroll = QScrollArea()
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.options_host = QWidget()
        self.options_host.setObjectName("pageContent")
        self.options_layout = QVBoxLayout(self.options_host)
        self.options_layout.setContentsMargins(0, 0, 8, 0)
        self.options_layout.setSpacing(12)
        self.options_scroll.setWidget(self.options_host)
        body.addWidget(self.options_scroll, 3)

        detail, detail_layout = make_card()
        detail.setMinimumWidth(340)
        self.detail_image = QLabel()
        self.detail_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_image.setMinimumHeight(120)
        self.detail_image.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        detail_layout.addWidget(self.detail_image)
        self.detail_name = section_label("", level=2)
        self.detail_name.setWordWrap(True)
        detail_layout.addWidget(self.detail_name)
        description_scroll = QScrollArea()
        description_scroll.setWidgetResizable(True)
        description_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.detail_text = info_label("")
        self.detail_text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        description_scroll.setWidget(self.detail_text)
        detail_layout.addWidget(description_scroll, 1)
        body.addWidget(detail, 2)
        layout.addLayout(body, 1)

        self.error_label = info_label("")
        self.error_label.setObjectName("warn")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        footer = QHBoxLayout()
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        footer.addStretch(1)
        self.back_button = QPushButton(tr("Back"))
        self.back_button.clicked.connect(self._back)
        footer.addWidget(self.back_button)
        self.next_button = QPushButton(tr("Next"))
        self.next_button.setObjectName("primary")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self._next)
        footer.addWidget(self.next_button)
        layout.addLayout(footer)

        self._show_step()

    # -- navigation -----------------------------------------------------------
    def _steps(self) -> list[int]:
        return self.wizard.visible_steps()

    def _current(self) -> int | None:
        steps = self._steps()
        if not steps:
            return None
        self._position = max(0, min(self._position, len(steps) - 1))
        return steps[self._position]

    def _next(self) -> None:
        step = self._current()
        if step is not None:
            problems = self.wizard.errors(step)
            if problems:
                self.error_label.setText(
                    tr("Make a choice in: {groups}", groups=", ".join(problems))
                )
                self.error_label.show()
                return
        if step is None or self._position >= len(self._steps()) - 1:
            self.accept()
            return
        self._position += 1
        self._show_step()

    def _back(self) -> None:
        if self._position > 0:
            self._position -= 1
            self._show_step()

    def selections(self) -> dict[tuple[int, int], list[int]]:
        return self.wizard.selections()

    # -- rendering ------------------------------------------------------------
    def _show_step(self) -> None:
        self.error_label.hide()
        clear_layout(self.options_layout)
        self._controls.clear()
        self._hover_sources.clear()
        steps = self._steps()
        step_index = self._current()
        last = step_index is None or self._position >= len(steps) - 1
        self.back_button.setEnabled(self._position > 0)
        self.next_button.setText(tr("Install") if last else tr("Next"))
        if step_index is None:
            self.step_label.setText("")
            self.options_layout.addWidget(
                info_label(tr("Nothing to choose - press Install to continue."))
            )
            self.options_layout.addStretch(1)
            self._show_detail(None)
            return
        self.wizard.enter(step_index)
        step = self._config.steps[step_index]
        self.step_label.setText(
            tr(
                "Step {number} of {total}  ·  {name}",
                number=self._position + 1,
                total=len(steps),
                name=step.name,
            )
        )
        first_selected: FomodOption | None = None
        for group_index, group in enumerate(step.groups):
            card, card_layout = make_card()
            card_layout.setSpacing(6)
            title_row = QHBoxLayout()
            title_row.addWidget(section_label(group.name, level=2))
            title_row.addStretch(1)
            hint = QLabel(tr(GROUP_HINTS.get(group.group_type, "Choose any")))
            hint.setObjectName("dim")
            title_row.addWidget(hint)
            card_layout.addLayout(title_row)
            single = group.group_type in ("SelectExactlyOne", "SelectAtMostOne")
            controls: list[QAbstractButton] = []
            for option_index, option in enumerate(group.options):
                row = QHBoxLayout()
                control: QAbstractButton = (
                    QRadioButton(option.name) if single else QCheckBox(option.name)
                )
                # Exclusivity is FomodWizard's job - it also has to allow
                # unticking the one choice of a "one, or none" group.
                control.setAutoExclusive(False)
                control.clicked.connect(
                    lambda _checked=False, g=group_index, o=option_index: self._toggle(g, o)
                )
                control.installEventFilter(self)
                self._hover_sources[control] = option
                row.addWidget(control, 1)
                tag = QLabel("")
                tag.setObjectName("dim")
                row.addWidget(tag)
                control.setProperty("fomodTag", tag)
                card_layout.addLayout(row)
                controls.append(control)
                if first_selected is None and self.wizard.is_selected(
                    step_index, group_index, option_index
                ):
                    first_selected = option
            self._controls[(step_index, group_index)] = controls
            self.options_layout.addWidget(card)
        self.options_layout.addStretch(1)
        self._sync_controls()
        self.options_scroll.verticalScrollBar().setValue(0)
        if first_selected is None and step.groups and step.groups[0].options:
            first_selected = step.groups[0].options[0]
        self._show_detail(first_selected)

    def _toggle(self, group_index: int, option_index: int) -> None:
        step_index = self._current()
        if step_index is None:
            return
        self.wizard.toggle(step_index, group_index, option_index)
        self.error_label.hide()
        self._sync_controls()
        option = self._config.steps[step_index].groups[group_index].options[option_index]
        self._show_detail(option)
        # A choice here can show or hide later steps.
        last = self._position >= len(self._steps()) - 1
        self.next_button.setText(tr("Install") if last else tr("Next"))

    def _sync_controls(self) -> None:
        for (step_index, group_index), controls in self._controls.items():
            group = self._config.steps[step_index].groups[group_index]
            for option_index, control in enumerate(controls):
                kind = self.wizard.option_type(step_index, group_index, option_index)
                selected = self.wizard.is_selected(step_index, group_index, option_index)
                control.setChecked(selected)
                locked = group.group_type == "SelectAll" or (kind == REQUIRED and selected)
                control.setEnabled(not locked and not (kind == NOT_USABLE and not selected))
                tag = control.property("fomodTag")
                if isinstance(tag, QLabel):
                    tag.setText(tr(_TYPE_TAGS[kind]) if kind in _TYPE_TAGS else "")
                if kind == NOT_USABLE:
                    control.setToolTip(tr("Not available with your earlier choices."))
                else:
                    control.setToolTip("")

    # -- details --------------------------------------------------------------
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.FocusIn):
            option = self._hover_sources.get(watched)
            if option is not None:
                self._show_detail(option)
        return super().eventFilter(watched, event)

    def _pixmap(self, relative: str) -> QPixmap | None:
        if relative not in self._pixmaps:
            path = resolve_image(self._root, relative)
            pixmap = QPixmap(str(path)) if path is not None else None
            self._pixmaps[relative] = pixmap if pixmap is not None and not pixmap.isNull() else None
        return self._pixmaps[relative]

    def _show_detail(self, option: FomodOption | None) -> None:
        image = option.image if option is not None and option.image else self._config.image
        pixmap = self._pixmap(image) if image else None
        if pixmap is None:
            self.detail_image.clear()
            self.detail_image.hide()
        else:
            width = max(self.detail_image.width(), 300)
            self.detail_image.setPixmap(
                pixmap.scaled(
                    width,
                    _IMAGE_MAX_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.detail_image.show()
        if option is None:
            self.detail_name.setText(self._config.name)
            self.detail_text.setText("")
            return
        self.detail_name.setText(option.name)
        self.detail_text.setText(
            _clean(option.description) or tr("No description for this option.")
        )
