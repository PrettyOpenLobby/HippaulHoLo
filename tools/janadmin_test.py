#!/usr/bin/env python3
"""janadmin_test.py -- JongHoLow's tools for an OpenLobby admin panel (services/janadmin.py),
over HTTP, the way the panel's proxy calls them.

    python tools/janadmin_test.py

Three made-up players: Lex and Mira seated at one table that is being played, Arden watching
it. Checks the token, the description (three pages, the three actions with the panel's kinds),
the status, every page without a script, a notice and a close landing in jan:gm-notice as
janworld.gmnotice delivers them, a kick by handle, by POL ID and from the Tables page posted for
the right member, the event operations janevent.py has (open by hand, close, the schedule),
and the account window's player lookup (a seated player online at their table; nothing for a
member with no record, and nothing sensitive).
Throwaway PostgreSQL (janpg.py); the live keys are this process's own store.
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jan_testenv  # noqa: E402

jan_testenv.setup()
import janpg        # noqa: E402

TMP = tempfile.mkdtemp(prefix="janadmin-")
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
os.environ["POL_JAN_ADMIN_TOKEN"] = TOKEN = "janadmin-test-token-1"
os.environ.pop("POL_VALKEY_URL", None)          # live keys stay in this process
for k in ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
          "POL_JAN_EVENT_OPEN", "POL_JAN_EVENT_CLOSE", "POL_JAN_EVENT_AUTO"):
    os.environ.pop(k, None)

if janpg.fresh_database() is None:
    sys.exit(janpg.skip_or_fail("janadmin_test"))

fails = []


def check(name, ok, detail=""):
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            "  " + str(detail) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


janpg.pol_accounts({1: [("Lex", True, None)], 2: [("Mira", True, None)],
                    3: [("Arden", True, None)]})
LEX, MIRA, ARDEN = 1, 2, 3

import janadmin     # noqa: E402
import janevent     # noqa: E402
import janseats     # noqa: E402
import janstats     # noqa: E402
import janstore     # noqa: E402
from janworld import gmnotice  # noqa: E402
from polcore import blobs      # noqa: E402

now = time.time()
TID = janseats.room_table_id(101, 1)            # Room 1-1, Table 1
janstore.Snapshot(janstore.seats_key()).write({"v": 2, "tables": {str(TID): {
    "master": LEX, "in_play": now,
    "seats": {str(LEX): {"seat": 0, "name": "Lex", "polid": 11, "at": now},
              str(MIRA): {"seat": 1, "name": "Mira", "polid": 12, "at": now}},
    "gallery": {str(ARDEN): {"slot": 0, "name": "Arden", "polid": 13, "at": now}}}}})
janstore.kv.set_json(janstore.watch_key(), {"stamp": now, "tables": {str(TID): {
    "watchable": True, "state": {
        "room": 101, "table": 1, "round": {"wind": "S", "number": 2, "honba": 1}, "wall": 48,
        "seats": [{"name": "Lex", "score": 31000, "mid": LEX}, {"name": "Mira", "score": 24000, "mid": MIRA},
                  {"name": "COM 1", "bot": True, "score": 22000}, {"name": "COM 2", "bot": True,
                                                                  "score": 23000}]}}}})
import live_sessions  # noqa: E402
live_sessions.write_marker("authsess-jan", 2)

port = janadmin.build()
host, n = port.start()
url = "http://127.0.0.1:%d" % n


def get(path, form=None, token=TOKEN):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url + path, data=data, method="GET" if form is None else "POST",
                                 headers={"X-Devtool-Token": token, "X-Devtool-Actor": "tobin-gm",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def action(aid, params):
    r = urllib.request.Request(url + "/openlobby/actions/" + aid, data=json.dumps(params).encode(),
                               headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "tobin-gm"},
                               method="POST")
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())


def doc_posts():
    gmnotice._store().forget()
    return [p for p in (gmnotice._read().get("posts") or [])]


print("the protocol")
check("without the token nothing answers", get("/openlobby/module.json", token="x")[0] == 403)
desc = json.loads(get("/openlobby/module.json")[1])
check("it describes itself as jan, JongHoLow, PS2, content 3",
      (desc["key"], desc["title"], desc["platform"], desc["content_ids"])
      == ("jan", "JongHoLow", "PlayOnline: PS2", [3]), desc)
check("...with three pages, each its own id",
      [(t["id"], t["path"]) for t in desc["tools"]]
      == [("events", "events"), ("tables", "tables"), ("notices", "notices")], desc["tools"])
acts = {a["id"]: a for a in desc["actions"]}
check("...and the panel module's three actions with its kinds and fields",
      sorted(acts) == ["close", "kick", "notice"]
      and (acts["notice"]["kind"], [f["id"] for f in acts["notice"]["fields"]]) == ("announce", ["text"])
      and (acts["close"]["kind"], acts["close"]["fields"]) == ("", [])
      and (acts["kick"]["kind"], [f["id"] for f in acts["kick"]["fields"]]) == ("kick", ["handles"])
      and acts["kick"]["danger"], acts)
live = json.loads(get("/openlobby/status.json")[1])
check("the status: online from the authsess-jan marker, the table as a room",
      live.get("online") == 2 and live["rooms_label"] == "tables"
      and live["rooms"] == [{"name": "Room 1-1, Table 1", "detail": "2/4 players, South 2, honba 1",
                             "state": "playing"}], live)
check("...no event, no summary", live["summary"] == "", live)

print("the pages")
for p in ("/events", "/tables", "/notices"):
    st, body = get(p)
    check("%s renders, without a script" % p, st == 200 and "<script" not in body.lower()
          and "<h1>" in body, body[:200])
st, body = get("/tables")
check("Tables shows who sits where, the master, the round, scores and the watcher",
      all(x in body for x in ("Room 1-1, Table 1", "1: Lex (master)", "2: Mira", "South 2, honba 1",
                              "Lex 31,000", "COM 1 (COM)", "Arden")), body[-1500:])
check("...with a Kick for each seat", body.count(">Kick</button>") == 2)

print("GM notice")
gmnotice.clear()
st, body = get("/notices", {"text": "  Maintenance   in ten minutes. "})
ps = doc_posts()
check("a notice lands in jan:gm-notice, spaces folded",
      "Notice #1 posted" in body and len(ps) == 1 and ps[0]["op"] == "open"
      and ps[0]["text"] == "Maintenance in ten minutes." and ps[0]["member"] == 0, ps)
recs = gmnotice.pending_for(MIRA)
check("...and janworld.gmnotice hands it to a player on their next line",
      len(recs) == 1 and gmnotice.opcodes_name(recs[0]).endswith("NOTICEMSGOPEN"),
      [gmnotice.opcodes_name(r) for r in recs])
check("an empty notice is refused", "needs text" in get("/notices", {"text": " "})[1]
      and len(doc_posts()) == 1)
out = action("notice", {"text": "From the panel"})
check("the notice action works for the panel too",
      out["ok"] and doc_posts()[-1]["text"] == "From the panel", out)
out = action("close", {})
check("the close action posts a close for everyone",
      out["ok"] and doc_posts()[-1]["op"] == "close" and doc_posts()[-1]["member"] == 0, out)
st, body = get("/notices", {"close": "1"})
check("...and so does the page's Close", doc_posts()[-1]["op"] == "close" and len(doc_posts()) == 4)
st, body = get("/notices", {"handles": "mira"})
p = doc_posts()[-1]
check("a kick by handle (any case) posts for that member alone",
      "Kick posted for 1 member" in body and p["op"] == "kick" and p["member"] == MIRA, p)
check("the posts list names the kicked player", "Mira" in get("/notices")[1])
out = action("kick", {"polid": "jantest01", "handles": ""})
p = doc_posts()[-1]
check("an account's Kick ({polid, handles}) finds the member by POL ID",
      out["ok"] and p["op"] == "kick" and p["member"] == LEX, (out, p))
out = action("kick", {"handles": "Lex, Arden"})
check("...and several handles post one kick each",
      out["ok"] and [(q["op"], q["member"]) for q in doc_posts()[-2:]] == [("kick", LEX), ("kick", ARDEN)],
      out)
out = action("kick", {"handles": "Marigold"})
check("an unknown handle is refused, nothing posted", not out["ok"] and doc_posts()[-1]["member"] == ARDEN, out)
st, body = get("/tables", {"kick": str(MIRA)})
check("Kick on the Tables page posts for that seat's member",
      "Kick posted" in body and doc_posts()[-1] == dict(doc_posts()[-1], op="kick", member=MIRA))

print("the event")
st, body = get("/events")
check("with nothing stored the page says so", "none stored" in body and "No event is running" in body)
check("closing with nothing stored is refused", "No event is stored" in get("/events", {"close": "1"})[1])
st, body = get("/events", {"open": "Tobin Cup", "days": "2"})
rec = janevent.record()
check("Open stores a new event from now, as janevent.py --open does",
      rec.get("id") == 1 and rec.get("name") == "Tobin Cup" and not rec.get("auto")
      and abs(rec["closes_at"] - rec["opens_at"] - 2 * 86400) <= 1 and janevent.is_running(), rec)
check("...and the page shows it running", "Event 1, Tobin Cup, is open" in body
      and "Tobin Cup (id 1)" in body and "yes, 1d 23h left" in body, body[-2500:])
check("a bad number of days is refused", "Days is a number" in get("/events", {"open": "X", "days": "two"})[1]
      and janevent.record()["id"] == 1)
# Lex has won inside this event
blobs.put(str(LEX), janstats.STATS_PATH, json.dumps(
    {"games_played": 3, "places": [2, 1, 0, 0], "result_x10": 900, "event": 1,
     "result_x10_event": 1200, "games_event": 2}).encode("utf-8"))
blobs.put(str(MIRA), janstats.STATS_PATH, json.dumps(
    {"games_played": 1, "places": [0, 0, 1, 0], "result_x10": -100}).encode("utf-8"))
st, body = get("/events")
check("the standings list who won in this event (Lex), not who has not (Mira)",
      "<td>Lex</td><td>120,000</td><td>2</td>" in body and "<td>Mira</td>" not in body, body[-2500:])
live = json.loads(get("/openlobby/status.json")[1])
check("the status's summary is the running event", live["summary"].startswith("Event: Tobin Cup, until"),
      live["summary"])
keep_cal = janevent._calendar
FRI = int(now // 86400) * 86400
janevent._calendar = lambda t: {"key": "weekend", "name": "Weekend Cup", "start": FRI,
                                "end": FRI + 72 * 3600}
try:
    st, body = get("/events", {"rotate": "1"})
    check("Run the schedule leaves a hand-opened event alone, as auto_rotate does",
          "hand-opened, left alone" in body and janevent.record()["id"] == 1, body[-800:])
    st, body = get("/events", {"close": "1"})
    rec = janevent.record()
    check("Close ends the running event now, as janevent.py --close does",
          "is closed" in body and rec["id"] == 1 and not janevent.is_running(), rec)
    check("...and closing again is refused", "not running" in get("/events", {"close": "1"})[1])
    st, body = get("/events", {"rotate": "1"})
    rec = janevent.record()
    check("the schedule then opens the calendar's window as a new event",
          "opened" in body and rec["id"] == 2 and rec["name"] == "Weekend Cup" and rec["auto"], rec)
finally:
    janevent._calendar = keep_cal

print("one player, for the account window")
check("the description says it answers for a player", desc.get("player") is True, desc)


def player(**q):
    st, body = get("/openlobby/player.json?" + urllib.parse.urlencode(q))
    return st, json.loads(body)


st, doc = player(polid="", handles="lex", members="")
ch = (doc.get("characters") or [{}])[0]
facts = {f["label"]: f["value"] for f in ch.get("facts") or []}
check("a player by handle (any case): level, rank, rating, games, first places, JAN",
      st == 200 and len(doc["characters"]) == 1 and ch["name"] == "Lex"
      and facts == {"level": "1", "rank": "tier 1 of 19", "rating": "8.80", "games": "3",
                    "first places": "2", "JAN": "90,000"}, doc)
check("...seated now, so online, with the table named", ch["online"] is True
      and ch["detail"] == "Lv 1, 3 games, at Room 1-1, Table 1", ch)
st, doc2 = player(polid="jantest01", handles="", members="")
check("the same player by POL ID", doc2.get("characters") == doc["characters"], doc2)
st, doc3 = player(polid="", handles="", members="%d,%d" % (LEX, MIRA))
check("several members at once, each its own entry",
      [c["name"] for c in doc3.get("characters") or []] == ["Lex", "Mira"], doc3)
st, none = player(polid="", handles="Arden", members="")
check("a member with no record is nothing", st == 200 and none == {}, none)
st, none = player(polid="NOBODY00", handles="Marigold", members="")
check("an account the game never saw is nothing", none == {}, none)
text = json.dumps(doc3)
check("nothing sensitive in the answer",
      not any(s in text.lower() for s in ("jantest01", "password", "session", "token", "127.0.0.1")), text)

port.stop()
print("janadmin_test: %s" % ("all checks passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
