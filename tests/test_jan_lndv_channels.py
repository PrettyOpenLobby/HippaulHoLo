#!/usr/bin/env python3
"""MjGETLNDVACK: byte-identical by default, channel ids with the knob on.

    python tests/test_jan_lndv_channels.py

POL_JAN_LNDV_CHANNELS=1 (default off) serves Project Crystal Server's channel
ids (its server POL ids XOR K("MJS")) and lobby domain 3. The versions the two
client builds check (0x20020227 at +0x30, 0x20020603 at +0x38) must not move.
K comes from the core's mgkey module, so this suite needs the OpenLobby
checkout beside this one (see tools/jan_testenv.py).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import jan_testenv  # noqa: E402
jan_testenv.setup(need_core=True)
for k in list(os.environ):
    if k.startswith("POL_JAN_LNDV"):
        os.environ.pop(k)

import janhourou as jh  # noqa: E402
import janwire  # noqa: E402
import mgkey  # noqa: E402

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


# an opener in the shape the client sends (the fields the ack copies are
# id8 and payload; their values are the member's own)
REQ = janwire.pack(opcode=jh.MjGETLNDV, f13=0, src=0xFD, dst=0xFE, sub=5,
                   id8=1, payload=0x5B01E2BB73B3B60B + 8, length=32)[:0x20]

print("default: the record served today")
off = jh.getlndv_ack(REQ)
chk("length 0x40 (dual)", len(off), 0x40)
chk("three u64s zero", struct.unpack_from("<QQQ", off, 0x18), (0, 0, 0))
chk("2002 version at +0x30", struct.unpack_from("<I", off, 0x30)[0], 0x20020227)
chk("+0x34 u16 / +0x36 u8 zero", (struct.unpack_from("<H", off, 0x34)[0], off[0x36]),
    (0, 0))
chk("2004 version at +0x38", struct.unpack_from("<I", off, 0x38)[0], 0x20020603)
chk("+0x3C..+0x3F zero", off[0x3C:0x40], bytes(4))
chk("2004's 4th u64 low half is the 2002 version",
    struct.unpack_from("<Q", off, 0x30)[0] & 0xFFFFFFFF, 0x20020227)

print("POL_JAN_LNDV_CHANNELS=1")
os.environ["POL_JAN_LNDV_CHANNELS"] = "1"
on = jh.getlndv_ack(REQ)
k = mgkey.minigame_client_key("MJS")
chk("K(MJS) is 64-bit and not 0", 0 < k < (1 << 64), True)
want = (jh.LNDV_POLID_PROFILE ^ k, jh.LNDV_POLID_RANK ^ k, jh.LNDV_POLID_LOBBY ^ k)
chk("profile / rank / lobby ids", struct.unpack_from("<QQQ", on, 0x18), want)
# Crystal's ids are 41-bit (8 base-36 digits), so K's bits 41..63 survive
chk("lobby id keeps K's bits 41..63",
    struct.unpack_from("<Q", on, 0x28)[0] >> 41, k >> 41)
chk("2002 version unchanged", struct.unpack_from("<I", on, 0x30)[0], 0x20020227)
chk("2002 volume 0, domain 3", (struct.unpack_from("<H", on, 0x34)[0], on[0x36]),
    (0, 3))
chk("2004 version unchanged", struct.unpack_from("<I", on, 0x38)[0], 0x20020603)
chk("2004 volume 0, domain 3", (struct.unpack_from("<H", on, 0x3C)[0], on[0x3E]),
    (0, 3))
chk("header identical", on[:0x18], off[:0x18])

print("explicit field knobs still win")
os.environ["POL_JAN_LNDV_F28"] = "0"
chk("F28=0 over the channel id", struct.unpack_from("<Q", jh.getlndv_ack(REQ),
                                                    0x28)[0], 0)
os.environ.pop("POL_JAN_LNDV_F28")
os.environ.pop("POL_JAN_LNDV_CHANNELS")

print("%s" % ("ALL OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
