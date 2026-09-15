#!/usr/bin/env python3
"""Janhourou's wire format: the 32-byte message record and its text framing.

This is the RUNTIME copy -- `janhourou.py` imports it, so it must stay
dependency-free and live under services/ (the Docker build context). It
encodes the reverse engineering of the game's message set, and every address
below is in the decrypted `JanHouRou.pex` at base 0x00280000.

A message is a 32-byte struct handed to `0x002c9d00`, which logs it by name and
sends it. The struct does NOT go on the wire raw: the outbound drain at
`0x002a96b8` pulls a record off queue 0 and serialises it to an ASCII line whose
FIRST character is a class tag.

    'A'  chat only -- opcode 0 (MjISAY) / 1 (MjISAYGALLEY)
         sprintf("%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%s")        fmt @0x004140f0
         tail = sprintf("%s%s%c%s", "#CHAT#", rec+0x18, '\t', rec+0x28)
                                                            fmt @0x00414118
         -- in a chat record +0x18 and +0x28 hold C STRINGS, not the u64.

    'B'  every other opcode -- the whole record, six bits at a time.
         0x002a8c60 appends one input byte (3-into-4, incremental);
         0x002a8dc0 gives the length as n + n/3 + 1 (the base64 formula).

The alphabet is NOT a table: the encoder does `ori v1, v1, 0x40` on each 6-bit
group, so value v becomes byte 0x40 + v ('@' .. 0x7F). Hence no output byte can
be NUL -- the line stays C-string safe and IRC-legal -- and this is **not** the
A64 alphabet the login channel uses. Do not reuse that decoder here.

CAREFUL: the range ends at 0x7F, so a six-bit group of 63 encodes to **DEL**,
which is not printable. These lines are NUL-free but they are NOT plain text --
copying one out of a terminal silently loses those bytes, and any logging or
transport that assumes printable ASCII will corrupt them. Keep them as bytes.

A 32-byte record becomes a 44-character line.

    python janwire.py --selftest
    python janwire.py --decode 'B@...'
"""
import argparse
import struct

# --- the 32-byte record ------------------------------------------------------
# Field names come from what the builders store. MjSUTE (@0x00285b9c) and
# MjTGMPONG (@0x002c65ac) agree on every one of them.
#
# ENDIANNESS: LITTLE. Corrected 2026-08-12 from the first live PS2 capture.
# This file originally packed the multi-byte fields big-endian, which was never
# measured -- it round-tripped against itself, so --selftest passed and the
# error stayed invisible. The real client's first message settles it:
#
#     ... 2e e3 01 5b | 20 00 | 40 | 00 | fd | fe | 00 | 05 | ...
#                       ^^^^^ +0x10, the TOTAL LENGTH field
#
# The record is 32 bytes and the documented value of that field is 32. `20 00`
# is 32 little-endian and 8192 big-endian, so the field is little-endian -- as
# it must be, since the PS2's EE is a little-endian MIPS and the builders store
# it with a plain `sh`. The same applies to the u64s at +0x08 and +0x18.
_U64 = "<Q"
_U16 = "<H"

MjISAY, MjISAYGALLEY = 0, 1
MjTGMPING, MjTGMPONG = 27, 28

# rec[0x15], the destination, as the resolver at 0x002a9120 reads it.
DST_CHANNEL = -1        # send to the channel named at 0x00444cd0 (the table)
DST_REPLY = -2          # destination id is the record's own +0x08
DST_SERVER = -3         # neither: the caller's standing peer id is used


