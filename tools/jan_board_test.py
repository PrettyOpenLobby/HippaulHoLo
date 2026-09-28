#!/usr/bin/env python3
"""jan_board_test.py -- the Jan rankings board (services/boardjan.py) and the
polboards service it runs in.

    python tools/jan_board_test.py

Pins: the five categories in janstats' own order and values; names read from
the account database READ-ONLY (its rows are unchanged afterwards); the up/down/New
glyph from the previous rank; that the board NEVER rewrites the rank snapshot
the client's glyph compares against; and the page server's routes.

The rank snapshot is a PostgreSQL table (jan_rank_snapshot), and the accounts
live in PostgreSQL too, so this suite runs on a throwaway database (janpg.py):
the runner's, or its own. Without a server
it reports SKIP, or FAIL under POL_TEST_REQUIRE_DB=1.
"""
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
import janpg                                                   # noqa: E402

CHECKS = []


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-70s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""),
          flush=True)
    if not cond:
        raise AssertionError(label)


class FakeNet:
    """Stands in for urllib.request.urlopen (as tools/fe_map_test.py):
    records every request, answers from a script of (status, json)."""

    def __init__(self):
        self.calls = []
        self.script = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.data or b""))
        st, data = self.script.pop(0) if self.script else (200, {})
        raw = json.dumps(data).encode()
        if st >= 400:
            raise urllib.error.HTTPError(req.full_url, st, "x", {}, _Body(raw))
        return _Resp(st, raw)


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self, *a):
        return self.raw

    def close(self):
        pass


class _Resp(_Body):
    def __init__(self, st, raw):
        super().__init__(raw)
        self.status = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def bot_checks(boardjan, polboards):
    print("the JongHoLow Discord bot: one rankings post, a button per list")
    args = polboards.build_parser().parse_args(
        ["--jan-port", "1", "--jan-url", "https://jan.example"])
    boardjan._SNAP.update(t=0.0, snap=None)
    boardjan._VIEW.update(cat=0, page=0, t=0.0)
    snap = boardjan.cached_snapshot(args)
    payload, files = boardjan.discord_bot_message(snap, args)
    rows = payload["components"]
    tabs, second = rows[0]["components"], rows[1]["components"]
    check("the post: the first list's screen by the board's own render.png, no upload",
          payload["embeds"][0]["image"]["url"].startswith("https://jan.example/render.png?cat=0&page=0&v=")
          and not files and payload["embeds"][0]["title"].startswith(snap["categories"][0]["name"]))
    check("...a button per list, the one on show lit",
          len(tabs) == len(snap["categories"]) and tabs[0]["style"] == 1
          and all(t["style"] == 2 for t in tabs[1:]) and tabs[1]["custom_id"] == "jan:view:1:0")
    check("...L1 / R1 for pages (L1 off on page 1) and a link to the board",
          second[0]["disabled"] and second[1]["label"].startswith("R1")
          and second[2]["style"] == 5 and second[2]["url"] == "https://jan.example/#0")
    ids = [c["custom_id"] for r in rows for c in r["components"] if "custom_id" in c]
    check("...every custom_id unique (Discord refuses the post otherwise)", len(ids) == len(set(ids)))
    r = boardjan.discord_interaction({"data": {"custom_id": "jan:view:2:0"}}, args)
    check("a click EDITS the post to that list (UPDATE_MESSAGE)",
          r["type"] == 7 and r["data"]["embeds"][0]["title"].startswith(snap["categories"][2]["name"])
          and r["data"]["components"][0]["components"][2]["style"] == 1)
    check("...and the post stays on it for the next refresh", boardjan.bot_view() == (2, 0))
    bad = boardjan.discord_interaction({"data": {"custom_id": "jan:view:9:0"}}, args)
    check("a click on something not on the board gets a PRIVATE note",
          bad["type"] == 4 and bad["data"]["flags"] == 64)
    boardjan._VIEW.update(t=0.0)
    check("...and the post drifts back to the first list after a while",
          boardjan.bot_view() == (0, 0))
    live = boardjan.discord_live_messages(
        {"tables": [{"id": "5", "room": 101, "table": 2, "names": ["A"], "round": {}}],
         "watch_delay": 30}, args, bot=True)
    lp = live[0]["build"]()[0]
    check("as the bot, a live table's post has a Watch button, not a bare link",
          lp["components"][0]["components"][0]["url"] == "https://jan.example/watch#5"
          and "https://" not in lp["content"] and "30 s behind" in lp["content"])
    cmd = polboards.command_of(boardjan, "jan")
    check("/janboard rankings | live moves a feed to the channel it is typed in",
          cmd["name"] == "janboard"
          and {"live", "rankings"} <= {o["name"] for o in cmd["options"]}
          and polboards.feed_names(boardjan, "jan") == {"rankings": "jan", "live": "jan_live"}, cmd)


