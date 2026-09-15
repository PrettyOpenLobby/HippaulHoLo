#!/usr/bin/env python3
"""Build Janhourou's `b/g/PTL` -- the room's PLAYER and TABLE list.

THIRD AND LAST LINK OF THE LOBBY CHAIN. `b/g/ZL` picks a parlour, `b/g/RL%03d`
picks a room, and confirming a room makes `roommain__TableSelectMain_0033f010`
call `sqMgCpEnterRoom` and then spin here:

    do { Request_Game_Start(); lobbysub__002b7160(m); } while (lobbysub__002b7530(m) == 0);

`lobbysub__002b7530` returns the **table count**, so

    WARNING: A `b/g/PTL` WITH NO USABLE TABLE HANGS THE ROOM SCREEN FOREVER. WARNING:

`002b7160` re-polls the cp layer's in-memory arrays every 0x78 frames (~2 s) and
never re-fetches the file, so the client sits there quietly asking us for
nothing -- indistinguishable from a dead client, and the same silent-hang shape
as the zone list's `+0x2C` host. Serve at least one table that passes BOTH gates
below.

THE CONTAINER, off `cp__002fc608` and `cp__002fb560` (MIPS, not the decompiler --
Ghidra blurs these copy loops with composed induction variables):

    +0x00 .. +0x3F   not read by the unpacker
    +0x40  u32   SERIAL -- see "the serial is a sequence number" below
    +0x44  s32   MEMBER count      records from +0x0050, stride  88 (0x58)
    +0x48  s32   TABLE  count      records from +0x5850, stride 104 (0x68)

and the geometry closes twice with no slack, which is the check that the strides
were read right:

    0x50   + 256 *  88 == 0x5850          0x5850 + 256 * 104 == 0xC050 == 49232

`cp__002fc608` reads exactly 0xC050 = 49232 bytes into `0x003fb8a8`, logs
`sqMg PTList Load Finished: %d`, and the path is the literal `b/g/PTL` with no
id in it (`mg__002f61c8`). **The room id rides as the fourth argument of the
read**, not in the path, so a per-room list has to be keyed on that.

WARNING: **THE DECLARED LENGTH MUST BE 23028, AND ONLY `POL_RESOURCE_PAYLEN` SETS IT.**
49232 is the READER'S BUFFER SIZE, not the content length, and STATUS §8's
standing finding applies: a 3:0 reply declared at the reader's buffer size is
truncated on the wire -- the PS2 takes ~43 KB and then waits for the rest for
ever, with no error and nothing on the wire. Confirmed twice on 2026-08-17, the
second time as a hang on 「テーブルリストの情報を取得中です。」 with
`b/g/PTL: serving 49236B` as the last line in the log.

WARNING: **CORRECTION, and it cost two launches: TRIMMING THE FILE DOES NOT SET THE
LENGTH.** `2f3104fe` claimed `responders.py` declares a stored resource at its
own file size + 4 and that trimming therefore retires the override. That is
FALSE for this path. The `filesize + 4` branches are path-specific -- mail
objects, the auction lists and `_RANK_LIST_PATH`. For `b/g/PTL` the declared
length is the `_FETCH_PATHLEN` constant (49232 + 4), overridable ONLY by
`POL_RESOURCE_PAYLEN`, so `docker-compose.yml` carries `b/g/PTL=23028` and must
keep carrying it. WARNING: It is an ENV VAR: editing compose is not enough, the `login`
container has to be recreated.

The CLI still trims to `content_length()` = `0x5850 + tables*104` (23024 for
four tables) so the file matches the declaration exactly rather than being
padded past it, and `--full` writes the whole container. But the trim is
cosmetic; the override is what makes it work.

WARNING: **`b/g/PTL` resolves to ONE file per member** (`<member>.b_g_PTL.bin`), so the
two games genuinely share the path -- whichever tool wrote it last is what both
read. Tetra Master's rooms render nothing because members 1 and 16 have no such
file at all and fall through to the zero-fill (count 0 = no tables, at any
length), NOT because of the declared length. Sixteen tables would be
`0x5850 + 16*104 + 4 = 24276`, and moving the override there needs both tracks
to agree.

WARNING: **THE COUNTS ARE NOT BOUNDS-CHECKED.** `cp__002fb560` copies `blob[+0x44]`
records into the client's array without testing its capacity. The consumer's own
buffers (`lobbysub__002b7060`) are 0x5810 at stride 0x58 = **256 members** and
0x1a00 at stride 0x68 = **64 tables**, so those are the ceilings this tool
enforces -- 64 tables, not the file's 256 slots.

TWO GATES, and a record must pass both to be seen:

    cp__002fc388        members: the u64 id at +0x00 must be NON-ZERO
                        tables:  the BYTE at +0x18 must be non-zero -- which is
                                 the first character of the table's name, so
                                 "unnamed" and "absent" are the same thing
    lobbysub__002b71e0  tables:  the u32 at +0x10 must be EXACTLY 1

THE MEMBER RECORD (88 bytes). Named by SE's own PlayerList dump
(`mg__002f66f8`, format `$%08X%08X %8d $%02x $%04x (%8s) %d %d(%d) %d %d
$%08X%08X (%s)` -- the argument order is only visible in the MIPS, since the EE
ABI passes eight of them in a0..a3/t0..t3):

    +0x00  u64   member id      (0 = empty slot -- the gate)
    +0x08  u64   a second id
    +0x10  u32                  +0x24  u16
    +0x14  u32                  +0x26  u8
    +0x18  u32                  +0x27  u8
    +0x1C  u32   CHARACTER LEVEL -- the row draws "Lv%2d" from it
    +0x20  u32                  +0x28  16 B  NAME  (the row's ZoneName widget)
                                +0x38  32 B  a second string  -> 0x58 = 88

THE TABLE RECORD (104 bytes). Named by the TableList dump (`mg__002f6780`,
`%s $%08X%08X %d %d %d/%d (%s)`) and by the row renderer
(`roomwin__TableTitleStr3_0033bf70` + `roomwin__LockIcon_0033c590`):

    +0x00  u64   table id
    +0x08  u32   CAPACITY   \  the dump prints these as "%d/%d" = seated/capacity
    +0x0C  u32   SEATED     /  and the row draws one pin per seat:
                              Pin_ORG >= 1, Pin_Red >= 2, Pin_PP >= 3, Pin_G >= 4
    +0x10  u32   must be exactly 1 -- the second gate
    +0x14  u8    STATE, an 8-value enum (below)
    +0x15  3 B
    +0x18  13 B  NAME -- 13 bytes is the channel-name width the room record's
                 +0xB8 uses too, and "Join Table Channel Name = %s" is logged on
                 entry, so this is very probably the table's IRC channel
    +0x25  3 B
    +0x28  64 B  THE PARAMETER CSV -- 14 fields, see below   -> 0x68 = 104

THE PARAMETER CSV AT +0x28 -- 2026-08-17, and it is NOT an opaque string.
`lmenu__002b2620` sscanf's it with the format at JanHouRou.pex 0x00414e40:

    "%5d,%5d,%3d,%3d,%3d,%1d,%2d,%2d,%1d,%1d,%1d,%1d,%3d,%1d"

into a 0x30-byte struct. WARNING: **The field order is only correct in the MIPS.** The
EE ABI passes six of the fourteen output pointers in a2..t3 and the other eight
on the stack, so Ghidra renders them scrambled; read at 0x002b2620 the stores
are in CSV order 1:1:

    out+0x00 s16 = f1      out+0x08 u32 = f5      out+0x1c u32 = f10
    out+0x02 u16 = f2      out+0x0c u32 = f6      out+0x20 u32 = f11
    out+0x04 u8  = f3      out+0x10 u32 = f7      out+0x24 u32 = f12
    out+0x05 u8  = f4      out+0x14 u32 = f8      out+0x28 u32 = f13
                           out+0x18 u32 = f9      out+0x2c u32 = f14

WARNING: **NOTHING IN THE MODULE WRITES THIS STRING.** Both xrefs to the format are
sscanf (`lmenu__002b2620` over a table record, `lmenu__002b2770` over a bare
string). The client only ever parses it, so **the server authors it** -- and
serving an empty +0x28 makes all fourteen fields zero on every screen that
reads it, which is eighteen call sites: the room screen, the table screen, the
rules screen (`ruleset.c`), the banish screen and the table-list sort.

WHAT THE FIELDS ARE, where SE's own code names them:

    f1   the TABLE NUMBER. `roomwin__0033d650` draws it as "% 2d番卓：%s" with
         the state label -- so f1 = 0 is the "00" that was drawn over every
         table before this field existed.
    f2   \  the key `lglobal__002ae880` and `ruleset.c` pass to the
    f3   /  `b/g/MJSTableInfoSub` fetch, alongside the table's u64 id
    f5   the table-list SORT key      (`lobbysub__002b79e0`)
    f6   filter predicate A           (same, via lobbysub__002b7f50)
    f8   TitleSet.ReservedSeat        (its own trace string, 0x0041fb40)
    f10  filter predicate B           (`lobbysub__002b79e0`)

    f4, f7, f9, f11, f12, f13, f14 are parsed and stored but no consumer has
    been read that names them. They are authored as 0 and that is a CHOICE,
    not a measurement.

WARNING: **THE WIDTHS ARE A CEILING, not padding.** `%3d` reads at most three digits,
so a value wider than its field is silently split across the following comma
and every later field shifts. `table_params()` refuses that rather than
emitting a string the client will misparse.

THE STATE ENUM at +0x14, read off `roomwin__LockIcon_0033c590`, which is a
switch with SE's own labels on the arms:

    0  the OpeningTbl widget    -- a free table
    1  設定中   being configured
    2  募集中   RECRUITING -- accepting players
    3  待機中   waiting
    4  the Ready widget
    5  対局中   in play
    6  終了中   finishing
    7  封鎖中   closed
    anything else -> "ありえないけどきちゃった…" is logged and the row BLANKS.

THE SERIAL AT +0x40 is a sequence number, not a version stamp: after the load,
`cp__002fb560` drains an update ring and applies any queued record whose own
serial is exactly `serial + 1`, bumping the stored serial as it goes. So
incremental table/member updates ride the wire keyed to this number.

WARNING: MEASURED: the container geometry, both strides, both counts, both gates, the
two ceilings, the state enum, and every field named above. NOT MEASURED: what
member +0x08..+0x20 (except the level) mean, what the two trailing strings hold,
and whether the table name really is the channel name.

    python tools/janptl.py --out data/resources/8.b_g_PTL.bin
    python tools/janptl.py --dump data/resources/8.b_g_PTL.bin
"""
import argparse
import struct

