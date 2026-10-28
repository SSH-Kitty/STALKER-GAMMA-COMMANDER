#!/usr/bin/env python3
"""Rebuild the Red Forest theme's scenes in commander_gui/assets/redforest/.

One set per time of day (dawn, day, dusk, night):
- <phase>.jpg: sky, far forest, the forester's tower and the camp at its foot.
- <phase>_left.png / <phase>_right.png: the big trees framing each edge, on
  their own see-through layers so the app can sway them (ui/redforest.py).

The campfire's flames are left out: the app draws them so they can flicker.
Draws the scene as SVG, renders it blurred with a headless Chromium-based
browser and saves it. The blur is baked in: the frosted cards sit over it.

Usage: ./build-redforest-backdrop.py [browser]   (default: brave, then chromium)
Needs Pillow (python3 -m pip install pillow).
"""
import math
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent / "commander_gui" / "assets" / "redforest"
W, H = 1722, 1094  # scene's drawing size; scaled to cover the output
OUT_W, OUT_H = 1920, 1200
BLUR = 2
NEAR_BLUR = 1.2  # the edge trees stand nearer the camera, so they are less out of focus
TOWER_X = 930
FIRE_SCALE = 1.4

PHASES = {
    "day": {
        "sky": ("#efe4d6", "#e2cdb6", "#d6bba0"), "sun": ("#fff6e4", 0.7, 0.66, 0.2, 80),
        "fog": "rgba(238,224,208,%s)", "far_ground": "#d3b29a", "ground": "#b98262",
        "litter": ("#c86a3a", "#d9893e", "#a8482a", "#9a6a4a", "#e0a24a"),
        "far": "#c09a86", "mid": "#8f5f4a", "near": "#5a3a2c",
        "crown_far": ("#dba48c", "#e2b494", "#d0967c"),
        "crown_mid": ("#c9603a", "#d97a3a", "#b5482c", "#e39a3c", "#c24a2c"),
        "crown_near": ("#a82c18", "#c43f20", "#d9622c", "#e39a3c", "#b8341c"),
        "gold": ("#d9a441", "#c98a2e", "#e3b85a", "#b8642a"),
        "wood": "#4e362a", "wood_lit": "#6b4a38", "rock": ("#9a9088", "#b8aea4", "#6e655e"), "glass": "#d8c8b8", "fire_pool": False, "smoke": 0, "stars": 0,
    },
    "dawn": {
        "sky": ("#c4c3d8", "#e9cbc0", "#f4c9a4"), "sun": ("#ffd8a8", 0.9, 0.7, 0.5, 95),
        "fog": "rgba(244,218,206,%s)", "far_ground": "#d4a99a", "ground": "#b47a62",
        "litter": ("#c4643c", "#d68246", "#a64830", "#9a6450", "#e2a058"),
        "far": "#c2988e", "mid": "#8c5c4e", "near": "#5a382e",
        "crown_far": ("#dca096", "#e4b0a0", "#d09084"),
        "crown_mid": ("#c85c40", "#d87844", "#b4482e", "#e29a50", "#c24a32"),
        "crown_near": ("#a82c1c", "#c23e26", "#d65e32", "#e29a48", "#b6341e"),
        "gold": ("#dca04c", "#cc8a36", "#e6b664", "#b86230"),
        "wood": "#4e3430", "wood_lit": "#6a4840", "rock": ("#9c8c88", "#bca8a0", "#6e605c"), "glass": "#f0cdb0", "fire_pool": False, "smoke": .6, "stars": 0,
    },
    "dusk": {
        "sky": ("#4a3c62", "#b06a78", "#f2a464"), "sun": ("#ffae62", 0.9, 0.16, 0.6, 120),
        "fog": "rgba(196,128,118,%s)", "far_ground": "#8a5c62", "ground": "#6e4140",
        "litter": ("#a4442e", "#b85e36", "#843226", "#6e4440", "#c8823e"),
        "far": "#8a6070", "mid": "#5a3a40", "near": "#33222a",
        "crown_far": ("#a87078", "#b47e80", "#9c6670"),
        "crown_mid": ("#a4402e", "#b85a34", "#8c3428", "#c87a3c", "#9a3a2c"),
        "crown_near": ("#7e2a20", "#962f22", "#a8442a", "#b8662e", "#6e2420"),
        "gold": ("#c88a3a", "#b07230", "#d8a050", "#9a5428"),
        "wood": "#2e2026", "wood_lit": "#46303a", "rock": ("#6e5a60", "#86707a", "#4a3a42"), "glass": "#6a4a52", "fire_pool": True, "smoke": .35, "stars": 14,
    },
    "night": {
        "sky": ("#0c1424", "#18233a", "#2a3550"), "sun": ("#f4f6fb", 1.0, 0.6, 0.13, 46),
        "fog": "rgba(78,94,124,%s)", "far_ground": "#3a4560", "ground": "#2a3044",
        "litter": ("#4a3440", "#56404a", "#3a2c38", "#3e4256", "#6a4a46"),
        "far": "#46506a", "mid": "#2e3044", "near": "#181a26",
        "crown_far": ("#58607a", "#626a82", "#505872"),
        "crown_mid": ("#6a3c48", "#7a4650", "#583442", "#84544e", "#62384a"),
        "crown_near": ("#4c2832", "#5a2e36", "#683a3c", "#7a4c44", "#40222c"),
        "gold": ("#7a6650", "#6a5646", "#8a7458", "#5a4440"),
        "wood": "#141620", "wood_lit": "#262838", "rock": ("#3a4050", "#4c5468", "#262a38"), "glass": "#2c3448", "fire_pool": True, "smoke": .35, "stars": 300, "moon": True,
    },
}


