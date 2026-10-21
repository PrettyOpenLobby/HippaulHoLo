"""janadmin.py -- JongHoLow (Janhourou) in an OpenLobby admin panel (the panel's admin protocol,
olport.py).

Runs inside the JongHoLow board's process (boardjan.start), which has this title's code and the
database but holds no player's connection: restarting it disconnects nobody. Off unless
POL_JAN_ADMIN_PORT is set.

  POL_JAN_ADMIN_PORT    the HTTP port, e.g. 8822
  POL_JAN_ADMIN_BIND    default 127.0.0.1; the panel's container reaches this host-networked
                        process through the Docker gateway (for example 172.17.0.1, Docker's default bridge, with a token)
  POL_JAN_ADMIN_TOKEN   the token the panel sends (POL_ADMIN_GAME_JAN_TOKEN on its side)

The panel gets three pages, each its own moderator permission:

  Events       the event record (janevent: the jan_event table's 'current' row), whether it is
               running, the calendar's next windows (eventcal's "jan" list) and the standings of
               the running event (janstats' Event ranking). The three operations janevent.py's
               command line has: open one by hand (--open, --days), close the running one
               (--close), and run the schedule now (--rotate, what pol-janevent runs hourly).
               Results of an event that has ended are not kept anywhere: a new event id starts
               every member's event winnings at zero, so there is nothing to show for one.
  Tables       every table someone is seated at or that is being played: who sits where, the
               master, the round, the scores, the watchers (janseats' published seats and
               jangame's live tables document, the one boardjan's watch view reads)
  GM notice    the in-room notice (open and close) and a kick by handle, through
               janworld.gmnotice exactly as tools/jan_gmnotice.py posts them, and the posts
               now in the jan:gm-notice document

and three actions with the ids, kinds and fields the panel's own module had (adminmodules/jan.py):
notice (announce, text), close, and kick (kick, handles; an account's Kick sends polid too).

Every change is logged here and, through the panel, in the panel's activity log with the
moderator's name.
"""
import os
import sys
import time

import olpage
import olport

PORT = int(os.environ.get("POL_JAN_ADMIN_PORT", "0") or 0)
BIND = os.environ.get("POL_JAN_ADMIN_BIND", "127.0.0.1")
TOKEN = os.environ.get("POL_JAN_ADMIN_TOKEN", "")
#: authsess's Jan marker (boardjan.LIVE_MARKER), and how fresh it must be to count, as the
#: panel's own status reader takes it (adminmodules.sources.MARKER_FRESH)
MARKER = "authsess-jan"
MARKER_FRESH = 60.0
#: the panel's own limit on a notice line (adminmodules/jan.py)
NOTICE_MAX = 200
#: janevent's settings that win over the stored record where they are set
EVENT_ENV = ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
             "POL_JAN_EVENT_OPEN", "POL_JAN_EVENT_CLOSE", "POL_JAN_EVENT_AUTO")


def _say(msg):
    print("[janadmin] " + msg, file=sys.stderr, flush=True)


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "-"