TOTAL = 49232           # cp__002fc608: mg__002f5c58(..., 0xc050, ...)
SERIAL_OFF = 0x40
MEMBER_COUNT_OFF = 0x44
TABLE_COUNT_OFF = 0x48

MEMBER_OFF = 0x0050
MEMBER_REC = 88         # 0x58
MEMBER_SLOTS = 256      # and 0x50 + 256*88 == 0x5850 exactly

TABLE_OFF = 0x5850
TABLE_REC = 104         # 0x68
TABLE_SLOTS = 256       # and 0x5850 + 256*104 == 49232 exactly

#: The CONSUMER's buffers, from lobbysub__002b7060 -- and cp__002fb560 does not
#: bounds-check, so these are the real ceilings.
MEMBER_MAX = 0x5810 // MEMBER_REC       # 256
TABLE_MAX = 0x1A00 // TABLE_REC         # 64

# member record
M_ID = 0x00             # u64 -- non-zero or cp__002fc388 drops the row
M_ID2 = 0x08            # u64
M_LEVEL = 0x1C          # u32 -- drawn as "Lv%2d"
M_NAME = 0x28           # 16 bytes
M_TEXT2 = 0x38          # 32 bytes  -> 0x58

# table record
T_ID = 0x00             # u64
T_CAPACITY = 0x08       # u32
T_SEATED = 0x0C         # u32 -- 0..4, one pin each
T_GATE = 0x10           # u32 -- must be exactly 1
T_STATE = 0x14          # u8
T_NAME = 0x18           # 13 bytes -- non-zero first byte or cp__002fc388 drops it
T_TEXT2 = 0x28          # 64 bytes, the parameter CSV  -> 0x68

