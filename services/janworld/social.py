"""The table-social layer: the master's seat and commands, kicks, stored rules, table chat."""
import struct
import janwire                                                  # noqa: E402
from .deps import janrules, janseats
from . import galley, notices, opcodes, pushqueue, seating, wirelog


# --- the table-social layer (2026-09-04) -------------------------------------

def _master_seat_of(tid):
    """The master's seat at lobby table `tid`, for MjNOTICEMEMBER +0x54."""
    if janseats is None or not janseats.enabled() or not tid:
        return 0
    try:
        ms = janseats.master_seat_of(tid)
    except Exception:
        ms = None
    return int(ms) if ms is not None else 0


def lobby_notice(tid):
    """MjNOTICEMEMBER for a table that is NOT in play: the seated humans'
    PolIDs by seat (an empty seat stays 0 -- the client's occupied count is
    the number of non-zero ids, and it picks the one-button "You are the new
    Table Master" for 1 and the Yes/No "Become the new Table Master?" for
    more, lobbydsp.c:172-179), +0x50 Ready for every occupied seat, +0x54 the
    master's seat, names in the 64-byte tail."""
    ids, flags, names = [0, 0, 0, 0], [0, 0, 0, 0], bytearray(4 * notices.NAME_SLOT)
    for m, st, nm, pid in pushqueue._table_members(tid):
        if not 0 <= st < 4:
            continue
        ids[st] = pid or m
        flags[st] = 1
        raw = (nm or seating.display_name(m)).encode("cp932", "replace")[:notices.NAME_SLOT]
        names[st * notices.NAME_SLOT:st * notices.NAME_SLOT + len(raw)] = raw
    return notices.member_list_notice(ids=ids, a=notices.RESULT_OK, b=0,
                              seat=_master_seat_of(tid), flags=flags,
                              tail=bytes(names))


def notify_master_changes(peer="-"):
    """Turn every promotion the seat store recorded into the notice that
    makes the promoted client say so. Returns the (table, member) pairs."""
    if janseats is None or not janseats.enabled():
        return []
    try:
        changes = janseats.take_master_changes()
    except Exception:
        return []
    for tid, new in changes:
        n = pushqueue.push_table(tid, lobby_notice(tid))
        wirelog.log("%s   table %s: member %s is the new MASTER (seat %d) -- "
            "MjNOTICEMEMBER +0x54 queued for %d member(s)"
            % (peer, tid, new, _master_seat_of(tid), n))
    return changes


def warn_expiring_seats(peer="-"):
    """MjNOTICETIMEUPWARNING for seats about to age out (called from the
    `<DR>` path, the lobby's only regular tick). Inert on the client beyond a
    debug line; the seat store is the thing that actually expires them."""
    if janseats is None or not janseats.enabled():
        return 0
    n = 0
    try:
        for tid, member, left in janseats.expiring_seats():
            if pushqueue.push_record(member, notices.timeup_notice(left)):
                n += 1
                wirelog.log("%s   table %s: member %s's seat expires in %ds -- "
                    "MjNOTICETIMEUPWARNING queued" % (peer, tid, member, left))
    except Exception as e:
        wirelog.log("%s   seat-expiry warning raised: %s" % (peer, e))
    return n


def broadcast_server_quit(peer="shutdown"):
    """MjNOTICESERVERQUIT to every seated member (graceful shutdown). Queued;
    the idle push has POL_SHUTDOWN_GRACE seconds to carry it."""
    if janseats is None or not janseats.enabled():
        return 0
    n = 0
    try:
        for tid in list(janseats.snapshot()):
            n += pushqueue.push_table(int(tid), notices.serverquit_notice())
    except Exception as e:
        wirelog.log("%s   server-quit broadcast raised: %s" % (peer, e))
    if n:
        wirelog.log("%s   MjNOTICESERVERQUIT queued for %d member(s)" % (peer, n))
    return n


