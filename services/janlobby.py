#!/usr/bin/env python3
"""Janhourou's lobby lists, built from LIVE state instead of authored files.

WHAT THIS IS FOR, in one line: the zone list, the room list and the table/member
list are the three screens that tell a player who else is here, and until now all
three were hand-authored `.bin` files under `data/resources/` -- a fixed ladder of
2/4/6/8 players in rooms that were empty, and one hard-coded member row. This
module builds the same three blobs from the room registry, so the numbers are
true.

    b/g/ZL        the parlour list          -> `zone_list_blob`
    b/g/RL%03d    one parlour's rooms       -> `room_list_blob`
    b/g/PTL       one room's members+tables -> `ptl_blob`

────────────────────────────────────────────────────────────────────────────────
THE FINDING THAT MADE THIS THE RIGHT FIX -- read this before "fixing" the member
list anywhere else.

The lobby's **Member** screen (`MemberListMain`) does NOT render `MjNOTICEMEMBER`.
It renders `b/g/PTL`'s MEMBER block. Measured 2026-08-18 in `jan-c-full`, and it
is three functions deep with no inference in it:

    roommain.c:116  iVar5 = lobbysub__002b6f10(obj, 10)      <- ONE source object
    roommain.c:123  roomwin__0033bef0(iVar3, iVar5)          <- the TABLE screen
    roommain.c:124  roomwin__0033e240(iVar4, iVar5)          <- the MEMBER screen

    roomwin__ZoneListPage_0033e2c0 (the member rows):
        count = lobbysub__002b7740(src)          src+0x38  (or +0x48 when filtered)
        base  = lobbysub__002b7700(src, &n)      src+0x30
        row N is at base + page*0x420 + N*0x58
            ZoneName  <- rec + 0x28              the NAME
            ChrLv     <- rec + 0x1C              drawn as "Lv%2d"

and `src+0x30` is filled by `lobbysub__002b71e0` out of `cp__002fc388`, i.e. the
cp context's member array -- which is `b/g/PTL` `+0x50`, stride 0x58, exactly as
`tools/janptl.py` documents it.

`MjNOTICEMEMBER` lands somewhere else entirely: `malloc__002c67a0` copies it to
`0x00445ed0`, whose only readers are the TITLE screen's `ReturnRoom` panel
(`ReturnRoom__Name1_00364fc0`, four `Name1..Name4` labels) and the in-game seat
display. Both are real screens; neither is the lobby member list.

WARNING: So a member list that draws nothing is a PTL with no members, and no amount of
correct `MjNOTICEMEMBER` will populate it. The two are separate subsystems that
happen to both be called "the member list".

────────────────────────────────────────────────────────────────────────────────
THE ONE THING THAT IS STILL WRONG, AND IT IS NOT FIXABLE FROM HERE.

`b/g/PTL` IS FETCHED BEFORE THE CLIENT JOINS THE ROOM CHANNEL. Measured on the
console 2026-08-17 (`logs/lobby.log` + `logs/authserv.log`, same session):

    23:59:35.803   3:0 'b/g/PTL': serving 23028B
    23:59:36.391   JOIN #MJS0R011

-- the fetch happens inside `sqMgCpEnterRoom`'s state pump (states 10/11) and the
join is state 12. So at the moment we build the blob, the requester is not in any
room channel and the registry cannot say WHICH room they picked.

The room id IS on the wire: `sqMgCpEnterRoom(RgmNick, chan, domain, volume)`
stores its first argument in `DAT_003f1878` (`cp__002fcae8` prints it as
`RgmNick %08X%08X`), and `cp__002fc608` passes that same global as the fourth
argument of the read. `request_key()` below reads the field it lands in --
payload +0x08, the one `responders.py` had labelled "session id" -- and it does
vary per path per session, which is consistent with a key and not with a session
id. It is NOT yet confirmed to equal our own room id at +0x00 of the RL record,
because no capture pairs a known room with its key. `room_for_key()` therefore
LOOKS UP the key and returns None when it does not recognise it; the caller then
falls back to `ANY_ROOM` (everyone in any Jan room) and says so in the log. One
launch with two rooms visited settles it.

────────────────────────────────────────────────────────────────────────────────
LIVE MEMBERSHIP ARRIVES AT ROOM-ENTRY TIME ONLY.

`lobbysub__002b7160` re-polls the cp arrays every 0x78 frames (~2 s) but never
re-fetches the file, so this blob is a SNAPSHOT taken when the player walked in.
Someone arriving after them appears only if the server pushes an update record:
`cp__002fb560` drains a ring and applies any queued record whose own serial is
exactly `+0x40 + 1`. That message's wire format is unread, and it is the next
piece of this workstream.

────────────────────────────────────────────────────────────────────────────────
DUPLICATION, DELIBERATE. The three record layouts also live in
`tools/jan{zone,room,ptl}list.py`, which are CLI authoring tools and are not
shipped into the container (`services/Dockerfile` COPYs named files). Rather than
make the runtime depend on a directory it cannot see, the layout constants are
repeated here and `selftest()` CROSS-CHECKS them against those tools whenever it
can import them -- which it can in the repo, where the check actually matters.
A divergence is a test failure, not a silent wire bug.
"""
import os
import struct

try:
    # THE SEAT STORE (2026-09-02) -- reservations, masters and in-play marks,
    # written by the auth band (janhourou) and read here across the container
    # split via its /data file. Optional: absent, seats come from channel
    # occupancy alone, exactly as before.
    import janseats
except ImportError:                                             # pragma: no cover
    janseats = None

# --- b/g/ZL, from tools/janzonelist.py ---------------------------------------
ZL_TOTAL = 2120                 # cp__002fc740: mg__002f5c58(..., 0x848, ...)
ZL_HDR = 0x48
ZL_REC = 0x40
ZL_COUNT_OFF = 0x40
ZL_MAX = (ZL_TOTAL - ZL_HDR) // ZL_REC          # 32
ZL_F_PLAYERS = 0x00             # the row's "BodyCount"  (zoneselw.c: *puVar3)
ZL_F_ROOMS = 0x04               # the row's "RoomCount"  (zoneselw.c: puVar3[1])
ZL_F_GATE = 0x08                # non-zero or Copy_Zone_Information drops the row
ZL_F_NAME = 0x0C                # 32 bytes
ZL_F_HOST = 0x2C                # 16 bytes -- the host the client DIALS
ZL_F_ID = 0x3C                  # s8 -- the %03d of b/g/RL%03d

# --- b/g/RL%03d, from tools/janroomlist.py -----------------------------------
RL_TOTAL = 51272                # cp__002fc878: mg__002f5c58(..., 0xc848, ...)
RL_HDR = 0x48
RL_REC = 200
RL_COUNT_OFF = 0x40
RL_MAX = (RL_TOTAL - RL_HDR) // RL_REC          # 256
RL_F_ROOMID = 0x00              # u64 -- sqMgCpEnterRoom arg 1
RL_F_PLAYERS = 0x10             # u32 -- RoomPlayerNum / "BodyCount"
RL_F_TABLES = 0x18              # u32 -- TableNum, the room's TOTAL
RL_F_GATE = 0x20                # u32 -- must be exactly 1
RL_F_NAME = 0x28
RL_F_OPENTABLES = 0x86          # s8, biased by +0x20 -- the "Open Tables" column
RL_F_CHANNEL = 0xB8             # 13 bytes -- the room's IRC channel
RL_NAME_MAX = RL_F_OPENTABLES - RL_F_NAME       # 94
RL_CHANNEL_MAX = 0xC5 - RL_F_CHANNEL            # 13

# --- b/g/PTL, from tools/janptl.py -------------------------------------------
PTL_TOTAL = 49232               # cp__002fc608: mg__002f5c58(..., 0xc050, ...)
PTL_SERIAL_OFF = 0x40
PTL_MEMBER_COUNT_OFF = 0x44
PTL_TABLE_COUNT_OFF = 0x48
PTL_MEMBER_OFF = 0x0050
PTL_MEMBER_REC = 88             # 0x58, and 0x50 + 256*88 == 0x5850 exactly
PTL_TABLE_OFF = 0x5850
PTL_TABLE_REC = 104             # 0x68, and 0x5850 + 256*104 == 49232 exactly
PTL_MEMBER_MAX = 0x5810 // PTL_MEMBER_REC       # 256, the CONSUMER's array
PTL_TABLE_MAX = 0x1A00 // PTL_TABLE_REC         # 64
PTL_M_ID = 0x00                 # u64 -- non-zero or cp__002fc388 drops the row
#: u32 -- THE ID "VIEW PROFILE" SENDS (2026-09-04, finding 27). MemberMenu.c:
#: 294-298 copies the row to window+0x600 and calls
#: `sqMgPfcGetCharacterProfile(dest, *(u32*)(row+0x18), 0)` (`lwu` at
#: 0x00352528), which goes out as `<PG>(0x00000000XXXXXXXX,0)`. So this must be
#: the member's Jan CONTENT ID -- the key `_pfc_profile_reply` answers by --
#: or the popup for anybody else is blank. Left zero until now.
PTL_M_PROFILE_ID = 0x18
PTL_M_LEVEL = 0x1C              # u32 -- drawn as "Lv%2d"
PTL_M_NAME = 0x28               # 16 bytes -- the row's ZoneName widget
PTL_M_NAME_MAX = 0x38 - PTL_M_NAME              # 16
PTL_T_ID = 0x00
PTL_T_CAPACITY = 0x08
PTL_T_SEATED = 0x0C
PTL_T_GATE = 0x10               # u32 -- must be exactly 1
PTL_T_STATE = 0x14
PTL_T_NAME = 0x18               # 13 bytes -- non-zero first byte or it is dropped
PTL_T_NAME_MAX = 13
PTL_T_TEXT2 = 0x28              # 64 bytes, the 14-field parameter CSV
PTL_T_TEXT2_MAX = PTL_TABLE_REC - PTL_T_TEXT2   # 64

#: The sscanf format at JanHouRou.pex 0x00414e40, as (name, max digits). A value
#: wider than its conversion does not truncate -- it shifts every later field.
#:
#: KEY: FIELDS 6 AND 8 ARE THE MEMBERS PANEL (2026-09-04, finding 25). Parsed by
#: `lmenu__002b2770` into a struct where field 6 lands at +0x10 and field 8 at
#: +0x18, and BOTH member panels read exactly those two:
#:
#:     lmenu.c:837/852 + tablememberwindow.c:439/461   seat = struct+0x18
#:         -> that seat is drawn FIRST, under "Table Master"     (field 8)
#:     lmenu.c:840/854 + tablememberwindow.c:444/465
#:         (struct+0x10 >> seat) & 1  ->  "Ready" / "Away"        (field 6)
#:
#: so field 6 is a READY BITMASK (bit N = seat N is in the content, written in
#: DECIMAL, 0..15 fits `%2d`) and field 8 is the MASTER'S SEAT. We authored
#: both as 0, which drew everyone "Away" and made seat 0 the master. WARNING: The
#: panels parse the CSV at `b/g/MJSTableInfoSub` +0x2EC; the PTL row's own
#: +0x28 copy is read only for fields 4/5/9 (lobbysub.c:1465-1469). Both are
#: authored from the same producer so they cannot disagree.
PTL_CSV_FIELDS = [
    ("number", 5), ("info_key1", 5), ("info_key2", 3), ("f4", 3), ("sort", 3),
    ("filter_a", 1), ("ready_mask", 2), ("reserved_seat", 2), ("master_seat", 1),
    ("filter_b", 1), ("f11", 1), ("f12", 1), ("f13", 3), ("f14", 1),
]

#: `roomwin__LockIcon_0033c590`'s switch arms. Anything outside 0..7 makes the
#: client log "ありえないけどきちゃった…" and BLANK the row.
TABLE_STATES = {0: "open", 1: "setting up", 2: "recruiting", 3: "waiting",
                4: "ready", 5: "in play", 6: "finishing", 7: "closed"}

