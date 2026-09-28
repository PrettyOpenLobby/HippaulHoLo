"""The offline selftest (a whole hanchan with no client) and the command line."""
import argparse
import os
import random
import struct
import threading
import time
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
from .deps import janseats, janstats
import jangame  # the facade: its writes reach every copy
from . import bots, knobs, manager, narration, table, tablegallery, tablesashiuma


# --- selftest ----------------------------------------------------------------

def _client_ack(rec, seat=0, spectator=False, slot=None):
    """Build the ack the real client would send for `rec`.

    console__00284380: 0x18 bytes, opcode+1, the sequence ECHOED, src = my seat,
    dst = 4, f16 = 1 (0 for MjBYE), sub = 3.

    A SPECTATOR (console__00284310, console.c:2552-2577) sends MjGALLEYACK
    instead, whatever the record: op 0x30, +0x13 = the seq, +0x14 = its
    GALLERY SLOT (the RESERVEACK +0x19 byte, NOT a seat), +0x15 4, +0x16 =
    f16 (0 for the BYE-equivalent after MjGAMEEND), +0x17 6. The old shape
    here (opcode+1 on sub 6) was wrong -- spec §5.
    """
    h = janwire.unpack(rec)
    ack = M.ACK_OF.get(h["opcode"])
    if ack is None:
        return None
    if spectator:
        return janwire.pack(opcode=M.MjGALLEYACK, f13=h["f13"],
                            src=(seat if slot is None else slot) & 0xFF, dst=4,
                            f16=0 if ack == M.MjBYE else 1, sub=6,
                            length=0x18)[:0x18]
    return janwire.pack(opcode=ack, f13=h["f13"], src=seat, dst=4,
                        f16=0 if ack == M.MjBYE else 1, sub=3,
                        length=0x18)[:0x18]


def _client_sute(rec, seat, index, action=M.SUTE_DISCARD):
    h = janwire.unpack(rec)
    out = bytearray(janwire.pack(opcode=M.MjSUTE, f13=h["f13"], src=seat, dst=4,
                                 f16=1, sub=3, length=0x20))
    struct.pack_into("<I", out, 0x18, action | (index & 0xF))
    return bytes(out)


def _client_naki(rec, seat, word=M.NAKI_PASS):
    h = janwire.unpack(rec)
    out = bytearray(janwire.pack(opcode=M.MjNAKIACK, f13=h["f13"], src=seat,
                                 dst=4, f16=1, sub=3, length=0x20))
    struct.pack_into("<I", out, 0x18, word)
    return bytes(out)


