"""Where the core lobby's modules are, for the suites that need them.

The Janhourou title runs inside the OpenLobby core (services/titles.py
there), so a suite that exercises the seam imports both trees. The core is
found, in order: `OPENLOBBY_SERVICES` in the environment; a sibling checkout
(`../openlobby/services` next to this repository); `/app` (the container
image, where both trees are installed side by side).

    import jan_testenv; jan_testenv.setup()

puts this repository's `services/` and the core's on `sys.path` and names the
title in `POL_TITLES`, so `import responders` loads it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.normpath(os.path.join(HERE, os.pardir, "services"))


def core_path():
    cands = [os.environ.get("OPENLOBBY_SERVICES", ""),
             os.path.normpath(os.path.join(HERE, os.pardir, os.pardir,
                                           "openlobby", "services")),
             "/app"]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "titles.py")):
            return c
    return None


def setup(need_core=True):
    # services/ FIRST, ahead of this tools/ directory: tools/janptl.py and
    # friends are command-line tools beside the modules they inspect.
    while SERVICES in sys.path:
        sys.path.remove(SERVICES)
    sys.path.insert(0, SERVICES)
    core = core_path()
    if core is None:
        if need_core:
            raise SystemExit("[SKIP] the OpenLobby core is not beside this tree "
                             "(set OPENLOBBY_SERVICES)")
        return None
    if core not in sys.path:
        sys.path.insert(1, core)
    os.environ.setdefault("POL_TITLES", "jantitle")
    return core
