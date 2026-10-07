"""One inbound line to its answer: the game manager, the reserve and master
commands, the rest captured.
"""
import os
import struct
import janwire                                                  # noqa: E402
from .deps import jangame, janmsgs, janseats
from . import (builds, galley, notices, opcodes, opener, pushqueue, reservelimits, seating, social,
               wirelog)


# One manager for the whole process: a member's table has to survive across
# lines, and the auth band hands us one line at a time with no session object of
# our own. Keyed by member id, so two consoles get two tables.
GAMES = jangame.Manager() if jangame is not None else None
if GAMES is not None:
    # the web board's watch rule (defined below; resolved at call time)
    GAMES.watchable = lambda tid: galley.web_watchable(tid)
if jangame is not None:
    # Route jangame's human-readable per-move narration into this channel, so a
    # gameplay event shows up as BOTH the wire record and a plain-English move.
    jangame.TRACE = lambda m: wirelog.log(m)


def handle_line(line, peer="-", member_id=0):
    """Decode one line, and hand back anything queued for this member first.

    WARNING: THIS WRAPPER IS THE SECOND HUMAN'S ONLY WAY IN. The transport is
    reply-only: a record leaves on the socket of whoever just spoke, so a hand
    could only ever be played by the one player the server happened to be
    answering. Everything for another seat waits in `Table.outbox` and rides
    out on that member's next line -- and they always have one, because an
    in-game client re-sends its last ack about every 2 s and a client still in
    the room polls `<DR>` on the same cadence. Neither needs anything from us
    to keep talking, which is what makes a reply-only transport enough.

    Queued records go FIRST. They are older than whatever this line is about,
    and the client dedups on the sequence byte, so order is the whole contract.
    """
    out = _handle_line(line, peer=peer, member_id=member_id)
    queued = take_pending(member_id, peer=peer)
    if not queued:
        return out
    if out is None:
        rest = []
    elif isinstance(out, (list, tuple)):
        rest = list(out)
    else:
        rest = [out]
    wirelog.log("%s   -> %d queued record(s) for member %s ride this line"
        % (peer, len(queued), member_id))
    return queued + rest


def take_pending(member_id, peer="-", limit=None):
    """Encoded records waiting for this member, and clear them.

    WARNING: EXPOSED BECAUSE THE GAME BAND IS NOT THE ONLY TICK. A player still
    sitting in the room does NOT send game-band lines -- their ~2s heartbeat is
    the class-L `<DR>` poll, which never reaches `handle_line` at all. Queueing
    the game-start trio for them and draining it only here meant it was never
    delivered: measured live 2026-09-04, "queued the game-start trio for member
    6 at seat 1" with no matching delivery, and P2 sat in the lobby watching a
    table it was seated at start without it. `responders` drains this from the
    `<DR>` path too.
    """
    if not member_id:
        return []
    out = pushqueue._take_pushed(member_id, limit=limit)
    if limit is not None:
        limit -= len(out)
        if limit <= 0:
            return out
    if GAMES is None:
        return out
    try:
        out += [janwire.encode(r)
                for r in builds.to_peer_build(
                    GAMES.pending_for_member(member_id, limit=limit),
                    member_id=member_id, peer=peer)]
    except Exception as e:                                      # never fatal
        wirelog.log("%s   pending drain raised: %s -- dropping nothing, retrying on "
            "the next line" % (peer, e))
    return out


