#!/usr/bin/env python3
"""Rebuild the Winter theme's scenes in commander_gui/assets/winter/.

One set per time of day:
- backdrop.jpg (day), dawn.jpg, dusk.jpg, night.jpg: a courtyard of Soviet
  panel blocks with a snowed-in playground.
- day|dawn|dusk|night_left.png / _right.png: the trees framing each edge, on
  their own see-through layers so the app can sway them (ui/redforest.py).

Draws the scene as SVG, renders it blurred with a headless Chromium-based
browser and saves it. The blur is baked in: the frosted cards sit over it.
Away from day, the scene is shaded with a multiplied grade and its lights
(windows, porches, the street lamp, the moon) are added on top unshaded.

Usage: ./build-winter-backdrop.py [browser]   (default: brave, then chromium)
Needs Pillow (python3 -m pip install pillow).
"""
import itertools
import math
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent / "commander_gui" / "assets" / "winter"
W, H = 1722, 1094  # scene's drawing size; scaled to cover the output
OUT_W, OUT_H = 1920, 1200
BLUR = 2

V = {"sky": ("#dfe5ea", "#c3ccd4", "#a9b4bd"), "snowcap": "#eef2f5", "fog": "rgba(225,232,238,%s)",
     "stars": 0,  # added after the grade, so they stay bright
     # (color, opacity, x, y, radius) of the hazy sun or moon
     "sun": ("#ffffff", 0.55, 0.78, 0.18, 70),
     "moon": False,  # a sharp disc with a halo, kept bright over the grade
     # Multiplied over the whole scene, so the blocks and trees darken too.
     "grade": None,
     "lights": 0,  # how strongly lit windows, porches and the lamp show through the grade
     }

#: Output file -> what changes from the day scene.
PHASES = {
    "backdrop.jpg": {},
    "dawn.jpg": {
        "sky": ("#c9cfdc", "#e6cfc8", "#f0d3c0"), "fog": "rgba(240,224,216,%s)",
        "sun": ("#ffd9b3", 0.8, 0.78, 0.48, 80),
        "grade": "linear-gradient(#ebe6ee, #f4e9e2)", "lights": 0.5,
    },
    "dusk.jpg": {
        "sky": ("#3d3c52", "#8a6a72", "#d98f5c"), "fog": "rgba(150,120,122,%s)",
        "sun": ("#ffb070", 0.75, 0.2, 0.5, 90),
        "grade": "linear-gradient(#6a6676, #8c7570)", "stars": 12, "lights": 0.8,
    },
    "night.jpg": {
        "sky": ("#0d131c", "#18212c", "#26303b"), "fog": "rgba(58,68,80,%s)",
        "sun": ("#f4f8fb", 1.0, 0.78, 0.16, 55), "moon": True,
        "grade": "linear-gradient(#363f4a, #45505b)", "stars": 320, "lights": 1,
    },
}


def spruce(x, base, h, color, rnd, snow=None):
    """A spruce with drooping, jagged branch tiers and snow along the tips.
    The trunk runs on below the base so the snow in front always buries it."""
    w = h * rnd.uniform(0.3, 0.38)
    tiers = max(5, int(h / 26))
    th = h * 0.88 / tiers
    out = [f'<rect x="{x-h*0.018:.1f}" y="{base-h*0.3:.0f}" width="{h*0.036:.1f}" height="{h*0.3+40:.0f}" fill="{color}"/>']
    for i in range(tiers):
        f = i / tiers
        y0 = base - h * 0.1 - th * i
        top = y0 - th * 2.1
        ww = w * (1 - f) ** 0.85 * rnd.uniform(0.85, 1.1) + h * 0.02
        droop = th * 0.35
        pts = [(x - ww, y0 + droop)]
        n = rnd.randint(3, 5)
        for k in range(1, 2 * n):
            px = x - ww + 2 * ww * k / (2 * n)
            pts.append((px, y0 + (droop * 0.6 if k % 2 == 0 else -th * 0.25)))
        pts += [(x + ww, y0 + droop), (x + ww * 0.12, top), (x, top - th * 0.15), (x - ww * 0.12, top)]
        out.append(f'<polygon points="{" ".join(f"{a:.0f},{b:.0f}" for a, b in pts)}" fill="{color}"/>')
        if snow:
            # a shelf of snow lying on the tier's lower boughs
            out.append(f'<path d="M{x-ww*0.92:.0f},{y0+droop*0.5:.0f} Q{x:.0f},{y0-th*0.95:.0f} {x+ww*0.92:.0f},{y0+droop*0.5:.0f} '
                       f'Q{x:.0f},{y0-th*0.25:.0f} {x-ww*0.92:.0f},{y0+droop*0.5:.0f} Z" fill="{snow}" opacity=".6"/>')
    return "".join(out)