def live_feed_checks(tmp, boardjan, polboards):
    print("Discord: a post per live watchable table, gone when the game is")
    args = polboards.build_parser().parse_args(
        ["--jan-port", "1", "--jan-url", "https://jan.example"])
    tb = {"id": "131073", "room": 201, "table": 1, "names": ["Seiryu", "Byak*ko", "COM 4"],
          "scores": [25000] * 3, "round": {"wind": "E", "number": 2}}
    snap = {"tables": [tb], "watch_delay": 30.0}
    slots = boardjan.discord_live_messages(snap, args)
    payload, files = slots[0]["build"]()
    text = payload["content"]
    check("a watchable table gets a post: where, the round, who, and the link to watch it",
          len(slots) == 1 and slots[0]["slot"] == "131073" and "Room 2-1, Table 1" in text
          and "(East 2)" in text and "Seiryu, Byakko and COM 4" in text
          and "https://jan.example/watch#131073" in text and "30 s behind the table" in text, text)
    check("...no link preview card, no mentions, no em dashes",
          payload["flags"] == 4 and payload["allowed_mentions"] == {"parse": []}
          and " - " not in text and not files)
    check("the live feed is registered, and shares the Jan board's channel by default",
          ("live", "jan_live") in polboards.feed_keys("jan")
          and "jan_live" in polboards.FEED_SHARES_MAIN
          and polboards.feed_fn(boardjan, "live", "ended_ttl") == 0)
    net = FakeNet()
    d = polboards.DiscordSet("jan_live", "https://discord.com/api/webhooks/9/SECRET",
                             os.path.join(tmp, "live_state.json"), every=15, ended_ttl=0,
                             opener=net)
    d.swept = True
    net.script = [(200, {"id": "m1"})]
    d.sync(slots, now=1000.0)
    check("the table's post goes up once", [c[0] for c in net.calls] == ["POST"]
          and d.msgs["131073"]["id"] == "m1")
    n0 = len(net.calls)
    d.sync(boardjan.discord_live_messages(snap, args), now=1005.0)
    check("...and not again while the table is live", len(net.calls) == n0)
    check("...across a restart too (the id is kept)",
          "131073" in polboards.DiscordSet("jan_live", "https://discord.com/api/webhooks/9/SECRET",
                                           os.path.join(tmp, "live_state.json"), opener=net).msgs)
    d.sync([], now=1010.0)                          # the game is over
    d.sync([], now=1015.0)
    check("when the game is no longer live, its post is DELETED (next poll)",
          [c[0] for c in net.calls[n0:]] == ["DELETE"] and "131073" not in d.msgs,
          [c[0] for c in net.calls[n0:]])
    n1 = len(net.calls)
    net.script = [(200, {"id": "m2"})]
    d.sync(slots, now=2000.0)
    check("a new game at the same table gets a new post",
          [c[0] for c in net.calls[n1:]] == ["POST"] and d.msgs["131073"]["id"] == "m2")


