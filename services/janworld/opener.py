"""What the client waits on before any lobby traffic: MjGETLNDV,
MjCHECKSAVEDATA, and the 2004 build's EmISEVENT.
"""
import os
import struct
import janwire                                                  # noqa: E402
from .deps import janevent

# --- MjGETLNDV: the opener, and the one message we know the client waits on ---
#
# CAPTURED LIVE 2026-08-12. This is the FIRST thing Janhourou sends -- about a
# second after the auth welcome, before any lobby or room traffic -- and it is
# the message the black screen was waiting on. The record, verbatim:
#
#     opcode=64 MjGETLNDV  len=32  src=-3  dst=-2  f13=0  sub=5
#     id8=5b01e32e3c5d4f85  payload=5b01e2f59f813ab1
#
# so the game addresses the SERVER (src -3) and asks for the reply to go to the
# id in its own +0x08 (dst -2) -- the same convention MjTGMPONG uses.
#
# THE GATE IS READ, NOT GUESSED -- 0x002ca5dc..0x002ca708, found the same
# way as the rest (the `sb rt,0x12(rs)` builder scan puts MjGETLNDV at 0x002ca618, and the
# immediate 65 at 0x002ca69c sits 0x84 bytes later in the same function). The send
# in that function matches the captured record field for field, which is what
# confirms this is the right site:
#
#     [s1+0x12] = 64      [s1+0x10] = 32 (sh -- a LITTLE-endian halfword, which is
#     [s1+0x14] = -3       the third confirmation of the endianness)
#     [s1+0x15] = -2      [s1+0x17] = 5      [s1+0x08] = fp    [s1+0x18] = [s6]
#
# and then it polls for the answer, up to 1200 times:
#
#     move  a0, zero               <-- READS QUEUE 0
#     jal   0x002c9d80(0, s1)
#     blezl v0 -> retry
#     lb    a0, 0x12(s1)
#     addiu v1, zero, 65
#     bne   a0, v1 -> retry        <-- the ONLY field it gates on
#
# TWO THINGS THIS CORRECTS, both of which cost a live launch each:
#
# 1. **The ACK must arrive in QUEUE 0, so rec[0x17] = 7**, because +0x17 INDEXES
#    the routing table (verified from the module: int32[8] at 0x003e4520 =
#    [2,3,4,5,6,7,8,0], so 7 -> queue 0). Echoing the request's sub of 5 sent it
#    to queue 7 and the waiter never saw it -- exactly the trap an earlier
#    pass fell into by getting it backwards.
# 2. **The ACK carries a real 56-byte BODY**, not a result byte. The accept path
#    copies six fields straight out to the caller's variables:
#
#        +0x18  u64  -> [sp+0xa4]        +0x30  u32  -> s7
#        +0x20  u64  -> [sp+0xa0]        +0x34  u16  -> [sp+0xa8]
#        +0x28  u64  -> [s3]             +0x36  u8   -> [sp+0xac]
#
#    Highest byte touched is +0x36, so the record is 0x38 = 56 bytes.
#
# The gate does NOT check the correlation nibble, src, dst or id8 -- only the
# opcode. So those stay on the protocol's normal conventions and cannot be the
# reason a reply is refused.
#
# **+0x30 IS A VERSION STAMP AND IT IS CHECKED.** Found 2026-08-12 after the
# zero-filled body got us past the gate but the game quit anyway. The gate loads
# it into s7 (`lw s7, 0x30(s1)` at 0x002ca6d8) and holds it across the whole
# MjCHECKSAVEDATA exchange, then at 0x002ca8e0:
#
#     lui  v0, 0x2002
#     ori  v0, v0, 0x0227          ; 0x20020227 -- a DATE, 2002-02-27
#     beq  s7, v0, <success>
#     ...                          ; else log "ERROR:GM Version" (0x00417970)
#     addiu v0, zero, -5           ; and RETURN -5
#
# So "Ln DV" is a game-manager VERSION the server states and the client refuses to
# run against anything else. Zero there is why the client completed both opener
# exchanges and then closed the connection and went back to the portal -- it was
# not stalling, it had already decided to abort. The other five fields are copied
# out to caller-owned variables and are NOT checked here.
#
#   POL_JAN_LNDV       0 disables the responder (back to capture-only)
#   POL_JAN_LNDV_SUB   override rec[0x17]; default 0 = the gate's drain sub-code
#   POL_JAN_LNDV_F18 / _F20 / _F28   the three u64s (unchecked here, default 0)
#   POL_JAN_LNDV_F30   the GM version -- MUST be 0x20020227 or the game quits
#   POL_JAN_LNDV_F34 / _F36   the u16 and u8 (unchecked here, default 0)
#   POL_JAN_LNDV_BODY  raw hex for the whole tail from +0x18, overrides the six
#
# WHAT THE FIELDS ARE. Project Crystal Server's GetLnDvAck names
# the 2004 layout: +0x18 Profile channel id, +0x20 Rank channel id, +0x28 Lobby
# channel id, +0x30 LobbyEvent channel id, +0x38 u32 version, +0x3C u16 lobby
# volume, +0x3E u8 lobby domain -- and fills them with its own server ids
# ("pp0002", "pp0004", "pp0001", "pp0001") each moved into Janhourou's id space
# (`MjKey` = XOR with mgkey's K("MJS")), version 0x20020603, volume 0, domain 3.
# The 2002 layout is the same list without LobbyEvent: +0x18 Profile, +0x20
# Rank, +0x28 Lobby, +0x30 version, +0x34 volume, +0x36 domain. One piece of
# that is ours, off the 2002 decompile (sqMgInitMg, objstrings.c:2436, 2460):
# the +0x28 u64 is where the client ADDRESSES its very next message, the
# MjCHECKSAVEDATA check (`uStack_3f8 = *in_t1_lo` -> record +0x08) -- a lobby
# server, as Crystal's name says. Profile/Rank/volume/domain are Crystal's
# names only.
#
#   POL_JAN_LNDV_CHANNELS=1   (default OFF) serve Crystal-style channel ids and
#                             domain 3 instead of zeros. It moves the
#                             MjCHECKSAVEDATA destination off id 0, the id the
#                             save check is answered at today; check that the
#                             save check is still answered before turning it on.
#                             The explicit POL_JAN_LNDV_F18/_F20/_F28/_F34/_F36
#                             still win. In the dual record the 2002 version must
#                             stay at +0x30, so the 2004 build's LobbyEvent id
#                             cannot be served there; with the knob on only its
#                             volume/domain (+0x3C/+0x3E) are filled.
GM_VERSION = 0x20020227             # measured: the compare at 0x002ca8e0

