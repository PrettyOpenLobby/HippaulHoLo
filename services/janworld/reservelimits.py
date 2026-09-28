"""A table's entry limits (money, level, titles): who a reservation refuses with result 10."""
import os
from .deps import janrules, janseats
from . import opener, wirelog

# --- RESERVE LIMITS: result 10 for a table whose entry limits you miss ------
#
# The master's second rules dialog carries seven entry limits (rule indexes
# 26-32: LIMIT_MONEY, LIMIT_LEVEL, LIMIT_SYOGO1..5 -- "Money limit", "Level
# limit", "Title 1..5 limit" in the client's dialog strings). janrules stores
# them; this is where they are enforced. Result 10 is the client's own "Entry
# limits not met. Cannot reserve" (TableMenuPopup.c:536, string 0x196030).
#
# THE EXTRA BYTE. Project Crystal Server documents result 10's +0x19 as a
# bitfield of the unmet conditions in the order (Money, Level, Title), so
# bit 0 money, bit 1 level, bit 2 title here. That order is Crystal's label
# and UNVERIFIED: the 2002 build's result-10 arm shows a fixed string and
# never reads the byte, which lands in the seat global 0x4460e8 like every
# other RESERVEACK extra (TableMenuPopup.c:213). A later build may render it.
#
# THE THRESHOLDS ARE NOT DECODED. Each rule is stored as "None | Yes" (janrules
# clamps it to 0/1), and nothing we have read says what amount or level "Yes"
# demands. So the amounts are server knobs and the default is the weakest
# plain reading of each label:
#   Money limit on   -> JAN balance >= POL_JAN_LIMIT_MONEY_MIN (default 1)
#   Level limit on   -> janstats level >= POL_JAN_LIMIT_LEVEL_MIN (default 1,
#                       which every player meets -- set it to use it)
#   Title n limit on -> holds title n (count > 0), janstats.SHOGO_KEYS order,
#                       the order the client's title byte arrays index
#
# The table's own seated members are never refused (the master set the limits
# and re-sends on a missed ACK). POL_JAN_RESERVE_LIMITS=1 enables; default off.
RESERVE_LIMIT_MONEY, RESERVE_LIMIT_LEVEL, RESERVE_LIMIT_TITLE = 0x01, 0x02, 0x04
RESERVE_LIMIT_RESULT = 10           # == janseats.RESERVE_LIMIT


def _reserve_limits_on():
    return os.environ.get("POL_JAN_RESERVE_LIMITS", "0") == "1"


def reserve_limits_unmet(member_id, tid):
    """The unmet-limit bitfield for `member_id` reserving table `tid`, 0 when
    every limit is met, the table has none, or the check cannot run."""
    if janrules is None or not member_id:
        return 0
    try:
        vals = janrules.values_for(tid) or {}
    except Exception:
        return 0
    money = int(vals.get("LIMIT_MONEY") or 0)
    level = int(vals.get("LIMIT_LEVEL") or 0)
    titles = [int(vals.get("LIMIT_SYOGO%d" % n) or 0) for n in range(1, 6)]
    if not (money or level or any(titles)):
        return 0
    if janseats is not None:
        try:
            if any(m == int(member_id) for m, _s, _n, _p in janseats.seats_at(tid)):
                return 0
        except Exception:
            pass
    try:
        import janstats
        rec = janstats.load(member_id)
    except Exception as exc:
        wirelog.log("jan: reserve limits for member %s not checked (%r)" % (member_id, exc))
        return 0
    unmet = 0
    if money and janstats.money_balance(rec) < opener._env_int("POL_JAN_LIMIT_MONEY_MIN", 1):
        unmet |= RESERVE_LIMIT_MONEY
    if level and janstats.level_of(rec) < opener._env_int("POL_JAN_LIMIT_LEVEL_MIN", 1):
        unmet |= RESERVE_LIMIT_LEVEL
    if any(titles):
        held = rec.get("titles") or {}
        for n, want in enumerate(titles):
            if want and int(held.get(janstats.SHOGO_KEYS[n], 0) or 0) <= 0:
                unmet |= RESERVE_LIMIT_TITLE
                break
    return unmet
