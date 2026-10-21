"""Server-to-client notices: kicked, time running out, server quitting, and the
table member list.
"""
import os
import struct
import janwire                                                  # noqa: E402
from . import opcodes, seating

# Server -> client notices. The dispatcher at 0x002c6384 maps the opcode to a
# BIT (not an index), and the seven bits are the TGM_NOTICE_* names in order:
#
#   14 MjNOTICEMEMBER         -> 1   MEMBER_LIST
#   15 MjNOTICEBANISH         -> 2   BANISH
#   16 MjNOTICECALL           -> 4   TABLE_CALL
#   18 MjNOTICEGAMESTART      -> 8   GAME_START
#   19 MjNOTICETIMEUPWARNING  -> 16  TIME_UP_WARNING
#   20 MjNOTICEGAMESETUP      -> 32  GAME_SETUP
#   21 MjNOTICESERVERQUIT     -> 64  SERVER_QUIT
#
# (17 MjNOTICECALLACK is the client's answer to the call, not a notice.)
NOTICE_BIT = {14: 1, 15: 2, 16: 4, 18: 8, 19: 16, 20: 32, 21: 64}

MjNOTICEMEMBER, MjNOTICEBANISH = 14, 15
MjMEMBERLISTREQ = 5
MEMBERLIST_ENABLE = os.environ.get("POL_JAN_MEMBERLIST", "1") == "1"

# Both notice handlers open by calling 0x002bbdd0(dst, msg), which is not a
# parser at all -- it copies the record's first 24 bytes: byte 0, then 7 bytes,
# then the u64 at +8, the u16 at +0x10, and 0x12..0x17 one at a time. So it is a
# header stash, and it confirms the record model from a third independent site:
# +0x00 is its own byte, +0x01..+0x07 a 7-byte blob, +0x08 a u64, +0x10 the
# length, +0x12..+0x17 the six single-byte fields.
HEADER_LEN = 0x18

# Player handles on the wire are 16 bytes: the banish handler copies two of them
# with an 8-iteration two-bytes-at-a-time loop (0x002c64f0, 0x002c651c).
NAME_LEN = 16


