#!/usr/bin/env python3
"""Janhourou 2004 build (20040727_2): 2002-layout in-game records -> 2004 layout.

Every function takes a FULL record as janmsgs builds it (0x18-byte header, body
at +0x18; offsets below are record offsets, like janmsgs's) and returns the
2004 record with the u16 length at +0x10 rewritten.

Addresses are in the plaintext 2004 module at base 0x00280000 unless marked
"2002". Dispatcher: 2004 0x00289c48 (2002 0x00287f74), same opcodes.

  opcode            2002 handler   2004 handler   layout
  MjHAIPAI    34    0x002851e0     0x00286210     DIFFERENT (0xd7 -> 0xd3)
  MjTSUMO     36    0x002857e0     0x00286b40     DIFFERENT (0x68 -> 0xb5)
  MjNAKI      38    0x002860b0     0x002877b0     identical
  MjALLDATA   46    0x00286af0     0x00287fe0     +1 byte (0x140 -> 0x141)
  MjYAKUDISP  40    0x002bcba0     0x0029f950     identical
  MjSEISAN    42    0x002bd2d0     0x002a0380     identical
  HALF1       56    0x00334be0     0x00302d90     identical
  HALF2       58    0x00335050     0x00303320     identical

PER-SEAT BYTES +0x23..+0x26 (HAIPAI, TSUMO, NAKI, ALLDATA): POLARITY FLIPPED.
  2002 0x00285148: lb v1,11(v0) ; subu v1,a0(=1),v1 ; sb -> 0x409ad8[i]
  2004 0x002860a4: lbu v0,11(v0); beq v0,0 -> sb 0 else sb 1 -> 0x47ec08[i]
  and the consumer is the same code in both (ready gauge auto-fill, 2002
  0x003271a4 / 2004 0x002f15a4: non-zero -> ready = 1, i.e. "a COM"). So 2002
  wire 1 = human, 2004 wire non-zero = COM. 2004 also uses the byte as the
  MjALLDATA subtype-3 gate (0x002887a8..b4: a2 = rec[0x23 + rec[0x22]];
  beq a2,0 -> no animation) where 2002 tested +0x1e (0x00287140).
"""
import struct

HAIPAI_LEN_2004 = 0xD3
TSUMO_LEN_2004 = 0xB5
ALLDATA_LEN_2004 = 0x141
TSUMO_NAME_OFF = 0x75
TSUMO_NAME_LEN = 16


def _relen(rec):
    rec = bytearray(rec)
    struct.pack_into("<H", rec, 0x10, len(rec))
    return bytes(rec)


def flip_seat_bytes(rec):
    """+0x23..+0x26: 2002 IsHuman (1 = human) -> 2004 IsCom (non-zero = COM).
    2004 store: 0x002860a0..b8."""
    rec = bytearray(rec)
    for o in range(0x23, 0x27):
        rec[o] = 0 if rec[o] else 1
    return bytes(rec)


def haipai_to_2004(rec, flip=True):
    """2004 body = 2002 body with 2 bytes DELETED at +0x3a and 2 at +0x42.

    2004 handler 0x00286210:
      +0x28 u32 -> 0x4ce3c0 (0x0028632c)                       same
      +0x2c..+0x39 14 x s8 -> 0x4ce3c4..d1 (0x00286340..e8)    2002 had 16
      +0x3a,+0x3b  u8 -> 0x4ce3d2/d3 (0x002863ec..fc)  UMA     2002 +0x3c,+0x3d
      +0x3c 3 B  -> 0x4ce3d4 (a1 = s0+20, 0x00286338/f8) red-five counts
                                                               2002 +0x3e
      +0x3f u8   -> 0x4ce3d7 (lb 23(s0), 0x00286400)           2002 +0x41 (3 B)
      +0x40 14 x u16 -> 0x4cdb40 dead wall (lhu 64, 0x002865cc) 2002 +0x44
      +0x5c 4 x 14 x u16 stride 0x1c -> 0x4cdb60 (lhu 92, 0x002865ec)
                                                               2002 +0x60
      +0xcc chiicha -> 0x4ce200 (0x00286468)                   2002 +0xd0
      +0xcd dealer  -> 0x4ce1f8 (0x00286428)                   2002 +0xd1
      +0xce wind    -> 0x4ce1e8   +0xcf kyoku -> 0x4ce1f0
      +0xd0 honba   -> 0x4ce268   +0xd1/+0xd2 dice -> 0x4ce280/81
    Rule indices 0..11 have the same readers in both builds (scan of
    0x452184.. against 0x4ce3c4..), so the two dropped bytes are taken to be
    2002 [14],[15] (never read in 2002; the server sends zeros in [8..15]
    anyway). 2004 [13] (0x4ce3d1) IS read, by the discard picker at 0x002a792c.
    """
    if len(rec) < 0xD7:
        raise ValueError("2002 MjHAIPAI is 0xd7 bytes, got %#x" % len(rec))
    out = rec[:0x3A] + rec[0x3C:0x42] + rec[0x44:0xD7]
    assert len(out) == HAIPAI_LEN_2004
    if flip:
        out = flip_seat_bytes(out)
    return _relen(out)


