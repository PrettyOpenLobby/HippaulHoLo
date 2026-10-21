#!/usr/bin/env python3
"""The gap audit, pinned: every fix it asked for, through the real
table, dispatch and store code, plus the two-human paths no live game has
exercised yet.

    python tools/jan_gaps_test.py          # exit 0 on success

Needs a PostgreSQL server for the player-record checks (jan_run_all gives it
a throwaway database); live state is this process's own in-memory store.
"""
import os
import struct
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402
jan_testenv.setup(need_core=False)

_TD = tempfile.mkdtemp(prefix="jangaps-")
os.environ["POL_LOG_DIR"] = os.path.join(_TD, "logs")
os.environ["POL_DATA_DIR"] = _TD
os.environ["POL_JAN_SEATS"] = "1"
os.environ["POL_JAN_SEATS_KEY"] = "jan:test:%s:seats" % os.path.basename(_TD)
os.environ["POL_JAN_RULES"] = "1"
os.environ["POL_JAN_RULES_KEY"] = "jan:test:%s:rules" % os.path.basename(_TD)
os.environ["POL_JAN_GMNOTICE_KEY"] = "jan:test:%s:gm" % os.path.basename(_TD)
for _k in ("POL_JAN_KAN_MOTION", "POL_JAN_GALLERY", "POL_JAN_SASHIUMA",
           "POL_JAN_RESERVE_LIMITS", "POL_JAN_RULES_RESET", "POL_JAN_NOTICECALL"):
    os.environ.pop(_k, None)
os.makedirs(os.environ["POL_LOG_DIR"], exist_ok=True)

import janwire                                                   # noqa: E402
import janmsgs as M                                              # noqa: E402
import janmsgs2004                                               # noqa: E402
import janmahjong as mj                                          # noqa: E402
import janrules                                                  # noqa: E402
import janseats                                                  # noqa: E402
import janlobby                                                  # noqa: E402
import jansave                                                   # noqa: E402
import janhourou                                                 # noqa: E402  the facade
from jantable import (bots, knobs, manager, narration, table,    # noqa: E402
                      tablesashiuma, selftest as tst)
from janworld import (dispatch, galley, gmnotice, notices,       # noqa: E402
                      opcodes, pushqueue, reservelimits, seating, social,
                      wirelog)

try:
    import janstats                                              # noqa: E402
    import janpg                                                 # noqa: E402
    HAVE_DB = bool(janpg.fresh_database())
except Exception:                                                # noqa: BLE001
    janstats, HAVE_DB = None, False

knobs.DEAL_DELAY = 0.0
knobs.SWEEPER = False
SASHIUMA_DEFAULT = tablesashiuma.SASHIUMA     # what the environment-free import chose
tablesashiuma.SASHIUMA = False                # READY must deal in these rigs
wirelog.LOG_DIR = os.environ["POL_LOG_DIR"]
_WIRELOG = wirelog.log
wirelog.log = lambda *a, **k: None           # the trace is not this suite's output

FAILS = []
COUNT = [0]


def check(label, cond, detail=""):
    COUNT[0] += 1
    print("  %-74s %s%s" % (label, "ok" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)
    return bool(cond)


def section(name):
    print("\n" + name)


def ops(recs):
    return [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in recs or ()]


def ready(t):
    return t.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                                 sub=3, length=0x18)[:0x18])


def fresh_stores():
    for mod in (janseats, janrules):
        mod._TABLES.clear()
        mod._ADOPTED[0] = True
        mod._OWNER[0] = True
    pushqueue._PUSH.clear()


def ask(op, table_id, member, f13=0x11, password=b"", polid=None):
    """One reserve-family request through the dispatcher, as the client sends
    it: the table at +0x08, the PolID at +0x18, the password at +0x44."""
    rec = bytearray(janwire.pack(opcode=op, f13=f13, src=1,
                                 dst=janwire.DST_SERVER & 0xFF, id8=table_id,
                                 length=0x108 if op == opcodes.MjPLAYREQ else 0x28,
                                 payload=(0x5B01E2BB73B3B600 + member
                                          if polid is None else polid)))
    rec += bytes((0x108 if op == opcodes.MjPLAYREQ else 0x28) - len(rec))
    if password:
        rec[0x44:0x44 + len(password)] = password
    out = dispatch.handle_line(janwire.encode(bytes(rec)), member_id=member)
    lines = out if isinstance(out, (list, tuple)) else ([out] if out else [])
    return [janwire.decode(l) for l in lines if l[:1] == b"B"]


def ack_of(recs, opcode=opcodes.MjRESERVEACK):
    return next((r for r in recs if r[0x12] == opcode), None)


def pushed(member):
    return [janwire.decode(l) for l in pushqueue._take_pushed(member) if l[:1] == b"B"]


def rig_turn(t, seat, tiles):
    """Make it `seat`'s turn with exactly `tiles` (14, the last one drawn)
    and hand back the MjTSUMO the client would answer."""
    k = t.kyoku
    h = k.hands[seat]
    h.tiles[:] = list(tiles)
    h.drawn = tiles[-1]
    k.turn = seat
    t.begin_routing()
    rec = t.msg_tsumo(seat)
    t.take_routes()
    return rec


