"""The server's own voice in the room: an operator posts an in-room notice (or
a kick out of the title), and every Jan client picks it up on its next line.

The 2004 client has three notices only the SERVER can send (notices.py has
the measurements): CmNOTICEMSGOPEN (94) opens the GM notice window with a
line of text, CmNOTICEMSGCLOSE (95) closes it, CmNOTICEJANHOLOWKICK (96)
sends the client out of JongHoLow. Retail used them for maintenance warnings
and for removing a player; nothing here had ever sent one.

HOW A POST TRAVELS. The tool that posts (`tools/jan_gmnotice.py`, or anything
holding this module) is not the process that talks to the clients, so a post
is a document under the Valkey key `jan:gm-notice` (janstore.gmnotice_key):

    {"seq": n, "posts": [{"seq", "op": "open"|"close"|"kick", "text",
                          "member": 0 or a member id, "at", "until"}, ...]}

The game band (`janworld.dispatch.take_pending`, which every Jan line and every
`<DR>` poll of a client in the room already goes through) hands each member
the posts it has not had yet and that are still current. A post addressed to
member 0 is for everyone; a kick is always addressed to one member. Delivery
is remembered in memory per member, so a restarted process re-delivers a
notice that is still open (the window just opens again), never a kick that
was already delivered more than `KICK_TTL_S` ago.

    POL_JAN_GMNOTICE=0       nothing is delivered (posting still works)
    POL_JAN_GMNOTICE_KEY     the Valkey key, default jan:gm-notice
"""
import os
import threading
import time
from . import notices, wirelog

try:
    import janstore                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janstore = None

#: How long an OPEN stays deliverable when the poster gives no time: the
#: client's own window lasts 0x1c20 frames (two minutes at 60 fps), so a
#: member who walks in later than that would see a stale line.
OPEN_TTL_S = 120
#: How long a kick waits for its member's next line.
KICK_TTL_S = 600
#: Posts kept in the document (the oldest fall off).
KEEP = 32

_LOCK = threading.Lock()
_SENT = {}                      # member id -> highest post seq delivered
_SHARED = [None]


def enabled():
    return os.environ.get("POL_JAN_GMNOTICE", "1") == "1"


def _store():
    if janstore is None:
        return None
    if _SHARED[0] is None:
        _SHARED[0] = janstore.Snapshot(janstore.gmnotice_key())
    _SHARED[0].key = janstore.gmnotice_key()
    return _SHARED[0]


def _read():
    st = _store()
    if st is None:
        return {}
    try:
        return st.read() or {}
    except Exception:                                           # noqa: BLE001
        return {}


def post(op, text="", member=0, ttl=None, now=None):
    """Publish one post. `op` is "open", "close" or "kick"; `member` 0 = every
    Jan client (a kick needs a member). Returns the post, or raises
    ValueError on a bad request."""
    if op not in ("open", "close", "kick"):
        raise ValueError("op must be open, close or kick, not %r" % (op,))
    member = int(member or 0)
    if op == "kick" and member <= 0:
        raise ValueError("a kick names one member")
    if op == "open" and not str(text or "").strip():
        raise ValueError("an open notice needs text")
    st = _store()
    if st is None:
        raise ValueError("no live store (janstore is missing)")
    now = time.time() if now is None else float(now)
    if ttl is None:
        ttl = KICK_TTL_S if op == "kick" else OPEN_TTL_S
    with _LOCK:
        doc = dict(st.read() or {})
        seq = int(doc.get("seq") or 0) + 1
        entry = {"seq": seq, "op": op, "text": str(text or ""),
                 "member": member, "at": now, "until": now + float(ttl)}
        posts = [p for p in (doc.get("posts") or []) if isinstance(p, dict)]
        posts.append(entry)
        st.write({"seq": seq, "posts": posts[-KEEP:]})
    return entry


#: The name janhourou's facade exposes.
def gm_post(op, text="", member=0, ttl=None):
    """`post`, under the name `janhourou.gm_post` (for an operator tool or the
    admin service holding the title)."""
    return post(op, text, member=member, ttl=ttl)


def clear():
    """Forget every post (a test, or an operator starting over)."""
    st = _store()
    if st is not None:
        try:
            st.clear()
        except Exception:                                       # noqa: BLE001
            pass
    with _LOCK:
        _SENT.clear()


def _record_for(p):
    op = p.get("op")
    if op == "open":
        return notices.gm_notice_open(p.get("text") or "")
    if op == "close":
        return notices.gm_notice_close()
    if op == "kick":
        return notices.gm_kick()
    return None


def pending_for(member_id, now=None, peer="-"):
    """The notice records `member_id` has not had yet, oldest first.

    +0x18 is 0 ("everyone") on every record: the record goes to this
    member's connection only, so the client's own-PolID test has nothing to
    discriminate, and 0 is the value the client accepts unconditionally."""
    try:
        member_id = int(member_id or 0)
    except (TypeError, ValueError):
        return []
    if member_id <= 0 or not enabled():
        return []
    doc = _read()
    top = int(doc.get("seq") or 0)
    if not top:
        return []
    now = time.time() if now is None else float(now)
    with _LOCK:
        have = _SENT.get(member_id)
        if have is None:
            # A member first seen now gets only what is still current.
            have = 0
        if have >= top:
            return []
        out, kicked = [], False
        for p in sorted((p for p in (doc.get("posts") or []) if isinstance(p, dict)),
                        key=lambda p: int(p.get("seq") or 0)):
            seq = int(p.get("seq") or 0)
            if seq <= have:
                continue
            target = int(p.get("member") or 0)
            if target and target != member_id:
                continue
            if float(p.get("until") or 0) < now:
                continue
            rec = _record_for(p)
            if rec is None:
                continue
            out.append(rec)
            kicked = kicked or p.get("op") == "kick"
            wirelog.log("%s   GM NOTICE %s #%d for member %s%s"
                        % (peer, opcodes_name(rec), seq, member_id,
                           (": %r" % p.get("text")) if p.get("text") else ""))
        _SENT[member_id] = top
    if kicked:
        _release_kicked(member_id, peer)
    return out


def opcodes_name(rec):
    from . import opcodes
    return opcodes.opname(rec[0x12])


def _release_kicked(member_id, peer):
    """A kicked client leaves the title without a word to us: free its seat
    and gallery place now, the way MjLEAVECONTENTS would have."""
    try:
        from .deps import janseats
        if janseats is not None and janseats.enabled():
            freed = janseats.release_member(member_id, why="gm-kick")
            if hasattr(janseats, "gallery_drop"):
                janseats.gallery_drop(member_id)
            if freed:
                wirelog.log("%s      GM kick: released member %s's seat at table %s"
                            % (peer, member_id, freed))
    except Exception as e:                                      # noqa: BLE001
        wirelog.log("%s      GM kick: releasing member %s raised %s"
                    % (peer, member_id, e))
