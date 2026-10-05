"""Janhourou as a title plugin for the OpenLobby core.

This module is what `POL_TITLES=jantitle` loads into the core's `login` and
`authsess` processes. It holds every piece of Janhourou logic that used to
live inside the core's responders.py -- the table-row delta stream behind
`<DR>`, the ranking and character-pool replies, the parlour, room and table
lists built from live state, the player save with the live record patched
in, the game records queued for a seat that is waiting, and the content
profile -- registered with the core through `titles.Title` (see
services/titles.py in OpenLobby for the contract). The game itself (the
deal, the calls, the scoring, the computer players) is `jangame.py`,
`janmahjong.py` and their siblings; `janhourou.py` is the 51272 parlour
listener and the record codec; this module is the seam that hands the core's
traffic to them.

Nothing here imports `responders`. The core plumbing these functions use is
bound by name through `titles.core` (`_CORE_NAMES` below): the moved code
keeps the names it always had, and running this module outside the core (the
selftests) binds the standalone defaults instead.
"""
import json
import os
import struct
import threading

import titles
from titles import core as _core

import janhourou
import janlobby
import janrules
import jansave
import janseats
import janstats

HERE = os.path.dirname(os.path.abspath(__file__))

#: Every core name the moved code reaches for. Bound at import and again when
#: the core (re)binds its handle; see titles.Core.__doc__ for what each is.
_CORE_NAMES = (
    "log", "NoPad", "PRESENCE", "ROOMS", "accounts", "polpro", "RESOURCE_DIR",
    "_game_notice_line", "_session_get", "_session_sid", "_member_content_id",
    "_member_display_name", "_live_rooms", "_title_zone", "_title_zone_lease",
    "_content_profiles", "_peer_build", "_client_builds",
)

#: Used when the core binds no `_peer_build` (an older core, or the selftests):
#: a per-thread marker nobody sets, so every client reads as the 2002 build.
_OWN_PEER_BUILD = threading.local()


def _rebind():
    for name in _CORE_NAMES:
        try:
            globals()[name] = getattr(_core, name)
        except AttributeError:
            globals()[name] = None
    if globals().get("_peer_build") is None:
        globals()["_peer_build"] = _OWN_PEER_BUILD


_rebind()

#: THE PROVEN CONFIGURATION AS DEFAULTS. The live service ran Janhourou with
#: every knob at the value the code reads when the environment says nothing
#: (its compose set only the parlour port), so there is nothing to pin here;
#: the knobs stay readable where they are used, each with its reason:
#: POL_JAN_DELTAS, POL_JAN_DELTA_CHUNK, POL_JAN_IDLE_PUSH, POL_JAN_LOBBY_LIVE,
#: POL_JAN_PUSH_CHUNK, POL_JAN_RANK, POL_JAN_SAVE_LIVE, POL_JAN_SAVE_SEAT,
#: POL_JAN_TITLE_ZONE.
RELEASE_DEFAULTS = {}
for _k, _v in RELEASE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

#: The reply templates ship with the title: config/polpro.json in a checkout,
#: /app/polpro.json in the image (the Dockerfile copies it beside the code).
POLPRO_SPEC_CANDIDATES = (
    os.path.normpath(os.path.join(HERE, os.pardir, "config", "polpro.json")),
    os.path.join(HERE, "polpro.json"),
)


# ---------------------------------------------------------------------------
# the roster delta stream, the rankings, the character pool
# ---------------------------------------------------------------------------
def _jan_roster_delta_reply(payload):
    """Janhourou's `<DD>` answer to a class-L `<DR>`. Returns (reply, handled).

    WHY THIS EXISTS: Jan's `b/g/PTL` is a SNAPSHOT taken at room entry, and
    until 2026-09-03 nothing ever updated it -- a reservation changed the seat
    count on the server and no client in the room could see it, including the
    one that made it. But the client had been asking all along: `cp__002fb218`
    polls `<DR>(serial)` about every 2 s while LobbyState == 15, and we logged
    every one of them as "no template for this command -- silent".

    THE MECHANISM IS TETRA MASTER'S, REUSED -- NOT A NEW PROTOCOL. `cp.c` and
    `lb.c` are two of the five sqMg translation units statically linked into
    BOTH game modules, so Janhourou already contains the whole consumer:
    `cp__002fae98` walks the `<DD>` pairs and `cp__002fb290` applies them, its
    command-0x15 arm parsing a `TD` group with `lb__002f9e10` into 104 bytes
    and copying them over the row `lb__002f9f18` matched BY NAME. `TD` is
    polpro tag index 21 == 0x15, which is what ties the tag to that arm.

    Three answers, and the difference between them is the whole point:

        deltas_after -> [..]   send them; the rows change with no fetch at all
        deltas_after -> []     CURRENT: say nothing. Silence is the right
                               answer to a poll with no news, and it is what
                               keeps this from becoming a re-fetch storm.
        deltas_after -> None   cannot serve from the log -- send `<DO>`, which
                               `cp__002fb7d0` turns into a snapshot reload.

    `<DO>` IS EMITTED HERE RATHER THAN LEFT TO THE TEMPLATE. There is no
    `MJS:DR` entry in polpro.json and class L withholds the `*` wildcard, so
    falling through is SILENCE -- and a client that had fallen out of the log
    would then never be told to reload. a title can fall through because its own `<DR>` entry
    exists; we cannot.

    THE CHUNK LIMIT IS TM'S, MEASURED THE HARD WAY: a batch the client cannot
    fully apply is answered with a reload instead, because A PREFIX HAS NEVER
    BEEN OBSERVED TO ADVANCE A CLIENT -- see `_roster_delta_reply`'s banner for
    the logs behind that. Jan serves four tables, so a full resync is four
    deltas and this should effectively never fire.
    """
    if (janseats is None or janlobby is None
            or not janseats.enabled()
            or os.environ.get("POL_JAN_DELTAS", "1") != "1"):
        return None, False
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "DR":
            return None, False
        have = (groups[0][1] or ["0"])[0]
        # THE POLL IS THE LEASE (finding 24). A player waiting at a table sends
        # no game-band line for as long as they wait -- this `<DR>` every ~2 s
        # is their heartbeat -- so renew the seat here or a 15-minute wait
        # unseats them silently.
        _dr_member = _session_get("member_id")
        if _dr_member:
            janseats.touch(_dr_member)
        # THE ROOM (finding 30). The sequence and the rows are PER ROOM --
        # a `TD` delta is applied to the row matched BY NAME, and every
        # room's rows share the four names, so Room 2-3's delta landing in
        # Room 1-1 would overwrite the wrong row. A poller the registry
        # cannot place (no member, or not in a Jan channel) is answered
        # against room 0, the unknown-room set its PTL would have carried.
        _dr_room = _jan_member_room(_dr_member)
        _dr_rid = _dr_room.id if _dr_room is not None else 0
        # HEAL BEFORE ANSWERING. A seat expires at READ time with nobody
        # writing anything, so a row can move with no bump behind it, and this
        # poll is the only regular tick this subsystem gets.
        # (tm-table-state-invariant, one game over: a repair that only runs on
        # new writes never heals old state.)
        janseats.reconcile()
        # ...and an expiry that took a master out promoted somebody: queue
        # the MjNOTICEMEMBER that tells them, plus the time-up warnings.
        if janhourou is not None:
            try:
                janhourou.notify_master_changes(peer="dr-band")
                janhourou.warn_expiring_seats(peer="dr-band")
            except Exception as _e:
                log("authserv", f"  jan roster: master/expiry notices raised "
                                f"({_e!r}) -- continuing")
        cur = janseats.sequence(_dr_rid)
        deltas = janseats.deltas_after(have, _dr_rid)
    except Exception as e:
        log("authserv", f"  jan roster: <DR> raised ({e!r}) -- silent")
        return None, False
    _where = (_dr_room.channel if _dr_room is not None
              else "room UNKNOWN (room 0)")
    chunk = int(os.environ.get("POL_JAN_DELTA_CHUNK", "4") or 0)
    if deltas is not None and chunk and len(deltas) > chunk:
        log("authserv", f"  jan roster: <DR>({have}) in {_where} is owed "
                        f"{len(deltas)} deltas, more than the {chunk} one "
                        f"notice carries -- answering <DO>, i.e. a RELOAD")
        deltas = None
    if deltas is None:
        log("authserv", f"  jan roster: <DR>({have}) in {_where} cannot be "
                        f"served from the log (it is at {cur}) -- <DO>, i.e. "
                        f"reload the snapshot")
        return polpro.build([("DO", [str(cur)])]), True
    if not deltas:
        return None, True               # CURRENT -- silence, deliberately
    out = [("DD", ["0"]), ("DN", [str(len(deltas))])]
    rows = []
    for seq, tid in deltas:
        # `tid` is the ROOM's composite; the row it renders carries the base
        # id and the shared name, which is what the client matches on.
        values = janlobby.table_values_for(tid, _live_rooms())
        if values is None:
            log("authserv", f"  jan roster: delta {seq} names table {tid}, "
                            f"which is not in JAN_TABLES -- <DO> instead")
            return polpro.build([("DO", [str(cur)])]), True
        out.append(("DC", [str(int(seq))]))
        out.append(("TD", [str(v) for v in values]))
        rows.append("%d:%s seated=%s state=%s"
                    % (seq, values[0], values[5], values[3]))
    log("authserv", f"  jan roster: <DR>({have}) in {_where} -> {len(deltas)} "
                    f"delta(s), now at {cur} -- " + "  ".join(rows))
    return polpro.build(out), True


