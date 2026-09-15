#!/usr/bin/env python3
"""Janhourou's table seats -- who reserved which table, who is its master.

WHY THIS EXISTS (2026-09-02): the client's Start Game item is gated ENTIRELY on
the PTL table record's +0x14 state byte (TableMenu__Reserve_0034e120: enabled
iff state == 4), and the live PTL builder could only emit 0/2/5 -- so the item
was greyed for every table, for ever, since the live lobby replaced the authored
fixtures. Fixing the state byte alone is not enough to make a two-player table
work: the server also answered EVERY MjPLAYREQ with result 2 (ReserveMaster) and
EVERY MjPLAYCANCEL with the same 2, which the cancel wait-state treats as
"illegal code" (TableMenuPopup__No_Response_0034fec0 accepts only 5, 6, 0xb or
-1). So this module keeps the one piece of state both mistakes share: the seats.

WHAT THE CLIENT TOLD US (all read from the jan-c-full decompile, not guessed):

  * MjPLAYREQ (2) and MjPLAYCANCEL (3) both carry the TABLE ID at +0x08 -- the
    reserve builder `objstrings__002ca990` is called with `*(u64*)(ctx+0xa0)`,
    and ctx+0xa0 is the client's verbatim copy of the 0x68-byte PTL table
    record, whose +0x00 is the id WE authored. The cancel builder `002caab0`
    passes the same id (or `_DAT_00446510`, the my-table global set from it).
  * The MjRESERVEACK result ladder is wrsCpTableReserveCheck's (janhourou.py
    RESERVE_RESULTS). The reserve wait-state (TableMenuPopup__0034f3a0) seats
    on 1 (Reserve) and 2 (ReserveMaster) -- 2 additionally sets the master flag
    DAT_00445e31 and picks the 8-item master popup. The cancel wait-states
    (0034fec0 / 003502f0) complete on 5 (ReserveCancel), clear flags on 6
    (ReserveCancelError), retry on 0xb, and log "illegal code" on anything else
    -- which is what our blanket result=2 was.
  * rec[0x19] of the ACK is unpacked as TWO NIBBLES: DAT_004460e9 = extra >> 4,
    DAT_004460e8 = extra & 0xf. DAT_004460e8 is the seat the client stamps on
    every in-game message (jan-world-protocol: save +0x3ca), and lmenu computes
    the master flag as (save+0x3ca == save+0x3c9) -- so the low nibble is MY
    SEAT and the high nibble is read here as the MASTER'S seat. WARNING: The high
    nibble's meaning is INFERENCE from that save-file comparison; the low
    nibble's is measured. Every reply the old code sent was extra=0, which is
    exactly what this module still produces for a master at seat 0, so the
    proven-live single-player case is byte-identical.

WHERE THE STATE LIVES: the same split as tmroom.py, in miniature. Reservations
arrive on the AUTH band (`authsess` container); `b/g/PTL` is served on the
lobby band (`login` container). The writer keeps memory and publishes a JSON
file under POL_DATA_DIR; readers re-read it when its mtime moves. `_adopt`
seeds memory from the file before the first write so a container restart does
not wipe live reservations (tmroom learned that one the hard way -- see its
`_adopt` banner).

EXPIRY: a PS2 client that crashes or is powered off sends nothing more, so a
seat left alone would wedge its table for ever. Every Jan line on the auth band
touches the member's seats (handle_line calls `touch`); a seat silent past
POL_JAN_SEAT_TTL_S (default 900 s, the title-zone lease window) expires, and an
in-play mark silent past POL_JAN_INPLAY_TTL_S (default 600 s) falls back to
joinable. Expiry is computed at READ time from the timestamps, so the reader
container needs no write access to enforce it.

    POL_JAN_SEATS=0           kill switch: reserve/cancel fall back to the old
                              constant-result behaviour, nothing is stored
    POL_JAN_SEATS_FILE        default $POL_DATA_DIR/jan-seats.json
    POL_JAN_SEAT_TTL_S        default 1800; 0 = never expire
    POL_JAN_INPLAY_TTL_S      default 600; 0 = never expire
    POL_JAN_SEAT_WARN_S       default 120: MjNOTICETIMEUPWARNING goes out this
                              many seconds before a silent seat expires

THE LEASE IS RENEWED BY ANY LINE FROM THE MEMBER (2026-09-04, audit finding
24). Until then only GAME-band lines touched it, and a player sitting at a table
waiting for friends sends none of those -- their heartbeat is the class-L
`<DR>` poll every ~2 s. Fifteen minutes later they were silently unseated and
the next reserver became master while the original's client still held the
master menu. `responders._jan_roster_delta_reply` and the MJS polpro dispatch
now call `touch()` too, so a player waiting an hour keeps the seat; the TTL
only ever bites a client that has really stopped talking (crash, power-off),
and QUIT of the game-band session releases immediately.

MASTERSHIP MOVES WHEN THE MASTER GOES (finding 24/8). A cancel, kick, quit or
expiry that takes the master out promotes the lowest remaining seat, and
`take_master_changes()` hands the (table, new master) pairs to `janhourou`,
which pushes MjNOTICEMEMBER with +0x54 = the new master's seat -- the ONE
writer of the client's "You are the new Table Master" dialog
(`mahdisp.c:53-57`: `+0x54 != last master seat && +0x54 == my seat`).
`decline_master()` is MjCHMASTER: the promoted guest said No, so it rotates on.
"""
import json
import os
import threading
import time

CAPACITY = 4

#: MjRESERVEACK result codes this module answers with. Names from the client's
#: own ladder (janhourou.RESERVE_RESULTS); which codes each wait-state accepts
#: is in the module banner.
RESERVE, RESERVE_MASTER = 1, 2
RESERVE_CANCEL, RESERVE_CANCEL_ERROR = 5, 6
#: WARNING: A FULL TABLE IS 8, NOT 10 (2026-09-04, audit finding 33). The reserve
#: wait-state's texts (TableMenuPopup__0034f3a0, strings resolved from the
#: translated pex): 10 = "Entry limits not met. Cannot reserve" -- the LIMIT_*
#: gate, which a full table is not; 8 (ReserveTimeOut) = "Entry limits were
#: added, or the table status changed, so you cannot reserve a seat at this
#: table" -- the seat-is-gone case. There is NO "table full" string in the
#: client; 8 is the one that describes what happened.
RESERVE_FULL = 8
RESERVE_LIMIT = 10      # kept for callers that mean the ENTRY-LIMIT refusal

#: THE GALLERY (spectators, decoded 2026-09-04). The
#: MjRESERVEACK results the MjGALLEYREQ wait (TableMenuPopup__00350aa0,
#: TableMenuPopup.c:1083-1156) turns into an action: 1 enters the table
#: screen as a spectator (the +0x19 byte becomes the gallery SLOT the client
#: stamps on every MjGALLEYACK +0x14 -- never a seat), 3 "Already
#: spectating", 8 "the table status changed", 9 "password wrong", 10 "Entry
#: limits not met". MjGALLEYLEAVEREQ's wait (mahdisp.c:1116) accepts ONLY
#: 5 or 6 and has no timeout -- an unanswered leave hangs the client.
GALLEY_OK, GALLEY_DUP = 1, 3
GALLEY_NOT_PLAYING, GALLEY_PASSWORD, GALLEY_LIMITED = 8, 9, 10
GALLEY_LEFT, GALLEY_LEFT_ERROR = 5, 6

_LOCK = threading.RLock()
_TABLES = {}            # table id (str) -> {"master": int, "in_play": float,
                        #                    "seats": {member (str): seat dict},
                        #                    "gallery": {member (str): dict}}
_MEMBERS = {}           # member (str) -> {"face": int, "voice": int, ...}
_MASTER_CHANGES = []    # [(table id, new master member)] since the last take
_OWNER = [False]
_ADOPTED = [False]
_CACHE = {"mtime": -1.0, "data": {}}
_LAST_PUBLISH = [0.0]

_FILE_DEFAULT = os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                             "jan-seats.json")

# --- THE DELTA STREAM (2026-09-03) ------------------------------------------
#
# `b/g/PTL` is a SNAPSHOT the client fetches once on room entry, but the client
# also polls `<DR>(serial)` about every 2 s while LobbyState == 15
# (`cp__002fb218`) asking what has changed since the blob it holds. Answer it
# with `<DD>` and `cp__002fae98` walks the pairs while `cp__002fb290` applies
# them -- its command-0x15 arm parses a `TD` group with `lb__002f9e10` and
# copies the resulting 104 bytes over the row `lb__002f9f18` matched BY NAME.
# All of that code is in Janhourou too: `cp.c`/`lb.c` are two of the five sqMg
# translation units statically linked into BOTH game modules, so this is
# Tetra Master's mechanism reused, not a new protocol. `TD` is polpro tag index
# 21 == 0x15, which is what ties the tag to that arm.
#
# THE SEQUENCE LIVES HERE because this module is the one that knows when a row
# changes, and it already crosses the container split (auth band writes, lobby
# band reads) through its own JSON file. `janlobby.ptl_blob` stamps the blob's
# `+0x40` with `sequence()`, so a client that fetches is CURRENT by
# construction and its next poll is the silent one.
#
# WARNING: THE CLIENT APPLIES STRICTLY IN ORDER -- a gap is not a delay, it is a
# permanent stall -- so `deltas_after` checks contiguity rather than filtering.
# Straight from tmroom, along with its two hard-won limits: a client further
# behind than RELOAD_BEHIND is told to reload instead, and a batch bigger than
# the chunk limit is a reload too (a prefix has NEVER been observed to advance
# a client; see responders._roster_delta_reply's banner).
DELTA_WINDOW = 64
RELOAD_BEHIND = 0x32

