#!/usr/bin/env python3
"""Janhourou (PlayOnline content id 3) world-server -- 雀鳳楼.

STATUS: the message set, the record layout and the text framing are DECODED --
read out of the decrypted `JanHouRou.pex`, not guessed from captures (the
addresses are in `janwire.py`). What is NOT yet decoded is how a finished line reaches
the socket: the game hands it to the `sqMg*` gateway with a destination that is
either a channel name or a member id, and `sqMgJoinChannel` /
"Join Table Channel Name = %s" say it joins one IRC channel per table. So the
outermost carriage is very likely a PRIVMSG-shaped envelope we have not read.

This module is therefore two things at once, in the shape of tetramaster.py:

  * a real, testable implementation of everything that IS known -- the record,
    both framings, the opcode names, the addressing model, and the one exchange
    that is specified end to end (MjTGMPING -> MjTGMPONG);
  * a CAPTURE HARNESS for the rest. Anything we cannot yet speak is logged with
    its decoded header and a hexdump rather than answered, so the first real
    client contact tells us what the envelope is instead of being swallowed.

    python janhourou.py --selftest        # loopback: ping in, pong out
    python janhourou.py --serve           # listen (POL_JAN_PORT, default 51272)
    python janhourou.py --decode 'B@...'  # one line -> named fields

The client dials **gi003.pol.com** ("gi" + content id 3; IP 61.195.49.200 is
baked into the module) and 51272 is the only POL-band port it references.
"""
import argparse
import os
import socket
import socketserver
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janwire                                                  # noqa: E402
try:
    # THE GAME MANAGER. Optional in the same way responders.py treats this
    # module: absent = the old behaviour, which is that everything in the play
    # sequence is captured and not answered. jangame owns the seats, the hand
    # in progress and the sequence discipline; janmsgs owns the bodies;
    # janmahjong owns the rules. See jangame.py's banner for why a pure
    # request/response drive is enough.
    import jangame                                              # noqa: E402
    import janmsgs                                              # noqa: E402
except ImportError:                                             # pragma: no cover
    jangame = None
    janmsgs = None
try:
    # THE 2004 BUILD'S IN-GAME LAYOUTS (20040727_2). janmsgs builds every
    # record in the 2002 shape; three of them are read differently by the 2004
    # client, so a record bound for one goes through here first. Optional like
    # the rest: absent, every client is served the 2002 shape.
    import janmsgs2004                                          # noqa: E402
except ImportError:                                             # pragma: no cover
    janmsgs2004 = None
try:
    # ONLY for turning a member id into the name a person recognises -- see
    # `display_name`. Optional exactly as in responders.py: absent, a seat draws
    # blank rather than drawing something invented.
    import accounts                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    accounts = None
try:
    # THE SEAT STORE (2026-09-02). Reserve/cancel used to be answered with ONE
    # constant (result 2 for everything), which made every reserver a master and
    # broke cancel outright -- the cancel wait-state accepts only 5/6/0xb/-1
    # (TableMenuPopup__No_Response_0034fec0) and logged our 2 as "illegal code".
    # janseats keys the real answers off the table id BOTH requests carry at
    # +0x08, and feeds the PTL builder's seat counts across the container split.
    # Optional like the rest: absent (or POL_JAN_SEATS=0), the old constants
    # answer exactly as before.
    import janseats                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janseats = None
try:
    # THE RULE STORE (2026-09-04, audit finding 2). MjTBLCONFALL's 112-byte
    # body -- the 33 values the master picked on the rules dialog -- used to be
    # acked and dropped, so every hanchan played `janmahjong.Rules()` defaults
    # while the dialog showed something else. janrules parses it (layout read
    # off ruleset__00347da0 + malloc__002c5050) and keeps it per table for the
    # blob author and the game manager. Optional like the rest.
    import janrules                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janrules = None
try:
    # THE ROOM (audit finding 30). Every table id the client sends is the
    # 16-bit wire id of a row that exists in EVERY room; the room is the one
    # Jan channel the asker is JOINed to, which `janlobby.member_room` reads
    # out of the IRC registry snapshot `LIVE_ROOMS` hands us (responders
    # installs `_live_rooms`). Absent, every table is room 0's -- the shared
    # set, exactly as before.
    import janlobby                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janlobby = None

#: `callable() -> {chan: {"who": [{"member_id": ..}, ..]}}` -- the IRC room
#: registry, as `responders._live_rooms` publishes it. None = no registry
#: (standalone runs), so every table resolves to room 0.
LIVE_ROOMS = None

LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")
PORT = int(os.environ.get("POL_JAN_PORT", "51272"))
HOST = os.environ.get("POL_JAN_HOST", "0.0.0.0")

# Generated by `python work/ps2/janproto.py --tsv` from the name table at
# 0x003e43d0. Regenerate rather than editing: index IS the wire opcode.
#: MjISAYGALLEY (1) is DEAD in this client build: sending it needs chat flag
#: bit 2 and showing it bit 8, and the table screen initialises the widget
#: with flags 5 for players and spectators alike (chat.c:661-665, 707;
#: lobby.c:312). A spectator's line leaves as plain MjISAY on the table
#: channel; `_relay_chat` fans it out.
OPCODES = (
    'MjISAY', 'MjISAYGALLEY', 'MjPLAYREQ', 'MjPLAYCANCEL', 'MjRESERVEACK',
    'MjMEMBERLISTREQ', 'MjGALLEYREQ', 'MjGALLEYLEAVEREQ', 'MjMEMBERBANISH', 'MjCHMASTER',
    'MjTBLCONFSTART', 'MjGAMESTART', 'MjMASTERCMDACK', 'MjTBLCONFALL', 'MjNOTICEMEMBER',
    'MjNOTICEBANISH', 'MjNOTICECALL', 'MjNOTICECALLACK', 'MjNOTICEGAMESTART',
    'MjNOTICETIMEUPWARNING', 'MjNOTICEGAMESETUP', 'MjNOTICESERVERQUIT', 'MjLEAVEROOM',
    'MjENTERROOM', 'MjLEAVECONTENTS', 'MjENTERCONTENTS', 'MjMOVEACK', 'MjTGMPING',
    'MjTGMPONG', 'MjLEAVEGAME', 'MjLEAVEGAMEACK', 'MjENTERGAME', 'MjENTERGAMEACK',
    'MjREADY', 'MjHAIPAI', 'MjHAIPAIACK', 'MjTSUMO', 'MjSUTE', 'MjNAKI', 'MjNAKIACK',
    'MjYAKUDISP', 'MjYAKUDISPACK', 'MjSEISAN', 'MjSEISANACK', 'MjGAMEEND', 'MjBYE',
    'MjALLDATA', 'MjALLDATAACK', 'MjGALLEYACK', 'MjSASHIUMASTART', 'MjSASHIUMAREQUEST',
    'MjSASHIUMASELECT', 'MjSASHIUMAARGEE', 'MjSASHIUMARESULT', 'MjGAMERESULT',
    'MjGAMERESULTACK', 'MjGAMERESULTHALF1', 'MjGAMERESULTHALF1ACK', 'MjGAMERESULTHALF2',
    'MjGAMERESULTHALF2ACK', 'MjMEMBERLEAVE', 'MjMEMBERLEAVEANSER', 'MjHAIPAIDEBUG',
    'MjHAIPAIDEBUGACK', 'MjGETLNDV', 'MjGETLNDVACK', 'MjREADYSTATUS', 'MjCHECKSAVEDATA',
    'MjCHECKSAVEDATAACK', 'MjCHATMEMBERADD', 'MjCHATMEMBERDEL', 'MjCHATMEMBERACK',
    # 0x48 -- NOT in the client's name table (it ends at 71), but a real
    # opcode: `sqFileAccess__INFO_00372ea0` sends it right after
    # MjCHATMEMBERADD with a 16-byte "INFO" name at +0x18 and "%s has
    # entered." at +0x28, and the receive loop `sqFileAccess__003730d0`
    # returns 3 for it (a system line). Named here so the log stops calling
    # it "Mj Non Defind...(72)".
    'MjCHATINFO',
    # 73..100 exist only in the 2004 build (20040727_2), whose table at
    # 0x00485750 has 112 names. It also calls 72 MjCHATINFOMSG and reuses 15
    # as MjNOTICECANCEL. Only 92/93 are used on the wire.
    'UdCONNECT', 'UdCONNECTACK', 'UdDISCONNECT', 'UdDISCONNECTACK', 'UdGETSAVEDATA',
    'UdGETSAVEDATASTATUS', 'UdGETSAVEDATAACK', 'UdSETSAVEDATA', 'UdSETSAVEDATASTATUS',
    'UdSETSAVEDATAACK', 'UdSAVEDATAPACKET', 'UdDETECT', 'UdDETECTACK', 'UdPLAYERLIST',
    'UdPLAYERLISTACK', 'EmENTRYEVENT', 'EmENTRYEVENTACK', 'EmEVENTOPENCLOSE',
    'EmREPORTRANKINGID', 'EmISEVENT', 'EmISEVENTACK', 'CmNOTICEMSGOPEN',
    'CmNOTICEMSGCLOSE', 'CmNOTICEJANHOLOWKICK', 'EmEVENTPARTICIPANTADD',
    'EmEVENTPARTICIPANTADDACK', 'EmGETEVENTPARTICIPANT', 'EmGETEVENTPARTICIPANTACK',
)

# The seven TGM_NOTICE_* names at 0x00417040. rec[0x17] routes into queues 2..8
# via the table at 0x003e4520, so it is a subsystem selector, not a free field.
NOTICES = (
    'TGM_NOTICE_MEMBER_LIST', 'TGM_NOTICE_BANISH', 'TGM_NOTICE_TABLE_CALL',
    'TGM_NOTICE_GAME_START', 'TGM_NOTICE_TIME_UP_WARNING', 'TGM_NOTICE_GAME_SETUP',
    'TGM_NOTICE_SERVER_QUIT',
)

# Table-reservation result codes, read off the if/else ladder in
# wrsCpTableReserveCheck() at 0x002cabf0: `if (code == N) log(<name>)`, N = 1..11.
# The client takes the code from rec[0x18] of an MjRESERVEACK.
RESERVE_RESULTS = {
    1: 'Reserve',            # seated
    2: 'ReserveMaster',      # seated, and you are the table master
    3: 'ReserveDuplicate',
    4: 'ReserveDuplicate2',
    5: 'ReserveCancel',
    6: 'ReserveCancelError',
    7: 'ReserveReject',
    8: 'ReserveTimeOut',
    9: 'ReservePassWordError',
    10: 'ReserveLimit',
    11: 'ReservePOLproDataFlush',
}

# --- RESERVE LIMITS: result 10 for a table whose entry limits you miss ------
#
# The master's second rules dialog carries seven entry limits (rule indexes
# 26-32: LIMIT_MONEY, LIMIT_LEVEL, LIMIT_SYOGO1..5 -- "Money limit", "Level
# limit", "Title 1..5 limit" in the client's dialog strings). janrules stores
# them; this is where they are enforced. Result 10 is the client's own "Entry
# limits not met. Cannot reserve" (TableMenuPopup.c:536, string 0x196030).
#
# THE EXTRA BYTE. Project Crystal Server documents result 10's +0x19 as a
# bitfield of the unmet conditions in the order (Money, Level, Title), so
# bit 0 money, bit 1 level, bit 2 title here. That order is Crystal's label
# and UNVERIFIED: the 2002 build's result-10 arm shows a fixed string and
# never reads the byte, which lands in the seat global 0x4460e8 like every
# other RESERVEACK extra (TableMenuPopup.c:213). A later build may render it.
#
# THE THRESHOLDS ARE NOT DECODED. Each rule is stored as "None | Yes" (janrules
# clamps it to 0/1), and nothing we have read says what amount or level "Yes"
# demands. So the amounts are server knobs and the default is the weakest
# plain reading of each label:
#   Money limit on   -> JAN balance >= POL_JAN_LIMIT_MONEY_MIN (default 1)
#   Level limit on   -> janstats level >= POL_JAN_LIMIT_LEVEL_MIN (default 1,
#                       which every player meets -- set it to use it)
#   Title n limit on -> holds title n (count > 0), janstats.SHOGO_KEYS order,
#                       the order the client's title byte arrays index
#
# The table's own seated members are never refused (the master set the limits
# and re-sends on a missed ACK). POL_JAN_RESERVE_LIMITS=1 enables; default off.
RESERVE_LIMIT_MONEY, RESERVE_LIMIT_LEVEL, RESERVE_LIMIT_TITLE = 0x01, 0x02, 0x04
RESERVE_LIMIT_RESULT = 10           # == janseats.RESERVE_LIMIT


def _reserve_limits_on():
    return os.environ.get("POL_JAN_RESERVE_LIMITS", "0") == "1"


def reserve_limits_unmet(member_id, tid):
    """The unmet-limit bitfield for `member_id` reserving table `tid`, 0 when
    every limit is met, the table has none, or the check cannot run."""
    if janrules is None or not member_id:
        return 0
    try:
        vals = janrules.values_for(tid) or {}
    except Exception:
        return 0
    money = int(vals.get("LIMIT_MONEY") or 0)
    level = int(vals.get("LIMIT_LEVEL") or 0)
    titles = [int(vals.get("LIMIT_SYOGO%d" % n) or 0) for n in range(1, 6)]
    if not (money or level or any(titles)):
        return 0
    if janseats is not None:
        try:
            if any(m == int(member_id) for m, _s, _n, _p in janseats.seats_at(tid)):
                return 0
        except Exception:
            pass
    try:
        import janstats
        rec = janstats.load(member_id)
    except Exception as exc:
        log("jan: reserve limits for member %s not checked (%r)" % (member_id, exc))
        return 0
    unmet = 0
    if money and janstats.money_balance(rec) < _env_int("POL_JAN_LIMIT_MONEY_MIN", 1):
        unmet |= RESERVE_LIMIT_MONEY
    if level and janstats.level_of(rec) < _env_int("POL_JAN_LIMIT_LEVEL_MIN", 1):
        unmet |= RESERVE_LIMIT_LEVEL
    if any(titles):
        held = rec.get("titles") or {}
        for n, want in enumerate(titles):
            if want and int(held.get(janstats.SHOGO_KEYS[n], 0) or 0) <= 0:
                unmet |= RESERVE_LIMIT_TITLE
                break
    return unmet

# What the client actually requires of an MjRESERVEACK, from 0x002caba4:
#
#     0x002c9d80(5, buf)                 <- reservation traffic is QUEUE 5
#     if (buf[0x12] != 4)        return   -- opcode must be MjRESERVEACK
#     if (buf[0x13] != (tag & 0x0f)) return
#     result = buf[0x18];  extra = buf[0x19]
#
# So rec[0x13] is a CORRELATION TAG the client keeps in a global and compares
# against; a reply carrying the wrong one is silently dropped. That is what the
# unexplained "f13" field is, protocol-wide -- MjSUTE loads it from a global too
# (0x00285bec).
#
# WARNING: IT IS NOT A NIBBLE, AND MASKING IT TO ONE HANGS THE CLIENT. Corrected
# 2026-08-17 after a live hang on the rules screen. The two gates use DIFFERENT
# globals and DIFFERENT shapes:
#
#   objstrings__MjTgmTblReserve_002cab50   opcode 4   buf[0x13] == DAT_00449048 & 0xf
#   malloc__MjTgmTmCmdSuccess_002c5400     opcode 12  buf[0x13] == DAT_00449058 & 0xf | 0x20
#                                                                                  ^^^^^^
# and all four master-command senders (malloc.c) build theirs as
# `bStack_1ed = ++DAT_00449058 & 0xf | 0x20`. So in BOTH families the byte the
# client sends is the byte it wants back, and the only correct rule is to
# **echo rec[0x13] VERBATIM**.
#
# Masking to & 0x0f is what we did, and it worked for reserve purely by accident
# -- that tag has no high bits to lose. MjTBLCONFALL arrived with f13=0x21, we
# answered 0x01, `wrsCpMasterCmdCheck` compared it against 0x21 and returned 0 =
# still pending, and the table screen polled until its own 120000-tick timeout.
# Live evidence: MjPLAYREQ f13=1 -> ack f13=1 advanced; MjTBLCONFALL f13=33 ->
# ack f13=1 did not.
RESERVE_QUEUE = 5

# THE ROUTING RULE, read off 0x002c9d80 in full on 2026-08-12 after a live client
# bounced our reply straight back at us. It is simpler than every previous note
# here, and both of those notes were wrong:
#
#   0x002c9d80(sub, buf):
#       loop: rec = getRecord(scratch, 1)          <- ALWAYS FIFO 1, the raw inbound
#             if !rec: break
#             put(scratch, TABLE[rec[0x17]], len)  <- bucket it by its OWN sub-code
#       return getRecord(buf, TABLE[sub])          <- then take one from MY bucket
#
# The table is applied to BOTH sides, so it cancels. The rule for a server is
# therefore just: **rec[0x17] must equal the sub-code the waiting gate passes.**
# There is no queue arithmetic to do at all.
#
#   gate                          waits with        so the ACK needs rec[0x17] =
#   wrsCpTableReserveCheck() etc  drain(5)          5
#   the MjGETLNDV poll            drain(0)          0
#
# What this corrects: the older note said "rec[0x17] INDEXES the table and the
# entry is the queue, so to land in queue 5 the sub-code is 3". The indexing part
# is right, but `a0` is NOT a queue -- it is a sub-code that gets indexed the same
# way. Deriving the sub from a queue produced sub=3 for the four ACKs (never live-
# tested) and, when I echoed the request's 5 for MjGETLNDV, sub=7 -- and TABLE[7]
# is 0, which is the OUTBOUND FIFO. The client obligingly serialised our own ACK
# and sent it back to us, which is exactly what the log showed.
SUB_TO_QUEUE = (2, 3, 4, 5, 6, 7, 8, 0)     # kept: it is the real table, and the
                                            # bucket a record lands in is still
                                            # TABLE[sub]. It just is not something
                                            # a server ever needs to invert.


def queue_for_sub(sub):
    """The FIFO a record with this rec[0x17] gets bucketed into. Diagnostics only
    -- a server picks `sub` to match a gate, never a queue."""
    return SUB_TO_QUEUE[sub] if 0 <= sub < len(SUB_TO_QUEUE) else -1


MjPLAYREQ, MjPLAYCANCEL, MjRESERVEACK = 2, 3, 4
MjGALLEYREQ, MjGALLEYLEAVEREQ = 6, 7
MjMEMBERBANISH, MjCHMASTER, MjTBLCONFSTART = 8, 9, 10
MjGAMESTART, MjMASTERCMDACK, MjTBLCONFALL = 11, 12, 13
MjNOTICETIMEUPWARNING, MjNOTICESERVERQUIT = 19, 21
MjLEAVEROOM, MjLEAVECONTENTS = 22, 24
MjENTERROOM = 23                    # client->server after a RESERVE; no reply drained
MjLEAVEGAME, MjLEAVEGAMEACK, MjENTERGAME, MjENTERGAMEACK = 29, 30, 31, 32
MjCHATMEMBERADD, MjCHATMEMBERDEL, MjCHATMEMBERACK, MjCHATINFO = 69, 70, 71, 0x48

