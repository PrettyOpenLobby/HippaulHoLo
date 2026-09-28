"""Manager: every table in the process, routing by member, the sweeper, the watch file."""
import os
import threading
import time
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
from .deps import janrules, janseats
from . import knobs, narration, table


# --- the manager -------------------------------------------------------------

class Manager(object):
    """Every table this server is running, keyed by table id.

    `janhourou.py` holds one of these. A peer's records are routed to the table
    its member id is seated at; an unseated peer whose MjREADY starts a game
    is given one, which is what makes a single console playable today without
    a lobby round trip. Any OTHER in-game line from a member with no table is
    a ghost session (the server restarted under a live game) and is answered
    with MjGAMEEND without minting state (finding 32).

    THREADING (finding 9). ONE `RLock` guards `handle`, `tick`,
    `pending_for_member`, the registry and every outbox: two connection
    threads used to mutate one Table at once, and `begin_routing()` from
    thread B reset `_outgoing` under thread A, so `_route` bypassed and sent
    B's private MjTSUMO down A's socket. Everything a Table does now happens
    under the lock; the only thing done outside it is the deal delay
    (`take_hold`), so a 0.7 s deal does not stall every other table.

    THE DEADLINE SWEEPER (finding 8). `ensure_sweeper()` starts one daemon
    thread per Manager (lazily, from the first `handle`) that calls `tick()`
    every SWEEP_S under the lock. What a sweep produces is QUEUED into the
    live seats' outboxes, exactly as 8deee0f8 made the line-driven sweep do,
    and reaches the clients by the routes that already drain them: the next
    game-band line, the `<DR>` poll, and responders' idle push. `on_swept`
    (a callable taking member ids) lets the transport push at once.
    """

    def __init__(self, sweeper=None):
        self.tables = {}
        self.by_member = {}
        self.by_lobby = {}          # lobby key -> the Table playing on it
        self._lock = threading.RLock()
        self._sweeper = None
        self._sweeper_enabled = knobs.SWEEPER if sweeper is None else bool(sweeper)
        self.on_swept = None        # callable(member_ids) once a sweep queued records
        self._next_id = 1
        self.watchable = None       # callable(lobby key) -> bool: janhourou's rule
        self._watch_last = (None, 0.0)   # (the body last written, when)
        self._watch_err = 0.0
        self._watch_final = {}  # lobby key -> (when, entry): a game's LAST frame

    def _new_id(self):
        tid, self._next_id = self._next_id, self._next_id + 1
        return tid

    def _adopt(self, t):
        t._managed = True
        t.clock = self.clock
        t.last_line_at = self.clock()
        return t

    def table_for(self, member_id, nick=None, create=True):
        with self._lock:
            t = self.by_member.get(member_id)
            if t is not None:
                return t
            if not create:
                return None
            tid = self._new_id()
            t = self._adopt(table.Table(tid, channel=b"#MJS0R%03d" % tid))
            t.seat_player(member_id, nick, member=member_id)
            t.fill_with_bots()
            self.tables[tid] = t
            self.by_member[member_id] = t
            return t

    @staticmethod
    def lobby_key(lobby_id, room=None):
        """The Manager's key for a lobby table: the table id alone, or the
        ROOM-QUALIFIED id when the caller knows the room (finding 30), so
        Room 1-1 table 1 and Room 2-3 table 1 are different tables. The
        arithmetic is `janseats.room_table_id` (the seat store, the rule
        store and the lobby's per-room PTL all key by the same composite;
        janhourou passes it in as `lobby_id` with `room=None`)."""
        lobby_id = int(lobby_id or 0)
        if not room:
            return lobby_id
        if janseats is not None and hasattr(janseats, "room_table_id"):
            return janseats.room_table_id(room, lobby_id)
        return (int(room) << 16) | (lobby_id & 0xFFFF)

    @staticmethod
    def rules_for(key):
        """The rules the master stored for a lobby table (`janrules`), or
        None for the engine defaults."""
        if janrules is None:
            return None
        try:
            if hasattr(janrules, "enabled") and not janrules.enabled():
                return None
            return janrules.rules_for(key)
        except Exception:
            return None

    def table_for_lobby(self, lobby_id, members, nick=None, reset=True,
                        room=None):
        """The in-game table for one LOBBY table, seating everyone really there.

        WARNING: THIS IS WHAT `table_for` COULD NOT DO. That keys by member, so two
        humans at one table got two Tables and each played three bots -- proven
        live 2026-09-04, when a Start Game at a table this server knew held two
        people announced `0='PS2Tester' 1='COM 1' 2='COM 2' 3='COM 3'`.

        `members` is `janseats.seats_at()`: [(member, seat, name, polid)].
        Seats come from the STORE, not from arrival order, so the seat a player
        was told they had at reserve time is the seat they get -- the client
        stamps that number on every message it sends and a mismatch would
        misroute the whole hand.

        `rules=janrules.rules_for(lobby)` (finding 2): the hanchan plays what
        the dialog showed, re-read at every new game since the master may have
        changed them. `room` qualifies the table id (finding 30).
        """
        with self._lock:
            key = self.lobby_key(lobby_id, room)
            t = self.by_lobby.get(key)
            if t is None:
                tid = self._new_id()
                # THE CHANNEL IS THE PTL ROW'S NAME. `lobby_id` may already
                # be a composite (janhourou passes the store's key); the
                # channel a client JOINs at game start (`sqMgCpEnterTable`)
                # is the row name it copied at reserve time, which
                # `janseats.table_channel` authors: room-qualified
                # `#MJS0T<room:3><n:3>` since 2026-09-04, bare `#MJS0T00n`
                # for room 0. Informational here (nothing stamps it on a
                # record); it must only agree with what the lobby served.
                base = key & 0xFFFF
                if janseats is not None and hasattr(janseats, "split_table_id"):
                    base = janseats.split_table_id(key)[1]
                if janseats is not None and hasattr(janseats, "table_channel"):
                    chan = janseats.table_channel(key).encode("ascii")
                else:
                    chan = b"#MJS0T%03d" % (base or tid)
                t = self._adopt(table.Table(tid, channel=chan,
                                      rules=self.rules_for(key)))
                t.lobby_key = key
                self.tables[tid] = t
                self.by_lobby[key] = t
            # WARNING: A RUNNING HAND IS NEVER RE-SEATED. `reset=False` is for callers
            # that only want to LOOK at the table -- the member-list panel asks
            # on every open, and re-seating mid-hanchan would clear the wall,
            # the hands and every outbox out from under two players who are
            # mid-game.
            if t.state == "playing" and not reset:
                return t
            # Start Game is the explicit "new game" signal; clear the last one
            # (including anything still queued for a seat) before re-seating.
            if reset:
                t.reset_for_new_game()
                t.rules = self.rules_for(key) or t.rules
            t.seats = [None] * 4
            t.nicks = [None] * 4
            t.members = {}
            t.bots = {}
            for member, seat, name, polid in members:
                if not 0 <= seat < 4:
                    continue
                # The WIRE id must be the client's own PolID: a seat whose id
                # the client does not recognise as itself is a seat it will
                # not play.
                t.seat_player(polid or member, name or nick, seat=seat,
                              live=True, member=member)
                # A player is never also a spectator (one table screen).
                self.forget_spectator(member, "now seated", store=False)
                self.by_member[member] = t
            t.fill_with_bots()
            return t

    # -- the gallery (spectators; see the GALLERY banner) ----------------------

    def add_spectator(self, t, member, slot):
        """Route `member`'s lines to table `t` as a SPECTATOR and hand it the
        board. Any other gallery membership goes first."""
        member = int(member)
        with self._lock:
            old = self.by_member.get(member)
            if old is not None and old is not t and member in old.gallery:
                old.remove_spectator(member, "moved to table %s" % t.id)
            t.add_spectator(member, slot)
            self.by_member[member] = t
            return t.gallery.get(member)

    def forget_spectator(self, member, why="left", store=True):
        """Drop a spectator from its table (and, with `store`, from the seat
        store's gallery). A seated member is untouched. Returns the Table it
        was watching, or None."""
        try:
            member = int(member)
        except (TypeError, ValueError):
            return None
        with self._lock:
            t = self.by_member.get(member)
            if t is None or member not in t.gallery:
                return None
            t.remove_spectator(member, why)
            if t.seat_of_member(member) is None:
                del self.by_member[member]
        if store:
            self._store_gallery_drop(member)
        return t

    def spectator_table(self, member):
        """The Table `member` is WATCHING, or None."""
        with self._lock:
            try:
                t = self.by_member.get(int(member))
            except (TypeError, ValueError):
                return None
            return t if t is not None and int(member) in t.gallery else None

    @staticmethod
    def _store_gallery_drop(member):
        if janseats is None or not hasattr(janseats, "gallery_drop"):
            return
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return
            janseats.gallery_drop(member)
        except Exception:
            pass

    def _release_galleries(self):
        """A table whose MjGAMEEND went out: tell the seat store its gallery
        is over, ONCE (the client leaves the table screen on MjGAMEEND with
        no MjGALLEYLEAVEREQ, spec §1.11; a membership left in the store
        answers the next Watch with 3). The Table keeps its `gallery` until
        each spectator's exit ack or the reap, so queued copies still drain."""
        for t in self.tables.values():
            # Spectators `_gallery_copy` evicted for a full outbox: forget
            # them here and in the store (a Table cannot reach the store).
            while t._gallery_evicted:
                m = t._gallery_evicted.pop()
                if t.seat_of_member(m) is None and self.by_member.get(m) is t:
                    del self.by_member[m]
                self._store_gallery_drop(m)
            if t._gallery_released or t.state not in ("over", "finished"):
                continue
            t._gallery_released = True
            key = getattr(t, "lobby_key", None)
            if not t.gallery or key is None or janseats is None:
                continue
            if not hasattr(janseats, "clear_gallery"):
                continue
            try:
                if hasattr(janseats, "enabled") and not janseats.enabled():
                    continue
                gone = janseats.clear_gallery(key)
                if gone:
                    narration._trace("GALLERY of table %s released at game end: %s"
                           % (t.id, ", ".join(str(m) for m in gone)))
            except Exception:
                pass

    # -- the sweeper thread ----------------------------------------------------

    def ensure_sweeper(self):
        """Start the deadline thread once. Idempotent; a no-op when disabled."""
        if not self._sweeper_enabled or self._sweeper is not None:
            return
        with self._lock:
            if self._sweeper is not None:
                return
            th = threading.Thread(target=self._sweep_loop, name="jan-sweeper",
                                  daemon=True)
            self._sweeper = th
            th.start()
            narration._trace("SWEEPER started (every %.1fs, drop after %d timeouts)"
                   % (knobs.SWEEP_S, knobs.TIMEOUTS_TO_DROP))

    def _sweep_loop(self):
        while True:
            time.sleep(knobs.SWEEP_S)
            try:
                swept = self.tick()
            except Exception as e:                  # never let the thread die
                narration._trace("SWEEPER raised %s: %s" % (type(e).__name__, e))
                continue
            if not swept or self.on_swept is None:
                continue
            with self._lock:
                members = set()
                for t, _recs in swept:
                    for m, s in t.members.items():
                        if s in t.live and t.outbox.get(s):
                            members.add(m)
                    for m in t.gallery:
                        if t.outbox.get(t.gallery_key(m)):
                            members.add(m)
            try:
                self.on_swept(sorted(members))
            except Exception as e:
                narration._trace("SWEEPER push hook raised %s: %s" % (type(e).__name__, e))

    # -- inbound ---------------------------------------------------------------

    def handle(self, rec, member_id=0, nick=None):
        if not knobs.GAME_ENABLE:
            return []
        h = janwire.unpack(rec)
        op = h["opcode"]
        if op not in INGAME_OPCODES:
            return []
        self.ensure_sweeper()
        hold = 0.0
        with self._lock:
            t = self.table_for(member_id, nick, create=(op == M.MjREADY))
            if t is None:
                # No table and no MjREADY to start one: a client mid-game in
                # a hand this process does not hold. Answer the ghost end
                # WITHOUT minting a Table for it (finding 32); a stray MjBYE
                # is that client leaving and needs nothing.
                if op == M.MjBYE:
                    return []
                # A SPECTATOR's line (GALLERY banner): the exit acks after
                # its LEAVEREQ arrive with no table, and a ghost MjGAMEEND
                # is what would land in its room screen's FIFO. Silence --
                # for an MjGALLEYACK always, and for anything else from a
                # member the seat store says is watching.
                if op == M.MjGALLEYACK or self._store_says_spectator(member_id):
                    if op == M.MjGALLEYACK and h["f16"] == 0:
                        self._store_gallery_drop(member_id)   # its exit ack
                    return []
                narration._trace("GAMEEND (member %s sent %s with no table -- ghost "
                       "session, no state minted)" % (member_id, narration.M_NAME(op)))
                return [M.gameend(1)]
            if member_id and int(member_id) in t.gallery:
                # A SPECTATOR: bookkeeping, no mutation, no reply. Its exit
                # ack after MjGAMEEND drops it here and in the store.
                t.begin_routing()
                t.on_gallery(rec, member_id)
                t.take_routes()
                if int(member_id) not in t.gallery:
                    if t.seat_of_member(member_id) is None:
                        self.by_member.pop(int(member_id), None)
                    self._store_gallery_drop(member_id)
                self._release_galleries()
                self._reap()
                return []
            try:
                seat = t.seat_of_member(member_id) if member_id else None
                if seat is None:
                    seat = h["src"] if 0 <= h["src"] < 4 else None
                t.begin_routing()
                out = []
                if self._rejoin_pending(member_id):
                    out += t.rejoin(seat)
                # Sweep THIS table's deadline first: if another seat at it has
                # gone quiet, this line is our chance to play for it (the
                # thread does the same for every table, every second).
                #
                # WARNING: BUT NEVER SWEEP THE SEAT THIS LINE IS FROM (2026-09-03,
                # live): that seat is not silent, it just answered -- slowly. A
                # human reading an unfamiliar board routinely takes longer than
                # the deadline, and sweeping first auto-played the tile they
                # were still choosing, THEN their real discard landed on top --
                # two advance() passes, so duplicated bot turns and a SECOND
                # call offer (seen live: "the tile blinks and I can only hit
                # confirm"). The sweep still catches a DIFFERENT seat that has
                # genuinely gone quiet.
                if not (t._awaiting and t._awaiting[1] == seat):
                    out += t.tick(self.clock())
                records = out + t.handle(rec, member=member_id)
                result = self._route(t, member_id, records, t.take_routes())
                hold = t.take_hold()
            except Exception as e:                  # a bug here must not kill a session
                t.log.append(("error", "%s: %s" % (type(e).__name__, e)))
                raise
            self._release_galleries()
            self._reap()
            self._write_watch()
        if hold > 0:
            time.sleep(hold)                        # the deal delay, unlocked
        return result

    @staticmethod
    def _store_says_spectator(member_id):
        if janseats is None or not member_id or not hasattr(janseats, "gallery_of"):
            return False
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return False
            return bool(janseats.gallery_of(member_id))
        except Exception:
            return False

    def _rejoin_pending(self, member_id):
        if janseats is None or not member_id:
            return False
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return False
            return bool(janseats.rejoin_pending(member_id, clear=True))
        except Exception:
            return False

    def _route(self, t, member_id, records, routes):
        """Split this call's records: the caller's go back on its socket, every
        other live seat's waits in its outbox.

        Every record is built through `_emit`, so `routes` describes `records`
        exactly. One that is not is a BUG -- logged as `route-bypass` -- and
        with more than one live seat it is DROPPED rather than handed to
        whoever spoke: a private hand delivered to the wrong seat is worse than
        a lost record (finding 9). With one live seat the old behaviour (the
        caller gets it) is safe and kept.
        """
        by_id = {id(r): to for r, to in routes}
        unrouted = [r for r in records if id(r) not in by_id]
        if unrouted:
            t.log.append(("route-bypass", len(unrouted)))
            narration._trace("ROUTE BUG: %d record(s) built outside _emit on table %s"
                   % (len(unrouted), t.id))
            if len(t.live) > 1:
                records = [r for r in records if id(r) in by_id]
        caller = t.seat_of_member(member_id)
        if caller is None:
            if len(t.live) > 1:
                t.log.append(("route-no-caller", len(records)))
                return [r for r in records if by_id.get(id(r)) is None]
            return records              # a solo table: the one human gets it
        mine = [r for r in records if by_id.get(id(r)) in (None, caller)]
        for s in sorted(t.live):
            if s == caller:
                continue
            for r in records:
                to = by_id.get(id(r))
                if to is None or to == s:
                    t.queue_for(s, r)
        return mine

    def pending_for_member(self, member_id, limit=None):
        """Anything queued for this member while somebody else was talking, or
        while the sweeper played for a silent seat.

        Drained on EVERY line the member sends (game band or `<DR>`), and by
        responders' idle push for a client that is waiting and says nothing.
        """
        with self._lock:
            try:
                t = self.by_member.get(int(member_id))
            except (TypeError, ValueError):
                return []
            if t is None:
                return []
            seat = t.seat_of_member(member_id)
            if seat is None:
                # A SPECTATOR's copies (GALLERY banner) -- also after it was
                # dropped at game end, while its MjGAMEEND copy is queued.
                gk = t.gallery_key(member_id)
                if int(member_id) in t.gallery or gk in t.outbox:
                    return t.take_outbox(gk, limit=limit)
                return []
            return t.take_outbox(seat, limit=limit)

    def tick(self, now=None):
        """Sweep every table's deadline. Returns [(table, [records]), ...].

        Called every SWEEP_S by the sweeper thread (and by any caller that
        wants a sweep now, e.g. a selftest with a fake clock). Everything a
        sweep produces is QUEUED into the live seats' outboxes; the caller
        gets the list back for logging only.
        """
        with self._lock:
            now = self.clock() if now is None else now
            out = []
            for t in list(self.tables.values()):
                t.begin_routing()
                recs = t.tick(now)
                routes = t.take_routes()
                if not recs:
                    continue
                # WARNING: QUEUE THEM OR THEY ARE LOST (2026-09-04, live: a two-human
                # table where both consoles sat stuck). The sweeper is the ONLY
                # path that advances a table nobody is talking on -- and it
                # used to hand its records back to a caller that had, in its
                # own words, "no peer to send it to on this path", and dropped
                # them. The table's state moved on and NOBODY WAS EVER TOLD.
                by_id = {id(r): to for r, to in routes}
                unrouted = [r for r in recs if id(r) not in by_id]
                if unrouted:
                    # Routes do not describe these records, so we cannot say
                    # who each one is FOR -- and a private hand sent to the
                    # wrong seat is worse than a late one. Log it as the bug
                    # it is and queue only what IS described.
                    t.log.append(("tick-route-bypass", len(unrouted)))
                for r in recs:
                    to = by_id.get(id(r), False)
                    if to is False:
                        continue
                    for seat in sorted(t.live):
                        if to is None or to == seat:
                            t.queue_for(seat, r)
                # (the spectators' copies were queued by `_emit` itself)
                out.append((t, recs))
            self._release_galleries()
            self._reap()
            self._write_watch()
            return out

    def watch_tables(self):
        """{lobby key: {"watchable": bool, "state": ...}} for every table in a
        game (see WATCH_FILE). A table is listed even when it may not be
        watched, as watchable False with no state, so the board drops it at
        once when its creator switches watching off."""
        out = {}
        for t in list(self.tables.values()):
            key = getattr(t, "lobby_key", None)
            if not key or t.state not in ("playing", "over"):
                continue
            ok = False
            try:
                ok = bool(self.watchable and self.watchable(key))
            except Exception:
                ok = False
            out[str(key)] = ({"watchable": True, "state": t.public_state()} if ok
                             else {"watchable": False})
        now = time.time()
        for key, (when, entry) in list(self._watch_final.items()):
            if now - when > knobs.WATCH_FINAL_S:
                del self._watch_final[key]
            elif key not in out:
                out[key] = entry
        return out

    def _keep_final(self, t):
        """A finished game is forgotten at once (`_reap`); keep its last frame
        in the file for WATCH_FINAL_S so the board can show why it stopped
        (seen live 2026-09-13: the page just sat there after the last human
        left). Only a watchable game that got as far as a hand."""
        key = getattr(t, "lobby_key", None)
        if not key or t.kyoku is None or t.state != "finished":
            return
        try:
            if self.watchable and self.watchable(key):
                self._watch_final[str(key)] = (
                    time.time(), {"watchable": True, "state": t.public_state()})
        except Exception:                           # noqa: BLE001
            pass                                    # never touch a game over it

    def _write_watch(self, force=False):
        """WATCH_FILE, atomically, when the tables changed or WATCH_EVERY_S
        passed. Never raises: a failed write must not touch a game."""
        if not knobs.WATCH_FILE or knobs.WATCH_FILE == "0":
            return
        try:
            import json as _json
            body = _json.dumps(self.watch_tables(), sort_keys=True, separators=(",", ":"))
            now = time.time()
            if not force and body == self._watch_last[0] and \
                    now - self._watch_last[1] < knobs.WATCH_EVERY_S:
                return
            path = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), knobs.WATCH_FILE)
            tmp = "%s.tmp.%d" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write('{"stamp":%.3f,"tables":%s}' % (now, body))
            os.replace(tmp, path)
            self._watch_last = (body, now)
        except Exception as e:                      # noqa: BLE001
            if time.time() - self._watch_err > 300:
                self._watch_err = time.time()
                narration._trace("watch file not written (%s: %s) -- games unaffected"
                       % (type(e).__name__, e))

    @staticmethod
    def _owed_seats(t):
        """Seats whose LAST RECORDS ARE STILL IN THE OUTBOX and who are still
        sitting there waiting for them: a live human who has not said MjBYE.

        A bot has no socket, a seat that has said MjBYE has left the screen, and
        a spectator's key is not a seat -- none of those are owed anything."""
        return [s for s, q in t.outbox.items()
                if q and isinstance(s, int) and s in t.live
                and s not in t._byes and s not in t.bots]

    def _reap(self):
        """Forget finished tables, and idle ones nobody has spoken to for
        TABLE_TTL (finding 32). A playing table always finishes first: the
        sweeper drops its silent seats and `drop_seat` finishes it when the
        last live seat goes.

        WARNING: A FINISHED TABLE IS THE ONLY ROUTE ITS RECORDS HAVE. `pending_for_member`
        resolves through `by_member`, so popping the table here threw away
        whatever was still queued for the OTHER human -- silently, with no log
        and no error. The hand that does this is the ordinary one: the ladder
        advances on the FIRST ack (`BROADCAST_ACKED`), so player A can ack
        MjGAMEEND and send MjBYE while player B's copy of it is still in the
        outbox. B then sits on the results screen for ever, because the record
        that takes them off it no longer exists anywhere. That is "it hangs
        when you leave the table after a game is done", and it needs no network
        fault to happen -- only for one player to be a few seconds behind the
        other.

        So a finished table is HELD while a live human who has not said MjBYE
        still has records queued, for at most POL_JAN_FINISHED_GRACE seconds.
        The cap matters as much as the hold: a player who has walked away must
        not keep a table alive for ever, and the seat store -- not this object --
        is what makes the lobby row joinable again, so holding it costs nothing
        that a player can see.
        """
        for tid, t in list(self.tables.items()):
            idle = t.clock() - t.last_line_at
            if not (t.state == "finished"
                    or (t.state != "playing" and idle > knobs.TABLE_TTL)):
                continue
            if t.state == "finished":
                owed = self._owed_seats(t)
                if owed:
                    held = t.clock() - (t.finished_at or t.last_line_at)
                    if held < knobs.FINISHED_GRACE:
                        if not t._reap_held_said:
                            t._reap_held_said = True
                            narration._trace("HOLD table %d (finished) -- seat(s) %s still "
                                   "have %d record(s) queued; the reaper waits up "
                                   "to %gs for them"
                                   % (tid, ",".join(str(x) for x in owed),
                                      sum(len(t.outbox[x]) for x in owed),
                                      knobs.FINISHED_GRACE))
                        continue
                    narration._trace("DROPPING %d queued record(s) for seat(s) %s on table "
                           "%d -- finished %.0fs ago and nobody drained them "
                           "(POL_JAN_FINISHED_GRACE=%g)"
                           % (sum(len(t.outbox[x]) for x in owed),
                              ",".join(str(x) for x in owed), tid, held,
                              knobs.FINISHED_GRACE))
            self._keep_final(t)
            self.tables.pop(tid, None)
            for m, x in list(self.by_member.items()):
                if x is t:
                    del self.by_member[m]
            for k, x in list(self.by_lobby.items()):
                if x is t:
                    del self.by_lobby[k]
            for m in list(t.gallery):              # the store forgets them too
                self._store_gallery_drop(m)
            narration._trace("FORGET table %d (%s, idle %.0fs%s)"
                   % (tid, t.state, idle,
                      ", %d spectator(s)" % len(t.gallery) if t.gallery else ""))

    clock = staticmethod(time.monotonic)


# Every opcode the in-game loop can produce, plus the two the lobby sends on the
# way in. Anything else stays with janhourou.py's own responders.
INGAME_OPCODES = frozenset([
    M.MjREADY, M.MjHAIPAIACK, M.MjSUTE, M.MjNAKIACK, M.MjYAKUDISPACK,
    M.MjSEISANACK, M.MjALLDATA, M.MjALLDATAACK, M.MjBYE,
    M.MjGAMERESULTHALF1ACK, M.MjGAMERESULTHALF2ACK,
    M.MjSASHIUMAREQUEST, M.MjSASHIUMAARGEE, M.MjMEMBERLEAVEANSER,
    # The spectator's one ack (0x30, sub 6) -- routed to `Table.on_gallery`
    # by `Manager.handle`, never to `Table.handle` (GALLERY banner).
    M.MjGALLEYACK,
])
