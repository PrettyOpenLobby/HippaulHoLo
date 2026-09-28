"""Imports shared by the package's modules; an optional one is None when it is not installed."""
import argparse
import os
import random
import struct
import sys
import threading
import time
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
try:
    # THE RULES THE MASTER CHOSE (audit finding 2): `janrules.rules_for(tid)`
    # is what `Manager.table_for_lobby` passes as `rules=`. Optional like
    # janstats: absent = `mj.Rules()` defaults, exactly the old behaviour.
    import janrules                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janrules = None
try:
    # THE SEAT STORE, for one seam only: `janseats.rejoin_pending(member,
    # clear=True)` -- a member re-entering a playing table is re-seated and
    # resynced on their next in-game line (finding 29).
    import janseats                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janseats = None
try:
    # THE PLAYER RECORD. Optional in the same way janhourou.py treats this
    # module: absent = the old behaviour, which is that a finished hanchan is
    # computed and then dropped on the floor. The client cannot write its own
    # save (`sqMgWriteFileCheck` has 0 callers in JanHouRou.pex), so if we do
    # not write the result down here, nothing anywhere does.
    import janstats                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janstats = None


import collections  # noqa: E402  (local: keeps the import block above untouched)
