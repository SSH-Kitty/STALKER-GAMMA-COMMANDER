"""Help: the desktop About page's documentation, read on the Deck.

Renders ``commander_gui.ui.about_docs.TOPICS`` - the same data and the same
translations - so the Deck's help never falls behind the desktop's. A list
of topics, then one topic at a time in a text panel the D-pad scrolls
(DeckTextPanel). Copy buttons, file-manager buttons and the desktop's own
widgets are left out: there is nowhere on a Deck to paste to.

What only the desktop has (the Play page's toggles, its overlay key, window
rules for tiling desktops) is swapped for the Deck's way of doing it by
:func:`deck_topic`, keyed on the English text, before translation.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer

from commander_gui.i18n import tr
from commander_gui.ui.about_docs import TOPICS
from commander_gui.ui.about_page import _translated, live_values

from ..widgets import DeckRow, DeckTextPanel, deck_button, deck_label
from . import SCREENS
from .base import DeckScreen

#: Desktop nav keys a topic's "Open" button names -> the Deck screen for it.
_DECK_PAGES = {
    "install": "install",
    "systemcheck": "system",
    "play": "play",
    "update": "update",
    "modmanager": "mods",
    "multiplayer": "multiplayer",
    "utilities": "utilities",
    "settings": "settings",
}


#: Desktop-only sections and topics, left out on the Deck.
_SKIP_SECTIONS = {"The graphics overlay on Umbriel and niri", "Settings page"}

#: Desktop paragraphs -> the Deck's version (None leaves it out).
_TEXT = {
    "Two optional layers on the game's picture. Switch them on with the Performance overlay and Image effects toggles on the Play page, and choose how they look in Settings → Graphics.":
        "Two optional layers on the game's picture. Switch them on and choose how they look in Settings → Graphics. While a game runs, hold the in-game overlay shortcut to change them over the game.",
    "The quick toggles below the box add common ones for you. Click one to add its text to the box, and again to take it out. They come in three groups:":
        "In Deck Mode, Settings → Launch options has a switch for each common one. Typed launch options are edited in the desktop interface, and Deck Mode uses them too. The switches come in three groups:",
}

#: Desktop table rows, by term -> the Deck's (term, text), or None.
_ROWS = {
    "Hotkeys": (
        "In-game overlay",
        "Hold the in-game overlay shortcut (L4 + R4 unless you changed it in Settings) to open Graphics over the running game. Hold it again, or press B, to go back to the game.",
    ),
    "How to turn it on": (
        "How to turn it on",
        "Install GameMode with your package manager, then turn on Always use GameMode in Deck Mode's Settings. System Check shows whether it's installed.",
    ),
    "Desktop shortcut": None,
}


def deck_topic(topic: dict) -> dict:
    """``topic`` with the desktop-only parts swapped for the Deck's."""

    def block(item):
        if isinstance(item, str):
            return _TEXT.get(item, item)
        kind, data = item
        if kind == "table":
            rows = [_ROWS.get(term, (term, text)) for term, text in data]
            return kind, [row for row in rows if row is not None]
        return item

    sections = []
    for heading, blocks in topic["sections"]:
        if heading in _SKIP_SECTIONS:
            continue
        blocks = [b for b in map(block, blocks) if b is not None]
        sections.append((heading, blocks))
    return {**topic, "sections": sections}


def topic_html(topic: dict, values: dict[str, str]) -> str:
    """A translated topic as the rich text its panel shows."""
    parts = [f"<p>{html.escape(topic['summary'])}</p>"]
    for heading, blocks in topic["sections"]:
        if heading:
            parts.append(f"<h3>{html.escape(heading)}</h3>")
        for block in blocks:
            if isinstance(block, str):
                parts.append(f"<p>{html.escape(block)}</p>")
                continue
            kind, data = block
            if kind in ("list", "steps"):
                tag = "ul" if kind == "list" else "ol"
                items = "".join(f"<li>{html.escape(line)}</li>" for line in data)
                parts.append(f"<{tag}>{items}</{tag}>")
            elif kind == "table":
                parts += [f"<p><b>{html.escape(term)}</b><br>{html.escape(text)}</p>" for term, text in data]
            elif kind == "code":
                parts.append(f"<pre>{html.escape(data)}</pre>")
            elif kind in ("note", "warn"):
                parts.append(f"<p><i>{html.escape(data)}</i></p>")
            elif kind == "live":
                parts += [
                    f"<p>{html.escape(caption)}<br><tt>{html.escape(values.get(key) or tr('Not set'))}</tt></p>"
                    for caption, key in data
                ]
    return "".join(parts)


class HelpScreen(DeckScreen):
    came_from = None

    def build(self) -> None:
        self._topic: int | None = None
        self.list_widget = deck_label(tr("Help"), role="rowTitle")
        self.body.addWidget(self.list_widget)
        self.controls_row = DeckRow(tr("Controls"))
        self.controls_row.activated.connect(self.window.show_controller_help)
        self.body.addWidget(self.controls_row)
        self.topic_rows: list[DeckRow] = []
        for index, topic in enumerate(TOPICS):
            row = DeckRow(tr(topic["title"]))
            row.activated.connect(lambda index=index: self.show_topic(index))
            self.body.addWidget(row)
            self.topic_rows.append(row)

        self.title = deck_label("", role="rowTitle")
        self.body.addWidget(self.title)
        self.text = DeckTextPanel(min_height=420)
        self.text.label.setTextFormat(Qt.TextFormat.RichText)
        self.body.addWidget(self.text, 1)
        self.open_button = deck_button("", on_click=self._open_page)
        self.body.addWidget(self.open_button)
        self._show_list()

    def _list_widgets(self):
        return (self.list_widget, self.controls_row, *self.topic_rows)

    def _show_list(self) -> None:
        self._topic = None
        for widget in self._list_widgets():
            widget.show()
        for widget in (self.title, self.text, self.open_button):
            widget.hide()

    def show_topic(self, index: int) -> None:
        self._topic = index
        topic = _translated(deck_topic(TOPICS[index]))
        for widget in self._list_widgets():
            widget.hide()
        self.title.setText(topic["title"])
        self.text.setText(topic_html(topic, live_values(self.window)))
        page = _DECK_PAGES.get(topic["page"] or "")
        title = next((title for key, title, _glyph in SCREENS if key == page), "")
        self.open_button.setText(tr("Open {title}", title=tr(title)) if page else "")
        self.open_button.setVisible(bool(page))
        self.title.show()
        self.text.show()
        QTimer.singleShot(0, self, lambda: self.window._focus.focus(self.text))

    def _open_page(self) -> None:
        page = _DECK_PAGES.get(TOPICS[self._topic]["page"] or "") if self._topic is not None else None
        if page:
            self.window.set_page(page)

    def on_back(self) -> bool:
        if self._topic is not None:
            row = self.topic_rows[self._topic]
            self._show_list()
            QTimer.singleShot(0, self, lambda: self.window._focus.focus(row))
            return True
        self.window.set_page(self.came_from or self.window.start_screen())
        return True