def T(spec):
    return mj._hand_from(spec)


# --- 1. KUIKAE ---------------------------------------------------------------
section("1. kuikae: the client is told the forbidden kind, the suji is free")
t = table.Table(101, seed=7)
t.seat_player(0x1001, b"one", seat=0)
t.fill_with_bots()
ready(t)
k = t.kyoku
h = k.hands[0]
h.tiles[:] = T("34m") + T("111p") + T("789p") + T("5s") + T("123z") + T("6m")
h.melds = []
k.hands[3].pond.append(mj.MAN + 1)
k.last_discard_seat, k.last_discard = 3, mj.MAN + 1          # 2m
pair = [next(x for x in h.tiles if mj.kind(x) == mj.MAN + 2),
        next(x for x in h.tiles if mj.kind(x) == mj.MAN + 3)]
k.call_chi(0, pair)
check("retail rule: chi 3-4 on 2 forbids 2m only (no suji 5m)",
      k.kuikae_forbidden(0) == {mj.MAN + 1}, k.kuikae_forbidden(0))
t.begin_routing()
rec = t.msg_tsumo(0)
t.take_routes()
check("post-call MjTSUMO +0x62 names the called kind (2m) for the client's own "
      "refusal", rec[0x62] == (M.tile_byte(mj.MAN + 1) & 0x3F), rec[0x62])
check("...and +0x63 (auto-discard) stays 0, so the client tests it", rec[0x63] == 0)
r4 = janmsgs2004.tsumo_to_2004(rec)
check("2004 layout: the kind lands at +0x72, +0x73 = 0 (fn_002a7600 reads "
      "+0x72/+0x73)", r4[0x72] == rec[0x62] and r4[0x73] == 0, (r4[0x72], r4[0x73]))
# an ordinary draw turn carries 0 (a stale kind would refuse a plain discard)
t2 = table.Table(102, seed=8)
t2.seat_player(0x1002, b"two", seat=0)
t2.fill_with_bots()
ready(t2)
rr = rig_turn(t2, 0, T("123m456p789s1122z3z"))
check("an ordinary draw turn carries +0x62 = 0", rr[0x62] == 0, rr[0x62])
# the server refuses the genbutsu swap if one arrives, and takes the suji tile
_h0 = k.hands[0]                         # a 2m to try to throw back
_h0.tiles[_h0.tiles.index(mj.SOU + 4)] = mj.MAN + 1
_h0.sort()
t.begin_routing()
rec = t.msg_tsumo(0)
t.take_routes()
idx_2m = next(i for i, x in enumerate(k.hands[0].tiles) if mj.kind(x) == mj.MAN + 1)
out = t.handle(tst._client_sute(rec, 0, idx_2m))
check("a genbutsu kuikae discard that reaches the server is refused",
      any(e[0] == "refused-kuikae" for e in t.log) and ops(out)[-1:] == ["MjTSUMO"],
      ops(out))

# --- 2. RULES RESET ------------------------------------------------------------
section("2. a vacant table forgets its rules")
fresh_stores()
check("POL_JAN_RULES_RESET defaults to resetting", janseats._rules_reset_on())
janseats.reserve(5, 501, "a")
janrules.store(5, {"KYOKUSU": 0, "UMA": 4}, by=501)
check("the master's rules are stored", janrules.values_for(5)["KYOKUSU"] == 0)
janseats.reserve(5, 502, "b")
janseats.cancel(5, 501)                  # master leaves, the guest stays
check("a master leaving a table that still has a player keeps the rules",
      janrules.values_for(5)["KYOKUSU"] == 0 and janseats.master_of(5) == 502)
janseats.cancel(5, 502)                  # the table is empty
check("the last player leaving drops the table back to the defaults",
      janrules.values_for(5) == janrules.default_values()
      and janrules.values_for(5)["KYOKUSU"] == 7)
janseats.reserve(6, 601, "c")
janrules.store(6, {"KYOKUSU": 0}, by=601)
janseats._TABLES["6"]["seats"]["601"]["at"] = 1.0        # aged out, nobody wrote
janseats.reserve(6, 602, "d")
check("a first seat at a table whose seats all EXPIRED also starts from the "
      "defaults", janrules.values_for(6)["KYOKUSU"] == 7)
os.environ["POL_JAN_RULES_RESET"] = "never"
janseats.reserve(7, 701, "e")
janrules.store(7, {"KYOKUSU": 1}, by=701)
janseats.cancel(7, 701)
check("POL_JAN_RULES_RESET=never keeps the old behaviour",
      janrules.values_for(7)["KYOKUSU"] == 1)
os.environ.pop("POL_JAN_RULES_RESET")

# --- 3. TIME-UP WARNING --------------------------------------------------------
section("3. the seat time-up warning is in MINUTES")
fmt = "あなたが参加予約したテーブルは、あと%d分でキャンセルされます。"
tn = notices.timeup_notice(120)
check("120 s left reads 'あと2分' on the 2004 client (0x004b0440)",
      fmt % struct.unpack_from("<I", tn, 0x20)[0]
      == "あなたが参加予約したテーブルは、あと2分でキャンセルされます。")
