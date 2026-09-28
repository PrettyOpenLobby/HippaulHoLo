#!/usr/bin/env python3
"""Janhourou's PLAYER RECORD -- what a finished hanchan leaves behind.

WHY THIS EXISTS. **Janhourou never writes its own save.** `sqMgWriteFileCheck`
has **0 callers** in the whole of `JanHouRou.pex`, and `sqMgWriteFileOffset`'s
two callers are both inside `cp.c`/`mg.c` -- the library's own plumbing. No game
code path writes `U/g/MJSUserData` (see `jansave.py`). So the 976
bytes are the SERVER's to compose, exactly as Tetra Master's `U/g/TM0DataFile`
is, and a player's Level / Rank / Games Played / Money can only ever be as good
as what the server wrote down when the game ended.

This module is ONE SOURCE for every number the client shows about a player:

    the save        `jansave.FIELDS` <- `derive()`     (record screen, panel)
    the popup       `<PO>` values    <- `profile_fields()`
    the rankings    `U/g/MJS_RANKLIST<n>` <- `rank_list_blob()`  (same struct)
    the results     Level / LevelUp / Shogo / GetShogo <- `level()`, `shogo()`,
                    `getshogo()`
    the lobby row   "Lv%2d"          <- `level()`

so a level, a rank or a title cannot disagree with itself across screens
(finding 28: level was `1+games//10` on results, "Lv 1" in the row, 0 in the
popup).

    python janstats.py --selftest
    python janstats.py --show 8              # one member's record
    python janstats.py --record 8 --place 0 --score 32100 --result 45.0
    python janstats.py --ranklist 0          # dump a rank list

=== WHAT IS A FACT HERE, AND WHAT IS A POLICY =============================

The file stores **facts**: how many games, what place, what final table score,
what uma/oka result, which yaku were won, how many busts. Those come straight
out of `janmahjong.Game.ranking()` / the engine's win record and are as true
as the rules engine is.

Level / Rank / Rating / Title score are **derived** -- see the `=== POLICY`
block below; every formula there is declared as a placeholder, in one place,
and every value it produces can be pinned per member via `overrides`.

The TITLES are not policy: the record screen prints their thresholds
(ScoreDispWindow.c:303-315, read 2026-09-04 off the disassembly):

    Mahjong King      `10000 - today` JAN   -- 10,000 JAN won in one day
    King of Beasts    `100 - top % 100`     -- every 100 first places
    Winnings General  `20 - plus % 20`      -- every 20 games finished in plus
    Wild Tile King    `5 - last % 5`        -- every 5 last places
    Bust General      (not on that screen; the results panel counts it)
                                            -- a bust: finishing below zero

=== THE MONEY UNIT ========================================================

JAN = points x a per-table RATE, and the client does no arithmetic: the
results chain (`yamaguchi2__00334be0`) reads the rate from
`b/g/MJSTableInfoSub` +0x1EC aloud as "This table's rate is 1P = %d JAN" and
tweens between two server-supplied columns. Points are the uma/oka result in
thousands (the `10-20` uma notation), so `RATE` = 1000 makes JAN the raw
table points won -- a 40-point hanchan is 40,000 JAN and the 10,000 JAN/day
Mahjong King is one decent game. `POL_JAN_RATE` overrides; janlobby serves
the same number at +0x1EC so the rate read aloud is the rate used.

WARNING: **Bots must never be recorded.** `jangame.Table.bot_id()` mints non-zero
synthetic ids (`0x0B07...`) precisely so the client's seated-player count works,
which means "the id is non-zero" is NOT a test for "this is a person". The
caller passes only seats in `Table.live`; `record_game` additionally refuses any
id in the bot range as a backstop.

=== FOR THE GAME MANAGER (jangame) =========================================

    janstats.record_win(member, yaku_names, yakuman=False)
        on every win by a HUMAN seat, with the engine's yaku names (any of the
        spellings in YAKU_ALIASES; unknown names are counted under their own
        key and simply have no save slot). Bumps the per-yaku counters the
        record screen's three tabs draw and the yakuman count the popup shows.

    janstats.record_game(member, place, score, result, table_id=, bust=)
        once per finished hanchan per human seat (already called). `bust=True`
        when the seat ended below zero -- the Bust General title.

    janstats.level(member)              -> the u32 for HALF2 +0xA0 + seat*4
    janstats.level_up(member)           -> the delta for HALF2 +0x90 (0 = none)
    janstats.shogo(member)              -> [5 counts], SHOGO_NAMES order, for
                                           HALF2 +0x7C + title*4 + seat
    janstats.getshogo(member)           -> [5 flags], titles earned by the
                                           LAST recorded game, for +0x68
    janstats.jan_for_points(points)     -> the JAN column of the result record
"""
import argparse
import json
import os
import struct
import sys
import time

#: A history slot with nothing in it. WARNING: IT MUST BE NEGATIVE, AND THAT IS
#: MEASURED, not taste. `MjGAMERESULTHALF2`'s ranking values are read as SIGNED
#: (`rank = *(int *)(p + 0x18)`, yamaguchi2__00335050) and the print ladder is:
#:
#:     < 0    -> the "no rank" form
#:     < 99   -> prints rank + 1          <-- ZERO-BASED: 0 IS FIRST PLACE
#:     >= 99  -> another sentinel form
#:
#: So padding an unknown history row with 0 tells the player they came **first**
#: in a game that never happened. Four rows of zeros -- which is what
#: `jangame.msg_results2` sent before this -- read as four straight wins.
NO_RANK = -1

#: How many past games the results screen has room for: `Ranking[5][4]`, row 0
#: being the game that just finished.
HISTORY_ROWS = 4

#: Kept a little deeper than the screen needs so a `--show` is worth reading and
#: a future ladder has something to compute against.
HISTORY_KEEP = int(os.environ.get("POL_JAN_HISTORY_KEEP", "50"))

SUFFIX = ".jan_stats.json"

#: `jangame.Table.BOT_ID_BASE`, repeated rather than imported: this module must
#: stay importable on its own (responders.py picks it up without jangame), and a
#: constant that guards a data file should not depend on an optional import.
#: Asserted equal in the selftest, which is where a drift would show up.
BOT_ID_BASE = 0x0B07000000000000
BOT_ID_MASK = 0xFFFF000000000000

#: JAN per point -- see the banner. Served at `b/g/MJSTableInfoSub` +0x1EC.
RATE = int(os.environ.get("POL_JAN_RATE", "1000") or 1000)

#: The five titles in the order the client's byte arrays index them (janmsgs.
#: SHOGO_NAMES, read off the "You earned the next title." block). Keys here
#: are the record's `titles` dict keys.
SHOGO_KEYS = ("mahjong_king", "beast_king", "bust_general",
              "winnings_general", "wild_tile_king")
SHOGO_NAMES = ("Mahjong King", "King of Beasts", "Bust General",
               "Winnings Gen.", "Wild Tile King")
MAHJONG_KING_JAN = 10000        # ScoreDispWindow.c:303  `10000 - today`
BEAST_KING_EVERY = 100          # :310  `100 - top % 100`
WINNINGS_GENERAL_EVERY = 20     # :312  `0x14 - plus % 0x14`
WILD_TILE_KING_EVERY = 5        # :314  `5 - last % 5`

#: The yaku counters the record screen draws (ScoreDispWindow.c tabs 1-3,
#: read 2026-09-04 with every `lw` checked in the disassembly), as canonical
#: key -> save offset: 38 slots the client reads plus houtei's INTENDED slot
#: (below) = 39. The save's `_DAT_004461xx` global is `0x446110 + offset`.
#:
#: WARNING: SE's own off-by-one: the Haitei/Houtei line (tab 1, LB4) reads +0x128
#: and +0x12C, i.e. the Menzen-Tsumo slot and the intended HAITEI slot, and
#: +0x130 is never read by any instruction. The counters are written to the
#: INTENDED layout (haitei +0x12C, houtei +0x130) -- so on that one line the
#: screen shows Menzen Tsumo where Haitei belongs and Haitei where Houtei
#: belongs. Matching the client's bug would mean two yaku sharing one slot.
YAKU_OFFSETS = {
    # tab 1  和了回数１
    "riichi": 0x120, "ippatsu": 0x124, "menzen_tsumo": 0x128,
    "haitei": 0x12C, "houtei": 0x130, "rinshan": 0x134, "chankan": 0x138,
    "tanyao": 0x13C, "pinfu": 0x140, "iipeikou": 0x144, "yakuhai": 0x184,
    # tab 2  和了回数２
    "double_riichi": 0x11C, "sanshoku": 0x148, "sanshoku_doukou": 0x14C,
    "ittsu": 0x150, "sanankou": 0x154, "sankantsu": 0x158, "toitoi": 0x15C,
    "chiitoi": 0x160, "shousangen": 0x164, "honitsu": 0x168, "honroutou": 0x16C,
    "chanta": 0x170, "junchan": 0x174, "ryanpeikou": 0x178, "chinitsu": 0x17C,
    "double_wind": 0x180,
    # tab 3  和了回数３ -- the yakuman
    "tenhou": 0x198, "chiihou": 0x19C, "kokushi": 0x1A4, "daisangen": 0x1A8,
    "shousuushii": 0x1AC, "daisuushii": 0x1B0, "chinroutou": 0x1B4,
    "suuankou": 0x1B8, "suukantsu": 0x1BC, "tsuuiisou": 0x1C0,
    "ryuuiisou": 0x1C4, "chuuren": 0x1C8,
}
YAKUMAN_KEYS = ("tenhou", "chiihou", "kokushi", "daisangen", "shousuushii",
                "daisuushii", "chinroutou", "suuankou", "suukantsu", "tsuuiisou",
                "ryuuiisou", "chuuren")