def birch(x, base, h, rnd):
    """Tapering white trunk, dark lenticels and base, bare branching crown."""
    tx = x + h * rnd.uniform(-0.04, 0.04)
    bw = rnd.uniform(5, 8)
    s = [f'<polygon points="{x-bw/2:.1f},{base+30} {x+bw/2:.1f},{base+30} {tx+1:.1f},{base-h:.0f} {tx-1:.1f},{base-h:.0f}" fill="#e9e7e1"/>',
         f'<polygon points="{x:.1f},{base+30} {x+bw/2:.1f},{base+30} {tx+1:.1f},{base-h:.0f} {tx:.1f},{base-h:.0f}" fill="#b3b2ad" opacity=".55"/>']
    for _ in range(int(h / 12)):
        t = rnd.random() ** 1.4  # marks crowd towards the foot
        px, py, ww = x + (tx - x) * t, base - h * t, bw * (1 - t * 0.75)
        s.append(f'<rect x="{px-ww/2:.1f}" y="{py:.0f}" width="{ww*rnd.uniform(0.35,1):.1f}" height="{rnd.uniform(1.2,3):.1f}" fill="#2f2d2a"/>')
    s.append(f'<polygon points="{x-bw/2:.1f},{base+30} {x+bw/2:.1f},{base+30} {x+bw*0.4:.1f},{base-h*0.1:.0f} {x-bw*0.4:.1f},{base-h*0.07:.0f}" fill="#45403a" opacity=".8"/>')
    for _ in range(rnd.randint(5, 8)):
        t = rnd.uniform(0.4, 0.92)
        side = rnd.choice((-1, 1))
        s.append(twigs(x + (tx - x) * t, base - h * t, math.pi / 2 - side * rnd.uniform(0.45, 1.05),
                       h * rnd.uniform(0.07, 0.14), 3, rnd, "#5a534d", 2.4 * (1 - t) + 0.7))
    s.append(twigs(tx, base - h, math.pi / 2 + rnd.uniform(-0.3, 0.3), h * 0.07, 3, rnd, "#6a625b", 1.0))
    return "".join(s)


def grass(x, y, rnd, n=7):
    """Dry stalks poking out of the snow."""
    return "".join(
        f'<path d="M{x+rnd.uniform(-10,10):.0f},{y} q{rnd.uniform(-6,6):.0f},{-rnd.uniform(10,20):.0f} {rnd.uniform(-10,10):.0f},{-rnd.uniform(18,38):.0f}" '
        f'stroke="{rnd.choice(("#8a7a5e", "#76684f", "#9a8b6c"))}" stroke-width="1.2" fill="none"/>'
        for _ in range(n))


def ridge(pts):
    """Closed path: a smooth snow line through pts, filled down to the bottom."""
    d = f"M{pts[0][0]},{H+10} L{pts[0][0]},{pts[0][1]:.0f}"
    for (ax, ay), (bx, by) in itertools.pairwise(pts):
        d += f" Q{ax},{ay:.0f} {(ax+bx)/2:.0f},{(ay+by)/2:.0f}"
    return d + f" L{pts[-1][0]},{pts[-1][1]:.0f} L{pts[-1][0]},{H+10} Z"


def snowline(y, amp, rnd, step=160, flat=None):
    """Points for ridge(); `flat` (x0, x1, y) keeps the line flat over a stretch."""
    pts = []
    for x in range(-step, W + 2 * step, step):
        yy = y + rnd.uniform(-amp, amp)
        if flat and flat[0] < x < flat[1]:
            yy = flat[2]
        pts.append((x, yy))
    return pts


def twigs(x, y, angle, length, depth, rnd, color, width):
    """A leafless branch, split recursively: dead bushes and bare fruit trees."""
    x2 = x + math.cos(angle) * length
    y2 = y - math.sin(angle) * length
    out = [(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" '
            f'stroke-width="{width:.2f}" stroke-linecap="round"/>')]
    if depth:
        for _ in range(rnd.choice((2, 2, 3))):
            out.append(twigs(x2, y2, angle + rnd.uniform(-0.75, 0.75), length * rnd.uniform(0.55, 0.78),
                             depth - 1, rnd, color, max(0.5, width * 0.62)))
    return "".join(out)


SC = V["snowcap"]
#: Night window colours: warm bulbs, cold LEDs and TV-blue.
LIT = ("#ffd08a", "#f5b867", "#ffe2b0", "#d9e7ff", "#b8d2ff", "#ffc49a", "#a9c8ff")
#: Painted balcony fronts.
BALC = ("#d9c7a8", "#9fadb9", "#c9b18a", "#e4e4de", "#b5b9b4", "#c7a99a")


def glowing(parts):
    """Things that light up: hidden by day, added unshaded on top at night."""
    return f'<g class="lit">{"".join(parts)}</g>' if parts else ""


