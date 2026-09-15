#!/usr/bin/env python3
"""jan_boardfont_build.py -- Janhourou's ROM font as two WEB FONTS for the
live rankings page (services/boardjan.py).

Every string on the game's Rankings screen is drawn from Warashi/font/kanji.bin
(16x16, 4 coverage levels) by two drawers in hdd.c: a FIXED one (glyph
0x4A5 + byte, 8 px advance) and a PROPORTIONAL one (glyph 0x503 + byte, the
per-byte width table at 0x385520, then the kerning function hdd__002aa6e0).
The page used to get those as pixels inside one scaled PNG, which overlapped
and scaled badly (judged 2026-09-12). This turns the same glyphs into
outline fonts so the page can use REAL text:

  * each ink pixel becomes a square (coverage level >= INK), so the letter
    shapes are the ROM's, and stay sharp at any size;
  * 1 em = the 16 px glyph cell (64 units per pixel), ascent 13 px;
  * advances are SE's: 8 px fixed, the width table proportional, with the
    unconditional -1 px for Y/W/V/P/L folded into the glyph's advance and
    every (previous, current) rule of hdd__002aa6e0 emitted as a contextual
    kern on the CURRENT glyph -- the ROM kerns after drawing `cur`;
  * the proportional font also carries the rank column's up/down arrows
    (Shift-JIS 81AA / 81AB), 16 px like every 2-byte glyph.

Reads board.json from services/boardart/jan/ and the ROM font from YOUR
install (`--kanji <install>/Warashi/font/kanji.bin`); writes
janrom-fixed.woff2 / janrom-prop.woff2 (+ .woff) beside board.json. The
faces are traced from the game's own font, so they are not in the
repository: build them once from your copy. Needs fontTools and brotli
(build time only -- the server serves the files):

    python tools/jan_boardfont_build.py --kanji <install>/Warashi/font/kanji.bin
"""
import argparse
import json
import os
import sys

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, os.pardir, "services", "boardart", "jan")

PX = 64                     # font units per ROM pixel
UPM = 16 * PX               # the 16 px cell is 1 em
ASC, DESC = 13 * PX, 3 * PX
INK = 2                     # ROM coverage 0..3; 2 and 3 are ink, 1 is fringe

#: hdd__002aa6e0 as (previous set, {current set: pixels}); the sets are disjoint
KERN = [("zxusqponmkhecba", {"YV": -2, "WT": -1}),
        ("ZXK", {"QOGC": -1}),
        ("YWVPT", {"zyxwvusrqponmedcaJ": -2, "jMA": -1}),
        ("F", {"MJA": -1}),
        ("OGDC", {"jZXWVTMA": -1}),
        ("LMA", {"YWV": -3, "ywvT": -2, "tjfQOGC": -1})]
MINUS_ONE = "YWVPL"         # `if c in "YWVPL": x -= 1`, whatever came before


def sjis_index(b1, b2):
    a = (b1 * 2) & 0xff
    if b2 < 0x9f:
        hi = ((a + 0x1f) if a < 0x3f else (a + 0x9f)) & 0xff
        lo = ((b2 - 0x1f) if b2 < 0x7f else (b2 - 0x20)) & 0xff
    else:
        hi = ((a + 0x20) if a < 0x3f else (a + 0xa0)) & 0xff
        lo = (b2 - 0x7e) & 0xff
    v = hi * 0x100 + lo - 0x2121
    return (v & 0xff) + ((v & 0xff00) >> 8) * 0x5e


