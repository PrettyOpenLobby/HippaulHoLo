#!/usr/bin/env python3
"""Build pml/main/ma_i/masc03i.png -- the ONE main-menu shortcut icon SE will
not give back.

WHY THIS EXISTS
---------------
`pml/main/index.pml` draws the shortcut button as one PNG per content id:

    <img name="imShortcutBt" pos="0,0" size="51,50"
         src="'ma_i/masc'+$zero+''+$SC_ID+'i.png'">

SE still serves ids 1, 2 and 4 and those are installed as its own bytes (kept
beside them as `*.se-orig`) -- do not regenerate those, ever.  Id 3 is
Janhourou, which SE retired: `masc03i.png` 404s live on a session that fetched
its neighbours over the same connection, and the current live `in01.pml` has
dropped the `$SC_ID==3` arm entirely.  The PS2 `urlcache-1815` copy and our
panel-era copy both still declare it, so it shipped -- but no corpus we hold
has the bytes (name-searched all of E:\\ps2hdd).  It has to be authored.

HOW IT IS BUILT (all inputs are SE's own pixels)
------------------------------------------------
1. PLATE, from `masc01i.png`.  SE's construction is a grey frame (curved left
   edge, top rail, bottom rails) around a per-title tinted field, with the art
   overhanging the top rail.  The crystal is cut out and the field closed by
   horizontal interpolation, which works because the field is a smooth
   left-to-right gradient.  The top band the crystal occluded is restored by
   replicating column x=40 -- a clean column -- leftwards, because that band is
   horizontally constant.
2. SILHOUETTE, from 2-of-3 agreement across masc01/02/04.  Where two of the
   three icons agree on a transparent pixel, that pixel is outside the plate in
   all of them.  Art regions never agree, so they fall out automatically.
   Do NOT try to recover the FIELD this way: SE tints it per title (icy blue /
   cream / white-grey), so the three never agree there and a median just mushes
   the three artworks together.
3. TINT.  Hue-rotated to jade for a mahjong cloth.  Saturation is deliberately
   held down (SAT below): SE's tints are soft, and a full-strength green reads
   as neon next to masc01's icy blue.
4. ART, from SE's own Janhourou tile art in `pml/game/jan/jhai/`: `pavl01s.png`
   is the blank tile face and the `ptnl*.png` are the markings, meant to be
   layered on it.  `ptnl33s` is the red dragon (chun), the most legible tile at
   this size and the only one whose red matches SE's accent.  Tilted like the
   Tetra Master cards in masc02, over a soft drop shadow.

Rotation/scale go through a 4x supersample, so re-running is deterministic but
NOT byte-stable across Pillow versions.  Check the result in, don't regenerate
it in a build step.

    python tools/make_jan_icon.py [--out DIR] [--preview FILE]
"""

import argparse
import colorsys
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
#: The portal tree the core serves (OpenLobby's POL_WWW): the icon's inputs
#: are the neighbouring icons and the tile art already installed there, and
#: the output goes beside them. `--www` or POL_WWW names it.
WWW = os.path.join(os.environ.get("POL_WWW") or os.path.join(HERE, os.pardir, "www"),
                   "wh000.pol.com", "pml")
MA_I = os.path.join(WWW, "main", "ma_i")
JHAI = os.path.join(WWW, "game", "jan", "jhai")

DONOR = "masc01i.png"          # plate donor: its art is the narrowest
SIBLINGS = ["masc01i.png", "masc02i.png", "masc04i.png"]

CRYSTAL = (15, 34)             # x span of the donor's art, inclusive
CLEAN_COL = 40                 # a donor column with no art over it
TOP_BAND = (13, 19)            # rows whose content the art occluded
PLATE_TOP = 13                 # first row of the plate itself

HUE_SHIFT = -58.0              # icy blue -> jade
SAT = 0.72                     # SE's tints are soft; 1.0+ reads as neon

TILE_FACE = "pavl01s.png"
TILE_MARK = "ptnl33s.png"      # chun, the red dragon
TILE_W = 24
TILE_ROT = -14
TILE_POS = (12, 6)
SHADOW = ((14, 9), 0.6, 1.7)   # offset, opacity, blur radius
SS = 4