def block(x, base, floors, sections, rnd, fh=40, bw=36, tone="#b9bcb7", end=None, ew=0, far=None):
    """A Soviet prefab panel block, front on: panel seams, windows, a column of
    balconies per section (some glazed, painted fronts), a stairwell column with
    half-floor windows and a porch canopy, snow on the flat roof. `far` draws
    only a grid of windows in that colour, for blocks lost in the fog."""
    w = sections * 4 * bw
    top = base - floors * fh - 16
    s, lights = [], []
    if end:
        ex = x if end == "left" else x + w
        k = -1 if end == "left" else 1
        wall = f"M{ex},{top} L{ex+k*ew},{top+10} L{ex+k*ew},{base} L{ex},{base} Z"
        s.append(f'<path d="{wall}" fill="{tone}" filter="url(#conc)"/><path d="{wall}" fill="#000" opacity=".2"/>')
        for i in range(1, 3):
            s.append(f'<line x1="{ex+k*ew*i/3:.0f}" y1="{top+10*i/3:.0f}" x2="{ex+k*ew*i/3:.0f}" y2="{base}" stroke="#000" stroke-opacity=".18"/>')
        s.append(f'<path d="M{ex-k*2},{top-8} L{ex+k*ew},{top+3} L{ex+k*ew},{top+11} L{ex},{top} Z" fill="{SC}"/>')
    s.append(f'<rect x="{x}" y="{top}" width="{w}" height="{base-top}" fill="{tone}" filter="url(#conc)"/>')
    if far:
        for c in range(sections * 4):
            for f in range(floors):
                wx, wy = x + c * bw + bw * 0.25, base - (f + 1) * fh + fh * 0.3
                pane = f'x="{wx:.0f}" y="{wy:.0f}" width="{bw*0.5:.0f}" height="{fh*0.45:.0f}"'
                s.append(f'<rect {pane} fill="{far}"/>')
                if rnd.random() < 0.3:
                    lights.append(f'<rect {pane} fill="{rnd.choice(LIT)}" opacity=".8"/>')
        s.append(f'<rect x="{x-3}" y="{top-6}" width="{w+6}" height="7" fill="{SC}"/>')
        return "".join(s) + glowing(lights)
    for f in range(1, floors):
        y = base - f * fh
        s.append(f'<line x1="{x}" y1="{y}" x2="{x+w}" y2="{y+rnd.uniform(-.5,.5):.1f}" stroke="#000" stroke-opacity=".16" stroke-width="1.2"/>')
    for c in range(1, sections * 4):
        s.append(f'<line x1="{x+c*bw}" y1="{top}" x2="{x+c*bw}" y2="{base}" stroke="#000" stroke-opacity=".1"/>')
    # rain streaks under the roof edge
    for _ in range(sections * 6):
        s.append(f'<rect x="{rnd.uniform(x, x + w):.0f}" y="{top}" width="{rnd.uniform(3,12):.0f}" '
                 f'height="{rnd.uniform(30,fh*floors*.6):.0f}" fill="#3b3a36" opacity=".07"/>')
    for sec in range(sections):
        for b in range(4):
            bx = x + (sec * 4 + b) * bw
            for f in range(floors):
                fy = base - (f + 1) * fh
                if b == 2:  # stairwell
                    if f == 0:
                        s.append(f'<rect x="{bx+bw*.2:.0f}" y="{fy+fh*.25:.0f}" width="{bw*.6:.0f}" height="{fh*.75:.0f}" fill="#3a3330"/>'
                                 f'<rect x="{bx-4}" y="{fy+fh*.12:.0f}" width="{bw+8}" height="5" fill="#8e908b"/>'
                                 f'<rect x="{bx-5}" y="{fy+fh*.12-5:.0f}" width="{bw+10}" height="6" rx="3" fill="{SC}"/>')
                        lights.append(f'<circle cx="{bx+bw/2:.0f}" cy="{fy+fh*.2:.0f}" r="10" fill="#ffd9a0" opacity=".7" filter="url(#lglow)"/>')
                    else:
                        pane = f'x="{bx+bw*.3:.0f}" y="{fy+fh*0.55:.0f}" width="{bw*.4:.0f}" height="{fh*.38:.0f}"'
                        s.append(f'<rect {pane} fill="url(#glass)"/>')
                        if rnd.random() < 0.4:
                            lights.append(f'<rect {pane} fill="#fff1d0" opacity=".6"/>')
                    continue
                ww, wh = bw * (0.62 if b == 1 else 0.56), fh * 0.5
                wx, wy = bx + (bw - ww) / 2, fy + fh * 0.2
                s.append(f'<rect x="{wx-2:.1f}" y="{wy-2:.1f}" width="{ww+4:.1f}" height="{wh+4:.1f}" fill="{"#e9e9e4" if rnd.random()<.6 else "#6d5a48"}"/>'
                         f'<rect x="{wx:.1f}" y="{wy:.1f}" width="{ww:.1f}" height="{wh:.1f}" fill="url(#glass)"/>'
                         f'<line x1="{wx+ww/2:.1f}" y1="{wy:.1f}" x2="{wx+ww/2:.1f}" y2="{wy+wh:.1f}" stroke="#d9d9d4" stroke-width="1.2"/>'
                         f'<rect x="{wx-3:.1f}" y="{wy+wh+1:.1f}" width="{ww+6:.1f}" height="2.5" fill="{SC}"/>')
                if rnd.random() < 0.36:
                    tint = rnd.choice(LIT)
                    lights.append(f'<rect x="{wx-2:.1f}" y="{wy-2:.1f}" width="{ww+4:.1f}" height="{wh+4:.1f}" fill="{tint}" opacity=".35" filter="url(#lglow)"/>'
                                  f'<rect x="{wx:.1f}" y="{wy:.1f}" width="{ww:.1f}" height="{wh:.1f}" fill="{tint}" opacity=".92"/>'
                                  f'<line x1="{wx+ww/2:.1f}" y1="{wy:.1f}" x2="{wx+ww/2:.1f}" y2="{wy+wh:.1f}" stroke="#3a3530" stroke-width="1.2" opacity=".6"/>')
                if rnd.random() < 0.05:  # air conditioner
                    s.append(f'<rect x="{wx+ww+3:.0f}" y="{wy+wh*.3:.0f}" width="{bw*.3:.0f}" height="{fh*.22:.0f}" fill="#e8e8e4"/>')
                if b == 1 and f > 0:  # balcony
                    py = fy + fh * 0.62
                    s.append(f'<rect x="{bx+1}" y="{py:.0f}" width="{bw-2}" height="{fh*.36:.0f}" fill="{rnd.choice(BALC)}"/>'
                             f'<rect x="{bx}" y="{py+fh*.36:.0f}" width="{bw}" height="3" fill="#7d7f7b"/>'
                             f'<rect x="{bx-1}" y="{py-3:.0f}" width="{bw+2}" height="3" fill="{SC}"/>')
                    if rnd.random() < 0.55:  # glazed in
                        gy = fy + fh * 0.12
                        s.append(f'<rect x="{bx+1}" y="{gy:.0f}" width="{bw-2}" height="{py-gy:.0f}" fill="url(#glass)" opacity=".9"/>'
                                 + "".join(f'<line x1="{bx+1+(bw-2)*k/3:.0f}" y1="{gy:.0f}" x2="{bx+1+(bw-2)*k/3:.0f}" y2="{py:.0f}" stroke="#e9e9e4" stroke-width="1.5"/>' for k in range(4))
                                 + f'<rect x="{bx}" y="{gy-3:.0f}" width="{bw}" height="3" fill="{SC}"/>')
    # roof: parapet, snow, lift rooms on the tall blocks, TV antennas
    s.append(f'<rect x="{x-2}" y="{top-4}" width="{w+4}" height="6" fill="#8b8d89"/>'
             f'<rect x="{x-4}" y="{top-11}" width="{w+8}" height="8" rx="4" fill="{SC}"/>'
             f'<rect x="{x}" y="{base-12}" width="{w}" height="12" fill="#6f716d"/>')
    for sec in range(sections):
        cx = x + (sec * 4 + 2) * bw
        if floors > 6:
            s.append(f'<rect x="{cx-6}" y="{top-34}" width="{bw+12}" height="26" fill="{tone}" filter="url(#conc)"/>'
                     f'<rect x="{cx-8}" y="{top-40}" width="{bw+16}" height="7" rx="3" fill="{SC}"/>')
        if rnd.random() < 0.7:
            ax = cx + rnd.uniform(-bw * 2, bw * 2)
            s.append(f'<line x1="{ax:.0f}" y1="{top-8}" x2="{ax:.0f}" y2="{top-48}" stroke="#55585a" stroke-width="1.6"/>'
                     + "".join(f'<line x1="{ax-l:.0f}" y1="{top-44+k}" x2="{ax+l:.0f}" y2="{top-44+k}" stroke="#55585a" stroke-width="1.2"/>' for k, l in ((0, 14), (6, 10), (12, 7))))
    return "".join(s) + glowing(lights)