#: The tables a row delta can be issued for. Kept here rather than imported
#: from `janlobby` because janlobby imports THIS module -- the selftest
#: cross-checks the two so they cannot drift.
DEFAULT_TABLE_IDS = (1, 2, 3, 4)

#: ROOM-QUALIFIED TABLE IDS (audit finding 30, completed 2026-09-04). Every
#: table id THIS STORE KEYS BY is the composite `room_table_id(room, tid)` =
#: `room << 16 | tid`, so Room 1-1 table 1 (0x65_0001) and Room 2-3 table 1
#: (0xCB_0001) are two tables with two seat sets, two masters and two rows.
#: `split_table_id` unpacks one; `room_of_table` is the room half.
#:
#: KEY: THE WIRE ID STAYS THE 16-BIT BASE (1..4). The client echoes the PTL
#: row's +0x00 in MjPLAYREQ/CANCEL/GAMESTART `id8`, and its
#: `b/g/MJSTableInfoSub` subject carries the table in its TOP 16 BITS
#: (tablememberwindow.c:167 builds the key from the record's +0x00; measured
#: subject 0x0002_003d7c0054ab at table 2) -- a composite cannot survive a
#: 16-bit slot, so it never goes on the wire. The ROOM comes from the IRC
#: registry instead: the client holds exactly ONE room channel
#: (`sqMgCpEnterRoom` stores it in the single slot DAT_003f1884, and
#: roommain.c:146-176 waits on "IRC Room Part Complite" before it can pick
#: another room), so the asker's current `#MJS0R0zi` IS their room.
#: `resolve_table()` below is the one rule every arm uses to turn (member,
#: wire id) into a composite; `janhourou.member_room` supplies the room.
#:
#: ROOM 0 = "unknown room". `room_table_id(0, tid) == tid`, so the bare ids a
#: v1 store held ARE room 0's composites: `_adopt` migrates such a file with
#: no key rewriting, its seats stay playable and expire normally, and a
#: `b/g/PTL` we cannot place in any room (first visit, key unlearned) serves
#: room 0's four tables under room 0's sequence.
#:
#: THE DELTA SEQUENCE IS PER ROOM. A `TD` delta is applied to the row
#: `lb__002f9f18` matches BY NAME, and every room's rows carry the same four
#: names (`#MJS0T001`..4), so a delta for Room 2-3 arriving in Room 1-1 would
#: overwrite the wrong row. `_SEQ`/`_DELTAS` are therefore dicts keyed by
#: room, `ptl_blob` stamps the ROOM's sequence, and `<DR>` is answered
#: against the asker's room alone. File format v2: `seq` and `deltas` are
#: `{room: ...}`; a v1 `seq` int / `deltas` list migrate to room 0.
ROOM_SHIFT = 16


def room_table_id(room, tid):
    """The composite id for `tid` in `room` (0/None room = the bare id)."""
    tid = int(tid or 0)
    room = int(room or 0)
    if not room:
        return tid
    return (room << ROOM_SHIFT) | (tid & ((1 << ROOM_SHIFT) - 1))


def split_table_id(tid):
    """(room, base table id) for a possibly room-qualified id."""
    tid = int(tid or 0)
    return tid >> ROOM_SHIFT, tid & ((1 << ROOM_SHIFT) - 1)


def room_of_table(tid):
    """The room half of a composite id (0 = the unknown room)."""
    return split_table_id(tid)[0]


def table_label(tid):
    """`table 1 (room 101)` / `table 1` for the log -- a composite printed as
    one integer reads as garbage."""
    room, base = split_table_id(tid)
    return "table %d (room %d)" % (base, room) if room else "table %d" % base


# --- THE TABLE CHANNEL NAME (2026-09-04) -------------------------------------
#
# The in-game IRC channel a client JOINs at game start is NOT derived by the
# client: it is the PTL row's +0x18 (13 bytes), copied at reserve time into
# the client's table context (TableMenuPopup ctx+0xb8 -> 0x4467e8, both on a
# player's MjRESERVEACK result 1/2 and on a spectator's result 1,
# TableMenuPopup.c:1083-1156) and handed to `sqMgCpEnterTable(table_id,
# name)` (lobby.c:52-80). It is also the name `lb__002f9f18` matches a `TD`
# delta on. Both of those are per ROOM already (a room's PTL and its delta
# stream come from the same `table_values`), so the name may carry the room
# -- which is what stops Room 1-1's table 1 and Room 2-3's table 1 sharing
# ONE chat channel (`f61599cf` left `#MJS0T00n` shared across rooms).
#
# 12 characters + NUL fit the 13-byte field: `#MJS0T` + room(3) + base(3).
# Room 0 (the unknown room / a v1 store) keeps the bare `#MJS0T00n`, so the
# proven-live wire is untouched there. POL_JAN_TABLE_CHAN_SHARED=1 rolls
# every room back to the shared names (the rows a client already holds are
# refetched on the next room entry, so the switch is safe either way).
TABLE_CHAN_SHARED = os.environ.get("POL_JAN_TABLE_CHAN_SHARED", "0") == "1"
TABLE_CHAN_MAX = 12         # PTL_T_NAME_MAX (13) minus the NUL


def table_channel(tid):
    """`#MJS0T00n` for room 0, `#MJS0T<room:3><n:3>` otherwise (12 chars).
    A room too wide for three decimal digits is written in hex so the name
    never overruns the field and never collides with another room's."""
    room, base = split_table_id(tid)
    if not room or TABLE_CHAN_SHARED:
        return "#MJS0T%03d" % base
    name = "#MJS0T%03d%03d" % (room, base)
    if len(name) > TABLE_CHAN_MAX:
        name = "#MJS0T%03X%03d" % (room & 0xFFF, base)
    return name[:TABLE_CHAN_MAX]


# --- THE GALLERY (spectators, 2026-09-04) ------------------------------------
#
# A spectator is NEVER a seat: it lives in the table entry's "gallery" map,
# `{member: {"polid", "name", "slot", "at"}}`, which `seats_at`,
# `seat_count`, `_row_state` (the PTL row), `master_seat_of` and
# `janhourou.lobby_notice` (MjNOTICEMEMBER) never read. The client shows no
# gallery anywhere (not in any member list) and its only
# per-spectator byte is the SLOT it echoes in MjGALLEYACK +0x14. Membership
# is dropped by MjGALLEYLEAVEREQ (`gallery_leave`), a GAMEEND (`clear_
# gallery` -- the client sends NO leave on MjGAMEEND), the
# spectator's exit ack / table reap (`gallery_drop`), the last seat leaving
# (`_release`) and the seat TTL (a spectator's heartbeat is `touch`).

def _fresh_gallery(entry, now=None):
    """The unexpired gallery entries (same TTL as a seat)."""
    ttl = _seat_ttl()
    now = time.time() if now is None else now
    out = {}
    for mem, g in (entry.get("gallery") or {}).items():
        if ttl and now - float(g.get("at", 0)) > ttl:
            continue
        out[mem] = g
    return out


def gallery_join(table_id, member_id, name="", polid=0):
    """Put `member_id` in `table_id`'s gallery. Returns (result, slot):
    (GALLEY_OK, slot) or (GALLEY_DUP, slot) when it is ALREADY there -- in
    which case the stale entry is DROPPED too, so the next Watch is a 1 (a
    GALLEYREQ can only come from a client back in the room, i.e. one whose
    exit we never saw; answering 3 for ever would lock it out -- inferred).
    Membership of any OTHER table's gallery is released first:
    the client holds one table screen."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return GALLEY_LIMITED, 0
    with _LOCK:
        _adopt()
        now = time.time()
        for tid, entry in list(_TABLES.items()):
            if tid != str(int(table_id)):
                (entry.get("gallery") or {}).pop(str(member_id), None)
        entry = _entry(table_id)
        entry["gallery"] = _fresh_gallery(entry, now)
        g = entry["gallery"]
        mine = g.get(str(member_id))
        if mine is not None:
            slot = int(mine.get("slot", 0))
            del g[str(member_id)]
            _publish(force=False)
            return GALLEY_DUP, slot
        taken = {int(x.get("slot", 0)) for x in g.values()}
        slot = next(n for n in range(256) if n not in taken)
        g[str(member_id)] = {"slot": slot, "name": str(name or ""),
                             "at": now, "polid": int(polid or 0)}
        _publish(force=False)
        return GALLEY_OK, slot


def gallery_leave(table_id, member_id):
    """MjGALLEYLEAVEREQ: 5 = released, 6 = there was nothing to release
    (the only two codes the leave wait accepts -- and it MUST get one)."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return GALLEY_LEFT_ERROR
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id)))
        ok = False
        if entry is not None:
            ok = (entry.get("gallery") or {}).pop(str(member_id), None) is not None
        if not ok:
            # The named table and the gallery we hold them in can disagree
            # after a restart; release wherever they are.
            for e in _TABLES.values():
                if (e.get("gallery") or {}).pop(str(member_id), None) is not None:
                    ok = True
                    break
        if ok:
            _publish(force=False)
        return GALLEY_LEFT if ok else GALLEY_LEFT_ERROR


