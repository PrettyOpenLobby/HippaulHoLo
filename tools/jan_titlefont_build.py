#!/usr/bin/env python3
"""jan_titlefont_build.py -- the Rankings window's TITLE face for the live
board (services/boardjan.py).

SE's heading was pictures of words ("Janhourou" + "Ranking", white with a
dark edge, in what reads as Arial Rounded MT Bold). Two pictures side by side
never shared a baseline, and none of them says JongHoLow, so the title was
asked for as TEXT, white with the dark border, the same on every tab
(2026-09-12). Arial Rounded cannot be shipped to browsers; Nunito at weight
900 is a free (SIL OFL 1.1) rounded face that sets the same way.

Writes services/boardart/jan/jan-title.woff2 (the page) and jan-title.ttf
(Pillow, for render.png / Discord), printable ASCII only, and the licence
beside them as OFL-Nunito.txt. Build time only; needs fontTools + brotli.

    python tools/jan_titlefont_build.py "Nunito[wght].ttf" OFL.txt

Source: https://github.com/google/fonts/tree/main/ofl/nunito
"""
import os
import shutil
import sys

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, os.pardir, "services", "boardart", "jan")
WGHT = 900


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise SystemExit(__doc__)
    src, lic = argv
    f = TTFont(src)
    if "fvar" in f:
        instancer.instantiateVariableFont(f, {"wght": WGHT}, inplace=True)
    opts = subset.Options()
    opts.layout_features = ["kern", "liga", "calt"]
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=list(range(0x20, 0x7f)))
    sub.subset(f)
    os.makedirs(ART, exist_ok=True)
    ttf = os.path.join(ART, "jan-title.ttf")
    woff2 = os.path.join(ART, "jan-title.woff2")
    f.flavor = None
    f.save(ttf)
    f = TTFont(ttf)
    f.flavor = "woff2"
    f.save(woff2)
    shutil.copyfile(lic, os.path.join(ART, "OFL-Nunito.txt"))
    for p in (ttf, woff2, os.path.join(ART, "OFL-Nunito.txt")):
        print("  %-18s %7d B" % (os.path.basename(p), os.path.getsize(p)))


if __name__ == "__main__":
    main()