def watch_checks(tmp, boardjan):
    print("watching -- only allowed tables, only 30 s late")
    import janstore
    key = "jan:test:%s:tables-live" % os.path.basename(tmp)
    clock = {"t": 1000.0}
    w = boardjan.Watch(key=key, delay=30, clock=lambda: clock["t"])

    def write(tables, stamp=None):
        janstore.kv.set_json(key, {"stamp": clock["t"] if stamp is None else stamp,
                                   "tables": tables})

    def step(t):
        clock["t"] = t
        w.sample()

    s1 = {"room": 1, "table": 2, "seats": [{"name": "A", "score": 25000, "hand": ["1m"]}],
          "round": {"wind": "E", "number": 1}, "seq": 1}
    s2 = dict(s1, seq=2, seats=[{"name": "A", "score": 26000, "hand": ["2m"]}])
    write({"257": {"watchable": True, "state": s1}, "258": {"watchable": False}})
    step(1000.0)
    check("nothing is shown before the delay has passed", w.served() == {})
    step(1031.0)
    check("after 30 s the table appears -- as it WAS", w.served() == {"257": s1}, w.served())
    check("a table whose creator did not allow watching never appears",
          "258" not in w.served() and "258" not in w.hist)
    write({"257": {"watchable": True, "state": s2}})
    step(1040.0)
    step(1050.0)
    check("a new move stays hidden for 30 s (the old state is still served)",
          w.served()["257"] == s1)
    step(1071.0)
    check("...then shows", w.served()["257"] == s2)
    write({"257": {"watchable": False}})
    step(1072.0)
    check("switching watching off hides the table AT ONCE, history and all",
          w.served() == {} and "257" not in w.hist)
    write({"259": {"watchable": True, "state": s1}})
    step(1080.0)
    write({})
    step(1100.0)
    step(1111.0)
    check("a table that ended is still shown late, then drops",
          "259" in w.served())
    step(1131.0)
    check("...once the delay has caught up with its end", "259" not in w.served())
    write({"260": {"watchable": True, "state": s1}}, stamp=1131.0 - 500)
    step(1140.0)
    step(1180.0)
    check("a stale file (the jan server gone) shows no tables", w.served() == {})
    boardjan.WATCH = w
    write({"261": {"watchable": True, "state": s1}})
    step(1200.0)
    step(1231.0)
    st, body = boardjan.route("/watch.json", {"t": ["261"]}, None)[:2]
    j = json.loads(body)
    check("/watch.json serves the delayed table", st == 200 and j["state"] == s1
          and j["delay"] == 30, (st, j))
    st2, body2 = boardjan.route("/watch.json", {"t": ["258"]}, None)[:2]
    check("...and shows nothing of one that is not watchable (state null, not an error)",
          st2 == 200 and json.loads(body2)["state"] is None, body2)
    lst = boardjan.WATCH.summary()
    check("the page's list names the watchable tables", lst and lst[0]["id"] == "261"
          and lst[0]["names"] == ["A"], lst)
    # portraits: a player's member id never leaves the board -- it becomes their face
    s3 = json.loads(json.dumps(s1))
    s3["seats"][0]["mid"] = 15
    s3["seats"].append({"name": "", "bot": True, "mid": None, "score": 25000})
    real = boardjan.face_ids
    boardjan.face_ids = lambda mids, ttl=60.0: {15: 202 * 8 + 3}
    try:
        pub = boardjan.watch_public(s3)
    finally:
        boardjan.face_ids = real
    check("/watch.json swaps a member id for the PlayOnline portrait (no id leaves)",
          "mid" not in json.dumps(pub) and pub["seats"][0]["face"] == 202 * 8 + 3
          and "face" not in pub["seats"][1], pub)
    if os.path.exists(os.path.join(boardjan.FACES_DIR, "hnf202.png")):
        from PIL import Image as _ImF
        import io as _ioF
        png = boardjan.face_png(202 * 8 + 3)
        check("a portrait is one 64x96 tile of its sheet (ACKY Gallery, the COM faces)",
              png and _ImF.open(_ioF.BytesIO(png)).size == (64, 96))
        check("...face 0 is PlayOnline's blank, an unknown sheet is none",
              boardjan.face_png(0) and boardjan.face_png(8 * 999) is None)
    boardjan.WATCH = boardjan.Watch()


