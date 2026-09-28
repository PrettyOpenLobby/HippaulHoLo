#!/usr/bin/env python3
"""polboards.py -- the LIVE BOARDS service: each game's standings as a page
that looks like the game (2026-09-12).

The shape decided for it:
  * its OWN container, reading /data and the account database READ-ONLY --
    never inside a game server, so a bug in a leaderboard can never touch a
    live session, and nothing here ever writes game data;
  * ONE SUBDOMAIN PER GAME -- jan.example.com, tm.example.com,
    fmo.example.com -- so each board listens on its OWN port and the
    server owner's cloudflared maps each hostname to one;
  * each looks like its game, drawn from that client's own art.

A board is a module with: NAME, TITLE, PAGE, ART_DIR, art_files(),
cached_snapshot(args), and for Discord discord_message(snap, args) (+
optionally discord_events(prev, snap)). A board with more than one Discord
message names the extra FEEDS below; feed "auction" of board "tm" is
discord_auction_message / discord_auction_events, keyed on snap["auction_sig"],
with its own --tm-auction-discord-webhook and its own message id. A feed
whose board has discord_<feed>_messages (plural) posts a SET of messages
(DiscordSet: one per list, one per listing), not one. This file
owns the sockets and the webhooks. The ONE thing it writes is each webhook's
message id and where each feed posts, never game data: with a database
configured (POL_DATABASE_URL, as in the compose stack) that is the
`jan_board_state` table, one row per former state file, through janstore; with
none, the files in $POL_BOARDS_STATE_DIR as before (a checkout without a
database). An explicit --<feed>-discord-state path is always a file.

    python polboards.py --jan-port 8791 --fmo-port 8792 --tm-port 8793 [--bind 0.0.0.0] [--jan-discord-webhook URL]
"""
import argparse
import hashlib
import importlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import janstore

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# the bot's Gateway session (its "Watching N players online"). Imported at
# module level on purpose: a tool that walks imports to decide what to restart
# cannot see a lazy import inside a function.
import polgateway

#: board name -> module. Each gets --<name>-port; 0 leaves it off.
BOARDS = {"jan": "boardjan", "fmo": "boardfmo", "tm": "boardtm",
          "ffxi": "boardffxi", "doc": "boarddoc", "fe": "boardfe"}
if False:                                             # pragma: no cover
    # NEVER RUNS. Boards load BY NAME (importlib), which deploy/pol-stale-check's
    # ast import closure cannot see -- the femap lesson of the same day: a
    # board-only push would restart nothing. KEEP THIS == BOARDS.
    import boardjan  # noqa: F401
    import boardfmo  # noqa: F401
    import boardtm  # noqa: F401
    import boardffxi  # noqa: F401
    import boarddoc  # noqa: F401
    import boardfe  # noqa: F401

#: board name -> its Discord messages beyond the main one. Each feed is a
#: separate webhook (Tetra Master was given one channel for the
#: rankings and one for the auction, 2026-09-12), a separate message edited in
#: place, and a separate message-id file.
FEEDS = {"tm": ("auction", "live"), "jan": ("live",), "ffxi": ("conquest", "auction")}
#: feeds that post into their board's own channel when they have no webhook of
#: their own: the Jan "live" feed (a post per watchable table, 2026-09-13)
#: belongs next to the Jan board.
FEED_SHARES_MAIN = frozenset({"jan_live", "tm_live", "ffxi_conquest", "ffxi_auction"})


def feed_keys(name):
    """(feed, key) for every Discord message a board can post: ("", "tm")
    for the main one, ("auction", "tm_auction") for an extra feed. The key
    names the flags (--tm-auction-discord-webhook), the env
    (POL_BOARDS_TM_AUCTION_DISCORD_WEBHOOK) and the state file."""
    return [("", name)] + [(f, "%s_%s" % (name, f)) for f in FEEDS.get(name, ())]


def feed_fn(board, feed, what):
    """The board's discord_message / discord_events for a feed, or None."""
    return getattr(board, "discord_%s%s" % ("%s_" % feed if feed else "", what), None)


