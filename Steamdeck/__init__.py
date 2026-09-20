"""Steam Deck Mode - a second, minimal COMMANDER interface.

COMMANDER's main window is built for a mouse on a desktop monitor: a
1080x950 default size, ~28px controls, and a stylesheet whose only :focus
rule covers text inputs, so a focused button is invisible. None of that
survives a Steam Deck, which is a 1280x800 screen at 204 PPI held at arm's
length and driven by a D-pad, two trackpads and a touchscreen.

This package is the Deck-facing presentation layer. It adds no install,
launch or modlist logic of its own - every screen calls the same
``commander_gui`` backend modules the desktop pages call, and reuses
``commander_gui.ui.common``'s threading primitives unchanged. What it
replaces is the geometry: 72px rows, 96px primary buttons, a bottom tab bar,
full-screen pickers instead of dropdown popups, in-window overlays instead
of dialogs, and a focus ring loud enough to steer with a thumbstick.

Input relies entirely on Steam Input's default desktop layout (D-pad =
arrow keys, A = Enter, B = Escape, Y = Space, right trackpad = mouse), so
there is no gamepad dependency to install or device permission to grant.

The dependency runs one way: ``Steamdeck`` imports ``commander_gui``, never
the reverse. ``commander_gui`` touches this package at exactly one site, a
function-local import in ``commander_gui/main.py``'s Deck branch.

Two notes on this module itself:

* It must stay free of Qt imports. ``build-appimage.sh`` verifies the
  bundled payload by importing it with no display attached.
* The capitalised package name is deliberate (the project folder is
  ``Steamdeck/``). It is not PEP 8, and ruff's default rule set does not
  flag it; if ``N`` rules are ever enabled, exempt this package rather than
  renaming it. On a case-insensitive filesystem ``import steamdeck`` would
  also resolve, creating a second module object - harmless on the Linux
  target, but worth knowing.
"""

from __future__ import annotations

__version__ = "1.3.0"