def _jan_rank_reply(payload):
    """`MJS:RR` -> `<RF>(U/g/MJS_RANKLIST<n>)` + `<LN>(rows)`, or fall through.

    THE SHAPE IS rkc.c's (read 2026-09-04): `rkc__002f1cb0` maps the LEAD
    group's tag -- `<RF>` (91) = success, `<RG>` (92) = failure, anything
    else dropped -- and `rkc__002f1658` copies `<RF>`'s string into a 63-byte
    path buffer and `<LN>` into the record count. `sqMgRkcpReadRankList` then
    reads `count x 232` bytes of THAT PATH (the `<PO>` profile struct, one
    per row, already sorted: the client draws `index + 1` as the rank). The
    category is `<RR>`'s own value (0 Jan Rating, 1 Title score, 2 Overall
    gamble, 3 Weekly, 4 Event); `<PI>` carries the asker's PolID/domain/
    volume, not the category.

    The path carries the category so the FETCH (login band, `_rank_list_blob`)
    builds the same order this reply promised -- the same split TM's lists
    live with. `<LN>` 0 is legal (an empty list, ranking_win.c:847). Rows are
    capped at janstats.RANK_LIST_MAX; the client's record array holds 113.
    """
    if janstats is None or polpro is None:
        return None, False
    if os.environ.get("POL_JAN_RANK", "1") != "1":
        return None, False
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "RR":
            return None, False
        try:
            cat = int((groups[0][1] or ["0"])[0])
        except ValueError:
            cat = 0
        if not 0 <= cat < len(janstats.RANK_CATEGORIES):
            log("authserv", f"  jan rankings: <RR>({cat}) is not a category "
                            f"0..4 -- answering <RG>")
            return polpro.build([("RG", ["-8706"])]), True
        rows = len(janstats.rank_list(cat, remember=False))
        # AN EMPTY RANKING IS NOT AN EMPTY LIST, it is error -740. The 2004
        # build tests for exactly that code and answers it with its own
        # dialog, "Nobody is registered in this ranking yet." plus an OK
        # button (2004 module 0x00327404 tests -740 and branches to 0x00327418,
        # which raises the string at 0x0049eed0 and returns to state 11;
        # -8809 and anything else land on the POL error -13053 arms at
        # 0x00327450 / 0x0032748c). Served `<LN>`(0) instead, the screen goes
        # on to read a zero-row file and draws nothing, which reads as a hang.
        # The 2002 build has neither the code nor the string, so it keeps the
        # empty list `<LN>`(0) is documented for. POL_JAN_RANK_EMPTY=0 restores
        # the old answer for both.
        if (rows == 0 and os.environ.get("POL_JAN_RANK_EMPTY", "1") == "1"
                and _jan_peer_is_2004()):
            log("authserv", f"  jan rankings: <RR>({cat}) = "
                            f"{janstats.RANK_CATEGORIES[cat]} is empty -> "
                            f"<RG>(-740), the 2004 build's 'nobody is "
                            f"registered in this ranking yet'")
            return polpro.build([("RG", ["-740"])]), True
        path = janstats.rank_list_path(cat)
        log("authserv", f"  jan rankings: <RR>({cat}) = "
                        f"{janstats.RANK_CATEGORIES[cat]} -> <RF>({path}) "
                        f"<LN>({rows})")
        return polpro.build([("RF", [path]), ("LN", [str(rows)])]), True
    except Exception as exc:
        log("authserv", f"  jan rankings: reply failed ({exc!r}) -- the "
                        f"polpro.json entry still stands")
        return None, False


def _jan_rank_blob(path):
    """The `U/g/MJS_RANKLIST<n>` bytes for the fetch: every member with a game
    on record, named from the accounts DB, sorted for the path's category."""
    if janstats is None:
        return None
    cat = janstats.rank_category_of(path)
    if cat is None:
        return None
    names, cids = {}, {}
    for m in janstats.all_members():
        try:
            names[m] = _member_display_name(m) or None
            cids[m] = 0
            if accounts is not None:
                cids[m] = accounts.content_id_int(
                    _member_content_id(m, _JAN_CONTENT_ID)) or 0
        except Exception:
            continue
    try:
        blob = janstats.rank_list_blob(cat, names=names, content_ids=cids)
    except Exception as exc:
        log("lobby", f"  3:0 {path!r}: rank list build failed ({exc!r})")
        return None
    log("lobby", f"  3:0 {path!r}: {len(blob) // janstats.PROFILE_REC} row(s) "
                 f"of {janstats.RANK_CATEGORIES[cat]}")
    return blob


def _jan_member_for_cid(cid):
    """(member id, handle name) for a Jan Content ID, or (None, None)."""
    if accounts is None or not cid:
        return None, None
    try:
        db = accounts.connect()
        try:
            row = accounts.handle_by_content_id(db, cid)
            if row is None:
                return None, None
            return int(row["member_id"]), (row["handle_name"] or "").strip() or None
        finally:
            db.close()
    except Exception as exc:
        log("authserv", f"  jan profile: member lookup for {cid} failed ({exc!r})")
        return None, None


def _jan_show_flags(cid):
    """`{k: byte}` from the stored `<GR>` write: `<NO>(k, v)` sets reply value
    v41+k TO v, VERBATIM.

    WARNING: v IS NOT A BOOLEAN, and reading it as one threw away every per-field
    choice the player made. Live `<GR>` writes from one client carry
    `<NO>(22,3) <NO>(16,3) ...` and, after the player changed the settings,
    `<NO>(22,1) <NO>(16,1) ...`: the values toggled between are 3 and 1 and
    NEVER 0, so `1 if v == 0 else 0` mapped BOTH to 0 and the next `<PO>` read
    back the same all-zero tail whatever was picked. That is why only the goal
    field appeared to save. PROFILE_PO's own note is the rule -- `<NO>(i,v)`
    addresses the byte array the reply carries at value[41+i] -- and a byte
    array written by index is read back by index, so what 1 and 3 MEAN does
    not have to be known to carry them.

    The other three groups a SET sends, per the same table: `<PP>` is v3
    (struct +0x1C, the goal field), `<FO>` is v8 (+0x2A) and `<VO>` is v40
    (+0xC8). The group the old code looked for, `PO`, is the REPLY code: a
    client never sends it in a `<GR>`, so that arm never fired and `<FO>` was
    dropped."""
    out, extra = {}, {}
    rec = (_content_profiles() or {}).get(str(cid)) or {}
    for g, vals in rec.get("groups") or []:
        try:
            if g == "NO" and len(vals) >= 2:
                out[int(vals[0])] = int(vals[1]) & 0xFF
            elif g == "PP" and vals:
                extra[3] = int(vals[0])
            elif g == "FO" and vals:
                extra[8] = int(vals[0])
            elif g == "VO" and vals:
                extra[40] = int(vals[0])
        except (TypeError, ValueError):
            continue
    return out, extra


def _jan_pool_reply(payload):
    """`MJS:CR` (+`<AN>`): the member naming its Jan character.

    `<CS>` accepts; a name another member's record already carries is refused
    with `<NS>(-8700)` -- handlesel.c:394-415 draws that code as "That player
    name is already in use" (-8711 is the vulgar-name arm). The name is kept
    in janstats (`note_name`) so the popup, the rank list and the table
    panels all print what the player typed.
    """
    if janstats is None or polpro is None:
        return None, False
    try:
        groups = dict(polpro.parse(payload))
        if "CR" not in groups:
            return None, False
        member = _session_get("member_id")
        an = (groups.get("AN") or [""])[0].strip()
        if not member or not an:
            return None, False
        for other in janstats.all_members():
            if other != int(member) and janstats.load(other).get("name") == an:
                log("authserv", f"  jan pool: member {member} asked for the "
                                f"name {an!r}, held by member {other} -- <NS>")
                return polpro.build([("NS", ["-8700"])]), True
        janstats.note_name(member, an)
        log("authserv", f"  jan pool: member {member} is {an!r} -- <CS>")
        return polpro.build([("CS", ["0"])]), True
    except Exception as exc:
        log("authserv", f"  jan pool: reply failed ({exc!r}) -- the template "
                        f"answers")
        return None, False


# ---------------------------------------------------------------------------
# the game band: the peer pin, the sweeper push, the queued records
# ---------------------------------------------------------------------------
#: Janhourou's content id, and therefore its presence zone -- the same number the
#: games menu and the launch gate use (contentlist.CONTENT_NAMES[3]). Named here
#: because the title is inferred from traffic rather than reported by the client;
#: see `_title_zone_lease`.
_JAN_CONTENT_ID = 3


#: member id -> (target, nick, srv, session id, ChatSession) from the last
#: GAME-band line they sent. Process-local and deliberately so: it is a
#: routing convenience, and a member we have not heard from on that band has
#: nothing queued for them anyway. The ChatSession (5th, may be None) is what
#: the sweeper pushes on; older 4-tuples are still read everywhere.
_JAN_GAME_PEER = {}
#: member -> the address its game band last spoke from, for `_jan_pending_lines`
#: when it runs on the sweeper thread rather than the member's own.
_JAN_PEER_IP = {}