NAME_MAX = M_TEXT2 - M_NAME             # 16
TEXT2_MAX = MEMBER_REC - M_TEXT2        # 32
T_NAME_MAX = 13
T_TEXT2_MAX = TABLE_REC - T_TEXT2       # 64

#: The sscanf format at JanHouRou.pex 0x00414e40, as (name, max digits). Order
#: is CSV order, which is also the parsed struct's order -- checked in MIPS at
#: 0x002b2620, NOT taken from the decompiler.
CSV_FIELDS = [
    ("number", 5),          # f1  the table number, drawn as "% 2d番卓：%s"
    ("info_key1", 5),       # f2  \ the b/g/MJSTableInfoSub request key
    ("info_key2", 3),       # f3  /
    ("f4", 3),              # f4  parsed, no consumer read
    ("sort", 3),            # f5  the table-list sort key
    ("filter_a", 1),        # f6  filter predicate A
    ("ready_mask", 2),      # f7  per-seat READY bitmask (bit N = seat N "in
                            #     content") -- lmenu.c:840/854,
                            #     tablememberwindow.c:444/465 (2026-09-04)
    ("reserved_seat", 2),   # f8  TitleSet.ReservedSeat
    ("master_seat", 1),     # f9  the MASTER's seat 0..3 -- drawn first under
                            #     "Table Master" (lmenu.c:837, tmw.c:439)
    ("filter_b", 1),        # f10 filter predicate B
    ("f11", 1),             # f11 parsed, no consumer read
    ("f12", 1),             # f12 parsed, no consumer read
    ("f13", 3),             # f13 parsed, no consumer read
    ("f14", 1),             # f14 parsed, no consumer read
]


