#!/usr/bin/env python3
"""Rebuild the Winter theme's scenes in commander_gui/assets/winter/.

One per time of day: backdrop.jpg (day), dawn.jpg, dusk.jpg and night.jpg.
Draws the foggy village (abandoned dacha, spruce treeline, birches, power
line) as SVG, renders it blurred with a headless Chromium-based browser and
saves it as a JPEG. The blur is baked in: the frosted cards sit over it.

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

V = {"title": "B - Blizzard (cold grey-blue, heavy snow)",
        "sky": ("#dfe5ea", "#c3ccd4", "#a9b4bd"),
        "far": "#8e9aa4", "mid": "#6f7b85", "near": "#525c64",
        "house": "#41474c", "roof": "#34393d", "snowcap": "#eef2f5", "window": "#22272b", "lit": None,
        "fog": "rgba(225,232,238,%s)", "blur": 7,
        "card": "rgba(238,243,247,0.36)", "card_border": "rgba(255,255,255,0.75)",
        "card_shadow": "0 10px 36px rgba(40,55,70,0.22)",
        "input": "rgba(255,255,255,0.62)", "btn": "rgba(255,255,255,0.45)",
        "text": "#1b252d", "dim": "#56636d", "nav": "#4f5d68",
        "accent": "#4d7c4a", "accent_strong": "#3a6d37", "hero1": "#3a6d37", "hero2": "#62945a",
        "topbar": "rgba(232,238,243,0.62)", "storage": ("#62945a", "#3a6d37", "#9f9a63", "#8f9ba5"),
        "stars": 0,  # added after the grade, so they stay bright
        "flakes": 420, "wind": -30, "house_x": 430, "lights": False,
        # (color, opacity, x, y, radius) of the hazy sun or moon
        "sun": ("#ffffff", 0.55, 0.78, 0.18, 70),
        # Multiplied over the whole scene, so the house and trees darken too.
        "grade": None,
}

#: Output file -> what changes from the day scene.
PHASES = {
    "backdrop.jpg": {},
    "dawn.jpg": {
        "sky": ("#c9cfdc", "#e6cfc8", "#f0d3c0"), "fog": "rgba(240,224,216,%s)",
        "sun": ("#ffd9b3", 0.8, 0.78, 0.48, 80),
        "grade": "linear-gradient(#ebe6ee, #f4e9e2)",
    },
    "dusk.jpg": {
        "sky": ("#3d3c52", "#8a6a72", "#d98f5c"), "fog": "rgba(150,120,122,%s)",
        "sun": ("#ffb070", 0.75, 0.2, 0.5, 90),
        "grade": "linear-gradient(#6a6676, #8c7570)", "stars": 12,
    },
    "night.jpg": {
        "sky": ("#0d131c", "#18212c", "#26303b"), "fog": "rgba(58,68,80,%s)",
        "sun": ("#f4f8fb", 1.0, 0.78, 0.16, 55),
        "grade": "linear-gradient(#363f4a, #45505b)", "stars": 320,
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


def rock(x, y, r, rnd, snow):
    """A rounded boulder half sunk in the snow: lit top, dusting of snow on
    the crown, darker shadow side and a blue shadow cast on the snow."""
    n = rnd.randint(5, 7)
    pts = [(x - math.cos(math.pi * k / n) * r * 1.15 * rnd.uniform(0.9, 1.05),
            y - math.sin(math.pi * k / n) * r * rnd.uniform(0.75, 1.0)) for k in range(n + 1)]

    def smooth(p):
        d = f"M{p[0][0]:.0f},{p[0][1]:.0f}"
        for (ax, ay), (bx, by) in itertools.pairwise(p[1:-1]):
            d += f" Q{ax:.0f},{ay:.0f} {(ax+bx)/2:.0f},{(ay+by)/2:.0f}"
        return d + f" Q{p[-2][0]:.0f},{p[-2][1]:.0f} {p[-1][0]:.0f},{p[-1][1]:.0f}"

    body = smooth(pts) + " Z"
    cid = f"rk{x:.0f}_{y:.0f}"
    crown = min(py for _, py in pts)
    return (f'<clipPath id="{cid}"><path d="{body}"/></clipPath>'
            f'<ellipse cx="{x+r*0.7:.0f}" cy="{y-r*0.05:.0f}" rx="{r*1.5:.0f}" ry="{r*0.25:.0f}" fill="#8f9ca8" opacity=".5" filter="url(#rsoft)"/>'
            f'<path d="{body}" fill="#5a5f63"/>'
            f'<path d="{body}" fill="url(#rockShade)"/>'
            f'<ellipse cx="{x-r*0.2:.0f}" cy="{crown-r*0.15:.0f}" rx="{r*0.95:.0f}" ry="{r*0.42:.0f}" fill="{snow}" opacity=".8" clip-path="url(#{cid})"/>'
            f'<ellipse cx="{x:.0f}" cy="{y+r*0.04:.0f}" rx="{r*1.3:.0f}" ry="{r*0.08+2:.0f}" fill="{snow}"/>')


def grass(x, y, rnd, n=7):
    """Dry stalks poking out of the snow."""
    return "".join(
        f'<path d="M{x+rnd.uniform(-10,10):.0f},{y} q{rnd.uniform(-6,6):.0f},{-rnd.uniform(10,20):.0f} {rnd.uniform(-10,10):.0f},{-rnd.uniform(18,38):.0f}" '
        f'stroke="{rnd.choice(("#8a7a5e", "#76684f", "#9a8b6c"))}" stroke-width="1.2" fill="none"/>'
        for _ in range(n))


def stump(x, y, r, snow):
    return (f'<rect x="{x-r}" y="{y-r*1.7:.0f}" width="{2*r}" height="{r*1.7+6:.0f}" fill="#4a3d31"/>'
            f'<rect x="{x+r*0.3:.0f}" y="{y-r*1.7:.0f}" width="{r*0.7:.0f}" height="{r*1.7+6:.0f}" fill="#000" opacity=".25"/>'
            f'<ellipse cx="{x}" cy="{y-r*1.7:.0f}" rx="{r+2}" ry="{r*0.3:.0f}" fill="{snow}"/>'
            f'<ellipse cx="{x}" cy="{y+4}" rx="{r*1.6:.0f}" ry="{r*0.25+3:.0f}" fill="{snow}"/>')


def ridge(pts):
    """Closed path: a smooth snow line through pts, filled down to the bottom."""
    d = f"M{pts[0][0]},{H+10} L{pts[0][0]},{pts[0][1]:.0f}"
    for (ax, ay), (bx, by) in itertools.pairwise(pts):
        d += f" Q{ax},{ay:.0f} {(ax+bx)/2:.0f},{(ay+by)/2:.0f}"
    return d + f" L{pts[-1][0]},{pts[-1][1]:.0f} L{pts[-1][0]},{H+10} Z"


def snowline(y, amp, rnd, step=160, flat=None):
    """Points for ridge(); `flat` (x0, x1, y) keeps the line low under the house."""
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


def house(v, x, ground):
    """Abandoned dacha seen three-quarter on, after a real Russian summer house:
    dark vertical boards on the gable end, a glazed veranda along the long
    side above grey clapboard, a corrugated asbestos-slate roof with an
    overhang, a stone base, TV antenna, dead vines and bare fruit trees.
    """
    rnd = random.Random(11)
    sc = v["snowcap"]
    s = [('<defs>'
         # Weathered wood: streaky vertical grain multiplied over the paint.
         '<filter id="grainV" x="0" y="0" width="1" height="1">'
         '<feTurbulence type="fractalNoise" baseFrequency="0.9 0.012" numOctaves="3" seed="4"/>'
         '<feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.1 1.05"/>'
         '<feComposite in2="SourceGraphic" operator="in"/>'
         '<feBlend in="SourceGraphic" mode="multiply"/></filter>'
         '<filter id="grainH" x="0" y="0" width="1" height="1">'
         '<feTurbulence type="fractalNoise" baseFrequency="0.012 0.7" numOctaves="3" seed="9"/>'
         '<feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.0 1.0"/>'
         '<feComposite in2="SourceGraphic" operator="in"/>'
         '<feBlend in="SourceGraphic" mode="multiply"/></filter>'
         # Lichen and water stains on the slate.
         '<filter id="stain" x="0" y="0" width="1" height="1">'
         '<feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="4" seed="2"/>'
         '<feColorMatrix values="0 0 0 0 0.25  0 0 0 0 0.24  0 0 0 0 0.2  0 0 0 -2.2 1.25"/>'
         '<feComposite in2="SourceGraphic" operator="in"/></filter>'
         '<filter id="soft"><feGaussianBlur stdDeviation="1.2"/></filter>'
         '<filter id="softer"><feGaussianBlur stdDeviation="5"/></filter>'
         '<linearGradient id="eaveShade" x1="0" y1="0" x2="0" y2="1">'
         '<stop offset="0" stop-color="#000" stop-opacity=".55"/><stop offset=".35" stop-color="#000" stop-opacity=".12"/>'
         '<stop offset="1" stop-color="#000" stop-opacity="0"/></linearGradient>'
         '<linearGradient id="gableShade" x1="0" y1="0" x2="1" y2="0">'
         '<stop offset="0" stop-color="#000" stop-opacity=".28"/><stop offset="1" stop-color="#000" stop-opacity=".05"/></linearGradient>'
         '<linearGradient id="glass" x1="0" y1="0" x2="0.3" y2="1">'
         '<stop offset="0" stop-color="#b9c2c8"/><stop offset=".45" stop-color="#7d878e"/><stop offset="1" stop-color="#4b5359"/></linearGradient>'
         '<linearGradient id="roofLight" x1="0" y1="0" x2="0" y2="1">'
         '<stop offset="0" stop-color="#fff" stop-opacity=".18"/><stop offset="1" stop-color="#000" stop-opacity=".18"/></linearGradient>'
         '</defs>')]

    h = 150            # wall height at the front corner
    gw = 115           # gable end, foreshortened
    lw = 330           # long side, facing the viewer
    L, R = x + gw, x + gw + lw
    top = ground - h
    rise = 14          # the far gable corner sits higher: the end recedes
    peak = (x + L) / 2 + 6, top - 92
    base_h = 16

    # ---- gable end: dark vertical boards, receding to the left ----
    g_far_top, g_far_bot = (x, top - rise + 6), (x, ground - rise)
    gable = f"M{L},{ground} L{g_far_bot[0]},{g_far_bot[1]} L{g_far_top[0]},{g_far_top[1]} L{peak[0]:.0f},{peak[1]} L{L},{top} Z"
    s.append(f'<path d="{gable}" fill="#5a4636" filter="url(#grainV)"/>')
    for i in range(1, 14):
        px = x + i * gw / 14
        t = (px - x) / gw
        y_bot = ground - rise * (1 - t)
        y_top = peak[1] + abs(px - peak[0]) * (92 / (peak[0] - x)) + (6 if px < peak[0] else 0)
        shade = rnd.choice(("#3e3127", "#4a3a2e", "#33281f"))
        s.append(f'<line x1="{px:.1f}" y1="{y_top:.0f}" x2="{px:.1f}" y2="{y_bot:.0f}" stroke="{shade}" stroke-width="1.3"/>')
    s.append(f'<path d="{gable}" fill="url(#gableShade)"/>')
    # attic window: white frame, cracked pane, rag stuffed in a corner
    ax, ay = peak[0] - 12, peak[1] + 44
    s.append(f'<path d="M{ax-4},{ay-3} L{ax+26},{ay-5} L{ax+26},{ay+34} L{ax-4},{ay+36} Z" fill="#b8b2a6"/>')
    s.append(f'<path d="M{ax},{ay} L{ax+22},{ay-2} L{ax+22},{ay+31} L{ax},{ay+32} Z" fill="url(#glass)"/>')
    s.append(f'<line x1="{ax+11}" y1="{ay-1}" x2="{ax+11}" y2="{ay+31}" stroke="#b8b2a6" stroke-width="2"/>')
    s.append(f'<path d="M{ax+12},{ay+2} L{ax+21},{ay+14} L{ax+14},{ay+12} Z" fill="#23272a"/>')
    # stone base under the gable
    s.append(f'<path d="M{L},{ground} L{x},{ground-rise} L{x},{ground-rise+base_h} L{L},{ground+base_h} Z" fill="#8d8d88"/>')

    # ---- long side: grey clapboard below, veranda glazing above ----
    s.append(f'<rect x="{L}" y="{top}" width="{lw}" height="{h}" fill="#7d868c" filter="url(#grainH)"/>')
    for i in range(1, 8):
        yy = ground - i * 11
        s.append(f'<line x1="{L}" y1="{yy}" x2="{R}" y2="{yy+rnd.uniform(-1,1):.1f}" stroke="#5c646a" stroke-width="1.2"/>')
        s.append(f'<line x1="{L}" y1="{yy+1.5}" x2="{R}" y2="{yy+1.5}" stroke="#98a1a7" stroke-width=".6" opacity=".6"/>')
    # paint peeling off the clapboard in patches
    for _ in range(9):
        px, py = rnd.uniform(L + 10, R - 30), rnd.uniform(ground - 70, ground - 10)
        s.append(f'<ellipse cx="{px:.0f}" cy="{py:.0f}" rx="{rnd.uniform(6,18):.0f}" ry="{rnd.uniform(2,5):.0f}" fill="#6b5a49" opacity=".55"/>')
    # veranda: white-painted sash frame with many small panes
    vx0, vx1, vy0, vy1 = L + 10, R - 66, top + 12, top + 78
    s.append(f'<rect x="{vx0-4}" y="{vy0-4}" width="{vx1-vx0+8}" height="{vy1-vy0+10}" fill="#cfcbc1"/>')
    cols, rows = 10, 3
    cw, rh = (vx1 - vx0) / cols, (vy1 - vy0) / rows
    gone = {2, 7, 13, 21, 22, 26}
    for c in range(cols):
        for r in range(rows):
            i = c * rows + r
            px, py = vx0 + c * cw + 1.5, vy0 + r * rh + 1.5
            if i in gone:
                s.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{cw-3:.1f}" height="{rh-3:.1f}" fill="#1c1f21"/>')
                # shard left in the frame
                s.append(f'<path d="M{px:.1f},{py+rh-3:.1f} L{px+cw*0.5:.1f},{py+rh-3:.1f} L{px:.1f},{py+rh*0.4:.1f} Z" fill="url(#glass)"/>')
            else:
                s.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{cw-3:.1f}" height="{rh-3:.1f}" fill="url(#glass)"/>')
    # a grey curtain hanging behind the glass, sagging off its rail
    s.append(f'<path d="M{vx0+cw*4},{vy0+2} Q{vx0+cw*5},{vy0+18} {vx0+cw*6.5},{vy0+4} L{vx0+cw*6.4},{vy1-2} L{vx0+cw*4.1},{vy1-2} Z" fill="#9c9a90" opacity=".55"/>')
    # sill board, with snow on it
    s.append(f'<rect x="{vx0-8}" y="{vy1+5}" width="{vx1-vx0+16}" height="5" fill="#a9a499"/>')
    s.append(f'<path d="M{vx0-8},{vy1+5} Q{(vx0+vx1)/2},{vy1-1} {vx1+8},{vy1+5} Z" fill="{sc}"/>')
    # boarded door, hanging off one hinge, with rotten steps
    dx = R - 50
    s.append(f'<g transform="rotate(3 {dx} {top+16})"><rect x="{dx}" y="{top+16}" width="34" height="{h-16}" fill="#6e767b" filter="url(#grainV)"/>'
             f'<rect x="{dx+3}" y="{top+22}" width="28" height="40" fill="#2a2e31"/></g>')
    s.append(f'<line x1="{dx-6}" y1="{top+52}" x2="{dx+40}" y2="{top+70}" stroke="#7a6a58" stroke-width="5"/>')
    s.append(f'<path d="M{dx-10},{ground} L{dx+44},{ground} L{dx+40},{ground-7} L{dx-6},{ground-9} Z" fill="#4a3d31"/>')
    # stone base under the long side
    s.append(f'<rect x="{L}" y="{ground}" width="{lw}" height="{base_h}" fill="#94948e"/>')
    for i in range(int(lw / 22)):
        s.append(f'<line x1="{L+i*22+rnd.uniform(-3,3):.0f}" y1="{ground}" x2="{L+i*22:.0f}" y2="{ground+base_h}" stroke="#7a7a74" stroke-width="1"/>')
    # corner post where the two walls meet
    s.append(f'<rect x="{L-3}" y="{top}" width="6" height="{h}" fill="#4d3d30"/>')
    # shadow under the eaves
    s.append(f'<rect x="{x}" y="{top}" width="{gw+lw}" height="46" fill="url(#eaveShade)" filter="url(#soft)"/>')

    # ---- roof: corrugated slate, overhanging, ridge sagging in the middle ----
    over = 22
    eL, eR = (L - 14, top + 4), (R + over, top + 6)
    rL = (peak[0] - 4, peak[1] - 2)
    rR = (rL[0] + (eR[0] - eL[0]) - 8, rL[1] + 4)
    sag = 9
    roof = (f"M{rL[0]:.0f},{rL[1]:.0f} Q{(rL[0]+rR[0])/2:.0f},{rL[1]+sag*2:.0f} {rR[0]:.0f},{rR[1]:.0f} "
            f"L{eR[0]:.0f},{eR[1]:.0f} Q{(eL[0]+eR[0])/2:.0f},{eL[1]+sag:.0f} {eL[0]:.0f},{eL[1]:.0f} Z")
    s.append(f'<path d="{roof}" fill="#8f918c"/>')
    # corrugation: thin light/dark bands running down the slope
    n = 64
    for i in range(n + 1):
        t = i / n
        tx = rL[0] + t * (rR[0] - rL[0])
        ty = rL[1] + t * (rR[1] - rL[1]) + sag * 4 * t * (1 - t)
        bx = eL[0] + t * (eR[0] - eL[0])
        by = eL[1] + t * (eR[1] - eL[1]) + sag * 2 * t * (1 - t)
        color = "#6f716d" if i % 2 else "#a3a59f"
        s.append(f'<line x1="{tx:.1f}" y1="{ty:.1f}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{color}" stroke-width="1.6"/>')
    s.append(f'<path d="{roof}" fill="#8f918c" filter="url(#stain)" opacity=".9"/>')
    s.append(f'<path d="{roof}" fill="url(#roofLight)"/>')
    # sheets slipped off: dark gaps showing the battens underneath
    for t0, t1, d0, d1 in ((0.38, 0.47, 0.25, 0.62), (0.70, 0.75, 0.45, 0.9)):
        def at(t, d):
            tx = rL[0] + t * (rR[0] - rL[0]); ty = rL[1] + t * (rR[1] - rL[1]) + sag * 4 * t * (1 - t)
            bx = eL[0] + t * (eR[0] - eL[0]); by = eL[1] + t * (eR[1] - eL[1]) + sag * 2 * t * (1 - t)
            return tx + (bx - tx) * d, ty + (by - ty) * d
        a, b, c, d = at(t0, d0), at(t1, d0), at(t1, d1), at(t0, d1)
        s.append(f'<path d="M{a[0]:.0f},{a[1]:.0f} L{b[0]:.0f},{b[1]:.0f} L{c[0]:.0f},{c[1]:.0f} L{d[0]:.0f},{d[1]:.0f} Z" fill="#2b2622"/>')
        for k in range(1, 4):
            p0, p1 = at(t0, d0 + (d1 - d0) * k / 4), at(t1, d0 + (d1 - d0) * k / 4)
            s.append(f'<line x1="{p0[0]:.0f}" y1="{p0[1]:.0f}" x2="{p1[0]:.0f}" y2="{p1[1]:.0f}" stroke="#5a4a3a" stroke-width="2"/>')
    # barge board up the gable edge, fascia along the eave
    s.append(f'<path d="M{eL[0]-2:.0f},{eL[1]+2:.0f} L{rL[0]:.0f},{rL[1]-2:.0f}" stroke="#4a3b2f" stroke-width="5"/>')
    s.append(f'<path d="M{eL[0]:.0f},{eL[1]+3:.0f} Q{(eL[0]+eR[0])/2:.0f},{eL[1]+sag+3:.0f} {eR[0]:.0f},{eR[1]+3:.0f}" stroke="#5f5e58" stroke-width="4" fill="none"/>')
    # snow: thick on the upper slope, thinning and lumpy lower down, a
    # cornice curling over the eave, nothing where the sheets are gone
    snow = [f"M{rL[0]-6:.0f},{rL[1]-5:.0f} Q{(rL[0]+rR[0])/2:.0f},{rL[1]+sag*2-6:.0f} {rR[0]+6:.0f},{rR[1]-4:.0f}"]
    pts = []
    for i in range(9):
        t = i / 8
        tx = rL[0] + t * (rR[0] - rL[0]); ty = rL[1] + t * (rR[1] - rL[1]) + sag * 4 * t * (1 - t)
        bx = eL[0] + t * (eR[0] - eL[0]); by = eL[1] + t * (eR[1] - eL[1]) + sag * 2 * t * (1 - t)
        d = rnd.uniform(0.36, 0.46) if not (0.36 < t < 0.49) else 0.2
        pts.append((tx + (bx - tx) * d, ty + (by - ty) * d))
    pts.reverse()
    snow.append(f"L{pts[0][0]:.0f},{pts[0][1]:.0f}")
    for (ax_, ay_), (bx_, by_) in itertools.pairwise(pts):
        snow.append(f"Q{ax_:.0f},{ay_:.0f} {(ax_+bx_)/2:.0f},{(ay_+by_)/2:.0f}")
    snow.append(f"L{pts[-1][0]:.0f},{pts[-1][1]:.0f}")
    snow.append("Z")
    s.append(f'<path d="{" ".join(snow)}" fill="{sc}"/>')
    s.append(f'<path d="{" ".join(snow)}" fill="#c9d2da" opacity=".35" transform="translate(0 3)" filter="url(#soft)"/>')
    # icicles under the fascia
    for _ in range(14):
        ix = rnd.uniform(eL[0] + 10, eR[0] - 10)
        t = (ix - eL[0]) / (eR[0] - eL[0])
        iy = eL[1] + (eR[1] - eL[1]) * t + sag * 2 * t * (1 - t) + 4
        s.append(f'<path d="M{ix-1.5:.1f},{iy:.1f} L{ix+1.5:.1f},{iy:.1f} L{ix:.1f},{iy+rnd.uniform(5,16):.1f} Z" fill="#e3eaef"/>')
    # brick chimney, top courses fallen, and a bent TV antenna
    cx = rL[0] + (rR[0] - rL[0]) * 0.62
    cy = rL[1] + (rR[1] - rL[1]) * 0.62 + sag * 4 * 0.62 * 0.38
    s.append(f'<path d="M{cx},{cy+8} L{cx},{cy-30} L{cx+9},{cy-34} L{cx+20},{cy-28} L{cx+20},{cy+8} Z" fill="#7b5145"/>')
    for k in range(1, 6):
        s.append(f'<line x1="{cx}" y1="{cy+8-k*7}" x2="{cx+20}" y2="{cy+8-k*7}" stroke="#5e3d34" stroke-width=".8"/>')
    s.append(f'<path d="M{cx-2},{cy-30} Q{cx+10},{cy-38} {cx+22},{cy-28} L{cx+20},{cy-25} L{cx},{cy-27} Z" fill="{sc}"/>')
    tx = rL[0] + 30
    s.append(f'<line x1="{tx}" y1="{rL[1]+4}" x2="{tx+3}" y2="{rL[1]-60}" stroke="#55585a" stroke-width="2"/>')
    for k, w in ((0, 34), (9, 26), (18, 18)):
        s.append(f'<line x1="{tx+3-w/2}" y1="{rL[1]-52+k+w/10:.0f}" x2="{tx+3+w/2}" y2="{rL[1]-52+k-w/10:.0f}" stroke="#55585a" stroke-width="1.5"/>')
    s.append(f'<path d="M{tx+3},{rL[1]-40} Q{tx-40},{rL[1]-10} {x+6},{top-rise+40}" stroke="#55585a" stroke-width="1" fill="none"/>')

    # ---- dead growth: vines up the walls, bushes, bare apple trees ----
    for vx in (x + 16, x + 70, L + 40, L + 170, L + 250):
        vy = ground
        for k in range(rnd.randint(4, 7)):
            nx, ny = vx + rnd.uniform(-14, 14), vy - rnd.uniform(14, 24)
            s.append(f'<path d="M{vx:.0f},{vy:.0f} Q{vx+rnd.uniform(-10,10):.0f},{(vy+ny)/2:.0f} {nx:.0f},{ny:.0f}" stroke="#3b342d" stroke-width="1.6" fill="none" opacity=".85"/>')
            if rnd.random() < 0.6:
                s.append(twigs(nx, ny, rnd.uniform(0.6, 2.5), rnd.uniform(6, 12), 1, rnd, "#3b342d", 0.8))
            vx, vy = nx, ny
    for bx in (x - 30, L + 120, R + 40):
        for _ in range(7):
            s.append(twigs(bx + rnd.uniform(-20, 20), ground + 8, rnd.uniform(1.2, 1.95), rnd.uniform(22, 38), 3, rnd, "#463d34", 1.6))
    for ax, size in ((R + 120, 1.0), (x - 120, 0.8)):
        s.append(twigs(ax, ground + 10, 1.62, 80 * size, 5, rnd, "#4a4038", 7 * size))
    # snow piled against the walls, uneven, with a soft blue shadow side
    drift = f"M{x-70},{ground+12} "
    for i in range(1, 6):
        px = x - 70 + i * (gw + lw + 140) / 5
        drift += f"Q{px - (gw + lw + 140) / 10:.0f},{ground - rnd.uniform(14, 26):.0f} {px:.0f},{ground + (22 if i == 5 else -rnd.uniform(0, 8)):.0f} "
    drift += f"L{R+70},{ground+30} L{x-70},{ground+30} Z"
    s.append(f'<path d="{drift}" fill="#cfd8e0" transform="translate(0 4)" filter="url(#soft)"/>')
    s.append(f'<path d="{drift}" fill="{sc}"/>')
    # broken picket fence half-buried in the snow
    for i in range(14):
        fx = x - 260 + i * 17
        if i in (3, 4, 9):
            continue
        tilt = rnd.uniform(-9, 9)
        s.append(f'<path d="M{fx},{ground+12} L{fx+tilt:.0f},{ground-34} l3,-5 l3,5 L{fx+6},{ground+12} Z" fill="#4a3d32"/>')
    s.append(f'<line x1="{x-262}" y1="{ground-18}" x2="{x-40}" y2="{ground-12}" stroke="#3f342b" stroke-width="3"/>')
    return "".join(s)


def scene(v, rnd):
    sky, sc = v["sky"], v["snowcap"]
    g = H * 0.80
    hx = v["house_x"]
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice">',
         '<defs>',
         f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky[0]}"/><stop offset=".6" stop-color="{sky[1]}"/><stop offset="1" stop-color="{sky[2]}"/></linearGradient>',
         '<filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="18"/></filter>',
         '<filter id="rsoft"><feGaussianBlur stdDeviation="3"/></filter>',
         '<linearGradient id="rockShade" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/>'
         '<stop offset=".55" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".35"/></linearGradient>',
         f'<linearGradient id="fog" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{v["fog"] % 0}"/><stop offset=".5" stop-color="{v["fog"] % 0.85}"/><stop offset="1" stop-color="{v["fog"] % 0.95}"/></linearGradient>',
         f'<linearGradient id="nearfog" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{v["fog"] % 0}"/><stop offset=".35" stop-color="{v["fog"] % 0.9}"/><stop offset=".6" stop-color="{v["fog"] % 0.5}"/><stop offset="1" stop-color="{v["fog"] % 0}"/></linearGradient>',
         '</defs>',
         f'<rect width="{W}" height="{H}" fill="url(#sky)"/>',
         f'<circle cx="{W*v["sun"][2]}" cy="{H*v["sun"][3]}" r="{v["sun"][4]}" fill="{v["sun"][0]}" opacity="{v["sun"][1]}" filter="url(#glow)"/>',
         '<!--sky-->']
    # far tree line, standing in a distant snowfield
    for i in range(78):
        x = i * 23 + rnd.uniform(-8, 8)
        s.append(spruce(x, g - 122 + rnd.uniform(-6, 6), rnd.uniform(60, 135), v["far"], rnd, sc))
    s.append(f'<path d="{ridge(snowline(g - 124, 4, rnd, 220))}" fill="{sc}"/>')
    s.append(f'<rect y="{g-170}" width="{W}" height="240" fill="url(#fog)" opacity="0.9"/>')
    # power line poles (leaning) with sagging wires; feet hidden by the mid snow
    poles = [(120, 0), (620, 4), (1180, -6), (1680, 3)]
    for px, tilt in poles:
        s.append(f'<line x1="{px}" y1="{g-15}" x2="{px+tilt}" y2="{g-300}" stroke="{v["mid"]}" stroke-width="7"/>')
        s.append(f'<line x1="{px+tilt-30}" y1="{g-285}" x2="{px+tilt+30}" y2="{g-288}" stroke="{v["mid"]}" stroke-width="5"/>')
    for (a, ta), (b, tb) in itertools.pairwise(poles):
        for off in (-26, 26):
            mx = (a + b) / 2
            s.append(f'<path d="M{a+ta+off},{g-285} Q{mx},{g-215} {b+tb+off},{g-285}" stroke="{v["mid"]}" stroke-width="1.6" fill="none"/>')
    # mid trees, on a low snow bank that buries their trunks
    for i in range(30):
        x = rnd.uniform(0, W)
        if abs(x - hx - 210) < 330:
            continue
        s.append(spruce(x, g - 32 + rnd.uniform(-6, 6), rnd.uniform(130, 240), v["mid"], rnd, sc))
    mid = snowline(g - 36, 6, rnd, 140)
    s.append(f'<path d="{ridge(mid)}" fill="#d3dbe2" transform="translate(0 -4)" filter="url(#rsoft)"/>')
    s.append(f'<path d="{ridge(mid)}" fill="{sc}"/>')
    for x, r in ((1250, 9), (1580, 12), (1020, 7)):
        s.append(rock(x, g - 30, r, rnd, sc))
    s.append(house(v, hx, g - 20))
    # birches near
    for i in range(9):
        x = rnd.choice([rnd.uniform(0, 260), rnd.uniform(W - 300, W)])
        s.append(birch(int(x), g + 20 + rnd.uniform(0, 20), rnd.uniform(380, 620), rnd))
    # ground snow, kept low under the house so its drift meets it cleanly
    near = snowline(g + 2, 10, rnd, 170, (hx - 120, hx + 560, g + 4))
    s.append(f'<path d="{ridge(near)}" fill="#d0d9e1" transform="translate(0 -5)" filter="url(#rsoft)"/>')
    s.append(f'<path d="{ridge(near)}" fill="{sc}"/>')
    door = hx + 115 + 330 - 33
    s.append(f'<path d="M{door-14},{g-2} C{door-30},{g+60} {door+120},{g+110} {door+60},{H+10} L{door+250},{H+10} '
             f'C{door+250},{g+110} {door+10},{g+60} {door+14},{g-2} Z" fill="#cdd6de" opacity=".55" filter="url(#rsoft)"/>')
    for x, y, r in ((hx - 90, g + 6, 13), (hx + 560, g + 10, 17), (hx + 590, g + 12, 9)):
        s.append(rock(x, y, r, rnd, sc))
    s.append(f'<rect y="{g-260}" width="{W}" height="{H-g+270}" fill="url(#nearfog)" opacity="0.55"/>')
    # foreground: a closer drift with boulders, a stump and dry grass
    fore = snowline(g + 125, 12, rnd, 260)
    s.append(f'<path d="{ridge(fore)}" fill="#d6dee5" transform="translate(0 -6)" filter="url(#rsoft)"/>')
    s.append(f'<path d="{ridge(fore)}" fill="{sc}"/>')
    for x, y, r in ((170, g + 150, 42), (250, g + 162, 22), (1420, g + 148, 56), (1505, g + 168, 26), (1560, g + 175, 14), (40, g + 120, 30), (620, g + 140, 18), (1760, g + 130, 34)):
        s.append(rock(x, y, r, rnd, sc))
    s.append(stump(1180, g + 72, 20, sc))
    for x, y in ((120, g + 160), (300, g + 168), (1360, g + 160), (1600, g + 178), (1160, g + 76), (900, g + 40),
                 (hx - 60, g + 14), (680, g + 150), (1000, g + 150)):
        s.append(grass(x, y, rnd))
    s.append('</svg>')
    return "".join(s)


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
    so no star sits on a tree, the house or the fog. `moon` is (x, y, r) in
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


def main() -> None:
    browser = next(
        (b for b in sys.argv[1:] + ["brave", "chromium", "google-chrome-stable"] if shutil.which(b)),
        None,
    )
    if browser is None:
        sys.exit("No Chromium-based browser found; pass its command as the first argument.")
    for name, changes in PHASES.items():
        v = {**V, **changes}
        svg = scene(v, random.Random(7)).replace(f'width="{W}" height="{H}"', 'width="100%" height="100%"', 1)
        grade = (
            f'<div style="position:absolute;inset:-30px;background:{v["grade"]};mix-blend-mode:multiply"></div>'
            if v["grade"] else ""
        )

        def shoot(body):
            with tempfile.TemporaryDirectory() as tmp:
                page, png = Path(tmp) / "scene.html", Path(tmp) / "scene.png"
                page.write_text(
                    f'<!doctype html><html><body style="margin:0;width:{OUT_W}px;height:{OUT_H}px;'
                    f'overflow:hidden;background:{v["sky"][1]}"><div style="position:absolute;inset:-30px;'
                    f'filter:blur({BLUR}px) saturate(.85)">{body}</div>{grade}</body></html>'
                )
                subprocess.run(
                    [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                     f"--window-size={OUT_W},{OUT_H}", f"--screenshot={png}", page.as_uri()],
                    check=True, capture_output=True,
                )
                return Image.open(png).convert("RGB")

        img = shoot(svg)
        if v["stars"]:
            sky = shoot(svg.split("<!--sky-->")[0] + "</svg>")
            mx, my, k = to_output(W * v["sun"][2], H * v["sun"][3])
            img = add_stars(img, sky, v["stars"], (mx, my, v["sun"][4] * k * 1.6))
        out = OUT_DIR / name
        img.save(out, quality=88, optimize=True, progressive=True)
        print(out)


if __name__ == "__main__":
    main()