check("1 s left still reads 1 minute, never the 0 = 'time ran out' line",
      notices.timeup_minutes(1) == 1 and notices.timeup_minutes(0) == 0)

# --- 4. DISCONNECT RULES -------------------------------------------------------
section("4. the table's disconnect rules are played")
fresh_stores()
tablesashiuma.SASHIUMA = False


def lobby_table(key, mode, adjust, penalty, members=None):
    members = members or ((key * 10 + 1, 0), (key * 10 + 2, 1))
    janrules.store(key, {"DISCONNECT_MODE": mode, "DISCONNECT_ADJUST": adjust,
                         "DISCONNECT_PENALTY": penalty}, by=members[0][0])
    m = manager.Manager(sweeper=False)
    t = m.table_for_lobby(key, [(mem, st, "p%d" % mem, 0x5B00 + mem)
                                for mem, st in members])
    ready(t)
    return m, t


m4, t4 = lobby_table(41, 0, 0, 0)
check("the Manager hands the table its rule", t4.disconnect == ("proxy", False, 0))
t4.drop_seat(1, "quit (MjBYE)")
check("proxy: a CPU plays the seat", type(t4.bots[1]) is bots.Bot)
m4, t4 = lobby_table(42, 1, 0, 0)
t4.drop_seat(1, "connection lost")
check("tsumogiri: the seat only discards its draws",
      isinstance(t4.bots[1], bots.TsumogiriProxy) and not t4.bots[1].may_win)
k4 = t4.kyoku
h4 = k4.hands[1]
h4.tiles[:] = T("123m456p789s11z") + T("2z")   # a complete hand, tsumo 2z? no: 11z pair
h4.drawn = h4.tiles[-1]
check("...it throws the drawn tile and never declares",
      t4.bots[1].choose_discard(k4) == h4.drawn and t4.bots[1].calls(k4, mj.MAN, {"ron": True}) is None
      and not t4.bots[1].wants_riichi(k4) and t4.bots[1].wants_kan(k4) is None)
m4, t4 = lobby_table(43, 2, 1, 1)                # quit, settled, 10000
t4.drop_seat(1, "quit (MjBYE)")
check("quit: the game ends when the hand it left in is over", t4._end_after_hand)
k4 = t4.kyoku
k4.abort(mj.SUUFON_RENDA)
out = t4.next_kyoku_or_end()
check("...the next settlement goes straight to the results", ops(out) == ["MjGAMERESULTHALF1"],
      ops(out))
half1 = out[0]
before = struct.unpack_from("<4i", half1, 0x58)
after = struct.unpack_from("<4i", half1, 0x68)
check("penalty 10000: the leaver's score drops by 10000 at the penalty stage",
      before[1] - after[1] == 10000, (before, after))
check("...and the three who stayed share it (zero sum)",
      sum(after) == sum(before) and all(after[s] > before[s] for s in (0, 2, 3)),
      (before, after))
check("the game's end reason says why", t4.game.end_reason == "disconnect")
if HAVE_DB:
    rec = janstats.load(432)
    check("the leaver's record carries the exit and its penalty",
          rec["disconnects"] == 1 and rec["penalty_points"] == 10000
          and rec["last_disconnect"]["mode"] == "quit", rec.get("last_disconnect"))
    check("DISCONNECT_ADJUST Yes: the leaver is settled too (a game recorded)",
          rec["games_played"] == 1)
m4, t4 = lobby_table(44, 0, 0, 2)
t4.drop_seat(1, "timed out 3 times")
t4.kyoku.abort(mj.SUUFON_RENDA)
t4.game.end_kyoku()
t4.game.finished = True
h1 = t4.msg_results()
b44, a44 = struct.unpack_from("<4i", h1, 0x58), struct.unpack_from("<4i", h1, 0x68)
check("penalty 20000 under proxy mode", b44[1] - a44[1] == 20000, (b44, a44))
if HAVE_DB:
    check("DISCONNECT_ADJUST No: the leaver gets no game, only the exit",
          janstats.load(442)["games_played"] == 0
          and janstats.load(442)["disconnects"] == 1)
m4, t4 = lobby_table(45, 2, 0, 0)
t4.drop_seat(1, "connection lost")
t4.rejoin(1)
check("a rejoin cancels the departure and a quit it triggered",
      not t4.departed and not t4._end_after_hand)
m4, t4 = lobby_table(46, 0, 0, 0)
t4.kyoku.abort(mj.SUUFON_RENDA)
t4.game.end_kyoku()
t4.game.finished = True
h1 = t4.msg_results()
check("nobody left: +0x58 == +0x68, so the client skips the stage",
      h1[0x58:0x68] == h1[0x68:0x78])

