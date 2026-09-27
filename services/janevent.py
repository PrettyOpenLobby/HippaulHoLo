"""The Janhourou EVENT record: one running event at a time, shared by two
processes.

WHAT AN EVENT IS IN THIS BUILD, and it is much less than the name suggests.
Measured 2026-09-21 against the 2004 module (base 0x00280000):

  * The whole feature is ONE RANKING TAB. `RankingMain`'s fifth tab is
    "Event Ranking"; there is no entry screen, no roster window, no prize
    dialog and no event lobby. The 2002 build had the same tab, always on and
    called "Weekly Event Ranking"; 2004 renamed it and put it behind a flag.
  * Ten `Em*` opcodes exist in the name table (88..93, 97..100) and the client
    SENDS EXACTLY ONE of them: 92 EmISEVENT, asking whether an event is on. It
    receives 93 EmISEVENTACK. Every other one -- EmENTRYEVENT,
    EmEVENTOPENCLOSE, EmREPORTRANKINGID, EmEVENTPARTICIPANTADD(/ACK),
    EmGETEVENTPARTICIPANT(/ACK) -- is dead in this build: nothing sends or
    receives it, established by walking all 35 senders through 0x00392430 and
    all 17 receivers through 0x00392490.
  * So THE CLIENT CANNOT JOIN AN EVENT, cannot read a roster and cannot open
    or close one. Entry has to be arranged out of band. What the server
    controls is a single byte.
  * An event does not change the game: no special rules, no restricted table,
    no entry fee, no prize path. Outside the ranking screen there is no event
    string, opcode or field anywhere in the module.
  * The event ranking is the same 232-byte row as every other category, with
    the "games" column hidden (header 0x0049f130 is the empty string) and the
    winnings column reading profile value v21.

So running an event is: decide the window, tell the client the flag is 1
inside it, and count what players win while it is open.

    POL_JAN_EVENT       path to the record (default <resources>/janevent.json)
    POL_JAN_EVENT_ID    override the id    POL_JAN_EVENT_NAME  override the name
    POL_JAN_EVENT_OPEN / POL_JAN_EVENT_CLOSE  unix seconds, override the window
    POL_JAN_EVENT_FORCE 1 = always running, 0 = never (default: the window)
"""
import io
import json
import os
import time

#: The name the client is told, at most 63 bytes of cp932 plus a NUL: the ack
#: carries 64. NOTHING IN THIS BUILD DRAWS IT -- the gate at 0x003c5f30 copies
#: it to RankingMain+27640 and no site reads that back, and the object's own
#: allocation (0x00326d24, 27704 bytes) ends right after it. It is sent because
#: it is what the field is for, not because it has been seen on screen.
NAME_LEN = 64