def frosted(x, base, h, seed, dark="#4e4741"):
    """Bare deciduous tree with hoarfrost: the crown drawn dark, then again in white."""
    out = [f'<polygon points="{x-h*.025:.0f},{base+30} {x+h*.025:.0f},{base+30} {x+3:.0f},{base-h*.45:.0f} {x-3:.0f},{base-h*.45:.0f}" fill="{dark}"/>']
    for color, k, op in ((dark, 1.0, 1), (SC, 0.55, .85)):
        rnd = random.Random(seed)
        for _ in range(6):
            t = rnd.uniform(0.3, 0.45)
            out.append(f'<g opacity="{op}" transform="translate(0 {-1.5 if k < 1 else 0})">'
                       + twigs(x, base - h * t, math.pi / 2 + rnd.uniform(-0.75, 0.75), h * rnd.uniform(0.2, 0.32), 5, rnd, color, h * 0.014 * k)
                       + '</g>')
    return "".join(out)


def swing(x, y):
    p = "#3f6b8c"
    s = [f'<path d="M{lx-26},{y+10} L{lx},{y-118} L{lx+26},{y+10}" stroke="{p}" stroke-width="5" fill="none"/>' for lx in (x, x + 140)]
    s.append(f'<line x1="{x}" y1="{y-118}" x2="{x+140}" y2="{y-118}" stroke="{p}" stroke-width="6"/>'
             f'<line x1="{x}" y1="{y-122}" x2="{x+140}" y2="{y-122}" stroke="{SC}" stroke-width="3"/>')
    for cx in (x + 45, x + 95):
        s.append(f'<line x1="{cx-14}" y1="{y-116}" x2="{cx-14}" y2="{y-34}" stroke="#55585a" stroke-width="1.4"/>'
                 f'<line x1="{cx+14}" y1="{y-116}" x2="{cx+14}" y2="{y-34}" stroke="#55585a" stroke-width="1.4"/>'
                 f'<rect x="{cx-17}" y="{y-36}" width="34" height="6" fill="#b5524a"/>'
                 f'<rect x="{cx-17}" y="{y-40}" width="34" height="4" rx="2" fill="{SC}"/>')
    return "".join(s)