def _jan_sweeper_push(members):
    """`jangame.Manager.on_swept`: the deadline sweeper (a thread in THIS
    process, audit finding 8) just queued records for these members -- push
    them NOW down the game-band session each one last spoke on, instead of
    waiting for the 2 s idle tick. Same pin as `_jan_pending_lines` (the
    session id, not the nick), same chunk cap as the idle push, same
    `ChatSession.send` door (it takes the socket lock, so a connection thread
    writing at the same moment is safe). A member whose session is unknown or
    dead keeps its records queued: the idle tick / their next line drains."""
    chunk = int(os.environ.get("POL_JAN_PUSH_CHUNK", "2") or 0) or None
    pad = b" " if os.environ.get("POL_GAME_NOTICE_PAD") == "space" else b""
    for m in members or ():
        got = _JAN_GAME_PEER.get(m)
        sess = got[4] if got and len(got) > 4 else None
        if sess is None or not getattr(sess, "alive", False):
            continue
        try:
            lines = _jan_pending_lines(m, on_sid=got[3], limit=chunk)
        except Exception as exc:
            log("authserv", f"  sweeper push drain raised ({exc!r}) -- silent")
            continue
        sent = 0
        for ln in lines:
            if not sess.send([ln], pad_override=pad):
                log("authserv", f"  sweeper push to member {m} failed -- peer "
                                f"gone ({len(lines) - sent} record(s) lost)")
                break
            sent += 1
        if sent:
            log("authserv", f"  sweeper pushed {sent} jan record(s) unprompted "
                            f"to member {m}")


def _jan_pending_lines(member, on_sid=None, limit=None):
    """Jan game records queued for `member`, framed as game-band NOTICEs.

    Called from the `<DR>` path and the connection idle tick as well as the
    game path, because a player still in the room only ever sends `<DR>` and
    one waiting for their turn sends NOTHING -- see `janhourou.take_pending`.

    WARNING: `on_sid` PINS THE SOCKET, and it has to be the SESSION -- not the nick.
    A player holds more than one connection and they SHARE a nick, so a nick
    pin does not discriminate at all: measured live 2026-09-04, member 15's
    records went out on `127.0.0.1:50861` twice and then on `:50779`, and
    whatever took the second pipe was consumed into a socket the game was not
    reading. That is why a hand ran "a couple of rounds and then froze" -- it
    worked until a record happened to leave by the wrong door.

    A mismatch returns nothing and leaves the records QUEUED for the connection
    that does match.
    """
    if janhourou is None or not member:
        return []
    got = _JAN_GAME_PEER.get(member)
    if not got:
        return []           # never seen their game band; nothing to frame with
    # On the sweeper thread the per-thread build marker is unset, and an
    # in-game record is shaped by the build it is going to. Borrow the address
    # this member last spoke from, and put the thread back as it was.
    _had = hasattr(_peer_build, "ip")
    _was = getattr(_peer_build, "ip", None)
    if not _was:
        _peer_build.ip = _JAN_PEER_IP.get(member)
    try:
        return _jan_pending_lines_inner(member, got, on_sid=on_sid, limit=limit)
    finally:
        if _had:
            _peer_build.ip = _was
        elif hasattr(_peer_build, "ip"):
            del _peer_build.ip


def _jan_pending_lines_inner(member, got, on_sid=None, limit=None):
    if on_sid is not None and (len(got) < 4 or got[3] != on_sid):
        return []           # wrong connection -- leave them queued
    try:
        recs = janhourou.take_pending(member, peer="dr-band", limit=limit)
    except Exception as exc:
        log("authserv", f"  jan pending drain raised ({exc!r}) -- silent")
        return []
    if not recs:
        return []
    tgt, nk, sv = got[0], got[1], got[2]
    log("authserv", f"  jan: {len(recs)} queued record(s) for member {member} "
                    f"ride the <DR> reply")
    return [NoPad(_game_notice_line(b"GMJSG" + r, tgt, nk, sv)) for r in recs]


def _jan_idle_due(member, sid, sess_fresh=True, limit=None):
    """The Jan records a connection's idle tick should push, framed and ready.

    Split out so the gate can be tested: `tools/jan_delta_e2e` drives it with
    a stale session, which is the case that was broken.

    WARNING: SILENCE IS NOT A DEAD SOCKET HERE. The core's idle push reads a
    connection that has not spoken for `POL_PUSH_FRESH` seconds as a zombie,
    which is right for Tetra Master (a client in a match speaks every 15 s).
    A Jan client waiting for its turn record is quiet by construction, so that
    gate suppressed exactly the record the player was waiting on, and every
    delivery fell through to the 75 s deadline sweeper. The zombie hazard is
    still covered, and better: `on_sid` pins the drain to the session this
    member's game band last spoke on. `POL_JAN_PUSH_FRESH=1` restores the old
    coupling.
    """
    if janhourou is None or not member:
        return []
    if os.environ.get("POL_JAN_IDLE_PUSH", "1") != "1":
        return []
    if os.environ.get("POL_JAN_PUSH_FRESH") == "1" and not sess_fresh:
        return []
    if limit is None:
        limit = int(os.environ.get("POL_JAN_PUSH_CHUNK", "2") or 0) or None
    return _jan_pending_lines(member, on_sid=sid, limit=limit)


def notice(cls, payload, text, target, nick, srv, sess, tag=b"MJS"):
    """A game record (class G) on the game envelope: the title-zone lease,
    the peer pin for unprompted pushes, then janhourou.handle_line. The
    shared POLpro classes (P/R/A/L) are the core's: PASS hands them back.
    """
    if cls in (b"P", b"R", b"A", b"L"):
        return titles.PASS
    # *** THIS MESSAGE IS THE ONLY EVIDENCE THAT ANYONE IS IN JANHOUROU. ***
    # The client never reports content id 3 on 4:5 -- zero occurrences across
    # four log generations against 10,361 messages like this one -- so the friend
    # list showed a Janhourou player as "in the Viewer" for the whole session,
    # while Tetra Master (which does report) painted correctly.
    #
    # So we infer it from traffic, and we LEASE it rather than latch it, because
    # the client will not report the exit either. Every message renews it; when
    # the player stops, it lapses and the watcher pushes "left the title".
    # POL_JAN_TITLE_ZONE=0 turns the inference off without touching the game path.
    if os.environ.get("POL_JAN_TITLE_ZONE", "1") == "1":
        _title_zone_lease(_session_get("member_id"), _JAN_CONTENT_ID)
    try:
        # `member_id` keys the GAME MANAGER's table, the same way it keys
        # a title's save above: a hand of mahjong has to survive across
        # lines, and this band hands us one line at a time with no session
        # object of our own. 0 is fine for a single console.
        _jan_mid = _session_get("member_id") or 0
        if _jan_mid:
            # WARNING: A PUSH MUST NAME THE PEER ITSELF. An unprompted record has no
            # arriving message to answer, so the (target, nick) a reply gets
            # for free has to be remembered from the last line this member DID
            # send on the game band -- which they always have, because
            # reserving is a game-band message. Same law as TM's `source` split
            # for table-settings propagation, one game over.
            # WARNING: THE SESSION ID, NOT THE NICK. A player holds MORE THAN ONE
            # connection and they share a nick, so pinning by nick does not
            # discriminate: measured live 2026-09-04, member 15's records went
            # out on 127.0.0.1:50861 twice and then on :50779, and the ones
            # that took the second pipe were consumed into a socket the game
            # was not reading. `_session_sid()` is per-connection.
            # WARNING: ONLY FOR A 'B' RECORD. A chat 'A' line (MjISAY) is addressed
            # to the TABLE CHANNEL, so its `target` is a channel name; taking
            # it as the peer would frame every later push "from" the channel.
            if payload[1:2] != b"A":
                _JAN_GAME_PEER[_jan_mid] = (target, nick, srv, _session_sid(),
                                            sess)
                # ...and their ADDRESS, which is what says which build they
                # run. The sweeper pushes from its own thread, where the
                # per-thread marker is not set.
                _JAN_PEER_IP[_jan_mid] = getattr(_peer_build, "ip", None)
        reply = janhourou.handle_line(payload[1:], peer="auth-band",
                                      member_id=_jan_mid)
    except Exception as e:
        log("authserv", f"  janhourou raised on {payload[:80]!r}: {e} -- silent")
        return None
    if not reply:
        return None
    # Back the way it came: from the peer the game addressed (that nick is what
    # its own sqMg member table has bound to the id it is talking to), to us.
    # INFERENCE -- the prefix is the part with no direct evidence yet, so
    # POL_GAME_NOTICE_PREFIX switches it in one restart if the client ignores us:
    #   peer (default) -- ":<target>!~x@ NOTICE <nick> :..."  (empty host,
    #   srv            -- ":<srv> NOTICE <nick> :..."
    #   bare           -- "NOTICE <nick> :..."   (no prefix at all)
    #
    # SEVERAL LINES, like a title's own branch above: once a hand is running one
    # inbound record produces several outbound ones, because the three seats
    # with no client behind them play in between. A bare bytes reply stays a
    # single line, so nothing that predates the game manager changes.
    replies = reply if isinstance(reply, (list, tuple)) else [reply]
    return [NoPad(_game_notice_line(b"G" + tag + b"G" + r, target, nick, srv))
            for r in replies]


