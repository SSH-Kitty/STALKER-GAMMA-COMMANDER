"""One scale factor for the whole Deck interface.

Deck Mode is designed pixel-for-pixel for the Deck's 1280x800 panel. On a
bigger window - maximised on a monitor, or the Deck docked to a TV - it now
grows with the window, the way a game's HUD does, instead of sitting as a
fixed 1280x800 island in the middle. Everything that has a size goes through
:func:`px`: row heights, margins and spacings in Python, and (via
``deck_theme.build_deck_stylesheet``) every ``NNpx`` in the stylesheet.

The factor only ever grows the layout (never below 1.0 - the Deck's own
size is the floor the touch targets were designed for), and it is applied by
rebuilding the interface, the same way a language change is, so no widget
has to know how to re-measure itself.

Qt-free on purpose: the stylesheet builder imports it too.
"""

from __future__ import annotations

#: The design size everything is budgeted against.
DESIGN_W, DESIGN_H = 1280, 800

#: Past this the interface is legible from across a room; growing further
#: only wastes the extra space.
MAX_SCALE = 2.5

_scale = 1.0


def scale() -> float:
    return _scale


def set_scale(value: float) -> None:
    global _scale
    _scale = max(1.0, min(MAX_SCALE, float(value)))


def scale_for(width: int, height: int) -> float:
    """The factor a ``width`` x ``height`` window should use.

    The smaller of the two ratios, so the layout always fits: a 16:9 TV is
    limited by its height, a tall window by its width. Rounded to 0.05 so a
    window resized a few pixels does not trigger a rebuild.
    """
    if width <= 0 or height <= 0:
        return 1.0
    raw = min(width / DESIGN_W, height / DESIGN_H)
    return max(1.0, min(MAX_SCALE, round(raw * 20) / 20))


def px(value: float) -> int:
    """``value`` design pixels at the current scale."""
    if value == 0:
        return 0
    return max(1, round(value * _scale))
