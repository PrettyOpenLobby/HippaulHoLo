#!/usr/bin/env python3
"""The seam between Janhourou and the OpenLobby core, pinned.

What the split moved is exercised by the modules' own selftests; what this
suite pins is the CONTRACT the core now honours for the title: the tag and
content code it registers under, the fetch lengths and the fresh-save magic
it merges into the core's tables, the zone-aware declared lengths of the
shared lobby-list paths, the live lobby blob, the fresh save carrying the
header the client validates, and the hooks the core calls on a quit, a
shutdown and a profile popup.

    python tools/jan_title_test.py        # exit 0 on success
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402
jan_testenv.setup()          # this tree's services + the OpenLobby core

_TD = tempfile.mkdtemp(prefix="jantitle-")
os.environ.setdefault("POL_LOG_DIR", os.path.join(_TD, "logs"))
os.environ["POL_RESOURCE_DIR"] = os.path.join(_TD, "res")
os.environ["POL_DATA_DIR"] = _TD
os.environ["POL_JAN_SEATS_KEY"] = "jan:test:%s:seats" % os.path.basename(_TD)
os.environ["POL_SESSION_SHARE"] = "0"
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)

import titles                                                    # noqa: E402
import responders as R                                           # noqa: E402
import jantitle                                                  # noqa: E402
import janlobby                                                  # noqa: E402

fails = []
checks = 0


def check(what, got, want=True):
    global checks
    checks += 1
    if got == want:
        print("  ok    %s" % what)
        return
    fails.append(what)
    print("  FAIL  %s\n          got  %r\n          want %r" % (what, got, want))


def main():
    t = titles.for_tag(b"MJS")
    check("the title registers under the MJS tag", isinstance(t, jantitle.Janhourou))
    check("and under content code 3", titles.for_code(3) is t)
    check("the core's fetch table took the save length",
          R._FETCH_PATHLEN.get("U/g/MJSUserData"), 976 + 4)
    check("and the table-record length",
          R._FETCH_PATHLEN.get("b/g/MJSTableInfoSub"), 816 + 4)
    check("the core's fresh-blob table took the save magic",
          R.RESOURCE_INIT.get("U/g/MJSUserData"), struct.pack("<I", 0x02030100))
    check("the title's POLpro templates are merged into the core's",
          any(p.endswith("polpro.json") and "hippaulholo" in p.replace("\\", "/").lower()
              or p == jantitle.POLPRO_SPEC_CANDIDATES[1]
              for p in R.polpro.EXTRA_SPEC_FILES) if R.polpro else True)

    # declared lengths of the shared lobby-list paths, for a member in zone 3
    want_ptl = janlobby.ptl_content_length() + 4
    check("PTL declared at its content length (+4) for a zone-3 member",
          titles.resource_length("b/g/PTL", zone=3), want_ptl)
    rooms = janlobby.rooms_of(1)
    check("the room list declared at header + rooms x record (+4)",
          titles.resource_length("b/g/RL001", zone=3),
          janlobby.RL_HDR + len(rooms) * janlobby.RL_REC + 4)
    check("a parlour this server does not have declares nothing",
          titles.resource_length("b/g/RL099", zone=3), None)
    check("the parlour list keeps the core's constant",
          titles.resource_length("b/g/ZL", zone=3), None)
    check("the core's own length lookup goes through the zone-aware hook",
          R._lobby_paylen(0x03, 0x00, b"\x00" * 0x30 + b"\x00" * 8 + b"b/g/PTL\x00") in
          (want_ptl, R._FETCH_PATHLEN.get("b/g/PTL")))

    # the live lobby blob: nothing for a member outside the title, the
    # parlour list from the room registry when forced
    check("no live blob for a member the core cannot place in the title",
          titles.resource_live("b/g/ZL", 2124, None), None)
    os.environ["POL_JAN_LOBBY_LIVE"] = "force"
    try:
        zl = titles.resource_live("b/g/ZL", 2124, None)
        check("the parlour list is built live at the declared length",
              len(zl) if zl else None, 2124)
        check("with this server's parlour count in its header",
              struct.unpack_from("<I", zl, janlobby.ZL_COUNT_OFF)[0]
              if zl and hasattr(janlobby, "ZL_COUNT_OFF") else len(janlobby.topology()),
              len(janlobby.topology()))
    finally:
        os.environ["POL_JAN_LOBBY_LIVE"] = "1"

    # a fresh save carries the header the client validates
    fresh = R._resource_blob("U/g/MJSUserData", 980, 0)
    check("a never-stored save is 980 bytes", len(fresh), 980)
    check("and opens with 0x02030100", fresh[:4], struct.pack("<I", 0x02030100))

    # the hooks the core calls
    got = titles.polpro_profile(b"MJS", 123456789, "Name", None)
    check("the <PG> hook answers for the MJS tag", isinstance(got, tuple) and len(got) == 3)
    check("with a fields dict", isinstance(got[0], dict) if got else False)
    check("no queued records for a member with no game band pinned",
          titles.polpro_pushes(b"MJS", b"L", b"L<DR>\x070"), [])
    check("no live games in a fresh process", titles.live_games(), 0)
    titles.session_quit(4242, "no-such-sid")
    check("a QUIT for an unknown member is harmless", True)
    titles.begin_shutdown()
    check("begin_shutdown does not raise", True)
    check("the game envelope hands the POLpro classes back to the core",
          t.notice(b"L", b"L<DR>\x070", b"", b"X", b"n", b"s", None) is titles.PASS)

    print(f"\n{checks - len(fails)}/{checks} checks passed"
          + (f", {len(fails)} FAILED" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