class _Handler(BaseHTTPRequestHandler):
    server_version = "polboards/1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype, cache="no-store", headers=None):
        """`headers`: a board route's optional 5th element, extra response
        headers (boardtm's face lookups add Access-Control-Allow-Origin)."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        """A Discord button click (POST /discord/interactions), for a board
        with discord_interaction and a bot public key. Unsigned or
        mis-signed = 401, which is also what Discord's own endpoint check
        insists on; a PING is answered here."""
        board, args = self.server.board, self.server.board_args
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/pol/"):
            return self._forward_bridge(path[len("/pol"):])
        fn = getattr(board, "discord_interaction", None)
        # NOT gated on discord_interaction. That is the handler for the board's
        # own BUTTONS, and a board can have none (City Control, Dirge and FFXI
        # post a link button, which Discord never calls back for) and still
        # need this endpoint: /<board>board arrives here too, and without it
        # the bot could never be told which channel to post in.
        if path != "/discord/interactions":
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 1 << 20:
            return self._send(413, "too large", "text/plain")
        body = self.rfile.read(n)
        key = (getattr(args, "%s_discord_public_key" % board.NAME, "") or "").strip()
        try:
            ok = bool(key) and ed25519_verify(
                bytes.fromhex(key),
                (self.headers.get("X-Signature-Timestamp") or "").encode() + body,
                bytes.fromhex(self.headers.get("X-Signature-Ed25519") or ""))
        except ValueError:
            ok = False
        if not ok:
            return self._send(401, "invalid request signature", "text/plain")
        try:
            data = json.loads(body)
        except ValueError:
            return self._send(400, "bad json", "text/plain")
        if data.get("type") == 1:
            resp = {"type": 1}
        elif data.get("type") == 2 and (data.get("data") or {}).get("name") == "%sboard" % board.NAME:
            resp = handle_command(board, board.NAME, data)
        elif fn is None:
            # a board with no buttons of its own was clicked anyway
            resp = {"type": 4, "data": {"flags": 64, "allowed_mentions": {"parse": []},
                                        "content": "This board has no buttons."}}
        else:
            try:
                resp = fn(data, args)
            except Exception as e:                     # noqa: BLE001
                print("[polboards] %s interaction failed (%s)" % (board.NAME, e), flush=True)
                resp = {"type": 4, "data": {"flags": 64, "content": "The board could not "
                                            "answer that just now.",
                                            "allowed_mentions": {"parse": []}}}
        return self._send(200, json.dumps(resp), "application/json")

    def _forward_bridge(self, rest):
        """POST /pol/<rest> -> the PlayOnline Discord bridge (polbridge.py) on
        loopback. The bridge was given tm.example.com rather than a
        hostname of its own (2026-09-13), so its interactions arrive HERE and
        are passed on byte for byte with the signature headers: the bridge
        checks them against its own app's key, never this board's."""
        base = (os.environ.get("POL_BOARDS_BRIDGE_URL") or "").strip().rstrip("/")
        if not base:
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= 1 << 20:
            return self._send(413, "too large", "text/plain")
        req = urllib.request.Request(base + rest, data=self.rfile.read(n), method="POST")
        for h in ("Content-Type", "X-Signature-Ed25519", "X-Signature-Timestamp"):
            if self.headers.get(h):
                req.add_header(h, self.headers.get(h))
        try:
            # under Discord's 3 s answer window, with room for the tunnel
            with urllib.request.urlopen(req, timeout=2.5) as r:
                return self._send(r.status, r.read(),
                                  r.headers.get("Content-Type") or "application/json")
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read() or b"", "text/plain")
        except (urllib.error.URLError, OSError) as e:
            print("[polboards] bridge unreachable (%s)" % e, flush=True)
            return self._send(502, "bridge unavailable", "text/plain")

    def do_GET(self):
        board, args = self.server.board, self.server.board_args
        u = urllib.parse.urlparse(self.path)
        path = u.path
        route = getattr(board, "route", None)
        if route is not None:
            # a board's own routes (the Jan board's /render.png); never let one
            # take the server down
            try:
                r = route(path, urllib.parse.parse_qs(u.query), args)
            except Exception as e:                     # noqa: BLE001
                r = (500, "%s: %s" % (type(e).__name__, e), "text/plain", "no-store")
            if r is not None:
                return self._send(*r)
        if path in ("/", "/index.html"):
            return self._send(200, board.PAGE, "text/html; charset=utf-8")
        if path == "/state.json":
            try:
                snap = board.cached_snapshot(args)
            except Exception as e:                     # never take the server down
                return self._send(500, json.dumps({"error": "%s: %s" % (
                    type(e).__name__, e)}), "application/json")
            return self._send(200, json.dumps(snap, separators=(",", ":")),
                              "application/json; charset=utf-8")
        if path.startswith("/art/"):
            name = path[5:]
            if name not in board.art_files():
                return self._send(404, "no such art", "text/plain")
            try:
                with open(os.path.join(board.ART_DIR, name), "rb") as fh:
                    blob = fh.read()
            except OSError:
                return self._send(404, "missing", "text/plain")
            ctype = {".png": "image/png", ".woff2": "font/woff2", ".woff": "font/woff",
                     ".json": "application/json; charset=utf-8",
                     ".js": "text/javascript; charset=utf-8"}.get(
                os.path.splitext(name)[1].lower(), "application/octet-stream")
            # board.json is the art's INDEX: a day-cached copy from before a
            # re-bake hides the new art (TM 2026-09-13: "the watching art is
            # not installed" -- the browser kept the pre-watch board.json).
            # Revalidate it every time; the images it names can stay cached.
            return self._send(200, blob, ctype, "no-cache" if name.endswith(".json")
                              else "public, max-age=86400")
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        return self._send(404, "not found", "text/plain")


# ---------------------------------------------------------------------------
# Discord -- one message per board, edited in place. The FE map's pattern
# (services/femap.py), as asked for there: the message id
# survives a restart (no duplicate boards), a message someone deleted is
# posted again, a 429 is waited out, and the short status posts delete
# themselves after --discord-event-ttl so the board stays the thing in view.
# ---------------------------------------------------------------------------
_UA = "DiscordBot (https://playonline.invalid/polboards, 1) polboards"


def multipart(payload, files):
    """payload_json plus files[i], each (filename, content type, bytes)."""
    boundary = "polboards" + uuid.uuid4().hex
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
              "Content-Type: application/json\r\n\r\n" % boundary).encode()
             + json.dumps(payload).encode("utf-8") + b"\r\n"]
    for i, (fn, ctype, blob) in enumerate(files):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files[%d]\"; "
                      "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                      % (boundary, i, fn, ctype)).encode() + blob + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


def _brief(data):
    return json.dumps(data)[:160]


#: Discord bookkeeping in the database: state_path() hands out "db:<name>"
#: for it, and the readers and writers below go to this table instead of a file.
STATE_TABLE = "jan_board_state"
_DB_PREFIX = "db:"


def _state_db():
    """True when the bookkeeping goes to the database (POL_DATABASE_URL set)."""
    return bool(os.environ.get("POL_DATABASE_URL", "").strip())


def _state_errors():
    return (OSError, ValueError, AttributeError, TypeError) + janstore.errors()


def state_read(path):
    """The JSON document at `path`: a "db:<name>" row of STATE_TABLE, or a
    file. {} when there is none. Raises what reading it raises."""
    if isinstance(path, str) and path.startswith(_DB_PREFIX):
        janstore.ensure_schema()
        row = janstore.db.query_one(
            "SELECT data FROM jan_board_state WHERE name = %s", (path[len(_DB_PREFIX):],))
        return dict(row["data"]) if row and isinstance(row["data"], dict) else {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh) or {}


def state_write(path, data):
    """Replace the document at `path` (see state_read). Raises on failure."""
    if isinstance(path, str) and path.startswith(_DB_PREFIX):
        janstore.ensure_schema()
        janstore.db.execute(
            "INSERT INTO jan_board_state (name, data) VALUES (%s, %s::jsonb)"
            " ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (path[len(_DB_PREFIX):], janstore.jsonb(data)))
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class Discord:
    """One board's webhook. Every network error is logged and survived; the
    URL is a secret and is never printed."""

    def __init__(self, name, url, state, every=60.0, ttl=600.0, refresh=1800.0,
                 opener=None, auth=None):
        self.name = name
        # a BOT token: `url` is then a channel (API/channels/<id>), not a
        # webhook, and every call carries it (run_bot_feed). A SECRET.
        self.auth = (auth or "").strip() or None
        self.url = (url or "").strip().rstrip("/")
        self.path = state
        self.every = max(10.0, float(every or 60))
        self.refresh = max(60.0, float(refresh or 1800))
        self.ttl = max(0.0, float(ttl or 0))
        self.open = opener or urllib.request.urlopen
        self.events = []        # [{"id", "t"}] -- status posts still up
        self.swept = False      # the first sweep clears the last run's posts
        self.msg_id = self._load()
        self.last_sig = None
        self.last_edit = 0.0
        self.hold_until = 0.0

    def _log(self, text):
        print("[polboards] %s discord: %s" % (self.name, text), flush=True)

    def _hook(self):
        return hashlib.sha1(self.url.encode()).hexdigest()

    def _load(self):
        try:
            d = state_read(self.path)
            # the id belongs to ONE webhook; a new webhook starts fresh
            if d.get("hook") == self._hook():
                self.events = [{"id": str(e["id"]), "t": float(e.get("t") or 0)}
                               for e in (d.get("events") or []) if e.get("id")]
                return str(d.get("message_id") or "") or None
        except _state_errors():
            pass
        return None

    def _save(self):
        if not self.path or self.path == os.devnull:
            return
        try:
            state_write(self.path, {"hook": self._hook(), "message_id": self.msg_id,
                                    "events": self.events})
        except _state_errors() as e:
            self._log("could not save the message id (%s) -- a restart will "
                      "post a second board" % e)

    def _call(self, method, url, body=None, ctype=None):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", _UA)
        if self.auth:
            req.add_header("Authorization", "Bot %s" % self.auth)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with self.open(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:                          # noqa: BLE001
                pass
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"raw": raw[:200].decode("utf-8", "replace")}
            return e.code, data
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"error": str(e)}

    def _refused(self, what, status, data):
        """A post or edit Discord refused. 400/401/403 are the server owner's to
        fix (a bot without permission in the channel, a bad token), so the
        feed waits a minute instead of retrying every poll -- a flood of the
        same line otherwise -- and says what a bot needs. Anything else is
        tried again at once."""
        self._log("%s failed (%s %s)" % (what, status, _brief(data)))
        if status in (400, 401, 403):
            self.hold_until = time.time() + 60
            if status == 403 and self.auth:
                self._log("the bot may not post here: it must be IN the server and have "
                          "View Channel, Send Messages, Embed Links and Attach Files in "
                          "this channel -- trying again in a minute")

    def _post_url(self):
        """Where a new message goes: a webhook answers with the message only
        when asked (?wait=true); a bot posts to its channel's messages."""
        return self.url + ("/messages" if self.auth else "?wait=true")

    def _limited(self, status, data):
        if status != 429:
            return False
        try:
            self.hold_until = time.time() + float(data.get("retry_after", 5))
        except (TypeError, ValueError, AttributeError):
            self.hold_until = time.time() + 5
        self._log("rate limited, holding %.0f s" % (self.hold_until - time.time()))
        return True

    def tick(self, sig, build, now=None, force=False):
        """Edit (or first post) the board's message when `sig` changed -- at
        most once per --discord-every -- or every --discord-refresh so its
        timestamp stays honest. build() -> (payload, files). Returns what it did."""
        now = time.time() if now is None else now
        if not self.url or now < self.hold_until:
            return None
        self.sweep(now)
        if not force:
            if now - self.last_edit < self.every:
                return None
            if sig == self.last_sig and now - self.last_edit < self.refresh:
                return None
        payload, files = build()
        body, ctype = multipart(payload, files)
        did = None
        if self.msg_id:
            st, data = self._call("PATCH", "%s/messages/%s" % (self.url, self.msg_id),
                                  body, ctype)
            if st == 200:
                did = "edited"
            elif st == 404:
                self._log("the board message is gone -- posting a new one")
                self.msg_id = None
            elif self._limited(st, data):
                return "limited"
            else:
                self._refused("edit", st, data)
                return "failed"
        if not self.msg_id:
            st, data = self._call("POST", self._post_url(), body, ctype)
            if st in (200, 201) and data.get("id"):
                self.msg_id = str(data["id"])
                self._save()
                did = "posted"
                self._log("board message posted (id %s)" % self.msg_id)
            elif self._limited(st, data):
                return "limited"
            else:
                self._refused("post", st, data)
                return "failed"
        self.last_sig, self.last_edit = sig, now
        return did

    def _delete(self, mid):
        st, data = self._call("DELETE", "%s/messages/%s" % (self.url, mid))
        if st in (200, 204, 404):
            return "ok"
        if self._limited(st, data):
            return "limited"
        self._log("could not delete message %s (%s %s)" % (mid, st, _brief(data)))
        return None

    def _wipe(self, ids):
        """Delete these messages, waiting out rate limits. Returns how many went."""
        gone = 0
        for mid in ids:
            for _try in range(6):
                r = self._delete(mid)
                if r != "limited":
                    gone += r == "ok"
                    break
                time.sleep(max(1.0, min(60.0, self.hold_until - time.time())))
                self.hold_until = 0.0
        return gone

    def clear(self):
        """Delete the board message and any status posts, and forget them --
        for a feed that moves to another channel or to the bot."""
        gone = self._wipe(([self.msg_id] if self.msg_id else [])
                          + [e["id"] for e in self.events])
        self.msg_id, self.events = None, []
        self._save()
        return gone

    def say(self, text):
        """One short status post, with ?wait=true so its id comes back: that
        id is how sweep() deletes it later."""
        if not self.url or time.time() < self.hold_until:
            return False
        body = json.dumps({"content": text[:1900],
                           "allowed_mentions": {"parse": []}}).encode("utf-8")
        st, data = self._call("POST", self._post_url(), body, "application/json")
        if st in (200, 204):
            if data.get("id"):
                self.events.append({"id": str(data["id"]), "t": time.time()})
                self._save()
            return True
        if not self._limited(st, data):
            self._log("status post failed (%s %s)" % (st, _brief(data)))
        return False

    def sweep(self, now=None):
        """Delete status posts older than the TTL -- and, on the first call
        after a restart, every one the last run left behind. A 404 counts as
        done (someone already deleted it). Returns how many went."""
        now = time.time() if now is None else now
        if not self.url or now < self.hold_until:
            return 0
        first, self.swept = not self.swept, True
        doomed = [e for e in self.events
                  if first or (self.ttl > 0 and now - e["t"] >= self.ttl)]
        gone = 0
        for e in doomed:
            st, data = self._call("DELETE", "%s/messages/%s" % (self.url, e["id"]))
            if st in (200, 204, 404):
                self.events = [x for x in self.events if x["id"] != e["id"]]
                gone += 1
            elif self._limited(st, data):
                break
            else:
                self._log("could not delete message %s (%s %s)"
                          % (e["id"], st, _brief(data)))
        if gone:
            self._save()
            self._log("deleted %d %s status post(s)"
                      % (gone, "left-over" if first else "expired"))
        return gone