# --- 5. MjNOTICECALL -----------------------------------------------------------
section("5. 'your table is waiting' / 'the game can be started'")
fresh_stores()
a = ack_of(ask(opcodes.MjPLAYREQ, 2, 901))
check("the first reserver is master", a is not None and a[0x18] == 2)
check("...and is told nothing yet", pushed(901) == [])
got = ask(opcodes.MjPLAYREQ, 2, 902)
check("a guest's reply: its MjRESERVEACK, then MjNOTICECALL",
      [r[0x12] for r in got] == [opcodes.MjRESERVEACK, notices.MjNOTICECALL],
      [r[0x12] for r in got])
call = got[-1]
check("the guest's notice is +0x20 = 1 ('waiting for the game to start', bit 4)",
      struct.unpack_from("<I", call, 0x20)[0] == 1 and call[0x17] == 0)
mq = pushed(901)
check("the master gets +0x20 = 0 ('the game can be started', bit 0x80)",
      len(mq) == 1 and mq[0][0x12] == notices.MjNOTICECALL
      and struct.unpack_from("<I", mq[0], 0x20)[0] == 0)
again = ask(opcodes.MjPLAYREQ, 2, 902)
check("a re-sent reserve does not repeat the notices",
      [r[0x12] for r in again] == [opcodes.MjRESERVEACK] and pushed(901) == [])
cack = bytearray(janwire.pack(opcode=notices.MjNOTICECALLACK, length=0x18,
                              src=0xFD, dst=0xFE, sub=5, id8=2))[:0x18]
check("the client's own MjNOTICECALLACK is taken silently",
      dispatch.handle_line(janwire.encode(bytes(cack)), member_id=902) == [])

# --- 6. 2004-ONLY NOTICES 94/95/96 ---------------------------------------------
section("6. the in-room notice window and the title kick")
o = notices.gm_notice_open("Maintenance in 10 minutes.")
check("CmNOTICEMSGOPEN: opcode 94 ('^'), +0x18 = 0 (everyone), text at +0x2c",
      o[0x12] == 94 and struct.unpack_from("<Q", o, 0x18)[0] == 0
      and o[0x2C:0x2C + 26] == b"Maintenance in 10 minutes." and o[0x2C + 26] == 0)
check("...the length field is the record's", struct.unpack_from("<H", o, 0x10)[0] == len(o))
long_ = notices.gm_notice_open("あ" * 400)
check("a long text is cut to 255 bytes on a character boundary (buffer 0x180)",
      len(long_) <= 0x2C + 256 + 4 and long_[0x2C:].split(b"\0")[0].decode("cp932"))
check("close is opcode 95, kick 96, bare records",
      notices.gm_notice_close()[0x12] == 95 and notices.gm_kick()[0x12] == 96
      and len(notices.gm_kick()) == 0x20)
gmnotice.clear()
fresh_stores()
p = gmnotice.post("open", "Server restart at 12:00.")
recs = gmnotice.pending_for(951)
check("a post reaches a member on their next drain", [r[0x12] for r in recs] == [94])
check("...exactly once", gmnotice.pending_for(951) == [])
gmnotice.post("open", "for one player", member=952)
check("a member-addressed post goes to that member only",
      gmnotice.pending_for(951) == [] and [r[0x12] for r in gmnotice.pending_for(952)] == [94, 94])
gmnotice.post("open", "short", ttl=10, now=1000.0)
late = gmnotice.pending_for(954, now=2000.0)
check("an expired post is not delivered (the current ones still are)",
      late and all(r[0x2C:0x31] != b"short" for r in late))
janseats.reserve(9, 955, "k")
gmnotice.post("kick", member=955)
krec = [r[0x12] for r in gmnotice.pending_for(955)]
check("a kick is delivered as 96 and frees the member's seat",
      96 in krec and janseats.table_of(955) == 0, krec)
try:
    gmnotice.post("kick")
    check("a kick without a member is refused", False)
except ValueError:
    check("a kick without a member is refused", True)
lines = dispatch.take_pending(956)
check("the dispatcher's drain carries them (take_pending)",
      any(janwire.decode(l)[0x12] == 94 for l in lines if l[:1] == b"B"))

# --- 7. RETAIL FEATURES ON BY DEFAULT ------------------------------------------
section("7. kan animations, spectating, side bets and entry limits are on")
check("POL_JAN_KAN_MOTION defaults on", knobs.KAN_MOTION is True)
check("POL_JAN_GALLERY defaults on", galley.GALLERY_ENABLE is True)
check("POL_JAN_SASHIUMA defaults on (read at import)", SASHIUMA_DEFAULT is True)
check("POL_JAN_RESERVE_LIMITS defaults on", reservelimits._reserve_limits_on())
# one of each kan, through the real table, as the motion records
for spec, kind_, motion in (("1111m234p567s11z22z", "ankan", 7),):
    tk = table.Table(110, seed=9)
    tk.seat_player(0x1100, b"kan", seat=0)
    tk.fill_with_bots()
    ready(tk)
    rec = rig_turn(tk, 0, T(spec))
    outk = tk.handle(tst._client_sute(rec, 0, 0, action=M.SUTE_KAN))
    check("a human %s rides ONE motion-%d MjALLDATA, then MjTSUMO motion 0"
          % (kind_, motion),
          ops(outk) == ["MjALLDATA", "MjTSUMO"] and outk[0][0x18] == motion
          and outk[1][0x18] == 0, (ops(outk), [r[0x18] for r in outk]))
    check("...the rinshan tile rides the kan record at +0x1c (the client flies it)",
          outk[0][0x1C] == M.tile_byte(tk.kyoku.hands[0].drawn))
    check("...no tile byte is the back id 0x3F",
          all((b & 0x3F) != M.WIRE_BACK for b in
              outk[0][M.ALLDATA_HAND:M.ALLDATA_HAND + 4 * M.ALLDATA_HAND_STRIDE] if b))
