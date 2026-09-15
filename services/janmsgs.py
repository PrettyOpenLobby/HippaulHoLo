#!/usr/bin/env python3
"""Janhourou's IN-GAME messages: tile codec, builders and parsers.

`janwire.py` owns the 32-byte header and the six-bit line encoding; this file
owns everything above it that a hand of mahjong needs -- the tile representation
on the wire and the body layout of every message in the play sequence:

    MjREADY 0x21 -> MjHAIPAI 0x22/ACK -> MjTSUMO 0x24 -> MjSUTE 0x25
                 -> MjNAKI 0x26/ACK   -> MjYAKUDISP 0x28/ACK
                 -> MjSEISAN 0x2a/ACK -> MjGAMEEND 0x2c (acked as MjBYE 0x2d)
    MjALLDATA 0x2e/ACK is the resync; MjGAMERESULTHALF1/2 0x38/0x3a end a hanchan.

PROVENANCE. Every offset here was read out of the client's own receive handlers
in `work/ps2/out/jan-c-full/console.c` (and `mahdisp.c` for the two reply
encoders) -- SQUARE shipped JanHouRou.pex with its debug logging intact, so each
handler destructures its message field by field and IS the layout spec. Where a
field's MEANING is not established the builder takes it as a named argument and
defaults to zero rather than inventing a value; that is deliberate and matches
the rest of this workstream.

=== THE ONE RULE THAT MAKES ANY OF IT ARRIVE =================================

The in-game loop (`console__00287af0`, spawned as the "MjClient" task) calls

    objstrings__002c9d80(1, buf)

so it drains SUB-CODE 1, and by the client's routing rule (the sub-code at
rec[0x17] picks the FIFO) every in-game message we send must carry
**rec[0x17] = 1**. This is the same trap that cost two live
launches on MjGETLNDV (which drains on 0) and on the four table ACKs (which
drain on 5): a perfectly-formed record with the wrong sub is bucketed into
another FIFO and the waiting code never sees it. There is no queue arithmetic --
the sub we send must equal the sub the waiter passes.

=== THE SEQUENCE BYTE =======================================================

`rec[0x13]` is a sequence, and the client dedups on it:

    cVar1 = buf[0x13];
    if (DAT_00409b10 == cVar1) {            /* same as last time = a duplicate */
        if (buf[0x12] != '.')               /* MjALLDATA is exempt */
            objstrings__002c9d00(0x45ac90); /* re-send our last message */
        continue;                           /* and do NOT handle it */
    }
    DAT_00409b10 = cVar1;                   /* otherwise adopt it */

So the world protocol is at-least-once with client-side dedup, the client stamps
that same adopted value on everything it sends back (its acks, MjSUTE,
MjNAKIACK), and a server can match a reply to its message with no other state.
**A new message MUST carry a different byte from the previous one** or it will be
swallowed as a retransmission -- `Table.next_seq()` in jangame.py owns that.

    python janmsgs.py --selftest
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janwire                                                      # noqa: E402
import janmahjong as mj                                             # noqa: E402

# --- opcodes we build or parse ----------------------------------------------

MjREADY = 0x21
MjHAIPAI, MjHAIPAIACK = 0x22, 0x23
MjTSUMO, MjSUTE = 0x24, 0x25
MjNAKI, MjNAKIACK = 0x26, 0x27
MjYAKUDISP, MjYAKUDISPACK = 0x28, 0x29
MjSEISAN, MjSEISANACK = 0x2A, 0x2B
MjGAMEEND, MjBYE = 0x2C, 0x2D
MjALLDATA, MjALLDATAACK = 0x2E, 0x2F
MjSASHIUMASTART, MjSASHIUMAREQUEST = 0x31, 0x32
MjSASHIUMASELECT, MjSASHIUMAARGEE = 0x33, 0x34
MjSASHIUMARESULT = 0x35
MjGAMERESULT = 0x36
MjGAMERESULTHALF1, MjGAMERESULTHALF1ACK = 0x38, 0x39
MjGAMERESULTHALF2, MjGAMERESULTHALF2ACK = 0x3A, 0x3B
MjMEMBERLEAVE = 0x3C
MjHAIPAIDEBUG, MjHAIPAIDEBUGACK = 0x3E, 0x3F
#: The spectator's one ack (console.c:2566-2575): len 0x18, +0x13 = the seq
#: of the last record it applied, +0x14 = its GALLERY SLOT (the RESERVEACK
#: +0x19 byte, not a seat), +0x15 4, +0x16 1 (0 for the READY/BYE
#: equivalents), +0x17 6. Server->spectator on sub 6 it is the skip flag
#: (mahdisp.c:3281-3293). See jangame's GALLERY banner.
MjGALLEYACK = 0x30
#: MjREADYSTATUS 66 -- server->client, the BETWEEN-HANDS READY GAUGE
#: (yamaguchi2__003270f0, entered by mahdisp__002bd2d0 right after the client
#: sends MjSEISANACK). The client drains SUB 2 for opcode 'B' and copies the
#: four bytes at +0x18 into DAT_004097e0[seat]: non-zero = that seat is
#: ready; a ready seat's gauge fills in 7 steps (one per 5 iterations) and
#: the screen closes when all four are full (timeout 0x708 iterations, same
#: exit). A seat whose +0x23 IsHuman byte (`_put_is_human`) is 0 is a COM to
#: the client and fills by itself, which is why our all-zero IsHuman never
#: needed this record. Measured 2026-09-09, not screen-proved.
MjREADYSTATUS = 66
READY_SUB = 2
MjNOTICEGAMESTART = 18
MjNOTICEGAMESETUP = 20      # <- the one that actually starts the game
MjNOTICETIMEUPWARNING = 19

# MEASURED: `objstrings__002c9d80(1, buf)` at console.c's in-game loop.
INGAME_SUB = 1

# The header bytes the client's own in-game senders write, mirrored so our
# messages look like the traffic it already produces. The loop reads NONE of
# these -- it gates on the opcode and the sequence only -- so they are
# convention, not requirement, and they are here so a capture reads uniformly.
SRC_SERVER = janwire.DST_SERVER & 0xFF          # -3
DST_REPLY = janwire.DST_REPLY & 0xFF            # -2
F16_CONST = 1                                   # const 1 in every client builder

# --- the tile codec ----------------------------------------------------------
#
# A tile on the wire is ONE BYTE, and the client unpacks it to its internal u16
# in THREE different ways depending on where it sits. All three are lifted
# verbatim out of `MjClient_MjALLDATA`:
#
#   hand   u16 = (b & 0x80) << 8 |  b & 0x3f            console.c:+95
#   pond   u16 = (b & 0x80) << 8 | (b & 0x40) << 1 | b & 0x3f       +105/+149
#   meld   u16 = (b & 0x80) << 8 |  b & 0x7f                        +113
#
# Read them together and the byte is:
#
#   bits 0..5   the tile id
#   bit 6       part of the ID in a meld (7-bit there), a POND flag elsewhere
#               (it lands at u16 bit 7), and DISCARDED ENTIRELY in a hand
#   bit 7       a flag, kept as u16 bit 15 in all three
#
# ~~WARNING: THE CONSEQUENCE THAT DECIDES THE RED-FIVE QUESTION~~ RETRACTED 09-02:
# the bit-6-drop argument only rules out bit 6 as the red flag. Bit SEVEN
# survives all three forms and IS the red-five signal (the u16 0x8000 red-sheet
# bit) -- see the re-measurement banner below. AKA_IDS is gone with it.
#
# ~~OK: CONFIRMED LIVE 2026-08-17~~ DOWNGRADED 09-02: the probe drew man 1..9 in
# order, but man 1-9 encodes IDENTICALLY in the linear and the suit-nibble
# numbering, so the run could not discriminate them -- and the probe's pin
# tiles (linear 10-13) were invisible for an unrelated reason at the time (the
# missing deal animation), so nobody noticed they never drew. The rank ORDER
# within a suit is still what the probe measured; the SUIT BASES were not.
#
# WARNING: The old "the client never does arithmetic on ids" observation stands, and
# it is exactly WHY the map matters: every draw path indexes the sprite table
# at 0x385430 with the raw 6-bit id, and that table -- not the id itself -- is
# the atlas order. Its key layout is the measurement the numbering below now
# rests on, corroborated live 2026-09-02 (no honor ever drew, bamboo 3+ never
# drew, the table was drowning in circles).
TILE_FLAG = 0x80                # byte bit 7 -> u16 bit 15
POND_FLAG = 0x40                # byte bit 6 -> u16 bit 7 (pond only)

# WARNING: ZERO IS NOT A TILE -- and this is MEASURED, not a convention we chose.
# The client uses 0 as "empty slot" and as a terminator, in three places:
#
#   * the discard path vacates a hand slot with
#         *(u16 *)(&DAT_00451920 + idx*2 + seat*0x1c) = 0;
#   * it finds the end of a pond by SCANNING FOR ZERO
#         for (p = &DAT_00451d50 + seat*0x32; *p != 0; p++) i++;
#   * MjHAIPAI's 14-wide hand arrays are zero-padded for the three seats that
#     hold 13 tiles.
#
# So a tile whose id encodes to 0x0000 would vanish out of a hand and truncate a
# pond. The ids are therefore **1-based**: id 1..34 for the kinds, 35..37 for
# the red fives, all comfortably inside the 6-bit field.
#
# The ORDER within 1..34 is now confirmed live too (see the banner above); this
# note records how the 1-BASED part was found, which was earlier and separate:
# the first selftest run put a 1m in a hand and the tile disappeared, which is
# exactly the bug a live client would have shown as a hand with a hole in it.
# WARNING: RE-MEASURED 2026-09-02 - the LINEAR numbering above is RETRACTED. The
# client's sprite lookup, used by EVERY tile-draw path (gs__002a6430 itself at
# gs.c:2217, the deal/draw/discard animations, the score screen), is
#
#     sprite = *(u16 *)(0x385430 + (id & 0x3f) * 2)     // 0 = draw NOTHING
#
# and the table's shape (dumped from the pex) is suit-NIBBLE keyed:
#
#     ids 0x01..0x09 -> sprites  1..9    man 1-9
#     ids 0x11..0x19 -> sprites 10..18   pin 1-9
#     ids 0x21..0x29 -> sprites 19..27   sou 1-9
#     ids 0x31..0x37 -> sprites 28..34   E S W N haku hatsu chun
#     id  0x3F       -> sprite  35       the tile BACK
#     every other id -> 0                INVISIBLE
#
# i.e. id = (suit << 4) | rank. With the old linear ids a live
# table showed exactly the predicted holes: 1-7 of circles, 8-9 of bamboo and
# every wind + the white dragon never drew (map 0), pins 8-9 drew as circles
# 1-2, sou 1-7 drew as circles 3-9, and the two remaining dragons drew as
# bamboo 1-2. The 2026-08-17 "confirmed live" probe only ever read man 1-9 --
# the one run of ids the two numberings share -- so it could not discriminate.
#
# Red fives: the old own-ids answer (35/36/37) lands on 0x23-0x25 = sou 3-5
# here, which is wrong. The red-five signal is byte bit 7 -> u16 bit 15: the
# animations pick the RED SHEET on `value & 0x8000` (atlas 0x442270 vs
# 0x441910), and bit 7 survives all three byte forms, so the hand-form
# bit-6-drop argument above never ruled it out.
WIRE_ID = [((c // 9) << 4) | (c % 9) + 1 for c in range(27)] + \
          [0x31 + i for i in range(7)]
WIRE_BACK = 0x3F                        # the face-down back sprite's id
_WIRE_TO_CANON = {w: c for c, w in enumerate(WIRE_ID)}


def tile_byte(tile, flag=False, pond_flag=False):
    """Canonical tile (janmahjong) -> the wire byte. A red five is its plain
    five id with TILE_FLAG (bit 7 -> the client's u16 0x8000 red-sheet bit)."""
    if tile is None:
        return 0
    k = mj.kind(tile)
    b = WIRE_ID[k] & 0x3F
    if pond_flag:
        b |= POND_FLAG
    if flag or mj.is_red(tile):
        b |= TILE_FLAG
    return b


def tile_u16(tile, flag=False):
    """The u16 form the HAIPAI/TSUMO hand arrays use: bit15 flag, 6-bit id."""
    b = tile_byte(tile, flag)
    return ((b & TILE_FLAG) << 8) | (b & 0x3F)


def tile_from_byte(b):
    """Wire byte -> (canonical tile, flag, pond_flag). Inverse of tile_byte.
    A flagged FIVE is a red five (the flag doubles as the red-sheet bit)."""
    wid = b & 0x3F
    canon = _WIRE_TO_CANON.get(wid, wid)
    if (b & TILE_FLAG) and canon in (4, 13, 22):
        canon |= mj.RED
    return canon, bool(b & TILE_FLAG), bool(b & POND_FLAG)


def tile_from_u16(v):
    canon = _WIRE_TO_CANON.get(v & 0x3F, v & 0x3F)
    if (v & 0x8000) and canon in (4, 13, 22):
        canon |= mj.RED
    return canon, bool(v & 0x8000)


def hand_u16s(tiles, count=14):
    """A seat's concealed hand as `count` u16s, zero-padded. Zero = empty slot."""
    out = [tile_u16(t) for t in tiles[:count]]
    return out + [0] * (count - len(out))


# --- the dead wall (+0x44, "WAMPAI") -----------------------------------------
#
# MEASURED 2026-09-02 (retracting the "+0x44 is the 2D hand rail" reading):
# DAT_00451900 is the DEAD WALL. The client's own debug dump names it WAMPAI
# (yamaguchi__002e9a40) next to TEHAI for the hands; mahdisp__002c06f0 draws it
# as SEVEN TWO-TILE STACKS in the table centre (x advances once per two slots,
# even slot on top); and mahdisp.c:454 feeds even slots 4..12 to the score
# screen as dora indicators with odd 5..13 as the ura under them.
#
# WARNING: RE-MEASURED 2026-09-04 -- BOTH the flag and the slot order were inverted.
#
# THE FLAG. u16 bit 0x80 (byte form bit 0x40, console.c:3899 shifts it up) is
# REVEALED = face-UP, not face-down. Three independent sites, all in the C:
#   * the MjHAIPAI parser copies +0x44, then does `_DAT_00451908 &= 0xff7f`
#     (slot 4, console.c:3283) and after the deal calls yamaguchi__002e5e00 =
#     motion 11 at slot 4; that flip's LANDING (yamaguchi__002ef1c0) does
#     `*wall_slot |= 0x80`. Clear-then-flip-then-SET is a reveal only if SET
#     means face-up.
#   * the static renderer (mahdisp.c:2470..) passes `(v & 0x80) == 0` as
#     gs__002a6430's param_4, and param_4 == 1 is exactly what the ANKAN meld
#     branch (mahdisp.c:2081) passes for its two face-DOWN tiles (rotation
#     table 0x3854b0 = [3,1,0,2]: entries 0 and 1 differ by 180 degrees).
#   * motion 12 (yamaguchi__002ea8a0, the ura spread) skips an even slot whose
#     0x80 is CLEAR together with its partner -- the unrevealed pairs; and the
#     seen-tile scan yamaguchi__002ef450 counts a wall tile only when 0x80 is
#     SET.
#   Until this fix we sent 13 slots "face-down" = 13 slots REVEALED.
#
# THE SLOTS. Dora indicator 0 is at slot 4 (the post-deal auto-flip targets
# slot 4 and only slot 4), its ura at 5; kan-dora k (k = 1..4) at slot 4 + 2k
# with its ura at 5 + 2k; rinshan tiles at 0..3 (yamaguchi__002eb470 scans
# 0 -> 13 for the first occupied slot, and motion 9 zeroes it). The kan-dora
# chain (yamaguchi__002e8990) scans slots 12, 10, 8, 6 for the first with 0x80
# SET -- i.e. the HIGHEST revealed indicator = the newest one -- clears it for
# the beat, flips it (motion 11) and the landing sets it again. So a kan record
# ships the new indicator ALREADY REVEALED, which is exactly what the engine's
# eager `reveal_dora()` gives us. Slot columns 0..6 = R R D K1 K2 K3 K4, the
# physical dead wall.
#
# THE RINSHAN SLOTS after a kan: before each rinshan flight the client's own
# yamaguchi__002e9940 RE-FILLS the dead wall -- it writes a placeholder (id 1,
# face-down) into the slot BEFORE the first occupied one, floor 0 -- and the
# flight then empties the first occupied slot. With 0..3 populated that is:
# overwrite slot 0, pull slot 0; next kan: first occupied is 1, so refill 0,
# pull 0 again. The client's copy therefore has EXACTLY slot 0 empty after any
# number of kans, and `kans >= 1` here empties only slot 0 so a resync agrees
# with it (the haitei-replenishment the real game does, drawn as SE draws it).
#
# The client never reloads this buffer from MjHAIPAI per turn -- its own
# animation events (rinshan draws, kan-dora flips) mutate the copy -- but
# every MjALLDATA rewrites it from +0x44, so `kans` and `dora_shown` must
# track the engine or a resync un-draws a rinshan tile / un-flips an
# indicator.

#: slot of dora indicator i (0 = the deal's, 1..4 = kan-doras) and its ura.
WANPAI_DORA_SLOT = (4, 6, 8, 10, 12)
WANPAI_URA_SLOT = (5, 7, 9, 11, 13)
WANPAI_FLIP_SLOT = 4                   # the post-deal auto-flip (002e5e00)
WANPAI_KAN_SCAN = (12, 10, 8, 6)       # the kan-dora chain's scan order
WANPAI_REVEALED = 0x80                 # u16 form (MjHAIPAI): SET = face-up
WANPAI_REVEALED_BYTE = 0x40            # byte form (MjALLDATA): SET = face-up


def _wanpai_slots(dead, dora_shown=1, kans=0, legacy=False):
    """janmahjong Wall.dead (rinshan 0..3, dora 4..8, ura 9..13) -> 14
    (tile, revealed) pairs in the client's slot order. `kans` rinshan tiles
    have been drawn already: slot 0 is then EMPTY and 1..3 stay populated,
    which is what the client's own re-fill + motion 9 leave in its copy after
    any number of kans (banner); a resync must agree.

    `legacy=True` reproduces the pre-2026-09-04 wire form BYTE FOR BYTE
    (indicator i at slot 12-2i, ura at 13-2i, and the bit inverted: every
    slot but the shown indicators carried it) -- the form that was live until
    the re-measurement above. It exists as a rollback lever
    (`jangame.WANPAI_LEGACY`), not as a layout the C supports.
    """
    slots = [(None, False)] * 14
    for i in range(4):
        t = dead[i] if i < len(dead) else None
        slots[i] = (None if (kans and i == 0) else t, legacy)
    for i in range(5):
        dora = dead[4 + i] if 4 + i < len(dead) else None
        ura = dead[9 + i] if 9 + i < len(dead) else None
        if legacy:
            slots[12 - 2 * i] = (dora, not (i < dora_shown))
            slots[13 - 2 * i] = (ura, True)
        else:
            slots[WANPAI_DORA_SLOT[i]] = (dora, i < dora_shown)
            slots[WANPAI_URA_SLOT[i]] = (ura, False)
    return slots


def wanpai_u16s(dead, dora_shown=1, kans=0, legacy=False):
    """The dead wall as MjHAIPAI's +0x44 array (14 u16s, 0x80 = REVEALED)."""
    out = []
    for tile, revealed in _wanpai_slots(dead, dora_shown, kans, legacy):
        v = tile_u16(tile)
        if v and revealed:
            v |= WANPAI_REVEALED
        out.append(v)
    return out


def wanpai_bytes(dead, dora_shown=1, kans=0, legacy=False):
    """The dead wall as MjALLDATA's +0x44 array (14 bytes, 0x40 = REVEALED)."""
    out = []
    for tile, revealed in _wanpai_slots(dead, dora_shown, kans, legacy):
        b = tile_byte(tile)
        if b and revealed:
            b |= WANPAI_REVEALED_BYTE
        out.append(b)
    return out


# --- header helper -----------------------------------------------------------

def _msg(opcode, seq, body, length=None, sub=INGAME_SUB, src=SRC_SERVER,
         dst=DST_REPLY, id8=0):
    """A server->client in-game record: 0x18-byte header then `body` at +0x18."""
    body = bytes(body)
    total = length if length is not None else 0x18 + len(body)
    rec = bytearray(janwire.pack(opcode=opcode, f13=seq & 0xFF, src=src, dst=dst,
                                 f16=F16_CONST, sub=sub, id8=id8,
                                 length=total))
    rec = rec[:0x18] + bytearray(body)
    if len(rec) < total:
        rec += bytes(total - len(rec))
    return bytes(rec[:max(total, len(rec))])


def _put_is_human(body, is_human):
    """+0x23..+0x26, one byte per seat: the client's own log calls them
    `IsHuman? %d %d %d %d` (console__00285120, string 0x4105b0), and the
    HAIPAI/TSUMO/NAKI/ALLDATA handlers all run it on rec+0x18. Three readers,
    every one of `1 - byte` (= "is a COM"): the seat panel's label form
    (yamaguchi2 +0x12fc -> yamaguchi2__00324c70: a COM draws the C-P-U glyphs
    before the seat digit, a human only the digit), the between-hands ready
    gauge (a COM fills by itself, a human waits for MjREADYSTATUS), and the
    ALLDATA call motions 3-8 (`_DAT_00452ab0`: a COM event seat waits up to
    0xb4 frames for the previous flight to drain, a human does not). Retail
    sent 1 for the humans; we sent zeros for every seat until 2026-09-09."""
    for s, v in enumerate(list(is_human)[:4]):
        body[0x23 - 0x18 + s] = 1 if v else 0


# --- MjHAIPAI 0x22 -- the deal ----------------------------------------------
#
# console__MjClient_Haipai_002851e0, field for field:
#
#   +0x18  u8       MotionCommand      (console__00285120(msg + 0x18))
#                   WARNING: MUST BE 10 for a real deal. The parser ZEROES every
#                   per-tile visibility gate (DAT_004521a0, all 4 seats x 14
#                   slots), then yamaguchi__002e4660 dispatches +0x18 through
#                   the 14-entry motion table at 0x3e6fa0. Entry [10] =
#                   yamaguchi__002ea3b0 is the DEAL ANIMATION: it spawns the
#                   flying tiles and each one restores its gate on landing
#                   (**(obj+0xfc) = 1). motion=0 dispatches a no-op, the gates
#                   stay 0, and EVERY hand on the table renders empty -- the
#                   2026-09-02 invisible-hand bug.
#   +0x22  u8       SEAT BITMASK for motion 10 -- which seats' deals animate
#                   (bit s = seat s). 002ea3b0 skips seats not in the mask,
#                   which leaves their gates zeroed = their hands invisible.
#   +0x28  u32      -> DAT_00452180
#   +0x2c  18 x u8  -> DAT_00452184..95   eighteen consecutive scalars
#   +0x3e  3 bytes  -> 0x452196          (console__002825f0(dst, src, 3))
#   +0x41  3 bytes  -> 0x452199
#   +0x44  14 x u16 -> DAT_00451900      THE DEAD WALL (see the WAMPAI banner
#                   above wanpai_u16s; this is NOT the hand rail)
#   +0x60  4 x 14 x u16, SEAT STRIDE 0x1c -> DAT_00451920   THE FOUR HANDS
#                   WARNING: EACH u16's BIT 7 (0x80) IS FACE-DOWN (mahdisp__002c0f30
#                   draws the back when set, the face when clear -- the same
#                   bit the dead wall uses). Retail hides opponents: only the
#                   local seat is face-up (ml070i/ml073i show the other three
#                   as rows of backs). We must set 0x80 on every non-local
#                   seat's tiles or the player reads the whole table's hands.
#                   Bit 15 (0x8000) is unrelated -- that's the red sheet.
#   +0xd0  u8 -> 0x451fc0  CHIICHA (起家, the seat that dealt E1; the round-
#                          wind marker sits there, mahdisp.c:1948)
#   +0xd1  u8 -> 0x451fb8  DEALER (親)    +0xd2  u8 -> 0x451fa8  ROUND WIND (場:
#                          0 = East, 1 = South; picks "East n"/"South n")
#   +0xd3  u8 -> 0x451fb0  KYOKU (局, 0-based)   +0xd4  u8 -> 0x452028  HONBA
#   +0xd5  s8 -> 0x452040  DIE 1    +0xd6  s8 -> 0x452041  DIE 2  (1..6 each)
#
# 0x60 + 4*0x1c = 0xd0 EXACTLY -- the hands abut the trailing scalars with no
# padding, and that arithmetic closing is the check the array bounds were read
# right. Body runs to +0xd6, so the record is 0xd7 bytes.
#
# VERIFIED: THE SEVEN SCALARS ARE MEASURED (2026-09-04, retracting the "not
# established" reading above them). The client's own debug print
# (mahdisp.c:3016-3019) labels the globals 場/局/起家/親, the label writer
# yamaguchi2__Kyoku_0032ee30 indexes its 8-string table ["East 1".."South 4"]
# with `kyoku + round_wind * 4`, the wall-break seat is computed as
# `(dealer + die1 + die2 + 1) & 3` at console.c:3304 and the two dice are
# DRAWN on the table at the dealer's position by mahdisp.c:1892-1908, which
# indexes a 3-float rotation table with `die * 3 - 2` -- so a 0 die reads
# before the table. Send 1..6. The same globals are written by MjYAKUDISP
# (+0x1d..+0x1f), MjSEISAN (+0x28..+0x2c) and MjALLDATA (+0x13b..+0x13f,
# bit-packed); jangame's `_round_scalars()` is the single source for all four.
#
# THE RULE VECTOR at +0x2c (18 bytes -> DAT_00452184..95), where the client
# reads it -- everything not listed has NO read site in the module:
#
#   +0x2d  [1]  DORA rule enum -> DAT_00452185, the client's own MJS_CONFIG
#               DORA index: 0 Front · 1 Front+ura · 2 Front+kan · 3 All+kan
#               (no kan-ura) · 4 All. Gates the kan-dora chain (2/3/4,
#               yamaguchi.c:8313), the ura spread at a win (3 -> motion 13,
#               4/1 -> motion 12, mahdisp.c:657-680) and the dead-wall layout
#               branch (mahdisp.c:2470).
#   +0x2e  [2]  RED-FIVE RANK, 0-based (4 = fives): mahdisp.c:2863 compares
#               the rank nibble with table 0x3866e0 = [1..6][value].
#   +0x30  [4]  WAREME on/off -> DAT_00452188; the marker is drawn at the
#               wall-break seat the client computes itself (yamaguchi.c:818).
#   +0x31  [5]  RIICHI NEEDS 1000 -> DAT_00452189; mahdisp.c:3544 refuses the
#               Riichi button under 1000 only when this is non-zero.
#   +0x33  [7]  YAKITORI on/off -> DAT_0045218b: console.c:3068 arms every
#               seat's yakitori marker (DAT_00452058, cleared on a win) and
#               yamaguchi2.c:9071 gates the "Yakitori penalties" results stage.
#   +0x3c  [16] UMA for 1st (thousands) and +0x3d [17] UMA for 2nd -> the
#               results uma stage (yamaguchi2.c:8818-8841: 1st +a, 2nd +b,
#               3rd -b, 4th -a, all x1000). Both zero = no uma stage.
#   +0x3e..+0x40 (three_a) per-suit RED-FIVE COUNTS -> 0x452196..98, the
#               ceiling mahdisp.c:2860's red marking honours.
#   +0x2c, +0x2f, +0x32, +0x34..+0x3b, +0x41..+0x43: copied, never read.
HAIPAI_LEN = 0xD7
HAIPAI_HAND = 0x60
HAIPAI_SEAT_STRIDE = 0x1C
#: indices into the 18-byte rule vector (record offset = 0x2c + index)
RULE_DORA, RULE_AKA_RANK, RULE_WAREME, RULE_RIICHI_1000 = 1, 2, 4, 5
RULE_YAKITORI, RULE_UMA_1ST, RULE_UMA_2ND = 7, 16, 17
#: the client's DORA enum, by (ura_dora, kan_dora, kan_ura)
DORA_KIND = {(False, False, False): 0, (True, False, False): 1,
             (False, True, False): 2, (True, True, False): 3,
             (True, True, True): 4}


FACE_DOWN = 0x80                 # hand-u16 bit 7: draw the back, not the face


def rule_scalars(dora_kind=0, aka_rank=5, wareme=False, riichi_1000=True,
                 yakitori=False, uma=(20, 10)):
    """The 18-byte MjHAIPAI rule vector (+0x2c). `aka_rank` is 1-based (5 =
    fives); `uma` is (1st, 2nd) in thousands."""
    v = bytearray(18)
    v[RULE_DORA] = int(dora_kind) & 0xFF
    v[RULE_AKA_RANK] = max(0, int(aka_rank) - 1) & 0xFF
    v[RULE_WAREME] = 1 if wareme else 0
    v[RULE_RIICHI_1000] = 1 if riichi_1000 else 0
    v[RULE_YAKITORI] = 1 if yakitori else 0
    v[RULE_UMA_1ST] = int(abs(uma[0])) & 0xFF
    v[RULE_UMA_2ND] = int(abs(uma[1])) & 0xFF
    return bytes(v)


def haipai(seq, hands, position=(), motion=0, seat_mask=0, concealed=(), u28=0,
           scalars18=(), three_a=b"\0\0\0", three_b=b"\0\0\0",
           chiicha=0, dealer=0, round_wind=0, kyoku=0, honba=0, dice=(1, 1),
           is_human=(0, 0, 0, 0)):
    """MjHAIPAI: the four starting hands and the round's scalars.

    `hands` is four sequences of canonical tiles (janmahjong ids). A real deal
    wants motion=10 and seat_mask=0x0F (see the +0x18/+0x22 notes in the
    banner); `position` is the dead wall (wanpai_u16s); `concealed` is the set
    of seats to draw face-DOWN (every seat but the local player's -- retail
    shows only your own hand). `scalars18` is `rule_scalars()`, `three_a` the
    per-suit red-five counts. The seven trailing scalars are +0xd0..+0xd6 in
    the banner's order; `dice` is (die1, die2), 1..6 each.
    """
    concealed = set(concealed)
    body = bytearray(HAIPAI_LEN - 0x18)
    _put_is_human(body, is_human)

    def put(off, data):
        body[off - 0x18:off - 0x18 + len(data)] = data

    body[0] = motion & 0xFF                                     # +0x18
    body[0x22 - 0x18] = seat_mask & 0xFF                        # +0x22
    struct.pack_into("<I", body, 0x28 - 0x18, u28 & 0xFFFFFFFF)
    for i, v in enumerate(list(scalars18)[:18]):
        body[0x2C - 0x18 + i] = v & 0xFF
    put(0x3E, bytes(three_a)[:3].ljust(3, b"\0"))
    put(0x41, bytes(three_b)[:3].ljust(3, b"\0"))
    for i, v in enumerate(list(position)[:14]):
        struct.pack_into("<H", body, 0x44 - 0x18 + 2 * i, v & 0xFFFF)
    for s in range(4):
        tiles = hand_u16s(hands[s] if s < len(hands) else [])
        down = s in concealed
        for i, v in enumerate(tiles):
            if down and v:
                v |= FACE_DOWN
            struct.pack_into("<H", body,
                             HAIPAI_HAND - 0x18 + s * HAIPAI_SEAT_STRIDE + 2 * i,
                             v & 0xFFFF)
    d1, d2 = (list(dice) + [1, 1])[:2]
    for off, v in ((0xD0, chiicha), (0xD1, dealer), (0xD2, round_wind),
                   (0xD3, kyoku), (0xD4, honba), (0xD5, d1), (0xD6, d2)):
        body[off - 0x18] = v & 0xFF
    return _msg(MjHAIPAI, seq, body, length=HAIPAI_LEN)


# --- MjTSUMO 0x24 -- a draw --------------------------------------------------
#
# console__MjClient_Tsumo_002857e0 plus mahdisp__002c2ac0 (the discard picker it
# hands the same buffer to, which reads three fields the handler itself does
# not):
#
#   +0x18  u8       MotionCommand (0 normal, 2, 9 = run an animation first)
#                   MEASURED 2026-09-02 off the motion table at 0x3e6fa0:
#                   2 = yamaguchi__002e6740, THE DRAW ANIMATION -- seat +0x22
#                       draws tile +0x1b from the wall into hand slot 13, and
#                       this is the ONLY writer that re-opens slot 13's
#                       visibility gate (0x4521d4 + seat*0x38, zeroed by every
#                       HAIPAI parse) -- without it the drawn tile is
#                       INVISIBLE. Tile 0 aborts the spawn (sprite map 0), so
#                       motion=2 with an empty +0x1b animates NOTHING.
#                   3 = yamaguchi__002e73e0, THE DISCARD ANIMATION -- seat
#                       +0x22 discards tile +0x1b from hand slot +0x1d into
#                       its pond. This is the ONLY thing that fills an
#                       OPPONENT's pond outside an ALLDATA resync (there is no
#                       server->client MjSUTE at all; the local player's own
#                       discard is synthesised client-side). Slot 13 = a
#                       tsumogiri and carries riichi-sideways handling.
#   +0x1b  u8       the ANIMATED TILE (byte form, bit 7 = red sheet)
#   +0x1d  u8       the hand SLOT the animation works on
#   +0x21  u8       motion 3 only: RIICHI DECLARED WITH THIS DISCARD. The
#                   remote branch of the discard flight (yamaguchi.c:7920-7932)
#                   tests `== 1` exactly: voice 0x18, the sideways pond tile
#                   (0x45a49c), the chase-riichi block (0x457b48..) and banner
#                   0 (the riichi cut-in). Never written before 2026-09-04, so
#                   every CPU riichi was a silent ordinary discard.
#   +0x22  u8       the ANIMATED SEAT (NOT the drawing seat at +0x28)
#   +0x28  u16      the DRAWING SEAT, compared against [0x0042abc0] = my seat
#   +0x2a  14 x u16 that seat's hand -- SAME global and SAME 0x1c stride as
#                   MjHAIPAI's, which is the strongest structural cross-check
#                   available without a capture
#   +0x46..+0x49    four per-seat display bytes -> DAT_003865bd..c0
#   +0x4a  u8       the WALL COUNT (yamaguchi2__TsuMoSuu)
#   +0x4b  u8       -> DAT_003865bc          +0x4c  u8 -> DAT_003865b4
#   +0x4d  7 x u8   THE SELF-TURN ACTION MENU (MyMove) enable block -- the same
#                   7-byte structure the call menu uses at +0x4c, read through
#                   the MyMove itemmap DAT_004099b0 = [2,0,3,6] over the items
#                   [Riichi,Tsumo,Kan,Kyushu] (help strings PTR_00409970). So
#                   the unified byte layout is identical to the call menu's:
#                       [0]=Tsumo  [2]=Riichi  [3]=Kan  [6]=Kyushu kyuhai
#                   (byte 0 ALSO feeds yamaguchi__002e6cb0, which gates the
#                   client-side Tsumo lamp on points/phase -- NOT a riichi
#                   test, despite an earlier mislabel here). A button lights
#                   iff its byte is non-zero; Cancel is always shown. WARNING: We
#                   used to set only byte 0, so Riichi never appeared and Tsumo
#                   lit on any tenpai hand (2026-09-03).
#   +0x54  14 x u8  PER-HAND-SLOT FLAGS -> DAT_00446d50; bit 1 marks a slot the
#                   cursor may land on, so this is what makes a tile
#                   discardable. A riichi-declared hand sends only one.
#   +0x62  u8 -> _DAT_00451f88   +0x63  u8   +0x64  u8   (menu gates)
#
# Body therefore runs to at least +0x64; we send 0x68 so the three tail bytes
# are always present.
TSUMO_LEN = 0x68
TSUMO_HAND = 0x2A


def tsumo(seq, seat, hand, wall_count, motion=0, slot_flags=None,
          riichi_test=b"\0" * 7, per_seat=(0, 0, 0, 0), f4b=0, f4c=0,
          f62=0, f63=0, f64=0, anim_tile=0, anim_slot=0, anim_seat=0,
          riichi=0, is_human=(0, 0, 0, 0)):
    """MjTSUMO: seat `seat` has drawn; `hand` is its 14 tiles.

    `slot_flags` is the 14-byte selectability array at +0x54. Default: every
    occupied slot selectable (bit 1), which is an ordinary turn. Pass a
    single-slot array for a riichi hand -- the client's cursor only stops on
    slots whose flag has bit 1 set (`(*pbVar & 2) == 0` skips).

    `anim_tile`/`anim_slot`/`anim_seat` feed the MotionCommand (see the
    banner): motion 2 animates anim_seat drawing anim_tile; motion 3 animates
    anim_seat discarding anim_tile from anim_slot. anim_tile is the WIRE BYTE.
    `riichi=1` on a motion-3 record marks that discard as the riichi
    declaration (+0x21); motion 9 is the RINSHAN draw (anim_tile from the dead
    wall, the client pulls and zeroes the wall slot itself).
    """
    body = bytearray(TSUMO_LEN - 0x18)
    _put_is_human(body, is_human)
    body[0] = motion & 0xFF
    body[0x1B - 0x18] = anim_tile & 0xFF
    body[0x1D - 0x18] = anim_slot & 0xFF
    body[0x21 - 0x18] = 1 if riichi else 0
    body[0x22 - 0x18] = anim_seat & 0xFF
    struct.pack_into("<H", body, 0x28 - 0x18, seat & 0xFFFF)
    for i, v in enumerate(hand_u16s(hand)):
        struct.pack_into("<H", body, TSUMO_HAND - 0x18 + 2 * i, v & 0xFFFF)
    for i, v in enumerate(list(per_seat)[:4]):
        body[0x46 - 0x18 + i] = v & 0xFF
    body[0x4A - 0x18] = wall_count & 0xFF
    body[0x4B - 0x18] = f4b & 0xFF
    body[0x4C - 0x18] = f4c & 0xFF
    body[0x4D - 0x18:0x4D - 0x18 + 7] = bytes(riichi_test)[:7].ljust(7, b"\0")
    if slot_flags is None:
        slot_flags = [2 if i < len(hand) else 0 for i in range(14)]
    for i, v in enumerate(list(slot_flags)[:14]):
        body[0x54 - 0x18 + i] = v & 0xFF
    body[0x62 - 0x18] = f62 & 0xFF
    body[0x63 - 0x18] = f63 & 0xFF
    body[0x64 - 0x18] = f64 & 0xFF
    return _msg(MjTSUMO, seq, body, length=TSUMO_LEN)


# --- MjSUTE 0x25 -- the client's discard (INBOUND) --------------------------
#
# The client does not build a fresh record: it overwrites the MjTSUMO buffer's
# header (opcode 0x25, len 0x20, src = my seat, dst = 4, f16 = 1, sub = 3, f13 =
# the adopted sequence) and puts **one u32 at +0x18** = the value
# mahdisp__002c2ac0 returned from the discard UI. Logged as `TSUMO:%08xd`.
#
#   word & 0x0f        the HAND SLOT INDEX the client discarded (the client
#                      itself indexes DAT_00451920 with `& 0xf`)
#   word & 0xf0000     what it did:
#       0x00000  an ordinary discard
#       0x10000  RIICHI + this discard   (it plays the reach call, marks the
#                pond tile rotated and bumps its own riichi counter)
#       0x20000  no tile index -- the turn ENDS here (self-draw win)
#       0x30000  a tile index, and again the turn ends (a kan: the arm is
#                gated on `malloc__002c5710(idx)`, a can-I-kan-with-this test)
#       0x40000  no tile index, turn ends (the remaining menu action)
#
# WARNING: MEASURED: the field, the masks, and that 0x20000/0x30000 stop the client
# from continuing its turn (`bVar1 = true` skips the pond update). INFERRED: the
# NAME of each action. 0x10000 = riichi is solid -- that arm plays the reach
# voice line and sets the pond rotation. The other three are named by
# elimination from the menu and want one live confirmation each.
SUTE_ACTION_MASK = 0xF0000
SUTE_DISCARD, SUTE_RIICHI = 0x00000, 0x10000
SUTE_TSUMO_AGARI, SUTE_KAN, SUTE_OTHER = 0x20000, 0x30000, 0x40000
SUTE_ACTION_NAMES = {
    SUTE_DISCARD: "discard", SUTE_RIICHI: "riichi", SUTE_TSUMO_AGARI: "tsumo",
    SUTE_KAN: "kan", SUTE_OTHER: "other",
}


def parse_sute(rec):
    """MjSUTE -> {'seat', 'index', 'action', 'action_name', 'word', 'seq'}."""
    h = janwire.unpack(rec)
    word = struct.unpack_from("<I", rec, 0x18)[0] if len(rec) >= 0x1C else 0
    action = word & SUTE_ACTION_MASK
    return {"seat": h["src"], "seq": h["f13"], "word": word,
            "index": word & 0x0F, "action": action,
            "action_name": SUTE_ACTION_NAMES.get(action, "?%05x" % action)}


# --- MjNAKI 0x26 -- "here are your available calls" -------------------------
#
# console__MjClient_Naki_002860b0 for the display fields, mahdisp__002c36e0 (the
# reply encoder it calls) for the per-seat gates -- and it is the gates that
# matter, because they are what lights up each button:
#
#   +0x28  u16      -> _DAT_00451f88  THE CLAIMABLE TILE (u16 tile form). Not
#                   decoration: the pon reply arm spins `while (hand[i] & 0x3f)
#                   != (_DAT_00451f88 & 0x3f)` over the 14 slots, so a zero
#                   here HANGS the client the moment Pon is chosen, and the chi
#                   ghost preview positions are hand-tile minus this value.
#   +0x2a  s8       -> DAT_00452050 = value + 1: the DISCARDER's seat. The pond
#                   renderer (mahdisp.c:2347) blinks that seat's newest pond
#                   tile while the offer is open.
#   +0x2b  4 x u8   PER SEAT: non-zero = this seat has something to answer.
#                   Zero for my seat and the whole handler is skipped.
#   +0x2f..+0x32    four display bytes (the same globals MjTSUMO's +0x46..+0x49
#                   feed, one level along)
#   +0x33  u8       the WALL COUNT
#   +0x34  4 x u8   per seat -> DAT_003865bc
#   +0x38  4 x u8   per seat -> DAT_003865b4
#   +0x3c  4 x u8   per seat: enables the FIRST call button   -> word 0x10000
#   +0x40  4 x u8   per seat: enables button 3                -> word 0x40000
#   +0x44  4 x u8   per seat: enables button 4  (one tile)    -> tile | 0x20000
#   +0x48  4 x u8   per seat: enables button 5  (walks +-1/+-2 through the hand,
#                   so this is the SEQUENCE call)             -> tile | 0x30000
#   +0x4c  7 x u8 per seat  the call-menu state block: WHICH MENU ITEMS EXIST.
#                   All zeros = the menu builds as Cancel-only (the 09-03 bug).
#                   Measured layout (yamaguchi2__00328900 enables window item i
#                   iff block[itemmap[i]] != 0; the OpenChoice map DAT_004099d0
#                   is [1,5,4,3,-1] over [Ron,Chi,Pon,Kan,Cancel], the MyMove
#                   map DAT_004099b0 is [2,0,3,6,-1,-1] over [Riichi,Tsumo,Kan,
#                   Kyushu,Leave,Cancel]):
#                       [0] Tsumo   [1] RON   [2] Riichi  [3] KAN
#                       [4] PON     [5] CHI   [6] Kyushu kyuhai
#                   Cancel maps to -1 = always shown. Before the menu even
#                   opens, mahdisp__002c36e0 applies the player's saved
#                   call-notification mode (_DAT_00452918, lobby.c:113-128):
#                   mode 2 auto-passes unless block[1] (Ron), mode 1 unless
#                   block[1] or block[4] (Ron/Pon), mode 0 always prompts. So
#                   serve the TRUTHFUL bytes and the client applies the
#                   player's own preference -- never force [1]/[4] to defeat
#                   the gate, that draws call buttons whose reply arms are
#                   dead (their +0x3c..+0x48 gate is 0 -> selecting them does
#                   nothing).
#   +0x68  14 x u8 per seat the per-hand-slot flags for choosing the tile
#                          (`& 7`, so three bits are live here). Only the CHI
#                          navigator reads them: it walks between non-zero
#                          slots and the slot it stops on is the tile the ack
#                          carries -- flag exactly the chi-pair kinds.
#   +0xa0  4 x u8   per seat -> DAT_00452070
#
# 0x68 + 4*0x0e = 0xa0 exactly -- the same abutting-blocks check as MjHAIPAI's.
# Body runs to +0xa3.
#
# WHICH CALL IS WHICH -- CONFIRMED 2026-09-03 (was inference): the OpenChoice
# selection map DAT_00386210 = [1,5,4,3,-1] returns 1/5/4/3 for the labelled
# items Ron/Chi/Pon/Kan (help strings at 0x409990), and mahdisp__002c36e0
# routes 1 -> +0x3c gate -> 0x10000, 5 -> +0x48 -> tile|0x30000 (the +-1/+-2
# hand walk), 4 -> +0x44 -> tile|0x20000, 3 -> +0x40 -> 0x40000. So +0x3c=ron,
# +0x40=kan, +0x44=pon, +0x48=chi, exactly as NAKI_CALL_OFFSETS has them.
NAKI_LEN = 0xA4
NAKI_CALL_OFFSETS = {"ron": 0x3C, "kan": 0x40, "pon": 0x44, "chi": 0x48}


def naki(seq, wall_count, calls=None, u28=0, s2a=0, per_seat_34=(0, 0, 0, 0),
         per_seat_38=(0, 0, 0, 0), per_seat_a0=(0, 0, 0, 0),
         display=(0, 0, 0, 0), menu=None, slot_flags=None,
         is_human=(0, 0, 0, 0)):
    """MjNAKI: offer calls on the tile just discarded.

    `calls` is {seat: {"ron"/"kan"/"pon"/"chi": True}} -- any seat with at least
    one goes non-zero at +0x2b, which is the gate that makes its client answer
    at all. `slot_flags` is {seat: [14 bytes]} marking which of that seat's own
    tiles the call may use.
    """
    calls = calls or {}
    body = bytearray(NAKI_LEN - 0x18)
    _put_is_human(body, is_human)
    struct.pack_into("<H", body, 0x28 - 0x18, u28 & 0xFFFF)
    body[0x2A - 0x18] = s2a & 0xFF
    for s in range(4):
        body[0x2B - 0x18 + s] = 1 if calls.get(s) else 0
    for i, v in enumerate(list(display)[:4]):
        body[0x2F - 0x18 + i] = v & 0xFF
    body[0x33 - 0x18] = wall_count & 0xFF
    for i, v in enumerate(list(per_seat_34)[:4]):
        body[0x34 - 0x18 + i] = v & 0xFF
    for i, v in enumerate(list(per_seat_38)[:4]):
        body[0x38 - 0x18 + i] = v & 0xFF
    for cname, off in NAKI_CALL_OFFSETS.items():
        for s in range(4):
            body[off - 0x18 + s] = 1 if calls.get(s, {}).get(cname) else 0
    menu = menu or {}
    for s in range(4):
        blk = bytes(menu.get(s, b"\0" * 7))[:7].ljust(7, b"\0")
        body[0x4C - 0x18 + s * 7:0x4C - 0x18 + s * 7 + 7] = blk
    slot_flags = slot_flags or {}
    for s in range(4):
        flags = list(slot_flags.get(s, [0] * 14))[:14]
        flags += [0] * (14 - len(flags))
        for i, v in enumerate(flags):
            body[0x68 - 0x18 + s * 14 + i] = v & 0xFF
    for i, v in enumerate(list(per_seat_a0)[:4]):
        body[0xA0 - 0x18 + i] = v & 0xFF
    return _msg(MjNAKI, seq, body, length=NAKI_LEN)


# --- MjNAKIACK 0x27 -- the client's answer (INBOUND) ------------------------
#
# Built the same way MjSUTE is (the received buffer, rewritten in place, len
# 0x20) with mahdisp__002c36e0's return at +0x18:
#
#   0                       PASS -- no call
#   0x10000                 the +0x3c button, no tile
#   0x40000                 the +0x40 button, no tile
#   tile_u16 | 0x20000      the +0x44 button, carrying the CHOSEN TILE
#   tile_u16 | 0x30000      the +0x48 button (the sequence walk), same
#
# so the low 16 bits are a TILE in the client's u16 form, not an index -- unlike
# MjSUTE, where they are a hand slot. That asymmetry is real and easy to get
# wrong.
NAKI_PASS = 0
NAKI_RON, NAKI_KAN = 0x10000, 0x40000
NAKI_PON, NAKI_CHI = 0x20000, 0x30000
NAKI_ACTION_NAMES = {NAKI_PASS: "pass", NAKI_RON: "ron", NAKI_KAN: "kan",
                     NAKI_PON: "pon", NAKI_CHI: "chi"}

# WHICH CHI THE CLIENT MEANS (finding 20, measured 2026-09-04). The chi arm
# returns the tile UNDER THE CURSOR (`hand[cursor] | 0x30000`, mahdisp.c:3931),
# and the ghost preview that shows the player the run (mahdisp.c:3906-3919)
# derives the partner from `cursor_rank - claimed_rank`:
#
#     +2 -> partner claimed+1      pair (d+1, d+2)     cursor is the high end
#     +1 -> partner claimed+2      pair (d+1, d+2)     cursor is the middle
#     -1 -> partner claimed+1      pair (d-1, d+1)     cursor is the low end
#     -2 -> partner claimed-1      pair (d-2, d-1)     cursor is the low end
#
# so the cursor tile names the pair without ambiguity EXCEPT that the (d-1, d+1)
# shape is reachable only from the d-1 side, and the (d+1, d+2) shape from
# either of its tiles. A first-match over `chi_options` picks the wrong pair
# whenever the hand holds both shapes -- the tiles that leave the hand are
# then not the ones the player was shown.
CHI_PARTNER = {2: (1, 2), 1: (1, 2), -1: (-1, 1), -2: (-2, -1)}


def chi_pair_from_ack(claimed_kind, acked_kind):
    """(lo, hi) partner KINDS of the run the client's chi ack means, from the
    claimed tile's kind and the acked (cursor) tile's kind; None if the ack is
    not a rank neighbour in the same suit."""
    if claimed_kind >= 27 or acked_kind >= 27:
        return None
    if claimed_kind // 9 != acked_kind // 9:
        return None
    off = CHI_PARTNER.get(acked_kind - claimed_kind)
    if off is None:
        return None
    lo, hi = claimed_kind + off[0], claimed_kind + off[1]
    if lo // 9 != claimed_kind // 9 or hi // 9 != claimed_kind // 9:
        return None
    return (lo, hi)


def parse_nakiack(rec, claimed=None):
    """MjNAKIACK -> {'seat', 'action', 'action_name', 'tile', 'word', 'seq',
    'chi_pair'}. `tile` is the CURSOR tile (canonical), which for a chi names
    the shape; pass `claimed` (the offered tile) to have `chi_pair` filled with
    the partner kinds via `chi_pair_from_ack`."""
    h = janwire.unpack(rec)
    word = struct.unpack_from("<I", rec, 0x18)[0] if len(rec) >= 0x1C else 0
    action = word & SUTE_ACTION_MASK
    tile = None
    if action in (NAKI_PON, NAKI_CHI):
        tile = tile_from_u16(word & 0xFFFF)[0]
    pair = None
    if action == NAKI_CHI and tile is not None and claimed is not None:
        pair = chi_pair_from_ack(mj.kind(claimed), mj.kind(tile))
    return {"seat": h["src"], "seq": h["f13"], "word": word, "action": action,
            "tile": tile, "chi_pair": pair,
            "action_name": NAKI_ACTION_NAMES.get(action, "?%05x" % action)}


# --- MjYAKUDISP 0x28 -- the scoring display ---------------------------------
#
# `MjClient_MjYAKUDISP` is four lines and hands the message to mahdisp.cc, which
# is where the body is read -- a short handler does NOT mean a short message.
# Read out of mahdisp__002bcba0 (the sole 0x28 consumer) and the yaku-panel
# walker lruleset__00322cc0 / __00322d70, field by field (2026-09-04):
#
#   +0x18  u8   HAN        -> the limit tiers test it: < 5 normal; 5 mangan,
#                            6-7 haneman, 8-10 baiman, 11-12 sanbaiman, 13+
#                            yakuman (mahdisp.c:756-767)
#   +0x19  u8   FU
#   +0x1a  u8   WINNER seat -> _DAT_00451fa0 (hand reveal, score bucket)
#   +0x1b  u8   DEALER seat -> _DAT_00451fb8 (winner == dealer picks the
#                            dealer-tsumo split)
#   +0x1c  u8   0 = TSUMO / non-zero = RON (banner 2 vs 1, voice 0x13/0x14 vs
#                            0x11/0x12, and the payment reading below)
#   +0x1d  u8   ROUND WIND -> _DAT_00451fa8    +0x1e  u8  KYOKU -> _DAT_00451fb0
#   +0x1f  u8   HONBA      -> DAT_00452028     (the same globals MjHAIPAI's
#                            +0xd2/+0xd3/+0xd4 write; the panel header reads
#                            them)
#   +0x20  u8   YAKUMAN flag -> DAT_00446c78: non-zero forces the limit path
#                            regardless of han
#   +0x21  u8   SHOW URA   -> _DAT_00446d30: non-zero makes the win reveal
#                            spread the dead wall (motion 12/13 by the DORA
#                            rule) so the ura indicators show
#   +0x22  16 x (u16 YAKU ID, u16 HAN) pairs, id 0 terminates; the walker
#               steps 4 bytes for 11 row slots. The id indexes the 45-record
#               glyph table at 0x409540 (stride 0xC) -- the `janyaku.py NAMES`
#               id space: 1..28 ordinary, 32..44 yakuman -- with NO bounds
#               check. Ordinary rows (id < 0x20) draw their han from the
#               second word; yakuman rows draw none. Records 0 and 29..31 are
#               EMPTY in the table, so dora/ura/aka all go on one "Dora" row
#               (id 28) with the summed count.
#   +0x62  u16  unread
#   +0x64  s32  BASE PAYMENT -> _DAT_00446c60   +0x68  s32  DEALER'S SHARE ->
#               _DAT_00446c68. The client totals them (mahdisp.c:722-741):
#               ron            total = [+0x64]
#               dealer tsumo   total = [+0x64] * 3      (each non-dealer pays it)
#               other tsumo    total = [+0x68] + [+0x64] * 2
#               in POINTS, no scaling -- a 1000-point ron sends 1000. Never
#               written before 2026-09-04, hence the "0 pt".
#
# WARNING: RETRACTED with this: the old "+0x22 32 x u16 = the tile list drawn on the
# results panel". We sent TILE CODES into the yaku-id field, so the win screen
# indexed the yaku table with tile numbers and invented yaku (Haitei +
# Shousangen + Daisuushii on a 3-han hand, 2026-09-04 screenshot). The panel's
# TILES come from the table state (DAT_00451920 + seat*0x1c), not from here.
YAKUDISP_LEN = 0x6C
YAKU_PAIRS_OFF, YAKU_PAIRS_MAX = 0x22, 16

#: janmahjong Score.yaku name -> the client's yaku record id (janyaku.NAMES).
#: Verified against the glyph table at 0x409540: ids 1..28 ordinary, 32..44
#: yakuman; 0 and 29..31 are empty records.
YAKU_ID = {
    "double_riichi": 1, "riichi": 2, "ippatsu": 3, "haitei": 4, "houtei": 5,
    "menzen_tsumo": 6, "rinshan": 7, "chankan": 8, "tanyao": 9, "pinfu": 10,
    "iipeiko": 11, "sanshoku": 12, "sanshoku_doukou": 13, "ittsuu": 14,
    "sanankou": 15, "sankantsu": 16, "toitoi": 17, "chiitoitsu": 18,
    "shousangen": 19, "honitsu": 20, "honroutou": 21, "chanta": 22,
    "junchan": 23, "ryanpeiko": 24, "chinitsu": 25,
    "double_wind": 26, "yakuhai": 27, "dora": 28, "aka": 28, "ura": 28,
    "tenhou": 32, "chiihou": 33, "renhou": 34, "kokushi": 35, "kokushi13": 35,
    "daisangen": 36, "shousuushii": 37, "daisuushii": 38, "chinroutou": 39,
    "suuankou": 40, "suuankou_tanki": 40, "suukantsu": 41, "tsuuiisou": 42,
    "ryuuiisou": 43, "chuuren": 44, "chuuren9": 44,
}
YAKUMAN_ID_MIN = 0x20            # the client's own gate: id < 0x20 draws han


def yaku_rows(yaku):
    """janmahjong `Score.yaku` ([(name, han)]) -> [(id, han)] for +0x22.

    The engine names a dragon triplet `yakuhai_<tile>` and the two wind
    triplets `bakaze` (round wind) / `jikaze` (seat wind): each is an id-27
    "Yakuhai" row, EXCEPT that bakaze + jikaze together (seat wind == round
    wind, the same triplet counted twice) merge into ONE "double wind" row (id
    26, 2 han) -- that is the record the client's table has for 連風牌. dora /
    aka / ura merge into one id-28 row with the summed count. A name with no
    record (nagashi_mangan) is dropped rather than sent as a wild index -- the
    client's table lookup is unbounded.
    """
    rows = []
    winds = {}
    dora = 0
    for name, han in yaku:
        if name in ("bakaze", "jikaze"):
            winds[name] = winds.get(name, 0) + int(han)
            continue
        if name in ("dora", "aka", "ura"):
            dora += int(han)
            continue
        rid = 27 if name.startswith("yakuhai_") else YAKU_ID.get(name)
        if rid is None:
            continue
        rows.append((rid, 0 if rid >= YAKUMAN_ID_MIN else int(han)))
    if "bakaze" in winds and "jikaze" in winds:
        rows.append((26, winds["bakaze"] + winds["jikaze"]))
    else:
        for name in ("bakaze", "jikaze"):
            if name in winds:
                rows.append((27, winds[name]))
    if dora:
        rows.append((28, dora))
    return rows[:YAKU_PAIRS_MAX]


def yakudisp(seq, han=0, fu=0, winner=0, dealer=0, ron=0, round_wind=0,
             kyoku=0, honba=0, yakuman=0, show_ura=0, yaku=(), pay_base=0,
             pay_dealer=0):
    """MjYAKUDISP: the win as the client scores and displays it.

    `yaku` is [(id, han)] (see `yaku_rows`). `pay_base`/`pay_dealer` are the
    +0x64/+0x68 words in POINTS, read as the banner says: a ron sends the whole
    payment in `pay_base`; a dealer tsumo sends the per-player payment there; a
    non-dealer tsumo sends the non-dealer payment there and the dealer's in
    `pay_dealer`.
    """
    body = bytearray(YAKUDISP_LEN - 0x18)
    for off, v in ((0x18, han), (0x19, fu), (0x1A, winner), (0x1B, dealer),
                   (0x1C, 1 if ron else 0), (0x1D, round_wind), (0x1E, kyoku),
                   (0x1F, honba), (0x20, 1 if yakuman else 0),
                   (0x21, 1 if show_ura else 0)):
        body[off - 0x18] = int(v) & 0xFF
    for i, (rid, rhan) in enumerate(list(yaku)[:YAKU_PAIRS_MAX]):
        struct.pack_into("<HH", body, YAKU_PAIRS_OFF - 0x18 + 4 * i,
                         int(rid) & 0xFFFF, int(rhan) & 0xFFFF)
    struct.pack_into("<i", body, 0x64 - 0x18, int(pay_base))
    struct.pack_into("<i", body, 0x68 - 0x18, int(pay_dealer))
    return _msg(MjYAKUDISP, seq, body, length=YAKUDISP_LEN)


# --- MjSEISAN 0x2a -- the settlement ----------------------------------------
#
#   +0x18  4 x s32   THE FOUR SEATS' SCORES, widened to s64 by the reader --
#                    so NEGATIVE SCORES GO ON THE WIRE, which is exactly what
#                    MJS_CONFIG_IDX_HAKO governs
#   +0x28..+0x2c     the round-state block, in THIS order (mahdisp__002bd2d0,
#                    mahdisp.c:788-793): +0x28 CURRENT seat -> _DAT_00451fa0,
#                    +0x29 DEALER -> _DAT_00451fb8, +0x2a ROUND WIND ->
#                    _DAT_00451fa8, +0x2b KYOKU -> _DAT_00451fb0, +0x2c HONBA
#                    -> DAT_00452028. (We used to send round wind, kyoku,
#                    honba, sticks, 0 -- the honba landed in the round wind.)
#
# Body is 0x2d bytes, and the client acks with 0x2b.
SEISAN_LEN = 0x2D


def seisan(seq, scores, current_seat=0, dealer=0, round_wind=0, kyoku=0,
           honba=0, state=None):
    """MjSEISAN: the four scores after a hand. Negatives are allowed and real.
    `state` (5 bytes, the banner's order) overrides the named fields."""
    body = bytearray(SEISAN_LEN - 0x18)
    for i in range(4):
        struct.pack_into("<i", body, 4 * i, int(scores[i]))
    if state is None:
        state = (current_seat, dealer, round_wind, kyoku, honba)
    for i, v in enumerate(list(state)[:5]):
        body[0x28 - 0x18 + i] = v & 0xFF
    return _msg(MjSEISAN, seq, body, length=SEISAN_LEN)


# --- MjGAMEEND 0x2c ---------------------------------------------------------
#
# The in-game loop's arm sets phase 0x0e and calls console__00287a80; lobby.cc
# then leaves the game loop and sends MjBYE (0x2d) with f16 = 0. No body field
# has been read, so this is a bare 32-byte record.
def gameend(seq):
    return _msg(MjGAMEEND, seq, b"", length=0x20)


# --- MjALLDATA 0x2e -- full state, AND an event channel ---------------------
#
# 320 bytes (0x140). One 4-iteration loop over the seats:
#
#   +0x18  u8        the SUBTYPE -- and it is ALSO the MotionCommand. The same
#                    byte every other record puts a motion in: the handler
#                    switches on it for its own pre-work (cases 3..8) and then
#                    hands the record to yamaguchi__002e4660, the 14-entry
#                    motion table. See the CALL MOTIONS banner below.
#   +0x1a  u8        motions 4/5/6: the DISCARDER's seat (whose pond tail the
#                    claimed tile leaves).
#   +0x1c  u8        the EVENT TILE (subtype 3): the tile the event animates,
#                    read by console__MjClient_MjALLDATA and handed to
#                    yamaguchi__002e1ca0 -> the motion-2 pop (byte form,
#                    bit 7 = red sheet). Zero for a plain resync.
#                    ALIASED: motions 4/5 read it as the SECOND claimed hand
#                    tile. A record is one or the other, never both.
#   +0x1e  u8        the EVENT PRESENT flag: the handler runs the subtype-3
#                    animation only when this is non-zero. Zero for a resync.
#                    ALIASED: motions 4/5/6/7/8 read it as a HAND SLOT.
#   +0x22  u8        the EVENT SEAT: which seat the subtype animates for. For a
#                    call motion, the CALLER.
#   +0x23  4 x u8    PER SEAT, measured 2026-09-04 from console__00285120 (which
#                    every handler calls with rec+0x18, so it reads +0x23..+0x26)
#                    and from the motion handlers' own `rec[0x23+seat] == 0`
#                    gates: `1 - byte` goes to yamaguchi2__00325f10 per seat and
#                    to _DAT_00452ab0, which console__00286420 tests to decide
#                    whether to WAIT for the previous flight to drain.
#                    CORRECTED 2026-09-09: the client logs these as
#                    `IsHuman? %d %d %d %d`; `1 - byte` is "is a COM". A COM
#                    event seat waits for the flight, a human's does not. See
#                    `_put_is_human` (the same bytes in HAIPAI/TSUMO/NAKI).
#   +0x28  4 x s32   scores
#   +0x44  the 14-slot position array
#   +0x52  stride 0x0e   the four HANDS (byte tiles, hand unpack)
#   +0x8a  stride 0x17   the four PONDS, 23 tiles each. Pond unpack maps wire
#                        bit 6 -> u16 bit 7 (0x80), and WARNING: 0x80 means NOT DRAWN,
#                        not "rotated": both pond loops (mahdisp.c:2218 and
#                        :2343) `if ((t & 0x80) == 0)` around the whole draw AND
#                        around the position advance, so a flagged tile is
#                        skipped and the pond closes up over it. That is exactly
#                        right for a CLAIMED tile (a call takes it out of the
#                        pond) and exactly wrong for a riichi tile -- rotation
#                        lives at +0x136. MEASURED 2026-09-04.
#   +0xe6  stride 0x10   the four MELD blocks, 4x4 (meld unpack, 7-bit id)
#   +0x126 stride 4      the four MELD LAYOUT bytes (see `meld_types`)
#   +0x136 stride 1      PER SEAT: the RIICHI POND INDEX, ONE-BASED (0 = this
#                        seat has not declared). -> DAT_00451f90[seat], and the
#                        pond renderer (mahdisp.c:2350, and the position builder
#                        at :2220) takes its SIDEWAYS branch for the pond tile
#                        where `index+1 >= value`, drawing it at orientation
#                        seat+1 and shifting the rest of the row. MEASURED
#                        2026-09-04. This -- not a flag on the tile -- is how a
#                        declared riichi keeps its rotated tile across a resync.
#   +0x13a..+0x13f   the round state, BIT-PACKED (where HAIPAI/YAKUDISP/SEISAN
#                    spend a whole byte each), console.c:3899-3935:
#       +0x13a  four 2-bit fields, one per seat  -> DAT_003865bc/f0/624/658
#       +0x13b  HONBA -> 0x452028
#       +0x13c  THE RIICHI-STICK POT -> DAT_00452020 "ReachBou": this is the
#               ONLY message that writes it (HAIPAI has no field; YAKUDISP and
#               SEISAN zero it), so a resync that sends 0 blanks the pot.
#       +0x13d  bits 0-1 CURRENT seat -> 0x451fa0, 2-3 DEALER -> 0x451fb8,
#               4-5 KYOKU -> 0x451fb0, 6-7 CHIICHA -> 0x451fc0
#       +0x13e  bit 0 ROUND WIND -> 0x451fa8, bits 1-7 the WALL COUNT
#       +0x13f  the two DICE: low nibble -> 0x452040, high -> 0x452041 (the
#               handler swaps them once for the Kyoku label call and back)
#
# Every block abuts the next exactly (five closures, no padding) -- that is the
# check the interior was read right. **Two encodings of the same state, so a
# server has to implement both**, and this is the one message the client's dedup
# EXEMPTS, so it is always applied even on a repeated sequence byte.
# === THE CALL MOTIONS RIDE ON *THIS* MESSAGE ================================
#
# WARNING: CORRECTED 2026-09-04. `jan-motion-table.md` attributed the motion-4..8
# dispatch to "the MjNAKI parser, console.c:4073". Line 4073 is inside
# `console__MjClient_MjALLDATA_00286af0` (3759..4130) -- the MjALLDATA handler.
# The real MjNAKI handler is `console__MjClient_Naki_002860b0`, 728 bytes, and
# it NEVER calls `yamaguchi__002e4660`: it paints the call menu and encodes the
# reply, nothing else. (The dispatcher's own opcode gate does list '&' = 0x26,
# which is what made the misreading plausible -- but no MjNAKI code path
# reaches it.) **An MjNAKI with motion 4 in +0x18 animates nothing at all.**
#
# So a granted pon/chi/kan is an MjALLDATA whose +0x18 is the motion. Fields,
# read out of the handlers themselves (yamaguchi__002e7e70 / 002e84f0 /
# 002e8ae0 / 002e9a40 / 002e93f0):
#
#   motion 4 PON     +0x1a discarder  +0x1b,+0x1c tiles  +0x1e,+0x1f their
#                    slots  +0x22 caller
#   motion 5 CHI     identical fields; the claimed tile is forced leftmost
#   motion 6 MINKAN  +0x1a discarder  +0x1b tile  +0x1d,+0x1e,+0x1f slots
#                    +0x22 caller
#   motion 7 ANKAN   +0x1b tile  +0x1d first slot (it uses +0x1d, +1 and +2)
#                    +0x1e the fourth slot  +0x22 seat
#   motion 8 KAKAN   +0x1b tile  +0x1d MELD INDEX (<4)  +0x1e hand slot
#                    +0x22 seat
#   motion 10 REVEAL +0x22 a seat BITMASK: every masked seat's 14 hand slots
#                    fly in from +0x52 (the deal, the win reveal) and the
#                    handler fires banner 6 -- the 流局 RYUKYOKU cut-in -- and
#                    clears the masked seats' riichi-stick display
#                    (console.c:4085-4099). An EXHAUSTIVE DRAW is one of these
#                    with the tenpai seats in the mask and their real tiles in
#                    the hand block; an abortive draw sends mask 0 (the banner
#                    still fires; nothing flies). `jangame.msg_draw_reveal()`.
#
#   The three KAN motions (6/7/8) chain the kan-dora flip through
#   yamaguchi__002e8990 (gated on the DORA rule enum, HAIPAI +0x2d in {2,3,4}),
#   which scans +0x44 slots 12, 10, 8, 6 for the first with the REVEALED bit
#   set (see the WAMPAI banner: 0x40 in byte form) -- the newest indicator --
#   clears it for the beat, flips it (motion 11) and the landing sets it again.
#   So a kan record ships the dead wall with the new indicator ALREADY revealed
#   at its slot (4 + 2k) and the drawn rinshan tile still PRESENT at its slot.
#
#   KEY: THE RINSHAN DRAW RIDES ON THE SAME RECORD (console.c:4101-4118, read
#   2026-09-04): after the kan motion drains, the MjALLDATA handler tests
#   `rec[0x23 + seat] == 0` and, for motions 6/7/8, itself calls
#   yamaguchi__002e9940 (re-fills the dead wall: a placeholder id 1 at the slot
#   before the first occupied one, floor 0) and then yamaguchi__002e2660(
#   rec[+0x1c], rec[+0x22]), which SYNTHESISES a motion-9 record -- opcode
#   0x2e, +0x18 = 9, +0x1b = the byte it was given, +0x22 = the seat -- and
#   dispatches it: the rinshan tile flies from the first occupied wall slot to
#   hand slot 13 and the slot is zeroed. So on a kan record +0x1c is the
#   RINSHAN TILE (byte form), and the MjTSUMO that follows must carry motion 0
#   -- a motion 9 there would fly a SECOND tile out of the dead wall. With
#   +0x23+seat = 1 the handler skips the rinshan flight (the DEFERRED form: a
#   kakan holding for the chankan window) and MjTSUMO motion 9 draws it later
#   (its parser waits for pending dora flips to land first, console.c:3360).
#   `jangame.msg_kan()` builds both forms.
#
# WHICH STATE THE RECORD MUST CARRY -- this is the part that is not guessable,
# because the motion reads three different arrays and mutates two of them:
#
#   HANDS   PRE-call. The handler ORs 0x80 (= hide this slot) into
#           DAT_00451920[seat][slot] for each slot it names, so those slots
#           must still hold the claimed tiles.
#   PONDS   PRE-call. It zeroes the discarder's pond TAIL through
#           mahdisp__002bfa80 (which returns the last non-empty entry), so the
#           claimed tile must still be there and must NOT already carry the
#           claimed flag.
#   MELDS   POST-call. Its free-slot scan walks DAT_00451990 upward while the
#           meld is non-empty and then steps BACK one -- it lands on the LAST
#           OCCUPIED meld, i.e. the one just formed. It also only sets the
#           per-tile VISIBILITY words at DAT_00452410; the tile ids drawn come
#           from the +0xe6 block. A pre-call meld array puts the animation on
#           the previous meld and lands three tiles onto nothing.
#
# One record, three tenses. `jangame.msg_call()` builds it.
#
# WARNING: AND DO NOT SEND A RESYNC AFTER IT. The motion has already hidden the hand
# slots and popped the pond tail; a plain MjALLDATA behind it re-adds the tile
# to the pond and un-hides the slots. The caller's own MjTSUMO is fine and
# necessary -- MjClient_Tsumo dispatches its motion BEFORE it rewrites
# DAT_00451920 from +0x2a, so the repacked hand lands after the animation.
ALLDATA_LEN = 0x140
ALLDATA_HAND, ALLDATA_HAND_STRIDE = 0x52, 0x0E
ALLDATA_POND, ALLDATA_POND_STRIDE = 0x8A, 0x17
ALLDATA_MELD, ALLDATA_MELD_STRIDE = 0xE6, 0x10


def alldata(seq, hands, ponds, melds=None, scores=(0, 0, 0, 0), subtype=0,
            position=(), current_seat=0, wall_count=0, f13a=0, f13b=0, f13c=0,
            f13d_hi=(0, 0, 0), f13e_bit0=0, f13f=(0, 0), meld_types=None,
            tail136=(), event_tile=0, event_seat=0, event_present=0,
            call_from=0, call_tiles=(), call_slots=(), dealer=None, kyoku=None,
            chiicha=None, round_wind=None, honba=None, riichi_sticks=None,
            dice=None, seat_mask=None, seat_flags=(0, 0, 0, 0),
            is_human=(0, 0, 0, 0)):
    """MjALLDATA: the whole table, for a resync or a state event.

    `seat_flags` is the +0x23..+0x26 per-seat block (banner): 0 = animate
    this seat normally; 1 on the caller's seat of a kan record HOLDS the
    rinshan flight the handler would otherwise play from +0x1c. 2026-09-09:
    the same bytes are the client's IsHuman flags (`_put_is_human`), so the
    block on the wire is `is_human[s] | seat_flags[s]` -- a HUMAN seat is
    always 1 here, i.e. its own kan record never plays that flight (retail
    form; the rinshan draw for a human then has to come as a draw record).

    The round state is best given BY NAME: `dealer`, `kyoku`, `chiicha`,
    `round_wind`, `honba`, `riichi_sticks`, `dice=(d1, d2)` -- the same values
    MjHAIPAI carries at +0xd0..+0xd6 plus the stick pot. The raw forms
    (`f13b`, `f13c`, `f13d_hi` = (bits2-3, bits4-5, bits6-7), `f13e_bit0`,
    `f13f`) still work and are overridden by any named value.
    `seat_mask` writes +0x22 directly: for motion 10 it is the seats whose
    hands fly in / are revealed (the exhaustive-draw banner sends the TENPAI
    seats; the handler fires banner 6 even for an empty mask).
    `meld_types` is {seat: 4 layout bytes} for +0x126 -- the chi/pon selector
    that decides how each meld is drawn (see `meld_types()`); without it a chi
    draws with the pon layout (two tiles rotated).

    `call_from` (+0x1a), `call_tiles` (+0x1b then +0x1c) and `call_slots`
    (+0x1d, +0x1e, +0x1f IN ORDER) are the CALL MOTION fields -- see the banner.
    They are written last and OVERWRITE the `event_*` bytes they share, so a
    record is a subtype-3 draw+prep or a call, never both. Motions 4/5 read only
    the last two slots, so pass `(0, slot_a, slot_b)`; motion 6 reads all three;
    motions 7/8 read +0x1d and +0x1e.

    `subtype` 3 with `event_present` makes this an OPPONENT DRAW+PREP event:
    console__MjClient_MjALLDATA (via yamaguchi__002e1ca0) pops `event_tile`
    onto `event_seat`'s hand AND clears the settled-draw flag of that seat's
    LAST pond tile -- the local discard's populate-then-animate order -- so a
    motion-3 record sent right after flies the discard with NO settled
    pre-draw (no slingshot). The pond passed here must already END with the
    discard for that seat. See jangame.DISCARD_ANIM.
    """
    body = bytearray(ALLDATA_LEN - 0x18)
    _put_is_human(body, is_human)
    body[0] = subtype & 0xFF
    body[0x1C - 0x18] = event_tile & 0xFF   # already byte form (tile_byte)
    body[0x1E - 0x18] = event_present & 0xFF
    body[0x22 - 0x18] = event_seat & 0xFF
    for i, v in enumerate(list(seat_flags)[:4]):
        body[0x23 - 0x18 + i] |= v & 0xFF          # on top of IsHuman (banner)
    for i in range(4):
        struct.pack_into("<i", body, 0x28 - 0x18 + 4 * i, int(scores[i]))
    for i, v in enumerate(list(position)[:14]):
        body[0x44 - 0x18 + i] = v & 0xFF
    for s in range(4):
        base = ALLDATA_HAND - 0x18 + s * ALLDATA_HAND_STRIDE
        row = list(hands[s]) if s < len(hands) else []
        # WARNING: NEVER conceal a hand here by sending the BACK id 0x3F (WIRE_BACK).
        # The MjALLDATA hand byte has no face-down bit, and the back (sprite
        # 35) has NO 3D hand-tile model -- the client's tile-model draw
        # (malloc__002c5720 -> valloc__002d1a80) walks a NULL geometry pointer
        # and the EE spins forever on a TLB-miss flood that locks the whole
        # host. 561a4fb1 did exactly this: every game froze at the deal and the
        # host machine hung (2026-09-03, pc=0x2d1ae8).
        # Concealment in an MjALLDATA hand is IMPOSSIBLE (the handler rebuilds
        # all four hands from these bytes every time); send the real tiles.
        # Opponent concealment lives in the deal (MjHAIPAI's face-down bit) and,
        # properly, in NOT resyncing hands per turn -- see the motion table.
        for i in range(14):
            body[base + i] = tile_byte(row[i]) if i < len(row) else 0
        base = ALLDATA_POND - 0x18 + s * ALLDATA_POND_STRIDE
        prow = list(ponds[s]) if s < len(ponds) else []
        for i in range(23):
            if i < len(prow):
                t = prow[i]
                if isinstance(t, tuple):
                    body[base + i] = tile_byte(t[0], pond_flag=t[1])
                else:
                    body[base + i] = tile_byte(t)
            else:
                body[base + i] = 0
        base = ALLDATA_MELD - 0x18 + s * ALLDATA_MELD_STRIDE
        mrow = list(melds[s]) if melds and s < len(melds) else []
        for i in range(16):
            body[base + i] = mrow[i] if i < len(mrow) else 0
        # +0x126: this seat's 4 meld-layout bytes (chi=1, pon/kan=0).
        trow = list((meld_types or {}).get(s, b"")) if isinstance(meld_types, dict) \
            else (list(meld_types[s]) if meld_types and s < len(meld_types) else [])
        for i in range(4):
            body[0x126 - 0x18 + s * 4 + i] = (trow[i] & 0xFF) if i < len(trow) else 0
    for i, v in enumerate(list(tail136)[:4]):
        body[0x136 - 0x18 + i] = v & 0xFF
    # The call-motion prefix goes LAST: +0x1c and +0x1e are the same bytes the
    # subtype-3 event uses, and a call record must win.
    if call_from or call_tiles or call_slots:
        body[0x1A - 0x18] = call_from & 0xFF
        for i, v in enumerate(list(call_tiles)[:2]):
            body[0x1B - 0x18 + i] = v & 0xFF
        for i, v in enumerate(list(call_slots)[:3]):
            body[0x1D - 0x18 + i] = v & 0xFF
    if seat_mask is not None:
        body[0x22 - 0x18] = seat_mask & 0xFF
    hi = list(f13d_hi) + [0, 0, 0]
    if dealer is not None:
        hi[0] = dealer
    if kyoku is not None:
        hi[1] = kyoku
    if chiicha is not None:
        hi[2] = chiicha
    if honba is not None:
        f13b = honba
    if riichi_sticks is not None:
        f13c = riichi_sticks
    if round_wind is not None:
        f13e_bit0 = round_wind
    if dice is not None:
        f13f = dice
    body[0x13A - 0x18] = f13a & 0xFF
    body[0x13B - 0x18] = f13b & 0xFF
    body[0x13C - 0x18] = f13c & 0xFF
    body[0x13D - 0x18] = ((current_seat & 3) | ((hi[0] & 3) << 2)
                          | ((hi[1] & 3) << 4) | ((hi[2] & 3) << 6))
    body[0x13E - 0x18] = (f13e_bit0 & 1) | ((wall_count & 0x7F) << 1)
    body[0x13F - 0x18] = (f13f[0] & 0x0F) | ((f13f[1] & 0x0F) << 4)
    return _msg(MjALLDATA, seq, body, length=ALLDATA_LEN)


# --- how a meld is drawn (MEASURED 2026-09-03 from a live chi) ---------------
#
# The meld tiles live in MjALLDATA's +0xe6 block (16 bytes/seat = 4 melds x 4
# tiles, `meld_block`); a SECOND per-meld field at +0x126 (4 bytes/seat, one
# byte per meld, `meld_types`) picks the LAYOUT. The draw is
# mahdisp__002bc3e0's meld loop (mahdisp.c:2022):
#
#   type byte (+0x126)   0 -> ANKAN layout: [up, FACE-DOWN, FACE-DOWN, up]
#                        1 or 4 -> the open layout: drawn upright, only the tile
#                             marked 0x40 drawn rotated (sideways)
#   tile bit 6 (0x40)    marks the CALLED tile -- the one drawn rotated to show
#                        it was claimed. The unpack keeps bit 6 as part of the
#                        7-bit id, so it survives to u16 bit 6; the draw tests
#                        `& 0xc0` to find it and masks it off before drawing.
#
# WARNING: THE 09-03 BUG: we sent the type field all zeros, so a CHI drew with type 0
# ("two tiles turned face down", seen live). Fixed by sending type 1 for chi.
#
# WARNING: THE 09-04 BUG, THE SAME ONE HALF-FIXED: type 0 was named "PON/KAN" and left
# as pon's value, so every PON still drew face-down -- the live report was the
# identical symptom on pon a day later. **Type 0 is the ANKAN layout**, and the
# decompile is explicit: only that branch passes 1 in `gs__002a6430`'s param_4,
# and only for tiles [1] and [2]. param_4 indexes a rotation table and yields
# n*90 degrees about a DIFFERENT axis than the sideways claimed-tile marker
# (which comes from param_3), i.e. the tile is tipped onto its face. That is
# exactly how a concealed kan is drawn, so type 0 is RIGHT for ankan and wrong
# for everything else.
#
# Both consumers (`mahdisp.c:2035` draw, `yamaguchi.c:9720` tile position) test
# `(t == 4) || (t == 1)` and treat them identically, so 4 is safe for pon; SE
# keeps them distinct, so we do too.
MELD_TYPE_ANKAN = 0             # [up, face-down, face-down, up] -- concealed kan
MELD_TYPE_CHI = 1               # run, only the 0x40-marked tile rotated
MELD_TYPE_PON = 4               # pon / minkan / kakan, same draw branch as chi


def meld_block(melds):
    """A seat's melds as the 16 bytes MjALLDATA's +0xe6 block wants.

    Four melds of four tiles (meld unpack: 7-bit id, so bit 6 is part of the
    id). Tiles go in RANK order, and the CALLED tile carries bit 6 (POND_FLAG)
    so the client draws it rotated at its rank position -- see the banner above.
    """
    out = bytearray(16)
    for i, m in enumerate(melds[:4]):
        tiles = list(m.tiles) if hasattr(m, "tiles") else list(m)
        called = getattr(m, "called", None)
        # None can appear in a malformed/padded meld -- sort it last and never
        # mark it (the old encoder tolerated it as tile_byte(None) == 0).
        tiles = sorted(tiles, key=lambda t: mj.kind(t) if t is not None else 0x7F)
        marked = False
        for j, t in enumerate(tiles[:4]):
            b = tile_byte(t)
            if (not marked and t is not None and called is not None
                    and mj.kind(t) == mj.kind(called)):
                b |= POND_FLAG
                marked = True
            out[i * 4 + j] = b
    return bytes(out)


def meld_types(melds):
    """A seat's 4 meld-layout bytes for MjALLDATA's +0x126 field.

    Chi gets 1, a CONCEALED kan gets 0 (the face-down layout that 0 actually
    is), and everything open -- pon, minkan, kakan -- gets 4. An empty slot is 0
    and the client skips it because its tile block is zero, which is why 0 could
    masquerade as a safe default for so long.
    """
    out = bytearray(4)
    for i, m in enumerate(melds[:4]):
        kind_ = getattr(m, "kind", None)
        if kind_ == mj.CHI:
            out[i] = MELD_TYPE_CHI
        elif kind_ == mj.ANKAN:
            out[i] = MELD_TYPE_ANKAN
        else:
            out[i] = MELD_TYPE_PON
    return bytes(out)


# --- MjGAMERESULTHALF1 0x38 / HALF2 0x3a ------------------------------------
#
# HALF1 -- console__00287950 acks 0x39 at once and hands the message to
# yamaguchi2__00334be0, which runs FIVE readers in this order, every one of
# them PER SEAT (4 x u32 columns, seat = column), each animating a counter
# from a "before" column to an "after" column over 90 frames and then snapping
# to the "after" value, so the after column is what stays on screen. Labels
# from the translated build (2026-09-04):
#
#   yamaguchi2__00331f40  "Penalties for disconnects in this game now apply."
#       +0x58 + seat*4  before   +0x68 + seat*4  after   -- skipped when every
#       delta is zero
#   yamaguchi2__00332610  "This game ended with the results shown above."
#       +0xe8 + seat    PLACE, a BYTE, zero-based (drawn as byte + 1)
#                       "Converting scores to +/- points."
#       +0x78 + seat*4  the +/- points (displayed as-is)
#                       "Settling the placement points."  -- only if HAIPAI
#       +0x3c/+0x3d (the uma) are non-zero; the amounts it LABELS come from
#       those two bytes by place (1st +a, 2nd +b, 3rd -b, 4th -a, x1000 -- the
#       LABEL only, see UNITS below) while the counter runs
#       +0x78 -> +0x88 + seat*4, one frame per unit of the largest delta
#   yamaguchi2__00333370  "Yakitori penalties."  -- only if HAIPAI +0x33
#       +0x88 -> +0x98 + seat*4
#   yamaguchi2__00333a90  "Sashiuma" -- only if the 6-byte block at +0xec
#       matches the both-bits pair mask table 0x3e6a10 = [3,6,12,9,10,5] in
#       any slot (yamaguchi__002d98a0), i.e. the MjSASHIUMARESULT block
#       +0x98 -> +0xa8 + seat*4
#   yamaguchi2__00334210  "This game's final result is confirmed as above." /
#       "Final points converted into JAN." / "This table's rate is 1P = %d JAN"
#       +0x18 + seat*8  MONEY before (s64, logged as Money[0])
#       +0x38 + seat*8  MONEY after  (s64, logged as Money[1])
#
# WARNING: UNITS: +0x58/+0x68 are POINTS (the score column, a 25000-start hanchan),
# but **+0x78..+0xab are P** -- the +/-45 result point, NOT the score. Read that
# off the ANIMATIONS, 2026-09-12: the penalty stage (00331f40) and the money
# stage (00334210) both step `delta / 0x5a` for 0x5a frames and so don't care
# about scale, but the uma, yakitori and sashiuma stages size themselves off the
# data --
#
#     n = max over seats of (after - before);  step = (after - before) / n
#     (floored, min 1);  then `n` FRAMES of `display += step`
#
# -- one frame per unit. At x1000 a 10-20 uma is a 20000-frame stage (5.5 min at
# full speed; measured live at OVER HALF AN HOUR under emulation) and the
# 30-90 option is 90000; in P they are 20 and 90 frames, the same ballpark as
# the 0x5a the other two stages hard-code. P is also the unit the last stage
# converts ("1P = %d JAN"), so +0xa8 must be the number that rate is quoted
# against. The client draws these columns as plain integers -- yamaguchi2__00324b30
# has no decimal place -- so they are whole P, rounded.
# +0x18/+0x38 are JAN, the lifetime money the record screen shows (janstats
# `money`).
#
# WARNING: The x1000 in the uma stage is only the LABEL's amount (SE states an uma as
# "±20000 points" in the message text, from HAIPAI +0x3c/+0x3d); the counter it
# runs beside is in P.
#
# WARNING: Until 2026-09-04 this was written PER RANK -- `(seat, score, place,
# result*10)` at +0x58 + rank*0x10 -- against readers that walk PER SEAT with
# stride 4, +0xe8 was never written (the client read whatever followed the
# record: HALF1_LEN was 0xE8) and +0x18/+0x38 stayed zero.
HALF1_LEN = 0xF8
HALF1_MONEY_BEFORE, HALF1_MONEY_AFTER = 0x18, 0x38          # s64, stride 8
HALF1_PENALTY_BEFORE, HALF1_PENALTY_AFTER = 0x58, 0x68      # u32, stride 4
HALF1_POINTS, HALF1_AFTER_UMA = 0x78, 0x88
HALF1_AFTER_YAKITORI, HALF1_FINAL = 0x98, 0xA8
HALF1_PLACE, HALF1_SASHIUMA = 0xE8, 0xEC


def gameresult_half1(seq, places=(0, 1, 2, 3), scores=(0, 0, 0, 0),
                     penalised=None, points=(0, 0, 0, 0),
                     after_uma=None, after_yakitori=None, final=None,
                     money_before=(0, 0, 0, 0), money_after=None,
                     sashiuma=b""):
    """MjGAMERESULTHALF1, per SEAT throughout (index = seat).

    `places` zero-based; `scores` the raw end-of-game scores (+0x58);
    `penalised` the scores after a disconnect penalty (+0x68, default =
    scores so the stage is skipped); `points` the +/- conversion (+0x78);
    `after_uma` (+0x88), `after_yakitori` (+0x98) and `final` (+0xa8) default
    to the previous stage; `money_before`/`money_after` in JAN (+0x18/+0x38,
    s64); `sashiuma` the 6-byte RESULT block (both bits per pair that is on).
    """
    body = bytearray(HALF1_LEN - 0x18)

    def col32(off, vals, default):
        vals = list(default) if vals is None else list(vals)
        for seat in range(4):
            v = int(vals[seat]) if seat < len(vals) else 0
            struct.pack_into("<i", body, off - 0x18 + seat * 4, v)
        return vals

    def col64(off, vals, default):
        vals = list(default) if vals is None else list(vals)
        for seat in range(4):
            v = int(vals[seat]) if seat < len(vals) else 0
            struct.pack_into("<q", body, off - 0x18 + seat * 8, v)
        return vals

    scores = col32(HALF1_PENALTY_BEFORE, scores, (0, 0, 0, 0))
    col32(HALF1_PENALTY_AFTER, penalised, scores)
    points = col32(HALF1_POINTS, points, (0, 0, 0, 0))
    after_uma = col32(HALF1_AFTER_UMA, after_uma, points)
    after_yakitori = col32(HALF1_AFTER_YAKITORI, after_yakitori, after_uma)
    col32(HALF1_FINAL, final, after_yakitori)
    money_before = col64(HALF1_MONEY_BEFORE, money_before, (0, 0, 0, 0))
    col64(HALF1_MONEY_AFTER, money_after, money_before)
    for seat in range(4):
        body[HALF1_PLACE - 0x18 + seat] = (int(places[seat]) & 0xFF
                                           if seat < len(places) else 0)
    blk = bytes(sashiuma)[:6]
    body[HALF1_SASHIUMA - 0x18:HALF1_SASHIUMA - 0x18 + len(blk)] = blk
    return _msg(MjGAMERESULTHALF1, seq, body, length=HALF1_LEN)


# HALF2 (yamaguchi2__00335050) has a format string: it logs
# `Ranking[0..4] = {%d,%d,%d,%d}` with 20 %d, and the loop below it
# (`p = msg + seat*4; for (5) { rank = p[0x18]; p += 0x10; }`) fixes the
# geometry exactly:
#
#   Ranking[row][seat] = u32 at +0x18 + row*0x10 + seat*4      80 bytes
#
# WARNING: THE RANK IS ZERO-BASED: `<99` prints `rank + 1`, and `<0` / `>=99` are
# sentinels. **Send 0 for first place.**

# WARNING: HALF2 IS 0xB0, NOT 0x68 - MEASURED 2026-08-24, and we were 72 bytes short.
#
# The old value stopped exactly where the `Ranking[5][4]` block ends, because
# that block was all anyone had read. `yamaguchi2__File_Read_Complite_00335050`
# keeps going, and every field past it was being rendered from whatever happened
# to sit past the end of our message:
#
#   iVar12 = param_2 * 4                 <- param_2 is MY SEAT
#   iVar4  = param_1 + iVar12
#   ...  *(int *)(iVar4 + 0x18)          <- Ranking[row][seat], row stride 0x10
#   iVar4 = param_2 + param_1            <- NOTE: seat stride 1 here, not 4
#   ...  *(char *)(iVar4 + 0x68 + t*4)   <- GetShogo[title][seat], BYTES
#   ...  *(char *)(iVar4 + 0x7c + t*4)   <- Shogo[title][seat],    BYTES
#   ...  *(int  *)(iVar4 + 0x90)         <- LevelUp[seat]   "Level up! Now %d."
#   ...  *(int  *)(iVar4 + 0xa0)         <- Level[seat]     "Your level is now %d."
#
# so the highest byte touched is +0xAF and the record is 0xB0 = 176.
#
# KEY: **THIS IS WHERE A PLAYER'S LEVEL COMES FROM.** It is not in the save --
# `U/g/MJSUserData` has no level field anywhere in the module. The SERVER states
# it, per game, in this message. Same for the five titles.
HALF2_LEN = 0xB0

#: `Ranking[row][seat]` = u32 at RANKING_OFF + row*0x10 + seat*4.
RANKING_OFF, RANKING_ROWS = 0x18, 5
#: `GetShogo[title][seat]` = BYTE at +0x68 + title*4 + seat. Non-zero = the seat
#: EARNED that title in this game ("You earned the next title."). The whole
#: block being zero skips that screen entirely.
GETSHOGO_OFF = 0x68
#: `Shogo[title][seat]` = BYTE at +0x7C + title*4 + seat -- titles HELD.
SHOGO_OFF = 0x7C
#: `LevelUp[seat]` = u32 at +0x90 + seat*4. **0 = no level up**, and the client
#: branches on it: zero draws only "Your level is now %d", non-zero also draws
#: "Level up! Now %d." with THIS value.
LEVELUP_OFF = 0x90
#: `Level[seat]` = u32 at +0xA0 + seat*4 -- the level to display.
LEVEL_OFF = 0xA0

#: The five titles, in the order the byte arrays index them. Read off the
#: "You earned the next title." block at 0x0041ea70..0x0041eaf0.
SHOGO_NAMES = ("Mahjong King", "King of Beasts", "Bust General",
               "Winnings Gen.", "Wild Tile King")

#: WARNING: A BLANK RANKING SLOT MUST BE NEGATIVE, NOT ZERO -- and that follows
#: directly from the ladder above. The values are read SIGNED
#: (`rank = *(int *)(p + 0x18)`) and `0` is FIRST PLACE, so padding an unknown
#: row with zeros tells the player they won a game that never happened. This is
#: what `jangame.msg_results2` sent until 2026-08-24: row 0 was the real result
#: and rows 1..4 were `[0,0,0,0]`, i.e. four straight wins for all four seats.
#: `& 0xFFFFFFFF` below carries it to the wire as 0xFFFFFFFF, which is the -1
#: the client reads back.
NO_RANK = -1


def gameresult_half2(seq, ranking, levels=None, levelups=None,
                     shogo=None, getshogo=None):
    """MjGAMERESULTHALF2. `ranking` is 5 rows of 4 ZERO-BASED places.

    A row or seat the caller does not supply is filled with `NO_RANK`, NOT with
    zero -- see the constant. A caller that means "first place" has to say 0.

    `levels` / `levelups` are per-seat lists of 4. `levelups` defaults to all
    zero, which is the "no level up" branch; a non-zero entry makes the client
    draw "Level up! Now %d." with that value.

    `shogo` / `getshogo` are 5 lists of 4 bytes -- [title][seat], titles in
    `SHOGO_NAMES` order. `getshogo` all-zero skips the "You earned the next
    title." screen, which is the right default: a title nobody has earned must
    not be announced.
    """
    body = bytearray(HALF2_LEN - 0x18)

    def put32(off, val):
        struct.pack_into("<I", body, off - 0x18, int(val) & 0xFFFFFFFF)

    for row in range(RANKING_ROWS):
        vals = list(ranking[row]) if row < len(ranking) else [NO_RANK] * 4
        for seat in range(4):
            put32(RANKING_OFF + row * 0x10 + seat * 4,
                  vals[seat] if seat < len(vals) else NO_RANK)
    for base, table in ((GETSHOGO_OFF, getshogo), (SHOGO_OFF, shogo)):
        for title in range(len(SHOGO_NAMES)):
            row = list(table[title]) if table and title < len(table) else []
            for seat in range(4):
                body[base - 0x18 + title * 4 + seat] = (
                    int(row[seat]) & 0xFF if seat < len(row) else 0)
    for base, vals in ((LEVELUP_OFF, levelups), (LEVEL_OFF, levels)):
        for seat in range(4):
            put32(base + seat * 4,
                  vals[seat] if vals and seat < len(vals) else 0)
    return _msg(MjGAMERESULTHALF2, seq, body, length=HALF2_LEN)


# --- MjMEMBERLEAVE 0x3c / MjMEMBERLEAVEANSER 0x3d ---------------------------
#
# WARNING: THIS IS NOT A BARE NOTIFICATION -- IT ASKS A QUESTION.
# `console__MjClient_MjMEMBERLEAVE_002877d0` is sixteen lines and does only two
# things:
#
#     if (DAT_003e5980 != my_seat && !spectating) {
#         answer = yamaguchi2__003366f0(widget);      /* a yes/no DIALOG */
#         objstrings__002cb420(&my_id, session, answer);
#     }
#
# and `yamaguchi2__003366f0` opens a dialog widget and SPINS until the player
# answers (`+0x604`) or the turn clock's skip flag fires -- in which case the
# answer is 1. So a seat dropping out puts a modal question in front of the
# other three, and the hand does not move until they answer.
#
# VERIFIED 2026-09-04 (console.c:4135-4145): the handler reads NO field of the
# message. It tests its own globals (`DAT_003e5980`, the leaving seat, set by
# the lobby-side member bookkeeping; `_DAT_0042abc0` my seat; `DAT_0042aba8`
# spectating) and calls `yamaguchi2__003366f0(dialog, *param_1)` -- the
# message pointer IS passed, but that function (yamaguchi2.c:10601) takes one
# parameter and never touches the second: it runs the modal and returns the
# answer. The record is a bare trigger for the dialog. The seat at +0x18 and
# the member id in the header are kept for our own trace/capture legibility;
# nothing in the client depends on them.
#
# THE ANSWER, on the other hand, is measured exactly -- `objstrings__002cb420`
# stores every field:
#
#     +0x08  u64  the session handle (_DAT_004467d0)
#     +0x10  u16  0x28 = 40 -- the whole record
#     +0x12  u8   0x3d
#     +0x13       NOT WRITTEN (left as stack garbage; do not read it)
#     +0x14  s8   -3    +0x15  s8  -2    +0x16  u8  1    +0x17  u8  0
#     +0x18  u64  the answering member's OWN id (DAT_004464e0)
#     +0x20  u32  THE ANSWER: 1 = continue, 0 = do not
#
# WARNING: Note `sub` is **0**, not the in-game 1: the answer goes out on the lobby
# drain even though the question arrived in-game.
MjMEMBERLEAVEANSER = 0x3D
MEMBERLEAVE_ANSER_LEN = 0x28


def memberleave(seq, seat=0, member_id=0):
    """MjMEMBERLEAVE: tell the table a seat has gone, and ask the rest to stay."""
    body = bytearray(0x20 - 0x18)
    body[0] = seat & 0xFF                    # unread by the client (banner)
    return _msg(MjMEMBERLEAVE, seq, body, length=0x20, id8=member_id)


def parse_memberleave_anser(rec):
    """MjMEMBERLEAVEANSER -> {'member_id', 'answer', 'continue'}.

    WARNING: Do NOT read `f13` off this one: the builder never writes +0x13, so it
    carries whatever was on the client's stack. It is the one message in the
    protocol with no valid sequence byte.
    """
    h = janwire.unpack(rec)
    answer = struct.unpack_from("<I", rec, 0x20)[0] if len(rec) >= 0x24 else 0
    return {"member_id": h["payload"], "answer": answer,
            "continue": bool(answer), "session": h["id8"]}


# --- MjSASHIUMA* 0x31..0x35 -- the finishing-rank side-bet (sashiuma) ------
#
# Entirely SERVER-initiated: the client never sends 0x32/0x34 unprompted, only
# as the reply to our 0x31/0x33 (console.c:2765-2783 and 2855-2872). The whole
# handshake is PRE-GAME: the in-game dispatcher (console.c:4356-4435) numbers
# its phases START=1, SELECT=2, RESULT=3, HAIPAI=4, and the handlers keep just
# one "bet active" flag per pair (yamaguchi__002e0f80 -> score object +0x124)
# that the table's GM_UmaLineLayer draws as LINES between the paired players
# (yamaguchi__002e0820). The client computes NO money: the payout is ours,
# folded into the MjGAMERESULT rows.
#
# ONE 6-BYTE BLOCK AT +0x18, one byte per UNORDERED PAIR of seats, each byte a
# BITFIELD where seat s owns bit (1 << s). Read out of the constant tables
# 0x3e5610 (my bit) / 0x3e55b0 (pair slot) / 0x3e55e0 (both-bits mask) in a
# live savestate, 2026-09-04:
#
#       slot 0 = (0,1)   slot 1 = (1,2)   slot 2 = (2,3)
#       slot 3 = (0,3)   slot 4 = (1,3)   slot 5 = (0,2)
#
#   0x31 START   (-> each human)  +0x18  4 x 16-byte cp932 names, seat order.
#                                 console__00284670 reads name[seat] at
#                                 +0x18 + seat*0x10 for the pick list, THEN
#                                 zeroes +0x18..+0x1d and writes the pick there.
#   0x32 REQUEST (<- human)       +0x18  6 bytes: MY bit in the ONE pair I
#                                 picked; all zero = no bet wanted.
#   0x33 SELECT  (-> each human)  +0x18  6 bytes: every seat's proposals merged.
#                                 yamaguchi__002e13d0 classifies each pair as
#                                 mutual / mine-pending / theirs-pending / none.
#   0x34 ARGEE   (<- human)       +0x18  6 bytes: the SELECT block with MY bit
#                                 OR-ed into each pair I agreed to
#                                 (yamaguchi__002e1600).
#   0x35 RESULT  (-> each human)  +0x18  6 bytes: BOTH bits set for every pair
#                                 that is ON, 0 otherwise -- yamaguchi__002e1700
#                                 tests `(byte & both) == both`. No reply.
#
# Bodies: START runs to +0x57 (we send 0x60); the rest are 0x20, the same size
# the client uses for its own replies (console.c:2773 / 2862).
#
# The retail tables are inconsistent for SEAT 2 (0x3e5620 orders its opponents
# (3,0,1) where 0x3e55b0/0x3e55e0 use (1,0,3)), so a HUMAN at seat 2 could
# mis-pick. Our human sits at seat 0 and a bot's side is computed here, so it
# never fires -- noted so nobody chases it as a server bug.

SASHIUMA_PAIRS = ((0, 1), (1, 2), (2, 3), (0, 3), (1, 3), (0, 2))
SASHIUMA_SLOT = {}
for _i, (_a, _b) in enumerate(SASHIUMA_PAIRS):
    SASHIUMA_SLOT[(_a, _b)] = _i
    SASHIUMA_SLOT[(_b, _a)] = _i
SASHIUMA_BLOCK = 6
SASHIUMA_NAME = 16
SASHIUMA_START_LEN = 0x60
SASHIUMA_LEN = 0x20


def sashiuma_slot(a, b):
    """The +0x18 byte index of the pair of seats (a, b), either order."""
    return SASHIUMA_SLOT[(a, b)]


def sashiuma_bit(seat):
    """The bit seat `seat` owns inside a pair byte."""
    return 1 << seat


def sashiuma_start(seq, names):
    """MjSASHIUMASTART: `names` = four seat names (cp932 bytes or str)."""
    body = bytearray(SASHIUMA_START_LEN - 0x18)
    for s in range(4):
        nm = names[s] if s < len(names) and names[s] else b""
        if not isinstance(nm, bytes):
            nm = str(nm).encode("cp932", "replace")
        nm = nm[:SASHIUMA_NAME]
        body[s * SASHIUMA_NAME:s * SASHIUMA_NAME + len(nm)] = nm
    return _msg(MjSASHIUMASTART, seq, body, length=SASHIUMA_START_LEN)


def _sashiuma_block_msg(opcode, seq, block):
    body = bytearray(SASHIUMA_LEN - 0x18)
    b = bytes(block)[:SASHIUMA_BLOCK]
    body[:len(b)] = b
    return _msg(opcode, seq, body, length=SASHIUMA_LEN)


def sashiuma_select(seq, block):
    """MjSASHIUMASELECT: the merged proposal block."""
    return _sashiuma_block_msg(MjSASHIUMASELECT, seq, block)


def sashiuma_result(seq, block):
    """MjSASHIUMARESULT: both bits set per pair that is ON, else 0."""
    return _sashiuma_block_msg(MjSASHIUMARESULT, seq, block)


def parse_sashiuma(rec):
    """MjSASHIUMAREQUEST / MjSASHIUMAARGEE -> {'seat', 'seq', 'block'}."""
    h = janwire.unpack(rec)
    block = bytes(rec[0x18:0x18 + SASHIUMA_BLOCK])
    block = block + bytes(SASHIUMA_BLOCK - len(block))
    return {"seat": h["src"], "seq": h["f13"], "block": block}


# --- acks the client sends (INBOUND) ----------------------------------------
#
# console__00284380(opcode, f16, sub) builds them: 0x18 bytes, header only,
# f13 = the adopted sequence, src = my seat (DAT_004460e8, which is
# U/g/MJSUserData byte +0x3ca -- wire identity traces back to the save file),
# dst = 4, sub = 3. `ack = opcode + 1` throughout, and the sequence being echoed
# is what lets a server match an ack to its message with no other state.
#
#   0x23  MjHAIPAIACK        0x29  MjYAKUDISPACK (mahdisp.c)
#   0x2b  MjSEISANACK        0x2d  MjBYE, for MjGAMEEND (f16 = 0)
#   0x39  HALF1ACK           0x3b  HALF2ACK
#   0x21  MjREADY -- sent TWICE at game entry, sub 3 AND sub 5 (lobby.c)
ACK_OF = {MjHAIPAI: MjHAIPAIACK, MjYAKUDISP: MjYAKUDISPACK,
          MjSEISAN: MjSEISANACK, MjGAMEEND: MjBYE, MjALLDATA: MjALLDATAACK,
          MjNAKI: MjNAKIACK,
          MjGAMERESULTHALF1: MjGAMERESULTHALF1ACK,
          MjGAMERESULTHALF2: MjGAMERESULTHALF2ACK}
IS_ACK = frozenset(ACK_OF.values()) | {MjREADY}


def is_ack_for(rec, opcode):
    h = janwire.unpack(rec)
    return h["opcode"] == ACK_OF.get(opcode)


# --- MjNOTICEGAMESTART 18 ---------------------------------------------------
#
# The notice dispatcher at 0x002c6384 maps opcode 18 to BIT 8 =
# TGM_NOTICE_GAME_START, and the notice drain reads sub-code 0 -- NOT the
# in-game 1, because this arrives while the client is still on the table screen.
# No body field has been read; sent bare until one is.
NOTICE_SUB = 0


def notice_gamestart(seq, body=b""):
    return _msg(MjNOTICEGAMESTART, seq, body,
                length=0x20 if not body else 0x18 + len(body),
                sub=NOTICE_SUB, dst=janwire.DST_CHANNEL & 0xFF)


# --- WARNING: MjNOTICEGAMESETUP 20 -- THE MESSAGE THAT ACTUALLY STARTS THE GAME ----
#
# Found 2026-08-17 after a live client accepted MjNOTICEGAMESTART and then sat
# on 「皆さんをお待ちしております」 for ever. Follow the chain and it is not a
# judgement call:
#
#   lmenu__002b2e00        state 5 -> 6 (enter the game) iff
#                          roommain__TableSelectMain_0033f010 returns 1
#   TableSelectMain        do { lmenu__Request_Game_Start_002b2430();
#                               if (DAT_004460f2) { log("GameStart!!!!!!!!!");
#                                                   return 1; } } while (...)
#   Request_Game_Start     bit 0x20 -> DAT_004460f2 = 1, logs "Request Game Start"
#   the notice dispatcher  opcode 0x14 (20, GAME_SETUP) -> bit 0x20
#
# > **`MjNOTICEGAMESTART` (18) is NOT the trigger.** Its bit is 8, its flag is
# > `DAT_004460f0`, and that global is WRITTEN AND NEVER READ anywhere in the
# > module. Sending it is inert -- which is exactly what a client accepting our
# > message and then waiting for ever looks like.
#
# The dispatcher's 0x14 arm reads no field of the record, so a bare 32-byte
# notice is enough. (For the record, the 0x13 arm -- MjNOTICETIMEUPWARNING --
# DOES read one: `_DAT_00446f98 = msg[0x18] & 0xff`, the "Rest Time = %d" the
# lobby prints. That is the lobby countdown, not the in-game turn clock.)
def notice_gamesetup(seq, body=b""):
    return _msg(MjNOTICEGAMESETUP, seq, body,
                length=0x20 if not body else 0x18 + len(body),
                sub=NOTICE_SUB, dst=janwire.DST_CHANNEL & 0xFF)


def notice_timeup(seq, rest_seconds=0):
    """MjNOTICETIMEUPWARNING: the lobby's countdown. +0x18 is `Rest Time`."""
    body = bytearray(8)
    body[0] = rest_seconds & 0xFF
    return _msg(MjNOTICETIMEUPWARNING, seq, body, length=0x20,
                sub=NOTICE_SUB, dst=janwire.DST_CHANNEL & 0xFF)


def readystatus(seq, ready):
    """MjREADYSTATUS: the four between-hands ready bytes at +0x18, on sub 2
    (the MjREADYSTATUS banner). Any non-zero byte is "ready"."""
    body = bytearray(8)
    for s, v in enumerate(list(ready)[:4]):
        body[s] = 1 if v else 0
    return _msg(MjREADYSTATUS, seq, body, length=0x20, sub=READY_SUB)


# --- selftest ----------------------------------------------------------------

def selftest():
    ok = True

    def check(cond, msg):
        # bool(), not cond: several tests hand in a masked int, and `True & 128`
        # is 0 -- which fails the run with nothing printed.
        if not cond:
            print("FAIL: %s" % msg)
        return bool(cond)

    # --- the tile codec, against the client's own three unpacks --------------
    def unpack_hand(b):
        return ((b & 0x80) << 8) | (b & 0x3F)

    def unpack_pond(b):
        return (b & 0x3F) | ((b & 0x80) << 8) | ((b & 0x40) << 1)

    def unpack_meld(b):
        return ((b & 0x80) << 8) | (b & 0x7F)

    b = tile_byte(mj.MAN + 4)
    ok &= check(unpack_hand(b) == tile_u16(mj.MAN + 4), "hand unpack round trip")
    b = tile_byte(mj.PIN, flag=True)
    ok &= check(unpack_hand(b) & 0x8000, "bit 7 survives as u16 bit 15")
    b = tile_byte(mj.SOU + 3, pond_flag=True)
    ok &= check(unpack_pond(b) & 0x80, "the pond flag lands at u16 bit 7")
    ok &= check(unpack_hand(b) == unpack_hand(tile_byte(mj.SOU + 3)),
                "the HAND unpack throws bit 6 away -- which is why a red five "
                "cannot be a flag")
    ok &= check(unpack_meld(tile_byte(mj.MAN | 0x00)) < 0x80, "meld id is 7 bits")
    red = tile_byte(mj.MAN + 4 | mj.RED)
    ok &= check(red == (0x05 | TILE_FLAG),
                "a red five is the plain five id + the red-sheet flag bit")
    ok &= check(tile_from_byte(red)[0] == (mj.MAN + 4) | mj.RED,
                "and decodes back to a red five")
    # The nibble numbering, spot-checked at every suit boundary (2026-09-02:
    # this is what the client's sprite map at 0x385430 is keyed by).
    ok &= check(tile_byte(mj.MAN) == 0x01 and tile_byte(mj.MAN + 8) == 0x09,
                "man 1/9 -> 0x01/0x09")
    ok &= check(tile_byte(mj.PIN) == 0x11 and tile_byte(mj.PIN + 8) == 0x19,
                "pin 1/9 -> 0x11/0x19")
    ok &= check(tile_byte(mj.SOU) == 0x21 and tile_byte(mj.SOU + 8) == 0x29,
                "sou 1/9 -> 0x21/0x29")
    ok &= check(tile_byte(27) == 0x31 and tile_byte(33) == 0x37,
                "east -> 0x31, chun -> 0x37")
    ok &= check(all(tile_from_byte(tile_byte(t))[0] == t for t in range(34)),
                "every kind round-trips")
    ok &= check(all(tile_byte(t) & 0x3F for t in range(34)),
                "NO tile may encode to 0 -- the client vacates a hand slot with "
                "0 and finds the end of a pond by scanning for it")
    ok &= check(all(tile_u16(t) for t in range(34)) and tile_u16(0) != 0,
                "and the same in the u16 form the hand arrays use")

    # --- headers ------------------------------------------------------------
    for name_, rec in (("haipai", haipai(1, [[0] * 13] * 4)),
                       ("tsumo", tsumo(2, 0, [0] * 14, 70)),
                       ("naki", naki(3, 69)),
                       ("yakudisp", yakudisp(4)),
                       ("seisan", seisan(5, [25000] * 4)),
                       ("alldata", alldata(6, [[]] * 4, [[]] * 4)),
                       ("half1", gameresult_half1(7)),
                       ("half2", gameresult_half2(8, [[0, 1, 2, 3]] * 5)),
                       ("gameend", gameend(9))):
        h = janwire.unpack(rec)
        ok &= check(h["sub"] == INGAME_SUB,
                    "%s sub=%d, but the in-game loop drains on %d -- it would "
                    "never see this" % (name_, h["sub"], INGAME_SUB))
        ok &= check(h["length"] == len(rec),
                    "%s declares %d but is %d bytes" % (name_, h["length"], len(rec)))
        ok &= check(h["f16"] == F16_CONST, "%s +0x16" % name_)

    ok &= check(janwire.unpack(notice_gamestart(1))["sub"] == NOTICE_SUB,
                "a notice drains on sub 0, not the in-game 1")

    # --- MjGAMERESULTHALF2: the WHOLE record, not just the ranking block -----
    # Measured off yamaguchi2__File_Read_Complite_00335050. The old builder
    # stopped at 0x68 -- where Ranking ends -- so the client rendered titles and
    # levels from 72 bytes of whatever followed our message in its buffer.
    rec = gameresult_half2(
        0x20, [[0, 1, 2, 3]] + [[NO_RANK] * 4] * 4,
        levels=[7, 8, 9, 10], levelups=[0, 2, 0, 0],
        shogo=[[1, 0, 0, 0]] + [[0] * 4] * 4,
        getshogo=[[0] * 4, [0, 0, 3, 0]] + [[0] * 4] * 3)
    ok &= check(len(rec) == HALF2_LEN == 0xB0,
                "half2 is 0xB0, not the old 0x68: %#x" % len(rec))
    ok &= check(struct.unpack_from("<4i", rec, RANKING_OFF) == (0, 1, 2, 3),
                "row 0 is this game's placing")
    ok &= check(struct.unpack_from("<i", rec, RANKING_OFF + 0x10)[0] == -1,
                "a blank history row is SIGNED -1, never 0 (0 = first place)")
    for seat, want in enumerate((7, 8, 9, 10)):
        ok &= check(struct.unpack_from("<I", rec, LEVEL_OFF + seat * 4)[0] == want,
                    "Level[%d] at +%#x" % (seat, LEVEL_OFF + seat * 4))
    ok &= check(struct.unpack_from("<I", rec, LEVELUP_OFF + 4)[0] == 2,
                "LevelUp[1] = 2 makes the client draw 'Level up! Now 2.'")
    ok &= check(struct.unpack_from("<I", rec, LEVELUP_OFF)[0] == 0,
                "LevelUp[0] = 0 takes the no-level-up branch")
    # BYTE arrays: title stride 4, seat stride 1 -- the one place the two
    # blocks differ from everything else in this protocol.
    ok &= check(rec[SHOGO_OFF + 0 * 4 + 0] == 1,
                "Shogo[title 0][seat 0] is a BYTE at +0x7C")
    ok &= check(rec[GETSHOGO_OFF + 1 * 4 + 2] == 3,
                "GetShogo[title 1][seat 2] is a BYTE at +0x68 + 4 + 2")
    bare = gameresult_half2(0x21, [[0] * 4])
    ok &= check(len(bare) == HALF2_LEN, "a bare half2 is still full length")
    ok &= check(not any(bare[GETSHOGO_OFF:GETSHOGO_OFF + 20]),
                "and announces NO title -- a non-zero GetShogo would make the "
                "client claim the player earned one")
    ok &= check(all(struct.unpack_from("<i", bare, RANKING_OFF + r * 0x10 + s * 4)[0]
                    == NO_RANK for r in range(1, 5) for s in range(4)),
                "unsupplied history rows are NO_RANK across the block")

    # --- MjHAIPAI: every field where the client loads it ---------------------
    hands = [[mj.MAN + i for i in range(13)],
             [mj.PIN + i for i in range(13)],
             [mj.SOU + i for i in range(13)],
             [27 + (i % 7) for i in range(13)]]
    rec = haipai(0x11, hands, position=list(range(14)), motion=3, seat_mask=0x0F,
                 u28=0xDEADBEEF, scalars18=list(range(18)),
                 three_a=b"abc", three_b=b"xyz",
                 chiicha=1, dealer=2, round_wind=1, kyoku=3, honba=4, dice=(5, 6))
    ok &= check(len(rec) == HAIPAI_LEN, "haipai is 0xd7, got %#x" % len(rec))
    ok &= check(rec[0x18] == 3, "MotionCommand at +0x18")
    ok &= check(rec[0x22] == 0x0F, "deal-animation seat bitmask at +0x22")
    ok &= check(struct.unpack_from("<I", rec, 0x28)[0] == 0xDEADBEEF, "+0x28 u32")
    ok &= check(list(rec[0x2C:0x3E]) == list(range(18)), "18 scalars at +0x2c")
    ok &= check(rec[0x3E:0x41] == b"abc" and rec[0x41:0x44] == b"xyz",
                "the two 3-byte blocks")
    ok &= check(struct.unpack_from("<14H", rec, 0x44) == tuple(range(14)),
                "the 14-slot position array at +0x44")
    for s in range(4):
        base = HAIPAI_HAND + s * HAIPAI_SEAT_STRIDE
        got = struct.unpack_from("<14H", rec, base)
        want = tuple(hand_u16s(hands[s]))
        ok &= check(got == want, "seat %d hand at +%#x" % (s, base))
    ok &= check(HAIPAI_HAND + 4 * HAIPAI_SEAT_STRIDE == 0xD0,
                "0x60 + 4*0x1c must be exactly 0xd0 -- the bounds check")
    ok &= check(tuple(rec[0xD0:0xD7]) == (1, 2, 1, 3, 4, 5, 6), "the seven scalars")
    # Each trailing scalar at the offset the parser loads it from (console.c
    # 3201-3207): +0xd0 chiicha, +0xd1 dealer, +0xd2 round wind, +0xd3 kyoku,
    # +0xd4 honba, +0xd5/+0xd6 the dice.
    rec = haipai(0x11, hands, chiicha=3, dealer=2, round_wind=1, kyoku=0, honba=7,
                 dice=(4, 1))
    ok &= check(rec[0xD0] == 3 and rec[0xD1] == 2, "+0xd0 chiicha, +0xd1 dealer")
    ok &= check(rec[0xD2] == 1 and rec[0xD3] == 0,
                "+0xd2 is the ROUND WIND (console.c:3205), +0xd3 the kyoku")
    ok &= check(rec[0xD4] == 7, "+0xd4 honba")
    ok &= check((rec[0xD5], rec[0xD6]) == (4, 1),
                "+0xd5/+0xd6 are the DICE, not the riichi sticks")
    # The rule vector: every read site's index (banner), by name.
    rv = rule_scalars(dora_kind=4, aka_rank=5, wareme=True, riichi_1000=True,
                      yakitori=True, uma=(20, 10))
    rec = haipai(0x11, hands, scalars18=rv, three_a=bytes([1, 1, 1]))
    ok &= check(rec[0x2D] == 4, "+0x2d DORA enum (DAT_00452185) = 4 'All'")
    ok &= check(rec[0x2E] == 4, "+0x2e red rank is 0-BASED: fives -> 4 "
                "(mahdisp.c:2863 table [1..6][value])")
    ok &= check(rec[0x30] == 1 and rec[0x31] == 1 and rec[0x33] == 1,
                "+0x30 wareme, +0x31 riichi-needs-1000, +0x33 yakitori")
    ok &= check((rec[0x3C], rec[0x3D]) == (20, 10),
                "+0x3c/+0x3d uma 1st/2nd in thousands (yamaguchi2.c:8832-8841)")
    ok &= check(rec[0x3E:0x41] == bytes([1, 1, 1]), "+0x3e.. per-suit red counts")
    ok &= check(DORA_KIND[(True, True, True)] == 4 and DORA_KIND[(False, False, False)] == 0
                and DORA_KIND[(True, False, False)] == 1
                and DORA_KIND[(False, True, False)] == 2
                and DORA_KIND[(True, True, False)] == 3,
                "the DORA enum matches the client's label order")

    # Concealed seats: every occupied tile gets bit 7 set, the local seat none.
    cc = haipai(0x13, hands, motion=10, seat_mask=0x0F, concealed={1, 2, 3})
    seat0 = struct.unpack_from("<14H", cc, HAIPAI_HAND)
    ok &= check(all((v & FACE_DOWN) == 0 for v in seat0 if v),
                "the local seat (0) stays face-up")
    for s in (1, 2, 3):
        row = struct.unpack_from("<14H", cc, HAIPAI_HAND + s * HAIPAI_SEAT_STRIDE)
        ok &= check(all((v & FACE_DOWN) for v in row if v),
                    "concealed seat %d is face-down (bit 7)" % s)
        ok &= check(all((v & 0x3F) == (w & 0x3F)
                        for v, w in zip(row, hand_u16s(hands[s]))),
                    "concealed seat %d keeps its tile ids" % s)

    # --- the dead wall helpers ----------------------------------------------
    # 14 distinct plain tiles: kinds 0..13 -> wire ids (no red fives, so the
    # u16s carry no sheet bit and the flags are purely ours). The layout and
    # the flag are the WAMPAI banner's: dora-1 at slot 4 (the client's own
    # post-deal flip target), kan-dora k at 4+2k (the chain scans 12,10,8,6
    # for the NEWEST revealed one), rinshan at 0..3, and 0x80 = REVEALED.
    dead = list(range(14))
    w = wanpai_u16s(dead, dora_shown=1)
    ok &= check(len(w) == 14, "wanpai_u16s is 14 wide")
    ok &= check([v & 0x3F for v in w[:4]] == [tile_byte(t) for t in dead[:4]],
                "rinshan tiles dead[0..3] land in slots 0..3")
    ok &= check((w[4] & 0x3F, w[5] & 0x3F) == (tile_byte(dead[4]), tile_byte(dead[9])),
                "dora indicator 0 (dead[4]) at slot 4 -- the slot the HAIPAI "
                "parser clears and auto-flips -- its ura (dead[9]) at 5")
    ok &= check((w[12] & 0x3F, w[13] & 0x3F) == (tile_byte(dead[8]), tile_byte(dead[13])),
                "kan-dora 4 (dead[8]) at slot 12 with its ura at 13")
    ok &= check(w[WANPAI_FLIP_SLOT] & WANPAI_REVEALED,
                "the shown indicator carries 0x80 = REVEALED (002ef1c0 sets "
                "exactly this bit when the flip lands)")
    ok &= check(all((v & WANPAI_REVEALED) == 0 for i, v in enumerate(w) if i != 4),
                "every other slot has 0x80 CLEAR = face-down")
    # After two kans the engine shows three indicators: slots 4, 6, 8 revealed
    # and the chain's 12->6 scan finds slot 8 first -- the newest.
    w2 = wanpai_u16s(dead, dora_shown=3, kans=2)
    revealed = [i for i, v in enumerate(w2) if v & WANPAI_REVEALED]
    ok &= check(revealed == [4, 6, 8], "three indicators revealed at 4, 6, 8: %r"
                % revealed)
    ok &= check(next(i for i in WANPAI_KAN_SCAN if w2[i] & WANPAI_REVEALED) == 8,
                "the kan chain's scan (12,10,8,6) lands on the newest indicator")
    ok &= check(w2[0] == 0 and w2[1] != 0 and w2[2] != 0 and w2[3] != 0,
                "after rinshan draws only slot 0 is EMPTY -- the client re-fills "
                "the slot before the first occupied one (002e9940) and pulls it "
                "(motion 9), so its copy always has 1..3 populated: %r"
                % ["%#x" % v for v in w2[:4]])
    ok &= check([v for v in wanpai_u16s(dead, dora_shown=1, kans=1)[:4]]
                == [0] + [v for v in w[1:4]],
                "one kan: the same -- slot 0 empty, 1..3 as dealt")
    b14 = wanpai_bytes(dead, dora_shown=1)
    ok &= check([v & 0x3F for v in b14] == [v & 0x3F for v in w],
                "byte form carries the same ids in the same slots")
    ok &= check((b14[4] & 0x40) and all((v & 0x40) == 0 for i, v in enumerate(b14)
                                        if i != 4),
                "byte-form REVEALED flag is 0x40 (console.c:3899 shifts it to 0x80)")
    # the two forms agree after the client's own unpack
    ok &= check(all(unpack_pond(b) == v for b, v in zip(b14, w)),
                "ALLDATA's byte unpack reproduces HAIPAI's u16 exactly")
    # The rollback lever reproduces the pre-09-04 wire form exactly: indicator
    # 0 at slot 12 with the bit CLEAR, every other slot with the bit SET.
    lw = wanpai_u16s(dead, dora_shown=1, legacy=True)
    ok &= check((lw[12] & 0x3F) == tile_byte(dead[4]) and (lw[12] & 0x80) == 0
                and all(v & 0x80 for i, v in enumerate(lw) if i != 12)
                and (lw[13] & 0x3F) == tile_byte(dead[9]),
                "legacy=True is the old slot-12 form, bit inverted: %r"
                % ["%#x" % v for v in lw])
    lb = wanpai_bytes(dead, dora_shown=1, legacy=True)
    ok &= check((lb[12] & 0x40) == 0 and all(v & 0x40 for i, v in enumerate(lb)
                                              if i != 12),
                "and the legacy byte form matches it")

    # --- MjTSUMO ------------------------------------------------------------
    hand = [mj.MAN + i for i in range(9)] + [mj.PIN + i for i in range(5)]
    rec = tsumo(0x12, 2, hand, 43, motion=9, per_seat=(1, 2, 3, 4), f4b=7, f4c=8,
                riichi_test=bytes(range(7)), f62=1, f63=2, f64=3)
    ok &= check(len(rec) == TSUMO_LEN, "tsumo length")
    ok &= check(rec[0x18] == 9, "MotionCommand")
    ok &= check(struct.unpack_from("<H", rec, 0x28)[0] == 2, "the drawing seat")
    ok &= check(struct.unpack_from("<14H", rec, 0x2A) == tuple(hand_u16s(hand)),
                "the hand at +0x2a")
    ok &= check(rec[0x4A] == 43, "the wall count at +0x4a")
    ok &= check(tuple(rec[0x46:0x4A]) == (1, 2, 3, 4), "per-seat bytes")
    ok &= check(rec[0x4D:0x54] == bytes(range(7)), "the 7-byte riichi test")
    ok &= check(all(rec[0x54 + i] == 2 for i in range(14)),
                "every slot selectable by default (bit 1 -- the cursor test)")
    ok &= check((rec[0x62], rec[0x63], rec[0x64]) == (1, 2, 3), "the menu gates")
    rec = tsumo(0x12, 2, hand, 43, slot_flags=[0] * 13 + [2])
    ok &= check(rec[0x54] == 0 and rec[0x54 + 13] == 2,
                "a riichi hand can offer exactly one slot")
    rec = tsumo(0x12, 1, hand, 43, motion=3, anim_tile=tile_byte(mj.PIN),
                anim_slot=5, anim_seat=1, riichi=True)
    ok &= check(rec[0x21] == 1, "+0x21 == 1 marks a riichi discard (yamaguchi.c:7920)")
    ok &= check(tsumo(0x12, 1, hand, 43, motion=3, anim_seat=1)[0x21] == 0,
                "and a plain discard leaves it 0")
    rec = tsumo(0x12, 1, hand, 43, motion=9, anim_tile=tile_byte(mj.SOU),
                anim_seat=1)
    ok &= check(rec[0x18] == 9 and rec[0x1B] == tile_byte(mj.SOU) and rec[0x22] == 1,
                "motion 9 (rinshan) carries the tile at +0x1b and the seat at +0x22")

    # --- MjSUTE, parsed the way the client builds it ------------------------
    sute = bytearray(janwire.pack(opcode=MjSUTE, f13=0x12, src=2, dst=4,
                                  f16=1, sub=3, length=0x20))
    struct.pack_into("<I", sute, 0x18, 0x10007)
    p = parse_sute(bytes(sute))
    ok &= check(p["seat"] == 2 and p["index"] == 7, "sute seat/index")
    ok &= check(p["action"] == SUTE_RIICHI and p["action_name"] == "riichi",
                "0x10000 is the riichi arm")
    ok &= check(p["seq"] == 0x12, "the sequence comes back on the discard")

    # --- MjNAKI / MjNAKIACK -------------------------------------------------
    rec = naki(0x13, 40, calls={1: {"pon": True, "ron": True}, 3: {"chi": True}},
               slot_flags={1: [7] + [0] * 13},
               u28=tile_u16(mj.SOU + 5), s2a=2,
               menu={1: bytes([0, 1, 0, 0, 1, 0, 0]),
                     3: bytes([0, 0, 0, 0, 0, 1, 0])})
    ok &= check(len(rec) == NAKI_LEN, "naki length")
    ok &= check(rec[0x33] == 40, "wall count at +0x33")
    ok &= check(struct.unpack_from("<H", rec, 0x28)[0] == tile_u16(mj.SOU + 5),
                "+0x28 carries the claimable tile (the pon arm scans for it)")
    ok &= check(rec[0x2A] == 2, "+0x2a is the discarder seat")
    ok &= check(rec[0x2B + 1] == 1 and rec[0x2B + 3] == 1 and rec[0x2B] == 0,
                "+0x2b gates exactly the seats with a call")
    ok &= check(rec[0x44 + 1] == 1 and rec[0x3C + 1] == 1 and rec[0x48 + 1] == 0,
                "the per-call arrays")
    ok &= check(rec[0x48 + 3] == 1, "seat 3 may chi")
    ok &= check(rec[0x4C + 1 * 7 + 1] == 1 and rec[0x4C + 1 * 7 + 4] == 1
                and rec[0x4C + 1 * 7 + 5] == 0,
                "seat 1's menu block lights Ron+Pon only")
    ok &= check(rec[0x4C + 3 * 7 + 5] == 1 and rec[0x4C + 3 * 7 + 1] == 0,
                "seat 3's menu block lights Chi only")
    ok &= check(all(b == 0 for b in rec[0x4C:0x4C + 7]),
                "an uninvolved seat's menu block stays zero")
    ok &= check(0x4C + 4 * 7 == 0x68, "the menu blocks must abut +0x68")
    ok &= check(rec[0x68 + 1 * 14] == 7, "seat 1's slot flags at +0x68")
    ok &= check(0x68 + 4 * 14 == 0xA0, "the meld/flag blocks must abut at 0xa0")

    ack = bytearray(janwire.pack(opcode=MjNAKIACK, f13=0x13, src=1, dst=4,
                                 f16=1, sub=3, length=0x20))
    struct.pack_into("<I", ack, 0x18, NAKI_PON | tile_u16(mj.PIN + 4))
    p = parse_nakiack(bytes(ack))
    ok &= check(p["action_name"] == "pon" and p["tile"] == mj.PIN + 4,
                "a pon carries a TILE in the low half, not an index: %r" % p)
    struct.pack_into("<I", ack, 0x18, 0)
    ok &= check(parse_nakiack(bytes(ack))["action_name"] == "pass", "0 is a pass")
    # The chi cursor -> pair table (mahdisp.c:3906-3919), claimed 5p:
    c = mj.PIN + 4
    table = {mj.PIN + 6: (mj.PIN + 5, mj.PIN + 6),     # cursor 7p -> 6p7p
             mj.PIN + 5: (mj.PIN + 5, mj.PIN + 6),     # cursor 6p -> 6p7p
             mj.PIN + 3: (mj.PIN + 3, mj.PIN + 5),     # cursor 4p -> 4p6p
             mj.PIN + 2: (mj.PIN + 2, mj.PIN + 3)}     # cursor 3p -> 3p4p
    for cur, want in table.items():
        ok &= check(chi_pair_from_ack(c, cur) == want,
                    "chi ack cursor %s on %s -> %r, got %r"
                    % (mj.name(cur), mj.name(c), want, chi_pair_from_ack(c, cur)))
    ok &= check(chi_pair_from_ack(c, mj.PIN + 8) is None
                and chi_pair_from_ack(c, mj.SOU + 5) is None
                and chi_pair_from_ack(mj.PIN + 7, mj.PIN + 8) is None
                and chi_pair_from_ack(27, 28) is None,
                "a cursor that is not a same-suit neighbour, or a run off the "
                "suit's edge, is no pair")
    struct.pack_into("<I", ack, 0x18, NAKI_CHI | tile_u16(mj.PIN + 3))
    p = parse_nakiack(bytes(ack), claimed=mj.PIN + 4)
    ok &= check(p["action_name"] == "chi" and p["chi_pair"] == (mj.PIN + 3, mj.PIN + 5),
                "parse_nakiack(claimed=) fills chi_pair from the cursor tile: %r" % p)

    # --- MjYAKUDISP: every byte where mahdisp__002bcba0 loads it ------------
    rows = yaku_rows([("riichi", 1), ("menzen_tsumo", 1), ("pinfu", 1),
                      ("bakaze", 1), ("jikaze", 1), ("dora", 2), ("aka", 1),
                      ("ura", 1), ("nagashi_mangan", 5)])
    ok &= check(rows == [(2, 1), (6, 1), (10, 1), (26, 2), (28, 4)],
                "yaku rows: ids from the table, bakaze+jikaze (the engine's "
                "wind names) is the id-26 double wind, dora+aka+ura merge on "
                "id 28, unknown names are dropped: %r" % rows)
    rows2 = yaku_rows([("yakuhai_haku", 1), ("jikaze", 1), ("tanyao", 1)])
    ok &= check(rows2 == [(27, 1), (9, 1), (27, 1)],
                "a dragon (yakuhai_<tile>) and a lone wind are id-27 rows: %r" % rows2)
    ok &= check(yaku_rows([("kokushi13", 0), ("tenhou", 0)]) == [(35, 0), (32, 0)],
                "yakuman rows carry id >= 0x20 and no han")
    rec = yakudisp(0x20, han=3, fu=40, winner=2, dealer=1, ron=True, round_wind=1,
                   kyoku=2, honba=1, yakuman=0, show_ura=1, yaku=rows,
                   pay_base=5200, pay_dealer=0)
    ok &= check(len(rec) == YAKUDISP_LEN, "yakudisp is 0x6c")
    ok &= check(tuple(rec[0x18:0x1D]) == (3, 40, 2, 1, 1),
                "+0x18 han, +0x19 fu, +0x1a winner, +0x1b dealer, +0x1c ron")
    ok &= check(tuple(rec[0x1D:0x22]) == (1, 2, 1, 0, 1),
                "+0x1d round wind, +0x1e kyoku, +0x1f honba (mahdisp.c:607-609), "
                "+0x20 yakuman, +0x21 show-ura (mahdisp.c:611)")
    pairs = [struct.unpack_from("<HH", rec, 0x22 + 4 * i) for i in range(6)]
    ok &= check(pairs[:5] == rows and pairs[5] == (0, 0),
                "+0x22 is (id, han) PAIRS at stride 4, id 0 terminates: %r" % pairs)
    ok &= check(struct.unpack_from("<i", rec, 0x64)[0] == 5200,
                "+0x64 is the ron payment in points -- 5200 shows 5200")
    ok &= check(struct.unpack_from("<i", rec, 0x68)[0] == 0, "+0x68 dealer share")
    ok &= check(rec[0x62] == 0 and rec[0x63] == 0, "+0x62 stays unread/zero")
    rec = yakudisp(0x21, han=13, fu=30, winner=0, dealer=0, ron=False, yakuman=1,
                   yaku=[(35, 0)], pay_base=16000)
    ok &= check(rec[0x20] == 1 and rec[0x1C] == 0
                and struct.unpack_from("<i", rec, 0x64)[0] == 16000,
                "a dealer tsumo yakuman: flag set, tsumo, per-player 16000 "
                "(the client shows it x3)")

    # --- MjSEISAN, including the negative the HAKO rule allows --------------
    rec = seisan(0x14, [25000, -1200, 48000, 28200], state=(1, 2, 3, 4, 5))
    ok &= check(len(rec) == SEISAN_LEN, "seisan is 0x2d")
    ok &= check(struct.unpack_from("<4i", rec, 0x18) == (25000, -1200, 48000, 28200),
                "four SIGNED scores at +0x18")
    ok &= check(tuple(rec[0x28:0x2D]) == (1, 2, 3, 4, 5), "the round-state block")
    rec = seisan(0x14, [0] * 4, current_seat=3, dealer=2, round_wind=1, kyoku=2,
                 honba=5)
    ok &= check(tuple(rec[0x28:0x2D]) == (3, 2, 1, 2, 5),
                "SEISAN order is (current, dealer, round wind, kyoku, honba) -- "
                "mahdisp.c:788-793 -- not (wind, kyoku, honba, sticks, 0)")

    # --- MjALLDATA ----------------------------------------------------------
    ponds = [[(mj.MAN, True)] + [mj.PIN + 1] * 3, [], [], []]
    rec = alldata(0x15, hands, ponds, melds=[b"\1\2\3\4", b"", b"", b""],
                  scores=[25000, 25000, 25000, 25000], subtype=3,
                  current_seat=2, wall_count=57, f13d_hi=(1, 2, 3), f13f=(4, 5))
    ok &= check(len(rec) == ALLDATA_LEN, "alldata is 0x140")
    ok &= check(rec[0x18] == 3, "the subtype at +0x18")
    ok &= check(struct.unpack_from("<4i", rec, 0x28) == (25000,) * 4, "scores")
    ok &= check(rec[ALLDATA_HAND] == tile_byte(hands[0][0]), "hand block")
    ok &= check(rec[ALLDATA_POND] & 0x40, "a called pond tile keeps bit 6")
    ok &= check(rec[ALLDATA_MELD] == 1, "meld block")
    ok &= check(ALLDATA_HAND + 4 * ALLDATA_HAND_STRIDE == ALLDATA_POND,
                "hands abut ponds")
    ok &= check(ALLDATA_POND + 4 * ALLDATA_POND_STRIDE == 0xE6,
                "ponds abut melds")
    ok &= check(rec[0x13D] & 3 == 2, "current seat in +0x13d bits 0-1")
    ok &= check((rec[0x13D] >> 2) & 3 == 1 and (rec[0x13D] >> 6) & 3 == 3,
                "the three 2-bit fields above it")
    ok &= check(rec[0x13E] >> 1 == 57, "the 7-bit wall count at +0x13e")
    ok &= check((rec[0x13F] & 0xF, rec[0x13F] >> 4) == (4, 5), "the two nibbles")
    rec = alldata(0x15, hands, ponds, current_seat=1, wall_count=40, dealer=2,
                  kyoku=3, chiicha=1, round_wind=1, honba=2, riichi_sticks=3,
                  dice=(6, 2), seat_mask=0x0A, subtype=10)
    ok &= check(rec[0x13B] == 2, "+0x13b honba")
    ok &= check(rec[0x13C] == 3, "+0x13c is the RIICHI-STICK POT (console.c:3926)")
    ok &= check((rec[0x13D] & 3, (rec[0x13D] >> 2) & 3, (rec[0x13D] >> 4) & 3,
                 rec[0x13D] >> 6) == (1, 2, 3, 1),
                "+0x13d = current | dealer<<2 | kyoku<<4 | chiicha<<6")
    ok &= check(rec[0x13E] & 1 == 1 and rec[0x13E] >> 1 == 40,
                "+0x13e bit 0 is the ROUND WIND (console.c:3923)")
    ok &= check((rec[0x13F] & 0xF, rec[0x13F] >> 4) == (6, 2), "+0x13f = the dice")
    ok &= check(rec[0x18] == 10 and rec[0x22] == 0x0A,
                "motion 10 with a seat bitmask at +0x22 (the draw reveal)")
    ok &= check(rec[0x23:0x27] == b"\0\0\0\0", "+0x23..+0x26 default to zero")
    rec = alldata(0x15, hands, ponds, subtype=8, seat_flags=(0, 0, 1, 0),
                  call_from=0, call_tiles=[0x15, 0x22], call_slots=[1, 7, 0],
                  event_seat=2)
    ok &= check(rec[0x25] == 1 and rec[0x23] == 0 and rec[0x24] == 0 and rec[0x26] == 0,
                "seat_flags writes +0x23+seat (the HOLD-the-rinshan byte)")
    ok &= check(rec[0x1B] == 0x15 and rec[0x1C] == 0x22 and rec[0x1D] == 1
                and rec[0x1E] == 7 and rec[0x22] == 2,
                "a kan record: +0x1b the kan tile, +0x1c the RINSHAN tile the "
                "handler flies itself, +0x1d/+0x1e the slots, +0x22 the seat")

    # --- the meld block: a chi draws upright, only the called tile rotated ---
    class _M:
        def __init__(self, kind_, tiles, called):
            self.kind, self.tiles, self.called = kind_, tiles, called
    # seat 1 chi 7-8-9s claiming the 8s (from the left); seat 2 pon of 3 man.
    chi = _M(mj.CHI, [mj.SOU + 6, mj.SOU + 8, mj.SOU + 7], mj.SOU + 7)  # 7s,9s,+8s
    pon = _M(mj.PON, [mj.MAN + 2, mj.MAN + 2, mj.MAN + 2], mj.MAN + 2)
    blk1, blk2 = meld_block([chi]), meld_block([pon])
    typ1, typ2 = meld_types([chi]), meld_types([pon])
    ok &= check(typ1[0] == MELD_TYPE_CHI and typ2[0] == MELD_TYPE_PON,
                "chi layout is 1, pon layout is 0: %r %r" % (typ1[0], typ2[0]))
    # rank order 7,8,9 with ONLY the 8 (called) carrying bit 6
    ids = [b & 0x3F for b in blk1[:3]]
    ok &= check(ids == [tile_byte(mj.SOU + 6), tile_byte(mj.SOU + 7),
                        tile_byte(mj.SOU + 8)], "chi tiles land in rank order")
    called_marks = [bool(b & POND_FLAG) for b in blk1[:3]]
    ok &= check(called_marks == [False, True, False],
                "exactly the called 8s is flagged (0x40) for rotation: %r"
                % called_marks)
    ok &= check(sum(bool(b & POND_FLAG) for b in blk2[:3]) == 1,
                "a pon flags exactly one tile (the claimed one)")
    rec = alldata(0x17, hands, [[]] * 4, melds=[b"", blk1, blk2, b""],
                  meld_types={1: typ1, 2: typ2})
    ok &= check(rec[0x126 + 1 * 4] == MELD_TYPE_CHI,
                "seat 1's meld-type byte lands at +0x126+seat*4")
    ok &= check(rec[0x126 + 2 * 4] == MELD_TYPE_PON, "seat 2's is the pon layout")
    ok &= check(rec[ALLDATA_MELD + 1 * ALLDATA_MELD_STRIDE + 1] & POND_FLAG,
                "the chi's called tile keeps bit 6 in the +0xe6 block")

    # --- HALF1: per-SEAT columns, at the readers' offsets ---------------------
    rec = gameresult_half1(
        0x30, places=(2, 0, 3, 1), scores=(20000, 42000, 8000, 30000),
        points=(-10000, 12000, -22000, 0),
        after_uma=(0, 32000, -42000, 10000),
        after_yakitori=(0, 32000, -52000, 20000),
        final=(0, 52000, -52000, 0),
        money_before=(100, 200, 300, 400), money_after=(100, 252, 248, 400),
        sashiuma=bytes([3, 0, 0, 0, 0, 0]))
    ok &= check(len(rec) == HALF1_LEN == 0xF8,
                "half1 covers the +0xec block (was 0xE8): %#x" % len(rec))
    ok &= check(tuple(rec[0xE8:0xEC]) == (2, 0, 3, 1),
                "+0xe8 + seat = the PLACE byte, zero-based (yamaguchi2.c:8668)")
    ok &= check(struct.unpack_from("<4i", rec, 0x58) == (20000, 42000, 8000, 30000)
                and struct.unpack_from("<4i", rec, 0x68) == (20000, 42000, 8000, 30000),
                "+0x58/+0x68 raw scores before/after the disconnect stage (equal "
                "= the stage is skipped)")
    ok &= check(struct.unpack_from("<4i", rec, 0x78) == (-10000, 12000, -22000, 0),
                "+0x78 + seat*4 the +/- points column")
    ok &= check(struct.unpack_from("<4i", rec, 0x88) == (0, 32000, -42000, 10000),
                "+0x88 after uma")
    ok &= check(struct.unpack_from("<4i", rec, 0x98) == (0, 32000, -52000, 20000),
                "+0x98 after yakitori")
    ok &= check(struct.unpack_from("<4i", rec, 0xA8) == (0, 52000, -52000, 0),
                "+0xa8 final")
    ok &= check(struct.unpack_from("<q", rec, 0x18 + 8) == (200,)
                and struct.unpack_from("<q", rec, 0x38 + 8) == (252,),
                "+0x18/+0x38 + seat*8 money before/after, s64")
    ok &= check(rec[0xEC:0xF2] == bytes([3, 0, 0, 0, 0, 0]),
                "+0xec the sashiuma result block (yamaguchi__002d98a0)")
    bare = gameresult_half1(0x31, scores=(1, 2, 3, 4), points=(5, 6, 7, 8))
    ok &= check(struct.unpack_from("<4i", bare, 0xA8) == (5, 6, 7, 8)
                and struct.unpack_from("<4i", bare, 0x68) == (1, 2, 3, 4),
                "unsupplied stages inherit the previous column")

    # --- HALF2's ranking geometry, the one with a format string behind it ----
    ranking = [[0, 1, 2, 3], [1, 0, 3, 2], [2, 3, 0, 1], [3, 2, 1, 0], [0, 0, 0, 0]]
    rec = gameresult_half2(0x16, ranking)
    ok &= check(len(rec) == HALF2_LEN, "half2 is 80 bytes of body")
    for row in range(5):
        for seat in range(4):
            got = struct.unpack_from("<I", rec, 0x18 + row * 0x10 + seat * 4)[0]
            ok &= check(got == ranking[row][seat],
                        "Ranking[%d][%d] at +%#x" % (row, seat,
                                                     0x18 + row * 0x10 + seat * 4))
    ok &= check(ranking[0][0] == 0,
                "first place is ZERO -- the client prints rank+1")

    # --- MjMEMBERLEAVE / ANSER ----------------------------------------------
    rec = memberleave(0x17, seat=2, member_id=0xABCD)
    ok &= check(janwire.unpack(rec)["opcode"] == MjMEMBERLEAVE, "memberleave opcode")
    ok &= check(janwire.unpack(rec)["sub"] == INGAME_SUB,
                "the QUESTION arrives in-game (the dispatch arm is 0x3c)")

    ans = bytearray(janwire.pack(opcode=MjMEMBERLEAVEANSER, src=0xFD, dst=0xFE,
                                 f16=1, sub=0, id8=0x1111, payload=0x2222,
                                 length=MEMBERLEAVE_ANSER_LEN))
    ans += bytes(MEMBERLEAVE_ANSER_LEN - len(ans))
    struct.pack_into("<I", ans, 0x20, 1)
    p = parse_memberleave_anser(bytes(ans))
    ok &= check(p["continue"] and p["member_id"] == 0x2222,
                "the answer carries the answerer's own id and a 1: %r" % p)
    ok &= check(janwire.unpack(bytes(ans))["sub"] == 0,
                "and it goes out on sub 0, NOT the in-game 1")
    struct.pack_into("<I", ans, 0x20, 0)
    ok &= check(not parse_memberleave_anser(bytes(ans))["continue"], "0 = do not")

    # --- acks ---------------------------------------------------------------
    ok &= check(ACK_OF[MjHAIPAI] == MjHAIPAIACK and ACK_OF[MjGAMEEND] == MjBYE,
                "ack = opcode + 1, and GAMEEND's is named MjBYE")
    a = janwire.pack(opcode=MjHAIPAIACK, f13=0x11, src=0, dst=4, f16=1, sub=3)
    ok &= check(is_ack_for(a, MjHAIPAI), "an ack is recognised for its message")

    # --- every record still serialises to a legal line ----------------------
    for rec in (haipai(1, hands), tsumo(2, 0, hand, 70), naki(3, 69),
                alldata(4, hands, ponds), seisan(5, [0] * 4)):
        line = janwire.encode(rec)
        ok &= check(janwire.decode(line) == rec, "%d-byte record round trips" % len(rec))
        ok &= check(all(0x40 <= c <= 0x7F for c in line[1:]),
                    "every character stays in 0x40..0x7f")

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    # --- IsHuman +0x23..+0x26 and MjREADYSTATUS (2026-09-09) ----------------
    ih = (1, 0, 0, 1)
    hh = [list(range(1, 14))] * 4
    for name, r in (("haipai", haipai(1, hh, is_human=ih)),
                    ("tsumo", tsumo(1, 0, list(range(1, 15)), 60, is_human=ih)),
                    ("naki", naki(1, 60, is_human=ih)),
                    ("alldata", alldata(1, hh, [[], [], [], []], is_human=ih))):
        ok &= check(bytes(r[0x23:0x27]) == b"\x01\x00\x00\x01",
                    "%s carries IsHuman at +0x23..+0x26: %r" % (name, r[0x23:0x27]))
    for name, r in (("haipai", haipai(1, hh)), ("tsumo", tsumo(1, 0, list(range(1, 15)), 60)),
                    ("naki", naki(1, 60)), ("alldata", alldata(1, hh, [[], [], [], []]))):
        ok &= check(bytes(r[0x23:0x27]) == b"\0\0\0\0",
                    "%s IsHuman defaults to the old all-zero form" % name)
    rs = readystatus(0x21, (1, 0, 1, 1))
    hr = janwire.unpack(rs)
    ok &= check(hr["opcode"] == MjREADYSTATUS and hr["sub"] == READY_SUB
                and len(rs) == 0x20 and bytes(rs[0x18:0x1C]) == b"\x01\x00\x01\x01",
                "readystatus: opcode 66 on sub 2, four bytes at +0x18: %r" % (rs,))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.parse_args()
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
