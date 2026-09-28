"""The lobby push queue: records for a member who is not the one talking,
drained on their next line.
"""
import threading
import janwire                                                  # noqa: E402
from .deps import janseats
from . import wirelog


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
        wirelog.log("   queued for member %d: %s" % (member_id, why))
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
        wirelog.log("   -> %s queued for %d member(s) of table %s" % (why, n, tid))
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
