"""Sashiuma, the pre-game side bet: the handshake, the bots' side, the payout."""
import os
import janmsgs as M                                                 # noqa: E402


#: SASHIUMA -- the finishing-rank side-bet, entirely server-driven (janmsgs'
#: MjSASHIUMA banner). After every seat is READY and BEFORE the first deal each
#: human is offered the table (MjSASHIUMASTART), picks one opponent
#: (MjSASHIUMAREQUEST), sees the merged proposals (MjSASHIUMASELECT), agrees
#: (MjSASHIUMAARGEE) and is told which bets are ON (MjSASHIUMARESULT); the deal
#: rides in the same batch as the RESULT. The client draws the ON pairs as lines
#: on the table and computes NO money: at the end the pair's worse-placed seat
#: pays the better-placed one the stake, in result points (thousands), applied
#: to the MjGAMERESULT rows. `POL_JAN_SASHIUMA=1` turns the handshake ON.
#: WARNING: OFF BY DEFAULT UNTIL CONFIRMED LIVE (2026-09-04 audit): the first cut
#: defaulted ON with the knob unset, was pushed at 23:31Z, and pol-git-sync
#: deployed a never-exercised five-message handshake that holds EVERY deal
#: on prod. A feature with zero live instances does not get to sit in front
#: of the deal by default -- nothing counts as fixed without live proof.
_sv = os.environ.get("POL_JAN_SASHIUMA", "").strip().lower()
SASHIUMA = _sv in ("1", "on", "yes", "true")
#: The stake per bet in result points; 0 = the table uma's top value (20).
SASHIUMA_UMA = float(os.environ.get("POL_JAN_SASHIUMA_UMA", "").strip() or 0)
#: A bot: "accept" = agree to any bet proposed to it, propose none (default);
#: "propose" = also propose one bet of its own; "refuse" = never bet.
SASHIUMA_BOTS = (os.environ.get("POL_JAN_SASHIUMA_BOTS", "").strip().lower()
                 or "accept")