def gallery_drop(member_id):
    """Silently forget a spectator (exit ack, table reap, content leave).
    Returns the table id it was watching, or 0."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return 0
    with _LOCK:
        _adopt()
        freed = 0
        for tid, e in _TABLES.items():
            if (e.get("gallery") or {}).pop(str(member_id), None) is not None:
                try:
                    freed = int(tid)
                except (TypeError, ValueError):
                    freed = 0
        if freed:
            _publish(force=False)
        return freed


def clear_gallery(table_id):
    """The game ended: every spectator of `table_id` is out (the client
    leaves the table screen on MjGAMEEND without an MjGALLEYLEAVEREQ, so a
    membership left here would answer its next Watch with 3). Returns the
    member ids dropped."""
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id or 0)))
        if not entry:
            return []
        gone = list(entry.get("gallery") or {})
        entry["gallery"] = {}
        if gone:
            _publish(force=False)
        return [int(m) for m in gone if m.lstrip("-").isdigit()]


def gallery_at(table_id):
    """Who is watching: [(member_id, slot, name, polid)], by slot."""
    entry = (_live_tables() or {}).get(str(int(table_id or 0)))
    if not entry:
        return []
    out = []
    for mem, g in _fresh_gallery(entry).items():
        try:
            out.append((int(mem), int(g.get("slot", 0)), str(g.get("name") or ""),
                        int(g.get("polid") or 0)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: r[1])
    return out


def gallery_of(member_id):
    """The table this member is WATCHING (never seated at), or 0."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return 0
    for tid, entry in (_live_tables() or {}).items():
        if str(member_id) in _fresh_gallery(entry):
            try:
                return int(tid)
            except (TypeError, ValueError):
                return 0
    return 0


def gallery_slot(member_id):
    """The slot a spectator was given, or None."""
    member_id = int(member_id or 0)
    for entry in (_live_tables() or {}).values():
        g = _fresh_gallery(entry).get(str(member_id))
        if g is not None:
            return int(g.get("slot", 0))
    return None


def table_or_gallery_of(member_id):
    """(table id, is_spectator): the seat first, else the gallery, else
    (0, False). What chat relays and the roster handshake resolve on."""
    tid = table_of(member_id)
    if tid:
        return tid, False
    tid = gallery_of(member_id)
    return (tid, True) if tid else (0, False)


def resolve_table(member_id, wire_id=0, room=None):
    """THE ONE RULE that turns (asker, 16-bit wire id) into a composite id.

    `wire_id` is the table the client named (`id8`, the TableInfoSub subject's
    top 16 bits, MjTBLCONFALL +0x08 -- a composite is tolerated and its base
    used); `room` is the asker's current Jan room per the IRC registry, or
    None/0 when the caller cannot see it. In order:

      1. registry room + wire id  -> that room's table. The client can only
         name a table in the room it is JOINed to (see the ROOM_SHIFT note).
      2. no wire id              -> whatever table the member is seated at
         (MjGAMESTART's +0x08 measures 0 live; the seat is the truth).
      3. no registry room        -> the member's seat if its base matches,
         else the named base in the seated table's ROOM (a move within the
         room, e.g. a reserve elsewhere after the room channel was left),
         else room 0 (the unknown room -- exactly the v1 behaviour).

    3 is what keeps a `b/g/MJSTableInfoSub` fetched AFTER `sqMgCpEnterTable`
    (the game start PARTs nothing we have measured, but the seat is the safer
    anchor either way) on the right table.
    """
    base = split_table_id(wire_id)[1] if wire_id else 0
    room = int(room or 0)
    if room and base:
        return room_table_id(room, base)
    # A SPECTATOR holds no seat but is at a table all the same: its
    # `b/g/MJSTableInfoSub` fetch after `sqMgCpEnterTable` must land on the
    # table it watches (spec section 1.5), so the gallery anchors like a seat.
    seated = table_of(member_id) or gallery_of(member_id)
    if not base:
        return seated
    if seated:
        if split_table_id(seated)[1] == base:
            return seated
        return room_table_id(room_of_table(seated), base)
    return room_table_id(room, base)


_SEQ = {}               # room (str) -> the room's delta sequence
_DELTAS = {}            # room (str) -> [[seq, composite table id], ...] oldest first
_ROWS = {}              # composite table id (str) -> [seat count, in_play] as last sent


def _rk(room):
    return str(int(room or 0))


def enabled():
    return os.environ.get("POL_JAN_SEATS", "1") == "1"


def _path():
    return os.environ.get("POL_JAN_SEATS_FILE", _FILE_DEFAULT)


def _seat_ttl():
    return float(os.environ.get("POL_JAN_SEAT_TTL_S", "1800") or 0)


def _warn_s():
    return float(os.environ.get("POL_JAN_SEAT_WARN_S", "120") or 0)


def _inplay_ttl():
    return float(os.environ.get("POL_JAN_INPLAY_TTL_S", "600") or 0)


def _read_file():
    try:
        mtime = os.stat(_path()).st_mtime
    except OSError:
        return {}
    if mtime != _CACHE["mtime"]:
        try:
            with open(_path(), "r", encoding="utf-8") as f:
                _CACHE["data"] = json.load(f) or {}
            _CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _CACHE["data"]           # torn write: last good view stands
    return _CACHE["data"]


def _adopt():
    """Seed memory from the file BEFORE this process writes it -- setdefault,
    never overwrite, so a restart cannot publish emptiness over live seats."""
    if _ADOPTED[0]:
        return
    _ADOPTED[0] = True
    disk = _read_file()
    for tid, entry in (disk.get("tables") or {}).items():
        # A key that is not one of OUR tables is wreckage from the +0x18 era,
        # when every client wrote a record under its own PolID. It can never
        # become legitimate -- the client addresses a table by the id we
        # authored -- so it is dropped rather than carried forward, and its
        # seats go with it. Anyone actually seated re-reserves in one poll.
        try:
            known = split_table_id(int(tid))[1] in DEFAULT_TABLE_IDS
        except (TypeError, ValueError):
            known = False
        if not known:
            continue
        _TABLES.setdefault(tid, entry)
    # The sequence has to survive a restart or every client in a room is
    # suddenly AHEAD of us, which `deltas_after` answers with a reload -- once
    # per client, forever, because the next fetch restamps them at the lower
    # number we just went back to.
    #
    # v1 -> v2 (finding 30): a v1 file holds ONE `seq` int and ONE `deltas`
    # list, both for the shared table set -- which is room 0's, so they
    # migrate under key "0" and nothing is renumbered. Its table keys "1".."4"
    # already equal room 0's composites (room_table_id(0, t) == t), so the
    # seats above needed no rewriting either.
    seq, deltas = disk.get("seq"), disk.get("deltas")
    if not _SEQ:
        if isinstance(seq, dict):
            _SEQ.update({_rk(k): int(v or 0) for k, v in seq.items()})
        elif seq:
            _SEQ[_rk(0)] = int(seq or 0)
    if not _DELTAS:
        if isinstance(deltas, dict):
            for k, v in deltas.items():
                _DELTAS[_rk(k)] = list(v or [])
        elif deltas:
            _DELTAS[_rk(0)] = list(deltas)
    if not _ROWS:
        for k, v in (disk.get("rows") or {}).items():
            try:
                known = split_table_id(int(k))[1] in DEFAULT_TABLE_IDS
            except (TypeError, ValueError):
                known = False
            if known:
                _ROWS[str(int(k))] = v
    for mem, prefs in (disk.get("members") or {}).items():
        if isinstance(prefs, dict):
            _MEMBERS.setdefault(str(mem), prefs)


def _publish(force=True):
    """Hand the seats to the other container. Never raises.

    WARNING: EVERY publish syncs the row deltas first, so no mutator can add a seat
    without the sequence moving. Hanging the bump off the one function they all
    already call is deliberate: a delta producer per mutator is the shape that
    made TM's table state the project's most-repeated bug, and it always failed
    the same way -- somebody added a seventh writer and never a seventh bump.
    """
    if not _ADOPTED[0]:
        _adopt()
    _OWNER[0] = True
    if _sync_rows():
        force = True        # a sequence bump has to cross the split NOW: the
                            # lobby band stamps the blob from this file, and a
                            # blob stamped behind our memory hands the client a
                            # serial we would answer with deltas it already has
    now = time.time()
    if not force and now - _LAST_PUBLISH[0] < 5.0:
        return                              # a pure touch can wait a beat
    _LAST_PUBLISH[0] = now
    try:
        p = _path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 2, "tables": _TABLES, "seq": _SEQ,
                       "deltas": _DELTAS, "rows": _ROWS,
                       "members": _MEMBERS}, f)
        os.replace(tmp, p)                  # atomic
        _CACHE["mtime"] = -1.0
    except OSError:
        pass                                # memory still serves this process


def _live_tables():
    """The table map, from wherever it is actually known (tmroom's rule: the
    owner answers from memory, everyone else from the file)."""
    if _OWNER[0]:
        return _TABLES
    return (_read_file() or {}).get("tables", {})


def _live_seq(room=0):
    """One room's sequence, from memory or the file (either format)."""
    if _OWNER[0]:
        return int(_SEQ.get(_rk(room), 0))
    seq = (_read_file() or {}).get("seq")
    if isinstance(seq, dict):
        return int(seq.get(_rk(room)) or 0)
    return int(seq or 0) if not int(room or 0) else 0