def strokes(groups, width):
    """One <path> per colour: thousands of short strokes stay cheap to render."""
    return "".join(f'<path d="{"".join(d)}" stroke="{c}" stroke-width="{width:.2f}" stroke-linecap="round" fill="none"/>'
                   for c, d in groups.items() if d)


def tree(x, base, h, leaves, trunk, rnd, dead=False, sparse=False, lean=0.0):
    """Red Forest tree as in STALKER 2: crooked limbs splitting upward and
    outward, with lacy sprays of small red/orange leaves on the outer twigs.
    Sparse ones are the scraggly saplings."""
    limbs, foliage = [], {c: [] for c in leaves}
    scale = h / 400
    lw = max(0.6, 1.1 * scale)

    def spray(px, py, n):
        r = 34 * scale
        for _ in range(n):
            a = rnd.uniform(0, 2 * math.pi)
            d = r * math.sqrt(rnd.random())
            sx, sy = px + math.cos(a) * d * 1.4, py + math.sin(a) * d * 0.8
            ang = rnd.uniform(-2.6, -0.5) if rnd.random() < 0.6 else rnd.uniform(0, 2 * math.pi)
            ln = rnd.uniform(3, 8) * scale + 1
            foliage[rnd.choice(leaves)].append(f"M{sx:.1f},{sy:.1f}l{math.cos(ang)*ln:.1f},{-math.sin(ang)*ln:.1f}")

    def grow(px, py, ang, ln, w, depth):
        bend = rnd.uniform(-0.1, 0.1) * ln
        x2, y2 = px + math.cos(ang) * ln, py - math.sin(ang) * ln
        cx = (px + x2) / 2 + math.cos(ang + math.pi / 2) * bend
        cy = (py + y2) / 2 - math.sin(ang + math.pi / 2) * bend
        limbs.append(f'<path d="M{px:.1f},{py:.1f} Q{cx:.1f},{cy:.1f} {x2:.1f},{y2:.1f}" stroke="{trunk}" '
                     f'stroke-width="{w:.2f}" stroke-linecap="round" fill="none"/>')
        if not dead and depth <= 2:
            spray(x2, y2, (10 if sparse else 60) if depth == 0 else (5 if sparse else 30))
        if depth == 0:
            return
        kids = 1 if sparse and rnd.random() < 0.5 else rnd.choice((2, 2, 3))
        for _ in range(kids):
            na = ang + rnd.uniform(-0.85, 0.85)
            na += (math.pi / 2 - na) * 0.18  # reach for the light
            grow(x2, y2, na, ln * rnd.uniform(0.62, 0.8), max(0.5, w * 0.62), depth - 1)

    depth = 6 if h > 500 else 5 if h > 200 else 4
    if sparse:
        grow(x, base, math.pi / 2 + rnd.uniform(-0.08, 0.08), h * 0.45, max(1.5, h / 70), depth)
    else:
        grow(x, base, math.pi / 2 + lean + rnd.uniform(-0.12, 0.12), h * 0.42, max(2, h / 30), depth)
    return "".join(limbs) + strokes(foliage, lw)


