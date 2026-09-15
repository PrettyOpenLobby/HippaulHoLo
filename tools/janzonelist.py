#!/usr/bin/env python3
"""Build Janhourou's `b/g/ZL` zone list.

WHY THIS IS NOT `tmzonelist.py`. The container is the same -- `b/g/ZL` is built by
the shared `mg`/`cp` library that is statically linked into BOTH game modules
(`JanHouRou.pex` carries the literal at 0x199ef0, right beside Tetra Master's
`U/g/%s_BIDLIST`), and Jan reads it at the same 0x848 = 2120 bytes
(`cp__002fc740`). But **Jan gates each record on a field Tetra Master never
reads**, so a list that renders in TM draws EMPTY in Jan. That cost one live
launch; this tool exists so it costs none again.

THE READER, from the decompiled lobbysub.c,
`lobbysub__Copy_Zone_Information_002b58b0`:

    for (i = 0; i < *(int *)(buf + 0x40); i++) {        # count at +0x40
        if (*(int *)(buf + off + 0x50) != 0) {          # record +0x08 -- THE GATE
            dst[0] = *(u32 *)(rec + 0x48);              # record +0x00
            dst[1] = *(u32 *)(rec + 0x4c);              # record +0x04
            dst[2] = *(u32 *)(rec + 0x50);              # record +0x08 (copied too)
            copy 0x10 * 2 bytes from rec + 0x54;        # record +0x0C, 32 bytes
            copy 8 * 2 bytes    from rec + 0x74;        # record +0x2C, 16 bytes
            dst[0xf] = *(u8 *)(rec + 0x84);             # record +0x3C
            copy 3 bytes        from rec + 0x85;        # record +0x3D
        }
        off += 0x40;                                     # stride 64
    }
    *param_1 = accepted;                                 # ZERO accepted -> 0 pages

**A record whose +0x08 is zero is silently skipped**, and if none survive the
page count is 0 and the screen shows an empty list with no error -- exactly the
symptom. Both hand-authored lists we had (`8.*` and the TM-derived `1.*`) carried
+0x08 = 0, which is why neither drew.

The geometry closes exactly, which is the check that the reading is right: record
i occupies buf + i*0x40 + 0x48 .. + 0x88, so record 31 ends at 1984 + 0x88 =
**2120**, the full buffer, with no slack.

WARNING: **THE `%03d` IN `b/g/RL%03d` COMES FROM `+0x3C`, NOT `+0x00`** (corrected
2026-08-16; the earlier reading here was wrong and the live test could not tell,
because the file it was run against carried the same value in both fields).
`zonesel__ZoneSelectMain` copies the chosen row to `0x004466C0`, and
`zonemain__RoomSelectMain_00340ab0` does

    0x00340bb8  lb   a1, 0x004466fc      # = 0x004466C0 + 0x3C, a SIGNED BYTE
    0x00340bc0  jal  0x002b5c60          # list ctx, ctx[0] = that byte
    ...  First_Update_Start -> cp__002fc878(buf, ctx[0]) -> "b/g/RL%03d"

So the room-list number is **one signed byte at record +0x3C** (0..127 in
practice), and it is what a server must vary per zone. Four zones all carrying
+0x3C = 1 all fetch `RL001`, however distinct their +0x00 is.

**+0x00 and +0x04 are the two counters on the row**, read off the renderer
`zoneselw__0033a9d0` (the compaction at `Copy_Zone_Information` is 1:1 on
offsets, so a row offset IS a file offset):

    ZoneName  <- rec + 0x0C        BodyCount <- rec + 0x00 (u32, "%d")
                                   RoomCount <- rec + 0x04 (u32, "%d")

WARNING: MEASURED: the offsets, the widths, the stride, the count, the gate, the three
rendered fields and the id. NOT MEASURED: what +0x08 means beyond being the
gate, and what +0x3D..+0x3F hold.

WARNING: **`+0x2C` IS A SERVER ADDRESS, NOT A CHANNEL NAME.** `tmzonelist.py` reads it
as "the zone's IRC channel" from TM's side; Jan's side shows it is the host the
client DIALS. `zonesel__ZoneSelectMain_00339cb0`, on confirming a zone:

    lobbysub__002b5b20(ctx, &DAT_004466c0, ...)   # copy the chosen row here
    cp__002fa918();                               # IRC disconnect start
    do { r = cp__002fa988(); } while (r < 1);     # wait for it
    r = lmenu__002b2560(&DAT_004466ec);           # IRCOpen(...)  <-- 0x4466c0+0x2C
    if (r < 0) { "IRCOpen Error: %s"; while(true); }        # HARD HANG
    do { r = lmenu__002b25d0(); } while (r < 1);  # wait for connect
       if (r < 0) { "IRC Server Connection Error: %s"; while(true); }

`DAT_004466ec` is `DAT_004466c0 + 0x2C` exactly, and both error paths print it
with `%s` and then spin in `console__Task_Manager_Start` forever. So a zone whose
+0x2C is not a dialable host is a **silent, permanent hang with no network
traffic at all** -- which is what `#MJS0Z001` produced. Jan's world host is
`gi003.pol.com` ("gi" + content id 3), 13 chars inside the 16-byte field.

    python tools/janzonelist.py --out data/resources/8.b_g_ZL.bin
    python tools/janzonelist.py --dump data/resources/8.b_g_ZL.bin
"""
import argparse
import struct