def table_params(**over):
    """The 14-field CSV for a table record's +0x28, as bytes.

    Every field defaults to 0; pass any of the `CSV_FIELDS` names to set one.
    Raises if a value will not fit its sscanf width, because overflowing a
    field does not truncate -- it shifts every field after it.
    """
    unknown = set(over) - {n for n, _ in CSV_FIELDS}
    if unknown:
        raise ValueError("no such CSV field: %s" % ", ".join(sorted(unknown)))
    out = []
    for name, width in CSV_FIELDS:
        v = int(over.get(name, 0))
        text = "%d" % v
        if len(text) > width:
            raise ValueError(
                "%s = %d needs %d digits but its conversion is %%%dd; sscanf "
                "would take only the first %d and shift every later field"
                % (name, v, len(text), width, width))
        out.append(text)
    s = ",".join(out).encode("ascii")
    if len(s) >= T_TEXT2_MAX:
        raise ValueError("the CSV is %d bytes and +0x28 is %d with no room for "
                         "a terminator" % (len(s), T_TEXT2_MAX))
    return s.ljust(T_TEXT2_MAX, b"\x00")


def parse_params(raw):
    """Inverse of `table_params`, for --dump. Returns {name: value}."""
    text = raw.split(b"\x00")[0].decode("ascii", "replace")
    parts = text.split(",")
    out = {}
    for (name, _), part in zip(CSV_FIELDS, parts):
        try:
            out[name] = int(part)
        except ValueError:
            out[name] = None
    return out


#: roomwin__LockIcon_0033c590's switch arms, for --dump and for validation.
STATES = {
    0: "open (OpeningTbl)",
    1: "setting up",
    2: "recruiting",
    3: "waiting",
    4: "ready",
    5: "in play",
    6: "finishing",
    7: "closed",
}


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    return s.encode("cp932", "replace")[:limit - 1].ljust(limit, b"\x00")


