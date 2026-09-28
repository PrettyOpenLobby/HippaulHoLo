#!/usr/bin/env python3
"""jan_watch_test.py -- web watching, end to end: a real game in jangame ->
the tables document the Jan server publishes (Valkey `jan:tables-live`; this
process's in-memory store here) -> the board's 30 s delayed view.

    python tools/jan_watch_test.py

Pins the watching rules (decided 2026-09-12): only tables whose creator allowed
watchers appear; a table that may not be watched is listed WITHOUT its state;
the view is all four hands (like SE's in-game spectator) but never the wall
order or the ura dora; the board never serves the live state.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="janwatch-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
os.environ["POL_JAN_WATCH_KEY"] = "jan:test:%s:tables-live" % os.path.basename(TMP)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
import jan_testenv                # noqa: E402
jan_testenv.setup(need_core=False)

import janstore                   # noqa: E402
import janwire                    # noqa: E402
import janmsgs as M               # noqa: E402
import jangame                    # noqa: E402
import boardjan                   # noqa: E402

CHECKS = []


def read(key):
    raw = janstore.kv.get(key)
    return json.loads(raw) if raw else None


class _Broken:
    """A live store that is down: every call raises."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("the live store is down")
        return fail


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-72s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""), flush=True)
    if not cond:
        raise AssertionError(label)


def main():
    m = jangame.Manager(sweeper=False)
    key = jangame.Manager.lobby_key(1, 2)             # room 2, table 1
    t = m.table_for_lobby(key, [(15, 0, "Alice", 0xA1)])
    t.fill_with_bots()
    ready = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                         length=0x18)[:0x18]
    t.handle(ready)
    check("a real game is running at the lobby table", t.state == "playing" and t.kyoku)

    print("the tables document")
    path = jangame.WATCH_FILE
    m.watchable = lambda k: False
    m._write_watch(force=True)
    d = read(path)
    check("a table whose creator did not allow watching is listed WITHOUT its state",
          d["tables"] == {str(key): {"watchable": False}}, d["tables"])
    m.watchable = lambda k: k == key
    m._write_watch(force=True)
    d = read(path)
    e = d["tables"][str(key)]
    st = e["state"]
    check("an allowed table carries its public state", e["watchable"] and st["state"] == "playing")
    check("...room and table from the lobby key", (st["room"], st["table"]) == (2, 1))
    check("...four seats, the player named, the bots flagged",
          [s["name"] for s in st["seats"]][0] == "Alice"
          and [s["bot"] for s in st["seats"]] == [False, True, True, True])
    sizes = sorted(len(s["hand"]) for s in st["seats"])
    check("...ALL FOUR hands face up, like SE's spectator (13, 13, 13, 14)",
          sizes == [13, 13, 13, 14], sizes)
    tiles = [x for s in st["seats"] for x in s["hand"]] + st["dora"]
    check("...tiles in web notation (1m..9s, 1z..7z, red fives 0m/0p/0s)",
          all(len(x) == 2 and x[0] in "0123456789" and x[1] in "mpsz" for x in tiles), tiles[:8])
    check("...one dora indicator, and NOT the ura dora or the wall",
          len(st["dora"]) == 1 and "ura" not in json.dumps(st) and "live" not in st
          and st["wall"] == t.kyoku.wall.remaining)
    check("...the round: East 1, honba 0, the dealer's turn",
          st["round"]["wind"] == "E" and st["round"]["number"] == 1 and st["turn"] == t.kyoku.dealer,
          st["round"])
    before = janstore.kv.get(path)
    body = m._watch_last[0]
    m._write_watch()
    check("an unchanged table is not rewritten inside the heartbeat interval",
          m._watch_last[0] == body and janstore.kv.get(path) == before)

    print("the board's delayed view of that document")
    clock = {"t": d["stamp"]}
    w = boardjan.Watch(key=path, delay=30, clock=lambda: clock["t"])
    w.sample()
    check("the board shows nothing live", w.served() == {})
    clock["t"] += 31
    m._write_watch(force=True)                        # the server's heartbeat
    w.sample()
    got = w.served().get(str(key))
    check("30 s later it shows the table as it was", got == st)

    print("a failed write never touches the game")
    live = janstore.kv.default()
    janstore.kv.reset(_Broken())
    try:
        m._write_watch(force=True)
    finally:
        janstore.kv.reset(live)
    check("...the manager carries on", t.state == "playing")

    print("the last human leaves before the game is over")
    t.drop_seat(0, "left")
    check("...the game stops", t.state == "finished")
    m._reap()
    m._write_watch(force=True)
    d = read(path)
    e = d["tables"].get(str(key)) or {}
    end = (e.get("state") or {}).get("ended")
    check("...its LAST frame stays in the document, saying who left",
          end == {"early": True, "seat": 0, "why": "left", "name": "Alice"}, e)
    check("...with the seat marked as left", e["state"]["seats"][0]["left"] is True)
    check("...while the table itself is forgotten", key not in m.by_lobby)
    c2 = {"t": d["stamp"]}
    w2 = boardjan.Watch(key=path, delay=30, clock=lambda: c2["t"])
    w2.sample()
    c2["t"] += 31
    m._write_watch(force=True)
    w2.sample()
    got = w2.served().get(str(key)) or {}
    check("the board shows that frame 30 s later", (got.get("ended") or {}).get("early") is True, got)
    check("...but no longer offers the table (the list, the Discord post)",
          all(x["id"] != str(key) for x in w2.summary()), w2.summary())
    when, entry = m._watch_final[str(key)]
    m._watch_final[str(key)] = (when - jangame.WATCH_FINAL_S - 1, entry)
    m._write_watch(force=True)
    check("after WATCH_FINAL_S the document lets it go",
          str(key) not in read(path)["tables"])
    jangame.WATCH_FILE = "0"
    janstore.kv.delete(path)
    m._write_watch(force=True)
    check("POL_JAN_WATCH_KEY=0 writes nothing",
          read(path) is None and janstore.kv.keys("jan:*") == [])
    print("[jan_watch_test] OK -- %d checks" % len(CHECKS))


if __name__ == "__main__":
    main()
