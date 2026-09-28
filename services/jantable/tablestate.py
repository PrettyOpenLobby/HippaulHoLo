"""The board as the records carry it: the public view, round and rule scalars, dead wall, ponds."""
import random
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
from . import knobs, narration, table


class TableState:
    """Part of `Table` (table.py), which inherits it.

    The board as the records carry it: the public view, round and rule scalars,
    dead wall, ponds.
    """

    def wall_count(self):
        return self.kyoku.wall.remaining if self.kyoku else 0

    def public_state(self):
        """The table as SE's own spectator sees it -- all four hands face up
        (the spectator view shows every hand), the ponds, melds, scores and the
        round -- as plain JSON for the web board's DELAYED view. Never the
        wall's order and never the ura dora (secret until a win shows them)."""
        k, g = self.kyoku, self.game
        seats = []
        for s in range(4):
            seat = {"name": self.nicks[s] or "", "bot": s in self.bots,
                    "left": s in self.dropped,
                    # The player's ACCOUNT number, for the board to turn into
                    # their PlayOnline portrait (it never passes it on); None
                    # for a COM.
                    #
                    # WARNING: NOT `self.seats[s]`, and not a magnitude test on it.
                    # `seat_player`'s own docstring says `self.seats[]` holds the
                    # WIRE id -- the client's PolID -- while `self.members` holds
                    # our account number, and the portrait lives in
                    # `handle_profile` under the ACCOUNT. The old line also tested
                    # `< BOT_ID_BASE` to mean "a person", but a real PolID
                    # (0x5b01...) is ABOVE the bot base (0x0b07...), so it named
                    # every HUMAN seat None: `jan-tables-live.json` showed
                    # every seat `"mid": null` while two people were playing.
                    # The COMs looked fine only because the page draws those
                    # from its own ACKY pair.
                    "mid": (None if s in self.bots else self.member_of_seat(s)),
                    "score": (k.scores[s] if k is not None
                              else (g.scores[s] if g is not None else None))}
            h = k.hands[s] if k is not None else None
            if h is not None:
                seat.update(
                    hand=[narration._web_tile(x) for x in h.tiles],
                    drawn=narration._web_tile(h.drawn),
                    pond=[{"tile": narration._web_tile(x), "called": i in h.pond_called,
                           "riichi": i == h.riichi_index}
                          for i, x in enumerate(h.pond)],
                    melds=[{"kind": m.kind, "tiles": [narration._web_tile(x) for x in m.tiles],
                            "from": m.from_seat, "called": narration._web_tile(m.called)}
                           for m in h.melds],
                    riichi=bool(h.riichi))
            seats.append(seat)
        out = {"state": self.state, "seats": seats, "round": {}, "dora": [], "game": self.id,
               "wall": None, "turn": None, "last": None, "result": None}
        key = getattr(self, "lobby_key", None)
        if key:
            out["room"], out["table"] = int(key) >> 16, int(key) & 0xFFFF
        if k is not None:
            out["round"] = {"wind": "ESWN"[(k.round_wind - mj.EAST) & 3],
                            "number": k.kyoku + 1, "dealer": k.dealer,
                            "honba": k.honba, "sticks": k.riichi_sticks}
            out["dora"] = [narration._web_tile(x) for x in k.wall.dora_indicators()]
            out["wall"] = k.wall.remaining
            out["turn"] = k.turn
            if k.last_discard is not None and k.last_discard_seat >= 0:
                out["last"] = {"tile": narration._web_tile(k.last_discard), "seat": k.last_discard_seat}
            if k.result:
                kind_, data = k.result[0], k.result[1] if len(k.result) > 1 else None
                res = {"kind": str(kind_)}
                if kind_ in (mj.RON, mj.TSUMO) and isinstance(data, (list, tuple)):
                    res["wins"] = [{"seat": ws, "han": getattr(sc, "han", None),
                                    "points": getattr(sc, "points", None),
                                    "limit": getattr(sc, "limit", None),
                                    "yaku": [[str(n), v] for n, v in (getattr(sc, "yaku", None) or [])]}
                                   for ws, sc in data]
                out["result"] = res
        if self.state == "finished":
            # a game's last frame (Manager._keep_final): over, or stopped early
            ee = getattr(self, "ended_early", None)
            out["ended"] = ({"early": True, "seat": ee["seat"], "why": ee["reason"],
                             "name": self.nicks[ee["seat"]] or ""} if ee
                            else {"early": False})
        return out

    def _dice(self):
        """The hand's two dice (1..6 each). `Kyoku.dice` when the engine has it
        (it draws them at construction); otherwise a deterministic pair derived
        from the wall order, so the same hand always shows the same dice.

        TODO(engine): `Kyoku.dice` is the intended source -- drop the fallback
        once every deployed engine carries it.
        """
        k = self.kyoku
        d = getattr(k, "dice", None)
        if d and len(d) >= 2 and all(1 <= int(x) <= 6 for x in d[:2]):
            return (int(d[0]), int(d[1]))
        rng = random.Random(hash(tuple(k.wall.tiles)) ^ (k.kyoku * 7 + k.honba))
        return (rng.randint(1, 6), rng.randint(1, 6))

    def _round_scalars(self):
        """THE round state, once, for every builder that carries it.

        MEASURED 2026-09-04 (janmsgs' MjHAIPAI banner: the client's own debug
        labels 場/局/起家/親 at mahdisp.c:3016, the Kyoku label writer's
        `kyoku + wind*4` index, the wall-break arithmetic at console.c:3304 and
        the dice draw at mahdisp.c:1892). Four messages carry these -- HAIPAI
        +0xd0.., YAKUDISP +0x1d.., SEISAN +0x28.., ALLDATA +0x13b.. -- and
        every one of them now reads this structure, so they cannot disagree.
        """
        k = self.kyoku
        return table.RoundScalars(
            # 起家: the seat that dealt E1 -- where the round-wind marker sits.
            chiicha=(k.dealer - k.kyoku) & 3,
            dealer=k.dealer & 3,
            round_wind=(k.round_wind - mj.EAST) & 1,       # 0 East, 1 South
            kyoku=k.kyoku & 3,
            honba=min(255, k.honba),
            riichi_sticks=min(255, k.riichi_sticks),
            dice=self._dice(),
            current_seat=k.turn & 3,
        )

    def _rule_scalars(self):
        """MjHAIPAI's rule vector (+0x2c, 18 bytes) and the per-suit red-five
        counts (+0x3e), from `self.rules`. Every field the client READS is
        named in janmsgs' banner; the engine worker's newer Rules fields are
        read with getattr so this works before and after their commit."""
        r = self.rules
        kind_ = getattr(r, "dora_kind", None)
        if kind_ is None:
            kind_ = M.DORA_KIND.get(
                (bool(getattr(r, "ura_dora", True)),
                 bool(getattr(r, "kan_dora", True)),
                 bool(getattr(r, "kan_ura",
                              getattr(r, "ura_dora", True)
                              and getattr(r, "kan_dora", True)))), 4)
        uma = tuple(getattr(r, "uma", (20, 10, -10, -20)))
        aka = getattr(r, "aka_counts", None)
        if aka is None:
            aka = getattr(r, "aka", None)
        if aka is None:
            n = getattr(r, "aka_count", 3)
            aka = tuple(1 if i < n else 0 for i in range(3))
        vec = M.rule_scalars(dora_kind=kind_,
                             aka_rank=getattr(r, "aka_rank", 5),
                             wareme=bool(getattr(r, "wareme", False)),
                             # the engine (and the bot) refuse a riichi under
                             # 1000 points; tell the client so its button agrees
                             riichi_1000=True,
                             yakitori=bool(getattr(r, "yakitori", 0)),
                             uma=(uma[0], uma[1]) if len(uma) >= 2 else (0, 0))
        return vec, bytes(min(255, int(a)) for a in aka[:3]).ljust(3, b"\0")

    def _wanpai_u16(self):
        k = self.kyoku
        return M.wanpai_u16s(k.wall.dead, k.wall.dora_shown, k.wall.kans,
                             legacy=knobs.WANPAI_LEGACY)

    def _wanpai_bytes(self, dora_shown=None, kans=None):
        k = self.kyoku
        return M.wanpai_bytes(k.wall.dead,
                              k.wall.dora_shown if dora_shown is None else dora_shown,
                              k.wall.kans if kans is None else kans,
                              legacy=knobs.WANPAI_LEGACY)

    def _pond_wire(self, seat):
        """A seat's pond as MjALLDATA wants it.

        The flag is `POND_FLAG`, and WARNING: it means THE TILE IS NOT DRAWN -- both
        of the client's pond loops bracket the draw AND the position advance in
        `if ((t & 0x80) == 0)`, so a flagged tile is skipped and the pond closes
        up over it (measured 2026-09-04, janmsgs' +0x8a note). That is the right
        thing for a CLAIMED tile, which a call takes out of the pond.

        WARNING: IT IS THE WRONG THING FOR THE RIICHI TILE, which is what this used to
        do: `i == h.riichi_index` flagged the declaring discard and the client
        then refused to draw it, so a riichi tile vanished from the pond on the
        next resync. The sideways riichi tile is +0x136 instead -- a per-seat
        one-based pond index -- and `_riichi_indices()` serves it.

        A CALL motion pops the discarder's tail itself, so its record has to
        carry the tile unflagged -- which is why `call_snapshot()` takes this
        BEFORE the engine adds the index to `pond_called`.
        """
        h = self.kyoku.hands[seat]
        return [(t, i in h.pond_called) for i, t in enumerate(h.pond)]

    def _riichi_indices(self):
        """MjALLDATA +0x136: each seat's riichi pond index, ONE-BASED, 0 = none.

        The pond renderer takes its sideways branch for the tile where
        `index + 1 >= value` and shifts the rest of the row -- measured
        2026-09-04. Sending zeros (which we did until then) drew every riichi
        discard upright.
        """
        k = self.kyoku
        out = []
        for s in range(4):
            h = k.hands[s]
            out.append(h.riichi_index + 1 if h.riichi and h.riichi_index is not None
                       and h.riichi_index >= 0 else 0)
        return out