class DiscordSet(Discord):
    """One webhook, a SET of messages: one per slot the board names (the TM
    rankings: one per list; the TM auction: one per listing -- decided
    2026-09-13: "not a single super long post"). Each slot is posted once and
    edited in place when its sig changes (at most once per `every` seconds,
    except the edit that ENDS it). A slot that has ended, or has gone from the
    board, is edited to its final form and deleted `ended_ttl` seconds later;
    an ended slot that was deleted is not posted again unless it comes back
    to life (a relisted card is a new listing).

    State: {"hook", "messages": {slot: {"id", "sig", "t", "ended"}}, "done",
    "events"}. A state file from the single-message Discord (its
    "message_id") is migrated by deleting that old message on the first sweep.
    At most `per_tick` writes per tick, so a first run with many slots is
    spread over a few polls instead of tripping the webhook's rate limit."""

    def __init__(self, name, url, state, every=15.0, ttl=600.0, refresh=1800.0,
                 ended_ttl=600.0, per_tick=3, opener=None, auth=None):
        self.msgs, self.done = {}, set()
        super().__init__(name, url, state, 60, ttl, refresh, opener, auth=auth)
        self.every = max(1.0, float(every if every is not None else 15))
        self.ended_ttl = max(0.0, float(ended_ttl or 0))
        self.per_tick = max(1, int(per_tick))

    def _load(self):
        try:
            d = state_read(self.path)
            if d.get("hook") == self._hook():
                self.events = [{"id": str(e["id"]), "t": float(e.get("t") or 0)}
                               for e in (d.get("events") or []) if e.get("id")]
                if d.get("message_id"):
                    # the single board message this webhook carried before
                    self.events.append({"id": str(d["message_id"]), "t": 0.0})
                for slot, m in (d.get("messages") or {}).items():
                    if isinstance(m, dict) and m.get("id"):
                        self.msgs[str(slot)] = {"id": str(m["id"]), "sig": m.get("sig"),
                                                "t": float(m.get("t") or 0),
                                                "ended": m.get("ended")}
                self.done = set(str(s) for s in (d.get("done") or []))
        except _state_errors():
            pass
        return None

    def _save(self):
        if not self.path or self.path == os.devnull:
            return
        try:
            state_write(self.path, {"hook": self._hook(), "messages": self.msgs,
                                    "done": sorted(self.done), "events": self.events})
        except _state_errors() as e:
            self._log("could not save the message ids (%s) -- a restart will "
                      "post the set again" % e)

    def _post(self, payload, files):
        body, ctype = multipart(payload, files)
        st, data = self._call("POST", self._post_url(), body, ctype)
        if st in (200, 201) and data.get("id"):
            return str(data["id"])
        if self._limited(st, data):
            return "limited"
        self._refused("post", st, data)
        return None

    def _edit(self, mid, payload, files):
        body, ctype = multipart(payload, files)
        st, data = self._call("PATCH", "%s/messages/%s" % (self.url, mid), body, ctype)
        if st == 200:
            return "ok"
        if st == 404:
            return "gone"
        if self._limited(st, data):
            return "limited"
        self._refused("edit", st, data)
        return None

    def clear(self):
        """Delete every message this set put up (and the single message it
        carried before, migrated into events), waiting out rate limits; then
        forget them. Returns how many went. Used when a feed moves from its
        webhook to the bot, or to another channel, so no channel keeps both."""
        gone = self._wipe([m["id"] for m in self.msgs.values()]
                          + [e["id"] for e in self.events])
        self.msgs, self.events, self.done = {}, [], set()
        self._save()
        return gone

    def sync(self, slots, retire=None, now=None):
        """Bring the channel in line with `slots`, a list of {"slot", "sig",
        "ended", "build"} (build() -> (payload, files)). `retire(slot)` gives
        the final (payload, files) for a slot that has gone, or None. Returns
        [(what, slot)] for what it did."""
        now = time.time() if now is None else now
        if not self.url or now < self.hold_until:
            return []
        self.sweep(now)
        did, left, changed = [], [self.per_tick], [False]

        def spend(result, what, slot):
            left[0] -= 1
            if result == "limited":
                return False
            if result:
                did.append((what, slot))
                changed[0] = True
            return True

        seen = set()
        for s in slots:
            if left[0] <= 0 or now < self.hold_until:
                break
            slot, ended = str(s["slot"]), bool(s.get("ended"))
            seen.add(slot)
            if slot in self.done:
                if ended:
                    continue
                self.done.discard(slot)                   # alive again: a new post
                changed[0] = True
            m = self.msgs.get(slot)
            if m is None:
                if ended:
                    continue                  # never seen alive: nothing to announce
                mid = self._post(*s["build"]())
                if not spend(mid, "posted", slot):
                    break
                if mid:
                    self.msgs[slot] = {"id": mid, "sig": s["sig"], "t": now, "ended": None}
                continue
            if not ended and m.get("ended") is not None:
                m["ended"] = None                         # reopened (a relist)
                changed[0] = True
            stale = not ended and now - m["t"] >= self.refresh
            due = s["sig"] != m["sig"] and (ended or now - m["t"] >= self.every)
            if due or stale:
                r = self._edit(m["id"], *s["build"]())
                if not spend(r if r != "gone" else "ok", "edited" if r != "gone" else "lost", slot):
                    break
                if r == "gone":
                    self.msgs.pop(slot, None)             # someone deleted it: repost next tick
                    continue
                if r == "ok":
                    m["sig"], m["t"] = s["sig"], now
                else:
                    continue
            if ended and m.get("ended") is None and m["sig"] == s["sig"]:
                m["ended"] = now
                changed[0] = True
            if m.get("ended") is not None and now - m["ended"] >= self.ended_ttl and left[0] > 0:
                if not spend(self._delete(m["id"]), "deleted", slot):
                    break
                self.msgs.pop(slot, None)
                self.done.add(slot)
        for slot in [x for x in self.msgs if x not in seen]:
            if left[0] <= 0 or now < self.hold_until:
                break
            m = self.msgs[slot]
            if m.get("ended") is None:
                final = retire(slot) if retire else None
                if final:
                    r = self._edit(m["id"], *final)
                    if not spend(r, "final", slot):
                        break
                m["ended"] = now
                changed[0] = True
            elif now - m["ended"] >= self.ended_ttl:
                if not spend(self._delete(m["id"]), "deleted", slot):
                    break
                self.msgs.pop(slot, None)
        if changed[0]:
            self._save()
        return did


