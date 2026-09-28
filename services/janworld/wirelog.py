"""The janhourou log channel: timestamps, hexdumps, and one record described field by field."""
import os
import janwire                                                  # noqa: E402
from . import opcodes

LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")


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
    if 14 <= h["opcode"] <= 21 and h["sub"] < len(opcodes.NOTICES):
        note = "  sub?=%s" % opcodes.NOTICES[h["sub"]]
    return ("%s  len=%d src=%d dst=%s f13=%d f16=%d sub=%d payload=%016x%s"
            % (opcodes.opname(h["opcode"]), h["length"], h["src"], dst,
               h["f13"], h["f16"], h["sub"], h["payload"], note))