#: Crystal's server POL ids for the channels (MjConstants.cs), in POL-ID space.
LNDV_POLID_LOBBY = 0x00000113405D1B2C       # "pp0001", Crystal's balancer
LNDV_POLID_PROFILE = 0x0000011341108228     # "pp0002"
LNDV_POLID_RANK = 0x000000DC8475C22A        # "pp0004"
LNDV_LOBBY_DOMAIN = 3                       # Crystal's LobbyDomain


def _lndv_channels_on():
    return os.environ.get("POL_JAN_LNDV_CHANNELS", "0") == "1"


def lndv_channel_ids():
    """(profile, rank, lobby) as served with POL_JAN_LNDV_CHANNELS=1: each
    POL id XOR K("MJS"), Crystal's `MjKey`."""
    import mgkey
    return tuple(mgkey.game_id(x, "MJS") for x in
                 (LNDV_POLID_PROFILE, LNDV_POLID_RANK, LNDV_POLID_LOBBY))

# THE 2004 CLIENT READS A DIFFERENT LAYOUT AND DEMANDS A DIFFERENT VERSION.
# Measured 2026-09-21 on build 20040727_2 (the Janhourou the Dirge of Cerberus
# and Front Mission Online discs install), from a savestate taken on its error
# screen `JHR-12935-13302` ("the version differs, please update"). Its gate,
# linit.cc at 0x002e7530.., takes the same opcode 65 record and reads
#
#     +0x18 +0x20 +0x28 +0x30   FOUR u64s (the 2002 build has three)
#     +0x38                     the version     lw v1, 56(s6)
#     +0x3C u16, +0x3E u8
#
# and at 0x002e77fc compares the version with 0x20020603, a date again. We
# answered 56 bytes with 0x20020227 at +0x30, so it read past the end of the
# record and refused. The two clients send byte-identical requests (len 32,
# sub 5), so the request cannot say which one is asking. One record serves
# both: 0x40 long, the 2002 version where the 2002 build looks (+0x30, which
# the 2004 build copies out as the low half of its unchecked fourth u64) and
# the 2004 version where the 2004 build looks (+0x38, past everything the
# 2002 build reads). POL_JAN_LNDV_DUAL=0 restores the 56-byte record.
GM_VERSION_2004 = 0x20020603        # measured: the compare at 0x002e77fc
LNDV_DUAL = os.environ.get("POL_JAN_LNDV_DUAL", "1") == "1"
MjGETLNDV, MjGETLNDVACK = 64, 65

