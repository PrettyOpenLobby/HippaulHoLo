#!/usr/bin/env python3
"""Janhourou's per-table RULE STORE -- what the master chose on the rules dialog.

WHY THIS EXISTS (2026-09-04, audit finding 2): the rules were decorative. The
master pressed OK on 麻雀ルール設定, the client sent MjTBLCONFALL (opcode 13,
112 bytes), we answered MjMASTERCMDACK and DROPPED THE BODY. The rules blob
(`janlobby.mjs_table_info_blob`) took `values=` but its live caller never passed
any, and every `jangame.Table` was built on `janmahjong.Rules()` defaults -- so
9 of 19 mahjong rules were SERVED as one thing and PLAYED as another.

This module is the one place a table's 33 settings live. It is written on the
AUTH band (MjTBLCONFALL arrives there) and read on the LOBBY band (the
`b/g/MJSTableInfoSub` fetch is served there), so like `janseats` it persists to
a JSON file next to the seat store, atomically, and readers re-read on mtime.

=== THE WIRE BODY, READ OFF THE CLIENT'S OWN ENCODER =======================

`ruleset__00347da0(ctx, body)` (ruleset.c:801) fills the body the dialog sends:

    for each row of dialog A (40 slots, stride 0x80 at ctx+0xf9c):
        if row.present:                                   ctx+0xf9c
            body[0x39 + row.config_index] =               ctx+0xfa4
                ctx[0x44c + config_index*0x58 + row.selected]   ctx+0xfa8
    ...and the same for dialog B (ctx+0x23a4 / +0x23ac / +0x23b0)

`ctx+0x44c + idx*0x58` is the per-rule list of LEGAL VALUES that
`ruleset__003473b0` built from our blob's mask (the set-bit positions, in
order), so `[selected]` turns the cursor row back into the rule's VALUE -- the
same number our blob's +0x02 "current" byte carries. **One byte per config
index, at record +0x39 + index, 33 of them (+0x39..+0x59).**

`malloc__002c5050` (malloc.c:696) then builds the record around it:

    +0x08  u64   the TABLE id (its 2nd arg = ctx+0xa0, the PTL row's +0x00)
    +0x10  u16   0x70                    +0x12  0x0d = MjTBLCONFALL
    +0x13  ++DAT_00449058 & 0xf | 0x20   +0x14 -3   +0x15 -2   +0x16 5  +0x17 5
    +0x18  u64   the sender's PolID (DAT_004464e0)
    +0x20  2 x u64, +0x30 u32, +0x34 u16, +0x36..+0x38 bytes -- copied from
           caller registers; meaning unread, NOT parsed here
    +0x39  33 bytes  THE CONFIG VALUES, index = MJS_RULE_NAMES order
    +0x5a  17 bytes  a C string (strcpy) -- kept raw as `text`; probably the
           table password (LIMIT_PASS_WORD) but that is inference

Live capture agrees: 2026-08-16 "opcode 13, len 112, seq 0x21, sub 0x05".

=== VALUE SEMANTICS (the label arrays at JanHouRou.pex 0x40be50) =============

Dumped 2026-09-04 from the decrypted module, English from the translated
build; index == the value byte. Only the rules the ENGINE has a field for are
mapped into `janmahjong.Rules`; the rest are served back to the dialog and
kept for whoever adds them (IPPATSU/NOTEN/PINZUMO/YAKITORI/REACH_AFTER_KAN are
listed in `ENGINE_UNMAPPED` so the gap is visible, not silent).

    GENTEN   10000 15000 20000 25000 27000 30000
    RENCHAN  E:win S:win | E:win S:tenpai | E:win S:noten | E:tenpai S:tenpai
             | E:tenpai S:noten | E+S noten
    DORA     Front | Both(ura) | Front+Kan | All+Kan | All
    AKA_NUMn None 1 2 3 4       AKA_KIND 1..9 (the red rank; 5 = index 4)
    DOUBLE_RON / WAREME / HAKO / KIRIAGE / IPPATSU / KUITAN / PINZUMO /
    REACH_AFTER_KAN  None | Yes  (PINZUMO: "Tsumo 22 fu" | "With pinfu")
    YAKITORI None 5 10 20 points     NOTEN None, 3000..30000 on table
    KYOKUSU  Until East 1..4, South 1..4      UMA 0-0 5-10 10-20 10-30 20-60 30-90
    WAIT_TIME 10..60 sec   DISCONNECT_MODE Proxy | Tsumogiri | Quit
    DISCONNECT_ADJUST No | Yes    DISCONNECT_PENALTY None 10000 20000 30000
    GALLEY_LIMIT No | Yes | No chat only   TABLE_COMMENT 8 phrases
    LIMIT_* None | Yes/Stake

    POL_JAN_RULES=0          kill switch: parse+store become no-ops, every
                             table plays and serves the defaults
    POL_JAN_RULES_KEY        default jan:rules, the Valkey key the rule sets
                             are published under (janstore.Snapshot; it was
                             the file $POL_DATA_DIR/jan-rules.json). Live
                             state, like the seats they belong to.

=== THE KEY IS THE COMPOSITE TABLE ID (audit finding 30, 2026-09-04) =======

`store`/`values_for`/`rules_for`/`clear` key on whatever integer the caller
hands them, and every caller now hands `janseats.room_table_id(room, tid)`
-- `room << 16 | tid` -- so Room 1-1 table 1 and Room 2-3 table 1 keep two
rule sets. MjTBLCONFALL's +0x08 carries the 16-bit WIRE id (the PTL row's
+0x00, base 1..4); `janhourou._store_rules` resolves the room from the IRC
registry through `janseats.resolve_table` before storing. A bare id is room
0, the unknown room, exactly what a v1 store's keys "1".."4" already are.
"""
import json
import os
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janstore                                                 # noqa: E402
try:
    import janlobby                                             # noqa: E402