tdk = table.Table(111, seed=10)
tdk.seat_player(0x1110, b"dmk", seat=0)
tdk.fill_with_bots()
ready(tdk)
kk = tdk.kyoku
kk.hands[0].tiles[:] = T("555p") + T("123m789s1122z3z")
kk.hands[0].drawn = None
kk.hands[3].pond.append(mj.PIN + 4)
kk.last_discard_seat, kk.last_discard = 3, mj.PIN + 4
offer = tdk.msg_naki(mj.PIN + 4, {0: {"kan": True, "pon": True}})
outd = tdk.handle(tst._client_naki(offer, 0, M.NAKI_KAN | M.tile_u16(mj.PIN + 4)))
check("a human daiminkan rides ONE motion-6 MjALLDATA then MjTSUMO motion 0",
      ops(outd)[:2] == ["MjALLDATA", "MjTSUMO"] and outd[0][0x18] == 6
      and outd[1][0x18] == 0, (ops(outd), [r[0x18] for r in outd]))
# spectating answers 10 (the master's GALLEY_LIMIT) rather than 7 now
fresh_stores()
ask(opcodes.MjPLAYREQ, 3, 961)
g = ack_of(ask(opcodes.MjGALLEYREQ, 3, 962, f13=0x05))
check("a Watch on a table whose master never allowed one is 10, not 7",
      g is not None and g[0x18] in (janseats.GALLEY_LIMITED, janseats.GALLEY_NOT_PLAYING)
      and g[0x18] != galley.GALLEY_REFUSED, g and g[0x18])

# --- 8. PASSWORD TABLES --------------------------------------------------------
section("8. a password table answers 9 to the wrong password")
fresh_stores()
ask(opcodes.MjPLAYREQ, 4, 971)
janrules.store(4, {"LIMIT_PASS_WORD": 1, "text": "pw12"}, by=971)
csv = janlobby.table_params_text_for(4).split(",")
check("the PTL row's HasPassword field (CSV 9) is 1, so the client asks",
      csv[9] == "1", csv)
check("a table without one serves 0", janlobby.table_params_text_for(1).split(",")[9] == "0")
bad = ack_of(ask(opcodes.MjPLAYREQ, 4, 972, password=b"nope"))
check("the wrong password is result 9 (ReservePassWordError) and no seat",
      bad is not None and bad[0x18] == 9 and janseats.table_of(972) == 0)
none = ack_of(ask(opcodes.MjPLAYREQ, 4, 973))
check("no password at all is 9 too", none is not None and none[0x18] == 9)
good = ack_of(ask(opcodes.MjPLAYREQ, 4, 974, password=b"pw12"))
check("the right one seats the player (1)", good is not None and good[0x18] == 1)
again = ack_of(ask(opcodes.MjPLAYREQ, 4, 971))
check("the master re-affirming without one is still 2", again[0x18] == 2)
for mem in (971, 974):
    janseats.cancel(4, mem)
check("an emptied table loses its password with its rules",
      janlobby.table_params_text_for(4).split(",")[9] == "0")

# --- 9. THE 2004 STATISTICS ----------------------------------------------------
section("9. the ten 2004 save statistics are served")
check("the ten offsets are the page's (fn_0032fcc0)",
      sorted(jansave.STATS_2004.values()) == [0x2B4, 0x2BC, 0x2C0, 0x2C4, 0x2C8,
                                              0x2CC, 0x2E4, 0x2E8, 0x2EC, 0x2F0])
blob = jansave.to_2004(bytes(jansave.SIZE))
vals = {k_: i + 1 for i, k_ in enumerate(sorted(jansave.STATS_2004))}
b4 = jansave.apply_stats_2004(blob, vals)
check("apply_stats_2004 writes each as a u32 at its offset",
      all(struct.unpack_from("<I", b4, off)[0] == vals[k_]
          for k_, off in jansave.STATS_2004.items()))
check("...and leaves the magic and length alone",
      len(b4) == len(blob) and b4[:8] == blob[:8])