# ---------------------------------------------------------------------------
# A board's Discord BOT (asked for 2026-09-13): its messages can carry
# buttons, and a click comes back to POST /discord/interactions on the
# board's own page server, signed with Ed25519. The container has no crypto
# library, so the check is RFC 8032's own reference arithmetic, pinned by the
# RFC's test vectors in tools/tm_board_test.py.
# ---------------------------------------------------------------------------
DISCORD_API = "https://discord.com/api/v10"

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_x(y, sign):
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P:
        x = x * _ED_I % _ED_P
    if (x * x - x2) % _ED_P:
        return None
    return _ED_P - x if (x & 1) != sign else x


_ED_GY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_GX = _ed_x(_ED_GY, 0)
_ED_B = (_ED_GX, _ED_GY, 1, _ED_GX * _ED_GY % _ED_P)


def _ed_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _ED_P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _ED_P
    c = 2 * p[3] * q[3] * _ED_D % _ED_P
    d = 2 * p[2] * q[2] % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(s, p):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        s >>= 1
    return q


def _ed_eq(p, q):
    return ((p[0] * q[2] - q[0] * p[2]) % _ED_P == 0
            and (p[1] * q[2] - q[1] * p[2]) % _ED_P == 0)


def _ed_point(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _ED_P:
        return None
    x = _ed_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _ED_P)


