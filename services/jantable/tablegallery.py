"""Spectators: who watches a table, the copies they are sent, and their one ack."""
import os
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
from . import narration

# --- THE GALLERY (spectators, 2026-09-04) ------------------------------------
#
# Measured from the client (SPEC-jan-spectator.md; file:line = jan-c-full):
# a spectator is a fifth, NON-PLAYING listener. It enters the table screen on
# a MjRESERVEACK result 1 to its MjGALLEYREQ (janhourou._galley), JOINs the
# table channel, fetches b/g/MJSTableInfoSub, and from then on RENDERS every
# sub-1 record it is handed exactly as a player would: MjHAIPAI copies all
# four hands unconditionally (console.c:3264-3286), MjTSUMO writes +0x2a
# into ANY seat's hand array (3374-3381), MjALLDATA sets everything
# (3755-4129). It sends back ONE ack, MjGALLEYACK (0x30, sub 6, +0x13 = the
# seq of the last record it applied, +0x14 = its gallery SLOT, +0x16 = 1; 0
# for the two READY-equivalents at entry and the BYE-equivalent after
# MjGAMEEND) after HAIPAI/YAKUDISP/SEISAN/HALF1/HALF2/GAMEEND and NOTHING
# after TSUMO/NAKI/ALLDATA/MEMBERLEAVE (console.c:2552-2609, §2 of the spec).
# It also sends MjSASHIUMAREQUEST/ARGEE if it ever sees SASHIUMASTART/SELECT
# (the send is outside the spectator gate), so it is never sent those.
#
# THE RULES THIS FILE KEEPS:
#   * `Table.gallery` = {member: slot}. Never a seat, never in `live`, never
#     in `_awaiting` / the broadcast-ack ladders: a silent spectator cannot
#     stall a hand, and its acks are advisory (`on_gallery`, no mutation).
#   * EVERY record `_emit` builds is COPIED into the spectator's outbox
#     (`("g", member)`, drained by `Manager.pending_for_member`) -- including
#     `to=seat` draws, the only way it sees a live human's hand. One copy per
#     shared seq (the per-seat deal records share one). Never through
#     janhourou's `_PUSH` (PUSH_QUEUE_MAX truncates a burst; the client never
#     re-requests). With concealment off (the default) the copies are
#     already face-up -- the intended spectator view; with CONCEAL_* on the
#     copy is rebuilt with every seat revealed (msg_haipai / msg_alldata).
#   * NOTHING is sent before the spectator's FIRST MjGALLEYACK: the client
#     drains sub 1 to empty between its MjRESERVEACK and the two entry acks
#     (lobby.c:299-309), so an earlier record is read by nobody. That first
#     ack starts the stream -- a subtype-0 MjALLDATA of the board first when
#     a hand is running (spec open question 8, inferred: `console__00284fe0`
#     resets state at MjClient start and MjALLDATA sets everything), then
#     copies of everything `_emit` builds.
#   * A spectator whose outbox passes GALLERY_OUTBOX_MAX is dropped, never
#     re-nudged (`_gallery_copy`).
#   * Membership drops on: MjGALLEYLEAVEREQ (janhourou), the f16=0 ack after
#     MjGAMEEND, a GAMEEND (the seat store, `Manager._release_galleries` --
#     the client sends NO leave on MjGAMEEND, spec §1.11), a table reap, and
#     the seat-store TTL.
#   * OPTIONAL SYNC (POL_JAN_GALLERY_SKIP, default OFF -- spec open question
#     2): when a broadcast wait completes, a server->spectator MjGALLEYACK on
#     sub 6 sets the client's skip flag (mahdisp.c:3281-3293) so its yaku /
#     seisan screen closes in step with the players'. Only to a spectator
#     whose first ack has arrived -- the LimitTimeManager thread discards
#     sub 6 at start (3281-3286). Not needed for progress: those screens
#     also close on the DAT_00445b87-second timer or a button.
GALLERY_SKIP = os.environ.get("POL_JAN_GALLERY_SKIP", "0") == "1"


