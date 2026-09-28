#!/usr/bin/env python3
"""Run every Janhourou selftest, and exit non-zero if any of them fails.

    python tools/jan_run_all.py              # everything
    python tools/jan_run_all.py -k lobby     # only suites whose name contains
    python tools/jan_run_all.py -v           # stream each suite's own output

The list is explicit, not globbed: a suite that is not registered here does
not exist. Suites marked `core` exercise the seam with the OpenLobby core and
need it beside this tree (see jan_testenv.py); they are skipped, loudly, when
it is not found. The last entries run the core's own suites WITH this title
loaded, so their content-3 sections stop skipping.

Every suite imports OpenLobby's polcore (janstore.py), so the core has to be
found for any of them. Suites listed in NEEDS_DB each get a fresh, empty
PostgreSQL database (janpg.py, over OpenLobby's tools/pgtest.py), dropped when
the suite ends; with no server they SKIP, or FAIL under POL_TEST_REQUIRE_DB=1.
No suite ever sees a POL_DATABASE_URL or POL_VALKEY_URL from the environment
it was started in: live state is each suite's own in-memory store unless the
suite starts a Valkey of its own.

POL_DATA_DIR, POL_RESOURCE_DIR, POL_LOG_DIR and POL_LOGIN_PW_KEYFILE that
are not set point into a temporary directory made for the run and removed
at the end (scratch_state).
"""
import argparse
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
SERVICES = os.path.join(ROOT, "services")
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402

CORE = jan_testenv.core_path()
CORE_TOOLS = os.path.join(CORE or "", os.pardir, "tools")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # suites print non-ASCII markers
PY = sys.executable

#: (name, argv, cwd, needs the core)
SUITES = [
    # --- the game itself: the wire, the rules, the deal, the table ----------
    ("janwire",       [PY, "janwire.py", "--selftest"],        SERVICES, False),
    ("janrules",      [PY, "janrules.py", "--selftest"],       SERVICES, False),
    ("janmahjong",    [PY, "janmahjong.py", "--selftest"],     SERVICES, False),
    ("janmsgs",       [PY, "janmsgs.py", "--selftest"],        SERVICES, False),
    ("janmsgs2004",   [PY, "janmsgs2004.py"],                  SERVICES, False),
    ("jangame",       [PY, "jangame.py", "--selftest"],        SERVICES, False),
    ("janhourou",     [PY, "janhourou.py", "--selftest"],      SERVICES, False),
    ("janseats",      [PY, "janseats.py", "--selftest"],       SERVICES, False),
    ("janstats",      [PY, "janstats.py", "--selftest"],       SERVICES, False),
    ("janevent",      [PY, "janevent.py", "--selftest"],       SERVICES, False),
    ("jansave",       [PY, "jansave.py", "--selftest"],        SERVICES, False),
    # the lobby lists (parlour list, room list, PTL, the table record)
    ("janlobby",      [PY, "janlobby.py", "--selftest"],       SERVICES, False),
    ("jan_watch",     [PY, "jan_watch_test.py"],               HERE,     False),
    ("jan_board",     [PY, "jan_board_test.py"],               HERE,     False),
    # jangame.py / janhourou.py are facades over the jantable / janworld
    # packages: every name the tree rebinds through them must reach its owner
    ("facade_rebind", [PY, "facade_rebind_check.py"],          HERE,     False),
    # the opt-in switches (tests/): entry limits need only this tree; the
    # channel ids need the core's mgkey
    ("jan_reserve_limits", [PY, os.path.join(TESTS, "test_jan_reserve_limits.py")],
                      TESTS,    False),
    ("jan_lndv_channels", [PY, os.path.join(TESTS, "test_jan_lndv_channels.py")],
                      TESTS,    True),
    # --- the seam with the core -------------------------------------------
    ("jan_title",     [PY, "jan_title_test.py"],               HERE,     True),
    ("jan_delta_e2e", [PY, "jan_delta_e2e.py"],                HERE,     True),
    # --- the state outside the process: PostgreSQL tables and Valkey keys ----
    ("jan_store",     [PY, "jan_store_test.py"],               HERE,     True),
    # the files an earlier release kept, imported into those tables
    ("jan_import",    [PY, "jan_import_test.py"],              HERE,     True),
    # the core's own suites, with this title loaded
    ("core_resource", [PY, os.path.join(CORE_TOOLS, "resource_test.py")],
                      CORE_TOOLS, True),
    ("core_content_profile", [PY, os.path.join(CORE_TOOLS, "content_profile_test.py")],
                      CORE_TOOLS, True),
    ("core_pfc_profile", [PY, os.path.join(CORE_TOOLS, "pfc_profile_test.py")],
                      CORE_TOOLS, True),
    ("core_titlezone", [PY, os.path.join(CORE_TOOLS, "titlezone_test.py")],
                      CORE_TOOLS, True),
]


