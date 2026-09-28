#!/usr/bin/env python3
"""Author and decode Janhourou's `U/g/MJSUserData` -- THE PLAYER SAVE.

THE FILE SE'S SERVER OWNED. `sqMgWriteFileCheck` has **0 callers** in the whole
of `JanHouRou.pex` and `sqMgWriteFileOffset`'s two callers are both library
plumbing inside `cp.c`/`mg.c`, so **no Janhourou code path writes the save**.
The 976 bytes are composed server-side and served,
and whatever is not in them did not happen -- the same shape as Tetra Master's
`U/g/TM0DataFile` (see `tmsave.py`).

This is the other half of `janstats.py`: that module records what a finished
hanchan produced, this one turns it into the bytes the client reads.

    python jansave.py --dump  data/resources/8.U_g_MJSUserData.bin
    python jansave.py --member 8 --out NEW.bin      # author from the record
    python jansave.py --selftest

=== THE READER ==============================================================

`lmenu__002b2a90` fetches it:

    cp__002fc9a8("U/g/MJSUserData", &DAT_00446110, 0x3d0)

so **`0x00446110` is the save buffer base** and every `_DAT_004461xx`
..`_DAT_004464xx` in the decompile is `save + (addr - 0x446110)`. `0x3D0` = 976
bytes, valid offsets `+0x000..+0x3CF`.

`lmenu__002b2ab0` is Jan's `apply_from_save`, and `OFFSETS` below is read
straight off it. WARNING: **MEASURED: the offsets, the widths and the branch. NOT
MEASURED: what any field MEANS.** Those are addresses, not semantics.

**Level / Rank / Games Played / Money are NOT in that table**: `apply_from_save`
copies out identity, two strings and three flags. The stats are read at DRAW
time straight out of the `0x00446110` buffer -- the record screen
(ScoreDispWindow.c) and the handle panel (hnlaf.c:73-77) -- and `FIELDS`
below is the table of every such read: the money four, the three title
counters, LEVEL +0x238 / RANK +0x23C (both `lw`, drawn as "Lv.%2d Rank: %s"),
and the 39 per-yaku counters at +0x11C..+0x1C8. An exhaustive `lui`-relative
scan of the whole `.text` on 2026-09-04 found NO other read of the 976 bytes
(`games_played` has no home at all -- the client counts finishes by kind).

WARNING: **DO NOT FILL `FIELDS` IN FROM A GUESS.** `+0x000` is a magic the client
validates (`0x002b2af4`) and a mismatch returns **-650**, which the account
holder has already seen once as "save data is corrupt". Everything past it is
parsed without a checksum, so a wrong offset does not error -- it silently puts
a wrong number on the screen, or moves a flag nobody was looking at. That is the
failure mode this project has lost the most time to.
"""
import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import janstats
except ImportError:                                         # pragma: no cover
    janstats = None

#: `cp__002fc9a8(..., 0x3d0)` -- the length the client asks for.
SIZE = 0x3D0                                                        # 976

#: WARNING: THE FILE ON DISK MAY BE 980, NOT 976. `responders._FETCH_PATHLEN` declares
#: `976 + 4` and the signer writes a checksum over the last 4 bytes of the reply,
#: so a stored blob can carry a 4-byte TRAILER SLOT past the content. Both
#: lengths are accepted and the slot is preserved -- trimming it would serve a
#: reply 4 bytes short, which is the wrong-length read class that stalled this
#: very path at 664. Same rule as `tmsave.build`.
TRAILER = 4

#: JanHouRou.pex 0x002b2af4. The blob is rejected with **-650** without it:
#:
#:     lw    v1, 0x00446110      ; save+0x00
#:     lui   v0, 0x0203
#:     ori   v0, v0, 0x0100      ; 0x02030100
#:     beq   v1, v0, <ok>
#:     addiu v0, zero, -650      ; else: "save data is corrupt"
MAGIC = 0x02030100
MAGIC_OFF = 0x000

#: THE BRANCH. `lmenu__002b2ab0`: zero here skips the entire identity block and
#: zeroes three flags -- the client being told it has no player yet, which is
#: what it was told on every single boot until `mk_jan_save.py`. Structurally the
#: twin of Tetra Master's guild byte at its own save +0x3B. Both paths return 1,
#: so a zero is NOT an error.
EXISTING_PLAYER = 0x3C8

#: `DAT_004460E8` -- the byte the client uses as `src` on every in-game message
#: (`console__00284380` builds its acks with `src = DAT_004460e8`). So the wire
#: identity of a seat traces back to this byte of the save. See janmsgs.py.
WIRE_SEAT = 0x3CA
#: `DAT_004460E9`. `DAT_00445E31` is then set to `(0x4460E8 == 0x4460E9)`, i.e.
#: these two are compared against each other -- the shape of a "slot N of M" pair.
WIRE_SEAT_LIMIT = 0x3C9
#: `DAT_004464DB` -- THE REJOIN GATE (finding 29). `TableMenu__Reserve_0034e120`
#: (TableMenu.c:342-347): with a reservation held at THIS table (+0x3C8 set,
#: +0x18 == the row's id) the second menu item is "Rejoin" when this byte is
#: non-zero and "Cancel Reserve" when it is zero. Rejoin sends MjENTERGAME
#: (+0x08 = the table); result 1 re-enters the game loop and waits for an
#: MjALLDATA to rebuild the board. Set when the member's table is IN PLAY.
REJOIN = 0x3CB