def selftest(trace=False):
    ok = True
    seen = set()

    knobs.DEAL_DELAY = 0.0          # never sleep on the deal during the selftest

    # Persistence is exercised for real, but NEVER against a deployed
    # database: the member ids below are test ids and would litter it with
    # records for players who do not exist. jan_run_all.py gives this suite a
    # throwaway database of its own.

    # The side-bet handshake changes what READY answers (START, not the
    # deal); the legacy tests assume the deal. The sashiuma block below
    # turns it on explicitly for its own tables.
    tablesashiuma.SASHIUMA = False
    # Never start the sweeper THREAD in here: the tests drive fake clocks and
    # call `Manager.tick` themselves. The thread is covered by its own block.
    knobs.SWEEPER = False

    def check(cond, msg):
        if not cond:
            print("FAIL: %s" % msg)
        return bool(cond)

    # --- TWO HUMANS AT ONE TABLE ------------------------------------------
    # Everything below this comment is about the case that was broken until
    # 2026-09-04: a Start Game at a table holding two people built a table for
    # ONE of them and three bots, and the other was left in the lobby.
    _m = manager.Manager()
    _members = [(15, 0, "PS2Tester", 0xAAAA), (6, 1, "LaptopTest2", 0xBBBB)]
    _t2 = _m.table_for_lobby(2, _members)
    ok &= check(_t2.live == {0, 1}, "both humans are live seats: %r" % (_t2.live,))
    ok &= check(len(_t2.bots) == 2, "and the other two seats are bots")
    ok &= check(_t2.seats[0] == 0xAAAA and _t2.seats[1] == 0xBBBB,
                "each seat carries its own PolID, not our member number")
    ok &= check(_t2.seat_of_member(15) == 0 and _t2.seat_of_member(6) == 1,
                "both members route to their reserved seat")
    ok &= check(_m.by_member.get(15) is _t2 and _m.by_member.get(6) is _t2,
                "and BOTH members resolve to the SAME table")

    # A SECOND Start Game must not wipe a hand in progress. Both players can
    # hold a master menu at once (the client caches its master flag until the
    # next save fetch), so this is reachable, not hypothetical.
    _t2.state = "playing"
    _t2.seats[0] = 0xFEED
    _t2b = _m.table_for_lobby(2, _members, reset=False)
    ok &= check(_t2b is _t2 and _t2.seats[0] == 0xFEED,
                "a second Start Game on a PLAYING table changes nothing")
    _t2.state = "idle"

    # The deal: one record per live seat, each hiding the other's tiles.
    _t2.game = _t2.game or None
    _ready = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                          length=0x18)[:0x18]
    _out = _m.handle(_ready, member_id=15)
    _deal = [r for r in _out if janwire.unpack(r)["opcode"] == M.MjHAIPAI]
    ok &= check(len(_deal) == 1, "seat 0 is handed exactly ONE deal record")
    _p2 = _m.pending_for_member(6)
    _deal2 = [r for r in _p2 if janwire.unpack(r)["opcode"] == M.MjHAIPAI]
    ok &= check(len(_deal2) == 1, "and seat 1's copy waited in its outbox")
    ok &= check(_m.pending_for_member(6) == [],
                "draining the outbox empties it")

    if _deal and _deal2 and knobs.CONCEAL_DEAL:
        # MjHAIPAI +0x60: 4 hands, 14 u16 each, seat stride 0x1c. The two
        # copies must differ, and each must differ from the other exactly in
        # the two seats whose visibility swapped. (Only when CONCEAL_DEAL is on;
        # by default concealment is off -- see CONCEAL_RESYNC's RAM proof -- and
        # both copies are identical face-up.)
        def _hand(rec, seat):
            o = 0x60 + seat * 0x1C
            return rec[o:o + 28]
        a, b = _deal[0], _deal2[0]
        ok &= check(a != b, "the two deal records are NOT the same bytes")
        ok &= check(_hand(a, 0) != _hand(b, 0),
                    "seat 0's tiles are shown in its own copy and hidden in "
                    "seat 1's")
        ok &= check(_hand(a, 1) != _hand(b, 1),
                    "and seat 1's tiles the other way round")
        ok &= check(_hand(a, 2) == _hand(b, 2),
                    "a bot's hand is concealed in BOTH copies, identically")

    # A DRAW IS PRIVATE. Whatever seat 0 is handed on its own turn must not be
    # sitting in seat 1's outbox -- it carries seat 0's tiles and seat 0's
    # action menu.
    _m.pending_for_member(6)
    _ack = janwire.pack(opcode=M.MjHAIPAIACK, f13=1, src=0, dst=4, f16=1,
                        sub=3, length=0x18)[:0x18]
    _mine = _m.handle(_ack, member_id=15)
    _theirs = _m.pending_for_member(6)
    _priv = [r for r in _mine if janwire.unpack(r)["opcode"] == M.MjTSUMO]
    _leak = [r for r in _theirs if r in _priv]
    ok &= check(not _leak,
                "seat 0's own draw did not leak into seat 1's outbox")

    t = table.Table(1, seed=20260817)
    t.seat_player(0x1234, b"tester", seat=0)
    t.fill_with_bots()
    ok &= check(len(t.bots) == 3 and t.live == {0}, "one live seat, three bots")
    # WARNING: Every seat id must be NON-ZERO: the client's seated-player count is the
    # number of non-zero u64s in MjNOTICEMEMBER (malloc__002c67a0), so a zero
    # here is invisible to it and the table waits for a player who is sitting
    # right there. Cost one live run to find.
    ok &= check(all(t.member_ids()), "no seat announces id 0: %r"
                % ["%016x" % i for i in t.member_ids()])
    ok &= check(len(set(t.member_ids())) == 4, "and the four ids are distinct")

    # MjREADY starts a game and the first thing out is the deal.
    ready = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                         length=0x18)[:0x18]
    out = t.handle(ready)
    ok &= check(len(out) == 1, "READY produces exactly the deal, got %d" % len(out))
    haipai = out[0]
    hh = janwire.unpack(haipai)
    ok &= check(hh["opcode"] == M.MjHAIPAI, "READY -> MjHAIPAI")
    ok &= check(hh["sub"] == M.INGAME_SUB, "the deal drains on the in-game sub")
    ok &= check(len(haipai) == M.HAIPAI_LEN, "the deal is 0xd7 bytes")
    ok &= check(bytes(haipai[0x23:0x27]) == (b"\x01\0\0\0" if knobs.ISHUMAN_ENABLE
                                             else b"\0\0\0\0"),
                "IsHuman +0x23..+0x26 = the one human at seat 0: %r"
                % haipai[0x23:0x27])

    # every seat got 13 tiles, the dealer 14, and the wall reads the client's 70
    for s in range(4):
        base = M.HAIPAI_HAND + s * M.HAIPAI_SEAT_STRIDE
        tiles = [v for v in struct.unpack_from("<14H", haipai, base) if v]
        want = 14 if s == t.kyoku.dealer else 13
        ok &= check(len(tiles) == want,
                    "seat %d holds %d tiles, expected %d" % (s, len(tiles), want))
    ok &= check(t.kyoku.wall.remaining == mj.Wall.LIVE_AT_DEAL - 1,
                "after the dealer's draw the wall is 69, got %d"
                % t.kyoku.wall.remaining)

    # --- drive a whole hanchan through the wire, as the client would --------
    pending = [_client_ack(haipai, 0)]
    guard = 0
    seqs = []
    finished = False
    bot_discards = 0
    while pending and guard < 4000:
        guard += 1
        inbound = pending.pop(0)
        out = t.handle(inbound)
        prev = None
        for rec in out:
            h = janwire.unpack(rec)
            seen.add(h["opcode"])
            seqs.append(h["f13"])
            # POPULATE-THEN-ANIMATE (2026-09-03): a bot's motion-3 discard must
            # ride immediately behind an MjALLDATA whose pond for that seat
            # already ends with the discarded tile -- the client's flight
            # targets the pond tail (mahdisp__002bfb30) and aims at the table
            # centre when the tile is missing. In the default subtype-3 mode
            # that MjALLDATA is ALSO the draw+prep event: subtype 3, the event
            # tile matching the discard, present-flag set, event seat = the
            # discarding seat -- which is what clears the settled flag so there
            # is no pre-draw. See DISCARD_ANIM.
            if (h["opcode"] == M.MjTSUMO and rec[0x18] == 3
                    and rec[0x22] not in t.live):
                bot_discards += 1
                pv = janwire.unpack(prev)["opcode"] if prev else None
                if check(pv == M.MjALLDATA,
                         "bot discard (seat %d) is preceded by MjALLDATA, "
                         "got %s" % (rec[0x22], narration.M_NAME(pv) if pv else None)):
                    base = M.ALLDATA_POND + rec[0x22] * M.ALLDATA_POND_STRIDE
                    pond = [b for b in prev[base:base + M.ALLDATA_POND_STRIDE]
                            if b]
                    ok &= check(pond and (pond[-1] & 0x3F) == (rec[0x1B] & 0x3F),
                                "that MjALLDATA's pond for seat %d already "
                                "ends with the discard (pond tail %s, tile %s)"
                                % (rec[0x22],
                                   "%#x" % pond[-1] if pond else "empty",
                                   "%#x" % rec[0x1B]))
                    if knobs.DISCARD_ANIM == "subtype3":
                        ok &= check(prev[0x18] == 3, "the prep MjALLDATA is "
                                    "subtype 3, got %d" % prev[0x18])
                        ok &= check(prev[0x1E] != 0, "the prep MjALLDATA sets "
                                    "the event-present flag at +0x1e")
                        ok &= check(prev[0x22] == rec[0x22],
                                    "the prep MjALLDATA's event seat +0x22 (%d) "
                                    "matches the discard seat (%d)"
                                    % (prev[0x22], rec[0x22]))
                        ok &= check((prev[0x1C] & 0x3F) == (rec[0x1B] & 0x3F),
                                    "the prep MjALLDATA's event tile +0x1c (%#x) "
                                    "matches the discard tile (%#x)"
                                    % (prev[0x1C], rec[0x1B]))
                else:
                    ok = False
            prev = rec
            if trace:
                print("  -> %-22s len=%-4d seq=%3d sub=%d"
                      % (narration.M_NAME(h["opcode"]), h["length"], h["f13"], h["sub"]))
            if (h["sub"] != M.INGAME_SUB and h["opcode"] != M.MjNOTICEGAMESTART
                    and h["opcode"] != M.MjREADYSTATUS):   # sub 2 by measurement
                ok = check(False, "%s went out on sub %d -- the in-game loop "
                                  "drains on %d and would never see it"
                                  % (narration.M_NAME(h["opcode"]), h["sub"], M.INGAME_SUB))
            if h["length"] != len(rec):
                ok = check(False, "%s declares %d, is %d"
                           % (narration.M_NAME(h["opcode"]), h["length"], len(rec)))
        # answer the LAST record the way the client would
        if not out:
            continue
        rec = out[-1]
        h = janwire.unpack(rec)
        if h["opcode"] == M.MjTSUMO:
            hand = [v for v in struct.unpack_from("<14H", rec, M.TSUMO_HAND) if v]
            flags = rec[0x54:0x62]
            idx = next((i for i in range(len(hand)) if flags[i] & 2), len(hand) - 1)
            pending.append(_client_sute(rec, 0, idx))
        elif h["opcode"] == M.MjNAKI:
            # The 09-03 Cancel-only bug: an all-zero +0x4c menu block builds a
            # menu with no call items. Every offer must carry the claimable
            # tile and light the menu byte matching each per-call gate.
            ok &= check(struct.unpack_from("<H", rec, 0x28)[0] != 0,
                        "MjNAKI carries the claimable tile at +0x28")
            for s in range(4):
                if not rec[0x2B + s]:
                    continue
                sblk = rec[0x4C + s * 7:0x4C + s * 7 + 7]
                ok &= check(any(sblk),
                            "offered seat %d has a non-zero menu block" % s)
                for mb, off in ((1, 0x3C), (3, 0x40), (4, 0x44), (5, 0x48)):
                    ok &= check((sblk[mb] != 0) == (rec[off + s] != 0),
                                "seat %d menu byte %d mirrors gate +0x%x"
                                % (s, mb, off))
                if rec[0x48 + s]:
                    flg = rec[0x68 + s * 14:0x68 + s * 14 + 14]
                    ok &= check(any(f & 7 for f in flg),
                                "a chi offer flags at least one hand slot")
            pending.append(_client_naki(rec, 0, M.NAKI_PASS))
        elif h["opcode"] == M.MjGAMEEND:
            finished = True
        else:
            a = _client_ack(rec, 0)
            if a is not None:
                pending.append(a)

    ok &= check(finished, "the hanchan reached MjGAMEEND (guard %d)" % guard)
    if knobs.DISCARD_ANIM != "appear":     # "appear" emits no motion-3 to check
        ok &= check(bot_discards > 20,
                    "the pond-order check saw real bot discards (%d)"
                    % bot_discards)
    for op in (M.MjHAIPAI, M.MjTSUMO, M.MjSEISAN, M.MjGAMERESULTHALF1,
               M.MjGAMERESULTHALF2, M.MjGAMEEND):
        ok &= check(op in seen, "%s was never sent" % narration.M_NAME(op))

    # the dedup rule: no two CONSECUTIVE sends may share a sequence byte
    dupes = [i for i in range(1, len(seqs)) if seqs[i] == seqs[i - 1]]
    ok &= check(not dupes,
                "%d consecutive records reused a sequence byte -- the client "
                "would treat them as retransmissions and re-ack instead of "
                "handling them (at %r)" % (len(dupes), dupes[:5]))

    # points stayed conserved across the whole game
    total = sum(t.game.scores) + 1000 * t.game.riichi_sticks
    ok &= check(total == 100000,
                "points leaked: %d over %d hands (%r)"
                % (total, t.game.hands_played, t.game.scores))

    # --- the call MENU block, deterministically (the 09-03 Cancel-only bug) --
    # A known hand: pair of 6s (pon) plus 5s+7s (chi) on a 6s discard from the
    # left seat. The menu must light Pon+Chi, carry the tile at +0x28, and the
    # +0x68 flags must mark EXACTLY the chi-pair kinds -- the client's chi
    # navigator walks the flagged slots and sends the slot it stops on, so a
    # flagged pon tile would let it send a chi no pair can satisfy.
    t2 = table.Table(2, seed=42)
    t2.seat_player(0x2345, b"caller", seat=0)
    t2.fill_with_bots()
    t2.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    k2 = t2.kyoku
    h2 = k2.hands[0]
    h2.tiles[:] = [mj.SOU + 5, mj.SOU + 5, mj.SOU + 4, mj.SOU + 6,
                   mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.PIN, mj.PIN + 1,
                   mj.PIN + 2, mj.HONOR, mj.HONOR, mj.HONOR + 1]
    k2.last_discard_seat = 3
    k2.last_discard = mj.SOU + 5      # the tile seat 3 just discarded
    claim = mj.SOU + 5
    opts = t2.legal_calls(0, claim, 3)
    ok &= check("pon" in opts and "chi" in opts,
                "the forced hand offers pon+chi: %r" % opts)
    rec = t2.msg_naki(claim, {0: opts})
    ok &= check(struct.unpack_from("<H", rec, 0x28)[0] == M.tile_u16(claim),
                "+0x28 is the claimable tile")
    ok &= check(rec[0x2A] == 3, "+0x2a is the discarder seat")
    blk = rec[0x4C:0x4C + 7]
    ok &= check(blk[4] == 1 and blk[5] == 1,
                "the menu block lights Pon and Chi: %r" % list(blk))
    ok &= check(blk[0] == 0 and blk[1] == 0 and blk[2] == 0 and blk[6] == 0,
                "no phantom Tsumo/Ron/Riichi/Kyushu items: %r" % list(blk))
    flg = list(rec[0x68:0x68 + 14])
    ok &= check(flg[2] and flg[3],
                "the chi-pair tiles (5s,7s) are flagged: %r" % flg)
    ok &= check(not flg[0] and not flg[1] and not any(flg[4:]),
                "nothing else is -- a flagged pon tile derails the chi "
                "navigator: %r" % flg)

    # Execute the chi and confirm the meld renders IMMEDIATELY, upright: the
    # ack path must emit an MjALLDATA (carrying the meld + its chi layout byte)
    # BEFORE the discard-prompt MjTSUMO -- otherwise the exposed meld is
    # invisible until the next resync and draws with the pon layout (two tiles
    # sideways), the 2026-09-03 live bug.
    k2.hands[3].pond.append(claim)         # the discard the call claims
    pre_hand = list(h2.tiles)
    t2.pending_naki = (claim, {0: {"chi": True}})
    t2._awaiting = (M.MjNAKI, 0)
    ack = _client_naki(t2._last_sent, 0, M.NAKI_CHI | M.tile_u16(claim))
    out = t2.handle(ack)
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(M.MjALLDATA in ops and M.MjTSUMO in ops
                and ops.index(M.MjALLDATA) < ops.index(M.MjTSUMO),
                "a live chi emits MjALLDATA before the discard MjTSUMO: %r"
                % [narration.M_NAME(o) for o in ops])
    ad = out[ops.index(M.MjALLDATA)]
    ok &= check(t2.kyoku.hands[0].melds and t2.kyoku.hands[0].melds[0].kind == mj.CHI,
                "the chi meld exists on seat 0")
    ok &= check(ad[0x126 + 0 * 4] == M.MELD_TYPE_CHI,
                "and its +0x126 layout byte says chi (upright run)")
    ok &= check(any(ad[M.ALLDATA_MELD + i] & 0x40 for i in range(3)),
                "the called tile is flagged for rotation in the meld block")

    # --- THE CALL MOTION (2026-09-04) ---------------------------------------
    # The whole point of the record above: it carries motion 5, so the client
    # runs its chi handler -- banner, sound, tile flight, pond pop, meld build.
    # Before this, on_nakiack sent a bare resync and the call happened in
    # silence. Every field the handler reads is checked here, INCLUDING the
    # three tenses the one record has to be in.
    ok &= check(ops.count(M.MjALLDATA) == 1,
                "exactly ONE MjALLDATA -- a resync behind a motion re-adds the "
                "claimed pond tile and un-hides the hand slots: %r"
                % [narration.M_NAME(o) for o in ops])
    ok &= check(ad[0x18] == 5, "+0x18 is motion 5 (chi), not a subtype: %d"
                % ad[0x18])
    ok &= check(ad[0x1A] == 3, "+0x1a is the discarder: %d" % ad[0x1A])
    ok &= check(ad[0x22] == 0, "+0x22 is the caller: %d" % ad[0x22])
    sa, sb = ad[0x1E], ad[0x1F]
    hb = M.ALLDATA_HAND + 0 * M.ALLDATA_HAND_STRIDE
    ok &= check(sa != sb and sa < 14 and sb < 14,
                "+0x1e/+0x1f are two distinct hand slots: %d,%d" % (sa, sb))
    # SELF-CONSISTENT: the slots the motion hides must hold, in this record's
    # OWN hand block, the tiles it flies. That is what makes the record safe
    # whatever the client last heard.
    ok &= check(ad[hb + sa] == ad[0x1B] and ad[hb + sb] == ad[0x1C],
                "the named slots hold the named tiles in this record's hand "
                "block (%#x@%d, %#x@%d vs %#x/%#x)"
                % (ad[hb + sa], sa, ad[hb + sb], sb, ad[0x1B], ad[0x1C]))
    ok &= check(sorted((sa, sb))
                == sorted(table.Table.taken_slots(pre_hand, t2.kyoku.hands[0].tiles)),
                "and they are exactly the slots the meld consumed: %r vs %r"
                % (sorted((sa, sb)),
                   table.Table.taken_slots(pre_hand, t2.kyoku.hands[0].tiles)))
    # PRE-call pond: the motion pops the tail itself, so it must still be there
    # and must NOT already carry the claimed flag (which means "do not draw").
    pb = M.ALLDATA_POND + 3 * M.ALLDATA_POND_STRIDE
    prow = [b for b in ad[pb:pb + 23] if b]
    ok &= check(prow and (prow[-1] & M.POND_FLAG) == 0
                and (prow[-1] & 0x3F) == (M.tile_byte(claim) & 0x3F),
                "the discarder's pond still ENDS with the claimed tile, "
                "unflagged: %r" % [hex(b) for b in prow[-3:]])
    # POST-call melds: the client's free-meld scan lands on the last OCCUPIED
    # meld, and the tiles it draws come from this block.
    ok &= check(ad[M.ALLDATA_MELD] != 0,
                "the meld block is POST-call -- a pre-call one animates onto "
                "the previous meld and lands three tiles onto nothing")

    # A PON does the same with motion 4. This is the live report itself:
    # "your pon happens in silence".
    t3 = table.Table(3, seed=42)
    t3.seat_player(0x2346, b"ponner", seat=0)
    t3.fill_with_bots()
    t3.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    k3 = t3.kyoku
    k3.hands[0].tiles[:] = [mj.SOU + 5, mj.SOU + 5, mj.SOU + 4, mj.SOU + 8,
                            mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.PIN, mj.PIN + 1,
                            mj.PIN + 2, mj.HONOR, mj.HONOR, mj.HONOR + 1]
    k3.last_discard_seat = 2
    k3.last_discard = mj.SOU + 5
    k3.hands[2].pond.append(mj.SOU + 5)
    t3._last_sent = t3.msg_naki(mj.SOU + 5, {0: {"pon": True}})
    t3.pending_naki = (mj.SOU + 5, {0: {"pon": True}})
    t3._awaiting = (M.MjNAKI, 0)
    out = t3.handle(_client_naki(t3._last_sent, 0,
                                 M.NAKI_PON | M.tile_u16(mj.SOU + 5)))
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(ops.count(M.MjALLDATA) == 1 and M.MjTSUMO in ops,
                "a live pon emits one MjALLDATA then the discard MjTSUMO: %r"
                % [narration.M_NAME(o) for o in ops])
    pd = out[ops.index(M.MjALLDATA)]
    ok &= check(pd[0x18] == 4, "+0x18 is motion 4 (pon): %d" % pd[0x18])
    ok &= check(pd[0x1A] == 2 and pd[0x22] == 0,
                "+0x1a discarder / +0x22 caller: %d/%d" % (pd[0x1A], pd[0x22]))
    ok &= check(pd[0x126 + 0 * 4] == M.MELD_TYPE_PON,
                "and the meld draws with the PON layout, not ankan's")

    # --- the riichi tile is drawn sideways, not deleted (2026-09-04) ---------
    # POND_FLAG means "do not draw this pond tile" -- both of the client's pond
    # loops bracket the draw AND the position advance in `(t & 0x80) == 0`. We
    # used to set it on the riichi discard, which made it vanish on every
    # resync; the sideways tile is +0x136, a one-based pond index per seat.
    tr = table.Table(13, seed=5)
    tr.seat_player(0x5678, b"reacher", seat=0)
    tr.fill_with_bots()
    tr.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    kr = tr.kyoku
    hr = kr.hands[1]
    hr.pond.extend([mj.MAN, mj.MAN + 1, mj.MAN + 2])
    hr.riichi, hr.riichi_index = True, 1
    rd = tr.msg_alldata()
    ok &= check(rd[0x136 + 1] == 2,
                "+0x136 carries the riichi pond index one-based: %d"
                % rd[0x136 + 1])
    rb = M.ALLDATA_POND + 1 * M.ALLDATA_POND_STRIDE
    ok &= check(not (rd[rb + 1] & M.POND_FLAG),
                "and the riichi tile itself is NOT flagged -- the flag hides "
                "it: %#x" % rd[rb + 1])

    # --- the self-turn action menu (MyMove): Riichi is block[2], not [0] -----
    # A closed tenpai hand must light RIICHI (byte 2), never mistake it for
    # TSUMO (byte 0) -- the 2026-09-03 bug set byte 0 on any tenpai hand, so
    # Riichi never appeared and a Tsumo-win button lit without a win.
    tm = table.Table(11, seed=7)
    tm.seat_player(0x3456, b"reach", seat=0)
    tm.fill_with_bots()
    tm.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    km = tm.kyoku
    km.turn = 0
    hh = km.hands[0]
    # WARNING: A COMPLETE winning hand (123m 456m 789m 123p 99s, drew 9s) offers
    # TSUMO ONLY -- never Riichi. You do not declare riichi on a hand you can
    # win, and lighting both WEDGED THE CLIENT (2026-09-04 live hang: a
    # player pressed Riichi on a complete hand and the game froze with no
    # message ever returned; see _mymove_menu's can_win gate).
    hh.tiles = ([mj.MAN + i for i in range(9)]
                + [mj.PIN, mj.PIN + 1, mj.PIN + 2, mj.SOU + 8, mj.SOU + 8])
    hh.drawn = mj.SOU + 8
    hh.melds = []
    hh.menzen = True
    hh.riichi = False
    km.scores[0] = 25000
    menu = tm._mymove_menu(0)
    ok &= check(menu[tm.MENU_TSUMO] == 1,
                "a complete hand lights Tsumo: %r" % list(menu))
    ok &= check(menu[tm.MENU_RIICHI] == 0,
                "and NOT Riichi -- you never riichi a hand you can win: %r"
                % list(menu))

    # A NON-winning closed tenpai hand (123m 456m 789m 1p2p 99s, drew a blank
    # 5s) lights RIICHI, not Tsumo: discarding the 5s leaves it waiting on 3p.
    hh.tiles = ([mj.MAN + i for i in range(9)]
                + [mj.PIN, mj.PIN + 1, mj.SOU + 8, mj.SOU + 8, mj.SOU + 4])
    hh.drawn = mj.SOU + 4
    hh.menzen = True
    hh.riichi = False
    menu = tm._mymove_menu(0)
    ok &= check(menu[tm.MENU_RIICHI] == 1,
                "a non-winning closed tenpai hand lights Riichi (byte 2): %r"
                % list(menu))
    ok &= check(menu[tm.MENU_TSUMO] == 0,
                "and NOT Tsumo -- the drawn 5s does not complete it: %r"
                % list(menu))
    rec = tm.msg_tsumo(0, motion=2)
    ok &= check(rec[0x4D + tm.MENU_RIICHI] == 1,
                "the Riichi bit reaches MjTSUMO at +0x4d+2")
    # WARNING: THE RIICHI FREEZE (2026-09-04, slot 6): the client's riichi picker
    # lands its cursor ONLY on +0x54 slots with bit 0 (mahdisp.c:3546-3561); if
    # none are set it wedges at cursor 1000. The one riichi-legal discard here
    # is the drawn 5s (slot 13) -- it must carry bit 0; every occupied slot
    # keeps bit 1 for an ordinary discard.
    ok &= check(tm._riichi_discard_slots(hh) == [13],
                "only the 5s (slot 13) keeps tenpai when discarded: %r"
                % tm._riichi_discard_slots(hh))
    ok &= check(rec[0x54 + 13] & 1 == 1,
                "the riichi-legal slot 13 carries +0x54 bit 0 (the picker can "
                "land there): %#x" % rec[0x54 + 13])
    ok &= check(all(rec[0x54 + i] & 2 for i in range(14)),
                "every occupied slot still carries bit 1 (ordinary discard): %r"
                % [rec[0x54 + i] for i in range(14)])
    ok &= check(rec[0x54 + 0] & 1 == 0,
                "a NON-riichi-legal slot (0, a man tile) has no bit 0: %#x"
                % rec[0x54 + 0])
    ok &= check(rec[0x4B] == 0,
                "a hand that MAY riichi is not yet locked: +0x4b = %d"
                % rec[0x4B])
    # WARNING: A DECLARED RIICHI MUST LOCK THE CLIENT'S OWN CURSOR (2026-09-12): seen
    # live, a player could still discard any tile after declaring. +0x54 bit 1 binds
    # only the two MENU pickers (mahdisp.c:3492/3554); the ordinary press-X-on-
    # a-tile path (mahdisp.c:3607-3672) walks the HAND, not the flags, and the
    # byte that disables it is +0x4b -> DAT_003865bc + seat*0x34 (console.c:3400,
    # tested at mahdisp.c:3607). MjALLDATA +0x13a and MjNAKI +0x34 already carry
    # it; the draw record was clearing it back to zero every turn.
    hh.riichi = True
    hh.riichi_index = 0
    rec_r = tm.msg_tsumo(0, motion=2)
    ok &= check(rec_r[0x4B] == 1,
                "a riichi hand's own draw record sets the +0x4b cursor lock: %d"
                % rec_r[0x4B])
    ok &= check([rec_r[0x54 + i] for i in range(14)]
                == [0] * 13 + [2],
                "and only the drawn tile stays discardable at +0x54: %r"
                % [rec_r[0x54 + i] for i in range(14)])
    # VERIFIED: RIICHI TSUMOGIRI: +0x63 on top of the lock makes the client discard
    # slot 0xd with no input (mahdisp.c:3645; the third clause, DAT_003e4e08,
    # is written by NONE of the 4,146 decompiled functions and its .data image
    # holds 1, so the gate is ours). It may only be armed when the drawn tile
    # really IS at slot 13 and the seat has nothing to decide.
    ok &= check(rec_r[0x63] == 1,
                "a riichi hand with nothing to decide auto-discards its draw "
                "(+0x63): %d" % rec_r[0x63])
    ok &= check(hh.tiles[13] == hh.drawn,
                "and the tile the client will name -- slot 0xd, the only slot "
                "its locked cursor can be on -- IS the drawn one: %s vs %s"
                % (narration._tile(hh.tiles[13]), narration._tile(hh.drawn)))
    # A WINNING draw must reach the player: Tsumo is lit, so no auto-discard.
    hh.tiles[:] = ([mj.MAN + i for i in range(9)]
                   + [mj.PIN, mj.PIN + 1, mj.PIN + 2, mj.SOU + 8, mj.SOU + 8])
    hh.drawn = mj.SOU + 8
    rec_w = tm.msg_tsumo(0, motion=2)
    ok &= check(rec_w[0x4D + tm.MENU_TSUMO] == 1 and rec_w[0x63] == 0,
                "a riichi hand that can TSUMO keeps the lock but NOT the "
                "auto-discard -- the menu has to reach the player: menu=%d "
                "+0x63=%d" % (rec_w[0x4D + tm.MENU_TSUMO], rec_w[0x63]))
    ok &= check(rec_w[0x4B] == 1, "(the lock itself stays on: %d)" % rec_w[0x4B])
    # A POST-ANKAN rinshan draw is 11 tiles, so the drawn tile is NOT at slot
    # 13 and the client's fixed 0xd would name an empty slot -- withhold.
    hh.melds = [mj.Meld(mj.ANKAN, [mj.MAN] * 4, None, mj.MAN)]
    hh.tiles[:] = ([mj.MAN + i for i in range(1, 9)]
                   + [mj.PIN, mj.PIN + 1, mj.SOU + 4])
    hh.drawn = mj.SOU + 4
    rec_k = tm.msg_tsumo(0, motion=9)
    ok &= check(rec_k[0x4B] == 1 and rec_k[0x63] == 0,
                "a riichi hand mid-ankan keeps the lock but withholds the "
                "auto-discard -- its draw is at slot %d, not 0xd: +0x4b=%d "
                "+0x63=%d" % (len(hh.tiles) - 1, rec_k[0x4B], rec_k[0x63]))
    hh.melds = []
    hh.riichi = False
    hh.riichi_index = None
    # An OPEN hand may never riichi, even at tenpai.
    hh.melds = [mj.Meld(mj.CHI, [mj.MAN, mj.MAN + 1, mj.MAN + 2], 3, mj.MAN + 2)]
    hh.menzen = False
    ok &= check(tm._mymove_menu(0)[tm.MENU_RIICHI] == 0,
                "an open hand never offers Riichi")
    # Below 1000 points, riichi is unavailable even closed + tenpai.
    hh.melds = []
    hh.menzen = True
    km.scores[0] = 500
    ok &= check(tm._mymove_menu(0)[tm.MENU_RIICHI] == 0,
                "under 1000 points Riichi is unavailable")

    # --- the deal ack must NOT resync (2026-09-03 host-lockup regression) ----
    # 561a4fb1 made the HAIPAIACK emit an MjALLDATA that concealed opponents as
    # the back id 0x3F; the back has no 3D hand model, so the client crashed on
    # a null-geometry walk and every game froze at the deal, locking the host.
    # The deal animation (motion 10) already establishes the hands, so the ack
    # opens the first turn and sends NO resync. Guard the regression here.
    td = table.Table(12, seed=3)
    td.seat_player(0x4567, b"dealer", seat=0)
    td.fill_with_bots()
    deal = td.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                                  sub=3, length=0x18)[:0x18])
    ok &= check(deal and janwire.unpack(deal[0])["opcode"] == M.MjHAIPAI,
                "READY deals")
    out = td.handle(_client_ack(deal[0], 0))       # the HAIPAIACK
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(M.MjALLDATA not in ops,
                "the deal ack must NOT resync (host-lockup regression): %r"
                % [narration.M_NAME(o) for o in ops])
    ok &= check(M.MjTSUMO in ops, "the deal ack opens the first turn")
    # And no message may conceal a hand as the back id anywhere.
    for r in out:
        for cs in range(4):
            hbase = M.ALLDATA_HAND + cs * M.ALLDATA_HAND_STRIDE
            if len(r) >= hbase + 14:
                ok &= check(all(b != M.WIRE_BACK for b in r[hbase:hbase + 14]),
                            "no hand byte is the back id 0x3F")

    # --- the finished hanchan was WRITTEN DOWN -------------------------------
    # Before janstats this was the gap: the placings were computed and dropped,
    # so Level / Rank / Games Played / Money had nowhere to land.
    if janstats is not None:
        rec = janstats.load(0x1234)
        ok &= check(rec["games_played"] == 1,
                    "the finished hanchan reached the player record (%r games)"
                    % rec["games_played"])
        ok &= check(sum(rec["places"]) == 1 and len(rec["history"]) == 1,
                    "exactly one placing stored: %r" % (rec["places"],))
        for s in sorted(t.bots):
            ok &= check(not janstats.stats_exists(t.seats[s]),
                        "bot seat %d was NOT recorded -- its id is non-zero by "
                        "design and must not read as a person" % s)
        ok &= check(t._recorded, "the table knows it has recorded")
        again = janstats.load(0x1234)["games_played"]
        t.msg_results()
        ok &= check(janstats.load(0x1234)["games_played"] == again,
                    "a re-entered results message does NOT double-count")

        hist = t._history_rows()
        ok &= check(all(v == M.NO_RANK for row in hist for v in row),
                    "after one game every history row is NO_RANK -- the game "
                    "just played is row 0, not history: %r" % hist)

        # ...but a genuine SECOND hanchan at the same table must be recorded.
        # `Manager.by_member` keeps a Table for the life of the process, so this
        # is the ordinary case, not an edge one.
        t.start_game()
        ok &= check(not t._recorded, "start_game clears the record guard")
        t.game.scores = [30000, 25000, 25000, 20000]
        t.game.hands_played = 8
        t.msg_results()
        ok &= check(janstats.load(0x1234)["games_played"] == again + 1,
                    "the SECOND hanchan at the same table is recorded too "
                    "(%d games)" % janstats.load(0x1234)["games_played"])
        ok &= check(t._history_rows()[0][0] != M.NO_RANK,
                    "and now the FIRST game shows up as history: %r"
                    % t._history_rows()[0])

        # --- the history rows, and the sentinel that makes them safe ---------
        th = table.Table(9, seed=1)
        th.seat_player(0xBEEF, seat=0)
        th.fill_with_bots()
        janstats.record_game(0xBEEF, 1, 26000, 5.0)
        janstats.record_game(0xBEEF, 3, 12000, -30.0)
        rows = th._history_rows()
        ok &= check([r[0] for r in rows] == [3, 1, M.NO_RANK, M.NO_RANK],
                    "column 0 is that seat's own history, newest first: %r"
                    % [r[0] for r in rows])
        ok &= check(all(r[s] == M.NO_RANK for r in rows for s in (1, 2, 3)),
                    "a bot column holds no history")
        h2 = M.gameresult_half2(1, [[0, 1, 2, 3]] + rows)
        vals = struct.unpack_from("<20i", h2, 0x18)
        ok &= check(list(vals[:4]) == [0, 1, 2, 3], "row 0 is this game")
        ok &= check(vals[4] == 3 and vals[8] == 1,
                    "the history lands column-wise at +0x18+row*0x10: %r"
                    % (vals[:12],))
        # No emoji in a message that can be PRINTED: a Windows console is cp1252
        # and `print` of an astral character raises UnicodeEncodeError, which
        # turns a reported test failure into a crashed test run.
        ok &= check(vals[7] == -1,
                    "a blank slot reaches the wire SIGNED-NEGATIVE, not 0 -- "
                    "0 is FIRST PLACE and four zero rows read as four wins "
                    "(got %d)" % vals[7])
        ok &= check(struct.unpack_from("<I", h2, 0x18 + 7 * 4)[0] == 0xFFFFFFFF,
                    "and it is 0xFFFFFFFF on the wire")

        # persistence off -> the honest blank, not a row of wins
        _old_stats = os.environ.get("POL_JAN_STATS")
        knobs.STATS_ENABLE = False
        try:
            ok &= check(all(v == M.NO_RANK for row in th._history_rows()
                            for v in row),
                        "POL_JAN_STATS=0 gives four NO_RANK rows")
        finally:
            knobs.STATS_ENABLE = _old_stats != "0"

    # --- the WIN path, driven directly ---------------------------------------
    # The random hanchan above ends every hand in an exhaustive draw (a
    # tsumogiri client against three bots that only ever ron), so MjYAKUDISP is
    # never reached by luck. Force it: give seat 0 a hand that is one tile from
    # a win and have it declare the self-draw the client's own way.
    tw = table.Table(3, seed=1)
    tw.seat_player(0x777, seat=0)
    tw.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    k = tw.kyoku
    k.turn = 0
    h = k.hands[0]
    # 123m 456m 789m 123p 55s -- fourteen tiles, complete, and concealed, so
    # menzen tsumo alone is a yaku and the win cannot be refused for lack of one.
    h.tiles = [mj.MAN + i for i in range(9)] + \
              [mj.PIN + i for i in range(3)] + [mj.SOU + 4, mj.SOU + 4]
    h.melds = []
    h.menzen = True
    h.drawn = h.tiles[-1]
    h.riichi = False
    before = list(k.scores)
    out = tw.handle(_client_sute(tw._last_sent or M.tsumo(1, 0, h.tiles, 60), 0, 0,
                                 M.SUTE_TSUMO_AGARI))
    ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjYAKUDISP,
                "a declared tsumo produces MjYAKUDISP, got %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out])
    ok &= check(k.scores[0] > before[0],
                "and the winner is actually paid: %r -> %r" % (before, k.scores))
    # WARNING: THE WINNER SEAT LIVES AT +0x1a, NOT +0x18 (2026-09-04 live: a 30-fu
    # human tsumo laid the win animation on the player to the LEFT). The client
    # reveals _DAT_00451fa0 = record[+0x1a]; +0x18 is the han. Seat 0 won by
    # tsumo here, so +0x1a == 0 and +0x1c (ron flag) == 0, while +0x18 carries
    # the real han (>=1 for a menzen tsumo) -- never the seat.
    if out:
        yd = out[0]
        ok &= check(yd[0x1A] == 0,
                    "yakudisp winner seat at +0x1a is the actual winner (0): %d"
                    % yd[0x1A])
        ok &= check(yd[0x1C] == 0, "a tsumo sets +0x1c (ron flag) to 0: %d"
                    % yd[0x1C])
        won_score = tw.kyoku.result[1][0][1]
        ok &= check(yd[0x18] == min(255, won_score.han),
                    "and +0x18 carries the han, not the seat: %d vs han %d"
                    % (yd[0x18], won_score.han))
    if out:
        nxt = tw.handle(_client_ack(out[0], 0))
        ok &= check(nxt and janwire.unpack(nxt[0])["opcode"] == M.MjSEISAN,
                    "MjYAKUDISPACK -> MjSEISAN")
        if nxt:
            paid = struct.unpack_from("<4i", nxt[0], 0x18)
            ok &= check(list(paid) == list(k.scores),
                        "and the settlement carries the real scores: %r vs %r"
                        % (list(paid), k.scores))
            # WARNING: THE DEAL-STORM GUARD (2026-09-03, live): while we await the
            # SEISANACK the client re-sends the MjSUTE it thinks went unanswered.
            # That retry must NOT run the finished hand again and mint a fresh
            # MjSEISAN (four of them -> four SEISANACKs -> four dealt hands live).
            resend = tw.handle(_client_sute(nxt[0], 0, 0, M.SUTE_DISCARD))
            ok &= check(all(janwire.unpack(r)["opcode"] == M.MjSEISAN
                            for r in resend),
                        "a re-sent MjSUTE after the win re-nudges the awaited "
                        "MjSEISAN, it does not advance the hand: got %r"
                        % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in resend])
            ok &= check(janwire.unpack(resend[0])["f13"]
                        == janwire.unpack(nxt[0])["f13"] if resend else True,
                        "and the re-nudge is the SAME record (client dedups on "
                        "the sequence byte), not a freshly minted one")
            after = tw.handle(_client_ack(nxt[0], 0))
            ok &= check(after and janwire.unpack(after[-1])["opcode"] == M.MjHAIPAI,
                        "MjSEISANACK -> the next deal (last in the reply)")
            rsl = [r for r in after if janwire.unpack(r)["opcode"] == M.MjREADYSTATUS]
            ok &= check(len(rsl) == (1 if knobs.ISHUMAN_ENABLE else 0)
                        and (not rsl or (janwire.unpack(rsl[0])["sub"] == M.READY_SUB
                                         and bytes(rsl[0][0x18:0x1C]) == b"\x01\x01\x01\x01")),
                        "...and ONE MjREADYSTATUS on sub 2, all four ready "
                        "(one human, three bots): %r" % [r[0x18:0x1C] for r in rsl])
            ok &= check(tw._last_for.get(0) is not None
                        and janwire.unpack(tw._last_for[0])["opcode"] == M.MjHAIPAI,
                        "the gauge record did not displace the deal as the "
                        "re-nudge record")
            # And a re-sent SEISANACK (same retry timer) must be silent -- the
            # deal already happened on the first one.
            storm = tw.handle(_client_ack(nxt[0], 0))
            ok &= check(storm == [],
                        "a re-sent MjSEISANACK deals nothing: got %r"
                        % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in storm])

    # a hand the server does NOT agree is a win must not be paid out
    tr = table.Table(4, seed=2)
    tr.seat_player(0x778, seat=0)
    tr.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    tr.kyoku.turn = 0
    tr.kyoku.hands[0].tiles = [mj.MAN + (i % 9) for i in range(14)]
    tr.kyoku.hands[0].drawn = tr.kyoku.hands[0].tiles[-1]
    scores_before = list(tr.kyoku.scores)
    out = tr.handle(_client_sute(M.tsumo(1, 0, tr.kyoku.hands[0].tiles, 60), 0, 0,
                                 M.SUTE_TSUMO_AGARI))
    ok &= check(tr.kyoku.scores == scores_before,
                "a tsumo we cannot score pays nobody")
    ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjTSUMO,
                "and the seat is given its turn back rather than being stranded")
    ok &= check(any(e[0] == "refused-tsumo" for e in tr.log),
                "and the disagreement is LOGGED -- it is a scoring bug, not noise")

    # multi-ron is resolved in TURN ORDER from the discarder, not seat order:
    # it decides who takes the riichi sticks, and with double_ron off it decides
    # who wins at all (head bump).
    th = table.Table(5, seed=3)
    th.fill_with_bots()
    th.game = mj.Game(th.rules, rng=random.Random(3))
    th.kyoku = th.game.start_kyoku()
    th.legal_calls = lambda seat, tile, frm: {"ron": True}      # everyone can ron
    seen_order = []
    th.kyoku.win_ron = lambda seats, chankan=False, tile=None, from_seat=None: (
        seen_order.extend(seats) or [(seats[0], mj.Score([("x", 1)], 1, 30, 0,
                                                         {0: 0, 1: 0, 2: 0, 3: 0}))])
    th.on_win = lambda seat, score, ron_from=None: []
    th.resolve_bot_calls(mj.PIN, 2)
    ok &= check(seen_order == [3, 0, 1],
                "ron order from discarder 2 must be 3,0,1 -- got %r" % seen_order)

    # --- the escape hatch ----------------------------------------------------
    # Touching the file ends the game on the next message. It exists because the
    # client cannot leave a hand from its own side: only MjGAMEEND breaks the
    # in-game loop.
    import tempfile, time as _t
    esc = os.path.join(tempfile.gettempdir(), "jan_endgame_selftest.txt")
    if os.path.exists(esc):
        os.remove(esc)
    te = table.Table(8, seed=7)
    te.ESCAPE_FILE = esc
    te._escape_seen = te._escape_mtime()
    te.seat_player(0xE5C, seat=0)
    te.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    ok &= check(te.state == "playing", "a game is running")
    ack = _client_ack(te._last_sent, 0)
    ok &= check(not any(janwire.unpack(r)["opcode"] == M.MjGAMEEND
                        for r in te.handle(ack)),
                "no escape file -> the game continues")
    with open(esc, "w") as f:
        f.write("end")
    out = te.handle(_client_ack(te._last_sent, 0) or ack)
    ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjGAMEEND,
                "touching the file ends the game, got %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out])
    # and it must fire ONCE, not on every message after
    again = te.handle(ack)
    ok &= check(not any(janwire.unpack(r)["opcode"] == M.MjGAMEEND for r in again),
                "it fires once per touch, not forever")
    os.remove(esc)

    # --- graceful shutdown: the next line of a live game ends it cleanly -----
    # WARNING: The container-stop path (every deploy restarts authsess). SHUTDOWN is
    # set by responders.py's SIGTERM handler; a playing table then answers its
    # next inbound line with MjGAMEEND so the client drops to the menu instead
    # of freezing on the socket that is about to close.
    ts = table.Table(9, seed=17)
    ts.seat_player(0x5D07, seat=0)
    ts.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    ok &= check(ts.state == "playing", "a game is running before shutdown")
    _saved_shutdown = knobs.SHUTDOWN
    knobs.SHUTDOWN = True
    try:
        out = ts.handle(_client_ack(ts._last_sent, 0) or
                        janwire.pack(opcode=M.MjHAIPAIACK, src=0, dst=4, f16=1,
                                     sub=3, length=0x18)[:0x18])
        ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjGAMEEND,
                    "SHUTDOWN ends the live game with MjGAMEEND on its next "
                    "line: %r" % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out])
    finally:
        knobs.SHUTDOWN = _saved_shutdown
    # ...and the SWEEPER ends a live table whose clients are silent (a client
    # waiting for its turn sends nothing, so "its next line" never comes).
    ts2 = table.Table(10, seed=18)
    ts2.seat_player(0x5D08, seat=0)
    ts2.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    knobs.SHUTDOWN = True
    try:
        swept_end = ts2.tick(ts2.clock())
        ok &= check(swept_end and janwire.unpack(swept_end[0])["opcode"] == M.MjGAMEEND
                    and ts2.state == "over",
                    "SHUTDOWN: the sweeper tick ends a silent live table with "
                    "MjGAMEEND (queued for the idle push): %r"
                    % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in swept_end])
        ok &= check(ts2.tick(ts2.clock()) == [], "...once")
    finally:
        knobs.SHUTDOWN = _saved_shutdown
    # and with SHUTDOWN clear again, a fresh table plays normally
    ok &= check(not knobs.SHUTDOWN, "SHUTDOWN restored for the rest of the selftest")

    # --- an in-game message for a game we no longer have (restart recovery) --
    # WARNING: 2026-09-04 LIVE CRASH: a deploy restarted authsess and wiped the
    # Manager; the console kept playing, its next MjSUTE built a fresh idle
    # table and raised 'NoneType' has no attribute 'hands' -- swallowed, so the
    # server went silent and the console froze. A stray in-game message on a
    # gameless table must answer MjGAMEEND (clean exit to the menu), and
    # on_sute must never touch k when it is None.
    to_ = table.Table(11, seed=19)
    to_.seat_player(0xDEAD, seat=0)
    to_.fill_with_bots()          # table_for's shape: seated, botted, NOT started
    ok &= check(to_.kyoku is None and to_.state != "playing",
                "a fresh table has no active game")
    _sute = bytearray(janwire.pack(opcode=M.MjSUTE, f13=1, src=0, dst=4, f16=1,
                                   sub=3, length=0x20))
    struct.pack_into("<I", _sute, 0x18, 0x0d)
    out = to_.handle(bytes(_sute))          # must NOT raise
    ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjGAMEEND,
                "an in-game message on a gameless table answers MjGAMEEND, "
                "not a crash: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out])
    # and on_sute itself is safe if reached with k None (state still 'playing')
    to_.state = "playing"
    ok &= check(to_.on_sute(bytes(_sute)) is not None
                or to_.on_sute(bytes(_sute)) == [],
                "on_sute does not raise when kyoku is None")

    # a spectator's ack is MjGALLEYACK on sub 6 with its SLOT at +0x14 --
    # never a per-opcode ack, never a seat (console.c:2566-2575)
    spec = _client_ack(haipai, 0, spectator=True, slot=2)
    _sh = janwire.unpack(spec)
    ok &= check(_sh["opcode"] == M.MjGALLEYACK == 0x30 and _sh["sub"] == 6
                and spec[0x14] == 2 and spec[0x15] == 4 and spec[0x16] == 1
                and _sh["f13"] == janwire.unpack(haipai)["f13"] and len(spec) == 0x18,
                "the spectator ack is MjGALLEYACK/sub 6/slot at +0x14, seq echoed")
    ok &= check(_client_ack(M.gameend(9), 0, spectator=True)[0x16] == 0,
                "...and f16=0 for the BYE-equivalent after MjGAMEEND")

    # a resync request is answered with the full 320-byte state
    t2 = table.Table(2, seed=5)
    t2.seat_player(0x99, seat=1)
    t2.handle(janwire.pack(opcode=M.MjREADY, src=1, dst=4, length=0x18)[:0x18])
    ad = t2.handle(janwire.pack(opcode=M.MjALLDATA, src=1, dst=4, f16=1, sub=3,
                                length=0x20))
    ok &= check(len(ad) == 1 and len(ad[0]) == M.ALLDATA_LEN,
                "MjALLDATA -> a 0x140-byte resync")
    if ad:
        h = janwire.unpack(ad[0])
        ok &= check(h["length"] == M.ALLDATA_LEN, "and it declares its own length")
        ok &= check((ad[0][0x13E] >> 1) == min(0x7F, t2.wall_count()),
                    "the resync carries the live wall count")

    # --- opponents are CONCEALED in the resync (the vanish/reappear fix) -----
    # WARNING: An MjALLDATA hand byte cannot carry a face-down bit (the parser sends
    # byte 0x80 -> u16 0x8000 = red sheet, never the 0x80 the renderer tests),
    # so the only concealment a resync has is to send the hand EMPTY. Before
    # this, opponents were sent face-up and the first per-turn resync drew
    # every opponent's face -- the live symptom "they reappear after the first
    # move". The local seat keeps its tiles; the other three go to zero.
    def _hand_block(rec, seat):
        base = M.ALLDATA_HAND + seat * M.ALLDATA_HAND_STRIDE
        return [b for b in rec[base:base + 14] if b]
    if ad and knobs.CONCEAL_RESYNC:
        ok &= check(_hand_block(ad[0], 1),
                    "the LOCAL seat's hand is present in its own resync")
        ok &= check(all(not _hand_block(ad[0], s) for s in (0, 2, 3)),
                    "every non-local seat's hand is EMPTY -- no face leak: %r"
                    % {s: _hand_block(ad[0], s) for s in (0, 2, 3)})
    # A subtype-3 discard by a BOT keeps THAT seat's hand (the tile flies from
    # it) but still blanks the other two opponents.
    if knobs.CONCEAL_RESYNC:
        tcz = table.Table(24, seed=71)
        tcz.seat_player(0x5151, seat=0)
        tcz.fill_with_bots()
        tcz.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                                sub=3, length=0x18)[:0x18])
        kcz = tcz.kyoku
        dtile = kcz.hands[2].tiles[0]
        d3 = tcz.msg_alldata(discard_event=(dtile, 2))
        ok &= check(_hand_block(d3, 2),
                    "a subtype-3 discard KEEPS the discarder's hand -- the "
                    "tile flies from it (the 44e4fbbd slingshot class)")
        ok &= check(not _hand_block(d3, 1) and not _hand_block(d3, 3),
                    "but the two non-animating opponents are still blank")
        ok &= check(_hand_block(d3, 0),
                    "and the local seat is never blanked")

    # --- the deadline for a seat that says NOTHING --------------------------
    # Not a turn clock: the client has its own (LimitTimeManager) and plays the
    # default move when it expires. This is for a client that is GONE -- no ack,
    # no skip, nothing. It must fire strictly later than the client's own clock
    # or we would play a tile for someone who is merely thinking.
    td = table.Table(6, seed=11)
    td.seat_player(0xDEAD, seat=0)
    fake = {"t": 1000.0}
    td.clock = lambda: fake["t"]
    td.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    out = td.handle(_client_ack(td._last_sent, 0))          # -> MjTSUMO for seat 0
    ok &= check(out and janwire.unpack(out[-1])["opcode"] == M.MjTSUMO,
                "seat 0 is on the clock")
    ok &= check(td.deadline() > td.rules.wait_time,
                "our deadline (%.0fs) must outlast the CLIENT's own turn clock "
                "(%ds) -- otherwise we discard for a player who is thinking"
                % (td.deadline(), td.rules.wait_time))
    fake["t"] += td.deadline() - 1
    ok &= check(td.tick(fake["t"]) == [], "nothing fires one second early")
    before_pond = len(td.kyoku.hands[0].pond)
    fake["t"] += 2
    fired = td.tick(fake["t"])
    ok &= check(bool(fired), "the deadline fires once it is past")
    ok &= check(len(td.kyoku.hands[0].pond) == before_pond + 1,
                "and the silent seat's default discard was played for it")
    ok &= check(any(e[0] == "timeout" for e in td.log), "and it is logged")

    # --- a SWEPT call offer must not be answerable afterwards ---------------
    # WARNING: THE 2026-09-04 LIVE BUG. A player chose Ron and watched a BOT win
    # by tsumo instead. Mechanism: `wait_time` 60 + TURN_GRACE 15 = a 75s
    # deadline, the countdown window does not render (so there is no warning),
    # and a background sweeper thread fires with no message from anyone. It
    # passes the call and runs the game forward. The player's click then
    # arrived at a table that had moved on -- and the old code, finding
    # `pending_naki` already consumed, fell back to `(k.last_discard, {})` and
    # scored the ron against WHATEVER TILE was at the end of the pond by then.
    # That never completes the hand, so the ron was refused, and the refusal
    # path advanced the turn again.
    tn = table.Table(14, seed=23)
    tn.seat_player(0xC0FF, seat=0)
    tn.fill_with_bots()
    fk = {"t": 5000.0}
    tn.clock = lambda: fk["t"]
    tn.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    kn = tn.kyoku
    kn.hands[0].tiles[:] = [mj.SOU + 5, mj.SOU + 5, mj.SOU + 4, mj.SOU + 6,
                            mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.PIN, mj.PIN + 1,
                            mj.PIN + 2, mj.HONOR, mj.HONOR, mj.HONOR + 1]
    kn.last_discard_seat, kn.last_discard = 3, mj.SOU + 5
    kn.hands[3].pond.append(mj.SOU + 5)
    offer = tn.msg_naki(mj.SOU + 5, {0: {"pon": True}})
    ok &= check(tn.pending_naki is not None, "the offer is live")
    fk["t"] += tn.deadline() + 1
    tn.tick(fk["t"])                       # the sweeper passes it and moves on
    ok &= check(tn.pending_naki is None, "the sweeper closed the offer")
    melds_before = len(tn.kyoku.hands[0].melds) if tn.kyoku else 0
    pond_before = [len(tn.kyoku.hands[s].pond) for s in range(4)] if tn.kyoku else []
    late = tn.handle(_client_naki(offer, 0, M.NAKI_PON | M.tile_u16(mj.SOU + 5)))
    ok &= check(any(e[0] == "stale-nakiack" for e in tn.log),
                "a call-ack with no live offer is logged as STALE: %r"
                % [e[0] for e in tn.log[-4:]])
    ok &= check(tn.kyoku is None
                or (len(tn.kyoku.hands[0].melds) == melds_before
                    and [len(tn.kyoku.hands[s].pond) for s in range(4)] == pond_before),
                "and it mutates NOTHING -- it is a retransmission, not a "
                "decision")
    ok &= check(late == [],
                "and answers with NOTHING rather than re-nudging `_last_sent` "
                "-- that field is per-table, not per-seat, and a re-nudge "
                "bypasses routing, so with two humans it delivers one seat's "
                "record to the other: %r" % late)

    # And a ron is scored against the tile we OFFERED, never the pond tail.
    tro = table.Table(15, seed=29)
    tro.seat_player(0xB00C, seat=0)
    tro.fill_with_bots()
    tro.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                            sub=3, length=0x18)[:0x18])
    kro = tro.kyoku
    # 123m 456m 789m 123p 9s9s waiting on nothing in particular: give seat 0 a
    # hand that is complete on 9s and let the pond tail be something else.
    kro.hands[0].tiles[:] = ([mj.MAN + i for i in range(9)]
                             + [mj.PIN, mj.PIN + 1, mj.PIN + 2, mj.SOU + 8])
    kro.hands[0].menzen = True
    kro.hands[0].riichi = True             # riichi alone is a yaku for the ron
    kro.hands[0].riichi_index = 0
    kro.hands[3].pond.append(mj.SOU + 8)
    kro.last_discard_seat, kro.last_discard = 3, mj.SOU + 8
    offered = mj.SOU + 8
    ok &= check("ron" in tro.legal_calls(0, offered, 3),
                "seat 0 is offered the ron")
    # Now move the board on underneath it, exactly as the sweeper would.
    kro.hands[2].pond.append(mj.PIN + 5)
    kro.last_discard_seat, kro.last_discard = 2, mj.PIN + 5
    won = kro.win_ron([0], tile=offered, from_seat=3)
    ok &= check(bool(won),
                "win_ron honours the OFFERED tile even after the pond tail "
                "moved -- scoring k.last_discard is what refused a legitimate "
                "ron and handed the hand to a bot")

    # --- A DOUBLE RON HAS TWO WIN SCREENS -----------------------------------
    # WARNING: 2026-09-12, live: a player declared Ron and the win screen revealed
    # the hand of the player to their LEFT. `_settle` pays every winner, but
    # each call site showed `wins[0]` only, so with double ron on the seat that
    # lost the turn-order tie was paid and never shown. MjYAKUDISP names ONE
    # winner (+0x1a); the extra ones ride the ack ladder one screen at a time.
    td = table.Table(41, seed=211)
    td.seat_player(0xD001, b"dbl", seat=0)
    td.fill_with_bots()
    td.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])

    def _dops(recs):
        return [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in recs or ()]

    kd = td.kyoku
    kd.rules.double_ron = True
    dtile = mj.SOU + 8
    for s in (0, 2):                        # both complete on 9s, riichi = yaku
        hd = kd.hands[s]
        hd.tiles[:] = ([mj.MAN + i for i in range(9)]
                       + [mj.PIN, mj.PIN + 1, mj.PIN + 2, mj.SOU + 8])
        hd.melds, hd.menzen = [], True
        hd.riichi, hd.riichi_index = True, 0
        del hd.pond[:]
    kd.hands[3].pond.append(dtile)
    kd.last_discard_seat, kd.last_discard = 3, dtile
    before_d = list(kd.scores)
    # Seat 0 answered Ron; seat 2 is a bot and is asked here, in turn order
    # from the discarder -- so seat 0 (the discarder's shimocha) is first.
    out_d = td._resolve_naki(dtile, {0: {"ron": True}}, {0: ("ron", dtile)}, 3)
    ok &= check(_dops(out_d) == ["MjALLDATA", "MjYAKUDISP"]
                and out_d[-1][0x1A] == 0,
                "the first win screen is the seat closest to the discarder "
                "(0): %r / winner %r"
                % (_dops(out_d), out_d[-1][0x1A] if out_d else None))
    ok &= check(len(td._pending_wins) == 1 and td._pending_wins[0][0] == 2,
                "and the second winner (seat 2) is queued, not dropped: %r"
                % [s for s, _sc in td._pending_wins])
    ok &= check(kd.scores[0] > before_d[0] and kd.scores[2] > before_d[2],
                "both winners were PAID (they always were -- only the screen "
                "was missing): %r -> %r" % (before_d, list(kd.scores)))
    out_d2 = td._ack_advance(M.MjYAKUDISP)
    ok &= check(_dops(out_d2) == ["MjALLDATA", "MjYAKUDISP"]
                and out_d2[-1][0x1A] == 2,
                "the ack of the first screen pulls the SECOND winner's, not "
                "MjSEISAN: %r / winner %r"
                % (_dops(out_d2), out_d2[-1][0x1A] if out_d2 else None))
    ok &= check(td._awaiting == (M.MjYAKUDISP, None),
                "which is awaited in its own right: %r" % (td._awaiting,))
    ok &= check(_dops(td._ack_advance(M.MjYAKUDISP)) == ["MjSEISAN"]
                and not td._pending_wins,
                "and only the LAST ack moves the ladder on to the settlement")

    # --- the BACKGROUND sweeper must not throw its records away -------------
    # WARNING: 2026-09-04, live: a two-human table where both consoles sat stuck --
    # one with a hand it had already played, one with no tiles at all. The
    # background sweeper is the only thing that advances a table nobody is
    # talking on, and it handed its records to a caller whose own log line says
    # "no peer to send it to on this path". The state moved; nobody was told.
    tq = table.Table(16, seed=31)
    tq.seat_player(0xAAA1, seat=0)
    tq.seat_player(0xAAA2, seat=1)
    fq = {"t": 9000.0}
    tq.clock = lambda: fq["t"]
    mq = manager.Manager()
    mq.clock = lambda: fq["t"]
    mq.tables[tq.id] = tq
    tq.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    tq.handle(_client_ack(tq._last_sent, 0))         # -> someone is on the clock
    for s in (0, 1):
        tq.take_outbox(s)                            # start from empty
    awaited = tq._awaiting[1] if tq._awaiting else None
    fq["t"] += tq.deadline() + 1
    swept = mq.tick(fq["t"])
    ok &= check(bool(swept), "the background sweep fired")
    delivered = sum(len(tq.outbox.get(s) or []) for s in sorted(tq.live))
    ok &= check(delivered > 0,
                "and its records are QUEUED for the live seats (%d) rather "
                "than dropped -- a swept table that tells nobody is two stuck "
                "clients" % delivered)
    ok &= check(not any(e[0] == "tick-route-bypass" for e in tq.log),
                "the swept records routed cleanly: %r"
                % [e for e in tq.log if e[0] == "tick-route-bypass"])
    # And the seat that was awaited is among those told.
    if awaited is not None:
        ok &= check(len(tq.outbox.get(awaited) or []) > 0,
                    "the seat we were waiting on is told what happened")

    # --- THE BOTS CALL, and their call ANIMATES -----------------------------
    # WARNING: Until 2026-09-04 `Bot.calls` said "ron or nothing" and had NO CALLERS,
    # so a CPU never pon'd or chi'd in any hand -- it conceded every discard.
    # Seen live: "a CPU called tsumo and won but they never chi or
    # ponned which is just... odd?". A bot that cannot call is not an opponent.
    tb = table.Table(17, seed=41)
    tb.seat_player(0xB07C, seat=0)
    tb.fill_with_bots()
    tb.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    kb = tb.kyoku
    # Seat 1 holds a pair of green dragons: ponning them is an instant yakuhai,
    # which is the least arguable call in mahjong.
    # WARNING: It must be a hand the pon actually HELPS. The first draft of this test
    # used a tenpai hand, where ponning the pair destroys the wait and the bot
    # was right to refuse -- the test was wrong, not the policy.
    # (shanten 3 -> 2 on the pon; found by search, not by eye -- with too many
    # partials the shanten formula's cap binds and the pon gains nothing.)
    kb.hands[1].tiles[:] = [mj.HATSU, mj.HATSU, mj.SOU + 2, mj.PIN,
                            mj.PIN + 3, mj.CHUN, mj.MAN + 1, mj.MAN + 2,
                            mj.SOU + 8, mj.PIN + 8, mj.MAN + 3, mj.PIN + 2,
                            mj.SOU]
    kb.hands[1].menzen = True
    kb.turn = 0
    kb.hands[0].tiles[-1] = mj.HATSU
    kb.discard(0, tile=mj.HATSU)
    out = tb.resolve_bot_calls(mj.HATSU, 0)
    ok &= check(out is not None, "a bot takes an obvious yakuhai pon")
    if out:
        ops = [janwire.unpack(r)["opcode"] for r in out]
        ad = next((r for r in out
                   if janwire.unpack(r)["opcode"] == M.MjALLDATA
                   and r[0x18] in (4, 5)), None)
        ok &= check(ad is not None,
                    "and it rides the SAME call motion the human's does -- a "
                    "bot pon must animate too: %r" % [narration.M_NAME(o) for o in ops])
        if ad is not None:
            ok &= check(ad[0x18] == 4 and ad[0x22] == 1 and ad[0x1A] == 0,
                        "motion 4, caller seat 1, discarder seat 0: "
                        "%d/%d/%d" % (ad[0x18], ad[0x22], ad[0x1A]))
        ok &= check(len(kb.hands[1].melds) == 1
                    and kb.hands[1].melds[0].kind == mj.PON,
                    "the meld is really on the bot's hand")
    # A bot must NOT open a hand that can no longer score: a chi that leaves no
    # yaku path is how a bot bricks itself for the rest of the hand.
    tn2 = table.Table(18, seed=43)
    tn2.seat_player(0xB07D, seat=0)
    tn2.fill_with_bots()
    tn2.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                            sub=3, length=0x18)[:0x18])
    kn2 = tn2.kyoku
    h1 = kn2.hands[1]
    # A 4s5s6s chi: no terminal in the meld, so it cannot be chanta; three
    # suits, so it cannot be honitsu; and four terminals/honours still in hand
    # to shed, so it is not a plausible tanyao either. WARNING: The first draft used a
    # 1s2s3s chi and the bot took it -- correctly: a terminal run is exactly
    # what chanta is made of. The yaku that disqualifies tanyao QUALIFIES that
    # one, which is the whole reason the first cut of this policy called
    # nothing.
    h1.tiles[:] = [mj.SOU + 4, mj.SOU + 5, mj.MAN, mj.MAN + 8, mj.EAST,
                   mj.PIN + 2, mj.PIN + 4, mj.PIN + 6, mj.MAN + 1,
                   mj.MAN + 3, mj.MAN + 5, mj.PIN + 7, mj.PIN + 8]
    h1.menzen = True
    h1.melds = []
    bot1 = tn2.bots.get(1) or bots.Bot(1)
    ok &= check(bot1.calls(kn2, mj.SOU + 3, {"chi": True}) is None,
                "a bot passes a chi that would leave its hand with no yaku")

    # --- MULTI-SEAT CALL ARBITRATION (2026-09-04) ---------------------------
    # WARNING: The load-bearing two-human bug:
    # `pending_naki` was consumed by the FIRST ack, so with two humans offered
    # the same discard a fast pon beat a slow ron outright and the second
    # seat's genuine answer was dropped as stale. Claims on one discard must
    # resolve TOGETHER: ron > kan/pon > chi, rons in turn order (head bump /
    # double ron), and a bot's claim competes too.
    def _ron_hand(hd):
        # 123/456/789m + 123p + 9s, complete on the claimed 9s; riichi alone
        # is the yaku -- the exact construction the win_ron test above proved.
        hd.tiles[:] = ([mj.MAN + i for i in range(9)]
                       + [mj.PIN, mj.PIN + 1, mj.PIN + 2, mj.SOU + 8])
        hd.menzen = True
        hd.riichi = True
        hd.riichi_index = 0

    PON_HAND = [mj.SOU + 8, mj.SOU + 8, mj.SOU + 4, mj.SOU + 5,
                mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.PIN, mj.PIN + 1,
                mj.PIN + 2, mj.HONOR, mj.HONOR, mj.HONOR + 1]

    ta = table.Table(19, seed=47)
    ta.seat_player(0xA001, seat=0)
    ta.seat_player(0xA002, seat=1)
    ta.fill_with_bots()
    ta.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    ka = ta.kyoku
    ka.hands[0].tiles[:] = list(PON_HAND)
    _ron_hand(ka.hands[1])
    ka.last_discard_seat, ka.last_discard = 3, mj.SOU + 8
    ka.hands[3].pond.append(mj.SOU + 8)
    offer = ta.msg_naki(mj.SOU + 8, {0: {"pon": True}, 1: {"ron": True}})
    first = ta.handle(_client_naki(offer, 0,
                                   M.NAKI_PON | M.tile_u16(mj.SOU + 8)))
    ok &= check(first == [] and ta.pending_naki is not None,
                "a pon ack is HELD while a seat that could ron is still "
                "deciding: %r" % first)
    second = ta.handle(_client_naki(offer, 1, M.NAKI_RON))
    ops = [janwire.unpack(r)["opcode"] for r in second]
    # A ron's records: the win-reveal MjALLDATA (motion 0, the winner's hand
    # with the claimed tile as its 14th) and then the MjYAKUDISP.
    ok &= check(ops == [M.MjALLDATA, M.MjYAKUDISP] and second[0][0x18] == 0,
                "and the LATER ron outranks the earlier pon: %r"
                % [narration.M_NAME(o) for o in ops])
    if ops == [M.MjALLDATA, M.MjYAKUDISP]:
        wr = second[0]
        row = [wr[M.ALLDATA_HAND + 1 * M.ALLDATA_HAND_STRIDE + i] & 0x3F
               for i in range(14)]
        ok &= check(sum(1 for b in row if b) == 14
                    and M.tile_byte(mj.SOU + 8) in row,
                    "the win reveal carries the winner's 14 tiles incl. the "
                    "ronned 9s: %r" % row)
    ok &= check(not ka.hands[0].melds,
                "the pon was never formed -- the ron takes the discard")
    ok &= check(ta.pending_naki is None, "the offer is closed")

    # The reverse order must NOT wait: a ron on file dominates a seat that can
    # only pon, so it resolves on the spot.
    tb3 = table.Table(20, seed=53)
    tb3.seat_player(0xA003, seat=0)
    tb3.seat_player(0xA004, seat=1)
    tb3.fill_with_bots()
    tb3.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                            sub=3, length=0x18)[:0x18])
    kb3 = tb3.kyoku
    kb3.hands[0].tiles[:] = list(PON_HAND)
    _ron_hand(kb3.hands[1])
    kb3.last_discard_seat, kb3.last_discard = 3, mj.SOU + 8
    kb3.hands[3].pond.append(mj.SOU + 8)
    offer3 = tb3.msg_naki(mj.SOU + 8, {0: {"pon": True}, 1: {"ron": True}})
    out3 = tb3.handle(_client_naki(offer3, 1, M.NAKI_RON))
    ok &= check([janwire.unpack(r)["opcode"] for r in out3]
                == [M.MjALLDATA, M.MjYAKUDISP] and out3[0][0x18] == 0,
                "a ron resolves IMMEDIATELY when no outstanding seat can "
                "outrank it -- pon potential does not hold up a ron: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out3])

    # A BOT's ron must outrank a human's granted pon. Until arbitration, a
    # human answer closed the offer without ever asking the bots.
    tb4 = table.Table(21, seed=59)
    tb4.seat_player(0xA005, seat=0)
    tb4.fill_with_bots()
    tb4.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                            sub=3, length=0x18)[:0x18])
    kb4 = tb4.kyoku
    kb4.hands[0].tiles[:] = list(PON_HAND)
    _ron_hand(kb4.hands[2])            # the ron hand belongs to a BOT
    kb4.last_discard_seat, kb4.last_discard = 3, mj.SOU + 8
    kb4.hands[3].pond.append(mj.SOU + 8)
    ok &= check("ron" in tb4.legal_calls(2, mj.SOU + 8, 3),
                "the bot's ron is legal on the claimed tile")
    offer4 = tb4.msg_naki(mj.SOU + 8, {0: {"pon": True}})
    out4 = tb4.handle(_client_naki(offer4, 0,
                                   M.NAKI_PON | M.tile_u16(mj.SOU + 8)))
    ok &= check([janwire.unpack(r)["opcode"] for r in out4]
                == [M.MjALLDATA, M.MjYAKUDISP] and out4[0][0x18] == 0
                and not kb4.hands[0].melds,
                "a bot's ron outranks the human's pon at resolution: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in out4])

    # And the SWEEPER resolves the answers on file instead of discarding them:
    # seat 1's ron waiting on seat 0's silence is still a ron.
    tw = table.Table(22, seed=61)
    tw.seat_player(0xA006, seat=0)
    tw.seat_player(0xA007, seat=1)
    tw.fill_with_bots()
    fw = {"t": 12000.0}
    tw.clock = lambda: fw["t"]
    tw.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    kw = tw.kyoku
    _ron_hand(kw.hands[0])             # BOTH humans can ron -> one must wait
    hw = kw.hands[1]
    hw.tiles[:] = ([mj.PIN + i for i in range(9)]
                   + [mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.SOU + 8])
    hw.menzen = True
    hw.riichi = True
    hw.riichi_index = 0
    kw.last_discard_seat, kw.last_discard = 3, mj.SOU + 8
    kw.hands[3].pond.append(mj.SOU + 8)
    offerw = tw.msg_naki(mj.SOU + 8, {0: {"ron": True}, 1: {"ron": True}})
    held = tw.handle(_client_naki(offerw, 1, M.NAKI_RON))
    ok &= check(held == [] and tw.pending_naki is not None,
                "a ron holds while ANOTHER seat's ron is still possible -- "
                "head bump / double ron must see both")
    fw["t"] += tw.deadline() + 1
    swept = tw.tick(fw["t"])
    ok &= check(any(janwire.unpack(r)["opcode"] == M.MjYAKUDISP for r in swept),
                "and the sweeper RESOLVES the ron on file rather than "
                "sweeping it away: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in swept])

    # --- a mid-game QUIT substitutes, it does not strand the table ----------
    # WARNING: MjBYE used to set state="finished" unconditionally -- with two humans,
    # one player quitting mid-hand killed the whole table and every later line
    # from the OTHER human got silence (seen live: "it doesn't seem to quite
    # know how to handle someone leaving"). drop_seat existed for exactly this
    # and HAD NO CALLERS -- the same pattern as Bot.calls before 2026-09-04.
    tv = table.Table(23, seed=67)
    tv.seat_player(0xA008, seat=0)
    tv.seat_player(0xA009, seat=1)
    tv.fill_with_bots()
    tv.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])
    tv.handle(_client_ack(tv._last_sent, 0))       # open the first turn
    aw = tv._awaiting
    ok &= check(bool(aw) and aw[0] == M.MjTSUMO and aw[1] in (0, 1),
                "a human is on the clock: %r" % (aw,))
    quitter = aw[1]
    pond_before = len(tv.kyoku.hands[quitter].pond)
    outq = tv.handle(janwire.pack(opcode=M.MjBYE, f13=0, src=quitter, dst=4,
                                  f16=0, sub=3, length=0x18)[:0x18])
    ok &= check(tv.state == "playing",
                "a quit with another human at the table does NOT finish it")
    ok &= check(any(janwire.unpack(r)["opcode"] == M.MjMEMBERLEAVE
                    for r in outq),
                "the other seats are TOLD (MjMEMBERLEAVE): %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in outq])
    ok &= check(quitter not in tv.live and quitter in tv.bots
                and quitter in tv.dropped,
                "and the seat plays on as a bot")
    ok &= check(tv.kyoku is None or tv.kyoku.result is not None
                or len(tv.kyoku.hands[quitter].pond) > pond_before,
                "the turn the leaver was holding was played out IMMEDIATELY "
                "-- an `_awaiting` naming a dead seat never ticks and would "
                "stall the hand forever")
    ok &= check(not (tv._awaiting and tv._awaiting[1] == quitter),
                "nothing is left waiting on the departed seat: %r"
                % (tv._awaiting,))
    # The LAST human leaving ends the table exactly as before.
    other = 1 - quitter
    outl = tv.handle(janwire.pack(opcode=M.MjBYE, f13=0, src=other, dst=4,
                                  f16=0, sub=3, length=0x18)[:0x18])
    ok &= check(tv.state == "finished" and outl == [],
                "the last human's MjBYE finishes the table")

    # A quit while an offer is open: the leaver's silence becomes a pass and
    # the answers on file still resolve -- same contract as the sweeper.
    tv2 = table.Table(25, seed=73)
    tv2.seat_player(0xA00C, seat=0)
    tv2.seat_player(0xA00D, seat=1)
    tv2.fill_with_bots()
    tv2.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                            sub=3, length=0x18)[:0x18])
    kv2 = tv2.kyoku
    _ron_hand(kv2.hands[1])
    hv0 = kv2.hands[0]                     # both seats can ron -> one must wait
    hv0.tiles[:] = ([mj.PIN + i for i in range(9)]
                    + [mj.MAN, mj.MAN + 1, mj.MAN + 2, mj.SOU + 8])
    hv0.menzen = True
    hv0.riichi = True
    hv0.riichi_index = 0
    kv2.last_discard_seat, kv2.last_discard = 3, mj.SOU + 8
    kv2.hands[3].pond.append(mj.SOU + 8)
    offv = tv2.msg_naki(mj.SOU + 8, {0: {"ron": True}, 1: {"ron": True}})
    heldv = tv2.handle(_client_naki(offv, 1, M.NAKI_RON))
    ok &= check(heldv == [] and tv2.pending_naki is not None,
                "the ron is held while the other seat's ron is possible")
    outv = tv2.handle(janwire.pack(opcode=M.MjBYE, f13=0, src=0, dst=4,
                                   f16=0, sub=3, length=0x18)[:0x18])
    ok &= check(tv2.pending_naki is None,
                "the quit closes the leaver's half of the offer")
    ok &= check(tv2.kyoku.result is not None
                or any(janwire.unpack(r)["opcode"] == M.MjYAKUDISP
                       for r in outv),
                "and the ron already on file is RESOLVED, not dropped: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in outv])

    # --- a seat leaving ------------------------------------------------------
    tl = table.Table(7, seed=12)
    tl.seat_player(0xF00D, seat=0)
    tl.seat_player(0xBEEF, seat=1)
    tl.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    out = tl.drop_seat(1, "connection lost")
    ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjMEMBERLEAVE,
                "dropping a seat announces MjMEMBERLEAVE")
    ok &= check(1 not in tl.live and 1 in tl.bots,
                "and the seat keeps playing, automated -- the hand is not lost")
    ok &= check(tl.drop_seat(1) == [], "dropping it twice announces nothing")

    ans = bytearray(janwire.pack(opcode=M.MjMEMBERLEAVEANSER, src=0, dst=4,
                                 f16=1, sub=0, payload=0xF00D,
                                 length=M.MEMBERLEAVE_ANSER_LEN))
    ans += bytes(M.MEMBERLEAVE_ANSER_LEN - len(ans))
    struct.pack_into("<I", ans, 0x20, 1)
    tl.handle(bytes(ans))
    ok &= check(any(e[0] == "leave-answer" for e in tl.log),
                "the other seats' answers are recorded")

    # the manager routes by member id
    mgr = manager.Manager()
    a = mgr.table_for(0xAAA)
    b = mgr.table_for(0xBBB)
    ok &= check(a is not b and mgr.table_for(0xAAA) is a,
                "each member gets its own table, stably")

    # WARNING: A SLOW HUMAN MUST NOT BE SWEPT BY THEIR OWN MESSAGE (2026-09-03, live).
    # The wrapper swept the table deadline BEFORE handling the inbound, so a turn
    # that ran past the 45s deadline auto-discarded the drawn tile AND THEN the
    # player's real discard landed on top -- two advance() passes, duplicated bot
    # turns and a second call offer ("the tile blinks and I can only hit
    # confirm"). A message FROM the awaited seat is that seat answering, not
    # going silent, so the sweep must skip it.
    clk = {"t": 9000.0}
    ms = manager.Manager()
    ms.clock = lambda: clk["t"]
    tt = ms.table_for(0xADD5)
    tt.clock = lambda: clk["t"]
    ms.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18],
              member_id=0xADD5)
    ms.handle(_client_ack(tt._last_sent, 0), member_id=0xADD5)   # -> TSUMO, seat 0
    ok &= check(tt._awaiting == (M.MjTSUMO, 0),
                "the slow-human table is awaiting seat 0's discard")
    pond0 = len(tt.kyoku.hands[0].pond)
    clk["t"] += tt.deadline() + 100                    # the human dawdles, badly
    drawn = tt.kyoku.hands[0].drawn
    idx = tt.kyoku.hands[0].tiles.index(drawn) \
        if drawn in tt.kyoku.hands[0].tiles else 13
    ms.handle(_client_sute(tt._last_sent, 0, idx, M.SUTE_DISCARD), member_id=0xADD5)
    ok &= check(not any(e[0] == "timeout" for e in tt.log),
                "a discard from the awaited seat is NOT swept as a timeout, "
                "however long the human took")
    ok &= check(len(tt.kyoku.hands[0].pond) == pond0 + 1,
                "and the tile is discarded ONCE, not auto-played then discarded "
                "again (pond %d -> %d)" % (pond0, len(tt.kyoku.hands[0].pond)))


    # =========================================================================
    # THE TABLE STATE MACHINE (2026-09-04 audit: findings 6, 7, 8, 9, 10, 11,
    # 20, 21, 29, 30, 31, 32, 34). The audit's own probes are the first
    # blocks, verbatim in spirit: each one REPRODUCED before the fix.
    # =========================================================================
    _rdy0 = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                         length=0x18)[:0x18]

    def _stamped(op, seat, seq=0, f16=1):
        return janwire.pack(opcode=op, f13=seq, src=seat, dst=4, f16=f16,
                            sub=3, length=0x18)[:0x18]

    def _ops(recs):
        return [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in recs]

    def _two_humans(mgr, lobby, seed):
        tt = mgr.table_for_lobby(lobby, [(15, 0, "A", 0xAAAA), (6, 1, "B", 0xBBBB)])
        tt.rng = random.Random(seed)
        return tt

    def _force_tsumo(tt, seat):
        """Give `seat` 123m 456m 789m 123p 55s (complete, closed) on turn."""
        kk = tt.kyoku
        hh_ = kk.hands[seat]
        hh_.tiles = mj._hand_from("123m456m789m123p55s")[:14]
        hh_.melds, hh_.menzen, hh_.riichi = [], True, False
        hh_.drawn = hh_.tiles[-1]
        kk.turn = seat
        return mj.can_tsumo(hh_, kk.context_for(seat, hh_.drawn, True))

    # --- finding 6 (probe P1): a quit on the win screen must not wedge -------
    mp1 = manager.Manager()
    tp1 = _two_humans(mp1, 101, 5)
    tp1.start_game()
    scp = _force_tsumo(tp1, 0)
    ok &= check(scp is not None, "P1 rig: seat 0 holds a tsumo")
    tp1.kyoku.win_tsumo(0)
    tp1.begin_routing()
    tp1.on_win(0, scp)
    tp1.take_routes()
    ok &= check(tp1._awaiting == (M.MjYAKUDISP, None), "P1: the win screen is awaited")
    outb = mp1.handle(_stamped(M.MjBYE, 1, f16=0), member_id=6)
    ok &= check(tp1._awaiting == (M.MjYAKUDISP, None) and tp1.live == {0},
                "P1: seat 1's MjBYE on the win screen leaves the YAKUDISP wait "
                "ALONE (it used to become (MEMBERLEAVE, None), which nothing "
                "acks): %r" % (tp1._awaiting,))
    ok &= check("MjMEMBERLEAVE" in _ops(mp1.pending_for_member(15)),
                "P1: the survivor is told (MjMEMBERLEAVE in its outbox)")
    outy = mp1.handle(_client_ack(tp1._awaited_recs()[0][0], 0), member_id=15)
    ok &= check(_ops(outy) == ["MjSEISAN"],
                "P1: the survivor's YAKUDISPACK still progresses to MjSEISAN: %r"
                % _ops(outy))
    outs = mp1.handle(_client_ack(outy[0], 0), member_id=15) if outy else []
    ok &= check("MjHAIPAI" in _ops(outs),
                "P1: ...and its SEISANACK to the next deal: %r" % _ops(outs))

    # --- finding 7 (probe P2): a replayed MjSUTE is a retransmission ---------
    tp2 = table.Table(102, seed=7)
    tp2.seat_player(0x1111, "H", seat=0, member=15)
    tp2.fill_with_bots()
    o = tp2.handle(_rdy0)
    o = tp2.handle(_client_ack(o[0], 0))
    ok &= check(janwire.unpack(o[-1])["opcode"] == M.MjTSUMO, "P2 rig: seat 0 to move")
    sute2 = _client_sute(o[-1], 0, 3)
    o1 = tp2.handle(sute2)
    pond1 = len(tp2.kyoku.hands[0].pond)
    turn1 = tp2.kyoku.turn
    o2 = tp2.handle(sute2)                      # the client's ~1 min retry
    ok &= check(len(tp2.kyoku.hands[0].pond) == pond1 and tp2.kyoku.turn == turn1,
                "P2: the SAME MjSUTE again discards NOTHING (pond %d -> %d)"
                % (pond1, len(tp2.kyoku.hands[0].pond)))
    ok &= check(any(e[0] == "replay" for e in tp2.log), "P2: ...and is logged as a replay")
    ok &= check(o2 and janwire.unpack(o2[-1])["f13"] == janwire.unpack(o1[-1])["f13"],
                "P2: the reply is the record the seat is waiting on, SAME sequence "
                "(the client dedups it): %r" % _ops(o2))
    # A replayed ladder ack is silent (the deal-storm contract), a replayed
    # call-ack re-nudges, and neither mutates.
    tp2b = table.Table(103, seed=8)
    tp2b.seat_player(0x1112, "H", seat=0, member=15)
    tp2b.fill_with_bots()
    d = tp2b.handle(_rdy0)
    a1 = tp2b.handle(_client_ack(d[0], 0))
    a2 = tp2b.handle(_client_ack(d[0], 0))
    ok &= check(bool(a1) and a2 == [] and any(e[0] == "replay" for e in tp2b.log),
                "P2: a replayed HAIPAIACK is silent, not a second draw: %r" % _ops(a2))

    # --- finding 10 (probes P3/P5): the client-stamped seat is not trusted ---
    tp3 = table.Table(104, seed=8)
    tp3.seat_player(0x1111, "H", seat=0, member=15)
    tp3.fill_with_bots()
    o = tp3.handle(_rdy0)
    o = tp3.handle(_client_ack(o[0], 0))
    k3_ = tp3.kyoku
    h2len = len(k3_.hands[2].tiles)
    forged = _client_sute(o[-1], 2, 0)          # stamped with a bot's seat
    of = tp3.handle(forged, member=15)          # ...by the member holding seat 0
    ok &= check(len(k3_.hands[2].tiles) == h2len and not k3_.hands[2].pond
                and k3_.turn == 0,
                "P3: a MjSUTE stamped seat 2 from the member at seat 0 mutates "
                "NOTHING (seat 2: %d tiles, pond %d)"
                % (len(k3_.hands[2].tiles), len(k3_.hands[2].pond)))
    ok &= check(any(e[0] == "seat-mismatch" for e in tp3.log),
                "P3: ...and is logged as a seat mismatch")
    ok &= check(of and janwire.unpack(of[-1])["opcode"] == M.MjTSUMO,
                "P3: ...and the member is re-nudged with its own turn: %r" % _ops(of))
    # No member known (the stamp is all we have): the engine's turn check.
    of2 = tp3.handle(forged)
    ok &= check(len(k3_.hands[2].tiles) == h2len and k3_.turn == 0
                and any(e[0] == "refused-sute" for e in tp3.log),
                "P3: with no member to check against, a bot-seat MjSUTE is "
                "refused by the turn/live check, never applied")
    # SUTE_TSUMO_AGARI with no draw (probe P4): refused, not raised.
    k3_.hands[0].drawn = None
    try:
        o4 = tp3.handle(_client_sute(o[-1], 0, 0, action=M.SUTE_TSUMO_AGARI))
        ok &= check(any(e[0] == "refused-tsumo" for e in tp3.log)
                    and k3_.result is None,
                    "P4: a tsumo claim with no draw is refused: %r" % _ops(o4))
    except Exception as e:
        ok &= check(False, "P4: raised %s: %s" % (type(e).__name__, e))
    tp5 = table.Table(105, seed=10)
    tp5.seat_player(0x1111, "H", seat=0, member=15)
    tp5.fill_with_bots()
    o5 = tp5.handle(_stamped(M.MjREADY, 3, f16=0))
    ok &= check(tp5.live == set() or tp5.live == {0},
                "P5: a MjREADY stamped with a bot's seat does not make it live: %r"
                % (tp5.live,))
    ok &= check(3 in tp5.bots and any(e[0] == "ready-from-bot-seat" for e in tp5.log),
                "P5: seat 3 stays a bot, and it is logged")

    # --- finding 11 (probe P8): SUTE_KAN on a non-kan slot ------------------
    tp8 = table.Table(106, seed=3)
    tp8.seat_player(0x1111, "H", seat=0, member=15)
    tp8.fill_with_bots()
    o = tp8.handle(_rdy0)
    o = tp8.handle(_client_ack(o[0], 0))
    k8 = tp8.kyoku
    h8 = k8.hands[0]
    h8.tiles = mj._hand_from("1111m234s567s789p9p")[:14]
    h8.sort()
    h8.drawn = h8.tiles[-1]
    k8.turn = 0
    ok &= check(k8.ankan_options(0) == [mj.kind(h8.tiles[0])] and not k8.kakan_options(0),
                "P8 rig: one ankan (1m) available")
    ok &= check(tp8._mymove_menu(0)[tp8.MENU_KAN] == 1,
                "the Kan menu bit follows the ENGINE's kan options")
    bad = next(i for i, tt_ in enumerate(h8.tiles) if mj.kind(tt_) != mj.kind(h8.tiles[0]))
    o8 = tp8.handle(_client_sute(o[-1], 0, bad, action=M.SUTE_KAN))
    ok &= check(len(h8.tiles) == 14 and not h8.melds
                and any(e[0] == "refused-kan" for e in tp8.log),
                "P8: a SUTE_KAN on a non-kan slot is refused, no tile deleted "
                "(%d tiles, %d melds)" % (len(h8.tiles), len(h8.melds)))
    ok &= check(o8 and janwire.unpack(o8[-1])["opcode"] == M.MjTSUMO,
                "P8: ...and the turn is re-offered: %r" % _ops(o8))
    # ...and the REAL ankan on the right slot: resync path = ALLDATA + TSUMO,
    # the meld formed, the rinshan drawn, the indicator revealed at once.
    dora8 = k8.wall.dora_shown
    o8b = tp8.handle(_client_sute(o8[-1], 0, 0, action=M.SUTE_KAN))
    ok &= check(_ops(o8b) == ["MjALLDATA", "MjTSUMO"],
                "ankan (resync path): MjALLDATA then the rinshan MjTSUMO: %r" % _ops(o8b))
    ok &= check(h8.melds and h8.melds[0].kind == mj.ANKAN and len(h8.tiles) == 11
                and h8.drawn is not None and k8.wall.dora_shown == dora8 + 1,
                "ankan: meld ANKAN, 11 tiles + rinshan draw, kan-dora revealed NOW "
                "(%d -> %d)" % (dora8, k8.wall.dora_shown))
    ok &= check(o8b and o8b[-1][0x18] == 2, "the rinshan MjTSUMO is motion 2 on the resync path")

    # --- finding 21: the kan family, both record paths ----------------------
    # A DAIMINKAN by the human through NAKIACK kan.
    tk = table.Table(107, seed=42)
    tk.seat_player(0x2347, b"kanner", seat=0, member=15)
    tk.fill_with_bots()
    tk.handle(_rdy0)
    kk_ = tk.kyoku
    hk = kk_.hands[0]
    hk.tiles[:] = mj._hand_from("777p234s567s123m99m")[:13]
    kk_.last_discard_seat, kk_.last_discard = 3, mj._hand_from("7p")[0]
    kk_.hands[3].pond.append(kk_.last_discard)
    optk = tk.legal_calls(0, kk_.last_discard, 3)
    ok &= check("kan" in optk and "pon" in optk, "daiminkan rig: kan+pon offered: %r" % optk)
    nk = tk.msg_naki(kk_.last_discard, {0: optk})
    ok &= check(nk[0x4C + tk.NAKI_MENU_BYTE["kan"]] == 1, "the Kan call button is lit")
    ok_ = tk.handle(_client_naki(nk, 0, M.NAKI_KAN))
    ok &= check(_ops(ok_) == ["MjALLDATA", "MjTSUMO"] and hk.melds
                and hk.melds[0].kind == mj.MINKAN and hk.drawn is not None
                and kk_.pending_dora == 1,
                "daiminkan: MINKAN meld, rinshan drawn, kan-dora OWED until the "
                "discard (pending %d): %r" % (kk_.pending_dora, _ops(ok_)))
    # ...and the discard flushes the owed indicator.
    dk = kk_.wall.dora_shown
    idxk = hk.tiles.index(hk.drawn)
    tk.handle(_client_sute(ok_[-1], 0, idxk))
    ok &= check(kk_.wall.dora_shown == dk + 1 and kk_.pending_dora == 0,
                "the discard after a daiminkan reveals the kan-dora (%d -> %d)"
                % (dk, kk_.wall.dora_shown))

    # A KAKAN with a CHANKAN window: a bot that waits on the added tile robs it.
    tc = table.Table(108, seed=43)
    tc.seat_player(0x2348, b"kakan", seat=0, member=15)
    tc.fill_with_bots()
    tc.handle(_rdy0)
    kc = tc.kyoku
    hc = kc.hands[0]
    five_p = mj._hand_from("5p")[0]
    hc.melds = [mj.Meld(mj.PON, mj._hand_from("555p")[:3], 3, five_p)]
    hc.menzen = False
    hc.tiles = mj._hand_from("123m456m789m9s")[:10] + [five_p]
    hc.drawn = five_p
    kc.turn = 0
    kc.hands[1].tiles[:] = mj._hand_from("123m456m789m123s5p")[:13]   # tanki on 5p
    kc.hands[1].menzen = True
    for s_ in (2, 3):
        kc.hands[s_].tiles[:] = mj._hand_from("19m19p19s1234567z")[:13]  # nothing
    ok &= check(kc.kakan_options(0) == [mj.kind(five_p)], "kakan rig: 5p may be added")
    ok &= check(tc._mymove_menu(0)[tc.MENU_KAN] == 1, "...and the Kan button is lit for it")
    tsc = tc.msg_tsumo(0)
    oc = tc.handle(_client_sute(tsc, 0, hc.tiles.index(five_p), action=M.SUTE_KAN))
    ok &= check(kc.result is not None and kc.result[0] == mj.RON
                and kc.result[1][0][0] == 1,
                "kakan: the bot waiting on 5p ROBS the kan (chankan): %r"
                % (kc.result[0] if kc.result else None,))
    ok &= check(hc.melds[0].kind == mj.PON and len(hc.melds[0].tiles) == 3,
                "...the kan is cancelled back to a pon")
    ok &= check("MjYAKUDISP" in _ops(oc) and kc.pending_kan is None,
                "...and the win screen goes out: %r" % _ops(oc))
    ok &= check(any(n == "chankan" for n, _h in kc.result[1][0][1].yaku),
                "...scored WITH chankan: %r" % (kc.result[1][0][1],))

    # The same window offered to a LIVE seat: MjNAKI with only Ron, +0x28 the
    # added tile, the kan record held ("defer"), then pass -> the kan
    # completes, rinshan flies as motion 2 (resync path).
    mc2 = manager.Manager()
    tc2 = _two_humans(mc2, 109, 44)
    tc2.start_game()
    kc2 = tc2.kyoku
    hc2 = kc2.hands[0]
    hc2.melds = [mj.Meld(mj.PON, mj._hand_from("555p")[:3], 3, five_p)]
    hc2.menzen = False
    hc2.tiles = mj._hand_from("123m456m789m9s")[:10] + [five_p]
    hc2.drawn = five_p
    kc2.turn = 0
    kc2.hands[1].tiles[:] = mj._hand_from("123m456m789m123s5p")[:13]
    kc2.hands[1].menzen = True
    for s_ in (2, 3):
        kc2.hands[s_].tiles[:] = mj._hand_from("19m19p19s1234567z")[:13]
    tc2.begin_routing()
    tsc2 = tc2.msg_tsumo(0)
    tc2.take_routes()
    oc2 = mc2.handle(_client_sute(tsc2, 0, hc2.tiles.index(five_p), action=M.SUTE_KAN),
                     member_id=15)
    ok &= check(_ops(oc2) == ["MjALLDATA", "MjNAKI"] and tc2.pending_chankan is not None
                and kc2.pending_kan is not None,
                "kakan with a live robber: the held kan resync + an MjNAKI, the "
                "engine's kan PROVISIONAL: %r" % _ops(oc2))
    nk2 = oc2[-1]
    ok &= check(nk2[0x3C + 1] != 0 and nk2[0x40 + 1] == 0 and nk2[0x44 + 1] == 0
                and nk2[0x48 + 1] == 0
                and struct.unpack_from("<H", nk2, 0x28)[0] == M.tile_u16(five_p)
                and nk2[0x2A] == 0,
                "the chankan MjNAKI lights ONLY Ron for seat 1, +0x28 the added "
                "tile, +0x2a the kan seat")
    ok &= check(nk2 in mc2.pending_for_member(6), "...and seat 1 receives it")
    oc3 = mc2.handle(_client_naki(nk2, 1, M.NAKI_PASS), member_id=6)
    ok &= check(kc2.pending_kan is None and hc2.melds[0].kind == mj.KAKAN
                and hc2.drawn is not None and kc2.pending_dora == 1,
                "seat 1 passes: the kan COMPLETES (KAKAN, rinshan drawn, dora owed)")
    ok &= check(kc2.hands[1].temp_furiten, "...and the passer is furiten for the turn")
    o15 = mc2.pending_for_member(15)
    ok &= check(_ops(o15) == ["MjTSUMO"] and o15[0][0x18] == 2,
                "...the kan seat gets its rinshan MjTSUMO, motion 2 (resync path): %r"
                % _ops(o15))
    ok &= check(oc3 == [] or all(janwire.unpack(r)["opcode"] != M.MjTSUMO for r in oc3),
                "...and seat 1 is NOT handed seat 0's private turn record: %r" % _ops(oc3))
    # The ron instead: the kan is robbed.
    mc3 = manager.Manager()
    tc3 = _two_humans(mc3, 110, 45)
    tc3.start_game()
    kc3 = tc3.kyoku
    hc3 = kc3.hands[0]
    hc3.melds = [mj.Meld(mj.PON, mj._hand_from("555p")[:3], 3, five_p)]
    hc3.menzen = False
    hc3.tiles = mj._hand_from("123m456m789m9s")[:10] + [five_p]
    hc3.drawn = five_p
    kc3.turn = 0
    kc3.hands[1].tiles[:] = mj._hand_from("123m456m789m123s5p")[:13]
    kc3.hands[1].menzen = True
    for s_ in (2, 3):
        kc3.hands[s_].tiles[:] = mj._hand_from("19m19p19s1234567z")[:13]
    tc3.begin_routing()
    tsc3 = tc3.msg_tsumo(0)
    tc3.take_routes()
    oc4 = mc3.handle(_client_sute(tsc3, 0, hc3.tiles.index(five_p), action=M.SUTE_KAN),
                     member_id=15)
    oc5 = mc3.handle(_client_naki(oc4[-1], 1, M.NAKI_RON), member_id=6)
    ok &= check(kc3.result is not None and kc3.result[0] == mj.RON
                and kc3.result[1][0][0] == 1 and hc3.melds[0].kind == mj.PON,
                "seat 1 rons: the human robs the kan, the meld reverts to a pon")
    ok &= check("MjYAKUDISP" in _ops(oc5), "...and the win screen follows: %r" % _ops(oc5))

    # KAN_MOTION on: the motion-7 record for an ankan, then MjTSUMO motion 0.
    _km = knobs.KAN_MOTION
    knobs.KAN_MOTION = True
    try:
        tkm = table.Table(111, seed=3)
        tkm.seat_player(0x1111, "H", seat=0, member=15)
        tkm.fill_with_bots()
        o = tkm.handle(_rdy0)
        o = tkm.handle(_client_ack(o[0], 0))
        kkm = tkm.kyoku
        hkm = kkm.hands[0]
        hkm.tiles = mj._hand_from("1111m234s567s789p9p")[:14]
        hkm.sort()
        hkm.drawn = hkm.tiles[-1]
        kkm.turn = 0
        okm = tkm.handle(_client_sute(o[-1], 0, 0, action=M.SUTE_KAN))
        ok &= check(_ops(okm) == ["MjALLDATA", "MjTSUMO"] and okm[0][0x18] == 7
                    and okm[1][0x18] == 0,
                    "KAN_MOTION: an ankan is ONE motion-7 MjALLDATA then MjTSUMO "
                    "motion 0: %r / %d,%d" % (_ops(okm), okm[0][0x18], okm[1][0x18]))
    finally:
        knobs.KAN_MOTION = _km

    # A BOT daiminkan: an open bot holding the triplet takes the kan, the
    # records animate it (resync path) and the bot plays on.
    tb = table.Table(112, seed=46)
    tb.seat_player(0x2349, b"h", seat=0, member=15)
    tb.fill_with_bots()
    tb.handle(_rdy0)
    kb = tb.kyoku
    seven_p = mj._hand_from("7p")[0]
    hb1 = kb.hands[1]
    hb1.melds = [mj.Meld(mj.CHI, mj._hand_from("123m")[:3], 0, mj._hand_from("1m")[0])]
    hb1.menzen = False
    hb1.tiles[:] = mj._hand_from("777p456s234m9s")[:10]
    for s_ in (2, 3):
        kb.hands[s_].tiles[:] = mj._hand_from("19m19p19s1234567z")[:13]
    kb.turn = 0
    kb.last_discard_seat, kb.last_discard = 0, seven_p
    kb.hands[0].pond.append(seven_p)
    ok &= check(tb.bots[1].calls(kb, seven_p, tb.legal_calls(1, seven_p, 0)) == "kan",
                "Bot.calls takes a daiminkan that keeps its shape")
    ob = tb.resolve_bot_calls(seven_p, 0)
    ok &= check(ob is not None and hb1.melds[-1].kind == mj.MINKAN
                and len(hb1.pond) == 1 and kb.pending_dora in (0, 1),
                "the bot's daiminkan forms, and it discards after the rinshan: %r"
                % _ops(ob or []))
    ok &= check(ob and _ops(ob)[:2] == ["MjALLDATA", "MjTSUMO"],
                "...animated by the kan records first: %r" % _ops(ob or [])[:4])
    # Suukaikan: after a shared fourth kan, only ron is legal on the discard.
    kb._fourth_kan_discard = True
    ok &= check("pon" not in tb.legal_calls(2, seven_p, 0) and
                "chi" not in tb.legal_calls(1, seven_p, 0),
                "after a shared 4th kan the next discard may only be ronned")
    kb._fourth_kan_discard = False

    # --- finding 20: the chi pair the client's cursor MEANT ------------------
    tchi = table.Table(113, seed=47)
    tchi.seat_player(0x234A, b"chi", seat=0, member=15)
    tchi.fill_with_bots()
    tchi.handle(_rdy0)
    kch = tchi.kyoku
    hch = kch.hands[0]
    # 4p 5p 6p 7p in hand, 6p claimed: cursor on 7p means 7p+5p?? no --
    # cursor d+1 (7p) = (7p, 8p)... the table: acked 5p (d-1) = (5p, 7p);
    # acked 4p (d-2) = (4p, 5p); acked 7p (d+1) = (7p, 8p).
    hch.tiles[:] = mj._hand_from("4578p123m456s99s")[:13]
    six_p = mj._hand_from("6p")[0]
    kch.last_discard_seat, kch.last_discard = 3, six_p
    kch.hands[3].pond.append(six_p)
    pairs = mj.chi_options(hch, six_p)
    ok &= check(len(pairs) >= 2, "chi rig: several shapes hold (4-5, 5-7, 7-8): %r"
                % [mj.hand_str(p) for p in pairs])
    nch = tchi.msg_naki(six_p, {0: {"chi": True}})
    four_p = mj._hand_from("4p")[0]
    och = tchi.handle(_client_naki(nch, 0, M.NAKI_CHI | M.tile_u16(four_p)))
    ok &= check(hch.melds and sorted(mj.kind(t_) for t_ in hch.melds[0].tiles)
                == sorted(mj.kind(t_) for t_ in mj._hand_from("456p")[:3]),
                "cursor on 4p (d-2) takes the 4p-5p pair -- the client's own "
                "table, not first-match: %r" % (hch.melds[0] if hch.melds else None,))

    # --- finding 31: an exception mid-handle answers with a resync -----------
    tx = table.Table(114, seed=48)
    tx.seat_player(0x234B, b"x", seat=0, member=15)
    tx.fill_with_bots()
    o = tx.handle(_rdy0)
    o = tx.handle(_client_ack(o[0], 0))
    kx = tx.kyoku
    census_before = kx.tile_census()["total"]
    _real = tx.on_sute
    tx.on_sute = lambda rec, seat=None: (_ for _ in ()).throw(RuntimeError("boom"))
    ox = tx.handle(_client_sute(o[-1], 0, 0))
    tx.on_sute = _real
    ok &= check(_ops(ox)[:1] == ["MjALLDATA"] and any(e[0] == "error" for e in tx.log),
                "an exception in the handler is logged and answered with a resync, "
                "not silence: %r" % _ops(ox))
    ok &= check(kx.tile_census()["total"] == census_before == 136,
                "...and the engine is untouched (136 tiles)")
    ok &= check(_ops(ox) == ["MjALLDATA", "MjTSUMO"],
                "...with the turn re-offered, since it was this seat's: %r" % _ops(ox))
    # An engine refusal (ValueError) is a refused move with the turn re-offered.
    ovx = tx.handle(_client_sute(ox[-1], 0, 0, action=M.SUTE_RIICHI))
    ok &= check(any(e[0] in ("refused-riichi", "refused-sute") for e in tx.log)
                and _ops(ovx) == ["MjTSUMO"] and kx.hands[0].riichi is False,
                "a riichi the engine refuses is logged and the turn re-offered: %r"
                % _ops(ovx))

    # --- findings 8/31/33: the sweeper, escalation, broadcast re-send --------
    clk3 = {"t": 20000.0}
    m3 = manager.Manager()
    m3.clock = lambda: clk3["t"]
    t3_ = _two_humans(m3, 115, 49)
    m3.handle(_rdy0, member_id=15)
    m3.handle(_client_ack(t3_._last_for[0], 0), member_id=15)
    aw3 = t3_._awaiting
    ok &= check(aw3 and aw3[0] == M.MjTSUMO and aw3[1] in (0, 1), "sweeper rig: a human on the clock")
    silent = aw3[1]
    other = 1 - silent
    strikes = 0
    for _i in range(knobs.TIMEOUTS_TO_DROP + 2):
        if silent in t3_.dropped or t3_.kyoku is None or t3_.kyoku.result is not None:
            break
        # play the OTHER human's turns so the silent seat keeps coming round
        guard_ = 0
        while (t3_._awaiting and t3_._awaiting[1] == other and guard_ < 50
               and t3_.kyoku is not None and t3_.kyoku.result is None):
            guard_ += 1
            m3.pending_for_member(15 if other == 0 else 6)
            last = t3_._last_for.get(other)
            hh3 = janwire.unpack(last)
            if hh3["opcode"] == M.MjTSUMO:
                hand3 = [v for v in struct.unpack_from("<14H", last, M.TSUMO_HAND) if v]
                m3.handle(_client_sute(last, other, len(hand3) - 1),
                          member_id=15 if other == 0 else 6)
            elif hh3["opcode"] == M.MjNAKI:
                m3.handle(_client_naki(last, other, M.NAKI_PASS),
                          member_id=15 if other == 0 else 6)
            else:
                break
        if not (t3_._awaiting and t3_._awaiting[1] == silent):
            continue
        clk3["t"] += t3_.deadline() + 1
        m3.tick()
        strikes += 1
    ok &= check(silent in t3_.dropped and silent not in t3_.live,
                "a seat that strikes the deadline %d times in a row is DROPPED "
                "(strikes %d): dropped=%r" % (knobs.TIMEOUTS_TO_DROP, strikes, t3_.dropped))
    ok &= check(t3_.timeouts.get(silent) is None or t3_.timeouts.get(silent) >= 1,
                "...the strike counter drove it")
    ok &= check("MjMEMBERLEAVE" in _ops(m3.pending_for_member(15 if other == 0 else 6)),
                "...and MjMEMBERLEAVE reaches the survivor's outbox")
    # A line from a seat resets its strike count.
    t3b = table.Table(116, seed=50)
    t3b.seat_player(0xA1, seat=0, member=15)
    t3b.timeouts[0] = 2
    t3b.handle(_stamped(M.MjALLDATAACK, 0), member=15)
    ok &= check(t3b.timeouts.get(0) is None, "any line from the seat clears its strikes")
    # A BROADCAST ack nobody answers: re-sent ONCE, then the ladder proceeds.
    clk4 = {"t": 30000.0}
    m4 = manager.Manager()
    m4.clock = lambda: clk4["t"]
    t4_ = _two_humans(m4, 117, 51)
    t4_.start_game()
    t4_.game.scores = [30000, 25000, 25000, 20000]
    t4_.game.hands_played = 8
    t4_.begin_routing()
    t4_.msg_results()
    t4_.take_routes()
    ok &= check(t4_._awaiting == (M.MjGAMERESULTHALF1, None), "broadcast rig: HALF1 awaited")
    seq_h1 = t4_._seq
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    ok &= check(any(e[0] == "resend" for e in t4_.log)
                and t4_._awaiting == (M.MjGAMERESULTHALF1, None)
                and _ops(t4_.outbox.get(0, [])) == ["MjGAMERESULTHALF1"]
                and janwire.unpack(t4_.outbox[0][0])["f13"] == seq_h1,
                "an unacked HALF1 is RE-SENT once past the deadline, same "
                "sequence, to every live seat: %r" % _ops(t4_.outbox.get(0, [])))
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    ok &= check(t4_._awaiting == (M.MjGAMERESULTHALF2, None)
                and any(e[0] == "timeout-broadcast" for e in t4_.log),
                "...and the second deadline PROCEEDS as if acked: %r" % (t4_._awaiting,))
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    ok &= check(t4_._awaiting == (M.MjGAMEEND, None) and t4_.state == "over",
                "...through HALF2 to MjGAMEEND: %r" % (t4_._awaiting,))
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    clk4["t"] += t4_.deadline() + 1
    m4.tick()
    ok &= check(t4_.state == "finished",
                "...and an unacked MjGAMEEND finishes the table: state %s"
                % (t4_.state,))
    # WARNING: FORGETTING IT IS NOW THE SECOND STEP, NOT THE SAME ONE.
    # Nobody acked anything here, so both seats still have the whole end-of-game
    # ladder queued -- and dropping the table drops those records, which is the
    # "stuck on the results screen" hang. It is HELD for POL_JAN_FINISHED_GRACE
    # and then forgotten, so finding 32's no-growth guarantee still holds.
    ok &= check(t4_.id in m4.tables and manager.Manager._owed_seats(t4_),
                "...HELD first, because both seats are still owed records: %r"
                % (manager.Manager._owed_seats(t4_),))
    clk4["t"] += knobs.FINISHED_GRACE + 1
    m4.tick()
    ok &= check(t4_.id not in m4.tables,
                "...and the Manager FORGETS it past the grace (finding 32): "
                "tables %r" % (sorted(m4.tables),))

    # The sweeper THREAD itself: real clock, tiny deadline, it fires and the
    # hook is told which members have records waiting.
    _ss = knobs.SWEEP_S
    knobs.SWEEP_S = 0.05
    try:
        m5 = manager.Manager(sweeper=True)
        hooked = []
        m5.on_swept = lambda members: hooked.append(list(members))
        t5_ = _two_humans(m5, 118, 52)
        t5_.rules.wait_time = 0
        t5_.TURN_GRACE = 0.05
        m5.handle(_rdy0, member_id=15)
        m5.handle(_client_ack(t5_._last_for[0], 0), member_id=15)
        ok &= check(m5._sweeper is not None and m5._sweeper.is_alive(),
                    "Manager.handle starts the sweeper thread once")
        deadline_ = time.time() + 5
        while time.time() < deadline_ and not any(e[0] in ("timeout", "drop")
                                                   for e in t5_.log):
            time.sleep(0.02)
        ok &= check(any(e[0] in ("timeout", "drop") for e in t5_.log),
                    "the THREAD sweeps a silent seat with nobody talking")
        deadline_ = time.time() + 2
        while time.time() < deadline_ and not hooked:
            time.sleep(0.02)
        ok &= check(bool(hooked) and all(m in (6, 15) for ms_ in hooked for m in ms_),
                    "...and the push hook names the members with records queued: %r"
                    % hooked[:3])
    finally:
        knobs.SWEEP_S = _ss

    # --- finding 9: two threads on one table, under the lock -----------------
    m6 = manager.Manager()
    t6_ = _two_humans(m6, 119, 53)
    m6.handle(_rdy0, member_id=15)
    m6.handle(_client_ack(t6_._last_for[0], 0), member_id=15)
    m6.handle(_client_ack(t6_._last_for[1], 1), member_id=6)
    errs = []

    def _hammer(member, seat, n=150):
        try:
            for i in range(n):
                m6.pending_for_member(member)
                last = t6_._last_for.get(seat)
                if last is None:
                    continue
                hh6 = janwire.unpack(last)
                if hh6["opcode"] == M.MjTSUMO and t6_._awaiting == (M.MjTSUMO, seat):
                    hand6 = [v for v in struct.unpack_from("<14H", last, M.TSUMO_HAND) if v]
                    m6.handle(_client_sute(last, seat, i % max(1, len(hand6))),
                              member_id=member)
                elif hh6["opcode"] == M.MjNAKI:
                    m6.handle(_client_naki(last, seat, M.NAKI_PASS), member_id=member)
                else:
                    a6 = _client_ack(last, seat)
                    if a6 is not None:
                        m6.handle(a6, member_id=member)
                    else:
                        m6.handle(_stamped(M.MjALLDATA, seat), member_id=member)
        except Exception as e:
            errs.append("%s: %s" % (type(e).__name__, e))

    ths = [threading.Thread(target=_hammer, args=(15, 0)),
           threading.Thread(target=_hammer, args=(6, 1))]
    for th_ in ths:
        th_.start()
    for th_ in ths:
        th_.join(30)
    ok &= check(not errs, "two threads hammering one table raise nothing: %r" % errs[:2])
    ok &= check(not any(e[0] in ("route-bypass", "route-no-caller", "tick-route-bypass")
                        for e in t6_.log),
                "...and routing never had to bypass (no leak of a private record): %r"
                % [e for e in t6_.log
                   if isinstance(e[0], str) and e[0].startswith("route")][:3])
    ok &= check(t6_.kyoku is None or t6_.kyoku.tile_census()["total"] == 136,
                "...and the engine kept all 136 tiles")

    # --- finding 32: growth ----------------------------------------------------
    m7 = manager.Manager()
    ok &= check(m7.handle(_stamped(M.MjBYE, 0, f16=0), member_id=77) == [] and not m7.tables,
                "a stray member's MjBYE mints NO table")
    g7 = m7.handle(_stamped(M.MjSUTE, 0), member_id=77)
    ok &= check(_ops(g7) == ["MjGAMEEND"] and not m7.tables and 77 not in m7.by_member,
                "a stray member's in-game line gets the ghost MjGAMEEND with no state: %r"
                % _ops(g7))
    t7_ = m7.table_for(78)
    ok &= check(78 in m7.by_member and t7_.seat_of_member(78) == 0,
                "the solo path maps the member to its seat (finding 10 needs it)")
    ok &= check(isinstance(t7_.log, table._Log) and t7_.log.maxlen == knobs.LOG_MAX,
                "Table.log is a ring of %d" % knobs.LOG_MAX)
    for _i in range(knobs.LOG_MAX + 50):
        t7_.log.append(("x", _i))
    ok &= check(len(t7_.log) == knobs.LOG_MAX and t7_.log[-1] == ("x", knobs.LOG_MAX + 49)
                and t7_.log[-2:][0] == ("x", knobs.LOG_MAX + 48),
                "...bounded, newest kept, and it still slices")
    t7_.timeouts[0] = 2
    t7_.dropped.add(2)
    t7_.outbox[1] = [b"x"]
    t7_._last_in[0] = (1, 2, 3)
    t7_.naki_answers[0] = ("pass", None)
    t7_.reset_for_new_game()
    ok &= check(not t7_.timeouts and not t7_.dropped and not t7_.outbox
                and not t7_._last_in and not t7_.naki_answers and not t7_._byes,
                "reset_for_new_game clears timeouts/dropped/outbox/replay/answers")
    # Idle TTL: an idle table nobody speaks to is forgotten.
    clk7 = {"t": 40000.0}
    m7b = manager.Manager()
    m7b.clock = lambda: clk7["t"]
    t7b = m7b.table_for(79)
    clk7["t"] += knobs.TABLE_TTL + 1
    m7b.tick()
    ok &= check(t7b.id not in m7b.tables and 79 not in m7b.by_member,
                "an idle table past TABLE_TTL is forgotten")
    # And a finished one: every live seat's MjBYE after MjGAMEEND.
    m7c = manager.Manager()
    t7c = _two_humans(m7c, 120, 54)
    t7c.start_game()
    t7c.begin_routing()
    t7c.msg_gameend()
    t7c.take_routes()
    m7c.handle(_stamped(M.MjBYE, 0, f16=0), member_id=15)
    ok &= check(t7c.id in m7c.tables and t7c.state == "over",
                "after one of two MjBYEs the table is still held")
    m7c.handle(_stamped(M.MjBYE, 1, f16=0), member_id=6)
    ok &= check(t7c.state == "finished" and t7c.id not in m7c.tables
                and 15 not in m7c.by_member and 6 not in m7c.by_member
                and not m7c.by_lobby,
                "...after the second it is finished and forgotten everywhere")

    # WARNING: A FINISHED TABLE MUST NOT TAKE THE OTHER PLAYER'S RECORDS WITH IT.
    # `pending_for_member` resolves through `by_member`, so reaping the table
    # is the same thing as deleting whatever is still queued for the seat that
    # has not caught up -- and the ladder advances on the FIRST ack, so being
    # behind is ordinary, not a fault. The symptom is a client stuck on the
    # results screen after the game is done.
    clk7d = {"t": 50000.0}
    m7d = manager.Manager()
    m7d.clock = lambda: clk7d["t"]
    t7d = _two_humans(m7d, 121, 55)
    t7d.clock = lambda: clk7d["t"]
    t7d.start_game()
    t7d.begin_routing()
    t7d.msg_gameend()
    t7d.take_routes()
    t7d.outbox.pop(0, None)                 # seat 0 is up to date
    t7d.queue_for(1, _stamped(M.MjGAMEEND, 1))   # seat 1's copy, undelivered
    _dl = t7d.deadline()
    for _ in range(2):                      # resend once, then proceed-as-acked
        clk7d["t"] += _dl + 1
        m7d.tick()
    ok &= check(t7d.state == "finished",
                "a game end nobody acks finishes the table (the loss window)")
    ok &= check(t7d.id in m7d.tables and 6 in m7d.by_member,
                "...but it is HELD while seat 1 still has records queued")
    ok &= check(len(m7d.pending_for_member(6)) >= 1,
                "...and seat 1 can still drain them (this is the hang, fixed)")
    t7d.queue_for(1, _stamped(M.MjGAMEEND, 1))
    clk7d["t"] += knobs.FINISHED_GRACE + 1
    m7d.tick()
    ok &= check(t7d.id not in m7d.tables and 6 not in m7d.by_member,
                "...and past POL_JAN_FINISHED_GRACE it is forgotten anyway "
                "(a player who walked away cannot hold a table for ever)")
    # The hold is for a seat that is WAITING. A bot, a seat that said MjBYE and
    # a spectator key are not waiting for anything.
    m7e = manager.Manager()
    t7e = _two_humans(m7e, 122, 56)
    t7e.start_game()
    t7e.queue_for(1, _stamped(M.MjGAMEEND, 1))
    t7e._byes.add(1)
    ok &= check(manager.Manager._owed_seats(t7e) == [],
                "a seat that has already said MjBYE is owed nothing")
    t7e._byes.discard(1)
    t7e.bots[1] = bots.Bot(1)
    ok &= check(manager.Manager._owed_seats(t7e) == [],
                "...nor is a seat a CPU is playing")
    t7e.bots.pop(1, None)
    ok &= check(manager.Manager._owed_seats(t7e) == [1],
                "...but a live human waiting on a queued record IS")

    # --- finding 30: room-qualified lobby tables -------------------------------
    m8 = manager.Manager()
    ta = m8.table_for_lobby(1, [(15, 0, "A", 0xA1)], room=1)
    tb_ = m8.table_for_lobby(1, [(6, 0, "B", 0xB1)], room=2)
    ok &= check(ta is not tb_ and m8.table_for_lobby(1, [], room=1, reset=False) is ta,
                "Room 1 table 1 and Room 2 table 1 are DIFFERENT tables")
    _chan_qualified = janseats is not None and hasattr(janseats, "table_channel")
    ok &= check((ta.channel == b"#MJS0T001001" and tb_.channel == b"#MJS0T002001")
                if _chan_qualified else (ta.channel == b"#MJS0T001" == tb_.channel),
                "...each on ITS ROOM's channel, the PTL row name the client "
                "JOINs (janseats.table_channel; bare #MJS0T00n without janseats): "
                "%r / %r" % (ta.channel, tb_.channel))
    tc_ = m8.table_for_lobby(manager.Manager.lobby_key(1, 2), [(6, 0, "B", 0xB1)])
    ok &= check(tc_ is tb_ and m8.table_for_lobby(2, [(7, 0, "C", 0xC1)],
                                                 room=2).channel
                == (b"#MJS0T002002" if _chan_qualified else b"#MJS0T002"),
                "a composite passed as lobby_id finds the same table")
    ok &= check(m8.table_for_lobby(3, [(9, 0, "D", 0xD1)]).channel == b"#MJS0T003",
                "room 0 (no room known) keeps the bare #MJS0T00n channel")
    ok &= check(manager.Manager.lobby_key(1, 2) == (2 << 16) | 1 and manager.Manager.lobby_key(3) == 3,
                "the key is room << 16 | table, the bare id without a room")
    if janseats is not None and hasattr(janseats, "split_table_id"):
        ok &= check(janseats.split_table_id(janseats.room_table_id(2, 1)) == (2, 1),
                    "janseats round-trips the composite id")

    # --- finding 29: rejoin ------------------------------------------------------
    m9 = manager.Manager()
    t9_ = _two_humans(m9, 121, 55)
    m9.handle(_rdy0, member_id=15)
    m9.handle(_client_ack(t9_._last_for[0], 0), member_id=15)
    m9.handle(_stamped(M.MjBYE, 1, f16=0), member_id=6)       # seat 1 quits
    ok &= check(1 in t9_.bots and 1 not in t9_.live, "rejoin rig: seat 1 is a bot now")
    m9.pending_for_member(6)

    class _Seats(object):
        pending = {6: True}

        @staticmethod
        def enabled():
            return True

        @staticmethod
        def rejoin_pending(member, clear=False):
            hit = _Seats.pending.get(int(member), False)
            if clear:
                _Seats.pending.pop(int(member), None)
            return hit

    _js = janseats
    jangame.janseats = _Seats
    try:
        o9 = m9.handle(_stamped(M.MjREADY, 1, f16=0), member_id=6)
    finally:
        jangame.janseats = _js
    ok &= check(1 in t9_.live and 1 not in t9_.bots and 1 not in t9_.dropped
                and any(e[0] == "rejoin" for e in t9_.log),
                "the rejoining member is put BACK on its seat, the bot goes")
    ok &= check(_ops(o9)[:1] == ["MjALLDATA"],
                "...and gets a full MjALLDATA resync on that line: %r" % _ops(o9))
    if t9_._awaiting and t9_._awaiting[1] == 1:
        ok &= check("MjTSUMO" in _ops(o9) or "MjNAKI" in _ops(o9),
                    "...plus the record it is waited on for: %r" % _ops(o9))
    ok &= check(not _Seats.pending, "...and the flag was cleared")

    # --- bot quality (audit, rules engine 3) ------------------------------------
    tbq = table.Table(122, seed=56)
    tbq.fill_with_bots()
    tbq.game = mj.Game(tbq.rules, rng=random.Random(56))
    tbq.kyoku = tbq.game.start_kyoku()
    kq_ = tbq.kyoku
    hq = kq_.hands[1]
    hq.tiles[:] = mj._hand_from("123m456m789m123s99s")[:13] + [mj._hand_from("7z")[0]]
    hq.drawn = hq.tiles[-1]
    hq.riichi = True
    ok &= check(tbq.bots[1].choose_discard(kq_) == hq.drawn,
                "a riichi bot discards what it drew")
    hq.riichi = False
    hq.drawn = None
    hq.melds = [mj.Meld(mj.CHI, mj._hand_from("123m")[:3], 0, mj._hand_from("1m")[0])]
    hq.tiles[:] = mj._hand_from("7z456m789m123s99s")[:11]   # 7z at slot 0, junk
    ok &= check(mj.kind(tbq.bots[1].choose_discard(kq_)) == mj.kind(hq.tiles[0]),
                "after a call (no draw) the bot searches the hand: the lone honour "
                "goes, not tiles[-1]")
    hq.tiles[:] = mj._hand_from("4p456m789m123s99s")[:11]
    hq.kuikae = {mj.kind(mj._hand_from("4p")[0])}
    ok &= check(kq_.kuikae_forbidden(1) == hq.kuikae or not tbq.rules.kuikae,
                "kuikae rig: the engine forbids 4p")
    if tbq.rules.kuikae:
        ok &= check(mj.kind(tbq.bots[1].choose_discard(kq_)) != mj.kind(hq.tiles[0]),
                    "...and the bot never chooses a kuikae-forbidden tile")
    hq.kuikae = set()
    # ...and a HUMAN's forbidden discard is refused with a re-offer.
    thq = table.Table(123, seed=57)
    thq.seat_player(0x9A, seat=0, member=15)
    thq.fill_with_bots()
    thq.handle(_rdy0)
    khq = thq.kyoku
    hhq = khq.hands[0]
    hhq.melds = [mj.Meld(mj.CHI, mj._hand_from("123m")[:3], 3, mj._hand_from("1m")[0])]
    hhq.menzen = False
    hhq.tiles[:] = mj._hand_from("4p456m789m123s99s")[:11]
    hhq.drawn = None
    hhq.kuikae = {mj.kind(mj._hand_from("4p")[0])}
    khq.turn = 0
    if thq.rules.kuikae:
        tq_ = thq.msg_tsumo(0, motion=0)
        ok &= check(tq_[0x54 + 0] & 2 == 0 and tq_[0x54 + 1] & 2,
                    "the forbidden slot loses the discardable bit in MjTSUMO +0x54")
        oq = thq.handle(_client_sute(tq_, 0, 0))
        ok &= check(any(e[0] == "refused-kuikae" for e in thq.log)
                    and len(hhq.tiles) == 11 and _ops(oq) == ["MjTSUMO"],
                    "a human's kuikae discard is refused and the turn re-offered: %r"
                    % _ops(oq))

    # --- HALF2 carries the janstats ladder and titles ----------------------------
    if janstats is not None:
        tst = table.Table(124, seed=58)
        tst.seat_player(0x7A7A, seat=0, member=0x7A7A)
        tst.fill_with_bots()
        tst.start_game()
        tst.game.scores = [30000, 25000, 25000, 20000]
        tst.game.hands_played = 8
        tst.msg_results()
        lv, up = tst._levels()
        ok &= check(lv[0] == janstats.level(0x7A7A) and up[0] == janstats.level_up(0x7A7A),
                    "HALF2 levels come from janstats.level/level_up: %r %r" % (lv, up))
        h2r = tst.msg_results2()
        held, got = tst._titles()
        ok &= check(len(held) == 5 and len(got) == 5 and len(h2r) >= 0xA0 + 16,
                    "HALF2 carries the [title][seat] blocks from janstats.shogo/getshogo")
        # a human win is written to the yaku counters
        tsw = table.Table(125, seed=59)
        tsw.seat_player(0x7B7B, seat=0, member=0x7B7B)
        tsw.fill_with_bots()
        tsw.handle(_rdy0)
        scw = _force_tsumo(tsw, 0)
        before_y = dict(janstats.load(0x7B7B).get("yaku") or {})
        tsw.handle(_client_sute(M.tsumo(1, 0, tsw.kyoku.hands[0].tiles, 60), 0, 0,
                                M.SUTE_TSUMO_AGARI))
        after_y = dict(janstats.load(0x7B7B).get("yaku") or {})
        ok &= check(tsw.kyoku.result is not None and after_y != before_y
                    and sum(after_y.values()) > sum(before_y.values()),
                    "a human's win reaches janstats.record_win: %r" % (after_y,))

    # --- sashiuma: the pre-game side-bet handshake -----------------------------
    def _client_uma(op, seat, block, seq=0):
        hdr = janwire.pack(opcode=op, f13=seq, src=seat, dst=4, f16=1, sub=3,
                           length=0x20)[:0x18]
        return bytes(hdr) + bytes(block) + bytes(8 - len(bytes(block)))
    _ready = lambda: janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                                  sub=3, length=0x18)[:0x18]
    _old_uma = (tablesashiuma.SASHIUMA, tablesashiuma.SASHIUMA_BOTS)
    tablesashiuma.SASHIUMA = True
    tablesashiuma.SASHIUMA_BOTS = "accept"
    tu = table.Table(21, seed=5)
    tu.seat_player(0x5151, b"Ume", seat=0)
    out = tu.handle(_ready())
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(ops == [M.MjSASHIUMASTART],
                "READY with sashiuma on offers the table FIRST, no deal yet: %r"
                % [narration.M_NAME(o) for o in ops])
    ok &= check(tu.kyoku is None and tu.state == "playing",
                "the deal is held while the handshake runs (playing, no kyoku)")
    sel = None
    if out:
        st = out[0]
        ok &= check(len(st) == 0x60 and st[0x18:0x1B] == b"Ume",
                    "START carries the human's name at +0x18: %r" % st[0x18:0x28])
        ok &= check(st[0x18 + 32:0x18 + 37] == b"CPU 2",
                    "and a bot's name in its own 16-byte slot: %r"
                    % st[0x18 + 32:0x18 + 48])
    # seat 0 proposes a bet with seat 2 (across): its bit in slot 5
    blk = bytearray(6)
    blk[M.sashiuma_slot(0, 2)] = M.sashiuma_bit(0)
    out = tu.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, blk))
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(ops == [M.MjSASHIUMASELECT],
                "the last REQUEST answers with SELECT: %r" % [narration.M_NAME(o) for o in ops])
    if out:
        sel = out[0][0x18:0x1E]
        ok &= check(sel[5] == (M.sashiuma_bit(0) | M.sashiuma_bit(2)),
                    "SELECT shows the bot ACCEPTED (both bits in slot 5): %r" % sel)
        ok &= check(all(sel[i] == 0 for i in range(5)),
                    "and no other pair is proposed: %r" % sel)
    # a REQUEST cannot set ANOTHER seat's bit
    tu2 = table.Table(22, seed=5)
    tu2.seat_player(0x5252, b"Spoof", seat=0)
    tu2.handle(_ready())
    bad = bytearray(6)
    bad[M.sashiuma_slot(0, 1)] = M.sashiuma_bit(1)      # seat 1's bit, sent by seat 0
    out2 = tu2.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, bad))
    ok &= check(bool(out2) and out2[0][0x18 + M.sashiuma_slot(0, 1)] == 0,
                "a reply's foreign bit is ignored (seat 0 cannot speak for seat 1)")
    # a re-sent REQUEST after the phase moved re-nudges, never re-runs
    again = tu.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, blk))
    ok &= check(len(again) == 1
                and janwire.unpack(again[0])["opcode"] == M.MjSASHIUMASELECT
                and tu.sashiuma["phase"] == "agree",
                "a re-sent REQUEST re-nudges the SELECT instead of advancing")
    # the human agrees: echoes the block with its bit (already set)
    out = tu.handle(_client_uma(M.MjSASHIUMAARGEE, 0, sel if sel else blk))
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(ops[:1] == [M.MjSASHIUMARESULT] and M.MjHAIPAI in ops,
                "the last ARGEE answers RESULT and the DEAL rides in the same "
                "batch: %r" % [narration.M_NAME(o) for o in ops])
    if out:
        res = out[0][0x18:0x1E]
        ok &= check(res[5] == 5 and all(res[i] == 0 for i in range(5)),
                    "RESULT marks the 0-2 bet ON (both bits) and nothing else: %r"
                    % res)
    ok &= check(tu.sashiuma_pairs == [(0, 2)] and tu.kyoku is not None,
                "the table remembers the ON pair and the hand is dealt: %r"
                % tu.sashiuma_pairs)
    # the payout: the pair's better-placed seat takes the stake off the other
    tu.game.scores = [30000, 25000, 20000, 25000]        # seat 0 1st, seat 2 last
    base = {r["seat"]: dict(r) for r in tu.game.ranking()}
    by = {r["seat"]: r for r in tu._sashiuma_apply(tu.game.ranking())}
    stake = tu._sashiuma_stake()
    ok &= check(stake == 20.0, "default stake = the table uma's top value: %g" % stake)
    ok &= check(abs(by[0]["result"] - (base[0]["result"] + stake)) < 1e-6
                and abs(by[2]["result"] - (base[2]["result"] - stake)) < 1e-6,
                "seat 0 (1st) takes the stake off seat 2 (last): %r / %r, stake %g"
                % (by[0]["result"], by[2]["result"], stake))
    ok &= check(by[1]["result"] == base[1]["result"]
                and by[3]["result"] == base[3]["result"],
                "seats outside the bet are untouched")
    # no pick at all -> no bets, and the deal still goes out
    tu3 = table.Table(23, seed=5)
    tu3.seat_player(0x5353, b"Pass", seat=0)
    tu3.handle(_ready())
    o1 = tu3.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, bytes(6)))
    o2 = tu3.handle(_client_uma(M.MjSASHIUMAARGEE, 0, bytes(6)))
    ok &= check([janwire.unpack(r)["opcode"] for r in o1] == [M.MjSASHIUMASELECT]
                and bool(o2) and o2[0][0x18:0x1E] == bytes(6)
                and tu3.sashiuma_pairs == [] and tu3.kyoku is not None,
                "an empty pick yields an all-zero RESULT, no bets, and the deal")
    # a human who quits mid-handshake stops being waited on (two humans)
    tu5 = table.Table(25, seed=5)
    tu5.seat_player(0x5555, b"Stay", seat=0)
    tu5.seat_player(0x5656, b"Quit", seat=1)
    o5 = tu5.handle(_ready())
    ok &= check([janwire.unpack(r)["opcode"] for r in o5]
                == [M.MjSASHIUMASTART, M.MjSASHIUMASTART],
                "both humans are offered the table: %r"
                % [narration.M_NAME(janwire.unpack(r)["opcode"]) for r in o5])
    b5 = bytearray(6)
    b5[M.sashiuma_slot(0, 1)] = M.sashiuma_bit(0)      # seat 0 proposes to seat 1
    ok &= check(tu5.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, b5)) == [],
                "one REQUEST in, the other human still owed: nothing goes out")
    o5 = tu5.drop_seat(1, "quit")
    ops5 = [janwire.unpack(r)["opcode"] for r in o5]
    ok &= check(ops5[:1] == [M.MjMEMBERLEAVE] and M.MjSASHIUMASELECT in ops5,
                "the quitter is announced and the phase moves on without it: %r"
                % [narration.M_NAME(o) for o in ops5])
    selrec = [r for r in o5 if janwire.unpack(r)["opcode"] == M.MjSASHIUMASELECT]
    ok &= check(bool(selrec) and selrec[0][0x18 + M.sashiuma_slot(0, 1)] == 3,
                "and the quitter's seat, now a bot, accepts the bet (slot 0 = 3)")
    o5 = tu5.handle(_client_uma(M.MjSASHIUMAARGEE, 0, bytes([3, 0, 0, 0, 0, 0])))
    ok &= check(tu5.sashiuma_pairs == [(0, 1)] and tu5.kyoku is not None,
                "the bet is ON with the substitute and the hand deals: %r"
                % tu5.sashiuma_pairs)
    # switched off: READY deals at once, exactly as before
    tablesashiuma.SASHIUMA = False
    tu4 = table.Table(24, seed=5)
    tu4.seat_player(0x5454, b"Off", seat=0)
    o4 = tu4.handle(_ready())
    ok &= check(bool(o4) and janwire.unpack(o4[0])["opcode"] == M.MjHAIPAI
                and tu4.sashiuma is None,
                "POL_JAN_SASHIUMA=0: READY deals at once, no handshake")
    tablesashiuma.SASHIUMA, tablesashiuma.SASHIUMA_BOTS = _old_uma

    # --- THE RECORD-BUILDING LAYER, at the client's read offsets --------------
    # (2026-09-04: the round-state block, the rule vector, the win screen,
    # the draw banner, the riichi flag, the kan records, HALF1.) Every offset
    # here is the one the C loads it from; janmsgs' banners cite the lines.
    _rdy = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                        length=0x18)[:0x18]
    tq = table.Table(30, seed=101)
    tq.seat_player(0xB001, seat=0)
    tq.fill_with_bots()
    deal_out = tq.handle(_rdy)
    hp = [r for r in deal_out if janwire.unpack(r)["opcode"] == M.MjHAIPAI]
    kq = tq.kyoku
    scq = tq._round_scalars()
    ok &= check(len(hp) == 1, "one deal record for the one live seat")
    if hp:
        hp = hp[0]
        # +0xd0..+0xd6 (console.c:3201-3207): chiicha, dealer, round wind,
        # kyoku, honba, die 1, die 2 -- the wall break the client computes is
        # (dealer + d1 + d2 + 1) & 3 (console.c:3304), so the dice are 1..6.
        ok &= check(hp[0xD0] == scq.chiicha and hp[0xD1] == kq.dealer
                    and hp[0xD2] == 0 and hp[0xD3] == kq.kyoku
                    and hp[0xD4] == kq.honba,
                    "HAIPAI +0xd0 chiicha, +0xd1 dealer, +0xd2 round wind (East=0), "
                    "+0xd3 kyoku, +0xd4 honba: %r" % list(hp[0xD0:0xD7]))
        ok &= check((hp[0xD5], hp[0xD6]) == tuple(getattr(kq, "dice", scq.dice))
                    and 1 <= hp[0xD5] <= 6 and 1 <= hp[0xD6] <= 6,
                    "+0xd5/+0xd6 are the engine's two dice, 1..6: %r"
                    % [hp[0xD5], hp[0xD6]])
        # The rule vector (+0x2c): the DORA enum, the 0-based red rank, uma
        # in thousands, per-suit red counts -- from `self.rules`.
        rv, aka3 = tq._rule_scalars()
        ok &= check(hp[0x2D] == rv[M.RULE_DORA] and hp[0x2D] in range(5),
                    "+0x2d the DORA rule enum from the rules: %d" % hp[0x2D])
        ok &= check(hp[0x2E] == 4, "+0x2e red-five rank, 0-based (fives): %d" % hp[0x2E])
        ok &= check((hp[0x3C], hp[0x3D]) == (20, 10),
                    "+0x3c/+0x3d the uma for 1st/2nd in thousands: %r"
                    % [hp[0x3C], hp[0x3D]])
        ok &= check(hp[0x3E:0x41] == aka3 and sum(aka3) == 3,
                    "+0x3e..+0x40 the per-suit red-five counts: %r" % list(aka3))
        ok &= check(hp[0x31] == 1, "+0x31 riichi-needs-1000, as the engine plays it")
        # The dead wall at +0x44 (u16): dora-1 at slot 4 with 0x80 = revealed,
        # the flip target of the client's own post-deal 002e5e00.
        wq = struct.unpack_from("<14H", hp, 0x44)
        ok &= check((wq[4] & 0x3F) == (M.tile_byte(kq.wall.dead[4]) & 0x3F) and wq[4] & 0x80
                    and all((v & 0x80) == 0 for i, v in enumerate(wq) if i != 4),
                    "the deal's dead wall: indicator at slot 4 revealed, rest "
                    "face-down: %r" % ["%#x" % v for v in wq])
    # ALLDATA's bit-packed copy (console.c:3923-3935) and SEISAN's byte
    # copy (mahdisp.c:788-793) come from the same structure.
    kq.riichi_sticks = 2
    kq.honba = 1
    scq = tq._round_scalars()
    ad = tq.msg_alldata()
    ok &= check(ad[0x13C] == 2, "ALLDATA +0x13c is the riichi-stick POT: %d" % ad[0x13C])
    ok &= check(ad[0x13B] == 1 and (ad[0x13E] & 1) == 0
                and ((ad[0x13D] >> 2) & 3) == kq.dealer
                and ((ad[0x13D] >> 4) & 3) == kq.kyoku
                and (ad[0x13D] >> 6) == scq.chiicha
                and (ad[0x13F] & 0xF, ad[0x13F] >> 4) == tuple(scq.dice),
                "ALLDATA +0x13b honba, +0x13d dealer/kyoku/chiicha, +0x13e bit0 "
                "round wind, +0x13f the dice")
    se = tq.msg_seisan()
    ok &= check(tuple(se[0x28:0x2D]) == (kq.turn & 3, kq.dealer, 0, kq.kyoku, 1),
                "SEISAN +0x28.. = (current, dealer, wind, kyoku, honba): %r"
                % list(se[0x28:0x2D]))
    kq.riichi_sticks = 0
    kq.honba = 0

    # The payment words, per mahdisp.c:722-741 (in POINTS; honba stripped).
    class _Sc(object):
        def __init__(self, payments, points=0, yaku=(), han=1, fu=30, limit=""):
            self.payments, self.points, self.yaku = payments, points, list(yaku)
            self.han, self.fu, self.limit = han, fu, limit
    pw = table.Table._payment_words
    ok &= check(pw(_Sc({1: 1000, 3: -1000}), 1, 0, 1, 0) == (1000, 0),
                "a 1000-point ron sends 1000 in +0x64 -- the client shows it as-is")
    ok &= check(pw(_Sc({1: 1300, 3: -1300}), 1, 0, 1, 1) == (1000, 0),
                "honba (300/ron) is stripped from the word: the client adds none")
    ok &= check(pw(_Sc({0: 1500, 1: -500, 2: -500, 3: -500}), 0, 0, 0, 0) == (500, 0),
                "dealer tsumo: +0x64 = the per-player 500 (client x3)")
    ok &= check(pw(_Sc({1: 2000, 0: -1000, 2: -500, 3: -500}), 1, 0, 0, 0) == (500, 1000),
                "non-dealer tsumo: +0x64 = 500 from each non-dealer, +0x68 = "
                "the dealer's 1000 (client: 1000 + 500 x 2)")
    ok &= check(pw(_Sc({1: 2600, 0: -1200, 2: -700, 3: -700}), 1, 0, 0, 2) == (500, 1000),
                "and honba (100 each per tsumo) is stripped there too")

    # A real ron: the win reveal (14 tiles) then the YAKUDISP with the pairs.
    tw2 = table.Table(31, seed=103)
    tw2.seat_player(0xB002, seat=0)
    tw2.fill_with_bots()
    tw2.handle(_rdy)
    kw2 = tw2.kyoku
    _ron_hand(kw2.hands[1])
    kw2.last_discard_seat, kw2.last_discard = 0, mj.SOU + 8
    kw2.hands[0].pond.append(mj.SOU + 8)
    wins = kw2.win_ron([1])
    ok &= check(bool(wins), "the riichi hand rons the 9s")
    if wins:
        seat_w, sc_w = wins[0]
        out_w = tw2.on_win(seat_w, sc_w, ron_from=0)
        ops_w = [janwire.unpack(r)["opcode"] for r in out_w]
        ok &= check(ops_w == [M.MjALLDATA, M.MjYAKUDISP],
                    "on_win(ron) = the reveal ALLDATA then YAKUDISP: %r"
                    % [narration.M_NAME(o) for o in ops_w])
        yd = out_w[-1]
        rows_w = M.yaku_rows(sc_w.yaku)
        pairs_w = [struct.unpack_from("<HH", yd, 0x22 + 4 * i)
                   for i in range(len(rows_w) + 1)]
        ok &= check(pairs_w[:-1] == rows_w and pairs_w[-1] == (0, 0) and rows_w
                    and all(1 <= rid <= 44 for rid, _h in rows_w),
                    "+0x22 carries the (yaku id, han) pairs of the scored hand, "
                    "0-terminated: %r for %r" % (pairs_w, sc_w.yaku))
        ok &= check(yd[0x18] == sc_w.han and yd[0x19] == sc_w.fu and yd[0x1A] == 1
                    and yd[0x1B] == kw2.dealer and yd[0x1C] == 1,
                    "+0x18 han, +0x19 fu, +0x1a winner 1, +0x1b dealer, +0x1c ron")
        ok &= check(yd[0x1D] == 0 and yd[0x1E] == kw2.kyoku and yd[0x1F] == kw2.honba,
                    "+0x1d/+0x1e/+0x1f the round header (mahdisp.c:607-609)")
        ok &= check(yd[0x21] == 1, "+0x21 show-ura = 1: the winner was in riichi")
        base_w = struct.unpack_from("<i", yd, 0x64)[0]
        ok &= check(base_w == -sc_w.payments[0] and base_w > 0
                    and struct.unpack_from("<i", yd, 0x68)[0] == 0,
                    "+0x64 = what the discarder pays (%d), +0x68 = 0 on a ron"
                    % base_w)
        # the reveal: 14 tiles for seat 1, the claimed 9s flagged in seat 0's
        # pond (bit 6 = the pond closes over it), motion 0
        wr = out_w[0]
        row_w = [wr[M.ALLDATA_HAND + M.ALLDATA_HAND_STRIDE + i] & 0x3F for i in range(14)]
        pond0 = wr[M.ALLDATA_POND + len(kw2.hands[0].pond) - 1]
        ok &= check(wr[0x18] == 0 and sum(1 for b in row_w if b) == 14
                    and row_w.count(M.tile_byte(mj.SOU + 8)) == 2
                    and (pond0 & 0x3F) == M.tile_byte(mj.SOU + 8) and pond0 & M.POND_FLAG,
                    "the reveal is a motion-0 ALLDATA: winner's 13 + the ronned "
                    "tile, and that tile flagged claimed in the pond")
    # A tsumo needs no reveal record.
    tw3 = table.Table(32, seed=105)
    tw3.seat_player(0xB003, seat=0)
    tw3.fill_with_bots()
    tw3.handle(_rdy)
    kw3 = tw3.kyoku
    out_t = tw3.on_win(0, _Sc({0: 1500, 1: -500, 2: -500, 3: -500}, 1500,
                              [("menzen_tsumo", 1)], 1, 30))
    ok &= check([janwire.unpack(r)["opcode"] for r in out_t] == [M.MjYAKUDISP]
                and struct.unpack_from("<i", out_t[0], 0x64)[0] == 500
                and out_t[0][0x1C] == 0,
                "a tsumo is the YAKUDISP alone, per-player 500 in +0x64")

    # The exhaustive-draw beat (console.c:4064-4099): ONE motion-10 ALLDATA
    # with the TENPAI seats in +0x22, then SEISAN; an abort sends mask 0.
    td = table.Table(33, seed=107)
    td.seat_player(0xB004, seat=0)
    td.fill_with_bots()
    td.handle(_rdy)
    kd = td.kyoku
    kd.result = (mj.DRAW, {"tenpai": [0, 2], "nagashi": [], "dealer_repeat": True})
    out_d = td.on_kyoku_end()
    ops_d = [janwire.unpack(r)["opcode"] for r in out_d]
    ok &= check(ops_d == [M.MjALLDATA, M.MjSEISAN] and out_d[0][0x18] == 10
                and out_d[0][0x22] == 0b0101,
                "a draw = motion-10 ALLDATA with mask 0b0101 (seats 0, 2 tenpai) "
                "BEFORE the SEISAN: %r mask %#x"
                % ([narration.M_NAME(o) for o in ops_d], out_d[0][0x22] if out_d else -1))
    if out_d:
        rows_d = [[out_d[0][M.ALLDATA_HAND + s * M.ALLDATA_HAND_STRIDE + i] & 0x3F
                   for i in range(14)] for s in range(4)]
        ok &= check(sum(1 for b in rows_d[2] if b) >= 13,
                    "and the tenpai bot's real tiles are in the hand block")
    _old_cr = knobs.CONCEAL_RESYNC
    knobs.CONCEAL_RESYNC = True
    out_d2 = td.on_kyoku_end()
    if out_d2:
        rows_d2 = [[out_d2[0][M.ALLDATA_HAND + s * M.ALLDATA_HAND_STRIDE + i] & 0x3F
                    for i in range(14)] for s in range(4)]
        ok &= check(any(rows_d2[0]) and any(rows_d2[2]) and not any(rows_d2[1])
                    and not any(rows_d2[3]),
                    "under CONCEAL_RESYNC the noten opponents stay blank, the "
                    "tenpai ones reveal")
    knobs.CONCEAL_RESYNC = _old_cr
    kd.result = (mj.ABORT, {"why": "kyushu", "dealer_repeat": True})
    out_a = td.on_kyoku_end()
    ok &= check(len(out_a) == 2 and out_a[0][0x18] == 10 and out_a[0][0x22] == 0,
                "an abortive draw still sends the banner record, mask 0 "
                "(the handler fires banner 6 unconditionally)")

    # Bot riichi: +0x21 on the motion-3 record (yamaguchi.c:7920-7932).
    tr2 = table.Table(34, seed=109)
    tr2.seat_player(0xB005, seat=0)
    tr2.fill_with_bots()
    tr2.handle(_rdy)
    kr2 = tr2.kyoku
    t_r = kr2.hands[2].tiles[0]
    recs_r = tr2._show_discard(2, t_r, 0, riichi=True)
    last_r = recs_r[-1]
    ok &= check(janwire.unpack(last_r)["opcode"] == M.MjTSUMO and last_r[0x18] == 3
                and last_r[0x21] == 1 and last_r[0x22] == 2,
                "_show_discard(riichi=True) marks the motion-3 record: +0x21 = 1")
    ok &= check(tr2._show_discard(2, t_r, 0)[-1][0x21] == 0,
                "and an ordinary discard leaves +0x21 = 0")
    ok &= check(tr2.msg_tsumo(2, discard=(t_r, 0, 2, True))[0x21] == 1
                and tr2.msg_tsumo(2, discard=(t_r, 0, 2))[0x21] == 0,
                "msg_tsumo(discard=(tile, slot, seat, riichi)) threads it")

    # The KAN records, both paths. An ankan by seat 0 on its own turn.
    tk = table.Table(35, seed=111)
    tk.seat_player(0xB006, seat=0)
    tk.fill_with_bots()
    tk.handle(_rdy)
    kk = tk.kyoku
    hk = kk.hands[kk.dealer]
    kseat = kk.dealer
    hk.tiles[:] = ([mj.MAN] * 4 + [mj.MAN + 1, mj.MAN + 2, mj.MAN + 3]
                   + [mj.PIN + 4, mj.PIN + 5, mj.PIN + 6]
                   + [mj.SOU + 7, mj.SOU + 7, mj.SOU + 8, mj.SOU + 3])
    hk.drawn = hk.tiles[-1]
    hk.melds[:] = []
    kk.turn = kseat
    ok &= check(0 in kk.ankan_options(kseat), "the four 1m may be ankan'd now")
    pre_k = tk.call_snapshot()
    ok &= check(tuple(pre_k) == (pre_k[0], pre_k[1]) and pre_k.kans == 0
                and pre_k.dora_shown == 1,
                "call_snapshot() unpacks as (hands, ponds) and carries the wall state")
    kk.call_ankan(kseat, 0)
    ok &= check(hk.drawn is not None and kk.wall.kans == 1 and kk.wall.dora_shown == 2,
                "the engine drew the rinshan tile and revealed the kan-dora")
    _old_km = knobs.KAN_MOTION
    knobs.KAN_MOTION = False
    out_k = tk.msg_kan("ankan", kseat, pre_k)
    ok &= check([janwire.unpack(r)["opcode"] for r in out_k] == [M.MjALLDATA, M.MjTSUMO]
                and out_k[0][0x18] == 0 and out_k[1][0x18] == 2,
                "KAN_MOTION off: the resync path (ALLDATA subtype 0 + TSUMO motion 2)")
    wk0 = out_k[0][0x44:0x52]
    ok &= check(wk0[0] == 0 and wk0[1] != 0 and (wk0[6] & 0x40) and (wk0[4] & 0x40)
                and (wk0[8] & 0x40) == 0,
                "the resync's dead wall: rinshan slot 0 EMPTY, indicators at 4 "
                "and 6 revealed: %r" % ["%#x" % v for v in wk0])
    knobs.KAN_MOTION = True
    out_k = tk.msg_kan("ankan", kseat, pre_k)
    ops_k = [janwire.unpack(r)["opcode"] for r in out_k]
    ok &= check(ops_k == [M.MjALLDATA, M.MjTSUMO] and out_k[0][0x18] == 7
                and out_k[1][0x18] == 0,
                "KAN_MOTION on: ONE motion-7 ALLDATA then a motion-0 TSUMO (the "
                "handler flies the rinshan itself): %r motions %r"
                % ([narration.M_NAME(o) for o in ops_k], [r[0x18] for r in out_k]))
    rk = out_k[0]
    ok &= check(rk[0x1B] == M.tile_byte(mj.MAN) and rk[0x1C] == M.tile_byte(hk.drawn)
                and rk[0x1D] == 0 and rk[0x1E] == 3 and rk[0x22] == kseat
                and rk[0x23 + kseat] == (1 if knobs.ISHUMAN_ENABLE and kseat not in tk.bots
                                         else 0),
                "motion 7 fields: +0x1b the tile, +0x1c the RINSHAN tile, +0x1d "
                "first slot 0 (+1, +2 implied), +0x1e the 4th slot 3, +0x22 the "
                "seat, +0x23+seat = IsHuman (0 = a COM's rinshan flies now; a "
                "human's is 1 since 2026-09-09 and the TSUMO behind it carries "
                "the hand)")
    wk = rk[0x44:0x52]
    ok &= check(wk[0] != 0 and (wk[6] & 0x40) and (wk[4] & 0x40)
                and (wk[8] & 0x40) == 0,
                "the kan record's dead wall: rinshan tile STILL at slot 0, the "
                "new indicator at slot 6 already revealed (the chain scans "
                "12,10,8,6 for it): %r" % ["%#x" % v for v in wk])
    hand_k = [rk[M.ALLDATA_HAND + kseat * M.ALLDATA_HAND_STRIDE + i] & 0x3F
              for i in range(14)]
    ok &= check(hand_k[:4] == [M.tile_byte(mj.MAN)] * 4,
                "the hand block is PRE-kan (the four 1m still in slots 0..3)")
    ok &= check(rk[M.ALLDATA_MELD + kseat * M.ALLDATA_MELD_STRIDE] & 0x3F
                == M.tile_byte(mj.MAN),
                "and the meld block is POST-kan (the ankan is in it)")
    # the deferred form: a provisional kan holding for the chankan window
    out_kd = tk.msg_kan("ankan", kseat, pre_k, rinshan="defer")
    ok &= check(len(out_kd) == 1 and out_kd[0][0x18] == 7
                and out_kd[0][0x23 + kseat] == 1 and out_kd[0][0x1C] == 0,
                "rinshan='defer': the kan record alone, +0x23+seat = 1 holds "
                "the flight, +0x1c empty -- the caller's MjTSUMO motion 9 draws it")
    knobs.KAN_MOTION = False
    out_kd = tk.msg_kan("ankan", kseat, pre_k, rinshan="defer")
    ok &= check(len(out_kd) == 1 and out_kd[0][0x18] == 0,
                "deferred on the resync path: the ALLDATA alone")
    # A daiminkan on another seat's discard: motion 6 with the discarder.
    tk2 = table.Table(36, seed=113)
    tk2.seat_player(0xB007, seat=0)
    tk2.fill_with_bots()
    tk2.handle(_rdy)
    kk2 = tk2.kyoku
    dsc = kk2.dealer
    clr = (dsc + 2) % 4
    hc = kk2.hands[clr]
    hc.tiles[:] = ([mj.PIN + 1] * 3 + [mj.MAN + 1, mj.MAN + 2, mj.MAN + 3]
                   + [mj.PIN + 4, mj.PIN + 5, mj.PIN + 6]
                   + [mj.SOU + 7, mj.SOU + 7, mj.SOU + 8, mj.SOU + 3])
    hc.drawn = None
    hc.melds[:] = []
    kk2.last_discard, kk2.last_discard_seat = mj.PIN + 1, dsc
    kk2.hands[dsc].pond.append(mj.PIN + 1)
    pre_k2 = tk2.call_snapshot()
    kk2.call_pon(clr, kan=True)
    knobs.KAN_MOTION = True
    out_k2 = tk2.msg_kan("minkan", clr, pre_k2, discarder=dsc)
    rk2 = out_k2[0]
    ok &= check(rk2[0x18] == 6 and rk2[0x1A] == dsc and rk2[0x1B] == M.tile_byte(mj.PIN + 1)
                and rk2[0x1C] == M.tile_byte(hc.drawn)
                and (rk2[0x1D], rk2[0x1E], rk2[0x1F]) == (0, 1, 2) and rk2[0x22] == clr
                and out_k2[1][0x18] == 0,
                "daiminkan = motion 6: +0x1a discarder, +0x1b tile, +0x1c rinshan, "
                "+0x1d..+0x1f the three slots, +0x22 caller; TSUMO motion 0")
    pond_d = rk2[M.ALLDATA_POND + dsc * M.ALLDATA_POND_STRIDE + len(kk2.hands[dsc].pond) - 1]
    ok &= check((pond_d & 0x3F) == M.tile_byte(mj.PIN + 1) and not (pond_d & M.POND_FLAG),
                "the pond block is PRE-call: the claimed 2p still at the tail, unflagged")
    wk2 = rk2[0x44:0x52]
    ok &= check((wk2[6] & 0x40) and kk2.wall.dora_shown == 1
                and getattr(kk2, "pending_dora", 0) == 1,
                "a daiminkan's deferred kan-dora (engine: owed until the discard) "
                "is shown revealed on the record, as the client's chain will")
    knobs.KAN_MOTION = _old_km

    # HALF1: per-SEAT columns from a finished game (yamaguchi2.c:8430-9800).
    th = table.Table(37, seed=115)
    th.seat_player(0xB008, seat=0)
    th.fill_with_bots()
    th.handle(_rdy)
    th.game.scores[:] = [42000, 8000, 30000, 20000]
    h1 = th.msg_results()
    rank_h = th.game.ranking()
    ok &= check(len(h1) == M.HALF1_LEN == 0xF8, "HALF1 covers +0xec: %#x" % len(h1))
    ok &= check(tuple(h1[0xE8:0xEC]) == (0, 3, 1, 2),
                "+0xe8 + seat = the zero-based place by SEAT: %r" % list(h1[0xE8:0xEC]))
    ok &= check(struct.unpack_from("<4i", h1, 0x58) == (42000, 8000, 30000, 20000)
                and struct.unpack_from("<4i", h1, 0x68) == (42000, 8000, 30000, 20000),
                "+0x58/+0x68 the raw scores by seat (equal: no disconnect stage)")
    # WARNING: THE COLUMNS ARE IN P, NOT POINTS (2026-09-12). x1000 made the results
    # screen run for HALF AN HOUR: the uma/yakitori/sashiuma stages animate ONE
    # FRAME PER UNIT of the largest delta (yamaguchi2__00332610/00333370/
    # 00333a90), so a 10-20 uma at x1000 is a 20000-frame stage. See
    # msg_results.
    pts_h = struct.unpack_from("<4i", h1, 0x78)
    ok &= check(pts_h == (12 + 20, -22, 0, -10),
                "+0x78 = (score - 30000 return)/1000 in P (+ the 20 oka for "
                "1st): %r" % (pts_h,))
    uma_h = struct.unpack_from("<4i", h1, 0x88)
    ok &= check(uma_h == (52, -42, 10, -20),
                "+0x88 = after uma (+20/+10/-10/-20 by place, in P): %r" % (uma_h,))
    fin_h = struct.unpack_from("<4i", h1, 0xA8)
    ok &= check(all(fin_h[r["seat"]] == narration._round_p(r["result"]) for r in rank_h)
                and fin_h == uma_h,
                "+0xa8 = ranking()['result'] in P, and with no yakitori/sashiuma "
                "it equals the uma column: %r" % (fin_h,))
    ok &= check(max(a - p for a, p in zip(uma_h, pts_h)) <= 90,
                "and the uma stage's biggest delta fits the 0x5a frames SE "
                "hard-codes for the stages that DO divide -- one frame per "
                "unit is the animation: %d frames"
                % max(a - p for a, p in zip(uma_h, pts_h)))

    # =========================================================================
    # THE GALLERY (spectators, 2026-09-04): the Manager-level lifecycle. The
    # seat store is OFF here (janhourou's selftest drives the store end to
    # end); this is the copy stream, the ack discipline and the drops.
    # =========================================================================
    _old_seats_env = os.environ.get("POL_JAN_SEATS")
    os.environ["POL_JAN_SEATS"] = "0"

    def _gack(seq, slot, f16=1):
        """console__00284310: op 0x30, +0x13 seq, +0x14 slot, +0x15 4, +0x17 6."""
        return janwire.pack(opcode=M.MjGALLEYACK, f13=seq, src=slot, dst=4,
                            f16=f16, sub=6, length=0x18)[:0x18]

    mg = manager.Manager()
    tg = mg.table_for_lobby(301, [(15, 0, "A", 0xAAAA)])
    tg.rng = random.Random(77)
    deal_g = mg.handle(_rdy0, member_id=15)
    ok &= check(tg.state == "playing" and _ops(deal_g)[:1] == ["MjHAIPAI"],
                "gallery rig: a solo human's table dealt")
    ok &= check(mg.add_spectator(tg, 44, 2) == 2 and mg.spectator_table(44) is tg
                and mg.by_member.get(44) is tg and tg.seat_of_member(44) is None
                and 44 not in tg.live,
                "a spectator joins mid-hand: slot 2, routed to the table, NEVER a seat")
    ok &= check(mg.pending_for_member(44) == [],
                "NOTHING is queued before its first MjGALLEYACK (the client drains "
                "sub 1 to empty first, lobby.c:299-309)")
    o1 = mg.handle(_client_ack(tg._last_for[0], 0), member_id=15)   # HAIPAIACK -> play
    ok &= check(mg.pending_for_member(44) == [],
                "...even while the hand moves on without it")
    ok &= check(mg.handle(_gack(0, 2, f16=0), member_id=44) == [],
                "the entry GALLEYACK (f16=0) is answered with nothing on the line")
    snap_g = mg.pending_for_member(44)
    ok &= check(len(snap_g) == 1 and _ops(snap_g) == ["MjALLDATA"] and snap_g[0][0x18] == 0
                and all(any(snap_g[0][M.ALLDATA_HAND + s * M.ALLDATA_HAND_STRIDE + i]
                            for i in range(13)) for s in range(4)),
                "...and its FIRST record is a subtype-0 MjALLDATA with all four "
                "hands filled: %r" % _ops(snap_g))
    ok &= check(44 in tg.gallery_acked and tg.state == "playing",
                "the first ack marks its sub-6 reader live; the game did not move")
    mg.handle(_gack(0, 2, f16=0), member_id=44)         # the second entry ack
    ok &= check(mg.pending_for_member(44) == [], "the second entry ack queues nothing new")
    # From here EVERY record the human gets is copied -- byte for byte, the
    # seat-addressed draw included (a solo table hands the human everything).
    last_g = tg._last_for[0]
    hh_g = janwire.unpack(last_g)
    ok &= check(hh_g["opcode"] == M.MjTSUMO and tg._awaiting == (M.MjTSUMO, 0),
                "gallery rig: the human is on the clock")
    hand_g = [v for v in struct.unpack_from("<14H", last_g, M.TSUMO_HAND) if v]
    o2 = mg.handle(_client_sute(last_g, 0, len(hand_g) - 1), member_id=15)
    cp2 = mg.pending_for_member(44)
    ok &= check(o2 and cp2 == o2,
                "after the human's discard the spectator holds a COPY of every "
                "record the human got, byte for byte: %r" % _ops(cp2))
    ok &= check(any(to == 0 for _r, to in tg._awaiting_recs)
                and janwire.unpack(tg._last_for[0])["opcode"] == M.MjTSUMO,
                "...including the seat-addressed MjTSUMO draw (to=0)")
    # A silent spectator: it never acks the copies; the hand still advances.
    wall_g = tg.wall_count()
    turns_g = 0
    while (tg._awaiting and tg._awaiting[0] == M.MjTSUMO and tg._awaiting[1] == 0
           and turns_g < 3 and tg.kyoku is not None and tg.kyoku.result is None):
        lg = tg._last_for[0]
        hg = [v for v in struct.unpack_from("<14H", lg, M.TSUMO_HAND) if v]
        mg.handle(_client_sute(lg, 0, len(hg) - 1), member_id=15)
        turns_g += 1
    ok &= check(turns_g >= 1 and tg.wall_count() < wall_g,
                "a SILENT spectator never stalls the hand (%d human turn(s), wall "
                "%d -> %d)" % (turns_g, wall_g, tg.wall_count()))
    ok &= check(44 not in tg.timeouts and 44 not in tg.dropped,
                "...and it is never struck or dropped for its silence")
    cp3 = mg.pending_for_member(44)
    ok &= check(len(cp3) >= turns_g and 44 in tg.gallery,
                "its copies keep queueing meanwhile (%d)" % len(cp3))
    # Its GALLEYACK for a copy: bookkeeping, no mutation.
    state_g = (tg.kyoku.turn, tg.wall_count(), tg._awaiting, len(tg.log))
    seq_g = janwire.unpack(cp3[-1])["f13"]
    ok &= check(mg.handle(_gack(seq_g, 2), member_id=44) == []
                and tg.gallery_seq.get(44) == seq_g
                and (tg.kyoku.turn, tg.wall_count(), tg._awaiting) == state_g[:3],
                "a GALLEYACK (f16=1) is consumed: seq noted, nothing on the line, "
                "nothing moved")
    # A stray MjSASHIUMAREQUEST (its client sends one if it ever sees a
    # SASHIUMASTART): ignored -- no mutation, and NEVER the ghost MjGAMEEND.
    stray_g = janwire.pack(opcode=M.MjSASHIUMAREQUEST, f13=seq_g, src=0, dst=4,
                           f16=1, sub=3, length=0x20)[:0x20]
    ok &= check(mg.handle(stray_g, member_id=44) == []
                and (tg.kyoku.turn, tg.wall_count(), tg._awaiting) == state_g[:3]
                and tg.state == "playing"
                and not any(e[0] == M.MjGAMEEND for e in tg.log)
                and ("gallery-ignored", (44, "MjSASHIUMAREQUEST")) in tg.log,
                "a stray MjSASHIUMAREQUEST from a spectator is ignored, never a "
                "ghost MjGAMEEND")
    ok &= check(mg.handle(_stamped(M.MjALLDATAACK, 0), member_id=44) == []
                and tg._awaiting == state_g[2],
                "a spectator stamped like seat 0 is still routed as a spectator "
                "(its +0x14 is a slot, never a seat)")
    # The sashiuma handshake is never copied to a spectator.
    tg._gallery_copy(M.sashiuma_start(tg.next_seq(), ["A", "B", "C", "D"]))
    ok &= check(not any(janwire.unpack(r)["opcode"] in table.Table.GALLERY_NEVER
                        for r in (tg.outbox.get(tg.gallery_key(44)) or [])),
                "MjSASHIUMASTART is never copied to the gallery")
    mg.pending_for_member(44)
    # A second spectator that never acks gets nothing and moves nothing.
    mg.add_spectator(tg, 45, 0)
    tg.begin_routing()
    mg.handle(_stamped(M.MjALLDATAACK, 0), member_id=15)   # any human line
    ok &= check(mg.pending_for_member(45) == [] and 45 in tg.gallery
                and 45 not in tg.gallery_acked,
                "a spectator that has not acked yet is a member with no copies")
    # The optional skip: a server->spectator GALLEYACK on sub 6 after a
    # broadcast wait completes -- only to spectators that have acked.
    _old_skip = tablegallery.GALLERY_SKIP
    tablegallery.GALLERY_SKIP = True
    tg._gallery_skip()                       # what _ack_advance(MjYAKUDISP) calls
    tablegallery.GALLERY_SKIP = _old_skip
    skip_g = [r for r in (tg.outbox.get(tg.gallery_key(44)) or [])
              if janwire.unpack(r)["sub"] == 6]
    ok &= check(len(skip_g) == 1 and len(skip_g[0]) == 0x18 and skip_g[0][0x12] == 0x30
                and skip_g[0][0x14] == 2 and skip_g[0][0x17] == 6
                and not (tg.outbox.get(tg.gallery_key(45)) or []),
                "POL_JAN_GALLERY_SKIP: one 0x18-byte op-0x30 sub-6 record with the "
                "slot at +0x14 to the acked spectator only: %r" % [len(r) for r in skip_g])
    mg.pending_for_member(44)
    # Eviction: a spectator that stops draining is dropped, never re-nudged.
    tg.outbox[tg.gallery_key(44)] = [b"x"] * table.Table.GALLERY_OUTBOX_MAX
    tg.begin_routing()
    tg._emit(M.gameend(tg.next_seq()), "probe")        # any record
    tg.take_routes()
    mg._release_galleries()
    ok &= check(44 not in tg.gallery and mg.by_member.get(44) is None
                and tg.outbox.get(tg.gallery_key(44)) is None
                and any(e[0] == "spectator-gone" and e[1][0] == 44 for e in tg.log),
                "a spectator with %d undrained records is EVICTED (outbox dropped, "
                "routing forgotten)" % table.Table.GALLERY_OUTBOX_MAX)
    tg.state = "playing"                                # undo the probe's emit
    tg._awaiting = state_g[2]
    # GAMEEND: the copy reaches an acked spectator; its f16=0 ack drops it.
    mg.add_spectator(tg, 44, 2)
    mg.handle(_gack(0, 2, f16=0), member_id=44)
    mg.pending_for_member(44)
    tg.begin_routing()
    ge_g = tg.msg_gameend()
    tg.take_routes()
    mg._release_galleries()
    got_ge = mg.pending_for_member(44)
    ok &= check(got_ge and got_ge[-1] == ge_g and 44 in tg.gallery,
                "the MjGAMEEND copy reaches the spectator (membership kept until "
                "its exit ack so the copy can drain)")
    ok &= check(mg.handle(_gack(janwire.unpack(ge_g)["f13"], 2, f16=0), member_id=44) == []
                and 44 not in tg.gallery and mg.spectator_table(44) is None
                and mg.by_member.get(44) is None,
                "its GALLEYACK f16=0 after MjGAMEEND (the BYE-equivalent) drops the "
                "membership and the routing")
    ok &= check(mg.handle(_gack(0, 2, f16=0), member_id=44) == []
                and mg.by_member.get(44) is None and 44 not in mg.tables,
                "the SECOND exit ack (lobby.c:326), now with no table, is silence -- "
                "never the unknown-member ghost MjGAMEEND, never a minted table")
    ok &= check(mg.forget_spectator(45, "left", store=False) is tg and 45 not in tg.gallery
                and mg.forget_spectator(45, store=False) is None,
                "forget_spectator drops a never-acked spectator too, once")
    # A new game at the table starts with an empty gallery.
    mg.add_spectator(tg, 46, 0)
    tg.reset_for_new_game()
    ok &= check(tg.gallery == {} and tg.gallery_acked == set(),
                "reset_for_new_game empties the gallery")
    mg.by_member.pop(46, None)

    # CONCEALMENT ON: the spectator's copies reveal every seat, same seq.
    _old_cd, _old_cr = knobs.CONCEAL_DEAL, knobs.CONCEAL_RESYNC
    knobs.CONCEAL_DEAL = True
    knobs.CONCEAL_RESYNC = True
    mc_ = manager.Manager()
    tcg = mc_.table_for_lobby(302, [(15, 0, "A", 0xAAAA)])
    tcg.rng = random.Random(78)
    mc_.add_spectator(tcg, 47, 1)
    mc_.handle(_gack(0, 1, f16=0), member_id=47)        # listening before the deal
    deal_c = mc_.handle(_rdy0, member_id=15)
    hp_c = [r for r in deal_c if janwire.unpack(r)["opcode"] == M.MjHAIPAI][0]
    cp_c = [r for r in mc_.pending_for_member(47) if janwire.unpack(r)["opcode"] == M.MjHAIPAI]

    def _down(rec, seat):
        return [v for v in struct.unpack_from("<14H", rec, M.HAIPAI_HAND
                                              + seat * M.HAIPAI_SEAT_STRIDE)
                if v & M.FACE_DOWN]
    ok &= check(len(cp_c) == 1 and all(_down(hp_c, s) for s in (1, 2, 3))
                and not any(_down(cp_c[0], s) for s in range(4))
                and janwire.unpack(cp_c[0])["f13"] == janwire.unpack(hp_c)["f13"]
                and cp_c[0] != hp_c,
                "CONCEAL_DEAL on: the human's deal hides three hands, the "
                "spectator's copy shows all four, under the SAME seq")
    mc_.handle(_client_ack(tcg._last_for[0], 0), member_id=15)
    mc_.pending_for_member(47)
    tcg.begin_routing()
    rs_c = tcg.msg_alldata(label="probe resync")
    tcg.take_routes()
    cp_r = [r for r in mc_.pending_for_member(47) if janwire.unpack(r)["opcode"] == M.MjALLDATA]

    def _blank(rec, seat):
        return not any(rec[M.ALLDATA_HAND + seat * M.ALLDATA_HAND_STRIDE + i]
                       for i in range(13))
    ok &= check(len(cp_r) == 1 and cp_r[0] != rs_c
                and any(_blank(rs_c, s) for s in (1, 2, 3))
                and not any(_blank(cp_r[0], s) for s in range(4))
                and janwire.unpack(cp_r[0])["f13"] == janwire.unpack(rs_c)["f13"],
                "CONCEAL_RESYNC on: the human's MjALLDATA blanks opponents, the "
                "spectator's copy fills every seat, same seq")
    knobs.CONCEAL_DEAL = _old_cd
    knobs.CONCEAL_RESYNC = _old_cr
    if _old_seats_env is None:
        os.environ.pop("POL_JAN_SEATS", None)
    else:
        os.environ["POL_JAN_SEATS"] = _old_seats_env

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    return selftest(trace=a.trace)
