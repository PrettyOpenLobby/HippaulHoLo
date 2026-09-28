"""Janhourou's table and game manager (the old jangame.py), one module per concern.

    deps.py            Imports shared by the package's modules; an optional one is None when it is not installed.
    knobs.py           The POL_JAN_* switches the game reads at import, and the process-wide shutdown flag.
    narration.py       The plain-English move trace (TRACE), and the tile, point and opcode names it prints.
    bots.py            Bot: the seat with no client behind it, and how it discards and calls.
    table.py           Table: one table's seats, sequencing, deadlines, seat loss, and per-seat delivery.
    tablegallery.py    Spectators: who watches a table, the copies they are sent, and their one ack.
    tablestate.py      The board as the records carry it: the public view, round and rule scalars, dead wall, ponds.
    tablemsgs.py       The in-game records a table sends: deal, draw, call offer, yaku, settlement, resync, kan.
    tableresults.py    The end of a hanchan: the record written for each player and the two result screens.
    tablesashiuma.py   Sashiuma, the pre-game side bet: the handshake, the bots' side, the payout.
    tableflow.py       The drive loop: the deal, turns, bot moves, calls, kans, wins and the end of a hand.
    tableinbound.py    Inbound in-game records: replay detection, the dispatch, discards, call answers, the escape file.
    manager.py         Manager: every table in the process, routing by member, the sweeper, the watch file.
    selftest.py        The offline selftest (a whole hanchan with no client) and the command line.

jangame.py (one directory up) is the entry point and the compatibility
facade over these modules. Generated from the flat jangame.py by
tools/split/split_jan_pkg.py and its map.
"""
from . import table  # noqa: F401  (a split class's home loads before its parts)