#: The four builders that bump the RESERVE tag counter `DAT_00449048`
#: (objstrings.c:2555/2604/2806/2852). A kick is answered against THAT
#: counter (see `_kick`), so its latest value is remembered per seat.
RESERVE_TAG_OPCODES = (MjPLAYREQ, MjPLAYCANCEL, MjGALLEYREQ, MjGALLEYLEAVEREQ)

#: MjPLAYREQ +0x42 (u16): the player's VoiceCharaNum -- `lobbysub__002b9620`
#: copies option word[7] (voicesel.c "data.VoiceCharaNum") into the lobby
#: object's +0xb6, and `objstrings__002ca990` writes that as `uStack_1be`,
#: i.e. +0x42 of the 0x108-byte reserve record. It is the voice bank
#: `b/g/MJSTableInfoSub` +0x218 must serve back for the seat (lobby.c:138).
#: MjPLAYREQ +0x40, u16 LE -- the player's PlayOnline HANDLE-ICON index,
#: beside the voice at +0x42. The 2004 build reads a face per seat out of
#: `b/g/MJSTableInfoSub` and loads `hnf%03d.png` (index >> 3), cell
#: index & 7, from the Viewer's icon download folder. The client builds
#: the record at 0x003935e0 from its identity block: block+0x14 lands
#: here and block+0x16 at the voice, which is what pins the pair.
PLAYREQ_FACE_OFF = 0x40
PLAYREQ_VOICE_OFF = 0x42

#: MjMEMBERBANISH +0x28 (u64): the TARGET's PolID -- `malloc__002c4fb0`'s
#: third argument, `*(u64*)(ctx+0xa8)` in lmenu.c:1474 / the row's id in
#: TableMemberBanish.c:179. No name rides the request.
BANISH_TARGET_OFF = 0x28

#: THE GALLERY (spectators, decoded 2026-09-04; every byte is read out of the
#: client, file:line as in its C sources). MjGALLEYREQ (op 6, objstrings.c:2783-2830: len
#: 0x108, +0x08 table id, +0x13 = ++DAT_00449048 & 0xf, +0x18 PolID, +0x44
#: char[17] password -- ALWAYS "" in this client build, +0x55 char[16] name)
#: is answered on the reserve gate (objstrings.c:2627-2698: sub 5, +0x12 ==
#: 4, +0x13 == the tag, result +0x18, +0x19 -> DAT_0042aba0 = the GALLERY
#: SLOT the client stamps on every MjGALLEYACK +0x14). The result ladder
#: (TableMenuPopup.c:1083-1156): 1 enters the table screen as a spectator,
#: 3 "Already spectating", 7 "This table has refused you as a spectator",
#: 8 "Entry limits were added, or the table status changed", 9 "The
#: password you entered is wrong", 10 "Entry limits not met" -- each of
#: 3/7/8/9/10 closes the dialog and returns to the menu (state 7).
#: MjGALLEYLEAVEREQ (op 7, objstrings.c:2835-2870, len 0x28, +0x08 table
#: id) waits on the same gate for 5 or 6 ONLY, with no timeout (mahdisp.c:
#: 1116-1118): an unanswered leave hangs the client, so `_galley` answers
#: every leave.
GALLEY_REFUSED = 7
GALLEY_LEFT = 5
#: POL_JAN_GALLERY=1 grants spectator entry (`_galley` -> janseats' gallery
#: store -> `jangame.Manager.add_spectator`, which copies every record).
#: DEFAULT OFF: the refusal codes are measured, but what happens AFTER a
#: result 1 -- the client rendering copies of the players' records, the
#: subtype-0 MjALLDATA snapshot on a mid-hand join -- is inferred from the
#: decompile and has never been on a screen (spec section 4, questions 1 and
#: 8). A wrong render is not a refusal-code degradation, so the knob stays
#: off until the live test in the spec has been run. With it off a Watch is
#: refused with 7 (the dialog closes), exactly as before.
GALLERY_ENABLE = os.environ.get("POL_JAN_GALLERY", "0") == "1"
#: GALLEY_LIMIT (rule index 20, handlesel.c:977; labels ruleset.c:576-598:
#: 0 "No", 1 "Yes", 2 "No chat only") is the master's dialog choice and is
#: NOT enforced by the client (spec section 1.1) -- we refuse 10 for 0 and
#: drop a spectator's chat for 2. Its shipped default is 0 ("No"), so a
#: table whose master never opened the rules dialog refuses every Watch.
#: POL_JAN_GALLEY_LIMIT_DEFAULT=1 (or 2) is the value such a table plays
#: instead; a master's stored choice always wins over it.
GALLEY_LIMIT_DEFAULT = os.environ.get("POL_JAN_GALLEY_LIMIT_DEFAULT", "").strip()
GALLEY_LIMIT_DEFAULT = int(GALLEY_LIMIT_DEFAULT) if GALLEY_LIMIT_DEFAULT.isdigit() else None
GALLEY_NO, GALLEY_YES, GALLEY_NO_CHAT = 0, 1, 2

RESERVE_RESULT = int(os.environ.get("POL_JAN_RESERVE_RESULT", "2"))   # 2 = Master
TMCMD_RESULT = int(os.environ.get("POL_JAN_TMCMD_RESULT", "1"))      # 1 = Success

# The MjTgmTmCmd* result family, from the identical ladders in
# wrsCpEnterGameCheck() 0x002cb218, wrsCpLeaveGameCheck() 0x002cb358 and
# wrsCpMasterCmdCheck() 0x002c5480. Same four codes, same byte, three functions.
TMCMD_RESULTS = {1: 'Success', 2: 'Failed', 3: 'NoAuthority', 4: 'POLproDataFlush'}

# Every ACK contract read so far, and they are strikingly uniform: all arrive on
# QUEUE 5, all carry their result in rec[0x18], and they differ only in the
# opcode and whether the correlation nibble is checked.
#
#   check function            queue  opcode                nibble  results
#   wrsCpTableReserveCheck()    5    4  MjRESERVEACK        yes     RESERVE_RESULTS
#   wrsCpMasterCmdCheck()       5   12  MjMASTERCMDACK      yes     TMCMD_RESULTS
#   wrsCpLeaveGameCheck()       5   30  MjLEAVEGAMEACK      no      TMCMD_RESULTS
#   wrsCpEnterGameCheck()       5   32  MjENTERGAMEACK      no      TMCMD_RESULTS
#
# We echo the nibble unconditionally: the two that check it require it, and the
# two that do not are indifferent, so there is no case where echoing is wrong.
ACK_FOR = {
    MjPLAYREQ: MjRESERVEACK,
    MjPLAYCANCEL: MjRESERVEACK,
    # WARNING: A KICK WAITS ON THE RESERVE CHECK, NOT THE MASTER-COMMAND ONE
    # (2026-09-04, audit finding 14). TableMemberBanish.c:152-153 and
    # lmenu.c:1473 both poll `objstrings__MjTgmTblReserve_002cab50`, which
    # accepts only opcode 4 with +0x13 == `DAT_00449048 & 0xf` -- the RESERVE
    # counter. Answering MjMASTERCMDACK left the master on the "Removing
    # them from the table" spinner for 120000 ticks. The result byte's value
    # is never looked at (`!= 0` is the whole test). See `_kick`.
    MjMEMBERBANISH: MjRESERVEACK,
    MjCHMASTER: MjMASTERCMDACK,
    # MjGALLEYREQ/MjGALLEYLEAVEREQ wait on the same reserve check, with the
    # codes in `GALLEY_REFUSED` / `GALLEY_LEFT`.
    MjGALLEYREQ: MjRESERVEACK,
    MjGALLEYLEAVEREQ: MjRESERVEACK,
    MjTBLCONFSTART: MjMASTERCMDACK,
    # MjTBLCONFALL (13) -- ADDED 2026-08-16 from a LIVE wall, and it is the same
    # grade of inference as MjTBLCONFSTART above, not a measurement. Pressing OK
    # on the rules dialog sends opcode 13 (len 112, seq 0x21, sub 0x05) and the
    # log said `no handler`, so the table screen stops there. It is a master-only
    # command in the same 0-13 group as TBLCONFSTART, whose ACK is measured, so
    # MjMASTERCMDACK is the candidate. If the client ignores this reply, that is
    # DATA -- the nibble gate in wrsCpMasterCmdCheck() would drop a wrong-opcode
    # ACK silently, so a continued stall means the pairing is wrong, not the
    # framing.
    MjTBLCONFALL: MjMASTERCMDACK,
    MjGAMESTART: MjMASTERCMDACK,
    MjLEAVEGAME: MjLEAVEGAMEACK,
    MjENTERGAME: MjENTERGAMEACK,
}

# The sub-code the four gates wait on: every one of them calls 0x002c9d80(5, buf),
# so their ACKs must carry rec[0x17] = 5. (This was 3 until 2026-08-12, derived
# from the mistaken "sub indexes to a queue, wait on queue 5" reading. No live
# client had ever exercised these four, so the error was never visible.)
ACK_SUB = 5

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
GM_VERSION = 0x20020227             # measured: the compare at 0x002ca8e0

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
        tail = bytearray(LNDV_LEN - 0x18)          # +0x18 .. +0x37
        struct.pack_into("<QQQ", tail, 0x00,
                         _env_int("POL_JAN_LNDV_F18") & 0xFFFFFFFFFFFFFFFF,
                         _env_int("POL_JAN_LNDV_F20") & 0xFFFFFFFFFFFFFFFF,
                         _env_int("POL_JAN_LNDV_F28") & 0xFFFFFFFFFFFFFFFF)
        struct.pack_into("<I", tail, 0x18,
                         _env_int("POL_JAN_LNDV_F30", GM_VERSION) & 0xFFFFFFFF)
        struct.pack_into("<H", tail, 0x1C, _env_int("POL_JAN_LNDV_F34") & 0xFFFF)
        tail[0x1E] = _env_int("POL_JAN_LNDV_F36") & 0xFF
        if LNDV_DUAL:
            tail += bytearray(8)                    # +0x38 .. +0x3F
            struct.pack_into("<I", tail, 0x20, GM_VERSION_2004)
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
try:
    import janevent                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janevent = None


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