def arch(x, y, r):
    """Rainbow climbing arch seen at an angle, half sunk in the snow."""
    s = []
    for k, c in enumerate(("#b5524a", "#d1b450", "#6d9a5a", "#5a7fa6", "#b5524a", "#d1b450")):
        dx, dy = k * r * 0.2, -k * r * 0.07
        s.append(f'<path d="M{x+dx-r:.0f},{y+dy:.0f} A{r:.0f},{r*.95:.0f} 0 0 1 {x+dx+r:.0f},{y+dy:.0f}" stroke="{c}" stroke-width="6" fill="none"/>')
        if k:  # rungs back to the previous hoop
            for a in range(1, 8):
                th = math.pi * a / 8
                p0 = (x + dx - r * math.cos(th), y + dy - r * .95 * math.sin(th))
                s.append(f'<line x1="{p0[0]:.0f}" y1="{p0[1]:.0f}" x2="{p0[0]-r*.2:.0f}" y2="{p0[1]+r*.07:.0f}" stroke="#d8d8d2" stroke-width="3"/>')
        s.append(f'<path d="M{x+dx-r*.7:.0f},{y+dy-r*.68:.0f} A{r:.0f},{r*.95:.0f} 0 0 1 {x+dx+r*.7:.0f},{y+dy-r*.68:.0f}" stroke="{SC}" stroke-width="3" fill="none" transform="translate(0 -3)"/>')
    return "".join(s)


def mushroom(x, y):
    """Sandbox with the classic red toadstool sunshade."""
    s = [f'<path d="M{x-80},{y} L{x+60},{y} L{x+90},{y-22} L{x-50},{y-22} Z" fill="#6b5240"/>',
         f'<path d="M{x-76},{y-4} Q{x},{y-36} {x+86},{y-20} L{x-48},{y-20} Z" fill="{SC}"/>',
         f'<rect x="{x-3}" y="{y-120}" width="6" height="110" fill="#d9d4c7"/>',
         f'<path d="M{x-58},{y-112} Q{x},{y-160} {x+58},{y-112} Z" fill="#a8382c"/>']
    for dx, dy in ((-30, -124), (6, -136), (30, -122), (-8, -120)):
        s.append(f'<ellipse cx="{x+dx}" cy="{y+dy}" rx="6" ry="3.5" fill="#efe9dc"/>')
    s.append(f'<path d="M{x-40},{y-128} Q{x},{y-162} {x+40},{y-128} Q{x},{y-146} {x-40},{y-128} Z" fill="{SC}"/>')
    return "".join(s)


def bench(x, y, color):
    return (f'<line x1="{x+6}" y1="{y}" x2="{x+6}" y2="{y-26}" stroke="#3a3a38" stroke-width="3"/>'
            f'<line x1="{x+84}" y1="{y}" x2="{x+84}" y2="{y-26}" stroke="#3a3a38" stroke-width="3"/>'
            f'<rect x="{x}" y="{y-28}" width="90" height="6" fill="{color}"/>'
            f'<rect x="{x}" y="{y-52}" width="90" height="7" fill="{color}"/><rect x="{x}" y="{y-42}" width="90" height="7" fill="{color}"/>'
            f'<rect x="{x-2}" y="{y-34}" width="94" height="6" rx="3" fill="{SC}"/><rect x="{x-2}" y="{y-56}" width="94" height="5" rx="2" fill="{SC}"/>')