def tsumo_to_2004(rec, scores=(0, 0, 0, 0), names=(b"", b"", b"", b""),
                  flip=True):
    """2004 body = 2002 body with 16 bytes (4 x s32 SCORES) INSERTED at +0x28,
    and 4 x 16-byte seat NAMES appended at +0x75.

    2004 handler 0x00286b40 and its picker 0x002a7600 (2002 0x002c2ac0):
      +0x28 4 x s32 -> 0x4ceed0 / s64 0x4ce2e0 (0x00286c64..80)  NEW; the same
             globals MjALLDATA +0x28 fills (0x00288444), so these are SCORES
      +0x38 s16 drawing seat (lh 56: 0x00286b80, 0x00286db8, 0x00287044)
                                                                2002 +0x28
      +0x3a 14 x u16 hand (lhu 58, 0x00287004)                  2002 +0x2a
      +0x56..+0x59 per seat (lbu 86..89, 0x00287154..7c)        2002 +0x46
      +0x5a wall count -> 0x4ce1d8 (lbu 90, 0x00286bbc)         2002 +0x4a
      +0x5b (lbu 91)  +0x5c (lbu 92)                            2002 +0x4b/4c
      +0x5d 7 B menu (lbu 93, 0x0028709c and 0x002a7650)        2002 +0x4d
      +0x64 14 B slot flags -> 0x4c4f00 (lbu 100, 0x002a767c)   2002 +0x54
      +0x72,+0x73,+0x74 (lbu 114/115/116, 0x002a7634..40)       2002 +0x62..64
      +0x75 + 16*seat: name, copied to 0x4fc860 + 16*seat by 0x003083e0
             (0x00286c3c..4c: a1 = rec + s3 + 117, s3 += 16), then shown on
             the plates rotated by my seat (0x00286cd0..e8).     NEW
    +0x18..+0x27 (motion, tile +0x1b, slot +0x1d, riichi +0x21, seat +0x22,
    the four seat bytes) keep their offsets: the client's own motion-3 record
    is written at the same offsets in both builds (2004 0x002876d8..0x00287764,
    2002 0x00286000..0x0028608c).
    `names`: cp932 bytes, at most 15 + NUL (the copy goes through the module's
    import at [0x102514]; a C-string copy is assumed).
    """
    if len(rec) < 0x65:
        raise ValueError("2002 MjTSUMO runs to +0x64, got %#x" % len(rec))
    out = bytearray(rec[:0x28])
    out += struct.pack("<4i", *[int(s) for s in (list(scores) + [0] * 4)[:4]])
    out += rec[0x28:0x65]
    assert len(out) == TSUMO_NAME_OFF
    for n in (list(names) + [b""] * 4)[:4]:
        n = bytes(n)[:TSUMO_NAME_LEN - 1]
        out += n.ljust(TSUMO_NAME_LEN, b"\0")
    assert len(out) == TSUMO_LEN_2004
    out = bytes(out)
    if flip:
        out = flip_seat_bytes(out)
    return _relen(out)


def alldata_to_2004(rec, yakitori_mask=0, flip=True):
    """2004 body = 2002 body + ONE byte at +0x140.

    2004 handler 0x00287fe0: every 2002 offset is read at the same place
    (+0x28 scores, +0x44, +0x52, +0x8a, +0xe6, +0x126, +0x136, +0x13a..+0x13f).
      +0x140 bit s -> 0x4ce298[s] (0x0028847c..d0). 0x4ce298[] is the per-seat
             YAKITORI marker: armed from rule [7] at 0x00289afc..14, cleared
             for the winner at 0x0029fba4, drawn by 0x002b35c4..f8.  NEW
      subtype 3: the animation gate is rec[0x23 + rec[0x22]] != 0
             (0x002887a8..b4), not +0x1e (2002 0x00287140). With the flipped
             seat bytes a COM event seat passes. A HUMAN event seat does not:
             2004 animates a human's draw from the MjTSUMO it receives for that
             seat (0x00286dac..d0; 0x00286ec4..dc sets 0x4bb360 = seat + 1 and
             MjALLDATA skips the seat it names, 0x00288938..58). INFERRED: the
             2004 server sent MjTSUMO to the whole table.
      +0x21 is read only in the spectator branch (0x002889b4).
    """
    if len(rec) < 0x140:
        raise ValueError("2002 MjALLDATA is 0x140 bytes, got %#x" % len(rec))
    out = rec[:0x140] + bytes([yakitori_mask & 0x0F])
    if flip:
        out = flip_seat_bytes(out)
    return _relen(out)