#: `_DAT_00446510` -- the MY-TABLE global the tile menu compares a table
#: against (`TableMenu__0034dba0`: master menu needs `ctx+0xa0 ==
#: _DAT_00446510`). `lmenu__002b2ab0` loads it from save +0x18 on EVERY fetch,
#: unconditionally, so a zero here silently un-owns the table a player already
#: reserved the next time anything re-reads the save.
MY_TABLE = 0x18


#: WHERE "Back to Room" LOOKS. Added 2026-09-21; the 2004 build validates the
#: destination before it dials, and the 2002 build has no such branch, which is
#: why these two fields were never load-bearing before.
#:
#: `ReturnRoom.cc`'s state machine (2004 0x00330CC0) polls `b/g/ZL`, then at
#: state 3 (0x00330DE8) reads the ZONE from the save and scans the list:
#:
#:     00330dec  lbu  a2, [0x004C46DD]   ; = save +0x40D in 2004 = +0x3C5 here
#:     00330dfc  jal  0x003b8c60         ; find zone by id
#:     003b8c9c    lb v0, 60(rec)        ; the parsed ZL record's +0x3C id
#:     003b8ca0    bnel a2, v0, next     ; signed byte compare
#:     00330e2c  addiu a2, zero, -13172  ; no match -> "destination zone was
#:                                       ;   not found"
#:
#: and one state later (0x0033117C) reads the ROOM the same way from save
#: +0x010 against the `b/g/RL%03d` record's +0x00, raising -13181 on a miss.
#: Served zero, both lookups miss, so the menu item could only ever fail.
RETURN_ZONE = 0x3C5             # u8  -- matches `b/g/ZL` +0x3C  (janlobby.ZL_F_ID)
RETURN_ROOM = 0x010             # u64 -- matches `b/g/RL%03d` +0x00 (RL_F_ROOMID)


def apply_return_room(blob, zone_id=None, room_id=None):
    """The save with "Back to Room"'s destination stamped in, 2002 offsets.

    `to_2004` moves +0x3C5 to +0x40D and leaves +0x010 where it is, so this is
    written in 2002 coordinates like everything else here and converted after.
    A None leaves that field alone.
    """
    buf = bytearray(blob)
    if len(buf) <= RETURN_ZONE:
        return bytes(buf)
    if zone_id is not None:
        buf[RETURN_ZONE] = int(zone_id) & 0x7F
    if room_id is not None:
        struct.pack_into("<Q", buf, RETURN_ROOM, int(room_id) & 0xFFFFFFFFFFFFFFFF)
    return bytes(buf)


def apply_seat(blob, seat=None, master_seat=None, table_id=None, rejoin=None):
    """Stamp this member's reservation into the save.

    `rejoin` (True/False) writes +0x3CB, the byte that turns the menu's
    "Cancel Reserve" into "Rejoin" -- True while the member's table has a
    game in play, so a player who dropped out can come back to it.

    WARNING: THE MASTER FLAG IS COMPUTED HERE, NOT FROM THE RESERVE ACK. `DAT_00445E31`
    is written in exactly ONE place in the whole module --

        lmenu__002b2ab0:  DAT_00445E31 = (DAT_004460E8 == DAT_004460E9)
                          i.e.          (save +0x3CA  == save +0x3C9)

    -- and the ACK arm (`TableMenuPopup` 0x224/0x372) sets those two bytes but
    never the flag. So the flag is always whatever the last SAVE FETCH computed.
    We shipped +0x3C9 and +0x3CA both zero, so `0 == 0` and EVERY CLIENT MADE
    ITSELF THE MASTER -- reported live 2026-09-04 as two players at one table
    both seeing the master menu.

    WARNING: +0x3CA is load-bearing beyond the menu: it is `DAT_004460E8`, the `src`
    byte the client stamps on every in-game message, so it must be the player's
    REAL seat and never a value chosen to make a flag come out right.
    """
    out = bytearray(blob)
    # WARNING: TAKE THE IDENTITY BRANCH, OR NONE OF THE REST IS EVEN READ.
    # `lmenu__002b2ab0` opens with `if (save[+0x3C8] == 0)` and that arm zeroes
    # DAT_00445E30 (the RESERVED flag), DAT_004460E8 and DAT_004460E9 -- the
    # exact three globals the table menu gates on. We serve this file as
    # RESOURCE_INIT, which is the 4-byte magic and 976 zeros, so +0x3C8 was 0
    # and the client threw away the seat we had just carefully stamped.
    # DAT_00445E30 == 0 is also the FIRST condition in
    # TableMenu__Reserve_0034e120, which is why the Reserve item reverted to
    # "Reserve" for a player who had just reserved (seen live 2026-09-04).
    # A player sitting at a table demonstrably exists, so stamping a seat and
    # leaving this zero is a contradiction.
    if seat is not None or master_seat is not None or table_id is not None:
        out[EXISTING_PLAYER] = 1
    if table_id is not None and len(out) >= MY_TABLE + 8:
        struct.pack_into("<Q", out, MY_TABLE, int(table_id) & 0xFFFFFFFFFFFFFFFF)
    if seat is not None and len(out) > WIRE_SEAT:
        out[WIRE_SEAT] = int(seat) & 0xFF
    if master_seat is not None and len(out) > WIRE_SEAT_LIMIT:
        out[WIRE_SEAT_LIMIT] = int(master_seat) & 0xFF
    if rejoin is not None and len(out) > REJOIN:
        out[REJOIN] = 1 if rejoin else 0
    return bytes(out)