LNDV_ENABLE = os.environ.get("POL_JAN_LNDV", "1") == "1"
LNDV_DRAIN_SUB = 0                  # measured: `move a0, zero` at 0x002ca684, and
                                    # a0 is the SUB-CODE the gate waits on
LNDV_LEN = 0x38                     # measured: highest field read is +0x36
LNDV_SUB = int(os.environ.get("POL_JAN_LNDV_SUB", str(LNDV_DRAIN_SUB)))
LNDV_BODY = bytes.fromhex(os.environ.get("POL_JAN_LNDV_BODY", "").replace(" ", ""))


def _env_int(name, default=0):
    """`name` from the environment as an int (any base); unset or empty (a
    compose `${NAME:-}` with nothing behind it) reads as `default`."""
    v = (os.environ.get(name) or "").strip()
    return default if not v else int(v, 0)


def getlndv_ack(req, body=None):
    """MjGETLNDVACK in the layout the client's own gate reads (see above).

    MEASURED: the queue, the opcode gate, the six field offsets and widths, and
    that no other header field is checked; +0x28 is the MjCHECKSAVEDATA
    destination. NAMED BY CRYSTAL ONLY: profile / rank / lobby channel ids,
    volume, domain. Zero unless overridden or POL_JAN_LNDV_CHANNELS=1.
    """
    h = janwire.unpack(req)
    if h["opcode"] != MjGETLNDV or not LNDV_ENABLE:
        return None
    if body is None:
        body = LNDV_BODY
    if not body:
        chans = _lndv_channels_on()
        profile, rank, lobby = lndv_channel_ids() if chans else (0, 0, 0)
        domain = LNDV_LOBBY_DOMAIN if chans else 0
        tail = bytearray(LNDV_LEN - 0x18)          # +0x18 .. +0x37
        struct.pack_into("<QQQ", tail, 0x00,       # profile, rank, lobby
                         _env_int("POL_JAN_LNDV_F18", profile) & 0xFFFFFFFFFFFFFFFF,
                         _env_int("POL_JAN_LNDV_F20", rank) & 0xFFFFFFFFFFFFFFFF,
                         _env_int("POL_JAN_LNDV_F28", lobby) & 0xFFFFFFFFFFFFFFFF)
        struct.pack_into("<I", tail, 0x18,         # the 2002 GM version
                         _env_int("POL_JAN_LNDV_F30", GM_VERSION) & 0xFFFFFFFF)
        struct.pack_into("<H", tail, 0x1C,         # 2002 lobby volume
                         _env_int("POL_JAN_LNDV_F34") & 0xFFFF)
        tail[0x1E] = _env_int("POL_JAN_LNDV_F36", domain) & 0xFF   # 2002 domain
        if LNDV_DUAL:
            # +0x30..+0x37 above is the 2004 build's LobbyEvent id (unchecked);
            # +0x38 its version, +0x3C its volume, +0x3E its domain.
            tail += bytearray(8)                    # +0x38 .. +0x3F
            struct.pack_into("<I", tail, 0x20, GM_VERSION_2004)
            if chans:
                tail[0x26] = domain
        body = bytes(tail)
    rec = janwire.pack(
        opcode=MjGETLNDVACK,
        f13=h["f13"],                      # echoed VERBATIM -- never mask (see above)
        src=janwire.DST_SERVER & 0xFF,
        dst=janwire.DST_REPLY & 0xFF,
        sub=LNDV_SUB,                      # = the sub-code the gate drains on
        id8=h["payload"],
        length=0x18 + len(body),
    )
    return rec[:0x18] + body


