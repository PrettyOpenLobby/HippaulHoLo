"""Bot: the seat with no client behind it, and how it discards and calls."""
import janmahjong as mj                                             # noqa: E402
from . import knobs


# --- bots --------------------------------------------------------------------

class Bot(object):
    """A seat with no client behind it.

    Deliberately simple and deliberately HONEST: it wins when it can win, and
    otherwise it discards the tile that leaves it closest to tenpai. It never
    calls, which is legal play and keeps the meld path off the critical path
    until a live client has exercised it. `POL_JAN_BOT=tsumogiri` turns even the
    shanten search off, which matters if a slow host makes turns crawl.
    """

    #: How many terminal/honour TILES a hand may still be holding and still be
    #: called a plausible tanyao. They have to be discarded before the hand can
    #: win, and there are only so many turns left to do it in.
    TANYAO_SHED = 2

    #: How many terminal/honour tiles the concealed rest must already hold for
    #: a terminal-bearing call to read as chanta rather than an accident.
    CHANTA_LEAN = 4

    #: Pairs left in hand before a pon reads as a toitoi plan.
    TOITOI_PAIRS = 2

    def __init__(self, seat, policy=knobs.BOT_POLICY):
        self.seat = seat
        self.policy = policy

    def choose_discard(self, kyoku):
        """The tile to discard from a 14-tile (or post-call 11/8/5-tile) hand.

        2026-09-04 (audit, rules engine 3): a riichi hand discards what it
        drew, always; after a call there is no drawn tile and the hand is
        searched exactly like any other (it used to fall back to `tiles[-1]`
        -- the highest honour, whatever it did to the hand); and the kinds
        `kuikae_forbidden` names after this seat's own chi/pon are never
        chosen (the engine would refuse them and the bot would stall).
        """
        h = kyoku.hands[self.seat]
        if h.riichi and h.drawn in h.tiles:
            return h.drawn
        if self.policy == "tsumogiri" and h.drawn in h.tiles:
            return h.drawn
        try:
            forbidden = set(kyoku.kuikae_forbidden(self.seat))
        except Exception:
            forbidden = set()
        best, best_key = None, None
        seen = set()
        for t in h.tiles:
            k = mj.kind(t)
            if k in seen or k in forbidden:
                continue
            seen.add(k)
            c = h.counts()
            c[k] -= 1
            key = (mj.shanten(c, len(h.melds)), 0 if mj.is_red(t) else 1)
            if best_key is None or key < best_key:
                best, best_key = t, key
        if best is None:                    # every kind forbidden: cannot happen
            best = next((t for t in h.tiles if mj.kind(t) not in forbidden),
                        h.tiles[-1])
        return best

    def wants_kan(self, kyoku):
        """A concealed or added kan on this bot's own turn: ("ankan"|"kakan",
        kind) or None. Conservative: an ankan only when it does not push the
        hand further from tenpai (the engine has already checked a riichi
        ankan leaves the waits alone), a kakan only on the 4th tile just
        DRAWN. `kyoku.ankan_options/kakan_options` are the source of truth
        (turn, draw, 5th kan, dead wall, riichi)."""
        if self.policy == "tsumogiri":
            return None
        h = kyoku.hands[self.seat]
        if h.drawn is None:
            return None
        before = mj.shanten(h.counts(), len(h.melds))
        for k in kyoku.ankan_options(self.seat):
            c = h.counts()
            c[k] -= 4
            if h.riichi or mj.shanten(c, len(h.melds) + 1) <= before:
                return ("ankan", k)
        for k in kyoku.kakan_options(self.seat):
            if mj.kind(h.drawn) == k:
                return ("kakan", k)
        return None

    def wants_riichi(self, kyoku):
        h = kyoku.hands[self.seat]
        if h.riichi or not h.menzen or kyoku.scores[self.seat] < 1000:
            return False
        return kyoku.wall.remaining > 4 and mj.shanten(h.counts(), 0) <= 0

    def calls(self, kyoku, tile, options):
        """Which call to take on `tile`, or None.

        Until 2026-09-04 this said "ron or nothing" and had no callers at all,
        so a CPU never pon'd or chi'd in any hand -- it conceded every discard
        and made a solo game feel oddly passive (seen live: "they never
        chi or ponned which is just... odd?"). It also meant the call
        animations only ever fired when the human called.

        The judgement below is deliberately conservative. It is trying to be an
        opponent that PLAYS, not one that plays well.

        A DAIMINKAN is taken (2026-09-04) when the hand holds the triplet,
        the kan does not push it further from tenpai, and an open hand still
        has a yaku path -- the same tests a pon passes, with "not worse"
        instead of "closer" because the fourth tile adds nothing to shape.
        `Table.legal_calls` only offers "kan" while the dead wall and the
        kan count allow it. Both kan record paths (`msg_kan`) serve it.
        """
        if "ron" in options:
            return "ron"
        if self.policy == "tsumogiri":
            return None
        h = kyoku.hands[self.seat]
        if "kan" in options:
            take = mj.minkan_option(h, tile)
            if take and self._worth_calling(kyoku, h, tile, take, "pon",
                                            allow_equal=True):
                return "kan"
        # Rule precedence, and it is not cosmetic: a pon may be claimed by any
        # seat and beats a chi, which only the discarder's left neighbour can
        # make at all.
        for name in ("pon", "chi"):
            if name not in options:
                continue
            take = self._take_for(h, tile, name)
            if take and self._worth_calling(kyoku, h, tile, take, name):
                return name
        return None

    def _take_for(self, hand, tile, name):
        """The tiles this call would spend out of the hand, or None."""
        if name == "pon":
            return mj.pon_option(hand, tile)
        pairs = mj.chi_options(hand, tile)
        if not pairs:
            return None
        # Prefer the chi that leaves the hand closest to tenpai.
        best, best_key = None, None
        for pair in pairs:
            c = hand.counts()
            for t in pair:
                c[mj.kind(t)] -= 1
            key = mj.shanten(c, len(hand.melds) + 1)
            if best_key is None or key < best_key:
                best, best_key = pair, key
        return best

    def _worth_calling(self, kyoku, hand, tile, take, name, allow_equal=False):
        """Two tests: does it get us closer, and can the hand still score?"""
        before = mj.shanten(hand.counts(), len(hand.melds))
        c = hand.counts()
        for t in take:
            c[mj.kind(t)] -= 1
        after = mj.shanten(c, len(hand.melds) + 1)
        if after > before or (after == before and not allow_equal):
            return False                 # a call that does not advance is noise
        # WARNING: AN OPEN HAND WITH NO YAKU CAN NEVER WIN. Calling into one is how a
        # bot bricks itself for the rest of the hand -- it reaches tenpai and
        # then simply cannot claim anything. A closed hand may only be opened
        # if something will still score it.
        if hand.menzen and not self._open_yaku_path(kyoku, hand, tile, take, c,
                                                    name):
            return False
        return True

    def _open_yaku_path(self, kyoku, hand, tile, take, after_counts, name):
        """Is there still a yaku once this hand is open? Conservative: it names
        the three that survive opening and are cheap to test."""
        kinds = set(k for k, n in enumerate(after_counts) if n)
        kinds |= set(mj.kind(t) for t in take) | {mj.kind(tile)}
        for m in hand.melds:
            kinds |= set(mj.kind(t) for t in m.tiles)
        # Yakuhai: a triplet of a dragon, or of our own or the round's wind.
        if name == "pon":
            k = mj.kind(tile)
            if k in mj.DRAGONS or k in (kyoku.seat_wind(self.seat),
                                        kyoku.round_wind):
                return True
        for m in hand.melds:
            mk = set(mj.kind(t) for t in m.tiles)
            if len(mk) == 1:
                k = next(iter(mk))
                if k in mj.DRAGONS or k in (kyoku.seat_wind(self.seat),
                                            kyoku.round_wind):
                    return True
        # Tanyao. NOT "no terminal anywhere" -- that was the first cut and it
        # rejected 241 of 241 chi, because a 13-tile hand almost always holds a
        # terminal SOMEWHERE and tanyao is about the hand you finish with, not
        # the one you are holding. What actually kills tanyao is a MELD
        # containing a terminal or honour, since a meld can never be discarded.
        # The rest is only a question of how much you still have to shed.
        called = set(mj.kind(t) for t in take) | {mj.kind(tile)}
        for m in hand.melds:
            called |= set(mj.kind(t) for t in m.tiles)
        if not (called & mj.YAOCHUU):
            spare = sum(n for k, n in enumerate(after_counts)
                        if n and k in mj.YAOCHUU)
            if spare <= self.TANYAO_SHED:
                return True
        # Honitsu / chinitsu: one suit, plus honours.
        if len(set(k // 9 for k in kinds if k < mj.HONOR)) <= 1:
            return True
        # Chanta / junchan -- the MIRROR of tanyao, and the reason the first cut
        # rejected 211 of 214 improving chi: a 1-2-3 or 7-8-9 run is exactly
        # what this yaku is made of, and the terminal that disqualifies tanyao
        # is what QUALIFIES this. Every set must contain a yaochuu, and the
        # concealed rest has to already lean that way.
        if all(set(mj.kind(t) for t in m.tiles) & mj.YAOCHUU
               for m in hand.melds) and (called & mj.YAOCHUU):
            if sum(n for k, n in enumerate(after_counts)
                   if n and k in mj.YAOCHUU) >= self.CHANTA_LEAN:
                return True
        # Toitoi: all triplets. Only a pon can build it, and only if nothing
        # already called is a run.
        if name == "pon" and all(len(set(mj.kind(t) for t in m.tiles)) == 1
                                 for m in hand.melds):
            pairs = sum(1 for n in after_counts if n >= 2)
            if pairs >= self.TOITOI_PAIRS:
                return True
        return False