def _ed_bytes(p):
    zi = pow(p[2], _ED_P - 2, _ED_P)
    x, y = p[0] * zi % _ED_P, p[1] * zi % _ED_P
    return int(y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_verify(public, msg, sig):
    """RFC 8032 section 5.1.7: is `sig` the signature of `msg` by `public`?"""
    if len(public) != 32 or len(sig) != 64:
        return False
    a, r = _ed_point(public), _ed_point(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if a is None or r is None or s >= _ED_L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + public + msg).digest(), "little") % _ED_L
    return _ed_eq(_ed_mul(s, _ED_B), _ed_add(r, _ed_mul(h, a)))


def webhook_channel(hook, opener=None):
    """The channel a webhook posts into (a GET on the webhook needs only its
    own URL), or None."""
    d = Discord("lookup", hook, os.devnull, opener=opener)
    st, data = d._call("GET", d.url)
    if st != 200 or not isinstance(data, dict):
        return None
    return str(data.get("channel_id") or "") or None


def channel_guild(cid, token, opener=None):
    """Which server a channel is in. The webhook's channel has to be filed
    under its own guild like any other, or it and that server's own
    /<board>board choice would each run a worker and post twice."""
    d = Discord("lookup", "%s/channels/%s" % (DISCORD_API, cid), os.devnull,
                opener=opener, auth=token)
    st, data = d._call("GET", d.url)
    if st != 200 or not isinstance(data, dict):
        return None
    return str(data.get("guild_id") or "") or None


def _state_dir():
    return os.environ.get("POL_BOARDS_STATE_DIR") or ("/state" if os.path.isdir("/state") else "")


def bot_channels():
    """{"chosen": {feed key: {guild id: channel id}},
        "posted": {feed key: {guild id: channel id}}}: where a feed was told
    to post (/<board>board <feed>), and where it last did, per server. Kept
    in the database (the "discord_channels" row of STATE_TABLE), or with no
    database in the state dir.

    Per guild so that a bot invited to a second server adds a board there
    instead of moving the first server's board. Every feed runs a post per
    guild.

    The older flat file ({feed: channel}) still reads: its channel is filed
    under "", the guild that cannot be named from the file alone. That keeps
    a running board where it is across the upgrade, and the first
    /<board>board adopts it (see _note_channel).
    """
    try:
        data = state_read(_channels_path()) if _channels_path() else {}
    except _state_errors():
        data = {}
    out = {}
    for which in ("chosen", "posted"):
        per = {}
        for key, val in (data.get(which) or {}).items():
            if isinstance(val, dict):
                got = {str(g): str(c) for g, c in val.items() if c}
                if got:
                    per[key] = got
            elif val:
                per[key] = {"": str(val)}           # the pre-guild file
        out[which] = per
    return out


def _channels_path():
    """Where bot_channels lives: the "discord_channels" row with a database,
    else the state dir's discord_channels.json, else nowhere ("")."""
    if _state_db():
        return _DB_PREFIX + "discord_channels"
    d = _state_dir()
    return os.path.join(d, "discord_channels.json") if d else ""


def _write_channels(data):
    path = _channels_path()
    if not path:
        return False
    try:
        state_write(path, data)
        return True
    except _state_errors():
        return False


def _note_channel(which, key, cid, guild=""):
    data = bot_channels()
    per = data[which].setdefault(key, {})
    per[str(guild or "")] = str(cid)
    if guild:
        # a named guild supersedes the unnamed one the pre-guild file left:
        # this IS that board, now that a command has told us whose it is
        per.pop("", None)
    return _write_channels(data)


def _forget_channel(which, key, guild=""):
    data = bot_channels()
    per = data[which].get(key) or {}
    if per.pop(str(guild or ""), None) is None:
        return False
    if per:
        data[which][key] = per
    else:
        data[which].pop(key, None)
    return _write_channels(data)


def feed_names(board, name):
    """{command option: feed key} -- a board names its feeds for the
    /<board>board command in DISCORD_FEED_NAMES ({feed: option})."""
    names = getattr(board, "DISCORD_FEED_NAMES", None) or {"": "board"}
    return {opt: key for feed, key in feed_keys(name)
            for f, opt in names.items() if f == feed}


def command_of(board, name):
    """The board's one slash command: /<board>board <feed> | status, for
    members who can Manage Server (Discord hides it from everyone else)."""
    title = getattr(board, "DISCORD_TITLE", name)
    opts = [{"type": 1, "name": opt, "description": ("Post the %s %s in this channel"
                                                      % (title, opt))[:100]}
            for opt in feed_names(board, name)]
    opts.append({"type": 1, "name": "status", "description": "Show where the board posts"})
    return {"name": "%sboard" % name, "type": 1, "contexts": [0], "integration_types": [0],
            "default_member_permissions": "32", "options": opts,
            "description": ("Choose where the %s board posts" % title)[:100]}


def register_commands(board, name, token, app_id, opener=None):
    """Put the board's command up (PUT = the app's whole command list, so it
    is idempotent). Returns the HTTP status."""
    d = Discord("%s_commands" % name, "%s/applications/%s/commands" % (DISCORD_API, app_id),
                os.devnull, opener=opener, auth=token)
    st, data = d._call("PUT", d.url, json.dumps([command_of(board, name)]).encode("utf-8"),
                       "application/json")
    if st not in (200, 201):
        d._log("could not register /%sboard (%s %s)" % (name, st, _brief(data)))
    return st


#: Manage Server or Administrator: who may move a board
_MOVERS = 0x20 | 0x8


