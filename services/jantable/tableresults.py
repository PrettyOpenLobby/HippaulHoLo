"""The end of a hanchan: the record written for each player and the two result screens."""
import janmsgs as M                                                 # noqa: E402
from .deps import janstats
from . import knobs, narration


class TableResults:
    """Part of `Table` (table.py), which inherits it.

    The end of a hanchan: the record written for each player and the two result
    screens.
    """

    def _record_result(self, rank):
        """Write the finished hanchan into each LIVE seat's record.

        THE ONLY PLACE A GAME IS PERSISTED, and it is here rather than in
        `msg_gameend` on purpose: the escape hatch and a dropped table both reach
        MjGAMEEND without a finished hanchan, and an abandoned game must not
        count as one played. `_recorded` makes it once per game whatever the
        client re-acks.

        WARNING: LIVE SEATS ONLY. `bot_id()` mints NON-ZERO ids deliberately -- the
        client derives its player count by counting non-zero ids -- so "the id is
        not 0" is NOT a test for "this is a person". `self.live` is. janstats
        refuses a bot id as a second line of defence.

        A stats failure is logged and swallowed: the hanchan is over either way
        and the player is looking at the results screen. Losing a record is bad;
        throwing here would lose the screen too.
        """
        if self._recorded or janstats is None or not knobs.STATS_ENABLE:
            return
        self._recorded = True
        by_seat = {r["seat"]: r for r in rank}
        hako = getattr(self.game, "end_reason", None) == "hako"
        for seat in sorted(self.live):
            # OUR account number when we hold one (the lobby path seats the
            # PolID as the wire id; the record is keyed by the member).
            member, r = self.member_of_seat(seat), by_seat.get(seat)
            if not member or r is None:
                continue
            try:
                if janstats.record_game(member, r["place"], r["score"],
                                        r["result"], table_id=self.id,
                                        bust=bool(hako and r["score"] < 0)):
                    self.log.append(("recorded", (member, seat, r["place"])))
            except Exception as e:
                self.log.append(
                    ("stats-error", "seat %d member %r: %s: %s"
                     % (seat, member, type(e).__name__, e)))

    def _history_rows(self, rows=4):
        """Rows 1..4 of `Ranking[5][4]` -- each seat's own recent placings.

        MEASURED: the geometry (`Ranking[row][seat]` = u32 at `+0x18 + row*0x10 +
        seat*4`), that the values are zero-based, and that `< 0` prints the "no
        rank" form. INFERRED: that rows 1..4 are history at all. The client's own
        log line names them `Ranking[1]`..`Ranking[4]` and nothing states what
        they hold; this reads them COLUMN-wise, so column `seat` is that seat's
        last four games.

        WARNING: The fill is `NO_RANK`, never 0. Both readings of those rows agree that
        an unknown slot has to be negative -- 0 is first place.

        `skip=1` because `_record_result` has already put the game that just
        finished at the head of the history, and it is row 0 on the wire.
        """
        out = [[M.NO_RANK] * 4 for _ in range(rows)]
        if janstats is None or not knobs.STATS_ENABLE:
            return out
        skip = 1 if self._recorded else 0
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                past = janstats.recent_places(member, n=rows, skip=skip)
            except Exception as e:
                self.log.append(("stats-error", "history seat %d: %s" % (seat, e)))
                continue
            for row in range(rows):
                out[row][seat] = past[row]
        return out

    def _money(self, seat, result):
        """(JAN before, JAN after) for a LIVE seat from its record -- the
        record already holds this game, and `record_game` kept the balance it
        started from in `money_prev`. The balance is floored at 0 game by game
        (janstats `money_balance`), so 'before' cannot be backed out of the
        lifetime sum: after two losses and a win that sum is still negative,
        and the results screen counted 0 -> 0 JAN for a won game."""
        member = self.member_of_seat(seat)
        if seat not in self.live or not member or janstats is None or not knobs.STATS_ENABLE:
            return 0, 0
        try:
            rec = janstats.load(member)
            after = janstats.derive(rec)["money"]
            before = rec.get("money_prev")
            if not isinstance(before, int):
                before = max(0, after - janstats.jan_for_points(result))
            return before, after
        except Exception as e:
            self.log.append(("stats-error", "money seat %d: %s" % (seat, e)))
            return 0, 0

    def msg_results(self):
        """HALF1 then HALF2 -- but only HALF1 goes out now; its ack pulls HALF2.

        HALF1 is PER SEAT (janmsgs' banner, 2026-09-04): the readers walk
        4 x u32 columns indexed by seat and animate stage to stage --
        raw score (+0x58/+0x68, the disconnect-penalty stage, skipped when
        equal) -> +/- points (+0x78: score - return, plus the oka for 1st) ->
        after uma (+0x88, the stage the client labels from HAIPAI +0x3c/+0x3d)
        -> after yakitori (+0x98, stage gated on HAIPAI +0x33) -> final
        (+0xa8 = `ranking()['result']`, the sashiuma stage gated on the +0xec
        block) -> money (+0x18 -> +0x38, JAN). Anything the engine folds into
        `result` beyond uma/oka/sashiuma (yakitori, when it lands) shows in the
        yakitori stage.

        WARNING: +0x78..+0xab ARE IN **P** -- THE +/-45 POINT, NOT THE 25000 SCORE
        (2026-09-12). We sent them x1000 and the results screen took OVER HALF
        AN HOUR to count up. Three of the five stages -- uma
        (`yamaguchi2__00332610`), yakitori (`00333370`) and sashiuma
        (`00333a90`) -- size their animation off the DATA:

            n = max over seats of (after - before)
            step[seat] = (after - before) / n     (floored, minimum 1)
            for n frames: display += step         <- ONE FRAME PER UNIT

        so the stage runs for as many frames as the biggest delta. At x1000 a
        10-20 uma is 20000 frames (5.5 min at full speed, far worse under
        emulation) and 30-90 is 90000; in P it is 20 and 90 frames -- the same
        ballpark as the two stages SE hard-coded to 0x5a (the penalty stage
        `00331f40` and the money stage `00334210`, which both divide by 0x5a
        and loop 0x5a, and so are scale-free). P is also the unit the money
        stage converts at ("This table's rate is 1P = %d JAN", and janstats'
        money = points x rate), so +0xa8 has to be the same number the JAN
        conversion is quoted against. +0x58/+0x68 stay RAW SCORES: that stage
        is the score column and its ramp is the scale-free 0x5a one.
        """
        rank = self._sashiuma_apply(self.game.ranking())
        self._record_result(rank)
        rules = self.game.rules
        by_seat = {r["seat"]: r for r in rank}
        genten = getattr(rules, "genten", 25000)
        ret = getattr(rules, "oka_return", 30000)
        uma = list(getattr(rules, "uma", (20, 10, -10, -20))) + [0] * 4
        oka = (ret - genten) * 4 / 1000.0        # in P, like everything below
        stake = self._sashiuma_stake() if self.sashiuma_pairs else 0
        sashi = [0.0] * 4
        for a, b in self.sashiuma_pairs or ():
            ra, rb = by_seat.get(a), by_seat.get(b)
            if ra is None or rb is None:
                continue
            win, lose = (a, b) if ra["place"] < rb["place"] else (b, a)
            sashi[win] += stake
            sashi[lose] -= stake
        places, scores, points, after_uma, after_yaki, final = ([0] * 4 for _ in range(6))
        money_before, money_after = [0] * 4, [0] * 4
        for s in range(4):
            r = by_seat[s]
            places[s] = r["place"]
            scores[s] = int(r["score"])
            # Every column below is P. The stage values are the RUNNING TOTAL
            # at each step, so each one is rounded from the exact figure rather
            # than from the previous (already rounded) stage -- that keeps the
            # last stage equal to the result we record and pay in JAN.
            base = (scores[s] - ret) / 1000.0 + (oka if r["place"] == 0 else 0)
            points[s] = narration._round_p(base)
            after_uma[s] = narration._round_p(base + int(uma[r["place"]]))
            final[s] = narration._round_p(float(r["result"]))
            after_yaki[s] = narration._round_p(float(r["result"]) - sashi[s])
            money_before[s], money_after[s] = self._money(s, r["result"])
        blk = bytearray(6)
        for a, b in self.sashiuma_pairs or ():
            blk[M.sashiuma_slot(a, b)] = M.sashiuma_bit(a) | M.sashiuma_bit(b)
        rec = M.gameresult_half1(self.next_seq(), places=places, scores=scores,
                                 points=points, after_uma=after_uma,
                                 after_yakitori=after_yaki, final=final,
                                 money_before=money_before,
                                 money_after=money_after, sashiuma=bytes(blk))
        self._awaiting = (M.MjGAMERESULTHALF1, None)
        self.trace("RESULT places=%s scores=%s points=%s final=%s (P)"
                   % (places, scores, points, final))
        return self._emit(rec, "results 1")

    def _levels(self):
        """`([Level per seat], [LevelUp per seat])` for MjGAMERESULTHALF2.

        THE LEVEL A PLAYER SEES COMES FROM HERE. It is not in the save --
        measured 2026-08-24, `U/g/MJSUserData` has no level field anywhere in
        the module; `yamaguchi2__00335050` reads it out of THIS message at
        `+0xA0 + seat*4`. So if we send zero, the results screen says "Your
        level is now 0".

        `LevelUp` is the DELTA and doubles as a flag: non-zero makes the client
        draw "Level up! Now %d.". `janstats.level` / `level_up` are the one
        ladder every screen reads (the lobby row, the popup, this) -- the
        delta is claimed only for the game just recorded.
        """
        levels, ups = [1] * 4, [0] * 4
        if janstats is None or not knobs.STATS_ENABLE:
            return levels, ups
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                levels[seat] = int(janstats.level(member))
                ups[seat] = int(janstats.level_up(member)) if self._recorded else 0
            except Exception as e:
                self.log.append(("stats-error", "level seat %d: %s" % (seat, e)))
        return levels, ups

    def _titles(self):
        """`(shogo, getshogo)` for HALF2 -- [title][seat], titles HELD and
        titles EARNED by the game just recorded, from the record screen's
        own thresholds (janstats). All zero without a record: `getshogo`
        non-zero makes the client announce "You earned the next title."."""
        held = [[0] * 4 for _ in range(5)]
        got = [[0] * 4 for _ in range(5)]
        if janstats is None or not knobs.STATS_ENABLE:
            return held, got
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                s = list(janstats.shogo(member))[:5]
                g = list(janstats.getshogo(member))[:5] if self._recorded else []
                for i, v in enumerate(s):
                    held[i][seat] = int(v) & 0xFF
                for i, v in enumerate(g):
                    got[i][seat] = 1 if v else 0
            except Exception as e:
                self.log.append(("stats-error", "titles seat %d: %s" % (seat, e)))
        return held, got

    def msg_results2(self):
        rank = {r["seat"]: r["place"] for r in self.game.ranking()}
        # Five rows of four. Row 0 is this game's placing; rows 1..4 are each
        # seat's recent history, NO_RANK where we hold none. WARNING: ZERO-BASED --
        # the client prints rank+1, so 0 means first place and is never a blank.
        row0 = [rank[s] for s in range(4)]
        levels, ups = self._levels()
        shogo, getshogo = self._titles()
        rec = M.gameresult_half2(self.next_seq(), [row0] + self._history_rows(),
                                 levels=levels, levelups=ups,
                                 shogo=shogo, getshogo=getshogo)
        self._awaiting = (M.MjGAMERESULTHALF2, None)
        return self._emit(rec, "results 2")

    def msg_gameend(self):
        rec = M.gameend(self.next_seq())
        self._awaiting = (M.MjGAMEEND, None)
        self.state = "over"
        return self._emit(rec, "game over")
