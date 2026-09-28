"""The in-game records a table sends: deal, draw, call offer, yaku, settlement, resync, kan."""
import time
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
from . import knobs, narration, table


class TableMessages:
    """Part of `Table` (table.py), which inherits it.

    The in-game records a table sends: deal, draw, call offer, yaku,
    settlement, resync, kan.
    """

    # -- the messages --------------------------------------------------------

    def msg_haipai(self):
        # Hold the deal a beat so the client's camera zoom-in finishes before
        # the deal freezes it (the board-zoom fix; see DEAL_DELAY). Thread-per-
        # connection server, so this blocks only this client's thread.
        if knobs.DEAL_DELAY > 0:
            if self._managed:
                self._hold_s = max(self._hold_s, knobs.DEAL_DELAY)   # Manager sleeps, unlocked
            else:
                time.sleep(knobs.DEAL_DELAY)
        k = self.kyoku
        hands = [list(k.hands[s].tiles) for s in range(4)]
        # The dealer holds 14 at this point; the client's array is 14 wide, so
        # the other three simply have a trailing empty slot.
        #
        # WARNING: RETRACTED 2026-09-02 (same day): "+0x44 is the 2D hand rail" was
        # wrong -- +0x44 is the DEAD WALL (client's own debug label: WAMPAI),
        # and stuffing seat 0's hand in it drew garbage stacks in the table
        # centre. The hand was invisible for a different reason entirely:
        # motion=0. The HAIPAI parser zeroes every per-tile visibility gate
        # (DAT_004521a0) and only MotionCommand 10 -- the deal animation, seats
        # picked by the +0x22 bitmask -- restores them as the tiles land. The
        # fluctuating 6/8/10-tile hands were single tiles being revealed by the
        # motion=2 draw animations instead. See janmsgs' WAMPAI banner.
        # Conceal every seat but the local players': retail shows only your
        # own hand, the other three as rows of backs (ml070i/ml073i). The
        # human is seat 0; `self.live` is the set of local seats.
        # WARNING: ONE DEAL RECORD PER LIVE SEAT. This used to conceal
        # `set(range(4)) - self.live` -- every seat that is not a person --
        # which is exactly right while there is ONE person and hands the other
        # player your tiles the moment there are two. Each live seat now gets
        # its own record concealing all three of the others.
        #
        # With a single live seat the result is byte-identical to what this
        # sent before: `{0,1,2,3} - {0}` is the same `{1,2,3}` the old
        # expression produced, so the proven-live solo path is untouched.
        #
        # ONE SEQUENCE FOR ALL OF THEM, deliberately: the client dedups on
        # `rec[0x13]` against the last record IT received, and these go to
        # different clients. Bumping per copy would work too, but sharing it
        # keeps the deal a single logical message.
        seq = self.next_seq()
        targets = sorted(self.live) if self.live else [None]
        out = []
        self._awaiting = (M.MjHAIPAI, None)     # before the emits: _emit keys on it
        gallery_rec = None
        for _s in targets:
            # Concealment via the face-down bit means INVISIBLE, not backs, in
            # this engine (see CONCEAL_DEAL / the RAM proof) -- so with it off,
            # deal every seat face-up and let opponents render consistently.
            if knobs.CONCEAL_DEAL:
                concealed = (set(range(4)) - {_s} if _s is not None
                             else set(range(4)) - set(self.live))
            else:
                concealed = set()
            sc = self._round_scalars()
            rules18, aka3 = self._rule_scalars()
            rec = M.haipai(seq, hands, motion=10, seat_mask=0x0F,
                           is_human=self._is_human(),
                           concealed=concealed,
                           # +0x44 the dead wall: dora-1 at slot 4 (the slot
                           # the client itself clears and auto-flips), all
                           # else face-down -- janmsgs' WAMPAI banner.
                           position=self._wanpai_u16(),
                           scalars18=rules18, three_a=aka3,
                           chiicha=sc.chiicha, dealer=sc.dealer,
                           round_wind=sc.round_wind, kyoku=sc.kyoku,
                           honba=sc.honba, dice=sc.dice)
            if self.gallery and concealed and gallery_rec is None:
                # The spectators' deal: the SAME seq, every seat face-up
                # (GALLERY banner). With CONCEAL_DEAL off this is never
                # needed -- `rec` already shows all four hands.
                gallery_rec = M.haipai(seq, hands, motion=10, seat_mask=0x0F,
                                       is_human=self._is_human(),
                                       concealed=set(),
                                       position=self._wanpai_u16(),
                                       scalars18=rules18, three_a=aka3,
                                       chiicha=sc.chiicha, dealer=sc.dealer,
                                       round_wind=sc.round_wind, kyoku=sc.kyoku,
                                       honba=sc.honba, dice=sc.dice)
            out.append(self._emit(rec, "deal", to=_s, gallery=gallery_rec))
        ds = k.wall.dora_shown
        if isinstance(ds, (list, tuple)):
            dora = "/".join(narration._tile(t) for t in ds) if ds else "?"
        else:
            dora = narration._tile(ds) if ds is not None else "?"
        self.trace("DEAL  dealer=%s  dora=%s  wall=%d  [%d record(s), one per "
                   "live seat]"
                   % (self._seatname(k.dealer), dora, self.wall_count(),
                      len(out)))
        for s in range(4):
            shown = mj.hand_str(k.hands[s].tiles)
            self.trace("  hand %s%s = %s"
                       % (self._seatname(s),
                          "" if s in self.live else " [concealed on screen]",
                          shown))
        return out

    #: MyMove action-menu byte indices (the MjTSUMO +0x4d block), from the
    #: client's itemmap DAT_004099b0 = [2,0,3,6] over [Riichi,Tsumo,Kan,Kyushu].
    #: Same unified layout as the call menu (NAKI_MENU_BYTE): a shared
    #: 7-byte enable block, so a self-turn and a call offer speak the same
    #: dialect. WARNING: We used to set only byte 0, which is TSUMO -- so Riichi
    #: (byte 2) never lit and Tsumo lit on any tenpai hand (2026-09-03 fix).
    MENU_TSUMO, MENU_RIICHI, MENU_KAN, MENU_KYUSHU = 0, 2, 3, 6

    def _riichi_discard_slots(self, h):
        """Hand-slot indices whose discard leaves this 14-tile hand tenpai.

        These are the riichi-legal discards. The turn-menu MjTSUMO must mark
        each such slot with **bit 0** of its +0x54 flag (DAT_00446d50): when the
        player presses Riichi, the client's tile picker (mahdisp__002c2ac0,
        lVar5==2 branch) scans ONLY bit-0 slots for a cursor landing. If none
        are set it falls through to the ±1000 sentinels (`uVar10 = 1000` at
        mahdisp.c:3560) and WEDGES with no message sent back -- the exact
        riichi freeze (cursor 1000, _DAT_00452010==1). bit 1 stays the general
        "discardable" flag for a normal (non-riichi) discard.
        """
        k = self.kyoku
        if k is not None and k.turn == h.seat and h.tile_total() == 14:
            # The engine's own answer (every riichi condition applied,
            # REACH_AFTER_KAN included) whenever it is in a position to give
            # one; the local tenpai scan below is the fallback for a hand
            # that is not on turn (the selftests build those directly).
            try:
                kinds = set(mj.kind(t) for t in k.riichi_discards(h.seat))
            except Exception:
                kinds = set()
            if kinds:
                return [i for i, t in enumerate(h.tiles) if mj.kind(t) in kinds]
        nmeld = len(h.melds)
        slots = []
        for i, t in enumerate(h.tiles):
            counts = list(h.counts())
            counts[mj.kind(t)] -= 1
            if mj.is_tenpai(counts, nmeld):
                slots.append(i)
        return slots

    def _can_riichi_discard(self, h):
        """True if some discard leaves this 14-tile hand tenpai (riichi-ready)."""
        return bool(self._riichi_discard_slots(h))

    def _mymove_menu(self, seat):
        """The 7-byte self-turn action-menu enable block for `seat`.

        Each button is authored to its REAL legality; the client draws only the
        lit ones (plus the always-present Cancel). The client applies its own
        gates to Tsumo (yamaguchi__002e6cb0), but Riichi/Kan/Kyushu are ours to
        decide, so the offer must be exactly right -- a lit button whose reply
        arm the server rejects is a dead button.
        """
        k = self.kyoku
        h = k.hands[seat]
        block = bytearray(7)
        # Tsumo: the drawn hand is a scoreable self-draw win.
        can_win = False
        if h.drawn is not None:
            ctx = k.context_for(seat, h.drawn, True)
            if mj.can_tsumo(h, ctx) is not None:
                block[self.MENU_TSUMO] = 1
                can_win = True
        # Riichi: closed, not already declared, >=1000 points, a draw still to
        # come, and some discard reaches tenpai.
        #
        # WARNING: NOT WHEN THE HAND ALREADY WON (2026-09-04 LIVE HANG). If the drawn
        # tile completes the hand, the menu must offer ONLY Tsumo -- you never
        # declare riichi on a hand you can win. We used to light both, and a
        # player, learning, pressed Riichi on a complete hand: the client's
        # riichi-select flow (mahdisp__002c2ac0) cannot declare on a winning
        # hand and WEDGED THE GAME with no message ever sent back to us (the
        # trace ends at that MjTSUMO). `can_win` gates it out.
        if (h.menzen and not h.riichi and not can_win
                and k.scores[seat] >= 1000
                and self.wall_count() >= 4 and self._can_riichi_discard(h)
                and (k.turn != seat or h.tile_total() != 14
                     or k.can_riichi(seat))):
            block[self.MENU_RIICHI] = 1
        # Kan: `Kyoku.ankan_options/kakan_options` are the source of truth
        # (turn, draw, riichi waits-unchanged, no 5th kan, dead wall) -- the
        # hand-only module functions lit the button for kans the engine then
        # refused (audit 11/21), and a lit button whose reply we reject is a
        # dead button.
        try:
            if k.ankan_options(seat) or k.kakan_options(seat):
                block[self.MENU_KAN] = 1
        except Exception:
            pass
        # Kyushu kyuhai: first uninterrupted go-round, still concealed, and 9+
        # distinct terminals/honors -> an abortive draw.
        if k.first_go_round and h.menzen and not any(x.melds for x in k.hands):
            distinct_yao = sum(1 for kk in set(mj.kind(t) for t in h.tiles)
                               if kk in mj.YAOCHUU)
            if distinct_yao >= 9:
                block[self.MENU_KYUSHU] = 1
        return bytes(block)

    def msg_tsumo(self, seat, motion=None, discard=None):
        """Seat `seat`'s state after a draw -- and the turn's ANIMATION.

        Every record carries ONE MotionCommand (janmsgs' +0x18 banner,
        2026-09-02). A bot's discard (motion 3) is the ANIMATION only: it never
        writes the pond array, so the pond-bearing MjALLDATA must land first
        (see DISCARD_ANIM). `discard=(tile, slot, seat[, riichi])` builds the
        motion-3 form -- `riichi` True sets +0x21, the byte the remote discard
        flight tests for the riichi cut-in, voice and sideways tile
        (yamaguchi.c:7920-7932; never sent before 2026-09-04, so every CPU
        riichi was silent). Otherwise a real draw gets motion 2 with the drawn
        tile (motion 9 = the same for a RINSHAN draw: the client pulls the
        dead-wall slot itself), and a turn with no draw (after a call)
        animates nothing. The local player's OWN discard is animated
        client-side -- never send motion 3 with the human's seat or the pond
        double-appends.
        """
        k = self.kyoku
        h = k.hands[seat]
        anim_tile = anim_slot = anim_seat = 0
        riichi = False
        if discard is not None:
            motion = 3
            anim_tile = M.tile_byte(discard[0])
            anim_slot = discard[1] & 0xFF
            anim_seat = discard[2]
            riichi = bool(discard[3]) if len(discard) > 3 else False
        elif h.drawn is not None:
            if motion is None:
                motion = 2
            if motion in (2, 9):
                anim_tile = M.tile_byte(h.drawn)
                anim_slot = 13
                anim_seat = seat
        if motion is None:
            motion = 0
        # The self-turn ACTION MENU (MyMove) -- the +0x4d block, same 7-byte
        # menu-enable structure the call menu uses at +0x4c, but read through
        # the MyMove itemmap DAT_004099b0 = [2,0,3,6]: Riichi=block[2],
        # Tsumo=block[0], Kan=block[3], Kyushu=block[6]. Only for the seat
        # whose turn it actually is (a live seat that just drew).
        menu = self._mymove_menu(seat) if seat in self.live else b"\0" * 7
        slot_flags = None
        auto = 0
        if h.riichi:
            # A declared riichi may only discard what it drew: offer that slot.
            # The DRAWN tile is the one `Kyoku.draw_for` appended, i.e. the LAST
            # -- not `index()`'s first match, which points at an older copy when
            # the hand already held that tile and disagrees with slot 13, the
            # only slot the client's auto-discard can name.
            slot_flags = [0] * 14
            idx = len(h.tiles) - 1
            if h.drawn is not None and h.tiles and h.tiles[-1] != h.drawn:
                idx = (h.tiles.index(h.drawn) if h.drawn in h.tiles
                       else len(h.tiles) - 1)
            slot_flags[idx] = 2
            # VERIFIED: RIICHI TSUMOGIRI (2026-09-12): with the +0x4b lock on, +0x63
            # makes the client discard the drawn tile with no input at all
            # (mahdisp.c:3645 -- `+0x63 && DAT_003865bc[seat] && DAT_003e4e08 &&
            # !menu_was_opened`). DAT_003e4e08 is never written by ANY of the
            # 4,146 decompiled functions and its .data image at 0x3e4e08 holds
            # 1, so that clause is a constant -- the gate is really ours. The
            # client returns hand slot **0xd**, always (the free cursor is
            # skipped while the lock is on, so the cursor never leaves its
            # initial 0xd), so this may only be armed when the drawn tile really
            # is at slot 13: true for a 14-tile closed hand, FALSE after an
            # ankan (10 tiles + rinshan), which is why `idx == 13` gates it.
            # And never when the seat still has a decision to make -- a menu
            # with Tsumo or a Kan lit must reach the player, so `any(menu)`
            # withholds it. A riichi hand offers nothing else: Riichi is gated
            # on `not h.riichi` and Kyushu on the first go-round.
            auto = 1 if (knobs.RIICHI_AUTO and seat in self.live and idx == 13
                         and h.drawn is not None and not any(menu)) else 0
        elif seat in self.live and menu[self.MENU_RIICHI]:
            # Riichi is OFFERED this turn. Every occupied slot keeps bit 1 (a
            # normal discard is still allowed if the player cancels riichi), and
            # each riichi-legal discard also gets bit 0 -- without which the
            # client's riichi picker finds no landing slot and freezes at cursor
            # 1000 (mahdisp.c:3546-3561). See _riichi_discard_slots.
            slot_flags = [2 if i < len(h.tiles) else 0 for i in range(14)]
            for i in self._riichi_discard_slots(h):
                slot_flags[i] |= 1
        elif seat in self.live and h.drawn is None:
            # The turn right after this seat's own chi/pon: the kinds kuikae
            # forbids lose bit 1 (not discardable) so the picker cannot land
            # on a tile the engine would refuse.
            forbidden = k.kuikae_forbidden(seat)
            if forbidden:
                slot_flags = [2 if (i < len(h.tiles)
                                    and mj.kind(h.tiles[i]) not in forbidden)
                              else 0 for i in range(14)]
        rec = M.tsumo(self.next_seq(), seat, h.tiles, self.wall_count(),
                      is_human=self._is_human(),
                      motion=motion, slot_flags=slot_flags,
                      riichi_test=menu,
                      # WARNING: +0x4b IS THE CLIENT'S OWN RIICHI LOCK (2026-09-12):
                      # console.c:3400 copies it into this seat's
                      # DAT_003865bc+seat*0x34, the byte mahdisp.c:3607 tests
                      # before it runs the FREE hand cursor at all -- non-zero
                      # and the cursor cannot leave the drawn tile. +0x54 alone
                      # is not enough: its bit 1 only binds the two MENU-driven
                      # pickers (mahdisp.c:3492 discard, :3554 riichi), and the
                      # ordinary "press X on a tile" path (mahdisp.c:3607-3672)
                      # never reads +0x54, which is why a riichi hand could
                      # still discard from anywhere in the hand. MjALLDATA
                      # +0x13a and MjNAKI +0x34 already carry this per seat; the
                      # draw record is the one that was clearing it back to 0.
                      f4b=1 if h.riichi else 0,
                      f63=auto,
                      per_seat=tuple(1 if k.hands[s].riichi else 0 for s in range(4)),
                      anim_tile=anim_tile, anim_slot=anim_slot,
                      anim_seat=anim_seat, riichi=riichi)
        self._awaiting = (M.MjTSUMO, seat)
        if discard is not None:
            # THE opponent-discard record. anim_seat (+0x22) is the seat the
            # client animates the tile FOR -- if a COM's tile lands in the
            # centre instead of its own pond, this is the field to check it
            # against on screen.
            self.trace("DISCARD %s tile=%s slot=%d  [motion 3, anim_seat=%d%s]"
                       % (self._seatname(discard[2]), narration._tile(discard[0]),
                          discard[1], discard[2], ", RIICHI +0x21" if riichi else ""))
        elif motion in (2, 9):
            self.trace("DRAW  %s tile=%s  [motion %d%s%s]"
                       % (self._seatname(seat), narration._tile(h.drawn), motion,
                          " rinshan" if motion == 9 else "",
                          # The one string that proves the tsumogiri path is
                          # LIVE: the client should answer with MjSUTE slot 13
                          # and no input at all.
                          ", RIICHI LOCK +0x4b" + (" + AUTO-DISCARD +0x63"
                                                   if auto else "")
                          if h.riichi else ""))
        # A DRAW IS PRIVATE, A DISCARD IS NOT. The draw form carries that
        # seat's hand AND its action menu (`riichi_test`), so sending it to the
        # other human would both show them the tiles and offer them somebody
        # else's Riichi/Tsumo/Kan buttons. The discard form is the animation
        # everyone has to see, so it still goes to everyone.
        #
        # `seat in self.live` keeps a BOT's draw broadcast, which is what makes
        # a COM's turn visible at all (the motion-2 animation).
        _to = seat if (discard is None and seat in self.live) else None
        return self._emit(rec, "draw seat %d" % seat if discard is None
                          else "discard seat %d" % discard[2], to=_to)

    #: The call menu's per-action byte inside the MjNAKI +0x4c 7-byte block,
    #: measured from the client's static tables (the OpenChoice window's
    #: item->block map DAT_004099d0 = [1,5,4,3,-1] for [Ron,Chi,Pon,Kan,Cancel]
    #: and its selection->action map DAT_00386210). Bytes 0/2/6 are the MyMove
    #: menu's Tsumo/Riichi/Kyushu and stay zero in a call offer.
    NAKI_MENU_BYTE = {"ron": 1, "kan": 3, "pon": 4, "chi": 5}

    def msg_naki(self, tile, options, from_seat=None):
        k = self.kyoku
        calls = {}
        slots = {}
        menus = {}
        for seat, opt in options.items():
            calls[seat] = {name: True for name in opt}
            blk = bytearray(7)
            for name in opt:
                blk[self.NAKI_MENU_BYTE[name]] = 1
            menus[seat] = bytes(blk)
            h = k.hands[seat]
            flags = [0] * 14
            if "chi" in opt:
                # The client's chi navigator walks EXACTLY the flagged slots
                # (mahdisp__002c36e0 copies +0x68 and steps between `& 7`
                # non-zero entries; the slot it stops on is the tile the ack
                # carries) -- so flag precisely the kinds that appear in some
                # legal chi pair with this discard, and nothing else. A pon
                # kind must NOT be flagged here: the cursor could stop on it
                # and send a chi the pair matcher cannot place.
                kinds = set()
                for pair in mj.chi_options(h, tile):
                    for t in pair:
                        kinds.add(mj.kind(t))
                for i, t in enumerate(h.tiles[:14]):
                    if mj.kind(t) in kinds:
                        flags[i] = 1
            elif "pon" in opt or "kan" in opt:
                # The pon/kan arms match the claimed tile by id, not by slot;
                # these flags are only the hand highlight.
                for i, t in enumerate(h.tiles[:14]):
                    if mj.kind(t) == mj.kind(tile):
                        flags[i] = 1
            slots[seat] = flags
        rec = M.naki(self.next_seq(), self.wall_count(), calls=calls,
                     is_human=self._is_human(),
                     # +0x28 (_DAT_00451f88) is the claimable tile itself: the
                     # client's pon arm spins scanning the hand for a tile with
                     # this id, so an unset value hangs the client.
                     u28=M.tile_u16(tile),
                     # +0x2a is the discarder; the client highlights that
                     # seat's newest pond tile (DAT_00452050 = value+1).
                     s2a=max(0, k.last_discard_seat if from_seat is None
                             else from_seat),
                     menu=menus, slot_flags=slots,
                     per_seat_34=tuple(1 if k.hands[s].riichi else 0
                                       for s in range(4)))
        self.pending_naki = (tile, options)
        self.naki_answers = {}
        self._awaiting = (M.MjNAKI, next(iter(options)) if options else None)
        for seat, opt in options.items():
            self.trace("OFFER call to %s on %s: %s  [client shows the call "
                       "buttons for these; a bare Cancel = it disagrees]"
                       % (self._seatname(seat), narration._tile(tile),
                          "/".join(sorted(opt))))
        return self._emit(rec, "calls on %s" % mj.name(tile))

    @staticmethod
    def _payment_words(score, winner, dealer, ron, honba):
        """MjYAKUDISP +0x64 (base) / +0x68 (dealer share), in POINTS, from the
        engine's per-seat payments with the honba stripped -- the client sums
        them itself (ron: base; dealer tsumo: base x3; other tsumo: dealer
        share + base x2, mahdisp.c:722-741)."""
        pay = getattr(score, "payments", None) or {}
        paid = {}
        for s, d in pay.items():
            if s != winner and d < 0:
                paid[s] = -d - (300 if ron else 100) * honba
        if ron:
            base = max(paid.values()) if paid else int(getattr(score, "points", 0))
            return max(0, base), 0
        if winner == dealer:
            base = max(paid.values()) if paid else int(getattr(score, "points", 0)) // 3
            return max(0, base), 0
        dealer_share = paid.get(dealer, 0)
        others = [v for s, v in paid.items() if s != dealer]
        base = max(others) if others else int(getattr(score, "points", 0)) // 4
        return max(0, base), max(0, dealer_share)

    def msg_yakudisp(self, seat, score, ron_from=None):
        """The win screen, every field where mahdisp__002bcba0 reads it
        (janmsgs' MjYAKUDISP banner, 2026-09-04): the seat block (+0x18 han,
        +0x19 fu, +0x1a WINNER, +0x1b dealer, +0x1c ron), the round header
        (+0x1d wind, +0x1e kyoku, +0x1f honba), the yakuman and show-ura
        flags, the (yaku id, han) PAIRS at +0x22 and the two payment words.

        WARNING: History: +0x22 used to carry the TILES (the panel draws those from
        the table state, not from here), so the client indexed its yaku table
        with tile codes and invented yaku; +0x64/+0x68 were never written
        ("0 pt"); +0x1d..+0x21 were zero, so the header read E1 and the ura
        never spread. And before 81afa6c3 the winner was in +0x18.
        """
        k = self.kyoku
        h = k.hands[seat]
        sc = self._round_scalars()
        ron = 0 if ron_from is None else 1
        rows = M.yaku_rows(list(getattr(score, "yaku", []) or []))
        # +0x20 -> DAT_00446c78: non-zero forces the limit path. A counted
        # yakuman reaches it by han (13+) anyway; the flag keeps the client's
        # own yakuman bookkeeping honest for both.
        yakuman = 1 if getattr(score, "limit", "") in ("yakuman", "kazoe_yakuman") else 0
        show_ura = 1 if (h.riichi and getattr(self.rules, "ura_dora", True)) else 0
        base, dealer_share = self._payment_words(score, seat, k.dealer, ron,
                                                 k.honba)
        rec = M.yakudisp(self.next_seq(), han=min(255, score.han),
                         fu=min(255, score.fu), winner=seat, dealer=k.dealer,
                         ron=ron, round_wind=sc.round_wind, kyoku=sc.kyoku,
                         honba=sc.honba, yakuman=yakuman, show_ura=show_ura,
                         yaku=rows, pay_base=base, pay_dealer=dealer_share)
        self._awaiting = (M.MjYAKUDISP, None)
        self.trace("YAKU  %s: %s  han=%d fu=%d  base=%d dealer=%d%s%s"
                   % (self._seatname(seat),
                      " ".join("%d:%d" % r for r in rows) or "(no rows)",
                      score.han, score.fu, base, dealer_share,
                      "  YAKUMAN" if yakuman else "",
                      "  ura shown" if show_ura else ""))
        return self._emit(rec, "yaku for seat %d (%s)"
                          % (seat, "ron" if ron else "tsumo"))

    def msg_seisan(self):
        sc = self._round_scalars()
        # +0x28..+0x2c in the reader's order (mahdisp.c:788-793): current
        # seat, dealer, round wind, kyoku, honba. Not wind/kyoku/honba/sticks.
        rec = M.seisan(self.next_seq(), self.kyoku.scores,
                       current_seat=sc.current_seat, dealer=sc.dealer,
                       round_wind=sc.round_wind, kyoku=sc.kyoku, honba=sc.honba)
        self._awaiting = (M.MjSEISAN, None)
        self._seisan_ready = set()          # MjREADYSTATUS starts over per settlement
        return self._emit(rec, "settlement")

    def msg_alldata(self, subtype=0, discard_event=None, hands=None, ponds=None,
                    call=None, label=None, reveal=None, wanpai=None,
                    seat_mask=None, seat_flags=None, to=None, emit=True):
        """The whole table. `discard_event=(tile, seat)` makes it a subtype-3
        DRAW+PREP: the client pops the tile onto the seat's hand and CLEARS the
        settled-draw flag of that seat's last pond tile, so a motion-3 record
        sent immediately after flies the discard with no settled pre-draw. The
        pond for that seat must already end with the discard (call this AFTER
        k.discard()). See DISCARD_ANIM.

        `subtype` is ALSO the MotionCommand -- same byte -- which is what makes
        this the message a granted call rides on. `hands`/`ponds` override the
        live board for exactly that case (a call motion wants PRE-call hands and
        ponds against POST-call melds; see janmsgs' CALL MOTIONS banner), and
        `call` is `{"from": discarder, "tiles": [...], "slots": [...],
        "seat": caller}`. Use `msg_call()` / `msg_kan()`.

        `reveal` is a set of seats whose hands stay populated even under
        CONCEAL_RESYNC (a win reveal, the tenpai seats at a draw); `wanpai`
        overrides the +0x44 dead wall (a kan record); `seat_mask` writes +0x22
        (motion 10's seat bitmask); `seat_flags` the +0x23..+0x26 per-seat
        block (1 on a kan caller's seat = hold the rinshan flight).

        `emit=False` builds the record WITHOUT `_emit` (a spectator's join
        snapshot: no route, no deadline reset, no spectator copy)."""
        k = self.kyoku
        if hands is None:
            hands = [list(k.hands[s].tiles) for s in range(4)]
        full_hands = [list(hd) for hd in hands]     # before any concealment
        reveal = set(reveal or ())
        if knobs.CONCEAL_RESYNC:
            # Blank every non-local seat's HAND (see CONCEAL_RESYNC) EXCEPT the
            # seat this record animates. An empty hand draws NOTHING (the safe
            # uVar1==0 path), matching the deal's concealed state, so the two
            # non-animating opponents never "reappear" face-up.
            #
            # WARNING: THE ANIMATING SEAT KEEPS ITS HAND. A subtype-3 discard flies
            # the tile from that seat's hand area, and a call motion builds its
            # meld from the caller's hand slots -- blank either and the fly
            # springs from nowhere (the 44e4fbbd centre-slingshot class). The
            # animating seat is the discarder (subtype 3) or the caller (a call
            # motion); a plain resync animates nobody, so all three opponents
            # blank. Ponds and melds below stay real -- they are public.
            anim_seat = (discard_event[1] if discard_event is not None
                         else (call.get("seat") if call is not None else None))
            hands = [list(hands[s]) if (s in self.live or s == anim_seat
                                        or s in reveal)
                     else [] for s in range(4)]
        if ponds is None:
            ponds = [self._pond_wire(s) for s in range(4)]
        melds = [M.meld_block(k.hands[s].melds) for s in range(4)]
        meld_types = {s: M.meld_types(k.hands[s].melds) for s in range(4)}
        sc = self._round_scalars()
        ev_tile = ev_seat = ev_present = 0
        if discard_event is not None:
            subtype = 3
            ev_tile = M.tile_byte(discard_event[0])
            ev_seat = discard_event[1] & 0xFF
            ev_present = 1
        if call is not None:
            # +0x22 is the CALLER on a call motion. +0x1c/+0x1e are overwritten
            # by the call fields inside M.alldata(), so the event bytes here are
            # dead and stay zero.
            ev_seat = call["seat"] & 0xFF
        seq = self.next_seq()

        def build(hands_):
            return M.alldata(seq, hands_, ponds, melds=melds,
                        is_human=self._is_human(),
                        meld_types=meld_types,
                        # +0x44 is the DEAD WALL (byte-per-tile here, u16 in
                        # MjHAIPAI) -- see msg_haipai's banner and janmsgs'
                        # WAMPAI banner: 0x40 = REVEALED, rinshan slot 0 empty
                        # once a kan has drawn, so a resync agrees with what the
                        # client's own flips, re-fills and motion-9 draws did to
                        # its copy.
                        position=self._wanpai_bytes() if wanpai is None else wanpai,
                        scores=k.scores, subtype=subtype,
                        current_seat=sc.current_seat,
                        wall_count=min(0x7F, self.wall_count()),
                        # +0x13b..+0x13f by NAME (console.c:3923-3935): honba,
                        # the riichi-stick pot (+0x13c, ONLY written here --
                        # a 0 blanked "ReachBou" every bot turn), dealer/
                        # kyoku/chiicha in +0x13d, the round wind in +0x13e
                        # bit 0 and the dice in +0x13f.
                        honba=sc.honba, riichi_sticks=sc.riichi_sticks,
                        dealer=sc.dealer, kyoku=sc.kyoku, chiicha=sc.chiicha,
                        round_wind=sc.round_wind, dice=sc.dice,
                        seat_mask=seat_mask,
                        seat_flags=tuple(seat_flags) if seat_flags else (0, 0, 0, 0),
                        f13a=sum((1 if k.hands[s].riichi else 0) << (2 * s)
                                 for s in range(4)),
                        # +0x136: the sideways riichi tile, per seat.
                        tail136=self._riichi_indices(),
                        event_tile=ev_tile, event_seat=ev_seat,
                        event_present=ev_present,
                        call_from=(call or {}).get("from", 0),
                        call_tiles=(call or {}).get("tiles", ()),
                        call_slots=(call or {}).get("slots", ()))
        rec = build(hands)
        if not emit:
            return rec
        # The spectators' copy under CONCEAL_RESYNC: same seq, no seat
        # blanked (GALLERY banner). Off (the default) `rec` is already full.
        gallery_rec = (build(full_hands) if knobs.CONCEAL_RESYNC and self.gallery
                       and hands != full_hands else None)
        return self._emit(rec, label or ("resync" if discard_event is None
                                         else "draw+prep seat %d"
                                         % discard_event[1]), to=to,
                          gallery=gallery_rec)

    #: Which motion animates each granted call (janmsgs' CALL MOTIONS banner).
    #: The three KAN motions are MEASURED (2026-09-04) and driven by `msg_kan()`
    #: behind KAN_MOTION (default off). Both of the 09-03 blockers resolved in
    #: the C: the chain's gate DAT_00452185 is MjHAIPAI +0x2d = the DORA rule
    #: enum (now sent from the rules), and the "new indicator still face-down"
    #: requirement was INVERTED -- 0x80 in the wall array is REVEALED, the chain
    #: scans 12,10,8,6 for the newest revealed indicator, so the engine's eager
    #: reveal is exactly what the record must carry (janmsgs' WAMPAI banner).
    #: And the rinshan draw is NOT a separate motion-9 MjTSUMO: the MjALLDATA
    #: handler synthesises it from +0x1c (janmsgs' CALL MOTIONS banner).
    CALL_MOTION = {"chi": 5, "pon": 4, "minkan": 6, "daiminkan": 6, "kan": 6,
                   "ankan": 7, "kakan": 8, "shouminkan": 8}

    def call_snapshot(self):
        """Freeze the hands and ponds a call motion animates against.

        Call this BEFORE the engine forms the meld. The motion hides the hand
        slots it is handed and pops the discarder's pond tail itself, so the
        record must still show both -- while its MELD block has to be the
        post-call one, because the client's free-meld scan lands on the LAST
        OCCUPIED meld and the tiles it draws come from that block. One record,
        two tenses; janmsgs' CALL MOTIONS banner has the derivation.

        Unpacks as `(hands, ponds)` exactly as before; the dead-wall state a KAN
        record needs (`dora_shown`, `kans`) rides as attributes.
        """
        k = self.kyoku
        return table.CallSnapshot([list(k.hands[s].tiles) for s in range(4)],
                            [self._pond_wire(s) for s in range(4)],
                            dora_shown=k.wall.dora_shown, kans=k.wall.kans)

    @staticmethod
    def taken_slots(pre_hand, post_hand):
        """Which slots of `pre_hand` the meld consumed, in order.

        A multiset walk rather than a set difference: two copies of a kind are
        exactly the case a pon hits, and only one of them left the hand.
        """
        rest = list(post_hand)
        out = []
        for i, t in enumerate(pre_hand):
            if t in rest:
                rest.remove(t)
            else:
                out.append(i)
        return out

    def msg_call(self, action, seat, pre, discarder):
        """A granted pon/chi as SE animates it: ONE MjALLDATA carrying the motion.

        WARNING: NOTHING MAY FOLLOW THIS BUT THE CALLER'S OWN MjTSUMO. The motion has
        already hidden the hand slots and zeroed the pond tail; a plain
        MjALLDATA behind it puts the claimed tile back in the pond and un-hides
        the slots -- the generalised form of the 09-03 slingshot. MjTSUMO is
        safe because MjClient_Tsumo dispatches its motion BEFORE it rewrites the
        hand array from +0x2a, so the repacked hand lands after the animation.
        """
        k = self.kyoku
        motion = self.CALL_MOTION[action]
        pre_hands, pre_ponds = pre
        slots = self.taken_slots(pre_hands[seat], k.hands[seat].tiles)[:2]
        while len(slots) < 2:                       # never index off the hand
            slots.append(0)
        tiles = [M.tile_byte(pre_hands[seat][i]) for i in slots]
        self.trace("CALL  %s %s from %s  slots=%s  [motion %d -- banner, sound, "
                   "meld build and pond pop are all client-side]"
                   % (self._seatname(seat), action.upper(),
                      self._seatname(discarder), slots, motion))
        return self.msg_alldata(
            subtype=motion, hands=pre_hands, ponds=pre_ponds,
            # +0x1d is unread by motions 4/5; +0x1e/+0x1f are the two slots.
            call={"from": discarder, "seat": seat,
                  "tiles": tiles, "slots": [0] + slots},
            label="%s seat %d" % (action, seat))

    def msg_kan(self, kind, seat, pre, discarder=None, rinshan=None):
        """A granted KAN -- daiminkan / ankan / kakan -- as records.

        `kind` is "minkan" (= "daiminkan" / "kan"), "ankan" or "kakan"
        (= "shouminkan"); `pre` is the `call_snapshot()` taken BEFORE the
        engine formed the kan (and drew the rinshan tile); `discarder` the seat
        whose discard a daiminkan claims. Call it AFTER `k.call_pon(seat,
        kan=True)` / `call_ankan` / `call_kakan` and send NOTHING after it but
        what the caller's turn needs -- the rinshan draw is already inside.

        `rinshan`: "now" (the rinshan tile is in hand: the record carries it),
        "defer" (a PROVISIONAL kakan/ankan holding for the chankan window: the
        record holds the flight and the caller sends `msg_tsumo(seat,
        motion=9)` after `complete_kakan()`), or None = "now" when the hand
        has drawn (`h.drawn`) else "defer".

        KAN_MOTION off (default): today's resync -- MjALLDATA subtype 0 (the
        post-kan board, rinshan tile in hand) + MjTSUMO motion 2. Proven live
        for a daiminkan; this also gives ankan/kakan the resync they lacked
        (the meld used to appear only on the next bot turn). A deferred kan
        sends the resync alone; the later motion-9 MjTSUMO is then a motion 2.

        KAN_MOTION on: ONE MjALLDATA carrying motion 6/7/8 with the fields the
        handler reads (yamaguchi__002e8ae0 / 002e9a40 / 002e93f0 -- janmsgs'
        CALL MOTIONS banner) in three tenses (PRE hands and ponds, POST melds),
        the dead wall with the new indicator REVEALED and the rinshan tile still
        in its slot, and the RINSHAN TILE at +0x1c: the MjALLDATA handler itself
        chains the kan-dora flip, re-fills the dead wall and flies that tile to
        hand slot 13 (console.c:4101-4118 -> yamaguchi__002e2660 = a synthesised
        motion 9). The MjTSUMO that follows carries motion 0 -- a motion 9
        there would pull a SECOND tile out of the dead wall. Falls back to the
        resync form when the hand slots do not have the shape the motion needs
        (ankan wants three consecutive slots + one).
        """
        k = self.kyoku
        h = k.hands[seat]
        motion = self.CALL_MOTION.get(kind)
        if motion not in (6, 7, 8):
            raise ValueError("msg_kan: kind %r is not a kan" % (kind,))
        if rinshan is None:
            rinshan = "now" if h.drawn is not None else "defer"
        deferred = rinshan == "defer"
        pre_hands, pre_ponds = pre
        post = list(h.tiles)
        # The rinshan tile is in `post` (draw_for appended it) and not in
        # `pre`, so the multiset walk names exactly the slots the kan took.
        slots = self.taken_slots(pre_hands[seat], post)
        need = {6: 3, 7: 4, 8: 1}[motion]
        shape_ok = len(slots) == need
        if motion == 7 and shape_ok:
            shape_ok = (slots[1] == slots[0] + 1 and slots[2] == slots[0] + 2)
        meld_index = None
        if motion == 8:
            for i, m in enumerate(h.melds):
                if m.kind == mj.KAKAN and slots and \
                        m.base == mj.kind(pre_hands[seat][slots[0]]):
                    meld_index = i
            shape_ok = shape_ok and meld_index is not None
        if not knobs.KAN_MOTION or not shape_ok:
            if knobs.KAN_MOTION and not shape_ok:
                self.log.append(("kan-shape", (kind, seat, slots)))
            self.trace("KAN   %s %s  [resync path%s%s]"
                       % (self._seatname(seat), kind,
                          "" if not knobs.KAN_MOTION else ", motion shape unavailable",
                          ", rinshan deferred" if deferred else ""))
            out = [self.msg_alldata(label="kan resync seat %d" % seat)]
            if not deferred:
                out.append(self.msg_tsumo(seat, motion=2))
            return out
        tile = M.tile_byte(pre_hands[seat][slots[0]])
        rinshan_tile = 0 if deferred else M.tile_byte(h.drawn)
        if motion == 6:
            call_slots = slots[:3]
        elif motion == 7:
            call_slots = [slots[0], slots[3], 0]
        else:
            call_slots = [meld_index, slots[0], 0]
        # The dead wall the motion reads: the rinshan tile STILL in its slot
        # (the handler's own motion 9 takes it), the new indicator ALREADY
        # revealed at its 4+2k slot (the chain scans 12,10,8,6 for the newest
        # revealed and flips it). A daiminkan/kakan indicator the engine
        # defers to the next discard (`pending_dora`) is shown now, as the
        # client will; a provisional kan has not counted its dora yet, so the
        # record shows one more than the engine does.
        pre_kans = getattr(pre, "kans", max(0, k.wall.kans - 1))
        pending = int(getattr(k, "pending_dora", 0))
        if deferred and getattr(k, "pending_kan", None) is not None:
            pending += 1
        shown = min(5, k.wall.dora_shown + pending)
        wanpai = self._wanpai_bytes(dora_shown=shown, kans=pre_kans)
        flags = [0, 0, 0, 0]
        if deferred:
            flags[seat] = 1                 # +0x23+seat: hold the rinshan flight
        self.trace("KAN   %s %s%s  slots=%s  [motion %d; kan-dora flip, meld "
                   "build, pond pop and the rinshan draw (%s) are client-side]"
                   % (self._seatname(seat), kind,
                      " from %s" % self._seatname(discarder)
                      if motion == 6 and discarder is not None else "",
                      slots, motion,
                      "HELD for the chankan window" if deferred
                      else "+0x1c " + narration._tile(h.drawn)))
        rec = self.msg_alldata(
            subtype=motion, hands=pre_hands, ponds=pre_ponds, wanpai=wanpai,
            call={"from": discarder or 0, "seat": seat,
                  "tiles": [tile, rinshan_tile], "slots": call_slots},
            seat_flags=flags, label="%s seat %d" % (kind, seat))
        if deferred:
            return [rec]
        return [rec, self.msg_tsumo(seat, motion=0)]

    def msg_draw_reveal(self):
        """The exhaustive-draw beat: ONE MjALLDATA with motion 10 and the
        TENPAI seats in +0x22 -- console.c:4085-4099 fires banner 6 (the 流局
        cut-in) and flies those seats' hands in from the hand block, so the
        block carries their real tiles; noten opponents stay blank (they do
        not show). An abortive draw sends mask 0: the banner still fires and
        nothing flies. Nothing follows it but MjSEISAN -- the motion mutates
        no state, so no resync is owed either.
        """
        k = self.kyoku
        kind_, data = k.result if k.result is not None else (None, {})
        shown = set()
        if kind_ == mj.DRAW and isinstance(data, dict):
            shown = set(data.get("tenpai", ()) or ())
            shown |= set(s for s, _sc in (data.get("nagashi", ()) or ()))
        mask = sum(1 << s for s in shown)
        hands = [list(k.hands[s].tiles) if (s in shown or s in self.live) else []
                 for s in range(4)]
        self.trace("RYUKYOKU banner  tenpai=%s  [motion 10, mask %#x]"
                   % ("/".join(self._seatname(s) for s in sorted(shown)) or "nobody",
                      mask))
        return self.msg_alldata(subtype=10, hands=hands, seat_mask=mask,
                                reveal=shown, label="draw reveal")