def handle_command(board, name, data):
    """/<board>board <feed>: post that feed in THIS channel from now on;
    /<board>board status: where each feed posts. Answered privately."""
    opts = (data.get("data") or {}).get("options") or []
    sub = str(opts[0].get("name")) if opts and isinstance(opts[0], dict) else "status"
    names = feed_names(board, name)
    title = getattr(board, "DISCORD_TITLE", name)
    cid = str(data.get("channel_id") or (data.get("channel") or {}).get("id") or "")
    # which server this was typed in: a feed's channel is remembered per guild,
    # so a second server ADDS a board rather than moving the first one's
    guild = str(data.get("guild_id") or (data.get("guild") or {}).get("id") or "")
    try:
        perms = int(((data.get("member") or {}).get("permissions")) or 0)
    except (TypeError, ValueError):
        perms = 0

    def reply(text):
        return {"type": 4, "data": {"flags": 64, "content": text,
                                    "allowed_mentions": {"parse": []}}}

    def where(key, ch):
        """Where this feed posts IN THIS SERVER. The pre-guild entry ("") is
        this server's too until a command files it properly."""
        for g in (guild, ""):
            if (ch["chosen"].get(key) or {}).get(g):
                return "<#%s>" % ch["chosen"][key][g]
        for g in (guild, ""):
            if (ch["posted"].get(key) or {}).get(g):
                return "<#%s> (its webhook's channel)" % ch["posted"][key][g]
        return "nowhere here yet: run this command in the channel you want"

    if sub == "status":
        ch = bot_channels()
        return reply("\n".join("**%s %s**: %s" % (title, opt, where(key, ch))
                               for opt, key in names.items()))
    if sub not in names:
        return reply("There is no %r feed on this board." % sub)
    if not perms & _MOVERS:
        return reply("Only members who can manage the server can move the board.")
    if not cid or not _note_channel("chosen", names[sub], cid, guild):
        return reply("The board could not save that just now; try again in a moment.")
    return reply("Done: the %s %s will post in <#%s> within a few seconds, and its old "
                 "post goes away. The bot needs to be able to see and post in this "
                 "channel." % (title, sub, cid))


def start_presence(board, args, name, token):
    """Hold the bot's Gateway session so its status reads "Watching 12 players
    online". Presence is the one Discord thing with no REST endpoint, so this
    is the only place the service keeps a socket open -- see polgateway.

    Where the number comes from, in order:
      * the board's own `presence_count(args)`, for a board that already knows
        (Jan counts the people seated at its live tables). This route needs
        no change to a game server.
      * else the marker `PRESENCE_GAME` (default: the board's name) publishes,
        which live_sessions.py has been writing all along.

    A board says what its count is CALLED in PRESENCE_ONE/PRESENCE_MANY: Tetra
    Master's marker counts matches, not players. With no number at all the
    status is simply blank, so a board for a game that does not report costs
    nothing.
    """
    off = ("off", "0", "no", "false")
    if (os.environ.get("POL_BOARDS_PRESENCE", "on") or "").strip().lower() in off:
        return None
    game = getattr(board, "PRESENCE_GAME", name)
    idle = getattr(board, "PRESENCE_IDLE", "")
    one = getattr(board, "PRESENCE_ONE", "player online")
    many = getattr(board, "PRESENCE_MANY", "players online")
    own = getattr(board, "presence_count", None)

    def text():
        n = None
        if own is not None:
            n = own(args)
        if n is None:
            n = polgateway.read_count(game)
        return polgateway.count_text(game, idle=idle, n=n, one=one, many=many)

    return polgateway.Presence(name, token, status_fn=text).start()


def run_bot_feed(board, args, key, feed, hook, token, opener=None, rounds=None,
                 period=None):
    """A feed posted AS THE BOT, so its messages can carry buttons, over the
    channel API with the bot's token: into the channel chosen
    with /<board>board, else the one its webhook points at. What the webhook
    posted is deleted first; when the chosen channel changes -- now, or while
    the service was down -- the feed's posts in the old channel are deleted
    before it posts in the new one. A board with discord_<feed>_bot_message
    posts that ONE message; otherwise the feed's message set, with bot=True
    so it adds its buttons. `rounds` bounds each channel's loop for tests.

    ONE POST PER SERVER. This is a supervisor: it works out which (guild,
    channel) pairs the feed owes a post to and runs a worker for each, so a
    bot invited to three servers keeps three boards rather than three servers
    fighting over one. A guild that stops being a target has its post deleted.

    THE WEBHOOK IS OPTIONAL. A board with a webhook keeps posting in its
    channel by default, as before. A board with none simply waits for
    /<board>board <feed> in each server that wants it, which is what makes a
    bot invitable without handing anyone a webhook URL first.
    """
    if hook:
        gone = DiscordSet(key, hook, state_path(args, key), opener=opener).clear()
        if gone:
            print("[polboards] %s: deleted %d message(s) the webhook had posted -- the "
                  "bot posts now" % (key, gone), flush=True)
    single = feed_fn(board, feed, "bot_message")
    # a feed's own ended-message life (the live feeds: 0, gone with the game),
    # as the webhook path already honours it
    ended = feed_fn(board, feed, "ended_ttl")
    ended = args.discord_ended_ttl if ended is None else ended
    #: the webhook's channel and the guild it is in, looked up at most once
    home = {"cid": None, "guild": None, "tried": False}

    def make(cid, guild):
        # one state file per (feed, guild): a message id belongs to the server
        # it was posted in, and sharing one file across servers loses posts
        path = state_path(args, "%s_bot%s" % (key, ("_" + guild) if guild else ""))
        url = "%s/channels/%s" % (DISCORD_API, cid)
        if single is not None:
            return Discord(key, url, path, args.discord_every, args.discord_event_ttl,
                           args.discord_refresh, opener=opener, auth=token)
        return DiscordSet(key, url, path, args.discord_slot_every, args.discord_event_ttl,
                          args.discord_refresh, ended, opener=opener, auth=token)

    def targets():
        """{guild: channel} this feed owes a post to right now."""
        chosen = bot_channels()["chosen"].get(key) or {}
        if chosen:
            # ONCE A CHANNEL HAS BEEN CHOSEN, THE WEBHOOK IS DONE. Keeping the
            # webhook's channel as a standing extra target would bring back
            # what /<board>board exists to escape: a webhook channel that
            # answers 403 Missing Permissions would be posted to, and refuse,
            # forever.
            return dict(chosen)
        want = {}
        if hook and not home["tried"]:
            home["tried"] = True
            home["cid"] = webhook_channel(hook, opener)
            if home["cid"]:
                home["guild"] = channel_guild(home["cid"], token, opener) or ""
            else:
                print("[polboards] %s: cannot read the webhook's channel yet" % key,
                      flush=True)
                home["tried"] = False       # ask again on the next pass
        if home["cid"]:
            want[home["guild"] or ""] = home["cid"]
        want.update(chosen)                 # a server's own choice wins for it
        return want

    def sweep(want):
        """Delete what we posted in a server that is no longer a target (the
        bot was thrown out, or the feed was moved away)."""
        for g, cid in list((bot_channels()["posted"].get(key) or {}).items()):
            if g in want:
                continue                    # its own worker handles a move
            n = make(cid, g).clear()
            _forget_channel("posted", key, g)
            print("[polboards] %s: no longer posts in server %s (%d post(s) deleted)"
                  % (key, g or "?", n), flush=True)

    def run_one(guild, cid, stop_evt=None):
        was = (bot_channels()["posted"].get(key) or {}).get(guild)
        if was and was != cid:
            n = make(was, guild).clear()
            print("[polboards] %s: moved from channel %s to %s (%d old post(s) deleted)"
                  % (key, was, cid, n), flush=True)
        _note_channel("posted", key, cid, guild)
        d = make(cid, guild)
        stop = lambda: ((stop_evt is not None and stop_evt.is_set())      # noqa: E731
                        or targets().get(guild) != cid)
        if single is not None:
            watch(board, args, d, period=period, rounds=rounds, feed=feed,
                  message_fn=single, stop=stop)
        else:
            watch_set(board, args, d, period=period, rounds=rounds, feed=feed, stop=stop)
        return d

    if rounds is not None:                  # tests: one pass, synchronous
        want = targets()
        sweep(want)
        d = None
        for guild, cid in want.items():
            d = run_one(guild, cid)
        return d

    workers, quiet = {}, False
    while True:
        want = targets()
        sweep(want)
        for guild in [g for g, w in workers.items() if want.get(g) != w["cid"]]:
            workers.pop(guild)["stop"].set()
        for guild, cid in want.items():
            if guild in workers and workers[guild]["thread"].is_alive():
                continue
            ev = threading.Event()
            t = threading.Thread(target=run_one, args=(guild, cid, ev), daemon=True,
                                 name="discord-bot-%s-%s" % (key, guild or "home"))
            workers[guild] = {"cid": cid, "stop": ev, "thread": t}
            t.start()
        if not want and not quiet:
            quiet = True
            print("[polboards] %s: nowhere to post yet -- run /%sboard in a channel"
                  % (key, board.NAME if hasattr(board, "NAME") else key), flush=True)
        elif want:
            quiet = False
        time.sleep(30)