except ImportError:                                             # pragma: no cover
    janlobby = None
try:
    import janmahjong                                           # noqa: E402
except ImportError:                                             # pragma: no cover
    janmahjong = None

MjTBLCONFALL = 13
TBLCONFALL_LEN = 0x70
CONFIG_OFF = 0x39               # ruleset__00347da0: body[0x39 + config index]
CONFIG_COUNT = 33               # `while (i < 0x21)` everywhere the client walks them
TEXT_OFF = 0x5A                 # console__002825f0(auStack_1a6, body+0x5a, 0x11)
TEXT_LEN = 0x11

#: The 33 names in config-index order -- janlobby's list is the authority when
#: it is importable; this copy keeps the module standalone (selftest asserts
#: they agree).
RULE_NAMES = [
    "GENTEN", "RENCHAN", "DORA", "AKA_NUM1", "AKA_NUM2", "AKA_NUM3", "AKA_KIND",
    "DOUBLE_RON", "WAREME", "HAKO", "KIRIAGE", "YAKITORI", "IPPATSU", "KUITAN",
    "NOTEN", "PINZUMO", "KYOKUSU", "UMA", "REACH_AFTER_KAN", "TABLE_COMMENT",
    "GALLEY_LIMIT", "WAIT_TIME", "DISCONNECT_MODE", "DISCONNECT_ADJUST",
    "DISCONNECT_PENALTY", "LIMIT_PASS_WORD", "LIMIT_MONEY", "LIMIT_LEVEL",
    "LIMIT_SYOGO1", "LIMIT_SYOGO2", "LIMIT_SYOGO3", "LIMIT_SYOGO4", "LIMIT_SYOGO5",
]
if janlobby is not None:
    RULE_NAMES = list(janlobby.MJS_RULE_NAMES)

#: value index -> the number the label names (0x40be50, see the banner).
GENTEN_POINTS = (10000, 15000, 20000, 25000, 27000, 30000)
UMA_PAIRS = ((0, 0), (5, 10), (10, 20), (10, 30), (20, 60), (30, 90))
NOTEN_POINTS = (0, 3000, 6000, 9000, 12000, 15000, 18000, 21000, 24000, 27000, 30000)
YAKITORI_POINTS = (0, 5, 10, 20)
DISCONNECT_MODES = ("proxy", "tsumogiri", "quit")
DISCONNECT_PENALTY_POINTS = (0, 10000, 20000, 30000)