if HAVE_DB:
    tsx = table.Table(120, seed=11)
    tsx.seat_player(0x1200, b"w", seat=0, member=1201)
    tsx.seat_player(0x1300, b"d", seat=1, member=1202)
    tsx.fill_with_bots()
    ready(tsx)
    ks = tsx.kyoku
    hw = ks.hands[0]
    hw.tiles[:] = T("123456789m123p9s")
    hw.menzen, hw.riichi, hw.riichi_index = True, True, 4
    ks.hands[1].pond.append(mj.SOU + 8)
    ks.last_discard_seat, ks.last_discard = 1, mj.SOU + 8
    wins = ks.win_ron([0])
    tsx.on_wins(wins, ron_from=1)
    tsx._record_hand_stats()
    tsx._record_hand_stats()            # a re-ack arriving twice
    d0 = janstats.derive(janstats.load(1201))
    d1 = janstats.derive(janstats.load(1202))
    check("the winner: 1 hand, 1 win, its points, a riichi on turn 5",
          d0["hand_hands"] == 1 and d0["hand_wins"] == 1
          and d0["hand_win_points"] > 0 and d0["hand_win_max"] == d0["hand_win_points"]
          and d0["hand_riichi"] == 1 and d0["hand_riichi_junme"] == 5, d0)
    check("the discarder: 1 hand, 1 deal-in", d1["hand_hands"] == 1
          and d1["hand_deal_ins"] == 1 and d1["hand_wins"] == 0, d1)
    tsx.next_kyoku_or_end()             # the settlement: still once
    check("a hand is counted once, whatever re-acks arrive",
          janstats.derive(janstats.load(1201))["hand_hands"] == 1)

# --- 10. THE TRACE ---------------------------------------------------------------
section("10. the trace: dora indicator, table rules list, client kind, rotation")
said = []
_old = narration.TRACE if hasattr(narration, "TRACE") else None
import jangame                                                   # noqa: E402
jangame.TRACE = said.append
tt = table.Table(130, seed=12)
tt.seat_player(0x1300, b"t", seat=0)
tt.fill_with_bots()
ready(tt)
deal = next((l for l in said if "DEAL" in l), "")
ind = narration._tile(tt.kyoku.wall.dora_indicators()[0])
check("DEAL names the real indicator, not the count printed as a tile",
      ("dora=%s " % ind) in deal, deal)
jangame.TRACE = None
check("ENGINE_UNMAPPED lists table rules only (all 19 mahjong rules are played)",
      not set(janrules.ENGINE_UNMAPPED) & set(janrules.RULE_NAMES[:19])
      and "IPPATSU" not in janrules.ENGINE_UNMAPPED)
check("a tagged auth-band peer is never drawn as a name",
      seating.display_name(0, "auth-band[m3 PS2/P2U 20260921_J 2004]") == "")
os.environ["POL_JAN_LOG_MAX_MB"] = "0.001"
import contextlib                                                # noqa: E402
import io                                                        # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    for i in range(40):
        _WIRELOG("rotation line %d %s" % (i, "x" * 60))
logs = sorted(os.listdir(wirelog.LOG_DIR))
check("janhourou.log rotates at the cap (POL_JAN_LOG_MAX_MB)",
      "janhourou.log.1" in logs and os.path.getsize(
          os.path.join(wirelog.LOG_DIR, "janhourou.log")) < 4096, logs)
check("...keeping POL_LOG_KEEP generations", "janhourou.log.4" not in logs)
os.environ.pop("POL_JAN_LOG_MAX_MB")

# --- 11. TWO HUMANS --------------------------------------------------------------
section("11. two-human paths through the real table")
fresh_stores()
tablesashiuma.SASHIUMA = False
# a whole two-human table through the dispatcher: reserve, master, guest
ask(opcodes.MjPLAYREQ, 1, 1001)
ask(opcodes.MjPLAYREQ, 1, 1002)
pushed(1001)
# chat relay: the 'A' line from one reaches the other
chat = janwire.chat_line(janwire.pack(opcode=janwire.MjISAY, length=0x80), b"hello", b"")
dispatch.handle_line(chat, member_id=1001)
q = pushqueue._take_pushed(1002)
check("table chat: one human's line is relayed to the other", any(l[:1] == b"A" and b"hello" in l for l in q), q)
# master hand-over: the master cancels, the guest is promoted and told
ask(opcodes.MjPLAYCANCEL, 1, 1001)
q = pushed(1002)
promo = [r for r in q if r[0x12] == notices.MjNOTICEMEMBER]
check("master hand-over: the guest becomes master and gets MjNOTICEMEMBER "
      "with +0x54 = its own seat",
      janseats.master_of(1) == 1002 and promo and promo[-1][0x54] == 1, [r[0x12] for r in q])
# kick: the new master removes a guest
ask(opcodes.MjPLAYREQ, 1, 1003)
pushed(1002)
ban = bytearray(janwire.pack(opcode=opcodes.MjMEMBERBANISH, f13=0x22, src=1,
                             dst=janwire.DST_SERVER & 0xFF, id8=1, length=0x30,
                             payload=0x5B01E2BB73B3B600 + 1002))
ban += bytes(0x30 - len(ban))
struct.pack_into("<Q", ban, opcodes.BANISH_TARGET_OFF, 0x5B01E2BB73B3B600 + 1003)
dispatch.handle_line(janwire.encode(bytes(ban)), member_id=1002)
kq = pushed(1003)
bn = [r for r in kq if r[0x12] == notices.MjNOTICEBANISH]
check("kick: the guest's seat is freed and its MjNOTICEBANISH names it at +0x20",
      janseats.table_of(1003) == 0 and bn
      and struct.unpack_from("<Q", bn[0], 0x20)[0] == 0x5B01E2BB73B3B600 + 1003)

