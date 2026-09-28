"""The offline selftest (loopback, no client) and the command line."""
import argparse
import os
import socket
import struct
import threading
import janwire                                                  # noqa: E402
from .deps import jangame, janmsgs, janrules, janseats
from . import dispatch, galley, notices, opcodes, opener, pushqueue, seating, server, wirelog


def selftest():
    """Stand the server up on a loopback port and complete a ping exchange."""
    ok = True
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address

    ping = janwire.pack(opcode=janwire.MjTGMPING,
                        src=janwire.DST_REPLY & 0xFF,
                        dst=janwire.DST_SERVER & 0xFF,
                        payload=0x0123456789ABCDEF)
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(janwire.encode(ping) + b"\r\n")
        s.settimeout(5)
        got = s.recv(4096).strip()

    print("sent  %s" % janwire.encode(ping).decode("latin1"))
    print("got   %s" % got.decode("latin1"))
    if not got.startswith(b"B"):
        print("FAIL: reply is not a 'B' line"); ok = False
    else:
        h = janwire.unpack(janwire.decode(got))
        if h["opcode"] != janwire.MjTGMPONG:
            print("FAIL: replied %s, expected MjTGMPONG" % opcodes.opname(h["opcode"])); ok = False
        if h["id8"] != 0x0123456789ABCDEF:
            print("FAIL: pong did not echo the ping's payload (%016x)" % h["id8"]); ok = False
        if (h["src"], h["dst"]) != (-3, -2):
            print("FAIL: pong src/dst = %d/%d, expected -3/-2" % (h["src"], h["dst"])); ok = False

    # Every request->ACK pair, against the gates the client actually applies.
    for req_op, want_ack in sorted(opcodes.ACK_FOR.items()):
        req = janwire.pack(opcode=req_op, f13=0x1D, src=1,
                           dst=janwire.DST_SERVER & 0xFF, payload=0xCAFE)
        ack = opcodes.ack_for(req)
        a = janwire.unpack(ack)
        why = "%s -> %s" % (opcodes.opname(req_op), opcodes.opname(want_ack))
        if a["opcode"] != want_ack:
            print("FAIL: %s produced %s" % (why, opcodes.opname(a["opcode"]))); ok = False
        if a["f13"] != 0x1D:
            print("FAIL: %s tag %#x, expected 0x1d echoed verbatim"
                  % (why, a["f13"])); ok = False
        # The rule is equality with the gate's drain sub-code -- NOT a queue
        # lookup. A record whose sub is 7 lands in TABLE[7] = 0 = the outbound
        # FIFO and is transmitted straight back to the server; that is a real
        # observed failure, so assert against it explicitly.
        if a["sub"] != opcodes.ACK_SUB:
            print("FAIL: %s sub %d, but the gate drains on sub %d"
                  % (why, a["sub"], opcodes.ACK_SUB)); ok = False
        if opcodes.queue_for_sub(a["sub"]) == 0:
            print("FAIL: %s sub %d buckets into the OUTBOUND fifo -- the client "
                  "will echo it back, not read it" % (why, a["sub"])); ok = False
        if opcodes.result_name(want_ack, ack[0x18]) == "?":
            print("FAIL: %s result byte %d is not in its ladder"
                  % (why, ack[0x18])); ok = False
        if dispatch.handle_line(janwire.encode(req)) is None:
            print("FAIL: %s went unanswered" % why); ok = False
    if opcodes.RESERVE_RESULTS[2] != 'ReserveMaster' or opcodes.TMCMD_RESULTS[3] != 'NoAuthority':
        print("FAIL: result ladders do not match the binary"); ok = False

    # MjNOTICEMEMBER: check the fields land where the client loads them
    ids = (0x1111111111111111, 0x2222222222222222, 0x3333333333333333, 0x4444444444444444)
    n = notices.member_list_notice(ids=ids, vals=(1.0, 2.0, 3.0, 4.0), flags=(9, 8, 7, 6),
                           a=0xAAAA, b=0xBBBB, seat=2, tail=b"#JAN0001")
    nh = janwire.unpack(n)
    if nh["opcode"] != notices.MjNOTICEMEMBER or nh["length"] != 0x95 or len(n) != 0x95:
        print("FAIL: notice header %r len %d" % (nh, len(n))); ok = False
    if nh["dst"] != -1:
        print("FAIL: a notice must go to the channel (dst -1), got %d" % nh["dst"]); ok = False
    if struct.unpack_from("<4Q", n, 0x30) != ids:
        print("FAIL: seat ids not at +0x30"); ok = False
    if struct.unpack_from("<4f", n, 0x20) != (1.0, 2.0, 3.0, 4.0):
        print("FAIL: floats not at +0x20"); ok = False
    if tuple(n[0x50:0x54]) != (9, 8, 7, 6) or n[0x54] != 2:
        print("FAIL: per-seat flags not at +0x50"); ok = False
    if n[0x55:0x5D] != b"#JAN0001":
        print("FAIL: tail block not at +0x55"); ok = False

    # THE NAME BLOCK. The bug this covers is not subtle and was live for days:
    # one of the two call sites passed no table, so every seat fell through to a
    # placeholder and the screen read PLAYER / CPU1 / CPU2 / CPU3.
    class _T(object):
        nicks = ["PS2Tester", None, None, None]
        bots = {1: object(), 2: object()}
    names = seating._member_names((1, 2, 3, 4), _T())
    slots = [names[i * notices.NAME_SLOT:(i + 1) * notices.NAME_SLOT].rstrip(bytes(1))
             for i in range(4)]
    if len(names) != 4 * notices.NAME_SLOT:
        print("FAIL: the name block is %d bytes, not 64" % len(names)); ok = False
    if slots[0] != b"PS2Tester":
        print("FAIL: a seated player's own name is %r" % slots[0]); ok = False
    if slots[1] != b"COM 1" or slots[2] != b"COM 2":
        print("FAIL: bot seats are %r/%r" % (slots[1], slots[2])); ok = False
    if slots[3] != b"":
        print("FAIL: an unknown seat must stay BLANK (it is the one signal that "
              "says 'no name', and a friendly constant hides it); got %r"
              % slots[3]); ok = False
    if seating._member_names((1, 2, 3, 4)) != bytes(4 * notices.NAME_SLOT):
        print("FAIL: with no table every slot must be blank, not a placeholder")
        ok = False
    if notices.NOTICE_BIT[notices.MjNOTICEMEMBER] != 1 or notices.NOTICE_BIT[21] != 64:
        print("FAIL: notice bit map does not match the binary"); ok = False

    # MjNOTICEBANISH: two u64s then two 16-byte names. WARNING: THE KICKED PolID IS
    # +0x20 -- lobbydsp.c:164 compares notice+0x20 against the recipient's
    # own PolID to decide whether to un-seat itself. +0x18 is never read.
    bn = notices.banish_notice(kicked=0xDEAD, by=0xBEEF, kicked_name=b"loser",
                       by_name=b"master")
    if len(bn) != 0x48 or janwire.unpack(bn)["length"] != 0x48:
        print("FAIL: banish length %d" % len(bn)); ok = False
    if struct.unpack_from("<Q", bn, 0x20)[0] != 0xDEAD:
        print("FAIL: the KICKED PolID must be at +0x20 (the one field the "
              "client compares against its own id)"); ok = False
    if struct.unpack_from("<Q", bn, 0x18)[0] != 0xBEEF:
        print("FAIL: the kicker's id goes at +0x18"); ok = False
    if bn[0x38:0x3D] != b"loser" or bn[0x28:0x2E] != b"master":
        print("FAIL: banish names not at +0x28 (by) / +0x38 (kicked)"); ok = False
    if len(bn[0x28:0x38]) != notices.NAME_LEN:
        print("FAIL: name field is not 16 bytes"); ok = False
    tn = notices.timeup_notice(90)
    if (janwire.unpack(tn)["opcode"] != opcodes.MjNOTICETIMEUPWARNING
            or struct.unpack_from("<I", tn, 0x20)[0] != 90 or tn[0x18] != 0):
        print("FAIL: MjNOTICETIMEUPWARNING's rest time is +0x20 (malloc.c:1357), "
              "not +0x18"); ok = False
    sq = notices.serverquit_notice()
    if janwire.unpack(sq)["opcode"] != opcodes.MjNOTICESERVERQUIT or len(sq) != 0x20:
        print("FAIL: MjNOTICESERVERQUIT is a bare 0x20 notice"); ok = False

    # --- MjGETLNDV, against the REAL captured request ------------------------
    # Not a synthesised record: this is the live line from the PS2 client, so
    # the responder is exercised on exactly the bytes it will meet.
    LIVE_GETLNDV = b"B^@@@@@@@@@BESut|KnLAVr@@P@C}\x7f`@ElSjAg\x7fWb@Ul"
    req = janwire.decode(LIVE_GETLNDV)
    rh = janwire.unpack(req)
    if rh["opcode"] != opener.MjGETLNDV or rh["length"] != 32:
        print("FAIL: captured opener is %s len %d, expected MjGETLNDV len 32"
              % (opcodes.opname(rh["opcode"]), rh["length"])); ok = False
    ack = opener.getlndv_ack(req)
    ah = janwire.unpack(ack)
    if ah["opcode"] != opener.MjGETLNDVACK:
        print("FAIL: opener answered with %s" % opcodes.opname(ah["opcode"])); ok = False
    if ah["f13"] != rh["f13"]:
        print("FAIL: LNDV ack did not echo the correlation tag verbatim"); ok = False
    # THE reason the first live attempt still black-screened: the ACK has to land
    # in queue 0, and rec[0x17] INDEXES the routing table rather than being the
    # queue. Assert the indirection, not the literal, so this cannot regress.
    if ah["sub"] != opener.LNDV_DRAIN_SUB:
        print("FAIL: LNDV ack sub %d, but the gate at 0x002ca684 drains on sub %d"
              % (ah["sub"], opener.LNDV_DRAIN_SUB)); ok = False
    if opcodes.queue_for_sub(ah["sub"]) == 0:
        print("FAIL: LNDV ack sub %d buckets into the OUTBOUND fifo -- this is "
              "the bug that made the client echo our ACK back" % ah["sub"])
        ok = False
    if ah["id8"] != rh["payload"]:
        print("FAIL: LNDV ack must address the id the request named"); ok = False
    if (ah["src"], ah["dst"]) != (-3, -2):
        print("FAIL: LNDV ack src/dst = %d/%d, expected -3/-2"
              % (ah["src"], ah["dst"])); ok = False
    # the body the gate copies out: six fields, highest byte +0x36
    want_len = 0x40 if opener.LNDV_DUAL else 0x38
    if len(ack) != want_len or ah["length"] != want_len:
        print("FAIL: LNDV ack is %d bytes / length field %d, expected %#x"
              % (len(ack), ah["length"], want_len)); ok = False
    # The 2004 build's version, past everything the 2002 build reads.
    if opener.LNDV_DUAL and struct.unpack_from("<I", ack, 0x38)[0] != 0x20020603:
        print("FAIL: +0x38 = %#x, but the 2004 client demands 0x20020603 "
              "(0x002e77fc)" % struct.unpack_from("<I", ack, 0x38)[0]); ok = False
    probe = opener.getlndv_ack(req, body=bytes(range(0x18, 0x38)))
    if struct.unpack_from("<Q", probe, 0x18)[0] != 0x1f1e1d1c1b1a1918:
        print("FAIL: body does not start at +0x18"); ok = False
    if probe[0x36] != 0x36:
        print("FAIL: body does not reach +0x36 (the last field the gate reads)")
        ok = False
    # The GM version at +0x30. Not cosmetic: a mismatch makes the client log
    # "ERROR:GM Version", return -5 and quit the game AFTER completing both
    # opener exchanges -- which reads like success right up until it isn't.
    if struct.unpack_from("<I", ack, 0x30)[0] != 0x20020227:
        print("FAIL: +0x30 = %#x, but the client demands 0x20020227 (0x002ca8e0)"
              % struct.unpack_from("<I", ack, 0x30)[0]); ok = False
    if len(janwire.encode(ack)) != 1 + janwire.enc_len(want_len):
        print("FAIL: the ack does not serialise to its own line length")
        ok = False
    # --- the EVENT flag, the one byte the client's Event tab is gated on ----
    _ev = janwire.pack(id8=0x11, length=40, opcode=opener.EmISEVENT, src=4,
                       dst=janwire.DST_REPLY & 0xFF, f16=3, sub=5)[:0x18]
    _ev += (0x2222).to_bytes(8, "little") + (6).to_bytes(4, "little") + bytes(4)
    _keep = {k: os.environ.get(k) for k in
             ("POL_JAN_EVENT_FORCE", "POL_JAN_EVENT_ID", "POL_JAN_EVENT_NAME",
              "POL_JAN_ISEVENT_FLAG")}
    try:
        for _k in _keep:
            os.environ.pop(_k, None)
        os.environ["POL_JAN_EVENT_FORCE"] = "0"
        _a = opener.emisevent_ack(_ev)
        if not (_a and len(_a) == 0x98 and _a[0x94] == 0 and _a[0x17] == 6):
            print("FAIL: with no event the ack is 0x98 bytes, sub 6, flag 0")
            ok = False
        os.environ["POL_JAN_EVENT_FORCE"] = "1"
        os.environ["POL_JAN_EVENT_ID"] = "9"
        os.environ["POL_JAN_EVENT_NAME"] = "Cup"
        _a = opener.emisevent_ack(_ev)
        if not (_a and _a[0x94] == 1
                and struct.unpack_from("<I", _a, 0x4C)[0] == 9
                and _a[0x54:0x57] == b"Cup" and _a[0x57] == 0):
            print("FAIL: a running event sets the flag, the id at +0x4C and "
                  "the NUL-terminated name at +0x54")
            ok = False
        # POL_JAN_ISEVENT_FLAG is read at import, like every other knob here,
        # so the override is the module constant.
        _was = opener.ISEVENT_FLAG
        try:
            opener.ISEVENT_FLAG = 2
            if opener.emisevent_ack(_ev)[0x94] != 2:
                print("FAIL: POL_JAN_ISEVENT_FLAG must still override the store")
                ok = False
        finally:
            opener.ISEVENT_FLAG = _was
    finally:
        for _k, _v in _keep.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # --- MjCHECKSAVEDATA, against the REAL captured request ------------------
    # The second opener message, captured 2026-08-12 once the LNDV handshake
    # completed. Same treatment: drive the responder with the client's own bytes.
    LIVE_SAVEDATA = (b"BnP@@@@@@@@@@@@@@@@@@@C`@PpC}\x7f`@E@@@@@@@@@@"
                     b"BqNhF_}^HAVp@@@@@@@@@@@@@@@@@@@@@")
    sreq = janwire.decode(LIVE_SAVEDATA)
    sh = janwire.unpack(sreq)
    if sh["opcode"] != opener.MjCHECKSAVEDATA:
        print("FAIL: captured second message is %s, expected MjCHECKSAVEDATA"
              % opcodes.opname(sh["opcode"])); ok = False
    sack = opener.checksavedata_ack(sreq)
    sa = janwire.unpack(sack)
    if sa["opcode"] != opener.MjCHECKSAVEDATAACK:
        print("FAIL: answered with %s" % opcodes.opname(sa["opcode"])); ok = False
    if sa["sub"] != opener.SAVEDATA_DRAIN_SUB:
        print("FAIL: savedata ack sub %d, gate drains on %d"
              % (sa["sub"], opener.SAVEDATA_DRAIN_SUB)); ok = False
    if opcodes.queue_for_sub(sa["sub"]) == 0:
        print("FAIL: savedata ack buckets into the OUTBOUND fifo"); ok = False
    if sack[0x18] != 1:
        print("FAIL: savedata ack +0x18 = %d; anything but 1 makes the client "
              "resend forever (0x002ca8c0)" % sack[0x18]); ok = False
    if dispatch.handle_line(LIVE_SAVEDATA) is None:
        print("FAIL: the captured MjCHECKSAVEDATA went unanswered"); ok = False

    if dispatch.handle_line(LIVE_GETLNDV) is None:
        print("FAIL: the captured opener went unanswered -- this is the black "
              "screen"); ok = False

    # an opcode we deliberately do not answer must stay silent, not guess
    if dispatch.handle_line(janwire.encode(janwire.pack(opcode=janwire.MjISAY + 34))) is not None:
        print("FAIL: answered a message we have not decoded"); ok = False

    # 72 from the client's own name table + 0x48, the undocumented chat INFO
    # line sqFileAccess sends and receives.
    # ... + 73..100, the 2004 build's additions.
    if len(opcodes.OPCODES) != 101 or opcodes.opname(0x48) != "MjCHATINFO" or opcodes.opname(93) != "EmISEVENTACK":
        print("FAIL: opcode table is %d entries, expected 101 (0x48 = MjCHATINFO)"
              % len(opcodes.OPCODES)); ok = False
    if opcodes.ACK_FOR[opcodes.MjMEMBERBANISH] != opcodes.MjRESERVEACK:
        print("FAIL: a kick must be answered with MjRESERVEACK -- the kicker "
              "polls wrsCpTableReserveCheck (TableMemberBanish.c:153)"); ok = False

    # --- the seat store, driven through handle_line like the auth band does --
    if janseats is not None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            os.environ["POL_JAN_SEATS"] = "1"
            os.environ["POL_JAN_SEATS_FILE"] = os.path.join(td, "seats.json")
            janseats._TABLES.clear()
            janseats._ADOPTED[0] = True
            janseats._OWNER[0] = True

            # WARNING: THE TABLE GOES IN `id8` (+0x08) AND THE PolID IN `payload`
            # (+0x18) -- that is the client's layout, and building it the other
            # way round is precisely the bug this exercises. `polid` defaults to
            # a value that is NOT a table id, so anything reading the wrong
            # field lands in a record no seat_count() ever asks about.
            def _ask(op, table, member, f13=0x11, polid=None):
                req = janwire.pack(opcode=op, f13=f13, src=1,
                                   dst=janwire.DST_SERVER & 0xFF, id8=table,
                                   payload=(0x5B01E2BB73B3B60B + member
                                            if polid is None else polid))
                out = dispatch.handle_line(janwire.encode(req), member_id=member)
                if isinstance(out, (list, tuple)):
                    out = out[0] if out else None
                return janwire.decode(out) if isinstance(out, bytes) else None

            a = _ask(opcodes.MjPLAYREQ, 1, 8)
            if a is None or a[0x18] != 2 or a[0x19] != 0:
                print("FAIL: first reserver must get result 2 extra 0 (the "
                      "master reply confirmed live), got %r/%r"
                      % (a and a[0x18], a and a[0x19])); ok = False
            b = _ask(opcodes.MjPLAYREQ, 1, 16)
            if b is None or b[0x18] != 1 or b[0x19] != 0x01:
                print("FAIL: second reserver must get result 1 (guest) with "
                      "seat 1 in the low nibble, got %r/%r"
                      % (b and b[0x18], b and b[0x19])); ok = False
            c = _ask(opcodes.MjPLAYCANCEL, 1, 16)
            if c is None or c[0x18] != 5:
                print("FAIL: cancel of a held seat must answer 5 "
                      "(ReserveCancel -- 'Reserve cansel Complite'), got %r"
                      % (c and c[0x18])); ok = False
            d = _ask(opcodes.MjPLAYCANCEL, 1, 16)
            if d is None or d[0x18] != 6:
                print("FAIL: cancel with no seat must answer 6 "
                      "(ReserveCancelError), got %r" % (d and d[0x18])); ok = False
            # WARNING: THE REGRESSION GUARD. A reservation must land on the table
            # the PTL builder will ask about (1..4) and nowhere else -- reading
            # the PolID as the table is invisible on the wire (the client still
            # gets its ACK) and shows up only here.
            if janseats.seat_count(1) != 1:
                print("FAIL: table 1 holds %d seats, expected 1 -- the reserve "
                      "was keyed off the wrong field again"
                      % janseats.seat_count(1)); ok = False
            if list(janseats.snapshot()) != ["1"]:
                print("FAIL: the seat store grew tables %r; only '1' was ever "
                      "reserved" % list(janseats.snapshot())); ok = False
            if janseats.table_of(8) != 1:
                print("FAIL: table_of(8) = %r, expected 1 (the MjGAMESTART "
                      "fallback)" % janseats.table_of(8)); ok = False

            # WARNING: THE MEMBER LIST MUST ANSWER FROM THE SEAT STORE. It did not
            # until 2026-09-04: `MjGAMESTART` was taught the store and this was
            # not, so "View table members" went on drawing ONE human and three
            # COMs at a table the server knew held two. The live report was
            # simply "no change".
            if notices.MEMBERLIST_ENABLE and dispatch.GAMES is not None:
                # Put a second body back at the table: the cancels above left
                # member 8 alone, and one human is the case that always worked.
                _ask(opcodes.MjPLAYREQ, 1, 16)
                _req = janwire.pack(opcode=notices.MjMEMBERLISTREQ, f13=9, src=1,
                                    dst=janwire.DST_SERVER & 0xFF,
                                    id8=1, payload=0x5B01E2BB73B3B60B)
                _rp = dispatch.handle_line(janwire.encode(_req), member_id=8)
                if isinstance(_rp, (list, tuple)):
                    _rp = _rp[-1] if _rp else None
                _rp = janwire.decode(_rp) if isinstance(_rp, bytes) else None
                if _rp is None or len(_rp) < 0x95:
                    print("FAIL: MjMEMBERLISTREQ went unanswered"); ok = False
                else:
                    _ids = [struct.unpack_from("<Q", _rp, 0x30 + 8 * i)[0]
                            for i in range(4)]
                    _nm = [_rp[0x55 + i * notices.NAME_SLOT:
                               0x55 + (i + 1) * notices.NAME_SLOT].split(b"\0", 1)[0]
                           for i in range(4)]
                    # members 8 and 16 hold seats 0 and 1 at table 1 by now
                    # Assert on the IDS, not the names: `display_name` reads
                    # the accounts DB and there is none in a selftest, so both
                    # humans legitimately draw blank here.
                    _want = {0x5B01E2BB73B3B60B + 8, 0x5B01E2BB73B3B60B + 16}
                    if not _want.issubset(set(_ids)):
                        print("FAIL: the member list carries %r -- both seated "
                              "humans' PolIDs must be in it, not one human and "
                              "three COMs"
                              % (["%016x" % i for i in _ids],)); ok = False
                    _coms = [n for n in _nm if n.startswith(b"COM")]
                    if len(_coms) != 2:
                        print("FAIL: %r -- exactly two seats are bots" % (_nm,))
                        ok = False
                    if len(set(_ids)) != 4 or not all(_ids):
                        print("FAIL: seat ids %r must be four distinct "
                              "non-zero values" % (["%016x" % i for i in _ids],))
                        ok = False
                    # +0x54 is the MASTER's seat (member 8 reserved first)
                    if _rp[0x54] != 0:
                        print("FAIL: the master sits at seat 0, +0x54 says %d"
                              % _rp[0x54]); ok = False
                    # ...and for the GUEST it is STILL the master's seat, not
                    # the guest's own (finding 25 -- the old code sent the
                    # recipient's seat, which is the change-master trigger).
                    _rp2 = dispatch.handle_line(janwire.encode(_req), member_id=16)
                    if isinstance(_rp2, (list, tuple)):
                        _rp2 = _rp2[-1] if _rp2 else None
                    _rp2 = janwire.decode(_rp2) if isinstance(_rp2, bytes) else None
                    if _rp2 is None or _rp2[0x54] != 0:
                        print("FAIL: the guest's copy must carry the MASTER's "
                              "seat 0 at +0x54, got %r"
                              % (_rp2 and _rp2[0x54])); ok = False

            # without a member id the old constant behaviour must be untouched
            e = _ask(opcodes.MjPLAYCANCEL, 1, 0)
            if e is None or e[0x18] != opcodes.RESERVE_RESULT:
                print("FAIL: member_id 0 must keep the pre-store behaviour")
                ok = False

            # --- THE TABLE-SOCIAL LAYER (2026-09-04) -----------------------
            # members 8 (master, seat 0) and 16 (guest, seat 1) sit at table 1.
            pushqueue._PUSH.clear()
            janseats.take_master_changes()

            # a full table is refused with 8, not 10
            _ask(opcodes.MjPLAYREQ, 1, 21); _ask(opcodes.MjPLAYREQ, 1, 22)
            f = _ask(opcodes.MjPLAYREQ, 1, 99)
            if f is None or f[0x18] != 8:
                print("FAIL: a full table must answer 8 ('the table status "
                      "changed'), got %r" % (f and f[0x18])); ok = False
            _ask(opcodes.MjPLAYCANCEL, 1, 21); _ask(opcodes.MjPLAYCANCEL, 1, 22)
            pushqueue._PUSH.clear()

            # the reserve tag is remembered (the kick reply carries it)
            _ask(opcodes.MjPLAYREQ, 1, 8, f13=0x07)
            if janseats.reserve_tag(8) != 7:
                print("FAIL: MjPLAYREQ's +0x13 must be remembered as the "
                      "reserve tag, got %r" % janseats.reserve_tag(8)); ok = False

            # MjTBLCONFALL: the rules are STORED before the ack
            if janrules is not None:
                os.environ["POL_JAN_RULES_FILE"] = os.path.join(td, "rules.json")
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = True
                janrules._OWNER[0] = True
                _cf = bytearray(janwire.pack(opcode=opcodes.MjTBLCONFALL, f13=0x21, src=1,
                                             dst=janwire.DST_SERVER & 0xFF, id8=1,
                                             payload=0x5B01E2BB73B3B60B + 8,
                                             length=0x70))
                _cf += bytes(0x70 - len(_cf))
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 4
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("GENTEN")] = 5
                _cr = dispatch.handle_line(janwire.encode(bytes(_cf)), member_id=8)
                _cr = janwire.decode(_cr[0] if isinstance(_cr, list) else _cr)
                if janwire.unpack(_cr)["opcode"] != opcodes.MjMASTERCMDACK or _cr[0x13] != 0x21:
                    print("FAIL: MjTBLCONFALL must still be acked with "
                          "MjMASTERCMDACK, tag echoed"); ok = False
                if janrules.values_for(1)["UMA"] != 4 or janrules.values_for(1)["GENTEN"] != 5:
                    print("FAIL: the master's rules were not stored: %r"
                          % janrules.values_for(1)); ok = False
                # a guest's MjTBLCONFALL is acked but NOT stored
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 1
                dispatch.handle_line(janwire.encode(bytes(_cf)), member_id=16)
                if janrules.values_for(1)["UMA"] != 4:
                    print("FAIL: a guest must not be able to change the rules")
                    ok = False
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = False
                janrules._OWNER[0] = False
                janrules._CACHE["mtime"] = -1.0
                os.environ.pop("POL_JAN_RULES_FILE", None)

            # MjGALLEYREQ with POL_JAN_GALLERY off: refused with 7, the dialog
            # closes; a LEAVEREQ from a member in no gallery: 6 (the other code
            # the leave wait accepts -- it MUST get one of 5/6, mahdisp.c:1116).
            # The grant path is exercised further down, on a running table.
            g = _ask(opcodes.MjGALLEYREQ, 1, 44, f13=0x03)
            if g is None or janwire.unpack(g)["opcode"] != opcodes.MjRESERVEACK \
                    or g[0x18] != galley.GALLEY_REFUSED or g[0x13] != 0x03:
                print("FAIL: MjGALLEYREQ must be refused with MjRESERVEACK 7, "
                      "tag echoed, got %r" % (g and (g[0x18], g[0x13]))); ok = False
            g2 = _ask(opcodes.MjGALLEYLEAVEREQ, 1, 44, f13=0x04)
            if g2 is None or g2[0x18] != janseats.GALLEY_LEFT_ERROR or g2[0x13] != 0x04:
                print("FAIL: MjGALLEYLEAVEREQ from a non-spectator must be "
                      "answered 6, tag echoed, got %r" % (g2 and g2[0x18])); ok = False

            # MjLEAVEROOM: chosen silence, seat released, master promoted
            janseats.take_master_changes()
            _lr = janwire.pack(opcode=opcodes.MjLEAVEROOM, f13=0x11, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 8, length=0x28)
            _out = dispatch.handle_line(janwire.encode(_lr + bytes(8)), member_id=8)
            if _out is None or [l for l in _out if l[:1] == b"B" and
                                janwire.unpack(janwire.decode(l))["opcode"]
                                not in (notices.MjNOTICEMEMBER,)]:
                print("FAIL: MjLEAVEROOM must be answered with SILENCE (an "
                      "empty list, never 'no responder'), got %r" % (_out,))
                ok = False
            if janseats.table_of(8) != 0 or janseats.master_of(1) != 16:
                print("FAIL: the leaving master's seat must go and the guest "
                      "be promoted; master is %r" % janseats.master_of(1))
                ok = False
            # ...and the survivor was told: MjNOTICEMEMBER +0x54 = their seat
            _q = pushqueue._take_pushed(16)
            _nm = [janwire.decode(l) for l in _q if l[:1] == b"B"]
            _nm = [r for r in _nm if janwire.unpack(r)["opcode"] == notices.MjNOTICEMEMBER]
            if not _nm or _nm[-1][0x54] != 1 or _nm[-1][0x50 + 1] != 1:
                print("FAIL: the promoted guest must be queued an MjNOTICEMEMBER "
                      "with +0x54 = 1 (their seat) and +0x51 Ready; got %r"
                      % ([(r[0x54], list(r[0x50:0x54])) for r in _nm],)); ok = False
            if _nm and struct.unpack_from("<Q", _nm[-1], 0x30)[0] != 0:
                print("FAIL: the lobby notice must leave the EMPTY seat 0 as id "
                      "0 -- the client's occupant count picks the dialog"); ok = False

            # KICK: 16 is master now; 8 comes back as a guest and is kicked
            _ask(opcodes.MjPLAYREQ, 1, 8, f13=0x09)
            _ask(opcodes.MjPLAYREQ, 1, 16, f13=0x0C)       # 16's reserve counter is 0xC
            pushqueue._PUSH.clear()
            janseats.take_master_changes()
            _kb = bytearray(janwire.pack(opcode=opcodes.MjMEMBERBANISH, f13=0x2D, src=1,
                                         dst=janwire.DST_SERVER & 0xFF, id8=1,
                                         payload=0x5B01E2BB73B3B60B + 16,
                                         length=0x30))
            _kb += bytes(0x30 - len(_kb))
            struct.pack_into("<Q", _kb, opcodes.BANISH_TARGET_OFF, 0x5B01E2BB73B3B60B + 8)
            _kr = dispatch.handle_line(janwire.encode(bytes(_kb)), member_id=16)
            _kr = [janwire.decode(l) for l in _kr] if _kr else []
            _ack = [r for r in _kr if janwire.unpack(r)["opcode"] == opcodes.MjRESERVEACK]
            if not _ack or _ack[0][0x13] != 0x0C or _ack[0][0x18] == 0:
                print("FAIL: a kick must be answered with MjRESERVEACK carrying "
                      "the kicker's RESERVE tag (0xC), not the banish tag 0x2D; "
                      "got %r" % ([(r[0x12], r[0x13], r[0x18]) for r in _kr],))
                ok = False
            if janseats.table_of(8) != 0 or janseats.table_of(16) != 1:
                print("FAIL: the kicked member must lose the seat, the kicker "
                      "keep it"); ok = False
            _vq = [janwire.decode(l) for l in pushqueue._take_pushed(8) if l[:1] == b"B"]
            _bz = [r for r in _vq if janwire.unpack(r)["opcode"] == notices.MjNOTICEBANISH]
            if not _bz or struct.unpack_from("<Q", _bz[0], 0x20)[0] != \
                    0x5B01E2BB73B3B60B + 8:
                print("FAIL: the victim must be queued MjNOTICEBANISH with THEIR "
                      "PolID at +0x20"); ok = False
            # the kicker's copy rode its own reply line (queued records go
            # first -- see handle_line)
            if not any(janwire.unpack(r)["opcode"] == notices.MjNOTICEBANISH for r in _kr):
                print("FAIL: the kicker's client is told too (it shows the "
                      "dialog like everyone else)"); ok = False
            # a GUEST cannot kick
            _ask(opcodes.MjPLAYREQ, 1, 8, f13=0x0A)
            _kb2 = bytearray(_kb)
            struct.pack_into("<Q", _kb2, 0x18, 0x5B01E2BB73B3B60B + 8)
            struct.pack_into("<Q", _kb2, opcodes.BANISH_TARGET_OFF, 0x5B01E2BB73B3B60B + 16)
            dispatch.handle_line(janwire.encode(bytes(_kb2)), member_id=8)
            if janseats.table_of(16) != 1:
                print("FAIL: a guest's MjMEMBERBANISH must not release anybody")
                ok = False
            pushqueue._PUSH.clear()

            # MjCHMASTER: the master declines -> the other seat takes it
            janseats.take_master_changes()
            _cm = janwire.pack(opcode=opcodes.MjCHMASTER, f13=0x2E, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 16, length=0x28)
            _cr = dispatch.handle_line(janwire.encode(_cm + bytes(8)), member_id=16)
            _cr = [janwire.decode(l) for l in _cr] if _cr else []
            _cr = [r for r in _cr if janwire.unpack(r)["opcode"] == opcodes.MjMASTERCMDACK]
            if not _cr or _cr[0][0x18] != 1 or _cr[0][0x13] != 0x2E:
                print("FAIL: MjCHMASTER must be acked MjMASTERCMDACK result 1 "
                      "(the only value lobbydsp__002b4dc0 accepts)"); ok = False
            if janseats.master_of(1) != 8:
                print("FAIL: declining must pass the mastership to the next "
                      "seat, master is %r" % janseats.master_of(1)); ok = False
            if not any(janwire.unpack(janwire.decode(l))["opcode"] == notices.MjNOTICEMEMBER
                       for l in pushqueue._take_pushed(8) if l[:1] == b"B"):
                print("FAIL: the new master must be told via MjNOTICEMEMBER")
                ok = False
            pushqueue._PUSH.clear()

            # TABLE CHAT: an 'A' line from 8 reaches 16 verbatim, not 8
            _say = janwire.pack(opcode=janwire.MjISAY, length=0x128, src=0,
                                dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4)
            _line = janwire.chat_line(_say, text=b"PS2Tester", extra=b"hello")
            if dispatch.handle_line(_line, member_id=8) is not None:
                print("FAIL: a chat line is relayed, never answered"); ok = False
            if pushqueue._take_pushed(16) != [_line] or pushqueue._take_pushed(8) != []:
                print("FAIL: the chat line must be queued for the table-mate "
                      "only, verbatim"); ok = False
            # MjCHATMEMBERADD from 8: relayed to 16, and answered with 16's ACK
            _add = janwire.pack(opcode=opcodes.MjCHATMEMBERADD, length=0x30, src=0,
                                dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                                payload=0x5B01E2BB73B3B60B + 8)
            _add = _add[:0x20] + b"PS2Tester".ljust(16, b"\0")
            _ar = dispatch.handle_line(janwire.encode(_add), member_id=8)
            _ar = [janwire.decode(l) for l in (_ar or [])]
            if len(_ar) != 1 or janwire.unpack(_ar[0])["opcode"] != opcodes.MjCHATMEMBERACK \
                    or janwire.unpack(_ar[0])["payload"] != 0x5B01E2BB73B3B60B + 16 \
                    or janwire.unpack(_ar[0])["sub"] != 4 \
                    or janwire.unpack(_ar[0])["dst"] != -1:
                print("FAIL: an ADD must be answered with one MjCHATMEMBERACK per "
                      "table-mate carrying THEIR PolID (sub 4, dst -1); got %r"
                      % ([(janwire.unpack(r)["opcode"],
                           "%016x" % janwire.unpack(r)["payload"]) for r in _ar],))
                ok = False
            _rq = [janwire.decode(l) for l in pushqueue._take_pushed(16)]
            if not any(janwire.unpack(r)["opcode"] == opcodes.MjCHATMEMBERADD for r in _rq):
                print("FAIL: the ADD itself must be relayed to the table-mate")
                ok = False
            # the 0x48 INFO line is relayed too, and has a name now
            _info = janwire.pack(opcode=opcodes.MjCHATINFO, length=0x128, src=0,
                                 dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4)
            _info = _info[:0x18] + b"INFO".ljust(16, b"\0") + \
                b"PS2Tester has entered.".ljust(0x100, b"\0")
            dispatch.handle_line(janwire.encode(_info), member_id=8)
            if not any(janwire.unpack(janwire.decode(l))["opcode"] == opcodes.MjCHATINFO
                       for l in pushqueue._take_pushed(16)):
                print("FAIL: the 0x48 INFO line must be relayed"); ok = False
            # a pushed line rides take_pending FIRST
            pushqueue.push_line(16, b"Bfake")
            if dispatch.take_pending(16)[:1] != [b"Bfake"]:
                print("FAIL: take_pending must drain the lobby push queue")
                ok = False
            if dispatch.take_pending(16, limit=0) != []:
                pass
            # the rejoin flag: MjENTERGAME on a PLAYING table marks the member
            janseats.set_in_play(1, 8)
            _eg = janwire.pack(opcode=opcodes.MjENTERGAME, f13=0x2F, src=1,
                               dst=janwire.DST_SERVER & 0xFF, id8=1,
                               payload=0x5B01E2BB73B3B60B + 16, length=0x28)
            _er = dispatch.handle_line(janwire.encode(_eg + bytes(8)), member_id=16)
            _er = janwire.decode(_er[0]) if isinstance(_er, list) else None
            if _er is None or janwire.unpack(_er)["opcode"] != opcodes.MjENTERGAMEACK \
                    or _er[0x18] != 1:
                print("FAIL: MjENTERGAME on a playing table must be Success")
                ok = False
            if not janseats.rejoin_pending(16, clear=True):
                print("FAIL: MjENTERGAME on a playing table must flag the "
                      "rejoin for jangame"); ok = False
            janseats.clear_in_play(1)
            pushqueue._PUSH.clear()

            # --- TWO ROOMS, ONE TABLE NUMBER (finding 30) -------------------
            # Member 8 is JOINed to #MJS0R011 (Room 1-1 = 101), member 16 to
            # #MJS0R023 (Room 2-3 = 203). Both press Reserve on "table 1";
            # the wire carries id8=1 for both, the registry tells them apart.
            _saved_rooms = seating.LIVE_ROOMS
            seating.LIVE_ROOMS = lambda: {          # noqa: E731
                "#MJS0R011": {"who": [{"member_id": 8, "name": "PS2Tester"},
                                      {"member_id": 44, "name": "Watcher"}]},
                "#MJS0R023": {"who": [{"member_id": 16, "name": "DeckTest"}]}}
            janseats._TABLES.clear(); janseats._DELTAS.clear()
            janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats.take_master_changes()
            if dispatch.GAMES is not None:
                dispatch.GAMES.by_lobby.clear(); dispatch.GAMES.by_member.clear(); dispatch.GAMES.tables.clear()
            _t101 = janseats.room_table_id(101, 1)
            _t203 = janseats.room_table_id(203, 1)
            if seating.member_room(8) != 101 or seating.member_room(16) != 203 or seating.member_room(99) != 0:
                print("FAIL: member_room must read the registry: 8->%r 16->%r"
                      % (seating.member_room(8), seating.member_room(16))); ok = False
            _ra = _ask(opcodes.MjPLAYREQ, 1, 8, f13=0x01)
            _rb = _ask(opcodes.MjPLAYREQ, 1, 16, f13=0x01)
            if _ra is None or _rb is None or _ra[0x18] != 2 or _rb[0x18] != 2:
                print("FAIL: two members in two rooms reserving 'table 1' must "
                      "BOTH be masters (2/ReserveMaster), got %r / %r"
                      % (_ra and _ra[0x18], _rb and _rb[0x18])); ok = False
            if janseats.table_of(8) != _t101 or janseats.table_of(16) != _t203:
                print("FAIL: the seats must land in each member's OWN room: "
                      "8 at %r, 16 at %r" % (janseats.table_of(8),
                                             janseats.table_of(16))); ok = False
            if ([m for m, _s, _n, _p in janseats.seats_at(_t101)] != [8]
                    or [m for m, _s, _n, _p in janseats.seats_at(_t203)] != [16]
                    or janseats.seat_count(1) != 0):
                print("FAIL: separate seat sets expected; bare table 1 must be "
                      "empty: %r" % janseats.snapshot()); ok = False
            if (janseats.sequence(101) != 1 or janseats.sequence(203) != 1
                    or janseats.deltas_after(0, 101) != [(1, _t101)]
                    or janseats.deltas_after(0, 203) != [(1, _t203)]
                    or janseats.deltas_after(0, 0) != []):
                print("FAIL: each room must have its own delta stream: 101=%r "
                      "203=%r" % (janseats.deltas_after(0, 101),
                                  janseats.deltas_after(0, 203))); ok = False
            # a second reserver in room 203 moves room 203 only
            seating.LIVE_ROOMS = lambda: {          # noqa: E731
                "#MJS0R011": {"who": [{"member_id": 8}, {"member_id": 44}]},
                "#MJS0R023": {"who": [{"member_id": 16}, {"member_id": 21}]}}
            _rc = _ask(opcodes.MjPLAYREQ, 1, 21, f13=0x01)
            if _rc is None or _rc[0x18] != 1 or janseats.sequence(101) != 1 \
                    or janseats.sequence(203) != 2:
                print("FAIL: a guest joining room 203's table 1 must be a guest "
                      "(1) of member 16 and move ONLY room 203's sequence")
                ok = False
            # the rules: one MjTBLCONFALL per room, keyed by composite
            if janrules is not None:
                os.environ["POL_JAN_RULES_FILE"] = os.path.join(td, "rules2.json")
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = True
                janrules._OWNER[0] = True
                _cf = bytearray(janwire.pack(opcode=opcodes.MjTBLCONFALL, f13=0x21, src=1,
                                             dst=janwire.DST_SERVER & 0xFF, id8=1,
                                             payload=0x5B01E2BB73B3B60B + 8,
                                             length=0x70))
                _cf += bytes(0x70 - len(_cf))
                _cf[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 4
                dispatch.handle_line(janwire.encode(bytes(_cf)), member_id=8)
                _cf2 = bytearray(_cf)
                struct.pack_into("<Q", _cf2, 0x18, 0x5B01E2BB73B3B60B + 16)
                _cf2[janrules.CONFIG_OFF + janrules.RULE_NAMES.index("UMA")] = 1
                dispatch.handle_line(janwire.encode(bytes(_cf2)), member_id=16)
                if (janrules.values_for(_t101)["UMA"] != 4
                        or janrules.values_for(_t203)["UMA"] != 1
                        or janrules.has_rules(1)):
                    print("FAIL: MjTBLCONFALL must key the rules by the "
                          "composite: 101=%r 203=%r bare=%r"
                          % (janrules.values_for(_t101)["UMA"],
                             janrules.values_for(_t203)["UMA"],
                             janrules.has_rules(1))); ok = False
            # Start Game in both rooms -> TWO in-game tables
            if dispatch.GAMES is not None:
                _gs = janwire.pack(opcode=opcodes.MjGAMESTART, f13=0x22, src=1,
                                   dst=janwire.DST_SERVER & 0xFF, id8=1,
                                   payload=0x5B01E2BB73B3B60B + 8)
                _ga = dispatch.handle_line(janwire.encode(_gs), member_id=8)
                _gs2 = janwire.pack(opcode=opcodes.MjGAMESTART, f13=0x22, src=1,
                                    dst=janwire.DST_SERVER & 0xFF, id8=1,
                                    payload=0x5B01E2BB73B3B60B + 16)
                _gb = dispatch.handle_line(janwire.encode(_gs2), member_id=16)
                if not _ga or not _gb:
                    print("FAIL: MjGAMESTART in each room must be answered")
                    ok = False
                _ta = dispatch.GAMES.by_lobby.get(_t101)
                _tb = dispatch.GAMES.by_lobby.get(_t203)
                if _ta is None or _tb is None or _ta is _tb:
                    print("FAIL: Manager.table_for_lobby must give TWO tables, "
                          "keyed %r; got %r" % ((_t101, _t203),
                                                sorted(dispatch.GAMES.by_lobby)))
                    ok = False
                elif (0x5B01E2BB73B3B60B + 16 in _ta.member_ids()
                      or 0x5B01E2BB73B3B60B + 8 not in _ta.member_ids()
                      or 0x5B01E2BB73B3B60B + 16 not in _tb.member_ids()
                      or 0x5B01E2BB73B3B60B + 21 not in _tb.member_ids()):
                    print("FAIL: each room's table seats its own humans: "
                          "101=%r 203=%r" % (["%x" % i for i in _ta.member_ids()],
                                              ["%x" % i for i in _tb.member_ids()]))
                    ok = False
                if _ta is not None and _ta.channel != b"#MJS0T101001":
                    print("FAIL: the in-game channel is the ROOM-QUALIFIED PTL "
                          "row name Room 1-1's client JOINs: %r" % _ta.channel)
                    ok = False
                if not janseats.is_in_play(_t101) or not janseats.is_in_play(_t203) \
                        or janseats.is_in_play(1):
                    print("FAIL: in-play must be per composite"); ok = False
                if janrules is not None and (_ta.rules.uma != (60, 20, -20, -60)
                                             or _tb.rules.uma != (10, 5, -5, -10)):
                    print("FAIL: each table plays ITS room's rules: 101 uma=%r "
                          "203 uma=%r" % (_ta.rules.uma, _tb.rules.uma)); ok = False

                # --- THE GALLERY, END TO END (2026-09-04) -------------------
                # Member 44 (Room 1-1, no seat) watches Room 1-1's table 1
                # while member 8 plays it against three bots. Every byte
                # below is the client's own, read out of its sources.
                if _ta is not None and janrules is not None:
                    _old_delay = jangame.DEAL_DELAY
                    jangame.DEAL_DELAY = 0.0
                    _old_gal = galley.GALLERY_ENABLE
                    _rdy = janwire.pack(opcode=janmsgs.MjREADY, f13=0, src=0, dst=4,
                                        f16=0, sub=3, length=0x18)[:0x18]
                    _dl = dispatch.handle_line(janwire.encode(_rdy), member_id=8)
                    if _ta.state != "playing" or not _dl:
                        print("FAIL: gallery rig: member 8's MjREADY must deal")
                        ok = False
                    pushqueue._PUSH.clear()

                    def _watch(member=44, f13=0x05, table=1):
                        r = _ask(opcodes.MjGALLEYREQ, table, member, f13=f13)
                        return (r[0x18], r[0x19], r[0x13]) if r is not None else None

                    def _gack(seq, slot, f16=1):
                        # console__00284310 (console.c:2552-2577): op 0x30, +0x13
                        # seq, +0x14 the RESERVEACK +0x19 slot, +0x15 4, +0x17 6
                        return janwire.pack(opcode=janmsgs.MjGALLEYACK, f13=seq,
                                            src=slot, dst=4, f16=f16, sub=6,
                                            length=0x18)[:0x18]

                    def _ops_of(lines):
                        return [janwire.unpack(janwire.decode(l))["opcode"]
                                for l in (lines or []) if l[:1] == b"B"]

                    # knob off: 7, whatever the table is doing
                    if _watch() != (galley.GALLEY_REFUSED, 0, 0x05):
                        print("FAIL: with POL_JAN_GALLERY off a Watch on a running "
                              "table is still 7, got %r" % (_watch(),)); ok = False
                    galley.GALLERY_ENABLE = True
                    # the master's stored rules say GALLEY_LIMIT 0 (No) -> 10
                    if janrules.values_for(_t101)["GALLEY_LIMIT"] != 0 \
                            or _watch() != (janseats.GALLEY_LIMITED, 0, 0x05):
                        print("FAIL: GALLEY_LIMIT 0 must refuse with 10, got %r"
                              % (_watch(),)); ok = False
                    if janseats.gallery_of(44) or dispatch.GAMES.spectator_table(44) is not None:
                        print("FAIL: a refused Watch must leave no membership"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 1}, by=8)
                    # a player cannot watch its own table: 7
                    if _watch(member=8) != (galley.GALLEY_REFUSED, 0, 0x05):
                        print("FAIL: a seated member's Watch is 7, got %r"
                              % (_watch(member=8),)); ok = False
                    # a table with nothing running: 8 (Room 2-3's table 1 is
                    # seated but never dealt -- state idle -- and 16 is its
                    # only human, so ask from 16's room-mate... there is none:
                    # use a table in room 101 nobody reserved)
                    janrules.store(janseats.room_table_id(101, 2), {"GALLEY_LIMIT": 1}, by=8)
                    if _watch(table=2) != (janseats.GALLEY_NOT_PLAYING, 0, 0x05):
                        print("FAIL: a Watch on a table with no game is 8, got %r"
                              % (_watch(table=2),)); ok = False
                    # a password table (LIMIT_PASS_WORD on + text): 9 -- the
                    # client always sends "" -- and the flag alone gates it
                    janrules.store(_t101, {"LIMIT_PASS_WORD": 1, "text": "abc"}, by=8)
                    if _watch() != (janseats.GALLEY_PASSWORD, 0, 0x05):
                        print("FAIL: a password table refuses with 9, got %r"
                              % (_watch(),)); ok = False
                    janrules.store(_t101, {"LIMIT_PASS_WORD": 0}, by=8)
                    # the grant: 1, slot 0 at +0x19, tag echoed
                    _w = _watch()
                    if _w != (janseats.GALLEY_OK, 0, 0x05):
                        print("FAIL: the Watch must be granted 1/slot 0/tag 5, got %r"
                              % (_w,)); ok = False
                    if janseats.gallery_of(44) != _t101 or dispatch.GAMES.spectator_table(44) is not _ta \
                            or janseats.table_of(44) != 0 or 44 in _ta.live \
                            or janseats.seat_count(_t101) != 1:
                        print("FAIL: the spectator is in the store's gallery and the "
                              "manager's table, and is NOT a seat"); ok = False
                    if janseats.resolve_table(44, 1) != _t101 \
                            or janseats.resolve_table(44, 1, room=seating.member_room(44)) != _t101:
                        print("FAIL: the spectator's b/g/MJSTableInfoSub fetch must "
                              "resolve to the table it watches"); ok = False
                    # again: 3, and the stale membership is dropped; then 1 again
                    if _watch(f13=0x06) != (janseats.GALLEY_DUP, 0, 0x06) \
                            or janseats.gallery_of(44) != 0 \
                            or dispatch.GAMES.spectator_table(44) is not None:
                        print("FAIL: a second Watch is 3 (Already spectating) and "
                              "drops the stale membership"); ok = False
                    if _watch(f13=0x07) != (janseats.GALLEY_OK, 0, 0x07):
                        print("FAIL: ...so the NEXT Watch is a 1 again"); ok = False
                    # nothing before its first MjGALLEYACK (the entry drain)
                    if dispatch.take_pending(44) != []:
                        print("FAIL: nothing may be queued for a spectator before "
                              "its first MjGALLEYACK"); ok = False
                    _e0 = dispatch.handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44)
                    if _ops_of(_e0) != [janmsgs.MjALLDATA] or janwire.decode(_e0[0])[0x18] != 0:
                        print("FAIL: the first (entry) GALLEYACK is answered with the "
                              "subtype-0 MjALLDATA snapshot and nothing else, got %r"
                              % _ops_of(_e0)); ok = False
                    if dispatch.handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44) is not None:
                        print("FAIL: the second entry ack rides silence"); ok = False
                    # the human's HAIPAIACK moves the hand; the spectator gets a
                    # byte-for-byte copy of everything the human got
                    _hp = _ta._last_for[0]
                    _o8 = dispatch.handle_line(janwire.encode(jangame._client_ack(_hp, 0)), member_id=8)
                    _c44 = dispatch.take_pending(44)
                    if not _o8 or _c44 != _o8:
                        print("FAIL: the spectator's copies must equal the human's "
                              "records: %r vs %r" % (_ops_of(_c44), _ops_of(_o8))); ok = False
                    if janmsgs.MjTSUMO not in _ops_of(_c44):
                        print("FAIL: ...including the seat-addressed MjTSUMO draw"); ok = False
                    # its ack: consumed, no mutation, no reply; a stray
                    # SASHIUMAREQUEST: ignored, never a ghost MjGAMEEND
                    _st = (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting)
                    _sq = janwire.unpack(janwire.decode(_c44[-1]))["f13"]
                    if dispatch.handle_line(janwire.encode(_gack(_sq, 0)), member_id=44) is not None \
                            or _ta.gallery_seq.get(44) != _sq \
                            or (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting) != _st:
                        print("FAIL: a spectator's GALLEYACK is consumed without "
                              "mutation or reply"); ok = False
                    _stray = janwire.pack(opcode=janmsgs.MjSASHIUMAREQUEST, f13=_sq,
                                          src=0, dst=4, f16=1, sub=3, length=0x20)[:0x20]
                    if dispatch.handle_line(janwire.encode(_stray), member_id=44) is not None \
                            or _ta.state != "playing" \
                            or (_ta.kyoku.turn, _ta.wall_count(), _ta._awaiting) != _st:
                        print("FAIL: a stray MjSASHIUMAREQUEST from a spectator is "
                              "ignored -- no reply, no ghost MjGAMEEND"); ok = False
                    # a silent spectator never stalls: the human plays on
                    _turns = 0
                    while (_ta._awaiting == (janmsgs.MjTSUMO, 0) and _turns < 3
                           and _ta.kyoku is not None and _ta.kyoku.result is None):
                        _lt = _ta._last_for[0]
                        _hd = [v for v in struct.unpack_from("<14H", _lt, janmsgs.TSUMO_HAND) if v]
                        dispatch.handle_line(janwire.encode(jangame._client_sute(_lt, 0, len(_hd) - 1)),
                                    member_id=8)
                        _turns += 1
                    if _turns < 1 or 44 in _ta.timeouts or not dispatch.take_pending(44):
                        print("FAIL: the hand advances with a SILENT spectator, whose "
                              "copies keep queueing (%d turn(s))" % _turns); ok = False
                    # CHAT both ways; suppressed for the spectator under limit 2
                    pushqueue._PUSH.clear()
                    _say44 = janwire.chat_line(
                        janwire.pack(opcode=janwire.MjISAY, length=0x128, src=0,
                                     dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4),
                        text=b"Watcher", extra=b"nice hand")
                    if dispatch.handle_line(_say44, member_id=44) is not None \
                            or pushqueue._take_pushed(8) != [_say44] or pushqueue._take_pushed(44) != []:
                        print("FAIL: a spectator's MjISAY reaches the seated human, "
                              "verbatim, not itself"); ok = False
                    if dispatch.handle_line(_line, member_id=8) is not None \
                            or pushqueue._take_pushed(44) != [_line] or pushqueue._take_pushed(8) != []:
                        print("FAIL: a player's MjISAY reaches the spectator"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 2}, by=8)
                    dispatch.handle_line(_say44, member_id=44)
                    dispatch.handle_line(_line, member_id=8)
                    if pushqueue._take_pushed(8) != [] or pushqueue._take_pushed(44) != [_line]:
                        print("FAIL: GALLEY_LIMIT 2 (No chat only) drops the spectator's "
                              "line and still relays the players' to it"); ok = False
                    janrules.store(_t101, {"GALLEY_LIMIT": 1}, by=8)
                    _add44 = janwire.pack(opcode=opcodes.MjCHATMEMBERADD, length=0x30, src=0,
                                          dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                                          payload=0x5B01E2BB73B3B60B + 44)
                    _add44 = _add44[:0x20] + b"Watcher".ljust(16, b"\0")
                    _ar44 = dispatch.handle_line(janwire.encode(_add44), member_id=44)
                    _ar44 = [janwire.decode(l) for l in (_ar44 or []) if l[:1] == b"B"]
                    if len(_ar44) != 1 or janwire.unpack(_ar44[0])["opcode"] != opcodes.MjCHATMEMBERACK \
                            or janwire.unpack(_ar44[0])["payload"] != 0x5B01E2BB73B3B60B + 8 \
                            or pushqueue._take_pushed(8) != [janwire.encode(_add44)]:
                        print("FAIL: a spectator's CHATMEMBERADD is relayed to the "
                              "seated human and answered with ITS ack (one per seated "
                              "human)"); ok = False
                    pushqueue._PUSH.clear()
                    # LEAVEREQ: 5, membership gone everywhere; again: 6
                    _lv = _ask(opcodes.MjGALLEYLEAVEREQ, 1, 44, f13=0x08)
                    if _lv is None or _lv[0x18] != galley.GALLEY_LEFT or _lv[0x13] != 0x08 \
                            or janseats.gallery_of(44) != 0 \
                            or dispatch.GAMES.spectator_table(44) is not None \
                            or 44 in _ta.gallery or dispatch.take_pending(44) != []:
                        print("FAIL: MjGALLEYLEAVEREQ is 5 and drops the membership "
                              "in the store and the manager, got %r"
                              % (_lv and _lv[0x18])); ok = False
                    if _ask(opcodes.MjGALLEYLEAVEREQ, 1, 44, f13=0x09)[0x18] != janseats.GALLEY_LEFT_ERROR:
                        print("FAIL: a second leave is 6"); ok = False
                    if dispatch.handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44) is not None:
                        print("FAIL: the exit acks after a leave (no table now) are "
                              "silence, never a ghost MjGAMEEND"); ok = False
                    # GAMEEND drops the membership without any leave
                    if _watch(f13=0x0A) != (janseats.GALLEY_OK, 0, 0x0A):
                        print("FAIL: after a leave, Watch is 1 again"); ok = False
                    dispatch.handle_line(janwire.encode(_gack(0, 0, f16=0)), member_id=44)
                    dispatch.take_pending(44)
                    _ta.begin_routing()
                    _ge = _ta.msg_gameend()
                    _ta.take_routes()
                    dispatch.GAMES._release_galleries()
                    _c44 = dispatch.take_pending(44)
                    if janseats.gallery_of(44) != 0 or not _c44 \
                            or _c44[-1] != janwire.encode(_ge):
                        print("FAIL: MjGAMEEND clears the store's gallery and the "
                              "spectator still gets the GAMEEND copy"); ok = False
                    if dispatch.handle_line(janwire.encode(_gack(janwire.unpack(_ge)["f13"], 0, f16=0)),
                                   member_id=44) is not None \
                            or dispatch.GAMES.spectator_table(44) is not None:
                        print("FAIL: the BYE-equivalent GALLEYACK after MjGAMEEND drops "
                              "the manager membership silently"); ok = False
                    if _watch(f13=0x0B) != (janseats.GALLEY_NOT_PLAYING, 0, 0x0B):
                        print("FAIL: a Watch on the finished table is 8 (not 3): %r"
                              % (_watch(f13=0x0B),)); ok = False
                    galley.GALLERY_ENABLE = _old_gal
                    jangame.DEAL_DELAY = _old_delay
                    pushqueue._PUSH.clear()
                dispatch.GAMES.by_lobby.clear(); dispatch.GAMES.by_member.clear(); dispatch.GAMES.tables.clear()
            if janrules is not None:
                janrules._TABLES.clear()
                janrules._ADOPTED[0] = False
                janrules._OWNER[0] = False
                janrules._CACHE["mtime"] = -1.0
                os.environ.pop("POL_JAN_RULES_FILE", None)
            # without a registry the wire id is room 0's -- the v1 behaviour
            seating.LIVE_ROOMS = None
            if seating._table_for(8, 1) != _t101 or seating._table_for(4242, 1) != 1:
                print("FAIL: without a registry a SEATED member's table is still "
                      "theirs (%r) and a stranger's is room 0 (%r)"
                      % (seating._table_for(8, 1), seating._table_for(4242, 1))); ok = False
            seating.LIVE_ROOMS = _saved_rooms
            pushqueue._PUSH.clear()
            janseats.take_master_changes()

            janseats._TABLES.clear()
            janseats._DELTAS.clear(); janseats._ROWS.clear(); janseats._SEQ.clear()
            janseats._OWNER[0] = False
            janseats._ADOPTED[0] = False
            janseats._CACHE["mtime"] = -1.0
            os.environ.pop("POL_JAN_SEATS", None)
            os.environ.pop("POL_JAN_SEATS_FILE", None)

    srv.shutdown()
    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--decode", metavar="LINE")
    ap.add_argument("--port", type=int, default=server.PORT)
    a = ap.parse_args()
    if a.decode:
        rec = janwire.decode(a.decode)
        if len(rec) < 32:
            print("only %d bytes decoded from %d characters -- a line this short "
                  "usually means unprintable bytes were lost in transit: six-bit "
                  "value 63 encodes to 0x7f (DEL), so these lines cannot be "
                  "copied out of a terminal. Feed the raw bytes."
                  % (len(rec), len(a.decode)))
            return 1
        print(wirelog.describe(rec))
        return 0
    if a.serve:
        server.serve(port=a.port)
        return 0
    return selftest()