def outside_mask(dirpath):
    """Pixels two of the three SE icons agree are transparent."""
    ims = []
    for n in SIBLINGS:
        im = Image.open(os.path.join(dirpath, n)).convert("RGBA")
        pad = Image.new("RGBA", (51, 50), (0, 0, 0, 0))
        pad.paste(im, (0, 0))          # masc02 is 49 wide; left-aligned wins
        ims.append(np.array(pad).astype(float))
    out = np.zeros((50, 51), bool)
    for i in range(len(ims)):
        for j in range(i + 1, len(ims)):
            agree = np.abs(ims[i] - ims[j]).sum(axis=2) < 24
            out |= agree & (ims[i][:, :, 3] < 40)
    out[:PLATE_TOP, :] = True          # everything above the plate is overhang
    return out


def bare_plate(dirpath):
    donor = np.array(Image.open(os.path.join(dirpath, DONOR)).convert("RGBA")).astype(float)
    out = donor.copy()
    x0, x1 = CRYSTAL
    for y in range(PLATE_TOP, 47):
        left, right = out[y, x0 - 1].copy(), out[y, x1 + 1].copy()
        if left[3] < 40 and right[3] < 40:
            continue
        if left[3] < 40:
            left = right.copy()
        if right[3] < 40:
            right = left.copy()
        for x in range(x0, x1 + 1):
            t = (x - (x0 - 1)) / float(x1 + 2 - x0)
            out[y, x] = left * (1 - t) + right * t
    off = outside_mask(dirpath)
    for y in range(*TOP_BAND):
        src = donor[y, CLEAN_COL].copy()
        if src[3] < 40:
            continue
        for x in range(10, 45):
            if not off[y, x]:
                out[y, x] = src
    out[off] = 0
    return Image.fromarray(out.astype("uint8"))


def retint(im, hue=HUE_SHIFT, sat=SAT):
    """Rotate the donor plate's hue. Keep `sat` well under 1.0 -- SE's tints are
    soft, and full strength reads as neon beside masc01's icy blue."""
    a = np.array(im).astype(float) / 255.0
    out = a.copy()
    for y in range(a.shape[0]):
        for x in range(a.shape[1]):
            if a[y, x, 3] <= 0.02:
                continue
            h, l, s = colorsys.rgb_to_hls(*a[y, x, :3])
            out[y, x, :3] = colorsys.hls_to_rgb((h + hue / 360.0) % 1.0, l,
                                                min(1.0, s * sat))
    return Image.fromarray((out * 255).astype("uint8"))


def fit_art(src, width, rot):
    """Scale+rotate an artwork through a 4x supersample."""
    im = src.resize((src.width * SS, src.height * SS), Image.LANCZOS)
    if rot:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
    h = int(round(width * im.height / float(im.width)))
    return im.resize((width, h), Image.LANCZOS)


def compose(base, art, pos, shadow=SHADOW):
    """Drop `art` onto `base` over a soft shadow, SE's own arrangement."""
    off, opacity, blur = shadow
    sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sh.paste((20, 26, 20, 255), off, art.split()[3].point(lambda v: int(v * opacity)))
    base.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))
    base.alpha_composite(art, pos)
    return base


def tile():
    face = Image.open(os.path.join(JHAI, TILE_FACE)).convert("RGBA").copy()
    face.alpha_composite(Image.open(os.path.join(JHAI, TILE_MARK)).convert("RGBA"))
    return fit_art(face, TILE_W, TILE_ROT)


def build(dirpath):
    return compose(retint(bare_plate(dirpath)), tile(), TILE_POS)


def main():
    global WWW, MA_I, JHAI
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--www", help="the portal tree (POL_WWW); default %s"
                    % os.path.dirname(os.path.dirname(WWW)))
    ap.add_argument("--out", help="where to write masc03i.png (default: the tree's main/ma_i)")
    ap.add_argument("--preview", help="also write an 8x nearest-neighbour blow-up here")
    a = ap.parse_args()
    if a.www:
        WWW = os.path.join(a.www, "wh000.pol.com", "pml")
        MA_I = os.path.join(WWW, "main", "ma_i")
        JHAI = os.path.join(WWW, "game", "jan", "jhai")
    if not a.out:
        a.out = MA_I

    for n in SIBLINGS + [TILE_FACE]:
        d = MA_I if n.startswith("masc") else JHAI
        if not os.path.exists(os.path.join(d, n)):
            sys.exit("missing input %s -- SE art must be installed first" % n)

    im = build(MA_I)
    dst = os.path.join(a.out, "masc03i.png")
    im.save(dst)
    print("wrote %s  (%dx%d)" % (dst, im.width, im.height))
    if a.preview:
        bg = Image.new("RGBA", im.size, (38, 38, 42, 255))
        bg.alpha_composite(im)
        bg.resize((im.width * 8, im.height * 8), Image.NEAREST).save(a.preview)
        print("wrote %s" % a.preview)


if __name__ == "__main__":
    main()
