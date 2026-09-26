"""The Welcome screen shown when Deck Mode opens.

The latest COMMANDER patch notes on the left, links to GitHub, the Discord
and the credits on the right, and a "Don't show again" box in the corner.
Drawn as a translucent overlay in the live theme's colours, so the screen
it opens over still shows through.

The patch notes come from the release feed (``updates.
fetch_latest_release_notes``), fetched in the background: the panel opens
at once with a placeholder and never waits on the network.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QVBoxLayout, QWidget

from commander_gui import __version__, __version_label__, gui_settings
from commander_gui.deck_launch import in_game_mode
from commander_gui.i18n import tr
from commander_gui.themes import active_theme_tokens
from commander_gui.ui.about_page import _DISCORD, _GITHUB, CREDITS
from commander_gui.ui.brand_icons import brand_icon, icon_size
from commander_gui.ui.common import BackgroundTask
from commander_gui.ui.common import open_url as open_web_url
from commander_gui.ui.welcome_overlay import release_name, version_status
from commander_gui.updates import effective_update_channel, fetch_latest_release_notes

from .scale import px
from .widgets import (
    BUTTON_H,
    DeckOverlay,
    DeckTextPanel,
    deck_button,
    deck_divider_v,
    deck_label,
    repolish,
)

RELEASES_URL = f"{_GITHUB}/releases"


def open_url(window, url: str) -> None:
    """Open ``url`` in a browser - Steam's own under Game Mode.

    Game Mode's gamescope session has no desktop browser to hand a link
    to; ``steam://openurl/`` opens it in the Steam overlay instead.
    """
    target = f"steam://openurl/{url}" if in_game_mode() else url
    if open_web_url(target):
        window.notify(tr("Opening {url}...", url=url))
    else:
        window.notify(tr("Could not open {url}", url=url), 6000)


class WelcomePanel(QWidget):
    """The overlay's body: notes | links, and the checkbox under them."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self._task: BackgroundTask | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(px(12))

        # Your version -> latest release, and how they compare, on one line.
        bar = QFrame()
        bar.setObjectName("deckVersionBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(px(18), px(10), px(18), px(10))
        bar_layout.setSpacing(px(10))
        bar_layout.addWidget(deck_label(tr("Your version"), role="caption"))
        installed = deck_label(__version_label__, role="section")
        bar_layout.addWidget(installed)
        bar_layout.addSpacing(px(10))
        bar_layout.addWidget(deck_divider_v(), 0, Qt.AlignmentFlag.AlignVCenter)
        bar_layout.addSpacing(px(10))
        bar_layout.addWidget(deck_label(tr("Latest release"), role="caption"))
        self.latest_label = deck_label("\u2026", role="section")
        bar_layout.addWidget(self.latest_label)
        # Section headings carry top padding; here it pushed the numbers
        # below their captions' line.
        for value in (installed, self.latest_label):
            value.setStyleSheet("padding-top: 0px;")
        bar_layout.addStretch(1)
        self.version_status = deck_label("", role="body")
        bar_layout.addWidget(self.version_status)
        outer.addWidget(bar)
        self._set_latest(None)

        row = QHBoxLayout()
        row.setSpacing(px(24))

        notes_column = QVBoxLayout()
        notes_column.setSpacing(px(8))
        self.notes_title = deck_label(tr("Patch notes"), role="section")
        notes_column.addWidget(self.notes_title)
        self.notes = DeckTextPanel(
            tr("Loading patch notes..."), min_height=320, max_height=420
        )
        notes_column.addWidget(self.notes, 1)
        row.addLayout(notes_column, 3)

        links = QVBoxLayout()
        links.setSpacing(px(12))
        links.addSpacing(px(34))
        self.github_button = deck_button(
            "  " + tr("GitHub"), on_click=lambda: open_url(window, _GITHUB)
        )
        self.discord_button = deck_button(
            "  " + tr("Discord"), on_click=lambda: open_url(window, _DISCORD)
        )
        logo_color = active_theme_tokens().get("text_btn_hover", "#e0e0e0")
        for button, logo in ((self.github_button, "github"), (self.discord_button, "discord")):
            color = "#ffffff" if logo == "discord" else logo_color
            button.setIcon(brand_icon(logo, color, px(26)))
            button.setIconSize(icon_size(px(26)))
        # Discord in its own colour, first in the list: where to get help.
        self.discord_button.setObjectName("deckDiscord")
        self.releases_button = deck_button(
            "\u2197  " + tr("All releases"), on_click=lambda: open_url(window, RELEASES_URL)
        )
        self.credits_button = deck_button(
            "\u2605  " + tr("Credits"), on_click=self._show_credits
        )
        for button in (
            self.discord_button,
            self.github_button,
            self.releases_button,
            self.credits_button,
        ):
            button.setFixedHeight(px(BUTTON_H))
            links.addWidget(button)
        links.addStretch(1)
        row.addLayout(links, 2)
        outer.addLayout(row, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.hide_check = QCheckBox(tr("Don't show again"))
        self.hide_check.setObjectName("deckCheck")
        self.hide_check.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hide_check.setChecked(
            bool(gui_settings.load_gui_settings().get("welcome_hidden"))
        )
        self.hide_check.toggled.connect(
            lambda on: gui_settings.save_gui_settings(welcome_hidden=bool(on))
        )
        bottom.addWidget(self.hide_check)
        outer.addLayout(bottom)

        self._load_notes()

    def _set_latest(self, latest_title: str | None) -> None:
        name = release_name(latest_title)
        self.latest_label.setText(name or ("\u2026" if latest_title is None else "?"))
        text, kind = version_status(latest_title)
        self.version_status.setText(("\u25cf  " + text) if kind != "dim" else text)
        self.version_status.setObjectName(
            {"ok": "deckBodyOk", "warn": "deckBodyWarn"}.get(kind, "deckCaption")
        )
        repolish(self.version_status)

    def _load_notes(self) -> None:
        channel = effective_update_channel(
            gui_settings.load_gui_settings().get("update_channel"), __version__
        )
        task = BackgroundTask(fetch_latest_release_notes, channel, parent=self)
        self._task = task
        task.result.connect(self._show_notes)
        task.error.connect(lambda _m: self._show_notes(None))
        task.start()

    def _show_notes(self, result: object) -> None:
        self._task = None
        label = self.notes.label
        if not isinstance(result, tuple) or len(result) != 2 or not result[1]:
            self._set_latest("")
            label.setTextFormat(Qt.TextFormat.PlainText)
            self.notes.setText(
                tr("Couldn't load the patch notes. They are on the Releases page:")
                + "\n"
                + RELEASES_URL
            )
            return
        title, body = result
        self._set_latest(str(title))
        # GitHub's feed carries the release body already rendered to HTML.
        # QLabel's rich text fetches nothing remote and runs no script.
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(False)
        self.notes.setText(str(body))

    def _show_credits(self) -> None:
        text = "\n".join(tr("• {line}", line=tr(line)) for line in CREDITS)
        self.window.show_overlay(
            DeckOverlay(
                tr("Credits"),
                DeckTextPanel(text, min_height=200, max_height=420),
                [(tr("Close"), self.window.dismiss_overlay, "primary")],
                panel_width=900,
                translucent=True,
            ),
            stacked=True,
        )


def show_welcome(window) -> None:
    panel = WelcomePanel(window)
    overlay = DeckOverlay(
        tr("Welcome to COMMANDER"),
        panel,
        panel_width=1150,
        header_buttons=[("✕", window.dismiss_overlay, "normal")],
        translucent=True,
    )
    overlay.default_button = panel.github_button
    overlay.hints = (("A", "Select"), ("R-stick", "Scroll notes"), ("B", "Close"))
    overlay.scroll_target = panel.notes
    # Kept across a rebuild (language change, rescale): the window calls
    # this to put the panel back instead of silently dropping it.
    overlay.reopen = lambda: show_welcome(window)
    window.show_overlay(overlay)