#: Rules the client offers that `janmahjong.Rules` has NO field for today.
#: Served back to the dialog faithfully; not played. Listed so the gap is a
#: fact in one place rather than a surprise on the table.
ENGINE_UNMAPPED = ("IPPATSU", "NOTEN", "PINZUMO", "YAKITORI", "REACH_AFTER_KAN",
                   "AKA_KIND", "TABLE_COMMENT", "GALLEY_LIMIT", "LIMIT_PASS_WORD",
                   "LIMIT_MONEY", "LIMIT_LEVEL", "LIMIT_SYOGO1", "LIMIT_SYOGO2",
                   "LIMIT_SYOGO3", "LIMIT_SYOGO4", "LIMIT_SYOGO5")

_LOCK = threading.RLock()
_TABLES = {}                    # table id (str) -> {"values": {name: int}, ...}
_OWNER = [False]
_ADOPTED = [False]
_SHARED = janstore.Snapshot(janstore.rules_key())


def enabled():
    return os.environ.get("POL_JAN_RULES", "1") == "1"


def _path():
    """The live key the rule sets are published under."""
    return janstore.rules_key()


def _read_file():
    """The published rules document ({} when there is none)."""
    _SHARED.key = _path()
    return _SHARED.read()


def _adopt():
    """Seed memory from the published document before the first write
    (setdefault, never overwrite) so a restart cannot publish emptiness over a
    table's rules."""
    if _ADOPTED[0]:
        return
    _ADOPTED[0] = True
    for tid, entry in (_read_file().get("tables") or {}).items():
        if isinstance(entry, dict):
            _TABLES.setdefault(str(tid), entry)


def _publish():
    _OWNER[0] = True
    try:
        with _LOCK:
            doc = json.loads(json.dumps({"tables": _TABLES}))
        _SHARED.key = _path()
        _SHARED.write(doc)
    except Exception as exc:                # noqa: BLE001 -- Valkey away
        janstore._whine("publishing the table rules", exc)   # memory still serves


def _live_tables():
    if _OWNER[0]:
        return _TABLES
    return (_read_file() or {}).get("tables", {})


# --- the wire body -----------------------------------------------------------

def default_values():
    """The 33 values a table plays before its master touches the dialog --
    `janlobby.MJS_RULE_DEFAULT`, which is kept in step with the engine."""
    base = dict(janlobby.MJS_RULE_DEFAULT) if janlobby is not None else {}
    return {name: int(base.get(name, 0)) for name in RULE_NAMES}


def _clamp(name, value):
    count = (janlobby.MJS_RULE_VALUE_COUNT.get(name, 64)
             if janlobby is not None else 64)
    return max(0, min(int(value), count - 1))


def parse_tblconfall(body):
    """The 33 `{name: value}` pairs (plus `table`, `polid`, `text`) out of an
    MjTBLCONFALL record.

    Accepts the whole 0x70-byte record. A body handed over WITHOUT its 0x18-byte
    header (0x58 bytes) is recognised by length and re-based. Raises ValueError
    on anything too short to hold the 33 values -- a short record is a
    different message, not a table with fewer rules.
    """
    rec = bytes(body)
    if len(rec) == TBLCONFALL_LEN - 0x18:
        rec = bytes(0x18) + rec
    if len(rec) < CONFIG_OFF + CONFIG_COUNT:
        raise ValueError("MjTBLCONFALL body is %d bytes; the 33 values end at "
                         "+0x%x" % (len(rec), CONFIG_OFF + CONFIG_COUNT))
    out = {}
    for i, name in enumerate(RULE_NAMES):
        out[name] = _clamp(name, rec[CONFIG_OFF + i])
    out["table"] = struct.unpack_from("<Q", rec, 0x08)[0] if len(rec) >= 0x10 else 0
    out["polid"] = struct.unpack_from("<Q", rec, 0x18)[0] if len(rec) >= 0x20 else 0
    raw = rec[TEXT_OFF:TEXT_OFF + TEXT_LEN]
    out["text"] = raw.split(b"\0", 1)[0].decode("cp932", "replace")
    return out