def naki_to_2004(rec, flip=True):
    """Same offsets (+0x28, +0x2a, +0x2b.., +0x2f..+0x34, +0x38..+0x3c, +0xa0
    in both 0x002860b0 and 0x002877b0); only the seat bytes flip."""
    return _relen(flip_seat_bytes(rec) if flip else rec)


NOTICE_GAMESETUP_LEN_2004 = 0x60


def notice_gamesetup_to_2004(rec, names=(b"", b"", b"", b"")):
    """Opcode 20. THE 2004 BUILD READS FOUR 16-BYTE SEAT NAMES HERE; 2002
    reads nothing, and the record is 0x20.

    2002 0x002c665c is one instruction, `addiu s0, zero, 32`, falling through
    to the bit ladder. 2004 0x00392f98 is four calls before that same line:

        jal 0x003083e0 ; a0 = 0, a1 = s1+32   -> rec+0x20, 16 bytes
        jal 0x003083e0 ; a0 = 1, a1 = s1+48   -> rec+0x30
        jal 0x003083e0 ; a0 = 2, a1 = s1+64   -> rec+0x40
        jal 0x003083e0 ; a0 = 3, a1 = s1+80   -> rec+0x50

    and 0x003083e0 is `strcpy(0x004fc860 + (seat << 4), src)`, the IN-GAME
    NAMEPLATE table -- the same 0x004fc860 MjTSUMO's names land in (see
    TSUMO_NAME_OFF). So the 2004 record is 0x60.

    MEASURED IN RAM: sent the bare 0x20 notice, all four copies read past the
    record and 0x004fc860..0x004fc89f is zero, i.e. four blank nameplates.
    """
    rec = bytearray(rec)
    if len(rec) < NOTICE_GAMESETUP_LEN_2004:
        rec += bytearray(NOTICE_GAMESETUP_LEN_2004 - len(rec))
    for s in range(4):
        nm = bytes(names[s]) if s < len(names) else b""
        o = 0x20 + s * TSUMO_NAME_LEN
        rec[o:o + TSUMO_NAME_LEN] = nm[:TSUMO_NAME_LEN - 1].ljust(
            TSUMO_NAME_LEN, b"\0")
    return _relen(rec)


CONVERTERS = {20: notice_gamesetup_to_2004, 34: haipai_to_2004,
              36: tsumo_to_2004, 38: naki_to_2004, 46: alldata_to_2004}


def to_2004(rec, **kw):
    f = CONVERTERS.get(rec[0x12])
    return f(rec, **kw) if f else bytes(rec)


# --- self-test ----------------------------------------------------------------