def bush(x, y, size, v, rnd):
    """Red scrub: a few short stems fanning out from one root, each ending
    in the same lacy leaf sprays as the trees."""
    return "".join(
        tree(x + rnd.uniform(-6, 6) * size / 60, y, size * rnd.uniform(0.7, 1.1), v["crown_mid"], v["mid"], rnd,
             lean=rnd.uniform(-0.75, 0.75))
        for _ in range(rnd.randint(3, 5))
    )


def rock(x, y, size, v, rnd):
    """A weathered boulder half sunk in the leaf litter: lit top, shaded
    flank, a cast shadow and a few leaves caught on it."""
    base, lit, dark = v["rock"]
    n = rnd.randint(7, 10)
    pts = []
    for i in range(n):  # upper half of a lumpy ellipse; the ground hides the rest
        a = math.pi * i / (n - 1)
        r = size * rnd.uniform(0.8, 1.15)
        pts.append((x + math.cos(a) * r * 1.25, y - math.sin(a) * r * 0.95))
    body = " ".join(f"{px:.0f},{py:.0f}" for px, py in pts)
    top = " ".join(f"{px:.0f},{py + (y - py) * 0.45:.0f}" for px, py in pts[1:-1])
    s = [f'<ellipse cx="{x + size * 0.35:.0f}" cy="{y + 2:.0f}" rx="{size * 1.6:.0f}" ry="{size * 0.25:.0f}" fill="#000" opacity=".18" filter="url(#soft)"/>',
         f'<polygon points="{body}" fill="{base}"/>',
         f'<polygon points="{top} {pts[-2][0]:.0f},{y:.0f}" fill="{lit}" opacity=".7"/>',
         f'<polygon points="{x:.0f},{y:.0f} {" ".join(f"{px:.0f},{py:.0f}" for px, py in pts[: n // 3])}" fill="{dark}" opacity=".55"/>']
    for _ in range(rnd.randint(1, 3)):
        lx, ly = x + rnd.uniform(-1, 1) * size, y - rnd.uniform(0.3, 0.7) * size * 0.75
        s.append(f'<ellipse cx="{lx:.0f}" cy="{ly:.0f}" rx="{size * 0.12 + 1.5:.1f}" ry="{size * 0.05 + 1:.1f}" '
                 f'fill="{rnd.choice(v["litter"])}" transform="rotate({rnd.uniform(-30, 30):.0f} {lx:.0f} {ly:.0f})"/>')
    return "".join(s)


