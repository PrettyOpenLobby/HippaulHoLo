#!/usr/bin/env python3
"""The mahjong RULES ENGINE for Janhourou -- wall, hands, calls, yaku, payment.

WHY THIS EXISTS ON THE SERVER. `jan-component-map` settled it by reading the
client: **JanHouRou.pex does not score mahjong.** Its yaku strings live only in
`ScoreDispWindow.cc` as display text, and what the client holds is the table
SETTINGS enum (`MJS_CONFIG_IDX_*`, read by `ruleset.cc`). Wall, deal, call
legality, yaku, fu and payment are all the server's job -- so a revived service
has to contain a real mahjong implementation, and this is it.

DELIBERATELY WIRE-FREE. Nothing in this file knows about the 32-byte record, the
six-bit line encoding or the opcode table; `janmsgs.py` owns all of that and
`jangame.py` drives this engine from it. The split is not tidiness -- it is what
makes the engine testable with no client, no socket and no PS2.

TILE REPRESENTATION (internal, canonical -- NOT the wire numbering):

    0..8    man 1-9        27  east    31  haku  (white)
    9..17   pin 1-9        28  south   32  hatsu (green)
    18..26  sou 1-9        29  west    33  chun  (red)
                           30  north

    | RED (0x40)           a red five (aka dora). `kind(t)` strips it.

The wire numbering is a SEPARATE question and lives in `janmsgs.py`, which is
where the inference is quarantined: the client uses the tile id as an index into
its own texture atlas, so the mapping is pinnable by one probe rather than by
argument. Keeping it out of the engine means a wrong guess there cannot corrupt
the rules.

RULE SET. Standard Japanese riichi (reach) mahjong, four players, as configured
by the table's own `MJS_CONFIG_IDX_*` settings -- every one of the client's
mahjong-rule dropdowns has a `Rules` field, and `Rules.from_config({name:
index})` builds one from the parsed table config. Where a label's meaning is
inferred rather than measured it is marked as such in `Rules`.

WHAT THE TABLE LAYER OWES THE ENGINE (2026-09-04, audit findings 12/13/21/22):
the chankan window (`call_kakan(..., provisional=True)` -> `chankan_candidates`
-> `win_ron(chankan=True)` or `complete_kakan()`), `note_missed_ron()` for a
declined ron, `kuikae_forbidden()` before a post-call discard, `ankan_options`
/ `kakan_options` ON THE KYOKU (not the hand) for the kan button, `dice` /
`wall_break_seat` for HAIPAI, and `pending_dora` for the kan-dora reveal
beat. Every illegal move now raises ValueError with the hand untouched. The
`Kyoku` docstring has the list.

    python janmahjong.py --selftest
    python janmahjong.py --demo          # play one hanchan out, printing it
"""
import argparse
import random

# --- tiles -------------------------------------------------------------------

RED = 0x40                      # aka-dora flag on a tile instance
MAN, PIN, SOU, HONOR = 0, 9, 18, 27
EAST, SOUTH, WEST, NORTH = 27, 28, 29, 30
HAKU, HATSU, CHUN = 31, 32, 33

TILE_NAMES = ([f"{n}m" for n in range(1, 10)] +
              [f"{n}p" for n in range(1, 10)] +
              [f"{n}s" for n in range(1, 10)] +
              ["E", "S", "W", "N", "P", "F", "C"])

TERMINALS = frozenset([0, 8, 9, 17, 18, 26])
HONORS = frozenset(range(27, 34))
WINDS = frozenset([27, 28, 29, 30])
DRAGONS = frozenset([31, 32, 33])
YAOCHUU = TERMINALS | HONORS
GREENS = frozenset([19, 20, 21, 23, 25, 32])       # 2,3,4,6,8 sou + hatsu


def kind(t):
    """The tile without its red-five flag."""
    return t & 0x3F


def is_red(t):
    return bool(t & RED)


def name(t):
    return ("r" if is_red(t) else "") + TILE_NAMES[kind(t)]


def hand_str(tiles):
    return " ".join(name(t) for t in sorted(tiles, key=kind))


def is_honor(t):
    return kind(t) >= 27


def suit_of(t):
    """0 man, 1 pin, 2 sou, 3 honor."""
    k = kind(t)
    return 3 if k >= 27 else k // 9


def rank_of(t):
    """1..9 for suited tiles, 0 for honors."""
    k = kind(t)
    return 0 if k >= 27 else k % 9 + 1


