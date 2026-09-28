#!/usr/bin/env python3
"""jan_store_test.py -- Janhourou's state outside the process, on a real
PostgreSQL database and a real Valkey server.

    python tools/jan_store_test.py

Pins:
  * the migrations: services/jan_migrations/ applies next to OpenLobby's own
    set in one schema_migrations table, and a second run of either applies
    nothing;
  * the event record (jan_event): stored, read back, closed; the self-test
    works on a row of its own and leaves the server's row alone;
  * the ranking's previous order (jan_rank_snapshot): remembered per
    category, and a scoped writer (the self-test) never touches the real rows;
  * the board's Discord bookkeeping (polboards): message ids and the bot's
    channels go to jan_board_state when a database is configured;
  * live state across processes, on Valkey: seats and a table's rules one
    process publishes are what another process reads, and a room key one
    process learned is known to the next.

It needs a PostgreSQL server (Docker, or POL_TEST_DATABASE_URL) and, for the
cross-process part, Valkey (Docker, or POL_TEST_VALKEY_URL). Without them it
reports SKIP, or FAIL when POL_TEST_REQUIRE_DB=1.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.normpath(os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402

CORE = jan_testenv.setup(need_core=False)
import janpg                                                       # noqa: E402

TMP = tempfile.mkdtemp(prefix="jan-store-")
os.makedirs(os.path.join(TMP, "resources"))
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_LOG_DIR"] = TMP
os.environ.pop("POL_VALKEY_URL", None)
for _k in ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
           "POL_JAN_EVENT_OPEN", "POL_JAN_EVENT_CLOSE"):
    os.environ.pop(_k, None)

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  --  %r" % (detail,)) if detail and not ok else ""), flush=True)
    if not ok:
        FAILS.append(label)


def migrations(janstore):
    print("migrations")
    from polcore import db
    applied = db.migrate(directory=janstore.MIGRATIONS_DIR, log=lambda m: None)
    check("CrystalHoLo's set applies on an empty database", applied == ["4001_jan_state"],
          applied)
    tables = {r["table_name"] for r in db.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    check("...creating its jan_ tables",
          {"jan_event", "jan_rank_snapshot", "jan_board_state"} <= tables, tables)
    core = db.migrate(log=lambda m: None)
    names = [n for _v, n, _p in db.migration_files()]
    check("OpenLobby's own set still applies afterwards, every file of it",
          core == names and len(names) >= 1, (core, names))
    have = db.applied_migrations()
    check("both sets share schema_migrations without a clash",
          4001 in have and all(v < 1000 for v in have if v != 4001), sorted(have))
    again = (db.migrate(directory=janstore.MIGRATIONS_DIR, log=lambda m: None),
             db.migrate(log=lambda m: None))
    check("a second run of either set applies nothing", again == ([], []), again)


def event(janevent, janstore):
    print("the event record (jan_event)")
    janevent._RECORD_CACHE.update(row=None, at=0.0, rec={})
    check("nothing stored: no event", janevent.record() == {} and not janevent.is_running())
    rec = janevent.store(5, "Weekend Cup", opens_at=1000, closes_at=None)
    check("a stored event reads back", janevent.record() == rec and rec["id"] == 5)
    janevent._RECORD_CACHE.update(row=None, at=0.0, rec={})
    check("...from the table, not just this process's cache",
          janevent.record()["name"] == "Weekend Cup" and janevent.event_id(now=2000) == 5)
    row = janstore.db.query_one("SELECT data FROM jan_event WHERE name = 'current'")
    check("it is the 'current' row", row and row["data"]["id"] == 5, row)
    ok = janevent.selftest()
    check("the module's own self-test passes on this database", ok)
    rows = janstore.db.query("SELECT name, data FROM jan_event")
    check("...and leaves only the server's row, untouched",
          [(r["name"], r["data"]["id"]) for r in rows] == [("current", 5)], rows)
    janevent.store(5, "Weekend Cup", opens_at=1000, closes_at=3000)
    check("closing it ends the window", not janevent.is_running(now=3000)
          and janevent.is_running(now=2999))


def snapshot(janstats, janstore):
    print("the ranking's previous order (jan_rank_snapshot)")
    janstats._snapshot_store({"0": [3, 1, 2], "4": [7]})
    check("each category is remembered", janstats._snapshot_load() == {"0": [3, 1, 2], "4": [7]})
    janstats._snapshot_store({"0": [1, 3, 2]})
    check("...and replaced on the next list, the others kept",
          janstats._snapshot_load() == {"0": [1, 3, 2], "4": [7]})
    keep = janstats._SNAPSHOT_SCOPE[0]
    janstats._SNAPSHOT_SCOPE[0] = "selftest-x_y:"
    try:
        check("a scoped reader sees none of the real rows", janstats._snapshot_load() == {})
        janstats._snapshot_store({"0": [9]})
        check("...and its own writes", janstats._snapshot_load() == {"0": [9]})
    finally:
        janstats._SNAPSHOT_SCOPE[0] = keep
    check("the real rows are untouched by it",
          janstats._snapshot_load() == {"0": [1, 3, 2], "4": [7]})
    rc = janstats.selftest()
    check("the module's own self-test passes on this database", rc == 0)
    check("...and leaves the real rows as they were",
          janstats._snapshot_load() == {"0": [1, 3, 2], "4": [7]}
          and all(not r["category"].startswith("selftest-janstats")
                  for r in janstore.db.query("SELECT category FROM jan_rank_snapshot")))


def boards(polboards, janstore):
    print("the board's Discord bookkeeping (jan_board_state)")
    args = polboards.build_parser().parse_args(["--jan-port", "1"])
    path = polboards.state_path(args, "jan_live")
    check("with a database, a feed's message ids are a jan_board_state row",
          path == "db:jan_live_discord", path)
    hook = "https://discord.com/api/webhooks/8/SECRET"
    d = polboards.Discord("jan", hook, polboards.state_path(args, "jan"))
    d.msg_id = "4321"
    d._save()
    again = polboards.Discord("jan", hook, polboards.state_path(args, "jan"))
    check("a restarted board edits the same message", again.msg_id == "4321")
    row = janstore.db.query_one("SELECT data FROM jan_board_state WHERE name = 'jan_discord'")
    check("...kept as a row, the webhook as a hash, never the URL",
          row and "SECRET" not in str(row["data"]), row)
    polboards._note_channel("chosen", "jan", "777", guild="g2")
    check("the bot's channels are the discord_channels row",
          polboards.bot_channels()["chosen"] == {"jan": {"g2": "777"}})


CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
what = sys.argv[2]
if what == "seats":
    import janseats
    janseats.reserve(1, 8, "PS2Tester")
elif what == "rules":
    import janrules
    janrules.store(3, {"GALLEY_LIMIT": 1}, by=8)
elif what == "roomkey":
    import janlobby
    janlobby._KEYMAP["123456"] = "#MJS0R001"
    janlobby._KEYMAP_LOADED[0] = True
    janlobby._keymap_save()
print("child done")
"""