# ---------------------------------------------------------------------------
# the lobby band: declared lengths, the live lists, the save record
# ---------------------------------------------------------------------------
JAN_USERDATA_PATH = "U/g/MJSUserData"


#: Declared payload lengths of Janhourou's own resource fetches, merged into
#: the core's table. Each +4 is the 03:00 trailer. See responders._FETCH_PATHLEN
#: for the rule and the reader-side measurements these came from.
FETCH_PATHLEN = {
    # 976 bytes of data (JanHouRou.pex 0x002b2aa4: a2 = 976) + 4 checksum.
    "U/g/MJSUserData": 976 + 4,
    # b/g/MJSTableInfoSub -- JANHOUROU'S TABLE-SETTINGS BLOB, and the only fetch
    # in the game that is Jan's alone rather than shared sqMg. Requested by
    # ruleset.cc via lfile__002ae000(obj, key, "b/g/MJSTableInfoSub", 0x330) --
    # 816 bytes -- and driven by lfriend.cc's read machine (30 retries, 60-tick
    #
    # WARNING: UNLIKE b/g/PTL, ZEROS ARE NOT SEMANTICALLY RIGHT HERE. ruleset__003473b0
    # reads 33 twelve-byte records at +0x20 -- {group, config index, current,
    # default, legal-value mask lo, mask hi} -- so an all-zero blob yields
    # SelectItems = 0 for all 33 settings: a rules screen on which nothing can be
    # chosen. The length is still worth declaring (a short read is strictly
    # worse), but a WORKING settings screen needs a real blob, not the fallback.
    "b/g/MJSTableInfoSub": 816 + 4,
    # Janhourou 2004 only: `0x299bf0(..., "U/g/MJSOptionData", 144)`.
    "U/g/MJSOptionData": 144 + 4,
}


#: A FRESH resource is not necessarily all zeros -- some carry a magic the client
#: validates before it will use the blob at all.
#:
#: `U/g/MJSUserData` must open with the u32 **0x02030100**. Measured 2026-08-13 at
#: JanHouRou.pex 0x002b2af0, immediately after the 976-byte read:
#:
#:     lw    v1, 0x00446110      ; buf+0x00
#:     lui   v0, 0x0203
#:     ori   v0, v0, 0x0100      ; 0x02030100
#:     beq   v1, v0, <ok>
#:     addiu v0, zero, -650      ; else: the "save data is corrupt" return
#:
#: which is exactly the error the player saw once the black screen was gone: we
#: were serving 976 valid-length but all-zero bytes, so the check failed on the
#: first word. Everything past it is stats and counters that a new profile can
#: legitimately have at zero.
RESOURCE_INIT = {
    JAN_USERDATA_PATH: struct.pack("<I", 0x02030100),   # JanHouRou.pex 0x002b2af4
}


#: Content id 3. A member the traffic inference has parked in Janhourou gets its
#: lobby lists built live; everyone else -- above all Tetra Master, which SHARES
#: `b/g/ZL`, `b/g/RL000` and `b/g/PTL` -- keeps the stored file untouched.
#:
#:   POL_JAN_LOBBY_LIVE=1       the default: live, but only inside Janhourou
#:   POL_JAN_LOBBY_LIVE=force   live for every requester (ignores the title zone)
#:   POL_JAN_LOBBY_LIVE=0       off; the authored blobs answer, as before
#: PlayOnline's ACKY Gallery, sheets hnf202 and hnf203: the portraits the game
#: itself gives its computer players, 8 tiles each. An index is sheet * 8 + tile,
#: so the pair spans 202*8 .. 202*8+15.
_JAN_ACKY_FACE = 202 * 8
_JAN_ACKY_TILES = 16
#: `jangame.Table.BOT_ID_BASE`. A seat with no human needs a NON-ZERO id here
#: or the member dialog draws no row for it; the game's own notice mints its
#: from the in-game table id, which this process cannot see, so this one is
#: derived from the lobby id instead. Both are obviously synthetic and neither
#: can collide with a real PolID.
_JAN_BOT_ID_BASE = 0x0B07000000000000
#: The icon sheets this server can back with art (the board's `faces` folder,
#: baked from the user's own client). An index whose sheet is NOT on the
#: client's drive makes its loader return -1 and clear the descriptor -- a
#: blank seat, which is worse than the default portrait -- so an index we
#: cannot back with art is served as 0 instead.
_JAN_FACE_DIR = os.path.join(HERE, "boardart", "faces")
_JAN_FACE_SHEETS = None


def _jan_face_ok(idx):
    """`idx` if its sheet is one we ship, else 0."""
    global _JAN_FACE_SHEETS
    idx = int(idx or 0)
    if not idx:
        return 0
    if _JAN_FACE_SHEETS is None:
        try:
            _JAN_FACE_SHEETS = {
                int(n[3:6]) for n in os.listdir(_JAN_FACE_DIR)
                if n.startswith("hnf") and n.endswith(".png") and n[3:6].isdigit()}
        except OSError:
            _JAN_FACE_SHEETS = set()
    return idx if (idx >> 3) in _JAN_FACE_SHEETS else 0


#: member -> True once their build was resolved from an address that claimed
#: the title, so the game band can answer for a proxied connection.
_JAN_BUILD_BY_MEMBER = {}

#: First W2U-0003 version (yyyymmdd) that ships the 2004 module to PC.
JAN_PC_2004_FROM = "20261005"


def _jan_peer_is_2004():
    """True when the client on THIS thread runs Janhourou 20040727_2.

    The two builds send byte-identical openers and the 3:0 request carries no
    length, so the lobby cannot tell them apart from its own traffic. The patch
    server can: each title checks its own patch channel before it launches and
    claims its exact build there, which the patch server notes per address
    (`titles.core._client_builds(address)`, the core's `clientbuild:<address>`
    in Valkey; the same hand-over the portal eras use).
    The most recently seen `*/0003` claim for this address decides: any P2U
    (US Viewer) claim is the 2004 build, and a PS2 claim is judged by its
    version.

    The address is the core's per-thread marker (`_peer_build.ip`), set on the
    lobby connection and on the auth session the game band rides.

    POL_JAN_SAVE_2004: `auto` (default), `1` = everybody, `0` = nobody.
    """
    mode = os.environ.get("POL_JAN_SAVE_2004", "auto")
    if mode in ("0", "1"):
        return mode == "1"
    if jansave is None:
        return False
    # WARNING: THE ADDRESS IS NOT ALWAYS THE PLAYER'S. The Jan GAME band rides the
    # auth session, and that one can reach us proxied: the same player whose
    # lobby fetches came from their own address can send every game-band line
    # from 127.0.0.1, an address with no title claim at all. The test then
    # answered "2002" for a 2004 client and every record on that band went out
    # in the wrong shape -- silently, because a wrong layout draws as a wrong
    # screen and never as an error. So the answer is remembered against the
    # MEMBER, who is the same person whatever the transport, and the address
    # is only how it is first learned.
    _member = (_session_get("member_id") if _session_get else None) or 0
    ip = getattr(_peer_build, "ip", None)
    if not ip:
        return bool(_JAN_BUILD_BY_MEMBER.get(_member)) if _member else False
    try:
        entry = (_client_builds(ip) if _client_builds else None) or {}
    except Exception:                                         # noqa: BLE001
        return False
    claims = [(v.get("seen", ""), k, v.get("version", ""))
              for k, v in entry.items() if k.endswith("/0003")]
    if not claims:
        # This address never claimed the title. If we learned the answer for
        # this member on a connection that DID carry one, keep it.
        return bool(_JAN_BUILD_BY_MEMBER.get(_member)) if _member else False
    _seen, key, version = max(claims)
    # A US Viewer (region P2U) can only ever install the 2004 build, and once
    # it has taken an overlay from the P2U-0003 channel it claims THAT
    # version, which the version test alone would read as the 2002 tree.
    # The PC Viewer (W2U) runs JongHoLow as a static recomp, shipped on the
    # W2U-0003 channel: up to 20260930_0 it was the 2002 module, from
    # JAN_PC_2004_FROM on it is the 2004 one.
    if key.startswith("W2U/"):
        got = (version or "")[:8] >= JAN_PC_2004_FROM
    else:
        got = True if key.startswith("P2U/") else jansave.is_2004_version(version)
    if _member:
        _JAN_BUILD_BY_MEMBER[_member] = got
    return got


# WHICH BUILD IS BEING ANSWERED. janhourou turns game records into bytes, and
# the 2004 client reads several of them differently (janmsgs2004). Same
# per-thread test the lobby fetches use.
janhourou.IS_2004 = _jan_peer_is_2004