class TableGallery:
    """Part of `Table` (table.py), which inherits it.

    Spectators: who watches a table, the copies they are sent, and their one
    ack.
    """

    # -- the gallery (spectators; see the GALLERY banner) ----------------------

    #: Records a spectator must NEVER get: the side-bet handshake. Its client
    #: answers SASHIUMASTART/SELECT with REQUEST/ARGEE even as a spectator
    #: (console.c:2761-2785, 2851-2874 -- the send is outside the
    #: DAT_0042aba8 gate), and it has no bet to place.
    GALLERY_NEVER = frozenset([M.MjSASHIUMASTART, M.MjSASHIUMASELECT,
                               M.MjSASHIUMARESULT])
    #: The broadcast waits whose completion may close the spectators' screen
    #: (the yaku wait mahdisp.c:479-487 and the seisan/results timers).
    GALLERY_SKIP_AFTER = frozenset([M.MjYAKUDISP, M.MjSEISAN,
                                    M.MjGAMERESULTHALF1, M.MjGAMERESULTHALF2])

    @staticmethod
    def gallery_key(member):
        """The outbox key a spectator's copies queue under -- never a seat
        number, so `take_outbox(seat)` for seats 0..3 cannot collide."""
        return ("g", int(member))

    #: A spectator whose outbox grows past this is DROPPED, not re-nudged: it
    #: has stopped draining (its socket is gone or it is stuck), and a
    #: player's records must never queue behind a dead listener.
    GALLERY_OUTBOX_MAX = int(os.environ.get("POL_JAN_GALLERY_OUTBOX_MAX", "96") or 96)

    def _gallery_copy(self, rec, h=None, alt=None):
        """Queue `alt or rec` for every LISTENING spectator -- ONE copy per
        shared seq (msg_haipai emits one record per live seat under one seq).

        Only spectators whose first MjGALLEYACK has arrived get copies: the
        client DRAINS sub 1 to empty between its MjRESERVEACK and those two
        entry acks (lobby.c:299-309), so anything queued earlier would be
        thrown away unread. A spectator that has not acked yet is handed a
        fresh board snapshot when it does (`on_gallery`)."""
        if not self.gallery:
            return 0
        h = h or janwire.unpack(rec)
        if h["opcode"] in self.GALLERY_NEVER:
            return 0
        key = (h["opcode"], h["f13"])
        if key == self._gallery_copied:
            return 0
        self._gallery_copied = key
        copy = rec if alt is None else alt
        n = 0
        for m in list(self.gallery):
            if m not in self.gallery_acked:
                continue
            gk = self.gallery_key(m)
            if len(self.outbox.get(gk) or ()) >= self.GALLERY_OUTBOX_MAX:
                self.remove_spectator(m, "evicted: %d undrained record(s)"
                                      % len(self.outbox.get(gk) or ()))
                self._gallery_evicted.append(m)
                continue
            self.queue_for(gk, copy)
            n += 1
        return n

    def add_spectator(self, member, slot):
        """A spectator enters (janhourou answered its MjGALLEYREQ with 1).
        Nothing is queued yet: the client drains sub 1 before its first
        MjGALLEYACK (lobby.c:299-309), and that ack is what starts the stream
        -- with a subtype-0 MjALLDATA of the whole board first when a hand is
        running (`on_gallery`). Returns the slot."""
        member = int(member)
        slot = int(slot) & 0xFF
        self.gallery[member] = slot
        self.gallery_acked.discard(member)
        self.gallery_seq.pop(member, None)
        self.outbox.pop(self.gallery_key(member), None)
        self.log.append(("spectator", (member, slot)))
        self.trace("SPECTATOR member %d watches from slot %d -- waiting for its "
                   "first MjGALLEYACK before anything is sent" % (member, slot))
        return slot

    def _gallery_snapshot(self, member):
        """The board for a spectator that just started listening: a subtype-0
        MjALLDATA with EVERY seat revealed (whatever CONCEAL_* says), built
        WITHOUT `_emit` so no player's deadline, route or last-record moves
        (inferred, spec open question 8: `console__00284fe0` resets state at
        MjClient start and MjALLDATA sets everything). Nothing when no hand
        is running: the next deal is the first record then."""
        if self.state != "playing" or self.kyoku is None:
            self.trace("SPECTATOR member %d is listening -- no hand running, "
                       "the next deal is its first record" % member)
            return None
        snap = self.msg_alldata(reveal=range(4), label="spectator snapshot",
                                emit=False)
        self.queue_for(self.gallery_key(member), snap)
        self.log.append(("gallery-snapshot", member))
        self.trace("SPECTATOR member %d is listening -- board snapshot queued "
                   "(MjALLDATA subtype 0, every hand face-up)" % member)
        return snap

    def remove_spectator(self, member, why="left", keep_outbox=False):
        """Forget a spectator. `keep_outbox` leaves what is queued (a GAMEEND
        copy the client has not drained yet); an explicit leave drops it."""
        member = int(member)
        if self.gallery.pop(member, None) is None:
            return False
        self.gallery_acked.discard(member)
        self.gallery_seq.pop(member, None)
        if not keep_outbox:
            self.outbox.pop(self.gallery_key(member), None)
        self.log.append(("spectator-gone", (member, why)))
        self.trace("SPECTATOR member %d %s" % (member, why))
        return True

    def on_gallery(self, rec, member):
        """A record FROM a spectator: bookkeeping only. Never a mutation of the
        game, never a reply on sub 1 (nothing a spectator sends is answered
        there -- the RESERVEACKs it waits on are janhourou's), and never the
        unknown-member ghost MjGAMEEND, which would throw it out of the
        table for a stray MjSASHIUMAREQUEST.

        MjGALLEYACK: +0x13 is the seq of the last record it applied (kept for
        the log -- correlate on seq, there is no opcode in it); f16 = 0 after
        MjGAMEEND is the BYE-equivalent (console.c:4247-4259 and the second
        one from lobby.c:326) and drops the membership; f16 = 0 before the
        end is one of the two READY-equivalents at entry (lobby.c:299-309).
        Either way the client's sub-6 reader is live from the first ack on
        (`gallery_acked`), which is what the optional skip gates on."""
        h = janwire.unpack(rec)
        op = h["opcode"]
        member = int(member)
        if op == M.MjGALLEYACK:
            self.gallery_seq[member] = h["f13"]
            if h["f16"] == 0 and self.state in ("over", "finished"):
                self.log.append(("gallery-bye", (member, h["f13"])))
                self.remove_spectator(member, "left after MjGAMEEND (GALLEYACK f16=0)")
                return []
            if member not in self.gallery_acked:
                # Its entry drain is over (lobby.c:299-309): from here every
                # record is copied, starting with the board as it stands.
                self.gallery_acked.add(member)
                self.trace("SPECTATOR member %d: first MjGALLEYACK (seq %d, "
                           "f16=%d) -- its sub-6 reader is live"
                           % (member, h["f13"], h["f16"]))
                self._gallery_snapshot(member)
            self.log.append(("gallery-ack", (member, h["f13"], h["f16"])))
            return []
        # A stray SASHIUMAREQUEST/ARGEE, an MjALLDATA request, anything else:
        # logged and ignored -- a spectator moves nothing.
        self.log.append(("gallery-ignored", (member, narration.M_NAME(op))))
        self.trace("SPECTATOR member %d sent %s -- ignored (no mutation, no "
                   "reply)" % (member, narration.M_NAME(op)))
        return []

    def gallery_skip_record(self, slot=0):
        """The server->spectator MjGALLEYACK on sub 6 (mahdisp.c:3286-3293:
        a sub-6 record whose +0x12 == 0x30 sets `_DAT_00446d40`, "Skip
        Request!!"). Header only, 0x18 bytes; +0x14 carries the slot the way
        the client's own copy does."""
        return janwire.pack(opcode=M.MjGALLEYACK, f13=0, src=slot & 0xFF, dst=4,
                            f16=1, sub=6, length=0x18)[:0x18]

    def _gallery_skip(self):
        """POL_JAN_GALLERY_SKIP: after a broadcast wait completes, queue the
        skip record for every spectator whose first ack has arrived (the
        LimitTimeManager discards sub 6 at thread start). Off by default --
        spec open question 2."""
        if not GALLERY_SKIP or not self.gallery:
            return 0
        n = 0
        for m, slot in list(self.gallery.items()):
            if m not in self.gallery_acked:
                continue
            self.queue_for(self.gallery_key(m), self.gallery_skip_record(slot))
            n += 1
        if n:
            self.log.append(("gallery-skip", n))
        return n