def discord_checks(tmp, boardjan, polboards):
    import contextlib
    import io
    print("Discord -- one message, edited in place")
    hook = "https://discord.com/api/webhooks/123/SECRETTOKEN"
    state = os.path.join(tmp, "state", "jan_discord.json")
    args = polboards.build_parser().parse_args(
        ["--jan-port", "1", "--jan-url", "https://jan.example"])
    boardjan._SNAP.update(t=0.0, snap=None)
    snap = boardjan.snapshot(args)
    payload, files = boardjan.discord_message(snap, args)
    e = payload["embeds"][0]
    check("the message has every category as text, linked to the page",
          all(c["name"] in e["description"] for c in snap["categories"])
          and e["url"] == "https://jan.example" and "Quinn" in e["description"])
    if boardjan.board() is not None:
        check("...and the Jan Rating screen as its image",
              files and files[0][0] == "jan-rankings.png" and files[0][2][:4] == b"\x89PNG"
              and e["image"]["url"] == "attachment://jan-rankings.png")
    check("no em dashes in anything posted", " - " not in json.dumps(payload,
                                                                         ensure_ascii=False))
    net = FakeNet()
    d = polboards.Discord("jan", hook, state, every=60, ttl=600, refresh=1800, opener=net)
    build = lambda: boardjan.discord_message(snap, args)      # noqa: E731
    net.script = [(200, {"id": "999"})]
    check("the first tick POSTs ?wait=true and keeps the id",
          d.tick("s1", build, now=1000.0) == "posted" and d.msg_id == "999"
          and net.calls[-1][0] == "POST" and net.calls[-1][1].endswith("?wait=true"))
    check("...across a restart (no second board)",
          polboards.Discord("jan", hook, state, opener=net).msg_id == "999")
    check("...but not for a different webhook",
          polboards.Discord("jan", hook + "x", state, opener=net).msg_id is None)
    n0 = len(net.calls)
    check("nothing inside the interval, nothing for an unchanged board",
          d.tick("s2", build, now=1030.0) is None and d.tick("s1", build, now=1100.0) is None
          and len(net.calls) == n0)
    check("a changed board is EDITED in place",
          d.tick("s2", build, now=1100.0) == "edited" and net.calls[-1][0] == "PATCH"
          and net.calls[-1][1].endswith("/messages/999"))
    check("an unchanged board is re-edited after --discord-refresh",
          d.tick("s2", build, now=1100.0 + 1801) == "edited")
    net.script = [(404, {"message": "Unknown Message"}), (200, {"id": "1000"})]
    check("a deleted message is posted again",
          d.tick("s3", build, now=5000.0) == "posted" and d.msg_id == "1000")
    net.script = [(429, {"retry_after": 30})]
    check("a 429 is waited out", d.tick("s4", build, now=6000.0) == "limited"
          and d.tick("s4", build) is None)
    d.hold_until = 0.0
    out = io.StringIO()
    net.script = [(500, {"message": "boom"})]
    with contextlib.redirect_stdout(out):
        d.tick("s5", build, now=9000.0)
    check("a failure is logged WITHOUT the webhook's secret",
          "failed" in out.getvalue() and "SECRETTOKEN" not in out.getvalue(), out.getvalue())
    net.script = [(200, {"id": "e1"})]
    d.say("hello")
    check("a status post is kept for the sweep", [x["id"] for x in d.events] == ["e1"])
    net.script = [(204, {})]
    check("...and deleted after the TTL",
          d.sweep(now=time.time() + 601) == 1 and d.events == [])
    s_a = boardjan.snapshot(args)
    s_b = json.loads(json.dumps(s_a))
    rows = s_b["categories"][0]["rows"]
    rows[0], rows[1] = rows[1], rows[0]
    ev = boardjan.discord_events(s_a, s_b)
    check("a new #1 is news (and only in the category it changed)",
          len(ev) == 1 and rows[0]["name"] in ev[0]
          and boardjan.CATEGORIES[0] in ev[0], ev)
    s_c = json.loads(json.dumps(s_a))
    s_c["categories"][0]["rows"][0]["name"] = ""
    check("...but a name that failed to read is not",
          boardjan.discord_events(s_c, s_a) == [] and boardjan.discord_events(s_a, s_c) == [])
    check("no webhook = nothing posted, whatever the board does",
          polboards.Discord("jan", "", state, opener=net).tick("x", build) is None)
    _url = os.environ.pop("POL_DATABASE_URL")          # this one is about files
    try:
        check("without a database or a state dir the id is not kept (and main() says so)",
              os.path.normpath(polboards.state_path(args, "jan"))
              in (os.devnull, os.path.normpath("/state/jan_discord.json"))
              or os.environ.get("POL_BOARDS_STATE_DIR"))
    finally:
        os.environ["POL_DATABASE_URL"] = _url
    check("with a database the id is a jan_board_state row",
          polboards.state_path(args, "jan") == "db:jan_discord")
    boardjan._SNAP.update(t=0.0, snap=None)
    net2 = FakeNet()
    d2 = polboards.Discord("jan", hook, os.path.join(tmp, "w.json"), opener=net2)
    net2.script = [(200, {"id": "w1"})]
    polboards.watch(boardjan, args, d2, period=0.01, rounds=2)
    check("the watcher posts once and then holds (same board, inside the interval)",
          [c[0] for c in net2.calls] == ["POST"], [c[0] for c in net2.calls])