# --- MjCHECKSAVEDATA: the SECOND message of the opener, reached 2026-08-12 -----
#
# The client sends this the moment MjGETLNDVACK lands, so it is the next thing
# between us and a running game. Its gate is at 0x002ca800..0x002ca8c0, read the
# same way as LNDV's, and the module's own debug strings name the exchange:
#
#     0x004178e0  "IM UD Check Request"
#     0x00417900  "ERROR:IM UD Check Failed Retry..."
#     0x00417930  "ERROR:IM UD Check Time Out"
#     0x00417950  "ERROR:IM UD Check Success!"      <- SE prefixes success too
#
#     move  a0, zero              <-- drains on SUB-CODE 0, same as LNDV
#     jal   0x002c9d80(0, s1)
#     lb    a0, 0x12(s1)
#     addiu v1, zero, 68
#     bne   a0, v1 -> retry       <-- opcode 68 MjCHECKSAVEDATAACK
#     lbu   v1, 0x18(s1)
#     addiu v0, zero, 1
#     bne   v1, v0 -> "Failed Retry..."
#             ...success: s0 = 1
#     beq   s0, zero, 0x002ca760  <-- s0 == 0 RESENDS THE REQUEST, forever
#
# So exactly two fields are read: the opcode, and a BYTE at +0x18 that must be
# **1**. Nothing else in the record is touched, so the ACK is a plain 32-byte
# record -- unlike MjGETLNDVACK, whose six-field body the gate really does copy
# out. Getting the byte wrong is not silent-and-idle like the LNDV bug was: it
# spins, re-sending the request.
#
#   POL_JAN_SAVEDATA        0 disables the responder
#   POL_JAN_SAVEDATA_SUB    override rec[0x17]; default 0
#   POL_JAN_SAVEDATA_OK     the byte at +0x18; default 1 = the success branch
MjCHECKSAVEDATA, MjCHECKSAVEDATAACK = 67, 68

SAVEDATA_ENABLE = os.environ.get("POL_JAN_SAVEDATA", "1") == "1"
SAVEDATA_DRAIN_SUB = 0              # measured: `move a0, zero` at 0x002ca800
SAVEDATA_SUB = int(os.environ.get("POL_JAN_SAVEDATA_SUB", str(SAVEDATA_DRAIN_SUB)))
SAVEDATA_OK = _env_int("POL_JAN_SAVEDATA_OK", 1)


def checksavedata_ack(req):
    """MjCHECKSAVEDATAACK, in the layout the gate at 0x002ca800 reads.

    MEASURED: the drain sub-code, the opcode, and that +0x18 must be 1. Nothing
    else is read, and a wrong byte makes the client resend rather than stall.
    """
    h = janwire.unpack(req)
    if h["opcode"] != MjCHECKSAVEDATA or not SAVEDATA_ENABLE:
        return None
    rec = bytearray(janwire.pack(
        opcode=MjCHECKSAVEDATAACK,
        f13=h["f13"],
        src=janwire.DST_SERVER & 0xFF,
        dst=janwire.DST_REPLY & 0xFF,
        sub=SAVEDATA_SUB,
        id8=h["payload"],
    ))
    rec[0x18] = SAVEDATA_OK & 0xFF
    return bytes(rec)


# --- EmISEVENT / EmISEVENTACK: the 2004 build's Rankings opener ---------------
#
# Build 20040727_2 only (the 2002 build has no opcode 92/93). Every address is
# in the plaintext 2004 module, base 0x00280000. Drop-in for janhourou.py, which
# already has `os`, `struct`, `janwire` and `_env_int`.
#
# SENDER  0x003c5ec0(ctx = 0x004c46f0), called from RankingMain state 5
#         (ranking_main.cc, 0x003271c8..cc):
#     rec+0x08  u64  = [0x004c3e98]           (the standing GM peer id)
#     rec+0x10  u16  = 40                     addiu v1,zero,40 @0x003c5ed0
#     rec+0x12  u8   = 92                     @0x003c5ec4
#     rec+0x13       NOT WRITTEN (stack junk; the capture shows 0)
#     rec+0x14 = 4, +0x15 = -2, +0x16 = 3, +0x17 = 5
#     rec+0x18  u64  = *ctx  (own PolID; the same u64 MjTGMPONG puts at +0x18,
#                             0x00392f68)
#     rec+0x20  u32  = 6     a CONSTANT: `addiu v0,zero,6` @0x003c5f08,
#                            `sw v0,48(sp)` @0x003c5f18. Not a ranking kind and
#                            not an event id -- nothing feeds it. INFERENCE: it
#                            names the FIFO sub-code the client will wait on,
#                            because the waiter below drains on exactly 6.
#     rec+0x24  u32  not written (junk)
#
# GATE    0x003c5f30(E = RankingMain+0x6bac), polled from state 6 (0x003271f8):
#     addiu a0, zero, 6          @0x003c5f48   <-- drains SUB-CODE 6
#     jal   0x00392490           (the 2004 twin of 0x002c9d80; it buckets every
#                                 inbound record by rec[0x17] through
#                                 int32[8] @0x00485910 = [2,3,4,5,6,7,8,0],
#                                 same table as 2002's 0x003e4520)
#     blez  v0 -> return 0
#     lb v1,18(s1); addiu v0,zero,93; bne -> return 0     (opcode only; src,
#                                 dst, f13, f16, id8 are never looked at)
#     lw  v0, 76(s1)  -> E+60    rec+0x4C u32   copied, no reader found
#     lw  v0, 80(s1)  -> E+64    rec+0x50 u32   copied, no reader found
#     memcpy(E+76, s1+84, 64)    rec+0x54 64 B  copied (event name? INFERENCE)
#     lbu v1, 148(s1) -> E+72    rec+0x94 u8    THE FLAG
#     return 1
#   rec+0x18..0x4B are never read. Highest byte read is +0x94, so the record is
#   0x98 long.
#
# THE FLAG: 0x00327ed0 (state 7) does `lw v0,27636(a0)` (= E+72) and enables
#   the "Event" tab only when it is exactly 1 (`bne v0,a2` with a2=1), else
#   disables it; 0x00327f20 is the same test, and the tab's help line is then
#   0x0049f4c0 "no event is being held, the ranking cannot be viewed".
#   So 0 = no event, 1 = event running. Any other value = no event.
#
# TIMEOUT: state 5 arms 0x0038d3f0(120) -- 0x0038d1d0 stores 120*1000, so it is
#   120 SECONDS -- and state 6 raises dialog -13059 "the server is very busy"
#   (0x0049ee30) when it expires, then leaves the screen. That 2-minute wait
#   is the reported hang. A wrong-opcode record in FIFO 6 is eaten and the poll
#   goes on; the gate can never return <0, so -13058 is unreachable.
#
#   POL_JAN_ISEVENT        0 disables the responder
#   POL_JAN_ISEVENT_SUB    override rec[0x17]; default 6
#   POL_JAN_ISEVENT_FLAG   the byte at +0x94; default 0 = no event
EmISEVENT, EmISEVENTACK = 92, 93