def selftest():
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import janmsgs as J
    ok = True

    def check(c, what):
        nonlocal ok
        print(("ok   " if c else "FAIL ") + what)
        ok &= bool(c)

    hands = [[1, 3, 6, 8, 9, 14, 15, 15, 17, 17, 18, 26, 27, 29],
             list(range(13)), list(range(9, 22)), list(range(14, 27))]
    dead = list(range(14))
    h2 = J.haipai(1, hands, position=J.wanpai_u16s(dead), motion=10,
                  seat_mask=0x0F, concealed=(1, 2, 3), u28=0x11223344,
                  scalars18=J.rule_scalars(dora_kind=3, aka_rank=5, uma=(20, 10)),
                  three_a=b"\1\1\1", chiicha=2, dealer=3, round_wind=1, kyoku=2,
                  honba=4, dice=(5, 6), is_human=(1, 0, 0, 0))
    h4 = haipai_to_2004(h2)
    check(len(h2) == 0xD7 and len(h4) == 0xD3, "HAIPAI 0xd7 -> 0xd3")
    check(struct.unpack_from("<H", h4, 0x10)[0] == 0xD3, "HAIPAI length field")
    check(h4[:0x23] == h2[:0x10] + b"\xd3\x00" + h2[0x12:0x23]
          and h4[0x18] == 10 and h4[0x22] == 0x0F,
          "HAIPAI motion +0x18 / seat mask +0x22 unmoved")
    check(h4[0x23:0x27] == bytes([0, 1, 1, 1]), "HAIPAI seat bytes flipped")
    check(h4[0x28:0x2C] == h2[0x28:0x2C], "HAIPAI +0x28 u32")
    check(h4[0x2D] == 3 and h4[0x2E] == 4, "HAIPAI dora rule +0x2d, aka +0x2e")
    check((h4[0x3A], h4[0x3B]) == (20, 10), "HAIPAI uma at +0x3a/+0x3b")
    check(h4[0x3C:0x3F] == b"\1\1\1", "HAIPAI red-five counts at +0x3c")
    check(h4[0x40:0x5C] == h2[0x44:0x60], "HAIPAI dead wall at +0x40")
    check(h4[0x5C:0xCC] == h2[0x60:0xD0], "HAIPAI hands at +0x5c")
    check(struct.unpack_from("<H", h4, 0x5C)[0] ==
          struct.unpack_from("<H", h2, 0x60)[0] != 0, "HAIPAI seat 0 slot 0")
    check(tuple(h4[0xCC:0xD3]) == (2, 3, 1, 2, 4, 5, 6),
          "HAIPAI chiicha/dealer/wind/kyoku/honba/dice at +0xcc..+0xd2")

    t2 = J.tsumo(2, 0, hands[0], 69, motion=2, anim_tile=0x16, anim_slot=13,
                 anim_seat=0, riichi_test=bytes([1, 0, 1, 0, 0, 0, 0]),
                 per_seat=(1, 2, 3, 0), f4b=7, f4c=8, f62=9, f63=1, f64=1,
                 is_human=(1, 0, 0, 0))
    t4 = tsumo_to_2004(t2, scores=(25000, 24000, 26000, -100),
                       names=(b"Lex", b"COM 1", b"COM 2", b"COM 3"))
    check(len(t2) == 0x68 and len(t4) == 0xB5, "TSUMO 0x68 -> 0xb5")
    check(struct.unpack_from("<H", t4, 0x10)[0] == 0xB5, "TSUMO length field")
    check(t4[0x18] == 2 and t4[0x1B] == 0x16 and t4[0x1D] == 13 and
          t4[0x22] == 0, "TSUMO motion/tile/slot/seat unmoved")
    check(struct.unpack_from("<4i", t4, 0x28) == (25000, 24000, 26000, -100),
          "TSUMO scores at +0x28")
    check(struct.unpack_from("<h", t4, 0x38)[0] == 0, "TSUMO drawing seat +0x38")
    check(t4[0x3A:0x56] == t2[0x2A:0x46], "TSUMO hand at +0x3a")
    check(tuple(t4[0x56:0x5A]) == (1, 2, 3, 0), "TSUMO per-seat at +0x56")
    check(t4[0x5A] == 69, "TSUMO wall count at +0x5a")
    check(t2[0x5A] == 2, "2002 layout puts slot flag 2 at +0x5a = the '2 left'")
    check((t4[0x5B], t4[0x5C]) == (7, 8), "TSUMO +0x5b/+0x5c")
    check(t4[0x5D:0x64] == bytes([1, 0, 1, 0, 0, 0, 0]), "TSUMO menu at +0x5d")
    check(t4[0x64:0x72] == bytes([2] * 14), "TSUMO slot flags at +0x64")
    check(tuple(t4[0x72:0x75]) == (9, 1, 1), "TSUMO +0x72..+0x74")
    check(t4[0x75:0x85] == b"Lex".ljust(16, b"\0") and
          t4[0xA5:0xB5] == b"COM 3".ljust(16, b"\0"), "TSUMO names at +0x75")
    check(struct.unpack_from("<h", t2, 0x38)[0] != 0,
          "2002 layout: +0x38 is a hand tile, so 2004 reads 'not my turn'")

    a2 = J.alldata(3, hands, [[], [], [], []], scores=(1, 2, 3, 4), subtype=3,
                   wall_count=57, event_tile=0x16, event_seat=1,
                   event_present=1, is_human=(1, 0, 0, 0))
    a4 = alldata_to_2004(a2, yakitori_mask=0b1010)
    check(len(a2) == 0x140 and len(a4) == 0x141, "ALLDATA 0x140 -> 0x141")
    check(struct.unpack_from("<H", a4, 0x10)[0] == 0x141, "ALLDATA length field")
    check(a4[0x140] == 0b1010, "ALLDATA yakitori mask at +0x140")
    check(a4[0x27:0x140] == a2[0x27:0x140], "ALLDATA interior unmoved")
    check(a4[0x23 + a4[0x22]] != 0,
          "ALLDATA subtype-3 gate rec[0x23+seat] set for a COM event seat")
    check(to_2004(a2)[0x12] == 46 and len(to_2004(h2)) == 0xD3, "dispatch")
    print("ALL OK" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