def car(x, y, color):
    """A parked car under a thick hump of snow."""
    return (f'<path d="M{x},{y} L{x},{y-22} Q{x+4},{y-28} {x+20},{y-30} L{x+38},{y-48} L{x+92},{y-48} L{x+112},{y-30} '
            f'Q{x+134},{y-28} {x+138},{y-20} L{x+138},{y} Z" fill="{color}"/>'
            f'<path d="M{x+42},{y-44} L{x+88},{y-44} L{x+104},{y-30} L{x+30},{y-30} Z" fill="#3a4148"/>'
            f'<path d="M{x-2},{y-24} Q{x+20},{y-36} {x+36},{y-36} L{x+42},{y-54} Q{x+65},{y-62} {x+92},{y-54} L{x+108},{y-34} Q{x+130},{y-34} {x+140},{y-22} '
            f'Q{x+110},{y-28} {x+100},{y-32} Q{x+65},{y-38} {x+36},{y-32} Q{x+16},{y-28} {x-2},{y-24} Z" fill="{SC}"/>')


def lamp(x, y, h=230):
    """Street lamp; its bulb, light cone and pool on the snow only show at night."""
    return (f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y-h}" stroke="#4a4d50" stroke-width="5"/>'
            f'<path d="M{x},{y-h} Q{x+4},{y-h-22} {x+34},{y-h-18}" stroke="#4a4d50" stroke-width="4" fill="none"/>'
            f'<rect x="{x+26}" y="{y-h-20}" width="22" height="7" rx="3" fill="#5a5d60"/>'
            + glowing([f'<ellipse cx="{x+37}" cy="{y-h-12}" rx="10" ry="4" fill="#fff4dc"/>',
                       f'<circle cx="{x+37}" cy="{y-h-10}" r="40" fill="#ffd9a0" opacity=".55" filter="url(#lamp)"/>',
                       f'<path d="M{x+30},{y-h-10} L{x-60},{y+30} L{x+140},{y+30} L{x+44},{y-h-10} Z" fill="#ffe2b8" opacity=".12" filter="url(#lamp)"/>',
                       f'<ellipse cx="{x+40}" cy="{y+18}" rx="140" ry="34" fill="#ffd9a0" opacity=".35" filter="url(#lamp)"/>']))


def bush(x, y, rnd):
    return "".join(twigs(x + rnd.uniform(-25, 25), y + 6, rnd.uniform(1.2, 1.95), rnd.uniform(28, 46), 3, rnd, "#6b4a38", 1.6) for _ in range(9))


def drift(y, rnd, amp=8, step=170):
    """A band of snow from y down, with a soft blue shadow along its top."""
    d = ridge(snowline(y, amp, rnd, step))
    return (f'<path d="{d}" fill="#d0d9e1" transform="translate(0 -5)" filter="url(#rsoft)"/>'
            f'<path d="{d}" fill="{SC}"/>')