TOTAL = 2120            # cp__002fc740: mg__002f5c58(..., 0x848, ...)
HDR = 0x48              # records start here
REC = 0x40              # off += 0x40 in Copy_Zone_Information
COUNT_OFF = 0x40        # loop bound
MAX_ZONES = (TOTAL - HDR) // REC          # 32, and 0x48 + 32*0x40 == 2120 exactly

F_PLAYERS = 0x00        # -> dst[0], rendered as "BodyCount"
F_ROOMS = 0x04          # -> dst[1], rendered as "RoomCount"
F_GATE = 0x08           # -> dst[2] AND the skip test
F_NAME = 0x0C           # 32 bytes
F_CHANNEL = 0x2C        # 16 bytes -- the DIALED HOST, not a channel
F_ID = 0x3C             # s8 -- THE %03d of b/g/RL%03d
F_TAIL = 0x3D           # 3 bytes

NAME_MAX = F_CHANNEL - F_NAME       # 32
CHANNEL_MAX = F_ID - F_CHANNEL      # 16


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    return s.encode("cp932", "replace")[:limit - 1].ljust(limit, b"\x00")


def build(zones):
    """`zones` = [(zone_id, players, rooms, gate, name, host), ...].

    `zone_id` is the +0x3C byte, i.e. the N of `b/g/RL%03d`.
    """
    if len(zones) > MAX_ZONES:
        raise ValueError("%d zones; the buffer holds %d" % (len(zones), MAX_ZONES))
    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, COUNT_OFF, len(zones))
    for i, (zid, players, rooms, gate, name, host) in enumerate(zones):
        if gate == 0:
            raise ValueError("zone %r has +0x08 = 0; Copy_Zone_Information "
                             "SKIPS it and the list draws empty" % name)
        if not 0 <= zid <= 127:
            raise ValueError("zone %r has id %d; +0x3C is loaded with `lb`, so "
                             "anything outside 0..127 asks for a negative or "
                             "wrapped RL path" % (name, zid))
        o = HDR + i * REC
        struct.pack_into("<I", buf, o + F_PLAYERS, players)
        struct.pack_into("<I", buf, o + F_ROOMS, rooms)
        struct.pack_into("<I", buf, o + F_GATE, gate)
        buf[o + F_NAME:o + F_NAME + NAME_MAX] = _text(name, NAME_MAX)
        buf[o + F_CHANNEL:o + F_CHANNEL + CHANNEL_MAX] = _text(host, CHANNEL_MAX)
        buf[o + F_ID] = zid
    assert len(buf) == TOTAL
    return bytes(buf)


def dump(blob):
    n = struct.unpack_from("<I", blob, COUNT_OFF)[0]
    print("total %d B, count = %d (max %d)" % (len(blob), n, MAX_ZONES))
    shown = 0
    for i in range(min(n, MAX_ZONES)):
        o = HDR + i * REC
        gate = struct.unpack_from("<I", blob, o + F_GATE)[0]
        name = blob[o + F_NAME:o + F_CHANNEL].split(b"\x00")[0]
        chan = blob[o + F_CHANNEL:o + F_ID].split(b"\x00")[0]
        shown += gate != 0
        print("  [%2d] id=%-3d -> b/g/RL%03d  players=%d rooms=%d +08=%d%s "
              "name=%r host=%r" % (
                  i, blob[o + F_ID], blob[o + F_ID],
                  struct.unpack_from("<I", blob, o + F_PLAYERS)[0],
                  struct.unpack_from("<I", blob, o + F_ROOMS)[0], gate,
                  "" if gate else "  <-- SKIPPED, +0x08 is zero",
                  name.decode("cp932", "replace"), chan.decode("cp932", "replace")))
    print("Jan will render %d of %d." % (shown, n))


#: Names are ASCII on purpose: if they come out as garbage we have learned the
#: encoding is wrong rather than the layout.
#: +0x2C is the host the client DIALS on confirm (see above), so every zone
#: points at Jan's world server. The counters differ per row on purpose: they are
#: rendered, so a screenshot tells us which row is which and confirms the two
#: fields at once.
SERVER = "gi003.pol.com"
DEFAULT = [
    #  id  players rooms gate  name              host
    (   1,      4,    4,   1, "Test Parlour 1", SERVER),
    (   2,      8,    3,   1, "Test Parlour 2", SERVER),
    (   3,     12,    2,   1, "Test Parlour 3", SERVER),
    (   4,     16,    1,   1, "Test Parlour 4", SERVER),
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        blob = build(DEFAULT)
        dump(blob)
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out, len(blob)))