class TableSashiuma:
    """Part of `Table` (table.py), which inherits it.

    Sashiuma, the pre-game side bet: the handshake, the bots' side, the payout.
    """

    # -- sashiuma: the pre-game side-bet handshake -----------------------------
    #
    # Wire form in janmsgs' MjSASHIUMA banner. State is `self.sashiuma`:
    # {"phase": "request"|"agree"|"done", "block": bytearray(6),
    #  "answered": set(), "started": set()}. Every human is asked; a bot's side
    # is decided here, instantly. The deal is HELD until the RESULT goes out and
    # then rides in the same batch, so the client sees phases 1-2-3 and then 4
    # (HAIPAI) -- the order its own dispatcher numbers them.

    def _sashiuma_names(self):
        names = []
        for s in range(4):
            nm = self.nicks[s]
            if not nm and s in self.bots:
                nm = "CPU %d" % s
            names.append(nm or "")
        return names

    def _sashiuma_stake(self):
        if SASHIUMA_UMA > 0:
            return float(SASHIUMA_UMA)
        uma = getattr(self.rules, "uma", None) or (20,)
        return float(abs(uma[0]))

    def msg_sashiuma_start(self, seat):
        self.sashiuma["started"].add(seat)
        rec = M.sashiuma_start(self.next_seq(), self._sashiuma_names())
        self._awaiting = (M.MjSASHIUMASTART, None)
        return self._emit(rec, "sashiuma start", to=seat)

    def _sashiuma_begin(self):
        self.sashiuma = {"phase": "request", "block": bytearray(M.SASHIUMA_BLOCK),
                         "answered": set(), "started": set()}
        self._sashiuma_bots_propose()
        self.trace("SASHIUMA offer -> %s  [stake %g]"
                   % (", ".join(self._seatname(s) for s in sorted(self.live)),
                      self._sashiuma_stake()))
        return [self.msg_sashiuma_start(s) for s in sorted(self.live)]

    def _sashiuma_bots_propose(self):
        """Policy "propose": each bot proposes one bet to a random other seat."""
        if SASHIUMA_BOTS != "propose":
            return
        blk = self.sashiuma["block"]
        for s in sorted(self.bots):
            x = self.rng.choice([o for o in range(4) if o != s])
            blk[M.sashiuma_slot(s, x)] |= M.sashiuma_bit(s)

    def _sashiuma_bots_agree(self):
        """Bots accept every bet proposed to them, unless policy "refuse"."""
        if SASHIUMA_BOTS == "refuse":
            return
        blk = self.sashiuma["block"]
        for b in self.bots:
            for x in range(4):
                if x == b:
                    continue
                i = M.sashiuma_slot(b, x)
                if blk[i] & M.sashiuma_bit(x):
                    blk[i] |= M.sashiuma_bit(b)

    def _sashiuma_merge(self, seat, block):
        """OR the sender's OWN bits from its reply into the table block. Only
        that seat's bit is honoured: a reply cannot set another seat's bit."""
        blk = self.sashiuma["block"]
        mine = M.sashiuma_bit(seat)
        for i in range(M.SASHIUMA_BLOCK):
            if block[i] & mine:
                blk[i] |= mine

    def _sashiuma_all_answered(self):
        return all(s in self.sashiuma["answered"] for s in self.live)

    def _sashiuma_to_select(self):
        st = self.sashiuma
        self._sashiuma_bots_agree()
        st["phase"] = "agree"
        st["answered"] = set()
        seq = self.next_seq()
        self._awaiting = (M.MjSASHIUMASELECT, None)
        return [self._emit(M.sashiuma_select(seq, st["block"]), "sashiuma select",
                           to=s) for s in sorted(self.live)]

    def on_sashiuma_request(self, rec, seat=None):
        st = self.sashiuma
        p = M.parse_sashiuma(rec)
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else None
        if st is None or st["phase"] != "request" or seat is None:
            # A re-sent reply after the phase moved on: re-nudge, never re-run.
            return self._renudge(seat)
        self._sashiuma_merge(seat, p["block"])
        st["answered"].add(seat)
        picks = [x for x in range(4) if x != seat
                 and st["block"][M.sashiuma_slot(seat, x)] & M.sashiuma_bit(seat)]
        self.trace("SASHIUMA %s proposes %s"
                   % (self._seatname(seat),
                      ", ".join(self._seatname(x) for x in picks) or "nothing"))
        if not self._sashiuma_all_answered():
            return []
        return self._sashiuma_to_select()

    def on_sashiuma_agree(self, rec, seat=None):
        st = self.sashiuma
        p = M.parse_sashiuma(rec)
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else None
        if st is None or st["phase"] != "agree" or seat is None:
            return self._renudge(seat)
        self._sashiuma_merge(seat, p["block"])
        st["answered"].add(seat)
        if not self._sashiuma_all_answered():
            return []
        return self._sashiuma_finish()

    def _sashiuma_finish(self):
        """Settle the block to the ON pairs, announce it, and deal."""
        st = self.sashiuma
        blk = st["block"]
        pairs = []
        for i, (a, b) in enumerate(M.SASHIUMA_PAIRS):
            both = M.sashiuma_bit(a) | M.sashiuma_bit(b)
            if blk[i] & both == both:
                blk[i] = both
                pairs.append((a, b))
            else:
                blk[i] = 0
        st["phase"] = "done"
        self.sashiuma_pairs = pairs
        self.trace("SASHIUMA on: %s"
                   % (", ".join("%s-%s" % (self._seatname(a), self._seatname(b))
                                for a, b in pairs) or "no bets"))
        seq = self.next_seq()
        out = [self._emit(M.sashiuma_result(seq, bytes(blk)), "sashiuma result",
                          to=s) for s in sorted(self.live)]
        return out + self.start_kyoku()

    def _sashiuma_seat_gone(self, seat):
        """A human left mid-handshake: the rest must not wait on it for ever.
        `drop_seat` has already removed it from `live`, so re-check completion."""
        st = self.sashiuma
        if not st or st["phase"] == "done" or not self._sashiuma_all_answered():
            return []
        if st["phase"] == "request":
            return self._sashiuma_to_select()
        return self._sashiuma_finish()

    def _sashiuma_apply(self, rank):
        """Fold the side-bets into the final results: the pair's worse-placed
        seat pays the better-placed one the stake (result points)."""
        if not self.sashiuma_pairs:
            return rank
        stake = self._sashiuma_stake()
        by_seat = {r["seat"]: r for r in rank}
        for a, b in self.sashiuma_pairs:
            ra, rb = by_seat.get(a), by_seat.get(b)
            if ra is None or rb is None:
                continue
            win, lose = (ra, rb) if ra["place"] < rb["place"] else (rb, ra)
            win["result"] = round(win["result"] + stake, 1)
            lose["result"] = round(lose["result"] - stake, 1)
            self.trace("SASHIUMA %s beats %s: %+g / %+g"
                       % (self._seatname(win["seat"]), self._seatname(lose["seat"]),
                          stake, -stake))
        return rank
