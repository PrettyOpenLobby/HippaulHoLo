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
"""
import argparse
import os
import subprocess
import sys
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
    # the opt-in switches (tests/): entry limits need only this tree; the
    # channel ids need the core's mgkey
    ("jan_reserve_limits", [PY, os.path.join(TESTS, "test_jan_reserve_limits.py")],
                      TESTS,    False),
    ("jan_lndv_channels", [PY, os.path.join(TESTS, "test_jan_lndv_channels.py")],
                      TESTS,    True),
    # --- the seam with the core -------------------------------------------
    ("jan_title",     [PY, "jan_title_test.py"],               HERE,     True),
    ("jan_delta_e2e", [PY, "jan_delta_e2e.py"],                HERE,     True),
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


def main():
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
    failed, skipped = [], []
    print(f"running {len(todo)} suite(s); core: {CORE or 'NOT FOUND'}")
    for name, cmd, cwd, needs_core in todo:
        if needs_core and CORE is None:
            print("  %-22s ... SKIP  (no OpenLobby core beside this tree)" % name)
            skipped.append(name)
            continue
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, timeout=600,
                               capture_output=not args.v, text=True,
                               encoding="utf-8", errors="replace")
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok, r = False, None
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