def _handle_line(line, peer="-", member_id=0):
    """Decode one line and return the reply -- None, one line, or a LIST.

    A list is not an optimisation: once a hand is running, one inbound record
    routinely produces several outbound ones (the three non-human seats play in
    between), so the caller must be able to send more than one. `_game_notice_
    reply` in responders.py already does this for Tetra Master; the Janhourou
    branch was extended to match.

    Silence is still the default for anything we cannot yet speak: a wrong
    answer teaches us nothing, whereas a logged unknown is a lead.
    """
    tag = line[:1]
    if tag == b"A":
        try:
            h = janwire.parse_chat_line(line)
        except ValueError as e:
            wirelog.log("%s   malformed 'A' line (%s): %r" % (peer, e, line[:120]))
            return None
        wirelog.log("%s   <- %s  text=%r extra=%r"
            % (peer, opcodes.opname(h["opcode"]), h["text"], h["extra"]))
        social._relay_chat(line, h["opcode"], member_id, peer)
        return None
    if tag != b"B":
        wirelog.log("%s   UNKNOWN class tag %r -- not one of ours; %d bytes:\n%s"
            % (peer, tag, len(line), wirelog.hexdump(line)))
        return None

    rec = janwire.decode(line)
    # 24, not 32: the client's own ack sender (console__00284380) declares 0x18
    # and sends a header only, so every MjHAIPAIACK / MjSEISANACK / MjBYE is a
    # 24-byte record. A 32-byte floor here silently swallowed the entire ack
    # half of the protocol -- which nothing had noticed, because no client had
    # ever been given a message worth acking.
    if len(rec) < janwire.HEADER_LEN:
        wirelog.log("%s   short record (%d bytes) from %r" % (peer, len(rec), line[:80]))
        return None
    h = janwire.unpack(rec)
    wirelog.log("%s   <- %s" % (peer, wirelog.describe(rec)))
    if h["body"]:
        wirelog.log("%s      body %d bytes:\n%s" % (peer, len(h["body"]), wirelog.hexdump(h["body"])))

    if janseats is not None and janseats.enabled() and member_id:
        # Any line from a member is proof they are alive; a seat whose member
        # goes silent past POL_JAN_SEAT_TTL_S expires (crash/power-off safety,
        # the lesson of tm-reservation-heartbeat -- Jan's client sends no
        # explicit heartbeat we have measured, so "still talking" is the lease).
        janseats.touch(member_id)
        # WARNING: AND THE COUNTERPART `set_in_play` NEVER HAD. Nothing in the tree
        # called `clear_in_play` -- it had one caller, its own selftest -- so a
        # table went "in play" at the first Start Game and STAYED there. The
        # TTL cannot save it either, and deliberately so: `touch` above renews
        # the in-play mark on every line precisely so a long hanchan does not
        # time out and let somebody reserve into a running game. That makes the
        # renewal correct and the missing clear the whole bug.
        #
        # Ask the thing that actually knows. `state == "over"` is the hanchan
        # ending (jangame sets it with the final MjGAMEEND) -- NOT
        # `!= "playing"`, which is also true in the gap between MjGAMESTART and
        # the first MjREADY and would wipe the mark we had just set.
        #
        # Reported live 2026-09-04: "table 1 already shows as being in-game
        # with one player" on walking into the room.
        if GAMES is not None:
            _t = GAMES.table_for(member_id, create=False)
            if _t is not None and getattr(_t, "state", None) == "over":
                _tid = janseats.table_of(member_id)
                if _tid and janseats.is_in_play(_tid):
                    janseats.clear_in_play(_tid)
                    wirelog.log("%s      the hanchan is over -- table %s is joinable "
                        "again" % (peer, _tid))

    if h["opcode"] == janwire.MjTGMPING:
        reply = janwire.pong_for(rec)
        wirelog.log("%s   -> %s" % (peer, wirelog.describe(reply)))
        return janwire.encode(reply)

    if h["opcode"] == notices.MjMEMBERLISTREQ and notices.MEMBERLIST_ENABLE:
        # MjMEMBERLISTREQ (5) -> MjNOTICEMEMBER (14). Seen live 2026-08-13, sent
        # just before the personal-rankings request and unanswered, so it is a
        # candidate prerequisite for that screen.
        #
        # The pairing is the notice dispatcher's, not a guess: 0x002c6384 maps
        # opcode 14 to bit 1 = TGM_NOTICE_MEMBER_LIST, and the gate that drains
        # notices (0x002c6374, labelled "Receive Notice") reads **sub-code 0** --
        # which is exactly what member_list_notice() already emits. Its field
        # offsets were measured off the client's own loads at 0x002c63f0.
        #
        # Four ids, for the same reason the game-start notice sends four: the
        # client's player count is the number of NON-ZERO ids here. +0x50 is
        # Ready/Away per seat (ReturnRoom.c:343/358) -- every seat of a full
        # table is occupied, so all four are 1 -- and +0x54 the MASTER's seat
        # (mahdisp.c:57). The four floats at +0x20 are still unread.
        master_seat = 0
        if GAMES is not None:
            who = seating.display_name(member_id, peer)
            _lobby, _members = seating._lobby_seating(member_id)
            if _members:
                # LOOK, do not rebuild: this panel opens while a hand may be
                # running, and `reset=True` would clear the game.
                table = GAMES.table_for_lobby(_lobby, _members, who, reset=False)
                master_seat = social._master_seat_of(_lobby)
                wirelog.log("%s      member list for lobby table %s: %s"
                    % (peer, _lobby,
                       ", ".join("%d=%s" % (st, nm or m)
                                 for m, st, nm, _p in _members)))
            elif (hasattr(GAMES, "spectator_table")
                  and GAMES.spectator_table(member_id) is not None):
                # A SPECTATOR asking (its client only builds this from the
                # room loop, roommain.c:150, but the Kansen menu thread is
                # not decompiled): answer from the table it WATCHES and never
                # seat it there -- the fallback below would put it at seat 0
                # of a running game.
                table = GAMES.spectator_table(member_id)
                master_seat = social._master_seat_of(getattr(table, "lobby_key", 0))
            else:
                table = GAMES.table_for(member_id, who)
                table.seat_player(h["payload"], who, seat=0, member=member_id)
                table.fill_with_bots()
            ids = table.member_ids()
        else:
            table, ids = None, (h["payload"], 0, 0, 0)
        # `table` -- NOT omitted. This call site used to pass ids only, so every
        # seat took the no-name path however well the seating had worked.
        # +0x54 is the MASTER's seat (mahdisp.c:57, finding 25) -- it was the
        # recipient's own seat, which promoted every guest who opened the
        # panel. +0x50 is Ready per seat: every seat here is occupied.
        reply = notices.member_list_notice(ids=ids, a=notices.RESULT_OK, b=0, seat=master_seat,
                                   flags=(1, 1, 1, 1),
                                   tail=seating._member_names(ids, table))
        wirelog.log("%s   -> %s  [%dB, %d members]  %s"
            % (peer, wirelog.describe(reply), len(reply), len([s for s in ids if s]),
               notices._seat_summary(ids, table)))
        return janwire.encode(reply)

    if h["opcode"] == opener.MjCHECKSAVEDATA:
        reply = opener.checksavedata_ack(rec)
        if reply is None:
            wirelog.log("%s      MjCHECKSAVEDATA received but POL_JAN_SAVEDATA=0 -- silent"
                % peer)
            return None
        wirelog.log("%s   -> %s  [+0x18=%d -> %s]"
            % (peer, wirelog.describe(reply), reply[0x18],
               "IM UD Check Success" if reply[0x18] == 1 else
               "IM UD Check Failed -- the client will RESEND"))
        return janwire.encode(reply)

    if h["opcode"] == opener.EmISEVENT:
        reply = opener.emisevent_ack(rec)
        if reply is None:
            wirelog.log("%s      EmISEVENT received but POL_JAN_ISEVENT=0 -- silent" % peer)
            return None
        wirelog.log("%s   -> %s  [%dB, sub=%d, +0x94=%d -> %s]"
            % (peer, wirelog.describe(reply), len(reply), opener.ISEVENT_SUB, reply[0x94],
               "event tab ON" if reply[0x94] == 1 else "no event"))
        return janwire.encode(reply)

    if h["opcode"] == opener.MjGETLNDV:
        reply = opener.getlndv_ack(rec)
        if reply is None:
            wirelog.log("%s      MjGETLNDV received but POL_JAN_LNDV=0 -- staying silent"
                % peer)
            return None
        gm = struct.unpack_from("<I", reply, 0x30)[0] if len(reply) >= 0x34 else 0
        wirelog.log("%s   -> %s  [%dB, sub=%d (gate drains on %d, buckets to fifo %d), "
            "GM version +0x30=%#010x%s]"
            % (peer, wirelog.describe(reply), len(reply), opener.LNDV_SUB, opener.LNDV_DRAIN_SUB,
               opcodes.queue_for_sub(opener.LNDV_SUB), gm,
               "" if gm == opener.GM_VERSION else "  *** != 0x20020227, the client will "
               "log 'ERROR:GM Version' and quit ***"))
        return janwire.encode(reply)

    # --- the in-game conversation ------------------------------------------
    # Everything from MjREADY to MjGAMEEND belongs to the game manager. It is
    # the only branch here that keeps state between lines, and the only one that
    # can answer with several records at once.
    if GAMES is not None and h["opcode"] in jangame.INGAME_OPCODES:
        try:
            out = GAMES.handle(rec, member_id=member_id,
                               nick=seating.display_name(member_id, peer))
        except Exception as e:
            wirelog.log("%s   jangame raised on %s: %s -- staying silent rather than "
                "guessing" % (peer, opcodes.opname(h["opcode"]), e))
            return None
        if not out:
            wirelog.log("%s      %s: nothing to send (the table is waiting on another "
                "seat)" % (peer, opcodes.opname(h["opcode"])))
            return None
        for r in out:
            wirelog.log("%s   -> %s" % (peer, wirelog.describe(r)))
        return [janwire.encode(r) for r in
                builds.to_peer_build(out, member_id=member_id, peer=peer)]

    # --- reserve / cancel, against the SEAT STORE (2026-09-02) --------------
    # WARNING: THE TABLE ID IS `id8` (+0x08), NOT `payload` (+0x18). Corrected
    # 2026-09-03 from a decoded live record -- this one field is why every
    # reservation went into a table nobody was looking at.
    #
    #   objstrings__002ca990(rec_src, DAT_00445960, table_id, label) builds
    #   MjPLAYREQ as: +0x08 = its THIRD argument, +0x18 = *rec_src. The call
    #   site (TableMenuPopup__0034f2f0) passes ctx+0x1d8 as rec_src and
    #   *(u64*)(ctx+0xa0) as the third argument, and ctx+0xa0 is the table id
    #   -- TableMenu__0034e9f0 stores exactly that word into _DAT_00446510,
    #   the my-table global the menu compares against. ctx+0x1d8 is the
    #   PLAYER: it lands in _DAT_004464e0, which the client prints as
    #   "PolID: %d". objstrings__002caab0 (MjPLAYCANCEL) has the same shape.
    #
    # Live proof (member 6 reserving table 1, 2026-09-03T22:35:34):
    #   id8=0000000000000001            <- our authored #MJS0T001
    #   payload=5b01e2bb73b3b60b        <- the <PD> PolID of LaptopTest2
    # (a <PD> PolID is the member's nick fold XOR mgkey's K("MJS"), the same
    # per-title id key Tetra Master uses with "TM0")
    # The old code keyed the store off `payload`, so every client wrote its own
    # per-player record and ptl_blob's seat_count(1..4) always read 0: the row
    # never showed an occupant, "View table members" stayed greyed (its gate is
    # the record's +0x0c seat count), and two humans could never share a table.
    # WARNING: That last symptom is what jan-table-id-is-per-session recorded as an
    # id-space mismatch. There is no mismatch -- the ids it compared were the
    # two players' PolIDs, read out of the wrong field.
    # The extra byte rec[0x19] goes back as two nibbles (the client splits it:
    # DAT_004460e9 = extra >> 4, DAT_004460e8 = extra & 0xf): low = the seat we
    # granted (measured -- it becomes the seat stamped on every in-game
    # message), high = the master's seat (inference from lmenu's
    # save+0x3ca == save+0x3c9 master test). A master at seat 0 yields extra=0,
    # byte-identical to every reply the constant-result era sent live.
    if (h["opcode"] in (opcodes.MjPLAYREQ, opcodes.MjPLAYCANCEL) and janseats is not None
            and janseats.enabled() and member_id):
        # THE ROOM (finding 30): `id8` is the 16-bit row id every room's
        # PTL carries; the asker's registry room qualifies it, so Room 1-1
        # table 1 and Room 2-3 table 1 are two seat sets. See `_table_for`.
        tid = seating._table_for(member_id, h["id8"])
        if h["opcode"] == opcodes.MjPLAYREQ:
            # `h["payload"]` is this client's PolID (+0x18) -- the id it
            # stamps on its own messages and the id MjNOTICEMEMBER's four seat
            # slots carry. The seat store keeps it so the GAME can seat this
            # player later without having to see another message from them.
            # +0x13 is the reserve-family tag a later kick must echo, and
            # +0x42 the voice bank the table-info blob serves for the seat.
            voice = (struct.unpack_from("<H", rec, opcodes.PLAYREQ_VOICE_OFF)[0]
                     if len(rec) >= opcodes.PLAYREQ_VOICE_OFF + 2 else None)
            face = (struct.unpack_from("<H", rec, opcodes.PLAYREQ_FACE_OFF)[0]
                    if len(rec) >= opcodes.PLAYREQ_FACE_OFF + 2 else None)
            # ENTRY LIMITS FIRST (opt-in, see `reserve_limits_unmet`): a refusal
            # must not move the member off a seat they hold elsewhere.
            unmet = reservelimits.reserve_limits_unmet(member_id, tid) if reservelimits._reserve_limits_on() else 0
            if unmet:
                janseats.note_reserve_tag(member_id, h["f13"])
                result, seat, extra = reservelimits.RESERVE_LIMIT_RESULT, 0, unmet
                wirelog.log("%s   member %s misses %s's entry limits (bits 0x%02X) -> "
                    "result 10" % (peer, member_id, seating._tl(tid), unmet))
            else:
                result, seat, mseat = janseats.reserve(
                    tid, member_id, seating.display_name(member_id, peer),
                    polid=h["payload"], tag=h["f13"], voice=voice,
                                    face=face)
                extra = ((mseat & 0xF) << 4) | (seat & 0xF)
        else:
            janseats.note_reserve_tag(member_id, h["f13"])
            result, seat, extra = janseats.cancel(tid, member_id), 0, 0
        reply = opcodes.ack_for(rec, result=result, extra=extra)
        r = janwire.unpack(reply)
        wirelog.log("%s   -> %s  result=%d/%s  [%s: %s]"
            % (peer, wirelog.describe(reply), reply[0x18],
               opcodes.result_name(r["opcode"], reply[0x18]), seating._tl(tid),
               janseats.snapshot().get(str(int(tid)), (0, 0, False))))
        out = [janwire.encode(reply)]
        # A cancel by the master promoted somebody: tell the table.
        social.notify_master_changes(peer=peer)
        return out

    if (h["opcode"] in opcodes.RESERVE_TAG_OPCODES and janseats is not None
            and janseats.enabled() and member_id):
        janseats.note_reserve_tag(member_id, h["f13"])

    if (h["opcode"] in (opcodes.MjGALLEYREQ, opcodes.MjGALLEYLEAVEREQ)
            and janseats is not None and member_id):
        return galley._galley(rec, h, member_id, peer)

    if (h["opcode"] in (opcodes.MjLEAVEROOM, opcodes.MjLEAVECONTENTS, opcodes.MjLEAVEGAME)
            and janseats is not None and janseats.enabled() and member_id):
        # MjLEAVEROOM (22) / MjLEAVECONTENTS (24) / MjLEAVEGAME (29): the
        # member is walking away from wherever their seat was. 29's ACK still
        # comes from the generic table below. 22 and 24 are FIRE-AND-FORGET
        # -- roommain.c:146-160 sends MjLEAVEROOM (tagged off a different
        # counter, `DAT_00449050|0x10`, so no wait-state can even match it)
        # and falls straight into sqMgCpExitRoom(). Silence is the correct
        # answer, and it is now a chosen silence rather than "no responder".
        freed = janseats.release_member(member_id, why=opcodes.opname(h["opcode"]))
        wirelog.log("%s      released member %s's seat(s) on %s%s"
            % (peer, member_id, opcodes.opname(h["opcode"]),
               " (table %d)" % freed if freed else ""))
        # A spectator walking out of the room/content: its gallery membership
        # goes with it (the client sends no MjGALLEYLEAVEREQ on this path).
        if GAMES is not None and hasattr(GAMES, "forget_spectator"):
            if GAMES.forget_spectator(member_id, opcodes.opname(h["opcode"])) is not None:
                wirelog.log("%s      ...and its gallery membership" % peer)
        elif hasattr(janseats, "gallery_drop"):
            janseats.gallery_drop(member_id)
        if h["opcode"] != opcodes.MjLEAVEGAME:
            # A departing master promoted somebody: tell the survivors.
            social.notify_master_changes(peer=peer)
            return []

    if h["opcode"] == opcodes.MjENTERROOM:
        # MEASURED 2026-09-09. The post-RESERVE room entry: roommain.c:109
        # (`wrsCpTgmEnterRoom Run`) -> objstrings__002cae70, sent when the
        # client is back on the room screen of the table it just reserved
        # (TableMenuPopup case 1/2 = RESERVEACK 1 seated / 2 seated as master
        # set DAT_00445e30, and the table's room is the current room). Body:
        # +0x18 the table record's id (DAT_004464e0), +0x08 the row's +0x208
        # id, +0x13 = 0x10 | a 4-bit counter, sub 5. NOTHING drains a reply
        # -- the client goes straight on to MjMEMBERLISTREQ, which IS
        # answered -- and seating happened at MjPLAYREQ (janseats). So this
        # arm only names it instead of logging "no responder".
        wirelog.log("%s      MjENTERROOM: back in the room of table %016x (id8 %016x, "
            "member %s) -- fire-and-forget, nothing to answer"
            % (peer, h["payload"], h["id8"], member_id))
        return []

    if h["opcode"] == opcodes.MjMEMBERBANISH and janseats is not None and member_id:
        return social._kick(rec, h, member_id, peer)

    if h["opcode"] == opcodes.MjCHMASTER and janseats is not None and member_id:
        return social._decline_master(rec, h, member_id, peer)

    if (h["opcode"] in (opcodes.MjCHATMEMBERADD, opcodes.MjCHATMEMBERDEL, opcodes.MjCHATMEMBERACK,
                        opcodes.MjCHATINFO) and member_id):
        return social._chat_member(line, rec, h, member_id, peer)

    if h["opcode"] == opcodes.MjTBLCONFALL and member_id:
        # THE RULES, BEFORE THE ACK (finding 2): the master's 33 choices are
        # stored so the next `b/g/MJSTableInfoSub` fetch and the next
        # `table_for_lobby` see them. The ack itself is unchanged.
        social._store_rules(rec, h, member_id, peer)

    if (h["opcode"] == opcodes.MjENTERGAME and janseats is not None
            and janseats.enabled() and member_id):
        # Rejoin (finding 29): a member re-entering a table whose game is in
        # play is answered Success (below) and FLAGGED, so the game manager
        # can resync their seat on the next line -- `janseats.rejoin_pending`.
        _tid = seating._table_for(member_id, h["id8"])
        if _tid and janseats.is_in_play(_tid) and janseats.mark_rejoin(member_id):
            wirelog.log("%s      MjENTERGAME on a PLAYING table %s -- rejoin flagged "
                "for member %s (jangame resyncs on their next line)"
                % (peer, _tid, member_id))

    reply = opcodes.ack_for(rec)
    if reply is not None:
        r = janwire.unpack(reply)
        wirelog.log("%s   -> %s  result=%d/%s"
            % (peer, wirelog.describe(reply), reply[0x18],
               opcodes.result_name(r["opcode"], reply[0x18])))
        out = [janwire.encode(reply)]
        # MjGAMESTART: the master pressed Start Game, we said Success, and the
        # client parks in state 0x1f waiting to be TOLD the game is starting.
        # The two messages that carry it out of there are the member list and
        # the GAME_START notice (bit 8 of the notice dispatcher at 0x002c6384),
        # after which it runs sqMgCpEnterTable and the keyed
        # b/g/MJSTableInfoSub fetch, and then sends MjREADY.
        #
        # WARNING: INFERENCE, and the most load-bearing one in this file: nothing
        # measured states that these two follow the ACK, only that the client
        # cannot proceed without being told. If a live client ignores them,
        # POL_JAN_DRIVE=0 puts it back exactly as it was.
        if (h["opcode"] == opcodes.MjGAMESTART and janseats is not None
                and janseats.enabled()):
            # +0x08 of MjGAMESTART is the table id: malloc__002c5220 puts its
            # SECOND argument there, and TableMenu__0034e8d0 passes
            # _DAT_00446510 -- the my-table global. (+0x18 is *param_1 =
            # &DAT_004464e0, the PolID again, same as MjPLAYREQ.)
            # WARNING: MEASURED ZERO LIVE 2026-09-03: the global is only stored on the
            # reserve-ACK arm (TableMenuPopup 0x34f3a0), and a Start Game that
            # follows any other path carries 0. So fall back to the table this
            # member is actually seated at -- the seat store knows, and it is
            # the more trustworthy answer in every case. Room-qualified
            # (finding 30) by `_table_for`.
            tid = seating._table_for(member_id, h["id8"])
            if tid:
                janseats.set_in_play(tid, member_id)
        if (h["opcode"] == opcodes.MjGAMESTART and GAMES is not None
                and os.environ.get("POL_JAN_DRIVE", "1") == "1"):
            # FOUR NON-ZERO IDS, not one. The client counts the non-zero u64s at
            # +0x30 to get its seated-player count (malloc__002c67a0), so a table
            # of one human and three bots must announce four ids or the client
            # waits for players who are, as far as it knows, not there. Measured
            # after a live run sat on 「皆さんをお待ちしております」 with the
            # game-start pair otherwise accepted.
            who = seating.display_name(member_id, peer)
            # WARNING: SEAT THE TABLE THE LOBBY SAYS EXISTS. `table_for` keys by
            # MEMBER, so two humans at one table got two Tables and each played
            # three bots -- proven live 2026-09-04, when a Start Game at a
            # table holding two people announced `0='PS2Tester' 1='COM 1'
            # 2='COM 2' 3='COM 3'` and the other human was shown a table it
            # could no longer touch. The seat store is the authority on who is
            # there and which seat each of them was given.
            _lobby, _members = seating._lobby_seating(
                member_id, prefer=seating._table_for(member_id, h["id8"]))
            if _members:
                # WARNING: A SECOND START GAME MUST NOT RESTART A RUNNING ONE.
                # Both seated players can end up holding a master menu (the
                # client caches its master flag until the next save fetch --
                # `lmenu__002b2ab0` is the only writer), so two MjGAMESTARTs
                # for one table is reachable in practice, not just in theory.
                # `reset=True` would call reset_for_new_game() and wipe the
                # wall, the hands and every outbox out from under a hand that
                # is already being played. `table_for_lobby` returns a
                # "playing" table untouched when reset is False.
                _running = (GAMES.by_lobby.get(int(_lobby)) is not None
                            and getattr(GAMES.by_lobby.get(int(_lobby)),
                                        "state", None) == "playing")
                table = GAMES.table_for_lobby(_lobby, _members, who,
                                              reset=not _running)
                if _running:
                    wirelog.log("%s      table %s is ALREADY PLAYING -- re-driving the "
                        "notices for member %s rather than restarting the hand"
                        % (peer, _lobby, member_id))
                wirelog.log("%s      seating lobby table %s from the store: %s"
                    % (peer, _lobby,
                       ", ".join("%d=%s" % (st, nm or m)
                                 for m, st, nm, _p in _members)))
            else:
                table = GAMES.table_for(member_id, who)
                # Start Game is the explicit "new game" signal. Clear any stale
                # per-hand state first -- a prior hand that errored out leaves
                # the table stuck in state "playing", and then the MjREADY that
                # follows never re-deals (reset_for_new_game's banner).
                table.reset_for_new_game()
                table.seat_player(h["payload"], who, seat=0, member=member_id)
                table.fill_with_bots()
            seats = table.member_ids()
            # +0x54 = the MASTER's seat (finding 25); +0x50 = every seat
            # Ready, which a full four-seat game is. A game started through
            # the member fallback has one human at seat 0 = the master.
            notice = notices.member_list_notice(ids=seats, a=notices.RESULT_OK, b=0,
                                        seat=social._master_seat_of(_lobby) if _members else 0,
                                        flags=(1, 1, 1, 1),
                                        tail=seating._member_names(seats, table))
            # WARNING: AND THE ONE THAT ACTUALLY STARTS IT. Measured 2026-08-17 after a
            # live client accepted the pair above and then waited for ever:
            # TableSelectMain returns 1 (= enter the game) on DAT_004460f2, which
            # is set by notice bit 0x20 = MjNOTICEGAMESETUP (20).
            #
            # MjNOTICEGAMESTART (18) is NOT sent. In the 2002 build its flag
            # (DAT_004460f0) is never read, but the 2004 build's lobby notice
            # printer (0x003a80a0) answers bit 8 with two lines: "the table you
            # reserved has started its game" and then "Unfortunately, the
            # reservation was cancelled" (0x004b0390, 0x004b03d0). Its flag
            # DAT_004c42b0 is never read either, so the only thing 18 did for a
            # 2004 player was tell them their table was cancelled as they sat
            # down at it. SE presumably sent 18 to reservers who were NOT
            # pulled into the game.
            setup = janmsgs.notice_gamesetup(2)
            wirelog.log("%s   -> %s  [driving the table out of state 0x1f]  %s"
                % (peer, wirelog.describe(notice), notices._seat_summary(seats, table)))
            wirelog.log("%s   -> %s  [THE TRIGGER: bit 0x20 -> DAT_004460f2 -> "
                "TableSelectMain returns 1]" % (peer, wirelog.describe(setup)))
            # The 2004 build reads four seat names out of the setup notice
            # (janmsgs2004). The copies queued for the other seats below stay
            # in 2002 shape and are converted per recipient as they drain.
            out += [janwire.encode(_r) for _r in
                    builds.to_peer_build([notice, setup],
                                  member_id=member_id, peer=peer)]
            # EVERY OTHER SEATED HUMAN NEEDS THIS EXACT PAIR, or their client
            # sits in the lobby watching a table it is seated at go "in play"
            # without it -- which is what was seen live as "being kicked".
            # They ride out on that member's next line (~2 s).
            for _m, _st, _nm, _pid in _members:
                if _m == member_id:
                    continue
                for _r in (notice, setup):
                    table.queue_for(_st, _r)
                wirelog.log("%s      queued the game-start pair for member %s at seat "
                    "%d" % (peer, _m, _st))
        return out

    wirelog.log("%s      (no responder for %s yet -- captured, not answered)"
        % (peer, opcodes.opname(h["opcode"])))
    return None