def _jan_save_for_build(path, data):
    """`U/g/MJSUserData` as THIS client's build reads it.

    Applied last, after `_jan_save_live` has patched the live record in using
    2002 offsets. The 2004 build wants magic 0x02060300 and 1048 bytes; serving
    it the 2002 blob is `JHR-650-13034` (measurements in `jansave.to_2004`).
    The LENGTH moves with it in `resource_length`.
    """
    if jansave is None or path != JAN_USERDATA_PATH or not _jan_peer_is_2004():
        return data
    want = jansave.SIZE + jansave.TRAILER
    try:
        out = jansave.to_2004(data[:want].ljust(want, b"\x00"))
    except ValueError as e:
        log("lobby", f"  3:0 {path!r}: 2004 build, but NOT converted ({e})")
        return data
    log("lobby", f"  3:0 {path!r}: 2004 build (20040727_2) -- {want}B save moved "
                 f"to the {len(out)}B layout, magic {jansave.MAGIC_2004:#010x}")
    return out


def _jan_resource_patch(path, data):
    """The live record patched into the save, then the save reshaped for the
    requester's build. A 2004 client's fresh blob arrives at the 2004 length
    already (the core sized it from `resource_length`), so the converter is
    handed the 2002 shape it expects and grows it back."""
    if (path == JAN_USERDATA_PATH and jansave is not None
            and _jan_peer_is_2004()):
        want = jansave.SIZE + jansave.TRAILER
        return _jan_save_for_build(
            path, _jan_save_live(path, bytes(data[:want]).ljust(want, b"\x00")))
    return _jan_save_live(path, data)


def _jan_lobby_blob(path, n, req_pt=None):
    """Janhourou's zone / room / table list from LIVE room state, or None.

    None means "not ours" and the caller serves the stored file, which is the
    behaviour every other title keeps.

    WARNING: THE TITLE-ZONE GATE IS WHY THIS IS SAFE ON A SHARED PATH. `b/g/PTL` and
    `b/g/ZL` are one resource per member for BOTH games (`8.b_g_PTL.bin` and
    friends), and the Tetra Master track authors its own. Answering those paths
    unconditionally would replace a live worker's content with Jan's topology.
    """
    mode = os.environ.get("POL_JAN_LOBBY_LIVE", "1").strip().lower()
    if janlobby is None or mode in ("0", "off", "no", ""):
        return None
    if not (path == "b/g/ZL" or path == "b/g/PTL"
            or path == "b/g/MJSTableInfoSub"
            or (path.startswith("b/g/RL") and path[6:].isdigit())):
        return None
    member = _session_get("member_id")
    if mode != "force" and _title_zone(member) != _JAN_CONTENT_ID:
        return None
    live = _live_rooms()
    # LEARN THE ROOM KEY. Any Jan fetch is a chance to bind a key somebody
    # asked with a moment ago to the channel they have since JOINed -- see
    # janlobby's "learned rather than reversed" banner. Costs nothing when
    # there is nothing pending, which is almost always.
    try:
        for _k, _c in janlobby.reconcile_room_keys(live):
            log("lobby", f"  jan room key {_k:#x} LEARNED -> {_c} "
                         f"(now {len(janlobby.learned_rooms())} known)")
    except Exception as exc:
        log("lobby", f"  jan room-key reconcile failed ({exc!r}) -- the room "
                     f"list still falls back to every room's occupants")
    try:
        if path == "b/g/ZL":
            blob = janlobby.zone_list_blob(live)
            if _jan_peer_is_2004():
                # The 2004 build keeps a flags byte at record +0x0C and reads
                # the name from +0x0D (janlobby.zone_list_to_2004).
                blob = janlobby.zone_list_to_2004(blob)
            what = "%d zone(s)" % len(janlobby.topology())
        elif path.startswith("b/g/RL"):
            zone = int(path[6:], 10)
            blob = janlobby.room_list_blob(zone, live)
            if blob is None:
                log("lobby", f"  3:0 {path!r}: zone {zone} is not in this "
                             f"server's topology -- falling back to the stored "
                             f"blob rather than serving an EMPTY room list, "
                             f"which walks the client off its row table")
                return None
            if _jan_peer_is_2004():
                # Its "Players" column is two biased nibble bytes at record
                # +0x8F/+0x90, not the u32 at +0x10 (janlobby.room_list_to_2004).
                blob = janlobby.room_list_to_2004(blob)
            what = "zone %d, %d room(s)" % (zone, len(janlobby.rooms_of(zone)))
        elif path == "b/g/MJSTableInfoSub":
            # The rules AND the member rows -- see janlobby's banner. Which
            # table is in the fetch's subject: measured 2026-09-04, the TOP 16
            # BITS carry the table id (subject 0x0002_003d7c0054ab while the
            # asker sat at table 2, 0x0001_003d7c0054a8 at table 1). The low 48
            # bits are the room-key family we still cannot decode, so the id is
            # validated against our own tables and falls back to whichever
            # table the ASKER is seated at -- serving the wrong table's members
            # would be worse than serving none.
            #
            # THE ROOM (finding 30): those 16 bits are the BASE id every
            # room's PTL carries, so the composite is (asker's registry
            # room, base) -- `janseats.resolve_table`, which falls back to
            # the seat the asker holds and last to room 0.
            _tid = 0
            _subj = janlobby.request_key(req_pt)
            _known = [t for t, _c in janlobby.JAN_TABLES]
            _base = (_subj >> 48) if (_subj >> 48) in _known else 0
            if janseats is not None and janseats.enabled():
                _tid = janseats.resolve_table(member, _base,
                                              room=_jan_member_room_id(member))
            else:
                _tid = _base
            _seats, _params, _voices, _values = None, None, None, None
            _faces = None
            if _tid:
                _params = janlobby.table_params_text_for(_tid)
                if janseats is not None and janseats.enabled():
                    _by = {st: (pid, nm, _m)
                           for _m, st, nm, pid in janseats.seats_at(_tid)}
                    _seats = [(_by[i][0], _by[i][1]) if i in _by else None
                              for i in range(4)]
                    # VoiceType[seat] (+0x218): the VoiceCharaNum each
                    # member sent at reserve time (MjPLAYREQ +0x42).
                    _voices = [janseats.voice_of(_by[i][2]) if i in _by else 0
                               for i in range(4)]
                    # FaceType[seat] (+0x208): the PlayOnline handle-icon
                    # index the member sent at reserve time (MjPLAYREQ +0x40).
                    # The 2004 build loads `hnf<index>>3>.png` cell index&7
                    # from the Viewer's icon folder; 0 draws the default.
                    _faces = [_jan_face_ok(janseats.face_of(_by[i][2]))
                              if i in _by else 0 for i in range(4)]
                # THE BOTS ARE NOT IN THE SEAT STORE, AND THE GAME IS NOT
                # IN THIS PROCESS. The seat store holds real reservations
                # only, so a table played against three bots reported ONE
                # member: the 2004 "Table N members" dialog draws a row per
                # non-zero PolID in this blob, and the in-game plates take
                # their portrait from FaceType beside it. The game manager
                # knows the bots, but it lives in the AUTH session's process
                # and this fetch is served by the lobby's -- `GAMES.by_lobby`
                # is empty here, which is why looking there filled nothing.
                # What IS shared is the seat store, so the test is: the table
                # is in play and this seat holds no human, therefore a bot.
                # Its name and portrait are derived, not looked up, so both
                # processes agree without talking.
                if (janseats is not None and janseats.enabled()
                        and janseats.is_in_play(_tid)):
                    if _seats is None:
                        _seats = [None] * 4
                    if _faces is None:
                        _faces = [0] * 4
                    for _s in range(4):
                        if _seats[_s] is not None:
                            continue
                        _seats[_s] = (_JAN_BOT_ID_BASE | (int(_tid) << 8) | _s,
                                      janhourou.BOT_NAME % _s
                                      if janhourou is not None else "COM %d" % _s)
                        # A bot has no PlayOnline handle and so no icon of its
                        # own. The game gives its computer players one of the
                        # ACKY Gallery portraits (sheets hnf202 + hnf203), the
                        # same pair the watch page draws. Steady per seat, so a
                        # face never changes under the player mid-hand.
                        _faces[_s] = _JAN_ACKY_FACE + ((int(_tid) + _s)
                                                       % _JAN_ACKY_TILES)
                # THE RULES THE MASTER CHOSE (finding 2), or the defaults.
                if janrules is not None:
                    _values = janrules.values_for(_tid)
            if os.environ.get("POL_JAN_FACE", "1") != "1":
                _faces = None
            blob = janlobby.mjs_table_info_blob(values=_values, seats=_seats,
                                                params=_params, voices=_voices,
                                                faces=_faces)
            if _jan_peer_is_2004():
                # 1040 bytes in that build, with the CSV at +0x3CC; an 816-byte
                # reply is what made every table ask for a password
                # (janlobby.table_info_to_2004).
                blob = janlobby.table_info_to_2004(blob)
            what = ("%d rule settings%s, table %s: %s"
                    % (janlobby.MJS_TI_COUNT,
                       " (master's)" if (janrules is not None and _tid
                                         and janrules.has_rules(_tid)) else
                       " (defaults)",
                       (janseats.table_label(_tid) if janseats is not None
                        and _tid else (_tid or "?")),
                       ", ".join("%d=%s" % (i, (v or ("", ""))[1] or "-")
                                 for i, v in enumerate(_seats or []))
                       or "no member rows"))
        else:
            key = janlobby.request_key(req_pt)
            # Remember who asked with this key; the JOIN ~0.6s from now is what
            # names it. Recorded BEFORE the lookup so the very first visit to a
            # room still teaches us, even though it cannot be answered yet.
            try:
                janlobby.note_room_fetch(member, key)
            except Exception:
                pass
            # WHICH ROOM (finding 30), in order: the LEARNED key; else the
            # room the asker is JOINed to (a `<DO>` reload re-fetches from
            # inside the room, key unlearned or not); else, last, the old
            # shared view -- everyone in any Jan room, and ROOM 0's four
            # tables under room 0's sequence. The tables/serial follow the
            # same choice as the members, so a client that was placed wrong
            # here (first-ever visit to a room: the key is unlearned and the
            # fetch precedes the JOIN) reports room 0's serial on its next
            # `<DR>`, which is answered against its REAL room and reloads
            # it -- by then the JOIN has taught us the key.
            room = janlobby.room_for_key(key)
            if room is not None:
                what = "room %s (key %#x)" % (room.channel, key)
            else:
                room = _jan_member_room(member)
                if room is not None:
                    what = ("room %s (key %#x unlearned; the asker is JOINed "
                            "there)" % (room.channel, key))
            if room is not None:
                who = janlobby.occupants(live, room.channel)
            else:
                # THE FETCH PRECEDES THE JOIN -- measured, see janlobby.py. With
                # no room named we serve everyone in any Jan room rather than
                # nobody, and say so, because an empty member list is exactly the
                # symptom this whole change exists to remove.
                who = [o for r in janlobby.all_rooms()
                       for o in janlobby.occupants(live, r.channel)]
                what = ("room UNKNOWN (key %#x names none of ours, asker in "
                        "no Jan channel) -- serving every Jan room's occupants "
                        "and room 0's tables" % key)
            me = _jan_self_occupant()
            if me is not None and not any(o.member_id == me.member_id
                                          for o in who):
                # The requester is entering this room and is not in its channel
                # yet, so they are absent from the registry at exactly the moment
                # they are about to walk in. Their own row is the one row the
                # member screen must never be missing.
                who.append(me)
            # THE ROW'S LEVEL AND PROFILE ID: "Lv%2d" from
            # janstats -- the same level every other screen shows -- and +0x18
            # = the Jan Content ID that View Profile sends as `<PG>`.
            for o in who:
                _jan_decorate_occupant(o)
            # THE SERIAL IS THE DELTA SEQUENCE, NOT A CONSTANT. The client
            # reports this number back in `<DR>` every ~2 s and we answer
            # relative to it, so a blob stamped with anything else desyncs it
            # permanently: stamp low and it is owed deltas it already holds,
            # stamp high and `deltas_after` can only ever tell it to reload.
            # Stamping the CURRENT sequence makes a fetch leave the client
            # CURRENT by construction, so its next poll is the silent one --
            # which is what stops this channel becoming the re-fetch storm it
            # became on the Tetra Master side.
            # THE SERIAL IS THE DELTA SEQUENCE, and the rule lives in
            # `janlobby.blob_serial()` so a test can reach it -- it has a trap
            # in it that is invisible from here. See its docstring.
            blob = janlobby.ptl_blob(who, live, serial=janlobby.blob_serial(room),
                                     room=room)
            what += ", %d member(s): %s" % (
                len(who), ", ".join(o.name or "?" for o in who) or "none")
    except Exception as exc:
        log("lobby", f"  3:0 {path!r}: live build failed ({exc!r}) -- serving "
                     f"the stored blob instead")
        return None
    log("lobby", f"  3:0 {path!r}: LIVE from the room registry -- {what}")
    return blob[:n].ljust(n, bytes(1))