def _live_deltas(room=0):
    if _OWNER[0]:
        return _DELTAS.get(_rk(room)) or []
    deltas = (_read_file() or {}).get("deltas")
    if isinstance(deltas, dict):
        return deltas.get(_rk(room)) or []
    return (deltas or []) if not int(room or 0) else []


def rooms_known():
    """Every room this store has a table, row or sequence for (0 included
    when a v1 store was adopted or a room-less caller reserved)."""
    out = set()
    for k in list(_TABLES) + list(_ROWS):
        try:
            room, base = split_table_id(int(k))
        except (TypeError, ValueError):
            continue
        if base in DEFAULT_TABLE_IDS:       # not the +0x18-era PolID wreckage
            out.add(room)
    for k in _SEQ:
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def _fresh_seats(entry, now=None):
    """A table entry's seats with the expired ones filtered OUT (not removed --
    the reader container must not need write access to enforce a TTL)."""
    ttl = _seat_ttl()
    now = time.time() if now is None else now
    out = {}
    for mem, s in (entry.get("seats") or {}).items():
        if ttl and now - float(s.get("at", 0)) > ttl:
            continue
        out[mem] = s
    return out


def _entry(tid):
    e = _TABLES.setdefault(str(int(tid)), {"master": 0, "in_play": 0.0,
                                           "seats": {}, "gallery": {}})
    e.setdefault("gallery", {})         # an entry adopted from an older file
    return e


def _free_seat(seats):
    taken = {int(s.get("seat", 0)) for s in seats.values()}
    for n in range(CAPACITY):
        if n not in taken:
            return n
    return None


# --- the row deltas ----------------------------------------------------------

def _row_state(tid):
    """(seat count, in play) for one table, EXACTLY as janlobby renders it.

    WARNING: These two numbers are the only thing a `TD` delta can change, so they are
    the only thing worth a sequence bump. Deriving them here from the same
    helpers `seat_count`/`is_in_play` use is what keeps the delta and the
    snapshot from ever disagreeing.
    """
    entry = _TABLES.get(str(int(tid)))
    if not entry:
        return (0, False)
    seats = _fresh_seats(entry)
    mark = float(entry.get("in_play") or 0)
    ttl = _inplay_ttl()
    live = bool(mark) and bool(seats)
    if live and ttl and time.time() - mark > ttl:
        live = False
    return (len(seats), live)


def _sync_rows():
    """Bump the sequence for every table whose rendered row has moved.

    THE ONE PRODUCER OF DELTAS. Every mutation calls it, and so does
    `reconcile()` -- which matters because a seat expires at READ time with
    nobody writing anything, and a repair that only runs on new writes never
    heals old state (tm-table-state-invariant's lesson, one game over). The
    `<DR>` poll is a ~2 s tick, so routing reconcile through it means an
    expiry reaches the screen on its own.

    WARNING: Returns the number of bumps; the caller decides whether to publish.
    """
    # WARNING: ONLY THE REAL TABLES. This used to add every key in `_TABLES`, which
    # meant the legacy PolID-keyed junk left by the +0x18 bug (the table id
    # is per session, not a PolID) got rows and DELTAS of its own -- and
    # `janlobby.table_values_for` cannot render a table that is not in
    # JAN_TABLES, so the responder answered those with `<DO>`. Measured live
    # 2026-09-04: deltas 5 and 6 named `6557771827156071947` and
    # `6557772211577543196`, so every client whose sequence spanned them ate a
    # full snapshot reload instead of a row update. A delta for a row nobody
    # can draw is not a delta.
    #
    # PER ROOM (finding 30): each known room's four composites, against that
    # room's own sequence. A row nobody has sent yet is EMPTY, not unknown --
    # a fresh room's PTL already showed four empty tables at sequence 0, so
    # the first bump in a room is its first real change, not four phantom
    # "unknown -> empty" moves.
    bumps = 0
    for room in sorted(rooms_known()):
        rk = _rk(room)
        for base in sorted(DEFAULT_TABLE_IDS):
            tid = room_table_id(room, base)
            key = str(tid)
            now = list(_row_state(tid))
            if _ROWS.get(key, [0, False]) == now:
                continue
            _ROWS[key] = now
            _SEQ[rk] = int(_SEQ.get(rk, 0)) + 1
            log_ = _DELTAS.setdefault(rk, [])
            log_.append([_SEQ[rk], tid])
            del log_[:-DELTA_WINDOW]
            bumps += 1
    return bumps


def reconcile():
    """Heal rows that changed with nobody writing (a seat aged out).

    Called from the `<DR>` responder, which is the only regular tick this
    module gets. Publishes only when something actually moved. Also the place
    an EXPIRED master's table gets a new one: `_drop_stale_master` used to run
    only inside `reserve()`, so a table whose master had crashed kept pointing
    at them until somebody new sat down (finding 24).
    """
    if not enabled():
        return 0
    with _LOCK:
        _adopt()
        moved = 0
        for tid, entry in list(_TABLES.items()):
            live = _fresh_seats(entry)
            gone = [m for m in (entry.get("seats") or {}) if m not in live]
            if not gone:
                continue
            # Drop the expired seats for real (the reader filters them; the
            # writer may as well forget them) so promotion sees the truth.
            for m in gone:
                if _release(tid, int(m), why="expired"):
                    moved += 1
        n = _sync_rows() + moved
        if n:
            _publish()
        return n


def expiring_seats(now=None):
    """[(table id, member, seconds left)] for seats inside the warning window
    that have not been warned yet -- MjNOTICETIMEUPWARNING's feed. Marks them
    warned; a `touch()` clears the mark so a seat renewed and silent again is
    warned again."""
    ttl, warn = _seat_ttl(), _warn_s()
    if not enabled() or not ttl or not warn:
        return []
    now = time.time() if now is None else now
    out = []
    with _LOCK:
        _adopt()
        changed = False
        for tid, entry in _TABLES.items():
            for mem, s in (entry.get("seats") or {}).items():
                left = ttl - (now - float(s.get("at", 0)))
                if 0 < left <= warn and not s.get("warned"):
                    s["warned"] = True
                    changed = True
                    try:
                        out.append((int(tid), int(mem), int(left)))
                    except (TypeError, ValueError):
                        continue
        if changed:
            _publish()
    return out


def take_master_changes():
    """The (table id, new master member) pairs promoted since the last call.
    `janhourou` turns each into an MjNOTICEMEMBER with +0x54 = the new seat."""
    with _LOCK:
        out = list(_MASTER_CHANGES)
        del _MASTER_CHANGES[:]
        return out


def sequence(room=0):
    """The update sequence `janlobby.ptl_blob` must stamp into `+0x40` for a
    PTL of `room` (0 = the unknown-room set).

    A blob carrying anything else desynchronises the client permanently: it
    reports what it holds, and we answer relative to that number -- and it
    is PER ROOM, because the rows it counts are.
    """
    if not enabled():
        return 0
    return _live_seq(room)


def deltas_after(have, room=0):
    """The (seq, composite table id) pairs a client at `have` in `room`
    still needs.

    None  -- cannot serve from the log: reload the snapshot (`<DO>`).
    []    -- the client is CURRENT: say NOTHING, which is what keeps the poll
             from turning into a b/g/PTL re-fetch storm.
    [..]  -- send them in order.
    """
    cur = _live_seq(room)
    try:
        have = int(have)
    except (TypeError, ValueError):
        return None
    if have == cur:
        return []
    if have > cur or have < cur - RELOAD_BEHIND:
        return None                     # ahead of us, or past the reload gate
    want = [d for d in (_live_deltas(room) or []) if int(d[0]) > have]
    if len(want) != cur - have:
        return None                     # we do not hold the whole chain
    for i, d in enumerate(want):
        if int(d[0]) != have + 1 + i:
            return None                 # a gap is a permanent stall
    return [(int(d[0]), int(d[1])) for d in want]


# --- the writer API (auth band) ----------------------------------------------

def reserve(table_id, member_id, name="", polid=0, tag=None, voice=None):
    """Seat `member_id` at `table_id`. Returns (result, my_seat, master_seat).

    First body in -> seat 0 + ReserveMaster (2); later ones -> the next free
    seat + Reserve (1). Re-reserving a held seat re-affirms the original answer
    (idempotent -- the client re-sends on a missed ACK). A full table answers
    RESERVE_FULL (8). Holding a seat at ANOTHER table releases it first: the
    client's own my-table state is one global (_DAT_00446510), so one seat per
    member is the only shape it can represent anyway.

    `tag` is the request's +0x13 -- the client's RESERVE-family counter
    (`DAT_00449048 & 0xf`), which the kick reply must echo (see
    `reserve_tag`). `voice` is MjPLAYREQ +0x42, the player's VoiceCharaNum
    (the voice bank `b/g/MJSTableInfoSub` +0x218 serves per seat).
    """
    member_id = int(member_id or 0)
    if member_id <= 0:
        return RESERVE_MASTER, 0, 0        # nothing to key on; old behaviour
    with _LOCK:
        _adopt()
        now = time.time()
        for tid, entry in list(_TABLES.items()):
            if tid != str(int(table_id)) and str(member_id) in entry["seats"]:
                _release(tid, member_id, why="moved")
        entry = _entry(table_id)
        entry["seats"] = _fresh_seats(entry, now)
        _drop_stale_master(entry)
        seats = entry["seats"]
        mine = seats.get(str(member_id))
        if mine is not None:
            mine["at"] = now
            mine.pop("warned", None)
            if polid and not mine.get("polid"):
                mine["polid"] = int(polid)
            if tag is not None:
                mine["rtag"] = int(tag) & 0xF
            if voice is not None:
                mine["voice"] = int(voice) & 0xFFFF
            if not entry.get("master"):
                entry["master"] = member_id     # vacant mastership: claimed by
            result = RESERVE_MASTER if entry.get("master") == member_id else RESERVE
            _publish()
            return result, int(mine["seat"]), _master_seat(entry)
        seat = _free_seat(seats)
        if seat is None:
            return RESERVE_FULL, 0, _master_seat(entry)
        if not entry.get("master"):
            # First body in -- or the first one after a master cancelled out
            # from under the table -- runs it. WARNING: Note the master's SEAT is then
            # not necessarily 0; the ACK's high nibble carries where it is.
            entry["master"] = member_id
        seats[str(member_id)] = {"seat": seat, "name": str(name or ""),
                                 "at": now, "polid": int(polid or 0)}
        if tag is not None:
            seats[str(member_id)]["rtag"] = int(tag) & 0xF
        if voice is not None:
            seats[str(member_id)]["voice"] = int(voice) & 0xFFFF
        result = RESERVE_MASTER if entry.get("master") == member_id else RESERVE
        _publish()
        return result, seat, _master_seat(entry)


