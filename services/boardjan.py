#!/usr/bin/env python3
"""boardjan.py -- JongHoLow's (Janhourou's) RANKINGS as a live board
(polboards, 2026-09-12).

The page IS the game's Rankings screen: the static layer (parchment, heading
art, header bar, buttons) baked from the shipped data by
tools/jan_boardart_bake.py, and the rows drawn live with the game's own ROM
font, widths, kerning and edge, and the module's own format strings -- the
same rules the rank-art build proved against SE's manual screenshots.

The data is the same five lists the client shows (janstats.RANK_CATEGORIES)
from the SAME function the lobby serves them with, janstats.rank_list, so the
page and the game can never disagree about who is where.

TWO THINGS THIS MODULE MUST NEVER DO, both of which the obvious call does:

  * write the rank snapshot. `rank_list(..., remember=True)` (its default)
    rewrites jan-rank-snapshot.json, which is what the client's up/down/New
    glyph compares against. A page polled every few seconds would reset every
    player's movement to "no change". Always `remember=False`.
  * open accounts.db through `accounts.connect()`. That applies the schema --
    a WRITE lock even when every statement is a no-op -- and sets the journal
    mode on the file the login services are writing. Names are read through a
    plain read-only SQLite URI instead, the one query `primary_handle_row`
    runs. (The known WAL hazard is a Windows bind mount; prod is Linux.)
"""
import datetime
import hashlib
import io
import json
import os
import sqlite3
import struct
import threading
import time

import janstats

NAME = "jan"
TITLE = "JongHoLow - Rankings"
CATEGORIES = janstats.RANK_CATEGORIES
VALUE_FIELD = janstats.RANK_SORT_VALUE          # category -> vN of the sort value
PREV_FIELD = janstats.RANK_PREV_VALUE           # category -> vN of the previous rank
#: Jan Rating and Title score travel x1e6 (janstats.FLOAT_SCALE); the three
#: winnings columns are whole JAN.
FLOAT_CATEGORIES = frozenset((0, 1))
NEW = janstats.NO_RANK & 0xFF                   # the previous-rank byte for "never ranked"

#: NOT under services/fedata/: pol-git-sync restarts felobby AND feworld for
#: anything there (FE's data tables), so board art lives in its own tree.
ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "boardart", "jan")

_NAMES = {"t": 0.0, "map": {}}
_NAMES_LOCK = threading.Lock()
_SNAP = {"t": 0.0, "snap": None}
_SNAP_LOCK = threading.Lock()
_RENDER = {}
_RENDER_LOCK = threading.Lock()
_WARNED = set()


def accounts_path():
    """POL_ACCOUNTS_DB, else /data/accounts.db -- where prod's login/authsess
    keep it (their env says so). accounts.DEFAULT_DB's /config/accounts.db
    does not exist there: the first deploy's names were all blank."""
    return os.environ.get("POL_ACCOUNTS_DB", "/data/accounts.db")


def member_names(members, ttl=60.0):
    """{member: handle name}, read-only, cached for `ttl` seconds."""
    members = [int(m) for m in members]
    now = time.time()
    with _NAMES_LOCK:
        if now - _NAMES["t"] < ttl and all(m in _NAMES["map"] for m in members):
            return dict(_NAMES["map"])
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % accounts_path(), uri=True,
                               timeout=5)
        try:
            for m in members:
                row = conn.execute(
                    "SELECT handle_name FROM handle WHERE member_id = ?"
                    " ORDER BY is_primary DESC, id ASC LIMIT 1", (m,)).fetchone()
                out[m] = str(row[0]) if row and row[0] else ""
        finally:
            conn.close()
    except sqlite3.Error as e:
        if "names" not in _WARNED:
            _WARNED.add("names")
            print("[boardjan] cannot read names from %s (%s) -- rows show "
                  "without them" % (accounts_path(), e), flush=True)
        return {m: "" for m in members}
    with _NAMES_LOCK:
        _NAMES.update(t=now, map=out)
    return dict(out)


def move_of(prev, index):
    """The client's movement glyph from the previous 0-based rank: 'new' when
    the member was not on the last list, else up / down / same."""
    prev = int(prev) & 0xFF
    if prev == NEW:
        return "new"
    if prev > index:
        return "up"
    if prev < index:
        return "down"
    return "same"


def _lists(names):
    """{category: [(member, fields)]} -- janstats' own rows, remember=False."""
    return {c: janstats.rank_list(c, names=names, remember=False)
            for c in range(len(CATEGORIES))}


def category(cat, names, limit=janstats.RANK_LIST_MAX, rows=None):
    """One category, best first, as plain rows."""
    if rows is None:
        rows = janstats.rank_list(cat, limit=limit, names=names, remember=False)
    out = []
    b = board()
    for i, (m, f) in enumerate(rows[:limit]):
        # the ranked value: a float for the two rating columns (see
        # `row_strings` -- an int() here rounded 8.42 to 8 in the JSON the page
        # ranks and labels from), whole JAN for the three money ones
        raw = f.get(VALUE_FIELD[cat], 0) or 0
        raw = float(raw) if cat in FLOAT_CATEGORIES else int(raw)
        prev = int(f.get(PREV_FIELD[cat], NEW) or 0) & 0xFF
        # the page sets these as REAL text in the ROM web font
        # (tools/jan_boardfont_build.py): the game's own strings, not pixels
        cells = ({k: v.decode("cp932", "replace")
                  for k, v in row_strings(b, cat, i, f).items()} if b else None)
        out.append({
            "cells": cells,
            "rank": i + 1,
            "name": str(f.get(2) or ""),
            "value": (raw / float(janstats.FLOAT_SCALE)) if cat in FLOAT_CATEGORIES
            else raw,
            "games": int(f.get(17, 0) or 0),
            "level": int(f.get(32, 0) or 0),
            "rank_title": int(f.get(26, 0) or 0),
            "move": move_of(prev, i),
            "prev": None if prev == NEW else prev + 1,
        })
    return out