#: Spellings the engine (or a human) might use -> canonical key. Matching is
#: case-insensitive with spaces/hyphens/underscores/dots stripped, so
#: "Menzen Tsumo", "menzen-tsumo", "MENZENTSUMO" all land. Kanji included
#: because the win screen's own table is Japanese.
YAKU_ALIASES = {
    "riichi": "riichi", "reach": "riichi", "立直": "riichi", "riich": "riichi",
    "ippatsu": "ippatsu", "一発": "ippatsu",
    "menzentsumo": "menzen_tsumo", "tsumo": "menzen_tsumo", "menzenchintsumohou":
    "menzen_tsumo", "面前清自摸": "menzen_tsumo", "門前清自摸和": "menzen_tsumo",
    "fullyconcealedhand": "menzen_tsumo",
    "haitei": "haitei", "haiteiraoyue": "haitei", "海底摸月": "haitei",
    "houtei": "houtei", "houteiraoyui": "houtei", "河底撈魚": "houtei",
    "rinshan": "rinshan", "rinshankaihou": "rinshan", "嶺上開花": "rinshan",
    "chankan": "chankan", "搶槓": "chankan", "robbingakan": "chankan",
    "tanyao": "tanyao", "allsimples": "tanyao", "タンヤオ": "tanyao", "断幺九": "tanyao",
    "pinfu": "pinfu", "平和": "pinfu",
    "iipeikou": "iipeikou", "iipeiko": "iipeikou", "一盃口": "iipeikou",
    "yakuhai": "yakuhai", "飜牌": "yakuhai", "役牌": "yakuhai", "haku": "yakuhai",
    "hatsu": "yakuhai", "chun": "yakuhai", "seatwind": "yakuhai",
    "roundwind": "yakuhai", "prevalentwind": "yakuhai", "dragon": "yakuhai",
    "white": "yakuhai", "green": "yakuhai", "red": "yakuhai",
    "doubleriichi": "double_riichi", "daburii": "double_riichi",
    "ダブル立直": "double_riichi", "doublereach": "double_riichi",
    "sanshoku": "sanshoku", "sanshokudoujun": "sanshoku", "三色同順": "sanshoku",
    "sanshokudoukou": "sanshoku_doukou", "sanshokudoko": "sanshoku_doukou",
    "三色同刻": "sanshoku_doukou",
    "ittsu": "ittsu", "ikkitsuukan": "ittsu", "一気通貫": "ittsu", "straight": "ittsu",
    "sanankou": "sanankou", "sananko": "sanankou", "三暗刻": "sanankou",
    "sankantsu": "sankantsu", "三槓子": "sankantsu",
    "toitoi": "toitoi", "toitoihou": "toitoi", "対々和": "toitoi", "対々": "toitoi",
    "chiitoi": "chiitoi", "chiitoitsu": "chiitoi", "七対子": "chiitoi",
    "sevenpairs": "chiitoi",
    "shousangen": "shousangen", "小三元": "shousangen",
    "honitsu": "honitsu", "混一色": "honitsu", "halfflush": "honitsu",
    "honroutou": "honroutou", "honroto": "honroutou", "混老頭": "honroutou",
    "chanta": "chanta", "honchanta": "chanta", "全帯公": "chanta", "混全帯幺九": "chanta",
    "junchan": "junchan", "junchanta": "junchan", "純全帯公": "junchan",
    "純全帯幺九": "junchan",
    "ryanpeikou": "ryanpeikou", "ryanpeiko": "ryanpeikou", "二盃口": "ryanpeikou",
    "chinitsu": "chinitsu", "清一色": "chinitsu", "fullflush": "chinitsu",
    "doublewind": "double_wind", "renfonpai": "double_wind", "連風牌": "double_wind",
    "doubleyakuhai": "double_wind",
    "tenhou": "tenhou", "天和": "tenhou", "chiihou": "chiihou", "chihou": "chiihou",
    "地和": "chiihou", "kokushi": "kokushi", "kokushimusou": "kokushi",
    "国士無双": "kokushi", "thirteenorphans": "kokushi",
    "daisangen": "daisangen", "大三元": "daisangen",
    "shousuushii": "shousuushii", "shousuushi": "shousuushii", "小四喜": "shousuushii",
    "daisuushii": "daisuushii", "daisuushi": "daisuushii", "大四喜": "daisuushii",
    "chinroutou": "chinroutou", "chinroto": "chinroutou", "清老頭": "chinroutou",
    "suuankou": "suuankou", "suuanko": "suuankou", "四暗刻": "suuankou",
    "suukantsu": "suukantsu", "四槓子": "suukantsu",
    "tsuuiisou": "tsuuiisou", "tsuiisou": "tsuuiisou", "字一色": "tsuuiisou",
    "ryuuiisou": "ryuuiisou", "ryuiisou": "ryuuiisou", "緑一色": "ryuuiisou",
    "chuuren": "chuuren", "chuurenpoutou": "chuuren", "九連宝燈": "chuuren",
    "ninegates": "chuuren",
}


def canonical_yaku(name):
    """A yaku name in any spelling -> its canonical key (or the cleaned
    name itself when unknown, so it is still counted)."""
    raw = str(name or "").strip()
    key = "".join(ch for ch in raw.lower() if ch not in " -_.'()")
    return YAKU_ALIASES.get(key) or YAKU_ALIASES.get(raw) or key or "unknown"


def stats_dir():
    """POL_RESOURCE_DIR first, exactly as `tetramaster._collection_dir` resolves
    it and for the same reason -- deriving it from POL_DATA_DIR alone agrees only
    until something sets the override."""
    root = os.environ.get("POL_RESOURCE_DIR")
    if not root:
        root = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return root


def stats_file(member_id):
    """Where a member's record is kept: beside the resources, because that
    directory is already the bind-mounted per-member state we serve from, but
    under its own suffix so it can never collide with a POL resource path."""
    if member_id in (None, ""):
        return None
    return os.path.join(stats_dir(), "%s%s" % (member_id, SUFFIX))


def blank(member_id=None):
    return {
        "member": member_id,
        #: The name the client showed us (`<AN>` / the handle) -- what the
        #: rank list and the popup print. Set by `note_name`.
        "name": "",
        "games_played": 0,
        #: How many times this player finished 1st, 2nd, 3rd, 4th.
        "places": [0, 0, 0, 0],
        #: Sum of final TABLE scores (the 25000-start points), signed.
        "score_total": 0,
        #: Sum of uma/oka results, x10 so it stays an integer. `ranking()`
        #: returns one decimal place, and `msg_results` already sends x10.
        "result_x10": 0,
        #: The all-time JAN balance, a running total with a FLOOR OF ZERO:
        #: each game adds its result x RATE and a loss stops at 0. No debt
        #: is carried, so a win after a losing run pays
        #: out in full. `money_prev` is the balance before the last game, for
        #: that game's results screen. None = a record from before the balance
        #: existed; `money_balance` rebuilds it from the history.
        "money": None,
        "money_prev": None,
        #: Games that finished in PLUS. The record screen renders
        #: `20 - plus_scores % 20` as progress toward the Winnings General
        #: title, so this is a cumulative COUNT, not a sum.
        "plus_games": 0,
        #: Games that ended below zero (hako) -- the Bust General title.
        "busts": 0,
        #: The three windowed money totals the record screen draws beside the
        #: all-time one. `_day` is the UTC day `result_x10_today` belongs to and
        #: `_week` its ISO week, so a stale window resets on the next game
        #: rather than accumulating forever.
        "result_x10_today": 0,
        "result_x10_week": 0,
        "result_x10_best": 0,
        "games_week": 0,
        "day": None,
        "week": None,
        #: The EVENT window, keyed by the running event's id rather than by a
        #: date: an event opens and closes when the server says so, so a new
        #: id is what starts everyone at zero. `janevent` owns the id; this is
        #: profile value v21, the only column the client's Event ranking draws
        #: besides the name and level (measured 2026-09-21: the games column
        #: for category 4 is the empty header at 0x0049f130).
        "result_x10_event": 0,
        "games_event": 0,
        "event": None,
        #: Per-yaku win counts, canonical key -> count (`record_win`).
        "yaku": {},
        #: Wins, and wins that were yakuman.
        "wins": 0,
        "yakuman": 0,
        #: Titles HELD, key -> count (SHOGO_KEYS). Cumulative: a title earned
        #: twice is held twice ("x2" on the popup).
        "titles": {},
        #: Titles earned by the LAST recorded game (SHOGO_KEYS order, 0/1) --
        #: `getshogo` for that game's results screen.
        "last_titles": [0, 0, 0, 0, 0],
        #: The day Mahjong King was last awarded, so it is once per day.
        "mahjong_king_day": None,
        #: Newest first, capped at HISTORY_KEEP.
        "history": [],
        #: Anything here wins over `derive()`. See `set_override`.
        "overrides": {},
    }