#: Read straight off `lmenu__002b2ab0`. `(offset, width, note)`; width 0 = a
#: NUL-terminated string whose length is not established.
#:
#: WARNING: `+0x2B8` (u64) and `+0x2BA` (byte) OVERLAP as recorded. Both reads are in
#: the decompile going to different destinations (0x446100 / 0x446102), so this
#: is transcribed as measured rather than tidied up; one of the two widths is
#: probably narrower than written down. Do not build anything on that pair until
#: it is re-read.
OFFSETS = (
    (0x000, 4, "u32 MAGIC -- must be 0x02030100, else -650"),
    (0x010, 8, "u64 -> 0x446508"),
    (0x018, 8, "u64 -> 0x446510, and hdd__002a9050(4, it)"),
    (0x2B6, 1, "byte -> 0x445E30, PLUS 1"),
    (0x2B8, 8, "u64 -> 0x446100  (overlaps +0x2BA, see above)"),
    (0x2BA, 1, "byte -> 0x446102 (overlaps +0x2B8, see above)"),
    (0x2CB, 0, "STRING -> 0x446520"),
    (0x2EB, 0, "STRING -> 0x446540"),
    (0x3C5, 1, "byte -> 0x4465C0"),
    (0x3C8, 1, "THE BRANCH: 0 = blank player, identity block skipped"),
    (0x3C9, 1, "byte -> 0x4460E9"),
    (0x3CA, 1, "byte -> 0x4460E8 -- the in-game `src`, i.e. wire identity"),
)

#: PARTIAL: THE DECLARED TABLE -- one decompiler pass fills this in.
#:
#: `name -> {'off', 'width', 'signed', 'max', 'note'}`. `off = None` means the
#: destination is UNMEASURED and `apply_live` will not write it. Filling one in
#: is the whole change needed to make that stat reach the screen; nothing else
#: here or in `janstats.py` has to move.
#:
#: WHERE TO LOOK. Not `apply_from_save` -- it has been read and none of these is
#: in it. The record/profile screen draws them, so the candidates are the
#: functions that format them:
#:
#:   * the PTL member row already draws `"Lv%2d"` from ITS OWN record `+0x1C`
#:     (`roomwin__0033dcc0`, janptl.py `M_LEVEL`), so LEVEL has a second possible
#:     home on the lobby list rather than the save. WARNING: That field is NOT free:
#:     `b/g/PTL` is shared with Tetra Master and `tmroom.py` carries a captured
#:     non-zero value 7 at exactly `+0x1C`. Measure before writing either.
#:   * RANK may not live here at all -- `sqMgPfcSendGameResult2Rkgm` sends the
#:     game result to the RANKING manager over POLpro, and `pfcRankUpdResult`
#:     (`<OK>`/`<OG>`, Jan 0x002f2920) is the completion. `config/polpro.json`
#:     serves `MJS:RR` with `<LN>` = 0 rows today, so the rankings screen has
#:     nothing to draw whatever the save says.
FIELDS = {
    # --- MEASURED 2026-08-24 off ScoreDispWindow.c, the 成績 (record) screen ---
    #
    # Page 0 is titled "Winnings" and is four `%20ld JAN` money lines followed by
    # three title-progress lines. Every read site was checked in the
    # disassembly rather than trusted from the decompiler: the money four are
    # `ld` (0x00363fa8, 0x00363fd4, 0x00364000, ...) and the counters are `lw`
    # (0x003640d4, 0x00364114, 0x00364154). `%20ld` is a 20-digit column, which
    # only a doubleword can fill -- so the money fields are s64 and NOT s32.
    "money":        {"off": 0x028, "width": 8, "signed": True,
                     "max": 0x7FFFFFFFFFFFFFFF,
                     "note": "' Total won  %20ld JAN' -- the all-time balance"},
    "money_weekly": {"off": 0x030, "width": 8, "signed": True,
                     "max": 0x7FFFFFFFFFFFFFFF,
                     "note": "' Weekly won %20ld JAN'"},
    "money_today":  {"off": 0x040, "width": 8, "signed": True,
                     "max": 0x7FFFFFFFFFFFFFFF,
                     "note": "' Won today  %20ld JAN'; ALSO drives the Mahjong "
                             "King line, which prints 10000 - this"},
    "money_best":   {"off": 0x048, "width": 8, "signed": True,
                     "max": 0x7FFFFFFFFFFFFFFF,
                     "note": "' Best today %20ld JAN'"},
    # The three title-progress counters. Each is a RUNNING TOTAL that the screen
    # renders modulo the title's threshold, so the count is cumulative and the
    # client does the "to go" arithmetic:
    #     100  - top_finishes % 100   "Top finishes for King of Beasts"
    #     20   - plus_scores  % 20    "Plus scores for Winnings General"
    #     5    - last_places  % 5     "Last places for Wild Tile King"
    "top_finishes": {"off": 0x0D0, "width": 4, "signed": False,
                     "max": 0xFFFFFFFF, "note": "1st-place finishes, cumulative"},
    "last_places":  {"off": 0x22C, "width": 4, "signed": False,
                     "max": 0xFFFFFFFF, "note": "4th-place finishes, cumulative"},
    "plus_scores":  {"off": 0x230, "width": 4, "signed": False,
                     "max": 0xFFFFFFFF, "note": "games finished in plus, cumulative"},

    # --- MEASURED 2026-09-04: the handle panel and the yaku tabs ------------
    #
    # KEY: LEVEL AND RANK ARE IN THE SAVE AFTER ALL. hnlaf.c:73-77 draws the
    # "PlayerRank" widget as `"Lv.%2d Rank: %s"` from `_DAT_00446348` (+0x238,
    # `lw` at 0x002b2cd8 / 0x003420b8) and `ReturnRoom__00364c60(_DAT_0044634c)`
    # (+0x23C, `lw`, the 95-tier index 0..94). The results screen ALSO gets a
    # level from MjGAMERESULTHALF2 +0xA0 -- both come from `janstats.level_of`
    # now, so they cannot disagree (the 2026-08-24 note that level "is NOT in
    # the save" was wrong about the save and right about HALF2).
    "level":        {"off": 0x238, "width": 4, "signed": False, "max": 99,
                     "note": "'Lv.%2d' on the handle panel (hnlaf.c:77); the "
                             "SAME number rides HALF2 +0xA0"},
    "rank":         {"off": 0x23C, "width": 4, "signed": False, "max": 94,
                     "note": "the 95-tier rank index -> ReturnRoom__00364c60 "
                             "name table (0..94, else 'Error')"},
    "games_played": {"off": None, "width": 4, "signed": False,
                     "max": 0xFFFFFFFF,
                     "note": "no read site found -- the record screen counts "
                             "FINISHES by kind (top/last/plus), never a total"},
}