def _sig(lists):
    h = hashlib.sha1()
    for c in sorted(lists):
        for m, f in lists[c]:
            h.update(repr((c, m, sorted(f.items()))).encode())
    h.update(str(int(time.time() // 604800)).encode())
    return h.hexdigest()[:16]


#: the sidebar's "Recent games": the newest finished hanchan across everyone
RECENT_MAX = 12
#: authsess's Jan marker (services/live_sessions.py): {"count", "stamp"},
#: rewritten every few seconds while authserv runs. pol-git-sync trusts a
#: stamp for POL_DEPLOY_MATCH_GRACE_S (900 s) before a push may restart a
#: game; the board reads it the same way.
LIVE_MARKER = "authsess-jan-sessions-live.json"
LIVE_GRACE_S = float(os.environ.get("POL_DEPLOY_MATCH_GRACE_S", "900") or 900)


def recent_games(members, names, limit=RECENT_MAX):
    """[{name, place (0-based), result (points), t, table}], newest first,
    from each member's own history (janstats keeps the last 50)."""
    out = []
    for m in members:
        rec = janstats.load(m)
        for h in (rec.get("history") or [])[:limit]:
            try:
                out.append({"name": names.get(int(m), "") or str(rec.get("name") or ""),
                            "place": int(h.get("place", 0)),
                            "result": int(h.get("result_x10", 0)) / 10.0,
                            "t": int(h.get("t", 0)), "table": h.get("table")})
            except (TypeError, ValueError, AttributeError):
                continue
    out.sort(key=lambda g: -g["t"])
    return out[:limit]


def live_games(now=None):
    """How many hanchan authsess has in progress: the marker's count while
    its stamp is fresh, 0 once it is stale, None when there is no marker
    (the page then says nothing rather than a false 0)."""
    path = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), LIVE_MARKER)
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        stamp = float(d.get("stamp") or 0)
        count = int(d.get("count") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    now = time.time() if now is None else now
    return count if 0 <= now - stamp < LIVE_GRACE_S else 0


# ---------------------------------------------------------------------------
# WATCHING (rules decided 2026-09-12). Only the tables whose creator allowed
# watchers -- the game's own rule, GALLEY_LIMIT and no password, judged by
# jangame when it writes the file -- and only as they were WATCH_DELAY seconds
# ago: all four hands, like SE's in-game spectator (the chosen rule), but
# too late to relay to a player. jangame writes TABLES_FILE on every table
# event and at least every few seconds while a table is live; the board
# samples it once a second, keeps a short history per table, and never serves
# the live state. A table whose watching is switched off disappears at once,
# history and all; a stale file (jan down) means no tables.
# ---------------------------------------------------------------------------
TABLES_FILE = "jan-tables-live.json"
WATCH_DELAY = float(os.environ.get("POL_BOARDS_JAN_WATCH_DELAY", "30") or 30)
WATCH_STALE_S = 120.0           # no rewrite for this long = the writer is gone
WATCH_KEEP_S = 90.0             # history kept beyond the delay


class Watch:
    """The delayed view of jangame's live tables file. Thread-safe."""

    def __init__(self, path=None, delay=None, clock=time.time):
        self.path = path
        self.delay = WATCH_DELAY if delay is None else float(delay)
        self.clock = clock
        self.hist = {}          # table id -> [(t, state or None when it ended)]
        self.lock = threading.Lock()
        self.thread = None
        self._last_err = 0.0

    def file(self):
        return self.path or os.path.join(os.environ.get("POL_DATA_DIR", "/data"), TABLES_FILE)

    def _read(self, now):
        """{table id: entry} from the file now, or {} when it is missing or
        stale. An entry is {"watchable": bool, "state": {...}}."""
        try:
            with open(self.file(), encoding="utf-8") as fh:
                d = json.load(fh) or {}
            if not 0 <= now - float(d.get("stamp") or 0) < WATCH_STALE_S:
                return {}
            return {str(k): v for k, v in (d.get("tables") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def sample(self, now=None):
        now = self.clock() if now is None else now
        live = self._read(now)
        with self.lock:
            for tid, e in live.items():
                if not e.get("watchable") or not isinstance(e.get("state"), dict):
                    self.hist.pop(tid, None)        # watching switched off: gone NOW
                    continue
                h = self.hist.setdefault(tid, [])
                if not h or h[-1][1] != e["state"]:
                    h.append((now, e["state"]))
            for tid, h in list(self.hist.items()):
                if tid not in live and h and h[-1][1] is not None:
                    h.append((now, None))           # the game ended: show it, late
                # prune, but keep the newest entry at or before the served moment
                cut = now - self.delay - WATCH_KEEP_S
                while len(h) > 1 and h[1][0] <= cut:
                    h.pop(0)
                if h and h[-1][1] is None and h[-1][0] <= now - self.delay - WATCH_KEEP_S:
                    del self.hist[tid]

    def served(self, now=None):
        """{table id: state} as of `delay` seconds ago."""
        now = self.clock() if now is None else now
        out = {}
        with self.lock:
            for tid, h in self.hist.items():
                st = None
                for t, s in h:
                    if t <= now - self.delay:
                        st = s
                    else:
                        break
                if st is not None:
                    out[tid] = st
        return out

    def summary(self, now=None):
        """The watchable tables for the page's list: who, where, the round."""
        out = []
        for tid, st in sorted(self.served(now).items()):
            if st.get("ended"):
                continue        # the game's last frame: nothing left to watch
            seats = st.get("seats") or []
            out.append({"id": tid, "room": st.get("room"), "table": st.get("table"),
                        "names": [s.get("name") or "" for s in seats],
                        "scores": [s.get("score") for s in seats],
                        "round": st.get("round") or {}})
        return out

    def start(self, period=1.0):
        if self.thread is not None:
            return

        def loop():
            while True:
                try:
                    self.sample()
                except Exception as e:                 # noqa: BLE001
                    if time.time() - self._last_err > 300:
                        self._last_err = time.time()
                        print("[boardjan] watch sampler error (%s) -- still running" % e,
                              flush=True)
                time.sleep(period)

        self.thread = threading.Thread(target=loop, name="jan-watch", daemon=True)
        self.thread.start()


WATCH = Watch()


def start(args=None):
    """polboards' start hook: sample the tables from startup, so the delayed
    view has its 30 s of history before anyone opens the page."""
    WATCH.start()


#: the bot's status. Jan counts the PEOPLE SEATED at live tables rather than a
#: marker: jangame publishes none, and the board already samples the tables
#: for /watch.
PRESENCE_ONE = "player at the tables"
PRESENCE_MANY = "players at the tables"


def presence_count(args=None):
    """Distinct human players seated at a table that is still being played.
    A COM seat has no `mid` (and is flagged `bot`), so it is not a person."""
    seen = set()
    try:
        for tid, st in WATCH.served().items():
            if st.get("ended"):
                continue
            for s in (st.get("seats") or []):
                if isinstance(s, dict) and not s.get("bot") and s.get("mid") is not None:
                    seen.add(s["mid"])
    except Exception:                                  # noqa: BLE001
        return None                                    # fall back to a marker
    return len(seen)


def snapshot(args=None, now=None):
    now = time.time() if now is None else float(now)
    members = janstats.all_members()
    names = member_names(members)
    lists = _lists(names)
    b = board()
    per = b["rows_per_page"] if b else 10
    upd = [s.decode("cp932") for s in update_strings(b, now)] if b else None
    return {"board": NAME, "title": TITLE, "updated": int(now),
            "poll_s": float(getattr(args, "poll", 5.0) or 5.0),
            "members": len(members), "sig": _sig(lists),
            "art": bool(b), "update": upd,
            "recent": recent_games(members, names),
            "live_games": live_games(now),
            "tables": WATCH.summary(), "watch_delay": WATCH.delay,
            "categories": [{"id": c, "name": n, "float": c in FLOAT_CATEGORIES,
                            "pages": max(1, (len(lists[c]) + per - 1) // per),
                            "rows": category(c, names, rows=lists[c])}
                           for c, n in enumerate(CATEGORIES)]}


def cached_snapshot(args=None, ttl=2.0):
    now = time.time()
    with _SNAP_LOCK:
        if _SNAP["snap"] is not None and now - _SNAP["t"] < ttl:
            return _SNAP["snap"]
    snap = snapshot(args, now)
    with _SNAP_LOCK:
        _SNAP.update(t=now, snap=snap)
    return snap


def art_files():
    """What the pages may load directly: the baked layers, the web fonts,
    board.json (the layout), and /watch's 3D pieces (w3d.json, the textures,
    the call banners, three.module.min.js). kanji.bin itself is not served."""
    try:
        return frozenset(n for n in os.listdir(ART_DIR)
                         if n.endswith((".png", ".woff2", ".woff", ".json", ".js")))
    except OSError:
        return frozenset()


# ---------------------------------------------------------------------------
# portraits for /watch: each player's own PlayOnline face (profile field 19,
# z_ficon = sheet * 8 + tile), and for a COM one of PlayOnline's ACKY Gallery
# faces (hnf202 + hnf203) as the game itself shows -- the page asks for them by
# id at /face.png. The sheets are the shared services/boardart/faces/.
# ---------------------------------------------------------------------------
FACES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardart", "faces")
PORTRAIT_FIELD = 19
_FACE_IDS = {"t": 0.0, "map": {}}
_FACE_PNG = {}
_FACE_LOCK = threading.Lock()


def face_ids(members, ttl=60.0):
    """{member id: z_ficon} through a read-only URI, cached `ttl` s; 0 = none picked."""
    members = [int(m) for m in members if str(m).isdigit()]
    now = time.time()
    with _FACE_LOCK:
        if now - _FACE_IDS["t"] >= ttl:
            _FACE_IDS.update(t=now, map={})
        out = dict(_FACE_IDS["map"])
    want = [m for m in members if m not in out]
    if not want:
        return out
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % accounts_path(), uri=True, timeout=5)
        try:
            for m in want:
                row = conn.execute(
                    "SELECT val_int FROM handle_profile WHERE field_id = ? AND handle_id ="
                    " (SELECT id FROM handle WHERE member_id = ?"
                    "  ORDER BY is_primary DESC, id ASC LIMIT 1)", (PORTRAIT_FIELD, m)).fetchone()
                out[m] = int(row[0]) if row and row[0] else 0
        finally:
            conn.close()
    except sqlite3.Error:
        for m in want:
            out.setdefault(m, 0)
    with _FACE_LOCK:
        _FACE_IDS["map"].update(out)
    return out


def face_png(fid):
    """One portrait PNG: tile `fid & 7` of hnf<fid >> 3>.png (app.dll 0x4a7d199:
    4 x 2 tiles, left to right then the second row). 0 is PlayOnline's own
    blank, hnf000's first tile. None for an id no sheet has."""
    fid = int(fid)
    if fid < 0:
        return None
    with _FACE_LOCK:
        if fid in _FACE_PNG:
            return _FACE_PNG[fid]
    png = None
    try:
        from PIL import Image
        with Image.open(os.path.join(FACES_DIR, "hnf%03d.png" % (fid >> 3))) as im:
            im = im.convert("RGBA")
            tw, th, t = im.width // 4, im.height // 2, fid & 7
            buf = io.BytesIO()
            im.crop(((t % 4) * tw, (t // 4) * th, (t % 4 + 1) * tw, (t // 4 + 1) * th)).save(
                buf, "PNG", optimize=True)
            png = buf.getvalue()
    except (OSError, ValueError, ImportError):
        png = None
    with _FACE_LOCK:
        if len(_FACE_PNG) > 512:
            _FACE_PNG.clear()
        _FACE_PNG[fid] = png
    return png


def watch_public(st):
    """A served table as the page gets it: each player's member id (jangame's
    "mid") swapped for their portrait id ("face"). No member id leaves the board."""
    if not isinstance(st, dict):
        return st
    seats = [s if isinstance(s, dict) else {} for s in (st.get("seats") or [])]
    mids = [s.get("mid") for s in seats if s.get("mid") is not None]
    faces = face_ids(mids) if mids else {}
    out = dict(st, seats=[])
    for s in seats:
        s = dict(s)
        mid = s.pop("mid", None)
        if mid is not None and str(mid).isdigit():
            s["face"] = faces.get(int(mid), 0)
        out["seats"].append(s)
    return out


# ---------------------------------------------------------------------------
# the game's text, drawn the game's way (ported from build_rankart.py, which
# cites hdd.c / mail.c / ranking_win.c for every rule)
# ---------------------------------------------------------------------------
_BOARD = {"d": None, "loaded": False}


def board():
    """board.json from the bake, or None when the art is not baked."""
    if not _BOARD["loaded"]:
        _BOARD["loaded"] = True
        try:
            with open(os.path.join(ART_DIR, "board.json"), encoding="utf-8") as fh:
                _BOARD["d"] = json.load(fh)
        except (OSError, ValueError):
            _BOARD["d"] = None
        # THE LAYOUT SHIPS, THE ART DOES NOT: board.json is in the repository
        # and the sheets it names come from the user's own bake, so "baked"
        # means the first base sheet is on disk, not that the layout parsed.
        d = _BOARD["d"]
        bases = (d or {}).get("bases") or []
        if d is not None and (not bases or not os.path.exists(
                os.path.join(ART_DIR, bases[0]))):
            _BOARD["d"] = None
    return _BOARD["d"]


def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def mailfmt(v, n):
    """mail__0030b280: '%d.' then n digits by repeated *10 in float32, then up
    to n-1 trailing '0's become spaces ('0.0      ', '18.5   ')."""
    v = f32(v)
    i = int(v)
    head = b"%d." % i
    digs = []
    for _ in range(n):
        v = f32(f32(v - f32(float(i))) * 10.0)
        i = int(v)
        digs.append(0x30 if i < 0 else 0x30 + i % 10)
    for k in range(n - 1):
        j = len(digs) - 1 - k
        if digs[j] != 0x30:
            break
        digs[j] = 0x20
    return head + bytes(digs)


def kern(x, cur, prev):
    """hdd__002aa6e0, applied AFTER `cur` is drawn."""
    c, p = chr(cur), chr(prev) if prev else ""
    if c in "YWVPL":
        x -= 1
    if p and p in "zxusqponmkhecba":
        if c in "YV":
            x -= 2
        elif c in "WT":
            x -= 1
    elif p and p in "ZXK":
        if c in "QOGC":
            x -= 1
    elif p and p in "YWVPT":
        if c in "zyxwvusrqponmedcaJ":
            x -= 2
        elif c in "jMA":
            x -= 1
    elif p == "F":
        if c in "MJA":
            x -= 1
    elif p and p in "OGDC":
        if c in "jZXWVTMA":
            x -= 1
    elif p and p in "LMA":
        if c in "YWV":
            x -= 3
        elif c in "ywvT":
            x -= 2
        elif c in "tjfQOGC":
            x -= 1
    return x


class RomText:
    """kanji.bin (low nibble first) + hdd.c's fixed and proportional drawers."""

    def __init__(self, b):
        Image = _pil()
        self.Image = Image
        with open(os.path.join(ART_DIR, "kanji.bin"), "rb") as fh:
            self.bin = fh.read()
        self.alpha = list(b["alpha"])
        self.w = list(b["widths"])
        self.space = int(b["space_advance"])
        self.edge = [tuple(p) for p in b["edge_passes"]]
        self._g = {}
        self._t = {}

    def glyph(self, idx):
        g = self._g.get(idx)
        if g is None:
            g = self.Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            o = idx * 128
            if 0 <= idx and o + 128 <= len(self.bin):
                px = g.load()
                for y in range(16):
                    row = self.bin[o + y * 8: o + y * 8 + 8]
                    for x in range(16):
                        b = row[x // 2]
                        v = (b & 0xF) if x % 2 == 0 else (b >> 4)
                        if v:
                            px[x, y] = (255, 255, 255, self.alpha[v])
            self._g[idx] = g
        return g

    def tinted(self, idx, rgb, half=False):
        key = (idx, tuple(rgb), half)
        t = self._t.get(key)
        if t is None:
            gl = self.glyph(idx)
            t = self.Image.new("RGBA", gl.size, tuple(rgb) + (255,))
            a = gl.getchannel("A")
            t.putalpha(a.point(lambda v: v >> 1) if half else a)
            self._t[key] = t
        return t

    @staticmethod
    def lead(b):
        return 0x80 < b < 0xa0 or 0xdf < b < 0xfd

    @staticmethod
    def sjis_index(b1, b2):
        a = (b1 * 2) & 0xff
        if b2 < 0x9f:
            hi = ((a + 0x1f) if a < 0x3f else (a + 0x9f)) & 0xff
            lo = ((b2 - 0x1f) if b2 < 0x7f else (b2 - 0x20)) & 0xff
        else:
            hi = ((a + 0x20) if a < 0x3f else (a + 0xa0)) & 0xff
            lo = (b2 - 0x7e) & 0xff
        v = hi * 0x100 + lo - 0x2121
        return (v & 0xff) + ((v & 0xff00) >> 8) * 0x5e

    def layout(self, data, prop):
        out, x, prev, i = [], 0, 0, 0
        while i < len(data):
            b = data[i]
            if self.lead(b) and i + 1 < len(data):
                out.append((self.sjis_index(b, data[i + 1]), x))
                x += 16
                prev, i = 0, i + 2
                continue
            if not prop:
                if b == 0x20:
                    x += self.space
                elif b >= 0x20:
                    out.append((b + 0x4a5, x))
                    x += 8
            else:
                if b != 0x20:
                    out.append((b + 0x503, x))
                x = kern(x, b, prev) + self.w[b & 0x7f]
                prev = b
            i += 1
        return out, x

    def measure(self, data, prop):
        x, prev, i = 0, 0, 0
        while i < len(data):
            b = data[i]
            if self.lead(b) and i + 1 < len(data):
                x, prev, i = x + 16, 0, i + 2
                continue
            if b == 0x20:
                x += self.space
            elif b < 0x20:
                prev = 0
            else:
                x = (x + 8) if not prop else kern(x, b, prev) + self.w[b & 0x7f]
                prev = b
            i += 1
        return x

    def draw(self, canvas, data, st, rect):
        X, Y, Wd, Ht = rect
        width = self.measure(data, st["prop"])
        if st["halign"] == "right":
            x = X + Wd - width
        elif st["halign"] == "center":
            x = X + (Wd >> 1) - (width >> 1)
        else:
            x = X
        fs = st["font_size"]
        y = {"center": Y + ((Ht - fs) >> 1), "top": Y,
             "bottom": Y + Ht - fs}[st["valign"]]
        runs, _ = self.layout(data, st["prop"])
        for g, dx in runs:
            if st.get("outline"):
                e = self.tinted(g, st["outline"], half=True)
                for ox, oy in self.edge:
                    _comp(canvas, e, x + dx + ox, y + oy)
            _comp(canvas, self.tinted(g, st["fill"]), x + dx, y)


def _comp(dst, im, x, y):
    x, y = int(x), int(y)
    l, t = max(0, -x), max(0, -y)
    r, b = min(im.width, dst.width - x), min(im.height, dst.height - y)
    if r > l and b > t:
        dst.alpha_composite(im.crop((l, t, r, b)), (x + l, y + t))


_TEXT = {"t": None}


def _text():
    if _TEXT["t"] is None:
        _TEXT["t"] = RomText(board())
    return _TEXT["t"]


def _enc(s):
    """A name as the client would carry it: cp932, and '?' for anything the
    ROM cannot hold."""
    return str(s or "").encode("cp932", "replace")


def row_strings(b, cat, idx, f):
    """ranking_win__Str4_0035d5c0's five strings for one row, from janstats'
    <PO> fields (the same record the client receives)."""
    S = {k: v.encode("cp932") for k, v in b["strings"].items()}
    prev = int(f.get(PREV_FIELD[cat], NEW) or 0) & 0xFF
    prev = -1 if prev == NEW else prev
    if prev < 0:
        mk = S["new"]
    elif idx < prev:
        mk = S["up"]
    elif prev == idx:
        mk = S["same"]
    else:
        mk = S["down"]
    g = lambda k: int(f.get(k, 0) or 0)                  # noqa: E731
    # WARNING: THE FLOAT COLUMNS ARE NOT ints, AND THE DIVISOR IS THE KNOB.
    # This drew `mailfmt(f32(float(g(14))) / 1e6, 7)`: `g()` truncated the
    # rating to a whole number and the divisor was a literal, both left over
    # from before `janstats.FLOAT_SCALE` existed -- line 137 of this same file
    # had already been moved onto the knob, so one file held two scalings.
    # At FLOAT_SCALE 1 a rating of 8.423333 became `int` 8, then 8/1e6, which
    # `mailfmt` prints as **0.0000079** -- the `79` is float32 rounding, not a
    # 7.9, and the string reads exactly like a client dividing by 1e6.
    # KEY: Two different wrong scalings can print the same string: run the
    # formatter on the candidates before moving the constant.
    # `fl()` keeps the decimals and applies the scale actually in force, so the
    # column is right under EITHER setting of the knob.
    fl = lambda k: float(f.get(k, 0) or 0) / float(janstats.FLOAT_SCALE or 1)  # noqa: E731
    s = {"ranking": S["rankfmt"] % (mk, idx + 1),
         "ChrName": S["namefmt"] % _enc(f.get(2)),
         "ChrLevel": S["lvfmt"] % g(32)}
    if cat == 0:
        s["Str4"] = mailfmt(f32(fl(14)), 7)
        s["Str5"] = S["games0fmt"] % g(17)
    elif cat == 1:
        s["Str4"] = mailfmt(f32(fl(15)), 4)
        t = g(26)
        s["Str5"] = (b["titles"][t].encode("cp932") if 0 <= t < len(b["titles"])
                     else S["bad_title"])
    elif cat == 2:
        s["Str4"] = S["gamesfmt"] % g(22)
        s["Str5"] = S["moneyfmt"] % g(19)
    elif cat == 3:
        s["Str4"] = S["gamesfmt"] % g(23)
        s["Str5"] = S["moneyfmt"] % g(20)
    else:
        s["Str4"] = b""
        s["Str5"] = S["moneyfmt"] % g(21)
    return s


def update_strings(b, now=None):
    """The ranking week's start in JST, the way Str4_0035d5c0 prints it --
    WITHOUT the English build's fixed-offset overwrite ('Upda  yyyy/mm/dd'):
    the word is kept whole and the date follows it."""
    now = time.time() if now is None else now
    week = int(now // 604800)
    t = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=week * 604800 + 9 * 3600)
    word = b["strings"]["update"].encode("cp932").rstrip()
    return (word + b"  %04d/%02d/%02d" % (t.year, t.month, t.day),
            b"%02d:%02d" % (t.hour, t.minute))


def _draw_title(cv, b, cat, Image):
    """board.json "title" the way the page sets it: centred on x, on the
    baseline, shrunk to max_width, with the shadow then the edged text. A
    missing face leaves the title off rather than failing the render."""
    T = b.get("title")
    if not T:
        return
    sp = float(T.get("spacing", 0) or 0)
    try:
        from PIL import ImageDraw, ImageFont
        path = os.path.join(ART_DIR, T["ttf"])
        text = T["text"][cat]
        size = float(T["size"])
        font = ImageFont.truetype(path, int(round(size)))
        # ONE size on every tab, as the page: what the longest title needs.
        # The spacing does not shrink with the font: fit the glyph part.
        need = size
        for t in T["text"]:
            glyphs, gaps = font.getlength(t), sp * (len(t) - 1)
            if glyphs + gaps > T["max_width"]:
                need = min(need, size * (T["max_width"] - gaps) / glyphs)
        if need < size:
            font = ImageFont.truetype(path, int(need))
    except (OSError, ImportError, ValueError, IndexError, KeyError):
        return
    sw = int(T["stroke_px"])
    sh = T["shadow"]
    shade = tuple(int(v) for v in sh[:3]) + (int(round(float(sh[3]) * 255)),)
    total = font.getlength(text) + sp * (len(text) - 1)

    def spaced(d, x, y, **kw):
        # letter by letter, each at its kerned offset plus the spacing
        x0 = x - total / 2.0
        for i, ch in enumerate(text):
            d.text((x0 + font.getlength(text[:i]) + sp * i, y), ch, font=font,
                   anchor="ls", **kw)

    layer = Image.new("RGBA", cv.size, (0, 0, 0, 0))
    spaced(ImageDraw.Draw(layer), T["x"] + T["shadow_dx"], T["baseline"] + T["shadow_dy"],
           fill=shade, stroke_width=sw, stroke_fill=shade)
    cv.alpha_composite(layer)
    layer = Image.new("RGBA", cv.size, (0, 0, 0, 0))
    spaced(ImageDraw.Draw(layer), T["x"], T["baseline"], fill=tuple(T["fill"]),
           stroke_width=sw, stroke_fill=tuple(T["stroke"]))
    cv.alpha_composite(layer)


def render(cat, page=0, lists=None, now=None):
    """The Rankings screen for one category and page, as PNG bytes -- or None
    when the art is not baked or Pillow is missing."""
    b, Image = board(), _pil()
    if b is None or Image is None:
        return None
    cat = max(0, min(len(CATEGORIES) - 1, int(cat)))
    if lists is None:
        lists = _lists(member_names(janstats.all_members()))
    rows = lists[cat]
    per = int(b["rows_per_page"])
    pages = max(1, (len(rows) + per - 1) // per)
    page = max(0, min(pages - 1, int(page)))
    cv = Image.open(os.path.join(ART_DIR, b["bases"][cat])).convert("RGBA")
    _draw_title(cv, b, cat, Image)
    tx = _text()
    fx = b.get("fixups") or {}                  # the page's fixes, the same here
    if rows:
        label, hhmm = update_strings(b, now)
        tx.draw(cv, label, b["update"]["style"], fx.get("update_rect", b["update"]["rect"]))
        tx.draw(cv, hhmm, b["update_time"]["style"],
                fx.get("update_time_rect", b["update_time"]["rect"]))
    ox, oy = b["row_origin"]
    for i, (m, f) in enumerate(rows[page * per:(page + 1) * per]):
        idx = page * per + i
        s = row_strings(b, cat, idx, f)
        y0 = oy + b["row_pitch"] * i
        for col, spec in b["columns"].items():
            r = spec["rect"]
            st = spec["style"]
            if col == "ranking" and "rank_prop" in fx:
                st = dict(st, prop=bool(fx["rank_prop"]))
            tx.draw(cv, s.get(col, b""), st, (ox + r[0], y0 + r[1], r[2], r[3]))
    out = io.BytesIO()
    cv.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def render_cached(cat, page, args=None):
    snap = cached_snapshot(args)
    key = (int(cat), int(page), snap["sig"])
    with _RENDER_LOCK:
        hit = _RENDER.get((int(cat), int(page)))
        if hit and hit[0] == key:
            return hit[1]
    png = render(cat, page)
    with _RENDER_LOCK:
        _RENDER[(int(cat), int(page))] = (key, png)
    return png


def route(path, query, args):
    """polboards' hook for this board's own routes: /render.png, and
    /watch.json?t=<table> -- one watchable table as it was WATCH_DELAY ago."""
    if path in ("/watch", "/watch/"):
        return 200, WATCH_PAGE, "text/html; charset=utf-8", "no-store"
    if path == "/watch.json":
        # a table that is not shown (ended, not allowed, unknown) is an ANSWER,
        # "state": null -- not a 404 the viewer's browser logs as an error
        tid = (query.get("t") or [""])[0]
        st = watch_public(WATCH.served().get(tid))
        return (200, json.dumps({"id": tid, "delay": WATCH.delay, "state": st},
                                separators=(",", ":")),
                "application/json; charset=utf-8", "no-store")
    if path == "/face.png":
        # a portrait for /watch: ?id=<PlayOnline face id>
        try:
            png = face_png(int((query.get("id") or [""])[0]))
        except ValueError:
            return 400, "id is a PlayOnline face number", "text/plain", "no-store"
        if png is None:
            return 404, "no such portrait", "text/plain", "public, max-age=3600"
        return 200, png, "image/png", "public, max-age=86400"
    if path != "/render.png":
        return None
    try:
        cat = int((query.get("cat") or ["0"])[0])
        page = int((query.get("page") or ["0"])[0])
    except ValueError:
        return 400, "cat and page are numbers", "text/plain", "no-store"
    if not 0 <= cat < len(CATEGORIES) or page < 0:
        return 400, "cat is 0..4, page >= 0", "text/plain", "no-store"
    png = render_cached(cat, page, args)
    if png is None:
        return 503, "the Rankings art is not baked on this server", "text/plain", "no-store"
    return 200, png, "image/png", "no-cache"


# ---------------------------------------------------------------------------
# Discord (polboards' webhook): the Jan Rating screen as the image, the top of
# every category as text, and a short post when a category gets a new #1
# ---------------------------------------------------------------------------
DISCORD_TOP = 5
MOVE_MARK = {"new": "\U0001F195", "up": "▲", "down": "▼", "same": "▫"}


def _value_text(b, cat, r):
    if cat == 0:
        return "%.3f · %d game%s" % (r["value"], r["games"], "" if r["games"] == 1 else "s")
    if cat == 1:
        t = r["rank_title"]
        titles = (b or {}).get("titles") or []
        return "%.2f · %s" % (r["value"], titles[t] if 0 <= t < len(titles) else "?")
    return "{:,}".format(int(r["value"]))


def discord_message(snap, args=None):
    """(payload, files) for the board's one Discord message."""
    b = board()
    blocks = []
    for c in snap["categories"]:
        rows = c["rows"][:DISCORD_TOP]
        lines = ["`%2d.` %s **%s** %s" % (r["rank"], MOVE_MARK.get(r["move"], ""),
                                          (r["name"] or "(no name)").replace("*", ""),
                                          _value_text(b, c["id"], r))
                 for r in rows] or ["*No one ranked yet.*"]
        blocks.append("**%s**\n%s" % (c["name"], "\n".join(lines)))
    embed = {"title": "JongHoLow Rankings",
             "description": "\n\n".join(blocks)[:4000],
             "color": 0xB08A4E,
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(snap["updated"])),
             "footer": {"text": "%d player%s on file" % (
                 snap["members"], "" if snap["members"] == 1 else "s")}}
    url = (getattr(args, "jan_url", "") or "").strip()
    if url:
        embed["url"] = url
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    png = render_cached(0, 0, args) if b is not None else None
    files = []
    if png is not None:
        embed["image"] = {"url": "attachment://jan-rankings.png"}
        payload["attachments"] = [{"id": 0, "filename": "jan-rankings.png"}]
        files.append(("jan-rankings.png", "image/png", png))
    else:
        payload["attachments"] = []
    return payload, files


def discord_events(prev, snap):
    """A category's #1 changed hands. Both names must be known: a name read
    that failed once must not read as a new leader."""
    out = []
    for pc, c in zip(prev["categories"], snap["categories"]):
        if not pc["rows"] or not c["rows"]:
            continue
        was, now = pc["rows"][0]["name"], c["rows"][0]["name"]
        if was and now and was != now:
            out.append("\U0001F451 **%s** is now #1 in %s (was %s)"
                       % (now.replace("*", ""), c["name"], was.replace("*", "")))
    return out


# ---------------------------------------------------------------------------
# Discord feed "live" (asked for 2026-09-13): "when a game is up, it puts a
# message on the discord webhook with a link for people to observe and then
# deletes it when the game is no longer live". polboards' DiscordSet does the
# posting: one message per WATCHABLE table (the same delayed list the watching
# page serves -- the creator's own GALLEY_LIMIT, no password), edited as its
# round moves on, deleted as soon as the table is gone (ended_ttl 0), kept
# across restarts. It posts into the board's own channel unless
# POL_BOARDS_JAN_LIVE_DISCORD_WEBHOOK names another.
# ---------------------------------------------------------------------------
discord_live_ended_ttl = 0.0


def _table_label(tb):
    room = int(tb.get("room") or 0)
    return (("Room %d-%d, " % (room // 100, room % 100)) if room else "") + \
        "Table %s" % tb.get("table")


def live_text(tb, url="", delay=30.0):
    """The post for one live table. No markdown from player names."""
    names = [str(n).replace("*", "").replace("_", " ") for n in (tb.get("names") or []) if n]
    who = (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else "".join(names)
    r = tb.get("round") or {}
    rnd = ({"E": "East", "S": "South", "W": "West", "N": "North"}.get(r.get("wind"), r.get("wind"))
           + " %s" % r.get("number")) if r.get("wind") else ""
    head = "\U0001F004 **A JongHoLow table is live**: %s%s%s." % (
        _table_label(tb), (" (%s)" % rnd) if rnd else "", (" with " + who) if who else "")
    if not url:
        return head
    return "%s\nWatch it here, %d s behind the table: %s/watch#%s" % (
        head, int(round(float(delay or 0))), url.rstrip("/"), tb["id"])


def discord_live_messages(snap, args=None, bot=False):
    """polboards' message-set feed: a slot per watchable table while live.
    As the bot (`bot`), the link is a "Watch this table" button instead."""
    url = (getattr(args, "jan_url", "") or "").strip().rstrip("/")
    delay = snap.get("watch_delay", WATCH_DELAY)
    out = []
    for tb in snap.get("tables") or []:
        text = live_text(tb, "" if bot and url else url, delay)
        if bot and url:
            text += "\nShown %d s behind the table." % int(round(float(delay or 0)))
        # flags 4 = no link preview: the post is the link, not a card
        payload = {"content": text[:1900], "allowed_mentions": {"parse": []}, "flags": 4}
        if bot and url:
            payload["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": "Watch this table",
                 "url": "%s/watch#%s" % (url, tb["id"])}]}]
        out.append({"slot": str(tb["id"]), "ended": False,
                    "sig": hashlib.sha1(text.encode("utf-8")).hexdigest(),
                    "build": (lambda p=payload: (p, []))})
    return out


# ---------------------------------------------------------------------------
# The JongHoLow Discord BOT (asked for 2026-09-13: "turn Jan into its own
# discord bot so it can display the rankings like Tetra does"). polboards
# posts as the bot when POL_BOARDS_JAN_DISCORD_BOT_TOKEN is set: the rankings
# feed is ONE post -- a list's screen (the board's own render.png) with a
# button per list, L1 / R1 for pages and a link to the board -- and a click
# edits that post (UPDATE_MESSAGE); the live feed's posts get a Watch button.
# /janboard rankings | live moves a feed to the channel it is typed in.
# ---------------------------------------------------------------------------
DISCORD_FEED_NAMES = {"": "rankings", "live": "live"}
VIEW_RESET_S = 600.0            # the post drifts back to the first list after this
_VIEW = {"cat": 0, "page": 0, "t": 0.0}


def bot_view(now=None):
    """The list and page the rankings post shows: the last one clicked, for
    VIEW_RESET_S, else the first list."""
    now = time.time() if now is None else now
    if now - _VIEW["t"] > VIEW_RESET_S:
        return 0, 0
    return _VIEW["cat"], _VIEW["page"]


def _bot_labels():
    b = board() or {}
    caps = [x.get("caption") for x in ((b.get("layers") or {}).get("buttons") or [])]
    return [c if c else CATEGORIES[i] for i, c in
            enumerate(caps[:len(CATEGORIES)] + [None] * (len(CATEGORIES) - len(caps)))]


def discord_bot_message(snap, args=None, cat=None, page=None):
    """The rankings post as the bot posts it. (payload, files)."""
    if cat is None:
        cat, page = bot_view()
    cats = snap["categories"]
    cat = max(0, min(len(cats) - 1, int(cat)))
    pages = max(1, int(cats[cat].get("pages") or 1))
    page = max(0, min(pages - 1, int(page or 0)))
    base = (getattr(args, "jan_url", "") or "").strip().rstrip("/")
    embed = {"author": {"name": "JongHoLow Rankings"},
             "title": cats[cat]["name"] + ("  (page %d of %d)" % (page + 1, pages) if pages > 1 else ""),
             "color": 0xB08A4E,
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(snap["updated"])),
             "footer": {"text": "%d player%s on file" % (snap["members"],
                                                        "" if snap["members"] == 1 else "s")}}
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}, "attachments": []}
    files = []
    if base:
        embed["url"] = "%s/#%d" % (base, cat)
        # by the board's own public render.png: a click's answer carries no
        # upload, and v= moves with the lists so Discord's cache never lags
        embed["image"] = {"url": "%s/render.png?cat=%d&page=%d&v=%s"
                                 % (base, cat, page, snap["sig"])}
    else:
        png = render_cached(cat, page, args)
        if png is not None:
            embed["image"] = {"url": "attachment://jan-rankings.png"}
            payload["attachments"] = [{"id": 0, "filename": "jan-rankings.png"}]
            files = [("jan-rankings.png", "image/png", png)]
    labels = _bot_labels()
    tabs = [{"type": 2, "style": 1 if i == cat else 2, "label": labels[i],
             "custom_id": "jan:view:%d:0" % i} for i in range(len(cats))]
    second = [{"type": 2, "style": 2, "label": "◀ L1", "custom_id": "jan:page:%d:%d" % (cat, page - 1),
               "disabled": page <= 0},
              {"type": 2, "style": 2, "label": "R1 ▶", "custom_id": "jan:page:%d:%d" % (cat, page + 1),
               "disabled": page >= pages - 1}]
    if base:
        second.append({"type": 2, "style": 5, "label": "Open the board", "url": "%s/#%d" % (base, cat)})
    payload["components"] = [{"type": 1, "components": tabs}, {"type": 1, "components": second}]
    return payload, files


def discord_interaction(data, args=None):
    """A button click (polboards has checked its signature): jan:view:<list>:0
    and jan:page:<list>:<page> EDIT the rankings post to that list and page,
    and it stays there (bot_view); anything unknown gets a private note."""
    cid = str(((data or {}).get("data") or {}).get("custom_id") or "")
    parts = cid.split(":")

    def note(text):
        return {"type": 4, "data": {"flags": 64, "content": text,
                                    "allowed_mentions": {"parse": []}}}
    try:
        nums = [int(x) for x in parts[2:]]
    except ValueError:
        nums = []
    if parts[:2] in (["jan", "view"], ["jan", "page"]) and nums:
        snap = cached_snapshot(args)
        cat = nums[0]
        if not 0 <= cat < len(snap["categories"]):
            return note("That list is not on the board.")
        pages = max(1, int(snap["categories"][cat].get("pages") or 1))
        page = max(0, min(pages - 1, nums[1] if len(nums) > 1 else 0))
        _VIEW.update(cat=cat, page=page, t=time.time())
        return {"type": 7, "data": discord_bot_message(snap, args, cat, page)[0]}
    return note("That is not on the board any more.")


# ---------------------------------------------------------------------------
# the page: the Rankings WINDOW itself, filling the browser. Its layers are the
# game's art (tools/jan_boardart_bake.py -> board.json "layers"); every word on
# it is real text in the ROM's own glyphs (janrom-fixed / janrom-prop web
# fonts), placed by the game's rule: left / centre (>>1) / right in the node's
# rect, top = y + ((h - FontSize) >> 1), edge = the 15 offset passes at half
# alpha. The five category buttons swap the game's own state sprites; L1 / R1
# page like the game and, at the first / last page, roll into the previous /
# next category, so they switch tabs too.
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JongHoLow - Rankings</title>
<style>
@font-face{font-family:"JanROM Fixed";src:url(art/janrom-fixed.woff2) format("woff2"),url(art/janrom-fixed.woff) format("woff");font-display:block}
@font-face{font-family:"JanROM Prop";src:url(art/janrom-prop.woff2) format("woff2"),url(art/janrom-prop.woff) format("woff");font-display:block}
@font-face{font-family:"JanTitle";src:url(art/jan-title.woff2) format("woff2");font-display:block}
@font-face{font-family:"JanText";src:url(art/jan-text.woff2) format("woff2");font-weight:500;font-display:block}
@font-face{font-family:"JanText";src:url(art/jan-text-bold.woff2) format("woff2");font-weight:700;font-display:block}
/* the window is a panel on a mahjong table: felt behind it on any screen shape */
html,body{margin:0;height:100%;overflow:hidden;background:#1d3b2c}
/* felt: a faint fractal weave over the table's light fall-off */
body{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 .16 0 0 0 0'/%3E%3C/filter%3E%3Crect width='240' height='240' filter='url(%23n)'/%3E%3C/svg%3E"),radial-gradient(ellipse at 50% 42%,#2f5e47 0%,#224836 55%,#152d20 100%)}
/* the sheets: a deckled paper edge (the bake's masks), a burned rim inside
   it, and a shadow that follows the edge instead of a rectangle */
#wrap{position:fixed;left:0;top:0;width:640px;height:448px;transform-origin:0 0;-webkit-user-select:none;user-select:none;filter:drop-shadow(0 5px 12px rgba(0,0,0,.55))}
#stage{position:absolute;left:0;top:0;width:640px;height:448px;overflow:hidden;-webkit-mask:url(art/mask_main.png) 0 0/100% 100% no-repeat;mask:url(art/mask_main.png) 0 0/100% 100% no-repeat}
/* SE's own pieces lying over the sheet's corners: part of the window (its
   640x448 space, so they scale with it), over its empty margin only, the rest
   hanging on the felt and cut off by the browser's edge when there is none;
   never clickable */
#decor{position:absolute;left:0;top:0;width:100%;height:448px;pointer-events:none;z-index:4}
#decor .cl{position:absolute;transform-origin:50% 50%}
#decor .dp{position:absolute;background-repeat:no-repeat;background-size:100% 100%;filter:drop-shadow(0 2px 2px rgba(0,0,0,.45))}
#decor .tile{background-image:url(art/tiles_l.png)}
#stage::after,#side::after{content:"";position:absolute;inset:0;border-radius:12px;box-shadow:inset 0 0 26px 3px rgba(74,46,18,.42);pointer-events:none;z-index:10}
#stage img{position:absolute;display:block;pointer-events:none}
.t{position:absolute;white-space:pre;font-size:16px;line-height:16px;height:16px;font-kerning:normal;font-variant-numeric:tabular-nums;pointer-events:none}
button{position:absolute;margin:0;padding:0;border:0;background:none;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus{outline:none}
/* the game's button states: s3 = the flat highlight (the tab you are on),
   s1 = PRESSED (its pill sits 2 px lower, so the caption goes down with it),
   shown only while the button is held */
.cat{background:var(--s0) no-repeat 0 0}
.cat .t{color:var(--c0)}
.cat:hover,.cat:focus-visible{filter:brightness(1.07)}
.cat[aria-pressed=true]{background-image:var(--s3);z-index:2}
.cat[aria-pressed=true] .t{color:var(--c1)}
.cat:active{background-image:var(--s1);z-index:3;filter:none}
.cat:active .t{color:var(--c2);transform:translateY(2px)}
/* the sidebar: a second sheet of the same parchment, only when there is room */
#side{position:absolute;top:0;width:220px;height:448px;overflow:hidden;background:#d9c9a8 url(art/paper.png) 50% 50%/auto 100% no-repeat;-webkit-mask:url(art/mask_side.png) 0 0/100% 100% no-repeat;mask:url(art/mask_side.png) 0 0/100% 100% no-repeat;font-family:"JanText","M PLUS 1p",sans-serif;color:#2a2018}
#recent{list-style:none;margin:0;padding:0 16px;position:absolute;top:58px;left:0;right:0}
#recent li{padding:5px 0 6px;border-bottom:1px solid rgba(70,52,34,.28)}
#recent .r1,#recent .r2{display:flex;justify-content:space-between;align-items:baseline;white-space:nowrap;gap:8px}
#recent .nm{font-weight:700;font-size:14px;overflow:hidden;text-overflow:ellipsis}
#recent .pl{font-weight:700;font-size:13px;color:#4a3a28}
#recent .p0{color:#9a6400}
#recent .r2{font-size:12px;color:#5a4632;font-variant-numeric:tabular-nums}
#recent .up{color:#2d6a2d}
#recent .dn{color:#8a2b1e}
#recent .none{border:0;color:#5a4632;font-size:13px;text-align:center;padding-top:24px}
#recent .live a{display:block;color:inherit;text-decoration:none;pointer-events:auto}
#recent .live .nm{color:#7a2e08}
#recent .live:hover{background:rgba(255,248,225,.35)}
#recent .live .who{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#side-foot{position:absolute;left:16px;right:16px;bottom:16px;text-align:center;color:#3c2e20;font-size:13px;line-height:18px}
#side-foot b{font-weight:700}
.plate:hover img,.plate:focus-visible img{filter:brightness(1.18)}
.plate:active{transform:translateY(1px)}
#msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:16px system-ui,sans-serif;color:#2a2018}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
</style></head><body>
<div id="wrap" aria-hidden="true"><div id="stage"><div id="msg">Loading...</div></div><aside id="side" hidden></aside><div id="decor"></div></div>
<table class="sr" id="table"><caption id="cap"></caption><thead><tr><th>Rank</th><th>Name</th><th>Value</th><th>Games</th><th>Movement</th></tr></thead><tbody id="rows"></tbody></table>
<script>
"use strict";
let L = null, S = null, CAT = 0, PAGE = 0, SIG = '', OK = false;
const $ = s => document.querySelector(s);
const stage = $('#stage'), wrap = $('#wrap'), side = $('#side');
const E = {};                                   // the live text elements
const SIDE_W = 220, GAP = 14, PAD = 14;
const SHADOW = {};
function rgb(c){ return 'rgb(' + c.join(',') + ')'; }
function shadow(c){
  const k = c.join(',');
  return SHADOW[k] || (SHADOW[k] = L.edge_passes.map(p => p[0] + 'px ' + p[1] + 'px 0 rgba(' + k + ',.5)').join(','));
}
function el(tag, cls, parent){ const e = document.createElement(tag); if (cls) e.className = cls; (parent || stage).appendChild(e); return e; }
function box(e, r){ e.style.left = r[0] + 'px'; e.style.top = r[1] + 'px'; e.style.width = r[2] + 'px'; e.style.height = r[3] + 'px'; }
// Str4_0035d5c0 / hdd.c placement, in the node's rect
function put(e, text, r, st, keepColor, fit){
  e.textContent = text;
  // M PLUS 1p (jan-text): the ROM face, even traced, read as grainy. Edged
  // (white) labels in bold, dark ink in medium.
  e.style.fontFamily = '"JanText","M PLUS 1p","Meiryo",sans-serif';
  e.style.fontWeight = st.outline ? '700' : '500';
  if (!keepColor) e.style.color = rgb(st.fill);
  e.style.textShadow = st.outline ? shadow(st.outline) : 'none';
  // the node's own FontSize: 14 on the buttons and the date, 16 elsewhere;
  // `fit` shrinks a label that would leave its space, and only then
  let fs = st.font_size;
  const size = s => { e.style.fontSize = e.style.lineHeight = e.style.height = s + 'px'; };
  size(fs);
  let w = e.offsetWidth;
  if (fit && w > fit){ fs = fs * fit / w; size(fs); w = e.offsetWidth; }
  e.style.top = (st.valign === 'top' ? r[1] : st.valign === 'bottom' ? r[1] + r[3] - fs : r[1] + Math.floor((r[3] - fs) / 2)) + 'px';
  e.style.left = (st.halign === 'right' ? r[0] + r[2] - w : st.halign === 'center' ? r[0] + (r[2] >> 1) - (w >> 1) : r[0]) + 'px';
}
// labels (headers, captions, the date) in the proportional face: see the
// bake's FIXUPS.labels_prop
function label(st){ return (L.fixups || {}).labels_prop ? Object.assign({}, st, {prop: true}) : st; }
function art(name){ return 'art/' + name; }
function build(){
  const Y = L.layers;
  stage.innerHTML = '';
  HDR_FS = TITLE_FS = null;                       // re-measured on the new layout
  const bg = el('img'); bg.id = 'bg'; bg.alt = ''; bg.src = art(Y.bg); box(bg, [0, 0, 640, 448]);
  // the title: real text, white with SE's dark edge, one baseline on every tab
  const T = L.title, NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.id = 'title';
  svg.setAttribute('viewBox', '0 0 640 448'); svg.setAttribute('width', '640'); svg.setAttribute('height', '448');
  svg.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;overflow:visible';
  stage.appendChild(svg);
  const line = (fill, stroke, dx, dy) => {
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', T.x + dx); t.setAttribute('y', T.baseline + dy);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-family', '"' + T.family + '","Arial Rounded MT Bold",sans-serif');
    t.setAttribute('fill', fill); t.setAttribute('stroke', stroke);
    t.setAttribute('stroke-width', 2 * T.stroke_px); t.setAttribute('stroke-linejoin', 'round');
    t.setAttribute('paint-order', 'stroke');
    t.setAttribute('letter-spacing', T.spacing || 0);
    svg.appendChild(t);
    return t;
  };
  E.titleShadow = line(rgb(T.shadow.slice(0, 3)), rgb(T.shadow.slice(0, 3)), T.shadow_dx, T.shadow_dy);
  E.titleShadow.setAttribute('opacity', T.shadow[3]);
  E.title = line(rgb(T.fill), rgb(T.stroke), 0, 0);
  for (const [side, d, label] of [['L', -1, 'Previous (L1)'], ['R', 1, 'Next (R1)']]){
    const p = Y.plates[side + '_Base'], k = Y.plates[side + '1'];
    const b = el('button', 'plate'); b.id = 'plate-' + side; box(b, p.rect);
    b.title = label; b.setAttribute('aria-label', label);
    const a = el('img', '', b); a.alt = ''; a.src = art(p.img); box(a, [0, 0, p.rect[2], p.rect[3]]);
    const c = el('img', '', b); c.alt = ''; c.src = art(k.img);
    box(c, [k.rect[0] - p.rect[0], k.rect[1] - p.rect[1], k.rect[2], k.rect[3]]);
    b.onclick = () => step(d);
  }
  E.hdr = {};
  for (const n of Object.keys(Y.headers)) E.hdr[n] = el('span', 't');
  E.upd = [el('span', 't'), el('span', 't')]; E.upd[0].id = 'update'; E.upd[1].id = 'update-time';
  E.rows = [];
  for (let i = 0; i < L.rows_per_page; i++){
    const row = {};
    for (const col of Object.keys(L.columns)) row[col] = el('span', 't row');
    row._mark = el('span', 't row');                       // the rank's New / up / down tag
    row._fStr4 = el('span', 't row'); row._fStr5 = el('span', 't row');   // decimal parts
    E.rows.push(row);
  }
  E.status = el('span', 't'); E.status.id = 'status';
  E.cats = Y.buttons.map((spec, i) => {
    const r = spec.rect, b = el('button', 'cat'); box(b, r);
    b.dataset.cat = i; b.setAttribute('aria-label', spec.caption);
    for (const s of [0, 1, 3]) b.style.setProperty('--s' + s, 'url(' + art('btn' + r[2] + '_s' + s + '.png') + ')');
    Y.button_text.forEach((st, k) => b.style.setProperty('--c' + k, rgb(st.fill)));
    const cap = el('span', 't', b);
    // on the pill's body (sprite rows 6..26), not SE's node centre, which sat
    // the caption on the pill's lower edge
    const q = (L.fixups || {}).button_text_rect;
    // well clear of the pill's rounded ends (ink x 8..129): r[2] - 36 left
    // the long captions touching them (decided 2026-09-13: the captions were cramped)
    put(cap, spec.caption, q ? [q[0], q[1], r[2], q[3]] : [0, 0, r[2], r[3]],
        label(Y.button_text[0]), true, r[2] - 54);
    b.onclick = () => setCat(i);
    return b;
  });
  // one caption size for all five: the size the longest one needed
  const caps = E.cats.map(b => b.querySelector('.t'));
  const fs = Math.min(...caps.map(c => parseFloat(c.style.fontSize)));
  E.cats.forEach((b, i) => {
    const r = Y.buttons[i].rect, q = (L.fixups || {}).button_text_rect;
    put(caps[i], Y.buttons[i].caption, q ? [q[0], q[1], r[2], q[3]] : [0, 0, r[2], r[3]],
        Object.assign({}, label(Y.button_text[0]), {font_size: fs}), true);
  });
  for (const w of new Set(Y.buttons.map(b => b.rect[2]))) for (const s of [1, 3]){ const im = new Image(); im.src = art('btn' + w + '_s' + s + '.png'); }
}
// the window alone, or the window + the sidebar when that costs the window
// less than a fifth of its size (a 16:9 screen, not a phone)
function fit(){
  const W = innerWidth, H = innerHeight;
  const k1 = Math.min((W - 2 * PAD) / 640, (H - 2 * PAD) / 448);
  const k2 = Math.min((W - 2 * PAD) / (640 + GAP + SIDE_W), (H - 2 * PAD) / 448);
  const withSide = !!L && W >= 700 && k2 >= k1 * 0.8;
  const k = withSide ? k2 : k1, w = withSide ? 640 + GAP + SIDE_W : 640;
  side.hidden = !withSide;
  side.style.left = (640 + GAP) + 'px';
  wrap.style.width = w + 'px';
  wrap.style.transform = 'scale(' + k + ')';
  wrap.style.left = ((W - w * k) / 2) + 'px';
  wrap.style.top = ((H - 448 * k) / 2) + 'px';
  decorate();
}
// ---- the decoration ------------------------------------------------------------
// A few of SE's own pieces lie over the sheet's corners: the chocobo from
// JongHoLow's web pages with a pair of dice, a short wall with a point stick,
// a hand of tiles, crossed sticks. Each group is seeded (its tiles, dice and
// tilt never change).
function rng(seed){ let s = seed >>> 0; return () => (s = (s * 1664525 + 1013904223) >>> 0) / 4294967296; }
function tileIdx(c){
  if (c === 'back') return 37;
  const r = +c[0], s = c[1];
  if (s === 'z') return 26 + r;
  if (r === 0) return {m: 34, p: 35, s: 36}[s];
  return {m: 0, p: 9, s: 18}[s] + r - 1;
}
// a group: its size, and parts [art, x, y, w, h, degrees] (art: a file, or t:<tile>)
function cluster(name, R){
  const jig = (a) => (R() * 2 - 1) * a;
  if (name === 'chocobo')
    // the chocobo at ~1.25x its 43 px (2x was judged way too big)
    return {w: 116, h: 62, choco: true, parts: [['chocobo.png', 0, 4, 54, 53, 0],
      ['dice' + (1 + Math.floor(R() * 6)) + '.png', 60, 14, 26, 29, jig(18)],
      ['dice' + (1 + Math.floor(R() * 6)) + '.png', 88, 28, 26, 29, jig(18)]]};
  if (name === 'hand'){
    const pool = ['1z', '5z', '6z', '7z', '0p', '0m', '3m', '9s', '1p', '8s', '2z'], parts = [];
    for (let i = 0; i < 5; i++) parts.push(['t:' + pool.splice(Math.floor(R() * pool.length), 1)[0],
                                            i * 30, 3 + jig(3), 29, 38, jig(5)]);
    return {w: 150, h: 46, parts};
  }
  if (name === 'wall'){
    const parts = [];
    for (let i = 0; i < 6; i++) parts.push(['t:back', i * 29, 0, 29, 38, 0]);
    parts.push(['stick2.png', 26, 48, 124, 20, jig(4)]);
    return {w: 174, h: 70, parts};
  }
  return {w: 140, h: 60, parts: [['stick1.png', 6, 4, 124, 20, 7 + jig(3)],
    ['stick3.png', 0, 22, 124, 20, -5 + jig(3)], ['stick_riichi.png', 12, 38, 124, 20, 2 + jig(3)]]};
}
// WHERE (decided 2026-09-13): "some of them ... overlapping over the corners
// of the paper ... and if the window is too big they just look like
// decorations on the edges that are cut off". So they are part of the window:
// inside #wrap, in its 640x448 space, scaling and moving with it exactly --
// no layout of their own, nothing that can pop or fade. Each overlaps only
// the sheet's empty margin (clear of the title, the date, the headers and the
// buttons, plates and sidebar text -- the browser test checks each tilted
// box); the rest hangs over the felt. The chocobo sits on the sheet's empty
// top-left corner, within the 14 px the window always keeps from the
// browser's edge, so it is in view at any size. [left | right, top, tilt]
const CORNERS = {
  chocobo: {left: 4, top: -10, rot: -6},
  wall:    {left: -120, top: 434, rot: 6},
  hand:    {right: -120, top: 438, rot: -8},
  sticks:  {right: -82, top: -52, rot: -10},
};
function seedOf(s){ let h = 2166136261; for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619); return h >>> 0; }
function decorate(){
  const d = document.querySelector('#decor');
  if (!d || d.childElementCount) return;                // built once
  for (const [name, at] of Object.entries(CORNERS)){
    const c = cluster(name, rng(seedOf(name) ^ 20260913));
    const el = document.createElement('div');
    el.className = 'cl' + (c.choco ? ' choco' : '');
    el.style.width = c.w + 'px'; el.style.height = c.h + 'px'; el.style.top = at.top + 'px';
    if ('left' in at) el.style.left = at.left + 'px'; else el.style.right = at.right + 'px';
    el.style.transform = 'rotate(' + at.rot + 'deg)';
    for (const [art, x, y, w, h, deg] of c.parts){
      const p = document.createElement('div');
      p.className = 'dp' + (art.startsWith('t:') ? ' tile' : '');
      p.style.cssText = 'left:' + x + 'px;top:' + y + 'px;width:' + w + 'px;height:' + h +
                        'px;transform:rotate(' + deg + 'deg)';
      if (art.startsWith('t:')){
        p.style.backgroundSize = (38 * w) + 'px ' + h + 'px';
        p.style.backgroundPosition = (-tileIdx(art.slice(2)) * w) + 'px 0';
      } else p.style.backgroundImage = 'url(art/' + art + ')';
      el.appendChild(p);
    }
    d.appendChild(el);
  }
}
// one line of text at x, aligned left / right / center ON x (the table's
// columns), vertically centred in the band y..y+h
// one size for every header on every tab: the size the longest one needs.
// Each used to shrink on its own to fit its column, so the rating headers
// jumped between sizes from tab to tab (reported 2026-09-13).
let HDR_FS = null;
function headerFont(){
  if (HDR_FS !== null) return HDR_FS;
  const Y = L.layers, T = L.fixups.table, probe = el('span', 't');
  let fs = Infinity;
  for (const [n, spec] of Object.entries(Y.headers)){
    const st = label(spec.style);
    fs = Math.min(fs, st.font_size);
    if (n !== 'Str4' && n !== 'Str5') continue;
    const k = n === 'Str4' ? 0 : 1, cc = k ? T.c5 : T.c4;
    Y.header_text.forEach((pair, c) => {
      if (!(T.kinds[c] || [])[k] || !pair[k]) return;
      put(probe, pair[k], [0, 0, 0, spec.rect[3]], st, false, cc[1] - cc[0]);
      fs = Math.min(fs, parseFloat(probe.style.fontSize));
    });
  }
  probe.remove();
  return (HDR_FS = fs);
}
function at(e, text, st, x, align, y, h, fit){
  put(e, text, [x, y, 0, h], Object.assign({}, st, {halign: align}), false, fit);
}
function buildSide(){
  side.innerHTML = '';
  const NS = 'http://www.w3.org/2000/svg', T = L.title;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 220 48'); svg.setAttribute('width', '220'); svg.setAttribute('height', '48');
  svg.style.cssText = 'position:absolute;left:0;top:6px;overflow:visible';
  const sh = rgb(T.shadow.slice(0, 3));
  for (const [dx, dy, fill, stroke, op] of [[T.shadow_dx, T.shadow_dy, sh, sh, T.shadow[3]], [0, 0, rgb(T.fill), rgb(T.stroke), 1]]){
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', 110 + dx); t.setAttribute('y', 34 + dy); t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-family', '"' + T.family + '","Arial Rounded MT Bold",sans-serif');
    t.setAttribute('font-size', 21); t.setAttribute('letter-spacing', T.spacing || 0);
    t.setAttribute('fill', fill); t.setAttribute('stroke', stroke); t.setAttribute('stroke-width', 2 * T.stroke_px * 0.8);
    t.setAttribute('stroke-linejoin', 'round'); t.setAttribute('paint-order', 'stroke'); t.setAttribute('opacity', op);
    t.textContent = 'Recent games';
    svg.appendChild(t);
  }
  side.appendChild(svg);
  E.recent = document.createElement('ol'); E.recent.id = 'recent'; side.appendChild(E.recent);
  E.sideFoot = document.createElement('div'); E.sideFoot.id = 'side-foot'; side.appendChild(E.sideFoot);
}
function tableLabel(tb){
  const r = +tb.room || 0;
  return (r ? 'Room ' + Math.floor(r / 100) + '-' + (r % 100) + ', ' : '') + 'Table ' + tb.table;
}
function roundLabel(r){
  return r && r.wind ? ({E: 'East', S: 'South', W: 'West', N: 'North'}[r.wind] || r.wind) + ' ' + r.number : '';
}
function ago(t){
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 3600 ? Math.round(s / 60) + 'm ago'
       : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
}
function paintSide(){
  if (!S || !E.recent) return;
  const PLACE = ['1st', '2nd', '3rd', '4th'], span = (cls, text) => {
    const e = document.createElement('span'); e.className = cls; e.textContent = text; return e; };
  E.recent.innerHTML = '';
  // the tables you can watch (creator allowed it; shown 30 s late) come first
  for (const tb of (S.tables || [])){
    const li = document.createElement('li'), a = document.createElement('a');
    const r1 = document.createElement('div'), r2 = document.createElement('div');
    li.className = 'live'; a.href = 'watch#' + tb.id; r1.className = 'r1'; r2.className = 'r2';
    r1.append(span('nm', 'Watch: ' + tableLabel(tb)), span('pl', roundLabel(tb.round)));
    r2.append(span('who', (tb.names || []).filter(Boolean).join(', ')));
    a.append(r1, r2); li.appendChild(a); E.recent.appendChild(li);
  }
  const list = (S.recent || []).slice(0, 7);
  if (!list.length && !(S.tables || []).length){ const li = document.createElement('li'); li.className = 'none'; li.textContent = 'No games yet.'; E.recent.appendChild(li); }
  for (const g of list){
    const li = document.createElement('li'), a = document.createElement('div'), b = document.createElement('div');
    a.className = 'r1'; b.className = 'r2';
    a.append(span('nm', g.name || '(no name)'), span('pl p' + g.place, PLACE[g.place] || '?'));
    b.append(span(g.result > 0 ? 'up' : g.result < 0 ? 'dn' : '', (g.result > 0 ? '+' : '') + g.result.toFixed(1)),
             span('ago', ago(g.t)));
    li.append(a, b);
    E.recent.appendChild(li);
  }
  E.sideFoot.innerHTML = '';
  const lg = S.live_games, l1 = document.createElement('div'), l2 = document.createElement('div');
  const bold = document.createElement('b');
  bold.textContent = lg == null ? '' : lg ? lg + (lg === 1 ? ' game' : ' games') + ' in progress' : 'No games in progress';
  l1.appendChild(bold);
  l2.textContent = S.members + (S.members === 1 ? ' player' : ' players') + ' on file';
  E.sideFoot.append(l1, l2);
  // only as many games as fit above the footer
  const limit = E.sideFoot.offsetTop - 6;
  while (E.recent.children.length > 1){
    const last = E.recent.lastElementChild;
    if (E.recent.offsetTop + last.offsetTop + last.offsetHeight <= limit) break;
    last.remove();
  }
}
function pages(){ return S ? S.categories[CAT].pages : 1; }
function setCat(c){ CAT = c; PAGE = 0; show(); }
// L1 / R1: the game's paging; past either end, the neighbouring category
function step(d){
  const p = PAGE + d;
  if (S && p >= 0 && p < pages()) PAGE = p;
  else { CAT = (CAT + d + 5) % 5; PAGE = d < 0 ? pages() - 1 : 0; }
  show();
}
function two(n){ return (n < 10 ? '0' : '') + n; }
function statusText(){
  if (!OK) return 'Reconnecting...';
  const c = S.categories[CAT], t = new Date();
  const when = two(t.getHours()) + ':' + two(t.getMinutes()) + ':' + two(t.getSeconds());
  const who = c.rows.length ? c.rows.length + (c.rows.length === 1 ? ' player' : ' players') + ' ranked' : 'No one ranked yet';
  return who + '   Page ' + (PAGE + 1) + '/' + c.pages + '   Live ' + when;
}
// ONE title size on every tab: the size the longest title needs to fit
// max_width. Each used to shrink on its own, so the big title changed size
// from tab to tab (reported 2026-09-13). The rendered box: getBBox, since
// getComputedTextLength() leaves the letter-spacing out; shrinking scales the
// glyphs but not the spacing, so the glyph part is what is fitted.
let TITLE_FS = null;
function titleSize(){
  if (TITLE_FS !== null) return TITLE_FS;
  const T = L.title;
  let fs = T.size;
  for (const text of T.text){
    E.title.textContent = text; E.title.setAttribute('font-size', T.size);
    const w = E.title.getBBox().width, sp = (T.spacing || 0) * (text.length - 1);
    if (w > T.max_width) fs = Math.min(fs, T.size * (T.max_width - sp) / (w - sp));
  }
  return (TITLE_FS = fs);
}
function paintTitle(){
  const T = L.title, text = T.text[CAT], size = titleSize();
  for (const t of [E.titleShadow, E.title]){ t.textContent = text; t.setAttribute('font-size', size); }
}
function paintStatus(){
  if (!L || !E.status) return;
  const live = (S && OK && S.tables) || [];
  // a live table is one tap away even without the sidebar (phones)
  put(E.status, statusText() + (live.length ? '   Watch live' : ''), L.status.rect, L.status.style);
  E.status.style.pointerEvents = live.length ? 'auto' : 'none';
  E.status.style.cursor = live.length ? 'pointer' : '';
  E.status.onclick = live.length ? () => { location.href = 'watch#' + live[0].id; } : null;
}
function show(){
  if (!S || !L) return;
  try { history.replaceState(null, '', '#' + CAT); } catch (e) {}
  const Y = L.layers, c = S.categories[CAT], per = L.rows_per_page;
  paintTitle();
  // the table (the bake's FIXUPS.table): each header on its column's one
  // alignment, so it sits over its values on every tab
  const T = L.fixups.table, kinds = T.kinds[CAT];
  for (const [n, spec] of Object.entries(Y.headers)){
    const text = n === 'Str4' ? Y.header_text[CAT][0] : n === 'Str5' ? Y.header_text[CAT][1] : spec.text;
    const st = Object.assign({}, label(spec.style), {font_size: headerFont()}), y = spec.rect[1], h = spec.rect[3];
    if (n === 'ranking') at(E.hdr[n], text, st, T.rank[0], 'left', y, h);
    else if (n === 'ChrName1') at(E.hdr[n], text, st, T.name, 'left', y, h);
    else if (n === 'ChrLevel') at(E.hdr[n], text, st, T.lv, 'center', y, h);
    else {
      const cc = n === 'Str4' ? T.c4 : T.c5, kind = n === 'Str4' ? kinds[0] : kinds[1];
      at(E.hdr[n], kind ? text : '', st, kind === 'num' ? cc[1] : cc[0],
         kind === 'num' ? 'right' : 'left', y, h, cc[1] - cc[0]);
    }
  }
  const has = c.rows.length > 0 && S.update, fx = L.fixups || {};
  // the bake's fixups: where the English text does not fit SE's own layout
  put(E.upd[0], has ? S.update[0] : '', fx.update_rect || L.update.rect, label(L.update.style));
  put(E.upd[1], has ? S.update[1] : '', fx.update_time_rect || L.update_time.rect, label(L.update_time.style));
  // the rows on the same columns: SE's padding (its way of right-aligning in
  // ROM cells) is trimmed and the column's alignment does the work
  const [, oy] = L.row_origin, st = col => L.columns[col].style;
  E.rows.forEach((row, i) => {
    const r = c.rows[PAGE * per + i], cells = (r && r.cells) || {};
    const y = oy + L.row_pitch * i, h = L.row_pitch;
    // SE's "%s%3d": the New / up / down marker as a small tag, then the number
    const m = /^(.*?)\s*(\d+)$/.exec(cells.ranking || '') || ['', '', ''];
    at(row._mark, m[1], Object.assign({}, st('ranking'), {font_size: 12}), T.rank[0], 'left', y, h);
    at(row.ranking, m[2], st('ranking'), T.rank[1], 'right', y, h);
    at(row.ChrName, (cells.ChrName || '').trim(), st('ChrName'), T.name, 'left', y, h);
    at(row.ChrLevel, (cells.ChrLevel || '').trim(), st('ChrLevel'), T.lv, 'center', y, h);
    for (const [col, cc, kind] of [['Str4', T.c4, kinds[0]], ['Str5', T.c5, kinds[1]]]){
      const text = (cells[col] || '').trim(), frac = row['_f' + col];
      const d = kind === 'dec' ? /^(-?\d+)(\.\d*)?$/.exec(text) : null;
      if (kind === 'num') at(row[col], text, st(col), cc[1], 'right', y, h);
      else if (d) at(row[col], d[1], st(col), cc[2], 'right', y, h);    // the whole part ends on the point
      else at(row[col], text, st(col), cc[0], 'left', y, h);
      at(frac, d ? (d[2] || '') : '', st(col), cc[2], 'left', y, h);   // ...and the rest starts on it
    }
  });
  E.cats.forEach((b, i) => b.setAttribute('aria-pressed', String(i === CAT)));
  paintStatus();
  paintSide();
  $('#cap').textContent = c.name + ', page ' + (PAGE + 1) + ' of ' + c.pages;
  const tb = $('#rows'); tb.innerHTML = '';
  for (const r of c.rows.slice(PAGE * per, PAGE * per + per)){
    const tr = document.createElement('tr');
    for (const v of [r.rank, r.name || '(no name)', r.value, r.games, r.move]){ const td = document.createElement('td'); td.textContent = v; tr.appendChild(td); }
    tb.appendChild(tr);
  }
}
document.addEventListener('keydown', e => {
  if (e.key >= '1' && e.key <= '5') setCat(+e.key - 1);
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || e.key === '[') step(-1);
  else if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ']') step(1);
});
let tx = null;
addEventListener('touchstart', e => { tx = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive: true});
addEventListener('touchend', e => {
  if (tx === null) return;
  const dx = e.changedTouches[0].clientX - tx; tx = null;
  if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
}, {passive: true});
addEventListener('resize', fit);
async function poll(){
  try {
    const r = await fetch('state.json', {cache: 'no-store'});
    if (r.ok){
      const fresh = await r.json(), first = !S; S = fresh; OK = true;
      if (first){ const m = /^#([0-4])$/.exec(location.hash); if (m) CAT = +m[1]; show(); }
      else if (S.sig !== SIG){ PAGE = Math.min(PAGE, pages() - 1); show(); }
      SIG = S.sig;
    } else OK = false;
  } catch (e) { OK = false; }
  paintStatus();
  paintSide();
  setTimeout(poll, ((S && S.poll_s) || 5) * 1000);
}
async function init(){
  fit();
  try {
    const r = await fetch('art/board.json');
    if (!r.ok) throw new Error(r.status);
    L = await r.json();
  } catch (e) { $('#msg').textContent = 'The Rankings art is not installed on this server.'; return; }
  try { await Promise.all([document.fonts.load('500 16px "JanText"', 'Aa1'), document.fonts.load('700 16px "JanText"', 'Aa1'),
                           document.fonts.load('30px "' + L.title.family + '"', L.title.text[0])]); } catch (e) {}
  build(); buildSide(); fit(); poll();
}
init();
</script></body></html>
"""

# ---------------------------------------------------------------------------
# /watch: one table, as SE's own spectator sees it (all four hands), from
# /watch.json -- which only ever serves it WATCH_DELAY seconds late, and only
# while its creator allows watchers. Drawn in 3D (three.js, served from art/)
# with JongHoLow's OWN models and textures (tools/jan_watch3d_bake.py) laid
# out by the game's config2.dat (decided 2026-09-13: bigger, anchored, the
# game's 3D tiles, zoom on a player, hover names, portraits, played tiles,
# the game's call banners and results screen). The view stays behind the
# first human's seat; free look and the seat zooms always come back to it.
# ---------------------------------------------------------------------------
WATCH_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="/">
<title>JongHoLow - Watching</title>
<style>
@font-face{font-family:"JanTitle";src:url(art/jan-title.woff2) format("woff2");font-display:swap}
@font-face{font-family:"JanText";src:url(art/jan-text.woff2) format("woff2");font-weight:500;font-display:swap}
@font-face{font-family:"JanText";src:url(art/jan-text-bold.woff2) format("woff2");font-weight:700 900;font-display:swap}
:root{
  --room:#0d1512; --room-2:#152520; --ivory:#f3eee0; --ivory-dim:#c9c6b6;
  --orange:#e8892b; --brass:#cfae62; --ink:#1a2420; --glass:rgba(10,18,15,.78);
  --line:rgba(243,238,224,.14);
  --display:"JanTitle","Nunito","Arial Rounded MT Bold","Segoe UI",sans-serif;
  --body:"JanText","M PLUS 1p","Segoe UI",sans-serif;
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;height:100%;overflow:hidden}
body{background:radial-gradient(ellipse 80% 70% at 50% 46%,#1d3a2e 0%,var(--room-2) 45%,var(--room) 100%);
  color:var(--ivory);font:500 14px/1.45 var(--body);-webkit-font-smoothing:antialiased}
#c{position:fixed;inset:0;width:100%;height:100%;display:block;outline:none;touch-action:none}
#c.hot{cursor:pointer}
.hud{position:fixed;pointer-events:none;z-index:2}
button{font:inherit;color:inherit}

/* where this is, how late, the way back */
#tag{left:14px;top:14px;display:grid;gap:4px;max-width:320px;font-size:12.5px;color:var(--ivory-dim);padding:8px 12px;border-radius:10px;
  background:var(--glass);box-shadow:0 0 0 1px var(--line)}
#tag .where{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
#tag b{color:var(--ivory);font-weight:700}
#tag .lag{color:var(--brass);font-weight:700}
#tag a{pointer-events:auto;color:var(--ivory);font-weight:700;text-decoration:none;border-bottom:1px solid rgba(243,238,224,.35)}
#tag a:hover{border-bottom-color:var(--ivory)}
#tag a:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
#tag .hint{font-size:11.5px;line-height:1.4}

/* the round: a capsule at the top */
#round{left:50%;top:14px;transform:translateX(-50%);display:flex;align-items:center;gap:14px;
  padding:7px 16px 7px 18px;border-radius:999px;background:var(--glass);box-shadow:0 0 0 1px var(--line),0 6px 24px rgba(0,0,0,.35)}
#round:empty{display:none}
#round .rw{font:900 22px/1 var(--display);letter-spacing:.5px;color:#fff;white-space:nowrap}
#round .meta{display:flex;gap:12px;font-size:12.5px;color:var(--ivory-dim);font-variant-numeric:tabular-nums;white-space:nowrap}
#round .meta b{color:var(--ivory);font-weight:700}
#round .dora{display:flex;align-items:center;gap:6px;padding-left:12px;border-left:1px solid var(--line);font-size:12px;color:var(--brass);font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.face{display:inline-block;width:21px;height:28px;border-radius:3px;background-color:#f6f3ea;background-repeat:no-repeat;
  box-shadow:inset 0 -2px 0 var(--orange),0 1px 2px rgba(0,0,0,.5)}

/* a seat's card, beside that seat's edge of the table */
.seat{position:fixed;left:0;top:0;z-index:2;display:grid;grid-template-columns:auto 1fr;gap:0 10px;align-items:center;
  width:212px;padding:7px 12px 7px 7px;border-radius:12px;background:var(--glass);cursor:pointer;
  box-shadow:0 0 0 1px var(--line),0 8px 22px rgba(0,0,0,.4);transition:box-shadow .2s,background .2s;will-change:transform}
.seat:hover{background:rgba(18,30,25,.9)}
.seat:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
.seat.turn{box-shadow:0 0 0 2px var(--brass),0 0 22px rgba(207,174,98,.35),0 8px 22px rgba(0,0,0,.4)}
.seat.focus{background:rgba(26,40,33,.94)}
.pf{grid-row:span 2;width:40px;height:60px;border-radius:7px;background:#23372e center/cover no-repeat;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.12)}
.nm{display:flex;align-items:center;gap:6px;min-width:0}
.nm .w{font:900 13px/1 var(--display);color:var(--brass);width:14px;flex:none}
.nm .w.dealer{color:var(--orange)}
.nm .n{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row2{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--ivory-dim);font-variant-numeric:tabular-nums}
.chip{font-size:10.5px;line-height:15px;font-weight:700;letter-spacing:.06em;padding:0 5px;border-radius:4px;text-transform:uppercase}
.chip.riichi{background:var(--orange);color:#231306}
.chip.cpu{background:var(--ivory-dim);color:var(--ink)}
.chip.deal{background:transparent;color:var(--orange);box-shadow:inset 0 0 0 1px rgba(232,137,43,.6)}

/* the controls */
#bar{left:50%;bottom:14px;transform:translateX(-50%);pointer-events:auto;display:flex;align-items:center;gap:4px;
  padding:5px;border-radius:12px;background:var(--glass);box-shadow:0 0 0 1px var(--line),0 6px 24px rgba(0,0,0,.35);max-width:calc(100vw - 24px);overflow-x:auto}
#bar:empty{display:none}
#bar button{border:0;background:transparent;padding:6px 11px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;color:var(--ivory-dim)}
#bar button:hover{background:rgba(243,238,224,.08);color:var(--ivory)}
#bar button[aria-pressed="true"]{background:var(--ivory);color:var(--ink)}
#bar button:focus-visible{outline:2px solid var(--brass);outline-offset:1px}
#bar kbd{font:700 10.5px/1 var(--body);padding:2px 4px;margin-left:5px;border-radius:3px;background:rgba(243,238,224,.12);color:inherit;opacity:.75}
#bar .sep{width:1px;align-self:stretch;margin:4px 4px;background:var(--line)}

/* the tile card that follows the cursor */
#tip{left:0;top:0;display:flex;gap:10px;align-items:center;padding:8px 12px 8px 8px;border-radius:10px;background:rgba(8,14,12,.92);
  box-shadow:0 0 0 1px var(--line),0 10px 26px rgba(0,0,0,.5);max-width:300px;z-index:4}
#tip .face{width:36px;height:48px;flex:none}
#tip .t1{font-weight:700;font-size:15px}
#tip .t1 small{font-weight:500;font-size:12px;color:var(--brass);margin-left:6px}
#tip .t2{font-size:12.5px;color:var(--ivory-dim)}

/* the game's calls: its own banner pops over the seat that made it */
.call{position:fixed;left:0;top:0;z-index:3;pointer-events:none;
  transform:translate(var(--x),var(--y)) translate(-50%,-50%);animation:pop 1.5s cubic-bezier(.2,.8,.2,1) forwards}
.call img{display:block;height:auto;filter:drop-shadow(0 8px 16px rgba(0,0,0,.55))}
@keyframes pop{
  0%{opacity:0;transform:translate(var(--x),var(--y)) translate(-50%,-50%) scale(1.7)}
  16%{opacity:1;transform:translate(var(--x),var(--y)) translate(-50%,-50%) scale(.93)}
  26%{transform:translate(var(--x),var(--y)) translate(-50%,-50%) scale(1.03)}
  78%{opacity:1;transform:translate(var(--x),var(--y)) translate(-50%,-50%) scale(1)}
  100%{opacity:0;transform:translate(var(--x),var(--y)) translate(-50%,-60%) scale(1)}}

/* the settlement, as the game shows it: the four players at their own sides,
   points rolling from before to after */
#results{inset:0;pointer-events:auto;z-index:5;display:grid;grid-template-columns:1fr minmax(260px,420px) 1fr;
  grid-template-rows:1fr auto 1fr;gap:18px;padding:70px 24px 80px;
  background:radial-gradient(ellipse at 50% 50%,rgba(8,14,12,.66),rgba(8,14,12,.92));animation:fadein .35s ease-out}
@keyframes fadein{from{opacity:0}to{opacity:1}}
#results .mid{grid-column:2;grid-row:2;display:grid;justify-items:center;gap:6px;text-align:center}
#results .mid img{width:clamp(200px,24vw,330px);height:auto;filter:drop-shadow(0 8px 16px rgba(0,0,0,.5))}
#results .mid h2{margin:0;font:900 24px/1.2 var(--display);color:#fff;text-wrap:balance}
#results .mid .yk{color:var(--ivory);font-weight:700;text-wrap:balance;max-width:34ch}
#results .mid .pts{color:var(--brass);font-weight:700;font-variant-numeric:tabular-nums}
#results .mid button{margin-top:8px;border:0;border-radius:8px;padding:7px 14px;background:var(--ivory);color:var(--ink);font-weight:700;cursor:pointer}
#results .mid button:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
.pl{display:grid;grid-template-columns:auto 1fr;gap:0 12px;align-items:center;align-self:center;width:min(290px,100%);padding:10px 14px 10px 10px;border-radius:14px;
  background:rgba(12,22,18,.95);box-shadow:0 0 0 1px var(--line),0 12px 30px rgba(0,0,0,.5);animation:slide .55s cubic-bezier(.2,.8,.2,1) both}
.pl.p0{grid-column:2;grid-row:3;align-self:start;justify-self:center;--dx:0px;--dy:40px}
.pl.p1{grid-column:3;grid-row:2;justify-self:start;--dx:40px;--dy:0px;animation-delay:.08s}
.pl.p2{grid-column:2;grid-row:1;align-self:end;justify-self:center;--dx:0px;--dy:-40px;animation-delay:.16s}
.pl.p3{grid-column:1;grid-row:2;justify-self:end;--dx:-40px;--dy:0px;animation-delay:.24s}
@keyframes slide{from{opacity:0;transform:translate(var(--dx),var(--dy))}to{opacity:1;transform:none}}
.pl.win{box-shadow:0 0 0 2px var(--brass),0 0 26px rgba(207,174,98,.35),0 12px 30px rgba(0,0,0,.5)}
.pl .pf{width:48px;height:72px}
.pl .who{display:flex;align-items:center;gap:6px;font-weight:700;font-size:15px;min-width:0}
.pl .who .w{font:900 13px/1 var(--display);color:var(--brass)}
.pl .who .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pl .nums{display:grid;grid-template-columns:auto 1fr;gap:0 12px;align-items:baseline;font-variant-numeric:tabular-nums;font-size:12.5px;color:var(--ivory-dim)}
.pl .nums span:nth-child(even){text-align:right}
.pl .nums .now{font:700 21px/1.25 var(--body);color:var(--ivory)}
.pl .nums .d{font-weight:700;font-size:14px}
.pl .nums .d.up{color:#93d68c}.pl .nums .d.down{color:#f0907b}

/* a card over the table: the end of a game */
#card{left:50%;top:50%;transform:translate(-50%,-50%);pointer-events:auto;width:min(380px,calc(100vw - 32px));padding:20px 22px 16px;border-radius:16px;
  background:rgba(12,20,17,.95);box-shadow:0 0 0 1px var(--line),0 20px 60px rgba(0,0,0,.6);text-align:center;z-index:6}
#card h2{margin:0 0 4px;font:900 26px/1.1 var(--display);color:#fff;text-wrap:balance}
#card p{margin:0 0 12px;color:var(--ivory-dim);text-wrap:balance}
#card ol{list-style:none;margin:0 0 14px;padding:0;display:grid;gap:4px;font-variant-numeric:tabular-nums}
#card li{display:grid;grid-template-columns:22px 1fr auto;gap:8px;padding:5px 10px;border-radius:8px;background:rgba(243,238,224,.05);text-align:left}
#card li span:first-child{color:var(--brass);font-weight:700}
#card .acts{display:flex;justify-content:center;gap:8px}
#card button,#card a{border:0;border-radius:8px;padding:7px 14px;background:var(--ivory);color:var(--ink);font-weight:700;cursor:pointer;text-decoration:none}
#card a{background:rgba(243,238,224,.1);color:var(--ivory)}
#gone{inset:0;display:grid;place-items:center;text-align:center;padding:24px;color:var(--ivory-dim);font-size:16px;pointer-events:auto;z-index:7;
  background:radial-gradient(ellipse at 50% 46%,#1d3a2e 0%,var(--room-2) 45%,var(--room) 100%)}
#gone a{color:var(--brass)}
@media (max-width:760px){.seat{width:170px}.pf{width:32px;height:48px}#round .meta span.opt{display:none}#round .dora{display:none}#tag .hint{display:none}
  #results{grid-template-columns:1fr 1fr;grid-template-rows:auto auto auto;padding:64px 12px 76px;overflow:auto}
  #results .mid{grid-column:1/-1;grid-row:1}.pl.p0,.pl.p1,.pl.p2,.pl.p3{grid-column:auto;grid-row:auto;justify-self:stretch;align-self:start}}
@media (prefers-reduced-motion:reduce){.seat{transition:none}.call{animation-duration:1.2s;animation-timing-function:steps(1,end)}#results,.pl{animation:none}}
</style></head><body>
<canvas id="c" tabindex="0" aria-label="The mahjong table, drawn in 3D"></canvas>
<div id="tag" class="hud">
  <div class="where"><b id="where">JongHoLow</b><span class="lag" id="lag"></span><a href="./">Rankings</a></div>
  <div class="hint">Drag to turn the table, right-drag to move it, scroll to zoom. Click a player to zoom in. Esc puts it back.</div>
</div>
<div id="round" class="hud" aria-live="polite"></div>
<div id="seats"></div>
<div id="bar" class="hud" role="toolbar" aria-label="Camera"></div>
<div id="tip" class="hud" hidden></div>
<div id="results" class="hud" hidden></div>
<div id="card" class="hud" hidden></div>
<div id="gone" class="hud" hidden></div>
<script type="module">
import * as THREE from "./art/three.module.min.js";

// ---------------------------------------------------------------------------
// One watchable table, 30 s late, in JongHoLow's own 3D pieces: the tile mesh,
// faces and table from Warashi/data/model (tools/jan_watch3d_bake.py), laid out
// by the game's config2.dat. Each new frame of /watch.json is compared with the
// last one to play what happened: discards fly, calls pop the game's banners, a
// win opens the game's results screen with the points rolling.
// ---------------------------------------------------------------------------
const $ = s => document.querySelector(s);
const ID = (/^#?(\d+)$/.exec(location.hash) || /[?&]t=(\d+)/.exec(location.search) || [0, ""])[1];
const faceUrl = id => "face.png?id=" + (Math.max(0, id | 0));
const ACKY = 202 * 8;                      // PlayOnline's ACKY Gallery, hnf202 + hnf203: the COMs' faces

// ---- the table's layout: config2.dat, world (x, z), y up, felt at 0 ----
const HAND  = [[-13.0274, 18.1181, 1.85], [17.3636, 13.1846, 1.90], [12.8451, -17.6367, 1.85], [-17.5039, -12.7543, 1.90]];
const POND  = [[-4.9679, 8.9388], [8.2137, 5.3356], [4.7380, -8.3684], [-8.3177, -4.8279]];
const MELDS = [[[11.984, 18.132], [5.467, 18.135], [-1.124, 18.102], [-7.716, 18.105]],
               [[17.462, -12.085], [17.424, -5.469], [17.392, 1.091], [17.354, 7.633]],
               [[-12.124, -17.624], [-5.612, -17.630], [0.881, -17.642], [7.413, -17.591]],
               [[-17.592, 12.634], [-17.564, 6.032], [-17.482, -0.600], [-17.504, -7.211]]];
const STICK = [[10.4299, 10.5898], [9.8899, -10.3599], [-10.6298, -10.2297], [-10.0698, 10.9599]];
const MARK  = [[11.5693, 8.7699], [8.1100, -11.5397], [-11.7696, -8.3499], [-8.3000, 12.1197]];
const YAW = [0, Math.PI / 2, Math.PI, -Math.PI / 2];        // a seat's frame: its owner at +z
const OWN = [[0, 1], [1, 0], [0, -1], [-1, 0]];              // towards the owner
const DIR = [[1, 0], [0, -1], [-1, 0], [0, 1]];              // along the hand
const POND_STEP = 1.85, POND_ROW = 2.449, FLAT_Y = 0.76, TILT_OPEN = 1.42, TILT_FOCUS = 0.26;

// ---- tiles: web codes, names, faces ----
const SUIT = {m: "Characters", p: "Circles", s: "Bamboo"}, SUIT_JP = {m: "man", p: "pin", s: "sou"};
const HONOR = [["East wind", "ton"], ["South wind", "nan"], ["West wind", "sha"], ["North wind", "pei"],
               ["White dragon", "haku"], ["Green dragon", "hatsu"], ["Red dragon", "chun"]];
function kindOf(c){
  const r = +c[0], s = c[1];
  if (s === "z") return 26 + r;
  return {m: 0, p: 9, s: 18}[s] + (r === 0 ? 5 : r) - 1;
}
function tileName(c){
  const r = +c[0], s = c[1];
  if (s === "z") return {name: HONOR[r - 1][0], jp: HONOR[r - 1][1]};
  return {name: (r === 0 ? "Red 5" : r) + " of " + SUIT[s], jp: (r === 0 ? 5 : r) + "-" + SUIT_JP[s] + (r === 0 ? ", red five" : "")};
}
function doraFrom(c){
  const r = +c[0] || 5, s = c[1];
  if (s !== "z") return (r % 9 + 1) + s;
  if (r <= 4) return (r % 4 + 1) + "z";
  return (r === 7 ? 5 : r + 1) + "z";
}
let D = null;
function faceUV(c){
  const r = +c[0], red = r === 0 && c[1] !== "z";
  return red ? D.red["mps".indexOf(c[1])] : D.faces[kindOf(c)];
}
function faceCss(el, c, w, h){
  const uv = faceUV(c), us = uv.map(p => p[0]), vs = uv.map(p => 1 - p[1]);
  const u0 = Math.min(...us), u1 = Math.max(...us), v0 = Math.min(...vs), v1 = Math.max(...vs);
  const sx = w / ((u1 - u0) * 512), sy = h / ((v1 - v0) * 512);
  el.style.backgroundImage = "url(art/w3d-hai.png)";
  el.style.backgroundSize = (512 * sx) + "px " + (512 * sy) + "px";
  el.style.backgroundPosition = (-u0 * 512 * sx) + "px " + (-v0 * 512 * sy) + "px";
}

function gone(msg){
  const g = $("#gone"); g.innerHTML = "";
  const d = document.createElement("div"); d.textContent = msg + " ";
  const a = document.createElement("a"); a.href = "./"; a.textContent = "Back to the rankings";
  d.appendChild(a); g.appendChild(d); g.hidden = false;
}

// ---- three.js ----
const canvas = $("#c");
let renderer = null;
try { renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true}); } catch (e) { renderer = null; }
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(30, 1, 1, 800);
let dirty = true;
if (renderer){
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
}
scene.add(new THREE.HemisphereLight(0xfff6e6, 0x1c2c24, 1.15));
const sun = new THREE.DirectionalLight(0xffffff, 1.9);
sun.position.set(18, 70, 34);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, {left: -32, right: 32, top: 32, bottom: -32, near: 10, far: 160});
sun.shadow.bias = -0.0006;
sun.shadow.radius = 3;
scene.add(sun);
function texture(url){
  const t = new THREE.TextureLoader().load(url, () => { dirty = true; });
  t.colorSpace = THREE.SRGBColorSpace;
  if (renderer) t.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return t;
}
function geometry(m, uv){
  let g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(m.v, 3));
  g.setAttribute("uv", new THREE.Float32BufferAttribute(uv || m.uv, 2));
  g.setIndex(m.f);
  g = g.toNonIndexed();                 // flat faces: the PS2's own bevelled look
  g.computeVertexNormals();
  return g;
}
const baseMap = texture("art/w3d-hai.png");
const tileMat = new THREE.MeshStandardMaterial({map: baseMap, roughness: 0.42, metalness: 0, side: THREE.DoubleSide});
const hoverMat = tileMat.clone(); hoverMat.emissive = new THREE.Color(0x3a3322);
const lastMat = tileMat.clone(); lastMat.emissive = new THREE.Color(0x5a3a10);
const GEO = new Map();
// BEGINNER HINTS (the phone app's setting, frame flag S.hints): a small label
// in each face's top-right corner -- the number on the characters and the
// 1 of bamboo, E S W N on the winds, Wh G R on the dragons -- drawn into a
// copy of the face atlas, so the hand, the ponds and the melds all carry it.
const HINT_LABEL = kind => kind < 9 ? String(kind + 1) : kind === 18 ? "1" : kind >= 27 ? ["E", "S", "W", "N", "Wh", "G", "R"][kind - 27] : null;
let HINTS = false, hintMap = null;
function drawHints(img, faces, red){
  const c = document.createElement("canvas"); c.width = img.width; c.height = img.height;
  const g = c.getContext("2d"); g.drawImage(img, 0, 0);
  const put = (uv, text) => {
    const us = uv.map(p => p[0] * c.width), vs = uv.map(p => (1 - p[1]) * c.height);
    const x1 = Math.max(...us), y0 = Math.min(...vs), h = Math.max(...vs) - y0, w = x1 - Math.min(...us);
    const size = Math.max(8, Math.round(h * 0.24));
    g.font = "900 " + size + "px Arial, sans-serif"; g.textAlign = "right"; g.textBaseline = "top";
    g.lineWidth = Math.max(2, size / 4); g.strokeStyle = "#fff"; g.fillStyle = "#1a2a8a";
    const x = x1 - w * 0.06, y = y0 + h * 0.04;
    g.strokeText(text, x, y); g.fillText(text, x, y);
  };
  faces.forEach((uv, k) => { const t = HINT_LABEL(k); if (t) put(uv, t); });
  if (red && red[0]) put(red[0], "5");
  return c;
}
window.__janHints = drawHints;          // the app bakes its 2D hand strip the same way
function setHints(on){
  HINTS = on;
  const apply = map => { for (const m of [tileMat, hoverMat, lastMat]){ m.map = map; m.needsUpdate = true; } dirty = true; };
  if (!on){ apply(baseMap); return; }
  if (hintMap){ apply(hintMap); return; }
  const img = new Image();
  img.onload = () => {
    hintMap = new THREE.CanvasTexture(drawHints(img, D.faces, D.red));
    hintMap.colorSpace = THREE.SRGBColorSpace; hintMap.flipY = baseMap.flipY;
    if (renderer) hintMap.anisotropy = renderer.capabilities.getMaxAnisotropy();
    if (HINTS) apply(hintMap);
  };
  img.src = "art/w3d-hai.png";
}
function tileGeo(c){
  const key = c || "back";
  if (!GEO.has(key)){
    const uv = D.tile.uv.slice(), face = c ? faceUV(c) : D.faces[0];
    D.faceIdx.forEach((vi, i) => { uv[vi * 2] = face[i][0]; uv[vi * 2 + 1] = face[i][1]; });
    GEO.set(key, geometry(D.tile, uv));
  }
  return GEO.get(key);
}
let stickGeo = null, markGeo = null;
function setup(){
  const table = new THREE.Mesh(geometry(D.table), new THREE.MeshStandardMaterial(
    {map: texture("art/w3d-taku.png"), roughness: 0.92, metalness: 0, side: THREE.DoubleSide}));
  table.receiveShadow = true;
  scene.add(table);
  const prop = m => { const g = geometry(m); g.computeBoundingBox(); return g; };
  stickGeo = prop(D.stick); markGeo = prop(D.marker);
}

// ---- the camera: fixed views you can always go back to, and free look ----
let FOCUS = null, FOLLOW = false, FREE = false;
const cam = {from: null, to: null, t0: 0, dur: 0};
const controls = (() => {
  const target = new THREE.Vector3(), sph = new THREE.Spherical(), off = new THREE.Vector3();
  const v = {th: 0, ph: 0, zoom: 0, pan: new THREE.Vector3()};
  const ptrs = new Map();
  let mode = null, last = null, from = null, started = false, pinch = 0;
  const sync = () => { off.copy(camera.position).sub(target); sph.setFromVector3(off); };
  function begin(){
    if (cam.to) return false;
    sync(); FREE = true; FOLLOW = false; drawBar();
    return true;
  }
  const mid = () => { const [a, b] = [...ptrs.values()]; return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, Math.hypot(a[0] - b[0], a[1] - b[1])]; };
  canvas.addEventListener("contextmenu", e => e.preventDefault());
  canvas.addEventListener("pointerdown", e => {
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
    if (ptrs.size === 1){ mode = e.button === 2 || e.shiftKey || e.ctrlKey ? "pan" : "rotate"; last = from = [e.clientX, e.clientY]; started = false; }
    else if (ptrs.size === 2){ const m = mid(); mode = "pinch"; last = [m[0], m[1]]; pinch = m[2]; }
  });
  canvas.addEventListener("pointermove", e => {
    if (!mode || !ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    let x = e.clientX, y = e.clientY, scale = 1;
    if (mode === "pinch"){ const m = mid(); x = m[0]; y = m[1]; scale = pinch / (m[2] || 1); pinch = m[2]; }
    if (!started){
      if (mode !== "pinch" && Math.hypot(x - from[0], y - from[1]) < 4) return;   // still a click
      if (!begin()){ mode = null; return; }
      started = true;
    }
    const dx = x - last[0], dy = y - last[1], h = canvas.clientHeight || innerHeight;
    last = [x, y];
    if (mode === "rotate"){ v.th -= 2 * Math.PI * dx / h * 0.7; v.ph -= 2 * Math.PI * dy / h * 0.7; }
    else {
      const dist = sph.radius * Math.tan(camera.fov * Math.PI / 360);
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0).setY(0).normalize();
      const fwd = new THREE.Vector3().crossVectors(camera.up, right).setY(0).normalize();
      v.pan.addScaledVector(right, -2 * dx * dist / h).addScaledVector(fwd, 2 * dy * dist / h);
      if (mode === "pinch") v.zoom += Math.log(scale);
    }
  });
  const up = e => {
    ptrs.delete(e.pointerId);
    if (!ptrs.size) mode = null;
    else if (ptrs.size === 1){ const [p] = [...ptrs.values()]; mode = "rotate"; last = from = p; }
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("wheel", e => {
    e.preventDefault();
    if (!FREE && !begin()) return;
    if (FREE && !started) sync();
    v.zoom += Math.max(-1, Math.min(1, e.deltaY * 0.0015));
  }, {passive: false});
  function update(){
    if (cam.to){ v.th = v.ph = v.zoom = 0; v.pan.set(0, 0, 0); return false; }
    if (Math.abs(v.th) + Math.abs(v.ph) + Math.abs(v.zoom) + v.pan.lengthSq() < 1e-7) return false;
    const k = REDUCED ? 1 : 0.2;
    sph.theta += v.th * k;
    sph.phi = Math.max(0.04, Math.min(1.36, sph.phi + v.ph * k));
    sph.radius = Math.max(20, Math.min(260, sph.radius * Math.exp(v.zoom * k)));
    target.addScaledVector(v.pan, k);
    target.x = Math.max(-24, Math.min(24, target.x)); target.z = Math.max(-24, Math.min(24, target.z));
    target.y = Math.max(0, Math.min(8, target.y));
    off.setFromSpherical(sph); camera.position.copy(target).add(off); camera.lookAt(target);
    v.th *= 1 - k; v.ph *= 1 - k; v.zoom *= 1 - k; v.pan.multiplyScalar(1 - k);
    return true;
  }
  return {target, update};
})();

// ---- the pieces for one frame ----
const pieces = new THREE.Group(); scene.add(pieces);
let pickable = [], hands = [[], [], [], []], anims = [];
// the tile backs of hands this viewer may not see (null tiles, the phone app's
// own table): hidden while the camera is zoomed on that seat
let backs = [[], [], [], []];
let pondLast = [null, null, null, null], handMid = [null, null, null, null];
const V = (x, z, y = 0) => new THREE.Vector3(x, y, z);
const tilt = [TILT_OPEN, TILT_OPEN, TILT_OPEN, TILT_OPEN], tiltGoal = tilt.slice();

// one tile. mode: "stand" (a hand, tilted by its seat), "up" (lying face up), "down" (face down).
// The .p2m face is stored upside down AND mirrored: a half turn about the face normal (z = PI).
function makeTile(c, pos, seat, mode, info, side){
  const pivot = new THREE.Group();
  const mesh = new THREE.Mesh(tileGeo(mode === "down" ? null : c), tileMat);
  mesh.castShadow = true;
  mesh.rotation.order = "XYZ";
  if (mode === "stand"){
    // pivot on the tile's back bottom edge, so a tilt lays it back onto that edge
    pivot.position.copy(pos).addScaledVector(V(OWN[seat][0], OWN[seat][1]), -0.75);
    mesh.position.set(0, 1.25, 0.75);
    mesh.rotation.set(0, Math.PI, Math.PI);
    pivot.rotation.order = "YXZ";
    pivot.rotation.set(-tilt[seat], YAW[seat], 0);
  } else {
    pivot.position.copy(pos); pivot.position.y = FLAT_Y + (pos.y || 0);
    mesh.rotation.set(mode === "down" ? Math.PI / 2 : -Math.PI / 2, Math.PI, Math.PI);
    pivot.rotation.y = YAW[seat] + (side ? Math.PI / 2 : 0);
  }
  pivot.add(mesh);
  pieces.add(pivot);
  if (c && mode !== "down"){ mesh.userData = {code: c, seat, ...info}; pickable.push(mesh); }
  return {pivot, mesh};
}
// a piece arrives: from `from` to where it was built, on an arc, turning as it
// lands; `mesh` also tips a lying tile over in flight (it leaves the hand
// standing). Reduced motion keeps the move, shorter and without arc or spin.
function fly(pivot, from, dur, arc, spin, mesh, delay){
  if (REDUCED){ dur = Math.min(dur, 260); arc = 0; spin = 0; delay = 0; }
  const to = pivot.position.clone(), rot = pivot.rotation.y;
  pivot.position.copy(from);
  const a = {pivot, from: from.clone(), to, rot, spin: spin || 0, arc: arc || 0, t0: performance.now() + (delay || 0), dur};
  if (mesh){ a.mesh = mesh; a.x1 = mesh.rotation.x; a.x0 = 0; mesh.rotation.x = 0; }
  anims.push(a);
}

let S = null, VIEW = null;
function disp(s){ return (s - (VIEW || 0) + 4) % 4; }
function seatName(s){
  const x = (S.seats || [])[s] || {};
  return x.name || (x.bot ? (s ? "COM " + s : "COM") : "Seat " + (s + 1));
}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function build(state, prev){
  S = state;
  pieces.clear(); pickable = []; hands = [[], [], [], []]; anims = []; backs = [[], [], [], []];
  hovered = null; $("#tip").hidden = true;
  const oldPond = pondLast.slice();
  pondLast = [null, null, null, null]; handMid = [null, null, null, null];
  if (VIEW === null){ const h = (S.seats || []).findIndex(x => x && !x.bot); VIEW = h >= 0 ? h : 0; }
  const events = [];
  (S.seats || []).forEach((seat, s) => {
    const was = prev ? (prev.seats || [])[s] || {} : null;
    const p = disp(s), [ox, oz, step] = HAND[p], d = DIR[p], o = OWN[p];
    handMid[s] = V(ox + d[0] * step * 6, oz + d[1] * step * 6, 2.6);
    // the hand: sorted, the drawn tile half a step apart at its end
    let hand = (seat.hand || []).slice(), drawn = seat.drawn || null;
    const di = drawn && hand.length % 3 === 2 ? hand.lastIndexOf(drawn) : -1;
    if (di >= 0) hand.splice(di, 1); else drawn = null;
    // a null tile is one this viewer may not see (the phone app's own table
    // sends the other seats' hands so): it stands as a tile back, after the rest
    hand.sort((a, b) => (a === null) - (b === null) || (a === null ? 0
      : kindOf(a) - kindOf(b) + (a[0] === "0" ? 0.5 : 0) - (b[0] === "0" ? 0.5 : 0)));
    const put = (c, i) => makeTile(c, V(ox + d[0] * step * i, oz + d[1] * step * i), p, "stand",
                                   {where: "hand", owner: s, drawn: i >= hand.length});
    hand.forEach((c, i) => { const pv = put(c, i).pivot; hands[p].push(pv); if (c === null){ backs[p].push(pv); pv.visible = FOCUS !== p; } });
    if (drawn){
      const t = put(drawn, hand.length + 0.5);
      hands[p].push(t.pivot);
      if (was && was.drawn !== seat.drawn) fly(t.pivot, t.pivot.position.clone().add(V(0, 0, 3.2)), 320, 0);
    }
    // melds from the right-hand slot inwards; the called tile lies sideways
    const newMeld = was && (seat.melds || []).length > (was.melds || []).length;
    (seat.melds || []).forEach((m, mi) => {
      const [mx, mz] = MELDS[p][Math.min(mi, 3)];
      const tiles = (m.tiles || []).slice(), rel = m.from >= 0 ? (m.from - s + 4) % 4 : 0;
      let at = -1;
      if (rel && m.called){
        const i = tiles.indexOf(m.called); if (i >= 0) tiles.splice(i, 1);
        at = rel === 3 ? 0 : rel === 2 ? 1 : tiles.length;
        tiles.splice(at, 0, m.called);
      }
      let along = 0;
      const fresh = newMeld && mi === seat.melds.length - 1;
      tiles.forEach((c, i) => {
        const side = i === at, w = side ? 2.5 : 1.9, inset = side ? 0.3 : 0;
        const x = mx + d[0] * (along + w / 2) + o[0] * inset, z = mz + d[1] * (along + w / 2) + o[1] * inset;
        const hidden = m.kind === "ankan" && (i === 1 || i === 2);
        const t = makeTile(c, V(x, z), p, hidden ? "down" : "up",
                           {where: "meld", owner: s, meld: m.kind, from: m.from, called: side}, side);
        if (fresh){
          const src = side && m.from >= 0 && oldPond[m.from] ? oldPond[m.from] : handMid[s];
          fly(t.pivot, src, 700, 3.8, side ? 1.2 : 0.4, t.mesh, i * 90);
        }
        along += w;
      });
      if (fresh) events.push([m.kind, s]);
    });
    // the pond: rows of six from its origin, the riichi tile sideways
    const [px, pz] = POND[p];
    let left = -POND_STEP / 2, row = 0, n = 0;
    const pond = seat.pond || [], wasLen = was ? (was.pond || []).length : pond.length;
    pond.forEach((e, i) => {
      if (e.called) return;
      if (n && n % 6 === 0){ left = -POND_STEP / 2; row++; }
      const side = !!e.riichi, w = side ? 2.45 : POND_STEP, mid = left + w / 2, inset = side ? 0.3 : 0;
      const x = px + d[0] * mid + o[0] * (POND_ROW * row + inset), z = pz + d[1] * mid + o[1] * (POND_ROW * row + inset);
      const isLast = S.last && S.last.seat === s && i === pond.length - 1 && e.tile === S.last.tile;
      const t = makeTile(e.tile, V(x, z), p, "up", {where: "pond", owner: s, turn: i + 1, riichi: side, last: isLast}, side);
      if (isLast){ t.mesh.material = lastMat; t.mesh.userData.base = lastMat; }
      pondLast[s] = t.pivot.position.clone();
      if (i >= wasLen) fly(t.pivot, handMid[s], 720, 4.5, 0.7, t.mesh);
      left += w; n++;
    });
    // a riichi stick in front of the pond
    if (seat.riichi){
      const m = new THREE.Mesh(stickGeo, tileMat);
      m.position.set(STICK[p][0], -stickGeo.boundingBox.min.y + 0.02, STICK[p][1]);
      const bb = stickGeo.boundingBox, longX = bb.max.x - bb.min.x > bb.max.z - bb.min.z;
      m.rotation.y = YAW[p] + (longX ? 0 : Math.PI / 2);
      m.castShadow = true; pieces.add(m);
      if (was && !was.riichi){ fly(m, m.position.clone().add(V(0, 0, 6)), 500, 0); events.push(["riichi", s]); }
    }
  });
  // the dealer's marker
  const r = S.round || {};
  if (r.dealer != null){
    const p = disp(r.dealer), m = new THREE.Mesh(markGeo, tileMat);
    m.position.set(MARK[p][0], -markGeo.boundingBox.min.y + 0.02, MARK[p][1]);
    m.rotation.y = YAW[p]; m.castShadow = true; pieces.add(m);
  }
  // the dead wall: seven stacks of two, face down; the dora indicators turned up
  for (let i = 0; i < 14; i++){
    const stack = i >> 1, top = i & 1, x = -5.7503 + stack * 1.9;
    const di = top ? stack - 2 : -1, c = di >= 0 ? (S.dora || [])[di] : null;
    makeTile(c || null, V(x, -0.6, top ? 1.44 : 0), 0, c ? "up" : "down", {where: "dora"});
  }
  // a hand's end: the call, then the game's results screen
  if (S.result && !(prev && prev.result)){
    const k = S.result.kind, w = (S.result.wins || [])[0];
    events.push([k, w ? w.seat : null]);
    const before = (prev || state).seats.map(x => x.score);
    setTimeout(() => { if (S === state || (S && S.result && sameHand(S, state))) showResult(state, before); },
               REDUCED ? 300 : 1400);
  }
  drawHud();
  applyTilts();
  if (FOLLOW && prev && S.turn !== prev.turn && S.turn != null && !S.result) focusSeat(disp(S.turn));
  events.forEach(([k, s]) => announce(k, s));
  window.__jan3d.frames++;
  dirty = true;
}

// ---- the game's calls ----
const CALL = {pon: "Pon", chi: "Chi", kan: "Kan", riichi: "Riichi", ron: "Ron", tsumo: "Tsumo", draw: "Draw"};
function announce(kind, s){
  const k = /kan/.test(kind) ? "kan" : kind;
  if (!CALL[k]) return;
  const el = document.createElement("div");
  el.className = "call";
  const img = document.createElement("img");
  img.src = "art/call-" + k + ".png"; img.alt = CALL[k]; img.draggable = false;
  img.style.width = k === "ron" || k === "tsumo" ? "clamp(240px,30vw,420px)" : "clamp(190px,22vw,320px)";
  el.appendChild(img);
  let x = innerWidth / 2, y = innerHeight / 2;
  if (s != null){
    const p = disp(s), [px, pz] = POND[p], d = DIR[p], o = OWN[p];
    const q = V(px + d[0] * 4.6 + o[0] * 2.5, pz + d[1] * 4.6 + o[1] * 2.5, 2).project(camera);
    x = clamp((q.x + 1) / 2 * innerWidth, 140, innerWidth - 140);
    y = clamp((1 - q.y) / 2 * innerHeight, 110, innerHeight - 120);
  }
  el.style.setProperty("--x", x + "px"); el.style.setProperty("--y", y + "px");
  el.addEventListener("animationend", () => el.remove());
  setTimeout(() => el.remove(), 1700);            // even where animationend never fires
  document.body.appendChild(el);
}

// ---- the HUD ----
const fmt = n => n == null ? "" : Number(n).toLocaleString("en-US");
const WIND = {E: "East", S: "South", W: "West", N: "North"};
const cards = [0, 1, 2, 3].map(() => {
  const el = document.createElement("div");
  el.className = "seat"; el.tabIndex = 0; el.setAttribute("role", "button"); el.hidden = true;
  $("#seats").appendChild(el);
  return el;
});
// COM seats: a random ACKY Gallery portrait, as the game itself does -- steady for one game
function comFace(s){
  let x = (((+S.game || 0) * 2654435761) ^ ((+S.room || 0) * 73856093) ^ ((+S.table || 0) * 19349663) ^ 0x5bd1e995) >>> 0 || 1;
  const rnd = () => { x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0; return x / 4294967296; };
  const order = [...Array(16).keys()];
  for (let i = 15; i > 0; i--){ const j = Math.floor(rnd() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
  return order[s];
}
// a seat's portrait: its player's PlayOnline face, a COM's ACKY Gallery one, or
// PlayOnline's own blank (face 0) for a player who never picked one
function portrait(pf, seat, s){
  const id = seat.face != null ? seat.face : seat.bot && !seat.left ? ACKY + comFace(s) : 0;
  // an embedding page (the phone app) may paint faces itself: it has no
  // face.png route, only the sheets
  if (window.__janFace) return window.__janFace(pf, id);
  pf.style.backgroundImage = "url(" + faceUrl(id) + ")";
}
function drawHud(){
  const r = S.round || {}, room = +S.room || 0;
  $("#where").textContent = "JongHoLow" + (room ? ", Room " + Math.floor(room / 100) + "-" + (room % 100) : "") +
                            (S.table ? ", Table " + S.table : "");
  const R = $("#round"); R.innerHTML = "";
  if (r.wind){
    const rw = document.createElement("div"); rw.className = "rw";
    rw.textContent = (WIND[r.wind] || r.wind) + " " + (r.number || "");
    const meta = document.createElement("div"); meta.className = "meta";
    meta.innerHTML = "<span><b>" + (r.honba || 0) + "</b> honba</span><span class=\"opt\"><b>" + (r.sticks || 0) +
                     "</b> riichi stick" + (r.sticks === 1 ? "" : "s") + "</span>" +
                     (S.wall != null ? "<span><b>" + S.wall + "</b> tiles left</span>" : "");
    R.append(rw, meta);
    if ((S.dora || []).length){
      const d = document.createElement("div"); d.className = "dora"; d.textContent = "Dora";
      for (const c of S.dora){ const f = document.createElement("span"); f.className = "face"; faceCss(f, c, 21, 28);
        f.title = "Indicator " + tileName(c).name + ", so the dora is " + tileName(doraFrom(c)).name; d.appendChild(f); }
      R.appendChild(d);
    }
  }
  (S.seats || []).forEach((seat, s) => {
    const p = disp(s), el = cards[p];
    el.hidden = false;
    el.className = "seat" + (S.turn === s && !S.result ? " turn" : "") + (FOCUS === p && !FREE ? " focus" : "");
    el.innerHTML = "";
    el.setAttribute("aria-label", "Zoom in on " + seatName(s));
    const pf = document.createElement("div"); pf.className = "pf"; portrait(pf, seat, s);
    const nm = document.createElement("div"); nm.className = "nm";
    const w = document.createElement("span"); w.className = "w" + (r.dealer === s ? " dealer" : "");
    w.textContent = r.dealer != null ? "ESWN"[(s - r.dealer + 4) % 4] : "";
    const n = document.createElement("span"); n.className = "n"; n.textContent = seatName(s);
    nm.append(w, n);
    const r2 = document.createElement("div"); r2.className = "row2";
    const sc = document.createElement("span"); sc.textContent = fmt(seat.score); r2.appendChild(sc);
    const chip = (cls, text, title) => { const c = document.createElement("span"); c.className = "chip " + cls; c.textContent = text; if (title) c.title = title; r2.appendChild(c); };
    if (r.dealer === s) chip("deal", "Dealer");
    if (seat.riichi) chip("riichi", "Riichi");
    if (seat.left) chip("cpu", "CPU", seatName(s) + " left; the computer plays this seat");
    el.append(pf, nm, r2);
    el.onclick = () => { FOLLOW = false; focusSeat(FOCUS === p && !FREE ? null : p); };
    el.onkeydown = e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); el.onclick(); } };
  });
  drawBar();
}
function drawBar(){
  const bar = $("#bar"); bar.innerHTML = "";
  if (!S) return;
  const btn = (label, key, pressed, fn, title) => {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (key){ const k = document.createElement("kbd"); k.textContent = key; b.appendChild(k); }
    b.setAttribute("aria-pressed", pressed ? "true" : "false"); if (title) b.title = title;
    b.onclick = fn; bar.appendChild(b); return b;
  };
  const sep = () => { const d = document.createElement("span"); d.className = "sep"; bar.appendChild(d); };
  btn("Whole table", "Esc", FOCUS === null && !FOLLOW && !FREE, () => { FOLLOW = false; focusSeat(null); });
  sep();
  for (let p = 0; p < 4; p++){
    const s = (p + (VIEW || 0)) % 4;
    btn(seatName(s), String(p + 1), FOCUS === p && !FOLLOW && !FREE, () => { FOLLOW = false; focusSeat(p); }, "Zoom in on " + seatName(s));
  }
  sep();
  btn("Follow the turn", "F", FOLLOW, () => { FOLLOW = !FOLLOW; focusSeat(FOLLOW && S.turn != null ? disp(S.turn) : null); },
      "Keep the camera on whoever is playing");
}
function overviewPose(){
  const aspect = innerWidth / innerHeight, tgt = V(0, 2.2, 0), el = 1.02;
  const dir = V(0, Math.sin(el), Math.cos(el));      // V(x, z, y): up 31 degrees, behind the watcher's seat
  const corners = [V(-27, 27), V(27, 27), V(-27, -27), V(27, -27), V(0, 21, 3)];
  camera.aspect = aspect; camera.updateProjectionMatrix();
  const save = camera.position.clone(), saveQ = camera.quaternion.clone();
  let d = 60;
  for (; d < 400; d += 2){
    camera.position.copy(tgt).addScaledVector(dir, d); camera.lookAt(tgt); camera.updateMatrixWorld();
    if (corners.every(c => { const q = c.clone().project(camera); return Math.abs(q.x) < 0.9 && q.y < 0.8 && q.y > -0.78; })) break;
  }
  const pos = camera.position.clone();
  camera.position.copy(save); camera.quaternion.copy(saveQ); camera.updateMatrixWorld();
  return {pos, tgt};
}
function seatPose(p){
  const aspect = innerWidth / innerHeight, o = OWN[p], k = Math.max(1, 1.45 / aspect);
  const tgt = V(o[0] * 12.5, o[1] * 12.5, 1.2);
  return {pos: V(o[0] * (12.5 + 38 * k), o[1] * (12.5 + 38 * k), 30 * k), tgt};
}
function pose(){ return FOCUS === null ? overviewPose() : seatPose(FOCUS); }
function focusSeat(p, instant){
  FOCUS = p; FREE = false;
  for (let i = 0; i < 4; i++) tiltGoal[i] = i === p ? TILT_FOCUS : TILT_OPEN;
  cam.from = {pos: camera.position.clone(), tgt: controls.target.clone()}; cam.to = pose();
  cam.t0 = performance.now(); cam.dur = instant || REDUCED ? 0 : 750;
  if (S) drawHud();
  dirty = true;
}
function applyTilts(){
  for (let p = 0; p < 4; p++){
    for (const pv of hands[p]) pv.rotation.x = -tilt[p];
    // a hidden tile stands UPRIGHT, its face towards its owner, so every other
    // view sees only its back (laid open like a spectator's hand it would show
    // the atlas's first face, the 1-character); zoomed on its seat it is gone
    for (const pv of backs[p]){ pv.rotation.x = 0; pv.visible = FOCUS !== p; }
  }
}
const ease = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const easeOut = t => 1 - Math.pow(1 - t, 3);

// ---- cards follow their seats, from any angle ----
function placeCards(){
  if (!S) return;
  const w = innerWidth, h = innerHeight, c = V(0, 0).project(camera);
  const cx = (c.x + 1) / 2 * w, cy = (1 - c.y) / 2 * h;
  (S.seats || []).forEach((seat, s) => {
    const p = disp(s), o = OWN[p], el = cards[p];
    const a = V(o[0] * 24.5, o[1] * 24.5).project(camera);
    const ax = (a.x + 1) / 2 * w, ay = (1 - a.y) / 2 * h;
    let dx = ax - cx, dy = ay - cy; const len = Math.hypot(dx, dy) || 1; dx /= len; dy /= len;
    const cw = el.offsetWidth, ch = el.offsetHeight;
    let x = ax + dx * (cw / 2 + 10) - cw / 2, y = ay + dy * (ch / 2 + 10) - ch / 2;
    x = clamp(x, 10, w - cw - 10);
    y = clamp(y, 58, h - ch - 64);
    el.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
  });
}

// ---- hover: what a tile is ----
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
let hovered = null, mouseAt = null, downAt = null;
canvas.addEventListener("pointermove", e => { mouseAt = [e.clientX, e.clientY]; if (!e.buttons) pick(); });
canvas.addEventListener("pointerleave", () => { mouseAt = null; pick(); });
canvas.addEventListener("pointerdown", e => { downAt = [e.clientX, e.clientY]; $("#tip").hidden = true; });
function pick(){
  let hit = null;
  if (mouseAt && S){
    mouse.set(mouseAt[0] / innerWidth * 2 - 1, -(mouseAt[1] / innerHeight) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hits = ray.intersectObjects(pickable, false);
    hit = hits.length ? hits[0].object : null;
  }
  if (hit !== hovered){
    if (hovered) hovered.material = hovered.userData.base || tileMat;
    hovered = hit;
    if (hovered) hovered.material = hoverMat;
    dirty = true;
  }
  canvas.classList.toggle("hot", !!hit);
  const tip = $("#tip");
  if (!hit){ tip.hidden = true; return; }
  const u = hit.userData, nm = tileName(u.code);
  tip.innerHTML = "";
  const f = document.createElement("span"); f.className = "face"; faceCss(f, u.code, 36, 48);
  const tx = document.createElement("div");
  const t1 = document.createElement("div"); t1.className = "t1"; t1.textContent = nm.name;
  const sm = document.createElement("small"); sm.textContent = nm.jp; t1.appendChild(sm);
  const t2 = document.createElement("div"); t2.className = "t2";
  const who = u.owner != null ? seatName(u.owner) : "";
  t2.textContent = u.where === "hand" ? (u.drawn ? "Just drawn by " : "In the hand of ") + who
    : u.where === "pond" ? "Discard " + u.turn + " by " + who + (u.riichi ? ", declaring riichi" : "") + (u.last ? ", the latest" : "")
    : u.where === "meld" ? (u.called && u.from >= 0 ? "Called from " + seatName(u.from) + " for " : "Part of ") + who + "'s " + (u.meld || "meld")
    : "Dora indicator, so the dora is " + tileName(doraFrom(u.code)).name;
  tx.append(t1, t2); tip.append(f, tx);
  tip.hidden = false;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = mouseAt[0] + 16, y = mouseAt[1] + 18;
  if (x + tw > innerWidth - 8) x = mouseAt[0] - tw - 12;
  if (y + th > innerHeight - 8) y = mouseAt[1] - th - 12;
  tip.style.transform = "translate(" + x + "px," + y + "px)";
}
canvas.addEventListener("click", e => {
  if (!S) return;
  if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;   // that was a drag
  pick();
  if (hovered && hovered.userData.owner != null){
    const p = disp(hovered.userData.owner); FOLLOW = false; focusSeat(p === FOCUS && !FREE ? null : p);
  } else if (FOCUS !== null || FREE){ FOLLOW = false; focusSeat(null); }
});
addEventListener("keydown", e => {
  if (!S) return;
  if (e.target.closest && e.target.closest("button,a") && e.key !== "Escape") return;
  if (e.key === "Escape"){ FOLLOW = false; $("#card").hidden = true; $("#results").hidden = true; focusSeat(null); }
  else if (e.key.length === 1 && "1234".includes(e.key)){ FOLLOW = false; focusSeat(+e.key - 1); }
  else if (e.key === "f" || e.key === "F"){ FOLLOW = !FOLLOW; focusSeat(FOLLOW && S.turn != null ? disp(S.turn) : null); }
});

// ---- the game's results screen: each player at their own side, the points
// rolling from before to after in steps of 100, the call in the middle ----
function yakuName(y){ return String(y).replace(/_/g, " ").replace(/\b\w/g, m => m.toUpperCase()); }
function showResult(st, before){
  const res = st.result || {}, wins = res.wins || [], w = wins[0], box = $("#results");
  document.querySelectorAll(".call").forEach(e => e.remove());
  box.innerHTML = "";
  const add = (parent, tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; parent.appendChild(e); return e; };
  const rolls = [], r = st.round || {};
  (st.seats || []).forEach((seat, s) => {
    const p = disp(s), pl = add(box, "div", "pl p" + p + (wins.some(x => x.seat === s) ? " win" : ""));
    portrait(add(pl, "div", "pf"), seat, s);
    const who = add(pl, "div", "who");
    add(who, "span", "w", r.dealer != null ? "ESWN"[(s - r.dealer + 4) % 4] : "");
    add(who, "span", "n", seatName(s));
    const nums = add(pl, "div", "nums");
    add(nums, "span", "", "Before"); add(nums, "span", "", fmt(before[s]));
    add(nums, "span", "", "Change"); const d = add(nums, "span", "d", "0");
    add(nums, "span", "", "Now"); const n = add(nums, "span", "now", fmt(before[s]));
    rolls.push({d, n, b: before[s] || 0, a: seat.score || 0});
  });
  const mid = add(box, "div", "mid");
  if (res.kind === "ron" || res.kind === "tsumo" || res.kind === "draw"){
    const img = add(mid, "img"); img.src = "art/call-" + res.kind + ".png"; img.alt = CALL[res.kind];
  }
  if (w){
    const names = wins.map(x => seatName(x.seat));
    add(mid, "h2", "", names.join(" and ") + (res.kind === "ron" ? " win" + (names.length > 1 ? "" : "s") + " by ron" : " wins by tsumo"));
    for (const x of wins){
      add(mid, "div", "yk", (wins.length > 1 ? seatName(x.seat) + ": " : "") + (x.yaku || []).map(y => yakuName(y[0])).join(", "));
      add(mid, "div", "pts", (x.limit ? x.limit + ", " : "") + (x.han ? x.han + " han, " : "") + fmt(x.points) + " points");
    }
  } else if (res.kind === "draw"){
    add(mid, "h2", "", "Draw"); add(mid, "div", "yk", "The wall ran out.");
  } else {
    add(mid, "h2", "", "Hand aborted"); add(mid, "div", "yk", yakuName(res.kind || ""));
  }
  const b = add(mid, "button", "", "Back to the table"); b.type = "button";
  b.onclick = () => { box.hidden = true; };
  box.hidden = false;
  const t0 = performance.now() + (REDUCED ? 0 : 900), dur = REDUCED ? 0 : 1900;
  const tick = now => {
    const k = dur ? Math.max(0, Math.min(1, (now - t0) / dur)) : 1, e = easeOut(k);
    for (const x of rolls){
      const dv = Math.round((x.a - x.b) * e / 100) * 100;
      x.d.textContent = dv > 0 ? "+" + fmt(dv) : dv < 0 ? "−" + fmt(-dv) : "0";
      x.d.className = "d" + (dv > 0 ? " up" : dv < 0 ? " down" : "");
      x.n.textContent = fmt(x.b + dv);
    }
    if (k < 1 && !box.hidden) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
// ---- the end of a game: over, or stopped early because the last human left ----
function standings(st){ return (st.seats || []).map((x, s) => [seatName(s), x.score]).sort((a, b) => b[1] - a[1]); }
function endCard(st){
  const card = $("#card"); card.innerHTML = "";
  const early = st.ended && st.ended.early;
  const h = document.createElement("h2"); h.textContent = early ? "Game ended early" : "Game over"; card.appendChild(h);
  if (early){
    const p = document.createElement("p");
    p.textContent = (st.ended.name || "The last player") + (st.ended.why === "left" ? " left the table" : " dropped out") +
                    ", so the game stopped here.";
    card.appendChild(p);
  }
  const ol = document.createElement("ol");
  standings(st).forEach(([n, sc], i) => {
    const li = document.createElement("li");
    li.innerHTML = "<span>" + (i + 1) + "</span><span></span><span>" + fmt(sc) + "</span>";
    li.children[1].textContent = n; ol.appendChild(li);
  });
  card.appendChild(ol);
  const acts = document.createElement("div"); acts.className = "acts";
  const b = document.createElement("button"); b.type = "button"; b.textContent = "Look at the table";
  b.onclick = () => { card.hidden = true; };
  const a = document.createElement("a"); a.href = "./"; a.textContent = "Rankings";
  acts.append(b, a); card.appendChild(acts);
  card.hidden = false;
}

// ---- following the table ----
function sameHand(a, b){
  if (!a || !b) return false;
  const x = a.round || {}, y = b.round || {};
  return a.game === b.game && x.wind === y.wind && x.number === y.number && x.honba === y.honba && x.dealer === y.dealer;
}
let lastSig = "", ENDED = false;
function show(st){
  if (!!st.hints !== HINTS) setHints(!!st.hints);
  const prev = S && sameHand(S, st) ? S : null;
  if (!prev) $("#results").hidden = true;             // a new hand: the last one's screen goes
  build(st, prev);
  if (!ENDED && (st.ended || (st.state === "over" && !st.result))){
    ENDED = true;
    setTimeout(() => endCard(st), st.result && !REDUCED ? 5200 : 0);
  }
}
async function poll(){
  let wait = 1000;
  try {
    const r = await fetch("watch.json?t=" + encodeURIComponent(ID), {cache: "no-store"});
    const got = r.ok ? await r.json() : null;
    if (got && !got.state){
      if (ENDED){ $("#lag").textContent = "The game is over"; return; }     // keep the end card up
      gone("This table is not being shown: the game is over, or its players no longer allow watching.");
      wait = 5000;
    } else if (got){
      $("#gone").hidden = true;
      $("#lag").textContent = Math.round(got.delay) + " s behind the table";
      const sig = JSON.stringify(got.state);
      if (sig !== lastSig){ lastSig = sig; show(got.state); }
    }
  } catch (e) {}
  setTimeout(poll, wait);
}

// ---- the loop ----
function frame(now){
  requestAnimationFrame(frame);
  let moving = false;
  if (cam.to){
    const k = cam.dur ? Math.min(1, (now - cam.t0) / cam.dur) : 1, e = ease(k);
    camera.position.lerpVectors(cam.from.pos, cam.to.pos, e);
    controls.target.lerpVectors(cam.from.tgt, cam.to.tgt, e);
    camera.lookAt(controls.target);
    if (k >= 1) cam.to = null;
    moving = true;
  } else if (controls.update()) moving = true;
  for (let i = 0; i < 4; i++){
    const g = tiltGoal[i], d = g - tilt[i];
    if (Math.abs(d) > 1e-3){ tilt[i] += REDUCED ? d : d * 0.14; moving = true; } else tilt[i] = g;
  }
  if (anims.length){
    anims = anims.filter(a => {
      const k = Math.max(0, Math.min(1, (now - a.t0) / a.dur)), e = easeOut(k);
      if (a.mesh) a.mesh.rotation.x = a.x0 + (a.x1 - a.x0) * ease(Math.min(1, k * 1.15));
      a.pivot.position.lerpVectors(a.from, a.to, e);
      a.pivot.position.y += a.arc * Math.sin(Math.PI * k);
      a.pivot.rotation.y = a.rot + a.spin * (1 - e);
      return k < 1;
    });
    moving = true;
  }
  // a call is open on the last discard (S.offer, the phone app's table): it
  // pulses, as the PS2 blinks the tile you may claim
  if (S && S.offer){
    const k = REDUCED ? 1 : 0.5 + 0.5 * Math.sin(now / 150);
    lastMat.emissive.setRGB(0.25 + 0.75 * k, 0.16 + 0.5 * k, 0.04 + 0.12 * k);
    lastMat.userData.pulsed = true; moving = true;
  } else if (lastMat.userData.pulsed){ lastMat.emissive.setHex(0x5a3a10); lastMat.userData.pulsed = false; dirty = true; }
  if (!moving && !dirty) return;
  applyTilts();
  renderer.render(scene, camera);
  placeCards();
  dirty = false;
}
function resize(){
  renderer.setSize(innerWidth, innerHeight, false);
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  if (!FREE){ const p = pose(); camera.position.copy(p.pos); controls.target.copy(p.tgt); camera.lookAt(p.tgt); cam.to = null; }
  dirty = true;
}

// for the board's browser test: how the page is doing, and where a tile is on screen
window.__jan3d = {ok: false, frames: 0, tiles: () => pickable.length, focus: () => FOCUS,
  tileAt(i){ const v = new THREE.Vector3(); pickable[i].getWorldPosition(v); v.project(camera);
             return [(v.x + 1) / 2 * innerWidth, (1 - v.y) / 2 * innerHeight]; }};
addEventListener("hashchange", () => location.reload());
if (!renderer) gone("This view needs WebGL, and this browser did not offer it. Turn on hardware acceleration, or try another browser.");
else if (!ID) gone("No table was chosen.");
else fetch("art/w3d.json", {cache: "no-cache"}).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(d => {
  D = d; setup(); resize();
  addEventListener("resize", resize);
  window.__jan3d.ok = true;
  requestAnimationFrame(frame);
  poll();
}).catch(() => gone("The table's pieces did not load. Try again in a moment."));
</script></body></html>
"""