# --- the store ---------------------------------------------------------------

def store(table_id, values, by=0):
    """Remember `values` (a `parse_tblconfall` dict or a plain name->value map)
    for `table_id`. Returns the stored 33-value map."""
    if not enabled():
        return dict(values)
    table_id = int(table_id or 0)
    with _LOCK:
        _adopt()
        entry = _TABLES.setdefault(str(table_id), {})
        vals = entry.get("values") or {}
        for name in RULE_NAMES:
            if name in values:
                vals[name] = _clamp(name, values[name])
        entry["values"] = vals
        if "text" in values:
            entry["text"] = str(values["text"])
        entry["by"] = int(by or values.get("polid", 0) or 0)
        _publish()
        return dict(vals)


def clear(table_id):
    """Back to the defaults (a table whose master left takes its rules along
    -- the next master starts from the dialog's own defaults)."""
    with _LOCK:
        _adopt()
        if _TABLES.pop(str(int(table_id or 0)), None) is not None:
            _publish()
            return True
    return False


def password_for(table_id):
    """The 17-byte text the master's MjTBLCONFALL carried at +0x5a -- by
    inference the table password (LIMIT_PASS_WORD; see the banner) -- or ""
    when none was stored. A spectator's MjGALLEYREQ always carries "" at
    +0x44 (objstrings.c:2783-2830 is called with the empty string), so a
    non-empty password here is what turns a Watch into result 9."""
    if not enabled():
        return ""
    entry = (_live_tables() or {}).get(str(int(table_id or 0))) or {}
    return str(entry.get("text") or "")


def values_for(table_id):
    """All 33 values for `janlobby.mjs_table_info_blob(values=...)`: what the
    master stored, over the defaults for anything never set."""
    out = default_values()
    if not enabled():
        return out
    entry = (_live_tables() or {}).get(str(int(table_id or 0)))
    for name, v in ((entry or {}).get("values") or {}).items():
        if name in out:
            try:
                out[name] = _clamp(name, v)
            except (TypeError, ValueError):
                pass
    return out


def has_rules(table_id):
    """True if the master has stored rules for this table (not just defaults)."""
    if not enabled():
        return False
    return bool(((_live_tables() or {}).get(str(int(table_id or 0))) or {})
                .get("values"))


# --- the engine's view -------------------------------------------------------

def rules_kwargs(values):
    """`janmahjong.Rules(**kw)` for a 33-value map -- the existing fields only.

    The mapping is the label table (banner), rule by rule:

      genten        GENTEN_POINTS[v]
      uma           UMA_PAIRS[v] = (small, big) -> (+big, +small, -small, -big)
      hanchan       KYOKUSU >= 4 (a South round is in the game)
      aka_count     AKA_NUM1 + AKA_NUM2 + AKA_NUM3 (the Wall deals one red per
                    suit up to 3 -- see janmahjong.Wall; more is clamped there)
      kuitan / double_ron / hako / wareme / kiriage   value 1 = Yes
      renchan       RENCHAN in (3, 4): the dealer keeps the deal on a tenpai
                    draw in the EAST round; 0..2 (win-only) and 5 -> False.
                    The engine has one flag for both rounds.
      ura_dora      DORA in (1, 3, 4)      kan_dora  DORA in (2, 3, 4)
      wait_time     (WAIT_TIME + 1) * 10 seconds -- the client's own clock
                    label; jangame's deadline is derived from this SAME field,
                    so the two cannot drift (the fb24f81d lesson).
    """
    v = dict(default_values())
    v.update({k: int(x) for k, x in values.items() if k in v})
    small, big = UMA_PAIRS[_clamp("UMA", v["UMA"])]
    return {
        "genten": GENTEN_POINTS[_clamp("GENTEN", v["GENTEN"])],
        "uma": (big, small, -small, -big),
        "hanchan": v["KYOKUSU"] >= 4,
        "aka_count": v["AKA_NUM1"] + v["AKA_NUM2"] + v["AKA_NUM3"],
        "kuitan": v["KUITAN"] == 1,
        "double_ron": v["DOUBLE_RON"] == 1,
        "hako": v["HAKO"] == 1,
        "renchan": v["RENCHAN"] in (3, 4),
        "ura_dora": v["DORA"] in (1, 3, 4),
        "kan_dora": v["DORA"] in (2, 3, 4),
        "wait_time": (v["WAIT_TIME"] + 1) * 10,
        "wareme": v["WAREME"] == 1,
        "kiriage": v["KIRIAGE"] == 1,
    }