#: seated -> state. WARNING: THE OLD LADDER (0 open / 2 recruiting / 5 full) IS WHY
#: START GAME WAS GREYED FOR EVER (2026-09-02). The table menu's enable ladder
#: (TableMenu__Reserve_0034e120) enables GameStart iff the PTL table record's
#: +0x14 == 4, gates TableSetting/強制退場 on state in {2,3,4}, Watch on 5 --
#: and 4 was not in this function's image, so no table could ever start a game.
#: Worse, the client's copy of the record is a SNAPSHOT taken at room entry
#: (`lobbysub__002b7160` re-polls memory, never the file, and the serial+1
#: update record is unimplemented), so the state a master needs must already be
#: on the table BEFORE they reserve it. Hence the default ladder is now:
#:
#:     in play (janseats mark, or a full table)  -> 5   対局中
#:     anything joinable, seated or empty        -> 4   すぐゲームできます
#:
#: 4-with-a-seat-taken is the configuration PROVEN LIVE 2026-08-16/17 (the
#: authored fixture's READY_TABLE: reserve -> master menu -> Start Game -> a
#: hand was dealt and played). 4-while-empty is the one extrapolation: the
#: fixture era always had a seat marked. If a live client refuses to reserve an
#: empty state-4 table, POL_JAN_TABLE_STATES=legacy restores the old ladder in
#: one env flip (and POL_JAN_LOBBY_LIVE=0 still reverts to authored files
#: wholesale).
def table_state_for(seated, capacity=4, in_play=False):
    if os.environ.get("POL_JAN_TABLE_STATES", "ready").strip().lower() == "legacy":
        if seated <= 0:
            return 0                            # a free table
        if seated >= capacity:
            return 5                            # 対局中, in play
        return 2                                # 募集中, recruiting
    if in_play or seated >= capacity:
        return 5                                # 対局中 -- watchable, not joinable
    if seated <= 0:
        # WARNING: AN EMPTY TABLE IS **OPEN**, NOT READY. Serving 4 here was this
        # function's one acknowledged extrapolation, and it put every table in
        # a freshly-entered room into the state that means "reserved and ready
        # to start" -- seen live 2026-09-04: a room of untouched tables was
        # describing itself as ready.
        #
        # 4 is retained the moment anyone sits down, because Start Game is
        # gated on exactly `+0x14 == 4` (TableMenu__Reserve_0034e120) and that
        # configuration -- a seat taken, state 4 -- is the one PROVEN LIVE on
        # 2026-08-16/17. So the ladder is now: 0 nobody, 4 somebody, 5 playing.
        #
        # What SE served for the middle of that range is still unmeasured: the
        # enum's own name for 1..3 seated is 2 (募集中, recruiting), but 2 greys
        # Start Game, so the two cannot both be right and only 4 is measured.
        # `POL_JAN_TABLE_STATES=legacy` restores 0/2/5 in one env flip.
        return 0                                # 空席 -- nobody has reserved it
    return 4                                    # すぐゲームできます -- Start ungreys


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    if isinstance(s, bytes):
        raw = s
    else:
        raw = str(s).encode("cp932", "replace")
    return raw[:limit - 1].ljust(limit, b"\x00")


# --- the topology ------------------------------------------------------------
#
# Defaults reproduce the authored blobs byte-for-byte EXCEPT for the counts, so
# the only thing that changes on the wire when this module takes over is the
# thing it is meant to change. `POL_JAN_ZONES` overrides the shape:
#
#     POL_JAN_ZONES="1:Test Parlour 1:4,2:Test Parlour 2:4"   zone:name:rooms
#
JAN_HOST = os.environ.get("POL_JAN_WORLD_HOST", "gi003.pol.com")

#: WARNING: THESE NAMES ARE OURS, AND SE'S ARE NOT RECOVERABLE FROM ANYTHING WE HOLD.
#:
#: They used to read `Test Parlour 1`..`4`, and because this module is the LIVE
#: path (nothing sets POL_JAN_ZONES) that string was on a player's screen. A
#: fixture name that escapes onto the wire is a stopgap like any other, so it is
#: gone -- but replacing it with an invented Japanese parlour name would be
#: worse, because it would read as recovered content and nothing here is.
#:
#: WHAT WAS ACTUALLY LOOKED FOR, so nobody repeats it: zone names live in
#: `b/g/ZL` +0x0C and are SERVER content, so they died with SE's service --
#: the same was established for Tetra Master ("nothing in the client holds
#: them"). Confirmed for Janhourou too: `JanHouRou.pex` (1,747,456
#: bytes, decrypted) carries ONE cp932 荘 and it is inside the yakuman scoring
#: table (地和 / 国士無双 / 大三元), no 雀荘 name anywhere, and no katakana
#: ロビー. The module does not hold them.
#:
#: OK: THE ONE ROUTE THAT HAS WORKED: Tetra Master's real zone and room names --
#: `Mermaids' Dreamworld`, `Freewheeler Room 1`, `Novice Hall` -- were recovered
#: from PERIOD SCREENSHOTS of a live SE session, not from
#: any binary, and `1.b_g_RL000.bin` reproduces that list exactly today. If
#: Janhourou footage or screenshots ever turn up, that is where its parlour names
#: come from, and this table should be replaced from them rather than polished.
#:
#: Until then these are plainly numbered: true of what they are, claiming nothing
#: about what SE called them. `POL_JAN_ZONES="1:名前:4,..."` overrides the whole
#: table without a rebuild.
JAN_ZONE_DEFAULT = [(1, "Parlour 1", 4), (2, "Parlour 2", 4),
                    (3, "Parlour 3", 4), (4, "Parlour 4", 4)]
#: The four BASE tables every room owns -- (wire id, in-game channel). Each
#: `Room` turns these into its own four COMPOSITE ids (`Room.table_list()`),
#: so Room 1-1 table 1 and Room 2-3 table 1 are two tables in the seat store,
#: the rule store and `jangame.Manager.by_lobby` (audit finding 30, done
#: 2026-09-04).
#:
#: KEY: THE WIRE-ID DECISION: the PTL row's +0x00 stays the 16-bit BASE id.
#: The client echoes it in MjPLAYREQ `id8` (u64, could carry more) BUT its
#: `b/g/MJSTableInfoSub` subject packs the table into the TOP 16 BITS
#: (tablememberwindow.c:167 builds the key from the record's +0x00; measured
#: 0x0002_003d7c0054ab at table 2), so a composite would be truncated on the
#: way back. The ROOM is therefore never on the wire: `responders` resolves
#: it from the IRC registry -- the client holds ONE room channel at a time
#: (`sqMgCpEnterRoom` fills the single slot DAT_003f1884; roommain.c:146-176
#: waits for "IRC Room Part Complite" before the selector can pick another)
#: -- and `janseats.resolve_table` turns (asker, wire id) into the composite.
#: A single room's PTL is byte-for-byte what the shared set served.
#:
#: The in-game IRC channel is the row's NAME (+0x18, 13 bytes): the client
#: copies it at reserve time (TableMenuPopup ctx+0xb8 -> 0x4467e8) and JOINs
#: it at game start via `sqMgCpEnterTable`, and `lb__002f9f18` matches a
#: `TD` delta on it. It is OURS to author, and since 2026-09-04 it is
#: room-qualified -- `janseats.table_channel`: `#MJS0T<room:3><n:3>` for a
#: room, the bare `#MJS0T00n` below for room 0 -- so two rooms' table 1 no
#: longer share one chat channel (`f61599cf` had left it shared).
#: `table_seating` still attributes channel bodies to a composite through the
#: seat store (room 0's shared names, and any client on a stale row).
JAN_TABLES = [(1, "#MJS0T001"), (2, "#MJS0T002"),
              (3, "#MJS0T003"), (4, "#MJS0T004")]


def _rtid(room, base):
    """`janseats.room_table_id` without requiring janseats."""
    if janseats is not None:
        return janseats.room_table_id(room, base)
    room, base = int(room or 0), int(base or 0)
    return (room << 16) | (base & 0xFFFF) if room else base


def _split_tid(tid):
    if janseats is not None:
        return janseats.split_table_id(tid)
    tid = int(tid or 0)
    return tid >> 16, tid & 0xFFFF


def tables_for_room(room):
    """`[(composite id, channel), ...]` for a `Room`, a room id, or None/0
    (room 0 = the unknown-room set: the bare fixture, which is what a v1
    seat store's rows are)."""
    if room is None:
        rid, n = 0, len(JAN_TABLES)
    elif isinstance(room, Room):
        rid, n = room.id, room.tables
    else:
        rid, n = int(room or 0), len(JAN_TABLES)
    return [(_rtid(rid, t), table_channel(_rtid(rid, t), c))
            for t, c in JAN_TABLES[:n]]


def table_channel(tid, default=None):
    """The in-game channel NAME for a composite table id -- the PTL row's
    +0x18 (13 bytes), which is what the client copies at reserve time
    (TableMenuPopup ctx+0xb8 -> 0x4467e8) and hands to `sqMgCpEnterTable`
    at game start, and the name `lb__002f9f18` matches a `TD` delta on.
    We AUTHOR it, so it can be room-qualified: `janseats.table_channel`
    (room 0 keeps the bare `#MJS0T00n`). See janseats' TABLE CHANNEL banner."""
    if janseats is not None and hasattr(janseats, "table_channel"):
        return janseats.table_channel(tid)
    if default is not None:
        return default
    return "#MJS0T%03d" % _split_tid(tid)[1]

#: `room_for_key` returns this when the request's key names no room we know.
ANY_ROOM = "*"


class Room(object):
    __slots__ = ("zone", "index", "id", "name", "channel", "tables")

    def __init__(self, zone, index, tables=4):
        self.zone = zone
        self.index = index
        self.id = zone * 100 + index
        # Ours too, and for the same reason as the zone names above. SE's room
        # names in the twin game are things like `Freewheeler Room 1` and
        # `Novice Hall`; ours are numbered because we do not know Janhourou's.
        self.name = "Room %d-%d" % (zone, index)
        self.channel = "#MJS0R0%d%d" % (zone, index)
        self.tables = tables

    def table_list(self):
        """This room's tables: `[(composite id, in-game channel), ...]`."""
        return tables_for_room(self)

    def table_ids(self):
        return [t for t, _c in self.table_list()]


class Zone(object):
    __slots__ = ("id", "name", "rooms")

    def __init__(self, zone_id, name, n_rooms):
        self.id = zone_id
        self.name = name
        self.rooms = [Room(zone_id, i) for i in range(1, n_rooms + 1)]


def topology():
    """The zones and rooms this server offers. Cheap; call it per request."""
    env = os.environ.get("POL_JAN_ZONES", "").strip()
    if not env:
        return [Zone(z, n, r) for z, n, r in JAN_ZONE_DEFAULT]
    out = []
    for item in env.split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 3:
            continue
        try:
            out.append(Zone(int(parts[0], 0), ":".join(parts[1:-1]),
                            int(parts[-1], 0)))
        except ValueError:
            continue
    return out or [Zone(z, n, r) for z, n, r in JAN_ZONE_DEFAULT]


def rooms_of(zone_id):
    for z in topology():
        if z.id == int(zone_id):
            return z.rooms
    return []


def all_rooms():
    return [r for z in topology() for r in z.rooms]


def room_by_id(room_id):
    """The `Room` with this id (zone*100+index), or None (0 = no room)."""
    room_id = int(room_id or 0)
    if not room_id:
        return None
    for r in all_rooms():
        if r.id == room_id:
            return r
    return None


def member_room(live, member_id):
    """The Jan room `member_id` is JOINed to, per a `_live_rooms()` snapshot.

    THE ROOM-RESOLUTION RULE (finding 30): the client holds exactly one room
    channel -- `sqMgCpEnterRoom` writes the single slot DAT_003f1884 and
    roommain.c:146-176 spins on "IRC Room Part Complite" before the selector
    can pick another -- so the one `#MJS0R0zi` the registry has them in IS
    the room of every table request they make. None when they are in no Jan
    room (the ~0.6 s between a room's `b/g/PTL` fetch and its JOIN, or a
    session the registry has no member for) or, defensively, in more than
    one (a registry ghost), since guessing would seat them in the wrong room.
    """
    member_id = int(member_id or 0)
    if not member_id:
        return None
    hits = []
    for chan, entry in (live or {}).items():
        if not chan.startswith("#MJS0R"):
            continue
        for w in (entry or {}).get("who") or []:
            if isinstance(w, dict) and int(w.get("member_id") or 0) == member_id:
                hits.append(chan)
                break
    if len(hits) != 1:
        return None
    for r in all_rooms():
        if r.channel == hits[0]:
            return r
    return None


def member_room_id(live, member_id):
    """`member_room(...).id`, or 0."""
    r = member_room(live, member_id)
    return r.id if r is not None else 0