def _seat_of(member_id):
    """(table id str, entry, seat dict) for a member's live seat, or None."""
    for tid, entry in _TABLES.items():
        s = _fresh_seats(entry).get(str(int(member_id)))
        if s is not None:
            return tid, entry, s
    return None


def note_reserve_tag(member_id, tag):
    """Any RESERVE-family request (MjPLAYREQ/CANCEL/GALLEYREQ/GALLEYLEAVEREQ,
    the four builders that bump `DAT_00449048`) moves the client's counter;
    remember the latest so a kick reply can carry it."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return
    with _LOCK:
        _adopt()
        hit = _seat_of(member_id)
        if hit is not None:
            hit[2]["rtag"] = int(tag) & 0xF
            _publish(force=False)


def reserve_tag(member_id):
    """The client's current reserve-family tag, or None if never seen.

    WHY A KICK NEEDS IT (TableMemberBanish.c:152-153 + objstrings.c:2637):
    the banish is SENT with the master-command tag (`DAT_00449058&0xf|0x20`)
    but the client WAITS on `wrsCpTableReserveCheck`, which accepts only
    `MjRESERVEACK` whose +0x13 == `DAT_00449048 & 0xf` -- the counter the
    RESERVE builders bump. Echoing the banish's own tag can never match.
    """
    with _LOCK:
        _adopt()
        hit = _seat_of(int(member_id or 0)) if member_id else None
        if hit is None or hit[2].get("rtag") is None:
            return None
        return int(hit[2]["rtag"]) & 0xF


def voice_of(member_id):
    """The VoiceCharaNum the member sent at reserve time (MjPLAYREQ +0x42)."""
    for tid, entry in (_live_tables() or {}).items():
        s = _fresh_seats(entry).get(str(int(member_id or 0)))
        if s is not None:
            return int(s.get("voice") or 0)
    return 0


def note_member_pref(member_id, **prefs):
    """Per-member preferences that outlive a seat (persisted with the seats)."""
    member_id = int(member_id or 0)
    if member_id <= 0 or not prefs:
        return
    with _LOCK:
        _adopt()
        cur = _MEMBERS.setdefault(str(member_id), {})
        changed = False
        for k, v in prefs.items():
            if cur.get(k) != v:
                cur[k] = v
                changed = True
        if changed:
            _publish()


def member_pref(member_id, key, default=None):
    if _OWNER[0]:
        prefs = _MEMBERS.get(str(int(member_id or 0)))
    else:
        prefs = ((_read_file() or {}).get("members") or {}).get(
            str(int(member_id or 0)))
    return (prefs or {}).get(key, default)


def _drop_stale_master(entry):
    """A master who no longer holds a live seat is not the master.

    WARNING: WITHOUT THIS THE FIRST FIX IS INVISIBLE. `master` was the one field no
    TTL touched, so a table whose seats had all expired kept pointing at a
    member who was long gone -- and the next person to reserve it got result 1
    (Reserve, a guest) instead of 2 (ReserveMaster). No master flag means the
    4-item guest popup, which has no Start Game row at all. Prod's live
    jan-seats.json had exactly this on table 1 (`master: 8` from a 39-hour-old
    selftest seat) as of 2026-09-03.
    """
    mid = int(entry.get("master") or 0)
    if mid and str(mid) not in (entry.get("seats") or {}):
        entry["master"] = 0


def seats_at(table_id):
    """Who is really sitting at this table: [(member_id, seat, name, polid)].

    WARNING: THE GAME MANAGER NEEDS THIS AND HAD NO WAY TO ASK. It keyed its in-game
    table by MEMBER id and called `fill_with_bots()`, so a Start Game pressed at
    a table with two humans began a solo hanchan against three bots and the
    other human -- correctly seated right here -- was never in the game.
    Measured live 2026-09-04: MjNOTICEMEMBER announced
    `0='PS2Tester' 1='COM 1' 2='COM 2' 3='COM 3'` while this store held two.

    Ordered by seat, so the caller can trust the order. The PolID is what the
    client stamps in its own messages (+0x18) and it is what the seat table has
    to carry, since MjNOTICEMEMBER's four ids are PolIDs, not member numbers.
    """
    entry = (_live_tables() or {}).get(str(int(table_id)))
    if not entry:
        return []
    out = []
    for mem, s in _fresh_seats(entry).items():
        try:
            out.append((int(mem), int(s.get("seat", 0)), str(s.get("name") or ""),
                        int(s.get("polid") or 0)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: r[1])
    return out


def master_seat_of(table_id):
    """Which SEAT runs this table, or None if nobody does.

    The client compares its own seat against this to decide whether it is the
    master (`jansave.apply_seat`), so "no master" has to be distinguishable
    from "seat 0" -- returning 0 for an unowned table would hand the master
    menu to whoever sits at seat 0.
    """
    entry = (_live_tables() or {}).get(str(int(table_id)))
    if not entry:
        return None
    mid = int(entry.get("master") or 0)
    if not mid:
        return None
    s = _fresh_seats(entry).get(str(mid))
    return int(s["seat"]) if s else None


def _master_seat(entry):
    s = entry["seats"].get(str(entry.get("master") or 0))
    return int(s["seat"]) if s else 0


def _release(tid, member_id, why="left"):
    entry = _TABLES.get(str(tid))
    if not entry:
        return False
    gone = entry["seats"].pop(str(int(member_id)), None)
    if gone is None:
        return False
    if entry.get("master") == int(member_id):
        # THE NEXT SEATED GUEST TAKES OVER (2026-09-04). This used to leave
        # `master` empty on purpose -- "a server-side promotion would be
        # invisible to the guest's client" -- and that was true only because
        # nothing told the client. `mahdisp.c:53-57` is the writer of the
        # "You are the new Table Master" dialog: an MjNOTICEMEMBER whose +0x54
        # (the master's seat) differs from the last one and equals the
        # recipient's own seat. `take_master_changes()` hands the promotion to
        # janhourou, which pushes exactly that notice.
        entry["master"] = _promote(entry, str(tid), exclude=int(member_id))
    if not entry["seats"]:
        entry["in_play"] = 0.0
        entry["gallery"] = {}           # nothing left to watch
    return True


def _promote(entry, tid, exclude=0, after_seat=None):
    """Pick the new master: the lowest live seat (or the next one round from
    `after_seat`), skipping `exclude`. Records the change. 0 = nobody."""
    seats = _fresh_seats(entry)
    cands = sorted((int(s.get("seat", 0)), int(m)) for m, s in seats.items()
                   if int(m) != exclude)
    if not cands:
        return 0
    if after_seat is not None:
        later = [c for c in cands if c[0] > after_seat]
        cands = later + [c for c in cands if c[0] <= after_seat]
    new = cands[0][1]
    _MASTER_CHANGES.append((int(tid), new))
    return new


def master_of(table_id):
    """The MEMBER id running this table, or 0."""
    entry = (_live_tables() or {}).get(str(int(table_id or 0)))
    if not entry:
        return 0
    mid = int(entry.get("master") or 0)
    return mid if mid and str(mid) in _fresh_seats(entry) else 0


def decline_master(table_id, member_id):
    """MjCHMASTER: `member_id` was offered the mastership and said No. Pass it
    to the next seat round from theirs. Returns the new master member (which
    is `member_id` again if nobody else is seated -- the one-occupant dialog
    has no No button, so this cannot loop)."""
    member_id = int(member_id or 0)
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id or 0)))
        if entry is None:
            return 0
        mine = _fresh_seats(entry).get(str(member_id))
        if mine is None:
            return int(entry.get("master") or 0)
        new = _promote(entry, str(int(table_id)), exclude=member_id,
                       after_seat=int(mine.get("seat", 0)))
        entry["master"] = new or member_id
        _publish()
        return entry["master"]


def set_master(table_id, member_id):
    """Hand the table to a seated member outright (a server-owner lever)."""
    member_id = int(member_id or 0)
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id or 0)))
        if entry is None or str(member_id) not in _fresh_seats(entry):
            return False
        if entry.get("master") != member_id:
            entry["master"] = member_id
            _MASTER_CHANGES.append((int(table_id), member_id))
            _publish()
        return True


def mark_rejoin(member_id, pending=True):
    """MjENTERGAME was answered Success for a member whose table is playing:
    flag the seat so the game manager can resync them on their next line."""
    member_id = int(member_id or 0)
    with _LOCK:
        _adopt()
        hit = _seat_of(member_id) if member_id else None
        if hit is None:
            return False
        if pending:
            hit[2]["rejoin"] = time.time()
        else:
            hit[2].pop("rejoin", None)
        _publish()
        return True


def rejoin_pending(member_id, clear=False):
    """True if `mark_rejoin` flagged this member and nobody has resynced them.
    THE JANGAME SEAM: `if janseats.rejoin_pending(member, clear=True):
    <re-send MjHAIPAI/MjALLDATA for that seat>`."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return False
    with _LOCK:
        _adopt()
        hit = _seat_of(member_id)
        if hit is None or not hit[2].get("rejoin"):
            return False
        if clear:
            hit[2].pop("rejoin", None)
            _publish()
        return True


