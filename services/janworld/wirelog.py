"""The janhourou log channel: timestamps, hexdumps, and one record described field by field."""
import os
import sys
import threading
import janwire                                                 # noqa: E402
from . import opcodes

LOG_DIR = os.environ.get("POL_LOG_DIR", "/logs")


#: THE LOG IS ROTATED, the way the core rotates every channel it writes
#: (openlobby srvcore `_log_handle_locked`): at the cap the file becomes
#: `<channel>.log.1`, the older ones shift up, and POL_LOG_KEEP generations
#: are kept. The trace grew to 22 MB in a month without this (a hexdump of
#: every record). POL_JAN_LOG_MAX_MB overrides the core's POL_LOG_MAX_MB for
#: this file only; 0 appends for ever, as before.
#:
#: The file is still opened per line, not held open: more than one process
#: (authsess and login) writes the same channel, and a writer that held its
#: handle across another process's rotation would keep appending to the
#: renamed `.1` file. Opening per line follows the rename at once.
_LOG_LOCK = threading.Lock()


def _log_max_bytes():
    raw = os.environ.get("POL_JAN_LOG_MAX_MB")
    if raw is None or not raw.strip():
        raw = os.environ.get("POL_LOG_MAX_MB", "8")
    try:
        return int(float(raw) * 1024 * 1024)
    except ValueError:
        return 8 * 1024 * 1024


def _log_keep():
    try:
        return max(1, int(os.environ.get("POL_LOG_KEEP", "3")))
    except ValueError:
        return 3


def _rotate_if_full(path):
    """Shift `path` to `path.1` (and `.1` to `.2`, ...) once it has reached
    the cap. Checked BEFORE a write, so a log inherited oversized from the last
    run is rotated on its first new line, not after growing another cap."""
    cap = _log_max_bytes()
    if cap <= 0:
        return False
    try:
        if os.path.getsize(path) < cap:
            return False
    except OSError:
        return False
    for i in range(_log_keep(), 0, -1):
        src = path if i == 1 else "%s.%d" % (path, i - 1)
        dst = "%s.%d" % (path, i)
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                return False
    return True


def _stamp():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(msg, channel="janhourou"):
    line = "%s [%s] %s" % (_stamp(), channel, msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # a cp1252 console on a Windows host must not cost the caller its reply
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "backslashreplace").decode(enc), flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, channel + ".log")
        with _LOG_LOCK:
            _rotate_if_full(path)
            with open(path, "a", encoding="utf-8") as f:
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