def _jan_self_occupant():
    """The requesting member as a lobby-list row, or None."""
    if janlobby is None:
        return None
    member = _session_get("member_id")
    if not member:
        return None
    return _jan_decorate_occupant(
        janlobby.Occupant(_member_display_name(member), int(member)))


def _jan_decorate_occupant(o):
    """Fill a PTL row's level (janstats) and profile id (the Jan Content ID)."""
    if not o or not getattr(o, "member_id", 0):
        return o
    try:
        if janstats is not None:
            o.level = janstats.level(o.member_id)
        if accounts is not None:
            cid = accounts.content_id_int(_member_content_id(o.member_id,
                                                             _JAN_CONTENT_ID))
            o.profile_id = int(cid or 0) & 0xFFFFFFFF
    except Exception as exc:
        log("lobby", f"  jan row for member {o.member_id}: {exc!r} -- level/"
                     f"profile id left at defaults")
    return o


def _jan_member_room(member):
    """The `janlobby.Room` `member` is JOINed to, or None.

    THE ROOM-RESOLUTION RULE (finding 30): the client holds exactly one room
    channel (`sqMgCpEnterRoom` -> DAT_003f1884; roommain.c:146-176 waits on
    "IRC Room Part Complite" before another can be picked), so the one
    `#MJS0R0zi` the registry has them in IS the room of every table request
    -- MjPLAYREQ/CANCEL/GAMESTART/TBLCONFALL on the game band, the
    `<DR>` poll and the `b/g/MJSTableInfoSub` fetch here. For `b/g/PTL`
    it is the SECOND choice after the learned key, because that fetch
    precedes the JOIN by ~0.6 s (see `_jan_lobby_blob`).
    """
    if janlobby is None or not member:
        return None
    try:
        return janlobby.member_room(_live_rooms(), member)
    except Exception:
        return None


def _jan_member_room_id(member):
    r = _jan_member_room(member)
    return r.id if r is not None else 0


def _jan_save_live(path, data):
    """`U/g/MJSUserData` with the fetching member's LIVE record patched in.

    Same family as `_ptl_with_live_roster`, and for the same reason: the STORED
    file stays the base -- it holds the identity u64s and the two name strings,
    which are not ours to reset -- and the numbers we actually track are overlaid
    on the way out.

    What it writes (all measured -- `jansave.FIELDS`): the four money lines
    and three title counters of the record screen, level +0x238 / rank +0x23C
    of the handle panel, the 39 per-yaku counters, and -- from the seat store
    -- the reservation bytes +0x3C8..+0x3CA plus the REJOIN gate +0x3CB, set
    while the member's table has a game in play (finding 29: the menu shows
    "Rejoin" instead of "Cancel Reserve"). `games_played` alone has no home in
    the save and is reported skipped.
    """
    if jansave is None or janstats is None or path != JAN_USERDATA_PATH:
        return data
    if os.environ.get("POL_JAN_SAVE_LIVE", "1") != "1":
        return data
    member = _session_get("member_id")
    if not member:
        # No session, no member, and NOT a failure -- the same distinction
        # `_ptl_with_live_roster` had to learn to make.
        return data
    try:
        rec = janstats.load(member)
        vals = janstats.derive(rec)
        out, applied, skipped = jansave.apply_live(data, vals)
        # THE RESERVATION GOES IN THE SAVE. The client recomputes its master
        # flag from these bytes on every fetch and from nowhere else, so a save
        # that does not carry the seat is a save that makes everybody master.
        # See `jansave.apply_seat`.
        if (janseats is not None and janseats.enabled()
                and os.environ.get("POL_JAN_SAVE_SEAT", "1") == "1"):
            _tid = janseats.table_of(member)
            if _tid:
                _at = {m: (st, nm) for m, st, nm, _p in janseats.seats_at(_tid)}
                _mine = _at.get(member)
                _master = janseats.master_seat_of(_tid)
                if _mine is not None and _master is not None:
                    _playing = bool(janseats.is_in_play(_tid))
                    # THE SAVE CARRIES THE WIRE ID (finding 30): +0x18 is
                    # `_DAT_00446510`, which the menu compares against the
                    # PTL row's +0x00 -- the 16-bit base, never the composite.
                    out = jansave.apply_seat(out, seat=_mine[0],
                                             master_seat=_master,
                                             table_id=janseats.split_table_id(_tid)[1],
                                             rejoin=_playing)
                    log("lobby", f"  {path!r}: member {member} sits at "
                                 f"{janseats.table_label(_tid)} seat {_mine[0]}, "
                                 f"master seat {_master} -> "
                                 f"{'MASTER' if _mine[0] == _master else 'guest'}"
                                 f"{', game IN PLAY -> Rejoin' if _playing else ''}")
        # WHERE "Back to Room" GOES. The 2004 build validates the destination
        # against the lists before it dials: the zone byte at +0x3C5 against
        # `b/g/ZL` +0x3C and the room u64 at +0x010 against `b/g/RL%03d` +0x00
        # (jansave.apply_return_room). Served zero, both lookups miss and the
        # menu item can only answer JHR-0-13172, "the destination zone was not
        # found". The 2002 build has no such branch, which is why these two
        # fields were never load-bearing before.
        if os.environ.get("POL_JAN_SAVE_ROOM", "1") == "1":
            _room = _jan_member_room(member)
            _zid = _rid = None
            if _room is not None:
                _zid, _rid, _why = _room.zone, _room.id, _room.name
            elif janseats is not None and janseats.enabled():
                # The channel registry only knows a member who is JOINed right
                # now, and the save is fetched at the title screen where they
                # are not. The seat store still holds the composite table id,
                # whose room half IS the `b/g/RL%03d` id (janlobby.Room.id =
                # zone * 100 + index), so the zone is its hundreds digit.
                _t = janseats.table_of(member)
                _r = janseats.room_of_table(_t) if _t else 0
                if _r:
                    _zid, _rid, _why = _r // 100, _r, "from the seat store"
            if _rid:
                out = jansave.apply_return_room(out, zone_id=_zid, room_id=_rid)
                log("lobby", f"  {path!r}: Back to Room -> zone {_zid}, "
                             f"room {_rid} ({_why})")
            else:
                log("lobby", f"  {path!r}: member {member} is in no room -- "
                             f"Back to Room left unset")
        if applied:
            log("lobby", f"  {path!r}: member {member}'s live record patched in -- "
                         + ", ".join("%s %d->%d" % (k, o, nw)
                                     for k, (o, nw) in sorted(applied.items())))
        elif rec.get("games_played"):
            log("lobby", f"  {path!r}: member {member} has "
                         f"{rec['games_played']} game(s) on record and the "
                         f"stored save already carries them")
        return out
    except Exception as exc:
        log("lobby", f"  {path!r}: live record patch failed ({exc!r}) -- "
                     f"serving the stored blob unchanged")
        return data