#: The 39 per-yaku win counters (ScoreDispWindow.c tabs 1-3, every `lw`
#: checked in the disassembly 2026-09-04; `janstats.YAKU_OFFSETS` is the
#: authority and the selftest asserts the two agree). Each renders as
#: `<yaku> %10d 回`. WARNING: SE's own off-by-one: the Haitei/Houtei line reads
#: +0x128/+0x12C (Menzen Tsumo / Haitei), so `houtei` at its intended +0x130
#: is written but never drawn.
try:
    import janstats as _js_yaku
    for _key, _off in sorted(_js_yaku.YAKU_OFFSETS.items()):
        FIELDS["yaku_" + _key] = {"off": _off, "width": 4, "signed": False,
                                  "max": 0xFFFFFFFF,
                                  "note": "%s wins, tab %d" % (
                                      _key, 3 if _key in _js_yaku.YAKUMAN_KEYS
                                      else (2 if _off in (0x11C, 0x148, 0x14C,
                                                          0x150, 0x154, 0x158,
                                                          0x15C, 0x160, 0x164,
                                                          0x168, 0x16C, 0x170,
                                                          0x174, 0x178, 0x17C,
                                                          0x180) else 1))}
except ImportError:                                         # pragma: no cover
    pass


# === THE 2004 BUILD (20040727_2) ==============================================
#
# The build on the Dirge of Cerberus and Front Mission Online discs, and the
# only one a US Viewer can install, reads a LONGER save with a DIFFERENT magic.
# Measured 2026-09-21 in the plaintext module built from the Dirge disc (module
# base 0x280000, save buffer 0x004c42d0):
#
#   0x0029c398  read("U/g/MJSUserData", 0x004c42d0, 0x418)      1048, not 976
#   0x0029c434  lui v0,0x0206 / ori v0,v0,0x0300 / beq v1,v0    magic 0x02060300
#   0x0029c448  addiu v0,zero,-650                              else JHR-650
#
# Its apply_from_save (0x0029c44c..) reads the SAME fields as the 2002 one
# (`lmenu__002b2ab0`), and every one that sits past +0x2B6 has moved by exactly
# +0x48:
#
#     2002   +0x2b6 +0x2b8 +0x2ba +0x2cb +0x2eb +0x3c5 +0x3c8 +0x3c9 +0x3ca +0x3cb
#     2004   +0x2fe +0x300 +0x302 +0x313 +0x333 +0x40d +0x410 +0x411 +0x412 +0x413
#
# while +0x000/+0x010/+0x018 and every stat read up to +0x23C (the yaku
# counters, +0x22C/+0x230, LEVEL +0x238, RANK +0x23C) are where they were. So 72
# bytes went in somewhere in [+0x240, +0x2B6), a span NO 2002 code reads and
# that every save this server authors leaves zero -- which is why the exact
# insertion point cannot matter to us. The 2004 build reads ten new u32s at
# +0x2B4..+0x2F0 out of that block; their MEANING IS NOT MEASURED and they are
# served as zero.
#
# NOT MEASURED: whether the 2004 record screen still takes money from
# +0x028/+0x030/+0x040/+0x048 -- the lui-relative scan of the 2004 text finds
# +0x040 and a new +0x0C8 but not the other three, so that screen may read
# through a pointer now. A wrong number there is cosmetic; the magic is not.
MAGIC_2004 = 0x02060300
SIZE_2004 = 0x418                                                   # 1048
INSERT_AT_2004 = 0x240
INSERT_LEN_2004 = SIZE_2004 - SIZE                                  # 72