def timeup_notice(rest_seconds=0):
    """MjNOTICETIMEUPWARNING. The dispatcher's 0x13 arm (malloc.c:1354-1358)
    reads ONE field: `_DAT_00446f98 = msg[+0x20] & 0xff` -- +0x20, not +0x18
    (janmsgs.notice_timeup puts it at +0x18; that byte is never read). The
    only consumer prints "Rest Time = %d" to the debug log (lmenu.c:2009);
    there is no on-screen countdown, so this is fidelity, not function."""
    body = bytearray(0x28 - HEADER_LEN)
    struct.pack_into("<I", body, 0x08, int(rest_seconds) & 0xFF)
    rec = janwire.pack(opcode=MjNOTICETIMEUPWARNING, length=0x28,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(body)


def serverquit_notice():
    """MjNOTICESERVERQUIT. The dispatcher maps it to bit 0x40 and reads no
    field; no consumer handles the bit (the ladder in lmenu.c:1992-2020 stops
    at 0x20), so the client logs TGM_NOTICE_SERVER_QUIT and does nothing.
    Sent on a graceful shutdown anyway -- it is what SE's server would have
    sent, and a future client reading it costs us nothing."""
    rec = janwire.pack(opcode=MjNOTICESERVERQUIT, length=0x20,
                       src=janwire.DST_SERVER & 0xFF,
                       dst=janwire.DST_CHANNEL & 0xFF, sub=0)
    return rec[:HEADER_LEN] + bytes(0x20 - HEADER_LEN)


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

#: A BOT SEAT'S NAME, and it is a decision rather than a placeholder.
#:
#: The old code wrote `PLAYER` into seat 0 and `CPU1`..`CPU3` into the rest
#: whenever no real name was available, which made "this seat is a bot" and "we
#: failed to resolve a name" render identically -- and that is exactly how the
#: table came to show PLAYER/CPU1/CPU2/CPU3 for a run in which every name WAS
#: known. The two cases are now separate:
#:
#:     a bot seat        -> COM 1 / COM 2 / COM 3   (by seat, deliberate)
#:     no name resolved  -> the slot stays ALL ZERO, i.e. blank on screen
#:
#: so a blank row on the seat display is now a real signal (we had nobody to
#: name) instead of being hidden behind a friendly-looking constant.
BOT_NAME = "COM %d"

#: member id -> the handle name to draw. `accounts` is optional here in the same
#: way it is in responders.py, so a build without it degrades to a blank name
#: rather than to a wrong one.
_NAME_CACHE = {}


def display_name(member_id, nick=None):
    """What this member should be CALLED on screen.

    The wire identity is an opaque scrambled nick (`UELRIS73E`) and the auth band
    hands this module the literal string `auth-band` as its peer, so neither is a
    name a person would recognise. The handle is: `PS2Tester` is what the badge,
    the friend list and the sign-up wizard all show for member 8.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        mid = 0
    if mid <= 0:
        return "" if not nick or nick in ("-", "auth-band") else str(nick)
    if mid in _NAME_CACHE:
        return _NAME_CACHE[mid]
    name = ""
    if accounts is not None:
        try:
            db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB",
                                                 accounts.DEFAULT_DB))
            try:
                row = accounts.primary_handle_row(db, mid)
                name = str(row["handle_name"]) if row else ""
            finally:
                db.close()
        except Exception as e:                                  # pragma: no cover
            log("  display_name(%s): %s -- drawing a blank rather than a guess"
                % (mid, e))
            name = ""
    _NAME_CACHE[mid] = name
    return name


def member_room(member_id):
    """The id of the Jan room `member_id` is JOINed to per the registry, or 0.

    THE ROOM-RESOLUTION RULE (finding 30): the client can only reserve,
    start, list or configure a table while JOINed to exactly one room
    channel -- `sqMgCpEnterRoom` stores it in the single slot DAT_003f1884
    and roommain.c:146-176 waits on "IRC Room Part Complite" before the
    selector can pick another -- so that channel IS the room of every table
    request. `janlobby.member_room` does the lookup; this is the seam that
    hands it the registry.
    """
    if LIVE_ROOMS is None or janlobby is None or not member_id:
        return 0
    try:
        return int(janlobby.member_room_id(LIVE_ROOMS(), member_id) or 0)
    except Exception:
        return 0


def _table_for(member_id, wire_id=0):
    """The COMPOSITE table id a request from `member_id` naming `wire_id`
    (the 16-bit PTL row id, or 0) is about -- `janseats.resolve_table` with
    the asker's registry room. The one call every arm makes."""
    if janseats is None:
        return int(wire_id or 0)
    return janseats.resolve_table(member_id, wire_id, room=member_room(member_id))


def _tl(tid):
    """`table 1 (room 101)` for the log."""
    return janseats.table_label(tid) if janseats is not None else "table %s" % tid


def _lobby_seating(member_id, prefer=0):
    """(lobby table id, [(member, seat, name, polid)]) for whoever asks.

    ONE PRODUCER, because there are two callers and they were answering
    differently: `MjGAMESTART` was taught the seat store on 2026-09-04 and
    `MjMEMBERLISTREQ` was not, so the members panel went on drawing one human
    and three COMs at a table this server knew held two people. That is the
    same class of split the table lifecycle keeps producing -- fix the
    producer, not the caller.
    """
    if janseats is None or not janseats.enabled() or not member_id:
        return 0, []
    tid = prefer or janseats.table_of(member_id)
    if not tid:
        return 0, []
    members = janseats.seats_at(tid)
    # Only trust it if the asker is actually in it: a seat that expired out
    # from under them is not a table to rebuild from.
    if not any(m == member_id for m, _s, _n, _p in members):
        return 0, []
    return tid, members


def _member_names(ids, table=None):
    """The 64-byte name block, in seat order.

    WARNING: PASS `table`. Without it every seat falls through to the no-name path and
    the screen draws four blanks -- the `MjMEMBERLISTREQ` handler used to call
    this with one argument, which is why the seat display never once showed a
    real name however well the rest of the exchange worked.
    """
    out = bytearray(4 * NAME_SLOT)
    for s in range(4):
        nm = ""
        if table is not None:
            nicks = getattr(table, "nicks", None)
            if nicks and nicks[s]:
                nm = nicks[s]
                nm = nm.decode("cp932", "replace") if isinstance(nm, bytes) else str(nm)
            elif getattr(table, "bots", None) and s in table.bots:
                nm = BOT_NAME % s
        raw = nm.encode("cp932", "replace")[:NAME_SLOT]
        out[s * NAME_SLOT:s * NAME_SLOT + len(raw)] = raw
    return bytes(out)


#: One NUL. Spelled this way because a heredoc round trip turns an escape
#: into the byte itself and puts a real NUL in the source file.
_NUL = bytes(1)


def _seat_summary(ids, table=None):
    """`seat=id name` per seat, for the log.

    The names are the half nobody can see from the wire trace: a blank one at
    the LOCAL seat is the difference between "three COMs and me" and "I am
    watching four COMs play", and it costs one live run to find out which.
    """
    names = _member_names(ids, table)
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


def opname(op):
    return OPCODES[op] if 0 <= op < len(OPCODES) else "Mj Non Defind...(%d)" % op


def ack_for(req, result=None, extra=0):
    """Build the ACK the client's own gate will accept for `req`, or None.

    Every gate read so far wants the same four things: the right opcode, arrival
    on queue 5, the result code in rec[0x18], and (for two of them) rec[0x13]
    equal to the correlation tag the client is holding -- echoed VERBATIM,
    never masked (the master-cmd family carries 0x20 in it; see the note above).
    `extra` (rec[0x19]) is stored by the client but its meaning is not yet read,
    so it defaults to 0 rather than to a guess.

    MEASURED: the field layout, the queue, the opcode and the result ladders.
    INFERENCE: the request->ACK pairing. It is solid for 31->32 and 29->30
    (adjacent opcodes, matching names) and weaker for the master commands and
    MjPLAYREQ->MjRESERVEACK, where several requests plausibly share one ACK.
    A live client disagreeing is data, not a bug to paper over.
    """
    h = janwire.unpack(req)
    ack = ACK_FOR.get(h["opcode"])
    if ack is None:
        return None
    if result is None:
        result = RESERVE_RESULT if ack == MjRESERVEACK else TMCMD_RESULT
    rec = bytearray(janwire.pack(
        opcode=ack,
        f13=h["f13"],                           # the correlation tag, echoed VERBATIM
        src=janwire.DST_SERVER & 0xFF,
        dst=janwire.DST_REPLY & 0xFF,
        sub=ACK_SUB,                            # = the sub-code the gate drains on
        id8=h["payload"],
    ))
    rec[0x18] = result & 0xFF
    rec[0x19] = extra & 0xFF
    return bytes(rec)


def result_name(ack_opcode, code):
    table = RESERVE_RESULTS if ack_opcode == MjRESERVEACK else TMCMD_RESULTS
    return table.get(code, "?")


def _stamp():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(msg, channel="janhourou"):
    line = "%s [%s] %s" % (_stamp(), channel, msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, channel + ".log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def hexdump(data, limit=256):
    out = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        out.append("    %04x  %-47s  %s" % (
            off, " ".join("%02x" % b for b in chunk),
            "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)))
    if len(data) > limit:
        out.append("    ... %d more bytes" % (len(data) - limit))
    return "\n".join(out)


def describe(rec):
    h = janwire.unpack(rec)
    dst = {-1: "CHANNEL", -2: "REPLY(+0x08)", -3: "SERVER"}.get(h["dst"], str(h["dst"]))
    # The TGM_NOTICE_* names are a seven-entry enum sitting next to the eight
    # MjNOTICE* opcodes, so the mapping is an inference -- annotate only on a
    # notice opcode, and mark it as unconfirmed rather than asserting it.
    note = ""
    if 14 <= h["opcode"] <= 21 and h["sub"] < len(NOTICES):
        note = "  sub?=%s" % NOTICES[h["sub"]]
    return ("%s  len=%d src=%d dst=%s f13=%d f16=%d sub=%d payload=%016x%s"
            % (opname(h["opcode"]), h["length"], h["src"], dst,
               h["f13"], h["f16"], h["sub"], h["payload"], note))


# One manager for the whole process: a member's table has to survive across
# lines, and the auth band hands us one line at a time with no session object of
# our own. Keyed by member id, so two consoles get two tables.
GAMES = jangame.Manager() if jangame is not None else None
if GAMES is not None:
    # the web board's watch rule (defined below; resolved at call time)
    GAMES.watchable = lambda tid: web_watchable(tid)
if jangame is not None:
    # Route jangame's human-readable per-move narration into this channel, so a
    # gameplay event shows up as BOTH the wire record and a plain-English move.
    jangame.TRACE = lambda m: log(m)


# --- THE LOBBY PUSH QUEUE (2026-09-04) ---------------------------------------
#
# Records for a member who is NOT the one talking: a kick notice, a promotion,
# a chat line, a table-mate's "has entered". The game manager has its own
# outbox for in-game records; this is the same idea for the lobby layer, kept
# as ENCODED LINES because chat rides the 'A' form and everything else the
# 'B' form. `take_pending` drains it first, so it reaches the member by every
# route the game outbox does: their next game-band line, their `<DR>` poll
# (responders._jan_pending_lines), and the idle push.
_PUSH_LOCK = threading.RLock()
_PUSH = {}                      # member id -> [encoded line, ...]
PUSH_QUEUE_MAX = 64


def push_line(member_id, line, why=""):
    """Queue one already-encoded line for `member_id`."""
    try:
        member_id = int(member_id or 0)
    except (TypeError, ValueError):
        return False
    if member_id <= 0 or not line:
        return False
    with _PUSH_LOCK:
        q = _PUSH.setdefault(member_id, [])
        q.append(bytes(line))
        del q[:-PUSH_QUEUE_MAX]
    if why:
        log("   queued for member %d: %s" % (member_id, why))
    return True


def push_record(member_id, rec, why=""):
    """Queue a record (chat opcodes take the 'A' form, the rest 'B')."""
    return push_line(member_id, janwire.line_for(rec), why=why)


def _table_members(tid):
    """[(member, seat, name, polid)] at a lobby table, or []."""
    if janseats is None or not janseats.enabled() or not tid:
        return []
    try:
        return janseats.seats_at(tid)
    except Exception:
        return []


def _gallery_members(tid):
    """[(member, slot, name, polid)] watching a lobby table, or []."""
    if (janseats is None or not janseats.enabled() or not tid
            or not hasattr(janseats, "gallery_at")):
        return []
    try:
        return janseats.gallery_at(tid)
    except Exception:
        return []


def push_table(tid, rec_or_line, exclude=(), why="", gallery=False):
    """Queue for every seated human at lobby table `tid` (bots have no
    socket) -- and, with `gallery`, for every spectator of it: only for an
    "everyone at the table" intent (chat, the roster handshake), never for
    the seat-shaped notices (MjNOTICEMEMBER names four seats a spectator
    does not hold). `exclude` is member ids to skip. Returns how many got
    it."""
    line = (rec_or_line if isinstance(rec_or_line, (bytes, bytearray))
            and rec_or_line[:1] in (b"A", b"B") else janwire.line_for(rec_or_line))
    n = 0
    seen = set()
    for m, _st, _nm, _pid in _table_members(tid):
        seen.add(m)
        if m in exclude:
            continue
        if push_line(m, line):
            n += 1
    if gallery:
        for m, _slot, _nm, _pid in _gallery_members(tid):
            if m in exclude or m in seen:
                continue
            seen.add(m)
            if push_line(m, line):
                n += 1
    if why and n:
        log("   -> %s queued for %d member(s) of table %s" % (why, n, tid))
    return n


def _take_pushed(member_id, limit=None):
    with _PUSH_LOCK:
        q = _PUSH.get(int(member_id or 0))
        if not q:
            return []
        if limit is None or limit >= len(q):
            out, q[:] = list(q), []
        else:
            out, q[:] = q[:limit], q[limit:]
        return out


def pushed_count(member_id=None):
    with _PUSH_LOCK:
        if member_id is not None:
            return len(_PUSH.get(int(member_id), []))
        return sum(len(q) for q in _PUSH.values())


def handle_line(line, peer="-", member_id=0):
    """Decode one line, and hand back anything queued for this member first.

    WARNING: THIS WRAPPER IS THE SECOND HUMAN'S ONLY WAY IN. The transport is
    reply-only: a record leaves on the socket of whoever just spoke, so a hand
    could only ever be played by the one player the server happened to be
    answering. Everything for another seat waits in `Table.outbox` and rides
    out on that member's next line -- and they always have one, because an
    in-game client re-sends its last ack about every 2 s and a client still in
    the room polls `<DR>` on the same cadence. Neither needs anything from us
    to keep talking, which is what makes a reply-only transport enough.

    Queued records go FIRST. They are older than whatever this line is about,
    and the client dedups on the sequence byte, so order is the whole contract.
    """
    out = _handle_line(line, peer=peer, member_id=member_id)
    queued = take_pending(member_id, peer=peer)
    if not queued:
        return out
    if out is None:
        rest = []
    elif isinstance(out, (list, tuple)):
        rest = list(out)
    else:
        rest = [out]
    log("%s   -> %d queued record(s) for member %s ride this line"
        % (peer, len(queued), member_id))
    return queued + rest


#: Set by jantitle to `_jan_peer_is_2004` -- "the client on THIS thread runs
#: Janhourou 20040727_2". It is per-thread, so it can only be asked while
#: answering that client, which is exactly when a record is turned into bytes.
#: Unset (this module serving on its own), every client gets the 2002 shape.
IS_2004 = None

INGAME_2004 = os.environ.get("POL_JAN_INGAME_2004", "1") == "1"


def _peer_is_2004(peer="-"):
    if janmsgs2004 is None or not INGAME_2004 or IS_2004 is None:
        return False
    try:
        return bool(IS_2004())
    except Exception as e:                                      # never fatal
        log("%s   build check raised: %s -- serving the 2002 shape" % (peer, e))
        return False


def _table_scores_names(member_id):
    """`(scores, names)` for the table `member_id` sits at, for MjTSUMO's two
    new 2004 fields. Both are drawn on the 2004 score plates, and neither
    exists in the 2002 record, so they come from the table rather than from
    the record being converted."""
    scores, names = (0, 0, 0, 0), (b"", b"", b"", b"")
    if GAMES is None:
        return scores, names
    try:
        t = GAMES.by_member.get(int(member_id))
        if t is None:
            return scores, names
        k = getattr(t, "kyoku", None)
        if k is not None and getattr(k, "scores", None):
            scores = tuple(int(v) for v in list(k.scores)[:4])
        names = tuple((n or "").encode("cp932", "replace")[:15]
                      for n in t._sashiuma_names())
    except Exception:                                           # never fatal
        pass
    return scores, names


def to_peer_build(recs, member_id=0, peer="-"):
    """Every record in `recs`, in the shape the client being answered reads.

    The 2002 build takes them as janmsgs built them. For the 2004 build,
    MjHAIPAI loses four bytes, MjTSUMO gains the scores and the four seat
    names, MjALLDATA gains the yakitori byte, and the four per-seat bytes
    inside all of them flip meaning (2002 1 = human, 2004 non-zero = COM).
    See janmsgs2004 for the addresses behind each move.
    """
    if not recs or not _peer_is_2004(peer):
        return recs
    out, touched = [], []
    for r in recs:
        try:
            op = r[0x12]
            if op == 36:                                        # MjTSUMO
                scores, names = _table_scores_names(member_id)
                new = janmsgs2004.tsumo_to_2004(r, scores=scores, names=names)
            elif op == 20:                                      # GAMESETUP
                _scores, names = _table_scores_names(member_id)
                new = janmsgs2004.notice_gamesetup_to_2004(r, names=names)
            else:
                new = janmsgs2004.to_2004(r)
            if new is not r and bytes(new) != bytes(r):
                touched.append("%s %dB->%dB" % (opname(op), len(r), len(new)))
            out.append(new)
        except Exception as e:                                  # never fatal
            log("%s   2004 conversion of %s raised: %s -- sending the 2002 "
                "shape" % (peer, opname(r[0x12]), e))
            out.append(r)
    if touched:
        log("%s   2004 layout: %s" % (peer, ", ".join(touched)))
    return out


def take_pending(member_id, peer="-", limit=None):
    """Encoded records waiting for this member, and clear them.

    WARNING: EXPOSED BECAUSE THE GAME BAND IS NOT THE ONLY TICK. A player still
    sitting in the room does NOT send game-band lines -- their ~2s heartbeat is
    the class-L `<DR>` poll, which never reaches `handle_line` at all. Queueing
    the game-start trio for them and draining it only here meant it was never
    delivered: measured live 2026-09-04, "queued the game-start trio for member
    6 at seat 1" with no matching delivery, and P2 sat in the lobby watching a
    table it was seated at start without it. `responders` drains this from the
    `<DR>` path too.
    """
    if not member_id:
        return []
    out = _take_pushed(member_id, limit=limit)
    if limit is not None:
        limit -= len(out)
        if limit <= 0:
            return out
    if GAMES is None:
        return out
    try:
        out += [janwire.encode(r)
                for r in to_peer_build(
                    GAMES.pending_for_member(member_id, limit=limit),
                    member_id=member_id, peer=peer)]
    except Exception as e:                                      # never fatal
        log("%s   pending drain raised: %s -- dropping nothing, retrying on "
            "the next line" % (peer, e))
    return out


def _handle_line(line, peer="-", member_id=0):
    """Decode one line and return the reply -- None, one line, or a LIST.

    A list is not an optimisation: once a hand is running, one inbound record
    routinely produces several outbound ones (the three non-human seats play in
    between), so the caller must be able to send more than one. `_game_notice_
    reply` in responders.py already does this for Tetra Master; the Janhourou
    branch was extended to match.

    Silence is still the default for anything we cannot yet speak: a wrong
    answer teaches us nothing, whereas a logged unknown is a lead.
    """
    tag = line[:1]
    if tag == b"A":
        try:
            h = janwire.parse_chat_line(line)
        except ValueError as e:
            log("%s   malformed 'A' line (%s): %r" % (peer, e, line[:120]))
            return None
        log("%s   <- %s  text=%r extra=%r"
            % (peer, opname(h["opcode"]), h["text"], h["extra"]))
        _relay_chat(line, h["opcode"], member_id, peer)
        return None
    if tag != b"B":
        log("%s   UNKNOWN class tag %r -- not one of ours; %d bytes:\n%s"
            % (peer, tag, len(line), hexdump(line)))
        return None

    rec = janwire.decode(line)
    # 24, not 32: the client's own ack sender (console__00284380) declares 0x18
    # and sends a header only, so every MjHAIPAIACK / MjSEISANACK / MjBYE is a
    # 24-byte record. A 32-byte floor here silently swallowed the entire ack
    # half of the protocol -- which nothing had noticed, because no client had
    # ever been given a message worth acking.
    if len(rec) < janwire.HEADER_LEN:
        log("%s   short record (%d bytes) from %r" % (peer, len(rec), line[:80]))
        return None
    h = janwire.unpack(rec)
    log("%s   <- %s" % (peer, describe(rec)))
    if h["body"]:
        log("%s      body %d bytes:\n%s" % (peer, len(h["body"]), hexdump(h["body"])))

    if janseats is not None and janseats.enabled() and member_id:
        # Any line from a member is proof they are alive; a seat whose member
        # goes silent past POL_JAN_SEAT_TTL_S expires (crash/power-off safety,
        # the lesson of tm-reservation-heartbeat -- Jan's client sends no
        # explicit heartbeat we have measured, so "still talking" is the lease).
        janseats.touch(member_id)
        # WARNING: AND THE COUNTERPART `set_in_play` NEVER HAD. Nothing in the tree
        # called `clear_in_play` -- it had one caller, its own selftest -- so a
        # table went "in play" at the first Start Game and STAYED there. The
        # TTL cannot save it either, and deliberately so: `touch` above renews
        # the in-play mark on every line precisely so a long hanchan does not
        # time out and let somebody reserve into a running game. That makes the
        # renewal correct and the missing clear the whole bug.
        #
        # Ask the thing that actually knows. `state == "over"` is the hanchan
        # ending (jangame sets it with the final MjGAMEEND) -- NOT
        # `!= "playing"`, which is also true in the gap between MjGAMESTART and
        # the first MjREADY and would wipe the mark we had just set.
        #
        # Reported live 2026-09-04: "table 1 already shows as being in-game
        # with one player" on walking into the room.
        if GAMES is not None:
            _t = GAMES.table_for(member_id, create=False)
            if _t is not None and getattr(_t, "state", None) == "over":
                _tid = janseats.table_of(member_id)
                if _tid and janseats.is_in_play(_tid):
                    janseats.clear_in_play(_tid)
                    log("%s      the hanchan is over -- table %s is joinable "
                        "again" % (peer, _tid))

    if h["opcode"] == janwire.MjTGMPING:
        reply = janwire.pong_for(rec)
        log("%s   -> %s" % (peer, describe(reply)))
        return janwire.encode(reply)

    if h["opcode"] == MjMEMBERLISTREQ and MEMBERLIST_ENABLE:
        # MjMEMBERLISTREQ (5) -> MjNOTICEMEMBER (14). Seen live 2026-08-13, sent
        # just before the personal-rankings request and unanswered, so it is a
        # candidate prerequisite for that screen.
        #
        # The pairing is the notice dispatcher's, not a guess: 0x002c6384 maps
        # opcode 14 to bit 1 = TGM_NOTICE_MEMBER_LIST, and the gate that drains
        # notices (0x002c6374, labelled "Receive Notice") reads **sub-code 0** --
        # which is exactly what member_list_notice() already emits. Its field
        # offsets were measured off the client's own loads at 0x002c63f0.
        #
        # Four ids, for the same reason the game-start notice sends four: the
        # client's player count is the number of NON-ZERO ids here. +0x50 is
        # Ready/Away per seat (ReturnRoom.c:343/358) -- every seat of a full
        # table is occupied, so all four are 1 -- and +0x54 the MASTER's seat
        # (mahdisp.c:57). The four floats at +0x20 are still unread.
        master_seat = 0
        if GAMES is not None:
            who = display_name(member_id, peer)
            _lobby, _members = _lobby_seating(member_id)
            if _members:
                # LOOK, do not rebuild: this panel opens while a hand may be
                # running, and `reset=True` would clear the game.
                table = GAMES.table_for_lobby(_lobby, _members, who, reset=False)
                master_seat = _master_seat_of(_lobby)
                log("%s      member list for lobby table %s: %s"
                    % (peer, _lobby,
                       ", ".join("%d=%s" % (st, nm or m)
                                 for m, st, nm, _p in _members)))
            elif (hasattr(GAMES, "spectator_table")
                  and GAMES.spectator_table(member_id) is not None):
                # A SPECTATOR asking (its client only builds this from the
                # room loop, roommain.c:150, but the Kansen menu thread is
                # not decompiled): answer from the table it WATCHES and never
                # seat it there -- the fallback below would put it at seat 0
                # of a running game.
                table = GAMES.spectator_table(member_id)
                master_seat = _master_seat_of(getattr(table, "lobby_key", 0))
            else:
                table = GAMES.table_for(member_id, who)
                table.seat_player(h["payload"], who, seat=0, member=member_id)
                table.fill_with_bots()
            ids = table.member_ids()
        else:
            table, ids = None, (h["payload"], 0, 0, 0)
        # `table` -- NOT omitted. This call site used to pass ids only, so every
        # seat took the no-name path however well the seating had worked.
        # +0x54 is the MASTER's seat (mahdisp.c:57, finding 25) -- it was the
        # recipient's own seat, which promoted every guest who opened the
        # panel. +0x50 is Ready per seat: every seat here is occupied.
        reply = member_list_notice(ids=ids, a=RESULT_OK, b=0, seat=master_seat,
                                   flags=(1, 1, 1, 1),
                                   tail=_member_names(ids, table))
        log("%s   -> %s  [%dB, %d members]  %s"
            % (peer, describe(reply), len(reply), len([s for s in ids if s]),
               _seat_summary(ids, table)))
        return janwire.encode(reply)

    if h["opcode"] == MjCHECKSAVEDATA:
        reply = checksavedata_ack(rec)
        if reply is None:
            log("%s      MjCHECKSAVEDATA received but POL_JAN_SAVEDATA=0 -- silent"
                % peer)
            return None
        log("%s   -> %s  [+0x18=%d -> %s]"
            % (peer, describe(reply), reply[0x18],
               "IM UD Check Success" if reply[0x18] == 1 else
               "IM UD Check Failed -- the client will RESEND"))
        return janwire.encode(reply)

    if h["opcode"] == EmISEVENT:
        reply = emisevent_ack(rec)
        if reply is None:
            log("%s      EmISEVENT received but POL_JAN_ISEVENT=0 -- silent" % peer)
            return None
        log("%s   -> %s  [%dB, sub=%d, +0x94=%d -> %s]"
            % (peer, describe(reply), len(reply), ISEVENT_SUB, reply[0x94],
               "event tab ON" if reply[0x94] == 1 else "no event"))
        return janwire.encode(reply)

    if h["opcode"] == MjGETLNDV:
        reply = getlndv_ack(rec)
        if reply is None:
            log("%s      MjGETLNDV received but POL_JAN_LNDV=0 -- staying silent"
                % peer)
            return None
        gm = struct.unpack_from("<I", reply, 0x30)[0] if len(reply) >= 0x34 else 0
        log("%s   -> %s  [%dB, sub=%d (gate drains on %d, buckets to fifo %d), "
            "GM version +0x30=%#010x%s]"
            % (peer, describe(reply), len(reply), LNDV_SUB, LNDV_DRAIN_SUB,
               queue_for_sub(LNDV_SUB), gm,
               "" if gm == GM_VERSION else "  *** != 0x20020227, the client will "
               "log 'ERROR:GM Version' and quit ***"))
        return janwire.encode(reply)

    # --- the in-game conversation ------------------------------------------
    # Everything from MjREADY to MjGAMEEND belongs to the game manager. It is
    # the only branch here that keeps state between lines, and the only one that
    # can answer with several records at once.
    if GAMES is not None and h["opcode"] in jangame.INGAME_OPCODES:
        try:
            out = GAMES.handle(rec, member_id=member_id,
                               nick=display_name(member_id, peer))
        except Exception as e:
            log("%s   jangame raised on %s: %s -- staying silent rather than "
                "guessing" % (peer, opname(h["opcode"]), e))
            return None
        if not out:
            log("%s      %s: nothing to send (the table is waiting on another "
                "seat)" % (peer, opname(h["opcode"])))
            return None
        for r in out:
            log("%s   -> %s" % (peer, describe(r)))
        return [janwire.encode(r) for r in
                to_peer_build(out, member_id=member_id, peer=peer)]

    # --- reserve / cancel, against the SEAT STORE (2026-09-02) --------------
    # WARNING: THE TABLE ID IS `id8` (+0x08), NOT `payload` (+0x18). Corrected
    # 2026-09-03 from a decoded live record -- this one field is why every
    # reservation went into a table nobody was looking at.
    #
    #   objstrings__002ca990(rec_src, DAT_00445960, table_id, label) builds
    #   MjPLAYREQ as: +0x08 = its THIRD argument, +0x18 = *rec_src. The call
    #   site (TableMenuPopup__0034f2f0) passes ctx+0x1d8 as rec_src and
    #   *(u64*)(ctx+0xa0) as the third argument, and ctx+0xa0 is the table id
    #   -- TableMenu__0034e9f0 stores exactly that word into _DAT_00446510,
    #   the my-table global the menu compares against. ctx+0x1d8 is the
    #   PLAYER: it lands in _DAT_004464e0, which the client prints as
    #   "PolID: %d". objstrings__002caab0 (MjPLAYCANCEL) has the same shape.
    #
    # Live proof (member 6 reserving table 1, 2026-09-03T22:35:34):
    #   id8=0000000000000001            <- our authored #MJS0T001
    #   payload=5b01e2bb73b3b60b        <- the <PD> PolID of LaptopTest2
    # The old code keyed the store off `payload`, so every client wrote its own
    # per-player record and ptl_blob's seat_count(1..4) always read 0: the row
    # never showed an occupant, "View table members" stayed greyed (its gate is
    # the record's +0x0c seat count), and two humans could never share a table.
    # WARNING: That last symptom is what jan-table-id-is-per-session recorded as an
    # id-space mismatch. There is no mismatch -- the ids it compared were the
    # two players' PolIDs, read out of the wrong field.
    # The extra byte rec[0x19] goes back as two nibbles (the client splits it:
    # DAT_004460e9 = extra >> 4, DAT_004460e8 = extra & 0xf): low = the seat we
    # granted (measured -- it becomes the seat stamped on every in-game
    # message), high = the master's seat (inference from lmenu's
    # save+0x3ca == save+0x3c9 master test). A master at seat 0 yields extra=0,
    # byte-identical to every reply the constant-result era sent live.
    if (h["opcode"] in (MjPLAYREQ, MjPLAYCANCEL) and janseats is not None
            and janseats.enabled() and member_id):
        # THE ROOM (finding 30): `id8` is the 16-bit row id every room's
        # PTL carries; the asker's registry room qualifies it, so Room 1-1
        # table 1 and Room 2-3 table 1 are two seat sets. See `_table_for`.
        tid = _table_for(member_id, h["id8"])
        if h["opcode"] == MjPLAYREQ:
            # `h["payload"]` is this client's PolID (+0x18) -- the id it
            # stamps on its own messages and the id MjNOTICEMEMBER's four seat
            # slots carry. The seat store keeps it so the GAME can seat this
            # player later without having to see another message from them.
            # +0x13 is the reserve-family tag a later kick must echo, and
            # +0x42 the voice bank the table-info blob serves for the seat.
            voice = (struct.unpack_from("<H", rec, PLAYREQ_VOICE_OFF)[0]
                     if len(rec) >= PLAYREQ_VOICE_OFF + 2 else None)
            face = (struct.unpack_from("<H", rec, PLAYREQ_FACE_OFF)[0]
                    if len(rec) >= PLAYREQ_FACE_OFF + 2 else None)
            # ENTRY LIMITS FIRST (opt-in, see `reserve_limits_unmet`): a refusal
            # must not move the member off a seat they hold elsewhere.
            unmet = reserve_limits_unmet(member_id, tid) if _reserve_limits_on() else 0
            if unmet:
                janseats.note_reserve_tag(member_id, h["f13"])
                result, seat, extra = RESERVE_LIMIT_RESULT, 0, unmet
                log("%s   member %s misses %s's entry limits (bits 0x%02X) -> "
                    "result 10" % (peer, member_id, _tl(tid), unmet))
            else:
                result, seat, mseat = janseats.reserve(
                    tid, member_id, display_name(member_id, peer),
                    polid=h["payload"], tag=h["f13"], voice=voice,
                                    face=face)
                extra = ((mseat & 0xF) << 4) | (seat & 0xF)
        else:
            janseats.note_reserve_tag(member_id, h["f13"])
            result, seat, extra = janseats.cancel(tid, member_id), 0, 0
        reply = ack_for(rec, result=result, extra=extra)
        r = janwire.unpack(reply)
        log("%s   -> %s  result=%d/%s  [%s: %s]"
            % (peer, describe(reply), reply[0x18],
               result_name(r["opcode"], reply[0x18]), _tl(tid),
               janseats.snapshot().get(str(int(tid)), (0, 0, False))))
        out = [janwire.encode(reply)]
        # A cancel by the master promoted somebody: tell the table.
        notify_master_changes(peer=peer)
        return out

    if (h["opcode"] in RESERVE_TAG_OPCODES and janseats is not None
            and janseats.enabled() and member_id):
        janseats.note_reserve_tag(member_id, h["f13"])

    if (h["opcode"] in (MjGALLEYREQ, MjGALLEYLEAVEREQ)
            and janseats is not None and member_id):
        return _galley(rec, h, member_id, peer)

    if (h["opcode"] in (MjLEAVEROOM, MjLEAVECONTENTS, MjLEAVEGAME)
            and janseats is not None and janseats.enabled() and member_id):
        # MjLEAVEROOM (22) / MjLEAVECONTENTS (24) / MjLEAVEGAME (29): the
        # member is walking away from wherever their seat was. 29's ACK still
        # comes from the generic table below. 22 and 24 are FIRE-AND-FORGET
        # -- roommain.c:146-160 sends MjLEAVEROOM (tagged off a different
        # counter, `DAT_00449050|0x10`, so no wait-state can even match it)
        # and falls straight into sqMgCpExitRoom(). Silence is the correct
        # answer, and it is now a chosen silence rather than "no responder".
        freed = janseats.release_member(member_id, why=opname(h["opcode"]))
        log("%s      released member %s's seat(s) on %s%s"
            % (peer, member_id, opname(h["opcode"]),
               " (table %d)" % freed if freed else ""))
        # A spectator walking out of the room/content: its gallery membership
        # goes with it (the client sends no MjGALLEYLEAVEREQ on this path).
        if GAMES is not None and hasattr(GAMES, "forget_spectator"):
            if GAMES.forget_spectator(member_id, opname(h["opcode"])) is not None:
                log("%s      ...and its gallery membership" % peer)
        elif hasattr(janseats, "gallery_drop"):
            janseats.gallery_drop(member_id)
        if h["opcode"] != MjLEAVEGAME:
            # A departing master promoted somebody: tell the survivors.
            notify_master_changes(peer=peer)
            return []

    if h["opcode"] == MjENTERROOM:
        # MEASURED 2026-09-09. The post-RESERVE room entry: roommain.c:109
        # (`wrsCpTgmEnterRoom Run`) -> objstrings__002cae70, sent when the
        # client is back on the room screen of the table it just reserved
        # (TableMenuPopup case 1/2 = RESERVEACK 1 seated / 2 seated as master
        # set DAT_00445e30, and the table's room is the current room). Body:
        # +0x18 the table record's id (DAT_004464e0), +0x08 the row's +0x208
        # id, +0x13 = 0x10 | a 4-bit counter, sub 5. NOTHING drains a reply
        # -- the client goes straight on to MjMEMBERLISTREQ, which IS
        # answered -- and seating happened at MjPLAYREQ (janseats). So this
        # arm only names it instead of logging "no responder".
        log("%s      MjENTERROOM: back in the room of table %016x (id8 %016x, "
            "member %s) -- fire-and-forget, nothing to answer"
            % (peer, h["payload"], h["id8"], member_id))
        return []

    if h["opcode"] == MjMEMBERBANISH and janseats is not None and member_id:
        return _kick(rec, h, member_id, peer)

    if h["opcode"] == MjCHMASTER and janseats is not None and member_id:
        return _decline_master(rec, h, member_id, peer)

    if (h["opcode"] in (MjCHATMEMBERADD, MjCHATMEMBERDEL, MjCHATMEMBERACK,
                        MjCHATINFO) and member_id):
        return _chat_member(line, rec, h, member_id, peer)

    if h["opcode"] == MjTBLCONFALL and member_id:
        # THE RULES, BEFORE THE ACK (finding 2): the master's 33 choices are
        # stored so the next `b/g/MJSTableInfoSub` fetch and the next
        # `table_for_lobby` see them. The ack itself is unchanged.
        _store_rules(rec, h, member_id, peer)

    if (h["opcode"] == MjENTERGAME and janseats is not None
            and janseats.enabled() and member_id):
        # Rejoin (finding 29): a member re-entering a table whose game is in
        # play is answered Success (below) and FLAGGED, so the game manager
        # can resync their seat on the next line -- `janseats.rejoin_pending`.
        _tid = _table_for(member_id, h["id8"])
        if _tid and janseats.is_in_play(_tid) and janseats.mark_rejoin(member_id):
            log("%s      MjENTERGAME on a PLAYING table %s -- rejoin flagged "
                "for member %s (jangame resyncs on their next line)"
                % (peer, _tid, member_id))

    reply = ack_for(rec)
    if reply is not None:
        r = janwire.unpack(reply)
        log("%s   -> %s  result=%d/%s"
            % (peer, describe(reply), reply[0x18],
               result_name(r["opcode"], reply[0x18])))
        out = [janwire.encode(reply)]
        # MjGAMESTART: the master pressed Start Game, we said Success, and the
        # client parks in state 0x1f waiting to be TOLD the game is starting.
        # The two messages that carry it out of there are the member list and
        # the GAME_START notice (bit 8 of the notice dispatcher at 0x002c6384),
        # after which it runs sqMgCpEnterTable and the keyed
        # b/g/MJSTableInfoSub fetch, and then sends MjREADY.
        #
        # WARNING: INFERENCE, and the most load-bearing one in this file: nothing
        # measured states that these two follow the ACK, only that the client
        # cannot proceed without being told. If a live client ignores them,
        # POL_JAN_DRIVE=0 puts it back exactly as it was.
        if (h["opcode"] == MjGAMESTART and janseats is not None
                and janseats.enabled()):
            # +0x08 of MjGAMESTART is the table id: malloc__002c5220 puts its
            # SECOND argument there, and TableMenu__0034e8d0 passes
            # _DAT_00446510 -- the my-table global. (+0x18 is *param_1 =
            # &DAT_004464e0, the PolID again, same as MjPLAYREQ.)
            # WARNING: MEASURED ZERO LIVE 2026-09-03: the global is only stored on the
            # reserve-ACK arm (TableMenuPopup 0x34f3a0), and a Start Game that
            # follows any other path carries 0. So fall back to the table this
            # member is actually seated at -- the seat store knows, and it is
            # the more trustworthy answer in every case. Room-qualified
            # (finding 30) by `_table_for`.
            tid = _table_for(member_id, h["id8"])
            if tid:
                janseats.set_in_play(tid, member_id)
        if (h["opcode"] == MjGAMESTART and GAMES is not None
                and os.environ.get("POL_JAN_DRIVE", "1") == "1"):
            # FOUR NON-ZERO IDS, not one. The client counts the non-zero u64s at
            # +0x30 to get its seated-player count (malloc__002c67a0), so a table
            # of one human and three bots must announce four ids or the client
            # waits for players who are, as far as it knows, not there. Measured
            # after a live run sat on 「皆さんをお待ちしております」 with the
            # game-start pair otherwise accepted.
            who = display_name(member_id, peer)
            # WARNING: SEAT THE TABLE THE LOBBY SAYS EXISTS. `table_for` keys by
            # MEMBER, so two humans at one table got two Tables and each played
            # three bots -- proven live 2026-09-04, when a Start Game at a
            # table holding two people announced `0='PS2Tester' 1='COM 1'
            # 2='COM 2' 3='COM 3'` and the other human was shown a table it
            # could no longer touch. The seat store is the authority on who is
            # there and which seat each of them was given.
            _lobby, _members = _lobby_seating(
                member_id, prefer=_table_for(member_id, h["id8"]))
            if _members:
                # WARNING: A SECOND START GAME MUST NOT RESTART A RUNNING ONE.
                # Both seated players can end up holding a master menu (the
                # client caches its master flag until the next save fetch --
                # `lmenu__002b2ab0` is the only writer), so two MjGAMESTARTs
                # for one table is reachable in practice, not just in theory.
                # `reset=True` would call reset_for_new_game() and wipe the
                # wall, the hands and every outbox out from under a hand that
                # is already being played. `table_for_lobby` returns a
                # "playing" table untouched when reset is False.
                _running = (GAMES.by_lobby.get(int(_lobby)) is not None
                            and getattr(GAMES.by_lobby.get(int(_lobby)),
                                        "state", None) == "playing")
                table = GAMES.table_for_lobby(_lobby, _members, who,
                                              reset=not _running)
                if _running:
                    log("%s      table %s is ALREADY PLAYING -- re-driving the "
                        "notices for member %s rather than restarting the hand"
                        % (peer, _lobby, member_id))
                log("%s      seating lobby table %s from the store: %s"
                    % (peer, _lobby,
                       ", ".join("%d=%s" % (st, nm or m)
                                 for m, st, nm, _p in _members)))
            else:
                table = GAMES.table_for(member_id, who)
                # Start Game is the explicit "new game" signal. Clear any stale
                # per-hand state first -- a prior hand that errored out leaves
                # the table stuck in state "playing", and then the MjREADY that
                # follows never re-deals (reset_for_new_game's banner).
                table.reset_for_new_game()
                table.seat_player(h["payload"], who, seat=0, member=member_id)
                table.fill_with_bots()
            seats = table.member_ids()
            # +0x54 = the MASTER's seat (finding 25); +0x50 = every seat
            # Ready, which a full four-seat game is. A game started through
            # the member fallback has one human at seat 0 = the master.
            notice = member_list_notice(ids=seats, a=RESULT_OK, b=0,
                                        seat=_master_seat_of(_lobby) if _members else 0,
                                        flags=(1, 1, 1, 1),
                                        tail=_member_names(seats, table))
            start = janmsgs.notice_gamestart(1)
            # WARNING: AND THE ONE THAT ACTUALLY STARTS IT. Measured 2026-08-17 after a
            # live client accepted the pair above and then waited for ever:
            # TableSelectMain returns 1 (= enter the game) on DAT_004460f2, which
            # is set by notice bit 0x20 = MjNOTICEGAMESETUP (20). The flag
            # MjNOTICEGAMESTART (18) sets, DAT_004460f0, is written and NEVER
            # READ anywhere in the module -- so 18 alone is inert. 18 is kept
            # because SE presumably sends both and it costs nothing.
            setup = janmsgs.notice_gamesetup(2)
            log("%s   -> %s  [driving the table out of state 0x1f]  %s"
                % (peer, describe(notice), _seat_summary(seats, table)))
            log("%s   -> %s  [inert: its flag is never read]" % (peer, describe(start)))
            log("%s   -> %s  [THE TRIGGER: bit 0x20 -> DAT_004460f2 -> "
                "TableSelectMain returns 1]" % (peer, describe(setup)))
            # The 2004 build reads four seat names out of the setup notice
            # (janmsgs2004). The copies queued for the other seats below stay
            # in 2002 shape and are converted per recipient as they drain.
            out += [janwire.encode(_r) for _r in
                    to_peer_build([notice, start, setup],
                                  member_id=member_id, peer=peer)]
            # EVERY OTHER SEATED HUMAN NEEDS THIS EXACT TRIO, or their client
            # sits in the lobby watching a table it is seated at go "in play"
            # without it -- which is what was seen live as "being kicked".
            # They ride out on that member's next line (~2 s).
            for _m, _st, _nm, _pid in _members:
                if _m == member_id:
                    continue
                for _r in (notice, start, setup):
                    table.queue_for(_st, _r)
                log("%s      queued the game-start trio for member %s at seat "
                    "%d" % (peer, _m, _st))
        return out

    log("%s      (no responder for %s yet -- captured, not answered)"
        % (peer, opname(h["opcode"])))
    return None


# --- the table-social layer (2026-09-04) -------------------------------------

def _master_seat_of(tid):
    """The master's seat at lobby table `tid`, for MjNOTICEMEMBER +0x54."""
    if janseats is None or not janseats.enabled() or not tid:
        return 0
    try:
        ms = janseats.master_seat_of(tid)
    except Exception:
        ms = None
    return int(ms) if ms is not None else 0


def lobby_notice(tid):
    """MjNOTICEMEMBER for a table that is NOT in play: the seated humans'
    PolIDs by seat (an empty seat stays 0 -- the client's occupied count is
    the number of non-zero ids, and it picks the one-button "You are the new
    Table Master" for 1 and the Yes/No "Become the new Table Master?" for
    more, lobbydsp.c:172-179), +0x50 Ready for every occupied seat, +0x54 the
    master's seat, names in the 64-byte tail."""
    ids, flags, names = [0, 0, 0, 0], [0, 0, 0, 0], bytearray(4 * NAME_SLOT)
    for m, st, nm, pid in _table_members(tid):
        if not 0 <= st < 4:
            continue
        ids[st] = pid or m
        flags[st] = 1
        raw = (nm or display_name(m)).encode("cp932", "replace")[:NAME_SLOT]
        names[st * NAME_SLOT:st * NAME_SLOT + len(raw)] = raw
    return member_list_notice(ids=ids, a=RESULT_OK, b=0,
                              seat=_master_seat_of(tid), flags=flags,
                              tail=bytes(names))


def notify_master_changes(peer="-"):
    """Turn every promotion the seat store recorded into the notice that
    makes the promoted client say so. Returns the (table, member) pairs."""
    if janseats is None or not janseats.enabled():
        return []
    try:
        changes = janseats.take_master_changes()
    except Exception:
        return []
    for tid, new in changes:
        n = push_table(tid, lobby_notice(tid))
        log("%s   table %s: member %s is the new MASTER (seat %d) -- "
            "MjNOTICEMEMBER +0x54 queued for %d member(s)"
            % (peer, tid, new, _master_seat_of(tid), n))
    return changes


def warn_expiring_seats(peer="-"):
    """MjNOTICETIMEUPWARNING for seats about to age out (called from the
    `<DR>` path, the lobby's only regular tick). Inert on the client beyond a
    debug line; the seat store is the thing that actually expires them."""
    if janseats is None or not janseats.enabled():
        return 0
    n = 0
    try:
        for tid, member, left in janseats.expiring_seats():
            if push_record(member, timeup_notice(left)):
                n += 1
                log("%s   table %s: member %s's seat expires in %ds -- "
                    "MjNOTICETIMEUPWARNING queued" % (peer, tid, member, left))
    except Exception as e:
        log("%s   seat-expiry warning raised: %s" % (peer, e))
    return n


def broadcast_server_quit(peer="shutdown"):
    """MjNOTICESERVERQUIT to every seated member (graceful shutdown). Queued;
    the idle push has POL_SHUTDOWN_GRACE seconds to carry it."""
    if janseats is None or not janseats.enabled():
        return 0
    n = 0
    try:
        for tid in list(janseats.snapshot()):
            n += push_table(int(tid), serverquit_notice())
    except Exception as e:
        log("%s   server-quit broadcast raised: %s" % (peer, e))
    if n:
        log("%s   MjNOTICESERVERQUIT queued for %d member(s)" % (peer, n))
    return n


def _kick(rec, h, member_id, peer):
    """MjMEMBERBANISH (finding 14): free the target's seat, tell the table,
    and answer the check the kicker's client is actually polling.

    The target is the PolID at +0x28 (`malloc__002c4fb0`). Only the master
    may kick (the menu is master-only; a guest's request is acked and
    ignored). The reply MUST be MjRESERVEACK with +0x13 = the kicker's
    reserve-family tag -- see `janseats.reserve_tag` -- or the master sits on
    "Removing them from the table" for 120000 ticks. Result byte 5
    (ReserveCancel): the client tests only `!= 0`, and "cancel complete" is
    what happened.
    """
    tid = _table_for(member_id, h["id8"])
    target = (struct.unpack_from("<Q", rec, BANISH_TARGET_OFF)[0]
              if len(rec) >= BANISH_TARGET_OFF + 8 else 0)
    members = _table_members(tid)
    victim = next(((m, st, nm, pid) for m, st, nm, pid in members
                   if pid == target or m == target), None)
    is_master = bool(tid) and janseats.master_of(tid) == int(member_id)
    if not is_master:
        log("%s      MjMEMBERBANISH from member %s who is NOT master of table "
            "%s -- acked, nothing released" % (peer, member_id, tid))
    elif victim is None:
        log("%s      MjMEMBERBANISH names PolID %016x, nobody at table %s -- "
            "acked, nothing released" % (peer, target, tid))
    else:
        vm, vseat, vname, vpid = victim
        janseats.cancel(tid, vm)
        notice = banish_notice(kicked=vpid or vm, by=h["payload"],
                               kicked_name=(vname or "").encode("cp932", "replace"),
                               by_name=display_name(member_id, peer).encode(
                                   "cp932", "replace"))
        # Everyone left at the table sees the dialog; the victim's copy is
        # the one that un-seats them (+0x20 == their own PolID).
        n = push_table(tid, notice)
        n += 1 if push_record(vm, notice) else 0
        log("%s      KICK: master %s removed member %s (seat %d, PolID %016x) "
            "from table %s -- MjNOTICEBANISH queued for %d member(s)"
            % (peer, member_id, vm, vseat, vpid, tid, n))
    tag = janseats.reserve_tag(member_id)
    if tag is None:
        log("%s      WARNING: no reserve tag on record for member %s -- the kick ack "
            "carries 0 and the client's reserve check may not match it"
            % (peer, member_id))
        tag = 0
    reply = bytearray(ack_for(rec, result=janseats.RESERVE_CANCEL, extra=0))
    reply[0x13] = tag & 0xFF
    log("%s   -> %s  [MjRESERVEACK with the RESERVE tag %#x, not the banish's "
        "%#x -- wrsCpTableReserveCheck is what the kicker polls]"
        % (peer, describe(bytes(reply)), tag, h["f13"]))
    out = [janwire.encode(bytes(reply))]
    notify_master_changes(peer=peer)
    return out


def _decline_master(rec, h, member_id, peer):
    """MjCHMASTER (finding 24/8): the member offered the mastership answered
    No (lobbydsp.c:206-213 -- Yes sends nothing). Rotate to the next seat and
    say so; the ack is MjMASTERCMDACK and ONLY result 1 releases the client
    (lobbydsp__002b4dc0 loops on anything else)."""
    tid = _table_for(member_id, h["id8"])
    new = janseats.decline_master(tid, member_id) if tid else 0
    log("%s      MjCHMASTER: member %s declines table %s -- master is now %s"
        % (peer, member_id, tid, new or "nobody"))
    reply = ack_for(rec, result=1)
    log("%s   -> %s  result=1/Success" % (peer, describe(reply)))
    out = [janwire.encode(reply)]
    notify_master_changes(peer=peer)
    return out


def _gallery_limit(tid):
    """GALLEY_LIMIT for a table: the master's stored choice, else
    POL_JAN_GALLEY_LIMIT_DEFAULT, else the rule's shipped default (0)."""
    if janrules is None or not janrules.enabled():
        return GALLEY_LIMIT_DEFAULT if GALLEY_LIMIT_DEFAULT is not None else GALLEY_NO
    try:
        if not janrules.has_rules(tid) and GALLEY_LIMIT_DEFAULT is not None:
            return GALLEY_LIMIT_DEFAULT
        return int(janrules.values_for(tid).get("GALLEY_LIMIT", GALLEY_NO))
    except Exception:
        return GALLEY_NO


def web_watchable(tid):
    """May jan.example.com show this table? The rule (decided 2026-09-12):
    exactly the table creator's own choice -- GALLEY_LIMIT Yes (1) or "No chat
    only" (2) -- and never a password table (a web viewer has no password to
    give). It is `_galley`'s consent test for an in-game Watch, without the
    server-wide POL_JAN_GALLERY switch: that gates our in-game gallery
    feature, not the creator's consent. Default 0 = nothing is shown."""
    if not tid:
        return False
    try:
        if _gallery_limit(tid) not in (GALLEY_YES, GALLEY_NO_CHAT):
            return False
        if janrules is not None and janrules.enabled():
            if int(janrules.values_for(tid).get("LIMIT_PASS_WORD") or 0):
                return False
            if janrules.password_for(tid):
                return False
        return True
    except Exception:
        return False


#: MjGALLEYREQ's own fields (objstrings.c:2783-2830): the password the client
#: typed (char[17], always "" in this build -- the builder is called with
#: the empty string at 0x422d58 / 0x415bd8) and the member's name (char[16]).
GALLEYREQ_PASSWORD_OFF, GALLEYREQ_PASSWORD_LEN = 0x44, 0x11
GALLEYREQ_NAME_OFF, GALLEYREQ_NAME_LEN = 0x55, 0x10


def _cstr(rec, off, n):
    raw = bytes(rec[off:off + n]) if len(rec) > off else b""
    return raw.split(b"\0", 1)[0].decode("cp932", "replace")


def _galley(rec, h, member_id, peer):
    """MjGALLEYREQ / MjGALLEYLEAVEREQ: the gallery (spectators).

    A Watch is answered with MjRESERVEACK on the reserve gate (`ack_for`:
    sub 5, the tag echoed verbatim, result +0x18) and, for result 1, the
    gallery SLOT at +0x19 -- the one byte the client keeps (DAT_0042aba0,
    stamped on every MjGALLEYACK +0x14). Refusals, in order: 7 with the
    knob off or a member who holds a seat at that very table; 10 when the
    table's GALLEY_LIMIT is 0 (`_gallery_limit`); 9 when the master set a
    password (LIMIT_PASS_WORD, `janrules.password_for`) and the request's
    +0x44 does not match it (the client always sends "", so a password
    table refuses every Watch); 3 when the member is already in that
    gallery (and the stale membership is dropped, so the NEXT Watch is a 1
    -- the client only sends a GALLEYREQ from the room, i.e. after an exit
    we never saw); 8 when nothing is being played there (no in-play mark,
    no jangame table, or one that is over). Else 1: the seat store keeps
    the membership (`janseats.gallery_join`) and the game manager copies
    every record from the spectator's first MjGALLEYACK on
    (`jangame.Manager.add_spectator`).

    A leave is 5 when a membership was released (store or manager), 6
    otherwise -- the only two codes that wait accepts, and it never times
    out, so every MjGALLEYLEAVEREQ is answered.
    """
    tid = _table_for(member_id, h["id8"])

    def answer(result, why, slot=0):
        reply = ack_for(rec, result=result, extra=slot)
        log("%s   -> %s  result=%d/%s  [%s: %s]"
            % (peer, describe(reply), result, RESERVE_RESULTS.get(result, "?"),
               _tl(tid) if tid else "table ?", why))
        return [janwire.encode(reply)]

    if h["opcode"] == MjGALLEYLEAVEREQ:
        released = False
        if GAMES is not None and hasattr(GAMES, "forget_spectator"):
            released = GAMES.forget_spectator(member_id, "MjGALLEYLEAVEREQ",
                                              store=False) is not None
        if janseats is not None and janseats.enabled() and hasattr(janseats, "gallery_leave"):
            released = (janseats.gallery_leave(tid, member_id)
                        == janseats.GALLEY_LEFT) or released
        if released:
            return answer(GALLEY_LEFT, "left the gallery")
        return answer(janseats.GALLEY_LEFT_ERROR if janseats is not None else 6,
                      "was not in any gallery (6 = CancelError, the other code "
                      "the leave wait accepts)")

    # --- MjGALLEYREQ ---------------------------------------------------------
    if not GALLERY_ENABLE:
        return answer(GALLEY_REFUSED, "POL_JAN_GALLERY is off -- refused, the "
                                      "dialog closes")
    if (janseats is None or not janseats.enabled() or GAMES is None
            or not hasattr(janseats, "gallery_join") or not tid):
        return answer(GALLEY_REFUSED, "no seat store / game manager to watch "
                                      "through -- refused")
    if janseats.table_of(member_id) == tid:
        return answer(GALLEY_REFUSED, "member %s holds a SEAT at this table -- "
                                      "a player cannot also watch it" % member_id)
    limit = _gallery_limit(tid)
    if limit == GALLEY_NO:
        return answer(janseats.GALLEY_LIMITED,
                      "GALLEY_LIMIT is 0/No%s -- 'Entry limits not met'"
                      % ("" if GALLEY_LIMIT_DEFAULT is None or
                         (janrules is not None and janrules.has_rules(tid))
                         else " (POL_JAN_GALLEY_LIMIT_DEFAULT=%d)" % GALLEY_LIMIT_DEFAULT))
    # The password: LIMIT_PASS_WORD (rule index 25, No/Yes) switched on AND
    # the 17-byte text the master's MjTBLCONFALL carried at +0x5a (the same
    # width as the GALLEYREQ's +0x44 field -- `janrules.password_for`, by
    # inference). The client always sends "", so such a table refuses 9.
    password = ""
    if janrules is not None and hasattr(janrules, "password_for"):
        try:
            if int(janrules.values_for(tid).get("LIMIT_PASS_WORD", 0)):
                password = janrules.password_for(tid)
        except Exception:
            password = ""
    if password and _cstr(rec, GALLEYREQ_PASSWORD_OFF, GALLEYREQ_PASSWORD_LEN) != password:
        return answer(janseats.GALLEY_PASSWORD,
                      "the table has a password and the request carries %r"
                      % _cstr(rec, GALLEYREQ_PASSWORD_OFF, GALLEYREQ_PASSWORD_LEN))
    if janseats.gallery_of(member_id) == tid:
        janseats.gallery_drop(member_id)
        if hasattr(GAMES, "forget_spectator"):
            GAMES.forget_spectator(member_id, "re-Watch: stale membership dropped",
                                   store=False)
        return answer(janseats.GALLEY_DUP,
                      "already in this gallery -- 'Already spectating'; the stale "
                      "membership is dropped so the next Watch is a 1")
    t = GAMES.by_lobby.get(int(tid))
    if (not janseats.is_in_play(tid) or t is None
            or getattr(t, "state", None) in ("over", "finished")):
        return answer(janseats.GALLEY_NOT_PLAYING,
                      "nothing being played there (in_play=%s, table=%s) -- 'the "
                      "table status changed'"
                      % (janseats.is_in_play(tid),
                         getattr(t, "state", None) if t is not None else None))
    name = _cstr(rec, GALLEYREQ_NAME_OFF, GALLEYREQ_NAME_LEN) or display_name(member_id, peer)
    result, slot = janseats.gallery_join(tid, member_id, name, polid=h["payload"])
    if result != janseats.GALLEY_OK:
        return answer(result, "the seat store refused the gallery entry")
    GAMES.add_spectator(t, member_id, slot)
    log("%s      SPECTATOR: member %s (%s, PolID %#x) watches %s from gallery slot "
        "%d (GALLEY_LIMIT %d%s); copies start at its first MjGALLEYACK"
        % (peer, member_id, name, h["payload"], _tl(tid), slot, limit,
           ", chat suppressed" if limit == GALLEY_NO_CHAT else ""))
    return answer(janseats.GALLEY_OK, "spectator, slot %d" % slot, slot=slot)


def _store_rules(rec, h, member_id, peer):
    """Parse the master's MjTBLCONFALL into the rule store."""
    if janrules is None or not janrules.enabled():
        return False
    try:
        vals = janrules.parse_tblconfall(rec)
    except ValueError as e:
        log("%s      MjTBLCONFALL not parsed (%s) -- rules unchanged" % (peer, e))
        return False
    # +0x08 is the 16-bit wire id; the rule store keys by the composite
    # (finding 30), so qualify it with the master's room first.
    tid = _table_for(member_id, vals.get("table"))
    if not tid:
        log("%s      MjTBLCONFALL names no table and member %s has no seat -- "
            "rules not stored" % (peer, member_id))
        return False
    if (janseats is not None and janseats.enabled()
            and janseats.master_of(tid) not in (0, int(member_id))):
        log("%s      MjTBLCONFALL from member %s who is not master of table %s "
            "-- rules not stored" % (peer, member_id, tid))
        return False
    janrules.store(tid, vals, by=member_id)
    log("%s      RULES for table %s stored from member %s: %s"
        % (peer, tid, member_id, janrules.describe(vals)))
    return True


def _chat_table_of(member_id):
    """(table id, is_spectator) for a chat line's sender: the seat first,
    else the gallery (a spectator's MjISAY is plain MjISAY on the table
    channel, spec section 2 "Chat")."""
    if hasattr(janseats, "table_or_gallery_of"):
        try:
            return janseats.table_or_gallery_of(member_id)
        except Exception:
            pass
    return janseats.table_of(member_id), False


def _relay_chat(line, opcode, member_id, peer):
    """MjISAY / MjISAYGALLEY (finding 15): the 'A' line, verbatim, to every
    other human at the sender's table AND to its spectators. The receive
    loop (`sqFileAccess__003730d0`) drains sub-code 4 and reads +0x18 (name)
    and +0x28 (text) straight out of the record, so the sender's own line is
    exactly what the others need. A SPECTATOR's line goes the same way --
    unless the table's GALLEY_LIMIT is 2 ("No chat only"), when it is
    dropped here (the client does not enforce it)."""
    if janseats is None or not janseats.enabled() or not member_id:
        return 0
    tid, spectator = _chat_table_of(member_id)
    if not tid:
        log("%s      chat from member %s who holds no seat and watches nothing "
            "-- not relayed" % (peer, member_id))
        return 0
    if spectator and _gallery_limit(tid) == GALLEY_NO_CHAT:
        log("%s      %s from SPECTATOR %s at %s dropped: GALLEY_LIMIT is 2 "
            "(No chat only)" % (peer, opname(opcode), member_id, _tl(tid)))
        return 0
    n = push_table(tid, bytes(line), exclude=(int(member_id),), gallery=True)
    log("%s      %s%s relayed to %d table-mate(s)/spectator(s) at %s"
        % (peer, opname(opcode), " from a SPECTATOR" if spectator else "", n,
           _tl(tid)))
    return n


def _chat_member_ack(polid, name):
    """The 'G' record a peer answers an ADD with (sqFileAccess.c:246-258):
    len 0x30, src 0, dst -1, f16 4, sub 4, +0x18 its own id, +0x20 its
    16-byte name."""
    rec = janwire.pack(opcode=MjCHATMEMBERACK, length=0x30, src=0,
                       dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                       payload=polid)
    body = bytearray(0x30 - 0x20)
    raw = (name or "").encode("cp932", "replace")[:NAME_LEN]
    body[:len(raw)] = raw
    return rec[:0x20] + bytes(body)


def _chat_member(line, rec, h, member_id, peer):
    """MjCHATMEMBERADD/DEL/ACK and the 0x48 INFO line (finding 15).

    The client's own peers do two things with an ADD (sqFileAccess.c:
    100-190, 246-258): add the newcomer to their chat roster, and answer with
    a MjCHATMEMBERACK carrying THEIR id and name so the newcomer's roster
    fills. We are every peer at once: relay the record to the table-mates,
    and answer the ADD with one ACK per seated human. DEL, ACK and the INFO
    line ("%s has entered." / "has left.") are relayed as they are.
    """
    if janseats is None or not janseats.enabled():
        return None
    tid, spectator = _chat_table_of(member_id)
    if not tid:
        log("%s      %s from member %s who holds no seat and watches nothing "
            "-- dropped" % (peer, opname(h["opcode"]), member_id))
        return []
    # The roster handshake runs for spectators as for players (chat.c:61-70
    # -> 329-368, no DAT_0042aba8 in chat.c): a spectator's ADD reaches the
    # seated humans and the other spectators, and is answered with one ACK
    # per SEATED human (the other spectators' clients answer for themselves
    # when the relayed ADD reaches them, as any real peer does).
    n = push_table(tid, bytes(line), exclude=(int(member_id),), gallery=True)
    out = []
    if h["opcode"] == MjCHATMEMBERADD:
        for m, _st, nm, pid in _table_members(tid):
            if m == int(member_id):
                continue
            ack = _chat_member_ack(pid or m, nm or display_name(m))
            out.append(janwire.encode(ack))
        log("%s      %s%s relayed to %d table-mate(s)/spectator(s); answering "
            "with %d MjCHATMEMBERACK(s) on the seated humans' behalf"
            % (peer, opname(h["opcode"]), " from a SPECTATOR" if spectator else "",
               n, len(out)))
    else:
        log("%s      %s%s relayed to %d table-mate(s)/spectator(s)"
            % (peer, opname(h["opcode"]), " from a SPECTATOR" if spectator else "", n))
    return out


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = "%s:%d" % self.client_address
        log("%s connected" % peer)
        buf = b""
        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    i = min((buf.find(t) for t in (b"\r\n", b"\n") if t in buf),
                            default=-1)
                    if i < 0:
                        break
                    line, buf = buf[:i], buf[i:].lstrip(b"\r\n")
                    if not line:
                        continue
                    reply = handle_line(line, peer)
                    for r in ([reply] if isinstance(reply, bytes) else (reply or [])):
                        self.request.sendall(r + b"\r\n")
                if len(buf) > 65536:
                    log("%s   %d unterminated bytes -- not line framing?\n%s"
                        % (peer, len(buf), hexdump(buf)))
                    buf = b""
        except (ConnectionError, socket.timeout) as e:
            log("%s connection ended: %s" % (peer, e))
        log("%s disconnected" % peer)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host=HOST, port=PORT):
    """The standalone 51272 listener -- a CAPTURE HARNESS, not where games run.

    Every hanchan is played on the AUTH band inside `responders` (the client
    never dials this port for play; see jan-ingame-handler-is-authsess). The
    deadline sweeper that used to run here ticked a Manager that never held a
    table and logged "no peer to send it to" for records nobody was waiting
    for -- audit finding 8/33. Removed 2026-09-04: `jangame.Manager.tick` is
    swept by `Manager.handle` on the band the games are on.
    """
    srv = Server((host, port), Handler)
    log("listening on %s:%d (content id 3, gi003.pol.com) -- capture harness; "
        "games play on the auth band" % (host, port))
    srv.serve_forever()


def selftest():
    """Stand the server up on a loopback port and complete a ping exchange."""
    ok = True
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address

    ping = janwire.pack(opcode=janwire.MjTGMPING,
                        src=janwire.DST_REPLY & 0xFF,
                        dst=janwire.DST_SERVER & 0xFF,
                        payload=0x0123456789ABCDEF)
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(janwire.encode(ping) + b"\r\n")
        s.settimeout(5)
        got = s.recv(4096).strip()

    print("sent  %s" % janwire.encode(ping).decode("latin1"))
    print("got   %s" % got.decode("latin1"))
    if not got.startswith(b"B"):
        print("FAIL: reply is not a 'B' line"); ok = False
    else:
        h = janwire.unpack(janwire.decode(got))
        if h["opcode"] != janwire.MjTGMPONG:
            print("FAIL: replied %s, expected MjTGMPONG" % opname(h["opcode"])); ok = False
        if h["id8"] != 0x0123456789ABCDEF:
            print("FAIL: pong did not echo the ping's payload (%016x)" % h["id8"]); ok = False
        if (h["src"], h["dst"]) != (-3, -2):
            print("FAIL: pong src/dst = %d/%d, expected -3/-2" % (h["src"], h["dst"])); ok = False

    # Every request->ACK pair, against the gates the client actually applies.
    for req_op, want_ack in sorted(ACK_FOR.items()):
        req = janwire.pack(opcode=req_op, f13=0x1D, src=1,
                           dst=janwire.DST_SERVER & 0xFF, payload=0xCAFE)
        ack = ack_for(req)
        a = janwire.unpack(ack)
        why = "%s -> %s" % (opname(req_op), opname(want_ack))
        if a["opcode"] != want_ack:
            print("FAIL: %s produced %s" % (why, opname(a["opcode"]))); ok = False
        if a["f13"] != 0x1D:
            print("FAIL: %s tag %#x, expected 0x1d echoed verbatim"
                  % (why, a["f13"])); ok = False
        # The rule is equality with the gate's drain sub-code -- NOT a queue
        # lookup. A record whose sub is 7 lands in TABLE[7] = 0 = the outbound
        # FIFO and is transmitted straight back to the server; that is a real
        # observed failure, so assert against it explicitly.
        if a["sub"] != ACK_SUB:
            print("FAIL: %s sub %d, but the gate drains on sub %d"
                  % (why, a["sub"], ACK_SUB)); ok = False
        if queue_for_sub(a["sub"]) == 0:
            print("FAIL: %s sub %d buckets into the OUTBOUND fifo -- the client "
                  "will echo it back, not read it" % (why, a["sub"])); ok = False
        if result_name(want_ack, ack[0x18]) == "?":
            print("FAIL: %s result byte %d is not in its ladder"
                  % (why, ack[0x18])); ok = False
        if handle_line(janwire.encode(req)) is None:
            print("FAIL: %s went unanswered" % why); ok = False
    if RESERVE_RESULTS[2] != 'ReserveMaster' or TMCMD_RESULTS[3] != 'NoAuthority':
        print("FAIL: result ladders do not match the binary"); ok = False

    # MjNOTICEMEMBER: check the fields land where the client loads them
    ids = (0x1111111111111111, 0x2222222222222222, 0x3333333333333333, 0x4444444444444444)
    n = member_list_notice(ids=ids, vals=(1.0, 2.0, 3.0, 4.0), flags=(9, 8, 7, 6),
                           a=0xAAAA, b=0xBBBB, seat=2, tail=b"#JAN0001")
    nh = janwire.unpack(n)
    if nh["opcode"] != MjNOTICEMEMBER or nh["length"] != 0x95 or len(n) != 0x95:
        print("FAIL: notice header %r len %d" % (nh, len(n))); ok = False
    if nh["dst"] != -1:
        print("FAIL: a notice must go to the channel (dst -1), got %d" % nh["dst"]); ok = False
    if struct.unpack_from("<4Q", n, 0x30) != ids:
        print("FAIL: seat ids not at +0x30"); ok = False
    if struct.unpack_from("<4f", n, 0x20) != (1.0, 2.0, 3.0, 4.0):
        print("FAIL: floats not at +0x20"); ok = False
    if tuple(n[0x50:0x54]) != (9, 8, 7, 6) or n[0x54] != 2:
        print("FAIL: per-seat flags not at +0x50"); ok = False
    if n[0x55:0x5D] != b"#JAN0001":
        print("FAIL: tail block not at +0x55"); ok = False

    # THE NAME BLOCK. The bug this covers is not subtle and was live for days:
    # one of the two call sites passed no table, so every seat fell through to a
    # placeholder and the screen read PLAYER / CPU1 / CPU2 / CPU3.
    class _T(object):
        nicks = ["PS2Tester", None, None, None]
        bots = {1: object(), 2: object()}
    names = _member_names((1, 2, 3, 4), _T())
    slots = [names[i * NAME_SLOT:(i + 1) * NAME_SLOT].rstrip(bytes(1))
             for i in range(4)]
    if len(names) != 4 * NAME_SLOT:
        print("FAIL: the name block is %d bytes, not 64" % len(names)); ok = False
    if slots[0] != b"PS2Tester":
        print("FAIL: a seated player's own name is %r" % slots[0]); ok = False
    if slots[1] != b"COM 1" or slots[2] != b"COM 2":
        print("FAIL: bot seats are %r/%r" % (slots[1], slots[2])); ok = False
    if slots[3] != b"":
        print("FAIL: an unknown seat must stay BLANK (it is the one signal that "
              "says 'no name', and a friendly constant hides it); got %r"
              % slots[3]); ok = False
    if _member_names((1, 2, 3, 4)) != bytes(4 * NAME_SLOT):
        print("FAIL: with no table every slot must be blank, not a placeholder")
        ok = False
    if NOTICE_BIT[MjNOTICEMEMBER] != 1 or NOTICE_BIT[21] != 64:
        print("FAIL: notice bit map does not match the binary"); ok = False

    # MjNOTICEBANISH: two u64s then two 16-byte names. WARNING: THE KICKED PolID IS
    # +0x20 -- lobbydsp.c:164 compares notice+0x20 against the recipient's
    # own PolID to decide whether to un-seat itself. +0x18 is never read.
    bn = banish_notice(kicked=0xDEAD, by=0xBEEF, kicked_name=b"loser",
                       by_name=b"master")
    if len(bn) != 0x48 or janwire.unpack(bn)["length"] != 0x48:
        print("FAIL: banish length %d" % len(bn)); ok = False
    if struct.unpack_from("<Q", bn, 0x20)[0] != 0xDEAD:
        print("FAIL: the KICKED PolID must be at +0x20 (the one field the "
              "client compares against its own id)"); ok = False
    if struct.unpack_from("<Q", bn, 0x18)[0] != 0xBEEF:
        print("FAIL: the kicker's id goes at +0x18"); ok = False
    if bn[0x38:0x3D] != b"loser" or bn[0x28:0x2E] != b"master":
        print("FAIL: banish names not at +0x28 (by) / +0x38 (kicked)"); ok = False
    if len(bn[0x28:0x38]) != NAME_LEN:
        print("FAIL: name field is not 16 bytes"); ok = False
    tn = timeup_notice(90)
    if (janwire.unpack(tn)["opcode"] != MjNOTICETIMEUPWARNING
            or struct.unpack_from("<I", tn, 0x20)[0] != 90 or tn[0x18] != 0):
        print("FAIL: MjNOTICETIMEUPWARNING's rest time is +0x20 (malloc.c:1357), "
              "not +0x18"); ok = False
    sq = serverquit_notice()
    if janwire.unpack(sq)["opcode"] != MjNOTICESERVERQUIT or len(sq) != 0x20:
        print("FAIL: MjNOTICESERVERQUIT is a bare 0x20 notice"); ok = False

    # --- MjGETLNDV, against the REAL captured request ------------------------
    # Not a synthesised record: this is the live line from the PS2 client, so
    # the responder is exercised on exactly the bytes it will meet.
    LIVE_GETLNDV = b"B^@@@@@@@@@BESut|KnLAVr@@P@C}\x7f`@ElSjAg\x7fWb@Ul"
    req = janwire.decode(LIVE_GETLNDV)
    rh = janwire.unpack(req)
    if rh["opcode"] != MjGETLNDV or rh["length"] != 32:
        print("FAIL: captured opener is %s len %d, expected MjGETLNDV len 32"
              % (opname(rh["opcode"]), rh["length"])); ok = False
    ack = getlndv_ack(req)
    ah = janwire.unpack(ack)
    if ah["opcode"] != MjGETLNDVACK:
        print("FAIL: opener answered with %s" % opname(ah["opcode"])); ok = False
    if ah["f13"] != rh["f13"]:
        print("FAIL: LNDV ack did not echo the correlation tag verbatim"); ok = False
    # THE reason the first live attempt still black-screened: the ACK has to land
    # in queue 0, and rec[0x17] INDEXES the routing table rather than being the
    # queue. Assert the indirection, not the literal, so this cannot regress.
    if ah["sub"] != LNDV_DRAIN_SUB:
        print("FAIL: LNDV ack sub %d, but the gate at 0x002ca684 drains on sub %d"
              % (ah["sub"], LNDV_DRAIN_SUB)); ok = False
    if queue_for_sub(ah["sub"]) == 0:
        print("FAIL: LNDV ack sub %d buckets into the OUTBOUND fifo -- this is "
              "the bug that made the client echo our ACK back" % ah["sub"])
        ok = False
    if ah["id8"] != rh["payload"]:
        print("FAIL: LNDV ack must address the id the request named"); ok = False
    if (ah["src"], ah["dst"]) != (-3, -2):
        print("FAIL: LNDV ack src/dst = %d/%d, expected -3/-2"
              % (ah["src"], ah["dst"])); ok = False
    # the body the gate copies out: six fields, highest byte +0x36
    want_len = 0x40 if LNDV_DUAL else 0x38
    if len(ack) != want_len or ah["length"] != want_len:
        print("FAIL: LNDV ack is %d bytes / length field %d, expected %#x"
              % (len(ack), ah["length"], want_len)); ok = False
    # The 2004 build's version, past everything the 2002 build reads.
    if LNDV_DUAL and struct.unpack_from("<I", ack, 0x38)[0] != 0x20020603:
        print("FAIL: +0x38 = %#x, but the 2004 client demands 0x20020603 "
              "(0x002e77fc)" % struct.unpack_from("<I", ack, 0x38)[0]); ok = False
    probe = getlndv_ack(req, body=bytes(range(0x18, 0x38)))
    if struct.unpack_from("<Q", probe, 0x18)[0] != 0x1f1e1d1c1b1a1918:
        print("FAIL: body does not start at +0x18"); ok = False
    if probe[0x36] != 0x36:
        print("FAIL: body does not reach +0x36 (the last field the gate reads)")
        ok = False
    # The GM version at +0x30. Not cosmetic: a mismatch makes the client log
    # "ERROR:GM Version", return -5 and quit the game AFTER completing both
    # opener exchanges -- which reads like success right up until it isn't.
    if struct.unpack_from("<I", ack, 0x30)[0] != 0x20020227:
        print("FAIL: +0x30 = %#x, but the client demands 0x20020227 (0x002ca8e0)"
              % struct.unpack_from("<I", ack, 0x30)[0]); ok = False
    if len(janwire.encode(ack)) != 1 + janwire.enc_len(want_len):
        print("FAIL: the ack does not serialise to its own line length")
        ok = False
    # --- the EVENT flag, the one byte the client's Event tab is gated on ----
    _ev = janwire.pack(id8=0x11, length=40, opcode=EmISEVENT, src=4,
                       dst=janwire.DST_REPLY & 0xFF, f16=3, sub=5)[:0x18]
    _ev += (0x2222).to_bytes(8, "little") + (6).to_bytes(4, "little") + bytes(4)
    _keep = {k: os.environ.get(k) for k in
             ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
              "POL_JAN_ISEVENT_FLAG")}
    try:
        for _k in _keep:
            os.environ.pop(_k, None)
        os.environ["POL_JAN_EVENT_FORCE"] = "0"
        _a = emisevent_ack(_ev)
        if not (_a and len(_a) == 0x98 and _a[0x94] == 0 and _a[0x17] == 6):
            print("FAIL: with no event the ack is 0x98 bytes, sub 6, flag 0")
            ok = False
        os.environ["POL_JAN_EVENT_FORCE"] = "1"
        os.environ["POL_JAN_EVENT_ID"] = "9"
        os.environ["POL_JAN_EVENT_NAME"] = "Cup"
        _a = emisevent_ack(_ev)
        if not (_a and _a[0x94] == 1
                and struct.unpack_from("<I", _a, 0x4C)[0] == 9
                and _a[0x54:0x57] == b"Cup" and _a[0x57] == 0):
            print("FAIL: a running event sets the flag, the id at +0x4C and "
                  "the NUL-terminated name at +0x54")
            ok = False
        # POL_JAN_ISEVENT_FLAG is read at import, like every other knob here,
        # so the override is the module constant.
        global ISEVENT_FLAG
        _was = ISEVENT_FLAG
        try:
            ISEVENT_FLAG = 2
            if emisevent_ack(_ev)[0x94] != 2:
                print("FAIL: POL_JAN_ISEVENT_FLAG must still override the store")
                ok = False
        finally:
            ISEVENT_FLAG = _was
    finally:
        for _k, _v in _keep.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # --- MjCHECKSAVEDATA, against the REAL captured request ------------------
    # The second opener message, captured 2026-08-12 once the LNDV handshake
    # completed. Same treatment: drive the responder with the client's own bytes.
    LIVE_SAVEDATA = (b"BnP@@@@@@@@@@@@@@@@@@@C`@PpC}\x7f`@E@@@@@@@@@@"
                     b"BqNhF_}^HAVp@@@@@@@@@@@@@@@@@@@@@")
    sreq = janwire.decode(LIVE_SAVEDATA)
    sh = janwire.unpack(sreq)
    if sh["opcode"] != MjCHECKSAVEDATA:
        print("FAIL: captured second message is %s, expected MjCHECKSAVEDATA"
              % opname(sh["opcode"])); ok = False
    sack = checksavedata_ack(sreq)
    sa = janwire.unpack(sack)
    if sa["opcode"] != MjCHECKSAVEDATAACK:
        print("FAIL: answered with %s" % opname(sa["opcode"])); ok = False
    if sa["sub"] != SAVEDATA_DRAIN_SUB:
        print("FAIL: savedata ack sub %d, gate drains on %d"
              % (sa["sub"], SAVEDATA_DRAIN_SUB)); ok = False
    if queue_for_sub(sa["sub"]) == 0:
        print("FAIL: savedata ack buckets into the OUTBOUND fifo"); ok = False
    if sack[0x18] != 1:
        print("FAIL: savedata ack +0x18 = %d; anything but 1 makes the client "
              "resend forever (0x002ca8c0)" % sack[0x18]); ok = False
    if handle_line(LIVE_SAVEDATA) is None:
        print("FAIL: the captured MjCHECKSAVEDATA went unanswered"); ok = False

    if handle_line(LIVE_GETLNDV) is None:
        print("FAIL: the captured opener went unanswered -- this is the black "
              "screen"); ok = False

    # an opcode we deliberately do not answer must stay silent, not guess
    if handle_line(janwire.encode(janwire.pack(opcode=janwire.MjISAY + 34))) is not None:
        print("FAIL: answered a message we have not decoded"); ok = False

    # 72 from the client's own name table + 0x48, the undocumented chat INFO
    # line sqFileAccess sends and receives.
    # ... + 73..100, the 2004 build's additions.
    if len(OPCODES) != 101 or opname(0x48) != "MjCHATINFO" or opname(93) != "EmISEVENTACK":
        print("FAIL: opcode table is %d entries, expected 101 (0x48 = MjCHATINFO)"
              % len(OPCODES)); ok = False
    if ACK_FOR[MjMEMBERBANISH] != MjRESERVEACK:
        print("FAIL: a kick must be answered with MjRESERVEACK -- the kicker "
              "polls wrsCpTableReserveCheck (TableMemberBanish.c:153)"); ok = False

    # --- the seat store, driven through handle_line like the auth band does --
    if janseats is not None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            os.environ["POL_JAN_SEATS"] = "1"
            os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "seats.json")
            janseats._TABLES.clear()
            janseats._ADOPTED[0] = True
            janseats._OWNER[0] = True

            # WARNING: THE TABLE GOES IN `id8` (+0x08) AND THE PolID IN `payload`
            # (+0x18) -- that is the client's layout, and building it the other
            # way round is precisely the bug this exercises. `polid` defaults to
            # a value that is NOT a table id, so anything reading the wrong
            # field lands in a record no seat_count() ever asks about.
            def _ask(op, table, member, f13=0x11, polid=None):
                req = janwire.pack(opcode=op, f13=f13, src=1,
                                   dst=janwire.DST_SERVER & 0xFF, id8=table,
                                   payload=(0x5B01E2BB73B3B60B + member
                                            if polid is None else polid))
                out = handle_line(janwire.encode(req), member_id=member)
                if isinstance(out, (list, tuple)):
                    out = out[0] if out else None
                return janwire.decode(out) if isinstance(out, bytes) else None

            a = _ask(MjPLAYREQ, 1, 8)
            if a is None or a[0x18] != 2 or a[0x19] != 0:
                print("FAIL: first reserver must get result 2 extra 0 (the "
                      "master reply confirmed live), got %r/%r"
                      % (a and a[0x18], a and a[0x19])); ok = False
            b = _ask(MjPLAYREQ, 1, 16)
            if b is None or b[0x18] != 1 or b[0x19] != 0x01:
                print("FAIL: second reserver must get result 1 (guest) with "
                      "seat 1 in the low nibble, got %r/%r"
                      % (b and b[0x18], b and b[0x19])); ok = False
            c = _ask(MjPLAYCANCEL, 1, 16)
            if c is None or c[0x18] != 5:
                print("FAIL: cancel of a held seat must answer 5 "
                      "(ReserveCancel -- 'Reserve cansel Complite'), got %r"
                      % (c and c[0x18])); ok = False
            d = _ask(MjPLAYCANCEL, 1, 16)
            if d is None or d[0x18] != 6:
                print("FAIL: cancel with no seat must answer 6 "
                      "(ReserveCancelError), got %r" % (d and d[0x18])); ok = False
            # WARNING: THE REGRESSION GUARD. A reservation must land on the table
            # the PTL builder will ask about (1..4) and nowhere else -- reading
            # the PolID as the table is invisible on the wire (the client still
            # gets its ACK) and shows up only here.
            if janseats.seat_count(1) != 1:
                print("FAIL: table 1 holds %d seats, expected 1 -- the reserve "
                      "was keyed off the wrong field again"
                      % janseats.seat_count(1)); ok = False
            if list(janseats.snapshot()) != ["1"]:
                print("FAIL: the seat store grew tables %r; only '1' was ever "
                      "reserved" % list(janseats.snapshot())); ok = False
            if janseats.table_of(8) != 1:
                print("FAIL: table_of(8) = %r, expected 1 (the MjGAMESTART "
                      "fallback)" % janseats.table_of(8)); ok = False

            # WARNING: THE MEMBER LIST MUST ANSWER FROM THE SEAT STORE. It did not
            # until 2026-09-04: `MjGAMESTART` was taught the store and this was
            # not, so "View table members" went on drawing ONE human and three
            # COMs at a table the server knew held two. The live report was
            # simply "no change".
            if MEMBERLIST_ENABLE and GAMES is not None:
                # Put a second body back at the table: the cancels above left
                # member 8 alone, and one human is the case that always worked.
                _ask(MjPLAYREQ, 1, 16)
                _req = janwire.pack(opcode=MjMEMBERLISTREQ, f13=9, src=1,
                                    dst=janwire.DST_SERVER & 0xFF,
                                    id8=1, payload=0x5B01E2BB73B3B60B)
                _rp = handle_line(janwire.encode(_req), member_id=8)
                if isinstance(_rp, (list, tuple)):
                    _rp = _rp[-1] if _rp else None
                _rp = janwire.decode(_rp) if isinstance(_rp, bytes) else None
                if _rp is None or len(_rp) < 0x95:
                    print("FAIL: MjMEMBERLISTREQ went unanswered"); ok = False
                else:
                    _ids = [struct.unpack_from("<Q", _rp, 0x30 + 8 * i)[0]
                            for i in range(4)]
                    _nm = [_rp[0x55 + i * NAME_SLOT:
                               0x55 + (i + 1) * NAME_SLOT].split(b"\0", 1)[0]
                           for i in range(4)]
                    # members 8 and 16 hold seats 0 and 1 at table 1 by now
                    # Assert on the IDS, not the names: `display_name` reads
                    # the accounts DB and there is none in a selftest, so both
                    # humans legitimately draw blank here.
                    _want = {0x5B01E2BB73B3B60B + 8, 0x5B01E2BB73B3B60B + 16}
                    if not _want.issubset(set(_ids)):
                        print("FAIL: the member list carries %r -- both seated "
                              "humans' PolIDs must be in it, not one human and "
                              "three COMs"
                              % (["%016x" % i for i in _ids],)); ok = False
                    _coms = [n for n in _nm if n.startswith(b"COM")]
                    if len(_coms) != 2:
                        print("FAIL: %r -- exactly two seats are bots" % (_nm,))
                        ok = False
                    if len(set(_ids)) != 4 or not all(_ids):
                        print("FAIL: seat ids %r must be four distinct "
                              "non-zero values" % (["%016x" % i for i in _ids],))
                        ok = False
                    # +0x54 is the MASTER's seat (member 8 reserved first)
                    if _rp[0x54] != 0:
                        print("FAIL: the master sits at seat 0, +0x54 says %d"
                              % _rp[0x54]); ok = False
                    # ...and for the GUEST it is STILL the master's seat, not
                    # the guest's own (finding 25 -- the old code sent the
                    # recipient's seat, which is the change-master trigger).
                    _rp2 = handle_line(janwire.encode(_req), member_id=16)
                    if isinstance(_rp2, (list, tuple)):
                        _rp2 = _rp2[-1] if _rp2 else None
                    _rp2 = janwire.decode(_rp2) if isinstance(_rp2, bytes) else None
                    if _rp2 is None or _rp2[0x54] != 0:
                        print("FAIL: the guest's copy must carry the MASTER's "
                              "seat 0 at +0x54, got %r"
                              % (_rp2 and _rp2[0x54])); ok = False

            # without a member id the old constant behaviour must be untouched
            e = _ask(MjPLAYCANCEL, 1, 0)
            if e is None or e[0x18] != RESERVE_RESULT:
                print("FAIL: member_id 0 must keep the pre-store behaviour")
                ok = False

            # --- THE TABLE-SOCIAL LAYER (2026-09-04) -----------------------
            # members 8 (master, seat 0) and 16 (guest, seat 1) sit at table 1.
            _PUSH.clear()
            janseats.take_master_changes()

            # a full table is refused with 8, not 10
            _ask(MjPLAYREQ, 1, 21); _ask(MjPLAYREQ, 1, 22)
            f = _ask(MjPLAYREQ, 1, 99)
            if f is None or f[0x18] != 8:
                print("FAIL: a full table must answer 8 ('the table status "
                      "changed'), got %r" % (f and f[0x18])); ok = False
            _ask(MjPLAYCANCEL, 1, 21); _ask(MjPLAYCANCEL, 1, 22)
            _PUSH.clear()

            # the reserve tag is remembered (the kick reply carries it)
            _ask(MjPLAYREQ, 1, 8, f13=0x07)
            if janseats.reserve_tag(8) != 7:
                print("FAIL: MjPLAYREQ's +0x13 must be remembered as the "
                      "reserve tag, got %r" % janseats.reserve_tag(8)); ok = False

            # MjTBLCONFALL: the rules are STORED before the ack
            if janrules is not None:
                os.environ["POL_JAN_RULES_FILE"] = os.path.join(td, "rules.json")
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = True
                janrules._OWNER[0] = True
                _cf = bytearray(janwire.pack(opcode=MjTBLCONFALL, f13=0x21, src=1,
                                             dst=janwire.DST_SERVER & 0xFF, id8=1,
                                             payload=0x5B01E2BB73B3B60B + 8,
                                             length=0x70))
                _cf += bytes(0x70 - len(_cf))
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 4
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("GENTEN")] = 5
                _cr = handle_line(janwire.encode(bytes(_cf)), member_id=8)
                _cr = janwire.decode(_cr[0] if isinstance(_cr, list) else _cr)
                if janwire.unpack(_cr)["opcode"] != MjMASTERCMDACK or _cr[0x13] != 0x21:
                    print("FAIL: MjTBLCONFALL must still be acked with "
                          "MjMASTERCMDACK, tag echoed"); ok = False
                if janrules.values_for(1)["UMA"] != 4 or janrules.values_for(1)["GENTEN"] != 5:
                    print("FAIL: the master's rules were not stored: %r"
                          % janrules.values_for(1)); ok = False
                # a guest's MjTBLCONFALL is acked but NOT stored
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 1
                handle_line(janwire.encode(bytes(_cf)), member_id=16)
                if janrules.values_for(1)["UMA"] != 4:
                    print("FAIL: a guest must not be able to change the rules")
                    ok = False
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = False
                janrules._OWNER[0] = False
                janrules._CACHE["mtime"] = -1.0
                os.environ.pop("POL_JAN_RULES_FILE", None)

            # MjGALLEYREQ with POL_JAN_GALLERY off: refused with 7, the dialog
            # closes; a LEAVEREQ from a member in no gallery: 6 (the other code
            # the leave wait accepts -- it MUST get one of 5/6, mahdisp.c:1116).
            # The grant path is exercised further down, on a running table.
            g = _ask(MjGALLEYREQ, 1, 44, f13=0x03)
            if g is None or janwire.unpack(g)["opcode"] != MjRESERVEACK \
                    or g[0x18] != GALLEY_REFUSED or g[0x13] != 0x03:
                print("FAIL: MjGALLEYREQ must be refused with MjRESERVEACK 7, "
                      "tag echoed, got %r" % (g and (g[0x18], g[0x13]))); ok = False
            g2 = _ask(MjGALLEYLEAVEREQ, 1, 44, f13=0x04)
            if g2 is None or g2[0x18] != janseats.GALLEY_LEFT_ERROR or g2[0x13] != 0x04:
                print("FAIL: MjGALLEYLEAVEREQ from a non-spectator must be "
                      "answered 6, tag echoed, got %r" % (g2 and g2[0x18])); ok = False

            # MjLEAVEROOM: chosen silence, seat released, master promoted
            janseats.take_master_changes()
            _lr = janwire.pack(opcode=MjLEAVEROOM, f13=0x11, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 8, length=0x28)
            _out = handle_line(janwire.encode(_lr + bytes(8)), member_id=8)
            if _out is None or [l for l in _out if l[:1] == b"B" and
                                janwire.unpack(janwire.decode(l))["opcode"]
                                not in (MjNOTICEMEMBER,)]:
                print("FAIL: MjLEAVEROOM must be answered with SILENCE (an "
                      "empty list, never 'no responder'), got %r" % (_out,))
                ok = False
            if janseats.table_of(8) != 0 or janseats.master_of(1) != 16:
                print("FAIL: the leaving master's seat must go and the guest "
                      "be promoted; master is %r" % janseats.master_of(1))
                ok = False
            # ...and the survivor was told: MjNOTICEMEMBER +0x54 = their seat
            _q = _take_pushed(16)
            _nm = [janwire.decode(l) for l in _q if l[:1] == b"B"]
            _nm = [r for r in _nm if janwire.unpack(r)["opcode"] == MjNOTICEMEMBER]
            if not _nm or _nm[-1][0x54] != 1 or _nm[-1][0x50 + 1] != 1:
                print("FAIL: the promoted guest must be queued an MjNOTICEMEMBER "
                      "with +0x54 = 1 (their seat) and +0x51 Ready; got %r"
                      % ([(r[0x54], list(r[0x50:0x54])) for r in _nm],)); ok = False
            if _nm and struct.unpack_from("<Q", _nm[-1], 0x30)[0] != 0:
                print("FAIL: the lobby notice must leave the EMPTY seat 0 as id "
                      "0 -- the client's occupant count picks the dialog"); ok = False

            # KICK: 16 is master now; 8 comes back as a guest and is kicked
            _ask(MjPLAYREQ, 1, 8, f13=0x09)
            _ask(MjPLAYREQ, 1, 16, f13=0x0C)       # 16's reserve counter is 0xC
            _PUSH.clear()
            janseats.take_master_changes()
            _kb = bytearray(janwire.pack(opcode=MjMEMBERBANISH, f13=0x2D, src=1,
                                         dst=janwire.DST_SERVER & 0xFF, id8=1,
                                         payload=0x5B01E2BB73B3B60B + 16,
                                         length=0x30))
            _kb += bytes(0x30 - len(_kb))
            struct.pack_into("<Q", _kb, BANISH_TARGET_OFF, 0x5B01E2BB73B3B60B + 8)
            _kr = handle_line(janwire.encode(bytes(_kb)), member_id=16)
            _kr = [janwire.decode(l) for l in _kr] if _kr else []
            _ack = [r for r in _kr if janwire.unpack(r)["opcode"] == MjRESERVEACK]
            if not _ack or _ack[0][0x13] != 0x0C or _ack[0][0x18] == 0:
                print("FAIL: a kick must be answered with MjRESERVEACK carrying "
                      "the kicker's RESERVE tag (0xC), not the banish tag 0x2D; "
                      "got %r" % ([(r[0x12], r[0x13], r[0x18]) for r in _kr],))
                ok = False
            if janseats.table_of(8) != 0 or janseats.table_of(16) != 1:
                print("FAIL: the kicked member must lose the seat, the kicker "
                      "keep it"); ok = False
            _vq = [janwire.decode(l) for l in _take_pushed(8) if l[:1] == b"B"]
            _bz = [r for r in _vq if janwire.unpack(r)["opcode"] == MjNOTICEBANISH]
            if not _bz or struct.unpack_from("<Q", _bz[0], 0x20)[0] != \
                    0x5B01E2BB73B3B60B + 8:
                print("FAIL: the victim must be queued MjNOTICEBANISH with THEIR "
                      "PolID at +0x20"); ok = False
            # the kicker's copy rode its own reply line (queued records go
            # first -- see handle_line)
            if not any(janwire.unpack(r)["opcode"] == MjNOTICEBANISH for r in _kr):
                print("FAIL: the kicker's client is told too (it shows the "
                      "dialog like everyone else)"); ok = False
            # a GUEST cannot kick
            _ask(MjPLAYREQ, 1, 8, f13=0x0A)
            _kb2 = bytearray(_kb)
            struct.pack_into("<Q", _kb2, 0x18, 0x5B01E2BB73B3B60B + 8)
            struct.pack_into("<Q", _kb2, BANISH_TARGET_OFF, 0x5B01E2BB73B3B60B + 16)
            handle_line(janwire.encode(bytes(_kb2)), member_id=8)
            if janseats.table_of(16) != 1:
                print("FAIL: a guest's MjMEMBERBANISH must not release anybody")
                ok = False
            _PUSH.clear()

            # MjCHMASTER: the master declines -> the other seat takes it
            janseats.take_master_changes()
            _cm = janwire.pack(opcode=MjCHMASTER, f13=0x2E, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 16, length=0x28)
            _cr = handle_line(janwire.encode(_cm + bytes(8)), member_id=16)
            _cr = [janwire.decode(l) for l in _cr] if _cr else []
            _cr = [r for r in _cr if janwire.unpack(r)["opcode"] == MjMASTERCMDACK]
            if not _cr or _cr[0][0x18] != 1 or _cr[0][0x13] != 0x2E:
                print("FAIL: MjCHMASTER must be acked MjMASTERCMDACK result 1 "
                      "(the only value lobbydsp__002b4dc0 accepts)"); ok = False
            if janseats.master_of(1) != 8:
                print("FAIL: declining must pass the mastership to the next "
                      "seat, master is %r" % janseats.master_of(1)); ok = False
            if not any(janwire.unpack(janwire.decode(l))["opcode"] == MjNOTICEMEMBER
                       for l in _take_pushed(8) if l[:1] == b"B"):
                print("FAIL: the new master must be told via MjNOTICEMEMBER")
                ok = False
            _PUSH.clear()

            # TABLE CHAT: an 'A' line from 8 reaches 16 verbatim, not 8
            _say = janwire.pack(opcode=janwire.MjISAY, length=0x128, src=0,
                                dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4)
            _line = janwire.chat_line(_say, text=b"PS2Tester", extra=b"hello")
            if handle_line(_line, member_id=8) is not None:
                print("FAIL: a chat line is relayed, never answered"); ok = False
            if _take_pushed(16) != [_line] or _take_pushed(8) != []:
                print("FAIL: the chat line must be queued for the table-mate "
                      "only, verbatim"); ok = False
            # MjCHATMEMBERADD from 8: relayed to 16, and answered with 16's ACK
            _add = janwire.pack(opcode=MjCHATMEMBERADD, length=0x30, src=0,
                                dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                                payload=0x5B01E2BB73B3B60B + 8)
            _add = _add[:0x20] + b"PS2Tester".ljust(16, b"\0")
            _ar = handle_line(janwire.encode(_add), member_id=8)
            _ar = [janwire.decode(l) for l in (_ar or [])]
            if len(_ar) != 1 or janwire.unpack(_ar[0])["opcode"] != MjCHATMEMBERACK \
                    or janwire.unpack(_ar[0])["payload"] != 0x5B01E2BB73B3B60B + 16 \
                    or janwire.unpack(_ar[0])["sub"] != 4 \
                    or janwire.unpack(_ar[0])["dst"] != -1:
                print("FAIL: an ADD must be answered with one MjCHATMEMBERACK per "
                      "table-mate carrying THEIR PolID (sub 4, dst -1); got %r"
                      % ([(janwire.unpack(r)["opcode"],
                           "%016x" % janwire.unpack(r)["payload"]) for r in _ar],))
                ok = False
            _rq = [janwire.decode(l) for l in _take_pushed(16)]
            if not any(janwire.unpack(r)["opcode"] == MjCHATMEMBERADD for r in _rq):
                print("FAIL: the ADD itself must be relayed to the table-mate")
                ok = False
            # the 0x48 INFO line is relayed too, and has a name now
            _info = janwire.pack(opcode=MjCHATINFO, length=0x128, src=0,
                                 dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4)
            _info = _info[:0x18] + b"INFO".ljust(16, b"\0") + \
                b"PS2Tester has entered.".ljust(0x100, b"\0")
            handle_line(janwire.encode(_info), member_id=8)
            if not any(janwire.unpack(janwire.decode(l))["opcode"] == MjCHATINFO
                       for l in _take_pushed(16)):
                print("FAIL: the 0x48 INFO line must be relayed"); ok = False
            # a pushed line rides take_pending FIRST
            push_line(16, b"Bfake")
            if take_pending(16)[:1] != [b"Bfake"]:
                print("FAIL: take_pending must drain the lobby push queue")
                ok = False
            if take_pending(16, limit=0) != []:
                pass
            # the rejoin flag: MjENTERGAME on a PLAYING table marks the member
            janseats.set_in_play(1, 8)
            _eg = janwire.pack(opcode=MjENTERGAME, f13=0x2F, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 16, length=0x28)
            _er = handle_line(janwire.encode(_eg + bytes(8)), member_id=16)
            _er = janwire.decode(_er[0]) if isinstance(_er, list) else None
            if _er is None or janwire.unpack(_er)["opcode"] != MjENTERGAMEACK \
                    or _er[0x18] != 1:
                print("FAIL: MjENTERGAME on a playing table must be Success")
                ok = False
            if not janseats.rejoin_pending(16, clear=True):
                print("FAIL: MjENTERGAME on a playing table must flag the "
                      "rejoin for jangame"); ok = False
            janseats.clear_in_play(1)
            _PUSH.clear()

            # --- TWO ROOMS, ONE TABLE NUMBER (finding 30) -------------------
            # Member 8 is JOINed to #MJS0R011 (Room 1-1 = 101), member 16 to
            # #MJS0R023 (Room 2-3 = 203). Both press Reserve on "table 1";
            # the wire carries id8=1 for both, the registry tells them apart.
            global LIVE_ROOMS
            _saved_rooms = LIVE_ROOMS
            LIVE_ROOMS = lambda: {          # noqa: E731
                "#MJS0R011": {"who": [{"member_id": 8, "name": "PS2Tester"},
                                      {"member_id": 44, "name": "Watcher"}]},
                "#MJS0R023": {"who": [{"member_id": 16, "name": "DeckTest"}]}}
            janseats._TABLES.clear(); janseats._DELTAS.clear()
            janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats.take_master_changes()
            if GAMES is not None:
                GAMES.by_lobby.clear(); GAMES.by_member.clear(); GAMES.tables.clear()
            _t101 = janseats.room_table_id(101, 1)
            _t203 = janseats.room_table_id(203, 1)
            if member_room(8) != 101 or member_room(16) != 203 or member_room(99) != 0:
                print("FAIL: member_room must read the registry: 8->%r 16->%r"
                      % (member_room(8), member_room(16))); ok = False
            _ra = _ask(MjPLAYREQ, 1, 8, f13=0x01)
            _rb = _ask(MjPLAYREQ, 1, 16, f13=0x01)
            if _ra is None or _rb is None or _ra[0x18] != 2 or _rb[0x18] != 2:
                print("FAIL: two members in two rooms reserving 'table 1' must "
                      "BOTH be masters (2/ReserveMaster), got %r / %r"
                      % (_ra and _ra[0x18], _rb and _rb[0x18])); ok = False
            if janseats.table_of(8) != _t101 or janseats.table_of(16) != _t203:
                print("FAIL: the seats must land in each member's OWN room: "
                      "8 at %r, 16 at %r" % (janseats.table_of(8),
                                             janseats.table_of(16))); ok = False
            if ([m for m, _s, _n, _p in janseats.seats_at(_t101)] != [8]
                    or [m for m, _s, _n, _p in janseats.seats_at(_t203)] != [16]
                    or janseats.seat_count(1) != 0):
                print("FAIL: separate seat sets expected; bare table 1 must be "
                      "empty: %r" % janseats.snapshot()); ok = False
            if (janseats.sequence(101) != 1 or janseats.sequence(203) != 1
                    or janseats.deltas_after(0, 101) != [(1, _t101)]
                    or janseats.deltas_after(0, 203) != [(1, _t203)]
                    or janseats.deltas_after(0, 0) != []):
                print("FAIL: each room must have its own delta stream: 101=%r "
                      "203=%r" % (janseats.deltas_after(0, 101),
                                  janseats.deltas_after(0, 203))); ok = False
            # a second reserver in room 203 moves room 203 only
            LIVE_ROOMS = lambda: {          # noqa: E731
                "#MJS0R011": {"who": [{"member_id": 8}, {"member_id": 44}]},
                "#MJS0R023": {"who": [{"member_id": 16}, {"member_id": 21}]}}
            _rc = _ask(MjPLAYREQ, 1, 21, f13=0x01)
            if _rc is None or _rc[0x18] != 1 or janseats.sequence(101) != 1 \
                    or janseats.sequence(203) != 2:
                print("FAIL: a guest joining room 203's table 1 must be a guest "
                      "(1) of member 16 and move ONLY room 203's sequence")
                ok = False
            # the rules: one MjTBLCONFALL per room, keyed by composite
            if janrules is not None:
                os.environ["POL_JAN_RULES_FILE"] = os.path.join(td, "rules2.json")
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = True
                janrules._OWNER[0] = True
                _cf = bytearray(janwire.pack(opcode=MjTBLCONFALL, f13=0x21, src=1,
                                             dst=janwire.DST_SERVER & 0xFF, id8=1,
                                             payload=0x5B01E2BB73B3B60B + 8,
                                             length=0x70))
                _cf += bytes(0x70 - len(_cf))
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 4
                handle_line(janwire.encode(bytes(_cf)), member_id=8)
                _cf2 = bytearray(_cf)
                struct.pack_into("<Q", _cf2, 0x18, 0x5B01E2BB73B3B60B + 16)
                _cf2[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 1
                handle_line(janwire.encode(bytes(_cf2)), member_id=16)
                if (janrules.values_for(_t101)["UMA"] != 4
                        or janrules.values_for(_t203)["UMA"] != 1
                        or janrules.has_rules(1)):
                    print("FAIL: MjTBLCONFALL must key the rules by the "
                          "composite: 101=%r 203=%r bare=%r"
                          % (janrules.values_for(_t101)["UMA"],
                             janrules.values_for(_t203)["UMA"],
                             janrules.has_rules(1))); ok = False
            # Start Game in both rooms -> TWO in-game tables
            if GAMES is not None:
                _gs = janwire.pack(opcode=MjGAMESTART, f13=0x22, src=1,
                                   dst=janwire.DST_SERVER & 0xFF, id8=1,
                                   payload=0x5B01E2BB73B3B60B + 8)
                _ga = handle_line(janwire.encode(_gs), member_id=8)
                _gs2 = janwire.pack(opcode=MjGAMESTART, f13=0x22, src=1,
                                    dst=janwire.DST_SERVER & 0xFF, id8=1,
                                    payload=0x5B01E2BB73B3B60B + 16)
                _gb = handle_line(janwire.encode(_gs2), member_id=16)
                if not _ga or not _gb:
                    print("FAIL: MjGAMESTART in each room must be answered")
                    ok = False
                _ta = GAMES.by_lobby.get(_t101)
                _tb = GAMES.by_lobby.get(_t203)
                if _ta is None or _tb is None or _ta is _tb:
                    print("FAIL: Manager.table_for_lobby must give TWO tables, "
                          "keyed %r; got %r" % ((_t101, _t203),
                                                sorted(GAMES.by_lobby)))
                    ok = False
                elif (0x5B01E2BB73B3B60B + 16 in _ta.member_ids()
                      or 0x5B01E2BB73B3B60B + 8 not in _ta.member_ids()
                      or 0x5B01E2BB73B3B60B + 16 not in _tb.member_ids()
                      or 0x5B01E2BB73B3B60B + 21 not in _tb.member_ids()):
                    print("FAIL: each room's table seats its own humans: "
                          "101=%r 203=%r" % (["%x" % i for i in _ta.member_ids()],
                                              ["%x" % i for i in _tb.member_ids()]))
                    ok = False
                if _ta is not None and _ta.channel != b"#MJS0T101001":
                    print("FAIL: the in-game channel is the ROOM-QUALIFIED PTL "
                          "row name Room 1-1's client JOINs: %r" % _ta.channel)
                    ok = False
                if not janseats.is_in_play(_t101) or not janseats.is_in_play(_t203) \
                        or janseats.is_in_play(1):
                    print("FAIL: in-play must be per composite"); ok = False
                if janrules is not None and (_ta.rules.uma != (60, 20, -20, -60)
                                             or _tb.rules.uma != (10, 5, -5, -10)):
                    print("FAIL: each table plays ITS room's rules: 101 uma=%r "
                          "203 uma=%r" % (_ta.rules.uma, _tb.rules.uma)); ok = False

                # --- THE GALLERY, END TO END (2026-09-04) -------------------
                # Member 44 (Room 1-1, no seat) watches Room 1-1's table 1
                # while member 8 plays it against three bots. Every byte
                # below is the client's own, read out of its sources.
                if _ta is not None and janrules is not None:
                    _old_delay = jangame.DEAL_DELAY
                    jangame.DEAL_DELAY = 0.0
                    _old_gal = GALLERY_ENABLE
                    _rdy = janwire.pack(opcode=janmsgs.MjREADY, f13=0, src=0, dst=4,
                                        f16=0, sub=3, length=0x18)[:0x18]
                    _dl = handle_line(janwire.encode(_rdy), member_id=8)
                    if _ta.state != "playing" or not _dl:
                        print("FAIL: gallery rig: member 8's MjREADY must deal")
                        ok = False
                    _PUSH.clear()

                    def _watch(member=44, f13=0x05, table=1):
                        r = _ask(MjGALLEYREQ, table, member, f13=f13)
                        return (r[0x18], r[0x19], r[0x13]) if r is not None else None

                    def _gack(seq, slot, f16=1):
                        # console__00284310 (console.c:2552-2577): op 0x30, +0x13
                        # seq, +0x14 the RESERVEACK +0x19 slot, +0x15 4, +0x17 6
                        return janwire.pack(opcode=janmsgs.MjGALLEYACK, f13=seq,
                                            src=slot, dst=4, f16=f16, sub=6,
                                            length=0x18)[:0x18]

                    def _ops_of(lines):
                        return [janwire.unpack(janwire.decode(l))["opcode"]
                                for l in (lines or []) if l[:1] == b"B"]

                    # knob off: 7, whatever the table is doing
                    if _watch() != (GALLEY_REFUSED, 0, 0x05):
                        print("FAIL: with POL_JAN_GALLERY off a Watch on a running "
                              "table is still 7, got %r" % (_watch(),)); ok = False
                    globals()["GALLERY_ENABLE"] = True
                    # the master's stored rules say GALLEY_LIMIT 0 (No) -> 10
                    if janrules.values_for(_t101)["GALLEY_LIMIT"] != 0 \
                            or _watch() != (janseats.GALLEY_LIMITED, 0, 0x05):
                        print("FAIL: GALLEY_LIMIT 0 must refuse with 10, got %r"
                              % (_watch(),)); ok = False
                    if janseats.gallery_of(44) or GAMES.spectator_table(44) is not None:
                        print("FAIL: a refused Watch must leave no membership"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 1}, by=8)
                    # a player cannot watch its own table: 7
                    if _watch(member=8) != (GALLEY_REFUSED, 0, 0x05):
                        print("FAIL: a seated member's Watch is 7, got %r"
                              % (_watch(member=8),)); ok = False
                    # a table with nothing running: 8 (Room 2-3's table 1 is
                    # seated but never dealt -- state idle -- and 16 is its
                    # only human, so ask from 16's room-mate... there is none:
                    # use a table in room 101 nobody reserved)
                    janrules.store(janseats.room_table_id(101, 2), {"GALLEY_LIMIT": 1}, by=8)
                    if _watch(table=2) != (janseats.GALLEY_NOT_PLAYING, 0, 0x05):
                        print("FAIL: a Watch on a table with no game is 8, got %r"
                              % (_watch(table=2),)); ok = False
                    # a password table (LIMIT_PASS_WORD on + text): 9 -- the
                    # client always sends "" -- and the flag alone gates it
                    janrules.store(_t101, {"LIMIT_PASS_WORD": 1, "text": "abc"}, by=8)
                    if _watch() != (janseats.GALLEY_PASSWORD, 0, 0x05):
                        print("FAIL: a password table refuses with 9, got %r"
                              % (_watch(),)); ok = False
                    janrules.store(_t101, {"LIMIT_PASS_WORD": 0}, by=8)
                    # the grant: 1, slot 0 at +0x19, tag echoed
                    _w = _watch()
                    if _w != (janseats.GALLEY_OK, 0, 0x05):
                        print("FAIL: the Watch must be granted 1/slot 0/tag 5, got %r"
                              % (_w,)); ok = False
                    if janseats.gallery_of(44) != _t101 or GAMES.spectator_table(44) is not _ta \
                            or janseats.table_of(44) != 0 or 44 in _ta.live \
                            or janseats.seat_count(_t101) != 1:
                        print("FAIL: the spectator is in the store's gallery and the "
                              "manager's table, and is NOT a seat"); ok = False
                    if janseats.resolve_table(44, 1) != _t101 \
                            or janseats.resolve_table(44, 1, room=member_room(44)) != _t101:
                        print("FAIL: the spectator's b/g/MJSTableInfoSub fetch must "
                              "resolve to the table it watches"); ok = False
                    # again: 3, and the stale membership is dropped; then 1 again
                    if _watch(f13=0x06) != (janseats.GALLEY_DUP, 0, 0x06) \
                            or janseats.gallery_of(44) != 0 \
                            or GAMES.spectator_table(44) is not None:
                        print("FAIL: a second Watch is 3 (Already spectating) and "
                              "drops the stale membership"); ok = False
                    if _watch(f13=0x07) != (janseats.GALLEY_OK, 0, 0x07):
                        print("FAIL: ...so the NEXT Watch is a 1 again"); ok = False
                    # nothing before its first MjGALLEYACK (the entry drain)
                    if take_pending(44) != []:
                        print("FAIL: nothing may be queued for a spectator before "
                              "its first MjGALLEYACK"); ok = False
                    _e0 = handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44)
                    if _ops_of(_e0) != [janmsgs.MjALLDATA] or janwire.decode(_e0[0])[0x18] != 0:
                        print("FAIL: the first (entry) GALLEYACK is answered with the "
                              "subtype-0 MjALLDATA snapshot and nothing else, got %r"
                              % _ops_of(_e0)); ok = False
                    if handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44) is not None:
                        print("FAIL: the second entry ack rides silence"); ok = False
                    # the human's HAIPAIACK moves the hand; the spectator gets a
                    # byte-for-byte copy of everything the human got
                    _hp = _ta._last_for[0]
                    _o8 = handle_line(janwire.encode(jangame._client_ack(_hp, 0)), member_id=8)
                    _c44 = take_pending(44)
                    if not _o8 or _c44 != _o8:
                        print("FAIL: the spectator's copies must equal the human's "
                              "records: %r vs %r" % (_ops_of(_c44), _ops_of(_o8))); ok = False
                    if janmsgs.MjTSUMO not in _ops_of(_c44):
                        print("FAIL: ...including the seat-addressed MjTSUMO draw"); ok = False
                    # its ack: consumed, no mutation, no reply; a stray
                    # SASHIUMAREQUEST: ignored, never a ghost MjGAMEEND
                    _st = (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting)
                    _sq = janwire.unpack(janwire.decode(_c44[-1]))["f13"]
                    if handle_line(janwire.encode(_gack(_sq, 0)), member_id=44) is not None \
                            or _ta.gallery_seq.get(44) != _sq \
                            or (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting) != _st:
                        print("FAIL: a spectator's GALLEYACK is consumed without "
                              "mutation or reply"); ok = False
                    _stray = janwire.pack(opcode=janmsgs.MjSASHIUMAREQUEST, f13=_sq,
                                          src=0, dst=4, f16=1, sub=3, length=0x20)[:0x20]
                    if handle_line(janwire.encode(_stray), member_id=44) is not None \
                            or _ta.state != "playing" \
                            or (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting) != _st:
                        print("FAIL: a stray MjSASHIUMAREQUEST from a spectator is "
                              "ignored -- no reply, no ghost MjGAMEEND"); ok = False
                    # a silent spectator never stalls: the human plays on
                    _turns = 0
                    while (_ta._awaiting == (janmsgs.MjTSUMO, 0) and _turns < 3
                           and _ta.kyoku is not None and _ta.kyoku.result is None):
                        _lt = _ta._last_for[0]
                        _hd = [v for v in struct.unpack_from("<14H", _lt, janmsgs.TSUMO_HAND) if v]
                        handle_line(janwire.encode(jangame._client_sute(_lt, 0, len(_hd) - 1)),
                                    member_id=8)
                        _turns += 1
                    if _turns < 1 or 44 in _ta.timeouts or not take_pending(44):
                        print("FAIL: the hand advances with a SILENT spectator, whose "
                              "copies keep queueing (%d turn(s))" % _turns); ok = False
                    # CHAT both ways; suppressed for the spectator under limit 2
                    _PUSH.clear()
                    _say44 = janwire.chat_line(
                        janwire.pack(opcode=janwire.MjISAY, length=0x128, src=0,
                                     dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4),
                        text=b"Watcher", extra=b"nice hand")
                    if handle_line(_say44, member_id=44) is not None \
                            or _take_pushed(8) != [_say44] or _take_pushed(44) != []:
                        print("FAIL: a spectator's MjISAY reaches the seated human, "
                              "verbatim, not itself"); ok = False
                    if handle_line(_line, member_id=8) is not None \
                            or _take_pushed(44) != [_line] or _take_pushed(8) != []:
                        print("FAIL: a player's MjISAY reaches the spectator"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 2}, by=8)
                    handle_line(_say44, member_id=44)
                    handle_line(_line, member_id=8)
                    if _take_pushed(8) != [] or _take_pushed(44) != [_line]:
                        print("FAIL: GALLEY_LIMIT 2 (No chat only) drops the spectator's "
                              "line and still relays the players' to it"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 1}, by=8)
                    _add44 = janwire.pack(opcode=MjCHATMEMBERADD, length=0x30, src=0,
                                          dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                                          payload=0x5B01E2BB73B3B60B + 44)
                    _add44 = _add44[:0x20] + b"Watcher".ljust(16, b"\0")
                    _ar44 = handle_line(janwire.encode(_add44), member_id=44)
                    _ar44 = [janwire.decode(l) for l in (_ar44 or []) if l[:1] == b"B"]
                    if len(_ar44) != 1 or janwire.unpack(_ar44[0])["opcode"] != MjCHATMEMBERACK \
                            or janwire.unpack(_ar44[0])["payload"] != 0x5B01E2BB73B3B60B + 8 \
                            or _take_pushed(8) != [janwire.encode(_add44)]:
                        print("FAIL: a spectator's CHATMEMBERADD is relayed to the "
                              "seated human and answered with ITS ack (one per seated "
                              "human)"); ok = False
                    _PUSH.clear()
                    # LEAVEREQ: 5, membership gone everywhere; again: 6
                    _lv = _ask(MjGALLEYLEAVEREQ, 1, 44, f13=0x08)
                    if _lv is None or _lv[0x18] != GALLEY_LEFT or _lv[0x13] != 0x08 \
                            or janseats.gallery_of(44) != 0 \
                            or GAMES.spectator_table(44) is not None \
                            or 44 in _ta.gallery or take_pending(44) != []:
                        print("FAIL: MjGALLEYLEAVEREQ is 5 and drops the membership "
                              "in the store and the manager, got %r"
                              % (_lv and _lv[0x18])); ok = False
                    if _ask(MjGALLEYLEAVEREQ, 1, 44, f13=0x09)[0x18] != janseats.GALLEY_LEFT_ERROR:
                        print("FAIL: a second leave is 6"); ok = False
                    if handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44) is not None:
                        print("FAIL: the exit acks after a leave (no table now) are "
                              "silence, never a ghost MjGAMEEND"); ok = False
                    # GAMEEND drops the membership without any leave
                    if _watch(f13=0x0A) != (janseats.GALLEY_OK, 0, 0x0A):
                        print("FAIL: after a leave, Watch is 1 again"); ok = False
                    handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44)
                    take_pending(44)
                    _ta.begin_routing()
                    _ge = _ta.msg_gameend()
                    _ta.take_routes()
                    GAMES._release_galleries()
                    _c44 = take_pending(44)
                    if janseats.gallery_of(44) != 0 or not _c44 \
                            or _c44[-1] != janwire.encode(_ge):
                        print("FAIL: MjGAMEEND clears the store's gallery and the "
                              "spectator still gets the GAMEEND copy"); ok = False
                    if handle_line(janwire.encode(_gack(janwire.unpack(_ge)["f13"], 0, f16=0)),
                                   member_id=44) is not None \
                            or GAMES.spectator_table(44) is not None:
                        print("FAIL: the BYE-equivalent GALLEYACK after MjGAMEEND drops "
                              "the manager membership silently"); ok = False
                    if _watch(f13=0x0B) != (janseats.GALLEY_NOT_PLAYING, 0, 0x0B):
                        print("FAIL: a Watch on the finished table is 8 (not 3): %r"
                              % (_watch(f13=0x0B),)); ok = False
                    globals()["GALLERY_ENABLE"] = _old_gal
                    jangame.DEAL_DELAY = _old_delay
                    _PUSH.clear()
                GAMES.by_lobby.clear(); GAMES.by_member.clear(); GAMES.tables.clear()
            if janrules is not None:
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = False
                janrules._OWNER[0] = False
                janrules._CACHE["mtime"] = -1.0
                os.environ.pop("POL_JAN_RULES_FILE", None)
            # without a registry the wire id is room 0's -- the v1 behaviour
            LIVE_ROOMS = None
            if _table_for(8, 1) != _t101 or _table_for(4242, 1) != 1:
                print("FAIL: without a registry a SEATED member's table is still "
                      "theirs (%r) and a stranger's is room 0 (%r)"
                      % (_table_for(8, 1), _table_for(4242, 1))); ok = False
            LIVE_ROOMS = _saved_rooms
            _PUSH.clear()
            janseats.take_master_changes()

            janseats._TABLES.clear()
            janseats._DELTAS.clear(); janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats._OWNER[0] = False
            janseats._ADOPTED[0] = False
            janseats._CACHE["mtime"] = -1.0
            os.environ.pop("POL_JAN_SEATS", None)
            os.environ.pop("POL_JAN_SEATS_FILE", None)

    srv.shutdown()
    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--decode", metavar="LINE")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    if a.decode:
        rec = janwire.decode(a.decode)
        if len(rec) < 32:
            print("only %d bytes decoded from %d characters -- a line this short "
                  "usually means unprintable bytes were lost in transit: six-bit "
                  "value 63 encodes to 0x7f (DEL), so these lines cannot be "
                  "copied out of a terminal. Feed the raw bytes."
                  % (len(rec), len(a.decode)))
            return 1
        print(describe(rec))
        return 0
    if a.serve:
        serve(port=a.port)
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
