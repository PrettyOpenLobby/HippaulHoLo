#!/usr/bin/env python3
"""Throwaway PostgreSQL databases and Valkey servers for the self-tests, from
OpenLobby's tools/pgtest.py.

    import janpg
    url = janpg.fresh_database()    # sets POL_DATABASE_URL; None = no server
    vk = janpg.valkey_url()         # a Valkey for a cross-process test, or None

pgtest.py starts a throwaway postgres container on a free loopback port (or
uses POL_TEST_DATABASE_URL's server) and hands out one empty database per
call, and removes what it started when the test process exits. It is found
beside the OpenLobby core jan_testenv.py finds (OPENLOBBY_SERVICES, else the
checkout beside this repository).

When jan_run_all.py runs a suite it has already made that suite a database and
says so with JAN_TEST_DATABASE=1; the suite then uses POL_DATABASE_URL as
given. Otherwise a suite always makes its own and never trusts a
POL_DATABASE_URL it finds in the environment, which could be a real stack's.

With no Docker and no POL_TEST_DATABASE_URL there is no server:
fresh_database returns None and the suite reports SKIP, unless
POL_TEST_REQUIRE_DB=1 (CI), which makes that a failure. The same convention as
OpenLobby's polcore suites. POL_TEST_VALKEY_URL names a Valkey server the same
way.
"""
import atexit
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import jan_testenv  # noqa: E402

_CORE = jan_testenv.core_path()
if _CORE:
    for _p in (os.path.normpath(os.path.join(_CORE, os.pardir, "tools")), _CORE):
        if _p not in sys.path:
            sys.path.append(_p)

try:
    import pgtest  # noqa: E402
except ImportError:                                          # no core beside us
    pgtest = None


def require_db():
    return os.environ.get("POL_TEST_REQUIRE_DB", "") == "1"


def server_available():
    """True when pgtest can reach or start a server."""
    if pgtest is None:
        print("[janpg] OpenLobby's tools/pgtest.py was not found", file=sys.stderr)
        return False
    try:
        pgtest.server_url()
        return True
    except Exception as exc:                                 # noqa: BLE001
        print("[janpg] no test database server: %s" % exc, file=sys.stderr)
        return False


def fresh_database():
    """POL_DATABASE_URL for this process: the runner's, or a new empty
    database dropped at exit. None when no server is available."""
    if os.environ.get("JAN_TEST_DATABASE") == "1" and \
            os.environ.get("POL_DATABASE_URL"):
        return os.environ["POL_DATABASE_URL"]
    if not server_available():
        os.environ.pop("POL_DATABASE_URL", None)
        return None
    url = pgtest.create_database()
    atexit.register(_drop, url)
    os.environ["POL_DATABASE_URL"] = url
    return url


def _drop(url):
    try:
        from polcore import db
        db.close()
    except Exception:                                        # noqa: BLE001
        pass
    try:
        pgtest.drop_database(url)
    except Exception:                                        # noqa: BLE001
        pass


def valkey_url():
    """A Valkey server a test may write keys to: POL_TEST_VALKEY_URL, else a
    throwaway valkey/valkey:8-alpine container (removed at exit). None when
    neither is possible."""
    url = os.environ.get("POL_TEST_VALKEY_URL", "").strip()
    if not url:
        try:
            import valkey  # noqa: F401
        except ImportError:
            print("[janpg] the valkey package is not installed", file=sys.stderr)
            return None
        if pgtest is None or not pgtest.docker_available():
            print("[janpg] no Docker daemon and POL_TEST_VALKEY_URL is unset",
                  file=sys.stderr)
            return None
        _cid, port = pgtest.container("valkey/valkey:8-alpine", 6379,
                                      cmd=["valkey-server", "--save", "",
                                           "--appendonly", "no"])
        url = "valkey://127.0.0.1:%d/0" % port
    from polcore import kv
    probe = kv.ValkeyKV(url, prefix="janpg:")
    deadline = time.monotonic() + 30
    while True:
        try:
            probe.ping()
            return url
        except Exception:                                    # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def pol_accounts(spec):
    """Members 1..max(spec) in the fresh database, made with OpenLobby's own
    accounts functions. `spec` is {member id: [(handle name, primary,
    {handle_profile field: value}), ...]}; a member id with no entry gets no
    handle. Member ids come from the database's own sequence, so this needs
    an empty database, and says so if the ids come out different."""
    import accounts
    # a sealing key in the environment, so add_member never writes a key file
    os.environ.setdefault("POL_LOGIN_PW_KEY", "jan-selftest")
    conn = accounts.connect()
    try:
        for want in range(1, max(spec) + 1):
            polid = "JANTEST%02d" % want
            accounts.create_polid(conn, polid, "Passw0rdTest")
            mid = accounts.add_member(conn, polid, polid, "Passw0rdTest")
            if mid != want:
                raise RuntimeError("fixture member %d came out as %d -- the "
                                   "database was not empty" % (want, mid))
            for name, primary, profile in spec.get(want, ()):
                hid = accounts.set_handle(conn, mid, name, primary=primary)
                if profile:
                    accounts.set_handle_profile(conn, hid, profile)
    finally:
        conn.close()


def accounts_fingerprint():
    """Every row of the tables the board reads, for a read-only check."""
    import accounts
    conn = accounts.connect()
    try:
        return [list(map(tuple, conn.execute(
                    "SELECT * FROM %s ORDER BY 1, 2" % t).fetchall()))
                for t in ("member", "handle", "handle_profile")]
    finally:
        conn.close()


def skip_or_fail(suite, what="PostgreSQL server"):
    """What a suite returns when there is no server: 0 (SKIP), or 1 when
    POL_TEST_REQUIRE_DB=1."""
    if require_db():
        print("[%s] FAIL: no %s and POL_TEST_REQUIRE_DB=1" % (suite, what))
        return 1
    print("[%s] SKIP: no %s (set POL_TEST_DATABASE_URL or run Docker)" % (suite, what))
    return 0