def sun(v):
    """The hazy sun, or at night the moon: a sharp disc in a wide glow."""
    color, opacity, x, y, r = v["sun"]
    x, y = W * x, H * y
    if not v["moon"]:
        return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}" opacity="{opacity}" filter="url(#glow)"/>'
    return glowing([f'<circle cx="{x}" cy="{y}" r="{r*3.4:.0f}" fill="#9fb4d0" opacity=".35" filter="url(#halo)"/>',
                    f'<circle cx="{x}" cy="{y}" r="{r*1.3:.0f}" fill="{color}" opacity=".35" filter="url(#glow)"/>',
                    f'<circle cx="{x}" cy="{y}" r="{r*0.5:.0f}" fill="{color}"/>',
                    # maria: the faint grey seas on the face
                    f'<ellipse cx="{x-r*.15:.0f}" cy="{y-r*.12:.0f}" rx="{r*.18:.0f}" ry="{r*.13:.0f}" fill="#d3dbe2"/>',
                    f'<ellipse cx="{x+r*.17:.0f}" cy="{y+r*.1:.0f}" rx="{r*.12:.0f}" ry="{r*.16:.0f}" fill="#d9e0e6"/>'])


def scene(v, rnd):
    """(backdrop svg, left trees svg, right trees svg): a courtyard with a
    five-storey khrushchevka behind a snowed-in playground, taller blocks in
    the fog, frosted trees and birches at the sides."""
    sky = v["sky"]
    g = H * 0.8
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice">',
         '<defs>',
         f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky[0]}"/><stop offset=".6" stop-color="{sky[1]}"/><stop offset="1" stop-color="{sky[2]}"/></linearGradient>',
         f'<linearGradient id="fog" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{v["fog"] % 0}"/><stop offset=".55" stop-color="{v["fog"] % 0.8}"/><stop offset="1" stop-color="{v["fog"] % 0.9}"/></linearGradient>',
         ('<linearGradient id="glass" x1="0" y1="0" x2=".3" y2="1"><stop offset="0" stop-color="#8d979e"/>'
          '<stop offset=".5" stop-color="#4f585f"/><stop offset="1" stop-color="#363d43"/></linearGradient>'),
         # Weathered concrete: blotchy noise multiplied over the panel colour.
         ('<filter id="conc" x="0" y="0" width="1" height="1">'
          '<feTurbulence type="fractalNoise" baseFrequency="0.04 0.08" numOctaves="4" seed="3"/>'
          '<feColorMatrix values="0 0 0 0 0.2  0 0 0 0 0.2  0 0 0 0 0.2  0 0 0 -0.9 0.6"/>'
          '<feComposite in2="SourceGraphic" operator="in"/>'
          '<feBlend in="SourceGraphic" mode="multiply"/></filter>'),
         '<filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="18"/></filter>',
         '<filter id="rsoft"><feGaussianBlur stdDeviation="3"/></filter>',
         '<filter id="lglow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4"/></filter>',
         '<filter id="lamp" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="22"/></filter>',
         '</defs>',
         f'<rect width="{W}" height="{H}" fill="url(#sky)"/>',
         '<filter id="halo" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="45"/></filter>',
         sun(v),
         '<!--sky-->']
    s.append(block(-40, g - 120, 9, 3, rnd, fh=24, bw=22, tone="#a7aca9", far="#7e868c"))
    s.append(block(1330, g - 110, 9, 3, rnd, fh=26, bw=24, tone="#a9adaa", far="#7e868c"))
    s.append(f'<rect y="{g-420}" width="{W}" height="380" fill="url(#fog)"/>')
    s.append(block(420, g - 40, 5, 6, rnd, fh=50, bw=38, end="left", ew=110))
    for x in (380, 1350, 1400):
        s.append(spruce(x, g - 30, rnd.uniform(140, 200), "#55605a", rnd, SC))
    s.append(car(180, g - 30, "#7a2f2a") + car(1180, g - 30, "#2f4a5e"))
    s.append(drift(g - 34, rnd, 5))
    s.append(lamp(1250, g + 10))
    # the trees at each side go on their own layers (swayed in the app)
    left = [frosted(60, g + 20, 640, 3), frosted(250, g + 20, 520, 5)]
    right = [frosted(1520, g + 20, 620, 8), frosted(1690, g + 20, 560, 9), frosted(1380, g + 20, 420, 12)]
    left.append(birch(160, g + 30, 560, rnd))
    right.append(birch(1610, g + 30, 600, rnd))
    s.append(swing(820, g + 40) + mushroom(1120, g + 70) + bench(590, g + 70, "#6d9a5a") + bench(690, g + 72, "#d1b450"))
    s.append(drift(g + 70, rnd, 10, 210))
    s.append(arch(410, g + 170, 110))
    s.append(drift(g + 160, rnd, 14, 260))
    for x, y in ((120, g + 165), (1450, g + 175), (980, g + 160)):
        s.append(bush(x, y, rnd) + grass(x + 60, y + 10, rnd))
    s.append('</svg>')
    return "".join(s), svg(left), svg(right)