def rules_from_values(values):
    """A `janmahjong.Rules` for a 33-value map.

    Prefers `Rules.from_config(values)` when the engine grows one (it then owns
    the whole mapping, including the rules this module cannot express); until
    then the kwargs above. Returns None without janmahjong."""
    if janmahjong is None:
        return None
    from_config = getattr(janmahjong.Rules, "from_config", None)
    if callable(from_config):
        try:
            return from_config(values)
        except Exception:
            pass                                # fall back to the kwargs path
    return janmahjong.Rules(**rules_kwargs(values))


def rules_for(table_id):
    """THE THING `jangame.Manager.table_for_lobby` SHOULD PASS AS `rules=`.

    `Table(tid, channel=..., rules=janrules.rules_for(lobby_id))` -- one line
    at the construction site, and the hanchan plays what the dialog showed.
    """
    return rules_from_values(values_for(table_id))


def disconnect_policy(table_id):
    """(mode, adjust, penalty points) for the rejoin/disconnect ladder --
    config 22/23/24, served straight from the store."""
    v = values_for(table_id)
    return (DISCONNECT_MODES[_clamp("DISCONNECT_MODE", v["DISCONNECT_MODE"])],
            v["DISCONNECT_ADJUST"] == 1,
            DISCONNECT_PENALTY_POINTS[_clamp("DISCONNECT_PENALTY",
                                             v["DISCONNECT_PENALTY"])])


def describe(values):
    """One line for the log."""
    v = dict(default_values())
    v.update({k: int(x) for k, x in values.items() if k in v})
    kw = rules_kwargs(v)
    return ("genten %d uma %s %s aka %d kuitan %s dora ura=%s kan=%s "
            "double_ron %s hako %s renchan %s wait %ds"
            % (kw["genten"], "/".join(str(u) for u in kw["uma"]),
               "hanchan" if kw["hanchan"] else "tonpuu", kw["aka_count"],
               kw["kuitan"], kw["ura_dora"], kw["kan_dora"], kw["double_ron"],
               kw["hako"], kw["renchan"], kw["wait_time"]))


# --- selftest ----------------------------------------------------------------