# --- THE ROOM KEY, LEARNED RATHER THAN REVERSED ------------------------------
#
# `b/g/PTL` carries a u64 at +0x30 that varies per room -- measured
# 2026-09-04: 0x3d7c0054cc and 0x3d7c005584 from two different rooms. It is NOT
# our authored room id, NOT `(room_id ^ K) & 0xFF_FFFF_FFFF` (Tetra Master's id
# key -- tested, no match) and NOT the room peer nick folded (tested). Four
# samples of an unknown encoding cannot settle what it is, and inventing a
# formula for it is how this project's ledger got long.
#
# SO DO NOT DECODE IT -- OBSERVE IT. The client fetches `b/g/PTL` about 0.6 s
# BEFORE it JOINs the room's IRC channel, so the pair (key, channel) is
# available to anyone willing to wait one beat:
#
#   1. the fetch arrives with key K from member M      -> remember (M, K)
#   2. ~0.6 s later M JOINs `#MJS0R0xx`, and the room registry says so
#   3. any later Jan fetch reconciles: M is in exactly one Jan channel and
#      holds an unbound key, so K names that room. Bind it, persist it.
#
# WARNING: THE TWO HALVES ARE IN DIFFERENT CONTAINERS -- the fetch is served by
# `login`, the JOIN handled by `authsess` -- which is why the binding is done
# lazily on the LOGIN side against the published registry, rather than at the
# JOIN itself. Nothing has to cross the split in the write direction.
#
# The map is a cache of an OBSERVATION, so a wrong entry self-corrects the next
# time that room is entered, and an empty map degrades to exactly today's
# behaviour (ANY_ROOM, every Jan room's occupants).
_KEYMAP = {}                    # "key" -> channel
_KEY_PENDING = {}               # member id -> (key, first seen)
_KEYMAP_LOADED = [False]
KEY_BIND_WINDOW_S = 120.0


def _keymap_path():
    return os.environ.get(
        "POL_JAN_ROOMKEY_FILE",
        os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                     "jan-room-keys.json"))


def _keymap_load():
    if _KEYMAP_LOADED[0]:
        return
    _KEYMAP_LOADED[0] = True
    try:
        import json
        with open(_keymap_path(), "r", encoding="utf-8") as f:
            for k, v in (json.load(f) or {}).items():
                _KEYMAP[str(k)] = str(v)
    except (OSError, ValueError):
        pass


def _keymap_save():
    try:
        import json
        path = _keymap_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_KEYMAP, f)
        os.replace(tmp, path)
    except OSError:
        pass


def note_room_fetch(member_id, key):
    """A `b/g/PTL` fetch arrived. Remember who asked, so the JOIN can name it."""
    if not key or not member_id:
        return
    _keymap_load()
    if str(key) in _KEYMAP:
        return                              # already known
    import time as _t
    _KEY_PENDING[int(member_id)] = (int(key), _t.time())


def reconcile_room_keys(live):
    """Bind any pending key to the room its asker has since JOINed.

    Returns the list of (key, channel) bound by this call -- empty is the
    normal case. Cheap: it only looks at members with something pending.
    """
    if not _KEY_PENDING:
        return []
    _keymap_load()
    import time as _t
    now = _t.time()
    bound = []
    for member in list(_KEY_PENDING):
        key, seen = _KEY_PENDING[member]
        if now - seen > KEY_BIND_WINDOW_S:
            _KEY_PENDING.pop(member, None)
            continue
        chans = [c for c, e in (live or {}).items()
                 if c.startswith("#MJS0R")
                 and any((w or {}).get("member_id") == member
                         for w in (e.get("who") or []))]
        if len(chans) != 1:
            continue                        # ambiguous or not joined yet
        _KEY_PENDING.pop(member, None)
        if _KEYMAP.get(str(key)) == chans[0]:
            continue
        _KEYMAP[str(key)] = chans[0]
        bound.append((key, chans[0]))
    if bound:
        _keymap_save()
    return bound


def learned_rooms():
    """The map as it stands, for logging."""
    _keymap_load()
    return dict(_KEYMAP)


def room_for_key(key):
    """The room whose id is `key`, or None.

    `key` is the u64 the client sends at 3:0 payload +0x08 -- `sqMgCpEnterRoom`'s
    RgmNick, which we PUT THERE ourselves as the RL record's +0x00. That the
    field carries it is inference (see the module note); returning None rather
    than guessing is what keeps a wrong inference from inventing a room.
    """
    if not key:
        return None
    # THE LEARNED MAP FIRST. It is an observation; the id compare below is an
    # inference that has never once matched a live key.
    _keymap_load()
    chan = _KEYMAP.get(str(key))
    if chan:
        for r in all_rooms():
            if r.channel == chan:
                return r
    for r in all_rooms():
        if r.id == key:
            return r
    return None


# --- occupancy ---------------------------------------------------------------

class Occupant(object):
    """One person in a channel, as the lists want them.

    `profile_id` is the Jan CONTENT ID (what `<PG>` names) for the row's
    +0x18; 0 leaves View Profile on this member blank, which is the truth
    when we hold no Content ID for them."""
    __slots__ = ("name", "member_id", "level", "profile_id")

    def __init__(self, name="", member_id=0, level=1, profile_id=0):
        self.name = name or ""
        self.member_id = int(member_id or 0)
        self.level = int(level or 1)
        self.profile_id = int(profile_id or 0)

    def __repr__(self):
        return "<Occupant %r member=%d>" % (self.name, self.member_id)


def occupants(live, channel):
    """`[Occupant]` for one channel, from a `_live_rooms()`-shaped snapshot.

    Accepts both snapshot generations. The older one published only
    `{"members": n}` -- a count with no identity -- and this returns n NAMELESS
    occupants for it rather than inventing names: a blank row is a person we
    cannot name, which is true, where "Player 2" would be a fiction.
    """
    entry = (live or {}).get(channel)
    if not entry:
        return []
    who = entry.get("who")
    if isinstance(who, list):
        return [Occupant(w.get("name"), w.get("member_id"), w.get("level", 1))
                for w in who if isinstance(w, dict)]
    return [Occupant() for _ in range(int(entry.get("members", 0) or 0))]


def zone_players(live, zone):
    """Everyone in any of a zone's rooms. One person is in one room, so this is
    a sum and not a union."""
    return sum(len(occupants(live, r.channel)) for r in zone.rooms)


def tables_in_use(live, room=None):
    """How many of `room`'s tables have somebody sitting at them (a body in
    the table channel attributed to it, or a reservation in the seat store).
    `room` None = the unknown-room set, exactly the old shared count."""
    return sum(1 for tid, chan in tables_for_room(room)
               if table_seating(tid, chan, live)[0])


# --- the three blobs ---------------------------------------------------------

def zone_list_blob(live, host=None):
    """`b/g/ZL` with each parlour's real headcount."""
    zones = topology()
    if len(zones) > ZL_MAX:
        raise ValueError("%d zones; the buffer holds %d" % (len(zones), ZL_MAX))
    buf = bytearray(ZL_TOTAL)
    struct.pack_into("<I", buf, ZL_COUNT_OFF, len(zones))
    for i, z in enumerate(zones):
        o = ZL_HDR + i * ZL_REC
        struct.pack_into("<I", buf, o + ZL_F_PLAYERS, zone_players(live, z))
        struct.pack_into("<I", buf, o + ZL_F_ROOMS, len(z.rooms))
        # +0x08 is the gate and nothing else about it is known; 1 is what the
        # authored blobs carry and what the client accepts.
        struct.pack_into("<I", buf, o + ZL_F_GATE, 1)
        buf[o + ZL_F_NAME:o + ZL_F_HOST] = _text(z.name, ZL_F_HOST - ZL_F_NAME)
        buf[o + ZL_F_HOST:o + ZL_F_ID] = _text(host or JAN_HOST,
                                               ZL_F_ID - ZL_F_HOST)
        buf[o + ZL_F_ID] = z.id & 0x7F
    return bytes(buf)


def room_list_blob(zone_id, live):
    """`b/g/RL%03d` for one parlour, with each room's real headcount.

    WARNING: NEVER RETURNS AN EMPTY LIST. `lobbysub__002b6210` leaves its caller's row
    count unwritten on an empty list and `zonewin__ZoneListPage_003401f0` then
    loops on an uninitialised stack slot with a NULL row pointer.
    """
    rooms = rooms_of(zone_id)
    if not rooms:
        return None
    buf = bytearray(RL_TOTAL)
    struct.pack_into("<I", buf, RL_COUNT_OFF, len(rooms))
    for i, r in enumerate(rooms):
        o = RL_HDR + i * RL_REC
        # PER ROOM (finding 30): this room's four composites, so a table taken
        # in Room 2-3 no longer reads as taken in Room 1-1.
        free = max(0, min(r.tables, r.tables - tables_in_use(live, r)))
        struct.pack_into("<Q", buf, o + RL_F_ROOMID, r.id)
        struct.pack_into("<I", buf, o + RL_F_PLAYERS,
                         len(occupants(live, r.channel)))
        struct.pack_into("<I", buf, o + RL_F_TABLES, r.tables)
        struct.pack_into("<I", buf, o + RL_F_GATE, 1)
        buf[o + RL_F_NAME:o + RL_F_NAME + RL_NAME_MAX] = _text(r.name,
                                                               RL_NAME_MAX)
        buf[o + RL_F_OPENTABLES] = (0x20 + free) & 0xFF
        buf[o + RL_F_CHANNEL:o + RL_F_CHANNEL + RL_CHANNEL_MAX] = \
            _text(r.channel, RL_CHANNEL_MAX)
    return bytes(buf)


def _table_params_text(**over):
    """The 14-field CSV for a table record's +0x28, as TEXT.

    The delta carries this as a polpro VALUE, so it must not be NUL-padded --
    and it may contain commas freely: `lb__002f9e10`'s field splitter
    (`mg__002f8380`) separates on 0x06/0x07, the polpro value and group
    terminators. Ghidra names it "There_is_no_comma_separated" after an error
    string; the code never looks at a comma.
    """
    out = []
    for name, width in PTL_CSV_FIELDS:
        text = "%d" % int(over.get(name, 0))
        if len(text) > width:
            raise ValueError("%s = %s needs %d digits but its conversion is "
                             "%%%dd; sscanf would shift every later field"
                             % (name, text, len(text), width))
        out.append(text)
    s = ",".join(out)
    if len(s.encode("ascii")) >= PTL_T_TEXT2_MAX:
        raise ValueError("the CSV is %d bytes and +0x28 is %d with no room for "
                         "a terminator"
                         % (len(s.encode("ascii")), PTL_T_TEXT2_MAX))
    return s


# --- ONE TABLE ROW, TWO CONSUMERS -------------------------------------------
#
# The snapshot (`ptl_blob`) and the `<DD>` delta must never disagree about a
# row, so both are built from `table_values()` and only the last step differs:
# the snapshot runs it through `encode_table`, the delta sends the seven values
# as a `TD` group and lets the CLIENT run `lb__002f9e10` -- the same encoding,
# read from the other end.
#
# The mapping is `lb__002f9e10` field by field. SEVEN POLPRO VALUES:
#
#     0  13 bytes -> param_1+3    = +0x18  name (and lb__002f9f18 MATCHES ON IT)
#     1  u32      -> param_1+2    = +0x10  gate
#     2  lb__002f8d38 -> *param_1 = +0x00  id (hex text, "0x%016X")
#     3  u8       -> +0x14                 state
#     4  u32      -> param_1+1    = +0x08  capacity
#     5  u32      -> +0x0c                 seated
#     6  64 bytes -> param_1+5    = +0x28  the parameter CSV
#
# Every offset is one this module already authors, which is the check that it
# is the same record and not a lookalike. `TD` is polpro tag index 21 == 0x15,
# and 0x15 is the arm of `cp__002fb290` that does this write -- that is what
# ties the tag to the handler.
_TABLE_FIELDS = (
    (0, PTL_T_NAME, "s13"), (1, PTL_T_GATE, "u32"), (2, PTL_T_ID, "u64"),
    (3, PTL_T_STATE, "u8"), (4, PTL_T_CAPACITY, "u32"),
    (5, PTL_T_SEATED, "u32"), (6, PTL_T_TEXT2, "s64"),
)
NTABLEVALUES = 7


def _as_int(v):
    """`0x...` or decimal, the way the client's own parsers take it."""
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return 0


def encode_table(values):
    """The seven `TD` values -> the 104-byte table record, per `lb__002f9e10`."""
    vals = list(values) + [""] * (NTABLEVALUES - len(values))
    rec = bytearray(PTL_TABLE_REC)
    for idx, off, kind in _TABLE_FIELDS:
        v = vals[idx]
        if kind == "u64":
            struct.pack_into("<Q", rec, off, _as_int(v) & 0xFFFFFFFFFFFFFFFF)
        elif kind == "u32":
            struct.pack_into("<I", rec, off, _as_int(v) & 0xFFFFFFFF)
        elif kind == "u8":
            rec[off] = _as_int(v) & 0xFF
        else:
            n = 13 if kind == "s13" else 64
            rec[off:off + n] = _text(v, n)
    return bytes(rec)


