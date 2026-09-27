#!/usr/bin/env python3
"""polgateway.py -- a bot's Discord Gateway presence ("Watching 12 players").

WHY THIS EXISTS AT ALL. Every other Discord call in this project is plain
HTTPS: polboards posts and edits with urllib, polbridge answers interactions,
and neither ever opens a socket Discord keeps. Presence is the one thing that
cannot be done that way -- **Discord has no REST endpoint for a bot's status or
activity**. It is set in the IDENTIFY payload or with Gateway op 3, and nowhere
else. So a bot that says "12 players online" must hold a WebSocket open.

WHY IT IS HAND-ROLLED. The image installs pyyaml, pycryptodome, pymysql and
pillow -- no websocket library -- and the stdlib has no WebSocket client. This
module speaks RFC 6455 over `ssl` directly, the same choice polboards already
made for RFC 8032 (it verifies Ed25519 interaction signatures with the stdlib
rather than take a dependency). Client-side WebSocket is the easy half: we mask
what we send, the server never masks what it sends, and we need exactly one
text channel with ping/pong.

WHAT IT DOES NOT DO. It never *receives* anything useful: IDENTIFY asks for
`intents: 0`, so Discord sends us no guild, member or message events at all.
That keeps us out of privileged-intent review entirely -- presence is not an
intent, it is something we push. The only frames we read are the handshake,
heartbeat acks and the reconnect/resume bookkeeping.

A SIDE EFFECT WORTH KNOWING. Our bots currently show as **offline** in the
member list even though their slash commands work (an interactions-only bot has
no Gateway session, and the member list only knows about Gateway sessions).
Once a bot runs this, it finally looks alive.

Use it:

    pres = polgateway.Presence("tm", token, status_fn=lambda: "12 players online")
    pres.start()            # daemon thread; never raises into the caller
    pres.set("13 players online")    # or push, if you'd rather not be polled

Both `set()` and `status_fn` are debounced to Discord's presence limit (5 per
20 seconds per session); see MIN_INTERVAL. Nothing here ever raises into the
host process: a Gateway that will not come up must not take a board down with
it, so every failure is logged and retried with backoff.

    python polgateway.py --token ... --text "12 players online"   # smoke test
"""
import argparse
import base64
import errno
import hashlib
import json
import os
import random
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

#: Discord's Gateway. v10 is what the REST side already uses (polboards
#: DISCORD_API), and `encoding=json` keeps us off erlpack.
GATEWAY_URL = os.environ.get("POL_GATEWAY_URL",
                             "wss://gateway.discord.gg/?v=10&encoding=json")

#: RFC 6455's handshake constant. The server answers Sec-WebSocket-Key with
#: base64(sha1(key + this)); checking it is what proves we reached a WebSocket
#: and not a proxy that happened to say 101.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Presence updates are limited to 5 per 20 seconds per Gateway session. We
#: keep a wide margin: a player count that flickers is not worth a disconnect.
MIN_INTERVAL = float(os.environ.get("POL_GATEWAY_MIN_INTERVAL", "15.0"))

#: Gateway close codes we must NOT retry: the token is wrong, the intents were
#: refused, or we sent something invalid. Retrying these is a hot loop against
#: Discord that never recovers, so the thread stops and says why.
FATAL_CLOSE = {
    4004: "the bot token was not accepted",
    4010: "we sent an invalid shard",
    4011: "this bot needs sharding",
    4012: "the Gateway version we asked for is invalid",
    4013: "the intents we sent are invalid",
    4014: "the intents we sent are not approved for this bot",
}

#: Activity types. 3 = Watching, which reads best for a player count
#: ("Watching 12 players online"); 0 = Playing, 2 = Listening, 5 = Competing.
ACTIVITY_WATCHING = 3


def log(name, text):
    print("[polgateway] %s: %s" % (name, text), flush=True)


# --------------------------------------------------------------------------- #
# RFC 6455, client side
# --------------------------------------------------------------------------- #
class WebSocketError(Exception):
    """Any handshake or framing failure. Carries `code` for a Gateway close."""

    def __init__(self, text, code=None):
        Exception.__init__(self, text)
        self.code = code