def tower(x, g, v):
    """Forester's fire lookout: four timber legs splayed wide, X-braced in
    sections, a zigzag stair inside, a balcony with a railing and a dark
    cabin on top (the only light is the campfire below)."""
    wood, lit = v["wood"], v["wood_lit"]
    top, hb, ht = g - 470, 92, 44   # half-width at base / top
    s = []
    # back legs: thinner and lighter, they're further away
    for bx, tx in ((x - hb + 30, x - ht + 14), (x + hb - 30, x + ht - 14)):
        s.append(f'<line x1="{bx}" y1="{g-14}" x2="{tx}" y2="{top}" stroke="{lit}" stroke-width="5"/>')
    secs = 6
    for i in range(secs):
        t0, t1 = i / secs, (i + 1) / secs
        y0, y1 = g - (g - top) * t0, g - (g - top) * t1
        k0, k1 = hb - (hb - ht) * t0, hb - (hb - ht) * t1
        s.append(f'<line x1="{x-k0:.0f}" y1="{y0:.0f}" x2="{x+k1:.0f}" y2="{y1:.0f}" stroke="{wood}" stroke-width="3"/>')
        s.append(f'<line x1="{x+k0:.0f}" y1="{y0:.0f}" x2="{x-k1:.0f}" y2="{y1:.0f}" stroke="{wood}" stroke-width="3"/>')
        s.append(f'<line x1="{x-k1:.0f}" y1="{y1:.0f}" x2="{x+k1:.0f}" y2="{y1:.0f}" stroke="{wood}" stroke-width="5"/>')
        # stair flight, switching sides each section
        a, b = (x - k0 * 0.55, y0 - 4), (x + k1 * 0.55, y1 + 6)
        if i % 2:
            a, b = (x + k0 * 0.55, y0 - 4), (x - k1 * 0.55, y1 + 6)
        s.append(f'<line x1="{a[0]:.0f}" y1="{a[1]:.0f}" x2="{b[0]:.0f}" y2="{b[1]:.0f}" stroke="{lit}" stroke-width="4"/>')
        for k in range(1, 6):
            sx = a[0] + (b[0] - a[0]) * k / 6
            sy = a[1] + (b[1] - a[1]) * k / 6
            s.append(f'<line x1="{sx-5:.0f}" y1="{sy:.0f}" x2="{sx+5:.0f}" y2="{sy:.0f}" stroke="{lit}" stroke-width="1.5"/>')
    for bx, tx in ((x - hb, x - ht), (x + hb, x + ht)):  # front legs + footings
        s.append(f'<line x1="{bx}" y1="{g}" x2="{tx}" y2="{top}" stroke="{wood}" stroke-width="8"/>')
        s.append(f'<rect x="{bx-10}" y="{g-4}" width="20" height="10" fill="#8a8078" opacity=".8"/>')
    # balcony deck + railing
    s.append(f'<rect x="{x-ht-26}" y="{top-6}" width="{2*ht+52}" height="9" fill="{wood}"/>')
    s.append(f'<line x1="{x-ht-26}" y1="{top-36}" x2="{x+ht+26}" y2="{top-36}" stroke="{wood}" stroke-width="3"/>')
    for k in range(9):
        px = x - ht - 26 + k * (2 * ht + 52) / 8
        s.append(f'<line x1="{px:.0f}" y1="{top-36}" x2="{px:.0f}" y2="{top-6}" stroke="{wood}" stroke-width="2"/>')
    # cabin: a band of windows, pyramid roof with a deep overhang, lightning rod
    cw, ch = 74, 64
    cy = top - 6 - ch
    s.append(f'<rect x="{x-cw/2}" y="{cy}" width="{cw}" height="{ch}" fill="{wood}"/>')
    s.append(f'<rect x="{x-cw/2+6}" y="{cy+10}" width="{cw-12}" height="24" fill="{v["glass"]}" opacity=".6"/>')
    for k in (1, 2):
        s.append(f'<line x1="{x-cw/2+6+k*(cw-12)/3:.0f}" y1="{cy+10}" x2="{x-cw/2+6+k*(cw-12)/3:.0f}" y2="{cy+34}" stroke="{wood}" stroke-width="3"/>')
    s.append(f'<path d="M{x-cw/2-20},{cy+2} L{x},{cy-44} L{x+cw/2+20},{cy+2} Z" fill="{wood}"/>')
    s.append(f'<line x1="{x}" y1="{cy-44}" x2="{x}" y2="{cy-66}" stroke="{wood}" stroke-width="2"/>')
    return "".join(s)


def camp(x, y, v):
    """The camp at the tower's foot, without its flames: a ring of stones,
    crossed logs, a thin smoke trail (none by day: the fire is out) and two
    logs to sit on. At dusk and night the fire lights only a faint flat pool
    on the ground."""
    s = []
    if v["fire_pool"]:
        s.append(f'<ellipse cx="{x}" cy="{y+4}" rx="90" ry="16" fill="#ff9a40" opacity=".22" filter="url(#soft)"/>')
    if v["smoke"]:
        s.append(f'<path d="M{x},{y-30} C{x-14},{y-80} {x+22},{y-120} {x+6},{y-170} S{x+40},{y-240} {x+30},{y-300}" '
                 f'stroke="#625e66" stroke-width="14" stroke-linecap="round" fill="none" opacity="{v["smoke"]}" filter="url(#soft)"/>')
    for lx, a in ((x - 74, -4), (x + 70, 6)):
        s.append(f'<rect x="{lx-26}" y="{y-8}" width="52" height="11" rx="5" fill="{v["wood_lit"]}" transform="rotate({a} {lx} {y})"/>')
    for k in range(9):
        ang = k / 9 * 2 * math.pi
        s.append(f'<ellipse cx="{x+math.cos(ang)*24:.0f}" cy="{y+math.sin(ang)*6:.0f}" rx="6" ry="4" fill="#7c7670"/>')
    s.append(f'<line x1="{x-18}" y1="{y+2}" x2="{x+14}" y2="{y-10}" stroke="{v["wood"]}" stroke-width="5" stroke-linecap="round"/>')
    s.append(f'<line x1="{x+18}" y1="{y+2}" x2="{x-14}" y2="{y-10}" stroke="{v["wood"]}" stroke-width="5" stroke-linecap="round"/>')
    return "".join(s)