def table_seating(tid, chan, live=None):
    """(seated, in_play) for one table -- the union of two facts.

    Bodies in the table's IRC channel (in-game members; joining happens at game
    start) and RESERVATIONS from the seat store. Reserving never joins the
    channel, so before the store existed a lone master always read as 0.

    `tid` is the COMPOSITE id and `chan` the channel the room's PTL row
    names for it (`table_channel`: room-qualified since 2026-09-04, bare
    `#MJS0T00n` for room 0). With the seat store on, a body in it counts
    toward THIS composite only if the store seats them here -- which also
    keeps a SPECTATOR (it JOINs the table channel at entry, spec section
    1.5, and holds no seat) out of the seated count. Store off = one shared
    set (room 0), where every body counts as before.
    """
    bodies = occupants(live, chan)
    if janseats is not None and janseats.enabled():
        seated_here = {m for m, _s, _n, _p in janseats.seats_at(tid)}
        bodies = [o for o in bodies if o.member_id in seated_here]
        seated = max(len(bodies), len(seated_here))
        in_play = seated >= 4 or janseats.is_in_play(tid)
    else:
        seated = len(bodies)
        in_play = seated >= 4
    return max(0, min(4, seated)), in_play


def seat_marks(tid):
    """(ready bitmask, master seat) for the CSV, from the seat store.

    Ready = holds a live (unexpired) seat, i.e. is talking to us from inside
    the title -- `lmenu.c`'s own caption for the bit is "In content" /
    "Outside content". No store: nobody is ready and seat 0 is the master,
    which is what an authored fixture carried.
    """
    if janseats is None or not janseats.enabled():
        return 0, 0
    mask = 0
    for _m, st, _nm, _pid in janseats.seats_at(tid):
        if 0 <= st < 4:
            mask |= 1 << st
    ms = janseats.master_seat_of(tid)
    return mask, int(ms) if ms is not None else 0


def table_values(index, tid, chan, live=None):
    """The seven `TD` values for one table, as both consumers want them.

    `tid` is the composite; field 2 (the row's +0x00) carries the BASE id,
    the only thing the client can echo back -- see the JAN_TABLES banner.
    """
    seated, in_play = table_seating(tid, chan, live)
    return [chan, "1", "0x%016X" % _split_tid(tid)[1],
            str(table_state_for(seated, in_play=in_play)),
            "4", str(seated),
            table_params_text_for(tid, index=index)]


def blob_serial(room=0):
    """What `ptl_blob`'s `+0x40` must carry for a PTL of `room` (a `Room`,
    a room id, or 0/None for the unknown-room set).

    THE RULE, AND IT HAS ONE TRAP IN IT: stamp the sequence the seat store has
    actually issued, with NO FLOOR. `max(1, seq)` reads as harmless defensive
    tidying and is a permanent reload loop -- at sequence 0 the blob claims 1,
    the client reports 1, `deltas_after` sees a client AHEAD of the server and
    can only answer `<DO>`, and the reload restamps it 1 again. For ever.

    With the seat store off there is no sequence and nothing answers `<DR>`, so
    the old constant is kept: the wire is then byte-for-byte what it was.
    """
    if janseats is None or not janseats.enabled():
        return 1
    rid = room.id if isinstance(room, Room) else int(room or 0)
    return janseats.sequence(rid)


def table_params_text_for(tid, index=None):
    """The parameter CSV for one table -- field 0 is the number the member
    panel prints as `Table %02d members`, field 6 the Ready bitmask and
    field 8 the master's seat (see PTL_CSV_FIELDS). ONE producer for the PTL
    row, the `TD` delta and the `b/g/MJSTableInfoSub` +0x2EC copy. `tid` is
    the composite; the number and key are the BASE id's."""
    base = _split_tid(tid)[1]
    if index is None:
        index = next((i for i, (t, _c) in enumerate(JAN_TABLES) if t == base),
                     None)
        if index is None:
            return None
    ready, master = seat_marks(tid)
    return _table_params_text(number=index + 1, info_key1=base & 0xFFFF,
                              sort=index + 1, ready_mask=ready,
                              master_seat=master)


def table_values_for(tid, live=None):
    """The same, addressed by COMPOSITE table id -- what a delta for `tid`
    sends. None for a base id that is not one of the four."""
    base = _split_tid(tid)[1]
    for i, (t, chan) in enumerate(JAN_TABLES):
        if t == base:
            return table_values(i, tid, table_channel(tid, chan), live)
    return None


#: The member record's id must be non-zero (`cp__002fc388` reads 0 as an empty
#: slot). The high nibble keeps a synthesised id clearly distinguishable from a
#: real POL id in a capture -- the same convention `jangame.Table.BOT_ID_BASE`
#: uses for a bot seat.
PTL_MEMBER_ID_BASE = 0x1000000000000000


def member_id_for(occ, index):
    return PTL_MEMBER_ID_BASE | (occ.member_id or (index + 1))


def ptl_blob(members, live=None, serial=1, room=None):
    """`b/g/PTL` -- `members` is the room's `[Occupant]`, tables are `room`'s
    four composites (a `Room`, a room id, or None for the unknown-room set)
    with their seat counts from the seat store and the live table channels.
    On the wire a room's PTL is byte-for-byte the old shared one: the row
    ids are the base ids, only the seats behind them are the room's.

    WARNING: A PTL WITH NO TABLE HANGS THE ROOM SCREEN FOREVER: `roommain` spins on
    `lobbysub__002b7530`, which returns the table count, and the client asks us
    for nothing while it does. The fixture always has four.
    """
    if len(members) > PTL_MEMBER_MAX:
        members = members[:PTL_MEMBER_MAX]
    tables = tables_for_room(room)[:PTL_TABLE_MAX]
    if not tables:
        raise ValueError("a PTL with no tables hangs TableSelectMain forever")
    buf = bytearray(PTL_TOTAL)
    struct.pack_into("<I", buf, PTL_SERIAL_OFF, serial)
    struct.pack_into("<i", buf, PTL_MEMBER_COUNT_OFF, len(members))
    struct.pack_into("<i", buf, PTL_TABLE_COUNT_OFF, len(tables))
    for i, m in enumerate(members):
        o = PTL_MEMBER_OFF + i * PTL_MEMBER_REC
        struct.pack_into("<Q", buf, o + PTL_M_ID, member_id_for(m, i))
        struct.pack_into("<I", buf, o + PTL_M_PROFILE_ID,
                         getattr(m, "profile_id", 0) & 0xFFFFFFFF)
        struct.pack_into("<I", buf, o + PTL_M_LEVEL, m.level)
        buf[o + PTL_M_NAME:o + PTL_M_NAME + PTL_M_NAME_MAX] = \
            _text(m.name, PTL_M_NAME_MAX)
    for i, (tid, chan) in enumerate(tables):
        o = PTL_TABLE_OFF + i * PTL_TABLE_REC
        buf[o:o + PTL_TABLE_REC] = encode_table(
            table_values(i, tid, chan, live))
    return bytes(buf)


# --- b/g/MJSTableInfoSub: the 33 mahjong rule settings -----------------------
#
# The table-rules screen (麻雀ルール設定 / テーブルルール設定) reads 33 twelve-byte
# records at blob +0x20 (reader `ruleset__003473b0`): {flags, config index,
# current, default, legal-value mask lo (values 0-31), mask hi (32-63)}. The value
# LABELS come from `ruleset__003476a0`, which indexes a per-rule pointer array
# (table @ JanHouRou.pex 0x0040be50) by the SET BIT POSITION **with no bounds
# check** -- so a mask must cover ONLY the rule's real labelled values; a bit on a
# padding slot copies a null string pointer and draws blank/garbage.
#
# WARNING: ALL-ZERO IS THE BUG SEEN LIVE (2026-09-02): every record's config
# index reads 0, so all 33 rows draw as GENTEN ("Start points"), and every mask is
# empty, so `SelectItems == 0` and opening any row crashes on an empty value list.
# A real blob fixes both. `tools/jantableinfo.py` is the CLI twin (its LABEL_COUNTS
# are the padded array-gap counts; the counts here are the REAL labelled counts
# measured off 0x40be50 on 2026-09-02, which is what a correct mask needs).
MJS_TI_TOTAL = 816              # lfile__002ae000(..., 0x330)
MJS_TI_REC_OFF = 0x20
MJS_TI_REC = 12
MJS_TI_COUNT = 33               # fixed loop `while (i < 0x21)` -- no count field
MJS_TI_ROW_SLOTS = 40           # per-dialog row array; the cursor is unchecked

#: The 33 rule names in module order -- the list index IS the config index.
MJS_RULE_NAMES = [
    "GENTEN", "RENCHAN", "DORA", "AKA_NUM1", "AKA_NUM2", "AKA_NUM3", "AKA_KIND",
    "DOUBLE_RON", "WAREME", "HAKO", "KIRIAGE", "YAKITORI", "IPPATSU", "KUITAN",
    "NOTEN", "PINZUMO", "KYOKUSU", "UMA", "REACH_AFTER_KAN", "TABLE_COMMENT",
    "GALLEY_LIMIT", "WAIT_TIME", "DISCONNECT_MODE", "DISCONNECT_ADJUST",
    "DISCONNECT_PENALTY", "LIMIT_PASS_WORD", "LIMIT_MONEY", "LIMIT_LEVEL",
    "LIMIT_SYOGO1", "LIMIT_SYOGO2", "LIMIT_SYOGO3", "LIMIT_SYOGO4", "LIMIT_SYOGO5",
]

#: config index -> number of REAL (labelled) values. Measured 2026-09-02 from the
#: 0x40be50 value-label arrays (non-NULL pointers only). mask = (1<<count)-1, and
#: every count is <= 11 so the mask stays in the low word (avoids SE's high-word
#: low-5-bits quirk). Min is 2, so no mask is ever empty -> nothing crashes.
MJS_RULE_VALUE_COUNT = {
    "GENTEN": 6, "RENCHAN": 6, "DORA": 5, "AKA_NUM1": 5, "AKA_NUM2": 5,
    "AKA_NUM3": 5, "AKA_KIND": 9, "DOUBLE_RON": 2, "WAREME": 2, "HAKO": 2,
    "KIRIAGE": 2, "YAKITORI": 4, "IPPATSU": 2, "KUITAN": 2, "NOTEN": 11,
    "PINZUMO": 2, "KYOKUSU": 8, "UMA": 6, "REACH_AFTER_KAN": 2,
    "TABLE_COMMENT": 8, "GALLEY_LIMIT": 3, "WAIT_TIME": 6, "DISCONNECT_MODE": 3,
    "DISCONNECT_ADJUST": 2, "DISCONNECT_PENALTY": 4, "LIMIT_PASS_WORD": 2,
    "LIMIT_MONEY": 2, "LIMIT_LEVEL": 2, "LIMIT_SYOGO1": 2, "LIMIT_SYOGO2": 2,
    "LIMIT_SYOGO3": 2, "LIMIT_SYOGO4": 2, "LIMIT_SYOGO5": 2,
}

#: Rules 19-32 render on the SECOND dialog (テーブルルール設定); flags&1 selects it.
#: 0-18 are the mahjong rules on the first (麻雀ルール設定). INFERENCE from SE's
#: names + the two dialog titles (nothing in the module states it); live 08-17 all
#: on dialog A gave 4 tabs and B blank, so this split is what makes B populate.
MJS_TI_DIALOG_B = set(MJS_RULE_NAMES[19:])