def cancel(table_id, member_id):
    """Free the member's seat. 5 = done, 6 = there was nothing to cancel."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return RESERVE_CANCEL
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id)))
        if entry is not None:
            entry["seats"] = _fresh_seats(entry)
        ok = _release(str(int(table_id)), member_id, why="cancel")
        if not ok:
            # The id the cancel names and the seat we hold can disagree after a
            # restart or an expiry; releasing wherever the member actually sits
            # keeps the two sides consistent rather than wedging the client.
            for tid in list(_TABLES):
                if _release(tid, member_id, why="cancel"):
                    ok = True
                    break
        _publish()
        return RESERVE_CANCEL if ok else RESERVE_CANCEL_ERROR


def release_member(member_id, why="left"):
    """The member left the room/content/connection -- free whatever they held.
    Returns the table id they were released from, or 0."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return 0
    with _LOCK:
        _adopt()
        freed = 0
        for tid in list(_TABLES):
            if _release(tid, member_id, why=why):
                try:
                    freed = int(tid)
                except (TypeError, ValueError):
                    freed = 0
        _publish()
        return freed


def touch(member_id):
    """Renew the member's seat(s) and their table's in-play mark. Called for
    every Jan line on the auth band, so 'alive' means 'talking to us'."""
    member_id = int(member_id or 0)
    if member_id <= 0:
        return
    with _LOCK:
        _adopt()
        now = time.time()
        changed = False
        for entry in _TABLES.values():
            s = entry["seats"].get(str(member_id))
            if s is not None:
                s["at"] = now
                s.pop("warned", None)
                if entry.get("in_play"):
                    entry["in_play"] = now
                changed = True
            # A spectator's only heartbeat is its game-band line (a re-sent
            # MjGALLEYACK on an ack screen) or `<DR>`; renew its gallery
            # entry the same way, so the TTL is "stopped talking to us".
            g = (entry.get("gallery") or {}).get(str(member_id))
            if g is not None:
                g["at"] = now
                changed = True
        if changed:
            _publish(force=False)


def set_in_play(table_id, member_id=0):
    """The master pressed Start Game (MjGAMESTART names the table at +0x08)."""
    with _LOCK:
        _adopt()
        entry = _entry(table_id)
        entry["in_play"] = time.time()
        if member_id:
            touch(member_id)
        _publish()


def clear_in_play(table_id):
    with _LOCK:
        _adopt()
        entry = _TABLES.get(str(int(table_id)))
        if entry is not None:
            entry["in_play"] = 0.0
            _publish()


# --- the reader API (lobby band, PTL builder) --------------------------------

def table_of(member_id):
    """The table id this member currently holds a live seat at, or 0.

    The fallback for any message that should name a table and does not --
    MjGAMESTART's +0x08 is 0 whenever the client's my-table global was never
    stored. Returns an int so callers can `or` it.
    """
    member_id = int(member_id or 0)
    if member_id <= 0:
        return 0
    for tid, entry in (_live_tables() or {}).items():
        if str(member_id) in _fresh_seats(entry):
            try:
                return int(tid)
            except (TypeError, ValueError):
                return 0
    return 0


def seat_count(table_id):
    """Live (unexpired) reservations at this table."""
    entry = (_live_tables() or {}).get(str(int(table_id)))
    if not entry:
        return 0
    return len(_fresh_seats(entry))


def is_in_play(table_id):
    entry = (_live_tables() or {}).get(str(int(table_id)))
    if not entry:
        return False
    mark = float(entry.get("in_play") or 0)
    if not mark:
        return False
    ttl = _inplay_ttl()
    if ttl and time.time() - mark > ttl:
        return False
    if not _fresh_seats(entry):
        return False                        # a game with nobody at it is over
    return True


def snapshot():
    """For logs: {table id: (seats, master, in_play)}."""
    out = {}
    for tid, entry in (_live_tables() or {}).items():
        out[tid] = (len(_fresh_seats(entry)), int(entry.get("master") or 0),
                    bool(entry.get("in_play")))
    return out


# --- selftest ----------------------------------------------------------------

