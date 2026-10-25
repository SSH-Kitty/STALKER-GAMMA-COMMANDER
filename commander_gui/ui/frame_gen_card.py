"""The Frame generation card: lsfg-vk on or off and its settings, kept as
variables in the launch options text. On the Play page, and in the graphics
overlay, where COMMANDER passes the text back and forth."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import gui_settings, launch_options
from .common import exe_pids, info_label, make_card, open_url, section_label, tr


class FrameGenCard(QWidget):
    """get_text and set_text read and write the launch options text."""

    dll_changed = Signal()

    def __init__(self, get_text: Callable[[], str], set_text: Callable[[str], None]) -> None:
        super().__init__()
        self._get_text = get_text
        self._set_text = set_text
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build())

    #: Each settings row: (row label, variable, ((button text, value), ...)).
    #: A None value leaves the variable out, so lsfg-vk uses its default.
    _ROWS = (
        ("Multiplier", launch_options.FG_MULTIPLIER, (("2x", "2"), ("3x", "3"), ("4x", "4"), ("6x", "6"), ("8x", "8"))),
        ("Detail", launch_options.FG_FLOW_SCALE, (("100%", None), ("75%", "0.75"), ("50%", "0.5"), ("25%", "0.25"))),
        ("Mode", launch_options.FG_PERFORMANCE, (("Quality", None), ("Performance", "1"))),
        ("Latency", launch_options.FG_PRESENT, (("Normal", None), ("Low", "mailbox"))),
    )

    def _button(self, text: str, key: str | None, value) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("segment")
        button.setCheckable(True)
        button.setFixedHeight(34)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False: self._set(key, value))
        return button

    def _build(self) -> QFrame:
        """lsfg-vk frame generation: on/off and its settings, all kept as
        variables in the launch options text. Locked, with what's missing,
        until there's a Lossless Scaling DLL lsfg-vk can run."""
        card, layout = make_card()
        layout.setSpacing(12)
        # (row's buttons with their values, variable or None for on/off)
        self._rows: list[tuple[list[tuple[QPushButton, object]], str | None]] = []

        header = QHBoxLayout()
        header.addWidget(section_label(tr("Frame generation"), level=2))
        header.addStretch(1)
        toggle = []
        for text, value in ((tr("On"), True), (tr("Off"), False)):
            button = self._button(text, None, value)
            button.setMinimumWidth(72)
            header.addWidget(button)
            toggle.append((button, value))
        self._rows.append((toggle, None))
        layout.addLayout(header)
        layout.addWidget(info_label(tr("Lossless Scaling Frame Generation for smoother gameplay. Plays best above 40 FPS but adds a little input lag.")))
        self._missing = info_label("")
        self._missing.setObjectName("warn")
        self._missing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._missing)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        # Row label, by variable: the Latency row hides on lsfg-vk 2.
        self._row_labels: dict[str, QWidget] = {}
        for row, (label, key, choices) in enumerate(self._ROWS):
            self._row_labels[key] = info_label(tr(label), wrap=False)
            grid.addWidget(self._row_labels[key], row, 0)
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            row_buttons = []
            for text, value in choices:
                button = self._button(tr(text), key, value)
                buttons.addWidget(button, 1)
                row_buttons.append((button, value))
            grid.addLayout(buttons, row, 1)
            self._rows.append((row_buttons, key))
        # Which lsfg-vk runs goes with the DLL: Lossless.dll is 1.0's,
        # lsfg-vk.dll 2.0's. With both side by side, it's a click.
        self._engine_label = info_label("lsfg-vk", wrap=False)
        grid.addWidget(self._engine_label, len(self._ROWS), 0)
        engine = QHBoxLayout()
        engine.setSpacing(8)
        self._engines: list[tuple[QPushButton, str]] = []
        for text, name in (("1.0 · Lossless.dll", "Lossless.dll"), ("2.0 · lsfg-vk.dll", "lsfg-vk.dll")):
            button = QPushButton(text)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setFixedHeight(34)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, name=name: self._use_dll(name))
            engine.addWidget(button, 1)
            self._engines.append((button, name))
        grid.addLayout(engine, len(self._ROWS), 1)
        layout.addLayout(grid)
        self._engine_hint = info_label(tr("Lossless.dll comes with Lossless Scaling's usual version and runs on lsfg-vk 1.0, built into COMMANDER: everything changes in a running game. lsfg-vk.dll comes with its lsfg-vk beta (in Steam: Properties > Betas) and runs on lsfg-vk 2.0, installed on your system: on and off apply from the next launch."))
        self._engine_hint.setObjectName("dim")
        layout.addWidget(self._engine_hint)
        layout.addStretch(1)
        # Confirms what the running game uses.
        self._running = info_label("")
        self._running.setObjectName("accent")
        layout.addWidget(self._running)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._dll_label = info_label("", wrap=False)
        self._dll_label.setObjectName("dim")
        footer.addWidget(self._dll_label, 1)
        self._get_lossless_button = QPushButton(tr("Get Lossless Scaling"))
        self._get_lossless_button.setObjectName("secondary")
        self._get_lossless_button.setProperty("compact", True)
        self._get_lossless_button.clicked.connect(
            lambda: open_url("https://store.steampowered.com/app/993090/Lossless_Scaling/")
        )
        footer.addWidget(self._get_lossless_button)
        self._choose_dll_button = QPushButton(tr("Choose DLL"))
        self._choose_dll_button.setObjectName("secondary")
        self._choose_dll_button.setProperty("compact", True)
        self._choose_dll_button.setToolTip(tr("Pick Lossless Scaling's Lossless.dll or lsfg-vk.dll yourself."))
        self._choose_dll_button.clicked.connect(self._choose_dll)
        footer.addWidget(self._choose_dll_button)
        layout.addLayout(footer)
        return card

    def _choose_dll(self) -> None:
        dll = launch_options.lossless_dll()
        start = str(dll.parent if dll else Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Select Lossless Scaling's DLL"), start, "DLL (*.dll)"
        )
        if path:
            gui_settings.save_gui_settings(lossless_dll=path)
            self.sync()
            self.dll_changed.emit()

    def _use_dll(self, name: str) -> None:
        """The other DLL in the same folder: the other lsfg-vk."""
        other = _other_dll(launch_options.lossless_dll(), name)
        if other is not None:
            gui_settings.save_gui_settings(lossless_dll=str(other))
        self.sync()
        self.dll_changed.emit()

    def _set(self, key: str | None, value) -> None:
        text = self._get_text()
        if key is None:
            text = launch_options.frame_gen(text, value)
        else:
            text = launch_options.set_var(text, key, value)
        self._set_text(text)
        # Clicking the picked button again unticks it and leaves the text
        # as it was, so no textChanged: tick it back here.
        self.sync()

    def sync(self) -> bool:
        """Show the card's state from the options text, and lock it with what
        to install until lsfg-vk and Lossless Scaling are both there. True
        when they are."""
        text = self._get_text()
        on = launch_options.is_on(text, launch_options.FRAME_GEN)
        steps = []
        dll = launch_options.lossless_dll()
        version = launch_options.frame_gen_version(dll) if dll else None
        if dll is None:
            steps.append(tr("Frame generation needs a file from Lossless Scaling (paid, on Steam). Set its Properties > Compatibility to a Proton version and install it: COMMANDER finds the file there. Have the file already? Use Choose DLL."))
        elif version is None and dll.name.lower() == "lsfg-vk.dll" and "2" not in launch_options.lsfg_versions():
            steps.append(tr("lsfg-vk.dll runs on lsfg-vk 2.0. Install it on your system (on Arch and CachyOS: sudo pacman -S lsfg-vk, or from lsfg-vk.dev), or pick Lossless.dll."))
        elif version is None:
            steps.append(tr("That file isn't one lsfg-vk can use. Pick Lossless.dll or lsfg-vk.dll from Lossless Scaling."))
        if dll is None:
            self._dll_label.setText(tr("No DLL found"))
        elif version:
            self._dll_label.setText(tr("{name} on lsfg-vk {version}.0", name=launch_options.dll_label(dll), version=version))
        else:
            self._dll_label.setText(launch_options.dll_label(dll))
        self._dll_label.setToolTip(str(dll or ""))
        lines = steps if len(steps) == 1 else [f"{n}. {step}" for n, step in enumerate(steps, 1)]
        # Changes reach a running game only when lsfg-vk was loaded at its
        # start; say so rather than let the buttons seem to do nothing.
        loaded = game_has_frame_gen()
        if not steps and loaded is False:
            lines = [tr("Frame generation wasn't on when the game started, so changes apply from the next launch. In co-op, let COMMANDER restart Steam when it asks.")]
        elif not steps and loaded and version == "2" and not on:
            lines = [tr("lsfg-vk 2.0 can't switch off in a running game: it goes off from the next launch.")]
        elif not steps and on and version == "2" and launch_options.is_on(text, "ENABLE_VKBASALT=1"):
            lines = [tr("vkBasalt stays off while frame generation runs on lsfg-vk 2.0: together they show a black screen. On 1.0 they work together.")]
        self._running.setText(tr("Loaded in the running game, with {dll}.", dll=launch_options.dll_label(dll)) if loaded and dll else "")
        self._running.setVisible(bool(loaded and dll))
        self._missing.setText("\n".join(lines))
        self._missing.setVisible(bool(lines))
        self._get_lossless_button.setVisible(dll is None)
        # lsfg-vk 2 has no present mode setting of COMMANDER's.
        for widget in (self._row_labels[launch_options.FG_PRESENT],
                       *(button for button, _value in self._rows[-1][0])):
            widget.setVisible(version != "2")
        # Both versions, with the one for the other DLL there to pick when
        # it sits next to this one; else how to get it.
        for widget in (self._engine_label, self._engine_hint, *(button for button, _name in self._engines)):
            widget.setVisible(dll is not None)
        for button, name in self._engines:
            current = dll is not None and dll.name.lower() == name.lower()
            button.setChecked(current)
            available = current or _other_dll(dll, name) is not None
            button.setEnabled(available)
            if available:
                tip = ""
            elif name == "Lossless.dll":
                tip = tr("Needs Lossless.dll from Lossless Scaling's usual version, in the same folder. Or use Choose DLL.")
            elif "2" not in launch_options.lsfg_versions():
                tip = tr("Needs lsfg-vk 2.0 installed on your system (on Arch and CachyOS: sudo pacman -S lsfg-vk, or from lsfg-vk.dev).")
            else:
                tip = tr("Needs lsfg-vk.dll from Lossless Scaling's lsfg-vk beta (in Steam: Properties > Betas), in the same folder. Or use Choose DLL.")
            button.setToolTip(tip)
        for buttons, key in self._rows:
            current = on if key is None else launch_options.var(text, key)
            if key == launch_options.FG_MULTIPLIER:
                current = current or "2"
            for button, value in buttons:
                button.setChecked(value == current)
                # Left on (typed in, or set before an uninstall): Off stays
                # clickable so it can still be switched off.
                button.setEnabled(on and not steps if key else not steps or (on and value is False))
        return not steps


def game_has_frame_gen() -> bool | None:
    """Whether the running game has lsfg-vk loaded (its library mapped in):
    None when no game runs."""
    pids = exe_pids("AnomalyDX")
    if not pids:
        return None
    for pid in pids:
        try:
            maps = Path(f"/proc/{pid}/maps").read_bytes()
        except OSError:
            continue
        if b"liblsfg-vk" in maps:
            return True
    return False


def _other_dll(dll: Path | None, name: str) -> Path | None:
    """name (Lossless.dll or lsfg-vk.dll) next to dll, when its lsfg-vk can
    run it."""
    if dll is None:
        return None
    other = dll.parent / name
    wanted = "1" if name == "Lossless.dll" else "2"
    return other if other.is_file() and launch_options.frame_gen_version(other) == wanted else None