def state_path(args, name):
    """--<name>-discord-state, else the "<name>_discord" row of STATE_TABLE
    when a database is configured, else $POL_BOARDS_STATE_DIR (/state) /
    <name>_discord.json. With none of them, nothing is kept and a restart
    posts a second board -- said so at start."""
    p = getattr(args, "%s_discord_state" % name, "") or ""
    if p:
        return p
    if _state_db():
        return "%s%s_discord" % (_DB_PREFIX, name)
    d = os.environ.get("POL_BOARDS_STATE_DIR") or ("/state" if os.path.isdir("/state") else "")
    return os.path.join(d, "%s_discord.json" % name) if d else os.devnull


def watch(board, args, discord, period=None, rounds=None, feed="", message_fn=None,
          stop=None):
    """The board's Discord side: status posts for what the board calls news,
    then the message itself. `rounds` bounds the loop for tests; `stop()`
    ends it (the bot's channel moved)."""
    period = float(period or getattr(args, "poll", 5.0) or 5.0)
    message = message_fn or feed_fn(board, feed, "message")
    news = feed_fn(board, feed, "events")
    sig_key = "%s_sig" % feed if feed else "sig"
    prev, last_err, n = None, 0.0, 0
    while (rounds is None or n < rounds) and not (stop and stop()):
        n += 1
        try:
            snap = board.cached_snapshot(args)
            events = []
            if prev is not None and news is not None:
                events = list(news(prev, snap) or [])
            prev = snap
            for text in events[:5]:
                print("[polboards] %s: %s" % (discord.name, text.replace("**", "")),
                      flush=True)
                if getattr(args, "discord_events", "on") == "on":
                    discord.say(text)
            discord.tick(snap.get(sig_key, snap["sig"]), lambda: message(snap, args),
                         force=bool(events))
        except Exception as e:                         # noqa: BLE001
            if time.time() - last_err > 300:
                last_err = time.time()
                import traceback
                print("[polboards] %s discord watcher error (%s) -- still running"
                      % (discord.name, e), flush=True)
                traceback.print_exc()
        if rounds is None or n < rounds:
            time.sleep(period)


def watch_set(board, args, discord, period=None, rounds=None, feed="", stop=None):
    """A board feed with a SET of messages (discord_<feed>_messages): status
    posts for its news as watch() does, then DiscordSet.sync. A slot that
    disappears is retired from the last snapshot that still had it."""
    period = float(period or getattr(args, "poll", 5.0) or 5.0)
    messages, news = feed_fn(board, feed, "messages"), feed_fn(board, feed, "events")
    retire_fn = feed_fn(board, feed, "retire")
    prev, last, last_err, n = None, {}, 0.0, 0
    while (rounds is None or n < rounds) and not (stop and stop()):
        n += 1
        try:
            snap = board.cached_snapshot(args)
            events = list(news(prev, snap) or []) if prev is not None and news else []
            for text in events[:5]:
                print("[polboards] %s: %s" % (discord.name, text.replace("**", "")),
                      flush=True)
                if getattr(args, "discord_events", "on") == "on":
                    discord.say(text)
            kw = {"bot": True} if discord.auth else {}       # a bot adds its buttons
            slots = list(messages(snap, args, **kw) or [])
            for s in slots:
                last[str(s["slot"])] = snap
            retire = None
            if retire_fn is not None:
                retire = lambda slot: (retire_fn(slot, last[slot], args, **kw)   # noqa: E731
                                       if slot in last else None)
            discord.sync(slots, retire=retire)
            for slot in [k for k in last if k not in discord.msgs
                         and k not in {str(s["slot"]) for s in slots}]:
                last.pop(slot, None)
            prev = snap
        except Exception as e:                         # noqa: BLE001
            if time.time() - last_err > 300:
                last_err = time.time()
                import traceback
                print("[polboards] %s discord watcher error (%s) -- still running"
                      % (discord.name, e), flush=True)
                traceback.print_exc()
        if rounds is None or n < rounds:
            time.sleep(period)


class _Server(ThreadingHTTPServer):
    """The board's HTTP server. WARNING: socketserver's default listen backlog is 5:
    the TM watch page's animations load a burst of distinct frames at once and
    the sixth simultaneous connection was REFUSED (the browser test caught
    ERR_CONNECTION_REFUSED on art, 2026-09-13) -- Cloudflare opens bursts to
    the origin too. The backlog is a class attribute because listen() runs in
    the constructor."""
    request_queue_size = 128
    daemon_threads = True