def _left(sec):
    if sec <= 0:
        return "ended"
    d, rest = divmod(int(sec), 86400)
    h, m = divmod(rest // 60, 60)
    if d:
        return "%dd %dh" % (d, h)
    return "%dh %02dm" % (h, m) if h else "%dm" % m


# --------------------------------------------------------------------------- #
# members
# --------------------------------------------------------------------------- #
def members_for(polid="", handles=""):
    """Member ids for a POL ID and/or handles (comma separated), as the panel's module found
    them: a POL ID through member.polid, a handle through the handle table (any case)."""
    import accounts
    names = [h.strip() for h in str(handles or "").split(",") if h.strip()]
    db = accounts.connect()
    try:
        ids = set()
        if polid and str(polid).strip():
            ids |= {r["id"] for r in db.execute(
                "SELECT id FROM member WHERE polid = %s", (str(polid).strip().upper(),)).fetchall()}
        for n in names:
            ids |= {r["member_id"] for r in db.execute(
                "SELECT member_id FROM handle WHERE lower(handle_name) = lower(%s)", (n,)).fetchall()}
        return sorted(ids)
    finally:
        db.close()


def names_of(members):
    """{member: primary handle} (boardjan's read-only lookup), {} when it cannot be read."""
    import boardjan
    return boardjan.member_names([int(m) for m in members if str(m).isdigit()]) or {}


# --------------------------------------------------------------------------- #
# GM notice (janworld.gmnotice)
# --------------------------------------------------------------------------- #
def _gm():
    from janworld import gmnotice
    return gmnotice


def post_notice(text, actor):
    text = " ".join(str(text or "").split())
    if not text:
        return False, "A notice needs text."
    p = _gm().post("open", text[:NOTICE_MAX])
    _say("%s posted notice #%d: %r" % (actor, p["seq"], p["text"]))
    return True, "Notice #%d posted to every JongHoLow player." % p["seq"]


def close_notice(actor):
    p = _gm().post("close")
    _say("%s closed the notice window (#%d)" % (actor, p["seq"]))
    return True, "The notice window closes for every JongHoLow player."


def kick(actor, polid="", handles="", member=None):
    """Post a kick for each member a POL ID or handles name (or one member id)."""
    if member is not None:
        try:
            ids = [int(member)] if int(member) > 0 else []
        except (TypeError, ValueError):
            ids = []
    else:
        ids = members_for(polid, handles)
    if not ids:
        return False, "No JongHoLow player by that name."
    gm = _gm()
    for mid in ids:
        p = gm.post("kick", member=mid)
        _say("%s kicked member %s (#%d)" % (actor, mid, p["seq"]))
    return True, "Kick posted for %d member%s; it lands on their next move." % (
        len(ids), "" if len(ids) == 1 else "s")


def posts():
    """The posts now in the document, newest first."""
    doc = _gm()._read() or {}
    out = [p for p in (doc.get("posts") or []) if isinstance(p, dict)]
    out.sort(key=lambda p: -int(p.get("seq") or 0))
    return out


# --------------------------------------------------------------------------- #
# the event (janevent)
# --------------------------------------------------------------------------- #
def _ev():
    import janevent
    return janevent


def event_open(name, days, actor):
    """janevent.py --open NAME [--days N]: a new id, opening now."""
    ev = _ev()
    name = " ".join(str(name or "").split())
    if not name:
        return False, "An event needs a name."
    closes = None
    days = str(days or "").strip()
    if days:
        try:
            d = float(days)
        except ValueError:
            return False, "Days is a number, or blank for an event closed by hand."
        if not 0 < d <= 366:
            return False, "Give between a part of a day and 366 days, or leave it blank."
    now = int(time.time())
    if days:
        closes = now + int(d * 86400)
    eid = int(ev.record().get("id") or 0) + 1
    rec = ev.store(eid, name, now, closes)
    _say("%s opened event %s (%r) until %s" % (actor, rec["id"], rec["name"],
                                                _when(closes) if closes else "closed by hand"))
    return True, "Event %d, %s, is open %s." % (
        rec["id"], rec["name"], "until %s" % _when(closes) if closes else "until it is closed")


def event_close(actor):
    """janevent.py --close: the stored event ends now."""
    ev = _ev()
    rec = ev.record()
    if not rec.get("id"):
        return False, "No event is stored."
    if not ev._window(rec, time.time()):
        return False, "Event %s is not running; nothing to close." % rec["id"]
    ev.store(rec["id"], rec.get("name", ""), rec.get("opens_at"), time.time())
    _say("%s closed event %s (%s)" % (actor, rec["id"], rec.get("name") or "unnamed"))
    return True, "Event %s (%s) is closed." % (rec["id"], rec.get("name") or "unnamed")


def event_rotate(actor):
    """janevent.py --rotate: what the hourly timer runs."""
    did = _ev().auto_rotate(log=_say)
    _say("%s ran the schedule: %s" % (actor, did))
    return True, "The schedule ran: %s." % did


def calendar(n=6):
    """(upcoming windows, the last one that ended), or (None, None) without eventcal."""
    try:
        import eventcal
    except ImportError:
        return None, None
    try:
        return eventcal.upcoming("jan", n=n), eventcal.last_ended("jan")
    except Exception as e:                              # noqa: BLE001 -- a bad calendar file
        _say("the event calendar could not be read (%s)" % e)
        return None, None


def standings(limit=50):
    """[(name, winnings, event games)] of the running event, best first: janstats' Event
    ranking (category 4), read with remember=False so the client's up/down glyph never moves."""
    import janstats
    if not _ev().event_id():
        return []
    members = janstats.all_members()
    names = names_of(members) or None
    out = []
    for m, f in janstats.rank_list(4, names=names, remember=False):
        rec = janstats.current_windows(janstats.load(m))
        games = int(rec.get("games_event", 0) or 0)
        if not games:
            continue
        out.append((str(f.get(2) or ""), int(f.get(21, 0) or 0), games))
    return out[:limit]


# --------------------------------------------------------------------------- #
# the tables (janseats + jangame's live tables document)
# --------------------------------------------------------------------------- #
WIND = {"E": "East", "S": "South", "W": "West", "N": "North"}


def _label(tid):
    import janseats
    room, base = janseats.split_table_id(int(tid))
    return (("Room %d-%d, " % (room // 100, room % 100)) if room else "") + "Table %d" % base


def tables(now=None):
    """Every table someone is seated at or watching, or that jangame is playing:
    [{id, label, seats: [(seat, member, name)], master, gallery: [names], state,
      watchable, round, scores, wall}], by id."""
    import boardjan
    import janseats
    now = time.time() if now is None else now
    seats_doc = janseats._live_tables() or {}
    live = boardjan.WATCH._read(now)
    out = []
    for tid in sorted(set(seats_doc) | set(live), key=lambda t: int(t) if str(t).isdigit() else 0):
        if not str(tid).isdigit():
            continue
        entry = seats_doc.get(tid) or {}
        sts = janseats._fresh_seats(entry, now)
        gal = janseats._fresh_gallery(entry, now)
        game = live.get(tid) or {}
        if not sts and not gal and not game:
            continue
        seated = sorted(((int(s.get("seat", 0)), int(m), str(s.get("name") or ""))
                         for m, s in sts.items() if str(m).isdigit()))
        mark = float(entry.get("in_play") or 0)
        ttl = janseats._inplay_ttl()
        in_play = bool(mark) and bool(sts) and not (ttl and now - mark > ttl)
        st = game.get("state") if isinstance(game.get("state"), dict) else None
        rnd = (st or {}).get("round") or {}
        out.append({
            "id": int(tid), "label": _label(tid), "seats": seated,
            "master": int(entry.get("master") or 0),
            "gallery": sorted(str(g.get("name") or "") for g in gal.values()),
            "state": ("playing" if game else "starting" if in_play else "waiting"),
            "watchable": bool(game.get("watchable")),
            "players": [(str(s.get("name") or ""), bool(s.get("bot")), s.get("score"))
                        for s in (st or {}).get("seats") or [] if isinstance(s, dict)],
            "round": ("%s %s" % (WIND.get(rnd.get("wind"), rnd.get("wind")), rnd.get("number"))
                      + (", honba %s" % rnd["honba"] if rnd.get("honba") else "")
                      if rnd.get("wind") else ""),
            "wall": (st or {}).get("wall"),
            "ended": bool((st or {}).get("ended")),
        })
    return out


# --------------------------------------------------------------------------- #
# the pages
# --------------------------------------------------------------------------- #
def events_page(note=None):
    ev = _ev()
    now = time.time()
    rec = ev.record()
    cur = ev.current(now)
    win = ev.scheduled_window(now)
    stored = [("Event", "%s (id %s)" % (rec.get("name") or "unnamed", rec["id"]) if rec.get("id") else "none stored"),
              ("Opened", _when(rec.get("opens_at"))),
              ("Closes", _when(rec.get("closes_at")) if rec.get("closes_at") else
               ("when it is closed" if rec.get("id") else "-")),
              ("Opened by", ("the schedule" if rec.get("auto") else "hand") if rec.get("id") else "-"),
              ("Running now", ("yes, %s left" % _left(int(cur["closes_at"] - now))
                               if cur.get("closes_at") else "yes") if cur else "no")]
    sched = [("Window now", "%s, %s to %s" % (win[2] or "unnamed", _when(win[0]), _when(win[1]))
              if win else "none"),
             ("Schedule", "off (POL_JAN_EVENT_AUTO=0 here)"
              if os.environ.get("POL_JAN_EVENT_AUTO", "1") != "1" else "on, hourly (pol-janevent)")]
    env = [(k, os.environ[k]) for k in EVENT_ENV if os.environ.get(k, "") != ""]
    up, last = calendar()
    if up is None:
        cal = olpage.Raw('<p class="muted">The event calendar (eventcal) is not installed here, so '
                         'events are opened and closed by hand.</p>')
    else:
        cal = olpage.table(["Event", "Starts", "Ends", ""],
                           [[o.get("name") or o.get("key") or "-", _when(o["start"]), _when(o["end"]),
                             olpage.Raw('<span class="pill">now</span>')
                             if o["start"] <= now < o["end"] else ""] for o in up],
                           empty="Nothing on the calendar.")
        if last:
            cal = olpage.Raw(cal + olpage.kv([("Last ended", "%s, %s to %s" % (
                last.get("name") or last.get("key"), _when(last["start"]), _when(last["end"])))]))
    rows = [[i + 1, n, "{:,}".format(w), g] for i, (n, w, g) in enumerate(standings())]
    ops = olpage.Raw(
        olpage.form([("open", "Event name", ""), ("days", "Days (blank: until closed)", "")],
                    "Open an event", danger=True,
                    confirm="A new event id: every player's event winnings start at zero")
        + '<p class="muted">Opens now, as <span class="mono">janevent.py --open</span> does. '
          'The schedule leaves an event opened by hand alone.</p>'
        + olpage.form([], "Close the running event", hidden={"close": "1"}, danger=True, inline=True,
                      confirm="Ends it now, as janevent.py --close does")
        + " "
        + olpage.form([], "Run the schedule now", hidden={"rotate": "1"}, inline=True,
                      confirm="What pol-janevent runs every hour (janevent.py --rotate)"))
    parts = [olpage.grid(olpage.panel("The event", olpage.kv(stored)),
                         olpage.panel("The schedule", olpage.kv(sched),
                                      olpage.kv(env) if env else ""))]
    parts.append(olpage.panel("Standings", olpage.table(
        ["#", "Player", "Winnings", "Games"], rows,
        empty=("Nobody has finished a game in this event yet." if cur else "No event is running."))))
    parts.append(olpage.panel("Calendar", cal))
    parts.append(olpage.panel("Operations", ops))
    return olpage.page(
        "Events", *parts, note=note,
        sub="An event is the client's Event Ranking tab: the ranking counts what players win while "
            "the window is open. Players cannot enter one; playing inside the window is taking part. "
            "Results of an event that has ended are not kept. POL_JAN_EVENT_* settings on the jan "
            "service win over the stored record there; this page reads the record.")


def tables_page(note=None):
    rows = []
    for t in tables():
        seats = []
        for seat, mid, name in t["seats"]:
            seats.append(olpage.esc("%d: %s%s" % (seat + 1, name or "member %d" % mid,
                                                   " (master)" if mid == t["master"] else ""))
                         + " " + olpage.form([], "Kick", hidden={"kick": mid}, danger=True,
                                             inline=True, confirm="Sends them out of JongHoLow"))
        play = ", ".join("%s%s%s" % (n or "?", " (COM)" if bot else "",
                                     " %s" % "{:,}".format(sc) if isinstance(sc, int) else "")
                         for n, bot, sc in t["players"])
        state = t["state"] + (", ended" if t["ended"] else "")
        rows.append([t["label"], olpage.Raw("<br>".join(seats) or '<span class="muted">-</span>'),
                     state, t["round"] or "-",
                     play or olpage.Raw('<span class="muted">not watchable</span>'
                                        if t["state"] == "playing" else '<span class="muted">-</span>'),
                     ", ".join(t["gallery"]) or "-"])
    return olpage.page(
        "Tables", olpage.panel("", olpage.table(
            ["Table", "Seats", "State", "Round", "Scores", "Watching"], rows,
            empty="Nobody is at a table.")),
        note=note,
        sub="Live, not delayed. Seats are the reservations the lobby keeps; the round and scores "
            "come from the game, and only for a table whose creator allows watching.")


def notices_page(note=None):
    now = time.time()
    ps = posts()
    names = names_of([p.get("member") for p in ps if int(p.get("member") or 0)])
    rows = [[olpage.Raw('<span class="mono">%d</span>' % int(p.get("seq") or 0)),
             {"open": "Notice", "close": "Close", "kick": "Kick"}.get(p.get("op"), p.get("op")),
             p.get("text") or "-",
             (names.get(int(p["member"])) or "member %s" % p["member"]) if int(p.get("member") or 0)
             else "everyone",
             _when(p.get("at")),
             ("for %s" % _left(int(float(p.get("until") or 0) - now))
              if float(p.get("until") or 0) > now else olpage.Raw('<span class="muted">over</span>'))]
            for p in ps]
    return olpage.page(
        "GM notice",
        olpage.grid(
            olpage.panel("Notice", olpage.form([("text", "Message", "")], "Post the notice"),
                         olpage.Raw('<p class="muted">Opens the GM notice window for two minutes on '
                                    'every 2004 client at a table.</p>'),
                         olpage.form([], "Close the notice", hidden={"close": "1"}, inline=True)),
            olpage.panel("Kick", olpage.form([("handles", "Handle (comma separated)", "")], "Kick",
                                             danger=True, confirm="Sends them out of JongHoLow"),
                         olpage.Raw('<p class="muted">Lands on their next move and frees their seat.'
                                    '</p>'))),
        olpage.panel("Posts", olpage.table(["#", "What", "Text", "For", "Posted", "Delivered"], rows,
                                           empty="Nothing has been posted.")),
        note=note,
        sub="Posts go to the jan:gm-notice document; every Jan client picks up the ones meant for "
            "it on its next line. The 2002 client ignores them.")


def build():
    port = olport.AdminPort(
        "jan", "JongHoLow", platform="PlayOnline: PS2", token=TOKEN, bind=BIND, port=PORT,
        about="Tables run in the jan service; these tools run in the JongHoLow board.",
        content_ids=[3])

    @port.status
    def status():
        online = None
        try:
            import live_sessions
            m = live_sessions.read_marker(MARKER) or {}
            if 0 <= time.time() - float(m.get("stamp") or 0) < MARKER_FRESH:
                online = int(m.get("count") or 0)
        except Exception:                               # noqa: BLE001
            online = None
        ts = tables()
        cur = _ev().current()
        rooms = []
        for t in ts[:50]:
            n = len(t["seats"])
            rooms.append({"name": t["label"],
                          "detail": ", ".join(x for x in ("%d/4 players" % n, t["round"]) if x),
                          "state": t["state"]})
        facts = [{"label": "tables", "value": len(ts)},
                 {"label": "seated", "value": sum(len(t["seats"]) for t in ts)}]
        doc = {"up": True, "rooms_label": "tables", "rooms": rooms, "facts": facts,
               "summary": ("Event: %s%s" % (cur.get("name") or "unnamed",
                                           ", until %s" % _when(cur["closes_at"])
                                           if cur.get("closes_at") else "")) if cur else ""}
        if online is not None:
            doc["online"] = online
        return doc

    @port.tool("events", "Events", "events")
    def events(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            if f.get("close"):
                note = event_close(actor)
            elif f.get("rotate"):
                note = event_rotate(actor)
            elif "open" in f:
                note = event_open(f.get("open"), f.get("days"), actor)
        return 200, olpage.CTYPE, events_page(note).encode()

    @port.tool("tables", "Tables", "tables")
    def tables_tool(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            if f.get("kick"):
                note = kick(actor, member=f["kick"])
        return 200, olpage.CTYPE, tables_page(note).encode()

    @port.tool("notices", "GM notice", "notices")
    def notices_tool(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            if f.get("close"):
                note = close_notice(actor)
            elif "text" in f:
                note = post_notice(f["text"], actor)
            elif "handles" in f:
                note = kick(actor, handles=f["handles"])
        return 200, olpage.CTYPE, notices_page(note).encode()

    @port.action("notice", "GM notice", kind="announce",
                 fields=[{"id": "text", "label": "Message", "type": "text"}],
                 about="Opens the GM notice window for every player at a table (the 2004 client).")
    def notice(params, actor):
        return post_notice(params.get("text"), actor)

    @port.action("close", "Close the notice", about="Closes the GM notice window again.")
    def close(params, actor):
        return close_notice(actor)

    @port.action("kick", "Kick a player", kind="kick", danger=True,
                 fields=[{"id": "handles", "label": "Handle", "type": "text"}],
                 about="Sends the player out of JongHoLow on their next move.")
    def kick_action(params, actor):
        return kick(actor, polid=params.get("polid"), handles=params.get("handles"))

    return port


def start():
    """Called from boardjan.start. Never raises: a bad setting is logged, the board runs on."""
    if not PORT:
        return None
    try:
        port = build()
        host, n = port.start()
    except (OSError, ValueError) as e:
        _say("admin port not started (%s)" % e)
        return None
    _say("OpenLobby admin protocol on %s:%d" % (host, n))
    return port