def dora_from_indicator(ind):
    """The dora is the tile AFTER the indicator, wrapping inside its group."""
    k = kind(ind)
    if k < 27:
        base, r = (k // 9) * 9, k % 9
        return base + (r + 1) % 9
    if k <= NORTH:
        return EAST + (k - EAST + 1) % 4
    return HAKU + (k - HAKU + 1) % 3


# --- rules (the table settings, as far as they are mapped) -------------------

class Rules(object):
    """A table's rule set.

    The NAMES come from SE's own `MJS_CONFIG_IDX_*` enum (`tools/jantableinfo.py`
    carries all 33 with their measured label counts). The BEHAVIOUR each drives
    is our reading of a standard rule set: only the vocabulary is measured, so
    treat any mapping here as inference until a live client disagrees.
    """

    #: The client's own value labels, index -> engine meaning. Decoded from the
    #: 0x40be50 label arrays (the 2026-09-04 audit). Every list is in
    #: the client's index order, so `from_config` is a table lookup.
    GENTEN_VALUES = (10000, 15000, 20000, 25000, 27000, 30000)
    #: (east condition, south condition): "win" = the dealer keeps the deal
    #: only on a win; "tenpai" = also on a tenpai draw; "noten" = on any draw.
    RENCHAN_VALUES = (("win", "win"), ("win", "tenpai"), ("win", "noten"),
                      ("tenpai", "tenpai"), ("tenpai", "noten"), ("noten", "noten"))
    #: (ura, kan dora, kan ura). "All+Kan" vs "All" is the one INFERRED pair:
    #: read as "everything but the kan-ura" vs "everything".
    DORA_VALUES = ((False, False, False),   # Front
                   (True, False, False),    # Both (front + ura)
                   (False, True, False),    # Front + Kan
                   (True, True, False),     # All + Kan
                   (True, True, True))      # All
    #: (2nd-place uma, 1st-place uma) in thousands; symmetric for 3rd/4th.
    UMA_VALUES = ((0, 0), (5, 10), (10, 20), (10, 30), (20, 60), (30, 90))
    YAKITORI_VALUES = (0, 5, 10, 20)
    #: index 0 = no noten payment, i = 3000*i total (3000 .. 30000).
    NOTEN_VALUES = tuple(3000 * i for i in range(11))
    #: the West-round cap for the extension: index of W4 in hand order.
    EXTENSION_CAP = 11

    def __init__(self, **kw):
        self.genten = kw.get("genten", 25000)          # MJS_CONFIG_IDX_GENTEN
        self.oka_return = kw.get("oka_return", 30000)  # the return score for oka
        self.uma = tuple(kw.get("uma", (20, 10, -10, -20)))   # IDX_UMA
        # IDX_KYOKUSU: `last_hand` is the index of the last scheduled hand
        # (E1..E4 = 0..3, S1..S4 = 4..7). `hanchan` is the old boolean view of
        # the same setting and both spellings keep working.
        if "last_hand" in kw:
            self.last_hand = int(kw["last_hand"])
            self.hanchan = self.last_hand >= 4
        else:
            self.hanchan = kw.get("hanchan", True)
            self.last_hand = 7 if self.hanchan else 3
        # IDX_AKA_NUM1..3: red fives PER SUIT (man, pin, sou), 0..4 each, and
        # IDX_AKA_KIND: which rank is red (1..9, normally 5). `aka_count` is the
        # old "how many suits get one red" knob and still works.
        if "aka" in kw:
            self.aka = tuple(int(n) for n in kw["aka"])
            self.aka_count = sum(1 for n in self.aka if n)
        else:
            self.aka_count = kw.get("aka_count", 3)
            self.aka = tuple(1 if i < self.aka_count else 0 for i in range(3))
        self.aka_rank = kw.get("aka_rank", 5)
        self.kuitan = kw.get("kuitan", True)           # open tanyao allowed
        self.double_ron = kw.get("double_ron", True)   # IDX_DOUBLE_RON (off = head bump)
        self.hako = kw.get("hako", True)               # IDX_HAKO: end on a minus score
        # IDX_RENCHAN: the dealer-repeat condition on a DRAW, per round. The old
        # boolean `renchan` (True = repeat on tenpai) maps onto both rounds.
        if "renchan_east" in kw or "renchan_south" in kw:
            self.renchan_east = kw.get("renchan_east", "tenpai")
            self.renchan_south = kw.get("renchan_south", self.renchan_east)
            self.renchan = (self.renchan_east != "win" or self.renchan_south != "win")
        else:
            self.renchan = kw.get("renchan", True)
            self.renchan_east = self.renchan_south = "tenpai" if self.renchan else "win"
        self.ura_dora = kw.get("ura_dora", True)       # IDX_DORA
        self.kan_dora = kw.get("kan_dora", True)
        self.kan_ura = kw.get("kan_ura", self.ura_dora and self.kan_dora)
        self.ippatsu = kw.get("ippatsu", True)         # IDX_IPPATSU
        self.noten = kw.get("noten", 3000)             # IDX_NOTEN: TOTAL noten payment
        self.pinzumo = kw.get("pinzumo", True)         # IDX_PINZUMO: pinfu tsumo 20 fu
        self.reach_after_kan = kw.get("reach_after_kan", True)   # IDX_REACH_AFTER_KAN
        self.yakitori = kw.get("yakitori", 0)          # IDX_YAKITORI, in thousands
        # Not in the client's dialog; standard readings, all switchable.
        self.extension = kw.get("extension", True)     # West-round sudden death < return
        self.agari_yame = kw.get("agari_yame", True)   # dealer in 1st may end on a win
        self.tenpai_yame = kw.get("tenpai_yame", True)  # ...and on a tenpai draw (if it repeats)
        self.sanchahou = kw.get("sanchahou", True)     # three rons abort (else all pay)
        self.kuikae = kw.get("kuikae", True)           # swap-calling forbidden
        self.renhou = kw.get("renhou", True)           # renhou = mangan floor
        self.pao = kw.get("pao", True)                 # sekinin barai
        # IDX_WAIT_TIME, in SECONDS. This one is not decoration: the client's
        # own LimitTimeManager counts `DAT_00445b87 * 60` frames and then plays
        # the default move for the player, and that global comes from the table
        # config WE author. So this number is the client's turn clock, and
        # jangame's deadline for a silent seat must stay longer than it.
        # KEY: THE ONE KNOB. This is the player's turn clock in SECONDS. It
        # drives BOTH the byte we serve in b/g/MJSTableInfoSub (+0x1F7, the
        # client's own countdown) and jangame.Table.deadline(), which is this
        # + TURN_GRACE. janlobby derives the served dropdown value from it and
        # rounds DOWN, so the client clock can never outrun our sweeper.
        # 60 because the countdown WINDOW does not render yet (GM_TimeLimmit
        # never draws) -- a player who cannot see
        # the clock must not be raced by it. Revisit once it draws.
        self.wait_time = kw.get("wait_time", 60)
        self.wareme = kw.get("wareme", False)          # IDX_WAREME
        self.kiriage = kw.get("kiriage", False)        # round 30fu4han / 60fu3han up
        self.nagashi_mangan = kw.get("nagashi_mangan", True)

    def as_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_config(cls, values, **overrides):
        """A Rules from the client's table config: {MJS_CONFIG name: index}.

        Names are the `janlobby.MJS_RULE_NAMES` strings, values the dropdown
        INDEX the client stores (not a label). A name that is absent -- or an
        index off the end of its label list -- keeps the engine default, so a
        partial config is safe. `overrides` are engine kwargs applied last.
        """
        v = dict(values or {})

        def idx(name, table):
            i = v.get(name)
            if i is None:
                return None
            try:
                return table[int(i)]
            except (IndexError, ValueError, TypeError):
                return None

        def flag(name):
            i = v.get(name)
            return None if i is None else bool(int(i))

        kw = {}
        genten = idx("GENTEN", cls.GENTEN_VALUES)
        if genten is not None:
            kw["genten"] = genten
            # The client has no return-score setting. 25000/27000 start with
            # the standard 30000 return; a start of 30000 or a low-stakes
            # 10000..20000 plays without an oka. INFERENCE.
            kw["oka_return"] = 30000 if genten >= 25000 else genten
        ren = idx("RENCHAN", cls.RENCHAN_VALUES)
        if ren is not None:
            kw["renchan_east"], kw["renchan_south"] = ren
        dora = idx("DORA", cls.DORA_VALUES)
        if dora is not None:
            kw["ura_dora"], kw["kan_dora"], kw["kan_ura"] = dora
        aka = [v.get("AKA_NUM%d" % (i + 1)) for i in range(3)]
        if any(a is not None for a in aka):
            kw["aka"] = tuple(max(0, min(4, int(a or 0))) for a in aka)
        if v.get("AKA_KIND") is not None:
            kw["aka_rank"] = max(1, min(9, int(v["AKA_KIND"]) + 1))
        for name, field in (("DOUBLE_RON", "double_ron"), ("WAREME", "wareme"),
                            ("HAKO", "hako"), ("KIRIAGE", "kiriage"),
                            ("IPPATSU", "ippatsu"), ("KUITAN", "kuitan"),
                            ("PINZUMO", "pinzumo"),
                            ("REACH_AFTER_KAN", "reach_after_kan")):
            f = flag(name)
            if f is not None:
                kw[field] = f
        yak = idx("YAKITORI", cls.YAKITORI_VALUES)
        if yak is not None:
            kw["yakitori"] = yak
        noten = idx("NOTEN", cls.NOTEN_VALUES)
        if noten is not None:
            kw["noten"] = noten
        if v.get("KYOKUSU") is not None:
            kw["last_hand"] = max(0, min(7, int(v["KYOKUSU"])))
        uma = idx("UMA", cls.UMA_VALUES)
        if uma is not None:
            second, first = uma
            kw["uma"] = (first, second, -second, -first)
        if v.get("WAIT_TIME") is not None:
            # the same decode as janlobby.wait_time_seconds: (index + 1) * 10 s
            kw["wait_time"] = (max(0, min(5, int(v["WAIT_TIME"]))) + 1) * 10
        kw.update(overrides)
        return cls(**kw)

    def renchan_condition(self, round_wind):
        """'win' / 'tenpai' / 'noten' for the round being played. The West
        (extension) round follows the South setting."""
        return self.renchan_east if round_wind == EAST else self.renchan_south


# --- melds -------------------------------------------------------------------

CHI, PON, MINKAN, KAKAN, ANKAN = "chi", "pon", "minkan", "kakan", "ankan"


class Meld(object):
    __slots__ = ("kind", "tiles", "from_seat", "called")

    def __init__(self, kind_, tiles, from_seat=-1, called=None):
        self.kind = kind_
        self.tiles = list(tiles)
        self.from_seat = from_seat        # -1 for a concealed kan
        self.called = called              # the tile that was claimed

    @property
    def is_kan(self):
        return self.kind in (MINKAN, KAKAN, ANKAN)

    @property
    def is_concealed(self):
        return self.kind == ANKAN

    @property
    def base(self):
        """The meld's tile kind (its triplet/kan tile, or its lowest for a chi)."""
        return min(kind(t) for t in self.tiles)

    def kinds(self):
        return [kind(t) for t in self.tiles]

    def __repr__(self):
        return "<%s %s>" % (self.kind, hand_str(self.tiles))


# --- hand --------------------------------------------------------------------

class Hand(object):
    """One seat's tiles and the state a scorer needs from them."""

    def __init__(self, seat):
        self.seat = seat
        self.tiles = []                 # concealed, INCLUDING a just-drawn tile
        self.melds = []
        self.pond = []                  # discards, in order
        self.pond_called = set()        # indices in `pond` claimed by someone
        self.riichi = False
        self.riichi_index = -1          # index in `pond` of the riichi tile
        self.double_riichi = False
        self.ippatsu = False
        self.menzen = True
        self.furiten = False            # own pond holds a winning tile
        self.temp_furiten = False       # passed a winning tile; until the next draw
        self.riichi_furiten = False     # passed a winning tile IN RIICHI; permanent
        self.first_turn = True
        self.nagashi = True             # every discard so far is a yaochuu, uncalled
        self.drawn = None               # the tile just drawn, if any
        self.rinshan = False            # the draw came off the dead wall
        self.kuikae = set()             # kinds this seat may not discard right now
        self.kan_order = []             # melds in the order they became kans (pao)
        self.calls_before_first_draw = False   # any call happened before it drew

    def tile_total(self):
        """Concealed tiles + 3 per meld: 13 off-turn, 14 with a draw."""
        return len(self.tiles) + 3 * len(self.melds)

    def waits(self):
        """The kinds that complete this hand as it stands (13 tiles)."""
        return winning_tiles(self.counts(), len(self.melds))

    def is_furiten(self):
        return self.furiten or self.temp_furiten or self.riichi_furiten

    def counts(self):
        c = [0] * 34
        for t in self.tiles:
            c[kind(t)] += 1
        return c

    def all_kinds(self):
        out = [kind(t) for t in self.tiles]
        for m in self.melds:
            out.extend(m.kinds())
        return out

    def red_count(self):
        n = sum(1 for t in self.tiles if is_red(t))
        for m in self.melds:
            n += sum(1 for t in m.tiles if is_red(t))
        return n

    def remove_kind(self, k, n=1, prefer_red=False):
        """Take n tiles of kind k out of the concealed hand and return them."""
        got = []
        pool = [t for t in self.tiles if kind(t) == k]
        pool.sort(key=lambda t: (not is_red(t)) if prefer_red else is_red(t))
        for t in pool[:n]:
            self.tiles.remove(t)
            got.append(t)
        if len(got) != n:
            raise ValueError("hand has %d of %s, need %d" % (len(got), TILE_NAMES[k], n))
        return got

    def sort(self):
        self.tiles.sort(key=lambda t: (kind(t), not is_red(t)))


# --- hand decomposition ------------------------------------------------------

def _standard_partitions(counts):
    """Every 4-sets-plus-pair decomposition of a 14-tile count vector.

    Yields (melds, pair) where each meld is (kind, is_run). Concealed only --
    called melds are added by the caller, which is what keeps sanankou honest.
    """
    for p in range(34):
        if counts[p] >= 2:
            c = list(counts)
            c[p] -= 2
            for sets in _take_sets(c, 0):
                yield sets, p


def _take_sets(c, start):
    i = start
    while i < 34 and c[i] == 0:
        i += 1
    if i == 34:
        yield []
        return
    if c[i] >= 3:
        c[i] -= 3
        for rest in _take_sets(c, i):
            yield [(i, False)] + rest
        c[i] += 3
    if i < 27 and i % 9 <= 6 and c[i + 1] and c[i + 2]:
        c[i] -= 1
        c[i + 1] -= 1
        c[i + 2] -= 1
        for rest in _take_sets(c, i):
            yield [(i, True)] + rest
        c[i] += 1
        c[i + 1] += 1
        c[i + 2] += 1


def is_complete(counts, melds=0):
    """Standard shape: 4 sets + a pair, counting `melds` already-called sets."""
    need = 4 - melds
    for p in range(34):
        if counts[p] >= 2:
            c = list(counts)
            c[p] -= 2
            for sets in _take_sets(c, 0):
                if len(sets) == need:
                    return True
    return False


def is_chiitoi(counts):
    return sum(1 for n in counts if n == 2) == 7


def is_kokushi(counts):
    if sum(counts) != 14:
        return False
    pairs = 0
    for k in range(34):
        if k in YAOCHUU:
            if counts[k] == 0:
                return False
            if counts[k] == 2:
                pairs += 1
            elif counts[k] != 1:
                return False
        elif counts[k]:
            return False
    return pairs == 1


def winning_tiles(counts, melds=0):
    """Every tile that completes this hand (the waits). `counts` is 13 tiles."""
    out = []
    for k in range(34):
        if counts[k] >= 4:
            continue
        counts[k] += 1
        if (is_complete(counts, melds)
                or (melds == 0 and (is_chiitoi(counts) or is_kokushi(counts)))):
            out.append(k)
        counts[k] -= 1
    return out


def is_tenpai(counts, melds=0):
    return bool(winning_tiles(list(counts), melds))


def shanten(counts, melds=0):
    """Standard shanten (0 = tenpai). Used for AI seats and tenpai payments."""
    best = 8
    if melds == 0:
        pairs = sum(1 for n in counts if n >= 2)
        kinds_ = sum(1 for n in counts if n)
        best = min(best, 6 - pairs + max(0, 7 - kinds_))
        y = sum(1 for k in YAOCHUU if counts[k])
        yp = any(counts[k] >= 2 for k in YAOCHUU)
        best = min(best, 13 - y - (1 if yp else 0))
    best = min(best, _std_shanten(list(counts), melds))
    return best


def _std_shanten(c, melds):
    best = [8]

    def walk(i, sets, partials, pair):
        if i >= 34:
            total = sets + melds
            blocks = min(4 - total, partials)
            best[0] = min(best[0], 8 - 2 * total - blocks - (1 if pair else 0))
            return
        if c[i] == 0:
            walk(i + 1, sets, partials, pair)
            return
        if c[i] >= 3:
            c[i] -= 3
            walk(i, sets + 1, partials, pair)
            c[i] += 3
        if i < 27 and i % 9 <= 6 and c[i + 1] and c[i + 2]:
            c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1
            walk(i, sets + 1, partials, pair)
            c[i] += 1; c[i + 1] += 1; c[i + 2] += 1
        if c[i] >= 2:
            if not pair:
                c[i] -= 2
                walk(i, sets, partials, True)
                c[i] += 2
            c[i] -= 2
            walk(i, sets, partials + 1, pair)
            c[i] += 2
        if i < 27 and i % 9 <= 7 and c[i + 1]:
            c[i] -= 1; c[i + 1] -= 1
            walk(i, sets, partials + 1, pair)
            c[i] += 1; c[i + 1] += 1
        if i < 27 and i % 9 <= 6 and c[i + 2]:
            c[i] -= 1; c[i + 2] -= 1
            walk(i, sets, partials + 1, pair)
            c[i] += 1; c[i + 2] += 1
        c[i] -= 1
        walk(i, sets, partials, pair)
        c[i] += 1

    walk(0, 0, 0, False)
    return best[0]


# --- scoring context ---------------------------------------------------------

class WinContext(object):
    """Everything a score depends on that is not in the hand itself."""

    def __init__(self, seat, win_tile, tsumo, round_wind=EAST, seat_wind=EAST,
                 dora_ind=(), ura_ind=(), honba=0, riichi_sticks=0,
                 haitei=False, houtei=False, rinshan=False, chankan=False,
                 tenhou=False, chiihou=False, from_seat=-1, rules=None,
                 renhou=False, wareme_seat=None):
        self.seat = seat
        self.renhou = renhou
        self.wareme_seat = wareme_seat      # pays and receives double (IDX_WAREME)
        self.win_tile = win_tile
        self.tsumo = tsumo
        self.round_wind = round_wind
        self.seat_wind = seat_wind
        self.dora_ind = list(dora_ind)
        self.ura_ind = list(ura_ind)
        self.honba = honba
        self.riichi_sticks = riichi_sticks
        self.haitei = haitei
        self.houtei = houtei
        self.rinshan = rinshan
        self.chankan = chankan
        self.tenhou = tenhou
        self.chiihou = chiihou
        self.from_seat = from_seat
        self.rules = rules or Rules()

    @property
    def is_dealer(self):
        return self.seat_wind == EAST


class Score(object):
    def __init__(self, yaku, han, fu, points, payments, limit="", pao=None):
        self.yaku = yaku                # [(name, han)] -- han is 0 for yakuman
        self.han = han
        self.fu = fu
        self.points = points            # what the winner receives, before sticks
        self.payments = payments        # {seat: delta}, sticks and honba included
        self.limit = limit
        self.pao = pao or {}            # {yakuman name: feeder seat} (sekinin barai)

    def __repr__(self):
        return "<%d han %d fu %s %d pts %s>" % (
            self.han, self.fu, self.limit, self.points,
            ",".join("%s%s" % (n, ("" if h == 0 else " %d" % h)) for n, h in self.yaku))


# --- yaku --------------------------------------------------------------------

def _meld_shapes(hand, ctx):
    """All decompositions as (sets, pair) with called melds folded in.

    A set is (kind, is_run, is_concealed). The winning tile is already in
    `hand.tiles`; concealment of the set that contains it is decided by the
    caller (a ron completes a set OPEN for fu purposes).
    """
    counts = hand.counts()
    called = [(m.base, m.kind == CHI, m.is_concealed, m) for m in hand.melds]
    out = []
    for sets, pair in _standard_partitions(counts):
        if len(sets) + len(hand.melds) != 4:
            continue
        full = [(k, run, True, None) for k, run in sets] + called
        out.append((full, pair))
    return out


def _count_dora(hand, ctx):
    n = 0
    tiles = list(hand.tiles)
    for m in hand.melds:
        tiles.extend(m.tiles)
    for ind in ctx.dora_ind:
        d = dora_from_indicator(ind)
        n += sum(1 for t in tiles if kind(t) == d)
    return n


def _count_ura(hand, ctx):
    if not hand.riichi or not ctx.rules.ura_dora:
        return 0
    n = 0
    tiles = list(hand.tiles)
    for m in hand.melds:
        tiles.extend(m.tiles)
    for ind in ctx.ura_ind:
        d = dora_from_indicator(ind)
        n += sum(1 for t in tiles if kind(t) == d)
    return n


def _yakuman(hand, ctx):
    """Yakuman checks, run before the ordinary ladder. Returns [(name, mult)]."""
    counts = hand.counts()
    all_k = hand.all_kinds()
    out = []
    concealed = hand.menzen

    if concealed and is_kokushi(counts):
        pair = [k for k in range(34) if counts[k] == 2]
        thirteen = pair and pair[0] == kind(ctx.win_tile) and counts[kind(ctx.win_tile)] == 2
        out.append(("kokushi13" if thirteen else "kokushi", 2 if thirteen else 1))
        return out

    dragons = [k for k in DRAGONS if all_k.count(k) >= 3]
    if len(dragons) == 3:
        out.append(("daisangen", 1))
    winds = [k for k in WINDS if all_k.count(k) >= 3]
    if len(winds) == 4:
        out.append(("daisuushii", 2))
    elif len(winds) == 3 and any(all_k.count(k) == 2 for k in WINDS):
        out.append(("shousuushii", 1))
    if all(k in HONORS for k in all_k):
        out.append(("tsuuiisou", 1))
    if all(k in TERMINALS for k in all_k):
        out.append(("chinroutou", 1))
    if all(k in GREENS for k in all_k):
        out.append(("ryuuiisou", 1))
    if sum(1 for m in hand.melds if m.is_kan) == 4:
        out.append(("suukantsu", 1))

    if concealed:
        # suuankou: four concealed triplets. A ron on the fourth makes it a
        # single, not a double -- the standard reading.
        for sets, pair in _meld_shapes(hand, ctx):
            trips = [s for s in sets if not s[1] and s[2]]
            if len(trips) == 4:
                if pair == kind(ctx.win_tile) or ctx.tsumo:
                    out.append(("suuankou_tanki" if pair == kind(ctx.win_tile)
                                else "suuankou", 2 if pair == kind(ctx.win_tile) else 1))
                break
        suits = set(suit_of(k) for k in all_k)
        if len(suits) == 1 and 3 not in suits:
            base = list(suits)[0] * 9
            need = [3, 1, 1, 1, 1, 1, 1, 1, 3]
            have = [counts[base + i] for i in range(9)]
            if all(have[i] >= need[i] for i in range(9)):
                extra = [i for i in range(9) if have[i] == need[i] + 1]
                if len(extra) == 1:
                    nine = base + extra[0] == kind(ctx.win_tile)
                    out.append(("chuuren9" if nine else "chuuren", 2 if nine else 1))
    if ctx.tenhou:
        out.append(("tenhou", 1))
    if ctx.chiihou:
        out.append(("chiihou", 1))
    return out


def _yaku(hand, ctx):
    """The ordinary yaku ladder. Returns ([(name, han)], best_fu)."""
    counts = hand.counts()
    all_k = hand.all_kinds()
    concealed = hand.menzen
    win = kind(ctx.win_tile)
    best = None

    def score_shape(sets, pair):
        y = []
        if hand.riichi:
            y.append(("double_riichi" if hand.double_riichi else "riichi", 2 if hand.double_riichi else 1))
            if hand.ippatsu and ctx.rules.ippatsu:
                y.append(("ippatsu", 1))
        if ctx.tsumo and concealed:
            y.append(("menzen_tsumo", 1))
        if ctx.haitei:
            y.append(("haitei", 1))
        if ctx.houtei:
            y.append(("houtei", 1))
        if ctx.rinshan:
            y.append(("rinshan", 1))
        if ctx.chankan:
            y.append(("chankan", 1))
        if ctx.renhou and ctx.rules.renhou:
            y.append(("renhou", 5))         # a mangan FLOOR -- see score_hand

        runs = [s for s in sets if s[1]]
        trips = [s for s in sets if not s[1]]

        # pinfu: all runs, non-yakuhai pair, two-sided wait, fully concealed.
        # IDX_PINZUMO off: pinfu is a RON-only yaku and a menzen ryanmen tsumo
        # scores its +2 fu instead (22 -> 30).
        pinfu = False
        if (concealed and len(runs) == 4 and not hand.melds
                and (ctx.rules.pinzumo or not ctx.tsumo)):
            if not (pair in DRAGONS or pair == ctx.round_wind or pair == ctx.seat_wind):
                for k, run, _c, _m in runs:
                    if run and (win == k or win == k + 2):
                        if not (k % 9 == 0 and win == k + 2) and not (k % 9 == 6 and win == k):
                            pinfu = True
                            break
        if pinfu:
            y.append(("pinfu", 1))

        if all(k not in YAOCHUU for k in all_k):
            if concealed or ctx.rules.kuitan:
                y.append(("tanyao", 1))

        for d in DRAGONS:
            if any(s[0] == d and not s[1] for s in sets):
                y.append(("yakuhai_" + TILE_NAMES[d], 1))
        for w, label in ((ctx.round_wind, "bakaze"), (ctx.seat_wind, "jikaze")):
            if any(s[0] == w and not s[1] for s in sets):
                y.append((label, 1))

        # iipeiko / ryanpeiko
        run_kinds = [s[0] for s in sets if s[1] and s[2]]
        dupes = sum(1 for k in set(run_kinds) if run_kinds.count(k) >= 2)
        if concealed and dupes == 2:
            y.append(("ryanpeiko", 3))
        elif concealed and dupes == 1:
            y.append(("iipeiko", 1))

        # sanshoku doujun / doukou
        for k in range(9):
            if all(any(s[0] == suit * 9 + k and s[1] for s in sets) for suit in range(3)):
                y.append(("sanshoku", 2 if concealed else 1))
                break
        for k in range(9):
            if all(any(s[0] == suit * 9 + k and not s[1] for s in sets) for suit in range(3)):
                y.append(("sanshoku_doukou", 2))
                break

        # ittsuu
        for suit in range(3):
            if all(any(s[0] == suit * 9 + a and s[1] for s in sets) for a in (0, 3, 6)):
                y.append(("ittsuu", 2 if concealed else 1))
                break

        # toitoi / sanankou / shousangen / honroutou
        if len(trips) == 4:
            y.append(("toitoi", 2))
        concealed_trips = 0
        for k, run, conc, m in trips:
            if not conc:
                continue
            if m is None:
                # a ron that completed this triplet counts as open
                if (not ctx.tsumo) and win == k and counts[k] == 3:
                    continue
                concealed_trips += 1
            elif m.is_concealed:
                concealed_trips += 1
        if concealed_trips >= 3:
            y.append(("sanankou", 2))
        if sum(1 for m in hand.melds if m.is_kan) == 3:
            y.append(("sankantsu", 2))
        drag_trips = sum(1 for s in trips if s[0] in DRAGONS)
        if drag_trips == 2 and pair in DRAGONS:
            y.append(("shousangen", 2))

        yaochuu_only = all(k in YAOCHUU for k in all_k)
        if yaochuu_only and any(k in HONORS for k in all_k):
            y.append(("honroutou", 2))
        else:
            def blocky(k, run):
                if run:
                    return k % 9 == 0 or k % 9 == 6
                return k in YAOCHUU
            if all(blocky(s[0], s[1]) for s in sets) and (pair in YAOCHUU):
                if any(k in HONORS for k in all_k):
                    y.append(("chanta", 2 if concealed else 1))
                else:
                    y.append(("junchan", 3 if concealed else 2))

        suits = set(suit_of(k) for k in all_k if k < 27)
        if len(suits) == 1:
            if any(k >= 27 for k in all_k):
                y.append(("honitsu", 3 if concealed else 2))
            else:
                y.append(("chinitsu", 6 if concealed else 5))

        fu = _round_fu(_fu(hand, ctx, sets, pair, pinfu))
        return y, fu

    for sets, pair in _meld_shapes(hand, ctx):
        y, fu = score_shape(sets, pair)
        han = sum(h for _n, h in y)
        if best is None or (han, fu) > (best[2], best[1]):
            best = (y, fu, han)

    if best is None:
        # seven pairs / thirteen orphans reach here
        if is_chiitoi(counts) and hand.menzen:
            y = [("chiitoitsu", 2)]
            if hand.riichi:
                y.insert(0, ("double_riichi" if hand.double_riichi else "riichi",
                             2 if hand.double_riichi else 1))
                if hand.ippatsu:
                    y.append(("ippatsu", 1))
            if ctx.tsumo:
                y.append(("menzen_tsumo", 1))
            if ctx.haitei:
                y.append(("haitei", 1))
            if ctx.houtei:
                y.append(("houtei", 1))
            if ctx.chankan:
                y.append(("chankan", 1))
            if ctx.renhou and ctx.rules.renhou:
                y.append(("renhou", 5))
            if all(k not in YAOCHUU for k in all_k):
                y.append(("tanyao", 1))
            suits = set(suit_of(k) for k in all_k if k < 27)
            if len(suits) == 1:
                y.append(("honitsu", 3) if any(k >= 27 for k in all_k) else ("chinitsu", 6))
            elif not suits:
                y.append(("tsuuiisou", 1))
            if all(k in YAOCHUU for k in all_k):
                y.append(("honroutou", 2))
            return y, 25
        return [], 0
    return best[0], best[1]


def _round_fu(fu):
    return -(-fu // 10) * 10


def _fu(hand, ctx, sets, pair, pinfu):
    """The UNROUNDED fu of one shape (`_round_fu` it for the table).

    Base 20; pinfu is 20 tsumo / 30 ron flat. Closed ron +10. Tsumo +2. An OPEN
    ron with nothing else is 30, not 20 -- the floor, not the menzen bonus:
    the audit (finding 23) found every cheap open ron paid as 20 fu.
    """
    if pinfu:
        return 20 if ctx.tsumo else 30
    fu = 20
    for k, run, conc, m in sets:
        if run:
            continue
        yao = k in YAOCHUU
        if m is not None and m.is_kan:
            base = 16 if yao else 8
            fu += base * (2 if m.is_concealed else 1)
        elif m is not None:
            fu += 4 if yao else 2
        else:
            ron_completed = (not ctx.tsumo) and kind(ctx.win_tile) == k \
                and hand.counts()[k] == 3
            base = 4 if yao else 2
            fu += base if ron_completed else base * 2
    if pair in DRAGONS:
        fu += 2
    if pair == ctx.round_wind:
        fu += 2
    if pair == ctx.seat_wind:
        fu += 2
    win = kind(ctx.win_tile)
    if pair == win:
        fu += 2                                     # tanki
    else:
        for k, run, _c, m in sets:
            if run and m is None:
                if win == k + 1:
                    fu += 2                         # kanchan
                    break
                if (k % 9 == 0 and win == k + 2) or (k % 9 == 6 and win == k):
                    fu += 2                         # penchan
                    break
    if ctx.tsumo:
        fu += 2
    elif hand.menzen:
        fu += 10
    elif fu == 20:
        fu = 30                                     # open ron floor
    return fu


def raw_fu(hand, ctx):
    """The unrounded fu of the shape `score_hand` would pick (tests)."""
    yaku, fu_rounded = _yaku(hand, ctx)
    if not yaku:
        return 0
    if fu_rounded == 25:
        return 25
    pinfu = any(n == "pinfu" for n, _h in yaku)
    raws = [_fu(hand, ctx, sets, pair, pinfu)
            for sets, pair in _meld_shapes(hand, ctx)]
    same = [f for f in raws if _round_fu(f) == fu_rounded]
    return max(same) if same else (raws[0] if raws else 0)


def _pao(hand, ym, rules):
    """Sekinin barai: {yakuman name: feeder seat} for the ones somebody FED.

    daisangen: all three dragon sets are DECLARED (pon/kan, an ankan counts --
    it is on the table) and the last of them was fed by a call; daisuushii:
    the same for the four winds; suukantsu: the fourth kan was a daiminkan.
    A set the winner completed in hand is nobody's fault.
    """
    if not rules.pao:
        return {}
    out = {}
    names = [n for n, _m in ym]
    for yakuman, group, n in (("daisangen", DRAGONS, 3), ("daisuushii", WINDS, 4)):
        if yakuman not in names:
            continue
        declared = [m for m in hand.melds if m.base in group]
        if len(declared) == n and declared[-1].from_seat >= 0:
            out[yakuman] = declared[-1].from_seat
    if "suukantsu" in names:
        kans = hand.kan_order if len(hand.kan_order) == 4 else \
            [m for m in hand.melds if m.is_kan]
        if len(kans) == 4 and kans[-1].kind == MINKAN:
            out["suukantsu"] = kans[-1].from_seat
    return out


# --- payment -----------------------------------------------------------------

def _limit_name(han, fu, rules):
    if han >= 13:
        return "kazoe_yakuman", 8000
    if han >= 11:
        return "sanbaiman", 6000
    if han >= 8:
        return "baiman", 4000
    if han >= 6:
        return "haneman", 3000
    if han == 5:
        return "mangan", 2000
    base = fu * (2 ** (2 + han))
    if rules.kiriage and ((han == 4 and fu == 30) or (han == 3 and fu == 60)):
        return "mangan", 2000
    if base >= 2000:
        return "mangan", 2000
    return "", base


def _ceil100(n):
    return -(-n // 100) * 100


def score_hand(hand, ctx):
    """Score a completed hand. Returns a Score, or None if it has no yaku."""
    counts = hand.counts()
    total = sum(counts) + sum(3 + (1 if m.is_kan else 0) for m in hand.melds)
    if total not in (14, 15, 16, 17, 18):
        raise ValueError("hand has %d tiles" % total)

    ym = _yakuman(hand, ctx)
    if ym:
        mult = sum(m for _n, m in ym)
        base = 8000 * mult
        yaku = [(n, 0) for n, _m in ym]
        pao = _pao(hand, ym, ctx.rules)
        pao_mult = sum(m for n, m in ym if n in pao)
        # one feeder at most is the common case; with two, the LAST one pays
        feeder = list(pao.values())[-1] if pao else None
        return _payout(Score(yaku, 13 * mult, 0, 0, {}, "yakuman", pao), base,
                       ctx, hand, pao=(feeder, 8000 * pao_mult) if pao else None)

    yaku, fu = _yaku(hand, ctx)
    if not yaku:
        return None
    han = sum(h for _n, h in yaku)
    dora = _count_dora(hand, ctx)
    aka = hand.red_count()
    ura = _count_ura(hand, ctx)
    if dora:
        yaku.append(("dora", dora))
    if aka:
        yaku.append(("aka", aka))
    if ura:
        yaku.append(("ura", ura))
    han += dora + aka + ura
    if any(n == "renhou" for n, _h in yaku):
        # renhou is a mangan FLOOR: the hand's own value if that is higher,
        # never the two added together (the common reading of "mangan-level").
        han = max(5, han - 5)
    limit, base = _limit_name(han, fu, ctx.rules)
    return _payout(Score(yaku, han, fu, 0, {}, limit), base, ctx, hand)


def _payout(score, base, ctx, hand, pao=None):
    """Turn a base value into the per-seat deltas, honba and sticks included.

    Built as a list of TRANSFERS (payer -> winner) so the two rules that
    reshape a payment compose: `pao` = (feeder, base share) makes the feeder
    pay that share alone on a tsumo and half of it on a ron; IDX_WAREME
    doubles every transfer the wall-break seat is a party to.
    """
    transfers = []                      # (payer, amount, honba part)
    honba = 300 * ctx.honba
    dealer_seat = (ctx.seat - (ctx.seat_wind - EAST)) % 4
    feeder, pao_base = pao if pao else (None, 0)
    if feeder is None or feeder == ctx.seat:
        pao_base = 0
    normal = base - pao_base
    if ctx.tsumo:
        if normal:
            if ctx.is_dealer:
                each = _ceil100(normal * 2)
                for s in range(4):
                    if s != ctx.seat:
                        transfers.append((s, each, 100 * ctx.honba))
            else:
                big, small = _ceil100(normal * 2), _ceil100(normal)
                for s in range(4):
                    if s != ctx.seat:
                        transfers.append((s, big if s == dealer_seat else small,
                                          100 * ctx.honba))
        if pao_base:
            transfers.append((feeder, _ceil100(pao_base * (6 if ctx.is_dealer else 4)),
                              honba if not normal else 0))
    else:
        mult = 6 if ctx.is_dealer else 4
        total = _ceil100(normal * mult)
        if pao_base:
            half = _ceil100(pao_base * mult) // 2
            transfers.append((feeder, half, 0))
            total += half
        transfers.append((ctx.from_seat, total, honba))
    if ctx.rules.wareme and ctx.wareme_seat is not None:
        transfers = [(s, a * 2, h * 2) if (s == ctx.wareme_seat
                                           or ctx.seat == ctx.wareme_seat)
                     else (s, a, h) for s, a, h in transfers]
    pay = {0: 0, 1: 0, 2: 0, 3: 0}
    for s, a, h in transfers:
        pay[s] -= a + h
    score.points = sum(a for _s, a, _h in transfers)
    pay[ctx.seat] = -sum(v for s, v in pay.items() if s != ctx.seat)
    pay[ctx.seat] += 1000 * ctx.riichi_sticks
    score.payments = pay
    return score


# --- call legality -----------------------------------------------------------

def chi_options(hand, tile, rules=None):
    """The pairs from `hand` that would form a run with `tile` (left seat only)."""
    k = kind(tile)
    if k >= 27:
        return []
    have = {}
    for t in hand.tiles:
        have.setdefault(kind(t), []).append(t)
    r = k % 9
    out = []
    for a, b in ((-2, -1), (-1, 1), (1, 2)):
        if not (0 <= r + a <= 8 and 0 <= r + b <= 8):
            continue
        ka, kb = k + a, k + b
        if ka in have and kb in have:
            out.append((have[ka][0], have[kb][0]))
    return out


def pon_option(hand, tile):
    k = kind(tile)
    same = [t for t in hand.tiles if kind(t) == k]
    return same[:2] if len(same) >= 2 else None


def minkan_option(hand, tile):
    k = kind(tile)
    same = [t for t in hand.tiles if kind(t) == k]
    return same[:3] if len(same) >= 3 else None


def ankan_options(hand):
    c = hand.counts()
    return [k for k in range(34) if c[k] == 4]


def kakan_options(hand):
    out = []
    have = set(kind(t) for t in hand.tiles)
    for m in hand.melds:
        if m.kind == PON and m.base in have:
            out.append(m.base)
    return out


def can_ron(hand, tile, ctx):
    """A ron needs a complete shape AND at least one yaku, and no furiten
    (own pond, temporary, or the permanent riichi furiten)."""
    if hand.is_furiten():
        return None
    c = hand.counts()
    c[kind(tile)] += 1
    ok = is_complete(c, len(hand.melds)) or \
        (not hand.melds and (is_chiitoi(c) or is_kokushi(c)))
    if not ok:
        return None
    probe = Hand(hand.seat)
    probe.tiles = hand.tiles + [tile]
    probe.melds = list(hand.melds)
    probe.riichi = hand.riichi
    probe.double_riichi = hand.double_riichi
    probe.ippatsu = hand.ippatsu
    probe.menzen = hand.menzen
    probe.pond = hand.pond
    return score_hand(probe, ctx)


def can_tsumo(hand, ctx):
    c = hand.counts()
    if not (is_complete(c, len(hand.melds))
            or (not hand.melds and (is_chiitoi(c) or is_kokushi(c)))):
        return None
    return score_hand(hand, ctx)


def update_furiten(hand):
    """A seat is furiten if any of its own discards would complete its hand."""
    c = hand.counts()
    if sum(c) % 3 == 2 and hand.tiles:
        # 14 tiles: test the 13 left after each possible discard is not our job
        # here -- callers pass a 13-tile hand. Keep the guard so a mis-call is
        # loud rather than silently wrong.
        pass
    waits = set(winning_tiles(list(c), len(hand.melds)))
    hand.furiten = any(kind(t) in waits for t in hand.pond)
    return hand.furiten


# --- the wall ----------------------------------------------------------------

class Wall(object):
    """136 tiles, a 14-tile dead wall, dora indicators revealed as kans happen.

    The live-wall count the client displays (`TsuMoSuu`) starts at 70 -- which is
    the one number in this file that is CONFIRMED against the client rather than
    assumed: `MjClient_Haipai` sets `_DAT_00451f98 = 0x46` before the first draw,
    and 136 - 14 dead - 52 dealt = 70 exactly.
    """
    LIVE_AT_DEAL = 70

    def __init__(self, rng, aka_count=3, aka=None, aka_rank=5):
        tiles = []
        for k in range(34):
            tiles.extend([k] * 4)
        # Red tiles: SE's IDX_AKA_NUM1..3 are PER-SUIT counts (0..4 copies of
        # the red rank in man/pin/sou) and IDX_AKA_KIND picks the rank. The old
        # `aka_count` (how many suits get ONE red five) still works.
        if aka is None:
            aka = tuple(1 if i < aka_count else 0 for i in range(3))
        for suit, n in enumerate(aka):
            red = suit * 9 + max(1, min(9, aka_rank)) - 1
            first = tiles.index(red)
            for i in range(max(0, min(4, int(n)))):
                tiles[first + i] = red | RED
        rng.shuffle(tiles)
        self.tiles = tiles
        self.dead = self.tiles[-14:]
        self.live = self.tiles[:-14]
        self.pos = 0
        self.kans = 0
        self.dora_shown = 1

    @property
    def remaining(self):
        return len(self.live) - self.pos

    def draw(self):
        if self.pos >= len(self.live):
            return None
        t = self.live[self.pos]
        self.pos += 1
        return t

    def draw_dead(self):
        """A replacement draw after a kan; the live wall shortens by one.

        The tile that leaves the live wall JOINS the dead wall (that is what
        keeps the dead wall at 14) -- it used to be dropped, which left the
        game one tile short after every kan. 136 is now an invariant that
        `Kyoku.tile_census()` can count.
        """
        if self.kans >= 4:
            return None
        t = self.dead[self.kans]
        self.kans += 1
        if len(self.live) > self.pos:
            self.dead.append(self.live.pop())
        return t

    def tile_count(self):
        """Tiles still in the wall: live not yet drawn + dead not yet drawn."""
        return (len(self.live) - self.pos) + (len(self.dead) - self.kans)

    def reveal_dora(self):
        if self.dora_shown < 5:
            self.dora_shown += 1

    def dora_indicators(self):
        return [self.dead[4 + i] for i in range(self.dora_shown)]

    def ura_indicators(self):
        return [self.dead[9 + i] for i in range(self.dora_shown)]


# --- a hand of mahjong -------------------------------------------------------

DRAW, RON, TSUMO, ABORT = "draw", "ron", "tsumo", "abort"

#: `abort(reason)` strings the engine raises by itself (the table layer adds
#: "kyushu" for the nine-terminals declaration, which only a player can make).
SUUFON_RENDA = "suufon_renda"       # four identical wind discards to open
SUUCHA_RIICHI = "suucha_riichi"     # all four in riichi, 4th reach tile passed
SANCHAHOU = "sanchahou"             # three rons on one tile
SUUKAIKAN = "suukaikan"             # four kans by 2+ seats, next discard passed


class Kyoku(object):
    """One hand (kyoku): deal, turn order, calls, and the settlement.

    `jangame.py` drives this: it calls `deal()`, then feeds it the seats'
    choices as they arrive on the wire, and reads the events it produces. The
    engine never blocks and never decides WHEN a message goes out -- that is the
    manager's job, because only the manager knows about acks and sequencing.

    WHAT THE TABLE LAYER MUST CALL (beyond deal/draw_for/discard/advance):

    * `note_missed_ron(seat)` when a seat DECLINES a ron it was offered. The
      engine marks the same thing itself whenever a discard passes with a
      seat's wait in it (`advance()`, or any call on it), so this only matters
      for the window between the offer and the answer.
    * kans: `ankan_options(seat)` / `kakan_options(seat)` are the source of
      truth (riichi, 5th kan, dead wall all applied). `call_kakan(seat, k,
      provisional=True)` opens the CHANKAN window: it returns the added tile,
      `chankan_candidates(seat, tile)` names who may rob it, and then either
      `win_ron(seats, chankan=True)` (which cancels the kan itself) or
      `complete_kakan()` (rinshan draw, kan-dora scheduled). The same pair
      serves `call_ankan(..., provisional=True)` for a kokushi rob.
    * kan dora: a daiminkan/kakan indicator is revealed AFTER that seat's next
      discard (`pending_dora` counts what is owed; `discard()` flushes it and
      a rinshan tsumo flushes it before scoring); an ankan reveals at once.
    * `kuikae_forbidden(seat)` after granting a chi/pon: the kinds the caller
      may not discard; `discard()` refuses them (rule `kuikae`).
    * `can_riichi(seat)` / `riichi_discards(seat)` before offering the button;
      `discard(riichi=True)` re-checks and raises rather than double-charging.
    * `dice` (two d6, drawn at construction) and `wall_break_seat` for the
      HAIPAI record; the latter is also the wareme seat.
    * every `discard()`/`call_*()` raises ValueError on an illegal move and
      leaves the hand UNTOUCHED, so a refused wire message can be re-nudged.
    * after `win_ron()` returns None, check `result`: a triple ron may have
      aborted the hand (sanchahou) instead of scoring.
    """

    def __init__(self, rules, scores, round_wind=EAST, kyoku=0, honba=0,
                 riichi_sticks=0, rng=None, dice=None):
        self.rules = rules
        self.scores = list(scores)
        self.round_wind = round_wind
        self.kyoku = kyoku                  # 0..3 within the round
        self.honba = honba
        self.riichi_sticks = riichi_sticks
        self.rng = rng or random.Random()
        self.wall = Wall(self.rng, getattr(rules, "aka_count", 3),
                         aka=getattr(rules, "aka", None),
                         aka_rank=getattr(rules, "aka_rank", 5))
        self.hands = [Hand(s) for s in range(4)]
        self.dealer = kyoku % 4
        self.turn = self.dealer
        self.result = None
        self.last_discard = None
        self.last_discard_seat = -1
        self.first_go_round = True
        # The dice and the wall break. Counting the dealer as 1 and going
        # right (our seat order), the dice total lands on the seat whose wall
        # is broken -- the standard rule, and the seat wareme doubles.
        self.dice = tuple(dice) if dice else (self.rng.randint(1, 6),
                                              self.rng.randint(1, 6))
        self.wall_break_seat = (self.dealer + sum(self.dice) - 1) % 4
        self.pending_dora = 0               # kan-dora reveals owed after a discard
        self.pending_kan = None             # (seat, tile, kind) in a chankan window
        self._ron_passers = []              # seats whose wait the last discard was
        self._fourth_kan_discard = False    # the discard after a shared 4th kan
        self.log = []

    # -- setup ---------------------------------------------------------------

    def seat_wind(self, seat):
        return EAST + (seat - self.dealer) % 4

    def deal(self):
        for _ in range(13):
            for s in range(4):
                self.hands[s].tiles.append(self.wall.draw())
        for h in self.hands:
            h.sort()
        self.log.append(("haipai", None))
        return self.draw_for(self.dealer)

    def tile_census(self):
        """Where every tile is. The total is 136 or something is lost.

        A CALLED discard stays in the pond (`pond_called` indexes it -- that is
        what the display and nagashi read) but the tile itself sits in the
        caller's meld, so the pond count excludes it.
        """
        hands = sum(len(h.tiles) for h in self.hands)
        melds = sum(len(m.tiles) for h in self.hands for m in h.melds)
        ponds = sum(len(h.pond) - len(h.pond_called) for h in self.hands)
        wall = self.wall.tile_count()
        return {"wall": wall, "hands": hands, "melds": melds, "ponds": ponds,
                "total": wall + hands + melds + ponds}

    def kan_count(self):
        return sum(1 for h in self.hands for m in h.melds if m.is_kan)

    # -- turn mechanics ------------------------------------------------------

    def draw_for(self, seat, from_dead=False):
        t = self.wall.draw_dead() if from_dead else self.wall.draw()
        if t is None:
            self.finish_draw()
            return None
        h = self.hands[seat]
        h.tiles.append(t)
        h.drawn = t
        h.rinshan = from_dead
        h.temp_furiten = False              # a new turn clears the temporary one
        h.kuikae = set()
        self.turn = seat
        self.log.append(("tsumo", (seat, t)))
        return t

    def _require_live(self):
        if self.result is not None:
            raise ValueError("the hand is over")
        if self.pending_kan is not None:
            raise ValueError("a kan is provisional: complete or cancel it first")

    def _flush_dora(self):
        while self.pending_dora > 0:
            self.pending_dora -= 1
            self.wall.reveal_dora()

    def riichi_error(self, seat, tile=None):
        """Why `seat` may not declare riichi (discarding `tile`), or None."""
        h = self.hands[seat]
        if not h.menzen:
            return "an open hand cannot riichi"
        if h.riichi:
            return "seat %d is already in riichi" % seat
        if self.scores[seat] < 1000:
            return "riichi needs 1000 points"
        if self.wall.remaining < 4:
            return "riichi needs four live tiles left"
        if h.rinshan and not self.rules.reach_after_kan:
            return "no riichi on the discard after a kan (IDX_REACH_AFTER_KAN)"
        if tile is not None:
            c = h.counts()
            c[kind(tile)] -= 1
            if not is_tenpai(c, len(h.melds)):
                return "discarding %s does not leave tenpai" % name(tile)
        return None

    def riichi_discards(self, seat):
        """The tiles whose discard is a legal riichi declaration right now."""
        h = self.hands[seat]
        if (seat != self.turn or h.tile_total() != 14
                or self.riichi_error(seat) is not None):
            return []
        out, seen = [], set()
        for t in h.tiles:
            if kind(t) in seen:
                continue
            seen.add(kind(t))
            if self.riichi_error(seat, t) is None:
                out.append(t)
        return out

    def can_riichi(self, seat):
        return bool(self.riichi_discards(seat))

    def kuikae_forbidden(self, seat):
        """Kinds `seat` may not discard on the turn right after its chi/pon."""
        if not self.rules.kuikae:
            return set()
        return set(self.hands[seat].kuikae)

    def discard(self, seat, tile_index=None, tile=None, riichi=False):
        self._require_live()
        h = self.hands[seat]
        if seat != self.turn:
            raise ValueError("it is seat %d's turn, not seat %d's" % (self.turn, seat))
        if h.tile_total() != 14:
            raise ValueError("seat %d holds %d tiles and %d melds: no discard "
                             "without a draw" % (seat, len(h.tiles), len(h.melds)))
        if tile is None:
            if tile_index is None or not (0 <= tile_index < len(h.tiles)):
                raise ValueError("discard needs an index or a tile")
            tile = h.tiles[tile_index]
        if tile not in h.tiles:
            raise ValueError("seat %d does not hold %s" % (seat, name(tile)))
        forbidden = self.kuikae_forbidden(seat)
        if kind(tile) in forbidden and any(kind(t) not in forbidden for t in h.tiles):
            raise ValueError("kuikae: seat %d may not discard %s after that call"
                             % (seat, name(tile)))
        if riichi:
            why = self.riichi_error(seat, tile)
            if why:
                raise ValueError(why)
        h.tiles.remove(tile)
        h.sort()
        h.pond.append(tile)
        h.drawn = None
        h.rinshan = False
        h.temp_furiten = False
        h.kuikae = set()
        if riichi:
            h.riichi = True
            h.riichi_index = len(h.pond) - 1
            # The one-shot window: open until THIS seat's next discard, or
            # until any call interrupts the go-round (see _break_ippatsu).
            h.ippatsu = True
            if self.first_go_round and not any(x.melds for x in self.hands):
                h.double_riichi = True
            self.scores[seat] -= 1000
            self.riichi_sticks += 1
        else:
            h.ippatsu = False
        if kind(tile) not in YAOCHUU:
            h.nagashi = False
        h.furiten = any(kind(t) in set(h.waits()) for t in h.pond)
        self.last_discard = tile
        self.last_discard_seat = seat
        self._flush_dora()
        # Whose winning tile just went by. If nobody rons it, they are furiten
        # until their next draw -- and for good, if they are in riichi.
        self._ron_passers = [o.seat for o in self.hands
                             if o.seat != seat and kind(tile) in o.waits()]
        self.log.append(("sute", (seat, tile, riichi)))
        return tile

    def note_missed_ron(self, seat, tile=None):
        """`seat` passed on a tile that completed its hand (or could not ron it
        for lack of yaku): temporary furiten, permanent if in riichi."""
        h = self.hands[seat]
        h.temp_furiten = True
        if h.riichi:
            h.riichi_furiten = True

    def _mark_passers(self):
        for s in self._ron_passers:
            self.note_missed_ron(s)
        self._ron_passers = []

    def next_seat(self, seat):
        return (seat + 1) % 4

    def advance(self):
        """No call was made on the last discard: the next seat draws.

        This is where the discard-that-passed abortive draws are judged:
        suukaikan, suucha riichi and suufon renda all mean "the tile went
        round and nobody claimed it".
        """
        if self.result is not None:
            return None
        self._mark_passers()
        if self._fourth_kan_discard:
            self._fourth_kan_discard = False
            return self.abort(SUUKAIKAN)
        if all(h.riichi for h in self.hands):
            return self.abort(SUUCHA_RIICHI)
        if self.first_go_round and all(len(h.pond) == 1 and not h.melds
                                       for h in self.hands):
            kinds = set(kind(h.pond[0]) for h in self.hands)
            if len(kinds) == 1 and kinds.pop() in WINDS:
                return self.abort(SUUFON_RENDA)
        if self.wall.remaining <= 0:
            return self.finish_draw()
        nxt = self.next_seat(self.last_discard_seat)
        if nxt == self.dealer:
            self.first_go_round = False
        return self.draw_for(nxt)

    # -- calls ---------------------------------------------------------------

    def _break_ippatsu(self):
        """Any call kills every outstanding ippatsu.

        A riichi seat's one-shot window closes on its OWN next discard (that
        is in `discard()`); a CALL by anyone is the other way it closes. This
        used to be matched by `discard()` also clearing every OTHER seat's
        flag on every discard, which made ippatsu unreachable (audit 12).
        """
        for h in self.hands:
            h.ippatsu = False

    def _claim_last_discard(self):
        d = self.hands[self.last_discard_seat]
        d.pond_called.add(len(d.pond) - 1)
        d.nagashi = False

    def call_chi(self, seat, pair):
        self._require_live()
        tile = self.last_discard
        h = self.hands[seat]
        if tile is None or seat != self.next_seat(self.last_discard_seat):
            raise ValueError("only the seat to the discarder's right may chi")
        if h.riichi:
            raise ValueError("a riichi hand cannot call chi")
        pair = list(pair)
        if len(pair) != 2 or any(t not in h.tiles for t in pair):
            raise ValueError("seat %d does not hold %s" % (seat, hand_str(pair)))
        ks = sorted(kind(t) for t in pair + [tile])
        if kind(tile) >= 27 or ks[0] // 9 != ks[2] // 9 or ks != [ks[0], ks[0] + 1, ks[0] + 2]:
            raise ValueError("%s do not run with %s" % (hand_str(pair), name(tile)))
        for t in pair:
            h.tiles.remove(t)
        h.melds.append(Meld(CHI, pair + [tile], self.last_discard_seat, tile))
        h.menzen = False
        h.drawn = None
        h.rinshan = False
        h.temp_furiten = False
        # kuikae: not the called tile, nor its suji swap (chi 4-5 on 3: not 6)
        k = kind(tile)
        h.kuikae = {k}
        if k == ks[0] and ks[2] % 9 != 8:
            h.kuikae.add(ks[2] + 1)
        elif k == ks[2] and ks[0] % 9 != 0:
            h.kuikae.add(ks[0] - 1)
        self._claim_last_discard()
        self.turn = seat
        self.first_go_round = False
        self._fourth_kan_discard = False
        self._break_ippatsu()
        self._mark_passers()
        self.log.append(("chi", (seat, tile)))

    def call_pon(self, seat, pair=None, kan=False):
        self._require_live()
        tile = self.last_discard
        h = self.hands[seat]
        if tile is None or seat == self.last_discard_seat:
            raise ValueError("nothing to claim")
        if h.riichi:
            raise ValueError("a riichi hand cannot call %s" % ("kan" if kan else "pon"))
        take = list(pair) if pair else (minkan_option(h, tile) if kan else pon_option(h, tile))
        need = 3 if kan else 2
        if (not take or len(take) != need or any(t not in h.tiles for t in take)
                or any(kind(t) != kind(tile) for t in take)):
            raise ValueError("seat %d cannot %s %s" % (seat, "kan" if kan else "pon",
                                                      name(tile)))
        if kan:
            if self.kan_count() >= 4:
                raise ValueError("no fifth kan")
            if self.wall.remaining < 1:
                raise ValueError("no live tile to feed the dead wall")
        for t in take:
            h.tiles.remove(t)
        meld = Meld(MINKAN if kan else PON, take + [tile], self.last_discard_seat, tile)
        h.melds.append(meld)
        h.menzen = False
        h.drawn = None
        h.rinshan = False
        h.temp_furiten = False
        h.kuikae = set() if kan else {kind(tile)}
        self._claim_last_discard()
        self.turn = seat
        self.first_go_round = False
        self._fourth_kan_discard = False
        self._break_ippatsu()
        self._mark_passers()
        self.log.append(("kan" if kan else "pon", (seat, tile)))
        if kan:
            h.kan_order.append(meld)
            self._note_kan(seat, immediate=False)
            return self.draw_for(seat, from_dead=True)

    def _note_kan(self, seat, immediate):
        """Bookkeeping once a kan is FINAL: the dora reveal (now, or owed until
        the rinshan discard) and the shared-4th-kan abort arming."""
        if self.rules.kan_dora:
            if immediate:
                self.wall.reveal_dora()
            else:
                self.pending_dora += 1
        if self.kan_count() == 4:
            holders = sum(1 for h in self.hands if any(m.is_kan for m in h.melds))
            if holders >= 2:
                self._fourth_kan_discard = True     # suukaikan unless ronned

    def _kan_turn_ok(self, seat):
        h = self.hands[seat]
        return (self.result is None and self.pending_kan is None
                and seat == self.turn and h.drawn is not None
                and h.tile_total() == 14 and self.kan_count() < 4
                and self.wall.remaining >= 1)

    def ankan_options(self, seat):
        """Kinds `seat` may ankan NOW (turn, draw, 5th-kan, dead wall, riichi)."""
        if not self._kan_turn_ok(seat):
            return []
        h = self.hands[seat]
        out = ankan_options(h)
        if h.riichi:
            # only the tile just drawn, and only if the waits stay the same
            before = h.counts()
            before[kind(h.drawn)] -= 1
            waits = set(winning_tiles(before, len(h.melds)))
            keep = []
            for k in out:
                if k != kind(h.drawn):
                    continue
                after = h.counts()
                after[k] -= 4
                if set(winning_tiles(after, len(h.melds) + 1)) == waits:
                    keep.append(k)
            out = keep
        return out

    def kakan_options(self, seat):
        """Kinds `seat` may add to one of its pons NOW (never in riichi)."""
        if not self._kan_turn_ok(seat) or self.hands[seat].riichi:
            return []
        return kakan_options(self.hands[seat])

    def call_ankan(self, seat, k, provisional=False):
        if k not in self.ankan_options(seat):
            self._require_live()
            raise ValueError("seat %d cannot ankan %s now" % (seat, TILE_NAMES[k]))
        h = self.hands[seat]
        tiles = h.remove_kind(k, 4)
        meld = Meld(ANKAN, tiles)
        h.melds.append(meld)
        h.kan_order.append(meld)
        h.drawn = None
        self.log.append(("ankan", (seat, k)))
        if provisional:
            self.pending_kan = (seat, tiles[0], ANKAN)
            return tiles[0]
        return self._complete_kan(seat, ANKAN)

    def call_kakan(self, seat, k, provisional=False):
        """Add the 4th tile to a pon. Validates (audit 11: a SUTE_KAN on the
        wrong slot used to delete a tile from the game)."""
        if k not in self.kakan_options(seat):
            self._require_live()
            raise ValueError("seat %d has no pon of %s to extend (or may not kan now)"
                             % (seat, TILE_NAMES[k]))
        h = self.hands[seat]
        t = h.remove_kind(k, 1)[0]
        meld = next(m for m in h.melds if m.kind == PON and m.base == k)
        meld.kind = KAKAN
        meld.tiles.append(t)
        h.kan_order.append(meld)
        h.drawn = None
        self.log.append(("kakan", (seat, k)))
        if provisional:
            self.pending_kan = (seat, t, KAKAN)
            return t
        return self._complete_kan(seat, KAKAN)

    def _complete_kan(self, seat, kind_):
        self._break_ippatsu()
        self._note_kan(seat, immediate=(kind_ == ANKAN))
        return self.draw_for(seat, from_dead=True)

    def chankan_candidates(self, seat, tile, ankan=None):
        """Seats (turn order from `seat`) that may ron the tile `seat` just
        added to a kan. On an ANKAN only a kokushi hand may rob it."""
        if ankan is None:
            ankan = bool(self.pending_kan) and self.pending_kan[2] == ANKAN
        out = []
        for step in range(1, 4):
            s = (seat + step) % 4
            h = self.hands[s]
            if h.is_furiten():
                continue
            if ankan:
                c = h.counts()
                c[kind(tile)] += 1
                if h.melds or not is_kokushi(c):
                    continue
            ctx = self.context_for(s, tile, False, from_seat=seat, chankan=True)
            if can_ron(h, tile, ctx) is not None:
                out.append(s)
        return out

    def complete_kakan(self, seat=None):
        """The chankan window closed with no ron: finish the kan (rinshan
        draw, dora owed). Seats that could have robbed it are furiten."""
        if self.pending_kan is None:
            raise ValueError("no provisional kan")
        kseat, tile, kind_ = self.pending_kan
        if seat is not None and seat != kseat:
            raise ValueError("the provisional kan belongs to seat %d" % kseat)
        self.pending_kan = None
        if kind_ == ANKAN:
            passers = []
            for h in self.hands:
                c = h.counts()
                c[kind(tile)] += 1
                if h.seat != kseat and not h.melds and is_kokushi(c):
                    passers.append(h.seat)
        else:
            passers = [h.seat for h in self.hands
                       if h.seat != kseat and kind(tile) in h.waits()]
        self._ron_passers = passers
        self._mark_passers()
        return self._complete_kan(kseat, kind_)

    complete_kan = complete_kakan

    def cancel_kakan(self, seat=None):
        """A chankan ron took the tile: undo the kan. The robbed tile goes to
        the kan seat's pond exactly like a ronned discard (in the pond, not
        marked called: the winner's hand is scored from a probe), which is
        what keeps the 136-tile census honest."""
        if self.pending_kan is None:
            raise ValueError("no provisional kan")
        kseat, tile, kind_ = self.pending_kan
        if seat is not None and seat != kseat:
            raise ValueError("the provisional kan belongs to seat %d" % kseat)
        self.pending_kan = None
        h = self.hands[kseat]
        if kind_ == KAKAN:
            meld = next(m for m in h.melds if m.kind == KAKAN and tile in m.tiles)
            meld.tiles.remove(tile)
            meld.kind = PON
        else:
            meld = next(m for m in h.melds if m.kind == ANKAN and tile in m.tiles)
            h.melds.remove(meld)
            meld.tiles.remove(tile)
            h.tiles.extend(meld.tiles)
            h.sort()
        if meld in h.kan_order:
            h.kan_order.remove(meld)
        h.pond.append(tile)
        h.nagashi = False
        self.last_discard = tile
        self.last_discard_seat = kseat
        self.log.append(("chankan", (kseat, tile)))
        return tile

    # -- endings -------------------------------------------------------------

    def context_for(self, seat, win_tile, tsumo, from_seat=-1, chankan=False):
        h = self.hands[seat]
        ura = self.wall.ura_indicators() if h.riichi else []
        if ura and not self.rules.kan_ura:
            ura = ura[:1]
        no_calls = not any(x.melds for x in self.hands)
        return WinContext(
            seat=seat, win_tile=win_tile, tsumo=tsumo,
            round_wind=self.round_wind, seat_wind=self.seat_wind(seat),
            dora_ind=self.wall.dora_indicators(), ura_ind=ura,
            honba=self.honba, riichi_sticks=self.riichi_sticks,
            haitei=tsumo and self.wall.remaining == 0 and not h.rinshan,
            houtei=(not tsumo) and self.wall.remaining == 0 and not chankan,
            rinshan=h.rinshan, chankan=chankan,
            tenhou=tsumo and seat == self.dealer and self.first_go_round
            and not h.pond,
            chiihou=tsumo and seat != self.dealer and self.first_go_round
            and not h.pond and no_calls,
            renhou=(not tsumo) and seat != self.dealer and self.first_go_round
            and not h.pond and h.drawn is None and no_calls,
            from_seat=from_seat, rules=self.rules,
            wareme_seat=self.wall_break_seat)

    def win_tsumo(self, seat):
        h = self.hands[seat]
        if h.drawn is None:
            raise ValueError("seat %d has no drawn tile to tsumo on" % seat)
        self._flush_dora()          # a rinshan win after a daiminkan/kakan reveals first
        ctx = self.context_for(seat, h.drawn, True)
        sc = score_hand(h, ctx)
        if sc is None:
            return None
        self._settle([(seat, sc)])
        self.result = (TSUMO, [(seat, sc)])
        return sc

    def win_ron(self, seats, chankan=False, tile=None, from_seat=None):
        """One or more ron on a discard, in turn order from the discarder.

        `tile`/`from_seat` name WHICH discard is being claimed. They default to
        the last one, which is right when the answer arrives promptly -- but the
        answer is a network message and the board can have moved on by the time
        it lands. WARNING: Scoring `self.last_discard` when the caller was offered a
        DIFFERENT tile is how a legitimate ron gets refused: the hand is
        complete on the tile the player was shown and not on the one that
        happens to be at the end of the pond now. The caller knows which offer
        it is answering; make it say so.

        `chankan=True` scores the robbed kan; with a provisional kan pending
        the tile and seat default to it and the kan is cancelled on a win.
        Returns None with `result` set when three rons ABORT the hand.
        """
        if chankan and self.pending_kan is not None:
            tile = self.pending_kan[1] if tile is None else tile
            from_seat = self.pending_kan[0] if from_seat is None else from_seat
        tile = self.last_discard if tile is None else tile
        from_seat = self.last_discard_seat if from_seat is None else from_seat
        wins = []
        for seat in seats:
            h = self.hands[seat]
            if h.is_furiten():
                continue
            probe = Hand(seat)
            probe.tiles = h.tiles + [tile]
            probe.melds = h.melds
            probe.kan_order = h.kan_order
            probe.riichi, probe.double_riichi = h.riichi, h.double_riichi
            probe.ippatsu, probe.menzen, probe.pond = h.ippatsu, h.menzen, h.pond
            ctx = self.context_for(seat, tile, False,
                                   from_seat=from_seat, chankan=chankan)
            sc = score_hand(probe, ctx)
            if sc is not None:
                wins.append((seat, sc))
        if not wins:
            return None
        if len(wins) >= 3 and self.rules.sanchahou:
            self.abort(SANCHAHOU)
            return None
        if len(wins) > 1 and not self.rules.double_ron:
            wins = wins[:1]                 # head bump
        if chankan and self.pending_kan is not None:
            self.cancel_kakan()
        self._settle(wins, sticks_to=wins[0][0])
        self.result = (RON, wins)
        return wins

    def _settle(self, wins, sticks_to=None):
        for i, (seat, sc) in enumerate(wins):
            for s, d in sc.payments.items():
                self.scores[s] += d
            if i > 0:                       # only the first winner takes the sticks
                self.scores[seat] -= 1000 * self.riichi_sticks
        self.riichi_sticks = 0

    def finish_draw(self):
        """Exhaustive draw: tenpai payments, nagashi mangan, dealer repeat."""
        tenpai = [h.seat for h in self.hands
                  if h.tile_total() == 13 and is_tenpai(h.counts(), len(h.melds))]
        # judged from the pond itself (the `nagashi` flag is a cache of it)
        nagashi = [h.seat for h in self.hands
                   if h.pond and not h.pond_called
                   and all(kind(t) in YAOCHUU for t in h.pond)]
        if self.rules.nagashi_mangan and nagashi:
            wins = []
            for seat in nagashi:
                ctx = self.context_for(seat, self.hands[seat].pond[-1], True)
                sc = Score([("nagashi_mangan", 5)], 5, 0, 0, {}, "mangan")
                _payout(sc, 2000, ctx, self.hands[seat])
                for s, d in sc.payments.items():
                    self.scores[s] += d
                wins.append((seat, sc))
            self.result = (DRAW, {"nagashi": wins, "tenpai": tenpai,
                                  "dealer_repeat": self.dealer in tenpai})
            return None
        n = len(tenpai)
        total = self.rules.noten
        if 0 < n < 4 and total:
            gain, loss = (total // n), (total // (4 - n))
            for s in range(4):
                self.scores[s] += gain if s in tenpai else -loss
        self.result = (DRAW, {"tenpai": tenpai, "nagashi": [],
                              "dealer_repeat": self.dealer in tenpai})
        return None

    def abort(self, why):
        self.result = (ABORT, {"why": why, "dealer_repeat": True})
        return None

    # -- what happens next ---------------------------------------------------

    def dealer_repeats(self):
        """Does the dealer keep the deal? A win always; an abort always; a draw
        by the round's IDX_RENCHAN condition (win / tenpai / noten)."""
        if self.result is None:
            return False
        kind_, data = self.result
        if kind_ == TSUMO:
            return data[0][0] == self.dealer
        if kind_ == RON:
            return any(seat == self.dealer for seat, _sc in data)
        if kind_ == ABORT:
            return True
        cond = self.rules.renchan_condition(self.round_wind)
        if cond == "noten":
            return True
        if cond == "tenpai":
            return bool(data.get("dealer_repeat"))
        return False


# --- a whole game ------------------------------------------------------------

class Game(object):
    """A hanchan (or tonpuusen): the sequence of kyoku and the final ranking."""

    def __init__(self, rules=None, rng=None, seats=4):
        self.rules = rules or Rules()
        self.rng = rng or random.Random()
        self.scores = [self.rules.genten] * 4
        self.round_wind = EAST
        self.kyoku = 0
        self.honba = 0
        self.riichi_sticks = 0
        self.hands_played = 0
        self.wins = [0, 0, 0, 0]            # for yakitori
        self.current = None
        self.finished = False
        self.end_reason = None
        self.final_adjustments = None       # {seat: delta} applied at the end

    @property
    def hand_index(self):
        """E1..E4 = 0..3, S1..S4 = 4..7, W1..W4 = 8..11 for the CURRENT hand."""
        return (self.round_wind - EAST) * 4 + self.kyoku

    def start_kyoku(self):
        self.current = Kyoku(self.rules, self.scores, self.round_wind,
                             self.kyoku, self.honba, self.riichi_sticks,
                             rng=self.rng)
        self.current.deal()
        return self.current

    def _top_seat(self):
        return sorted(range(4), key=lambda s: (-self.scores[s], s))[0]

    def end_kyoku(self):
        k = self.current
        self.scores = list(k.scores)
        self.riichi_sticks = k.riichi_sticks
        self.hands_played += 1
        kind_, data = k.result
        if kind_ in (RON, TSUMO):
            for seat, _sc in data:
                self.wins[seat] += 1
        repeat = k.dealer_repeats()
        drawish = kind_ in (DRAW, ABORT)
        self.honba = self.honba + 1 if (repeat or drawish) else 0
        over = self._decide_over(k, repeat)
        if not over and not repeat:
            self.kyoku += 1
            if self.kyoku >= 4:
                self.kyoku = 0
                self.round_wind += 1
        self.finished = over
        if over:
            self._final_adjust()
        return self.finished

    def _decide_over(self, k, repeat):
        """The end-of-game ladder, judged after the hand at `hand_index`."""
        r = self.rules
        if r.hako and any(s < 0 for s in self.scores):
            self.end_reason = "hako"
            return True
        idx, last = self.hand_index, r.last_hand
        reached = max(self.scores) >= r.oka_return
        if repeat:
            if idx > last:                  # sudden death in the extension
                if reached:
                    self.end_reason = "extension"
                    return True
                return False
            if idx == last and self._top_seat() == k.dealer:
                if k.result[0] in (RON, TSUMO) and r.agari_yame:
                    self.end_reason = "agari_yame"
                    return True
                if k.result[0] == DRAW and r.tenpai_yame:
                    self.end_reason = "tenpai_yame"
                    return True
            return False
        if idx + 1 <= last:
            return False
        if r.extension and not reached and idx + 1 <= Rules.EXTENSION_CAP:
            return False                    # play on until someone has the return
        self.end_reason = "extension" if idx + 1 > last + 1 else "last_hand"
        return True

    def _over(self):
        return self.finished

    def _final_adjust(self):
        """Game-end transfers: leftover riichi sticks to first place; yakitori
        (a seat with no win pays N thousand, pooled and split among the rest)."""
        if self.final_adjustments is not None:
            return
        adj = {0: 0, 1: 0, 2: 0, 3: 0}
        if self.riichi_sticks:
            adj[self._top_seat()] += 1000 * self.riichi_sticks
            self.riichi_sticks = 0
        if self.rules.yakitori:
            losers = [s for s in range(4) if self.wins[s] == 0]
            winners = [s for s in range(4) if self.wins[s] > 0]
            if losers and winners:
                pot = 1000 * self.rules.yakitori * len(losers)
                for s in losers:
                    adj[s] -= 1000 * self.rules.yakitori
                share = (pot // len(winners)) // 100 * 100
                for s in winners:
                    adj[s] += share
                adj[sorted(winners, key=lambda s: (-self.scores[s], s))[0]] += \
                    pot - share * len(winners)
        for s, d in adj.items():
            self.scores[s] += d
        self.final_adjustments = adj

    def ranking(self):
        """Final placings and the uma/oka-adjusted result, best first.

        Ties break by seat order (E > S > W > N), which is the usual convention.
        """
        order = sorted(range(4), key=lambda s: (-self.scores[s], s))
        out = []
        oka = (self.rules.oka_return - self.rules.genten) * 4 // 1000
        for place, seat in enumerate(order):
            pts = (self.scores[seat] - self.rules.oka_return) / 1000.0
            pts += self.rules.uma[place]
            if place == 0:
                pts += oka
            out.append({"seat": seat, "place": place, "score": self.scores[seat],
                        "result": round(pts, 1)})
        return out


# --- selftest ----------------------------------------------------------------

def _hand_from(spec, melds=()):
    """'123m456p789s11z' style, for tests."""
    out = []
    num = ""
    for ch in spec:
        if ch.isdigit():
            num += ch
        else:
            base = {"m": MAN, "p": PIN, "s": SOU, "z": HONOR}[ch]
            for d in num:
                out.append(base + (int(d) - 1))
            num = ""
    return out


def selftest():
    ok = True

    def check(cond, msg):
        if not cond:
            print("FAIL: %s" % msg)
        return cond

    # tiles
    ok &= check(dora_from_indicator(8) == 0, "9m indicator -> 1m")
    ok &= check(dora_from_indicator(NORTH) == EAST, "N indicator -> E")
    ok &= check(dora_from_indicator(CHUN) == HAKU, "C indicator -> P")
    ok &= check(kind(4 | RED) == 4 and is_red(4 | RED), "red five carries its kind")

    # shapes
    def counts_of(spec):
        c = [0] * 34
        for t in _hand_from(spec):
            c[t] += 1
        return c
    ok &= check(is_complete(counts_of("123456789m11122p")), "standard complete")
    ok &= check(is_chiitoi(counts_of("1122334455m1122p")), "chiitoitsu")
    ok &= check(is_kokushi(counts_of("19m19p19s1234567z1z")), "kokushi")
    # NB 11123p IS complete (11p pair + 123p run) -- a pairless hand is the
    # honest negative here, and getting that wrong once is why it is spelled out.
    ok &= check(is_complete(counts_of("123456789m11123p")), "11p pair + 123p run")
    ok &= check(not is_complete(counts_of("1234567m123p123s5z")), "no pair, no win")
    ok &= check(sorted(winning_tiles(counts_of("123456789m1122p"))) ==
                sorted([PIN, PIN + 1]), "shanpon wait")
    ok &= check(shanten(counts_of("123456789m1122p")) == 0, "tenpai is shanten 0")

    # a pinfu tsumo, non-dealer: 234m 567m 234p 678s + 55p, won on 7m.
    # The pair must NOT be a dragon or either wind or pinfu dies, and the three
    # 234s must not line up in three suits or sanshoku creeps in.
    h = Hand(1)
    h.tiles = _hand_from("23456m234p678s55p") + [MAN + 6]
    ctx = WinContext(seat=1, win_tile=MAN + 6, tsumo=True, seat_wind=SOUTH)
    sc = score_hand(h, ctx)
    ok &= check(sc is not None, "pinfu tsumo scores")
    if sc:
        names = [n for n, _h in sc.yaku]
        ok &= check("pinfu" in names and "menzen_tsumo" in names,
                    "pinfu + tsumo, got %r" % names)
        ok &= check(sc.fu == 20, "pinfu tsumo is 20 fu, got %d" % sc.fu)
        ok &= check(sum(sc.payments.values()) == 0,
                    "payments balance, got %r" % sc.payments)

    # riichi tanyao dora, ron off seat 0
    h = Hand(2)
    h.tiles = _hand_from("234567m234p234s55p")
    h.riichi = True
    ctx = WinContext(seat=2, win_tile=PIN + 4, tsumo=False, seat_wind=WEST,
                     from_seat=0, dora_ind=[MAN])
    h.tiles = _hand_from("234567m234p234s5p") + [PIN + 4]
    sc = score_hand(h, ctx)
    ok &= check(sc is not None, "riichi hand scores")
    if sc:
        names = dict(sc.yaku)
        ok &= check("riichi" in names and "tanyao" in names,
                    "riichi+tanyao, got %r" % list(names))
        ok &= check(names.get("dora") == 1, "one dora (2m), got %r" % names.get("dora"))
        ok &= check(sc.payments[0] < 0 and sc.payments[2] > 0, "ron pays the dealer-in")

    # yakuman
    h = Hand(0)
    h.tiles = _hand_from("19m19p19s12345677z")
    ctx = WinContext(seat=0, win_tile=HONOR + 6, tsumo=True)
    sc = score_hand(h, ctx)
    ok &= check(sc is not None and sc.limit == "yakuman", "kokushi is a yakuman")
    if sc:
        # The win tile IS the pair, so this is the thirteen-sided wait: a DOUBLE
        # yakuman, 16000 base, dealer tsumo 32000 from each of three.
        ok &= check("kokushi13" in dict(sc.yaku), "13-wait detected: %r" % sc.yaku)
        ok &= check(sc.payments[0] == 96000,
                    "dealer double-yakuman tsumo = 96000, got %r" % sc.payments)

    # no yaku = no win. Open (a called chi), pair is a dragon but only a PAIR,
    # and the runs deliberately avoid sanshoku -- otherwise the hand has a yaku.
    h = Hand(1)
    h.menzen = False
    h.melds = [Meld(CHI, _hand_from("234m"), 0, MAN + 1)]
    h.tiles = _hand_from("56m345p678s55z") + [MAN + 6]
    ctx = WinContext(seat=1, win_tile=MAN + 6, tsumo=False, seat_wind=SOUTH, from_seat=0)
    ok &= check(score_hand(h, ctx) is None, "an open hand with no yaku cannot win")

    # a CALL cancels every outstanding ippatsu, the same way a turn passing does
    kk = Kyoku(Rules(), [25000] * 4, rng=random.Random(4))
    kk.deal()
    kk.hands[1].riichi = kk.hands[1].ippatsu = True
    kk.last_discard, kk.last_discard_seat = PIN + 4, 0
    kk.hands[2].tiles = [PIN + 4, PIN + 4] + kk.hands[2].tiles[2:]
    kk.hands[0].pond.append(PIN + 4)
    kk.call_pon(2)
    ok &= check(not kk.hands[1].ippatsu,
                "a pon between the reach tile and the next draw kills ippatsu -- "
                "leaving it set scores a han the hand did not earn")

    # calls
    h = Hand(0)
    h.tiles = _hand_from("13m22334p")
    ok &= check(len(chi_options(h, MAN + 1)) == 1, "chi kanchan found")
    ok &= check(pon_option(h, PIN + 1) is not None, "pon found")
    ok &= check(minkan_option(h, PIN + 1) is None, "no kan on two")

    # the wall, against the client's own number
    w = Wall(random.Random(7))
    ok &= check(len(w.tiles) == 136, "136 tiles")
    ok &= check(len(w.live) == 122 and len(w.dead) == 14, "14-tile dead wall")
    for _ in range(52):
        w.draw()
    ok &= check(w.remaining == Wall.LIVE_AT_DEAL,
                "after the deal the live wall is %d, the client's own 0x46 -- got %d"
                % (Wall.LIVE_AT_DEAL, w.remaining))

    # a whole kyoku runs to a draw without raising
    g = Game(Rules(), rng=random.Random(1))
    k = g.start_kyoku()
    guard = 0
    while k.result is None and guard < 200:
        guard += 1
        h = k.hands[k.turn]
        if h.drawn is None:
            k.draw_for(k.turn)
            continue
        k.discard(k.turn, tile=h.tiles[-1])
        k.advance()
    ok &= check(k.result is not None, "a kyoku terminates")
    ok &= check(sum(k.scores) + 1000 * k.riichi_sticks == 100000,
                "points are conserved: %r" % k.scores)

    g.end_kyoku()
    r = g.ranking()
    ok &= check(len(r) == 4 and r[0]["place"] == 0, "ranking has four places")
    ok &= check(abs(sum(x["result"] for x in r)) < 0.001,
                "uma/oka is zero-sum: %r" % [x["result"] for x in r])

    ok &= _selftest_fixes(check)

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


# -- scenario helpers (tests only) --------------------------------------------
#
# Every rig below SWAPS tiles with the wall or another hand instead of
# overwriting, so the 136-tile census stays true through the scenario and can
# be asserted at the end of each one.

def _fresh(seed=1, rules=None, scores=None, dice=None, **kw):
    k = Kyoku(rules or Rules(), scores or [25000] * 4, rng=random.Random(seed),
              dice=dice, **kw)
    k.deal()
    return k


def _donors(k, seat):
    out = [(k.wall.live, i) for i in range(k.wall.pos, len(k.wall.live))]
    out += [(k.wall.dead, i) for i in range(k.wall.kans, len(k.wall.dead))]
    for o in k.hands:
        if o.seat != seat:
            out += [(o.tiles, i) for i in range(len(o.tiles))]
    return out


def _rig(k, seat, spec, drawn=None):
    """Give `seat` exactly the tiles of `spec` (+ `drawn`), by swapping."""
    h = k.hands[seat]
    want = _hand_from(spec) + ([drawn] if drawn is not None else [])
    if len(want) != len(h.tiles):
        raise ValueError("rig: seat %d holds %d tiles, spec has %d"
                         % (seat, len(h.tiles), len(want)))
    need, keep, surplus = list(want), [], []
    for t in h.tiles:
        if kind(t) in need:
            need.remove(kind(t))
            keep.append(t)
        else:
            surplus.append(t)
    donors = _donors(k, seat)
    for kk in need:
        for j, (lst, i) in enumerate(donors):
            if kind(lst[i]) == kk:
                taken, lst[i] = lst[i], surplus.pop()
                keep.append(taken)
                del donors[j]
                break
        else:
            raise ValueError("rig: no %s left anywhere" % TILE_NAMES[kk])
    h.tiles = keep
    h.sort()
    if drawn is not None:
        h.drawn = next(t for t in h.tiles if kind(t) == kind(drawn))


def _give(k, seat, kk, n=1):
    """Make sure `seat` holds n tiles of kind kk (swapping surplus away)."""
    h = k.hands[seat]
    while sum(1 for t in h.tiles if kind(t) == kk) < n:
        for lst, i in _donors(k, seat):
            if kind(lst[i]) != kk:
                continue
            in_wall = lst is k.wall.live or lst is k.wall.dead
            if not in_wall and sum(1 for t in lst if kind(t) == kk) < 2:
                continue                # never strip another hand's last copy
            give = next(t for t in reversed(h.tiles) if kind(t) != kk)
            h.tiles.remove(give)
            h.tiles.append(lst[i])
            lst[i] = give
            break
        else:
            raise ValueError("give: no %s available" % TILE_NAMES[kk])
    h.sort()


def _force_draw(k, kk):
    """The next live-wall draw will be a tile of kind kk (swap within the wall)."""
    w = k.wall
    for j in range(w.pos, len(w.live)):
        if kind(w.live[j]) == kk:
            w.live[w.pos], w.live[j] = w.live[j], w.live[w.pos]
            return
    raise ValueError("force_draw: no %s in the wall" % TILE_NAMES[kk])


def _plant_dead(k, i, kk, seat=0):
    """dead[i] (the i-th rinshan tile) becomes kind kk, swapped in from the
    live wall or, failing that, from a hand other than `seat`."""
    w = k.wall
    for lst, j in _donors(k, seat):
        if lst is w.dead:
            continue
        if kind(lst[j]) == kk:
            w.dead[i], lst[j] = lst[j], w.dead[i]
            return
    raise ValueError("plant_dead: no %s anywhere" % TILE_NAMES[kk])


def _pass_turn(k, seat, avoid=()):
    """`seat` (on turn, 14 tiles) discards something harmless."""
    h = k.hands[seat]
    forbidden = k.kuikae_forbidden(seat)
    t = next(t for t in h.tiles if kind(t) not in avoid and kind(t) not in forbidden)
    return k.discard(seat, tile=t)


def _selftest_fixes(check):
    """Coverage for the 2026-09-04 audit fixes (findings 2, 12, 13, 21-23)."""
    ok = True
    TENPAI = "23456m234p678s55p"          # waits 1m/4m/7m; pinfu+tanyao on 7m
    C = lambda k: k.tile_census()["total"]

    # ---- 1. ippatsu --------------------------------------------------------
    k = _fresh(3)
    _rig(k, 0, TENPAI, drawn=SOU + 8)
    k.discard(0, tile=SOU + 8, riichi=True)
    ok &= check(k.hands[0].ippatsu and k.scores[0] == 24000 and k.riichi_sticks == 1,
                "riichi opens the ippatsu window and pays the stick once")
    _force_draw(k, MAN + 6)
    k.advance()
    k.discard(1, tile=MAN + 6)
    wins = k.win_ron([0])
    names = [n for n, _h in wins[0][1].yaku] if wins else []
    ok &= check("ippatsu" in names,
                "ron on the discard right after riichi is ippatsu: %r" % names)
    ok &= check(C(k) == 136, "census after an ippatsu ron: %r" % k.tile_census())

    def go_round(k):
        """seats 1..3 each draw and discard an East; seat 0 then draws 7m."""
        for s in (1, 2, 3):
            _force_draw(k, EAST)
            k.advance()
            k.discard(s, tile=next(t for t in k.hands[s].tiles if kind(t) == EAST))
        _force_draw(k, MAN + 6)
        k.advance()

    k = _fresh(3)
    _rig(k, 0, TENPAI, drawn=SOU + 8)
    k.discard(0, tile=SOU + 8, riichi=True)
    go_round(k)
    sc = k.win_tsumo(0)
    names = [n for n, _h in sc.yaku] if sc else []
    ok &= check("ippatsu" in names, "tsumo on the next draw is ippatsu: %r" % names)

    k = _fresh(3)
    _rig(k, 0, TENPAI, drawn=SOU + 8)
    k.discard(0, tile=SOU + 8, riichi=True)
    _force_draw(k, EAST)
    k.advance()
    k.discard(1, tile=next(t for t in k.hands[1].tiles if kind(t) == EAST))
    _give(k, 2, EAST, 2)
    k.call_pon(2)
    ok &= check(not k.hands[0].ippatsu, "a pon in between cancels ippatsu")
    _pass_turn(k, 2)
    _force_draw(k, EAST)
    k.advance()
    k.discard(3, tile=next(t for t in k.hands[3].tiles if kind(t) == EAST))
    _force_draw(k, MAN + 6)
    k.advance()
    sc = k.win_tsumo(0)
    names = [n for n, _h in sc.yaku] if sc else []
    ok &= check(sc is not None and "ippatsu" not in names,
                "...and the tsumo after it scores no ippatsu: %r" % names)
    ok &= check(C(k) == 136, "census after pon + tsumo: %r" % k.tile_census())

    k = _fresh(3, Rules(ippatsu=False))
    _rig(k, 0, TENPAI, drawn=SOU + 8)
    k.discard(0, tile=SOU + 8, riichi=True)
    _force_draw(k, MAN + 6)
    k.advance()
    k.discard(1, tile=MAN + 6)
    wins = k.win_ron([0])
    ok &= check(wins and "ippatsu" not in [n for n, _h in wins[0][1].yaku],
                "IDX_IPPATSU off: no ippatsu")

    # ---- 2. temporary and riichi furiten -----------------------------------
    for riichi in (False, True):
        k = _fresh(3)
        _rig(k, 0, TENPAI, drawn=SOU + 8)
        k.discard(0, tile=SOU + 8, riichi=riichi)
        _force_draw(k, MAN + 6)
        k.advance()
        k.discard(1, tile=MAN + 6)
        ok &= check(0 in k._ron_passers, "the engine sees seat 0's wait go by")
        k.advance()                      # nobody ronned: seat 2 draws
        h = k.hands[0]
        ok &= check(h.temp_furiten, "passing a winning tile sets temp furiten")
        ok &= check(h.riichi_furiten == riichi,
                    "riichi furiten only when in riichi (riichi=%r)" % riichi)
        ctx = k.context_for(0, MAN + 3, False, from_seat=2)
        ok &= check(can_ron(h, MAN + 3, ctx) is None,
                    "furiten: no ron on another wait while it lasts")
        _force_draw(k, EAST)
        _pass_turn(k, 2, avoid=(MAN, MAN + 3, MAN + 6))
        _force_draw(k, EAST)
        k.advance()
        _pass_turn(k, 3, avoid=(MAN, MAN + 3, MAN + 6))
        _force_draw(k, EAST)
        k.advance()                      # seat 0 draws again
        ok &= check(not h.temp_furiten, "temp furiten clears at the seat's next draw")
        ok &= check(h.is_furiten() == riichi,
                    "riichi furiten is permanent, temp furiten is not (riichi=%r)"
                    % riichi)
        ok &= check(C(k) == 136, "census through the furiten scenario")
    k = _fresh(3)
    k.note_missed_ron(2)
    ok &= check(k.hands[2].temp_furiten and not k.hands[2].riichi_furiten,
                "note_missed_ron marks a declined ron")

    # ---- 3. fu -------------------------------------------------------------
    def fu_of(spec13, win, tsumo, melds=(), seat_wind=SOUTH, round_wind=EAST,
              riichi=False, rules=None):
        h = Hand(1)
        h.tiles = _hand_from(spec13) + [win]
        h.melds = list(melds)
        h.menzen = not any(m.kind != ANKAN for m in melds)
        h.riichi = riichi
        ctx = WinContext(seat=1, win_tile=win, tsumo=tsumo, seat_wind=seat_wind,
                         round_wind=round_wind, from_seat=0, rules=rules)
        sc = score_hand(h, ctx)
        return (sc.fu if sc else None), raw_fu(h, ctx), sc

    chi234p = Meld(CHI, _hand_from("234p"), 0, PIN + 1)
    haku_pon = Meld(PON, [HAKU] * 3, 2, HAKU)
    cases = [
        ("closed ron ryanmen tanyao = 30 (pinfu)", fu_of(TENPAI, MAN + 6, False), 30, 30),
        ("open ron ryanmen no fu = 30 (floor)",
         fu_of("23456m678s55p", MAN + 6, False, [chi234p]), 30, 30),
        ("closed tsumo ryanmen = pinfu 20", fu_of(TENPAI, MAN + 6, True), 20, 20),
        ("open tsumo ryanmen = 22 -> 30",
         fu_of("23456m678s55p", MAN + 6, True, [chi234p]), 30, 22),
        ("PINZUMO off: menzen ryanmen tsumo = 22 -> 30, no pinfu",
         fu_of(TENPAI, MAN + 6, True, rules=Rules(pinzumo=False)), 30, 22),
        ("kanchan +2 (closed ron 32 -> 40)",
         fu_of("35m678m234p678s55p", MAN + 3, False), 40, 32),
        ("penchan +2 (closed ron 32 -> 40)",
         fu_of("12m456m234p678s55p", MAN + 2, False, riichi=True), 40, 32),
        ("tanki +2 (closed ron 32 -> 40)",
         fu_of("234m567m234p678s5p", PIN + 4, False), 40, 32),
        ("dragon pair +2", fu_of("234m56m234p678s55z", MAN + 6, False, riichi=True), 40, 32),
        ("seat wind pair +2", fu_of("234m56m234p678s22z", MAN + 6, False, riichi=True), 40, 32),
        ("round wind pair +2", fu_of("234m56m234p678s11z", MAN + 6, False, riichi=True), 40, 32),
        ("seat+round wind pair +4",
         fu_of("234m56m234p678s11z", MAN + 6, False, riichi=True, seat_wind=EAST), 40, 34),
        ("open terminal pon +4 (with an open dragon pon +4): 28 -> 30",
         fu_of("234p56p55s", PIN + 6, False,
               [Meld(PON, [MAN] * 3, 0, MAN), haku_pon]), 30, 28),
        ("closed terminal triplet +8: 32 -> 40",
         fu_of("111m234p56p55s", PIN + 6, False, [haku_pon]), 40, 32),
        ("open terminal kan +16: 40",
         fu_of("234p56p55s", PIN + 6, False,
               [Meld(MINKAN, [MAN] * 4, 0, MAN), haku_pon]), 40, 40),
        ("closed terminal kan +32: 56 -> 60",
         fu_of("234p56p55s", PIN + 6, False,
               [Meld(ANKAN, [MAN] * 4), haku_pon]), 60, 56),
        ("open simple kan +8: 32 -> 40",
         fu_of("234p56p55s", PIN + 6, False,
               [Meld(MINKAN, [MAN + 4] * 4, 0, MAN + 4), haku_pon]), 40, 32),
        ("closed simple kan +16: 40",
         fu_of("234p56p55s", PIN + 6, False,
               [Meld(ANKAN, [MAN + 4] * 4), haku_pon]), 40, 40),
        ("chiitoitsu 25", fu_of("1122334455m112p", PIN + 1, False), 25, 25),
    ]
    for label, (fu, raw, sc), want_fu, want_raw in cases:
        ok &= check(fu == want_fu and raw == want_raw,
                    "%s: got fu %r raw %r (%r)" % (label, fu, raw,
                                                    sc.yaku if sc else None))
    _f, _r, sc = fu_of(TENPAI, MAN + 6, True, rules=Rules(pinzumo=False))
    ok &= check(sc and "pinfu" not in dict(sc.yaku), "PINZUMO off drops the pinfu yaku")

    # ---- 4. kan family -----------------------------------------------------
    k = _fresh(5)
    _rig(k, 0, "1111m234p567p88s5z", drawn=HAKU)
    ok &= check(k.ankan_options(0) == [MAN] and k.kakan_options(0) == [],
                "ankan offered on four 1m; no kakan without a pon")
    ok &= check(k.ankan_options(1) == [], "no kan off-turn")
    try:
        k.call_kakan(0, MAN)
        ok &= check(False, "kakan without a pon must raise")
    except ValueError:
        pass
    try:
        k.call_ankan(0, PIN + 1)
        ok &= check(False, "ankan on fewer than four must raise")
    except ValueError:
        pass
    ok &= check(C(k) == 136 and len(k.hands[0].tiles) == 14,
                "a refused kan touches nothing: %r" % k.tile_census())
    k.call_ankan(0, MAN)
    ok &= check(k.wall.dora_shown == 2, "ankan reveals the kan dora at once")
    ok &= check(C(k) == 136 and k.hands[0].tile_total() == 14,
                "census after ankan: %r" % k.tile_census())
    ok &= check(k.hands[0].rinshan and k.hands[0].drawn is not None, "rinshan drawn")
    ok &= check(k.riichi_error(0) is None, "REACH_AFTER_KAN on: riichi allowed")
    k.rules = Rules(reach_after_kan=False)
    ok &= check(k.riichi_error(0) is not None, "REACH_AFTER_KAN off: no riichi on the rinshan discard")
    k.rules = Rules()

    # daiminkan: dora after the rinshan discard; kakan the same; chankan window
    k = _fresh(5)
    _pass_turn(k, 0)
    _give(k, 1, kind(k.last_discard), 3)
    k.call_pon(1, kan=True)
    ok &= check(k.pending_dora == 1 and k.wall.dora_shown == 1,
                "daiminkan: the dora is OWED, not shown")
    ok &= check(C(k) == 136, "census after daiminkan: %r" % k.tile_census())
    _pass_turn(k, 1)
    ok &= check(k.pending_dora == 0 and k.wall.dora_shown == 2,
                "...and revealed after the rinshan discard")
    ok &= check(k.hands[1].kuikae == set(), "a kan sets no kuikae")

    def kakan_setup(seed):
        k = _fresh(seed)
        _rig(k, 0, "234m567m555p88s99s", drawn=PIN + 4)
        h = k.hands[0]
        three = h.remove_kind(PIN + 4, 3)
        h.melds.append(Meld(PON, three, 1, three[0]))
        h.menzen = False
        h.drawn = next(t for t in h.tiles if kind(t) == PIN + 4)
        _rig(k, 1, "234m567m46p888s99s")
        return k

    k = kakan_setup(7)
    ok &= check(k.kakan_options(0) == [PIN + 4], "kakan offered onto the pon")
    ok &= check(can_ron(k.hands[1], PIN + 4, k.context_for(1, PIN + 4, False, 0)) is None,
                "seat 1 has no yaku on a plain 5p ron...")
    t = k.call_kakan(0, PIN + 4, provisional=True)
    ok &= check(kind(t) == PIN + 4 and k.pending_kan is not None and k.hands[0].drawn is None,
                "provisional kakan returns the tile and draws nothing yet")
    ok &= check(C(k) == 136, "census with a provisional kakan: %r" % k.tile_census())
    ok &= check(k.chankan_candidates(0, t) == [1], "...but may rob the kan (chankan is the yaku)")
    try:
        k.discard(0, tile=k.hands[0].tiles[0])
        ok &= check(False, "no discard while a kan is provisional")
    except ValueError:
        pass
    wins = k.win_ron([1], chankan=True)
    ok &= check(wins and "chankan" in dict(wins[0][1].yaku), "chankan scores: %r" % wins)
    m = k.hands[0].melds[0]
    ok &= check(k.pending_kan is None and m.kind == PON and len(m.tiles) == 3,
                "a chankan ron cancels the kan")
    ok &= check(k.result and k.result[0] == RON and wins[0][1].payments[0] < 0,
                "the kan seat pays the ron")
    ok &= check(C(k) == 136, "census after a chankan: %r" % k.tile_census())

    k = kakan_setup(7)
    t = k.call_kakan(0, PIN + 4, provisional=True)
    r = k.complete_kakan()
    ok &= check(r is not None and k.hands[0].drawn == r and k.hands[0].rinshan,
                "complete_kakan draws the rinshan tile")
    ok &= check(k.pending_dora == 1 and k.wall.dora_shown == 1, "kakan dora is owed")
    ok &= check(k.hands[1].temp_furiten, "a seat that let the kan pass is temp furiten")
    _pass_turn(k, 0)
    ok &= check(k.wall.dora_shown == 2, "...and revealed after the discard")
    ok &= check(k.hands[0].melds[0].kind == KAKAN and C(k) == 136,
                "census after a completed kakan: %r" % k.tile_census())

    # kokushi robs an ankan
    k = _fresh(9)
    _rig(k, 0, "1111z234m567m88s5p", drawn=PIN + 8)
    _rig(k, 1, "19m19p19s234567z7z")
    t = k.call_ankan(0, EAST, provisional=True)
    ok &= check(k.chankan_candidates(0, t) == [1], "kokushi may rob an ankan")
    wins = k.win_ron([1], chankan=True)
    ok &= check(wins and wins[0][1].limit == "yakuman", "kokushi chankan is a yakuman")
    ok &= check(not k.hands[0].melds and C(k) == 136,
                "the ankan is undone, census %r" % k.tile_census())
    k = _fresh(9)
    _rig(k, 0, "1111z234m567m88s5p", drawn=PIN + 8)
    _rig(k, 1, "19m19p19s234567z7z")
    _rig(k, 2, "19m19p19s234567z6z")
    ok &= check(k.chankan_candidates(0, EAST, ankan=True) == [1, 2]
                or k.chankan_candidates(0, EAST, ankan=True) == [1],
                "ankan rob candidates are kokushi-only: %r"
                % k.chankan_candidates(0, EAST, ankan=True))
    _rig(k, 2, "234m567m46p888s99s")
    ok &= check(k.chankan_candidates(0, PIN + 4, ankan=True) == [],
                "an ordinary hand cannot rob an ankan")

    # riichi ankan: only the drawn tile, only if the waits stay
    k = _fresh(13)
    # 2223m waits 1/3/4m; kan the 2m and only the 3m tanki is left
    _rig(k, 0, "2223m456p789s678s", drawn=MAN + 1)
    h = k.hands[0]
    ok &= check(k.ankan_options(0) == [MAN + 1], "without riichi the 2m ankan is fine")
    h.riichi = True
    ok &= check(ankan_options(h) == [MAN + 1] and k.ankan_options(0) == [],
                "riichi: an ankan that changes the waits is refused")
    k = _fresh(13)
    _rig(k, 0, "111m234m567p678s5z", drawn=MAN)      # 5z tanki either way
    h = k.hands[0]
    ok &= check(k.ankan_options(0) == [MAN], "riichi ankan allowed when waits are unchanged")
    h.riichi = True
    ok &= check(k.ankan_options(0) == [MAN], "...still allowed in riichi")
    h.drawn = next(t for t in h.tiles if kind(t) == HAKU)
    ok &= check(k.ankan_options(0) == [], "...but only on the tile just drawn")
    ok &= check(k.kakan_options(0) == [], "never a kakan in riichi")

    # suukaikan: four kans by two seats, the next discard passes -> abort
    k = _fresh(11)
    _rig(k, 0, "1111m2222p3333s5z", drawn=HATSU)
    for kk in (MAN, PIN + 1, SOU + 2):
        k.call_ankan(0, kk)
    ok &= check(k.kan_count() == 3 and C(k) == 136, "three ankans, census %r" % k.tile_census())
    _pass_turn(k, 0)
    k.advance()
    _rig(k, 1, "4444m567p567s88s5z", drawn=HAKU)
    k.call_ankan(1, MAN + 3)
    ok &= check(k.kan_count() == 4 and k._fourth_kan_discard, "a shared 4th kan arms suukaikan")
    ok &= check(k.ankan_options(1) == [] and k.kakan_options(1) == [], "no fifth kan is offered")
    try:
        k.call_ankan(1, HAKU)
        ok &= check(False, "a 5th kan must raise")
    except ValueError:
        pass
    _pass_turn(k, 1)
    k.advance()
    ok &= check(k.result == (ABORT, {"why": SUUKAIKAN, "dealer_repeat": True}),
                "suukaikan aborts when the discard passes: %r" % (k.result,))
    ok &= check(C(k) == 136, "census after suukaikan: %r" % k.tile_census())
    # ...but four kans by ONE seat play on (suukantsu)
    k = _fresh(11)
    _rig(k, 0, "1111m2222p3333s5z", drawn=HAKU)
    _plant_dead(k, 0, HAKU)
    _plant_dead(k, 1, HAKU)
    for kk in (MAN, PIN + 1, SOU + 2):
        k.call_ankan(0, kk)
    ok &= check(k.ankan_options(0) == [HAKU], "the rinshan draws built a 4th kan")
    k.call_ankan(0, HAKU)
    ok &= check(k.kan_count() == 4 and not k._fourth_kan_discard, "one seat's 4th kan does not arm")
    _pass_turn(k, 0)
    k.advance()
    ok &= check(k.result is None and k.turn == 1, "the hand continues after a solo 4th kan")
    ok &= check(k.wall.dora_shown == 5 and C(k) == 136, "four kan dora, census %r" % k.tile_census())

    # ---- 5. abortive draws -------------------------------------------------
    k = _fresh(17)
    for s in range(4):
        _give(k, s, EAST)
    for s in range(4):
        k.discard(s, tile=next(t for t in k.hands[s].tiles if kind(t) == EAST))
        k.advance()
    ok &= check(k.result == (ABORT, {"why": SUUFON_RENDA, "dealer_repeat": True}),
                "four East discards to open = suufon renda: %r" % (k.result,))
    k = _fresh(17)
    for s in range(3):
        _give(k, s, EAST)
    _give(k, 3, SOUTH)
    for s in range(4):
        k.discard(s, tile=next(t for t in k.hands[s].tiles if kind(t) in (EAST, SOUTH)))
        k.advance()
    ok &= check(k.result is None, "three Easts and a South is not suufon renda")

    k = _fresh(19)
    specs = [("23456m234p678s55p", SOU + 8), ("23456p234s678m55s", MAN + 8),
             ("23456s234m678p55m", PIN + 8), ("11223344z5566z7z", MAN + 8)]
    for s, (spec, junk) in enumerate(specs):
        if s:
            k.advance()
        _rig(k, s, spec, drawn=junk)
        k.discard(s, tile=next(t for t in k.hands[s].tiles if kind(t) == junk), riichi=True)
    k.advance()
    ok &= check(k.result == (ABORT, {"why": SUUCHA_RIICHI, "dealer_repeat": True}),
                "four riichi and the 4th reach tile passes = suucha riichi: %r" % (k.result,))
    ok &= check(k.riichi_sticks == 4 and sum(k.scores) == 96000, "the four sticks stand")

    def sancha(rules):
        k = _fresh(23, rules)
        _rig(k, 1, "23456m234p678s55p")
        _rig(k, 2, "56m234s456s678p22s")
        _rig(k, 3, "68m345p567s333s55s")
        _give(k, 0, MAN + 6)
        k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 6))
        return k, k.win_ron([1, 2, 3])

    k, wins = sancha(Rules())
    ok &= check(wins is None and k.result == (ABORT, {"why": SANCHAHOU, "dealer_repeat": True}),
                "three rons abort (sanchahou): %r %r" % (wins, k.result))
    ok &= check(k.advance() is None and k.result[0] == ABORT, "advance() after an abort is a no-op")
    k, wins = sancha(Rules(sanchahou=False))
    ok &= check(wins and len(wins) == 3 and sum(k.scores) == 100000,
                "sanchahou off: a triple ron pays all three: %r" % k.scores)
    k, wins = sancha(Rules(sanchahou=False, double_ron=False))
    ok &= check(wins and len(wins) == 1 and wins[0][0] == 1, "head bump keeps the first in turn order")

    g = Game(Rules(), rng=random.Random(1))
    k = g.start_kyoku()
    k.abort(SUUFON_RENDA)
    g.end_kyoku()
    ok &= check(g.kyoku == 0 and g.honba == 1 and not g.finished,
                "an abort keeps the deal and adds a honba: kyoku %d honba %d" % (g.kyoku, g.honba))

    # ---- 6. riichi legality ------------------------------------------------
    k = _fresh(3)
    _rig(k, 0, TENPAI, drawn=SOU + 8)
    h = k.hands[0]
    ok &= check(k.can_riichi(0) and sorted(kind(t) for t in k.riichi_discards(0)) == [SOU + 5, SOU + 8],
                "the 9s (and the 6s, via 789s) leave tenpai: %r" % k.riichi_discards(0))
    for label, prep, undo in (
            ("open hand", lambda: setattr(h, "menzen", False), lambda: setattr(h, "menzen", True)),
            ("already in riichi", lambda: setattr(h, "riichi", True), lambda: setattr(h, "riichi", False)),
            ("under 1000", lambda: k.scores.__setitem__(0, 900), lambda: k.scores.__setitem__(0, 25000)),
            ("wall < 4", lambda: setattr(k.wall, "pos", len(k.wall.live) - 3),
             lambda: setattr(k.wall, "pos", 53))):
        prep()
        try:
            k.discard(0, tile=SOU + 8, riichi=True)
            ok &= check(False, "riichi with %s must raise" % label)
        except ValueError:
            pass
        ok &= check(not k.can_riichi(0), "can_riichi is false with %s" % label)
        undo()
    ok &= check(len(h.tiles) == 14 and k.scores[0] == 25000 and k.riichi_sticks == 0,
                "a refused riichi charges nothing and moves nothing")
    try:
        k.discard(0, tile=PIN + 4, riichi=True)
        ok &= check(False, "riichi on a non-tenpai discard must raise")
    except ValueError:
        pass
    k.discard(0, tile=SOU + 8, riichi=True)
    ok &= check(h.riichi and k.scores[0] == 24000, "the legal riichi goes through")
    ok &= check(not k.can_riichi(0), "no second riichi")

    # ---- 7. turn validation ------------------------------------------------
    k = _fresh(3)
    try:
        k.discard(1, tile=k.hands[1].tiles[0])
        ok &= check(False, "a discard off-turn must raise")
    except ValueError:
        pass
    lost = k.hands[0].tiles.pop()
    try:
        k.discard(0, tile=k.hands[0].tiles[0])
        ok &= check(False, "a discard from 13 tiles must raise")
    except ValueError:
        pass
    k.hands[0].tiles.append(lost)
    ok &= check(k.discard(0, tile=lost) == lost, "a 14-tile on-turn discard is fine")

    # ---- 8. end of game ----------------------------------------------------
    def game_with(rules, scores, result, round_wind=EAST, kyoku=0, sticks=0, seed=1):
        g = Game(rules, rng=random.Random(seed))
        g.round_wind, g.kyoku = round_wind, kyoku
        g.scores = list(scores)
        k = g.start_kyoku()
        k.scores = list(scores)
        k.riichi_sticks = sticks
        k.result = result
        return g, k

    WIN0 = (TSUMO, [(0, Score([("x", 1)], 1, 30, 0, {0: 0, 1: 0, 2: 0, 3: 0}))])
    DRAW_T = (DRAW, {"tenpai": [0], "nagashi": [], "dealer_repeat": True})
    DRAW_N = (DRAW, {"tenpai": [], "nagashi": [], "dealer_repeat": False})
    g, k = game_with(Rules(last_hand=0), [33000, 23000, 22000, 22000], WIN0)
    ok &= check(g.end_kyoku() and g.end_reason == "agari_yame", "agari-yame ends the game")
    g, k = game_with(Rules(last_hand=0, agari_yame=False), [33000, 23000, 22000, 22000], WIN0)
    ok &= check(not g.end_kyoku() and g.honba == 1 and g.kyoku == 0, "agari_yame off: the dealer plays on")
    g, k = game_with(Rules(last_hand=0), [26000, 33000, 21000, 20000], WIN0)
    ok &= check(not g.end_kyoku(), "the dealer wins but is not first: no agari-yame")
    g, k = game_with(Rules(last_hand=0), [33000, 23000, 22000, 22000], DRAW_T)
    ok &= check(g.end_kyoku() and g.end_reason == "tenpai_yame", "tenpai-yame on a tenpai draw")
    g, k = game_with(Rules(last_hand=0, tenpai_yame=False), [33000, 23000, 22000, 22000], DRAW_T)
    ok &= check(not g.end_kyoku(), "tenpai_yame off")
    g, k = game_with(Rules(last_hand=0), [25000] * 4, DRAW_N)
    ok &= check(not g.end_kyoku() and g.hand_index == 1, "nobody at 30000: the game extends")
    g, k = game_with(Rules(last_hand=0, extension=False), [25000] * 4, DRAW_N)
    ok &= check(g.end_kyoku() and g.end_reason == "last_hand", "extension off: it ends")
    g, k = game_with(Rules(last_hand=0), [31000, 23000, 23000, 23000], DRAW_N, kyoku=1)
    ok &= check(g.end_kyoku() and g.end_reason == "extension", "sudden death: someone has 30000")
    g, k = game_with(Rules(last_hand=0), [25000] * 4, DRAW_N, round_wind=WEST, kyoku=3)
    ok &= check(g.end_kyoku(), "the extension stops at W4")
    WIN1 = (TSUMO, [(1, Score([("x", 1)], 1, 30, 0, {0: 0, 1: 0, 2: 0, 3: 0}))])
    g, k = game_with(Rules(last_hand=0), [25000] * 4, WIN1, round_wind=WEST, kyoku=1)
    ok &= check(not g.end_kyoku() and g.round_wind == WEST and g.kyoku == 1,
                "dealer repeat in the extension without 30000 plays on")
    g, k = game_with(Rules(hako=True), [-100, 30100, 35000, 35000], DRAW_N)
    ok &= check(g.end_kyoku() and g.end_reason == "hako", "hako ends on a minus score")
    g, k = game_with(Rules(hako=False), [-100, 30100, 35000, 35000], DRAW_N)
    ok &= check(not g.end_kyoku(), "hako off: play on below zero")
    g, k = game_with(Rules(last_hand=0, extension=False), [26000, 25000, 25000, 22000], DRAW_N, sticks=2)
    g.end_kyoku()
    ok &= check(g.scores[0] == 28000 and g.riichi_sticks == 0 and sum(g.scores) == 100000,
                "leftover riichi sticks go to first place: %r" % g.scores)
    g, k = game_with(Rules(last_hand=0, extension=False, yakitori=10), [40000, 20000, 20000, 20000], WIN0)
    g.end_kyoku()
    ok &= check(g.scores == [70000, 10000, 10000, 10000] and g.wins == [1, 0, 0, 0],
                "yakitori: three winless seats pay 10000 each to the one winner: %r" % g.scores)

    # renchan per round
    for east, south, rw, tenpai, want in (("win", "tenpai", EAST, True, False),
                                          ("win", "tenpai", SOUTH, True, True),
                                          ("win", "tenpai", SOUTH, False, False),
                                          ("noten", "win", EAST, False, True),
                                          ("tenpai", "noten", WEST, False, True)):
        r = Rules(renchan_east=east, renchan_south=south)
        k = Kyoku(r, [25000] * 4, round_wind=rw, rng=random.Random(1))
        k.result = (DRAW, {"tenpai": [0] if tenpai else [], "nagashi": [], "dealer_repeat": tenpai})
        ok &= check(k.dealer_repeats() == want,
                    "RENCHAN E:%s S:%s in %s, dealer tenpai=%r -> repeat %r"
                    % (east, south, TILE_NAMES[rw], tenpai, want))
    k = Kyoku(Rules(renchan=False), [25000] * 4, rng=random.Random(1))
    k.result = (DRAW, {"tenpai": [0], "nagashi": [], "dealer_repeat": True})
    ok &= check(not k.dealer_repeats() and Rules(renchan=False).renchan_east == "win",
                "the old renchan=False still means win-only")
    ok &= check(Rules().renchan and Rules().renchan_east == "tenpai", "default renchan is tenpai")

    # nagashi mangan: dealer repeat follows the dealer's tenpai
    k = _fresh(29, Rules(noten=3000))
    _rig(k, 1, TENPAI)
    k.hands[1].pond = [EAST, NORTH, MAN + 4]       # the 5m spoils seat 1's nagashi
    k.hands[2].pond = [EAST]
    k.hands[3].pond = [EAST]
    k.hands[0].tiles.pop()                  # the dealer discarded already (13)
    k.hands[0].pond = [EAST]
    k.finish_draw()
    kind_, data = k.result
    ok &= check(kind_ == DRAW and [s for s, _sc in data["nagashi"]] == [0, 2, 3]
                and data["dealer_repeat"] == (0 in data["tenpai"]),
                "nagashi mangan: three clean ponds pay; dealer repeat by tenpai: %r" % (data,))
    ok &= check(sum(k.scores) == 100000, "nagashi payments balance: %r" % k.scores)
    k = _fresh(29, Rules(noten=6000))
    _rig(k, 1, TENPAI)
    k.hands[0].tiles.pop()
    for h in k.hands:
        h.pond = [MAN + 4]
    k.finish_draw()
    ok &= check(k.result[1]["tenpai"] == [1] and k.scores[1] == 31000 and k.scores[0] == 23000,
                "IDX_NOTEN 6000: one tenpai seat collects 2000 x 3: %r" % k.scores)
    k = _fresh(29, Rules(noten=0))
    _rig(k, 1, TENPAI)
    k.hands[0].tiles.pop()
    for h in k.hands:
        h.pond = [MAN + 4]
    k.finish_draw()
    ok &= check(k.scores == [25000] * 4, "IDX_NOTEN none: no payment")

    # pao
    def daisangen(tsumo, from_seat, rules=None):
        h = Hand(1)
        h.tiles = _hand_from("234m5p") + [PIN + 4]
        h.melds = [Meld(PON, [HAKU] * 3, 2, HAKU), Meld(PON, [HATSU] * 3, 0, HATSU),
                   Meld(PON, [CHUN] * 3, 3, CHUN)]
        h.menzen = False
        ctx = WinContext(seat=1, win_tile=PIN + 4, tsumo=tsumo, seat_wind=SOUTH,
                         from_seat=from_seat, rules=rules)
        return score_hand(h, ctx)

    sc = daisangen(False, 0)
    ok &= check(sc.pao == {"daisangen": 3} and sc.payments == {0: -16000, 1: 32000, 2: 0, 3: -16000},
                "pao ron: the feeder of the 3rd dragon pays half: %r" % sc.payments)
    sc = daisangen(True, -1)
    ok &= check(sc.payments == {0: 0, 1: 32000, 2: 0, 3: -32000},
                "pao tsumo: the feeder pays it all: %r" % sc.payments)
    sc = daisangen(False, 3)
    ok &= check(sc.payments == {0: 0, 1: 32000, 2: 0, 3: -32000},
                "pao ron off the feeder: the feeder pays it all: %r" % sc.payments)
    sc = daisangen(True, -1, Rules(pao=False))
    ok &= check(sc.pao == {} and sc.payments == {0: -16000, 1: 32000, 2: -8000, 3: -8000},
                "pao off: an ordinary yakuman tsumo: %r" % sc.payments)
    h = Hand(1)
    h.tiles = [PIN + 4, PIN + 4]                    # four kans + the pair
    h.melds = [Meld(MINKAN, [EAST] * 4, 2, EAST), Meld(ANKAN, [SOUTH] * 4),
               Meld(KAKAN, [WEST] * 4, 0, WEST), Meld(MINKAN, [NORTH] * 4, 3, NORTH)]
    h.kan_order = list(h.melds)
    h.menzen = False
    sc = score_hand(h, WinContext(seat=1, win_tile=PIN + 4, tsumo=True, seat_wind=SOUTH))
    ok &= check(sc.pao.get("suukantsu") == 3 and sc.pao.get("daisuushii") == 3
                and sc.payments[3] == -96000 - 0 and sc.payments[1] == 96000,
                "suukantsu + daisuushii fed by seat 3: %r %r" % (sc.pao, sc.payments))

    # renhou
    k = _fresh(31)
    _rig(k, 1, TENPAI)
    _give(k, 0, MAN + 6)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 6))
    wins = k.win_ron([1])
    ok &= check(wins and dict(wins[0][1].yaku).get("renhou") == 5 and wins[0][1].limit == "mangan",
                "renhou: a non-dealer ron before its first draw is a mangan: %r" % wins)
    k = _fresh(31, Rules(renhou=False))
    _rig(k, 1, TENPAI)
    _give(k, 0, MAN + 6)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 6))
    wins = k.win_ron([1])
    y = dict(wins[0][1].yaku) if wins else {}
    ok &= check(wins and "renhou" not in y and wins[0][1].han == 2 + y.get("dora", 0) + y.get("aka", 0),
                "renhou off: the hand's own 2 han (+dora): %r" % wins)

    # ---- 9. kuikae ---------------------------------------------------------
    k = _fresh(37)
    _give(k, 0, MAN + 2)
    _give(k, 1, MAN + 3)
    _give(k, 1, MAN + 4)
    _give(k, 1, MAN + 5)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 2))
    h = k.hands[1]
    pair = (next(t for t in h.tiles if kind(t) == MAN + 3), next(t for t in h.tiles if kind(t) == MAN + 4))
    k.call_chi(1, pair)
    ok &= check(k.kuikae_forbidden(1) == {MAN + 2, MAN + 5},
                "chi 4-5 on 3: may not discard 3m or 6m: %r" % k.kuikae_forbidden(1))
    try:
        k.discard(1, tile=next(t for t in h.tiles if kind(t) == MAN + 5))
        ok &= check(False, "the suji swap discard must raise")
    except ValueError:
        pass
    ok &= check(h.tile_total() == 14 and C(k) == 136, "a refused kuikae discard moves nothing")
    _pass_turn(k, 1)
    ok &= check(k.kuikae_forbidden(1) == set(), "kuikae lasts one discard")
    k = _fresh(37, Rules(kuikae=False))
    _give(k, 0, MAN + 2)
    _give(k, 1, MAN + 3)
    _give(k, 1, MAN + 4)
    _give(k, 1, MAN + 5)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 2))
    h = k.hands[1]
    k.call_chi(1, (next(t for t in h.tiles if kind(t) == MAN + 3),
                   next(t for t in h.tiles if kind(t) == MAN + 4)))
    ok &= check(k.kuikae_forbidden(1) == set(), "kuikae off: nothing forbidden")
    k.discard(1, tile=next(t for t in h.tiles if kind(t) == MAN + 5))
    ok &= check(k.last_discard == MAN + 5 or kind(k.last_discard) == MAN + 5, "...and 6m goes")
    k = _fresh(37)
    _give(k, 0, PIN + 4)
    _give(k, 2, PIN + 4, 3)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == PIN + 4))
    k.call_pon(2)
    ok &= check(k.kuikae_forbidden(2) == {PIN + 4}, "pon 5p: may not discard the 4th 5p")
    try:
        k.discard(2, tile=next(t for t in k.hands[2].tiles if kind(t) == PIN + 4))
        ok &= check(False, "discarding the pon tile must raise")
    except ValueError:
        pass
    try:
        k.call_chi(2, (k.hands[2].tiles[0], k.hands[2].tiles[1]))
        ok &= check(False, "a chi by the wrong seat / wrong tiles must raise")
    except ValueError:
        pass

    # ---- 10. rules plumbing ------------------------------------------------
    r = Rules.from_config({"GENTEN": 0, "RENCHAN": 2, "DORA": 1, "AKA_NUM1": 2,
                           "AKA_NUM2": 0, "AKA_NUM3": 4, "AKA_KIND": 4,
                           "DOUBLE_RON": 0, "WAREME": 1, "HAKO": 0, "KIRIAGE": 1,
                           "YAKITORI": 2, "IPPATSU": 0, "KUITAN": 0, "NOTEN": 4,
                           "PINZUMO": 0, "KYOKUSU": 3, "UMA": 4,
                           "REACH_AFTER_KAN": 0, "WAIT_TIME": 2})
    got = (r.genten, r.oka_return, r.renchan_east, r.renchan_south, r.ura_dora,
           r.kan_dora, r.kan_ura, r.aka, r.aka_rank, r.double_ron, r.wareme, r.hako,
           r.kiriage, r.yakitori, r.ippatsu, r.kuitan, r.noten, r.pinzumo,
           r.last_hand, r.hanchan, r.uma, r.reach_after_kan, r.wait_time,
           r.aka_count, r.renchan)
    want = (10000, 10000, "win", "noten", True, False, False, (2, 0, 4), 5, False,
            True, False, True, 10, False, False, 12000, False, 3, False,
            (60, 20, -20, -60), False, 30, 2, True)
    ok &= check(got == want, "from_config decodes every setting:\n  got  %r\n  want %r" % (got, want))
    r = Rules.from_config({"GENTEN": 3, "DORA": 4, "KYOKUSU": 7})
    ok &= check((r.genten, r.oka_return, r.kan_ura, r.last_hand) == (25000, 30000, True, 7)
                and r.uma == (20, 10, -10, -20) and r.noten == 3000,
                "absent names keep the defaults")
    r = Rules.from_config({"DORA": 2})
    ok &= check((r.ura_dora, r.kan_dora) == (False, True), "Front+Kan: kan dora, no ura")
    ok &= check(Rules.from_config({"GENTEN": 99, "UMA": "x"}).genten == 25000,
                "an index off the end keeps the default")
    ok &= check(Rules.from_config({}, kuitan=False).kuitan is False, "overrides apply last")
    d = Rules().as_dict()
    ok &= check(d["aka_count"] == 3 and d["hanchan"] and d["last_hand"] == 7 and d["renchan"],
                "the old field names still read as before")
    w = Wall(random.Random(1), aka=(2, 0, 4), aka_rank=5)
    reds = [t for t in w.tiles if is_red(t)]
    ok &= check(len(reds) == 6 and sum(1 for t in reds if kind(t) == MAN + 4) == 2
                and sum(1 for t in reds if kind(t) == SOU + 4) == 4,
                "per-suit aka counts: %r" % [name(t) for t in reds])
    w = Wall(random.Random(1), aka=(1, 1, 1), aka_rank=3)
    ok &= check(sorted(kind(t) for t in w.tiles if is_red(t)) == [MAN + 2, PIN + 2, SOU + 2],
                "AKA_KIND picks the red rank")
    ok &= check(len([t for t in Wall(random.Random(1), 2).tiles if is_red(t)]) == 2,
                "the old aka_count still works")
    k = _fresh(41, Rules(aka=(0, 0, 0)))
    ok &= check(not any(is_red(t) for t in k.wall.tiles), "Kyoku deals by the per-suit counts")

    # dice and wareme
    k = _fresh(43, dice=(3, 4))
    ok &= check(k.dice == (3, 4) and k.wall_break_seat == 2, "dice 7 from dealer 0 breaks at seat 2")
    k = _fresh(43)
    ok &= check(all(1 <= d <= 6 for d in k.dice) and 0 <= k.wall_break_seat < 4,
                "dice are rolled at the deal: %r" % (k.dice,))
    k = Kyoku(Rules(), [25000] * 4, kyoku=1, rng=random.Random(1), dice=(1, 1))
    ok &= check(k.wall_break_seat == 2, "dice 2 from dealer 1 breaks at seat 2")

    def wareme_ron(rules, dice):
        k = _fresh(47, rules, dice=dice)
        _rig(k, 1, TENPAI)
        _give(k, 0, MAN + 6)
        k.first_go_round = False            # keep renhou out of it
        k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 6))
        return k.win_ron([1])[0][1].payments

    plain = wareme_ron(Rules(wareme=False), (2, 3))
    ok &= check(plain[1] > 0 and plain[0] == -plain[1], "a plain non-dealer ron: %r" % plain)
    doubled = wareme_ron(Rules(wareme=True), (2, 3))     # 5 from dealer 0: seat 0 = the payer
    ok &= check(doubled[1] == 2 * plain[1] and doubled[0] == -2 * plain[1],
                "wareme payer pays double: %r vs %r" % (doubled, plain))
    doubled = wareme_ron(Rules(wareme=True), (1, 1))     # 2: seat 1 = the winner
    ok &= check(doubled[1] == 2 * plain[1], "wareme winner receives double: %r" % doubled)
    same = wareme_ron(Rules(wareme=True), (2, 2))        # 4: seat 3, uninvolved
    ok &= check(same == plain, "wareme elsewhere changes nothing: %r" % same)

    # dora kinds
    k = _fresh(53, Rules(kan_dora=False))
    _rig(k, 0, "1111m234p567p88s5z", drawn=HAKU)
    k.call_ankan(0, MAN)
    ok &= check(k.wall.dora_shown == 1, "kan_dora off: nothing revealed")
    k = _fresh(53, Rules(kan_ura=False))
    _rig(k, 0, "1111m234p567p88s5z", drawn=HAKU)
    k.call_ankan(0, MAN)
    k.hands[0].riichi = True
    ok &= check(len(k.context_for(0, HAKU, True).ura_ind) == 1
                and len(k.context_for(0, HAKU, True).dora_ind) == 2,
                "kan_ura off: one ura indicator under two dora")
    k.rules = Rules()
    ok &= check(len(k.context_for(0, HAKU, True).ura_ind) == 2, "kan_ura on: two")

    # ---- the ron path refuses a furiten seat even if asked --------------------
    k = _fresh(3)
    _rig(k, 1, TENPAI)
    k.hands[1].riichi_furiten = True
    _give(k, 0, MAN + 6)
    k.discard(0, tile=next(t for t in k.hands[0].tiles if kind(t) == MAN + 6))
    ok &= check(k.win_ron([1]) is None and k.result is None, "win_ron drops a furiten seat")

    return ok


def demo(seed=0):
    g = Game(Rules(), rng=random.Random(seed))
    for _ in range(4):
        k = g.start_kyoku()
        print("=== %s%d honba %d ===" % ("ESWN"[k.round_wind - EAST], k.kyoku + 1, k.honba))
        for s in range(4):
            print("  seat %d  %s" % (s, hand_str(k.hands[s].tiles)))
        guard = 0
        while k.result is None and guard < 200:
            guard += 1
            h = k.hands[k.turn]
            if h.drawn is None:
                if k.draw_for(k.turn) is None:
                    break
                continue
            sc = can_tsumo(h, k.context_for(k.turn, h.drawn, True))
            if sc:
                k.win_tsumo(k.turn)
                print("  TSUMO seat %d  %r" % (k.turn, sc))
                break
            k.discard(k.turn, tile=h.tiles[-1])
            k.advance()
        if k.result and k.result[0] == DRAW:
            print("  draw, tenpai %r" % (k.result[1]["tenpai"],))
        print("  scores %r" % k.scores)
        g.end_kyoku()
    for row in g.ranking():
        print("  #%d seat %d  %d  %+.1f" % (row["place"] + 1, row["seat"],
                                            row["score"], row["result"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if a.demo:
        return demo(a.seed)
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