#: GUESSED sensible defaults -- the client ships NONE (current/default are read
#: straight from the blob). WAIT_TIME is NOT cosmetic: value 0 is the short end of
#: the turn clock and a low clock auto-advances a hand (see jangame's janclock
#: note), so it is defaulted to the far end. The rest are ordinary table defaults.
# WARNING: WAIT_TIME IS COUPLED TO `jangame.Table.deadline()` -- DO NOT RAISE IT ALONE.
# The served byte is the CLIENT's turn clock; our own sweeper plays a default for
# a seat that says nothing after `Rules.wait_time + TURN_GRACE`. The sweeper must
# outlast the client, or we auto-discard for a player who is still thinking and
# can see time left on their own clock (the a43804ca regression, again).
# 2 -> 30 s, which is `janmahjong.Rules.wait_time`, against a 45 s deadline.
#
# WARNING: EVERY VALUE HERE IS THE ENGINE'S DEFAULT, BY INDEX INTO THE LABEL TABLE
# (JanHouRou.pex 0x40be50, dumped 2026-09-04 -- see janrules.py's banner).
# Until then this said `UMA: 4`, which is the "20-60" label, while
# `janmahjong.Rules().uma` paid 10-20 (index 2): the dialog showed one thing
# and the table paid another (audit finding 2). `janrules.selftest` now
# asserts `rules_kwargs(default_values())` == `Rules()` field by field.
#
#   GENTEN 3   = 25000          RENCHAN 3  = E:tenpai S:tenpai (renchan=True)
#   DORA 3     = All+Kan (ura + kan dora)    AKA_NUM1..3 1 = one red per suit
#   AKA_KIND 4 = the 5          DOUBLE_RON 1, HAKO 1, KUITAN 1 = Yes
#   KYOKUSU 7  = Until South 4 (hanchan)     UMA 2 = 10-20
#   IPPATSU 1, NOTEN 1 (3000 on table), PINZUMO 1 (with pinfu),
#   REACH_AFTER_KAN 1 -- what the engine plays; no Rules field yet
#   DISCONNECT_MODE 0 = Proxy (a dropped seat is played by a bot)
MJS_RULE_DEFAULT = {"GENTEN": 3, "RENCHAN": 3, "DORA": 3, "AKA_NUM1": 1,
                    "AKA_NUM2": 1, "AKA_NUM3": 1, "AKA_KIND": 4,
                    "DOUBLE_RON": 1, "HAKO": 1, "IPPATSU": 1, "KUITAN": 1,
                    "NOTEN": 1, "PINZUMO": 1, "KYOKUSU": 7, "UMA": 2,
                    "REACH_AFTER_KAN": 1, "WAIT_TIME": 2}


#: WARNING: `b/g/MJSTableInfoSub` IS THE TABLE MEMBER RECORD, not just the rules.
#: The member panel does NOT read MjNOTICEMEMBER -- `tablememberwindow` fetches
#: THIS FILE (`lfile__002ae000(obj, key, "b/g/MJSTableInfoSub", 0x330)`) and
#: draws Name1..Name4 out of it. The layout is the client's OWN debug prints in
#: `tablememberwindow__00351320`, which name each field as an offset from the
#: copy base, so this is read rather than inferred:
#:
#:     &TitleSetPOLpro.PolID[0] = 0     +0x00   the four seats' PolIDs
#:     &TitleSetPOLpro.PolID[1] = 8     +0x08
#:     &TitleSetPOLpro.PolID[2] = 16    +0x10
#:     &TitleSetPOLpro.PolID[3] = 24    +0x18
#:     &TitleSetPOLpro.Config   = 32    +0x20   the 33 rule records
#:     &TitleSetPOLpro.Rule     = 428   +0x1AC
#:     &TitleSetPOLpro.TableRule= 456   +0x1C8
#:     &TitleSetPOLpro.FaceType[0..3]   +0x208..+0x214
#:                                      +0x22C  4 x 16-byte NAMES
#:                                      +0x2EC  the parameter CSV
#:
#: We authored the rules and NOTHING ELSE, which is exactly the two symptoms
#: reported live 2026-09-04: every member row blank (the PolIDs were zero) and
#: the window titled "Table 00 members" -- `Table %02d members` formatted with
#: field 0 of the +0x2EC CSV, which was also zero.
MJS_TI_POLID = 0x00             # 4 x u64, one per seat
MJS_TI_NAMES = 0x22C            # 4 x 16 bytes, seat order
MJS_TI_NAME_SLOT = 16
MJS_TI_PARAMS = 0x2EC           # the same 14-field CSV as the PTL row's +0x28

#: KEY: THE TURN CLOCK IS A FIELD IN THIS BLOB, AND WE HAD NEVER WRITTEN IT.
#: The client copies all 816 bytes to `0x00445990` (`lfile__002ae000(...,
#: "b/g/MJSTableInfoSub", 0x330)` then the delivery memcpy at
#: `0x002ae380(handle, 0x445990)`), so every blob offset has a fixed EE address.
#: `mahdisp__002c21a0` -- the LimitTimeManager thread -- reads its per-turn limit
#: with `lbu v1, 0x00445b87` and skips the pick when `limit*60 <= ticks`:
#:
#:     0x00445b87 - 0x00445990 = +0x1F7 = 503 = TableRule + 47
#:
#: which is inside `TitleSetPOLpro.TableRule` (+0x1C8, 64 bytes, running to
#: `FaceType[0]` at +0x208) -- the client's own debug prints name both. The value
#: is SECONDS: config index 21 is "Tsumo timer" and its six labels off 0x40be50
#: are 10/20/30/40/50/60 sec, so seconds = (value + 1) * 10.
#:
#: WARNING: This RETRACTS "nothing writes DAT_00445b87, so it is an uninitialised read
#: the console supplied by luck". No CPU store writes it because the whole blob
#: arrives by DMA -- and the SAVE region (0x00446100, known-good server data)
#: shows the same signature: 61 loads, 2 stores. "Read from many places, written
#: by none" is what a server-supplied blob looks like, not what a dead global
#: looks like. `polrun.py --patches janclock` was treating the symptom.
MJS_TI_RULE = 0x1AC             # TitleSetPOLpro.Rule       (28 bytes)
MJS_TI_TABLERULE = 0x1C8        # TitleSetPOLpro.TableRule  (64 bytes)
MJS_TI_TSUMO_TIMER = 0x1F7      # TableRule + 47 -- the per-turn clock, SECONDS
MJS_CONFIG_IDX_WAIT_TIME = 21   # "Tsumo timer": 10/20/30/40/50/60 sec

#: KEY: THE JAN RATE (2026-09-04). `console.c:4195` hands the results chain
#: `_DAT_00445b7c` = blob +0x1EC (TableRule + 36, u32), and
#: `yamaguchi2__00334be0` reads it aloud as "This table's rate is 1P = %d JAN"
#: before tweening the point column into the JAN column of the result record.
#: The client does NO arithmetic with it -- both columns are server-supplied
#: (janstats.RATE is the multiplier the server uses) -- but a zero here says
#: "1P = 0 JAN" on every results screen. Served from janstats so the number
#: read aloud is the number the record was scaled by.
MJS_TI_RATE = 0x1EC

#: FaceType[0..3] u32 +0x208, VoiceType[0..3] u16 +0x218, Volume[0..3] u16
#: +0x220, Domain[0..3] u8 +0x228 -- the client's own debug dump names all
#: four (lmenu.c:765-780). Only VoiceType has a READER: lobby.c:138 feeds the
#: four u16 to `sort__002cf250`, which streams `voice%02d.blk` for each seat
#: and sets the per-seat voice-id base `(type+1)*1000`. FaceType, Volume and
#: Domain are never loaded outside the dump (the face sprites come from the
#: PFG Face0..3 elements), so they stay zero on purpose -- writing a value
#: nothing reads is a guess that cannot be checked.
MJS_TI_FACETYPE = 0x208
MJS_TI_VOICETYPE = 0x218
MJS_TI_VOLUME = 0x220
MJS_TI_DOMAIN = 0x228


def tsumo_timer_seconds(value):
    """Seconds for a WAIT_TIME dropdown value -- the six labels are (v+1)*10.

    Clamped to the six REAL values: the client reads a byte and multiplies by 60
    to get ticks, so an out-of-range index would silently become a wrong clock
    rather than an error. 0 is not special-cased to "no limit" -- value 0 is
    "10 sec", and a ZERO BYTE (which is what we shipped) means the limit expires
    on the first tick and the picker returns its default slot 0x0d untouched.
    """
    return (min(max(int(value), 0), MJS_RULE_VALUE_COUNT["WAIT_TIME"] - 1) + 1) * 10


def wait_time_value_for_seconds(seconds):
    """The dropdown value whose clock is <= `seconds` -- never longer.

    Rounds DOWN so the served client clock can never outrun
    `jangame.Table.deadline()`, which is what keeps the sweeper from playing for
    a seat that still has time on screen. Below 10 s there is no such value, so
    the shortest (10 s) is used and the caller's deadline must allow for it.
    """
    v = int(seconds) // 10 - 1
    return min(max(v, 0), MJS_RULE_VALUE_COUNT["WAIT_TIME"] - 1)


# The served clock FOLLOWS janmahjong's Rules.wait_time rather than being a
# second copy of it. Deriving it is what makes the inversion impossible instead
# of merely asserted -- fb24f81d raised the client's clock alone and left our
# sweeper firing 15s early, which is the a43804ca regression class.
try:
    import janmahjong as _mj_wait
    MJS_RULE_DEFAULT["WAIT_TIME"] = wait_time_value_for_seconds(
        _mj_wait.Rules().wait_time)
except ImportError:                         # standalone run without the siblings
    pass


def mjs_table_info_blob(values=None, seats=None, params=None, voices=None,
                        rate=None):
    """`b/g/MJSTableInfoSub` -- 816 bytes, 33 rule records at +0x20.

    `seats` is `[(polid, name), ...]` in SEAT order (None for an empty seat) and
    `params` the table's parameter CSV -- the two halves the member panel draws.
    `voices` is the per-seat VoiceCharaNum (MjPLAYREQ +0x42) for +0x218; `rate`
    the JAN-per-point multiplier for +0x1EC (default janstats.RATE).

    `values` overrides a rule's served current/default by NAME (clamped to its
    real value count) -- `janrules.values_for(tid)` is the live source. Every
    rule offers all its real values; config index 26/27 (LIMIT_MONEY/
    LIMIT_LEVEL) are free-numeric and the reader forces one item, so their
    mask is ignored but still valid.
    """
    values = values or {}
    buf = bytearray(MJS_TI_TOTAL)
    rows = {0: 0, 1: 0}                 # per-dialog cursor (NORMAL rows: +1 each)
    for i, name in enumerate(MJS_RULE_NAMES):
        o = MJS_TI_REC_OFF + i * MJS_TI_REC
        count = MJS_RULE_VALUE_COUNT[name]
        cur = max(0, min(int(values.get(name, MJS_RULE_DEFAULT.get(name, 0))),
                         count - 1))
        mask = (1 << count) - 1         # every real value legal, low word only
        flags = 0x01 if name in MJS_TI_DIALOG_B else 0x00   # 0x00 = NORMAL row
        buf[o + 0x00] = flags
        buf[o + 0x01] = i               # config index = the rule (fixes the dupes)
        buf[o + 0x02] = cur             # current value
        buf[o + 0x03] = cur             # default value
        struct.pack_into("<I", buf, o + 0x04, mask & 0xFFFFFFFF)
        struct.pack_into("<I", buf, o + 0x08, (mask >> 32) & 0xFFFFFFFF)
        rows[1 if flags & 1 else 0] += 1
    # The per-turn clock lives in TableRule, NOT in the rule record the dialog
    # draws -- serving the dropdown alone left the clock at zero and the client
    # auto-discarded before it ever sampled the pad.
    buf[MJS_TI_TSUMO_TIMER] = tsumo_timer_seconds(
        buf[MJS_TI_REC_OFF + MJS_CONFIG_IDX_WAIT_TIME * MJS_TI_REC + 0x02])
    # NORMAL rows never page-break, so each dialog's cursor is just its row count;
    # 19 in A and 14 in B both sit inside the 40-slot arrays (the 08-16 crash was
    # page-break rows overrunning, which this build does not emit).
    if rows[0] > MJS_TI_ROW_SLOTS or rows[1] > MJS_TI_ROW_SLOTS:
        raise ValueError("dialog rows A=%d B=%d exceed the %d-slot array"
                         % (rows[0], rows[1], MJS_TI_ROW_SLOTS))
    # --- the member half ---------------------------------------------------
    for i, seat in enumerate((seats or [])[:4]):
        polid, name = (seat if seat else (0, ""))
        struct.pack_into("<Q", buf, MJS_TI_POLID + i * 8,
                         int(polid or 0) & 0xFFFFFFFFFFFFFFFF)
        o = MJS_TI_NAMES + i * MJS_TI_NAME_SLOT
        buf[o:o + MJS_TI_NAME_SLOT] = _text(name or "", MJS_TI_NAME_SLOT)
    if params:
        raw = params.encode("ascii", "replace") if isinstance(params, str) \
            else bytes(params)
        n = min(len(raw), MJS_TI_TOTAL - MJS_TI_PARAMS - 1)
        buf[MJS_TI_PARAMS:MJS_TI_PARAMS + n] = raw[:n]
    for i, v in enumerate((voices or [])[:4]):
        struct.pack_into("<H", buf, MJS_TI_VOICETYPE + 2 * i, int(v or 0) & 0xFFFF)
    if rate is None:
        try:
            import janstats as _js
            rate = _js.RATE
        except ImportError:                 # standalone run without the siblings
            rate = 0
    struct.pack_into("<I", buf, MJS_TI_RATE, int(rate) & 0xFFFFFFFF)
    return bytes(buf)


