"""Janhourou's world server (the old janhourou.py), one module per concern.

    deps.py            Imports shared by the package's modules; an optional one is None when it is not installed.
    wirelog.py         The janhourou log channel: timestamps, hexdumps, and one record described field by field.
    opcodes.py         The wire vocabulary: opcode and notice names, result codes, and which ACK answers which request.
    reservelimits.py   A table's entry limits (money, level, titles): who a reservation refuses with result 10.
    opener.py          What the client waits on before any lobby traffic: MjGETLNDV, MjCHECKSAVEDATA, and the 2004 build's EmISEVENT.
    seating.py         Who sits where: the room a member is in, the seats the lobby store holds, and the names drawn.
    notices.py         Server-to-client notices: kicked, time running out, server quitting, and the table member list.
    builds.py          Serving the 2004 client build: which build is asking, and each record reshaped for it.
    pushqueue.py       The lobby push queue: records for a member who is not the one talking, drained on their next line.
    dispatch.py        One inbound line to its answer: the game manager, the reserve and master commands, the rest captured.
    social.py          The table-social layer: the master's seat and commands, kicks, stored rules, table chat.
    galley.py          Spectators (the gallery): who may watch a table, the watch request and leave, the web board's rule.
    server.py          The standalone TCP listener (POL_JAN_PORT, 51272): one thread per connection, one line at a time.
    selftest.py        The offline selftest (loopback, no client) and the command line.

janhourou.py (one directory up) is the entry point and the compatibility
facade over these modules. Generated from the flat janhourou.py by
tools/split/split_jan_pkg.py and its map.
"""
