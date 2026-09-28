"""Serving the 2004 client build: which build is asking, and each record reshaped for it."""
import os
from .deps import janmsgs2004
from . import dispatch, opcodes, wirelog


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
        wirelog.log("%s   build check raised: %s -- serving the 2002 shape" % (peer, e))
        return False


def _table_scores_names(member_id):
    """`(scores, names)` for the table `member_id` sits at, for MjTSUMO's two
    new 2004 fields. Both are drawn on the 2004 score plates, and neither
    exists in the 2002 record, so they come from the table rather than from
    the record being converted."""
    scores, names = (0, 0, 0, 0), (b"", b"", b"", b"")
    if dispatch.GAMES is None:
        return scores, names
    try:
        t = dispatch.GAMES.by_member.get(int(member_id))
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
                touched.append("%s %dB->%dB" % (opcodes.opname(op), len(r), len(new)))
            out.append(new)
        except Exception as e:                                  # never fatal
            wirelog.log("%s   2004 conversion of %s raised: %s -- sending the 2002 "
                "shape" % (peer, opcodes.opname(r[0x12]), e))
            out.append(r)
    if touched:
        wirelog.log("%s   2004 layout: %s" % (peer, ", ".join(touched)))
    return out
