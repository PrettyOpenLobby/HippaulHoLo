#!/usr/bin/env python3
"""jan_textfont_build.py -- the Rankings page's TEXT face (services/boardjan.py).

The ROM font, even traced smooth, read as grainy at browser sizes (decided
2026-09-12: "if we have to we can just use a regular dark font"). M PLUS 1p is
a free (SIL OFL 1.1) Japanese gothic -- the same family of letter shapes as
the PS2 ROM's -- with fixed-width digits, so the rank and value columns line
up. Medium for the dark row text, Bold for the white edged labels.

Writes services/boardart/jan/jan-text.woff2 and jan-text-bold.woff2 (ASCII
plus the rank column's up/down arrows) and the licence as OFL-MPLUS1p.txt.
Build time only; needs fontTools + brotli. The Discord image keeps the ROM
font: at the screen's native 640x448 the bitmap glyphs are the game's own.

    python tools/jan_textfont_build.py MPLUS1p-Medium.ttf MPLUS1p-Bold.ttf OFL.txt

Source: https://github.com/google/fonts/tree/main/ofl/mplus1p
"""
import os
import shutil
import sys

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, os.pardir, "services", "boardart", "jan")
#: printable ASCII, the rank arrows, and the separators the page prints
UNICODES = list(range(0x20, 0x7f)) + [0x2191, 0x2193, 0x00B7, 0x2013]


def build(src, out):
    f = TTFont(src)
    opts = subset.Options()
    opts.layout_features = ["kern", "liga", "tnum", "palt"]
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=UNICODES)
    sub.subset(f)
    f.flavor = "woff2"
    f.save(out)
    print("  %-22s %7d B" % (os.path.basename(out), os.path.getsize(out)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        raise SystemExit(__doc__)
    medium, bold, lic = argv
    os.makedirs(ART, exist_ok=True)
    build(medium, os.path.join(ART, "jan-text.woff2"))
    build(bold, os.path.join(ART, "jan-text-bold.woff2"))
    shutil.copyfile(lic, os.path.join(ART, "OFL-MPLUS1p.txt"))


if __name__ == "__main__":
    main()