def ptl_content_length(n_tables=None):
    """The bytes of the PTL container that actually carry data.

    WARNING: THIS IS WHAT MUST BE DECLARED ON THE WIRE, NOT `PTL_TOTAL`. 49232 is the
    READER'S BUFFER, and a 3:0 reply declared at the reader's buffer size is
    truncated on the wire -- the console takes ~43 KB and waits for the rest for
    ever (measured twice on 2026-08-17). `POL_RESOURCE_PAYLEN`
    carries `b/g/PTL=23028`, which is this + 4.
    """
    n = len(JAN_TABLES) if n_tables is None else n_tables
    return PTL_TABLE_OFF + n * PTL_TABLE_REC


# --- the request key ---------------------------------------------------------

#: 3:0 request payload +0x08, i.e. frame +0x30. `responders.py` labelled this
#: "session id" from ONE PC capture of `u/account`; four Janhourou/Tetra Master
#: captures decrypted on 2026-08-18 show it varying by PATH within a single
#: session (member 16: `b/g/ZL` and `b/g/RL000` share 0x384ea5822c while
#: `b/g/PTL` carries 0xf0e4bcbb91), which a session id cannot do. It is the
#: fourth argument of `sqMgReadFileOffset` -- the fetch's KEY.
FETCH_KEY_OFF = 0x30


def request_key(pt):
    """The u64 key of a 3:0 request frame, or 0."""
    if not pt or len(pt) < FETCH_KEY_OFF + 8:
        return 0
    return struct.unpack_from("<Q", pt, FETCH_KEY_OFF)[0]


# --- selftest ----------------------------------------------------------------

def _crosscheck_tools():
    """Assert the vendored layouts still match `tools/jan*list.py`.

    Importable in the repo, absent in the container -- which is exactly the split
    this check exists for. Returns a list of human-readable differences.
    """
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    tools = os.path.join(os.path.dirname(here), "tools")
    if not os.path.isdir(tools):
        return None
    sys.path.insert(0, tools)
    bad = []
    try:
        import janzonelist, janroomlist, janptl
    except ImportError as exc:                   # pragma: no cover
        return ["cannot import the tools: %s" % exc]
    finally:
        sys.path.remove(tools)
    pairs = [
        ("ZL total", ZL_TOTAL, janzonelist.TOTAL),
        ("ZL hdr", ZL_HDR, janzonelist.HDR),
        ("ZL rec", ZL_REC, janzonelist.REC),
        ("ZL count off", ZL_COUNT_OFF, janzonelist.COUNT_OFF),
        ("ZL players", ZL_F_PLAYERS, janzonelist.F_PLAYERS),
        ("ZL rooms", ZL_F_ROOMS, janzonelist.F_ROOMS),
        ("ZL gate", ZL_F_GATE, janzonelist.F_GATE),
        ("ZL name", ZL_F_NAME, janzonelist.F_NAME),
        ("ZL host", ZL_F_HOST, janzonelist.F_CHANNEL),
        ("ZL id", ZL_F_ID, janzonelist.F_ID),
        ("RL total", RL_TOTAL, janroomlist.TOTAL),
        ("RL rec", RL_REC, janroomlist.REC),
        ("RL count off", RL_COUNT_OFF, janroomlist.COUNT_OFF),
        ("RL roomid", RL_F_ROOMID, janroomlist.F_ROOMID),
        ("RL players", RL_F_PLAYERS, janroomlist.F_PLAYERS),
        ("RL tables", RL_F_TABLES, janroomlist.F_TABLES),
        ("RL gate", RL_F_GATE, janroomlist.F_GATE),
        ("RL name", RL_F_NAME, janroomlist.F_NAME),
        ("RL opentables", RL_F_OPENTABLES, janroomlist.F_OPENTABLES),
        ("RL channel", RL_F_CHANNEL, janroomlist.F_CHANNEL),
        ("PTL total", PTL_TOTAL, janptl.TOTAL),
        ("PTL serial", PTL_SERIAL_OFF, janptl.SERIAL_OFF),
        ("PTL mcount", PTL_MEMBER_COUNT_OFF, janptl.MEMBER_COUNT_OFF),
        ("PTL tcount", PTL_TABLE_COUNT_OFF, janptl.TABLE_COUNT_OFF),
        ("PTL moff", PTL_MEMBER_OFF, janptl.MEMBER_OFF),
        ("PTL mrec", PTL_MEMBER_REC, janptl.MEMBER_REC),
        ("PTL toff", PTL_TABLE_OFF, janptl.TABLE_OFF),
        ("PTL trec", PTL_TABLE_REC, janptl.TABLE_REC),
        ("PTL m id", PTL_M_ID, janptl.M_ID),
        ("PTL m level", PTL_M_LEVEL, janptl.M_LEVEL),
        ("PTL m name", PTL_M_NAME, janptl.M_NAME),
        ("PTL t gate", PTL_T_GATE, janptl.T_GATE),
        ("PTL t state", PTL_T_STATE, janptl.T_STATE),
        ("PTL t name", PTL_T_NAME, janptl.T_NAME),
        ("PTL t text2", PTL_T_TEXT2, janptl.T_TEXT2),
        ("PTL csv fields", [n for n, _ in PTL_CSV_FIELDS],
         [n for n, _ in janptl.CSV_FIELDS]),
        ("PTL states", sorted(TABLE_STATES), sorted(janptl.STATES)),
    ]
    for what, mine, theirs in pairs:
        if mine != theirs:
            bad.append("%s: janlobby %r != tools %r" % (what, mine, theirs))
    return bad