def svg(body, defs=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="xMidYMid slice"><defs>{defs}</defs>{body}</svg>')


def scene(v, rnd):
    """(backdrop svg, left trees svg, right trees svg) for one time of day."""
    sky, sun = v["sky"], v["sun"]
    g = H * 0.80
    fire = (TOWER_X + 30, g + 46)
    defs = (f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky[0]}"/><stop offset=".6" stop-color="{sky[1]}"/><stop offset="1" stop-color="{sky[2]}"/></linearGradient>'
            '<filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter>'
            '<filter id="glow" x="-150%" y="-150%" width="400%" height="400%"><feGaussianBlur stdDeviation="22"/></filter>'
            f'<linearGradient id="fog" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{v["fog"] % 0}"/><stop offset=".5" stop-color="{v["fog"] % 0.85}"/><stop offset="1" stop-color="{v["fog"] % 0.95}"/></linearGradient>'
            f'<linearGradient id="haze" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{v["fog"] % 0}"/><stop offset=".55" stop-color="{v["fog"] % 0.75}"/><stop offset="1" stop-color="{v["fog"] % 0}"/></linearGradient>')
    s = [f'<rect width="{W}" height="{H}" fill="url(#sky)"/>']
    # sun / moon: a wide soft halo plus the disc
    s.append(f'<circle cx="{W*sun[2]}" cy="{H*sun[3]}" r="{sun[4]*3.2}" fill="{sun[0]}" opacity="{sun[1]*0.25:.2f}" filter="url(#glow)"/>')
    s.append(f'<circle cx="{W*sun[2]}" cy="{H*sun[3]}" r="{sun[4]}" fill="{sun[0]}" opacity="{sun[1]}" filter="url(#glow)"/>')
    if v.get("moon"):
        s.append(f'<circle cx="{W*sun[2]}" cy="{H*sun[3]}" r="{sun[4]*0.55}" fill="{sun[0]}"/>')
    s.append("<!--sky-->")
    # far forest standing on its own strip of ground
    s.append(f'<path d="M0,{g-118} Q{W*0.35},{g-132} {W*0.7},{g-116} T{W},{g-122} L{W},{g+20} L0,{g+20} Z" fill="{v["far_ground"]}"/>')
    for i in range(80):
        s.append(tree(i * 22 + rnd.uniform(-8, 8), g - 112, rnd.uniform(120, 200), v["crown_far"], v["far"], rnd,
                      sparse=rnd.random() < 0.3))
    s.append(f'<rect y="{g-210}" width="{W}" height="240" fill="url(#fog)" opacity="0.85"/>')
    for _ in range(34):
        x = rnd.uniform(0, W)
        if abs(x - TOWER_X) < 130:
            continue
        s.append(tree(x, g + 10, rnd.uniform(200, 310), v["crown_mid"], v["mid"], rnd,
                      dead=rnd.random() < 0.1, sparse=rnd.random() < 0.15))
    s.append(f'<path d="M0,{g} Q{W*0.3},{g-22} {W*0.55},{g+4} T{W},{g-8} L{W},{H} L0,{H} Z" fill="{v["ground"]}"/>')
    litter = {c: [] for c in v["litter"]}
    for _ in range(4200):  # fallen leaves and dry grass
        x, y = rnd.uniform(0, W), rnd.uniform(g - 4, H)
        k = 0.5 + (y - g) / (H - g) * 1.5
        if rnd.random() < 0.5:   # leaf: short flat dash
            a, ln = rnd.uniform(-0.5, 0.5), rnd.uniform(4, 9) * k
        else:                    # grass blade
            a, ln = math.pi / 2 + rnd.uniform(-0.6, 0.6), rnd.uniform(5, 12) * k
        litter[rnd.choice(v["litter"])].append(f"M{x:.0f},{y:.0f}l{math.cos(a)*ln:.1f},{-math.sin(a)*ln:.1f}")
    s.append(strokes(litter, 1.8))
    # rocks and bushes scattered over the floor, larger nearer the viewer,
    # drawn back to front; the tower's feet and the camp stay clear
    props = []
    for _ in range(26):
        y = rnd.uniform(g + 6, H - 20)
        x = rnd.uniform(-20, W + 20)
        if abs(x - TOWER_X) < 200 and y < g + 110:
            continue
        near = (y - g) / (H - g)
        props.append((y, x, near, rnd.random() < 0.45))
    props += [(g + 52, fire[0] - 150, 0.25, True), (g + 60, fire[0] + 140, 0.3, False)]  # by the camp
    for y, x, near, is_rock in sorted(props):
        size = 1 + near * 2.2
        s.append(rock(x, y, 20 * size, v, rnd) if is_rock else bush(x, y, 34 * size, v, rnd))
    s.append(tower(TOWER_X, g + 6, v))
    s.append(f'<g transform="translate({fire[0]} {fire[1]}) scale({FIRE_SCALE}) translate({-fire[0]} {-fire[1]})">{camp(*fire, v)}</g>')
    s.append(f'<rect y="{g-300}" width="{W}" height="380" fill="url(#haze)" opacity="0.45"/>')
    # the trees framing each edge go on their own layers (swayed in the app)
    sides = ([], [])
    rnd = random.Random(2)  # the edge trees don't change when the floor does; 2 keeps the sky open
    for i, x in enumerate((-40, 90, 210, W - 230, W - 90, W + 30)):
        x += rnd.uniform(-30, 30)
        if rnd.random() < 0.35:
            t = tree(x, H + 20, rnd.uniform(560, 760), v["gold"], v["near"], rnd)
        else:
            t = tree(x, H + 20, rnd.uniform(640, 860), v["crown_near"], v["near"], rnd, dead=rnd.random() < 0.15)
        sides[i >= 3].append(t)
    return svg("".join(s), defs), svg("".join(sides[0])), svg("".join(sides[1])), fire


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