def event_file():
    try:
        import janstats
        root = janstats.stats_dir()
    except Exception:                                       # pragma: no cover
        root = os.environ.get("POL_RESOURCE_DIR") or os.path.join(
            os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return os.environ.get("POL_JAN_EVENT", os.path.join(root, "janevent.json"))


def record():
    """The stored event, or {}. Never raises: a missing or broken file simply
    means no event, which is the safe answer."""
    try:
        with io.open(event_file(), encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def store(event_id, name="", opens_at=None, closes_at=None, auto=False):
    """Write the event record. `opens_at`/`closes_at` are unix seconds; None
    for `closes_at` means it runs until it is replaced.

    `auto` marks one the SCHEDULER opened. The scheduler only ever closes its
    own: an event a person opened by hand outlives the next tick, which it
    would not if the rule were simply "close anything outside the window" --
    and that rule would have quietly ended a hand-opened test within the hour.
    """
    rec = {"id": int(event_id), "name": name or "",
           "opens_at": None if opens_at is None else int(opens_at),
           "closes_at": None if closes_at is None else int(closes_at),
           "auto": bool(auto)}
    path = event_file()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
    return rec


def _window(rec, now):
    o, c = rec.get("opens_at"), rec.get("closes_at")
    if o is not None and now < int(o):
        return False
    if c is not None and now >= int(c):
        return False
    return True


def current(now=None):
    """The event that is RUNNING right now, or None.

    The environment wins over the file, so a server owner can open one without
    touching the store -- the same shape every other knob in this service has.
    """
    force = os.environ.get("POL_JAN_EVENT_FORCE")
    now = time.time() if now is None else float(now)
    rec = dict(record())
    for key, env, cast in (("id", "POL_JAN_EVENT_ID", int),
                           ("name", "POL_JAN_EVENT_NAME", str),
                           ("opens_at", "POL_JAN_EVENT_OPEN", int),
                           ("closes_at", "POL_JAN_EVENT_CLOSE", int)):
        v = os.environ.get(env)
        if v not in (None, ""):
            try:
                rec[key] = cast(v)
            except ValueError:
                pass
    if force == "0":
        return None
    if force == "1":
        rec.setdefault("id", 1)
        return rec
    if not rec.get("id"):
        return None
    return rec if _window(rec, now) else None


def is_running(now=None):
    return current(now) is not None


def event_id(now=None):
    """The running event's id, or 0. This is what keys a member's event
    window in `janstats`, so that a new event starts everyone at zero."""
    cur = current(now)
    return int(cur.get("id") or 0) if cur else 0


def name_bytes(now=None):
    cur = current(now)
    if not cur:
        return b""
    return (cur.get("name") or "").encode("cp932", "replace")[:NAME_LEN - 1]


# --- THE AUTOMATIC SCHEDULE ---------------------------------------------------
#
# An event that never ends is just the Weekly tab under another name, so events
# run in windows. WHICH windows is the shared event calendar's business
# (`eventcal`, the "jan" list, which Tetra Master's tournaments use too): the
# holidays -- Shogatsu, Setsubun, Golden Week, Mahjong Day, Tsukimi, Year-End
# -- and the Weekend Cup, Friday 00:00 UTC for 72 hours, in the weeks between.
# The calendar hands back one event at a time, a holiday before the weekend.
#
# A scheduled job runs `janevent.py --rotate` hourly (a cron entry or a
# systemd timer that execs it where the resource directory is writable).
# `auto_rotate` is idempotent -- it opens
# the window it is inside, closes one it has left, and does nothing the rest of
# the time. A missed run costs at most an hour of the window, never a duplicate
# event. The ids are still this module's own counter: the calendar says WHEN,
# and a window this store has not seen before is a new id, so `janstats` starts
# everyone at zero for it.
#
#   POL_JAN_EVENT_AUTO   0 = leave the record alone (default 1)


def _calendar(now):
    """The calendar's event for `now`, or None. Without the shared `eventcal`
    module (it ships with the core that has it) there is no schedule, and
    events are opened and closed by hand."""
    try:
        import eventcal
    except ImportError:
        return None
    return eventcal.current("jan", now)


def scheduled_window(now=None):
    """`(opens_at, closes_at, name)` of the scheduled event `now` falls in, or
    None when the calendar says no event runs at this moment."""
    now = time.time() if now is None else float(now)
    ev = _calendar(now)
    if not ev:
        return None
    return int(ev["start"]), int(ev["end"]), ev.get("name") or ""


def auto_rotate(now=None, log=None):
    """Open or close the scheduled event. Returns what it did, as a string.

    Idempotent: called again inside the same window it does nothing, and it
    never opens a window that has already closed.
    """
    now = time.time() if now is None else float(now)
    say = log or (lambda m: None)
    if os.environ.get("POL_JAN_EVENT_AUTO", "1") != "1":
        return "auto off"
    rec = record()
    win = scheduled_window(now)
    if win is None:
        if rec.get("id") and _window(rec, now):
            if not rec.get("auto"):
                return "hand-opened, left alone"
            store(rec["id"], rec.get("name", ""), rec.get("opens_at"), int(now),
                  auto=True)
            say("closed event %s (%s)" % (rec["id"], rec.get("name") or "unnamed"))
            return "closed"
        return "nothing to do"
    opens, closes, name = win
    if rec.get("opens_at") == opens and rec.get("id"):
        return "already open"            # the same window: leave it alone
    if rec.get("id") and not rec.get("auto") and _window(rec, now):
        return "hand-opened, left alone"     # a person's event outranks the clock
    eid = int(rec.get("id") or 0) + 1
    store(eid, name, opens, closes, auto=True)
    say("opened event %s (%s) until %s"
        % (eid, name, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(closes))))
    return "opened"


def selftest():
    ok = True

    def check(c, what):
        nonlocal ok
        print(("ok   " if c else "FAIL ") + what)
        ok &= bool(c)

    keep = {k: os.environ.get(k) for k in
            ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
             "POL_JAN_EVENT_OPEN", "POL_JAN_EVENT_CLOSE")}
    for k in keep:
        os.environ.pop(k, None)
    try:
        os.environ["POL_JAN_EVENT_FORCE"] = "0"
        check(current() is None and event_id() == 0,
              "FORCE=0 is never running, whatever is stored")
        os.environ["POL_JAN_EVENT_FORCE"] = "1"
        os.environ["POL_JAN_EVENT_ID"] = "7"
        os.environ["POL_JAN_EVENT_NAME"] = "Cup"
        check(is_running() and event_id() == 7 and name_bytes() == b"Cup",
              "FORCE=1 runs, and the environment supplies id and name")
        del os.environ["POL_JAN_EVENT_FORCE"]
        os.environ["POL_JAN_EVENT_OPEN"] = "1000"
        os.environ["POL_JAN_EVENT_CLOSE"] = "2000"
        check(current(now=1500) is not None, "inside the window it runs")
        check(current(now=999) is None and current(now=2000) is None,
              "before it opens and from the moment it closes, it does not")
        check(event_id(now=999) == 0,
              "...and a closed event keys nothing, so a member's window resets")
        os.environ["POL_JAN_EVENT_NAME"] = "x" * 200
        check(len(name_bytes(now=1500)) == NAME_LEN - 1,
              "the name is truncated to 63 bytes, leaving room for the NUL")
        del os.environ["POL_JAN_EVENT_ID"]
        check(current(now=1500) is None,
              "no id, no event -- an unset store is not a running event")

        # --- the automatic schedule, against a real calendar ---------------
        for k in ("POL_JAN_EVENT_OPEN", "POL_JAN_EVENT_CLOSE",
                  "POL_JAN_EVENT_NAME"):
            os.environ.pop(k, None)
        import tempfile
        global _calendar
        _keep_res = os.environ.get("POL_RESOURCE_DIR")
        _keep_cal = _calendar
        os.environ["POL_RESOURCE_DIR"] = tempfile.mkdtemp()
        # A stand-in for eventcal, so this tests the rotation and not the
        # calendar's data: the weekend rule, plus one holiday that starts on a
        # Saturday a month on and so lands inside that weekend's window.
        HOL = (1790337600 - 12 * 3600 + 28 * 86400 + 86400,
               1790337600 - 12 * 3600 + 28 * 86400 + 4 * 86400)

        def fake(now):
            if HOL[0] <= now < HOL[1]:
                return {"key": "hol", "id": 0, "name": "Holiday Cup",
                        "start": HOL[0], "end": HOL[1]}
            t = time.gmtime(now)
            opens = (now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
                     - ((t.tm_wday - 4) % 7) * 86400)
            if now >= opens + 72 * 3600:
                return None
            return {"key": "weekend", "id": 0, "name": "Weekend Cup",
                    "start": opens, "end": opens + 72 * 3600}
        _calendar = fake
        try:
            FRI, SUN, TUE = 1790337600, 1790463600, 1790596800
            w = scheduled_window(FRI)
            check(w and time.gmtime(w[0]).tm_wday == 4
                  and time.gmtime(w[0]).tm_hour == 0,
                  "the window opens on Friday at 00:00 UTC")
            check(w and w[1] - w[0] == 72 * 3600, "...and runs 72 hours")
            check(scheduled_window(SUN)[0] == w[0],
                  "Sunday night is still inside the same window")
            check(scheduled_window(TUE) is None, "Tuesday is not in any window")
            check(auto_rotate(FRI) == "opened" and is_running(FRI),
                  "rotating inside the window opens it")
            _id = record()["id"]
            check(auto_rotate(SUN) == "already open" and record()["id"] == _id,
                  "rotating again in the SAME window changes nothing")
            check(not is_running(TUE) and auto_rotate(TUE) == "nothing to do",
                  "after its own close time it is already over, so there is "
                  "nothing to close")
            # ...and one a PERSON opened is never closed by the clock
            store(99, "by hand", opens_at=FRI - 86400, closes_at=None)
            check(is_running(TUE), "an open-ended event runs until something "
                                   "closes it")
            check(auto_rotate(TUE) == "hand-opened, left alone"
                  and is_running(TUE),
                  "the scheduler leaves a HAND-opened event alone, outside the "
                  "window and inside it")
            check(auto_rotate(FRI + 7 * 86400) == "hand-opened, left alone",
                  "...even when its own window comes round")
            # the scheduler does close what the scheduler opened
            store(99, "by clock", opens_at=FRI - 86400, closes_at=None, auto=True)
            check(auto_rotate(TUE) == "closed" and not is_running(TUE),
                  "an event the SCHEDULER opened it does close")
            check(auto_rotate(FRI + 7 * 86400) == "opened"
                  and record()["id"] == 100,
                  "the next week is a NEW event, so winnings start at zero")
            check(record().get("auto") is True,
                  "...and it is marked as the scheduler's own")
            os.environ["POL_JAN_EVENT_AUTO"] = "0"
            check(auto_rotate(FRI + 14 * 86400) == "auto off",
                  "AUTO=0 leaves the record alone")
            os.environ.pop("POL_JAN_EVENT_AUTO", None)

            # --- a holiday from the calendar preempts a running weekend -----
            WK = FRI + 28 * 86400                    # that weekend's Friday
            check(auto_rotate(WK) == "opened"
                  and record()["name"] == "Weekend Cup",
                  "the weekend before the holiday opens as usual")
            wk_id = record()["id"]
            check(auto_rotate(HOL[0] + 3600) == "opened"
                  and record()["name"] == "Holiday Cup"
                  and record()["id"] == wk_id + 1,
                  "the holiday REPLACES it mid-window, as a new id")
            check(current(HOL[0] + 3600)["closes_at"] == HOL[1],
                  "...and runs to the calendar's end, not the weekend's")
            check(auto_rotate(HOL[1] - 3600) == "already open",
                  "the holiday is left alone for the rest of its window")
            check(auto_rotate(HOL[1] + 86400) == "nothing to do"
                  and not is_running(HOL[1] + 86400),
                  "after it the tab goes dark until the calendar opens one")
        finally:
            _calendar = _keep_cal
            if _keep_res is None:
                os.environ.pop("POL_RESOURCE_DIR", None)
            else:
                os.environ["POL_RESOURCE_DIR"] = _keep_res
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("janevent selftest: %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Open, close or show Janhourou's event.",
        epilog="The client cannot join an event -- it has no entry screen and "
               "sends no entry message -- so who takes part is arranged out of "
               "band. What this controls is the flag the ranking screen asks "
               "for, and the window that per-member winnings are counted in.")
    ap.add_argument("--open", metavar="NAME",
                    help="start an event with this name")
    ap.add_argument("--id", type=int, help="its id (default: the next one)")
    ap.add_argument("--days", type=float,
                    help="close it this many days from now")
    ap.add_argument("--close", action="store_true",
                    help="close the running event now")
    ap.add_argument("--show", action="store_true", help="print the record")
    ap.add_argument("--rotate", action="store_true",
                    help="open or close the scheduled event (what the timer runs)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(0 if selftest() else 1)
    if a.rotate:
        print(auto_rotate(log=print))
        return
    if a.close:
        rec = record()
        if not rec.get("id"):
            print("no event stored")
            return
        store(rec["id"], rec.get("name", ""), rec.get("opens_at"), time.time())
        print("closed event %s (%s)" % (rec["id"], rec.get("name") or "unnamed"))
        return
    if a.open:
        now = int(time.time())
        eid = a.id if a.id is not None else int(record().get("id") or 0) + 1
        closes = now + int(a.days * 86400) if a.days else None
        rec = store(eid, a.open, now, closes)
        print("event %s open: %r" % (rec["id"], rec["name"]))
        print("  from %s" % time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)))
        print("  until %s" % (time.strftime("%Y-%m-%d %H:%M UTC",
                                            time.gmtime(closes))
                              if closes else "closed by hand"))
        print("  file %s" % event_file())
        return
    cur = current()
    print("stored: %r" % (record() or None))
    print("running now: %s" % ("yes, id %s" % cur["id"] if cur else "no"))
    print("file: %s" % event_file())


if __name__ == "__main__":
    main()
