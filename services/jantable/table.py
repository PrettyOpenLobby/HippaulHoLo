"""Table: one table's seats, sequencing, deadlines, seat loss, and per-seat delivery."""
import os
import random
import time
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
import collections  # noqa: E402  (local: keeps the import block above untouched)
from . import (bots, knobs, narration, tableflow, tablegallery, tableinbound, tablemsgs,
               tableresults, tablesashiuma, tablestate)


class _Log(collections.deque):
    """`Table.log`: a bounded deque that still slices like the list it was
    (`t.log[-4:]` is how every selftest and probe reads it)."""

    def __getitem__(self, i):
        if isinstance(i, slice):
            return list(self)[i]
        return collections.deque.__getitem__(self, i)


#: The round state every builder reads (see Table._round_scalars).
RoundScalars = collections.namedtuple(
    "RoundScalars", "chiicha dealer round_wind kyoku honba riichi_sticks dice "
                    "current_seat")


class CallSnapshot(tuple):
    """`(hands, ponds)` frozen before a call, plus the dead-wall state a kan
    record needs. Unpacks as a 2-tuple, so existing callers are untouched."""

    def __new__(cls, hands, ponds, dora_shown=1, kans=0):
        return tuple.__new__(cls, (hands, ponds))

    def __init__(self, hands, ponds, dora_shown=1, kans=0):
        self._extra = (dora_shown, kans)

    @property
    def dora_shown(self):
        return self._extra[0]

    @property
    def kans(self):
        return self._extra[1]


# --- one table ---------------------------------------------------------------