def snapshot_rows():
    """The rank snapshot as stored: (category, order, updated_at) per row."""
    import janstore
    janstore.ensure_schema()
    return [(r["category"], r["data"], r["updated_at"]) for r in janstore.db.query(
        "SELECT category, data, updated_at FROM jan_rank_snapshot ORDER BY category")]


def main():
    if janpg.fresh_database() is None:
        return janpg.skip_or_fail("jan_board_test")
    tmp = tempfile.mkdtemp(prefix="boardjan-")
    res = os.path.join(tmp, "resources")
    os.makedirs(res)
    os.environ["POL_RESOURCE_DIR"] = res
    # the live-session marker stays in this process's own store
    os.environ.pop("POL_VALKEY_URL", None)
    janpg.pol_accounts({1: [("Lex", True, None)],
                        2: [("Quinn", True, None), ("OldName", False, None)],
                        3: [("Elena", True, None)]})
    # three players: 1 strong, 2 middling, 3 never played (not ranked)
    recs = {1: {"games_played": 4, "places": [3, 1, 0, 0], "result_x10": 800},
            2: {"games_played": 6, "places": [1, 2, 2, 1], "result_x10": -150},
            3: {"games_played": 0}}
    import janstats
    from polcore import blobs

    def put_record(m, r):
        """A stored record as janstats keeps it (see janstats.STATS_PATH)."""
        blobs.put(str(m), janstats.STATS_PATH, json.dumps(r).encode("utf-8"))

    def get_record(m):
        return json.loads(blobs.get(str(m), janstats.STATS_PATH))

    for m, r in recs.items():
        put_record(m, r)
    import boardjan
    import polboards

    print("the data")
    db_before = janpg.accounts_fingerprint()
    s = boardjan.snapshot()
    check("five categories, janstats' own order",
          [c["name"] for c in s["categories"]] == list(janstats.RANK_CATEGORIES))
    rating = s["categories"][0]["rows"]
    check("only members who have PLAYED are ranked (2 of 3)",
          s["members"] == 3 and len(rating) == 2, (s["members"], len(rating)))
    # SE's scale: 7 + (result / 10) / (games + 2); Lex +80 over 4, Quinn -15 over 6
    check("Jan Rating is best first, on SE's ~7 scale",
          [r["name"] for r in rating] == ["Lex", "Quinn"]
          and abs(rating[0]["value"] - (7.0 + 8.0 / 6)) < 1e-5
          and abs(rating[1]["value"] - (7.0 - 1.5 / 8)) < 1e-5, rating)
    check("the name is the member's PRIMARY handle",
          rating[1]["name"] == "Quinn")
    check("games and level ride along",
          rating[0]["games"] == 4 and rating[0]["level"] >= 1)
    check("with no previous list, every row is 'new'",
          all(r["move"] == "new" and r["prev"] is None for r in rating))
    check("the account tables are unchanged after reading names",
          janpg.accounts_fingerprint() == db_before)
    check("the board NEVER writes the rank snapshot the client's glyph uses",
          snapshot_rows() == [])

    print("movement")
    janstats.rank_list(0)                       # the GAME's call: remembers
    before = snapshot_rows()
    check("the game's own call remembers the order", [c for c, _o, _t in before] == ["0"]
          and before[0][1] == [1, 2], before)
    put_record(2, {"games_played": 7, "places": [4, 2, 1, 0], "result_x10": 1200})
    boardjan._SNAP.update(t=0.0, snap=None)
    rating = boardjan.snapshot()["categories"][0]["rows"]
    check("Quinn overtakes Lex: up for Quinn, down for Lex",
          [(r["name"], r["move"], r["prev"]) for r in rating]
          == [("Quinn", "up", 2), ("Lex", "down", 1)], rating)
    check("...and the snapshot is still the game's, untouched by the board",
          snapshot_rows() == before)
    check("move_of: same / new", boardjan.move_of(3, 3) == "same"
          and boardjan.move_of(255, 0) == "new")
    check("winnings are whole JAN, floats only for rating and title score",
          isinstance(boardjan.snapshot()["categories"][2]["rows"][0]["value"], int)
          and s["categories"][0]["float"] and not s["categories"][2]["float"])

    print("the sidebar: recent games, games in progress")
    for m, hist in ((1, [{"place": 1, "result_x10": 29, "score": 22900, "t": 1500, "table": 2}]),
                    (2, [{"place": 0, "result_x10": 469, "score": 36900, "t": 2000, "table": 1},
                         {"place": 3, "result_x10": -200, "score": 9000, "t": 1000, "table": 1}])):
        r = get_record(m)
        r["history"] = hist
        put_record(m, r)
    rg = boardjan.snapshot()["recent"]
    check("recent games are newest first, across players, with names and signed results",
          [(g["name"], g["place"], g["result"], g["t"]) for g in rg]
          == [("Quinn", 0, 46.9, 2000), ("Lex", 1, 2.9, 1500), ("Quinn", 3, -20.0, 1000)], rg)
    os.environ["POL_DATA_DIR"] = tmp
    import live_sessions
    check("no marker = no claim either way (None, not 0)", boardjan.live_games() is None)
    live_sessions.write_marker(boardjan.LIVE_MARKER, 2)
    check("a fresh marker = its count of games in progress", boardjan.live_games() == 2)
    check("...a stale one (past pol-git-sync's grace) = 0",
          boardjan.live_games(time.time() + boardjan.LIVE_GRACE_S + 5) == 0)
    # the snapshot below must see a stale marker: an old record in place of
    # the fresh one
    from polcore import kv
    kv.set(live_sessions.marker_key(boardjan.LIVE_MARKER),
           json.dumps({"count": 2, "stamp": time.time() - boardjan.LIVE_GRACE_S - 5}))
    check("...and the snapshot carries it", boardjan.snapshot()["live_games"] == 0)

    print("names when the account database is unreachable")

    def unreachable():
        raise ConnectionError("the account database is down")
    real_conn = boardjan._accounts_conn
    boardjan._accounts_conn = unreachable
    boardjan._NAMES.update(t=0.0, map={})
    try:
        rows = boardjan.snapshot()["categories"][0]["rows"]
    finally:
        boardjan._accounts_conn = real_conn
    check("rows still come back, just without names",
          len(rows) == 2 and all(r["name"] == "" for r in rows))
    boardjan._NAMES.update(t=0.0, map={})

    print("the game's text rules")
    check("mailfmt matches SE's own examples (9.0283365 / '0.0      ' / '18.5   ')",
          boardjan.mailfmt(9.0283365, 7) == b"9.0283365"
          and boardjan.mailfmt(0.0, 7) == b"0.0      "
          and boardjan.mailfmt(18.5, 4) == b"18.5   ",
          (boardjan.mailfmt(9.0283365, 7), boardjan.mailfmt(0.0, 7),
           boardjan.mailfmt(18.5, 4)))
    b = boardjan.board()
    baked = b is not None and os.path.exists(
        os.path.join(boardjan.ART_DIR, (b.get("bases") or ["?"])[0]))
    if not baked:
        print("  (art not baked -- run tools/jan_boardart_bake.py; render checks skipped)")
    else:
        S = b["strings"]
        pf = janstats.RANK_PREV_VALUE[0]
        cases = [(255, 0, S["new"]), (4, 1, S["up"]), (2, 2, S["same"]), (0, 3, S["down"])]
        got = [boardjan.row_strings(b, 0, idx, {pf: prev, 2: "X"})["ranking"]
               for prev, idx, _w in cases]
        check("the rank string carries the game's own marker words (New/up/-/down)",
              all(g.startswith(w.encode("cp932")) for g, (_p, _i, w) in zip(got, cases)),
              got)
        check("a name the ROM cannot hold becomes '?', never a crash",
              b"?" in boardjan.row_strings(b, 0, 0, {2: "Zoë☃", pf: 255})["ChrName"])
        lab, hhmm = boardjan.update_strings(b, now=1679 * 604800 + 100)
        check("the update label keeps the whole word (no 'Upda' overwrite) and the "
              "week start in JST", lab.endswith(b"  2002/03/07") and hhmm == b"09:00"
              and not lab.startswith(b"Upda "), (lab, hhmm))
        import struct
        for cat in range(5):
            png = boardjan.render(cat, 0)
            w, h = struct.unpack(">II", png[16:24])
            check("category %d renders the 640x448 screen" % cat,
                  png[:8] == b"\x89PNG\r\n\x1a\n" and (w, h) == (640, 448))
        check("a page past the end is clamped to the last page",
              boardjan.render(0, 99) == boardjan.render(0, 0))
        boardjan._SNAP.update(t=0.0, snap=None)
        s2 = boardjan.snapshot()
        cells = [r["cells"] for r in s2["categories"][0]["rows"]]
        check("every row carries the game's five strings as TEXT for the page",
              all(set(c) == {"ranking", "ChrName", "ChrLevel", "Str4", "Str5"}
                  for c in cells) and cells[0]["ChrName"] == " Quinn", cells)
        check("the update label rides in the snapshot as text",
              s2["update"] and s2["update"][0].startswith("Update  "), s2["update"])
        lay = b.get("layers") or {}
        check("the bake has the page's layers: bg, 5 buttons, 4 plates",
              lay.get("bg") == "bg.png"
              and len(lay.get("buttons", [])) == 5 and len(lay.get("plates", {})) == 4)
        T = b.get("title") or {}
        check("the title is TEXT for every tab, and the game is JongHoLow",
              len(T.get("text", [])) == 5 and T["text"][0] == "JongHoLow Ranking"
              and lay["buttons"][0]["caption"] == "JongHoLow"
              and "Janhourou" not in json.dumps(T) + json.dumps(lay["buttons"]))
        check("...with its face on disk for render.png",
              os.path.exists(os.path.join(boardjan.ART_DIR, T.get("ttf", "?"))))
        from PIL import Image as _Im
        masks = {}
        for n, size in (("mask_main.png", (1280, 896)), ("mask_side.png", (440, 896))):
            try:
                im = _Im.open(os.path.join(boardjan.ART_DIR, n))
                masks[n] = (n in boardjan.art_files() and im.mode == "RGBA" and im.size == size
                            and im.getpixel((0, 0))[3] == 0
                            and im.getpixel((size[0] // 2, size[1] // 2))[3] == 255)
            except OSError:
                masks[n] = False
        tb = (b.get("fixups") or {}).get("table") or {}
        check("the page's column model: one alignment per column, a kind per tab",
              len(tb.get("kinds", [])) == 5
              and all(k in ("num", "dec", "text", "") for ks in tb["kinds"] for k in ks)
              and tb["c4"][0] < tb["c4"][2] < tb["c4"][1] < tb["c5"][0] < tb["c5"][1], tb)
        check("the sheets' deckled edges: RGBA masks at 2x, clear at the corner, "
              "paper in the middle", all(masks.values()), masks)
        check("no SE heading pictures are left to serve",
              not [n for n in boardjan.art_files() if n.startswith("head_")])
        missing = [n for n in ["bg.png", "janrom-fixed.woff2", "janrom-prop.woff2",
                               "btn138_s0.png", "btn138_s1.png", "btn138_s3.png",
                               T.get("font", "?"), "tiles_l.png", "stick_riichi.png"]
                   + [p["img"] for p in lay.get("plates", {}).values()]
                   if n not in boardjan.art_files()]
        check("every layer and both fonts are servable art", not missing, missing)

    discord_checks(tmp, boardjan, polboards)
    watch_checks(tmp, boardjan)
    live_feed_checks(tmp, boardjan, polboards)
    bot_checks(boardjan, polboards)

    print("the service")
    x = socket.socket()
    x.bind(("127.0.0.1", 0))
    port = x.getsockname()[1]
    x.close()
    args = polboards.build_parser().parse_args(["--jan-port", str(port)])
    srv = polboards.serve(boardjan, args, port)
    base = "http://127.0.0.1:%d" % port

    def get(p):
        try:
            with urllib.request.urlopen(base + p, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()
    try:
        st, ct, body = get("/")
        check("/ is the page", st == 200 and ct.startswith("text/html"))
        st, ct, body = get("/state.json")
        j = json.loads(body)
        check("/state.json is the snapshot", st == 200 and j["board"] == "jan"
              and len(j["categories"]) == 5)
        for bad in ("/art/../boardjan.py", "/art/%2e%2e/janstats.py", "/art/x.png"):
            check("%s is refused" % bad, get(bad)[0] == 404)
        check("/healthz", get("/healthz")[:1] == (200,))
        st, ct, body = get("/watch")
        check("/watch is the watching page, in 3D", st == 200 and ct.startswith("text/html")
              and b"watch.json" in body and b"three.module.min.js" in body)
        if os.path.exists(os.path.join(boardjan.ART_DIR, "w3d.json")):
            st, ct, body = get("/art/three.module.min.js")
            check("three.js is served from our own site, as JavaScript",
                  st == 200 and ct.startswith("text/javascript") and b"Three.js Authors" in body[:400])
            st, ct, body = get("/art/w3d.json")
            w3d = json.loads(body) if st == 200 else {}
            check("the game's pieces: one tile mesh, 34 faces + 3 red fives, the table",
                  len(w3d.get("faces", [])) == 34 and len(w3d.get("red", [])) == 3
                  and len(w3d.get("faceIdx", [])) == 4 and w3d.get("table"))
            check("...and the seven call banners",
                  all(get("/art/call-%s.png" % k)[0] == 200
                      for k in ("riichi", "ron", "tsumo", "pon", "kan", "chi", "draw")))
        if os.path.exists(os.path.join(boardjan.FACES_DIR, "hnf202.png")):
            st, ct, body = get("/face.png?id=%d" % (202 * 8))
            check("/face.png serves a portrait by its PlayOnline id",
                  st == 200 and ct == "image/png" and body[:4] == b"\x89PNG", (st, ct))
        else:
            print("  (no portrait sheets in services/boardart/faces -- /face.png "
                  "serving skipped)")
        check("/face.png refuses what is not a portrait id",
              get("/face.png?id=x")[0] == 400 and get("/face.png?id=99999")[0] == 404)
        if boardjan.board() is not None:
            from PIL import Image as _Im2
            st, ct, body = get("/art/tiles_l.png")
            import io as _io
            check("the tile sheet is served: 38 tiles of 29x38 (SE's jhai kit)",
                  st == 200 and _Im2.open(_io.BytesIO(body)).size == (38 * 29, 38))
        if boardjan.board() is not None:
            st, ct, body = get("/art/janrom-prop.woff2")
            check("the ROM web font is served as font/woff2",
                  st == 200 and ct == "font/woff2" and body[:4] == b"wOF2", (st, ct))
            st, ct, body = get("/art/board.json")
            check("the layout is served as JSON", st == 200 and ct.startswith(
                "application/json") and "layers" in json.loads(body))
            check("the ROM itself (kanji.bin) is not served",
                  get("/art/kanji.bin")[0] == 404)
        check("--jan-port 0 would start nothing",
              polboards.build_parser().parse_args([]).jan_port == 0)
    finally:
        srv.shutdown()
    print("[jan_board_test] OK -- %d checks" % len(CHECKS))


if __name__ == "__main__":
    sys.exit(main())
