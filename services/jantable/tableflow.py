"""The drive loop: the deal, turns, bot moves, calls, kans, wins and the end of a hand."""
import os
import janmahjong as mj                                             # noqa: E402
from .deps import janstats
from . import bots, knobs, narration, tablesashiuma


class TableFlow:
    """Part of `Table` (table.py), which inherits it.

    The drive loop: the deal, turns, bot moves, calls, kans, wins and the end
    of a hand.
    """

    # -- the drive loop ------------------------------------------------------

    def start_game(self):
        self.fill_with_bots()
        self.game = mj.Game(self.rules, rng=self.rng)
        self.state = "playing"
        # WARNING: RESET THE RECORD GUARD HERE, not in `msg_results`. `Manager.by_member`
        # keeps a Table for the life of the process, so the SECOND hanchan at
        # this table runs on the same object -- and a `_recorded` left True would
        # silently drop every game after the first. The reset has to sit on the
        # thing that starts a game, or it never runs for the case it exists for.
        self._recorded = False
        self.sashiuma = None
        self.sashiuma_pairs = []
        if tablesashiuma.SASHIUMA and self.live:
            return self._sashiuma_begin()
        return self.start_kyoku()

    def reset_for_new_game(self):
        """Clear per-hand state so the NEXT MjREADY deals a fresh game.

        WARNING: WHY (2026-09-02, live): `Manager.by_member` keeps one Table per member
        for the whole process, and the MjREADY handler only calls `start_game()`
        when `state != "playing"`. A hand that ends cleanly leaves state "over",
        but a hand that ERRORS OUT mid-play (e.g. a `None` kyoku after an
        exception) leaves it stuck at "playing" for ever -- so every subsequent
        Start Game drives the notices, the client sends MjREADY, and the handler
        returns nothing ("the table is waiting on another seat"), no deal. Called
        from janhourou's MjGAMESTART drive -- the master pressing Start Game is
        the explicit "new game" signal. Keeps the seats/table identity; the
        seat_player + fill_with_bots that follow re-establish `live`.
        """
        self.state = "idle"
        self.game = None
        self.kyoku = None
        self._awaiting = None
        self._awaiting_since = None
        self.pending_naki = None
        self.naki_answers = {}
        self._pending_wins = []
        self._pending_ron_from = None
        self.dropped = set()
        self._recorded = False
        self.live = set()          # re-added by the drive's seat_player(live=True)
        self.outbox = {}           # nothing from the last hanchan may survive
        self._outgoing = []
        self.sashiuma = None
        self.sashiuma_pairs = []
        self.timeouts = {}
        self._last_in = {}
        self._last_for = {}
        self._awaiting_tag = None
        self._awaiting_recs = []
        self._resent_for = None
        self._byes = set()
        self.pending_chankan = None
        self._hold_s = 0.0
        self._last_sent = None
        # A new game starts with an empty gallery: the last one's spectators
        # were dropped at MjGAMEEND (the client left the table screen then).
        self.gallery = {}
        self.gallery_acked = set()
        self.gallery_seq = {}
        self._gallery_copied = None
        self._gallery_released = False
        self._gallery_evicted = []

    #: THE TILE-NUMBERING PROBE. `POL_JAN_DEAL="123456789m1234p"` forces seat
    #: 0's opening hand to exactly those tiles, so ONE screenshot settles what
    #: no amount of looking at a random hand can: whether `janmsgs.WIRE_ID` maps
    #: rank correctly inside a suit.
    #:
    #: The 2026-08-17 live run confirmed the SUIT order (the hand drew
    #: man -> pin -> sou, which a permuted mapping could not produce) but 2m vs
    #: 3m is not readable off a screenshot of a random hand. A known ascending
    #: run is readable at a glance by anyone, mahjong player or not: if the
    #: screen shows 1-9 of characters followed by 1-4 of circles, the mapping is
    #: right; any other order names its own error.
    #:
    #: WARNING: Deliberately crude: it OVERWRITES the dealt hand without removing those
    #: tiles from the wall, so duplicates are possible and the hand is not a
    #: legal deal. That is fine for reading a screen and wrong for anything else,
    #: which is why it is off unless the variable is set.
    #: WARNING: AN ENV VAR WAS THE WRONG CHOICE HERE and it cost a live run: setting
    #: `$env:POL_JAN_DEAL` on the host does NOT reach the container. `docker
    #: compose` only injects variables the compose file declares, so a bare
    #: shell variable is used for interpolating the YAML and nothing else -- the
    #: probe silently never ran. It also needs a container restart, which ends
    #: the player's session.
    #:
    #: So the file wins, exactly as `config/polpro.json` does for the plaintext
    #: channel: `config/` is already bind-mounted, and this is re-read per deal,
    #: so trying a different hand costs one text edit with the client still
    #: connected. The env var stays as an override for the selftests.
    DEAL_FILE = os.environ.get("POL_JAN_DEAL_FILE", "/config/jan_deal.txt")
    DEBUG_DEAL = os.environ.get("POL_JAN_DEAL", "")

    def _deal_spec(self):
        """The forced hand, from the env var or the live-editable file."""
        if self.DEBUG_DEAL:
            return self.DEBUG_DEAL
        try:
            with open(self.DEAL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        return line
        except OSError:
            pass
        return ""

    def _apply_debug_deal(self):
        spec = self._deal_spec()
        if not spec:
            return
        try:
            tiles = mj._hand_from(spec)
        except Exception as e:
            self.log.append(("bad-jan-deal", "%s: %s" % (spec, e)))
            return
        seat = min(self.live) if self.live else 0
        h = self.kyoku.hands[seat]
        want = 14 if seat == self.kyoku.dealer else 13
        h.tiles = (tiles + h.tiles)[:want]
        self.log.append(("debug-deal", (seat, mj.hand_str(h.tiles))))

    def start_kyoku(self):
        self.kyoku = self.game.start_kyoku()
        self.pending_naki = None
        self.naki_answers = {}
        self._pending_wins = []
        self._pending_ron_from = None
        self._apply_debug_deal()
        return self.msg_haipai()        # already a list: one record per seat

    def advance(self):
        """Run the table forward until a live seat has to decide something.

        Returns the records to send. This is the whole of the server's game
        logic: bots play instantly, and the loop stops the moment a human's
        input is needed -- which is exactly when a message goes out.
        """
        out = []
        guard = 0
        k = self.kyoku
        while k.result is None and guard < 400:
            guard += 1
            seat = k.turn
            h = k.hands[seat]
            if h.drawn is None and h.tile_total() < 14:
                if k.draw_for(seat) is None:
                    break                       # the wall ran out -> finish_draw
                h = k.hands[seat]
            if seat in self.live:
                out.append(self.msg_tsumo(seat))
                return out
            # --- a bot's turn ---------------------------------------------
            # WARNING: A BOT'S TURN IS VISIBLE OR IT DIDN'T HAPPEN (2026-09-02,
            # live): the old loop played bots in silence and the player
            # watched the game "skip to my turn with the tile count lower by
            # 3-4". The turn now animates; DISCARD_ANIM picks how (see there).
            recs, done = self._bot_turn(seat)
            out += recs
            if done:
                return out
        if k.result is not None:
            return out + self.on_kyoku_end()
        return out

    def _bot_turn(self, seat, drew_shown=False):
        """One bot's move from the tile it holds -- win, kan, riichi, discard --
        and whatever that discard provokes. Returns `(records, done)`: `done`
        means the caller must return, because a live seat is deciding or the
        hand ended (the end records are included).

        `drew_shown`: the draw this turn is already on screen (a kan's
        rinshan rides inside `msg_kan`), so the discard must not pop it
        again. Used by `advance()` for an ordinary turn and by `bot_call` /
        `_do_kan` for the turn that follows a call.
        """
        k = self.kyoku
        h = k.hands[seat]
        bot = self.bots.get(seat) or bots.Bot(seat)
        out = []
        if h.drawn is not None:
            sc = mj.can_tsumo(h, k.context_for(seat, h.drawn, True))
            if sc is not None:
                if not drew_shown:
                    out.append(self.msg_tsumo(seat))    # show the winning draw
                k.win_tsumo(seat)
                return out + self.on_win(seat, sc), True
            want = bot.wants_kan(k)
            if want is not None:
                out += self._do_kan(seat, want[0], want[1])
                if k.result is not None or self.pending_chankan is not None:
                    return out, True
                recs, done = self._bot_turn(seat, drew_shown=True)
                return out + recs, done
        riichi = bot.wants_riichi(k)
        tile = bot.choose_discard(k)
        # The slot the tile leaves, read BEFORE discard() removes it.
        slot = h.tiles.index(tile) if tile in h.tiles else len(h.tiles) - 1
        had_draw = h.drawn is not None and not drew_shown
        if had_draw and knobs.DISCARD_ANIM != "subtype3":
            out.append(self.msg_tsumo(seat))            # the draw, motion 2
        k.discard(seat, tile=tile, riichi=riichi)
        if had_draw and knobs.DISCARD_ANIM == "subtype3":
            # ONE MjALLDATA subtype 3 (draw+prep): the client pops the tile
            # onto the bot's hand AND clears its last pond slot's settled
            # flag IN-HANDLER (the local discard's order), so the motion-3
            # that follows flies with NO settled pre-draw -- no slingshot.
            # The pond already ends with the discard (k.discard ran). This
            # subtype-3 pop IS the draw beat; a separate motion-2 draw would
            # double it. A bot that CALLED (pon/chi/kan) has no draw beat
            # here -- `_show_discard` below covers that turn.
            out.append(self.msg_alldata(discard_event=(tile, seat)))
            out.append(self.msg_tsumo(seat, discard=(tile, slot, seat, riichi)))
            self.trace("DRAW+DISCARD %s tile=%s  [subtype-3 prep + motion-3]"
                       % (self._seatname(seat), narration._tile(tile)))
        elif had_draw and knobs.DISCARD_ANIM == "appear":
            # No fly: the discard just shows up in the pond (subtype-0
            # populate marks it settled and the renderer draws it).
            out.append(self.msg_alldata())
            self.trace("DISCARD %s tile=%s  [appear: populate, no fly]"
                       % (self._seatname(seat), narration._tile(tile)))
        elif had_draw:  # "fly" -- accurate landing, but the discard flashes settled
            out.append(self.msg_alldata())
            out.append(self.msg_tsumo(seat, discard=(tile, slot, seat, riichi)))
            self.trace("DISCARD %s tile=%s  [fly: subtype-0 populate + motion-3]"
                       % (self._seatname(seat), narration._tile(tile)))
        else:
            out += self._show_discard(seat, tile, slot, riichi,
                                      after_draw=drew_shown)
        recs, done = self._after_discard(tile, seat)
        return out + recs, done

    def _after_discard(self, tile, seat):
        """What a discard provokes: a call offer to the live seats, a bot's
        claim, or the next draw. Returns `(records, done)` like `_bot_turn`;
        `done` False means the caller may keep running `advance()`."""
        k = self.kyoku
        calls = self.offer_calls(tile, seat)
        if calls:
            return [self.msg_naki(tile, calls)], True
        res = self.resolve_bot_calls(tile, seat)
        if res is not None:
            return res, True
        k.advance()
        if k.result is not None:
            return self.on_kyoku_end(), True
        return [], False

    def _continue(self, tile, seat):
        """`_after_discard` for a HUMAN's discard: the records, run forward."""
        recs, done = self._after_discard(tile, seat)
        return recs if done else recs + self.advance()

    def offer_calls(self, tile, from_seat):
        """Which LIVE seats have a legal call on `tile`. Bots resolve separately."""
        out = {}
        k = self.kyoku
        for seat in sorted(self.live):
            if seat == from_seat:
                continue
            opts = self.legal_calls(seat, tile, from_seat)
            if opts:
                out[seat] = opts
        return out

    def legal_calls(self, seat, tile, from_seat):
        k = self.kyoku
        h = k.hands[seat]
        opts = {}
        ctx = k.context_for(seat, tile, False, from_seat=from_seat)
        if mj.can_ron(h, tile, ctx):
            opts["ron"] = True
        if h.riichi or getattr(k, "_fourth_kan_discard", False):
            # A riichi hand may only ron -- and so may EVERYONE on the discard
            # after a shared fourth kan: suukaikan unless it is ronned, and
            # the engine aborts the hand the moment that discard passes.
            return opts
        if mj.pon_option(h, tile):
            opts["pon"] = True
        if (mj.minkan_option(h, tile) and k.kan_count() < 4
                and k.wall.remaining >= 1):
            opts["kan"] = True
        if seat == (from_seat + 1) % 4 and mj.chi_options(h, tile):
            opts["chi"] = True
        return opts

    def resolve_bot_calls(self, tile, from_seat):
        """The bots' claims on a discard no LIVE seat was offered: ron first,
        then kan/pon, then chi -- each in turn order.

        WARNING: THE ORDER IS TURN ORDER FROM THE DISCARDER, not seat 0..3. It decides
        two real things: who takes the riichi sticks when several seats ron the
        same tile, and -- with MJS_CONFIG_IDX_DOUBLE_RON off -- WHICH of them
        wins at all (head bump / atamahane). Iterating range(4) made seat 0 the
        favourite for no reason.
        """
        k = self.kyoku
        winners = []
        for step in range(1, 4):
            seat = (from_seat + step) % 4
            if seat in self.live:
                continue
            if "ron" in self.legal_calls(seat, tile, from_seat):
                winners.append(seat)
        if winners:
            wins = k.win_ron(winners, tile=tile, from_seat=from_seat)
            if wins:
                return self.on_wins(wins, ron_from=from_seat)
            if k.result is not None:
                return self.on_kyoku_end()      # sanchahou: three rons abort
        # --- no ron: kan, then pon, then chi, still in turn order -------------
        # A pon/kan may be claimed by ANY seat and beats a chi, which only the
        # discarder's left neighbour can make -- so the whole table is asked
        # about tile claims before anyone is asked about chi.
        for want in ("kan", "pon", "chi"):
            for step in range(1, 4):
                seat = (from_seat + step) % 4
                if seat in self.live:
                    continue
                opts = self.legal_calls(seat, tile, from_seat)
                if want not in opts:
                    continue
                bot = self.bots.get(seat) or bots.Bot(seat)
                if bot.calls(k, tile, opts) == want:
                    return self.bot_call(seat, want, tile, from_seat)
        return None

    #: How deep a chain of bot-calls-then-discards may go before we stop
    #: honouring calls for this discard. Three bots can in principle keep
    #: claiming each other's discards; the hand still has to end.
    BOT_CALL_MAX_DEPTH = 6

    def bot_call(self, seat, action, tile, from_seat):
        """A bot takes a pon, chi or daiminkan: form the meld, ANIMATE it,
        then play the turn that follows.

        This is the same `msg_call()` / `msg_kan()` the human path uses, so a
        CPU's pon now fires the banner and flies the tiles exactly as yours
        does -- which until 2026-09-04 was impossible, because bots never
        called.
        """
        k = self.kyoku
        if self._bot_call_depth >= self.BOT_CALL_MAX_DEPTH:
            self.log.append(("bot-call-depth", (seat, action)))
            return None
        h = k.hands[seat]
        bot = self.bots.get(seat) or bots.Bot(seat)
        pre = self.call_snapshot()
        if action == "chi":
            chosen = bot._take_for(h, tile, "chi")
            if not chosen:
                return None
            k.call_chi(seat, chosen)
        elif action == "pon":
            k.call_pon(seat)
        elif action == "kan":
            k.call_pon(seat, kan=True)          # the rinshan draw happens inside
        else:
            return None
        self._bot_call_depth += 1
        try:
            if action == "kan":
                out = self.msg_kan("minkan", seat, pre, discarder=from_seat)
                recs, done = self._bot_turn(seat, drew_shown=True)
            else:
                out = [self.msg_call(action, seat, pre, from_seat)]
                # It is now this seat's turn WITHOUT a draw, so it discards.
                recs, done = self._bot_turn(seat)
            out += recs
            if done:
                return out
            return out + self.advance()
        finally:
            self._bot_call_depth -= 1

    def _show_discard(self, seat, tile, slot, riichi=False, after_draw=False):
        """The records that make a NON-LOCAL seat's discard visible. `riichi`
        marks the motion-3 record (+0x21) as the riichi declaration.

        Deliberately a separate helper from the block in `_bot_turn` rather
        than a refactor of it: that block is confirmed live and also folds in the
        draw beat, which a seat that just CALLED does not have. Honours
        DISCARD_ANIM so both paths move together.

        The subtype-3 form is safe straight after a pon/chi, and measured: the
        client's draw-pop arm is gated on `DAT_003e5a80` -- the last call
        motion -- not being 4..8, so after a pon or chi it skips the pop by
        itself and only the discard flies. SE anticipated exactly this beat.
        `after_draw` names the turn after a KAN on the resync path (the
        rinshan draw was a motion-2 MjTSUMO). The subtype-3 form is used
        there too: its in-handler pop re-shows the draw beat -- cosmetic --
        while the subtype-0 form marks the pond tile settled and produces the
        measured slingshot, which is worse.
        """
        if knobs.DISCARD_ANIM == "appear":
            return [self.msg_alldata()]
        if knobs.DISCARD_ANIM == "subtype3":
            return [self.msg_alldata(discard_event=(tile, seat)),
                    self.msg_tsumo(seat, discard=(tile, slot, seat, riichi))]
        return [self.msg_alldata(),
                self.msg_tsumo(seat, discard=(tile, slot, seat, riichi))]

    # -- the kan family --------------------------------------------------------

    def _do_kan(self, seat, kind_, target):
        """An ankan or kakan by `seat` (human or bot) on kind `target`, with
        the CHANKAN window: the engine forms the kan provisionally, the seats
        that may rob it are asked (a live seat through an MjNAKI carrying
        only the Ron bit and the added tile at +0x28; a bot answers now), and
        the kan completes -- rinshan draw, kan-dora owed -- only when nobody
        does. `msg_kan` serves both record paths; the deferred form holds the
        rinshan flight while a live seat decides and `msg_tsumo(motion=9)`
        (motion 2 on the resync path) flies it afterwards. Returns records.
        """
        k = self.kyoku
        pre = self.call_snapshot()
        if kind_ == "ankan":
            t = k.call_ankan(seat, target, provisional=True)
        else:
            t = k.call_kakan(seat, target, provisional=True)
        cands = k.chankan_candidates(seat, t)
        live_c = [s for s in cands if s in self.live]
        self.trace("KAN   %s declares %s on %s%s"
                   % (self._seatname(seat), kind_, narration._tile(t),
                      "  [chankan window: %s]"
                      % "/".join(self._seatname(s) for s in cands)
                      if cands else ""))
        if live_c:
            out = self.msg_kan(kind_, seat, pre, rinshan="defer")
            self.pending_chankan = (seat, kind_, pre)
            out.append(self.msg_naki(t, {s: {"ron": True} for s in live_c},
                                     from_seat=seat))
            return out
        if cands:                                   # bots rob when they can
            wins = k.win_ron(list(cands), chankan=True, tile=t, from_seat=seat)
            if wins:
                return self.on_wins(wins, ron_from=seat)
            if k.result is not None:
                return self.on_kyoku_end()          # sanchahou
        k.complete_kakan()
        return self.msg_kan(kind_, seat, pre)

    def _resolve_chankan(self, tile, options, answers):
        """The chankan window closed: rob the kan or complete it."""
        k = self.kyoku
        kseat, kind_, _pre = self.pending_chankan
        self.pending_chankan = None
        rons = []
        for step in range(1, 4):
            s = (kseat + step) % 4
            if s in self.live:
                act = answers.get(s, ("pass", None))[0]
                if s in options and act == "ron":
                    rons.append(s)
                elif s in options:
                    k.note_missed_ron(s)        # declined a completing tile
            elif s in k.chankan_candidates(kseat, tile):
                rons.append(s)
        if rons:
            wins = k.win_ron(rons, chankan=True, tile=tile, from_seat=kseat)
            if wins:
                self.trace("CHANKAN %s robs %s's %s"
                           % (self._seatname(wins[0][0]), self._seatname(kseat),
                              kind_))
                return self.on_wins(wins, ron_from=kseat)
            if k.result is not None:
                return self.on_kyoku_end()
            for s in rons:
                self.log.append(("refused-chankan", (s, tile)))
        k.complete_kakan()
        out = [self.msg_tsumo(kseat, motion=9 if knobs.KAN_MOTION else 2)]
        if kseat in self.live:
            return out
        recs, done = self._bot_turn(kseat, drew_shown=True)
        out += recs
        return out if done else out + self.advance()

    def msg_win_reveal(self, seat, ron_from):
        """A RON's reveal: the win screen synthesises motion 10 on the winner
        (mahdisp.c:649, `+0x22 = 1 << winner`) which flies DAT_00451920[winner]
        -- the table-state hand, 13 tiles, the winning tile still in the
        discarder's pond. ONE plain MjALLDATA first, with the winning tile
        appended to the winner's hand and the claimed pond tile flagged (bit
        6 = not drawn, the pond closes over it), makes the reveal 14 tiles.
        Subtype 0 mutates nothing in-handler, so nothing double-applies; the
        YAKUDISP's own motion follows it. A tsumo needs none of this -- the
        winning draw is already in the hand the client holds.
        """
        k = self.kyoku
        tile = k.last_discard
        hands = [list(k.hands[s].tiles) for s in range(4)]
        if tile is not None and len(hands[seat]) % 3 == 1:
            hands[seat] = hands[seat] + [tile]
        ponds = [self._pond_wire(s) for s in range(4)]
        if ron_from is not None and 0 <= ron_from < 4 and ponds[ron_from]:
            last, _flag = ponds[ron_from][-1]
            if tile is None or mj.kind(last) == mj.kind(tile):
                ponds[ron_from][-1] = (last, True)
        return self.msg_alldata(hands=hands, ponds=ponds, reveal={seat},
                                label="win reveal seat %d" % seat)

    def on_wins(self, wins, ron_from=None):
        """EVERY winner on one discard, in the order `win_ron` settled them.

        With MJS_CONFIG_IDX_DOUBLE_RON on (the default) two seats can ron the
        same tile and `Kyoku._settle` pays both -- but MjYAKUDISP names exactly
        one winner (+0x1a), so a second winner needs a second screen. The first
        goes out now; the rest are queued and each MjYAKUDISPACK pulls the next
        (see `_ack_advance`). Head bump (double_ron off) trims the list to one
        inside `win_ron`, so this is a no-op there.
        """
        wins = list(wins)
        self._pending_wins = [(s, sc) for s, sc in wins[1:]]
        self._pending_ron_from = ron_from
        if len(wins) > 1:
            self.trace("DOUBLE RON off %s: %s  [%d win screens, in turn order]"
                       % (self._seatname(ron_from),
                          " + ".join(self._seatname(s) for s, _sc in wins),
                          len(wins)))
        return self.on_win(wins[0][0], wins[0][1], ron_from=ron_from)

    def on_win(self, seat, score, ron_from=None):
        self.log.append(("win", (seat, repr(score))))
        self.trace("WIN   %s  han=%s fu=%s  %s  scores=%s"
                   % (self._seatname(seat), getattr(score, "han", "?"),
                      getattr(score, "fu", "?"),
                      "ron off %s" % self._seatname(ron_from)
                      if ron_from is not None else "tsumo",
                      list(self.kyoku.scores)))
        self._record_win(seat, score)
        out = []
        if ron_from is not None:
            out.append(self.msg_win_reveal(seat, ron_from))
        out.append(self.msg_yakudisp(seat, score, ron_from=ron_from))
        return out

    def _record_win(self, seat, score):
        """A LIVE seat's win -> its per-yaku counters (finding 28)."""
        if seat not in self.live or janstats is None or not knobs.STATS_ENABLE:
            return
        member = self.member_of_seat(seat)
        if not member:
            return
        try:
            yakuman = getattr(score, "limit", "") in ("yakuman", "kazoe_yakuman")
            names = [n for n, _h in (getattr(score, "yaku", None) or [])]
            janstats.record_win(member, names, yakuman=yakuman)
        except Exception as e:
            self.log.append(("stats-error", "win seat %d: %s: %s"
                             % (seat, type(e).__name__, e)))

    def on_kyoku_end(self):
        """A hand finished with no winner (exhaustive draw or an abort): the
        ryukyoku banner + tenpai reveal (motion 10), then the settlement."""
        self.trace("DRAW  exhaustive/abort  scores=%s" % list(self.kyoku.scores))
        return [self.msg_draw_reveal(), self.msg_seisan()]

    def next_kyoku_or_end(self):
        over = self.game.end_kyoku()
        if over:
            return [self.msg_results()]
        return self.start_kyoku()