def serve(board, args, port, bind="127.0.0.1"):
    srv = _Server((bind or "127.0.0.1", int(port)), _Handler)
    srv.board, srv.board_args = board, args
    threading.Thread(target=srv.serve_forever, name="board-" + board.NAME,
                     daemon=True).start()
    return srv


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", default=os.environ.get("POL_BOARDS_BIND", "127.0.0.1"),
                    help="address every board binds (0.0.0.0 when the tunnel "
                         "runs on another machine on the LAN)")
    ap.add_argument("--poll", type=float, default=5.0, metavar="S",
                    help="how often a page asks for fresh state")
    ap.add_argument("--discord-every", type=float, default=60.0, metavar="S",
                    help="at most one edit of a board's Discord message per S seconds")
    ap.add_argument("--discord-refresh", type=float, default=1800.0, metavar="S",
                    help="re-edit an unchanged board this often (its timestamp)")
    ap.add_argument("--discord-event-ttl", type=float, metavar="S",
                    default=float(os.environ.get("POL_BOARDS_DISCORD_EVENT_TTL") or 600),
                    help="delete a status post after S seconds (0 = keep)")
    ap.add_argument("--discord-events", choices=("on", "off"), default="on",
                    help="post the short status messages at all")
    ap.add_argument("--discord-slot-every", type=float, default=15.0, metavar="S",
                    help="a message-set feed (one message per list / listing): at "
                         "most one edit of each message per S seconds")
    ap.add_argument("--discord-ended-ttl", type=float, metavar="S",
                    default=float(os.environ.get("POL_BOARDS_DISCORD_ENDED_TTL") or 600),
                    help="a message-set feed: delete a message S seconds after what "
                         "it shows has ended (a sold card, an ended listing)")
    for name in BOARDS:
        env = "POL_BOARDS_%s_" % name.upper()
        ap.add_argument("--%s-port" % name, type=int, default=0, metavar="PORT",
                        help="serve the %s board on this port (0 = off)" % name)
        for feed, key in feed_keys(name):
            kenv = "POL_BOARDS_%s_" % key.upper()
            what = "the %s board%s" % (name, (" (%s)" % feed) if feed else "")
            flag = key.replace("_", "-")
            ap.add_argument("--%s-discord-webhook" % flag, metavar="URL",
                            default=os.environ.get(kenv + "DISCORD_WEBHOOK", ""),
                            help="post %s to this Discord webhook, edited in place "
                                 "(env %sDISCORD_WEBHOOK; a SECRET: keep it in "
                                 ".env)" % (what, kenv))
            ap.add_argument("--%s-discord-state" % flag, default="", metavar="FILE",
                            help="where its message id is kept (default "
                                 "$POL_BOARDS_STATE_DIR/%s_discord.json)" % key)
        ap.add_argument("--%s-discord-bot-token" % name, metavar="TOKEN",
                        default=os.environ.get(env + "DISCORD_BOT_TOKEN", ""),
                        help="post the %s board's Discord messages AS A BOT (so they "
                             "carry buttons), into the channels its webhooks point at "
                             "(env %sDISCORD_BOT_TOKEN; a SECRET: keep it in .env)"
                             % (name, env))
        ap.add_argument("--%s-discord-public-key" % name, metavar="HEX",
                        default=os.environ.get(env + "DISCORD_PUBLIC_KEY", ""),
                        help="the bot's public key: checks the button clicks Discord "
                             "sends to /discord/interactions")
        ap.add_argument("--%s-discord-app-id" % name, metavar="ID",
                        default=os.environ.get(env + "DISCORD_APP_ID", ""),
                        help="the bot's application id (the log names it)")
        ap.add_argument("--%s-url" % name, metavar="URL",
                        default=os.environ.get(env + "URL", ""),
                        help="the %s board's public page, linked from Discord" % name)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = []
    if _state_db():
        janstore.migrate_at_start("polboards")
    for name, modname in BOARDS.items():
        port = int(getattr(args, "%s_port" % name) or 0)
        if not port:
            continue
        board = importlib.import_module(modname)
        serve(board, args, port, args.bind)
        started.append("%s on http://%s:%d/" % (name, args.bind, port))
        if hasattr(board, "start"):
            board.start(args)               # a board's own background work (Jan: watching)
        token = (getattr(args, "%s_discord_bot_token" % name, "") or "").strip()
        app_id = (getattr(args, "%s_discord_app_id" % name, "") or "").strip()
        if token and app_id:
            threading.Thread(target=register_commands, args=(board, name, token, app_id),
                             name="discord-commands-" + name, daemon=True).start()
        if token:
            start_presence(board, args, name, token)
        for feed, key in feed_keys(name):
            hook = (getattr(args, "%s_discord_webhook" % key, "") or "").strip()
            if not hook and key in FEED_SHARES_MAIN:
                hook = (getattr(args, "%s_discord_webhook" % name, "") or "").strip()
            # A BOT NEEDS NO WEBHOOK. It is invited to a server and told where
            # to post with /<board>board, which is the whole point of having a
            # bot: nobody has to be handed a webhook URL to add a board.
            if not hook and not token:
                continue
            if hook and (not hook.startswith("https://") or "/api/webhooks/" not in hook):
                print("[polboards] %s: the Discord webhook does not look like a "
                      "Discord webhook URL -- posting is OFF" % key, flush=True)
                continue
            many = feed_fn(board, feed, "messages") is not None
            # the bot path draws its own message (it adds buttons); the webhook
            # path draws the plain one. A board missing the one its mode needs
            # says so rather than starting a feed that can never post.
            wants = "bot_message" if token else "message"
            if not many and feed_fn(board, feed, wants) is None:
                print("[polboards] %s: this board has no discord_%s%s -- posting is OFF"
                      % (key, ("%s_" % feed) if feed else "", wants), flush=True)
                continue
            if token:
                print("[polboards] %s: posting AS THE BOT (app %s)%s"
                      % (key, app_id or "?",
                         " into its webhook's channel" if hook
                         else " -- run /%sboard to say where" % name), flush=True)
                threading.Thread(target=run_bot_feed,
                                 args=(board, args, key, feed, hook, token),
                                 name="discord-bot-" + key, daemon=True).start()
                continue
            path = state_path(args, key)
            nostate = "" if path != os.devnull else " (NO state dir: a restart posts again)"
            if many:
                # a feed may say how long an ended message stays (Jan "live":
                # 0 -- gone as soon as the game is); else --discord-ended-ttl
                ended = feed_fn(board, feed, "ended_ttl")
                d = DiscordSet(key, hook, path, args.discord_slot_every,
                               args.discord_event_ttl, args.discord_refresh,
                               args.discord_ended_ttl if ended is None else ended)
                print("[polboards] %s: Discord webhook set; a message per slot, %d kept%s"
                      % (key, len(d.msgs), nostate), flush=True)
                target = watch_set
            else:
                d = Discord(key, hook, path, args.discord_every, args.discord_event_ttl,
                            args.discord_refresh)
                print("[polboards] %s: Discord webhook set; the board message is %s%s"
                      % (key, ("kept (id %s)" % d.msg_id) if d.msg_id else "new", nostate),
                      flush=True)
                target = watch
            threading.Thread(target=target, args=(board, args, d),
                             kwargs={"feed": feed}, name="discord-" + key,
                             daemon=True).start()
    if not started:
        raise SystemExit("polboards: no board has a port (--jan-port ...)")
    print("[polboards] serving: %s" % "; ".join(started), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