def build(members, tables, serial=1, csv=True):
    """`members` = [(id, name, level), ...]; `tables` = [(id, name, seated,
    capacity, state), ...] with an optional 6th element, a dict of CSV field
    overrides for `table_params`. Returns the 49232-byte blob."""
    if not tables:
        raise ValueError("a PTL with no tables hangs TableSelectMain forever -- "
                         "lobbysub__002b7530 returns the table count and the "
                         "screen spins until it is non-zero. Serve one.")
    if len(members) > MEMBER_MAX:
        raise ValueError("%d members; the client's own array holds %d and "
                         "cp__002fb560 does not bounds-check the count"
                         % (len(members), MEMBER_MAX))
    if len(tables) > TABLE_MAX:
        raise ValueError("%d tables; the client's own array holds %d and "
                         "cp__002fb560 does not bounds-check the count"
                         % (len(tables), TABLE_MAX))

    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, SERIAL_OFF, serial)
    struct.pack_into("<i", buf, MEMBER_COUNT_OFF, len(members))
    struct.pack_into("<i", buf, TABLE_COUNT_OFF, len(tables))

    for i, (mid, name, level) in enumerate(members):
        if mid == 0:
            raise ValueError("member %r has id 0; cp__002fc388 reads that as an "
                             "empty slot and drops the row" % name)
        o = MEMBER_OFF + i * MEMBER_REC
        struct.pack_into("<Q", buf, o + M_ID, mid)
        struct.pack_into("<I", buf, o + M_LEVEL, level)
        buf[o + M_NAME:o + M_NAME + NAME_MAX] = _text(name, NAME_MAX)

    for i, row in enumerate(tables):
        (tid, name, seated, capacity, state), over = row[:5], (row[5] if len(row) > 5 else {})
        if not name:
            raise ValueError("table %d has no name; cp__002fc388 gates on the "
                             "first BYTE at +0x18 and drops it" % tid)
        if state not in STATES:
            raise ValueError("table %r has state %r; LockIcon's switch has arms "
                             "0..7 and its default BLANKS the row" % (name, state))
        if not 0 <= seated <= 4:
            raise ValueError("table %r seats %d; the row draws one pin per seat "
                             "and there are four" % (name, seated))
        o = TABLE_OFF + i * TABLE_REC
        struct.pack_into("<Q", buf, o + T_ID, tid)
        struct.pack_into("<I", buf, o + T_CAPACITY, capacity)
        struct.pack_into("<I", buf, o + T_SEATED, seated)
        struct.pack_into("<I", buf, o + T_GATE, 1)
        buf[o + T_STATE] = state
        buf[o + T_NAME:o + T_NAME + T_NAME_MAX] = _text(name, T_NAME_MAX)
        # +0x28: the parameter CSV. `number` defaults to the 1-based row so the
        # screen draws "1番卓", "2番卓", ... instead of the "00" an empty field
        # gave every table; the MJSTableInfoSub key defaults to the table id's
        # low bits so distinct tables ask for distinct rule blobs.
        if csv:
            params = dict(number=i + 1, info_key1=tid & 0xFFFF, sort=i + 1)
            params.update(over)
            buf[o + T_TEXT2:o + T_TEXT2 + T_TEXT2_MAX] = table_params(**params)
        # else: leave +0x28 all-NUL, which is the pre-2026-08-17 wire shape.
        # Note that is NOT the same as a CSV of zeros -- sscanf matches nothing
        # on an empty string and leaves the client's own stack in the fields.

    assert len(buf) == TOTAL
    return bytes(buf)


def content_length(n_tables):
    """The bytes of the container that actually carry data, up to the last
    table record. This is what the file on disk should be -- see `--full`."""
    return TABLE_OFF + n_tables * TABLE_REC


def dump(blob):
    serial = struct.unpack_from("<I", blob, SERIAL_OFF)[0]
    nm = struct.unpack_from("<i", blob, MEMBER_COUNT_OFF)[0]
    nt = struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0]
    print("total %d B, serial = %d, %d members (max %d), %d tables (max %d)"
          % (len(blob), serial, nm, MEMBER_MAX, nt, TABLE_MAX))

    shown_m = 0
    for i in range(min(max(nm, 0), MEMBER_SLOTS)):
        o = MEMBER_OFF + i * MEMBER_REC
        mid = struct.unpack_from("<Q", blob, o + M_ID)[0]
        name = blob[o + M_NAME:o + M_TEXT2].split(b"\x00")[0]
        shown_m += mid != 0
        print("  member[%3d] id=%d%s name=%r Lv%d" % (
            i, mid, "" if mid else "  <-- DROPPED, id 0 is an empty slot",
            name.decode("cp932", "replace"),
            struct.unpack_from("<I", blob, o + M_LEVEL)[0]))

    shown_t = 0
    for i in range(min(max(nt, 0), TABLE_SLOTS)):
        o = TABLE_OFF + i * TABLE_REC
        gate = struct.unpack_from("<I", blob, o + T_GATE)[0]
        name = blob[o + T_NAME:o + T_NAME + T_NAME_MAX].split(b"\x00")[0]
        state = blob[o + T_STATE]
        why = []
        if not name:
            why.append("no name (+0x18 byte is 0)")
        if gate != 1:
            why.append("+0x10 = %d, not 1" % gate)
        if state not in STATES:
            why.append("state %d has no arm" % state)
        shown_t += not why
        print("  table [%3d] id=%d name=%r %d/%d state=%d (%s)%s" % (
            i, struct.unpack_from("<Q", blob, o + T_ID)[0],
            name.decode("cp932", "replace"),
            struct.unpack_from("<I", blob, o + T_SEATED)[0],
            struct.unpack_from("<I", blob, o + T_CAPACITY)[0],
            state, STATES.get(state, "NO SUCH STATE"),
            "  <-- DROPPED: " + "; ".join(why) if why else ""))
        csv = blob[o + T_TEXT2:o + T_TEXT2 + T_TEXT2_MAX]
        p = parse_params(csv)
        if not csv.split(b"\x00")[0]:
            print("              +0x28 CSV EMPTY -- sscanf matches nothing, so "
                  "the table number draws as 00 and its rule key is {id,0,0}")
        else:
            print("              +0x28 %s" % csv.split(b"\x00")[0].decode("ascii", "replace"))
            print("              number=%s infokey=(%s,%s) sort=%s reserved_seat=%s"
                  % (p.get("number"), p.get("info_key1"), p.get("info_key2"),
                     p.get("sort"), p.get("reserved_seat")))

    print("Jan will render %d of %d members and %d of %d tables."
          % (shown_m, max(nm, 0), shown_t, max(nt, 0)))
    if shown_t == 0:
        print("*** NO TABLE SURVIVES -- TableSelectMain will spin forever. ***")