def cross_process(url):
    print("live state across processes (Valkey)")
    from polcore import kv
    prefix = "jantest:%s:" % os.path.basename(TMP)
    env = dict(os.environ, POL_VALKEY_URL=url, POL_KV_PREFIX=prefix,
               PYTHONPATH=os.pathsep.join(p for p in (SERVICES, CORE or "") if p))
    for k in ("POL_JAN_SEATS_KEY", "POL_JAN_RULES_KEY", "POL_JAN_ROOMKEY_KEY"):
        env.pop(k, None)
    for what in ("seats", "rules", "roomkey"):
        r = subprocess.run([sys.executable, "-c", CHILD, SERVICES, what], env=env,
                           capture_output=True, text=True, timeout=120)
        check("a separate process publishes the %s" % what,
              r.returncode == 0 and "child done" in r.stdout, (r.stdout[-400:], r.stderr[-800:]))
    os.environ.update(POL_VALKEY_URL=url, POL_KV_PREFIX=prefix)
    kv.reset()
    try:
        check("this process is on Valkey", kv.default().backend == "valkey")
        import janseats
        import janrules
        import janlobby
        janseats._OWNER[0] = False
        janseats._SHARED.forget()
        check("a reader process sees the seat another process reserved",
              janseats.seat_count(1) == 1 and janseats.table_of(8) == 1)
        janrules._OWNER[0] = False
        janrules._SHARED.forget()
        check("...and the rules another process stored for a table",
              janrules.values_for(3)["GALLEY_LIMIT"] == 1 and janrules.has_rules(3))
        janlobby._KEYMAP.clear()
        janlobby._KEYMAP_LOADED[0] = False
        check("a room key one process learned is known to the next",
              janlobby.learned_rooms().get("123456") == "#MJS0R001", janlobby.learned_rooms())
        kv.default().flush()
    finally:
        for k in ("POL_VALKEY_URL", "POL_KV_PREFIX"):
            os.environ.pop(k, None)
        kv.reset()


def main():
    if janpg.fresh_database() is None:
        return janpg.skip_or_fail("jan_store")
    import janstore
    print("database: %s" % janstore.where())
    migrations(janstore)
    import janevent
    event(janevent, janstore)
    import janstats
    snapshot(janstats, janstore)
    import polboards
    boards(polboards, janstore)
    url = janpg.valkey_url()
    if url is None:
        if janpg.skip_or_fail("jan_store", "Valkey server"):
            FAILS.append("no Valkey")
    else:
        cross_process(url)
    print()
    if FAILS:
        print("FAILED: %d check(s)" % len(FAILS))
        return 1
    print("all store checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