class WebSocket:
    """One client WebSocket over TLS. Text frames only, which is all the
    Gateway sends us with `encoding=json`.

    Threading: `send` takes a lock because the heartbeat runs on its own
    thread while the reader blocks in `recv`. That is the whole reason the
    lock exists -- two threads, one socket.
    """

    def __init__(self, sock):
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buf = b""
        self.closed = False
        #: set when the peer sends a close frame, so the caller can tell a
        #: "Discord asked us to go" from a broken pipe
        self.close_code = None

    # -- handshake -------------------------------------------------------- #
    @classmethod
    def connect(cls, url, timeout=30.0):
        """GET the URL with the Upgrade headers and check the 101 back."""
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("wss", "ws"):
            raise WebSocketError("not a WebSocket URL: %r" % (url,))
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")

        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if u.scheme == "wss":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = ("GET %s HTTP/1.1\r\n"
                   "Host: %s\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n"
                   "User-Agent: DiscordBot (https://playonline.invalid/polgateway, 1)\r\n"
                   "\r\n" % (path, host, key))
            sock.sendall(req.encode("ascii"))

            head, rest = cls._read_head(sock)
            status = head.split("\r\n", 1)[0]
            if " 101" not in status:
                raise WebSocketError("the Gateway refused the upgrade: %s"
                                     % status.strip()[:120])
            want = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            got = ""
            for line in head.split("\r\n")[1:]:
                k, _, v = line.partition(":")
                if k.strip().lower() == "sec-websocket-accept":
                    got = v.strip()
            if got != want:
                # a 101 from something that is not a WebSocket: fail loudly
                # rather than feed its bytes to the frame reader
                raise WebSocketError("Sec-WebSocket-Accept did not match")
            ws = cls(sock)
            ws._buf = rest
            return ws
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    @staticmethod
    def _read_head(sock):
        """Read up to the blank line that ends the HTTP response head, and
        hand back whatever of the first frame came with it."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("the connection closed during the handshake")
            buf += chunk
            if len(buf) > 65536:
                raise WebSocketError("the handshake response is implausibly long")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head.decode("latin-1"), rest

    # -- framing ---------------------------------------------------------- #
    def _recv_exactly(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise
            except OSError as e:
                raise WebSocketError("read failed (%s)" % (e,))
            if not chunk:
                raise WebSocketError("the peer closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_frame(self):
        """One frame -> (fin, opcode, payload). Server frames are never
        masked; if one is, the peer is not talking RFC 6455 to a client."""
        b0, b1 = self._recv_exactly(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exactly(8))[0]
        if length > 16 * 1024 * 1024:
            raise WebSocketError("frame of %d bytes is implausible" % length)
        mask = self._recv_exactly(4) if masked else b""
        data = self._recv_exactly(length) if length else b""
        if masked:
            data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
        return fin, opcode, data

    def recv(self, timeout=None):
        """The next TEXT message, as str. Control frames are handled here and
        never returned: a ping is ponged, a close raises with its code.

        `timeout` is the socket's, so a caller can wake up to do other work;
        it re-raises socket.timeout, which is not an error condition.
        """
        self.sock.settimeout(timeout)
        parts, kind = [], None
        while True:
            fin, opcode, data = self._recv_frame()
            if opcode == 0x8:                       # close
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else None
                self.close_code = code
                reason = data[2:].decode("utf-8", "replace") if len(data) > 2 else ""
                raise WebSocketError("the Gateway closed the connection (%s %s)"
                                     % (code, reason), code=code)
            if opcode == 0x9:                       # ping -> pong it back
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:                       # pong, unsolicited
                continue
            if opcode in (0x1, 0x2):
                kind = opcode
                parts = [data]
            elif opcode == 0x0:                     # continuation
                parts.append(data)
            if fin and kind is not None:
                blob = b"".join(parts)
                return blob.decode("utf-8", "replace")

    def _send_frame(self, opcode, payload):
        """Client frames are ALWAYS masked (RFC 6455 5.3) -- Discord drops the
        connection on an unmasked one."""
        if self.closed:
            raise WebSocketError("the socket is closed")
        mask = os.urandom(4)
        body = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        with self._send_lock:
            try:
                self.sock.sendall(head + mask + body)
            except OSError as e:
                raise WebSocketError("write failed (%s)" % (e,))

    def send(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self):
        self.closed = True
        try:
            self._send_frame(0x8, b"\x03\xe8")      # 1000, going away
        except (WebSocketError, OSError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# the Gateway session
# --------------------------------------------------------------------------- #
class Presence:
    """Holds one Gateway session open and keeps the bot's activity text on it.

    `status_fn` is polled (cheaply -- ours reads one small JSON file) and its
    return value becomes the text; `set()` pushes instead. Either way the
    write to Discord is debounced to MIN_INTERVAL and skipped when the text
    has not changed, so a board that recomputes every 5 seconds does not spend
    its presence budget saying the same thing.

    Nothing in here raises into the caller. A bot whose Gateway is down is a
    bot with a stale status, which is strictly better than a board that died.
    """

    def __init__(self, name, token, status_fn=None, text="", every=15.0,
                 activity_type=ACTIVITY_WATCHING, status="online",
                 connect_fn=None, url=None):
        self.name = name
        self.token = (token or "").strip()
        self.status_fn = status_fn
        self.every = max(1.0, float(every or 15.0))
        self.activity_type = int(activity_type)
        self.status = status
        #: tests hand in a fake transport; production gets WebSocket.connect
        self.connect_fn = connect_fn or WebSocket.connect
        self.url = url or GATEWAY_URL

        self._lock = threading.Lock()
        self._want = (text or "").strip()   # what the status SHOULD say
        self._sent = None                   # what Discord was last told
        self._last_send = 0.0
        self._stop = threading.Event()
        self._thread = None

        # resume bookkeeping (op 9 tells us whether it is even allowed)
        self._seq = None
        self._session_id = None
        self._resume_url = None

    # -- what the status says --------------------------------------------- #
    def set(self, text):
        """Ask for this text. Applied on the session's next tick."""
        with self._lock:
            self._want = (text or "").strip()

    def _presence_payload(self):
        text = self._want
        activities = ([{"name": text, "type": self.activity_type}] if text else [])
        return {"op": 3, "d": {"since": None, "activities": activities,
                               "status": self.status, "afk": False}}

    # -- the thread ------------------------------------------------------- #
    def start(self):
        if not self.token:
            log(self.name, "no bot token -- presence is OFF")
            return self
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="gateway-" + self.name,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        """Connect, and keep reconnecting. Backoff is exponential with jitter
        and caps at 5 minutes: Discord being down must not turn into a busy
        loop, and every board reconnecting in lockstep would be our own
        thundering herd."""
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1.0                     # a clean session resets backoff
            except WebSocketError as e:
                if e.code in FATAL_CLOSE:
                    log(self.name, "%s -- presence is OFF until this is fixed"
                        % FATAL_CLOSE[e.code])
                    return
                log(self.name, "%s" % (e,))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log(self.name, "session failed (%s: %s)" % (type(e).__name__, e))
            if self._stop.is_set():
                return
            wait = min(300.0, delay) * (0.5 + random.random())
            self._stop.wait(wait)
            delay = min(300.0, delay * 2)

    def _session(self):
        """One connection: HELLO, IDENTIFY or RESUME, then the tick loop."""
        resuming = bool(self._session_id and self._resume_url)
        ws = self.connect_fn(self._resume_url if resuming else self.url)
        acked = [True]
        beat_stop = threading.Event()
        try:
            hello = json.loads(ws.recv(timeout=30.0))
            if hello.get("op") != 10:
                raise WebSocketError("expected HELLO, got op %r" % hello.get("op"))
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0

            def beat():
                # the first beat is jittered, as the Gateway asks, so a fleet
                # of bots does not heartbeat in lockstep
                beat_stop.wait(interval * random.random())
                while not beat_stop.is_set():
                    if not acked[0]:
                        # a heartbeat that was never acked = a zombie socket
                        # that still looks open. Drop it; _run reconnects.
                        log(self.name, "no heartbeat ack -- dropping a zombie session")
                        try:
                            ws.close()
                        except OSError:
                            pass
                        return
                    acked[0] = False
                    try:
                        ws.send(json.dumps({"op": 1, "d": self._seq}))
                    except (WebSocketError, OSError):
                        return
                    beat_stop.wait(interval)

            threading.Thread(target=beat, name="gateway-beat-" + self.name,
                             daemon=True).start()

            if resuming:
                ws.send(json.dumps({"op": 6, "d": {"token": self.token,
                                                   "session_id": self._session_id,
                                                   "seq": self._seq}}))
            else:
                ws.send(json.dumps({
                    "op": 2,
                    "d": {"token": self.token,
                          # 0: we want no events at all. Presence is pushed,
                          # not subscribed to, so no privileged intent applies.
                          "intents": 0,
                          "properties": {"os": sys.platform, "browser": "polgateway",
                                         "device": "polgateway"},
                          "presence": self._presence_payload()["d"]}}))
                self._sent = self._want

            self._tick_loop(ws, acked)
        finally:
            beat_stop.set()
            try:
                ws.close()
            except OSError:
                pass

    def _tick_loop(self, ws, acked):
        """Read frames until the socket goes, pushing presence when it is due.

        The read timeout is what paces us: `recv` wakes every `every` seconds
        whether or not Discord said anything, and that wake-up is when we look
        at `status_fn` and decide whether the status needs a new value.
        """
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=self.every)
            except socket.timeout:
                self._maybe_push(ws)
                continue
            msg = json.loads(raw)
            op = msg.get("op")
            if msg.get("s") is not None:
                self._seq = msg["s"]
            if op == 11:                            # heartbeat ack
                acked[0] = True
            elif op == 1:                           # asked to beat right now
                acked[0] = False
                ws.send(json.dumps({"op": 1, "d": self._seq}))
            elif op == 7:                           # reconnect, resume allowed
                raise WebSocketError("the Gateway asked us to reconnect")
            elif op == 9:                           # invalid session
                if not msg.get("d"):
                    self._session_id = self._resume_url = self._seq = None
                raise WebSocketError("the Gateway invalidated the session "
                                     "(resumable: %s)" % bool(msg.get("d")))
            elif op == 0 and msg.get("t") == "READY":
                d = msg.get("d") or {}
                self._session_id = d.get("session_id")
                url = d.get("resume_gateway_url")
                # the resume URL comes without the query the first URL had,
                # and a resume on v10 still needs it
                if url:
                    self._resume_url = url.rstrip("/") + "/?v=10&encoding=json"
                user = (d.get("user") or {}).get("username") or "?"
                log(self.name, "connected as %s" % user)
            elif op == 0 and msg.get("t") == "RESUMED":
                log(self.name, "resumed")
            self._maybe_push(ws)

    def _maybe_push(self, ws):
        """Send op 3 if the text changed and the debounce has elapsed."""
        if self.status_fn is not None:
            try:
                text = self.status_fn()
                if text is not None:
                    self.set(text)
            except Exception as e:                  # noqa: BLE001
                # a board's counter being unreadable must not kill the session
                log(self.name, "status_fn failed (%s: %s)" % (type(e).__name__, e))
        now = time.time()
        with self._lock:
            want, sent = self._want, self._sent
        if want == sent or now - self._last_send < MIN_INTERVAL:
            return
        ws.send(json.dumps(self._presence_payload()))
        self._last_send = now
        with self._lock:
            self._sent = want
        log(self.name, "status: %s" % (want or "(cleared)"))


