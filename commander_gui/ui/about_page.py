"""About: the COMMANDER documentation, plus version, update and project links.

A topic list with search on the left, the chosen topic on the right. Each
section folds open and shut, and "live" rows show the real paths and values
from this machine with Copy and Open buttons. The text itself is in
``about_docs.py`` and goes through ``tr()`` as the page is built.
"""

from __future__ import annotations

import html
import platform
import re
from pathlib import Path

import PySide6
from PySide6.QtCore import Qt, QTimer, qVersion
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version_label__
from ..config import (
    cli_binary_path,
    gui_settings_path,
    logs_dir,
    project_root,
    settings_dir,
    settings_path,
)
from ..themes import active_theme_tokens
from .about_docs import TOPICS
from .common import (
    info_label,
    make_card,
    open_in_file_manager,
    open_url,
    section_label,
    tr,
)
from .deck_switch import switch_mode

_GITHUB = "https://github.com/SSH-Kitty/STALKER-GAMMA-COMMANDER"
#: Permanent invite to the STALKER COMMANDER Discord server (lands on #welcome).
_DISCORD = "https://discord.gg/6A9psrtYhh"

#: (English) credit lines, shown on Deck Mode's and the Welcome screen;
#: translated with tr() where displayed.
CREDITS = (
    "SSH-Kitty — COMMANDER graphical user interface.",
    "Dnttnd — testing implementations, dev builds, bug reports, and helping polish the UI.",
    "FaithBeam — the STALKER-GAMMA CLI.",
    "Harbingerr — xrRazom Co-op, the multiplayer mod.",
    "Grokitach and the GAMMA team — the GAMMA modpack.",
    "The Anomaly Team — developers of the standalone S.T.A.L.K.E.R. Anomaly modification and engine base.",
    "GSC Game World — creators of the original S.T.A.L.K.E.R. games and universe.",
)

#: Live keys that are not paths, so get no Open button.
_NOT_PATHS = {"version", "packaging", "python", "qt", "profile", "runner", "mo2_profile"}


def live_values(window) -> dict[str, str]:
    """Every value a ``("live", ...)`` block can show, read fresh. "" = not set."""
    from .. import gui_settings
    from ..autostart import autostart_desktop_path
    from ..coop import state_dir
    from ..desktop_integration import menu_entry_path
    from ..game_backup import backups_root
    from ..log_dump import log_dumps_dir
    from ..modlist import modlist_path_for
    from ..self_update import commander_appimage_path

    settings = getattr(window, "settings", None)
    profile = getattr(settings, "active_profile", None)
    root = project_root()
    appimage = commander_appimage_path()
    if appimage is not None:
        packaging = f"AppImage ({appimage})"
    elif str(root).startswith(("/opt/", "/usr/")):
        packaging = "System package (AUR)"
    else:
        packaging = "Source checkout"
    state = gui_settings.load_gui_settings()
    values = {
        "version": __version_label__,
        "packaging": packaging,
        "project_root": str(root),
        "python": platform.python_version(),
        "qt": f"Qt {qVersion()}, PySide6 {PySide6.__version__}",
        "profile": profile.profile_name if profile else tr("No active profile"),
        "runner": state.get("runner") or "auto",
        "prefix": gui_settings.configured_wine_prefix(),
        "settings_dir": str(settings_dir()),
        "settings_json": str(settings_path()),
        "gui_settings": str(gui_settings_path()),
        "logs": str(logs_dir()),
        "launcher_log": str(logs_dir() / "launcher.log"),
        "dumps": str(log_dumps_dir()),
        "lock": str(settings_dir() / "commander.lock"),
        "cli": str(cli_binary_path()),
        "backups": str(backups_root()),
        "menu_entry": str(menu_entry_path()),
        "autostart": str(autostart_desktop_path()),
    }
    anomaly = getattr(profile, "anomaly", "") or ""
    gamma = getattr(profile, "gamma", "") or ""
    modlist = modlist_path_for(gamma, profile.mo2_profile) if gamma else None
    values.update(
        anomaly=anomaly,
        gamma=gamma,
        cache=getattr(profile, "cache", "") or "",
        mo2_profile=getattr(profile, "mo2_profile", "") or "",
        modlist=str(modlist) if modlist else "",
        md5_baseline=str(Path(gamma) / "gamma-md5.txt") if gamma else "",
        quarantine=str(Path(gamma) / "mods" / ".verify-quarantine") if gamma else "",
        coop_dir=str(state_dir(anomaly)) if anomaly else "",
    )
    return values