def shoot(browser, html, transparent):
    with tempfile.TemporaryDirectory() as tmp:
        page, png = Path(tmp) / "scene.html", Path(tmp) / "scene.png"
        page.write_text(html)
        args = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                f"--window-size={OUT_W},{OUT_H}", f"--screenshot={png}", page.as_uri()]
        if transparent:
            args.insert(-1, "--default-background-color=00000000")
        subprocess.run(args, check=True, capture_output=True)
        return Image.open(png).copy()


def main() -> None:
    browser = next(
        (b for b in sys.argv[1:] + ["brave", "chromium", "google-chrome-stable"] if shutil.which(b)),
        None,
    )
    if browser is None:
        sys.exit("No Chromium-based browser found; pass its command as the first argument.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, v in PHASES.items():
        back, left, right, fire = scene(v, random.Random(7))

        def page(body, background, blur=BLUR):
            return (f'<!doctype html><html><body style="margin:0;width:{OUT_W}px;height:{OUT_H}px;'
                    f'overflow:hidden;background:{background}"><div style="position:absolute;inset:-30px;'
                    f'filter:blur({blur}px) saturate(.92)">{body}</div></body></html>')

        img = shoot(browser, page(back, v["sky"][1]), False)
        if v["stars"]:
            sky = shoot(browser, page(back.split("<!--sky-->")[0] + "</svg>", v["sky"][1]), False)
            mx, my, k = to_output(W * v["sun"][2], H * v["sun"][3])
            img = add_stars(img, sky, v["stars"], (mx, my, v["sun"][4] * k * 1.3))
        img.convert("RGB").save(OUT_DIR / f"{name}.jpg", quality=88, optimize=True, progressive=True)
        print(OUT_DIR / f"{name}.jpg")
        for side, body in (("left", left), ("right", right)):
            layer = shoot(browser, page(body, "transparent", NEAR_BLUR), True).convert("RGBA")
            x0, _y0, x1, _y1 = layer.getbbox()
            # Kept full height and pinned to its edge, so the app can place it
            # from its width alone.
            layer = layer.crop((0, 0, x1, OUT_H) if side == "left" else (x0, 0, OUT_W, OUT_H))
            out = OUT_DIR / f"{name}_{side}.png"
            layer.quantize(256, method=Image.Quantize.FASTOCTREE).save(out, optimize=True)
            print(out, layer.size)
    fx, fy, k = to_output(*fire)
    print(f"campfire base at {fx / OUT_W:.4f}, {fy / OUT_H:.4f} of the image, flame scale {k * FIRE_SCALE:.3f}")


if __name__ == "__main__":
    main()
