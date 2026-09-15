#!/usr/bin/env python3
"""Build Janhourou's `b/g/RL%03d` room list -- one file per ZONE ID.

WHICH FILE THE CLIENT ASKS FOR. `zonesel__ZoneSelectMain` copies the chosen zone
row, dials its `+0x2C` host, then `zonemain__RoomSelectMain_00340ab0` runs
`cp__002fc878(buf, zone)` -> `b/g/RL%03d`. **The `%03d` is the zone record's
`+0x3C` byte** (`lb a1, 0x004466fc`), NOT its `+0x00` -- see janzonelist.py.
Confirmed live 2026-08-16: a zone with `+0x3C` = 1 fetched `RL001`.

WARNING: **AND IT MUST BE SERVED SHORT.** The reader's buffer is 51272, but a reply
that size is TRUNCATED ON THE WIRE -- caught mid-hang in a savestate at
`expect=51272 got=43226`, with the room-list machine parked at STATE 99. The
reader shrinks its request to `declared - 4`, so `POL_RESOURCE_PAYLEN`
declares 876 = 4 records' worth and the rest of its buffer is pre-zeroed by its
own constructor. See docker-compose.yml; 876 renders where 51276 hung.

THE READER, from the decompiled lobbysub.c,
`lobbysub__Room_List_Analyz_Now_002b5f30`:

    for (i = 0; i < *(int *)(buf + 0x40); i++) {      # count at +0x40
        if (*(int *)(rec + 0x68) == 1) {              # record +0x20 -- THE GATE
            ...copy 200 bytes' worth of fields...
        }
        off += 200;                                    # stride 0xC8
    }
    *(int *)(ctx + 4) = accepted;

WARNING: **The gate is `== 1`, not merely non-zero** -- stricter than the zone list's
`!= 0` at its own `+0x08`. A record with, say, 2 there is skipped in silence and
the room list draws empty, which looks exactly like "no rooms exist".

Field map, record-relative (buffer + 0x48 + i*200). The compaction is 1:1 on
offsets, so a compacted-row offset IS a file-record offset.

    +0x00  u64   ROOM ID    -> sqMgCpEnterRoom's first argument
    +0x08  u64
    +0x10  u32   RoomPlayerNum, rendered as the row's "BodyCount"
    +0x14  u32
    +0x18  u32   TableNum -- the room's TOTAL tables
    +0x1C  u32
    +0x20  u32   <-- gate, must be exactly 1
    +0x24  u16   -> sqMgCpEnterRoom arg 4          +0x28  128 bytes  TitleSet
    +0x26  u8                                      +0x86  s8   OPEN tables + 0x20
    +0x27  u8    -> sqMgCpEnterRoom arg 3          +0xA8  16 bytes
    +0xB8  13 bytes  THE ROOM'S IRC CHANNEL NAME   +0xC5  3 bytes -> 0xC8 = 200

The geometry closes with no slack, which is the check that the reading is right:
`(51272 - 0x48) / 200 = 256` records exactly, and 0x48 + 256*200 == 51272.

WHERE THE NAMES COME FROM -- three readers, no guessing (2026-08-16):

* `lobbysub__002b65b0` is SE's own **"--- Room List Dump ---"**, and it prints
  `room %d: TitleSet = %s` from +0x28, `RoomPlayerNum = %d` from +0x10 and
  `TableNum = %d` from +0x18.
* `zonewin__ZoneListPage_003401f0` is the row renderer: 22 bytes from +0x28 into
  the `ZoneName` widget, `%d` of the u32 at +0x10 into `BodyCount`, and
  `%d` of **`lb rec+0x86` minus 0x20** into `RoomCount`. That bias is measured
  (`0x00340334 lb v1,134(s3)` / `addiu a2,v1,-32`), so a zero byte there renders
  as **-32** -- a useful signature that the screen drew at all.
* `lmenu__002b25e0` is the room entry:
  `sqMgCpEnterRoom(ld rec+0x00, rec+0xB8, lbu rec+0x27, lhu rec+0x24)`, and
  `sqMgCpEnterTable` later refuses outright if the stored channel name's first
  byte is NUL. So **+0xB8 must be a real channel name** or the next screen
  cannot start a table.

WARNING: NOT MEASURED: +0x08, +0x14, +0x1C, +0x26, +0xA8, +0xC5, and what the two
`sqMgCpEnterRoom` scalars at +0x24/+0x27 mean.

WARNING: **NEVER SERVE A ZERO-ROOM LIST.** `lobbysub__002b6210(ctx, page, &n)` returns
without writing `*n` on three paths -- page count 0, page < 0, page >= count --
and its caller `zonewin__ZoneListPage_003401f0` never initialises that stack slot
(`0x00340288 addiu a2,sp,172`, and the loop re-reads `lw a0,172(sp)` every
iteration). With no rows the row pointer is also 0, so the loop copies 22 bytes
from address 0x28 and walks past the 8-entry `List01..List08` name table for a
garbage number of iterations. `build()` refuses an empty list for this reason.

    python tools/janroomlist.py --zone 1 --out data/resources/8.b_g_RL001.bin
    python tools/janroomlist.py --dump data/resources/8.b_g_RL001.bin
"""
import argparse
import struct

TOTAL = 51272           # cp__002fc878: mg__002f5c58(..., 0xc848, ...)
HDR = 0x48
REC = 200               # off += 200
COUNT_OFF = 0x40
MAX_ROOMS = (TOTAL - HDR) // REC          # 256, and 0x48 + 256*200 == 51272