def to_2004(blob):
    """A 2002-layout save (976, or 980 with the trailer slot) as the 2004
    build reads it: 1048 bytes, plus the trailer slot when one came in.

    A slice and a pad, not a second encoder -- same reasoning as
    `tmrank.to_ps2`. Everything this module writes is written in 2002
    coordinates first and moved here, so `apply_live` / `apply_seat` need no
    second offset table.
    """
    if len(blob) not in (SIZE, SIZE + TRAILER):
        raise ValueError("expected %d or %d bytes, got %d"
                         % (SIZE, SIZE + TRAILER, len(blob)))
    body, tail = blob[:SIZE], blob[SIZE:]
    if any(body[INSERT_AT_2004:0x2B4]):
        raise ValueError("bytes in +0x240..+0x2B4 are not zero; the insertion "
                         "point is not known well enough to move them")
    out = bytearray(body[:INSERT_AT_2004] + bytes(INSERT_LEN_2004)
                    + body[INSERT_AT_2004:])
    struct.pack_into("<I", out, MAGIC_OFF, MAGIC_2004)
    assert len(out) == SIZE_2004
    return bytes(out) + tail


def is_2004_version(version):
    """True for a patch-channel version string of the 2004-layout build.

    `20040727_2` is the only such build known. Versions from 2026 on are this
    server's own overlays, which are published on the 2002 tree.
    """
    v = (version or "")[:8]
    return v.isdigit() and "20040727" <= v < "20260000"


def measured_fields():
    """The subset of `FIELDS` that actually has a destination."""
    return {k: v for k, v in FIELDS.items() if v["off"] is not None}


