#!/usr/bin/env python3
"""Janhourou reserve refusal for unmet table entry limits (MjRESERVEACK 10).

    python tests/test_jan_reserve_limits.py

POL_JAN_RESERVE_LIMITS=1 turns it on (default off). Result 10 is the client's
"Entry limits not met. Cannot reserve"; the +0x19 bitfield order (money,
level, title) is Project Crystal Server's label, unverified. Temp dirs only,
and the player records are rows of a throwaway database (tools/janpg.py).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import janpg  # noqa: E402

if janpg.fresh_database() is None:
    sys.exit(janpg.skip_or_fail("test_jan_reserve_limits"))

tmp = tempfile.mkdtemp(prefix="janlimits-")
os.environ["POL_DATA_DIR"] = os.path.join(tmp, "data")
os.environ["POL_RESOURCE_DIR"] = os.path.join(tmp, "res")
os.environ["POL_LOG_DIR"] = os.path.join(tmp, "logs")
for d in ("data", "res", "logs"):
    os.makedirs(os.path.join(tmp, d), exist_ok=True)
os.environ["POL_JAN_SEATS"] = "1"
os.environ["POL_JAN_SEATS_KEY"] = "jan:test:%s:seats" % os.path.basename(tmp)
os.environ["POL_JAN_RULES"] = "1"
os.environ["POL_JAN_RULES_KEY"] = "jan:test:%s:rules" % os.path.basename(tmp)
for k in ("POL_JAN_RESERVE_LIMITS", "POL_JAN_LIMIT_MONEY_MIN",
          "POL_JAN_LIMIT_LEVEL_MIN"):
    os.environ.pop(k, None)

import janhourou as jh  # noqa: E402
import janrules  # noqa: E402
import janseats  # noqa: E402
import janstats  # noqa: E402
import janwire  # noqa: E402

jh.log = lambda *a, **k: None
for mod in (janseats, janrules):
    mod._TABLES.clear()
    mod._ADOPTED[0] = True
    mod._OWNER[0] = True

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


def ask(table, member, op=jh.MjPLAYREQ):
    req = janwire.pack(opcode=op, f13=0x11, src=1,
                       dst=janwire.DST_SERVER & 0xFF, id8=table,
                       payload=0x5B01E2BB73B3B60B + member)
    out = jh.handle_line(janwire.encode(req), member_id=member)
    if isinstance(out, (list, tuple)):
        out = out[0] if out else None
    rec = janwire.decode(out) if isinstance(out, bytes) else None
    return (rec[0x18], rec[0x19]) if rec else None


def member(mid, money=0, level=None, titles=None):
    rec = janstats.blank(mid)
    rec["money"] = money
    rec["titles"] = dict(titles or {})
    if level is not None:
        rec["overrides"]["level"] = level
    janstats.store(mid, rec)


MASTER, RICH, POOR, CHAMP = 8, 16, 21, 44
member(MASTER, money=5000)
member(RICH, money=5000, level=10)
member(POOR, money=0, level=1)
member(CHAMP, money=0, level=1, titles={janstats.SHOGO_KEYS[0]: 1})

chk("master reserves table 1", ask(1, MASTER)[0], janseats.RESERVE_MASTER)
tid = jh._table_for(MASTER, 1)
janrules.store(tid, {"LIMIT_MONEY": 1, "LIMIT_LEVEL": 1, "LIMIT_SYOGO1": 1},
               by=MASTER)

print("knob off (default): limits are not enforced")
chk("poor member seats", ask(1, POOR)[0], janseats.RESERVE)
ask(1, POOR, op=jh.MjPLAYCANCEL)

print("knob on")
os.environ["POL_JAN_RESERVE_LIMITS"] = "1"
os.environ["POL_JAN_LIMIT_LEVEL_MIN"] = "5"
chk("poor member: money+level+title unmet", ask(1, POOR),
    (jh.RESERVE_LIMIT_RESULT, jh.RESERVE_LIMIT_MONEY | jh.RESERVE_LIMIT_LEVEL
     | jh.RESERVE_LIMIT_TITLE))
chk("poor member holds no seat", any(m == POOR for m, *_ in janseats.seats_at(tid)),
    False)
chk("title holder: money+level unmet", ask(1, CHAMP),
    (jh.RESERVE_LIMIT_RESULT, jh.RESERVE_LIMIT_MONEY | jh.RESERVE_LIMIT_LEVEL))
chk("rich member without the title: title unmet", ask(1, RICH),
    (jh.RESERVE_LIMIT_RESULT, jh.RESERVE_LIMIT_TITLE))
janrules.store(tid, {"LIMIT_SYOGO1": 0}, by=MASTER)
chk("rich member, title limit off: seated", ask(1, RICH)[0], janseats.RESERVE)
chk("the master (seated) is never refused", ask(1, MASTER)[0],
    janseats.RESERVE_MASTER)

print("a table with no limits refuses nobody")
chk("poor member at table 2", ask(2, POOR)[0], janseats.RESERVE_MASTER)

print("a refusal does not move a seat held elsewhere")
chk("poor member tries table 1 again", ask(1, POOR)[0], jh.RESERVE_LIMIT_RESULT)
tid2 = jh._table_for(POOR, 2)
chk("still seated at table 2", any(m == POOR for m, *_ in janseats.seats_at(tid2)),
    True)

print("%s" % ("ALL OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
