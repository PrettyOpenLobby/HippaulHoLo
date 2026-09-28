"""Imports shared by the package's modules; an optional one is None when it is not installed."""
import argparse
import os
import socket
import socketserver
import struct
import sys
import threading
import janwire                                                  # noqa: E402
try:
    # THE GAME MANAGER. Optional in the same way responders.py treats this
    # module: absent = the old behaviour, which is that everything in the play
    # sequence is captured and not answered. jangame owns the seats, the hand
    # in progress and the sequence discipline; janmsgs owns the bodies;
    # janmahjong owns the rules. See jangame.py's banner for why a pure
    # request/response drive is enough.
    import jangame                                              # noqa: E402
    import janmsgs                                              # noqa: E402
except ImportError:                                             # pragma: no cover
    jangame = None
    janmsgs = None
try:
    # THE 2004 BUILD'S IN-GAME LAYOUTS (20040727_2). janmsgs builds every
    # record in the 2002 shape; three of them are read differently by the 2004
    # client, so a record bound for one goes through here first. Optional like
    # the rest: absent, every client is served the 2002 shape.
    import janmsgs2004                                          # noqa: E402
except ImportError:                                             # pragma: no cover
    janmsgs2004 = None
try:
    # ONLY for turning a member id into the name a person recognises -- see
    # `display_name`. Optional exactly as in responders.py: absent, a seat draws
    # blank rather than drawing something invented.
    import accounts                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    accounts = None
try:
    # THE SEAT STORE (2026-09-02). Reserve/cancel used to be answered with ONE
    # constant (result 2 for everything), which made every reserver a master and
    # broke cancel outright -- the cancel wait-state accepts only 5/6/0xb/-1
    # (TableMenuPopup__No_Response_0034fec0) and logged our 2 as "illegal code".
    # janseats keys the real answers off the table id BOTH requests carry at
    # +0x08, and feeds the PTL builder's seat counts across the container split.
    # Optional like the rest: absent (or POL_JAN_SEATS=0), the old constants
    # answer exactly as before.
    import janseats                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janseats = None
try:
    # THE RULE STORE (2026-09-04, audit finding 2). MjTBLCONFALL's 112-byte
    # body -- the 33 values the master picked on the rules dialog -- used to be
    # acked and dropped, so every hanchan played `janmahjong.Rules()` defaults
    # while the dialog showed something else. janrules parses it (layout read
    # off ruleset__00347da0 + malloc__002c5050) and keeps it per table for the
    # blob author and the game manager. Optional like the rest.
    import janrules                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janrules = None
try:
    # THE ROOM (audit finding 30). Every table id the client sends is the
    # 16-bit wire id of a row that exists in EVERY room; the room is the one
    # Jan channel the asker is JOINed to, which `janlobby.member_room` reads
    # out of the IRC registry snapshot `LIVE_ROOMS` hands us (responders
    # installs `_live_rooms`). Absent, every table is room 0's -- the shared
    # set, exactly as before.
    import janlobby                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janlobby = None
try:
    import janevent                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janevent = None