def selftest():
    ok = True
    # The blob checks below must count from `live` alone, not from whatever a
    # real /data/jan-seats.json on this machine happens to hold.
    os.environ["POL_JAN_SEATS"] = "0"

    def check(name, cond):
        nonlocal ok
        print("%-58s %s" % (name, "ok" if cond else "FAIL"))
        ok = ok and bool(cond)

    # The geometry closes twice with no slack -- the check that the strides are
    # right, and the reason the member stride can be trusted at all.
    check("PTL member block: 0x50 + 256*88 == 0x5850",
          PTL_MEMBER_OFF + 256 * PTL_MEMBER_REC == PTL_TABLE_OFF)
    check("PTL table block: 0x5850 + 256*104 == 49232",
          PTL_TABLE_OFF + 256 * PTL_TABLE_REC == PTL_TOTAL)
    check("ZL: 0x48 + 32*0x40 == 2120", ZL_HDR + 32 * ZL_REC == ZL_TOTAL)
    check("RL: 0x48 + 256*200 == 51272", RL_HDR + 256 * RL_REC == RL_TOTAL)

    live = {
        "#MJS0R011": {"members": 2, "who": [
            {"name": "PS2Tester", "member_id": 8},
            {"name": "Fox", "member_id": 1}]},
        "#MJS0R023": {"members": 1, "who": [{"name": "DeckTest",
                                             "member_id": 16}]},
        # a game at Room 1-1's table 1: its in-game channel is the
        # room-qualified name a client in #MJS0R011 JOINs (see JAN_TABLES)
        "#MJS0T101001": {"members": 1, "who": [{"name": "PS2Tester",
                                                "member_id": 8}]},
    }

    z = zone_list_blob(live)
    check("ZL is %d bytes" % ZL_TOTAL, len(z) == ZL_TOTAL)
    check("ZL count == 4", struct.unpack_from("<I", z, ZL_COUNT_OFF)[0] == 4)
    z0 = ZL_HDR
    check("ZL zone 1 BodyCount == 2 (the two in #MJS0R011)",
          struct.unpack_from("<I", z, z0 + ZL_F_PLAYERS)[0] == 2)
    check("ZL zone 1 RoomCount == 4",
          struct.unpack_from("<I", z, z0 + ZL_F_ROOMS)[0] == 4)
    z1 = ZL_HDR + ZL_REC
    check("ZL zone 2 BodyCount == 1",
          struct.unpack_from("<I", z, z1 + ZL_F_PLAYERS)[0] == 1)
    z2 = ZL_HDR + 2 * ZL_REC
    check("ZL zone 3 BodyCount == 0 (empty is EMPTY, not a placeholder)",
          struct.unpack_from("<I", z, z2 + ZL_F_PLAYERS)[0] == 0)
    check("ZL host is dialable",
          z[z0 + ZL_F_HOST:z0 + ZL_F_ID].split(b"\x00")[0] == JAN_HOST.encode())
    check("ZL zone ids are 1..4",
          [z[ZL_HDR + i * ZL_REC + ZL_F_ID] for i in range(4)] == [1, 2, 3, 4])

    r = room_list_blob(1, live)
    check("RL is %d bytes" % RL_TOTAL, len(r) == RL_TOTAL)
    check("RL count == 4", struct.unpack_from("<I", r, RL_COUNT_OFF)[0] == 4)
    r0 = RL_HDR
    check("RL room 1-1 players == 2",
          struct.unpack_from("<I", r, r0 + RL_F_PLAYERS)[0] == 2)
    check("RL room 1-2 players == 0",
          struct.unpack_from("<I", r, RL_HDR + RL_REC + RL_F_PLAYERS)[0] == 0)
    check("RL every gate is exactly 1",
          all(struct.unpack_from("<I", r, RL_HDR + i * RL_REC + RL_F_GATE)[0] == 1
              for i in range(4)))
    check("RL channel is #MJS0R011",
          r[r0 + RL_F_CHANNEL:r0 + RL_F_CHANNEL + RL_CHANNEL_MAX]
          .split(b"\x00")[0] == b"#MJS0R011")
    check("RL open tables == 3 (one of the four is occupied)",
          r[r0 + RL_F_OPENTABLES] - 0x20 == 3)
    check("RL for an unknown zone is None", room_list_blob(9, live) is None)

    who = occupants(live, "#MJS0R011")
    p = ptl_blob(who, live, room=101)     # Room 1-1: where the fixture's game is
    check("PTL is %d bytes" % PTL_TOTAL, len(p) == PTL_TOTAL)
    check("PTL member count == 2",
          struct.unpack_from("<i", p, PTL_MEMBER_COUNT_OFF)[0] == 2)
    check("PTL table count == 4",
          struct.unpack_from("<i", p, PTL_TABLE_COUNT_OFF)[0] == 4)
    m0 = PTL_MEMBER_OFF
    check("PTL member 0 name == PS2Tester",
          p[m0 + PTL_M_NAME:m0 + PTL_M_NAME + PTL_M_NAME_MAX]
          .split(b"\x00")[0] == b"PS2Tester")
    check("PTL member 0 id is NON-ZERO (a zero id is an empty slot)",
          struct.unpack_from("<Q", p, m0 + PTL_M_ID)[0] != 0)
    check("PTL member ids are distinct",
          len({struct.unpack_from("<Q", p, PTL_MEMBER_OFF + i * PTL_MEMBER_REC)[0]
               for i in range(2)}) == 2)
    t0 = PTL_TABLE_OFF
    check("PTL table 0 seats 1 and is 'ready' (state 4 = Start Game ungreys)",
          struct.unpack_from("<I", p, t0 + PTL_T_SEATED)[0] == 1
          and p[t0 + PTL_T_STATE] == 4)
    # WARNING: THIS CHECK USED TO ASSERT THE OPPOSITE, and its reason died on
    # 2026-09-04. It read "a free table is still 'ready' -- the snapshot the
    # master holds must say 4 BEFORE they reserve", which was true while
    # `b/g/PTL` was a room-entry SNAPSHOT with no way to update it: a table that
    # read 0 on the way in stayed 0 in the client's copy, and Start Game could
    # never ungrey. The delta stream (315866b7) pushes the new row within ~2s,
    # so the workaround is retired and an empty table can say what it is.
    check("PTL table 1 is free, so it is OPEN (0) -- the delta will make it 4 "
          "the moment somebody reserves it",
          p[PTL_TABLE_OFF + PTL_TABLE_REC + PTL_T_STATE] == 0)
    check("a full table is 'in play'", table_state_for(4) == 5)
    check("an in-play mark wins over a joinable count",
          table_state_for(1, in_play=True) == 5)
    os.environ["POL_JAN_TABLE_STATES"] = "legacy"
    check("legacy ladder restores 0/2/5",
          (table_state_for(0), table_state_for(1), table_state_for(4))
          == (0, 2, 5))
    os.environ.pop("POL_JAN_TABLE_STATES", None)
    check("PTL every table gate is exactly 1",
          all(struct.unpack_from("<I", p, PTL_TABLE_OFF + i * PTL_TABLE_REC
                                 + PTL_T_GATE)[0] == 1 for i in range(4)))
    check("PTL every table has a name (a nameless table is DROPPED)",
          all(p[PTL_TABLE_OFF + i * PTL_TABLE_REC + PTL_T_NAME] != 0
              for i in range(4)))
    check("PTL declared length is 23024 + 4 = the compose override",
          ptl_content_length() == 23024)
    check("PTL content ends inside the declared length",
          ptl_content_length() >= PTL_TABLE_OFF + 4 * PTL_TABLE_REC)

    # An empty room is a legal PTL: no members, four tables, and the room screen
    # still comes up. This is the case that used to hang.
    empty = ptl_blob([], {})
    check("PTL for an empty room has 0 members and 4 tables",
          struct.unpack_from("<i", empty, PTL_MEMBER_COUNT_OFF)[0] == 0
          and struct.unpack_from("<i", empty, PTL_TABLE_COUNT_OFF)[0] == 4)

    # A count-only snapshot (the pre-identity publish) degrades to nameless rows
    # rather than to invented ones.
    old = {"#MJS0R011": {"members": 3}}
    check("a count-only snapshot yields 3 nameless occupants",
          [o.name for o in occupants(old, "#MJS0R011")] == ["", "", ""])

    check("room_for_key(101) is room 1-1",
          getattr(room_for_key(101), "channel", None) == "#MJS0R011")
    check("room_for_key of an unknown key is None", room_for_key(0xDEAD) is None)
    check("request_key reads payload +0x08",
          request_key(b"\x02\x03\x00\x00" + b"\x00" * 0x2C
                      + struct.pack("<Q", 101)) == 101)

    # b/g/MJSTableInfoSub: the rules blob that fixes the all-zeros crash.
    ti = mjs_table_info_blob()
    check("MJSTableInfoSub is %d bytes" % MJS_TI_TOTAL, len(ti) == MJS_TI_TOTAL)
    idxs = [ti[MJS_TI_REC_OFF + i * MJS_TI_REC + 0x01] for i in range(MJS_TI_COUNT)]
    check("every record carries its OWN config index 0..32 (no duplicate rows)",
          idxs == list(range(MJS_TI_COUNT)))
    empties = []
    for i, name in enumerate(MJS_RULE_NAMES):
        o = MJS_TI_REC_OFF + i * MJS_TI_REC
        lo = struct.unpack_from("<I", ti, o + 0x04)[0]
        hi = struct.unpack_from("<I", ti, o + 0x08)[0]
        items = bin(lo).count("1") + bin(hi).count("1")
        if items == 0 and i not in (0x1A, 0x1B):
            empties.append(name)
        # a mask bit past the real value count would draw a null-pointer string
        if hi or lo >> MJS_RULE_VALUE_COUNT[name]:
            empties.append("%s:mask-overshoots-labels" % name)
    check("no rule has an empty OR over-wide mask (the crash + garbage cases)",
          not empties)
    if empties:
        print("    offenders:", empties)
    check("current value is always inside the mask (row selects, not blank)",
          all(ti[MJS_TI_REC_OFF + i * MJS_TI_REC + 0x02]
              < MJS_RULE_VALUE_COUNT[MJS_RULE_NAMES[i]] for i in range(MJS_TI_COUNT)))
    check("WAIT_TIME defaults off value 0 (a 0 clock auto-advances the hand)",
          ti[MJS_TI_REC_OFF + 21 * MJS_TI_REC + 0x02] != 0)
    # The clock the client actually reads (0x00445b87 = blob +0x1F7). A zero here
    # is the bug `polrun.py --patches janclock` was papering over.
    check("the turn clock at +0x1F7 is non-zero (blob +0x1F7 = EE 0x00445b87)",
          ti[MJS_TI_TSUMO_TIMER] != 0)
    check("the clock matches the WAIT_TIME dropdown, in seconds",
          ti[MJS_TI_TSUMO_TIMER]
          == tsumo_timer_seconds(ti[MJS_TI_REC_OFF + 21 * MJS_TI_REC + 0x02]))
    check("the clock lands inside TableRule (+0x1C8..+0x208)",
          MJS_TI_TABLERULE <= MJS_TI_TSUMO_TIMER < 0x208)
    # WARNING: THE CROSS-MODULE INVARIANT. The served clock is the CLIENT's; jangame's
    # sweeper plays a default after Rules.wait_time + TURN_GRACE. If the client
    # clock ever outruns the sweeper we auto-discard for a player who can still
    # see time on their own clock. Checked here so raising WAIT_TIME alone fails.
    try:
        import janmahjong as _mj
        import jangame as _jg
        _deadline = float(_mj.Rules().wait_time) + _jg.Table.TURN_GRACE
        check("the SERVED client clock (%ds) is inside jangame's deadline (%.0fs)"
              % (ti[MJS_TI_TSUMO_TIMER], _deadline),
              ti[MJS_TI_TSUMO_TIMER] < _deadline)
        check("the served clock matches janmahjong.Rules.wait_time",
              ti[MJS_TI_TSUMO_TIMER] == _mj.Rules().wait_time)
        check("seconds->value never rounds UP past the deadline",
              all(tsumo_timer_seconds(wait_time_value_for_seconds(x)) <= max(x, 10)
                  for x in (10, 15, 30, 45, 60, 90)))
    except ImportError:                     # standalone run without the siblings
        pass
    check("dialog A holds 0..18, dialog B holds 19..32",
          all((ti[MJS_TI_REC_OFF + i * MJS_TI_REC] & 1) == (1 if i >= 19 else 0)
              for i in range(MJS_TI_COUNT)))

    # The seat-store merge: a reservation with no channel body behind it must
    # still count as seated and must not read as in-play.
    if janseats is not None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            os.environ["POL_JAN_SEATS"] = "1"
            os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "seats.json")
            janseats._TABLES.clear()
            janseats._ADOPTED[0] = True     # empty store, do not adopt disk
            janseats._OWNER[0] = True
            janseats.reserve(2, 8, "PS2Tester")
            merged = ptl_blob([], {})       # nobody in any channel
            t1 = PTL_TABLE_OFF + 1 * PTL_TABLE_REC
            check("a reservation alone seats table 2 (channel empty)",
                  struct.unpack_from("<I", merged, t1 + PTL_T_SEATED)[0] == 1)
            check("a reserved table is 'ready', not 'in play'",
                  merged[t1 + PTL_T_STATE] == 4)
            janseats.set_in_play(2, 8)
            merged = ptl_blob([], {})
            check("MjGAMESTART flips the reserved table to 'in play'",
                  merged[t1 + PTL_T_STATE] == 5)
            janseats._TABLES.clear()
            janseats._OWNER[0] = False
            janseats._ADOPTED[0] = False
            janseats._CACHE["mtime"] = -1.0
            os.environ["POL_JAN_SEATS"] = "0"
            os.environ.pop("POL_JAN_SEATS_FILE", None)
    else:
        print("%-58s %s" % ("seat-store merge", "skipped (no janseats)"))
    os.environ.pop("POL_JAN_SEATS", None)

    diffs = _crosscheck_tools()
    if diffs is None:
        print("%-58s %s" % ("layout cross-check vs tools/", "skipped (no tools/)"))
    else:
        check("layout matches tools/jan{zone,room,ptl}list.py", not diffs)
        for d in diffs:
            print("    " + d)

    # --- THE DELTA ROW IS THE SNAPSHOT ROW ---------------------------------
    # `encode_table(table_values(..))` is what ptl_blob writes, so a delta and
    # a re-fetch can only ever agree if this holds. Compare against the bytes
    # the blob actually carries rather than against another call of the same
    # helper, or the check proves nothing.
    blob = ptl_blob([], None)
    for _i, (_tid, _chan) in enumerate(JAN_TABLES):
        _o = PTL_TABLE_OFF + _i * PTL_TABLE_REC
        _from_blob = blob[_o:_o + PTL_TABLE_REC]
        _from_delta = encode_table(table_values_for(_tid, None))
        check("table %d: the TD delta encodes the blob's own row" % _tid,
              _from_blob == _from_delta)

    # Every field the client's parser reads must survive the values round trip,
    # at the offset `lb__002f9e10` stores it to.
    _vals = ["#MJS0T002", "1", "0x%016X" % 2, "5", "4", "3", "9,8,7"]
    _rec = encode_table(_vals)
    check("TD field 0 -> +0x18 name",
          _rec[PTL_T_NAME:PTL_T_NAME + 9] == b"#MJS0T002")
    check("TD field 1 -> +0x10 gate",
          struct.unpack_from("<I", _rec, PTL_T_GATE)[0] == 1)
    check("TD field 2 -> +0x00 id (hex text)",
          struct.unpack_from("<Q", _rec, PTL_T_ID)[0] == 2)
    check("TD field 3 -> +0x14 state", _rec[PTL_T_STATE] == 5)
    check("TD field 4 -> +0x08 capacity",
          struct.unpack_from("<I", _rec, PTL_T_CAPACITY)[0] == 4)
    check("TD field 5 -> +0x0c seated",
          struct.unpack_from("<I", _rec, PTL_T_SEATED)[0] == 3)
    check("TD field 6 -> +0x28 CSV, commas and all",
          _rec[PTL_T_TEXT2:PTL_T_TEXT2 + 5] == b"9,8,7")
    # THE STATE LADDER. An untouched table must read OPEN -- serving 4 made a
    # whole room of tables describe themselves as "ready to start" the moment
    # anyone walked in (seen live 2026-09-04).
    check("an untouched table is OPEN (0), not ready", table_state_for(0) == 0)
    check("one seat makes it ready (4), so Start Game can ungrey",
          table_state_for(1) == 4)
    check("three seats is still ready", table_state_for(3) == 4)
    check("a full table is in play (5)", table_state_for(4) == 5)
    check("an explicit in-play mark is 5 whatever the count",
          table_state_for(1, in_play=True) == 5)

    # --- THE ROOM KEY IS LEARNED, NOT DECODED ------------------------------
    import tempfile as _tf
    os.environ["POL_JAN_ROOMKEY_FILE"] = os.path.join(_tf.mkdtemp(), "rk.json")
    _KEYMAP.clear(); _KEY_PENDING.clear(); _KEYMAP_LOADED[0] = False
    _LIVEKEY = 0x3d7c0054cc          # a real key off the wire, 2026-09-04
    _room1 = all_rooms()[0].channel
    check("an unseen key names no room (fall back to every room)",
          room_for_key(_LIVEKEY) is None)
    note_room_fetch(8, _LIVEKEY)
    check("a fetch with nobody joined yet binds nothing",
          reconcile_room_keys({}) == [])
    check("...and the key is still unresolved", room_for_key(_LIVEKEY) is None)
    _live_after_join = {_room1: {"who": [{"member_id": 8, "name": "x"}]}}
    check("once the asker has JOINed, the key binds to THAT room",
          reconcile_room_keys(_live_after_join) == [(_LIVEKEY, _room1)])
    _r = room_for_key(_LIVEKEY)
    check("and the key now resolves to it",
          _r is not None and _r.channel == _room1)
    check("binding is one-shot -- nothing pending, nothing rebound",
          reconcile_room_keys(_live_after_join) == [])
    # a second member in two channels at once must not teach us anything
    note_room_fetch(9, 0xdeadbeef)
    check("an ambiguous asker binds nothing",
          reconcile_room_keys({
              all_rooms()[0].channel: {"who": [{"member_id": 9}]},
              all_rooms()[1].channel: {"who": [{"member_id": 9}]}}) == [])
    check("the learned map survives a reload",
          (_KEYMAP_LOADED.__setitem__(0, False) or _KEYMAP.clear()
           or room_for_key(_LIVEKEY) is not None))
    os.environ.pop("POL_JAN_ROOMKEY_FILE", None)
    _KEYMAP.clear(); _KEY_PENDING.clear(); _KEYMAP_LOADED[0] = False

    check("an unknown table id has no delta row",
          table_values_for(999, None) is None)

    # --- b/g/MJSTableInfoSub carries the MEMBER ROWS ------------------------
    # The panel reads this file, not MjNOTICEMEMBER. We authored the rules and
    # nothing else, which drew four blank rows under "Table 00 members".
    _ti = mjs_table_info_blob(
        seats=[(0xAAAA, "PS2Tester"), (0xBBBB, "LaptopTest2"), None, None],
        params=table_params_text_for(2))
    check("the table-info blob is still 816 bytes", len(_ti) == MJS_TI_TOTAL)
    check("seat 0's PolID lands at +0x00",
          struct.unpack_from("<Q", _ti, MJS_TI_POLID)[0] == 0xAAAA)
    check("seat 1's PolID lands at +0x08",
          struct.unpack_from("<Q", _ti, MJS_TI_POLID + 8)[0] == 0xBBBB)
    check("an empty seat's PolID is 0, which is how the panel skips the row",
          struct.unpack_from("<Q", _ti, MJS_TI_POLID + 16)[0] == 0)
    check("seat 0's NAME lands at +0x22C",
          _ti[MJS_TI_NAMES:MJS_TI_NAMES + 9] == b"PS2Tester")
    check("seat 1's name is one 16-byte slot along",
          _ti[MJS_TI_NAMES + 16:MJS_TI_NAMES + 16 + 11] == b"LaptopTest2")
    # `Table %02d members` formats field 0 of the +0x2EC CSV -- a zero there is
    # literally the reported "Table 00".
    _csv = _ti[MJS_TI_PARAMS:MJS_TI_PARAMS + 32].partition(bytes(1))[0]
    check("the +0x2EC CSV names table 2 in field 0, so the title reads 02",
          _csv.split(b",")[0] == b"2")
    check("the rules survive the member half",
          _ti[MJS_TI_REC_OFF + 0x01] == 0)
    if janseats is not None:
        check("janseats' delta table ids match JAN_TABLES",
              tuple(t for t, _c in JAN_TABLES) == tuple(janseats.DEFAULT_TABLE_IDS))

    # --- the members panel's two CSV fields, the voice banks, the rate ------
    _ti = mjs_table_info_blob(voices=[3, 0, 7, 1], rate=1000)
    check("VoiceType[seat] is a u16 at +0x218 + 2*seat (lobby.c:138)",
          struct.unpack_from("<4H", _ti, MJS_TI_VOICETYPE) == (3, 0, 7, 1))
    check("FaceType/Volume/Domain stay zero -- nothing reads them",
          _ti[MJS_TI_FACETYPE:MJS_TI_VOICETYPE] == bytes(16)
          and _ti[MJS_TI_VOLUME:MJS_TI_NAMES] == bytes(12))
    check("the JAN rate lands at +0x1EC (console.c:4195 -> '1P = %d JAN')",
          struct.unpack_from("<I", _ti, MJS_TI_RATE)[0] == 1000)
    check("the rate defaults to janstats.RATE, never 0",
          struct.unpack_from("<I", mjs_table_info_blob(), MJS_TI_RATE)[0] > 0)
    _csv = _table_params_text(number=2, ready_mask=0b0101, master_seat=2)
    check("CSV field 6 = ready bitmask (decimal), field 8 = master seat",
          _csv.split(",")[6] == "5" and _csv.split(",")[8] == "2")
    try:
        _table_params_text(master_seat=10)
        check("a master seat wider than %1d raises (sscanf would shift)", False)
    except ValueError:
        check("a master seat wider than %1d raises (sscanf would shift)", True)
    if janseats is not None:
        import tempfile as _tf2
        with _tf2.TemporaryDirectory() as td:
            os.environ["POL_JAN_SEATS"] = "1"
            os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "seats.json")
            janseats._TABLES.clear()
            janseats._ADOPTED[0] = True
            janseats._OWNER[0] = True
            janseats.reserve(3, 8, "PS2Tester")
            janseats.reserve(3, 16, "DeckTest")
            janseats.cancel(3, 8)               # 16 (seat 1) is promoted
            check("seat_marks: the promoted guest's seat is the master seat "
                  "and only live seats are Ready",
                  seat_marks(3) == (0b0010, 1))
            _csv = table_params_text_for(3)
            check("...and both reach the CSV the member panels parse",
                  _csv.split(",")[6] == "2" and _csv.split(",")[8] == "1")
            _row = encode_table(table_values_for(3, None))
            check("...and the PTL row / TD delta carry the same CSV",
                  _row[PTL_T_TEXT2:PTL_T_TEXT2 + len(_csv)] == _csv.encode())
            janseats._TABLES.clear()
            janseats._OWNER[0] = False
            janseats._ADOPTED[0] = False
            janseats._CACHE["mtime"] = -1.0
            os.environ["POL_JAN_SEATS"] = "0"
            os.environ.pop("POL_JAN_SEATS_FILE", None)
    _who = [Occupant("PS2Tester", 8, profile_id=1000000803)]
    _p = ptl_blob(_who, {})
    check("PTL member row +0x18 = the Jan Content ID View Profile sends",
          struct.unpack_from("<I", _p, PTL_MEMBER_OFF + PTL_M_PROFILE_ID)[0]
          == 1000000803)

    # --- TWO ROOMS, ONE TABLE NUMBER (finding 30) ---------------------------
    # Room 1-1 (101) and Room 2-3 (203) each own a table 1. The row id stays
    # the base id; the row NAME (= the channel the client JOINs) is the
    # room's own since 2026-09-04, as are the seats, the open-table counts,
    # the serials and the deltas.
    _r11 = next(r for r in all_rooms() if r.channel == "#MJS0R011")
    _r23 = next(r for r in all_rooms() if r.channel == "#MJS0R023")
    check("Room 1-1 owns four composites of its own id",
          _r11.id == 101 and _r11.table_ids() == [(101 << 16) | t for t in (1, 2, 3, 4)]
          and _r23.table_ids()[0] == (203 << 16) | 1)
    check("tables_for_room(None) is the bare fixture (room 0)",
          [t for t, _c in tables_for_room(None)] == [1, 2, 3, 4]
          and tables_for_room(101) == _r11.table_list())
    check("member_room finds the ONE Jan channel a member is in",
          getattr(member_room(live, 8), "channel", None) == "#MJS0R011"
          and getattr(member_room(live, 16), "channel", None) == "#MJS0R023"
          and member_room(live, 4242) is None and member_room_id(live, 8) == 101)
    check("member_room refuses an ambiguous member (in two channels)",
          member_room({"#MJS0R011": {"who": [{"member_id": 9}]},
                       "#MJS0R012": {"who": [{"member_id": 9}]}}, 9) is None)
    if janseats is not None:
        import tempfile as _tf3
        with _tf3.TemporaryDirectory() as td:
            os.environ["POL_JAN_SEATS"] = "1"
            os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "seats.json")
            janseats._TABLES.clear(); janseats._DELTAS.clear()
            janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats._ADOPTED[0] = True
            janseats._OWNER[0] = True
            _t1_11 = janseats.room_table_id(101, 1)
            _t1_23 = janseats.room_table_id(203, 1)
            janseats.reserve(_t1_11, 8, "PS2Tester")
            janseats.reserve(_t1_23, 16, "DeckTest")
            janseats.reserve(_t1_23, 21, "guest")
            _p11 = ptl_blob([], {}, serial=blob_serial(_r11), room=_r11)
            _p23 = ptl_blob([], {}, serial=blob_serial(_r23), room=_r23)
            _p0 = ptl_blob([], {}, serial=blob_serial(0), room=None)
            _t0 = PTL_TABLE_OFF
            check("Room 1-1's PTL table 1 seats 1, Room 2-3's seats 2, the "
                  "unknown room's seats 0",
                  struct.unpack_from("<I", _p11, _t0 + PTL_T_SEATED)[0] == 1
                  and struct.unpack_from("<I", _p23, _t0 + PTL_T_SEATED)[0] == 2
                  and struct.unpack_from("<I", _p0, _t0 + PTL_T_SEATED)[0] == 0)
            check("on the wire the row id is the BASE id (1) in every room",
                  struct.unpack_from("<Q", _p11, _t0 + PTL_T_ID)[0] == 1
                  and struct.unpack_from("<Q", _p23, _t0 + PTL_T_ID)[0] == 1)
            check("...and the row name (the delta match key AND the channel "
                  "the client JOINs) is ROOM-QUALIFIED: #MJS0T101001 in Room "
                  "1-1, #MJS0T203001 in Room 2-3, bare #MJS0T001 in room 0",
                  _p11[_t0 + PTL_T_NAME:_t0 + PTL_T_NAME + 13] == b"#MJS0T101001\0"
                  and _p23[_t0 + PTL_T_NAME:_t0 + PTL_T_NAME + 13] == b"#MJS0T203001\0"
                  and _p0[_t0 + PTL_T_NAME:_t0 + PTL_T_NAME + 13] == b"#MJS0T001\0\0\0\0")
            check("...and a TD delta for the composite names the same channel",
                  table_values_for(_t1_11)[0] == "#MJS0T101001"
                  and table_values_for(_t1_23)[0] == "#MJS0T203001"
                  and table_values_for(1)[0] == "#MJS0T001")
            check("the CSV's table number is 1 in both rooms",
                  _p11[_t0 + PTL_T_TEXT2:_t0 + PTL_T_TEXT2 + 2] == b"1,"
                  == _p23[_t0 + PTL_T_TEXT2:_t0 + PTL_T_TEXT2 + 2])
            check("a room's PTL is stamped with ITS sequence",
                  struct.unpack_from("<I", _p11, PTL_SERIAL_OFF)[0] == 1
                  and struct.unpack_from("<I", _p23, PTL_SERIAL_OFF)[0] == 2
                  and struct.unpack_from("<I", _p0, PTL_SERIAL_OFF)[0] == 0)
            # everything else in the two rows is identical -- the ONLY bytes
            # that differ are the seat count, the state and the CSV marks
            _row11 = bytearray(_p11[_t0:_t0 + PTL_TABLE_REC])
            _row23 = bytearray(_p23[_t0:_t0 + PTL_TABLE_REC])
            for _o in (PTL_T_SEATED, PTL_T_STATE):
                _row11[_o:_o + 4] = _row23[_o:_o + 4] = bytes(4)
            _row11[PTL_T_NAME:PTL_T_NAME + PTL_T_NAME_MAX] = bytes(PTL_T_NAME_MAX)
            _row23[PTL_T_NAME:PTL_T_NAME + PTL_T_NAME_MAX] = bytes(PTL_T_NAME_MAX)
            _row11[PTL_T_TEXT2:] = _row23[PTL_T_TEXT2:] = b""
            check("the two rooms' rows differ ONLY in seats/state/name/CSV",
                  _row11 == _row23)
            _rl1 = room_list_blob(1, {})
            _rl2 = room_list_blob(2, {})
            check("RL: Room 1-1 has 3 open tables, Room 1-2 has 4",
                  _rl1[RL_HDR + RL_F_OPENTABLES] - 0x20 == 3
                  and _rl1[RL_HDR + RL_REC + RL_F_OPENTABLES] - 0x20 == 4)
            check("RL: Room 2-3 has 3 open tables, Room 2-1 has 4",
                  _rl2[RL_HDR + 2 * RL_REC + RL_F_OPENTABLES] - 0x20 == 3
                  and _rl2[RL_HDR + RL_F_OPENTABLES] - 0x20 == 4)
            check("table_values_for takes the composite and renders the "
                  "room's seats",
                  table_values_for(_t1_11, {})[5] == "1"
                  and table_values_for(_t1_23, {})[5] == "2"
                  and table_values_for(1, {})[5] == "0"
                  and table_values_for(_t1_11, {})[2] == "0x%016X" % 1)
            check("the TD delta for a room's table encodes that room's row",
                  encode_table(table_values_for(_t1_23, {}))
                  == _p23[_t0:_t0 + PTL_TABLE_REC])
            check("seat_marks is per composite",
                  seat_marks(_t1_11) == (0b0001, 0) and seat_marks(_t1_23) == (0b0011, 0)
                  and seat_marks(1) == (0, 0))
            # a body in the SHARED game channel is attributed by the store
            _live_body = {"#MJS0T001": {"who": [{"name": "DeckTest",
                                                 "member_id": 16}]}}
            check("a body in #MJS0T001 counts toward the composite that "
                  "seats them, not toward every room's table 1",
                  table_seating(_t1_23, "#MJS0T001", _live_body) == (2, False)
                  and table_seating(_t1_11, "#MJS0T001", _live_body) == (1, False)
                  and tables_in_use(_live_body, _r11) == 1)
            janseats._TABLES.clear(); janseats._DELTAS.clear()
            janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats._OWNER[0] = False
            janseats._ADOPTED[0] = False
            janseats._CACHE["mtime"] = -1.0
            os.environ["POL_JAN_SEATS"] = "0"
            os.environ.pop("POL_JAN_SEATS_FILE", None)
    os.environ.pop("POL_JAN_SEATS", None)

    print("\n%s" % ("ALL OK" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--topology", action="store_true",
                    help="print the zones, rooms and channels this server offers")
    args = ap.parse_args(argv)
    if args.topology:
        for z in topology():
            print("zone %d  %-20s  b/g/RL%03d" % (z.id, z.name, z.id))
            for r in z.rooms:
                print("    room %-4d %-16s %-12s %d tables"
                      % (r.id, r.name, r.channel, r.tables))
        print("tables (four per room, composite id = room << 16 | n; the "
              "wire id and channel are the base's): %s"
              % ", ".join("%d=%s" % (t, c) for t, c in JAN_TABLES))
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