#: One recruiting table so the screen can advance, plus three in other states so
#: a screenshot exercises the enum and says which is which. ASCII on purpose:
#: garbage on screen then means the encoding is wrong rather than the layout.
DEFAULT_TABLES = [
    (1, "#MJS0T001", 0, 4, 2),      # recruiting -- the one that matters
    (2, "#MJS0T002", 2, 4, 3),      # waiting, half full
    (3, "#MJS0T003", 4, 4, 5),      # in play, full
    (4, "#MJS0T004", 0, 4, 0),      # open
]
DEFAULT_MEMBERS = [
    (0x1000000000000001, "PS2Tester", 1),
]

#: `--ready`: table 01 in state 4 with a seat taken. THE TABLE MENU IS GATED ON
#: THIS RECORD, entirely -- `TableMenu__0034e0f0` reads the table record at
#: ctx+0xa0 and enables each item off it (measured 2026-08-17, and every greyed
#: item in that session's screenshot matched):
#:
#:     Start Game            state == 4          (SE's label: すぐゲームできます)
#:     Change Table Settings state in {2,3,4}
#:     強制退場              state in {2,3,4}
#:     Spectate              state == 5
#:     Table Members         seated != 0         (record +0x0c)
#:
#: So a table stuck in state 2 (募集中) can never start a game, however many
#: players are on it. WARNING: Whether a real server drives 2 -> 4 on a seat count, on a
#: master command, or on a timer is NOT established -- this flag just asserts the
#: state so the MjGAMESTART path can be reached without four consoles.
READY_TABLE = (1, "#MJS0T001", 1, 4, 4)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    ap.add_argument("--serial", type=int, default=1)
    ap.add_argument("--ready", action="store_true",
                    help="put table 01 in state 4 (Ready) with one seat taken, "
                         "so the master's Start Game item ungreys. See "
                         "READY_TABLE")
    ap.add_argument("--no-csv", action="store_true",
                    help="leave +0x28 empty, as it was before 2026-08-17. Use "
                         "this to reproduce the pre-CSV wire bytes exactly when "
                         "bisecting -- the tables then draw as table 00 again")
    ap.add_argument("--full", action="store_true",
                    help="write all %d bytes instead of trimming to the last "
                         "table record. WARNING: THIS TRUNCATES ON THE WIRE -- see the "
                         "note in the module docstring" % TOTAL)
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        tables = list(DEFAULT_TABLES)
        if a.ready:
            tables[0] = READY_TABLE
        blob = build(DEFAULT_MEMBERS, tables, a.serial, csv=not a.no_csv)
        dump(blob)
        if not a.full:
            blob = blob[:content_length(len(tables))]
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B, declared as %d)%s"
                  % (a.out, len(blob), len(blob) + 4,
                     "  WARNING: FULL -- the PS2 will truncate this" if a.full else ""))