def pack(id8=0, length=32, opcode=0, f13=0, src=0, dst=0, f16=0, sub=0,
         payload=0, head=b"\0" * 8, body=b""):
    """Build a record. `head` is the first 8 bytes, which no builder writes --
    they are left as the caller found them."""
    rec = bytearray(32)
    rec[0:8] = head
    struct.pack_into(_U64, rec, 8, id8 & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into(_U16, rec, 0x10, length if length is not None else 32 + len(body))
    rec[0x12] = opcode & 0xFF
    rec[0x13] = f13 & 0xFF
    rec[0x14] = src & 0xFF
    rec[0x15] = dst & 0xFF
    rec[0x16] = f16 & 0xFF
    rec[0x17] = sub & 0xFF
    struct.pack_into(_U64, rec, 0x18, payload & 0xFFFFFFFFFFFFFFFF)
    return bytes(rec) + body


def _s8(v):
    return v - 256 if v > 127 else v


HEADER_LEN = 0x18       # byte 0, a 7-byte blob, the u64, the length, six bytes


def unpack(rec):
    """Named fields from a record.

    WARNING: THE MINIMUM IS 24 BYTES, NOT 32. Corrected 2026-08-17 while building the
    in-game path: `console__00284380` -- the client's own ack sender -- stores
    `0x18` in the length field and sends a HEADER ONLY. Every MjHAIPAIACK,
    MjSEISANACK, MjBYE and the rest is 24 bytes on the wire, so a 32-byte floor
    rejects the whole ack half of the protocol. Nothing had noticed because no
    client had ever got far enough to send one.

    A short record has no +0x18, so `payload` reads 0 there rather than raising.
    """
    if len(rec) < HEADER_LEN:
        raise ValueError("record is %d bytes, need at least %d"
                         % (len(rec), HEADER_LEN))
    if len(rec) < 32:
        return {
            "f00": rec[0:8],
            "id8": struct.unpack_from(_U64, rec, 8)[0],
            "length": struct.unpack_from(_U16, rec, 0x10)[0],
            "opcode": rec[0x12], "f13": rec[0x13],
            "src": _s8(rec[0x14]), "dst": _s8(rec[0x15]),
            "f16": rec[0x16], "sub": rec[0x17],
            "payload": 0, "body": b"",
        }
    return {
        "f00": rec[0:8],
        "id8": struct.unpack_from(_U64, rec, 8)[0],
        "length": struct.unpack_from(_U16, rec, 0x10)[0],
        "opcode": rec[0x12],
        "f13": rec[0x13],
        "src": _s8(rec[0x14]),
        "dst": _s8(rec[0x15]),
        "f16": rec[0x16],
        "sub": rec[0x17],
        "payload": struct.unpack_from(_U64, rec, 0x18)[0],
        "body": rec[32:],
    }


# --- the 'B' text encoding ---------------------------------------------------

def enc_len(n):
    """0x002a8dc0: n + n/3 + 1."""
    return n + n // 3 + 1


def encode(rec):
    """0x002a8c60 applied byte by byte, then the 'B' tag at [0]."""
    out = bytearray(enc_len(len(rec)))
    for i, b in enumerate(rec):
        o = i + i // 3
        if i % 3 == 0:
            out[o] |= (b & 0xFC) >> 2
            out[o + 1] |= (b & 0x03) << 4
        elif i % 3 == 1:
            out[o] |= (b & 0xF0) >> 4
            out[o + 1] |= (b & 0x0F) << 2
        else:
            out[o] |= (b & 0xC0) >> 6
            out[o + 1] |= b & 0x3F
    return b"B" + bytes(0x40 | v for v in out)


def decode(line):
    """Inverse of encode(). Accepts the line with or without its class tag."""
    if isinstance(line, str):
        line = line.encode("latin1")
    if line[:1] in (b"B", b"A"):
        line = line[1:]
    six = [b & 0x3F for b in line]
    out = bytearray()
    for i in range(0, len(six) - 1, 4):
        g = six[i:i + 4]
        if len(g) < 2:
            break
        out.append(((g[0] << 2) | (g[1] >> 4)) & 0xFF)
        if len(g) >= 3:
            out.append(((g[1] << 4) | (g[2] >> 2)) & 0xFF)
        if len(g) >= 4:
            out.append(((g[2] << 6) | g[3]) & 0xFF)
    return bytes(out)


def chat_line(rec, text=b"", extra=b""):
    """The 'A' form, per the two sprintfs above."""
    h = unpack(rec)
    tail = b"#CHAT#" + text + b"\t" + extra
    return b"A" + (b"%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%s" % (
        rec[0], (h["id8"] >> 32) & 0xFFFFFFFF, h["id8"] & 0xFFFFFFFF,
        h["length"], h["opcode"], h["f13"],
        rec[0x14], rec[0x15], rec[0x16], rec[0x17], tail))


def parse_chat_line(line):
    """Inverse of chat_line() -- ten hex fields then the tail."""
    if isinstance(line, str):
        line = line.encode("latin1")
    if line[:1] == b"A":
        line = line[1:]
    parts = line.split(b",", 10)
    if len(parts) != 11:
        raise ValueError("chat line has %d fields, expected 11" % len(parts))
    f = [int(p, 16) for p in parts[:10]]
    tail = parts[10]
    text, _, extra = tail[len(b"#CHAT#"):].partition(b"\t")
    return {"f00": f[0], "id8": (f[1] << 32) | f[2], "length": f[3],
            "opcode": f[4], "f13": f[5], "src": _s8(f[6]), "dst": _s8(f[7]),
            "f16": f[8], "sub": f[9], "text": text, "extra": extra}


def line_for(rec, text=b"", extra=b""):
    """Serialise a record the way the drain does: chat takes the 'A' form."""
    return chat_line(rec, text, extra) if rec[0x12] in (MjISAY, MjISAYGALLEY) \
        else encode(rec)


# --- one fully specified exchange -------------------------------------------

def pong_for(ping_rec):
    """MjTGMPONG exactly as 0x002c65a8..0x002c65e0 builds it: echo the ping's
    +0x18 into our +0x08, our own value into +0x18, src -3, dst -2."""
    p = unpack(ping_rec)
    return pack(id8=p["payload"], opcode=MjTGMPONG,
                src=DST_SERVER & 0xFF, dst=DST_REPLY & 0xFF,
                payload=p["payload"])


def describe_short(h):
    """One line of named fields. janhourou.describe() adds the opcode NAME; this
    stays here so janwire has no import back into the server module."""
    return ("opcode=%d len=%d src=%d dst=%d f13=%d sub=%d id8=%016x payload=%016x"
            % (h["opcode"], h["length"], h["src"], h["dst"], h["f13"], h["sub"],
               h["id8"], h["payload"]))


def selftest():
    ok = True
    rec = pack(id8=0x0011223344556677, opcode=37, src=2, dst=4, f16=1, sub=3,
               payload=0x8899AABBCCDDEEFF)
    line = encode(rec)
    print("record  %s" % rec.hex())
    print("line    %s  (%d chars)" % (line.decode("latin1"), len(line)))

    if len(line) != 44 or len(line) != 1 + enc_len(32):
        print("FAIL: length %d, expected 44" % len(line)); ok = False
    if any(not 0x40 <= b <= 0x7F for b in line[1:]):
        print("FAIL: a character escaped 0x40..0x7f"); ok = False
    if decode(line) != rec:
        print("FAIL: round trip -> %s" % decode(line).hex()); ok = False

    h = unpack(decode(line))
    if (h["opcode"], h["src"], h["dst"], h["sub"]) != (37, 2, 4, 3):
        print("FAIL: fields %r" % h); ok = False

    # A six-bit group of 63 lands on 0x7F (DEL). Pin it: it is the reason these
    # lines must be handled as bytes, and it is easy to "fix" away by mistake.
    if encode(b"\xff" * 3) != b"B\x7f\x7f\x7f\x7f@":
        print("FAIL: 0xff*3 -> %r, expected four DELs" % encode(b"\xff" * 3)); ok = False

    ping = pack(opcode=MjTGMPING, src=DST_REPLY & 0xFF, dst=DST_SERVER & 0xFF,
                payload=0xDEADBEEF)
    q = unpack(pong_for(ping))
    if (q["opcode"], q["id8"], q["src"], q["dst"]) != (MjTGMPONG, 0xDEADBEEF, -3, -2):
        print("FAIL: pong %r" % q); ok = False
    print("pong    %s" % encode(pong_for(ping)).decode("latin1"))

    chat = pack(opcode=MjISAY, id8=0x1122334455667788, src=1, dst=DST_CHANNEL & 0xFF)
    cl = chat_line(chat, b"hello", b"x")
    back = parse_chat_line(cl)
    print("chat    %s" % cl.decode("latin1"))
    if back["opcode"] != MjISAY or back["text"] != b"hello" or back["dst"] != -1:
        print("FAIL: chat round trip %r" % back); ok = False
    if line_for(chat, b"hello", b"x") != cl or line_for(rec) != line:
        print("FAIL: line_for picked the wrong form"); ok = False

    # --- the ground truth: a real line, off the wire ------------------------
    # The PS2 client's FIRST world message, captured 2026-08-12 from the auth
    # hop (gi003.pol.com:51241) after `NOTICE <peer> :GMJSG` was stripped. This
    # is the only assertion here that is not self-referential -- everything
    # above round-trips our own encoder against itself, which is exactly how
    # the big-endian bug survived. If this one fails, believe it over the rest.
    LIVE = (b"B^@@@@@@@@@BESut|KnLAVr@@P@C}\x7f`@ElSjAg\x7fWb@Ul")
    lr = decode(LIVE)
    lh = unpack(lr)
    if len(LIVE) != 44 or len(lr) != 32:
        print("FAIL: live line %d chars -> %d bytes" % (len(LIVE), len(lr))); ok = False
    if lh["length"] != 32:
        print("FAIL: live record length field = %d, expected 32 -- the multi-byte "
              "fields are little-endian" % lh["length"]); ok = False
    if lh["opcode"] != 64:                       # MjGETLNDV
        print("FAIL: live opcode %d, expected 64 MjGETLNDV" % lh["opcode"]); ok = False
    if (lh["src"], lh["dst"], lh["f13"], lh["sub"]) != (-3, -2, 0, 5):
        print("FAIL: live src/dst/f13/sub = %d/%d/%d/%d, expected -3/-2/0/5"
              % (lh["src"], lh["dst"], lh["f13"], lh["sub"])); ok = False
    if encode(lr) != LIVE:
        print("FAIL: live line does not re-encode to itself"); ok = False
    print("live    %s" % describe_short(lh))

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--decode", metavar="LINE")
    a = ap.parse_args()
    if a.decode:
        rec = decode(a.decode)
        print(rec.hex())
        for k, v in unpack(rec).items():
            print("  %-8s %r" % (k, v))
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