def _kick(rec, h, member_id, peer):
    """MjMEMBERBANISH (finding 14): free the target's seat, tell the table,
    and answer the check the kicker's client is actually polling.

    The target is the PolID at +0x28 (`malloc__002c4fb0`). Only the master
    may kick (the menu is master-only; a guest's request is acked and
    ignored). The reply MUST be MjRESERVEACK with +0x13 = the kicker's
    reserve-family tag -- see `janseats.reserve_tag` -- or the master sits on
    "Removing them from the table" for 120000 ticks. Result byte 5
    (ReserveCancel): the client tests only `!= 0`, and "cancel complete" is
    what happened.
    """
    tid = seating._table_for(member_id, h["id8"])
    target = (struct.unpack_from("<Q", rec, opcodes.BANISH_TARGET_OFF)[0]
              if len(rec) >= opcodes.BANISH_TARGET_OFF + 8 else 0)
    members = pushqueue._table_members(tid)
    victim = next(((m, st, nm, pid) for m, st, nm, pid in members
                   if pid == target or m == target), None)
    is_master = bool(tid) and janseats.master_of(tid) == int(member_id)
    if not is_master:
        wirelog.log("%s      MjMEMBERBANISH from member %s who is NOT master of table "
            "%s -- acked, nothing released" % (peer, member_id, tid))
    elif victim is None:
        wirelog.log("%s      MjMEMBERBANISH names PolID %016x, nobody at table %s -- "
            "acked, nothing released" % (peer, target, tid))
    else:
        vm, vseat, vname, vpid = victim
        janseats.cancel(tid, vm)
        notice = notices.banish_notice(kicked=vpid or vm, by=h["payload"],
                               kicked_name=(vname or "").encode("cp932", "replace"),
                               by_name=seating.display_name(member_id, peer).encode(
                                   "cp932", "replace"))
        # Everyone left at the table sees the dialog; the victim's copy is
        # the one that un-seats them (+0x20 == their own PolID).
        n = pushqueue.push_table(tid, notice)
        n += 1 if pushqueue.push_record(vm, notice) else 0
        wirelog.log("%s      KICK: master %s removed member %s (seat %d, PolID %016x) "
            "from table %s -- MjNOTICEBANISH queued for %d member(s)"
            % (peer, member_id, vm, vseat, vpid, tid, n))
    tag = janseats.reserve_tag(member_id)
    if tag is None:
        wirelog.log("%s      WARNING: no reserve tag on record for member %s -- the kick ack "
            "carries 0 and the client's reserve check may not match it"
            % (peer, member_id))
        tag = 0
    reply = bytearray(opcodes.ack_for(rec, result=janseats.RESERVE_CANCEL, extra=0))
    reply[0x13] = tag & 0xFF
    wirelog.log("%s   -> %s  [MjRESERVEACK with the RESERVE tag %#x, not the banish's "
        "%#x -- wrsCpTableReserveCheck is what the kicker polls]"
        % (peer, wirelog.describe(bytes(reply)), tag, h["f13"]))
    out = [janwire.encode(bytes(reply))]
    notify_master_changes(peer=peer)
    return out


def _decline_master(rec, h, member_id, peer):
    """MjCHMASTER (finding 24/8): the member offered the mastership answered
    No (lobbydsp.c:206-213 -- Yes sends nothing). Rotate to the next seat and
    say so; the ack is MjMASTERCMDACK and ONLY result 1 releases the client
    (lobbydsp__002b4dc0 loops on anything else)."""
    tid = seating._table_for(member_id, h["id8"])
    new = janseats.decline_master(tid, member_id) if tid else 0
    wirelog.log("%s      MjCHMASTER: member %s declines table %s -- master is now %s"
        % (peer, member_id, tid, new or "nobody"))
    reply = opcodes.ack_for(rec, result=1)
    wirelog.log("%s   -> %s  result=1/Success" % (peer, wirelog.describe(reply)))
    out = [janwire.encode(reply)]
    notify_master_changes(peer=peer)
    return out


def _store_rules(rec, h, member_id, peer):
    """Parse the master's MjTBLCONFALL into the rule store."""
    if janrules is None or not janrules.enabled():
        return False
    try:
        vals = janrules.parse_tblconfall(rec)
    except ValueError as e:
        wirelog.log("%s      MjTBLCONFALL not parsed (%s) -- rules unchanged" % (peer, e))
        return False
    # +0x08 is the 16-bit wire id; the rule store keys by the composite
    # (finding 30), so qualify it with the master's room first.
    tid = seating._table_for(member_id, vals.get("table"))
    if not tid:
        wirelog.log("%s      MjTBLCONFALL names no table and member %s has no seat -- "
            "rules not stored" % (peer, member_id))
        return False
    if (janseats is not None and janseats.enabled()
            and janseats.master_of(tid) not in (0, int(member_id))):
        wirelog.log("%s      MjTBLCONFALL from member %s who is not master of table %s "
            "-- rules not stored" % (peer, member_id, tid))
        return False
    janrules.store(tid, vals, by=member_id)
    wirelog.log("%s      RULES for table %s stored from member %s: %s"
        % (peer, tid, member_id, janrules.describe(vals)))
    return True