ISEVENT_ENABLE = os.environ.get("POL_JAN_ISEVENT", "1") == "1"
ISEVENT_DRAIN_SUB = 6               # measured: `addiu a0, zero, 6` at 0x003c5f48
ISEVENT_SUB = int(os.environ.get("POL_JAN_ISEVENT_SUB", str(ISEVENT_DRAIN_SUB)))
ISEVENT_LEN = 0x98                  # measured: highest field read is +0x94
#: The default answer is now the EVENT STORE's, not a constant: `janevent`
#: says whether one is running, and its id and name ride along. The old
#: environment override still wins, so a screen can be forced either way
#: without touching the store.
ISEVENT_FLAG = _env_int("POL_JAN_ISEVENT_FLAG", -1)


def emisevent_ack(req, running=None, name=b"", f4c=0, f50=0):
    """EmISEVENTACK, in the layout the gate at 0x003c5f30 reads.

    MEASURED: the drain sub-code (6), the opcode (93), the four fields and
    their offsets/widths, that +0x94 == 1 is the only "event running" value,
    and that nothing else in the record is read.
    INFERENCE: that +0x54 is the event's name and +0x4C/+0x50 its ids -- they
    are stored and no reader was found, so zero is safe for "no event".
    """
    h = janwire.unpack(req)
    if h["opcode"] != EmISEVENT or not ISEVENT_ENABLE:
        return None
    if running is None:
        if ISEVENT_FLAG >= 0:
            flag = ISEVENT_FLAG
        elif janevent is not None:
            cur = janevent.current()
            flag = 1 if cur else 0
            if cur:
                # The id and the name ride the same record. NEITHER IS DRAWN in
                # this build -- the gate copies them to RankingMain+27624/+27628
                # and +27640 and no site reads any of the three back -- so they
                # are sent because that is what the fields are for, not because
                # anything has been seen to use them.
                name = name or janevent.name_bytes()
                f4c = f4c or int(cur.get("id") or 0)
        else:
            flag = 0
    else:
        flag = 1 if running else 0
    rec = bytearray(janwire.pack(
        opcode=EmISEVENTACK,
        f13=h["f13"],                      # echoed verbatim, as the other acks do
        src=janwire.DST_SERVER & 0xFF,
        dst=janwire.DST_REPLY & 0xFF,
        sub=ISEVENT_SUB,                   # = the sub-code the gate drains on
        id8=h["payload"],                  # the asker's PolID (req +0x18)
        length=ISEVENT_LEN,
    )[:0x18])
    rec += bytearray(ISEVENT_LEN - 0x18)   # +0x18 .. +0x97, all zero
    struct.pack_into("<II", rec, 0x4C, f4c & 0xFFFFFFFF, f50 & 0xFFFFFFFF)
    rec[0x54:0x54 + 64] = bytes(name)[:63].ljust(64, b"\0")   # memcpy of 64
    rec[0x94] = flag & 0xFF                # 1 = event running, else none
    return bytes(rec)