def svg(body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="xMidYMid slice">{"".join(body)}</svg>')


def to_output(x, y):
    """A scene point in output pixels: the SVG covers OUT + 60 px (the blur's
    margin, see main) and is centred, like preserveAspectRatio slice."""
    box_w, box_h = OUT_W + 60, OUT_H + 60
    k = max(box_w / W, box_h / H)
    return (box_w - W * k) / 2 - 30 + x * k, (box_h - H * k) / 2 - 30 + y * k, k


def add_stars(img, sky, count, moon):
    """Stars pricked into the open sky of `img`, drawn sharp after the blur so
    they read as stars, not falling snow: mostly sub-pixel points in white,
    blue and warm tints, a few brighter ones with a faint cross. They only go
    where `img` still matches `sky` (the same page with nothing but the sky),
    so no star sits on a tree, a block or the fog. `moon` is (x, y, r) in
    output pixels and stays clear too."""
    if not count:
        return img
    ss = 4  # drawn 4x and scaled down, for smooth sub-pixel dots
    layer = Image.new("RGBA", (OUT_W * ss, OUT_H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    sr = random.Random(2)
    top = OUT_H * (0.55 if count > 50 else 0.3)
    n = 0
    while n < count:
        x, y = sr.uniform(0, OUT_W), top * sr.random() ** 1.25  # thinning towards the horizon
        if math.dist((x, y), moon[:2]) < moon[2]:
            continue
        n += 1
        tint = sr.choice(((255, 255, 255), (255, 255, 255), (214, 228, 255), (255, 240, 214)))
        roll = sr.random()
        r, a = (0.9, 255) if roll < 0.05 else (0.65, 235) if roll < 0.2 else (sr.uniform(0.35, 0.5), sr.randint(150, 230))
        X, Y, R = x * ss, y * ss, r * ss
        d.ellipse((X - R, Y - R, X + R, Y + R), fill=(*tint, a))
        if roll < 0.05:
            arm = sr.uniform(2.5, 3.5) * ss
            d.line((X - arm, Y, X + arm, Y), fill=(*tint, 110), width=1)
            d.line((X, Y - arm, X, Y + arm), fill=(*tint, 110), width=1)
    layer = layer.resize((OUT_W, OUT_H), Image.Resampling.LANCZOS)
    open_sky = ImageChops.difference(img.convert("RGB"), sky.convert("RGB")).convert("L")
    open_sky = open_sky.point(lambda p: 255 if p < 4 else 0).filter(ImageFilter.MinFilter(9))
    out = img.convert("RGB")
    out.paste(layer.convert("RGB"), mask=ImageChops.multiply(layer.getchannel("A"), open_sky))
    return out


def shoot(browser, svg, background, css="", extra=""):
    with tempfile.TemporaryDirectory() as tmp:
        page, png = Path(tmp) / "scene.html", Path(tmp) / "scene.png"
        page.write_text(
            f'<!doctype html><html><style>{css}</style><body style="margin:0;width:{OUT_W}px;height:{OUT_H}px;'
            f'overflow:hidden;background:{background}"><div style="position:absolute;inset:-30px;'
            f'filter:blur({BLUR}px) saturate(.85)">{svg}</div>{extra}</body></html>'
        )
        args = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                f"--window-size={OUT_W},{OUT_H}", f"--screenshot={png}", page.as_uri()]
        if background == "transparent":
            args.insert(-1, "--default-background-color=00000000")
        subprocess.run(args, check=True, capture_output=True)
        return Image.open(png).copy()


def graded(img, grade):
    """`img` multiplied by a CSS linear-gradient(top, bottom), alpha kept."""
    top, bottom = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in grade[16:-1].split(", "))
    ramp = Image.new("RGB", (1, OUT_H))
    for y in range(OUT_H):
        ramp.putpixel((0, y), tuple(round(a + (b - a) * y / (OUT_H - 1)) for a, b in zip(top, bottom)))
    out = ImageChops.multiply(img.convert("RGB"), ramp.resize(img.size))
    out.putalpha(img.getchannel("A"))
    return out


def main() -> None:
    browser = next(
        (b for b in sys.argv[1:] + ["brave", "chromium", "google-chrome-stable"] if shutil.which(b)),
        None,
    )
    if browser is None:
        sys.exit("No Chromium-based browser found; pass its command as the first argument.")
    for name, changes in PHASES.items():
        v = {**V, **changes}
        back, left, right = scene(v, random.Random(7))
        back = back.replace(f'width="{W}" height="{H}"', 'width="100%" height="100%"', 1)
        grade = (
            f'<div style="position:absolute;inset:0;background:{v["grade"]};mix-blend-mode:multiply"></div>'
            if v["grade"] else ""
        )
        if v["lights"]:
            # Lights go on top of the shaded scene, each still hidden behind
            # whatever stands in front of it (every other shape drawn black).
            img = shoot(browser, back, v["sky"][1], extra=grade).convert("RGB")
            glow = shoot(browser, back, "#000", "svg *:not(.lit):not(.lit *){fill:#000!important;stroke:#000!important}")
            img = ImageChops.screen(img, glow.convert("RGB").point(lambda p, k=v["lights"]: round(p * k)))
        else:
            img = shoot(browser, back, v["sky"][1], ".lit{display:none}", grade).convert("RGB")
        if v["stars"]:
            sky = shoot(browser, back.split("<!--sky-->")[0] + "</svg>", v["sky"][1], ".lit{display:none}", grade)
            mx, my, k = to_output(W * v["sun"][2], H * v["sun"][3])
            img = add_stars(img, sky, v["stars"], (mx, my, v["sun"][4] * k * 1.6))
        out = OUT_DIR / name
        img.save(out, quality=88, optimize=True, progressive=True)
        print(out)
        stem = "day" if name == "backdrop.jpg" else name.removesuffix(".jpg")
        for side, body in (("left", left), ("right", right)):
            layer = shoot(browser, body.replace(f'width="{W}" height="{H}"', 'width="100%" height="100%"', 1),
                          "transparent").convert("RGBA")
            if v["grade"]:
                layer = graded(layer, v["grade"])
            x0, _y0, x1, _y1 = layer.getbbox()
            # Kept full height and pinned to its edge, so the app can place it
            # from its width alone.
            layer = layer.crop((0, 0, x1, OUT_H) if side == "left" else (x0, 0, OUT_W, OUT_H))
            out = OUT_DIR / f"{stem}_{side}.png"
            layer.quantize(256, method=Image.Quantize.FASTOCTREE).save(out, optimize=True)
            print(out, layer.size)


if __name__ == "__main__":
    main()
