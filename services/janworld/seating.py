"""Who sits where: the room a member is in, the seats the lobby store holds,
and the names drawn.
"""
import os
from .deps import accounts, janlobby, janseats
from . import notices, wirelog

#: `callable() -> {chan: {"who": [{"member_id": ..}, ..]}}` -- the IRC room
#: registry, as `responders._live_rooms` publishes it. None = no registry
#: (standalone runs), so every table resolves to room 0.
LIVE_ROOMS = None

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
            wirelog.log("  display_name(%s): %s -- drawing a blank rather than a guess"
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
    out = bytearray(4 * notices.NAME_SLOT)
    for s in range(4):
        nm = ""
        if table is not None:
            nicks = getattr(table, "nicks", None)
            if nicks and nicks[s]:
                nm = nicks[s]
                nm = nm.decode("cp932", "replace") if isinstance(nm, bytes) else str(nm)
            elif getattr(table, "bots", None) and s in table.bots:
                nm = BOT_NAME % s
        raw = nm.encode("cp932", "replace")[:notices.NAME_SLOT]
        out[s * notices.NAME_SLOT:s * notices.NAME_SLOT + len(raw)] = raw
    return bytes(out)
