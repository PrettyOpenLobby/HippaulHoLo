#!/usr/bin/env python3
"""Post the server's in-room notice to JongHoLow players, or kick one out.

The 2004 client shows CmNOTICEMSGOPEN's text in its GM notice window for two
minutes (a scrolling ticker when the line is long) and leaves the title on
CmNOTICEJANHOLOWKICK. The post goes into Valkey (janstore.gmnotice_key) and
every Jan client picks it up on its next line, so run this wherever the
server's environment is (POL_VALKEY_URL set), for example inside the authsess
container:

    python tools/jan_gmnotice.py open "Maintenance starts in 10 minutes."
    python tools/jan_gmnotice.py open --member 12 "A message for one player"
    python tools/jan_gmnotice.py close
    python tools/jan_gmnotice.py kick --member 12
    python tools/jan_gmnotice.py show

A 2002 client ignores all three notices (its dispatcher has no arm for them).
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jan_testenv                                              # noqa: E402

jan_testenv.setup(need_core=False)

from janworld import gmnotice                                   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("op", choices=("open", "close", "kick", "show"))
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--member", type=int, default=0,
                    help="one member id (0 = every Jan client; a kick needs one)")
    ap.add_argument("--ttl", type=float, default=None,
                    help="seconds the post stays deliverable (default: %ds for "
                         "a notice, %ds for a kick)"
                         % (gmnotice.OPEN_TTL_S, gmnotice.KICK_TTL_S))
    a = ap.parse_args(argv)
    if a.op == "show":
        print(json.dumps(gmnotice._read(), indent=1, ensure_ascii=False))
        return 0
    try:
        p = gmnotice.post(a.op, a.text, member=a.member, ttl=a.ttl)
    except ValueError as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    print("posted #%d %s%s%s" % (p["seq"], p["op"],
                                 " for member %d" % p["member"] if p["member"] else
                                 " for everyone",
                                 ": %r" % p["text"] if p["text"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