# --------------------------------------------------------------------------- #
# the player count a board shows
# --------------------------------------------------------------------------- #
#: THE COUNT IS ALREADY BEING PUBLISHED. services/live_sessions.py has written
#: `data/<service>-sessions-live.json` = {"count", "stamp"} every 10 seconds
#: since the deploy scripts needed to know whether recreating a container would
#: cut someone's session. feworld and fmo already call it. That marker is
#: exactly "how many players are in this game right now", it is atomic, and the
#: boards already mount /data read-only -- so presence reads it rather than
#: inventing a second mechanism for the same number.
#:
#: A game that does not spawn a thread per session publishes with
#: publish_count() instead; either way the file and its schema are the same.
DATA_DIR = os.environ.get("POL_DATA_DIR", "/data")

#: A count older than this is not a count, it is the last thing a dead service
#: said. The marker is rewritten every 10 s, so this is generous. Past it we
#: show nothing rather than a comfortable lie.
PRESENCE_STALE = float(os.environ.get("POL_PRESENCE_STALE", "180"))


#: games whose marker predates the live_sessions contract and is named
#: differently. Tetra Master's has the IDENTICAL {count, stamp} schema, so
#: presence reads it rather than asking tetramaster.py to publish a second
#: copy of the same number. Note it counts MATCHES, not players, which is why
#: a board says what its count is called.
MARKER_NAMES = {"tm": "tm-matches-live.json"}