def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print("%-62s %s" % (name, "ok" if cond else "FAIL"))
        ok = ok and bool(cond)

    check("33 rule names, in config-index order", len(RULE_NAMES) == 33
          and RULE_NAMES[0] == "GENTEN" and RULE_NAMES[17] == "UMA"
          and RULE_NAMES[21] == "WAIT_TIME" and RULE_NAMES[32] == "LIMIT_SYOGO5")
    if janlobby is not None:
        check("the names agree with janlobby.MJS_RULE_NAMES",
              RULE_NAMES == list(janlobby.MJS_RULE_NAMES))
        check("every label table here fits its measured value count",
              len(GENTEN_POINTS) == janlobby.MJS_RULE_VALUE_COUNT["GENTEN"]
              and len(UMA_PAIRS) == janlobby.MJS_RULE_VALUE_COUNT["UMA"]
              and len(NOTEN_POINTS) == janlobby.MJS_RULE_VALUE_COUNT["NOTEN"]
              and len(YAKITORI_POINTS) == janlobby.MJS_RULE_VALUE_COUNT["YAKITORI"]
              and len(DISCONNECT_MODES)
              == janlobby.MJS_RULE_VALUE_COUNT["DISCONNECT_MODE"]
              and len(DISCONNECT_PENALTY_POINTS)
              == janlobby.MJS_RULE_VALUE_COUNT["DISCONNECT_PENALTY"])

    # --- the wire body, built the way ruleset__00347da0 + malloc__002c5050
    #     build it: values at +0x39+idx, table at +0x08, PolID at +0x18.
    rec = bytearray(TBLCONFALL_LEN)
    struct.pack_into("<H", rec, 0x10, TBLCONFALL_LEN)
    rec[0x12] = MjTBLCONFALL
    rec[0x13] = 0x21
    rec[0x17] = 5
    struct.pack_into("<Q", rec, 0x08, 2)                 # table 2
    struct.pack_into("<Q", rec, 0x18, 0x5B01E2BB73B3B60B)  # the sender's PolID
    want = {"GENTEN": 5, "RENCHAN": 3, "DORA": 3, "AKA_NUM1": 1, "AKA_NUM2": 1,
            "AKA_NUM3": 1, "AKA_KIND": 4, "DOUBLE_RON": 1, "HAKO": 0,
            "KUITAN": 0, "KYOKUSU": 3, "UMA": 4, "WAIT_TIME": 0,
            "DISCONNECT_MODE": 2, "DISCONNECT_PENALTY": 3}
    for name, v in want.items():
        rec[CONFIG_OFF + RULE_NAMES.index(name)] = v
    rec[TEXT_OFF:TEXT_OFF + 4] = b"pw1\0"
    p = parse_tblconfall(bytes(rec))
    check("the 33 values are read at +0x39 + config index",
          all(p[n] == v for n, v in want.items()))
    check("an untouched index reads 0", p["IPPATSU"] == 0 and p["LIMIT_SYOGO5"] == 0)
    check("the table id is +0x08 and the PolID +0x18",
          p["table"] == 2 and p["polid"] == 0x5B01E2BB73B3B60B)
    check("the +0x5a string is kept", p["text"] == "pw1")
    check("a header-less 0x58-byte body is re-based",
          parse_tblconfall(bytes(rec[0x18:]))["UMA"] == 4)
    try:
        parse_tblconfall(b"\0" * 0x30)
        check("a short record raises", False)
    except ValueError:
        check("a short record raises", True)
    rec2 = bytearray(rec)
    rec2[CONFIG_OFF + RULE_NAMES.index("UMA")] = 200
    check("an out-of-range value is clamped to the rule's real count",
          parse_tblconfall(bytes(rec2))["UMA"] == len(UMA_PAIRS) - 1)

    # --- the engine mapping ----------------------------------------------
    kw = rules_kwargs(p)
    check("GENTEN 5 -> 30000 points", kw["genten"] == 30000)
    check("UMA 4 ('20-60') -> (+60, +20, -20, -60)", kw["uma"] == (60, 20, -20, -60))
    check("KYOKUSU 3 ('Until East 4') -> tonpuu", kw["hanchan"] is False)
    check("one red five per suit -> aka_count 3", kw["aka_count"] == 3)
    check("DORA 3 ('All+Kan') -> ura AND kan dora",
          kw["ura_dora"] and kw["kan_dora"])
    check("RENCHAN 3 ('E:tenpai') -> dealer repeats on tenpai", kw["renchan"])
    check("HAKO 0 / KUITAN 0 -> off", not kw["hako"] and not kw["kuitan"])
    check("WAIT_TIME 0 ('10 sec') -> wait_time 10", kw["wait_time"] == 10)
    kw0 = rules_kwargs({"DORA": 0, "RENCHAN": 0, "KYOKUSU": 7, "UMA": 2})
    check("DORA 0 ('Front') -> no ura, no kan", not kw0["ura_dora"] and not kw0["kan_dora"])
    check("RENCHAN 0 ('E:win') -> no tenpai repeat", not kw0["renchan"])
    check("KYOKUSU 7 ('Until South 4') -> hanchan", kw0["hanchan"])
    check("UMA 2 ('10-20') -> (+20, +10, -10, -20)", kw0["uma"] == (20, 10, -10, -20))
    check("disconnect ladder reads 22/23/24",
          DISCONNECT_MODES[2] == "quit" and DISCONNECT_PENALTY_POINTS[3] == 30000)

    # WARNING: THE DEFAULTS MUST BE THE ENGINE'S. The served dialog and the played
    # game come from the same 33 numbers now, so `Rules()` and
    # `rules_from_values(default_values())` have to agree field for field --
    # this is the check that catches janlobby.MJS_RULE_DEFAULT drifting.
    if janmahjong is not None and janlobby is not None:
        eng = janmahjong.Rules()
        dflt = rules_kwargs(default_values())
        for field, val in dflt.items():
            have = getattr(eng, field, None)
            check("default %s: dialog %r == engine %r" % (field, val, have),
                  val == have)
        r = rules_from_values(default_values())
        check("rules_from_values returns a Rules", isinstance(r, janmahjong.Rules))
        r2 = rules_from_values(p)
        check("...built from the master's values", r2.genten == 30000
              and r2.uma == (60, 20, -20, -60) and r2.wait_time == 10)

    # --- the store, across the container split --------------------------
    with tempfile.TemporaryDirectory() as td:
        os.environ["POL_JAN_RULES"] = "1"
        os.environ["POL_JAN_RULES_KEY"] = "jan:test:%s:rules" % os.path.basename(td)
        _TABLES.clear()
        _ADOPTED[0] = False
        _OWNER[0] = False
        _SHARED.forget()
        check("a fresh table serves the defaults",
              values_for(2) == default_values() and not has_rules(2))
        store(2, p)
        check("store keeps the 33 values", values_for(2)["UMA"] == 4
              and values_for(2)["GENTEN"] == 5 and has_rules(2))
        check("rules_for builds the engine's Rules from the store",
              janmahjong is None or rules_for(2).genten == 30000)
        check("disconnect_policy comes from the store",
              disconnect_policy(2) == ("quit", False, 30000))
        check("another table is untouched", values_for(1) == default_values())
        # a reader process (fresh module state) sees the file
        saved = dict(_TABLES)
        _TABLES.clear()
        _OWNER[0] = False
        _SHARED.forget()
        check("a reader process sees the published rules", values_for(2)["UMA"] == 4)
        # a restarted writer adopts rather than wipes
        _ADOPTED[0] = False
        store(3, {"GENTEN": 1})
        check("a restarted writer adopted the file (table 2 survived)",
              values_for(2)["UMA"] == 4 and values_for(3)["GENTEN"] == 1)
        check("clear() drops a table back to the defaults",
              clear(2) and values_for(2) == default_values())
        check("clear() of an unknown table is False", not clear(9))
        # finding 30: the same table NUMBER in two rooms is two rule sets
        _t101 = (101 << 16) | 1
        _t203 = (203 << 16) | 1
        store(_t101, {"UMA": 4})
        store(_t203, {"UMA": 1, "GENTEN": 0})
        check("Room 101 table 1 and Room 203 table 1 keep separate rules",
              values_for(_t101)["UMA"] == 4 and values_for(_t203)["UMA"] == 1
              and values_for(_t203)["GENTEN"] == 0
              and values_for(1) == default_values())
        check("rules_for builds each room's own Rules",
              janmahjong is None
              or (rules_for(_t101).uma == (60, 20, -20, -60)
                  and rules_for(_t203).genten == 10000))
        check("clearing one room's table leaves the other's",
              clear(_t101) and has_rules(_t203) and not has_rules(_t101))
        clear(_t203)
        _TABLES.clear()
        _TABLES.update(saved)
        os.environ["POL_JAN_RULES"] = "0"
        check("POL_JAN_RULES=0 serves the defaults whatever is stored",
              values_for(3) == default_values())
        os.environ.pop("POL_JAN_RULES", None)
        os.environ.pop("POL_JAN_RULES_KEY", None)
        _TABLES.clear()
        _ADOPTED[0] = False
        _OWNER[0] = False
        _SHARED.forget()

    check("describe() renders", "genten" in describe(p))
    print("\n%s" % ("ALL OK" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
