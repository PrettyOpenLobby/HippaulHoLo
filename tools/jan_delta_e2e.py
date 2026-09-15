#!/usr/bin/env python3
"""End-to-end guard for Janhourou's `<DR>`/`<DD>` table-row delta stream.

WHY A SEPARATE SUITE: the three modules involved each have their own selftest
and all three passed while the feature did not exist, because the thing that
can break is the SEAM -- janseats issues a sequence, janlobby renders a row,
the title puts the two on the wire, and nothing else exercises all three at
once. This drives the real responder with a real `<DR>` and decodes the answer
the way the CLIENT does.

The decode is `lb__002f9e10`'s field-to-offset mapping (see janlobby's
`_TABLE_FIELDS` banner). The load-bearing assertion is the one comparing a
delta row against the row a RE-FETCH would carry: if those ever disagree, a
client that applied a delta and a client that reloaded are looking at two
different tables, and neither of them is obviously wrong on screen.

    python tools/jan_delta_e2e.py
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.join(os.path.dirname(HERE), "services")
sys.path.insert(0, HERE)
import jan_testenv                                                 # noqa: E402
jan_testenv.setup()          # this tree's services + the OpenLobby core
os.chdir(SERVICES)

_TD = tempfile.mkdtemp()
os.environ["POL_JAN_SEATS_FILE"] = os.path.join(_TD, "seats.json")
os.environ.setdefault("POL_DATA_DIR", _TD)

import polpro                                                    # noqa: E402
import janseats                                                  # noqa: E402
import janlobby                                                  # noqa: E402
import responders                                                # noqa: E402
import jantitle                                                  # noqa: E402

ok = True


def check(name, cond):
    global ok
    print("%-62s %s" % (name, "ok" if cond else "FAIL"))
    ok = ok and bool(cond)


def ask(have):
    """One `<DR>(have)` through the real dispatch helper."""
    return jantitle._jan_roster_delta_reply(polpro.build([("DR", [str(have)])]))


def main():
    janseats._TABLES.clear()
    janseats._DELTAS.clear()
    janseats._ROWS.clear()
    janseats._SEQ.clear()
    janseats._ADOPTED[0] = True
    janseats._OWNER[0] = True

    janseats.reconcile()
    base = janseats.sequence()

    check("a CURRENT client is answered with silence",
          ask(base) == (None, True))

    reply, handled = ask(base + 99)
    check("a client ahead of us is handled, not ignored", handled and reply)
    check("...and the answer is <DO>, i.e. reload",
          bool(reply) and polpro.parse(reply)[0][0] == "DO")

    # THE EVENT: somebody reserves table 1.
    janseats.reserve(1, 6, "LaptopTest2")
    reply, handled = ask(base)
    check("a reservation produces a handled reply", handled and bool(reply))
    groups = polpro.parse(reply) if reply else []
    check("the envelope is DD, DN, then (DC, TD) pairs",
          [t for t, _v in groups] == ["DD", "DN", "DC", "TD"])
    check("DN carries the pair count", groups[1][1] == ["1"])
    check("DC carries the sequence", groups[2][1] == [str(base + 1)])
    check("the TD group carries seven values", len(groups[3][1]) == 7)

    # Decode it as the client does, and compare against a re-fetch.
    rec = janlobby.encode_table(groups[3][1])
    blob = janlobby.ptl_blob([], None, serial=janseats.sequence())
    off = janlobby.PTL_TABLE_OFF
    row0 = blob[off:off + janlobby.PTL_TABLE_REC]
    check("THE DELTA ROW == THE ROW A RE-FETCH WOULD CARRY", rec == row0)

    check("the reserved table reads seated=1 on the wire",
          struct.unpack_from("<I", rec, janlobby.PTL_T_SEATED)[0] == 1)
    check("...state 4, so Start Game stays enabled",
          rec[janlobby.PTL_T_STATE] == 4)
    check("...and lb__002f9f18 can match it by name",
          rec[janlobby.PTL_T_NAME:janlobby.PTL_T_NAME + 9] == b"#MJS0T001")

    serial = struct.unpack_from("<I", blob, janlobby.PTL_SERIAL_OFF)[0]
    check("a fetched blob is stamped with the CURRENT sequence",
          serial == janseats.sequence())
    check("...so that client's very next poll is silent",
          ask(serial) == (None, True))

    # In play, and back out again -- both are row changes and both must ride.
    seq = janseats.sequence()
    janseats.set_in_play(1, 6)
    d = janseats.deltas_after(seq)
    check("MjGAMESTART rides the same channel",
          d is not None and len(d) == 1)
    reply, _h = ask(seq)
    check("...and renders state 5 (in play, watchable)",
          janlobby.encode_table(polpro.parse(reply)[3][1])[janlobby.PTL_T_STATE] == 5)

    # A batch too big to apply must be a reload, never a prefix.
    os.environ["POL_JAN_DELTA_CHUNK"] = "1"
    seq = janseats.sequence()
    janseats.reserve(2, 7, "b")
    janseats.reserve(3, 8, "c")
    reply, handled = ask(seq)
    check("an over-long batch is answered <DO>, never a prefix",
          handled and polpro.parse(reply)[0][0] == "DO")
    os.environ.pop("POL_JAN_DELTA_CHUNK")

    # THE RELOAD-LOOP GUARD. Whatever the seat store's sequence is, the blob
    # must claim exactly that and never more -- a blob one ahead is a client
    # that can only ever be told to reload, which restamps it one ahead again.
    for _seq in (0, 1, 7):
        janseats._SEQ["0"] = _seq
        check("a blob at sequence %d claims %d, not more" % (_seq, _seq),
              janlobby.blob_serial() == _seq)
        check("...so the server is never behind its own blob",
              janseats.deltas_after(janlobby.blob_serial()) is not None)

    # WARNING: A QUEUED GAME RECORD MUST RIDE THE <DR> REPLY. A player still in the
    # room never sends a game-band line -- their only tick is this poll -- so
    # queueing the game-start trio and draining it on the game band alone meant
    # it was never delivered (live 2026-09-04).
    import janhourou
    janhourou.GAMES.by_lobby.clear(); janhourou.GAMES.by_member.clear()
    _members = [(15, 0, "P1", 0xAAAA), (6, 1, "P2", 0xBBBB)]
    _gt = janhourou.GAMES.table_for_lobby(1, _members)
    _rec = polpro.build([("DD", ["0"])])          # any bytes; framing is the test
    import janwire
    _gt.queue_for(1, janwire.pack(opcode=14, length=0x18)[:0x18])
    check("a record is queued for the seat that is not talking",
          len(janhourou.take_pending(6)) == 1)
    _gt.queue_for(1, janwire.pack(opcode=14, length=0x18)[:0x18])
    jantitle._JAN_GAME_PEER[6] = (b"TGT", b"NICK", b"srv", "SID1")
    _out = jantitle._jan_pending_lines(6)
    check("...and _jan_pending_lines frames it as a GAME-band notice",
          len(_out) == 1 and b"GMJSG" in bytes(_out[0]))
    _gt.queue_for(1, janwire.pack(opcode=14, length=0x18)[:0x18])
    jantitle._JAN_GAME_PEER.pop(6, None)
    check("with no known game peer it frames nothing rather than guessing",
          jantitle._jan_pending_lines(6) == [])

    # THE SOCKET PIN. A member can hold more than one connection and only the
    # one their game band is on will read a game record -- pushing into another
    # is "delivered is not landed". A mismatch must leave the queue INTACT.
    jantitle._JAN_GAME_PEER[6] = (b"TGT", b"NICK", b"srv", "SID1")
    check("a push on the WRONG SESSION frames nothing (same nick, other socket)",
          jantitle._jan_pending_lines(6, on_sid="SID2") == [])
    check("...and leaves the record queued for the right one",
          len(jantitle._jan_pending_lines(6, on_sid="SID1")) == 1)

    # THE BURST CAP. The remainder must stay queued, in order.
    for _i in range(5):
        _gt.queue_for(1, janwire.pack(opcode=14, f13=_i, length=0x18)[:0x18])
    _first = jantitle._jan_pending_lines(6, on_sid="SID1", limit=2)
    check("a capped push takes only its limit", len(_first) == 2)
    _rest = jantitle._jan_pending_lines(6, on_sid="SID1", limit=2)
    check("...and the remainder is still there for the next tick",
          len(_rest) == 2)
    check("...draining to empty, losing nothing",
          len(jantitle._jan_pending_lines(6, on_sid="SID1")) == 1)

    os.environ["POL_JAN_DELTAS"] = "0"
    check("POL_JAN_DELTAS=0 declines, leaving the old behaviour untouched",
          ask(0) == (None, False))
    os.environ.pop("POL_JAN_DELTAS")

    print("\n%s" % ("ALL OK" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