# the game itself with two humans
mgr = manager.Manager(sweeper=False)
tg = mgr.table_for_lobby(77, [(2001, 0, "h0", 0x2001), (2002, 1, "h1", 0x2002)])
ready(tg)
kg = tg.kyoku
check("two humans seated, two bots", tg.live == {0, 1} and len(tg.bots) == 2)
# quit to CPU: seat 1 says MjBYE mid-hand
bye = janwire.pack(opcode=M.MjBYE, f13=0x40, src=1, dst=4, f16=0, sub=3,
                   length=0x18)[:0x18]
mgr.handle(bye, member_id=2002)
q0 = mgr.pending_for_member(2001)
check("quit: seat 1 becomes a CPU and seat 0 is told (MjMEMBERLEAVE)",
      1 in tg.bots and 1 not in tg.live and "MjMEMBERLEAVE" in ops(q0), ops(q0))

# double ron by two humans: both answer, two win screens, both paid
td = table.Table(140, seed=13)
td.seat_player(0x1401, b"a", seat=0)
td.seat_player(0x1402, b"b", seat=2)
td.fill_with_bots()
ready(td)
kd = td.kyoku
kd.rules.double_ron = True
for s, spec in ((0, "123456789m123p9s"), (2, "123456789p123m9s")):
    hd = kd.hands[s]
    hd.tiles[:] = T(spec)
    hd.melds, hd.menzen, hd.riichi, hd.riichi_index = [], True, True, 0
kd.hands[3].pond.append(mj.SOU + 8)
kd.last_discard_seat, kd.last_discard = 3, mj.SOU + 8
before = list(kd.scores)
offer = td.msg_naki(mj.SOU + 8, {0: {"ron": True}, 2: {"ron": True}})
first = td.handle(tst._client_naki(offer, 2, M.NAKI_RON))
second = td.handle(tst._client_naki(offer, 0, M.NAKI_RON))
check("double ron (two humans): the first answer waits for the second",
      first == [], ops(first))
check("...then the first win screen goes out", "MjYAKUDISP" in ops(second), ops(second))
check("...both winners are paid", kd.scores[0] > before[0] and kd.scores[2] > before[2],
      (before, list(kd.scores)))
nxt = td._ack_advance(M.MjYAKUDISP)
check("...and the screen's ack pulls the second winner's", "MjYAKUDISP" in ops(nxt), ops(nxt))

# sanchahou: three humans ron one tile -> abortive draw
t3 = table.Table(141, seed=14)
for s in (0, 1, 2):
    t3.seat_player(0x1500 + s, b"r", seat=s)
t3.fill_with_bots()
ready(t3)
k3 = t3.kyoku
for s, spec in ((0, "123456789m123p9s"), (1, "123456789p123m9s"), (2, "123456789s123m9s")):
    hh = k3.hands[s]
    hh.tiles[:] = T(spec)
    hh.melds, hh.menzen, hh.riichi, hh.riichi_index = [], True, True, 0
# 9s is the shared wait for 0 and 1; seat 2 waits on a 9s too only if its pair
# is 9s: give it a tanki on 9s instead
k3.hands[2].tiles[:] = T("111m222p333s444z9s")
k3.hands[3].pond.append(mj.SOU + 8)
k3.last_discard_seat, k3.last_discard = 3, mj.SOU + 8
offer3 = t3.msg_naki(mj.SOU + 8, {0: {"ron": True}, 1: {"ron": True}, 2: {"ron": True}})
t3.handle(tst._client_naki(offer3, 0, M.NAKI_RON))
t3.handle(tst._client_naki(offer3, 1, M.NAKI_RON))
o3 = t3.handle(tst._client_naki(offer3, 2, M.NAKI_RON))
check("sanchahou: three rons on one tile end the hand as an abortive draw",
      k3.result is not None and k3.result[0] == mj.ABORT
      and k3.result[1]["why"] == mj.SANCHAHOU and "MjSEISAN" in ops(o3), (k3.result, ops(o3)))

# suufon renda: the human's East completes four East discards
tf = table.Table(142, seed=15)
tf.seat_player(0x1600, b"f", seat=0)
tf.fill_with_bots()
ready(tf)
kf = tf.kyoku
kf.first_go_round = True
for s in (1, 2, 3):
    kf.hands[s].pond[:] = [mj.EAST]
    kf.hands[s].melds = []
    kf.hands[s].tiles[:] = [x for x in kf.hands[s].tiles if mj.kind(x) != mj.EAST]
kf.hands[0].pond[:] = []
recf = rig_turn(tf, 0, T("1478m2589p3699s") + [mj.EAST, mj.SOUTH])
of = tf.handle(tst._client_sute(recf, 0, kf.hands[0].tiles.index(mj.EAST)))
check("suufon renda: the fourth East ends the hand (draw reveal + settlement)",
      kf.result is not None and kf.result[1].get("why") == mj.SUUFON_RENDA
      and ops(of)[-1:] == ["MjSEISAN"], (kf.result, ops(of)))