def banish_notice(kicked=0, by=0, kicked_name=b"", by_name=b""):
    """MjNOTICEBANISH, in the layout the client reads at malloc.c:1392-1417.

        +0x18  u64        -> 0x00446f68 -> 0x4460b8   (by whom)  NEVER READ
        +0x20  u64        -> 0x00446f70 -> 0x4460c0   THE KICKED PolID
        +0x28  16 bytes   -> 0x00446f78     (a name)  never read
        +0x38  16 bytes   -> 0x00446f88     (a name)  never read

    WARNING: +0x20 IS THE ONE THAT MATTERS, and the old inference had the two u64s
    the other way round. lobbydsp.c:159-170: every recipient gets the "YYY
    was kicked out by XXX" dialog (literal placeholders -- the names are not
    substituted), and `if (notice+0x20 == *(u64*)DAT_004464e0)` -- the
    recipient's OWN PolID -- it clears DAT_00445e30 (seated) and DAT_00445e31
    (master). So the kicked member's PolID must be at +0x20 or nobody's
    client un-seats itself. Nothing reads +0x18; the kicker goes there so a
    capture still says who did it.
    """
    body = bytearray(0x48 - HEADER_LEN)
    struct.pack_into("<QQ", body, 0x00, by & 0xFFFFFFFFFFFFFFFF,
                     kicked & 0xFFFFFFFFFFFFFFFF)
    body[0x10:0x10 + NAME_LEN] = by_name[:NAME_LEN].ljust(NAME_LEN, b"\0")
    body[0x20:0x20 + NAME_LEN] = kicked_name[:NAME_LEN].ljust(NAME_LEN, b"\0")
    rec = janwire.pack(opcode=MjNOTICEBANISH, length=0x48,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(body)


def timeup_minutes(rest_seconds):
    """The byte MjNOTICETIMEUPWARNING carries: whole MINUTES, rounded UP.

    0 seconds (or less) is 0, which the 2004 client prints as "your reservation
    was cancelled because time ran out"; anything left is at least 1 minute,
    so a warning never reads as that cancellation. Capped at the byte."""
    try:
        s = int(rest_seconds)
    except (TypeError, ValueError):
        s = 0
    if s <= 0:
        return 0
    return min(255, -(-s // 60))


def timeup_notice(rest_seconds=0):
    """MjNOTICETIMEUPWARNING: the seat's time is running out (or has).

    Both builds read ONE field, the byte at +0x20 (not +0x18; the zero-caller
    janmsgs.notice_timeup puts it at +0x18, which nothing reads):

      2002  malloc.c:1354-1358 stores `msg[+0x20] & 0xff` and the only
            consumer prints "Rest Time = %d" to the debug log (lmenu.c:2009).
      2004  Receive_Notice_00392c60 stores it in DAT_00511528 and the lobby
            notice printer fn_003a80a0 (bit 0x10) prints it on screen:
            0     -> "時間切れの為に、参加予約がキャンセルされました。"
                     (0x004b0400, time ran out, reservation cancelled) and
                     clears the reservation (fn_003a7ff0)
            n > 0 -> "あなたが参加予約したテーブルは、あと%d分でキャンセル
                     されます。" (0x004b0440: cancelled in %d MINUTES)

    So the value is MINUTES. `rest_seconds` is converted here
    (`timeup_minutes`, rounded up), which is what callers holding a TTL in
    seconds want."""
    body = bytearray(0x28 - HEADER_LEN)
    struct.pack_into("<I", body, 0x08, timeup_minutes(rest_seconds))
    rec = janwire.pack(opcode=opcodes.MjNOTICETIMEUPWARNING, length=0x28,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(body)


def serverquit_notice():
    """MjNOTICESERVERQUIT. The dispatcher maps it to bit 0x40 and reads no
    field. The 2002 ladder (lmenu.c:1992-2020) stops at 0x20, so that build
    logs TGM_NOTICE_SERVER_QUIT and does nothing; the 2004 lobby notice
    printer fn_003a80a0 prints "システムのメンテナンスが開始しました。参加予約は
    キャンセルされました。" (0x004b0480: maintenance has started, your
    reservation was cancelled) and clears the reservation. Sent on a graceful
    shutdown."""
    rec = janwire.pack(opcode=opcodes.MjNOTICESERVERQUIT, length=0x20,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(0x20 - HEADER_LEN)


# --- MjNOTICECALL (16): "your table is waiting" / "the game can start" -------
#
# MEASURED in the 2004 build (Receive_Notice_00392c60, jan_00390000.c:2482-
# 2495): on opcode 0x10 the client ANSWERS by itself -- it rewrites the record
# as MjNOTICECALLACK (17), len 0x18, +0x08 = our +0x18, src -3 dst -2, +0x16 0,
# sub 5, and sends it -- and then raises bit 4 when the u32 at +0x20 is 1,
# bit 0x80 otherwise. The lobby notice printer fn_003a80a0 prints:
#   bit 0x4   "あなたが参加予約したテーブルがゲーム開始待ちです。" (0x004b0330)
#             = "the table you reserved is waiting for the game to start"
#   bit 0x80  "ゲームが開始できます。" (0x004b0370) = "the game can be started"
# Both only print (their flag DAT_004c42af has no reader). The 2002 build sets
# a flag nobody reads, so the notice is harmless there.
MjNOTICECALL, MjNOTICECALLACK = 16, 17
CALL_WAITING = 1                # +0x20 == 1 -> bit 4, the reserver's line
CALL_CAN_START = 0              # anything else -> bit 0x80, the master's line


def call_notice(waiting, ref=0):
    """MjNOTICECALL. `waiting` True = "the table you reserved is waiting for
    the game to start" (a reserver), False = "the game can be started" (the
    master). `ref` lands at +0x18 and comes back at +0x08 of the client's own
    MjNOTICECALLACK, so the composite table id goes there."""
    body = bytearray(0x28 - HEADER_LEN)
    struct.pack_into("<Q", body, 0x00, int(ref) & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<I", body, 0x08, CALL_WAITING if waiting else CALL_CAN_START)
    rec = janwire.pack(opcode=MjNOTICECALL, length=0x28,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(body)


# --- the 2004-only server notices 94/95/96 ------------------------------------
#
# MEASURED (2004 build, Receive_Notice_00392c60, jan_00390000.c:2433-2450).
# Each one is acted on only when the u64 at +0x18 is the recipient's own PolID
# or 0 (0 = everyone):
#
#   94 '^' CmNOTICEMSGOPEN       strcpy(0x511530, record + 0x2c) -> bit 0x200;
#        fn_003a80a0 hands the text to the GMNotice window (GMNotice.cc,
#        fn_003c37d0 copies it to 0x512ef0, a 0x181-byte buffer), which shows
#        it for 0x1c20 frames (two minutes), centred when it is under 0x41
#        bytes and as a scrolling ticker when longer (fn_003c3460/3620).
#   95 '_' CmNOTICEMSGCLOSE      bit 0x400 -> fn_003c3910: the window closes.
#   96 '`' CmNOTICEJANHOLOWKICK  bit 0x800 -> sets the exit flags
#        (_DAT_004c3ea0/_DAT_004c3ea8 = 1), fn_00390600, fn_00319000(7): the
#        client leaves JongHoLow. This is a SERVER kick out of the title, not
#        the table master's kick (that one is MjNOTICEBANISH, 15).
#
# The 2002 build has no arm for any of them (its dispatcher returns 0 bits), so
# sending one to a 2002 client does nothing.
CmNOTICEMSGOPEN, CmNOTICEMSGCLOSE, CmNOTICEJANHOLOWKICK = 94, 95, 96
GMNOTICE_TEXT_OFF = 0x2C
#: The receive buffer at 0x511530 runs to the next global at 0x5116b0 (0x180
#: bytes, NUL included), and the record has to stay inside the size the game
#: band already carries (MjALLDATA is 0x140), so the text is capped at 255
#: bytes of cp932 plus its NUL.
GMNOTICE_TEXT_MAX = 0xFF


def _gm_text(text):
    raw = (text if isinstance(text, (bytes, bytearray))
           else str(text or "").encode("cp932", "replace"))
    raw = bytes(raw).split(_NUL, 1)[0]
    if len(raw) > GMNOTICE_TEXT_MAX:
        cut = raw[:GMNOTICE_TEXT_MAX]
        # never end on the first byte of a two-byte cp932 character
        try:
            cut.decode("cp932")
        except UnicodeDecodeError:
            cut = cut[:-1]
        raw = cut
    return raw


def gm_notice_open(text, polid=0):
    """CmNOTICEMSGOPEN: open the in-room notice window with `text`."""
    raw = _gm_text(text)
    length = GMNOTICE_TEXT_OFF + len(raw) + 1
    length += (-length) % 4
    rec = bytearray(janwire.pack(opcode=CmNOTICEMSGOPEN, length=length,
                                 src=janwire.DST_SERVER & 0xFF,
                                 dst=janwire.DST_CHANNEL & 0xFF, sub=0,
                                 payload=int(polid) & 0xFFFFFFFFFFFFFFFF))
    rec = rec[:0x20] + bytearray(length - 0x20)
    rec[GMNOTICE_TEXT_OFF:GMNOTICE_TEXT_OFF + len(raw)] = raw
    return bytes(rec)


def _gm_bare(opcode, polid=0):
    rec = janwire.pack(opcode=opcode, length=0x20,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0,
                       payload=int(polid) & 0xFFFFFFFFFFFFFFFF)
    return rec[:0x20]


def gm_notice_close(polid=0):
    """CmNOTICEMSGCLOSE: close the in-room notice window."""
    return _gm_bare(CmNOTICEMSGCLOSE, polid)


def gm_kick(polid=0):
    """CmNOTICEJANHOLOWKICK: the client leaves JongHoLow."""
    return _gm_bare(CmNOTICEJANHOLOWKICK, polid)


# WARNING: `+0x18` IS A RESULT CODE, AND 0 MEANS OK -- it is NOT a member count.
# Measured 2026-08-17 after a live client showed an EMPTY Table Members list
# while the seated COUNT was right. `mahdisp__GetTableMemberList_002bba80`:
#
#     _DAT_00446098 = malloc__002c67a0(buf);   /* the count: non-zero ids */
#     if (iStack_88 == 0) {                    /* iStack_88 IS buf +0x18 */
#         ... copy every field into the display globals ...
#
# so a non-zero +0x18 skips the whole copy and the list never populates. We had
# been sending the member count there (1, then 4), which is exactly wrong: the
# count is DERIVED from the ids at +0x30 and this field must be 0.
RESULT_OK = 0

#: The 64-byte block at +0x55 is four 16-byte names -- 4 x 16 = 64, and
#: `MjNOTICEBANISH` measured a handle at 16 bytes on the wire. INFERENCE that
#: these are the member names in seat order; the width and the block are
#: measured.
NAME_SLOT = 16


#: One NUL. Spelled this way because a heredoc round trip turns an escape
#: into the byte itself and puts a real NUL in the source file.
_NUL = bytes(1)


def _seat_summary(ids, table=None):
    """`seat=id name` per seat, for the log.

    The names are the half nobody can see from the wire trace: a blank one at
    the LOCAL seat is the difference between "three COMs and me" and "I am
    watching four COMs play", and it costs one live run to find out which.
    """
    names = seating._member_names(ids, table)
    out = []
    for s in range(4):
        raw = names[s * NAME_SLOT:(s + 1) * NAME_SLOT].split(_NUL, 1)[0]
        out.append("%d=%016x %r" % (s, ids[s] if s < len(ids) else 0,
                                    raw.decode("cp932", "replace")))
    return "  ".join(out)


def member_list_notice(ids=(0, 0, 0, 0), vals=(0.0, 0.0, 0.0, 0.0),
                       flags=(0, 0, 0, 0), a=0, b=0, seat=0, tail=b""):
    """MjNOTICEMEMBER, in the layout the client reads at 0x002c63f0.

    Every field below is MEASURED -- offset and type both -- from the loads the
    client performs. What the fields MEAN is not: they are four-wide per-seat
    arrays for a four-player table, and `ids` lines up with the 8-byte member
    ids the seat table holds, but that last part is inference.

        +0x18  u32          -> 0x00446ec8          (a)
        +0x1c  u32          -> 0x00446ecc          (b)
        +0x20  4 x float32  -> 0x00446ed0          (lwc1/swc1: floats, not ints)
        +0x30  4 x u64      -> 0x00446ee0          (the per-seat ids)
        +0x50  4 x u8       -> 0x00446f00          per-seat READY (!=0) / AWAY (0)
        +0x54  u8           -> 0x00446f04          THE MASTER'S SEAT, 0..3
        +0x55  64 bytes     -> 0x00446f05          (memcpy, a2 = 64)

    Note +0x18 is read here as TWO u32s, where the ACK path reads the same bytes
    as one u64 payload. The header is shared; the tail is per-opcode.

    WARNING: +0x54 IS THE MASTER'S SEAT, NOT THE RECIPIENT'S (2026-09-04, finding
    25). mahdisp.c:57 assigns it straight into `DAT_004460e9`, the master-seat
    global the RESERVEACK's high nibble seeded, and mahdisp.c:53 is the ONE
    writer of the change-master dialog: `+0x54 != DAT_004460e9 && +0x54 ==
    DAT_004460e8 (my seat)` -> "You are the new Table Master". ReturnRoom.c:337
    paints the seat it names into the `Name1` slot. We used to send the
    recipient's own seat, which told every guest they had just been promoted.
    The +0x50 bytes are what ReturnRoom.c:343/358 draw as Ready/Away.
    """
    body = bytearray(0x95 - 0x18)                  # from +0x18 to +0x55+64
    struct.pack_into("<II", body, 0x00, a & 0xFFFFFFFF, b & 0xFFFFFFFF)
    for i in range(4):
        struct.pack_into("<f", body, 0x08 + 4 * i, float(vals[i]))
        struct.pack_into("<Q", body, 0x18 + 8 * i, ids[i] & 0xFFFFFFFFFFFFFFFF)
        body[0x38 + i] = flags[i] & 0xFF
    body[0x3C] = seat & 0xFF
    body[0x3D:0x3D + min(64, len(tail))] = tail[:64]
    rec = janwire.pack(opcode=MjNOTICEMEMBER, length=0x95,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF,   # a notice goes to the table
                       sub=0)                            # -> queue 2
    return rec[:0x18] + bytes(body)
