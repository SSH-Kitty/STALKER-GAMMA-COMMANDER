"""Allow running Deck Mode directly: ``python -m Steamdeck``.

A convenience wrapper around ``python -m commander_gui --deck`` so the Deck
UI has a module target of its own for scripts and shortcuts. Startup itself
(the instance lock, theme, fonts, CLI check) belongs to ``commander_gui``
and is not duplicated here.
"""

from __future__ import annotations

import sys

from commander_gui.deck_launch import DECK_FLAG
from commander_gui.main import main

if __name__ == "__main__":
    sys.exit(main([sys.argv[0], *sys.argv[1:], DECK_FLAG]))