# ---------------------------------------------------------------------------
# the member profile (content id 3): the Viewer's profile and the <PG> popup
# ---------------------------------------------------------------------------
_JAN_MONEY, _JAN_GAMES, _JAN_YAKUMAN, _JAN_RANK, _JAN_LEVEL = 11, 17, 25, 26, 32
#: The five title COUNTS ("...の数"), in `janstats.SHOGO_KEYS` order. The order
#: is not assumed from the layout: each label names the title its counter
#: belongs to (万雀王 Mahjong King, 百獣王 King of Beasts, 箱大将 Bust General,
#: 浮将軍 Winnings General, 爆敗王 = the LAST-PLACE king, i.e. Wild Tile King,
#: whose threshold `5 - last % 5` is counted off last places).
_JAN_TITLES = (27, 28, 29, 30, 31)

#: WARNING: **JANHOUROU HAS TWO RANK LADDERS WITH DIFFERENT STRIDES, and passing an
#: index from one to the other renames every rank above the first tier.**
#:
#:   * THE GAME's (`janstats.RANK_*`, read out of `JanHouRou.pex`
#:     `ReturnRoom__00364c60`): 19 tiers x **5** variants, 95 names, index
#:     `tier*5 + variant`. Variant 4 is やる気無い ("lazy").
#:   * THE VIEWER's (`prof_003.pfb`'s own enum table): 19 tiers x **7**, 133
#:     values 0..132, four named variants then three 予備 ("spare") slots SE
#:     never filled -- so the Viewer has no name for the game's variant 4, and
#:     it lands on that tier's first spare.
#:
#: Both were measured, on their own file. Converting is `divmod` by the game's
#: stride and multiplying by the Viewer's; the spare is the honest result for a
#: variant the Viewer was never given a word for.
_JAN_RANK_VIEWER_VARIANTS, _JAN_RANK_VIEWER_MAX = 7, 132


def _jan_viewer_rank(idx):
    """A `janstats.rank_index_of` value in the VIEWER's rank enum. See above."""
    import janstats
    tier, variant = divmod(max(0, int(idx)), janstats.RANK_VARIANTS)
    return min(_JAN_RANK_VIEWER_MAX,
               tier * _JAN_RANK_VIEWER_VARIANTS + variant)


def profile_fields(cid, member_id):
    """`{schema slot: value}` for the Janhourou content profile: level, rank,
    games, money, yakuman and the five title counts, only for a member who
    has played. See the core's _content_game_fields for the slot map.
    """
    out = {}
    if member_id is None:
        return out
    try:
        # KEY: THE WHOLE OF JAN'S PROFILE IS ALREADY COMPUTED, once, in
        # `janstats` -- that module exists so a level, a rank or a title
        # cannot disagree with itself across the save, the results screen,
        # the rankings and the `<PO>` popup. The Viewer's profile is one
        # more screen onto the same record, so it reads the same functions;
        # deriving anything a second time here is exactly the drift
        # janstats was written to end.
        #
        # WARNING: Level and Rank are janstats POLICY (marked PARTIAL: there): SE's
        # server computed them and the client defines neither. These are the
        # same placeholders every other jan screen already shows, not a new
        # invention, and one `overrides` entry moves all of them together.
        #
        # A member with no games and no overrides gets NOTHING, not zeros:
        # "has never played" is what an unset field already says.
        import janstats
        rec = janstats.load(member_id)
        if int(rec.get("games_played", 0)) or rec.get("overrides"):
            d = janstats.derive(rec)
            out[_JAN_LEVEL] = int(d["level"])
            out[_JAN_RANK] = _jan_viewer_rank(d["rank"])
            out[_JAN_GAMES] = int(d["games_played"])
            out[_JAN_MONEY] = max(0, int(d["money"]))
            out[_JAN_YAKUMAN] = int(rec.get("yakuman") or 0)
            titles = rec.get("titles") or {}
            for key, slot in zip(janstats.SHOGO_KEYS, _JAN_TITLES):
                out[slot] = int(titles.get(key) or 0)
    except Exception as exc:
        log("lobby", f"content profile: Janhourou fields for {cid} failed "
                     f"({exc!r}) -- leaving them unset")
    return out


def polpro_profile(cid, name, member_id):
    """The `<PO>` fields for a `<PG>` on the MJS tag: (fields, name, member)."""
    fields = {}
    # JANHOUROU'S 71 SLOTS (finding 27), measured off showprof.c and
    # lb__002f90e0 on 2026-09-04: v2 name, v10 money, v12-v15 the
    # floats x1e6 (avg place, top rate, Jan Rating, Title score),
    # v17 total games, v19-v23 the gamble counters, v25 yakuman, v26
    # rank, v27-v31 the five title counts, v32 level, v41+ the show
    # flags the client saved with `<GR>`. ONE producer --
    # janstats.profile_fields -- shared with the rank list.
    _jm, _jname = _jan_member_for_cid(cid)
    if _jm is None:
        _jm = member_id
    flags, extra = _jan_show_flags(cid)
    if _jm:
        jf = janstats.profile_fields(
            _jm, name=(janstats.load(_jm).get("name") or name or _jname),
            content_id=cid, show_flags=flags)
        jf.update(extra)
        fields.update(jf)
        name = fields.get(2) or name
        member_id = _jm
    return fields, name, member_id


# ---------------------------------------------------------------------------
# glue that used to be inline in the core, now the title's own
# ---------------------------------------------------------------------------
def resource_length(path):
    """The declared 03:00 payload length for a Janhourou lobby list, or None.

    The parlour's room list and the room's table list are declared at the
    bytes that carry content, not at the reader's buffer: a console over a
    1280-MTU link drops the tail of a reply the size of the buffer and parks
    its reader (measured twice on 2026-08-17). The room
    list follows the rooms this server authors in that parlour
    (`janlobby.RL_HDR` + rooms x `janlobby.RL_REC`, the 876 the live service
    ran with for four rooms); the table list is `janlobby.ptl_content_length`
    (`0x5850 + tables x 104`), each +4 for the trailer. The core asks the
    requester's own title first (`titles.resource_length(path, zone=...)`),
    so a Tetra Master player's lengths are that title's.
    """
    if path == "b/g/MJSTableInfoSub" and _jan_peer_is_2004():
        # THE 2004 BUILD ASKS FOR 1040, NOT 816 (JanHouRou.pex 20040727_2:
        # `0x299bf0(..., "b/g/MJSTableInfoSub", 1040)`).
        n = janlobby.MJS_TI_TOTAL_2004 + 4
        log("lobby", f"  3:0 {path!r}: 2004 build -- serving {n} "
                     f"({janlobby.MJS_TI_TOTAL_2004} + 4 trailer), not "
                     f"{FETCH_PATHLEN.get(path)}")
        return n
    if path == JAN_USERDATA_PATH and _jan_peer_is_2004():
        # THE 2004 BUILD ASKS FOR 1048, NOT 976 (JanHouRou.pex 20040727_2,
        # 0x0029c39c `addiu a2,zero,1048`). +4 is the opcode's trailer.
        n = jansave.SIZE_2004 + 4
        log("lobby", f"  3:0 {path!r}: 2004 build -- serving {n} "
                     f"({jansave.SIZE_2004} + 4 trailer), not "
                     f"{FETCH_PATHLEN.get(path)}")
        return n
    if os.environ.get("POL_JAN_LOBBY_LIVE", "1").strip().lower() in ("0", "off", "no", ""):
        return None
    if path == "b/g/PTL":
        return janlobby.ptl_content_length() + 4
    if path.startswith("b/g/RL") and path[6:].isdigit():
        try:
            rooms = janlobby.rooms_of(int(path[6:]))
        except Exception:
            rooms = None
        if not rooms:
            return None
        return janlobby.RL_HDR + len(rooms) * janlobby.RL_REC + 4
    return None