#: suites that get a fresh PostgreSQL database of their own (see the docstring):
#: the ones that keep Janhourou's durable state (the player records, the event
#: record, the rank snapshot, the board's bookkeeping), and the core's suites
#: whose accounts and resources live in PostgreSQL too
NEEDS_DB = {"jangame", "janevent", "janstats", "jansave", "jan_board",
            "jan_reserve_limits", "jan_store", "jan_import", "core_resource",
            "core_content_profile"}


def _fresh_database():
    """(url, drop) for a new empty database, or (None, why)."""
    try:
        import janpg
        if not janpg.server_available():
            return None, "no PostgreSQL server (Docker, or POL_TEST_DATABASE_URL)"
        url = janpg.pgtest.create_database()
        return url, lambda: janpg.pgtest.drop_database(url)
    except Exception as exc:                                  # noqa: BLE001
        return None, "no test database (%s)" % exc


#: The state paths a suite falls back to when they are unset (see scratch_state).
SCRATCH_VARS = ("POL_DATA_DIR", "POL_RESOURCE_DIR", "POL_LOG_DIR",
                "POL_LOGIN_PW_KEYFILE")


def scratch_state():
    """Point every state path a suite may fall back to at a directory made
    for this run and removed when it ends, unless the caller set it.

    A suite that finds no POL_DATA_DIR uses /data, which on Windows is the
    root of the current drive, so a run could read and write a real server's
    files there. A value already set wins; POL_RESOURCE_DIR then follows
    POL_DATA_DIR, as the services derive it. Returns the directory made, or
    None when every variable was set.
    """
    missing = [k for k in SCRATCH_VARS if not os.environ.get(k, "").strip()]
    if not missing:
        return None
    root = tempfile.mkdtemp(prefix="jan-run-")
    atexit.register(shutil.rmtree, root, True)
    if "POL_DATA_DIR" in missing:
        os.environ["POL_DATA_DIR"] = os.path.join(root, "data")
        os.makedirs(os.environ["POL_DATA_DIR"])
    if "POL_RESOURCE_DIR" in missing:
        os.environ["POL_RESOURCE_DIR"] = os.path.join(os.environ["POL_DATA_DIR"],
                                                      "resources")
        if os.environ["POL_RESOURCE_DIR"].startswith(root):
            os.makedirs(os.environ["POL_RESOURCE_DIR"], exist_ok=True)
    if "POL_LOG_DIR" in missing:
        os.environ["POL_LOG_DIR"] = os.path.join(root, "logs")
        os.makedirs(os.environ["POL_LOG_DIR"])
    if "POL_LOGIN_PW_KEYFILE" in missing:
        os.makedirs(os.path.join(root, "keys"))
        os.environ["POL_LOGIN_PW_KEYFILE"] = os.path.join(root, "keys", "login-pw.key")
    return root


def main():
    scratch_state()
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", action="append", default=[])
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    todo = [s for s in SUITES if not args.k or any(k in s[0] for k in args.k)]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (SERVICES, CORE, env.get("PYTHONPATH", "")) if p])
    env["POL_TITLES"] = "jantitle"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    for k in ("POL_DATABASE_URL", "POL_VALKEY_URL", "JAN_TEST_DATABASE"):
        env.pop(k, None)
    failed, skipped = [], []
    print(f"running {len(todo)} suite(s); core: {CORE or 'NOT FOUND'}")
    for name, cmd, cwd, needs_core in todo:
        if needs_core and CORE is None:
            print("  %-22s ... SKIP  (no OpenLobby core beside this tree)" % name)
            skipped.append(name)
            continue
        suite_env, drop = env, None
        if name in NEEDS_DB:
            url, drop = _fresh_database()
            if url is None:
                if os.environ.get("POL_TEST_REQUIRE_DB") == "1":
                    print("  %-22s ... FAIL  (%s, POL_TEST_REQUIRE_DB=1)" % (name, drop))
                    failed.append(name)
                else:
                    print("  %-22s ... SKIP  (%s)" % (name, drop))
                    skipped.append(name)
                continue
            suite_env = dict(env, POL_DATABASE_URL=url, JAN_TEST_DATABASE="1")
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=cwd, env=suite_env, timeout=600,
                               capture_output=not args.v, text=True,
                               encoding="utf-8", errors="replace")
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok, r = False, None
        finally:
            if drop is not None:
                try:
                    drop()
                except Exception:                             # noqa: BLE001
                    pass
        print("  %-22s ... %s %6.1fs" % (name, "ok  " if ok else "FAIL",
                                         time.time() - t0), flush=True)
        if not ok:
            failed.append(name)
            if r is not None and not args.v:
                print("=" * 72)
                print((r.stdout or "")[-3000:])
                print((r.stderr or "")[-2000:])
                print("=" * 72)
    n = len(todo) - len(skipped)
    if failed:
        print(f"{n - len(failed)}/{n} suites passed, {len(failed)} FAILED: "
              f"{', '.join(failed)}")
        sys.exit(1)
    print(f"{n}/{n} suites passed" + (f" ({len(skipped)} skipped)" if skipped else ""))


if __name__ == "__main__":
    main()