def _chat_table_of(member_id):
    """(table id, is_spectator) for a chat line's sender: the seat first,
    else the gallery (a spectator's MjISAY is plain MjISAY on the table
    channel, spec section 2 "Chat")."""
    if hasattr(janseats, "table_or_gallery_of"):
        try:
            return janseats.table_or_gallery_of(member_id)
        except Exception:
            pass
    return janseats.table_of(member_id), False


def _relay_chat(line, opcode, member_id, peer):
    """MjISAY / MjISAYGALLEY (finding 15): the 'A' line, verbatim, to every
    other human at the sender's table AND to its spectators. The receive
    loop (`sqFileAccess__003730d0`) drains sub-code 4 and reads +0x18 (name)
    and +0x28 (text) straight out of the record, so the sender's own line is
    exactly what the others need. A SPECTATOR's line goes the same way --
    unless the table's GALLEY_LIMIT is 2 ("No chat only"), when it is
    dropped here (the client does not enforce it)."""
    if janseats is None or not janseats.enabled() or not member_id:
        return 0
    tid, spectator = _chat_table_of(member_id)
    if not tid:
        wirelog.log("%s      chat from member %s who holds no seat and watches nothing "
            "-- not relayed" % (peer, member_id))
        return 0
    if spectator and galley._gallery_limit(tid) == galley.GALLEY_NO_CHAT:
        wirelog.log("%s      %s from SPECTATOR %s at %s dropped: GALLEY_LIMIT is 2 "
            "(No chat only)" % (peer, opcodes.opname(opcode), member_id, seating._tl(tid)))
        return 0
    n = pushqueue.push_table(tid, bytes(line), exclude=(int(member_id),), gallery=True)
    wirelog.log("%s      %s%s relayed to %d table-mate(s)/spectator(s) at %s"
        % (peer, opcodes.opname(opcode), " from a SPECTATOR" if spectator else "", n,
           seating._tl(tid)))
    return n


def _chat_member_ack(polid, name):
    """The 'G' record a peer answers an ADD with (sqFileAccess.c:246-258):
    len 0x30, src 0, dst -1, f16 4, sub 4, +0x18 its own id, +0x20 its
    16-byte name."""
    rec = janwire.pack(opcode=opcodes.MjCHATMEMBERACK, length=0x30, src=0,
                       dst=janwire.DST_CHANNEL & 0xFF, f16=4, sub=4,
                       payload=polid)
    body = bytearray(0x30 - 0x20)
    raw = (name or "").encode("cp932", "replace")[:notices.NAME_LEN]
    body[:len(raw)] = raw
    return rec[:0x20] + bytes(body)


def _chat_member(line, rec, h, member_id, peer):
    """MjCHATMEMBERADD/DEL/ACK and the 0x48 INFO line (finding 15).

    The client's own peers do two things with an ADD (sqFileAccess.c:
    100-190, 246-258): add the newcomer to their chat roster, and answer with
    a MjCHATMEMBERACK carrying THEIR id and name so the newcomer's roster
    fills. We are every peer at once: relay the record to the table-mates,
    and answer the ADD with one ACK per seated human. DEL, ACK and the INFO
    line ("%s has entered." / "has left.") are relayed as they are.
    """
    if janseats is None or not janseats.enabled():
        return None
    tid, spectator = _chat_table_of(member_id)
    if not tid:
        wirelog.log("%s      %s from member %s who holds no seat and watches nothing "
            "-- dropped" % (peer, opcodes.opname(h["opcode"]), member_id))
        return []
    # The roster handshake runs for spectators as for players (chat.c:61-70
    # -> 329-368, no DAT_0042aba8 in chat.c): a spectator's ADD reaches the
    # seated humans and the other spectators, and is answered with one ACK
    # per SEATED human (the other spectators' clients answer for themselves
    # when the relayed ADD reaches them, as any real peer does).
    n = pushqueue.push_table(tid, bytes(line), exclude=(int(member_id),), gallery=True)
    out = []
    if h["opcode"] == opcodes.MjCHATMEMBERADD:
        for m, _st, nm, pid in pushqueue._table_members(tid):
            if m == int(member_id):
                continue
            ack = _chat_member_ack(pid or m, nm or seating.display_name(m))
            out.append(janwire.encode(ack))
        wirelog.log("%s      %s%s relayed to %d table-mate(s)/spectator(s); answering "
            "with %d MjCHATMEMBERACK(s) on the seated humans' behalf"
            % (peer, opcodes.opname(h["opcode"]), " from a SPECTATOR" if spectator else "",
               n, len(out)))
    else:
        wirelog.log("%s      %s%s relayed to %d table-mate(s)/spectator(s)"
            % (peer, opcodes.opname(h["opcode"]), " from a SPECTATOR" if spectator else "", n))
    return out