F_ROOMID = 0x00         # u64 -- sqMgCpEnterRoom arg 1
F_B = 0x08              # u64
F_PLAYERS = 0x10        # u32 -- RoomPlayerNum / "BodyCount"
F_D = 0x14              # u32
F_TABLES = 0x18         # u32 -- TableNum
F_F = 0x1C              # u32
F_GATE = 0x20           # u32 -- must be exactly 1
F_ARG4 = 0x24           # u16 -- sqMgCpEnterRoom arg 4
F_H = 0x26              # u8
F_ARG3 = 0x27           # u8  -- sqMgCpEnterRoom arg 3
F_NAME = 0x28           # TitleSet; the row draws the first 22 bytes
#: The widget is called `RoomCount`, but the on-screen COLUMN HEADER (confirmed
#: live 2026-08-16) reads **"Open Tables"** -- so this is the FREE table count,
#: a different quantity from SE's own `TableNum` at +0x18, which is the total.
F_OPENTABLES = 0x86     # s8, biased by +0x20
F_TEXT2 = 0xA8          # 16 bytes
F_CHANNEL = 0xB8        # 13 bytes -- the room's IRC channel name
F_TAIL = 0xC5           # 3 bytes

NAME_MAX = F_OPENTABLES - F_NAME    # 94, so the Open Tables byte stays clear
CHANNEL_MAX = F_TAIL - F_CHANNEL    # 13, and mgStrCopyLim takes exactly 13


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    return s.encode("cp932", "replace")[:limit - 1].ljust(limit, b"\x00")


def build(rooms):
    """`rooms` = [(room_id, name, channel, players, tables, open_tables, gate), ...].

    Gate must be 1 to be listed at all, and the list must not be empty.
    """
    if not rooms:
        raise ValueError("an empty room list is NOT a safe 'no rooms' -- "
                         "lobbysub__002b6210 leaves the row count unwritten and "
                         "zonewin__ZoneListPage_003401f0 loops on that stack "
                         "slot with a NULL row pointer. Serve at least one room.")
    if len(rooms) > MAX_ROOMS:
        raise ValueError("%d rooms; the buffer holds %d" % (len(rooms), MAX_ROOMS))
    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, COUNT_OFF, len(rooms))
    for i, (rid, name, channel, players, tables, open_tables, gate) in enumerate(rooms):
        if gate != 1:
            raise ValueError("room %r has +0x20 = %r; Room_List_Analyz_Now tests "
                             "== 1 exactly and SKIPS anything else" % (name, gate))
        if not channel:
            raise ValueError("room %r has no +0xB8 channel name; sqMgCpEnterTable "
                             "refuses when its first byte is NUL" % name)
        if not 0 <= open_tables <= 0x5F:
            raise ValueError("room %r has %d open tables; the byte at +0x86 is "
                             "stored with a +0x20 bias in a SIGNED byte"
                             % (name, open_tables))
        if open_tables > tables:
            raise ValueError("room %r shows %d open of %d total tables"
                             % (name, open_tables, tables))
        o = HDR + i * REC
        struct.pack_into("<Q", buf, o + F_ROOMID, rid)
        struct.pack_into("<I", buf, o + F_PLAYERS, players)
        struct.pack_into("<I", buf, o + F_TABLES, tables)
        struct.pack_into("<I", buf, o + F_GATE, gate)
        buf[o + F_NAME:o + F_NAME + NAME_MAX] = _text(name, NAME_MAX)
        buf[o + F_OPENTABLES] = 0x20 + open_tables
        buf[o + F_CHANNEL:o + F_CHANNEL + CHANNEL_MAX] = _text(channel, CHANNEL_MAX)
    assert len(buf) == TOTAL
    return bytes(buf)


def dump(blob):
    n = struct.unpack_from("<I", blob, COUNT_OFF)[0]
    print("total %d B, count = %d (max %d)" % (len(blob), n, MAX_ROOMS))
    shown = 0
    for i in range(min(n, MAX_ROOMS)):
        o = HDR + i * REC
        gate = struct.unpack_from("<I", blob, o + F_GATE)[0]
        name = blob[o + F_NAME:o + F_NAME + NAME_MAX].split(b"\x00")[0]
        chan = blob[o + F_CHANNEL:o + F_TAIL].split(b"\x00")[0]
        shown += gate == 1
        print("  [%3d] id=%d +0x20=%d%s name=%r chan=%r players=%d tables=%d "
              "(Open Tables draws %d)" % (
                  i, struct.unpack_from("<Q", blob, o + F_ROOMID)[0], gate,
                  "" if gate == 1 else "  <-- SKIPPED, gate must be exactly 1",
                  name.decode("cp932", "replace"), chan.decode("cp932", "replace"),
                  struct.unpack_from("<I", blob, o + F_PLAYERS)[0],
                  struct.unpack_from("<I", blob, o + F_TABLES)[0],
                  struct.unpack_from("<b", blob, o + F_OPENTABLES)[0] - 0x20))
    print("Jan will render %d of %d." % (shown, n))


def default_rooms(zone):
    """Four rooms, all gated in, ids and channels unique ACROSS zones.

    Names ASCII on purpose: garbage on screen then means the encoding is wrong
    rather than the layout. The counters differ per row so a screenshot says
    which row is which. The channel name shape is a GUESS -- the client only
    requires it to be non-empty and 12 chars or fewer; the server answers a JOIN
    for any channel.
    """
    return [(zone * 100 + i,
             "Test Room %d-%d" % (zone, i),
             "#MJS0R%03d" % (zone * 10 + i),
             i * 2,            # players
             i,                # total tables
             i,                # of which open
             1) for i in range(1, 5)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zone", type=int, default=1,
                    help="zone id -- picks the room ids, names and channels")
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        blob = build(default_rooms(a.zone))
        dump(blob)
        out = a.out
        if not out and a.zone is not None:
            out = "data/resources/8.b_g_RL%03d.bin" % a.zone
        if out:
            with open(out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (out, len(blob)))
