"""Author a `U/g/MJSUserData` blob for Janhourou.

WHY THIS EXISTS. Janhourou never writes its own save -- `sqMgWriteFileCheck` has
zero callers in `JanHouRou.pex` -- so the 976 bytes are entirely the server's to
compose, exactly as Tetra Master's `U/g/TM0DataFile` is.

WARNING: **THE LAYOUT NO LONGER LIVES HERE.** It moved to `services/jansave.py`, which
owns the magic, the branch byte, the twelve measured offsets from
`lmenu__002b2ab0` and the declared (still empty) stat table -- and which the
SERVER imports, so an authored blob and a served one cannot disagree. This file
is now the command-line front over that module and nothing else.

Two copies of a byte loop is a mistake this repo has already paid for: the same
consolidation is why `tmsave.default_header_writes` exists (`build`,
`apply_defaults` and tetramaster's mint path had drifted into three copies of
the same header write). Keeping the offsets in one place is the point.

    python tools/mk_jan_save.py 8            # -> data/resources/8.U_g_MJSUserData.bin
    python tools/mk_jan_save.py 8 --blank    # put the old zero-branch blob back
    python tools/mk_jan_save.py 8 --record   # ...from the member's janstats record

The member id is the `member.id` of the account the console logs in as
(PS2Tester = 8); `_resource_file` in responders.py scopes save data per member,
so the name must match or the file is simply never read.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESOURCES = os.path.join(os.path.dirname(HERE), "data", "resources")
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

import jansave                                                      # noqa: E402

#: Kept as re-exports so anything that imported them from here still works, and
#: so there is exactly ONE definition of each. `jansave` is the owner.
MAGIC = jansave.MAGIC
SIZE = jansave.SIZE
EXISTING_PLAYER = jansave.EXISTING_PLAYER


def build(existing=True):
    """The factory blob: the magic, the branch byte, and zeros.

    Thin by design -- `jansave.build` is the implementation, and its selftest
    asserts this exact blob byte for byte.
    """
    return jansave.build(existing=existing)[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("member", help="member.id from accounts.db (PS2Tester = 8)")
    ap.add_argument("--blank", action="store_true",
                    help="write the zero-branch blob instead (the control)")
    ap.add_argument("--record", action="store_true",
                    help="author from this member's janstats record -- the "
                         "identity branch follows whether they have finished a "
                         "game, and any stat field with a MEASURED offset is "
                         "filled in (today: none, by design)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the existing file's other bytes (the identity "
                         "u64s and the two name strings) instead of starting "
                         "from zeros")
    ap.add_argument("--dir", default=RESOURCES)
    args = ap.parse_args()

    dest = os.path.join(args.dir, "%s.U_g_MJSUserData.bin" % args.member)
    base = None
    if args.keep:
        try:
            with open(dest, "rb") as f:
                base = f.read()
        except OSError:
            print("  (no existing %s to keep -- starting from zeros)"
                  % os.path.basename(dest))

    if args.record:
        blob, applied, skipped = jansave.build_for_member(args.member, base=base)
    else:
        blob, applied, skipped = jansave.build(base=base, existing=not args.blank)

    os.makedirs(args.dir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(blob)
    print("wrote %d bytes -> %s" % (len(blob), dest))
    print("  +0x000 magic          = %#010x" % MAGIC)
    print("  +0x3C8 existing player= %d  (%s branch)"
          % (blob[EXISTING_PLAYER],
             "identity" if blob[EXISTING_PLAYER] else "blank"))
    for k, (old, new) in sorted(applied.items()):
        print("  %-14s %d -> %d" % (k, old, new))
    if args.record and skipped:
        print("  NOT written -- no measured save offset yet: %s"
              % ", ".join(skipped))


if __name__ == "__main__":
    sys.exit(main() or 0)