class Table(tablegallery.TableGallery, tablestate.TableState, tablemsgs.TableMessages,
            tableresults.TableResults, tablesashiuma.TableSashiuma, tableflow.TableFlow,
            tableinbound.TableInbound):
    """A mahjong table: four seats, a rule set, and the hand in progress."""

    def __init__(self, table_id=1, channel=b"#MJS0R001", rules=None, seed=None):
        self.id = table_id
        self.channel = channel
        self.rules = rules or mj.Rules()
        self.seats = [None, None, None, None]      # member id per seat, or None
        self.nicks = [None] * 4
        self.live = set()                          # seats with a real client
        self.bots = {}
        self._seisan_ready = set()   # human seats that acked the current MjSEISAN
        self.game = None
        self.kyoku = None
        self.state = "idle"
        self._seq = 0
        self._last_sent = None
        self.sashiuma = None                       # pre-game side-bet handshake
        self.sashiuma_pairs = []                   # the (a, b) bets that are ON
        self._outgoing = []            # [(rec, seat or None)] for this call
        self.outbox = {}               # seat -> [rec] waiting for it to speak
        self.members = {}              # our member id -> seat (routing)
        self._awaiting = None
        self._awaiting_since = None     # monotonic seconds, set when we send
        self.dropped = set()            # seats we have given up waiting on
        self._escape_seen = self._escape_mtime()
        self.rng = random.Random(seed if seed is not None else
                                 (int(knobs.GAME_SEED) if knobs.GAME_SEED else None))
        # Injectable so the deadline can be tested without sleeping through it.
        self.clock = time.monotonic
        self.log = _Log(maxlen=knobs.LOG_MAX)
        self.pending_naki = None       # (tile, {seat: options}) while a call is open
        self.naki_answers = {}         # seat -> (action, acked tile) collected so far
        #: DOUBLE RON: the winners after the first, waiting for their own win
        #: screen. MjYAKUDISP names ONE winner, so a two-seat ron has to be
        #: shown as two screens -- see on_wins().
        self._pending_wins = []        # [(seat, Score)] still to show
        self._pending_ron_from = None  # the discarder those screens name
        self._bot_call_depth = 0       # guards a chain of bot calls
        self._recorded = False         # this hanchan has been written down
        # --- 2026-09-04 (audit 6/7/8/29/31/32) ---------------------------------
        self.timeouts = {}             # seat -> consecutive deadline strikes
        self._last_in = {}             # seat -> (op, seq, word) of the last line APPLIED
        self._last_for = {}            # seat (or None) -> the last record sent to it
        self._awaiting_tag = None      # the `_awaiting` tuple `_awaiting_recs` describes
        self._awaiting_recs = []       # [(rec, to)] the current `_awaiting` waits on
        self._resent_for = None        # (awaiting, since) the sweeper re-sent once
        self._byes = set()             # live seats that answered MjGAMEEND with MjBYE
        self.finished_at = None        # when `_finish` ran (the reaper's grace)
        self._reap_held_said = False   # one HOLD line per table, not per sweep
        self.pending_chankan = None    # (seat, kind, pre_snapshot) in a chankan window
        self._hold_s = 0.0             # the deal delay, slept by Manager AFTER its lock
        self._managed = False          # True once a Manager owns this table
        self.last_line_at = self.clock()   # the idle TTL reads this
        # --- the gallery (see the GALLERY banner) -------------------------
        self.gallery = {}              # member -> gallery slot (NEVER a seat)
        self.gallery_acked = set()     # members whose first MjGALLEYACK arrived
        self.gallery_seq = {}          # member -> seq of the last record it acked
        self._gallery_copied = None    # (opcode, seq) of the last copy queued
        self._gallery_released = False # the store was told the game ended
        self._gallery_evicted = []     # members dropped for a full outbox

    # -- seats ---------------------------------------------------------------

    def seat_player(self, member_id, nick=None, seat=None, live=True,
                    member=None):
        """Seat a player. `member_id` is the WIRE id (the client's PolID, which
        is what MjNOTICEMEMBER's four slots carry); `member` is our own account
        number, kept so a record can be routed back to the right connection.

        The two were never distinguished while one human could be at a table --
        there was only ever one place to send anything.
        """
        if seat is None:
            seat = next((s for s in range(4) if self.seats[s] is None), None)
        if seat is None:
            return None
        self.seats[seat] = member_id
        self.nicks[seat] = nick
        if member:
            self.members[int(member)] = seat
        if live:
            self.live.add(seat)
            self.bots.pop(seat, None)
        return seat

    def seat_of_member(self, member):
        """Our account number -> its seat, or None."""
        try:
            return self.members.get(int(member))
        except (TypeError, ValueError):
            return None

    #: A bot seat's member id. WARNING: IT MUST BE NON-ZERO, and that is MEASURED, not
    #: taste: `malloc__002c67a0` derives the SEATED PLAYER COUNT by counting how
    #: many of MjNOTICEMEMBER's four u64 ids at +0x30 are non-zero --
    #:
    #:     do { if (*(longlong *)(p + 0x30) != 0) count++; p += 8; } while (i < 4);
    #:
    #: -- and stores it in `_DAT_00446098` (`GetTableMemberList`). Seating three
    #: bots as id 0 therefore told a live client the table had ONE player, and it
    #: sat on 「皆さんをお待ちしております」 ("waiting for everyone") for ever.
    #: Confirmed on the console 2026-08-17: the game-start pair was accepted, the
    #: table drew READY!, and then it waited -- correctly, on our own data.
    #: The high nibble keeps them clearly distinguishable from a real POL id in
    #: a capture.
    BOT_ID_BASE = 0x0B07000000000000

    def bot_id(self, seat):
        return self.BOT_ID_BASE | (self.id << 8) | seat

    def fill_with_bots(self):
        for s in range(4):
            if self.seats[s] is None:
                self.seats[s] = self.bot_id(s)
                self.bots[s] = bots.Bot(s)
        return self.bots

    def member_ids(self):
        """The four seat ids as MjNOTICEMEMBER wants them -- all non-zero once
        the table is full, because that count IS the client's player count."""
        return [self.seats[s] or self.bot_id(s) for s in range(4)]

    def member_of_seat(self, seat):
        """Our account number for a seat, or its wire id when we hold none
        (the solo `table_for` path seats the wire id as the member)."""
        for m, s in self.members.items():
            if s == seat:
                return m
        return self.seats[seat]

    # -- sequencing ----------------------------------------------------------

    def next_seq(self):
        """A byte that DIFFERS from the last one we sent.

        The client treats an equal `rec[0x13]` as a duplicate: it re-sends its
        previous message and does NOT handle ours. So this is not decoration --
        reusing a value silently stalls the hand.
        """
        self._seq = (self._seq + 1) & 0x7F
        if self._seq == 0:
            self._seq = 1
        return self._seq

    # -- the deadline --------------------------------------------------------
    #
    # WARNING: THE TURN CLOCK IS THE CLIENT'S, NOT OURS -- and that is measured.
    # `mahdisp__Skip_Request_002c21a0` is the `LimitTimeManager` thread:
    #
    #     if (timer_running) {
    #         ticks++;
    #         if (DAT_00445b87 * 0x3c <= ticks) { skip = 1; ... }
    #         yamaguchi2__TimeLimmit(widget, DAT_00445b87 - ticks / 0x3c);
    #     }
    #
    # `DAT_00445b87` is the limit in SECONDS (x0x3c = frames) and it is only
    # ever READ in the module -- it comes from the table config we author, i.e.
    # SE's own `MJS_CONFIG_IDX_WAIT_TIME`. When it expires the client sets a
    # flag that BOTH pickers poll (`mahdisp__002c2140`): the discard picker
    # returns its default slot and the call picker returns 0 = pass. A
    # spectator gets the same flag from an `MjGALLEYACK` (0x30) on sub 6.
    #
    # So a slow human cannot hang a table -- the client plays for them. What CAN
    # hang a table is a client that says nothing at all: a crash, a pulled
    # network cable, a PCSX2 window closed. Nothing arrives, no ack, no skip.
    # That is what this deadline is for, and it is why it must be LONGER than
    # the client's own clock -- otherwise we would race a player who is simply
    # thinking, and play a tile they did not choose.
    TURN_GRACE = float(os.environ.get("POL_JAN_TURN_GRACE", "15"))

    def deadline(self):
        return float(getattr(self.rules, "wait_time", 30)) + self.TURN_GRACE

    #: The broadcast acks a table can be waiting on (`_awaiting = (op, None)`).
    #: Every one of them has a ladder step in `_ack_advance`.
    BROADCAST_ACKED = frozenset([M.MjHAIPAI, M.MjYAKUDISP, M.MjSEISAN,
                                 M.MjGAMERESULTHALF1, M.MjGAMERESULTHALF2,
                                 M.MjGAMEEND])

    def tick(self, now):
        """The deadline, for a seat OR a broadcast ack.

        A silent SEAT gets its default move played, exactly as its own client
        would have; a seat that strikes the deadline TIMEOUTS_TO_DROP times in
        a row is a client that is gone and is dropped (finding 31/33). A
        BROADCAST ack (deal, win screen, settlement, results, game end) that
        nobody has answered is re-sent ONCE and then treated as acked by the
        seats that never answered (finding 31: these waits used to have no
        deadline at all). Returns the records that follow, or [].
        """
        if knobs.SHUTDOWN and self.state == "playing":
            self.log.append(("escape", "shutdown -- swept"))
            self.trace("GAMEEND (shutdown -- the sweeper ends the live game)")
            return [self.msg_gameend()]
        if self._awaiting is None or self._awaiting_since is None:
            return []
        if self.state not in ("playing", "over"):
            return []
        op, seat = self._awaiting
        if now - self._awaiting_since < self.deadline():
            return []
        if seat is None:
            if not self.live or op not in self.BROADCAST_ACKED:
                return []                   # nobody to hear it / not a ladder ack
            if self._resent_for == (self._awaiting, self._awaiting_since):
                self.log.append(("timeout-broadcast", (op, round(now - self._awaiting_since, 1))))
                self.trace("TIMEOUT nobody acked %s twice -- proceeding as if the "
                           "silent seats had" % narration.M_NAME(op))
                if op == M.MjSEISAN:
                    # ...and tell the gauges so: every live seat counts as ready.
                    self._seisan_ready |= set(self.live)
                    return self._ready_status(None) + (self._ack_advance(op) or [])
                return self._ack_advance(op) or []
            self.log.append(("resend", (op, round(now - self._awaiting_since, 1))))
            out = self._renudge_all()
            self._awaiting_since = now
            self._resent_for = (self._awaiting, now)
            return out
        if seat not in self.live:
            return []                       # nothing is waiting on a person
        strikes = self.timeouts[seat] = self.timeouts.get(seat, 0) + 1
        self.log.append(("timeout", (seat, op, round(now - self._awaiting_since, 1))))
        if strikes >= knobs.TIMEOUTS_TO_DROP:
            self.trace("TIMEOUT %s struck %d times in a row -- dropping the seat"
                       % (self._seatname(seat), strikes))
            return self.drop_seat(seat, "timed out %d times" % strikes)
        return self.play_default_for(seat, op)

    def _ack_advance(self, op):
        """The ladder step an ack of broadcast `op` buys. ONE writer for the
        ack arms in `handle()` AND for the sweeper's proceed-as-acked, so the
        two cannot disagree. None = `op` is not a ladder ack."""
        if op in self.GALLERY_SKIP_AFTER:
            # The players are done with this screen: tell the spectators'
            # LimitTimeManager to skip theirs (GALLERY banner; off by default).
            self._gallery_skip()
        if op == M.MjHAIPAI:
            return self.advance()
        if op == M.MjYAKUDISP:
            # WARNING: A DOUBLE RON HAS TWO WINNERS AND ONE WIN SCREEN (2026-09-12):
            # `_settle` paid both, but every call site showed `wins[0]` only, so
            # the seat that lost the head-bump order never saw its own hand --
            # seen live: a player ronned and watched the win screen reveal the OTHER
            # player. Each ack pulls the next winner's screen; SEISAN waits
            # until the queue is empty.
            if self._pending_wins:
                seat, score = self._pending_wins.pop(0)
                return self.on_win(seat, score, ron_from=self._pending_ron_from)
            return [self.msg_seisan()]
        if op == M.MjSEISAN:
            return self.next_kyoku_or_end()
        if op == M.MjGAMERESULTHALF1:
            return [self.msg_results2()]
        if op == M.MjGAMERESULTHALF2:
            return [self.msg_gameend()]
        if op == M.MjGAMEEND:
            self._finish("every live seat left (or timed out) after MjGAMEEND")
            return []
        return None

    def _finish(self, why):
        """The table is done: nothing is awaited and the Manager may forget it.

        `finished_at` is what the reaper's grace window is measured from -- see
        `Manager._reap`: a table whose last records are still queued for a human
        who has not said MjBYE must outlive its own game for a few seconds.
        """
        if self.state != "finished":
            self.log.append(("finished", why))
            self.finished_at = self.clock()
        self.state = "finished"
        self._awaiting = None
        self._awaiting_since = None

    def play_default_for(self, seat, op):
        """The move the client's own skip flag would have produced.

        Discard: its default slot (the drawn tile). A call: pass. Anything else
        is an ack we can simply proceed without.
        """
        k = self.kyoku
        if k is None or k.result is not None:
            return []
        if op == M.MjTSUMO:
            h = k.hands[seat]
            if k.turn != seat or h.tile_total() != 14:
                return []                   # not actually this seat's move
            if k.pending_kan is not None:   # its own kan is mid-window: no-op
                return []
            forbidden = k.kuikae_forbidden(seat)
            tile = h.drawn if h.drawn in h.tiles else None
            if tile is None or mj.kind(tile) in forbidden:
                tile = next((t for t in reversed(h.tiles)
                             if mj.kind(t) not in forbidden), h.tiles[-1])
            k.discard(seat, tile=tile)
            return self._continue(tile, seat)
        if op == M.MjNAKI:
            if self.pending_naki is not None:
                # WARNING: An answer already COLLECTED must not be swept away with
                # the silence (the arbitration hold): seat A's ron waiting on
                # seat B's timeout is still a ron. The silent seats pass; the
                # answers on file resolve exactly as if the last ack had come.
                tile, options = self.pending_naki
                answers, self.naki_answers = self.naki_answers, {}
                for s in options:
                    answers.setdefault(s, ("pass", None))
                self.pending_naki = None
                return self._resolve_naki(tile, options, answers,
                                          k.last_discard_seat)
            res = self.resolve_bot_calls(k.last_discard, k.last_discard_seat)
            if res is not None:
                return res
            k.advance()
            if k.result is not None:
                return self.on_kyoku_end()
            return self.advance()
        # anything else a seat can be waited on for: re-nudge it. The client
        # dedups on the sequence byte, so a genuine straggler is safe.
        return self._renudge(seat)

    # -- a seat leaving ------------------------------------------------------

    def drop_seat(self, seat, reason="left"):
        """A seat is gone: tell the table, and play the rest of the hand for it.

        WARNING: `MjMEMBERLEAVE` puts a MODAL yes/no dialog in front of the other three
        (see janmsgs), so this costs each of them an answer before anything else
        moves. That is the client's design, not ours.
        """
        if seat in self.dropped:
            return []
        was_awaiting = self._awaiting
        self.dropped.add(seat)
        self.live.discard(seat)
        self.timeouts.pop(seat, None)
        self.bots[seat] = bots.Bot(seat)         # the hand plays on, seat automated
        self.log.append(("drop", (seat, reason)))
        self.trace("LEAVE %s (%s) -- a CPU plays the seat from here"
                   % (self._seatname(seat), reason))
        if not self.live:
            # The last person is gone: there is nobody to announce it to and
            # nothing to play for. The Manager reaps a finished table.
            if self.state == "playing":
                # the web board says WHY the game stopped (public_state)
                self.ended_early = {"seat": seat, "reason": reason}
            self._finish("last live seat %d %s" % (seat, reason))
            return []
        rec = M.memberleave(self.next_seq(), seat=seat,
                            member_id=self.seats[seat] or 0)
        out = [self._emit(rec, "seat %d %s" % (seat, reason))]
        k = self.kyoku
        if self.state == "playing" and k is not None and k.result is None:
            # WARNING: THE TABLE MUST NEVER BE LEFT WAITING ON THE SEAT THAT LEFT.
            # `tick` skips a seat that is not live, so an `_awaiting` still
            # naming the leaver would stall the hand FOREVER, not 75s. Whatever
            # was owed is settled here, now:
            if self.pending_naki is not None:
                # The leaver's unanswered call offer becomes a pass. If live
                # seats are still deciding, point the deadline at one of them;
                # otherwise the offer is closed and resolves on the spot.
                tile, options = self.pending_naki
                if seat in options:
                    self.naki_answers.setdefault(seat, ("pass", None))
                blockers = [s for s in sorted(options)
                            if s in self.live and s not in self.naki_answers]
                if blockers:
                    self._awaiting = (M.MjNAKI, blockers[0])
                    return out
                answers, self.naki_answers = self.naki_answers, {}
                self.pending_naki = None
                return out + self._resolve_naki(tile, options, answers,
                                                k.last_discard_seat)
            if was_awaiting and was_awaiting[1] == seat:
                # The leaver held the turn: play its default immediately,
                # exactly as the deadline sweeper would have -- 75s from now.
                return out + self.play_default_for(seat, was_awaiting[0])
            # Waiting on another seat or on a broadcast ack: leave `_awaiting`
            # ALONE. The SEISAN/GAMERESULT ladders advance only while it still
            # names their record, and clobbering it here would wedge them for
            # the seats still playing.
            return out
        # WARNING: BETWEEN HANDS -- the win screen, the settlement, the results, the
        # end -- `_awaiting` is a BROADCAST ack (seat None) and it STAYS
        # EXACTLY AS IT IS (audit finding 6, probe P1). This used to set
        # `_awaiting = (MjMEMBERLEAVE, None)`: nothing acks MjMEMBERLEAVE, so
        # the survivor's YAKUDISPACK/SEISANACK no longer matched what the
        # ladder was waiting on and the table wedged for ever. The leaver's
        # outstanding ack is settled by the fact that any live seat's ack
        # advances the ladder, and `tick()` proceeds when nobody is left to.
        # Left during the pre-game side-bet handshake (no hand yet): the seat
        # is a bot now and no longer waited on -- finish the phase if it was
        # the last answer outstanding, so the others are not held for ever.
        if self.sashiuma and self.sashiuma["phase"] != "done":
            return out + self._sashiuma_seat_gone(seat)
        return out

    def rejoin(self, seat):
        """A member re-entered a playing table (finding 29): put the human back
        on its stored seat -- the bot that was substituted for it, if any,
        goes -- and resync it: a full MjALLDATA for that seat, plus the record
        it is being waited on for if it is its move."""
        if seat is None:
            return []
        was_bot = seat in self.bots
        self.bots.pop(seat, None)
        self.dropped.discard(seat)
        self.timeouts.pop(seat, None)
        self._byes.discard(seat)
        self.live.add(seat)
        self.log.append(("rejoin", (seat, was_bot)))
        self.trace("REJOIN %s%s -- resyncing"
                   % (self._seatname(seat), " (was a CPU)" if was_bot else ""))
        k = self.kyoku
        if k is None or self.state != "playing":
            return []
        out = [self.msg_alldata(label="rejoin resync seat %d" % seat, to=seat)]
        if k.result is not None:
            return out + self._renudge(seat)
        if (self.pending_naki is not None and seat in self.pending_naki[1]
                and seat not in self.naki_answers):
            return out + self._renudge(seat)
        if (k.turn == seat and k.hands[seat].tile_total() == 14
                and k.pending_kan is None):
            out.append(self.msg_tsumo(seat))
        return out

    # -- helpers -------------------------------------------------------------

    def _seatname(self, seat):
        """`seat 1 (right, CPU)` -- names the seat AND where it sits on the local
        player's screen, so a trace line can be matched to what is drawn."""
        if seat is None:
            return "seat ?"
        who = "YOU" if seat in self.live else "CPU"
        return "seat %d (%s, %s)" % (seat, narration._SEATPOS.get(seat, "?"), who)

    def trace(self, msg):
        """One human-readable move, tagged with the table and round so a long
        log stays legible. Goes to janhourou.log via the module TRACE hook."""
        k = self.kyoku
        rnd = ""
        if k is not None:
            rnd = "[t%s %s%d h%d] " % (self.id,
                                       "ESWN"[(k.round_wind - mj.EAST) & 3],
                                       (k.kyoku & 3) + 1, k.honba)
        narration._trace(rnd + msg)

    def _emit(self, rec, why="", to=None, gallery=None):
        """Record one outgoing message. `to` is the seat it is FOR, or None for
        every live seat. `gallery` is the copy the SPECTATORS get instead of
        `rec` (a builder passes one when `rec` conceals a hand); None = `rec`.

        WARNING: ROUTING IS ADDITIVE ON PURPOSE. Callers still get `rec` back and
        still build their own lists exactly as before, so with one live seat
        this changes nothing at all -- every route is either None or that seat,
        `Manager.handle` hands the whole list to the caller, and no outbox is
        ever written. The routes are a SIDE CHANNEL that only matters once a
        second human is at the table, which is the only case that was broken.
        """
        self._last_sent = rec
        self._awaiting_since = self.clock()
        h = janwire.unpack(rec)
        self.log.append((h["opcode"], why))
        self._outgoing.append((rec, to))
        # Per-seat "the last record you were sent" and "the record(s) the
        # current `_awaiting` is waiting on" -- what `_renudge` re-sends. A
        # builder assigns a NEW tuple to `_awaiting` before it emits, so
        # identity is the change detector; MjMEMBERLEAVE (emitted under an
        # unchanged wait) joins the list and is filtered out by opcode.
        if to is None:
            for s in self.live:
                self._last_for[s] = rec
            self._last_for[None] = rec
        else:
            self._last_for[to] = rec
        if self._awaiting is not self._awaiting_tag:
            self._awaiting_tag = self._awaiting
            self._awaiting_recs = []
        self._awaiting_recs.append((rec, to))
        # The spectators' copy -- of EVERY record, `to=seat` ones included
        # (GALLERY banner). Re-sends (`_renudge*`) bypass `_emit` on purpose:
        # a same-seq record would only make the spectator re-send its ack.
        self._gallery_copy(rec, h, gallery)
        return rec

    def _awaited_recs(self):
        op = self._awaiting[0] if self._awaiting else None
        return [(r, to) for r, to in self._awaiting_recs
                if janwire.unpack(r)["opcode"] == op]

    def _renudge(self, seat):
        """Re-send what `seat` is waiting on: the awaited record(s) addressed
        to it, else the last record it was sent. NOT re-stamped (the client
        dedups on the sequence byte: a straggler is harmless, a lost record
        is recovered) and routed through `_outgoing`, so `_route` never has to
        bypass for it -- the old `[self._last_sent]` was per-table, not
        per-seat, and with two humans handed one seat the other's record."""
        if seat is None:
            return []
        recs = [r for r, to in self._awaited_recs() if to is None or to == seat]
        if not recs:
            last = self._last_for.get(seat)
            recs = [last] if last is not None else []
        for r in recs:
            self._outgoing.append((r, seat))
        return recs

    def _is_human(self):
        """The +0x23..+0x26 IsHuman bytes (janmsgs `_put_is_human`): a seat a
        bot plays is a COM to the client, anything else is a human -- a seat
        whose client has not said READY yet included, so its panel and gauge
        take the human form from the first deal."""
        if not knobs.ISHUMAN_ENABLE:
            return (0, 0, 0, 0)
        return tuple(0 if s in self.bots else 1 for s in range(4))

    def _ready_status(self, seat):
        """MjREADYSTATUS after a MjSEISANACK: `seat` is ready; broadcast the
        four bytes (bots always ready, humans once they acked this MjSEISAN --
        `msg_seisan` clears the set). Routed through `_outgoing` like a
        re-send, NOT `_emit`: it is not a ladder record, so it must not
        become `_last_for` (a re-nudge would re-send it instead of the deal)
        and must not bump `_awaiting_since`. Its own sequence byte: the
        client only ever compares a seq with the PREVIOUS one (a gap is
        nothing, an equal byte is a retransmission), and the selftest's
        dedup guard walks every record in order."""
        if not knobs.ISHUMAN_ENABLE or self.game is None:
            return []
        if seat is not None and seat not in self.bots:
            self._seisan_ready.add(seat)
        ready = tuple(1 if (s in self.bots or s in self._seisan_ready) else 0
                      for s in range(4))
        rec = M.readystatus(self.next_seq(), ready)
        self.log.append((M.MjREADYSTATUS, "ready %r" % (ready,)))
        self._outgoing.append((rec, None))
        return [rec]

    def _renudge_all(self):
        """Re-send the awaited broadcast to every live seat, on its own route."""
        out = []
        for r, to in self._awaited_recs():
            if to is not None and to not in self.live:
                continue
            self._outgoing.append((r, to))
            out.append(r)
        return out

    def take_hold(self):
        """Seconds the Manager should sleep AFTER releasing its lock before
        answering -- the deal delay, which used to sleep inside `msg_haipai`
        and would now hold every other table and the sweeper with it."""
        s, self._hold_s = self._hold_s, 0.0
        return s

    # -- per-seat delivery ---------------------------------------------------
    #
    # The transport is reply-first: a record leaves on the socket of whoever
    # just spoke. That is fine for the seat taking its turn and useless for the
    # other three -- so anything for another seat waits in their outbox. Three
    # things drain it: that member's next game-band line (`handle_line`), the
    # `<DR>` poll of a client still in the room, and responders' idle push.
    # WARNING: The third is not optional: an in-game client that is WAITING for us
    # sends NOTHING (measured live 2026-09-04 -- responders' idle-push banner;
    # only a client on an ack screen re-sends, ~2 s). The deadline sweeper
    # thread queues here too (`Manager.tick`), and `Manager.on_swept` lets the
    # transport push at once.

    def begin_routing(self):
        self._outgoing = []

    def take_routes(self):
        out = self._outgoing
        self._outgoing = []
        return out

    def queue_for(self, seat, rec):
        self.outbox.setdefault(seat, []).append(rec)

    def take_outbox(self, seat, limit=None):
        """What is waiting for `seat`. `limit` leaves the remainder QUEUED.

        The remainder must survive: the client applies strictly in order and
        this band never resends, so a record dropped because a burst was too
        big is a hand that stops.
        """
        got = self.outbox.get(seat) or []
        if limit is None or len(got) <= limit:
            self.outbox.pop(seat, None)
            return got
        self.outbox[seat] = got[limit:]
        return got[:limit]