def _pack_into(buf, off, width, value, signed=False):
    """Write one field. Returns (old, new) or None if it did not move.

    Width 8 exists because the money fields really are 64-bit: the read sites in
    `ScoreDispWindow__00363f70` are `ld`, not `lw`, and the format strings ask
    for `%20ld` -- a 20-digit column, which only a doubleword can fill.
    """
    if width not in (1, 2, 4, 8):
        raise ValueError("save fields are 1, 2, 4 or 8 bytes, not %r" % (width,))
    if not (0 <= off and off + width <= SIZE):
        raise ValueError("offset %#x width %d falls outside the %d-byte save"
                         % (off, width, SIZE))
    fmt = ({1: "b", 2: "h", 4: "i", 8: "q"}[width] if signed
           else {1: "B", 2: "H", 4: "I", 8: "Q"}[width])
    mask = (1 << (8 * width)) - 1
    old = struct.unpack_from("<" + fmt, buf, off)[0]
    new = int(value)
    if signed:
        lo, hi = -(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1
        new = max(lo, min(hi, new))
    else:
        new &= mask
    if old == new:
        return None
    struct.pack_into("<" + fmt, buf, off, new)
    return old, new


def apply_live(blob, values):
    """Patch `values` into an existing save. Returns `(blob, applied, skipped)`.

    This is the seam `responders._resource_blob` calls on the way out, in the
    same family as `_ptl_with_live_roster`: the STORED file stays the base and
    the live numbers are overlaid, so a save authored by hand keeps everything
    this function does not know about.

    `applied` is `{name: (old, new)}`; `skipped` is the names whose destination
    is still unmeasured. **Today every stat is skipped**, and that is the correct
    behaviour -- see the banner. It logs rather than guesses.
    """
    out = bytearray(blob)
    if len(out) < SIZE:
        out.extend(b"\x00" * (SIZE - len(out)))
    applied, skipped = {}, []
    for name, spec in sorted(FIELDS.items()):
        if name not in values:
            continue
        if spec["off"] is None:
            skipped.append(name)
            continue
        v = min(int(values[name]), spec["max"])
        moved = _pack_into(out, spec["off"], spec["width"], v, spec["signed"])
        if moved:
            applied[name] = moved
    return bytes(out), applied, skipped


def build(values=None, base=None, existing=True, wire_seat=None):
    """A full 976-byte save (980 if `base` carried the trailer slot).

    `base` is an existing save whose other bytes are KEPT -- the two strings at
    +0x2CB/+0x2EB and the identity u64s live there and are not ours to reset.
    Rebuilding without a base is a factory blob: the magic, the branch byte, and
    zeros. That is exactly what `tools/mk_jan_save.py` writes, and this stays
    byte-compatible with it so the two cannot drift.

    `values` is a `janstats.derive()` dict; only fields with a measured offset
    are written.
    """
    out = bytearray(base if base else b"\x00" * SIZE)
    if len(out) not in (SIZE, SIZE + TRAILER):
        raise ValueError("base save is %d bytes; expected %d, or %d with the "
                         "trailer slot" % (len(out), SIZE, SIZE + TRAILER))
    struct.pack_into("<I", out, MAGIC_OFF, MAGIC)
    out[EXISTING_PLAYER] = 1 if existing else 0
    if wire_seat is not None:
        out[WIRE_SEAT] = int(wire_seat) & 0xFF
    if values:
        patched, applied, skipped = apply_live(bytes(out[:SIZE]), values)
        out[:SIZE] = patched
        return bytes(out), applied, skipped
    return bytes(out), {}, sorted(FIELDS)


def build_for_member(member_id, base=None):
    """The save this member's RECORD says they should have.

    `existing` follows the record: a member who has never finished a game is
    served the blank branch, which is the truth rather than a flag we set once
    and forgot. The server owner can still pin it with `mk_jan_save.py`.
    """
    if janstats is None:
        return build(base=base)
    rec = janstats.load(member_id)
    vals = janstats.derive(rec)
    return build(vals, base=base, existing=bool(rec.get("games_played")))


def decode(blob):
    """Every MEASURED field, by offset. Values only -- no semantics claimed."""
    out = {"size": len(blob),
           "magic": struct.unpack_from("<I", blob, MAGIC_OFF)[0]
           if len(blob) >= 4 else None}
    out["magic_ok"] = out["magic"] == MAGIC
    fields = []
    for off, width, note in OFFSETS:
        if off >= len(blob):
            fields.append((off, width, note, None))
            continue
        if width == 0:
            end = blob.find(b"\x00", off)
            raw = blob[off:end if end >= 0 else len(blob)]
            fields.append((off, width, note, raw))
        elif width == 1:
            fields.append((off, width, note, blob[off]))
        elif off + width <= len(blob):
            fmt = {4: "<I", 8: "<Q"}[width]
            fields.append((off, width, note,
                           struct.unpack_from(fmt, blob, off)[0]))
        else:
            fields.append((off, width, note, None))
    out["fields"] = fields
    out["existing_player"] = blob[EXISTING_PLAYER] if len(blob) > EXISTING_PLAYER else None
    return out


def dump(blob):
    d = decode(blob)
    print("save %d B  (client reads %d%s)" % (
        d["size"], SIZE,
        "" if d["size"] in (SIZE, SIZE + TRAILER)
        else "  !! UNEXPECTED LENGTH"))
    print("  magic %#010x  %s" % (
        d["magic"] or 0,
        "ok" if d["magic_ok"] else
        "!! WRONG -- lmenu__002b2ab0 returns -650 and the save is refused"))
    for off, width, note, val in d["fields"]:
        if width == 0:
            shown = repr(val) if val is not None else "-"
        elif val is None:
            shown = "-"
        elif width == 1:
            shown = "%d (%#04x)" % (val, val)
        else:
            shown = "%d (%#x)" % (val, val)
        print("  +%#05x  %-4s %-22s %s"
              % (off, {0: "str", 1: "u8", 4: "u32", 8: "u64"}[width], shown, note))
    if d["existing_player"] == 0:
        print("  !! +0x3C8 IS ZERO -- the client skips the whole identity block "
              "and treats this as a brand new player, every boot.")
    got = measured_fields()
    if got:
        print("stat fields with a measured home: %s" % ", ".join(sorted(got)))
    else:
        print("stat fields with a measured home: NONE. Level / Rank / Games "
              "Played / Money are recorded in janstats.py and have nowhere to "
              "land yet -- see FIELDS.")


# --- CLI / selftest ---------------------------------------------------------

def selftest():
    ok = True

    def check(cond, msg):
        if not cond:
            print("FAIL: %s" % msg)
        return bool(cond)

    blob, applied, skipped = build()
    ok &= check(len(blob) == SIZE, "a factory save is %d bytes, got %d"
                % (SIZE, len(blob)))
    ok &= check(struct.unpack_from("<I", blob, 0)[0] == MAGIC,
                "the magic is written -- without it the client returns -650")
    ok &= check(blob[EXISTING_PLAYER] == 1, "+0x3C8 takes the identity branch")

    # THE MASTER FLAG. `lmenu__002b2ab0` sets DAT_00445E31 = (+0x3CA == +0x3C9)
    # and nothing else in the module writes that flag -- so these two bytes ARE
    # the master menu. Shipping both zero made every client the master, which is
    # what two players at one table saw live on 2026-09-04.
    _own = apply_seat(blob, seat=0, master_seat=0, table_id=3)
    _guest = apply_seat(blob, seat=1, master_seat=0, table_id=3)
    ok &= check(_own[WIRE_SEAT] == _own[WIRE_SEAT_LIMIT],
                "the table's owner: +0x3CA == +0x3C9, so it reads as MASTER")
    ok &= check(_guest[WIRE_SEAT] != _guest[WIRE_SEAT_LIMIT],
                "a guest: +0x3CA != +0x3C9, so it does NOT read as master")
    ok &= check(_guest[WIRE_SEAT] == 1,
                "and the guest's wire seat is its REAL seat -- +0x3CA is the "
                "`src` byte on every in-game message, not a flag to tune")
    ok &= check(struct.unpack_from("<Q", _own, MY_TABLE)[0] == 3,
                "+0x18 carries the table, so a save re-fetch cannot un-own it")
    ok &= check(apply_seat(blob) == blob,
                "apply_seat with nothing to say changes nothing")
    _blank = bytes(SIZE)
    _stamped = apply_seat(_blank, seat=1, master_seat=0, table_id=3)
    ok &= check(_stamped[EXISTING_PLAYER] == 1,
                "stamping a seat TAKES THE IDENTITY BRANCH (+0x3C8 = 1) -- a "
                "zero there makes the client discard the seat, the master flag "
                "AND the reserved flag")
    ok &= check(_stamped[WIRE_SEAT] == 1 and _stamped[WIRE_SEAT_LIMIT] == 0,
                "...and the seat bytes survive alongside it")
    ok &= check(sorted(skipped) == sorted(FIELDS),
                "a build with no values reports EVERY field skipped, not "
                "silently dropped: %r" % (skipped,))
    ok &= check(not applied, "and nothing was written: %r" % (applied,))

    # The one that still has no home must STAY reported rather than quietly
    # doing nothing -- that distinction is the whole point of `skipped`.
    ok &= check(sorted(k for k, v in FIELDS.items() if v["off"] is None)
                == ["games_played"],
                "exactly games_played remains unmeasured: %r"
                % sorted(k for k, v in FIELDS.items() if v["off"] is None))
    ok &= check(FIELDS["level"]["off"] == 0x238 and FIELDS["rank"]["off"] == 0x23C
                and FIELDS["level"]["width"] == 4 and FIELDS["rank"]["max"] == 94,
                "level +0x238 / rank +0x23C, u32, rank capped at the 95th tier")
    if janstats is not None:
        _yk = [k for k in FIELDS if k.startswith("yaku_")]
        ok &= check(len(_yk) == len(janstats.YAKU_OFFSETS) == 39,
                    "39 yaku counters declared from janstats.YAKU_OFFSETS")
        ok &= check(FIELDS["yaku_riichi"]["off"] == 0x120
                    and FIELDS["yaku_chuuren"]["off"] == 0x1C8
                    and FIELDS["yaku_double_riichi"]["off"] == 0x11C,
                    "riichi +0x120, double riichi +0x11C, chuuren +0x1C8")
        _rec = janstats.blank(1)
        _rec["yaku"] = {"riichi": 7, "kokushi": 1}
        _rec["games_played"] = 25
        _patched, _applied, _skipped = apply_live(bytes(SIZE), janstats.derive(_rec))
        ok &= check(struct.unpack_from("<I", _patched, 0x120)[0] == 7
                    and struct.unpack_from("<I", _patched, 0x1A4)[0] == 1
                    and struct.unpack_from("<I", _patched, 0x238)[0]
                    == janstats.level_of(_rec)
                    and struct.unpack_from("<I", _patched, 0x23C)[0]
                    == janstats.rank_index_of(_rec),
                    "a derived record lands its yaku, level and rank")
        ok &= check(_skipped == ["games_played"],
                    "only games_played is skipped now: %r" % (_skipped,))
    _rj = apply_seat(blob, seat=1, master_seat=0, table_id=3, rejoin=True)
    ok &= check(_rj[REJOIN] == 1 and _rj[WIRE_SEAT] == 1,
                "rejoin=True sets +0x3CB (the 'Rejoin' menu gate)")
    ok &= check(apply_seat(_rj, rejoin=False)[REJOIN] == 0,
                "rejoin=False clears it")
    ok &= check(apply_seat(blob, seat=1, master_seat=0, table_id=3)[REJOIN] == 0,
                "rejoin unspecified leaves it alone")
    ok &= check(FIELDS["money"]["off"] == 0x028
                and FIELDS["money"]["width"] == 8
                and FIELDS["money"]["signed"],
                "money is the s64 at +0x028 -- `ld`, and `%20ld` on screen")

    # A 64-bit money value must survive the round trip: this is the field that
    # would silently truncate if the width were read as 4.
    big = 0x0000_00FF_1234_5678
    patched, applied, skipped = apply_live(blob, {"money": big})
    ok &= check(struct.unpack_from("<q", patched, 0x028)[0] == big,
                "a 64-bit balance round-trips: %#x"
                % struct.unpack_from("<q", patched, 0x028)[0])
    ok &= check(skipped in (["games_played"], []),
                "unmeasured fields not silently written: %r" % (skipped,))

    # Byte-for-byte identical to what tools/mk_jan_save.py writes, so the two
    # authors cannot drift apart.
    ref = bytearray(SIZE)
    struct.pack_into("<I", ref, 0x000, MAGIC)
    ref[EXISTING_PLAYER] = 1
    ok &= check(blob == bytes(ref),
                "a factory save matches mk_jan_save.py byte for byte")
    blank_blob, _, _ = build(existing=False)
    ok &= check(blank_blob[EXISTING_PLAYER] == 0, "--blank takes the zero branch")

    # The trailer slot survives.
    based, _, _ = build(base=b"\x00" * (SIZE + TRAILER))
    ok &= check(len(based) == SIZE + TRAILER,
                "a base with the 4-byte trailer slot keeps it: %d" % len(based))
    try:
        build(base=b"\x00" * 100)
        ok &= check(False, "a short base must raise")
    except ValueError:
        pass

    # A base's other bytes are kept.
    base = bytearray(SIZE)
    base[0x2CB:0x2CB + 5] = b"Lex\x00\x00"
    kept, _, _ = build(base=bytes(base))
    ok &= check(kept[0x2CB:0x2CB + 3] == b"Lex",
                "the identity strings survive a rebuild")

    # A measured field is written; an unmeasured one is REPORTED, not written.
    patched, applied, skipped = apply_live(blob, {"top_finishes": 7,
                                                  "games_played": 12})
    ok &= check(applied == {"top_finishes": (0, 7)},
                "a measured field is written: %r" % (applied,))
    ok &= check(skipped == ["games_played"],
                "and an unmeasured one is reported, not written: %r" % (skipped,))
    ok &= check(struct.unpack_from("<I", patched, 0x0D0)[0] == 7,
                "the value really landed at its offset")
    ok &= check(struct.unpack_from("<I", patched, 0)[0] == MAGIC,
                "patching never disturbs the magic")
    clamped, _, _ = apply_live(blob, {"top_finishes": 1 << 40})
    ok &= check(struct.unpack_from("<I", clamped, 0x0D0)[0] == 0xFFFFFFFF,
                "and is clamped to the field's max, not truncated")

    # No two measured fields may overlap -- the money four are 8 bytes each and
    # sit 8 and 16 apart, so this is a real hazard, not a formality.
    spans = sorted((v["off"], v["off"] + v["width"], k)
                   for k, v in FIELDS.items() if v["off"] is not None)
    for (a0, a1, an), (b0, b1, bn) in zip(spans, spans[1:]):
        ok &= check(a1 <= b0, "%s (+%#05x..%#05x) does not overlap %s (+%#05x)"
                    % (an, a0, a1, bn, b0))
    ok &= check(spans[-1][1] <= SIZE, "the last field fits inside the save")

    try:
        _pack_into(bytearray(SIZE), SIZE - 2, 4, 1)
        ok &= check(False, "a field running off the end must raise")
    except ValueError:
        pass

    d = decode(blob)
    ok &= check(d["magic_ok"] and d["existing_player"] == 1,
                "decode reads the two fields we author")
    ok &= check(len(d["fields"]) == len(OFFSETS),
                "decode reports every measured offset")

    if janstats is not None:
        # member 4242's record is a row of this suite's own database
        # (jan_run_all.py gives it one)
        b0, _, _ = build_for_member(4242)
        ok &= check(b0[EXISTING_PLAYER] == 0,
                    "a member with no record is served the BLANK branch")
        janstats.record_game(4242, 0, 40000, 50.0)
        b1, _, _ = build_for_member(4242)
        ok &= check(b1[EXISTING_PLAYER] == 1,
                    "and the identity branch once they have played")

    print("jansave selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def selftest_2004():
    base = build(existing=True)[0]
    b4 = to_2004(base)
    assert len(b4) == len(base) + INSERT_LEN_2004
    assert struct.unpack_from("<I", b4, 0)[0] == MAGIC_2004
    moved = ((0x2b6, 0x2fe), (0x2b8, 0x300), (0x2ba, 0x302), (0x2cb, 0x313),
             (0x2eb, 0x333), (0x3c5, 0x40d), (0x3c8, 0x410), (0x3c9, 0x411),
             (0x3ca, 0x412), (0x3cb, 0x413))
    probe = bytearray(base)
    for i, (old, _new) in enumerate(moved):
        probe[old] = 0x80 + i
    p4 = to_2004(bytes(probe))
    for i, (_old, new) in enumerate(moved):
        assert p4[new] == 0x80 + i, hex(new)
    assert p4[4:INSERT_AT_2004] == bytes(probe)[4:INSERT_AT_2004]
    assert is_2004_version("20040727_2") and not is_2004_version("20020314_0")
    assert not is_2004_version("20030909_0") and not is_2004_version("20260909_0")
    print("  ok   2004 layout: magic, length and the ten moved fields")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dump", metavar="SAVE")
    ap.add_argument("--member", metavar="MEMBER",
                    help="author from this member's janstats record")
    ap.add_argument("--base", metavar="SAVE", help="keep this save's other bytes")
    ap.add_argument("--blank", action="store_true",
                    help="write the zero-branch blob (the control)")
    ap.add_argument("--out", metavar="SAVE")
    ap.add_argument("--fields", action="store_true",
                    help="print the declared stat table and stop")
    a = ap.parse_args()

    if a.selftest or not (a.fields or a.dump or a.member or a.out or a.base):
        selftest_2004()
        return selftest()
    if a.fields:
        print(json.dumps(FIELDS, indent=2, sort_keys=True))
        print("measured: %s" % (sorted(measured_fields()) or "NONE"))
        return 0
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
        return 0

    base = None
    if a.base:
        with open(a.base, "rb") as f:
            base = f.read()
    if a.member:
        blob, applied, skipped = build_for_member(a.member, base=base)
    else:
        blob, applied, skipped = build(base=base, existing=not a.blank)
    if not a.out:
        print("nothing written -- pass --out")
        return 2
    with open(a.out, "wb") as f:
        f.write(blob)
    print("wrote %d bytes -> %s" % (len(blob), a.out))
    print("  +0x000 magic           = %#010x" % MAGIC)
    print("  +0x3C8 existing player = %d" % blob[EXISTING_PLAYER])
    if applied:
        for k, (old, new) in sorted(applied.items()):
            print("  %-14s %d -> %d" % (k, old, new))
    if skipped:
        print("  NOT written (no measured offset): %s" % ", ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