def bitmap(rom, idx):
    """16 rows of 16 coverage levels, LOW nibble first (janfont.py reads it
    the other way round and ghosts every glyph)."""
    o = idx * 128
    rows = []
    for y in range(16):
        row = rom[o + y * 8: o + y * 8 + 8]
        rows.append([(row[x // 2] & 0xF) if x % 2 == 0 else (row[x // 2] >> 4)
                     for x in range(16)])
    return rows


SS = 8          # field samples per ROM pixel
PAD = 2         # empty pixels around the glyph, so every contour closes
ISO = 0.5       # the outline's level on the 0..1 coverage field
EPS = 0.45      # simplification tolerance, in samples (~0.06 px)


def _field(bm):
    """The glyph's 4 coverage levels as a smooth field: bicubic upsampling of
    the 16x16 cell, SS samples per pixel. Squares of ink read as pixels at
    any scale (judged 2026-09-12: "really pixelated"); the iso line of this
    field keeps the ROM's letter shapes with the edges a TV blur gave them."""
    from PIL import Image
    n = 16 + 2 * PAD
    im = Image.new("F", (n, n), 0.0)
    px = im.load()
    for y in range(16):
        for x in range(16):
            px[x + PAD, y + PAD] = bm[y][x] / 3.0
    return im.resize((n * SS, n * SS), Image.BICUBIC).load(), n * SS


def _loops(f, n, t):
    """Marching squares: closed iso-lines of f at level t, in sample units."""
    def inside(x, y):
        return f[x, y] > t

    def point(key):
        k, x, y = key
        a = f[x, y]
        b = f[x + 1, y] if k == "h" else f[x, y + 1]
        u = (t - a) / (b - a) if b != a else 0.5
        return (x + u, y) if k == "h" else (x, y + u)

    nbr = {}

    def link(p, q):
        nbr.setdefault(p, []).append(q)
        nbr.setdefault(q, []).append(p)

    for y in range(n - 1):
        for x in range(n - 1):
            a, b = inside(x, y), inside(x + 1, y)
            c, d = inside(x + 1, y + 1), inside(x, y + 1)
            if a == b == c == d:
                continue
            T, R, B, L = ("h", x, y), ("v", x + 1, y), ("h", x, y + 1), ("v", x, y)
            cut = [e for e, (p, q) in ((T, (a, b)), (R, (b, c)), (B, (d, c)), (L, (a, d)))
                   if p != q]
            if len(cut) == 2:
                link(*cut)
                continue
            # the saddle: the centre decides which corners connect
            centre = (f[x, y] + f[x + 1, y] + f[x + 1, y + 1] + f[x, y + 1]) / 4 > t
            if a:                                   # a and c inside
                pairs = ((T, R), (L, B)) if centre else ((T, L), (R, B))
            else:                                   # b and d inside
                pairs = ((T, L), (R, B)) if centre else ((T, R), (L, B))
            for p, q in pairs:
                link(p, q)
    loops, seen = [], set()
    for start in nbr:
        if start in seen:
            continue
        loop, prev, cur = [], None, start
        while True:
            seen.add(cur)
            loop.append(point(cur))
            nxt = [q for q in nbr[cur] if q != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            if cur == start:
                break
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = (dx * dx + dy * dy) ** 0.5 or 1e-9
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        d = abs(dy * (pts[i][0] - x0) - dx * (pts[i][1] - y0)) / norm
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [pts[0], pts[-1]]
    return _rdp(pts[:idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def _simplify(loop, eps):
    """RDP on a closed loop, split at the point farthest from the first."""
    far = max(range(len(loop)), key=lambda i: (loop[i][0] - loop[0][0]) ** 2
              + (loop[i][1] - loop[0][1]) ** 2)
    a = _rdp(loop[:far + 1], eps)
    b = _rdp(loop[far:] + [loop[0]], eps)
    return a[:-1] + b[:-1]


def _area(p):
    return sum(p[i][0] * p[i - 1][1] - p[i - 1][0] * p[i][1] for i in range(len(p))) / 2.0


def _inside(pt, poly):
    x, y, hit = pt[0], pt[1], False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i - 1], poly[i]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def outline(bm):
    """A TrueType glyph traced round the smoothed field: outer contours
    clockwise, holes counter-clockwise (TrueType's nonzero convention)."""
    f, n = _field(bm)
    polys = []
    for loop in _loops(f, n, ISO):
        pts = []
        for sx, sy in _simplify(loop, EPS):
            p = (int(round(((sx + 0.5) / SS - PAD) * PX)),
                 int(round(ASC - ((sy + 0.5) / SS - PAD) * PX)))
            if not pts or pts[-1] != p:
                pts.append(p)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        if len(pts) >= 3 and abs(_area(pts)) > PX * PX * 0.05:
            polys.append(pts)
    pen = TTGlyphPen(None)
    for i, p in enumerate(polys):
        depth = sum(1 for j, q in enumerate(polys) if j != i and _inside(p[0], q))
        want_cw = depth % 2 == 0
        if (_area(p) < 0) != want_cw:           # y-up: clockwise = negative area
            p = p[::-1]
        pen.moveTo(p[0])
        for q in p[1:]:
            pen.lineTo(q)
        pen.closePath()
    return pen.glyph()


def gname(cp):
    return "space" if cp == 0x20 else "uni%04X" % cp


def build(family, glyphs, kern_fea=None):
    """glyphs: {codepoint: (bitmap or None, advance in px)}"""
    order = [".notdef"] + [gname(cp) for cp in sorted(glyphs)]
    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({cp: gname(cp) for cp in glyphs})
    empty = TTGlyphPen(None).glyph()
    fb.setupGlyf({".notdef": empty,
                  **{gname(cp): (outline(bm) if bm else empty)
                     for cp, (bm, _a) in glyphs.items()}})
    metrics = {".notdef": (8 * PX, 0)}
    for cp, (_bm, adv) in glyphs.items():
        metrics[gname(cp)] = (max(0, int(round(adv * PX))), 0)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASC, descent=-DESC, lineGap=0)
    fb.setupNameTable({"familyName": family, "styleName": "Regular",
                       "uniqueFontIdentifier": family.replace(" ", "") + "-ROM",
                       "fullName": family,
                       "psName": family.replace(" ", "") + "-Regular",
                       "version": "Version 1.0",
                       "copyright": "Glyphs from Janhourou's ROM font (c) SQUARE ENIX; "
                                    "outlined for the rankings page"})
    fb.setupOS2(sTypoAscender=ASC, sTypoDescender=-DESC, sTypoLineGap=0,
                usWinAscent=ASC, usWinDescent=DESC, fsSelection=0x40 | 0x80,
                achVendID="POL ")
    fb.setupPost()
    if kern_fea:
        addOpenTypeFeaturesFromString(fb.font, kern_fea)
    return fb.font


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kanji", default=os.path.join(ART, "kanji.bin"),
                    help="the game's ROM font, Warashi/font/kanji.bin in your install")
    a = ap.parse_args()
    if not os.path.isfile(a.kanji):
        sys.exit("no ROM font at %s -- point --kanji at your install's "
                 "Warashi/font/kanji.bin" % a.kanji)
    b = json.load(open(os.path.join(ART, "board.json"), encoding="utf-8"))
    rom = open(a.kanji, "rb").read()
    widths = b["widths"]
    space = int(b["space_advance"])

    fixed = {}
    for c in range(0x20, 0x7f):
        fixed[c] = (None if c == 0x20 else bitmap(rom, 0x4a5 + c),
                    space if c == 0x20 else 8)
    prop = {}
    for c in range(0x20, 0x7f):
        adv = widths[c] - (1 if chr(c) in MINUS_ONE else 0)
        prop[c] = (None if c == 0x20 else bitmap(rom, 0x503 + c), adv)
    for cp, (b1, b2) in ((0x2191, (0x81, 0xAA)), (0x2193, (0x81, 0xAB))):
        prop[cp] = (bitmap(rom, sjis_index(b1, b2)), 16)

    lines = ["languagesystem DFLT dflt;", "feature kern {"]
    for prev, curs in KERN:
        pc = " ".join(gname(ord(ch)) for ch in prev)
        for cur, px in curs.items():
            cc = " ".join(gname(ord(ch)) for ch in cur)
            lines.append("  pos [%s] [%s]' %d;" % (pc, cc, px * PX))
    lines.append("} kern;")
    fea = "\n".join(lines)

    for family, glyphs, f in (("JanROM Fixed", fixed, None),
                              ("JanROM Prop", prop, fea)):
        stem = "janrom-" + family.split()[1].lower()
        for flavor, ext in (("woff2", "woff2"), ("woff", "woff")):
            font = build(family, glyphs, f)
            font.flavor = flavor
            path = os.path.join(ART, "%s.%s" % (stem, ext))
            font.save(path)
            print("  %-22s %7d B" % (os.path.basename(path), os.path.getsize(path)))
    print("fonts written to %s" % os.path.normpath(ART))


if __name__ == "__main__":
    sys.exit(main())