def open_page_button(window, page: str, style: str) -> QPushButton:
    """A button that opens nav page ``page`` (or Deck Mode for ``"deck"``)."""
    if page == "deck":
        button = QPushButton(tr("Switch to Steam Deck Mode"))
        button.clicked.connect(lambda: switch_mode(window, deck=True))
    else:
        # Imported here: main_window imports this module.
        from .main_window import NAV_ITEMS

        title = tr(dict(NAV_ITEMS).get(page, "Settings"))
        button = QPushButton(tr("Open {title}", title=title))
        button.clicked.connect(lambda _checked=False, key=page: window.set_page(key))
    button.setObjectName(style)
    return button


def _text(text: str) -> QLabel:
    label = info_label(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _mono(text: str) -> QLabel:
    """A selectable label for a path, command or other exact value."""
    label = QLabel(text)
    label.setObjectName("mono")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _copy_button(get_text) -> QPushButton:
    button = QPushButton(tr("Copy"))
    button.setObjectName("copyCommand")

    def copy() -> None:
        QGuiApplication.clipboard().setText(get_text())
        button.setText(tr("Copied!"))
        QTimer.singleShot(1500, button, lambda: button.setText(tr("Copy")))

    button.clicked.connect(copy)
    return button


def _translated(topic: dict) -> dict:
    """A topic with every visible string run through ``tr()``. Code blocks
    stay as written (their Copy button copies them), as do live keys."""

    def block(item):
        if isinstance(item, str):
            return tr(item)
        kind, data = item
        if kind in ("list", "steps"):
            return kind, [tr(line) for line in data]
        if kind == "table":
            return kind, [(tr(term), tr(text)) for term, text in data]
        if kind == "live":
            return kind, [(tr(caption), key) for caption, key in data]
        if kind in ("note", "warn"):
            return kind, tr(data)
        return item

    return {
        **topic,
        "title": tr(topic["title"]),
        "summary": tr(topic["summary"]),
        "sections": [(tr(heading), [block(item) for item in blocks]) for heading, blocks in topic["sections"]],
    }


def _strings(topic: dict) -> list[str]:
    """Every searchable string in a topic."""
    found = [topic["title"], topic["summary"]]
    for heading, blocks in topic["sections"]:
        found.append(heading)
        for block in blocks:
            if isinstance(block, str):
                found.append(block)
                continue
            kind, data = block
            if kind in ("list", "steps"):
                found += data
            elif kind in ("table", "live"):
                found += [part for row in data for part in row]
            elif kind != "widget":
                found.append(data)
    return found


def _highlight(label: QLabel, words: list[str]) -> None:
    """Mark every searched word in a label; with no words, show it plain again."""
    plain = label.property("plainText")
    if plain is None:
        plain = label.text()
        label.setProperty("plainText", plain)
    pattern = "|".join(re.escape(word) for word in words)
    if not pattern or not re.search(pattern, plain, re.IGNORECASE):
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(plain)
        return
    tokens = active_theme_tokens()
    mark = f'<span style="background-color: {tokens["selection"]}; color: {tokens["selection_text"]};">'
    parts = re.split(f"({pattern})", plain, flags=re.IGNORECASE)
    # re.split with one group alternates: text, match, text, match, ...
    body = "".join(
        f"{mark}{html.escape(part)}</span>" if odd else html.escape(part)
        for odd, part in ((i % 2, part) for i, part in enumerate(parts))
    )
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(f'<span style="white-space: pre-wrap;">{body}</span>')


class _Section(QWidget):
    """One documentation section: a heading that folds its body open and shut."""

    def __init__(self, heading: str, body: QWidget, blob: str) -> None:
        super().__init__()
        self.heading = heading
        self.blob = blob
        card, layout = make_card()
        self.header = QPushButton()
        self.header.setObjectName("docSection")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.clicked.connect(lambda: self.set_open(self.body.isHidden()))
        layout.addWidget(self.header)
        self.body = body
        layout.addWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        self.set_open(True)

    def set_open(self, opened: bool) -> None:
        self.body.setVisible(opened)
        self.header.setText(f"{'▾' if opened else '▸'}  {self.heading}")


class AboutPage(QWidget):
    """The COMMANDER documentation: topic list on the left, the topic on the right."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._topics = [_translated(topic) for topic in TOPICS]
        self._blobs = [" ".join(_strings(topic)).casefold() for topic in self._topics]
        #: Built on first visit; index -> (widget, sections, expand button).
        self._built: dict[int, tuple[QWidget, list[_Section], QPushButton]] = {}
        #: index -> the topic's text labels that search highlights.
        self._labels: dict[int, list[QLabel]] = {}
        #: (key, value label, open button) for every live row built so far.
        self._live: list[tuple[str, QLabel, QPushButton | None]] = []
        self._values: dict[str, str] = {}

        self.update_button = QPushButton("")
        self.update_button.setObjectName("primary")
        self.update_button.hide()
        self.update_button.clicked.connect(lambda: open_url(f"{_GITHUB}/releases"))

        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 20)
        outer.setSpacing(16)

        side = QVBoxLayout()
        side.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search the documentation"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        side.addWidget(self.search)
        self.topics = QListWidget()
        self.topics.setObjectName("helpTopics")
        self.topics.setFixedWidth(250)
        self.topics.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for index, topic in enumerate(self._topics):
            item = QListWidgetItem(topic["title"])
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.topics.addItem(item)
        self.topics.currentItemChanged.connect(self._show)
        side.addWidget(self.topics, 1)
        outer.addLayout(side)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Scoped: a bare rule would cascade to every button inside.
        self.scroll.viewport().setObjectName("scrollViewport")
        self.scroll.viewport().setStyleSheet("#scrollViewport { background: transparent; }")
        outer.addWidget(self.scroll, 1)

        self.empty = QWidget()
        empty_layout = QVBoxLayout(self.empty)
        self.empty_label = info_label("")
        self.empty_label.setObjectName("dim")
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch(1)

        self.topics.setCurrentRow(0)
        # The status bar's check is the one check: reuse its result.
        found = getattr(window, "commander_update_found", None)
        if found is not None:
            found.connect(self._on_commander_update_checked)
        self._on_commander_update_checked(getattr(window, "_commander_update_tag", None))

    # -- update button and links ------------------------------------------

    def _on_commander_update_checked(self, tag: object) -> None:
        if not tag or not isinstance(tag, str):
            return
        self.update_button.setText(tr("Update available: {tag} — open Releases", tag=tag))
        self.update_button.show()

    def _link_buttons(self) -> list[QPushButton]:
        links = (
            (tr("Project on GitHub"), _GITHUB, "primary"),
            (tr("Join the Discord"), _DISCORD, "secondary"),
            (tr("Releases"), f"{_GITHUB}/releases", "secondary"),
            (tr("stalker-gamma-cli (FaithBeam)"), "https://github.com/FaithBeam/stalker-gamma-cli", "secondary"),
            (tr("GAMMA Modpack (Grokitach)"), "https://github.com/Grokitach/Stalker_GAMMA", "secondary"),
        )
        buttons = []
        for text, url, style in links:
            button = QPushButton(text)
            button.setObjectName(style)
            button.clicked.connect(lambda _checked=False, target=url: open_url(target))
            buttons.append(button)
        return buttons

    # -- topics -----------------------------------------------------------

    def show_topic(self, title: str) -> None:
        """Open the topic whose English title is ``title``."""
        self.search.clear()
        index = next(i for i, topic in enumerate(TOPICS) if topic["title"] == title)
        self.topics.setCurrentRow(index)

    def _filter(self, text: str) -> None:
        words = text.casefold().split()
        first_visible = None
        for row in range(self.topics.count()):
            item = self.topics.item(row)
            hidden = not all(word in self._blobs[row] for word in words)
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        current = self.topics.currentItem()
        if first_visible is None:
            self.topics.setCurrentItem(None)
            self._show(None)
        elif current is None or current.isHidden():
            self.topics.setCurrentItem(first_visible)
        else:
            self._apply_search(current.data(Qt.ItemDataRole.UserRole))

    def _show(self, item, _previous=None) -> None:
        self.scroll.takeWidget()
        if item is None:
            self.empty_label.setText(
                tr("Nothing in the documentation matches \"{query}\".", query=self.search.text().strip())
            )
            self.scroll.setWidget(self.empty)
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index not in self._built:
            self._built[index] = self._build_topic(self._topics[index])
            live = {label for _key, label, _opener in self._live}
            # Live values stay plain: their Copy button copies label.text().
            self._labels[index] = [
                label for label in self._built[index][0].findChildren(QLabel) if label not in live
            ]
            self._update_live()
        self._apply_search(index)
        self.scroll.setWidget(self._built[index][0])
        self.scroll.verticalScrollBar().setValue(0)

    def _apply_search(self, index: int) -> None:
        """With a search, open only the sections that match it and mark the words."""
        if index not in self._built:
            return
        words = self.search.text().casefold().split()
        for label in self._labels[index]:
            _highlight(label, words)
        _widget, sections, expand = self._built[index]
        for section in sections:
            section.set_open(all(word in section.blob for word in words))
        self._sync_expand(expand, sections)

    def _sync_expand(self, button: QPushButton, sections: list[_Section]) -> None:
        all_open = not any(section.body.isHidden() for section in sections)
        button.setText(tr("Collapse all") if all_open else tr("Expand all"))

    def _build_topic(self, topic: dict) -> tuple[QWidget, list[_Section], QPushButton]:
        content = QWidget()
        content.setObjectName("pageContent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)

        hero, layout = make_card()
        layout.addWidget(section_label(topic["title"], level=1))
        layout.addWidget(_text(topic["summary"]))
        buttons = QHBoxLayout()
        if topic["page"] is not None:
            buttons.addWidget(open_page_button(self.window, topic["page"], "primary"))
        expand = QPushButton()
        expand.setObjectName("pillButton")
        expand.setFixedHeight(34)
        expand.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(expand)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        body.addWidget(hero)

        sections = []
        for heading, blocks in topic["sections"]:
            section_body = QWidget()
            section_body.setObjectName("panelTransparent")
            section_layout = QVBoxLayout(section_body)
            section_layout.setContentsMargins(0, 4, 0, 0)
            section_layout.setSpacing(10)
            for block in blocks:
                self._add_block(section_layout, block)
            blob = " ".join(_strings({"title": heading, "summary": "", "sections": [("", blocks)]}))
            section = _Section(heading, section_body, blob.casefold())
            section.header.clicked.connect(lambda: self._sync_expand(expand, sections))
            sections.append(section)
            body.addWidget(section)
        body.addStretch(1)

        def toggle_all() -> None:
            opened = any(section.body.isHidden() for section in sections)
            for section in sections:
                section.set_open(opened)
            self._sync_expand(expand, sections)

        expand.clicked.connect(toggle_all)
        return content, sections, expand

    def _add_block(self, layout: QVBoxLayout, block) -> None:
        if isinstance(block, str):
            layout.addWidget(_text(block))
            return
        kind, data = block
        if kind == "list":
            for line in data:
                layout.addWidget(_text(f"•  {line}"))
        elif kind == "steps":
            for number, line in enumerate(data, 1):
                layout.addWidget(_text(f"{number}.  {line}"))
        elif kind == "table":
            for term, text in data:
                rows = QVBoxLayout()
                rows.setSpacing(2)
                label = QLabel(term)
                label.setObjectName("helpTerm")
                label.setWordWrap(True)
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                rows.addWidget(label)
                rows.addWidget(_text(text))
                layout.addLayout(rows)
        elif kind == "code":
            row = QHBoxLayout()
            row.addWidget(_mono(data), 1)
            row.addWidget(_copy_button(lambda value=data: value), 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(row)
        elif kind in ("note", "warn"):
            label = _text(data)
            label.setObjectName("accent" if kind == "note" else "warn")
            layout.addWidget(label)
        elif kind == "live":
            for caption, key in data:
                self._add_live_row(layout, caption, key)
        elif kind == "widget" and data == "update":
            layout.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignLeft)
        elif kind == "widget" and data == "links":
            for button in self._link_buttons():
                layout.addWidget(button)

    def _add_live_row(self, layout: QVBoxLayout, caption: str, key: str) -> None:
        name = QLabel(caption)
        name.setObjectName("dim")
        layout.addWidget(name)
        row = QHBoxLayout()
        value = _mono("")
        row.addWidget(value, 1)
        row.addWidget(_copy_button(value.text), 0, Qt.AlignmentFlag.AlignTop)
        opener = None
        if key not in _NOT_PATHS:
            opener = QPushButton(tr("Open"))
            opener.setObjectName("copyCommand")
            opener.clicked.connect(lambda _checked=False, label=value: self._open_path(label.text()))
            row.addWidget(opener, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        self._live.append((key, value, opener))

    @staticmethod
    def _open_path(text: str) -> None:
        path = Path(text).expanduser()
        open_in_file_manager(path if path.is_dir() else path.parent)

    def _update_live(self) -> None:
        if not self._live:
            return
        if not self._values:
            self._values = live_values(self.window)
        for key, label, opener in self._live:
            text = self._values.get(key, "")
            label.setText(text or tr("Not set"))
            if opener is not None:
                path = Path(text).expanduser() if text else None
                opener.setEnabled(bool(path) and (path.exists() or path.parent.is_dir()))

    def refresh(self) -> None:
        self._values = {}
        self._update_live()