def _session_quit(member_id, sid):
    """A JAN SEAT DIES WITH ITS GAME-BAND SESSION (finding 24). Only that
    session: a member holds several connections (GM chat QUITs too), and
    `_JAN_GAME_PEER` pins the one the game talks on. Releasing promotes the
    next guest if the leaver was master; the notice is queued for the
    survivors' next line."""
    try:
        _qp = _JAN_GAME_PEER.get(member_id) if member_id else None
        if (janseats.enabled() and _qp and len(_qp) >= 4 and _qp[3] == sid):
            _qt = janseats.release_member(member_id, why="QUIT")
            if _qt:
                log("authserv", f"  jan: member {member_id} QUIT its game-band "
                                f"session -- seat at table {_qt} released")
                janhourou.notify_master_changes(peer="quit")
    except Exception as _e:
        log("authserv", f"  jan: QUIT seat release raised ({_e!r})")


def _idle_pushes(member_id, peers):
    """Records queued for a seat that is WAITING, for the core's idle tick.

    WARNING: JANHOUROU NEEDS A REAL PUSH, AND THIS IS WHY. Records for the
    seat that is not talking wait in `Table.outbox` and were drained only
    when that member SPOKE -- on the game band (`handle_line`) or on their
    `<DR>` poll. Both of those are replies, and an in-game client that is
    waiting for us SENDS NOTHING: seat 0 discards, seat 1's private MjTSUMO
    goes to its outbox, and seat 1 -- correctly -- has nothing to say, so
    nothing moves. `<DR>` does not help either: that poll is gated on
    LobbyState == 15 and stops the moment the player enters the table.

    PINNED to this connection (the session id, not the nick: a player holds
    several connections and they share a nick), and CAPPED: a batch the
    client cannot apply in one go advances it not at all; the remainder
    stays queued for the next tick, seconds away. The core frames each body
    as `G<tag>G<record>` to the peer named here and sends it down this
    connection. Quiet connections are asked too (`idle_push_when_quiet`):
    see `_jan_idle_due` for why silence is not a dead socket here.
    """
    if os.environ.get("POL_JAN_IDLE_PUSH", "1") != "1" or not member_id:
        return []
    got = _JAN_GAME_PEER.get(member_id)
    if not got or len(got) < 4 or got[3] != _session_sid():
        return []
    limit = int(os.environ.get("POL_JAN_PUSH_CHUNK", "2") or 0) or None
    # The records are shaped for the build they go to, which the per-thread
    # marker says; borrow the member's last game-band address if it is unset.
    _had = hasattr(_peer_build, "ip")
    _was = getattr(_peer_build, "ip", None)
    if not _was:
        _peer_build.ip = _JAN_PEER_IP.get(member_id)
    try:
        recs = janhourou.take_pending(member_id, peer="dr-band", limit=limit)
    except Exception as exc:
        log("authserv", f"  jan idle push drain raised ({exc!r}) -- the queue "
                        f"is untouched, so the next tick retries")
        return []
    finally:
        if _had:
            _peer_build.ip = _was
        elif hasattr(_peer_build, "ip"):
            del _peer_build.ip
    return [(got[0], r) for r in recs]


def _live_games():
    g = getattr(janhourou, "GAMES", None)
    if g is None:
        return 0
    try:
        return sum(1 for t in list(g.tables.values())
                   if getattr(t, "state", None) == "playing")
    except Exception:
        return 0


#: The service name Janhourou's own live-game count is published under
#: (live_sessions.py: the key `live:authsess-jan`). The Jan board and the
#: admin panel's Overview read it.
LIVE_SERVICE = "authsess-jan"


def _publish_live(n):
    """Publish Janhourou's count of hanchan in progress as its own marker.

    Called from `live_games`, which the core asks from its `authsess-titles`
    heartbeat. That heartbeat runs every 10 s and only in the authsess
    process, the one that holds the games, so the marker is written by the
    right process and at the right pace without a thread of our own. A
    login process never asks, so it never overwrites the count with a 0.
    """
    try:
        import live_sessions
        live_sessions.write_marker(LIVE_SERVICE, n)
    except Exception:                                         # noqa: BLE001
        pass


def _begin_shutdown():
    """A container stop used to SIGKILL the process, and a game in progress
    froze the console on a socket that vanished under it. The core holds the
    process a few seconds after SIGTERM while `live_games` is non-zero; the
    in-game handler answers each live table's next line (the client re-acks
    about every 2 s) with MjGAMEEND, so the player drops cleanly to the menu."""
    try:
        import jangame
        jangame.request_shutdown()
    except Exception:
        pass
    try:
        janhourou.broadcast_server_quit()     # MjNOTICESERVERQUIT, queued
    except Exception:
        pass


def _install_sweeper():
    """The Manager's sweeper thread starts lazily on the first in-game line
    (`jangame.Manager.ensure_sweeper`); this is how its records leave."""
    games = getattr(janhourou, "GAMES", None)
    if games is None:
        return
    try:
        games.on_swept = _jan_sweeper_push
    except Exception as _e:                                   # pragma: no cover
        log("authserv", f"jan sweeper push hook not installed: {_e!r}")


class Janhourou(titles.Title):
    tag = b"MJS"
    content_code = _JAN_CONTENT_ID
    fetch_pathlen = FETCH_PATHLEN
    resource_init = RESOURCE_INIT
    #: The 2004 build keeps its profile and option settings in
    #: `U/g/MJSOptionData`: it reads 144 bytes (`0x299bf0(..., 144)`) and
    #: writes the same 144 back (`0x299ef0(..., 144, 0)`). The 2002 build has
    #: no such file. An object of any other length is not this file.
    resource_write_len = {"U/g/MJSOptionData": 144}
    polpro_spec_files = tuple(p for p in POLPRO_SPEC_CANDIDATES if os.path.isfile(p))

    def core_bound(self):
        _rebind()
        # JANHOUROU'S ROOM (finding 30). Every table id on the Jan game band
        # is a 16-bit row id that exists in EVERY room; the room is the one
        # Jan channel the asker is JOINed to, and the core's room registry is
        # where that is known. `janhourou.member_room` reads it through this.
        janhourou.LIVE_ROOMS = _live_rooms
        janhourou.IS_2004 = _jan_peer_is_2004
        _install_sweeper()

    def describe(self):
        return ("MJS binary records: parlours, rooms, tables, seats, a hanchan "
                "against players or the computer, rankings, the profile.")

    # --- the auth band ---
    def notice(self, cls, payload, text, target, nick, srv, sess):
        return notice(cls, payload, text, target, nick, srv, sess)

    def polpro_reply(self, cls, payload):
        # THE DELTA STREAM ANSWERS `<DR>` BEFORE THE TEMPLATE DOES (class L);
        # the RANKING LISTS (R) and the CHARACTER NAME (P, `<CR>`) answer
        # themselves for the same reason -- which list, and whether a name is
        # free, is a VALUE the static template cannot express. Declining
        # falls through to the template.
        if cls == b"L":
            return _jan_roster_delta_reply(payload)
        if cls == b"R":
            return _jan_rank_reply(payload)
        if cls == b"P":
            return _jan_pool_reply(payload)
        return None, False

    def polpro_noted(self, cls, payload, target):
        # Any lobby-band line from the member renews their seat lease
        # (finding 24); `<DR>` does it in the delta reply, the rest here.
        if janseats.enabled():
            try:
                janseats.touch(_session_get("member_id"))
            except Exception:
                pass

    def polpro_pushes(self, cls, payload):
        return _jan_pending_lines(_session_get("member_id"), on_sid=_session_sid())

    def polpro_profile(self, cid, name, member_id):
        return polpro_profile(cid, name, member_id)

    def session_quit(self, member_id, sid):
        _session_quit(member_id, sid)

    def idle_pushes(self, member_id, peers):
        return _idle_pushes(member_id, peers)

    @property
    def idle_push_when_quiet(self):
        # A core that gates its idle push on the connection having spoken
        # recently asks this title anyway: a Jan client waiting for its turn
        # record is silent (see `_jan_idle_due`). POL_JAN_PUSH_FRESH=1 puts
        # Janhourou back behind the gate.
        return os.environ.get("POL_JAN_PUSH_FRESH") != "1"

    def requeue_pushes(self, member_id, items, why=""):
        # A record the socket refused is lost, as it always was: the table
        # re-sends its state on the seat's next line, and a seat whose
        # connection is gone is released by the sweeper.
        if items:
            log("authserv", f"  jan: {len(items)} pushed record(s) undelivered "
                            f"to member {member_id} ({why})")

    def live_games(self):
        n = _live_games()
        _publish_live(n)
        return n

    def begin_shutdown(self):
        _begin_shutdown()

    # --- the lobby band ---
    def resource_length(self, path):
        return resource_length(path)

    def resource_live(self, path, n, req_pt):
        return _jan_lobby_blob(path, n, req_pt)

    def resource_template(self, path):
        if janstats.is_rank_list_path(path):
            return _jan_rank_blob(path)      # built live from the stat files
        return None

    def resource_patch(self, path, data, subject):
        return _jan_resource_patch(path, data)

    # --- the member profile ---
    def profile_fields(self, cid, member_id):
        return profile_fields(cid, member_id)


def register():
    # Janhourou's own tables (janstore, services/jan_migrations/), applied
    # when the core loads the title, before the first game line needs them
    if os.environ.get("POL_DATABASE_URL", "").strip():
        import janstore
        janstore.migrate_at_start("jantitle")
    return titles.register(Janhourou())
