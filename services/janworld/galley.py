"""Spectators (the gallery): who may watch a table, the watch request and
leave, the web board's rule.
"""
import os
import janwire                                                  # noqa: E402
from .deps import janrules, janseats
from . import dispatch, opcodes, seating, wirelog

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
    tid = seating._table_for(member_id, h["id8"])

    def answer(result, why, slot=0):
        reply = opcodes.ack_for(rec, result=result, extra=slot)
        wirelog.log("%s   -> %s  result=%d/%s  [%s: %s]"
            % (peer, wirelog.describe(reply), result, opcodes.RESERVE_RESULTS.get(result, "?"),
               seating._tl(tid) if tid else "table ?", why))
        return [janwire.encode(reply)]

    if h["opcode"] == opcodes.MjGALLEYLEAVEREQ:
        released = False
        if dispatch.GAMES is not None and hasattr(dispatch.GAMES, "forget_spectator"):
            released = dispatch.GAMES.forget_spectator(member_id, "MjGALLEYLEAVEREQ",
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
    if (janseats is None or not janseats.enabled() or dispatch.GAMES is None
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
        if hasattr(dispatch.GAMES, "forget_spectator"):
            dispatch.GAMES.forget_spectator(member_id, "re-Watch: stale membership dropped",
                                   store=False)
        return answer(janseats.GALLEY_DUP,
                      "already in this gallery -- 'Already spectating'; the stale "
                      "membership is dropped so the next Watch is a 1")
    t = dispatch.GAMES.by_lobby.get(int(tid))
    if (not janseats.is_in_play(tid) or t is None
            or getattr(t, "state", None) in ("over", "finished")):
        return answer(janseats.GALLEY_NOT_PLAYING,
                      "nothing being played there (in_play=%s, table=%s) -- 'the "
                      "table status changed'"
                      % (janseats.is_in_play(tid),
                         getattr(t, "state", None) if t is not None else None))
    name = _cstr(rec, GALLEYREQ_NAME_OFF, GALLEYREQ_NAME_LEN) or seating.display_name(member_id, peer)
    result, slot = janseats.gallery_join(tid, member_id, name, polid=h["payload"])
    if result != janseats.GALLEY_OK:
        return answer(result, "the seat store refused the gallery entry")
    dispatch.GAMES.add_spectator(t, member_id, slot)
    wirelog.log("%s      SPECTATOR: member %s (%s, PolID %#x) watches %s from gallery slot "
        "%d (GALLEY_LIMIT %d%s); copies start at its first MjGALLEYACK"
        % (peer, member_id, name, h["payload"], seating._tl(tid), slot, limit,
           ", chat suppressed" if limit == GALLEY_NO_CHAT else ""))
    return answer(janseats.GALLEY_OK, "spectator, slot %d" % slot, slot=slot)