def is_bot_id(member_id):
    """True for a `jangame` bot seat. Non-zero ids are NOT all people."""
    try:
        v = int(member_id)
    except (TypeError, ValueError):
        return False
    return v > 0 and (v & BOT_ID_MASK) == BOT_ID_BASE


def load(member_id):
    """This member's record, with every key present. Never raises."""
    rec = blank(member_id)
    path = stats_file(member_id)
    if not path:
        return rec
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return rec
    if not isinstance(data, dict):
        return rec
    rec.update({k: v for k, v in data.items() if k in rec})
    # A hand-edited file must not be able to crash a game in progress.
    if not isinstance(rec.get("places"), list) or len(rec["places"]) != 4:
        rec["places"] = [0, 0, 0, 0]
    if not isinstance(rec.get("history"), list):
        rec["history"] = []
    for k in ("overrides", "yaku", "titles"):
        if not isinstance(rec.get(k), dict):
            rec[k] = {}
    if not isinstance(rec.get("last_titles"), list) or len(rec["last_titles"]) != 5:
        rec["last_titles"] = [0, 0, 0, 0, 0]
    rec["member"] = member_id
    return rec


def store(member_id, rec):
    """Write atomically. Returns True on success; a failure is logged by the
    caller, never raised into a hand in progress."""
    path = stats_file(member_id)
    if not path:
        return False
    try:
        os.makedirs(stats_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def note_name(member_id, name):
    """Remember what this member is called (the popup's and rank list's
    name column). Cheap and idempotent."""
    if not member_id or is_bot_id(member_id) or not name:
        return False
    rec = load(member_id)
    if rec.get("name") == str(name):
        return False
    rec["name"] = str(name)[:16]
    return store(member_id, rec)


def _event_id(when=None):
    """The running event's id, or 0. Kept behind a helper so janstats does not
    hard-depend on janevent: without it there is simply never an event."""
    try:
        import janevent
        return janevent.event_id(when)
    except Exception:                                       # pragma: no cover
        return 0


def _roll_windows(rec, when=None):
    t = time.gmtime(when if when is not None else time.time())
    day = time.strftime("%Y-%j", t)
    week = time.strftime("%G-W%V", t)
    if rec.get("day") != day:
        rec["day"], rec["result_x10_today"] = day, 0
    if rec.get("week") != week:
        rec["week"], rec["result_x10_week"], rec["games_week"] = week, 0, 0
    # The event window rolls on the EVENT ID, not on the clock. 0 = no event
    # running, which also resets it, so winnings never leak from a closed
    # event into the next one.
    ev = _event_id(when)
    if rec.get("event") != ev:
        rec["event"], rec["result_x10_event"], rec["games_event"] = ev, 0, 0
    return day, week


def current_windows(rec, when=None):
    """A COPY of `rec` with the today / week / event windows rolled to now.

    `record_game` only rolls them when a game is recorded, so a player who
    does not play the next day was still shown yesterday's winnings as
    "today" (42,700 JAN "today" from the previous day's game). Every reader
    goes through this; the file is not written."""
    rec = dict(rec)
    _roll_windows(rec, when)
    return rec


def jan_for_points(points):
    """The JAN column of a result record for a `result` in points (one
    decimal is fine): points x RATE, truncated toward zero."""
    return int(float(points) * RATE)


def record_game(member_id, place, score, result=0.0, table_id=None, when=None,
                bust=None, name=None):
    """One finished hanchan -> the updated record, written to disk.

    `place` is ZERO-BASED, as the client's own ranking values are. `result` is
    the uma/oka-adjusted score in points (one decimal), i.e. exactly what
    `janmahjong.Game.ranking()` puts in its `result` key. `bust` says the seat
    ended below zero (defaults to `score < 0`).

    Awards titles by the record screen's thresholds and leaves them in
    `last_titles` for that game's `getshogo`.

    Returns the new record, or None if this id is not a person we keep records
    for (a bot, or no id at all).
    """
    if not member_id or is_bot_id(member_id):
        return None
    place = int(place)
    if not 0 <= place <= 3:
        raise ValueError("place is zero-based 0..3, got %r" % (place,))
    rec = load(member_id)
    if name:
        rec["name"] = str(name)[:16]
    r10 = int(round(float(result) * 10))
    # The balance first, while `result_x10` and the history still describe
    # the record BEFORE this game (`money_balance` may rebuild from them).
    rec["money_prev"] = money_balance(rec)
    rec["money"] = max(0, rec["money_prev"] + jan_for_points(r10 / 10.0))
    rec["games_played"] += 1
    rec["places"][place] += 1
    rec["score_total"] += int(score)
    rec["result_x10"] += r10
    if r10 > 0:
        rec["plus_games"] += 1
    if bust is None:
        bust = int(score) < 0
    if bust:
        rec["busts"] = int(rec.get("busts", 0)) + 1

    # The windowed totals. Rolling them here rather than at read time keeps the
    # file self-describing -- `--show` reads the same numbers the client will --
    # and means a member who never plays again does not have "today" quietly
    # follow them around.
    day, _week = _roll_windows(rec, when)
    rec["result_x10_today"] += r10
    rec["result_x10_week"] += r10
    rec["games_week"] = int(rec.get("games_week", 0)) + 1
    if rec.get("event"):
        rec["result_x10_event"] += r10
        rec["games_event"] = int(rec.get("games_event", 0)) + 1
    rec["result_x10_best"] = max(int(rec.get("result_x10_best", 0)),
                                 rec["result_x10_today"])

    # --- the titles, by the thresholds the record screen prints -----------
    earned = [0, 0, 0, 0, 0]
    titles = rec.setdefault("titles", {})
    if (jan_for_points(rec["result_x10_today"] / 10.0) >= MAHJONG_KING_JAN
            and rec.get("mahjong_king_day") != day):
        rec["mahjong_king_day"] = day
        earned[0] = 1
    if place == 0 and rec["places"][0] % BEAST_KING_EVERY == 0:
        earned[1] = 1
    if bust:
        earned[2] = 1
    if r10 > 0 and rec["plus_games"] % WINNINGS_GENERAL_EVERY == 0:
        earned[3] = 1
    if place == 3 and rec["places"][3] % WILD_TILE_KING_EVERY == 0:
        earned[4] = 1
    for i, key in enumerate(SHOGO_KEYS):
        if earned[i]:
            titles[key] = int(titles.get(key, 0)) + 1
    rec["last_titles"] = earned

    rec["history"].insert(0, {
        "place": place,
        "score": int(score),
        "result_x10": r10,
        "table": table_id,
        "t": int(when if when is not None else time.time()),
    })
    del rec["history"][HISTORY_KEEP:]
    store(member_id, rec)
    return rec


def record_win(member_id, yaku_names=(), yakuman=False, when=None):
    """One win by a human seat -> the per-yaku counters, written to disk.

    THE JANGAME SEAM (finding 28): call it from the win path with the
    engine's yaku names for the winning hand (any spelling in YAKU_ALIASES).
    A yakuman is counted from the names (any YAKUMAN_KEYS) OR the flag.
    Returns the record, or None for a bot / no id.
    """
    if not member_id or is_bot_id(member_id):
        return None
    rec = load(member_id)
    yaku = rec.setdefault("yaku", {})
    keys = [canonical_yaku(n) for n in (yaku_names or ()) if n]
    for k in keys:
        yaku[k] = int(yaku.get(k, 0)) + 1
    rec["wins"] = int(rec.get("wins", 0)) + 1
    if yakuman or any(k in YAKUMAN_KEYS for k in keys):
        rec["yakuman"] = int(rec.get("yakuman", 0)) + 1
    store(member_id, rec)
    return rec


def recent_places(member_id_or_rec, n=HISTORY_ROWS, skip=0):
    """The last `n` placings, newest first, padded with `NO_RANK`.

    `skip` drops that many of the newest entries, which is what the results
    screen wants: row 0 is the game that just finished and is already on the
    wire, so rows 1..4 are `skip=1`.
    """
    rec = (member_id_or_rec if isinstance(member_id_or_rec, dict)
           else load(member_id_or_rec))
    hist = rec.get("history") or []
    out = [int(h.get("place", NO_RANK)) for h in hist[skip:skip + n]]
    return out + [NO_RANK] * (n - len(out))


# === POLICY: the derived values ============================================
#
# PARTIAL: EVERY FORMULA BELOW IS A PLACEHOLDER, AND IS MARKED AS ONE ON PURPOSE.
# The client shows a Level, a 95-tier Rank, a "Jan Rating" and a "Title
# score" and defines none of them -- they were SE's server's to compute. So
# the contract is deliberately narrow: turn recorded FACTS into numbers, in
# one place, that the server owner can override per member, and use those SAME
# numbers on every screen.

#: Games per level, in the placeholder ladder. PARTIAL: invented; 5 chosen
#: so it moves at the same pace as the rank ladder
#: (RANK_TIER_GAMES), which was already a tier every 5 games. It was 10, which
#: on a server this size meant a player sat on Lv 1 indefinitely.
LEVEL_GAMES = int(os.environ.get("POL_JAN_LEVEL_GAMES", "5"))
#: The PTL member row draws `"Lv%2d"`, so two digits is the width the UI expects.
LEVEL_MAX = int(os.environ.get("POL_JAN_LEVEL_MAX", "99"))
#: What a fresh player starts with. PARTIAL: invented; 0 is the honest default.
START_MONEY = int(os.environ.get("POL_JAN_START_MONEY", "0"))
#: The Jan Rating's SCALE is SE's (decided 2026-09-12: "we definitely want
#: it to be more like SE"): SE's own ranking shot (manual ml099i, a 2002 test
#: DB) shows every rated player at 6.7..9.1, a 1-game player already at 7.10,
#: and 0.0 for a 76-game player -- a floor. The FORMULA is ours; SE's is in
#: nothing we hold:
#:     rating = max(0, BASE + sum(result / SCALE) / (games + PRIOR))
#: i.e. the average uma/oka result per game, shrunk toward BASE by PRIOR
#: phantom average games so one lucky hanchan cannot top the board.
RATING_BASE = float(os.environ.get("POL_JAN_RATING_BASE", "7.0"))
RATING_SCALE = float(os.environ.get("POL_JAN_RATING_SCALE", "10.0") or 10.0)
RATING_PRIOR = float(os.environ.get("POL_JAN_RATING_PRIOR", "2.0"))
#: The rank variants key off the rating: 3 "exceptional" from here up.
RATING_EXCEPTIONAL = float(os.environ.get("POL_JAN_RATING_EXCEPTIONAL", "8.0"))
#: Title score weights per title HELD (SHOGO_KEYS). PARTIAL: ours; the one measured
#: constraint is that SE's scores are all multiples of 0.25 (18.5, 7.75, 4.75).
#: 爆敗王 (every 5 last places) counts against, floored at 0 like the rating.
TITLE_WEIGHTS = {"mahjong_king": 1.0, "beast_king": 5.0, "winnings_general": 0.5,
                 "bust_general": 0.25, "wild_tile_king": -0.25}

#: The rank ladder (`ReturnRoom__00364c60`, `PTR_DAT_0040fe60`): 95 names =
#: 19 TIERS x 5 VARIANTS, index = tier*5 + variant, 0..94 valid, else
#: "Error". Tier bases: 0 新人 Rookie, 5 凡人 Novice, 10 強者, 15 天才, 20 名人,
#: 25 豪傑, 30 将軍, 35 偉人, 40 元帥, 45 魔人, 50 皇帝, 55 仙人, 60 青竜,
#: 65 白虎, 70 朱雀, 75 玄武, 80 麒麟, 85 鳳凰, 90 雀神 Jan God. Variants:
#: 0 plain, 1 "rich", 2 "lonely/sad", 3 "lucky/rare", 4 "lazy/top-5%".
RANK_TIERS, RANK_VARIANTS, RANK_MAX = 19, 5, 94
#: PARTIAL: Games per tier in the placeholder ladder.
RANK_TIER_GAMES = int(os.environ.get("POL_JAN_RANK_TIER_GAMES", "5"))


def _facts(rec):
    games = int(rec.get("games_played", 0))
    places = rec.get("places") or [0, 0, 0, 0]
    result_pts = int(rec.get("result_x10", 0)) / 10.0
    avg_place = ((sum((i + 1) * int(places[i]) for i in range(4)) / float(games))
                 if games else 0.0)
    top_rate = (int(places[0]) / float(games)) if games else 0.0
    return games, places, result_pts, avg_place, top_rate


def rating_of(rec):
    """Jan Rating on SE's scale (see RATING_BASE): BASE + the average
    uma/oka result per game / SCALE, shrunk by PRIOR, floored at 0. A +40
    hanchan moves a 1-game player to 7 + 4/3 = 8.33; the floor is SE's 0.0.
    Rounded to SE's 6 decimals (the wire is x1e6). Overridable."""
    ov = (rec.get("overrides") or {}).get("rating")
    if ov is not None:
        try:
            return float(ov)
        except (TypeError, ValueError):
            pass
    games, _p, result_pts, _a, _t = _facts(rec)
    if not games:
        return RATING_BASE
    r = RATING_BASE + (result_pts / RATING_SCALE) / (games + RATING_PRIOR)
    return round(max(0.0, r), 6)


def title_score_of(rec):
    """PARTIAL: Title score: titles held x TITLE_WEIGHTS, floored at 0 -- quarter
    steps, like every score in SE's shot."""
    t = rec.get("titles") or {}
    return max(0.0, float(sum(int(t.get(k, 0) or 0) * w for k, w in TITLE_WEIGHTS.items())))


def level_of(rec):
    """THE level: `1 + games // LEVEL_GAMES`, capped -- one function, every
    screen. PARTIAL: placeholder ladder."""
    ov = (rec.get("overrides") or {}).get("level")
    if ov is not None:
        try:
            return max(1, min(LEVEL_MAX, int(ov)))
        except (TypeError, ValueError):
            pass
    games = int(rec.get("games_played", 0))
    return min(LEVEL_MAX, 1 + games // LEVEL_GAMES) if games else 1


def rank_index_of(rec):
    """PARTIAL: The 95-tier rank index. Tier = games // RANK_TIER_GAMES (capped at
    18, Jan God); variant from the record: 4 when a player's rating has hit
    SE's 0.0 floor (the variant is "5%以下", BOTTOM 5% -- SE's Majin: 1 game,
    0.0, やる気無い新人), 3 "exceptional" at RATING_EXCEPTIONAL and up, 2
    "sad" when the average place is worse than 2.75, 1 "rich" when the money
    is positive, else 0 plain. Overridable."""
    ov = (rec.get("overrides") or {}).get("rank")
    if ov is not None:
        try:
            return max(0, min(RANK_MAX, int(ov)))
        except (TypeError, ValueError):
            pass
    games, _p, result_pts, avg_place, _t = _facts(rec)
    tier = min(RANK_TIERS - 1, games // RANK_TIER_GAMES)
    rating = rating_of(rec)
    if games and rating <= 0.0:
        variant = 4
    elif games and rating >= RATING_EXCEPTIONAL:
        variant = 3
    elif games and avg_place > 2.75:
        variant = 2
    elif result_pts > 0:
        variant = 1
    else:
        variant = 0
    return min(RANK_MAX, tier * RANK_VARIANTS + variant)


def money_of(rec, key="result_x10"):
    return jan_for_points(int(rec.get(key, 0)) / 10.0)


def money_balance(rec):
    """The all-time JAN balance: floored at 0 game by game, so a loss never
    leaves a debt for the next win to pay off (see `blank`).

    A record written before the balance existed has `money` None. Its balance
    is rebuilt by replaying the history oldest first, with any games older
    than the history (HISTORY_KEEP) folded in as one opening step."""
    v = rec.get("money")
    if isinstance(v, int) and not isinstance(v, bool):
        return max(0, v)
    hist = list(reversed(rec.get("history") or []))
    older = (int(rec.get("result_x10", 0))
             - sum(int(h.get("result_x10", 0)) for h in hist))
    bal = max(0, START_MONEY + jan_for_points(older / 10.0))
    for h in hist:
        bal = max(0, bal + jan_for_points(int(h.get("result_x10", 0)) / 10.0))
    return bal


def derive(rec):
    """Everything `jansave.FIELDS` and the results screen can ask for.

    THE MEASURED HALF maps one-to-one onto save fields whose read sites were
    disassembled (2026-08-24 the money four and the three title counters;
    2026-09-04 level +0x238, rank +0x23C and the 38 yaku counters):

      * `money*` are JAN = points x RATE (see the banner), s64 on screen.
      * `top_finishes` / `last_places` / `plus_scores` are cumulative counts
        the client renders modulo a threshold.
      * `yaku_<key>` are the per-yaku win counts, one per YAKU_OFFSETS entry.
      * `level` / `rank` are `level_of` / `rank_index_of` -- the SAME numbers
        the popup, the rank list and HALF2 carry.

    `rec['overrides']` wins over all of it, so a live test can pin any value.
    """
    rec = current_windows(rec)
    games, places, _r, _a, _t = _facts(rec)
    out = {
        "games_played": games,
        "level": level_of(rec),
        "money": money_balance(rec),
        "rank": rank_index_of(rec),
        # measured save fields
        "top_finishes": int(places[0]),
        "last_places": int(places[3]),
        "plus_scores": int(rec.get("plus_games", 0)),
        "money_today": max(0, money_of(rec, "result_x10_today")),
        "money_weekly": max(0, money_of(rec, "result_x10_week")),
        "money_best": max(0, money_of(rec, "result_x10_best")),
    }
    yaku = rec.get("yaku") or {}
    for key in YAKU_OFFSETS:
        out["yaku_" + key] = int(yaku.get(key, 0))
    for k, v in (rec.get("overrides") or {}).items():
        if k in out and k not in ("level", "rank"):    # those two already applied
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
    return out


def level(member_id):
    """The u32 for HALF2 +0xA0 + seat*4, the PTL row's Lv, the popup's v32."""
    return level_of(load(member_id))


def level_up(member_id):
    """The delta for HALF2 +0x90 + seat*4 after the game just recorded (0 =
    no level up). Computed by re-deriving the level one game back."""
    rec = load(member_id)
    now = level_of(rec)
    before = dict(rec)
    before["games_played"] = max(0, int(rec.get("games_played", 0)) - 1)
    return max(0, now - level_of(before))


def shogo(member_id):
    """[5] titles HELD (counts, SHOGO_KEYS order) -- HALF2 +0x7C + title*4."""
    t = load(member_id).get("titles") or {}
    return [int(t.get(k, 0)) & 0xFF for k in SHOGO_KEYS]


def getshogo(member_id):
    """[5] titles EARNED by the last recorded game -- HALF2 +0x68; a non-zero
    entry makes the client announce "You earned the next title."."""
    lt = load(member_id).get("last_titles") or [0] * 5
    return [1 if x else 0 for x in list(lt)[:5]] + [0] * (5 - min(5, len(lt)))


def set_override(member_id, key, value):
    """Pin one derived value for this member. `value=None` clears it."""
    rec = load(member_id)
    if value is None:
        rec["overrides"].pop(key, None)
    else:
        rec["overrides"][key] = (float(value) if key == "rating" else int(value))
    store(member_id, rec)
    return rec


def summary(member_id):
    """One line for a log."""
    rec = load(member_id)
    d = derive(rec)
    return ("member %s: %d game(s) %s, Lv%d rank %d money %d rating %.3f "
            "titles %s"
            % (member_id, d["games_played"],
               "/".join(str(n) for n in rec["places"]),
               d["level"], d["rank"], d["money"], rating_of(rec),
               ",".join("%s=%d" % (k, v) for k, v in
                        sorted((rec.get("titles") or {}).items())) or "-"))


# === THE PROFILE / RANK RECORD ============================================
#
# ONE STRUCT, TWO CONSUMERS. The popup's `<PO>` reply is 71 positional values
# parsed by `lb__002f90e0` into a 0xE8-byte struct (pfc.c), and the rankings
# screen reads `U/g/<stem>_RANKLIST` as an array of EXACTLY that struct
# (rkc.c / ranking_win.c, every offset it draws falls on a `<PO>` field). So
# `profile_fields()` produces the value list once and `pack_profile()` lays
# it out as the 232-byte record.
#
# value -> struct (lb.c:519-668; polpro.PROFILE_PO carries the same table):
#   v0  u64 +0x10 content id     v1  int +0x18       v2  str +0x00 name (16)
#   v3..v6 u8 +0x1c..+0x1f       v7,v8 u8 +0x29,+0x2a    v9 str +0x30 (64)
#   v10,v11 u64 +0x70,+0x78      v12..v15 float +0x80..+0x8c  (x1e6 on the wire)
#   v16..v23 u32 +0x90..+0xac    v24..v31 u16 +0xb0..+0xbe    v32..v39 u8 +0xc0..
#   v40 u8 +0xc8                 v41..v70 u8 +0xc9..+0xe6 = SHOW FLAGS k=v-41
#
# what the popup draws (showprof.c) and the rank list sorts on (ranking_win.c):
#   v10 Money               v12 Avg place   v13 Top rate   v14 Jan Rating
#   v15 Title score         v17 Total games v19 Overall gamble winnings
#   v20 Weekly winnings     v21 Event       v22 Gamble games  v23 Weekly games
#   v24 update week (unix/604800, record 0 only)  v25 Yakuman count
#   v26 Rank index          v27..v31 the five title counts (SHOGO_KEYS order:
#   Mahjong King, King of Beasts, Bust General, Winnings General, Wild Tile
#   King)                   v32 Level       v35..v39 previous rank per category
#   (signed, -1 = "New")
#
# WARNING: THE FLOATS ARE INTEGERS x 1e6 ON THE WIRE: `mg__002f8300` -> atof with no
# scaling, and every draw site divides by 1e6 (showprof.c:381, ranking_win.c:
# 352). Sending "2.5" would show 0.000002.
#
# WARNING: THAT IS NOT TRUE OF THE 2004 BUILD, MEASURED ON SCREEN 2026-09-21. Its
# ranking screen drew a fresh player's Jan Rating as `7000000.0` where the
# value meant is 7.0 -- i.e. it prints what we send, undivided. Neither module
# even REFERENCES a 1e6 constant: the only 1e6 doubles in either binary
# (2002 0x4194c8/0x41b148, 2004 0x495998/0x497ef0) have no `ldc1` pointing at
# them and belong to the C library, and the R5900 has no double FPU anyway.
# SE's own ranking shot has every rated player at 6.7..9.1, which is the scale
# `RATING_BASE` was set from, so 7.0 is what the column is meant to read.
# Both channels serve the 2004 build now, so the default is 1; set
# POL_JAN_FLOAT_SCALE=1000000 to put the old scaling back for a 2002 client.
#
# WARNING: THIS SURVIVED A CHALLENGE, and the challenge is worth keeping.
# A ranking screenshot showed a rating of 8.423333 drawn as `0.0000079`,
# which reads exactly like a client dividing by 1e6 -- and it is not. That shot
# is OUR OWN web reproduction of the screen (`boardjan.render`), which had a
# hardcoded `/ 1e6` left over from before this knob existed AND truncated the
# field to an int first: `mailfmt(f32(8) / 1e6, 7)` is `0.0000079`, the `79`
# coming out of float32 rounding, not out of any 7.9. Fixed in `row_strings`.
# KEY: Two different wrong scalings can print the same string; before moving THIS
# constant, run the formatter on the candidate inputs and see which one it is.
PROFILE_REC = 0xE8
FLOAT_SCALE = int(os.environ.get("POL_JAN_FLOAT_SCALE", "1") or 1)
RANK_CATEGORIES = ("JongHoLow Rating", "Title score", "Overall gamble winnings",
                   "Weekly gamble winnings", "Event game winnings")
RANK_SORT_VALUE = {0: 14, 1: 15, 2: 19, 3: 20, 4: 21}     # category -> vN
RANK_PREV_VALUE = {0: 35, 1: 36, 2: 37, 3: 38, 4: 39}     # category -> vN
RANK_LIST_STEM = "MJS"                  # `U/g/%s_RANKLIST` (mg.c:1019) + category
RANK_LIST_MAX = 100                     # the widget array holds 113; 100 is a page
                                        # count the client's own paging expects
#: The previous order of each rank list, which feeds the up/down/New glyph: the
#: jan_rank_snapshot table (PostgreSQL, through janstore; it was the file
#: <resources>/jan-rank-snapshot.json), one row per category.
RANK_SNAPSHOT = "jan_rank_snapshot"
#: prefixed to each category's row name; the self-test sets a scope of its own
#: so running it on a live server cannot move anyone's glyph
_SNAPSHOT_SCOPE = [""]

_PO_LAYOUT = (
    (0, 1, "u64", 0x10), (1, 1, "int", 0x18), (2, 1, "str", 0x00),
    (3, 4, "int", 0x1C), (7, 2, "int", 0x29), (9, 1, "str", 0x30),
    (10, 2, "u64", 0x70), (12, 4, "float", 0x80), (16, 8, "int", 0x90),
    (24, 8, "int", 0xB0), (32, 8, "int", 0xC0), (40, 1, "int", 0xC8),
    (41, 2, "int", 0xC9), (43, 4, "int", 0xCB), (47, 8, "int", 0xCF),
    (55, 8, "int", 0xD7), (63, 8, "int", 0xDF),
)
_PO_WIDTH = {0x1C: 1, 0x29: 1, 0x90: 4, 0xB0: 2, 0xC0: 1, 0xC8: 1,
             0xC9: 1, 0xCB: 1, 0xCF: 1, 0xD7: 1, 0xDF: 1}


def profile_fields(member_id, name=None, content_id=0, show_flags=None,
                   prev_ranks=None, week=None):
    """`{vN: value}` for `polpro.build_po` -- THE popup and rank-row values.

    Floats go out already x1e6 (see above). `show_flags` is `{k: 0/1}` for
    v41+ (`_content_profile_note`'s stored `<NO>(k,v)` pairs; unset = shown,
    which is the client's own default -- index 0 of {Public, Private}).
    `prev_ranks` is `{category: previous 0-based rank}` for v35..v39; absent
    = -1 = "New".
    """
    rec = current_windows(load(member_id))
    games, places, _r, avg_place, top_rate = _facts(rec)
    t = rec.get("titles") or {}
    money = money_balance(rec)
    f = {
        0: int(content_id or 0), 1: int(content_id or 0),
        2: (name or rec.get("name") or ""),
        10: money,
        11: money,                          # "Total winnings" (v11, unread --
                                            # showprof.c:400 prints v10; kept
                                            # equal so a fixed client agrees)
        # ROUNDED, NOT TRUNCATED TO int: at FLOAT_SCALE 1 the decimals ARE the
        # value (an average place of 2.5 is 2.5, and SE's own rating column
        # runs 6.7..9.1), and it is the scaling that used to carry them.
        12: round(avg_place * FLOAT_SCALE, 6),
        13: round(top_rate * FLOAT_SCALE, 6),
        14: round(rating_of(rec) * FLOAT_SCALE, 6),
        15: round(title_score_of(rec) * FLOAT_SCALE, 6),
        17: games,
        19: money,
        20: max(0, money_of(rec, "result_x10_week")),
        # v21, the Event ranking's only value column. Zero unless an event is
        # running and this member has won something inside it.
        21: max(0, money_of(rec, "result_x10_event")) if rec.get("event") else 0,
        22: games,
        23: int(rec.get("games_week", 0)),
        24: int((week if week is not None else time.time()) // 604800) & 0xFFFF,
        25: int(rec.get("yakuman", 0)),
        26: rank_index_of(rec),
        32: level_of(rec),
    }
    for i, key in enumerate(SHOGO_KEYS):
        f[27 + i] = int(t.get(key, 0))
    for cat, vn in RANK_PREV_VALUE.items():
        pr = (prev_ranks or {}).get(cat, NO_RANK)
        f[vn] = int(pr) & 0xFF
    for k, v in (show_flags or {}).items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= k <= 29:
            f[41 + k] = int(v) & 0xFF
    return f


def pack_profile(fields):
    """The 0xE8-byte struct `lb__002f90e0` would have produced from these
    `<PO>` values -- one rank-list row."""
    rec = bytearray(PROFILE_REC)
    for first, count, kind, base in _PO_LAYOUT:
        for j in range(count):
            v = fields.get(first + j)
            if kind == "str":
                raw = str(v or "").encode("cp932", "replace")
                lim = 16 if base == 0x00 else 64
                raw = raw[:lim - 1]
                rec[base:base + len(raw)] = raw
            elif kind == "u64":
                struct.pack_into("<Q", rec, base + 8 * j,
                                 int(v or 0) & 0xFFFFFFFFFFFFFFFF)
            elif kind == "float":
                # the STRUCT holds a float; the wire value is already x1e6
                struct.pack_into("<f", rec, base + 4 * j, float(v or 0))
            else:
                w = _PO_WIDTH.get(base, 4)
                off = base + w * j
                iv = int(v or 0)
                if w == 1:
                    rec[off] = iv & 0xFF
                elif w == 2:
                    struct.pack_into("<H", rec, off, iv & 0xFFFF)
                else:
                    struct.pack_into("<I", rec, off, iv & 0xFFFFFFFF)
    return bytes(rec)


def rank_list_path(category):
    """`U/g/MJS_RANKLIST<n>` -- the client's own `U/g/%s_RANKLIST` format
    (mg.c:1019, dead code in this build but SE's name) plus the category, so
    the fetch (login band) knows which ORDER the reply (auth band) promised."""
    return "U/g/%s_RANKLIST%d" % (RANK_LIST_STEM, int(category))


def is_rank_list_path(path):
    return rank_category_of(path) is not None


def rank_category_of(path):
    stem = "U/g/%s_RANKLIST" % RANK_LIST_STEM
    p = str(path or "")
    if not p.startswith(stem):
        return None
    tail = p[len(stem):]
    if tail == "":
        return 0
    if tail.isdigit() and 0 <= int(tail) < len(RANK_CATEGORIES):
        return int(tail)
    return None


def all_members():
    """Every member with a record on disk."""
    out = []
    try:
        for fn in os.listdir(stats_dir()):
            if fn.endswith(SUFFIX):
                mid = fn[:-len(SUFFIX)]
                if mid.isdigit():
                    out.append(int(mid))
    except OSError:
        pass
    return sorted(out)


def _snapshot_load():
    """{category (str): [member, ...]} as last remembered, or {} (also when the
    database cannot be reached: every row then reads as new)."""
    scope = _SNAPSHOT_SCOPE[0]
    try:
        import janstore
        janstore.ensure_schema()
        rows = janstore.db.query("SELECT category, data FROM jan_rank_snapshot"
                                 " WHERE left(category, %s) = %s", (len(scope), scope))
    except Exception:                                       # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        cat = r["category"][len(scope):]
        if cat.isdigit() and isinstance(r["data"], list):
            out[cat] = r["data"]
    return out


def _snapshot_store(d):
    """Remember each category's order. Never raises: a failed write only means
    the next list compares against an older one."""
    scope = _SNAPSHOT_SCOPE[0]
    try:
        import janstore
        janstore.ensure_schema()
        with janstore.db.transaction() as conn:
            for cat, order in d.items():
                janstore.db.execute(
                    "INSERT INTO jan_rank_snapshot (category, data) VALUES (%s, %s::jsonb)"
                    " ON CONFLICT (category) DO UPDATE SET data = EXCLUDED.data,"
                    " updated_at = now()",
                    (scope + str(cat), janstore.jsonb(order)), conn=conn)
    except Exception:                                       # noqa: BLE001
        pass


def rank_list(category, limit=RANK_LIST_MAX, names=None, content_ids=None,
              remember=True):
    """[(member, fields)] sorted for `category` (0..4), best first.

    Only members who have PLAYED are ranked. `names`/`content_ids` are
    `{member: value}` lookups the caller (responders) resolves from the
    accounts DB; the record's own stored name is the fallback. The previous
    order is kept apart from the records (RANK_SNAPSHOT, so the login
    container never rewrites a member's record) and feeds the up/down/New
    glyph.
    """
    category = int(category)
    if category not in RANK_SORT_VALUE:
        raise ValueError("rank category is 0..4, got %r" % (category,))
    snap = _snapshot_load()
    prev = {int(m): i for i, m in enumerate(snap.get(str(category)) or [])
            if str(m).isdigit()}
    rows = []
    for m in all_members():
        rec = load(m)
        if not int(rec.get("games_played", 0)):
            continue
        f = profile_fields(
            m, name=(names or {}).get(m), content_id=(content_ids or {}).get(m, 0),
            prev_ranks={category: prev.get(m, NO_RANK)})
        # A caller that resolved names and got none for this member is looking
        # at an account that no longer exists: ranked, it was a row with a
        # blank name. Same rule as tmrank's list. The
        # CLI passes no names at all and still sees every record.
        if names is not None and not str(f.get(2) or "").strip():
            continue
        rows.append((m, f))
    key = RANK_SORT_VALUE[category]
    # WARNING: FLOAT, NOT int. Categories 0 and 1 sort on the Jan Rating and the
    # Title score, and at FLOAT_SCALE 1 those are 5.95 / 8.42 / 8.51 -- an
    # `int()` here collapsed every rated player into two or three buckets and
    # then ranked them by MEMBER NUMBER inside each. Measured 2026-09-22: six
    # players came out 8.06, 8.51, 8.27, 8.42, 7.40, 5.95, i.e. not in order.
    # (It was invisible while the wire carried these x1e6, because then they
    # really were integers -- see janstats.FLOAT_SCALE.) `float()` is right
    # under either scaling; the tiebreak stays whole games.
    rows.sort(key=lambda mf: (-float(mf[1].get(key, 0) or 0),
                              -int(mf[1].get(17, 0) or 0), mf[0]))
    rows = rows[:limit]
    if remember:
        snap[str(category)] = [m for m, _f in rows]
        _snapshot_store(snap)
    return rows


def rank_list_blob(category, limit=RANK_LIST_MAX, names=None, content_ids=None):
    """The `U/g/MJS_RANKLIST<n>` bytes: N x 232, already in rank order."""
    rows = rank_list(category, limit=limit, names=names, content_ids=content_ids)
    return b"".join(pack_profile(f) for _m, f in rows)


# --- CLI / selftest ---------------------------------------------------------

def selftest():
    import tempfile
    ok = True

    def check(cond, msg):
        if not cond:
            print("FAIL: %s" % msg)
        return bool(cond)

    # The bot guard must agree with the module that mints the ids.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import jangame
        ok &= check(jangame.Table.BOT_ID_BASE == BOT_ID_BASE,
                    "BOT_ID_BASE drifted from jangame: %#x vs %#x"
                    % (jangame.Table.BOT_ID_BASE, BOT_ID_BASE))
    except ImportError:
        print("note: jangame not importable, BOT_ID_BASE not cross-checked")
    try:
        import janmsgs
        ok &= check(tuple(janmsgs.SHOGO_NAMES) == SHOGO_NAMES,
                    "SHOGO_NAMES order drifted from janmsgs: %r" % (janmsgs.SHOGO_NAMES,))
    except ImportError:
        pass
    try:
        import polpro
        ok &= check(tuple((a, b, c, d) for a, b, c, d in polpro.PROFILE_PO)
                    == _PO_LAYOUT,
                    "the <PO> layout drifted from polpro.PROFILE_PO")
    except ImportError:
        pass

    old = os.environ.get("POL_RESOURCE_DIR")
    tmp = tempfile.mkdtemp(prefix="janstats-")
    os.environ["POL_RESOURCE_DIR"] = tmp
    old_scope = _SNAPSHOT_SCOPE[0]
    _SNAPSHOT_SCOPE[0] = "selftest-%s:" % os.path.basename(tmp)
    try:
        ok &= check(load(9)["games_played"] == 0, "an unknown member reads blank")
        ok &= check(recent_places(9) == [NO_RANK] * 4,
                    "and their history is four NO_RANKs, not four zeros -- a "
                    "zero would print as FIRST PLACE")

        ok &= check(is_bot_id(BOT_ID_BASE | 0x0102), "a bot id is recognised")
        ok &= check(not is_bot_id(8), "a real member id is not a bot")
        ok &= check(record_game(BOT_ID_BASE | 3, 0, 40000, 50.0) is None,
                    "a bot's result is REFUSED -- it must never reach the file")
        ok &= check(not os.path.exists(stats_file(BOT_ID_BASE | 3)),
                    "and no file was created for it")
        ok &= check(record_win(BOT_ID_BASE | 3, ["riichi"]) is None,
                    "a bot's win is refused too")

        record_game(9, 0, 42300, 45.5, table_id=1, name="PS2Tester")
        record_game(9, 3, 11200, -33.8, table_id=1)
        rec = load(9)
        ok &= check(rec["games_played"] == 2, "two games recorded")
        ok &= check(rec["places"] == [1, 0, 0, 1], "one 1st and one 4th")
        ok &= check(rec["score_total"] == 53500, "scores summed")
        ok &= check(rec["result_x10"] == 455 - 338,
                    "results summed x10 with no float drift: %r"
                    % rec["result_x10"])
        ok &= check(rec["name"] == "PS2Tester", "the name is kept")
        ok &= check(recent_places(9) == [3, 0, NO_RANK, NO_RANK],
                    "history is newest first and padded: %r" % recent_places(9))
        ok &= check(recent_places(9, skip=1) == [0, NO_RANK, NO_RANK, NO_RANK],
                    "skip=1 drops the game that is already in row 0")

        ok &= check(derive(rec)["level"] == 1 and level(9) == 1,
                    "two games is still level 1, on both APIs")
        # THE MONEY UNIT: JAN = points x RATE (11.7 points -> 11700 at 1000)
        ok &= check(derive(rec)["money"] == max(0, START_MONEY + int(11.7 * RATE)),
                    "money = points x RATE: %r" % derive(rec)["money"])
        # THE FLOOR (lost, lost, won, and the results screen counted
        # 0 -> 0 JAN). A loss stops at zero and is not
        # carried, so the win after it pays out in full.
        record_game(11, 3, 21000, -29.0)
        record_game(11, 3, 22400, -27.6)
        ok &= check(derive(load(11))["money"] == max(0, START_MONEY - int(56.6 * RATE)),
                    "two losses floor the balance: %r" % derive(load(11))["money"])
        rec11 = record_game(11, 0, 27500, 37.5)
        ok &= check(rec11["money_prev"] == max(0, START_MONEY - int(56.6 * RATE))
                    and rec11["money"] == rec11["money_prev"] + int(37.5 * RATE),
                    "...and the win pays in full from the floor, before %r after %r"
                    % (rec11["money_prev"], rec11["money"]))
        # A record from before the balance existed rebuilds it from the history
        # with the same floor, not from the lifetime sum (which says 0 here).
        legacy = load(11)
        legacy["money"] = legacy["money_prev"] = None
        ok &= check(money_balance(legacy) == rec11["money"]
                    and (START_MONEY or max(0, money_of(legacy)) == 0),
                    "a pre-balance record replays to %r, not the old 0"
                    % money_balance(legacy))
        os.remove(stats_file(11))       # the ranking checks below count records
        # A WIN YESTERDAY IS NOT "TODAY":
        # the windows are rolled when READ, not only when the next game lands.
        record_game(11, 0, 32700, 42.7, when=time.time() - 8 * 86400)
        d11 = derive(load(11))
        ok &= check(d11["money_today"] == 0 and d11["money_weekly"] == 0
                    and d11["money_best"] == int(42.7 * RATE),
                    "a game 8 days old is not today's or this week's: %r"
                    % ({k: d11[k] for k in ("money_today", "money_weekly",
                                            "money_best")},))
        os.remove(stats_file(11))
        ok &= check(jan_for_points(45.5) == int(45.5 * RATE),
                    "jan_for_points scales by RATE")
        ok &= check(0 <= derive(rec)["rank"] <= RANK_MAX,
                    "rank stays inside the 95-tier ladder")
        ok &= check(rank_index_of(load(9)) == 0 * 5 + 1,
                    "2 games, plus money -> tier 0 'Rich Rookie' (1): %r"
                    % rank_index_of(load(9)))

        set_override(9, "level", 42)
        ok &= check(derive(load(9))["level"] == 42 and level(9) == 42,
                    "an override wins, on both APIs")
        set_override(9, "level", None)
        ok &= check(derive(load(9))["level"] == 1, "and clears")
        set_override(9, "rank", 94)
        ok &= check(derive(load(9))["rank"] == 94, "a rank override wins")
        set_override(9, "rank", None)

        # --- the yaku counters and the yakuman count ---------------------
        record_win(9, ["Riichi", "Menzen Tsumo", "pinfu", "dragon"])
        record_win(9, ["国士無双"], yakuman=False)
        record_win(9, ["tanyao"], yakuman=True)
        r = load(9)
        ok &= check(r["yaku"].get("riichi") == 1 and r["yaku"].get("menzen_tsumo") == 1
                    and r["yaku"].get("pinfu") == 1 and r["yaku"].get("yakuhai") == 1
                    and r["yaku"].get("kokushi") == 1 and r["yaku"].get("tanyao") == 1,
                    "every spelling lands on its canonical key: %r" % (r["yaku"],))
        ok &= check(r["wins"] == 3 and r["yakuman"] == 2,
                    "wins 3, yakuman 2 (one by name, one by flag): %r/%r"
                    % (r["wins"], r["yakuman"]))
        d = derive(r)
        ok &= check(d["yaku_riichi"] == 1 and d["yaku_kokushi"] == 1
                    and d["yaku_chinitsu"] == 0,
                    "derive exposes yaku_<key> for every save slot")
        ok &= check(len([k for k in d if k.startswith("yaku_")]) == len(YAKU_OFFSETS)
                    == 39, "39 yaku counters: 38 read slots + houtei's intended one")
        ok &= check(len(set(YAKU_OFFSETS.values())) == 39
                    and all(0x11C <= o <= 0x1C8 for o in YAKU_OFFSETS.values())
                    and YAKU_OFFSETS["houtei"] == 0x130,
                    "the 39 offsets are distinct and inside +0x11C..+0x1C8")
        ok &= check(canonical_yaku("Chiitoitsu") == "chiitoi"
                    and canonical_yaku("nonsense") == "nonsense",
                    "unknown names are kept, not dropped")

        # --- the titles, by the record screen's thresholds ----------------
        m = 77
        for _ in range(99):
            record_game(m, 0, 40000, 20.0)
        ok &= check(load(m)["titles"].get("beast_king", 0) == 0
                    and getshogo(m) == [0, 0, 0, 0, 0],
                    "99 top finishes: no King of Beasts yet")
        record_game(m, 0, 40000, 20.0)
        ok &= check(load(m)["titles"].get("beast_king") == 1
                    and getshogo(m)[1] == 1 and shogo(m)[1] == 1,
                    "the 100th top finish awards King of Beasts: %r"
                    % (load(m)["titles"],))
        ok &= check(load(m)["titles"].get("winnings_general") == 5,
                    "100 plus games = Winnings General x5: %r"
                    % (load(m)["titles"],))
        # Mahjong King: 10000 JAN in a day, once per day
        m2 = 78
        record_game(m2, 0, 30000, 5.0, when=1_700_000_000)
        ok &= check(getshogo(m2)[0] == 0, "5 points (5000 JAN) is not a king")
        record_game(m2, 0, 30000, 6.0, when=1_700_000_100)
        ok &= check(getshogo(m2)[0] == 1 and shogo(m2)[0] == 1,
                    "11 points today (11000 JAN) awards Mahjong King")
        record_game(m2, 0, 30000, 6.0, when=1_700_000_200)
        ok &= check(getshogo(m2)[0] == 0 and shogo(m2)[0] == 1,
                    "...only once that day")
        record_game(m2, 0, 30000, 12.0, when=1_700_000_000 + 86400 * 2)
        ok &= check(shogo(m2)[0] == 2, "...and again the next day")
        # Wild Tile King every 5 lasts; Bust General on a bust
        m3 = 79
        for i in range(5):
            record_game(m3, 3, 5000, -30.0)
        ok &= check(shogo(m3)[4] == 1 and getshogo(m3)[4] == 1,
                    "5 last places = Wild Tile King")
        record_game(m3, 3, -2000, -50.0)
        ok &= check(getshogo(m3) == [0, 0, 1, 0, 0] and load(m3)["busts"] == 1,
                    "a bust awards Bust General: %r" % getshogo(m3))
        ok &= check(level_up(m) == 0 or level_up(m) >= 0, "level_up is a delta")
        m4 = 80
        for _ in range(LEVEL_GAMES - 1):
            record_game(m4, 1, 26000, 5.0)
        ok &= check(level(m4) == 1 and level_up(m4) == 0, "one short of a level")
        record_game(m4, 1, 26000, 5.0)
        ok &= check(level(m4) == 2 and level_up(m4) == 1,
                    "the LEVEL_GAMES-th game levels up: %d/%d"
                    % (level(m4), level_up(m4)))

        # --- the profile / rank record -----------------------------------
        f = profile_fields(9, name="PS2Tester", content_id=1000000903,
                           show_flags={0: 0, 4: 1}, prev_ranks={0: 2})
        ok &= check(f[2] == "PS2Tester" and f[17] == 2 and f[32] == 1
                    and f[25] == 2 and f[26] == rank_index_of(load(9)),
                    "profile fields: name, games, level, yakuman, rank")
        ok &= check(f[14] == round(rating_of(load(9)) * FLOAT_SCALE, 6)
                    and f[12] == round(2.5 * FLOAT_SCALE, 6),
                    "floats go out x FLOAT_SCALE (avg place 2.5 -> %d): %r"
                    % (int(round(2.5 * FLOAT_SCALE)), f[12]))
        ok &= check(f[41] == 0 and f[45] == 1 and 43 not in f,
                    "show flags land at v41+k; unset stay unset (= shown)")
        ok &= check(f[35] == 2 and f[36] == 0xFF,
                    "previous ranks: cat 0 = 2, others -1 ('New') as 0xFF")
        blob = pack_profile(f)
        ok &= check(len(blob) == PROFILE_REC == 232, "a profile row is 232 bytes")
        ok &= check(blob[:9] == b"PS2Tester" and blob[9] == 0,
                    "name at +0x00, NUL-terminated")
        ok &= check(struct.unpack_from("<Q", blob, 0x10)[0] == 1000000903,
                    "content id at +0x10")
        ok &= check(abs(struct.unpack_from("<f", blob, 0x88)[0] - float(f[14]))
                    <= abs(float(f[14])) * 1e-6
                    and struct.unpack_from("<f", blob, 0x80)[0]
                    == float(2.5 * FLOAT_SCALE),
                    "floats at +0x80.. as float32 holding the scaled value")
        ok &= check(struct.unpack_from("<I", blob, 0x94)[0] == 2
                    and struct.unpack_from("<H", blob, 0xB4)[0] == f[26]
                    and struct.unpack_from("<H", blob, 0xB2)[0] == 2
                    and blob[0xC0] == 1,
                    "games +0x94 u32, rank +0xb4 u16, yakuman +0xb2, level +0xc0")
        ok &= check(struct.unpack_from("<H", blob, 0xB0)[0] == f[24],
                    "update week at +0xb0")
        ok &= check(blob[0xC3] == 2 and blob[0xC4] == 0xFF,
                    "prev ranks at +0xc3..+0xc7")
        ok &= check(blob[0xC9] == 0 and blob[0xCD] == 1,
                    "show flags at +0xc9 + k")

        # --- the rank list -----------------------------------------------
        named = {m: "P%d" % m for m in all_members()}
        named[9] = "PS2Tester"
        for cat in range(5):
            rows = rank_list(cat, names=named)
            ok &= check(all(int(rec_.get("games_played", 1)) for _m, rec_ in
                            ((mm, load(mm)) for mm, _f in rows)),
                        "only players with games are ranked (cat %d)" % cat)
        rows = rank_list(0)
        order = [m for m, _f in rows]
        ok &= check(order[0] == 77 and 9 in order and len(order) == 5,
                    "cat 0 (rating): the 100-win member leads: %r" % order)
        ok &= check(rows[0][1][35] == 0 & 0xFF and rows[0][1][35] != 0xFF,
                    "the second build knows the previous rank (0, '-')")
        rows2 = rank_list(2)
        ok &= check([m for m, _f in rows2][0] == 77,
                    "cat 2 (money) is sorted on v19")
        # a member the caller could not name (a deleted account) is not ranked;
        # with no names at all (the CLI) everyone still is
        gone = dict(named)
        gone[77] = None
        order_named = [m for m, _f in rank_list(0, names=gone, remember=False)]
        ok &= check(77 not in order_named and 9 in order_named
                    and len(order_named) == 4,
                    "an unnamed member is left off the list: %r" % order_named)
        rl = rank_list_blob(0, limit=2)
        ok &= check(len(rl) == 2 * PROFILE_REC, "the blob is N x 232, capped")
        ok &= check(rank_list_path(3) == "U/g/MJS_RANKLIST3"
                    and rank_category_of("U/g/MJS_RANKLIST3") == 3
                    and rank_category_of("U/g/MJS_RANKLIST") == 0
                    and rank_category_of("U/g/MJS_RANKLIST9") is None
                    and rank_category_of("U/g/TM0_RANKLIST2") is None
                    and is_rank_list_path("U/g/MJS_RANKLIST1"),
                    "the rank list paths round-trip and never match TM's")
        try:
            rank_list(7)
            ok &= check(False, "an unknown category must raise")
        except ValueError:
            pass
        ok &= check(note_name(9, "Fox") and load(9)["name"] == "Fox",
                    "note_name updates the stored name")

        # A corrupt file must degrade to blank, not raise into a live hand.
        with open(stats_file(9), "w", encoding="utf-8") as f_:
            f_.write("{ not json")
        ok &= check(load(9)["games_played"] == 0, "a corrupt file reads blank")

        try:
            record_game(9, 7, 0, 0)
            ok &= check(False, "an out-of-range place must raise")
        except ValueError:
            pass
    finally:
        if old is None:
            os.environ.pop("POL_RESOURCE_DIR", None)
        else:
            os.environ["POL_RESOURCE_DIR"] = old
        try:
            import janstore
            janstore.db.execute("DELETE FROM jan_rank_snapshot WHERE left(category, %s) = %s",
                                (len(_SNAPSHOT_SCOPE[0]), _SNAPSHOT_SCOPE[0]))
        except Exception:                                   # noqa: BLE001
            pass
        _SNAPSHOT_SCOPE[0] = old_scope

    print("janstats selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--show", metavar="MEMBER")
    ap.add_argument("--record", metavar="MEMBER")
    ap.add_argument("--place", type=int, default=0, help="ZERO-BASED, 0 = 1st")
    ap.add_argument("--score", type=int, default=25000)
    ap.add_argument("--result", type=float, default=0.0)
    ap.add_argument("--win", metavar="MEMBER",
                    help="record a win; --yaku names the yaku")
    ap.add_argument("--yaku", nargs="*", default=[])
    ap.add_argument("--ranklist", type=int, metavar="CATEGORY",
                    help="print the rank list for a category 0..4")
    ap.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                    help="pin a derived value (level/rank/money/rating); "
                         "VALUE 'none' clears it")
    ap.add_argument("--member", metavar="MEMBER", help="target for --set")
    a = ap.parse_args()

    if a.selftest or not (a.record or a.win or a.ranklist is not None or a.set
                          or a.show):
        return selftest()
    if a.record:
        rec = record_game(a.record, a.place, a.score, a.result)
        if rec is None:
            print("refused: %r is a bot id or empty" % a.record)
            return 1
        print(summary(a.record))
        return 0
    if a.win:
        rec = record_win(a.win, a.yaku)
        print("refused" if rec is None else "yaku now %r" % (rec["yaku"],))
        return 0
    if a.ranklist is not None:
        for i, (m, f) in enumerate(rank_list(a.ranklist, remember=False)):
            print("%3d  member %-6s %-16s %s=%s  games %d  Lv%d rank %d"
                  % (i + 1, m, f.get(2, ""), RANK_CATEGORIES[a.ranklist],
                     f.get(RANK_SORT_VALUE[a.ranklist]), f.get(17), f.get(32),
                     f.get(26)))
        return 0
    if a.set:
        if not a.member:
            print("--set needs --member")
            return 2
        key, val = a.set
        set_override(a.member, key, None if val.lower() == "none" else val)
        print(summary(a.member))
        return 0
    if a.show:
        rec = load(a.show)
        print(summary(a.show))
        print("  file    %s" % stats_file(a.show))
        print("  derived %r" % (derive(rec),))
        print("  recent  %r  (NO_RANK = %d)" % (recent_places(rec), NO_RANK))
        for h in rec["history"][:HISTORY_ROWS]:
            print("    place %d (prints as %d)  score %6d  result %+.1f"
                  % (h["place"], h["place"] + 1, h["score"],
                     h["result_x10"] / 10.0))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