# suucha riichi: the human's riichi is the fourth
tr = table.Table(143, seed=16)
tr.seat_player(0x1700, b"r", seat=0)
tr.fill_with_bots()
ready(tr)
kr = tr.kyoku
for s in (1, 2, 3):
    kr.hands[s].riichi = True
    kr.hands[s].tiles[:] = T("111m222m333m44z")[:13] if s == 1 else kr.hands[s].tiles
recr = rig_turn(tr, 0, T("123456789m123p") + [mj.SOUTH, mj.WEST])
kr.hands[0].menzen = True
idx_w = kr.hands[0].tiles.index(mj.WEST)
orr = tr.handle(tst._client_sute(recr, 0, idx_w, action=M.SUTE_RIICHI))
check("suucha riichi: the fourth riichi passing ends the hand",
      kr.result is not None and kr.result[1].get("why") == mj.SUUCHA_RIICHI,
      (kr.result, ops(orr)))

# kyushu kyuhai: the human's first-turn declaration
tk9 = table.Table(144, seed=17)
tk9.seat_player(0x1800, b"k", seat=0)
tk9.fill_with_bots()
ready(tk9)
k9 = tk9.kyoku
k9.first_go_round = True
for hh in k9.hands:
    hh.melds = []
rec9 = rig_turn(tk9, 0, T("19m19p19s1234567z") + T("5m"))
o9 = tk9.handle(tst._client_sute(rec9, 0, 0, action=M.SUTE_OTHER))
check("kyushu kyuhai: declared on the first turn, an abortive draw",
      k9.result is not None and k9.result[1].get("why") == "kyushu"
      and "MjSEISAN" in ops(o9), (k9.result, ops(o9)))

# suukaikan: the discard after a shared fourth kan passes
tkk = table.Table(145, seed=18)
tkk.seat_player(0x1900, b"s", seat=0)
tkk.fill_with_bots()
ready(tkk)
kkk = tkk.kyoku
reck = rig_turn(tkk, 0, T("123m456p789s1122z3z"))
kkk._fourth_kan_discard = True
okk = tkk.handle(tst._client_sute(reck, 0, 13))
check("suukaikan: the discard after a shared fourth kan passes, abortive draw",
      kkk.result is not None and kkk.result[1].get("why") == mj.SUUKAIKAN,
      (kkk.result, ops(okk)))

# pao: daisangen with the third dragon fed by seat 3, ronned off seat 2
tp = table.Table(146, seed=19)
tp.seat_player(0x1A00, b"p", seat=1)
tp.fill_with_bots()
ready(tp)
kp = tp.kyoku
hp = kp.hands[1]
hp.melds = [mj.Meld(mj.PON, [mj.HAKU] * 3, 0, mj.HAKU),
            mj.Meld(mj.PON, [mj.HATSU] * 3, 2, mj.HATSU),
            mj.Meld(mj.PON, [mj.CHUN] * 3, 3, mj.CHUN)]
hp.menzen = False
hp.tiles[:] = T("123m5p")
kp.hands[2].pond.append(mj.PIN + 4)
kp.last_discard_seat, kp.last_discard = 2, mj.PIN + 4
op_ = tp.msg_naki(mj.PIN + 4, {1: {"ron": True}})
before_p = list(kp.scores)
outp = tp.handle(tst._client_naki(op_, 1, M.NAKI_RON))
won = kp.result[1][0][1] if kp.result and kp.result[0] == mj.RON else None
check("pao: daisangen fed by seat 3 names seat 3 liable",
      won is not None and won.pao.get("daisangen") == 3, won and won.pao)
check("...and seat 3 pays a share though seat 2 dealt in",
      kp.scores[3] < before_p[3] and kp.scores[2] < before_p[2], (before_p, list(kp.scores)))
check("...the win screen goes out", "MjYAKUDISP" in ops(outp), ops(outp))

# renhou: a non-dealer rons on the first go-round before its first draw
tn_ = table.Table(147, seed=20)
tn_.seat_player(0x1B00, b"n", seat=1)
tn_.fill_with_bots()
ready(tn_)
kn = tn_.kyoku
kn.first_go_round = True
hn = kn.hands[1]
hn.tiles[:] = T("123456789m123p9s")
hn.melds, hn.menzen = [], True
dealer = kn.dealer
kn.hands[dealer].pond.append(mj.SOU + 8)
kn.last_discard_seat, kn.last_discard = dealer, mj.SOU + 8
on_ = tn_.msg_naki(mj.SOU + 8, {1: {"ron": True}})
outn = tn_.handle(tst._client_naki(on_, 1, M.NAKI_RON))
wn = kn.result[1][0][1] if kn.result and kn.result[0] == mj.RON else None
check("renhou: a first-go-round ron before the first draw scores renhou",
      dealer != 1 and wn is not None and any(n == "renhou" for n, _h in wn.yaku),
      wn and wn.yaku)

print("\n%d checks, %d failed" % (COUNT[0], len(FAILS)))
for f in FAILS:
    print("  FAILED: " + f)
sys.exit(1 if FAILS else 0)