def marker_path(game, directory=None):
    """Where `game` publishes its live count. Kept identical to
    live_sessions.marker_path -- the same file, by contract."""
    name = MARKER_NAMES.get(game, "%s-sessions-live.json" % game)
    return os.path.join(directory or DATA_DIR, name)


def publish_count(game, count, extra=None, directory=None):
    """Say how many players a game has, for a service that cannot just count
    its own per-session threads. Atomic, because a board may read at any
    moment, and best-effort: a status is never worth an exception."""
    try:
        path = marker_path(game, directory)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rec = {"count": int(count), "stamp": time.time()}
        if extra:
            rec.update(extra)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_count(game, directory=None, stale=None):
    """How many players `game` has, or None if nobody has said recently."""
    stale = PRESENCE_STALE if stale is None else float(stale)
    try:
        with open(marker_path(game, directory), encoding="utf-8") as fh:
            rec = json.load(fh) or {}
        if stale > 0 and time.time() - float(rec.get("stamp") or 0) > stale:
            return None
        return max(0, int(rec.get("count") or 0))
    except (OSError, ValueError, TypeError):
        return None


def count_text(game, directory=None, stale=None, idle="", n=None,
               one="player online", many="players online"):
    """The activity text for a count: "12 players online", "1 player online",
    and `idle` (default: nothing) when there is nobody or nobody has said.

    `one`/`many` because not every count is players: Tetra Master's marker
    counts MATCHES, and saying "3 players online" of it would be a lie. A
    board that works its own number out passes it as `n`.
    """
    if n is None:
        n = read_count(game, directory, stale)
    # ZERO IS A NUMBER AND GETS SHOWN. This used to fall back to `idle` (i.e.
    # no status at all), which on a server that is usually empty made every
    # bot look broken rather than idle -- there is no way to tell "the bot is
    # working and nobody is on" from "the bot is dead". Only an UNKNOWN count
    # clears the status, which is the case that really is unknowable.
    if n is None:
        return idle
    return "%d %s" % (n, one if n == 1 else many)


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="hold a bot's Discord presence")
    ap.add_argument("--token", default=os.environ.get("POL_GATEWAY_TOKEN", ""),
                    help="the bot token (a SECRET: prefer the environment)")
    ap.add_argument("--name", default="test", help="what the log calls this bot")
    ap.add_argument("--text", default="", help="the activity text to hold")
    ap.add_argument("--game", default="", help="instead of --text, show this "
                                               "game's published player count")
    ap.add_argument("--presence-dir", default=None, help="where --game reads")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="stop after S seconds (0 = run until killed)")
    args = ap.parse_args(argv)
    if not args.token:
        raise SystemExit("polgateway: --token (or POL_GATEWAY_TOKEN) is required")
    fn = None
    if args.game:
        fn = lambda: count_text(args.game, args.presence_dir)      # noqa: E731
    pres = Presence(args.name, args.token, status_fn=fn, text=args.text).start()
    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        pres.stop()


if __name__ == "__main__":
    main()
