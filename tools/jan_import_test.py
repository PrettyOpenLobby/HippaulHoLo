"""jan_import_test.py -- `python janstore.py import event|rank_snapshot|
board_state`: Janhourou's old files into PostgreSQL.

    python tools/jan_import_test.py

The files are written by the code that wrote them, taken from git
(OLD_COMMIT, the last commit that kept them as files) and run from a
temporary directory: janevent.store() writes janevent.json,
janstats._snapshot_store() writes jan-rank-snapshot.json, and the web
board's Discord class and _note_channel() write jan_discord.json,
jan_live_discord.json and discord_channels.json. Entries no version could
map are added by hand.

Checked on a fresh database: the rows and their values, the server's own
readers (janevent.record(), janstats._snapshot_load(), polboards) reading
them back, a second run that changes nothing, --dry-run, the refusal on a
table that already holds rows, --merge, and sources that are byte for byte
what they were.

Needs the OpenLobby core beside this tree and a PostgreSQL server (Docker,
or POL_TEST_DATABASE_URL); SKIPs without one, or FAILs under
POL_TEST_REQUIRE_DB=1.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
SERVICES = os.path.join(ROOT, "services")
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402

CORE = jan_testenv.setup(need_core=False)
import janpg                                                       # noqa: E402

#: The last commit whose event record, rank snapshot and board state were
#: files (the `game-split` branch).
OLD_COMMIT = "1434bb4e94bc3a98361cfe5a6bb39189a813db02"
OLD_FILES = ("services/janevent.py", "services/janstats.py",
             "services/polboards.py", "services/polgateway.py")

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           ("  --  %s" % (str(detail)[:600],)) if detail and not ok else ""),
          flush=True)
    if not ok:
        FAILS.append(label)


FIXTURE = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
res, state = sys.argv[2], sys.argv[3]
os.environ["POL_RESOURCE_DIR"] = res
os.environ["POL_JAN_EVENT"] = os.path.join(res, "janevent.json")
os.environ["POL_BOARDS_STATE_DIR"] = state
import janevent, janstats
janevent.store(7, name="Autumn cup", opens_at=1790000000, closes_at=None, auto=True)
janstats._snapshot_store({"0": [3, 5, 9], "1": [5, 3], "3": []})
import polboards as B
class Net:
    def __call__(self, req, timeout=None):
        class R:
            status = 200
            def read(self, *a): return b'{"id": "555"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
hook = "https://discord.com/api/webhooks/1/x"
for name in ("jan", "jan_live"):
    B.Discord(name, hook, os.path.join(state, name + "_discord.json"),
              opener=Net()).tick("s1", lambda: ({"content": "board"}, []), now=1000.0)
B._note_channel("chosen", "jan", "4242", guild="99")
B._note_channel("posted", "jan_live", "4243", guild="99")
'''


def old_files(base):
    probe = subprocess.run(["git", "cat-file", "-e", OLD_COMMIT + "^{commit}"],
                           cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        print("FAIL: this suite needs the file-based code at %s (a shallow clone "
              "lacks it: git fetch --unshallow)" % OLD_COMMIT)
        sys.exit(1)
    code = os.path.join(base, "old")
    os.makedirs(code)
    for f in OLD_FILES:
        src = subprocess.run(["git", "show", "%s:%s" % (OLD_COMMIT, f)], cwd=ROOT,
                             capture_output=True, check=True).stdout
        with open(os.path.join(code, os.path.basename(f)), "wb") as fh:
            fh.write(src)
    with open(os.path.join(base, "fixture.py"), "w", encoding="utf-8") as fh:
        fh.write(FIXTURE)
    res = os.path.join(base, "data", "resources")
    state = os.path.join(base, "state")
    os.makedirs(res)
    os.makedirs(state)
    env = {k: v for k, v in os.environ.items()
           if k not in ("POL_DATABASE_URL", "POL_VALKEY_URL")}
    p = subprocess.run([sys.executable, os.path.join(base, "fixture.py"), code, res,
                        state], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout + p.stderr)
        print("FAIL: the old code could not write its files")
        sys.exit(1)
    # what no version could map
    snap = os.path.join(res, "jan-rank-snapshot.json")
    with open(snap, encoding="utf-8") as fh:
        d = json.load(fh)
    d.update({"weekly": [1], "4": {"not": "a list"}})
    with open(snap, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    with open(os.path.join(state, "README.txt"), "w") as fh:
        fh.write("not state")
    with open(os.path.join(state, "tm_discord.json"), "w") as fh:
        fh.write("not json")
    return os.path.join(res, "janevent.json"), snap, state


def digest(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (hashlib.sha256(fh.read()).hexdigest(),
                                                 os.stat(p).st_mtime_ns)
    return out


def run(*args):
    p = subprocess.run([sys.executable, os.path.join(SERVICES, "janstore.py"), "import"]
                       + list(args), cwd=SERVICES, env=dict(os.environ),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def fingerprint(db):
    out = {}
    for t in ("jan_event", "jan_rank_snapshot", "jan_board_state"):
        if db.query_one("SELECT to_regclass(%s) IS NOT NULL AS ok", (t,))["ok"]:
            out[t] = sorted(repr(sorted((k, str(v)) for k, v in r.items()))
                            for r in db.query("SELECT * FROM %s" % t))
    return out


def main():
    if janpg.fresh_database() is None:
        return janpg.skip_or_fail("jan_import")
    base = tempfile.mkdtemp(prefix="jan-import-")
    try:
        _main(base)
    finally:
        try:
            from polcore import db
            db.close()
        except Exception:                                    # noqa: BLE001
            pass
        shutil.rmtree(base, ignore_errors=True)
    print()
    print("FAIL: %d check(s)" % len(FAILS) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


def _main(base):
    os.environ.pop("POL_VALKEY_URL", None)
    from polcore import db
    event, snap, state = old_files(base)
    before = digest(base)

    print("--dry-run: a report and nothing written, not even the tables")
    code, out = run("event", event, "--dry-run")
    check("exit 0", code == 0, out)
    check("says so", "Dry run: nothing was written." in out, out)
    check("plans the one record", "1 to insert" in out, out)
    check("no table was created", fingerprint(db) == {})

    print("import event")
    code, out = run("event", event)
    check("exit 0", code == 0, out)
    row = db.query_one("SELECT name, data FROM jan_event")
    check("the record is the 'current' row", row and row["name"] == "current"
          and row["data"] == {"id": 7, "name": "Autumn cup", "opens_at": 1790000000,
                              "closes_at": None, "auto": True}, row)
    import janevent
    janevent._RECORD_CACHE.update(row=None)
    check("janevent.record() reads it", janevent.record().get("name") == "Autumn cup")

    print("import rank_snapshot")
    code, out = run("rank_snapshot", snap)
    check("exit 0", code == 0, out)
    rows = {r["category"]: r["data"] for r in db.query(
        "SELECT category, data FROM jan_rank_snapshot")}
    check("one row per category", rows == {"0": [3, 5, 9], "1": [5, 3], "3": []}, rows)
    check("a category that is not a number, and one that is not a list, are skipped",
          "skipped category 'weekly'" in out and "skipped category '4'" in out, out)
    import janstats
    check("janstats._snapshot_load() reads it", janstats._snapshot_load()
          == {"0": [3, 5, 9], "1": [5, 3], "3": []})

    print("import board_state")
    code, out = run("board_state", state)
    check("exit 0", code == 0, out)
    rows = {r["name"]: r["data"] for r in db.query("SELECT name, data FROM jan_board_state")}
    check("a row per state file, named as the board names it",
          sorted(rows) == ["discord_channels", "jan_discord", "jan_live_discord"], sorted(rows))
    check("the message ids", rows.get("jan_discord", {}).get("message_id") == "555"
          and rows.get("jan_live_discord", {}).get("message_id") == "555", rows)
    check("the channels, per guild", rows.get("discord_channels")
          == {"chosen": {"jan": {"99": "4242"}}, "posted": {"jan_live": {"99": "4243"}}},
          rows.get("discord_channels"))
    check("a file that is not board state, and one that is not JSON, are skipped",
          "skipped README.txt" in out and "skipped tm_discord.json" in out, out)
    import polboards
    check("the board reads the imported message id",
          polboards.Discord("jan", "https://discord.com/api/webhooks/1/x",
                            "db:jan_discord").msg_id == "555")
    check("and the imported channels", polboards.bot_channels()["chosen"]
          == {"jan": {"99": "4242"}})
    code, out = run("board_state", os.path.join(state, "discord_channels.json"))
    check("one file at a time: already there", code == 0 and "Nothing to import" in out, out)

    print("a second run changes nothing")
    fp = fingerprint(db)
    for args in (("event", event), ("rank_snapshot", snap), ("board_state", state)):
        code, out = run(*args)
        check("%s: exit 0, nothing to import" % args[0],
              code == 0 and "Nothing to import" in out, out)
        code, out = run(*(args + ("--merge",)))
        check("%s --merge: nothing either" % args[0],
              code == 0 and "Nothing to import" in out, out)
    check("every row as it was", fingerprint(db) == fp)

    print("a table that already holds rows: refused, then --merge")
    snap2 = os.path.join(base, "snap2.json")
    with open(snap2, "w") as fh:
        json.dump({"0": [9, 5, 3], "2": [3]}, fh)
    code, out = run("rank_snapshot", snap2)
    check("refused: exit 2", code == 2, out)
    check("says why", "REFUSED: jan_rank_snapshot" in out and "--merge" in out, out)
    check("nothing written", fingerprint(db) == fp)
    code, out = run("rank_snapshot", snap2, "--merge", "--dry-run")
    check("--merge --dry-run writes nothing", code == 0 and fingerprint(db) == fp, out)
    code, out = run("rank_snapshot", snap2, "--merge")
    check("--merge: exit 0, one row", code == 0 and "Done: 1 row(s) written." in out, out)
    rows = {r["category"]: r["data"] for r in db.query(
        "SELECT category, data FROM jan_rank_snapshot")}
    check("the new category is in, the one in both keeps the table's order",
          rows.get("2") == [3] and rows.get("0") == [3, 5, 9], rows)
    check("and the report names it", "kept the table's row, the file's differs: ('0',)"
          in out, out)
    ev2 = os.path.join(base, "ev2.json")
    with open(ev2, "w") as fh:
        json.dump({"id": 8, "name": "Winter"}, fh)
    code, out = run("event", ev2)
    check("a second event record is not new: nothing written, the difference named",
          code == 0 and "the file's differs: ('current',)" in out
          and db.query_one("SELECT data FROM jan_event")["data"]["id"] == 7, out)

    print("bad sources")
    code, out = run("event", os.path.join(base, "missing.json"))
    check("a missing file: exit 1", code == 1 and "cannot read" in out, out)
    lst = os.path.join(base, "list.json")
    with open(lst, "w") as fh:
        fh.write("[1]")
    code, out = run("rank_snapshot", lst)
    check("a file that is not an object: exit 1", code == 1, out)
    for p in (snap2, ev2, lst):
        os.remove(p)
    check("the sources are byte for byte what they were", digest(base) == before,
          sorted(set(digest(base)) ^ set(before)))


if __name__ == "__main__":
    sys.exit(main())
