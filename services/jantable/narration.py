"""The plain-English move trace (TRACE), and the tile, point and opcode names it prints."""
import os
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402


def _web_tile(t):
    """A tile for the web board: '1m'..'9m', '1p'.., '1s'.., honours '1z'..'7z'
    (E S W N haku hatsu chun), a red five '0m'/'0p'/'0s'; None for no tile."""
    if t is None or t < 0:
        return None
    k = mj.kind(t)
    if k >= 27:
        return "%dz" % (k - 26)
    return "%d%s" % (0 if mj.is_red(t) else k % 9 + 1, "mps"[k // 9])

# The client's in-game loop sets DAT_00409b00 to a phase id in every arm; we
# keep the same names so a log line here can be lined up against one from the
# console's own tracing.
PHASE = {0x22: 4, 0x24: 5, 0x26: 6, 0x2E: 7, 0x3C: 8, 0x28: 9, 0x2A: 10,
         0x38: 12, 0x3A: 13, 0x2C: 14}

#: Human-readable per-move narration. janhourou.py points this at its log() so
#: the trace lands in janhourou.log alongside the wire records -- so a screen
#: event can be lined up against BOTH the raw record and a plain-English move.
#: Left None in the selftests (they assert on state, not prose) and gated by
#: POL_JAN_TRACE so it can be turned off without a code change.
TRACE = None
TRACE_ENABLE = os.environ.get("POL_JAN_TRACE", "1") == "1"


def _trace(msg):
    if TRACE is not None and TRACE_ENABLE:
        try:
            TRACE("GAME  " + msg)
        except Exception:
            pass


#: The four seat positions as the LOCAL player sees them, so a trace line names
#: where a tile should land on screen. Filled per-table from `self.live` (the
#: human is the live seat); a spectator or an all-bot table just reads "seatN".
_SEATPOS = {0: "bottom/self", 1: "right", 2: "across", 3: "left"}


def _round_p(x):
    """A P value (the +/-45 result point) as the INTEGER the results screen
    draws. Half away from zero, so +4.5 -> 5 and -4.5 -> -5; `round()` would
    make both 4 and turn a symmetric pair of results into an asymmetric one.
    The columns are u32-packed ints (MjGAMERESULTHALF1) and the screen's digit
    renderer has no decimal place -- see msg_results."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _tile(t):
    """A tile id as a short readable name, e.g. '3m', '0p' (red 5p), 'E', 'back'."""
    if t is None:
        return "--"
    try:
        return mj.name(t)
    except Exception:
        return "?%02x" % (t & 0xFF)


def M_NAME(op):
    for k, v in vars(M).items():
        if k.startswith("Mj") and v == op:
            return k
    return "op%#x" % op