def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print("%-58s %s" % (name, "ok" if cond else "FAIL"))
        ok = ok and bool(cond)

    with tempfile.TemporaryDirectory() as td:
        os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "jan-seats.json")
        _TABLES.clear()
        _ADOPTED[0] = False
        _OWNER[0] = False
        _CACHE["mtime"] = -1.0

        r, seat, ms = reserve(1, 8, "PS2Tester")
        check("first reserver is the master (result 2, seat 0)",
              (r, seat, ms) == (RESERVE_MASTER, 0, 0))
        r2, seat2, ms2 = reserve(1, 16, "DeckTest")
        check("second reserver is a guest (result 1, seat 1, master at 0)",
              (r2, seat2, ms2) == (RESERVE, 1, 0))
        check("extra byte for the master is 0 (the value confirmed live)",
              ((ms & 0xF) << 4 | (seat & 0xF)) == 0)
        check("seat_count sees both", seat_count(1) == 2)
        r3, seat3, _ = reserve(1, 8, "PS2Tester")
        check("re-reserve is idempotent (master again, same seat)",
              (r3, seat3) == (RESERVE_MASTER, 0))
        check("re-reserve did not add a seat", seat_count(1) == 2)

        r4, seat4, _ = reserve(2, 16, "DeckTest")
        check("reserving elsewhere MOVES the guest (result 2 at the new table)",
              (r4, seat4) == (RESERVE_MASTER, 0))
        check("the old seat was released", seat_count(1) == 1)
        reserve(1, 16, "DeckTest")

        reserve(1, 21, "c"); reserve(1, 22, "d")
        rl, _sl, _ = reserve(1, 99, "late")
        check("a full table answers RESERVE_FULL (8, 'the table status "
              "changed'), NOT 10 ('Entry limits not met')",
              rl == RESERVE_FULL == 8)

        check("cancel frees the seat (result 5)", cancel(1, 22) == RESERVE_CANCEL)
        check("cancelling nothing is ReserveCancelError (6)",
              cancel(1, 22) == RESERVE_CANCEL_ERROR)
        check("cancel with a stale table id still finds the member",
              cancel(3, 21) == RESERVE_CANCEL)

        set_in_play(1, 8)
        check("MjGAMESTART marks the table in play", is_in_play(1))
        clear_in_play(1)
        check("clear_in_play clears it", not is_in_play(1))

        # master cancels while a guest remains: the seat frees and the
        # mastership MOVES to the guest -- and the move is reported, because
        # an MjNOTICEMEMBER with +0x54 = the guest's seat is what makes their
        # client show "You are the new Table Master" (mahdisp.c:53-57).
        take_master_changes()
        check("master cancel releases", cancel(1, 8) == RESERVE_CANCEL)
        check("the remaining guest is PROMOTED",
              seat_count(1) == 1 and master_of(1) == 16)
        check("...and the promotion is reported for the notice",
              take_master_changes() == [(1, 16)])
        check("reported once", take_master_changes() == [])
        rn, seatn, _ = reserve(1, 33, "next")
        check("a later reserver is a GUEST of the promoted master",
              rn == RESERVE and master_of(1) == 16)
        # MjCHMASTER: the promoted guest says No -> the next seat round
        check("decline_master rotates to the next seat",
              decline_master(1, 16) == 33 and master_of(1) == 33
              and take_master_changes() == [(1, 33)])
        check("...and round again past the end of the seat order",
              decline_master(1, 33) == 16 and master_of(1) == 16)
        check("a lone occupant declining keeps the mastership",
              (cancel(1, 33) == RESERVE_CANCEL and decline_master(1, 16) == 16
               and master_of(1) == 16))
        take_master_changes()
        check("set_master hands the table over and reports it",
              (reserve(1, 33, "back")[0] == RESERVE and set_master(1, 33)
               and master_of(1) == 33 and take_master_changes() == [(1, 33)]))
        check("set_master refuses a stranger", not set_master(1, 4242))
        cancel(1, 33)
        take_master_changes()

        # THE RESERVE TAG -- the kick reply must carry the client's
        # reserve-family counter, not the banish's own tag.
        check("a seat remembers its reserve tag",
              reserve(1, 16, "DeckTest", tag=0x27, voice=3)[0] in (RESERVE,
                                                                   RESERVE_MASTER)
              and reserve_tag(16) == 0x7)
        note_reserve_tag(16, 0x28)
        check("note_reserve_tag moves it", reserve_tag(16) == 0x8)
        check("an unknown member has no tag", reserve_tag(4242) is None)
        check("the voice choice (MjPLAYREQ +0x42) is kept per seat",
              voice_of(16) == 3 and voice_of(4242) == 0)
        note_member_pref(16, face=2)
        check("a member preference persists", member_pref(16, "face") == 2)

        # REJOIN: the flag the game manager polls after MjENTERGAME Success.
        check("rejoin_pending is False until marked", not rejoin_pending(16))
        check("mark_rejoin flags a seated member", mark_rejoin(16))
        check("...and it reads back", rejoin_pending(16))
        check("clear=True consumes it",
              rejoin_pending(16, clear=True) and not rejoin_pending(16))
        check("mark_rejoin refuses a stranger", not mark_rejoin(4242))

        # the seat-expiry warning window
        os.environ["POL_JAN_SEAT_TTL_S"] = "100"
        os.environ["POL_JAN_SEAT_WARN_S"] = "30"
        _TABLES["1"]["seats"]["16"]["at"] = time.time() - 80
        warn = expiring_seats()
        check("a seat inside the warning window is reported once",
              [(t, m) for t, m, _l in warn] == [(1, 16)]
              and 0 < warn[0][2] <= 30 and expiring_seats() == [])
        touch(16)
        _TABLES["1"]["seats"]["16"]["at"] = time.time() - 80
        check("a touch re-arms the warning", len(expiring_seats()) == 1)
        _TABLES["1"]["seats"]["16"]["at"] = time.time()
        os.environ["POL_JAN_SEAT_TTL_S"] = "900"
        os.environ.pop("POL_JAN_SEAT_WARN_S", None)
        take_master_changes()
        rn, seatn, _ = reserve(1, 33, "next")
        check("a reserver joining the promoted master is a guest",
              rn == RESERVE and master_of(1) == 16)
        check("table_of finds a seated member", table_of(33) == 1)
        check("master_seat_of names the owner's SEAT, not their member id",
              master_seat_of(1) == _TABLES["1"]["seats"][
                  str(_TABLES["1"]["master"])]["seat"])
        check("master_seat_of is None for a table nobody owns",
              master_seat_of(4) is None)
        check("table_of is 0 for a stranger", table_of(4242) == 0)

        # A master whose seat has expired must not keep the table hostage: the
        # next reserver has to come back result 2, or their popup has no Start
        # Game row. Prod carried exactly this stale record (see
        # _drop_stale_master).
        _TABLES["9"] = {"master": 8, "in_play": 0.0,
                        "seats": {"8": {"seat": 0, "name": "x",
                                        "at": time.time() - 100000}}}
        rs, _ss, _ms = reserve(9, 77, "after the ghost")
        check("a stale master does not demote the next reserver",
              rs == RESERVE_MASTER)

        # expiry: age a seat past the TTL and it vanishes from every read
        os.environ["POL_JAN_SEAT_TTL_S"] = "1"
        _TABLES["1"]["seats"][str(33)]["at"] = time.time() - 5
        check("a silent seat expires at read time",
              seat_count(1) == 1)          # 16 remains, 33 aged out
        os.environ["POL_JAN_SEAT_TTL_S"] = "900"

        # the cross-container read: a second 'process' (fresh module state)
        # sees the published file
        _publish()
        saved = dict(_TABLES)
        _TABLES.clear()
        _OWNER[0] = False
        _CACHE["mtime"] = -1.0
        check("a reader process sees the published seats", seat_count(1) >= 1)
        # ...and a restarted WRITER adopts instead of wiping
        _ADOPTED[0] = False
        reserve(2, 44, "restart")
        check("a restarted writer adopted the file (table 1 survived)",
              seat_count(1) >= 1)
        _TABLES.clear()
        _TABLES.update(saved)

        # --- the row delta stream ------------------------------------------
        # Run LAST: it clears the tables, so anything after it would be
        # asserting against an empty store (which is how the first draft of
        # this block broke every check below it).
        _TABLES.clear()
        _DELTAS.clear()
        _ROWS.clear()
        _SEQ.clear()
        _ADOPTED[0] = True
        _OWNER[0] = True

        # AN UNSENT ROW IS AN EMPTY ROW. A fresh room's PTL already showed
        # four empty tables at sequence 0, so the first sync has nothing to
        # say -- it used to bump once per table ("unknown -> empty"), which
        # made a room's first reservation arrive as FOUR deltas.
        reconcile()
        base = sequence()
        check("a fresh store bumps nothing: an unsent row IS an empty row",
              base == 0 and reconcile() == 0)
        check("a client at the current sequence is CURRENT (silence)",
              deltas_after(base) == [])
        check("a client AHEAD of us is told to reload",
              deltas_after(base + 5) is None)

        reserve(1, 6, "LaptopTest2")
        d = deltas_after(base)
        check("a reservation issues exactly one delta, for its own table",
              d is not None and len(d) == 1 and d[0][1] == 1)
        check("...and it advances the sequence by one",
              sequence() == base + 1)
        reserve(1, 6, "LaptopTest2")
        check("re-reserving the SAME seat issues NO delta (idempotent)",
              sequence() == base + 1)

        set_in_play(1, 6)
        check("MjGAMESTART moves the row, so it issues a delta",
              sequence() == base + 2)
        d = deltas_after(base)
        check("a client two behind gets a contiguous chain",
              d is not None and [x[0] for x in d] == [base + 1, base + 2])

        # A gap must be refused outright -- the client applies strictly in
        # order, so a hole is a permanent stall, not a delay.
        keep = list(_DELTAS["0"])
        _DELTAS["0"] = [x for x in keep if int(x[0]) != base + 1]
        check("a HOLE in the chain is refused (reload, never a partial)",
              deltas_after(base) is None)
        _DELTAS["0"] = keep

        check("a client further behind than RELOAD_BEHIND reloads",
              deltas_after(sequence() - RELOAD_BEHIND - 1) is None)

        # Expiry moves a row with nobody writing: reconcile is what notices.
        os.environ["POL_JAN_SEAT_TTL_S"] = "1"
        _TABLES["1"]["seats"]["6"]["at"] = time.time() - 500
        before = sequence()
        check("reconcile issues the delta an EXPIRY would otherwise hide "
              "(and drops the dead seat for real)",
              reconcile() == 2 and sequence() == before + 1
              and "6" not in _TABLES["1"]["seats"])
        check("reconcile is idempotent once it has caught up",
              reconcile() == 0 and sequence() == before + 1)
        # an EXPIRED master hands the table to the survivor without a write
        os.environ["POL_JAN_SEAT_TTL_S"] = "900"
        reserve(2, 61, "m"); reserve(2, 62, "g")
        take_master_changes()
        _TABLES["2"]["seats"]["61"]["at"] = time.time() - 5000
        reconcile()
        check("an expired master is replaced by the surviving guest",
              master_of(2) == 62 and take_master_changes() == [(2, 62)])
        cancel(2, 62)
        take_master_changes()
        os.environ["POL_JAN_SEAT_TTL_S"] = "1"
        os.environ["POL_JAN_SEAT_TTL_S"] = "900"

        # WARNING: A TABLE THAT IS NOT ONE OF OURS EARNS NOTHING. The legacy
        # PolID-keyed wreckage from the +0x18 era used to get rows and deltas
        # of its own, and `janlobby.table_values_for` cannot draw one -- so the
        # responder answered every client whose sequence spanned it with a full
        # snapshot reload. Live on 2026-09-04 as deltas 5 and 6.
        _TABLES["6557771827156071947"] = {
            "master": 6, "in_play": 0.0,
            "seats": {"6": {"seat": 0, "name": "ghost", "at": time.time()}}}
        # Settle first: restoring the TTL above un-expires the seat this block
        # aged out, which is a REAL row change and would otherwise be counted
        # against the ghost table below.
        reconcile()
        before = sequence()
        check("a table that is not one of ours earns NO delta",
              reconcile() == 0 and sequence() == before)

        # ...and it does not survive a restart either.
        _publish()
        _TABLES.clear()
        _ROWS.clear()
        _ADOPTED[0] = False
        _adopt()
        check("adopt drops the legacy PolID-keyed tables",
              "6557771827156071947" not in _TABLES)
        check("...and keeps the real ones", "1" in _TABLES or not _TABLES)

        # --- TWO ROOMS, ONE TABLE NUMBER (finding 30) ----------------------
        # Room 1-1 (101) table 1 and Room 2-3 (203) table 1: different seats,
        # different masters, different deltas, and a `<DR>` in one room never
        # sees the other's rows.
        _TABLES.clear(); _DELTAS.clear(); _ROWS.clear(); _SEQ.clear()
        _ADOPTED[0] = True; _OWNER[0] = True
        t101 = room_table_id(101, 1)
        t203 = room_table_id(203, 1)
        check("the composite is room << 16 | table and splits back",
              t101 == (101 << 16) | 1 and split_table_id(t203) == (203, 1)
              and room_of_table(t101) == 101 and room_of_table(1) == 0)
        check("table_label reads as a room and a table",
              table_label(t101) == "table 1 (room 101)"
              and table_label(3) == "table 3")
        ra = reserve(t101, 8, "PS2Tester")
        rb = reserve(t203, 16, "DeckTest")
        check("member 8 in room 101 and member 16 in room 203 are BOTH "
              "masters of 'table 1'",
              ra[0] == RESERVE_MASTER and rb[0] == RESERVE_MASTER)
        check("the two tables hold different seat sets",
              [m for m, _s, _n, _p in seats_at(t101)] == [8]
              and [m for m, _s, _n, _p in seats_at(t203)] == [16]
              and seat_count(1) == 0)
        check("table_of names the member's OWN room's table",
              table_of(8) == t101 and table_of(16) == t203)
        check("master_of is per composite",
              master_of(t101) == 8 and master_of(t203) == 16
              and master_of(1) == 0)
        check("each room has its own sequence, at 1",
              sequence(101) == 1 and sequence(203) == 1 and sequence(0) == 0)
        d101, d203 = deltas_after(0, 101), deltas_after(0, 203)
        check("each room's delta names only ITS composite",
              d101 == [(1, t101)] and d203 == [(1, t203)])
        check("a client current in room 203 is silent there",
              deltas_after(1, 203) == [])
        check("room 0 (the unknown room) saw nothing", deltas_after(0, 0) == [])
        reserve(t203, 21, "guest")
        check("a guest in room 203 moves room 203's sequence only",
              sequence(203) == 2 and sequence(101) == 1
              and deltas_after(1, 203) == [(2, t203)])
        set_in_play(t101, 8)
        check("in-play is per composite",
              is_in_play(t101) and not is_in_play(t203) and not is_in_play(1))
        check("rooms_known lists the rooms with state",
              rooms_known() >= {101, 203})
        # resolve_table: the ONE rule the arms use
        check("resolve_table: registry room + wire id -> that room's table",
              resolve_table(8, 1, room=101) == t101
              and resolve_table(99, 2, room=203) == room_table_id(203, 2))
        check("resolve_table: no wire id -> the member's seat",
              resolve_table(8, 0, room=None) == t101
              and resolve_table(16, 0, room=101) == t203)
        check("resolve_table: no room, matching base -> the member's seat",
              resolve_table(16, 1) == t203)
        check("resolve_table: no room, other base -> the seated room's table",
              resolve_table(16, 3) == room_table_id(203, 3))
        check("resolve_table: nothing known -> room 0 (the v1 behaviour)",
              resolve_table(4242, 2) == 2 and resolve_table(4242, 0) == 0)
        check("resolve_table tolerates a composite as the wire id",
              resolve_table(4242, t101, room=203) == room_table_id(203, 1))
        check("in-play moved room 101's row, so its sequence is 2",
              sequence(101) == 2 and sequence(203) == 2)
        check("cancel in one room leaves the other's seat alone",
              cancel(t101, 8) == RESERVE_CANCEL and master_of(t203) == 16
              and seat_count(t203) == 2 and sequence(101) == 3
              and sequence(203) == 2)
        take_master_changes()

        # --- a v1 file (one seq, one delta list, bare ids) migrates to room 0
        _publish()
        v1 = {"tables": {"2": {"master": 44, "in_play": 0.0,
                               "seats": {"44": {"seat": 0, "name": "old",
                                                "at": time.time()}}}},
              "seq": 7, "deltas": [[7, 2]], "rows": {"2": [1, False]},
              "members": {}}
        with open(os.environ["POL_JAN_SEATS_FILE"], "w", encoding="utf-8") as f:
            json.dump(v1, f)
        _TABLES.clear(); _DELTAS.clear(); _ROWS.clear(); _SEQ.clear()
        _ADOPTED[0] = False; _OWNER[0] = False; _CACHE["mtime"] = -1.0
        check("a READER of a v1 file sees its seq as room 0's",
              sequence(0) == 7 and sequence(101) == 0 and seat_count(2) == 1)
        _adopt()
        check("a WRITER adopting a v1 file keeps the seat under room 0",
              table_of(44) == 2 and room_of_table(table_of(44)) == 0
              and _SEQ == {"0": 7} and _DELTAS == {"0": [[7, 2]]})
        check("...and the migrated room answers deltas as before",
              deltas_after(6, 0) == [(7, 2)] and deltas_after(7, 0) == [])
        reserve(t101, 8, "PS2Tester")
        check("a new room beside the migrated one starts its own sequence",
              sequence(101) == 1 and sequence(0) == 7)
        _publish()
        _TABLES.clear(); _DELTAS.clear(); _ROWS.clear(); _SEQ.clear()
        _OWNER[0] = False; _CACHE["mtime"] = -1.0
        check("a reader of the v2 file sees both rooms",
              sequence(0) == 7 and sequence(101) == 1 and seat_count(t101) == 1)
        _ADOPTED[0] = False

        # --- THE GALLERY (spectators) ------------------------------------
        _adopt()
        _seq_before = sequence(101)
        gr, gslot = gallery_join(t101, 77, "Watcher", polid=0x7777)
        check("a spectator joins a gallery: result 1 (Reserve) with slot 0",
              (gr, gslot) == (GALLEY_OK, 0))
        check("...it is NOT a seat: seat_count/seats_at/table_of unchanged",
              seat_count(t101) == 1 and 77 not in [m for m, *_ in seats_at(t101)]
              and table_of(77) == 0)
        check("...but gallery_of / gallery_at / table_or_gallery_of see it",
              gallery_of(77) == t101 and gallery_at(t101) == [(77, 0, "Watcher", 0x7777)]
              and table_or_gallery_of(77) == (t101, True)
              and table_or_gallery_of(8) == (t101, False)
              and gallery_slot(77) == 0)
        check("...and the PTL row did not move (no delta for a spectator)",
              sequence(101) == _seq_before)
        gr2, _ = gallery_join(t101, 78, "Second")
        check("a second spectator takes the next slot", gr2 == GALLEY_OK
              and gallery_slot(78) == 1)
        gr3, gslot3 = gallery_join(t101, 77, "Watcher")
        check("watching a table you already watch is 3 (ReserveDuplicate) "
              "with the old slot, and the stale entry is dropped",
              (gr3, gslot3) == (GALLEY_DUP, 0) and gallery_of(77) == 0)
        gr4, gslot4 = gallery_join(t101, 77, "Watcher")
        check("...so the NEXT Watch is a 1 again (slot 0 re-used)",
              (gr4, gslot4) == (GALLEY_OK, 0))
        gallery_join(t203, 77, "Watcher")
        check("watching another table MOVES the spectator (one table screen)",
              gallery_of(77) == t203 and 77 not in [m for m, *_ in gallery_at(t101)])
        check("MjGALLEYLEAVEREQ: 5 released, 6 nothing to release, and a "
              "wrong table id still finds the member",
              gallery_leave(t101, 77) == GALLEY_LEFT
              and gallery_leave(t203, 77) == GALLEY_LEFT_ERROR
              and gallery_leave(t203, 4242) == GALLEY_LEFT_ERROR)
        gallery_join(t101, 77, "Watcher")
        check("clear_gallery (GAMEEND) drops everyone at that table only",
              sorted(clear_gallery(t101)) == [77, 78] and gallery_at(t101) == []
              and clear_gallery(t101) == [])
        gallery_join(t101, 77, "Watcher")
        check("gallery_drop forgets a spectator wherever it is",
              gallery_drop(77) == t101 and gallery_of(77) == 0 and gallery_drop(77) == 0)
        gallery_join(t101, 77, "Watcher")
        _g = _TABLES[str(t101)]["gallery"]["77"]
        _g["at"] = time.time() - _seat_ttl() - 5
        check("a spectator ages out on the seat TTL...", gallery_of(77) == 0)
        touch(77)
        check("...and touch() renews it (its heartbeat is any line)",
              gallery_of(77) == t101)
        _publish()
        _TABLES.clear(); _DELTAS.clear(); _ROWS.clear(); _SEQ.clear()
        _OWNER[0] = False; _CACHE["mtime"] = -1.0
        check("the gallery crosses the container split (file v2)",
              gallery_of(77) == t101)
        _ADOPTED[0] = False
        _adopt()
        cancel(t101, 8)
        check("the last seat leaving empties the gallery too",
              gallery_at(t101) == [])
        # a spectator's TableInfoSub fetch resolves to the table it WATCHES
        reserve(t203, 8, "PS2Tester", polid=0x8)
        gallery_join(t203, 77, "Watcher")
        check("resolve_table anchors a SPECTATOR on the table it watches "
              "(no seat, no registry room, wire id 1 -> Room 2-3's table 1)",
              resolve_table(77, 1) == t203 and resolve_table(77, 0) == t203
              and resolve_table(77, 2) == room_table_id(203, 2)
              and resolve_table(4243, 1) == 1)
        gallery_drop(77)
        cancel(t203, 8)
        # the in-game channel name: room 0 keeps the proven wire, a room
        # qualifies it inside the 13-byte PTL_T_NAME field
        check("table_channel: room 0 = #MJS0T00n (byte-identical to the "
              "shared era)", table_channel(1) == "#MJS0T001"
              and table_channel(4) == "#MJS0T004")
        check("table_channel: a room qualifies the name (12 chars + NUL fit "
              "the 13-byte field) and two rooms' table 1 differ",
              table_channel(t101) == "#MJS0T101001"
              and table_channel(t203) == "#MJS0T203001"
              and len(table_channel(t101)) <= TABLE_CHAN_MAX
              and table_channel(t101) != table_channel(t203))
        check("table_channel: a room too wide for 3 digits is hexed, never "
              "overruns", len(table_channel(room_table_id(4095, 4))) <= TABLE_CHAN_MAX
              and table_channel(room_table_id(4095, 4)) == "#MJS0TFFF004")

        os.environ.pop("POL_JAN_SEATS_FILE", None)

    print("\n%s" % ("ALL OK" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
