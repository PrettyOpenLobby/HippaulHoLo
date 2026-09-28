"""Inbound in-game records: replay detection, the dispatch, discards, call
answers, the escape file.
"""
import os
import struct
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
from . import bots, knobs, narration


class TableInbound:
    """Part of `Table` (table.py), which inherits it.

    Inbound in-game records: replay detection, the dispatch, discards, call
    answers, the escape file.
    """

    # -- inbound -------------------------------------------------------------

    #: THE ESCAPE HATCH. Touch this file and the next inbound message ends the
    #: game.
    #:
    #: WHY IT HAS TO EXIST: the client's in-game loop only breaks when
    #: `_DAT_0042abb0` is set, and the ONLY thing that sets it is the MjGAMEEND
    #: handler (console__00287a80). So a player cannot leave a hand from their
    #: own side -- the account holder found this by trying the system menu and
    #: nothing happening -- and our manager otherwise sends MjGAMEEND only after
    #: a full hanchan. Without this, a test session ends by resetting the
    #: emulator.
    #:
    #: A FILE rather than an env var, for the same reason config/jan_deal.txt is
    #: one: `config/` is bind-mounted and this is checked per message, so it
    #: works with the client still connected and needs no restart. (An env var
    #: would need a container restart, which ends the session -- the exact thing
    #: it is trying to avoid.)
    #:
    #: Fires ONCE per touch: the mtime is adopted when the table is created, and
    #: a change from that value triggers exactly one MjGAMEEND. `config/` is
    #: mounted read-only, so the server cannot clear the file itself.
    ESCAPE_FILE = os.environ.get("POL_JAN_ESCAPE_FILE", "/config/jan_endgame.txt")

    def _escape_mtime(self):
        try:
            return os.path.getmtime(self.ESCAPE_FILE)
        except OSError:
            return None

    def _escape_requested(self):
        m = self._escape_mtime()
        if m is not None and m != self._escape_seen:
            self._escape_seen = m
            return True
        return False

    #: In-game lines whose REPLAY must be detected. The client re-sends its
    #: last message on a timer (~2 s for an ack, ~60 s for a MjSUTE), and a
    #: repeat of the last APPLIED (op, seq, word) from a seat is that
    #: retransmission -- audit finding 7: it used to land as a second discard.
    DEDUP_OPS = frozenset([M.MjSUTE, M.MjNAKIACK, M.MjHAIPAIACK,
                           M.MjYAKUDISPACK, M.MjSEISANACK,
                           M.MjGAMERESULTHALF1ACK, M.MjGAMERESULTHALF2ACK,
                           M.MjSASHIUMAREQUEST, M.MjSASHIUMAARGEE])
    #: A replayed ladder ack gets SILENCE (the ladder already moved; a re-sent
    #: deal/settlement with an older sequence would be applied as new by a
    #: client whose last record is newer -- the deal-storm class). A replayed
    #: SUTE/NAKIACK/side-bet reply re-nudges what the seat is waiting on.
    SILENT_ON_REPLAY = frozenset([M.MjHAIPAIACK, M.MjYAKUDISPACK, M.MjSEISANACK,
                                  M.MjGAMERESULTHALF1ACK, M.MjGAMERESULTHALF2ACK])

    def handle(self, rec, member=None):
        """One inbound in-game record -> the records that answer it.

        `member` is OUR account number for the sender when the transport knows
        it (the Manager always passes it). It decides the SEAT: a record whose
        stamped seat (+0x14) disagrees with the seat that member holds is
        refused and re-nudged (audit finding 10 -- a mis-saved or spoofed seat
        used to mutate another seat's hand out of turn). With no member the
        stamp is all there is, and the engine's own turn/size checks are the
        second line.

        Three guards wrap the dispatch: the replay check (finding 7), a
        `ValueError` from the engine -- which validates BEFORE it mutates --
        is a refused move that re-offers the turn, and any other exception is
        logged and answered with a resync for the sender instead of the old
        silence (finding 31).
        """
        if self.state == "playing" and (knobs.SHUTDOWN or self._escape_requested()):
            reason = ("shutdown -- ending the live game cleanly" if knobs.SHUTDOWN
                      else "the server ended the game")
            self.log.append(("escape", reason))
            self.trace("GAMEEND (%s)" % reason)
            return [self.msg_gameend()]
        h = janwire.unpack(rec)
        op = h["opcode"]
        if member and int(member) in self.gallery:
            # A SPECTATOR's +0x14 is its gallery slot, never a seat: it must
            # not fall through to the `src` fallback below (GALLERY banner).
            return self.on_gallery(rec, member)
        src = h["src"] if 0 <= h["src"] < 4 else None
        seat = self.seat_of_member(member) if member else None
        if seat is None:
            seat = src
        elif src is not None and src != seat:
            self.log.append(("seat-mismatch", (narration.M_NAME(op), src, seat)))
            self.trace("REFUSED %s stamped seat %d from the member holding seat "
                       "%d -- re-nudging" % (narration.M_NAME(op), src, seat))
            return self._renudge(seat)
        self.last_line_at = self.clock()
        if seat is not None:
            self.timeouts.pop(seat, None)       # it spoke: not a vanished client
        if op in self.DEDUP_OPS and seat is not None:
            word = struct.unpack_from("<I", rec, 0x18)[0] if len(rec) >= 0x1C else 0
            key = (op, h["f13"], word)
            if self._last_in.get(seat) == key:
                self.log.append(("replay", (seat, narration.M_NAME(op), h["f13"])))
                if op in self.SILENT_ON_REPLAY:
                    return []           # the ladder acks: silence is proven live
                return self._renudge(seat)
            self._last_in[seat] = key
        try:
            return self._dispatch(rec, h, op, seat)
        except ValueError as e:
            # The engine refused the move and touched nothing.
            self.log.append(("refused-%s" % narration.M_NAME(op)[2:].lower(),
                             (seat, str(e))))
            self.trace("REFUSED %s from %s: %s" % (narration.M_NAME(op), self._seatname(seat), e))
            return self._reoffer(seat)
        except Exception as e:
            self.log.append(("error", "%s from seat %s: %s: %s"
                             % (narration.M_NAME(op), seat, type(e).__name__, e)))
            self.trace("ERROR %s from %s: %s: %s -- resyncing the sender"
                       % (narration.M_NAME(op), self._seatname(seat), type(e).__name__, e))
            try:
                if self.kyoku is not None and self.state == "playing" and seat is not None:
                    out = [self.msg_alldata(label="resync after error", to=seat)]
                    if (self._awaiting and self._awaiting == (M.MjTSUMO, seat)
                            and self.kyoku.result is None):
                        out.append(self.msg_tsumo(seat))
                    return out
            except Exception as e2:
                self.log.append(("error", "resync itself failed: %r" % (e2,)))
            return []

    def _reoffer(self, seat):
        """After a refused move: a FRESH turn record if the seat is on turn
        (the client has left its picker and needs a new sequence to re-enter
        it -- a same-sequence re-nudge is deduped and the client would only
        re-send the refused line), else the plain re-nudge."""
        k = self.kyoku
        if (seat is not None and k is not None and k.result is None
                and self._awaiting and self._awaiting == (M.MjTSUMO, seat)
                and k.turn == seat and k.pending_kan is None):
            return [self.msg_tsumo(seat)]
        return self._renudge(seat)

    def _dispatch(self, rec, h, op, seat):
        # WARNING: A message for a game the server no longer has (2026-09-04 LIVE: a
        # deploy restarted authsess and wiped the Manager; the console kept
        # playing, its next MjSUTE built a fresh idle table via table_for and
        # CRASHED on `k.hands` -- the session try/except swallowed it, so the
        # server went silent and the console froze). Any in-game message that
        # is NOT MjREADY, arriving with no active kyoku on a table that is not
        # playing, means the client is mid-game in a hand we have forgotten.
        # Answer MjGAMEEND so it drops cleanly to the menu instead of freezing
        # -- the restart-recovery counterpart to the graceful-shutdown end
        # (msg_gameend needs only next_seq(), so it is safe with kyoku None).
        # MjREADY still starts a fresh game below.
        if op != M.MjREADY and self.kyoku is None and self.state != "playing":
            self.log.append(("orphan-ingame", narration.M_NAME(op)))
            self.trace("GAMEEND (no active game -- the server was restarted; "
                       "ending the client's ghost session cleanly)")
            return [self.msg_gameend()]
        if self.state == "finished" and op != M.MjREADY:
            # A finished table (every live seat gone) answers nothing but a
            # ghost end: the Manager is about to forget it.
            if op in (M.MjBYE, M.MjMEMBERLEAVEANSER):
                return []
            self.log.append(("finished-ingame", narration.M_NAME(op)))
            return [self._emit(M.gameend(self.next_seq()), "ghost end", to=seat)]

        if op == M.MjREADY:
            if seat is not None and seat not in self.bots:
                self.live.add(seat)
            elif seat is not None:
                # A READY stamped with a bot's seat (audit probe P5): it used
                # to make that bot a "live" seat and wait on it for ever.
                self.log.append(("ready-from-bot-seat", seat))
            if self.state != "playing":
                return self.start_game()
            # A second human's READY while the side-bet handshake is still
            # collecting picks: offer them the table too -- their client is
            # in the in-game loop now and will answer.
            if (self.sashiuma and self.sashiuma["phase"] == "request"
                    and seat is not None and seat not in self.sashiuma["started"]):
                return [self.msg_sashiuma_start(seat)]
            return []

        if op == M.MjHAIPAIACK:
            # Idempotent, same reason as the SEISANACK arm below: only the ack
            # of the deal we are actually waiting on opens the first turn. A
            # re-sent HAIPAIACK must NOT draw a second time.
            if self._awaiting and self._awaiting[0] == M.MjHAIPAI:
                # WARNING: DO NOT send an MjALLDATA resync here (2026-09-03). The
                # deal animation (MjHAIPAI motion 10) already establishes every
                # seat's hand; a resync at this point can only REBUILD all four
                # hands from wire bytes, and a concealed opponent hand has no
                # safe MjALLDATA encoding -- 0x3F (the back) froze every game at
                # the deal and locked the host (561a4fb1, reverted). Just open
                # the first turn. If a dealer-human's opponents ever look wrong
                # right after the deal, fix it in the MjHAIPAI motion-10 path,
                # never with a per-deal resync.
                return self._ack_advance(M.MjHAIPAI)
            return []

        if op == M.MjSUTE:
            return self.on_sute(rec, seat=seat)

        if op == M.MjNAKIACK:
            return self.on_nakiack(rec, seat=seat)

        if op == M.MjYAKUDISPACK:
            if self._awaiting and self._awaiting[0] == M.MjYAKUDISP:
                return self._ack_advance(M.MjYAKUDISP)
            return []

        if op == M.MjSEISANACK:
            # WARNING: THE DEAL-STORM BUG (2026-09-03, live): each ack dealt a fresh
            # hand. The client acks every MjSEISAN we send, and a won hand sent
            # several of them (the YAKUDISPACK plus every retried MjSUTE that
            # reached on_kyoku_end), so four SEISANACKs dealt four hands --
            # seen live: "the same 13th tile four times, discardable in one
            # turn" (four MjHAIPAI -> four identical opening draws). Gate on
            # `_awaiting` exactly like the GAMERESULT ack arms: the first
            # SEISANACK advances the ladder; every retry gets silence.
            # 2026-09-09: every SEISANACK (first or a later human's) also
            # feeds the client's between-hands ready gauge. It goes FIRST:
            # the ladder record stays the LAST thing in a reply, which is
            # what the transport re-nudges and what a client answers (the
            # gauge record drains on its own sub and is never acked).
            ready = self._ready_status(seat)
            if self._awaiting and self._awaiting[0] == M.MjSEISAN:
                return ready + (self._ack_advance(M.MjSEISAN) or [])
            return ready

        if op == M.MjALLDATA or op == M.MjALLDATAACK:
            # The client sends MjALLDATA to ask for a resync; the ack needs
            # nothing back.
            return [self.msg_alldata(to=seat)] if op == M.MjALLDATA else []

        # WARNING: THE ACK ARMS MUST BE IDEMPOTENT (2026-09-02, live): the client
        # re-sends an ack on a ~2s timer until satisfied, and answering EVERY
        # MjGAMERESULTHALF2ACK with a fresh MjGAMEEND re-triggered the whole
        # end-screen sequence -- seen live: each results screen played three
        # times (log 23:02:44/46/49Z: HALF2ACK -> GAMEEND f13=1,2,3, then the
        # client gave up and sent MjBYE). Gate on `_awaiting`: the first ack of
        # the record we are actually waiting on advances the ladder; a re-sent
        # ack gets silence. A record the client never got is re-sent by the
        # deadline sweeper (`tick`: once, then the ladder proceeds).
        if op == M.MjGAMERESULTHALF1ACK:
            if self._awaiting and self._awaiting[0] == M.MjGAMERESULTHALF1:
                return self._ack_advance(M.MjGAMERESULTHALF1)
            return []

        if op == M.MjGAMERESULTHALF2ACK:
            if self._awaiting and self._awaiting[0] == M.MjGAMERESULTHALF2:
                return self._ack_advance(M.MjGAMERESULTHALF2)
            return []

        if op == M.MjSASHIUMAREQUEST:
            return self.on_sashiuma_request(rec, seat=seat)

        if op == M.MjSASHIUMAARGEE:
            return self.on_sashiuma_agree(rec, seat=seat)

        if op == M.MjMEMBERLEAVEANSER:
            p = M.parse_memberleave_anser(rec)
            self.log.append(("leave-answer", (seat, p["answer"])))
            # WARNING: What a "do not continue" is supposed to DO is not established --
            # nothing in the module states whether one refusal ends the game or
            # whether all three must agree. We record it and play on, which is
            # the conservative reading; a live client that then stalls is data.
            if not p["continue"]:
                self.log.append(("leave-refused", seat))
            # No advance() here: drop_seat already settled whatever the leaver
            # owed the table, so there is nothing to run forward -- and a blind
            # advance() re-emits the current turn's draw (NOT deduped:
            # msg_tsumo takes a fresh seq), once per answering seat.
            return []

        if op == M.MjBYE:
            # After MjGAMEEND (state "over") this is the client leaving: once
            # every live seat has, the table is finished and the Manager
            # forgets it (finding 32).
            # WARNING: MID-GAME it is a QUIT, and finishing the whole table here is
            # what stranded the remaining players (seen live 2026-09-04: "it
            # doesn't seem to quite know how to handle someone leaving") --
            # state went "finished" and every later line from the OTHER human
            # got silence. While anyone live remains, the seat is substituted
            # instead: drop_seat announces MjMEMBERLEAVE, seats a bot, and
            # settles whatever the leaver was being waited on for.
            if (self.state == "playing" and seat is not None
                    and seat in self.live and len(self.live) > 1):
                return self.drop_seat(seat, "quit (MjBYE)")
            if self.state == "over":
                if seat is not None:
                    self._byes.add(seat)
                if self._byes >= self.live:
                    self._finish("every live seat said MjBYE")
                return []
            self._finish("MjBYE from seat %s" % (seat,))
            return []

        return []

    def on_sute(self, rec, seat=None):
        p = M.parse_sute(rec)
        k = self.kyoku
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else None
        # The hand is already over: this is a re-sent discard (the client
        # retries a MjSUTE it thinks went unanswered, on a ~1min timer). Running
        # the discard logic again re-emitted MjSEISAN via on_kyoku_end and fed
        # the deal-storm gated out in handle(). Re-nudge with whatever we last
        # sent -- the awaited YAKUDISP/SEISAN, which the client dedups on its
        # sequence byte -- instead of advancing the finished hand a second time.
        #
        # WARNING: `k IS None`, not just resolved: a discard can arrive with no kyoku
        # at all -- between hands, or after a restart wiped the game (the
        # 2026-09-04 crash: `k.hands` on None). Handle it exactly like a
        # resolved hand -- re-nudge or stay silent, never touch k. Mirrors
        # on_nakiack, which already guards this.
        if k is None or k.result is not None:
            return self._renudge(seat)
        # VALIDATE BEFORE MUTATE (finding 10/11): the seat must be a person,
        # on turn, holding 14 tiles, with no kan pending -- the engine raises
        # on all of these too, but naming the refusal here keeps the log
        # legible.
        if seat is None or seat not in self.live:
            self.log.append(("refused-sute", (seat, "not a live seat")))
            return self._renudge(seat)
        h = k.hands[seat]
        if k.turn != seat or h.tile_total() != 14 or k.pending_kan is not None:
            self.log.append(("refused-sute", (seat, "not this seat's move: turn=%d "
                                              "tiles=%d" % (k.turn, h.tile_total()))))
            self.trace("REFUSED discard from %s -- it is %s's move"
                       % (self._seatname(seat), self._seatname(k.turn)))
            return self._renudge(seat)
        act = p["action"]

        if act == M.SUTE_TSUMO_AGARI:
            sc = (mj.can_tsumo(h, k.context_for(seat, h.drawn, True))
                  if h.drawn is not None else None)
            if sc is None:
                # The client offered a button we do not agree with. Do NOT
                # invent a win: re-send the draw so the hand stays legal, and
                # log it -- a disagreement here is a scoring bug worth seeing.
                self.log.append(("refused-tsumo", seat))
                return [self.msg_tsumo(seat)]
            k.win_tsumo(seat)
            return self.on_win(seat, sc)

        if act == M.SUTE_KAN:
            # The cursor slot names the tile; the ENGINE says whether it may
            # be kanned right now (audit 11: a SUTE_KAN on a non-kan slot
            # used to delete that tile from the game).
            idx = p["index"]
            target = mj.kind(h.tiles[idx]) if 0 <= idx < len(h.tiles) else None
            ank, kak = k.ankan_options(seat), k.kakan_options(seat)
            if target is not None and target in ank:
                return self._do_kan(seat, "ankan", target)
            if target is not None and target in kak:
                return self._do_kan(seat, "kakan", target)
            self.log.append(("refused-kan", (seat, idx, target)))
            self.trace("REFUSED kan by %s on slot %d (%s): not a kan the engine "
                       "allows now (ankan %s, kakan %s)"
                       % (self._seatname(seat), idx,
                          narration._tile(h.tiles[idx]) if target is not None else "?",
                          [mj.TILE_NAMES[x] for x in ank],
                          [mj.TILE_NAMES[x] for x in kak]))
            return [self.msg_tsumo(seat)]

        if act == M.SUTE_OTHER:
            # Kyushu kyuhai: the only MyMove action that returns 0x40000. Honour
            # it as an abortive draw (dealer repeats, honba +1) when the hand is
            # actually eligible; otherwise it is a stray button and we re-draw.
            if (k.first_go_round and h.menzen
                    and not any(x.melds for x in k.hands)
                    and sum(1 for kk in set(mj.kind(t) for t in h.tiles)
                            if kk in mj.YAOCHUU) >= 9):
                self.trace("KYUSHU %s -> abortive draw" % self._seatname(seat))
                k.abort("kyushu")
                return self.on_kyoku_end()
            self.log.append(("unhandled-sute-action", p["word"]))
            return [self.msg_tsumo(seat)]

        idx = p["index"]
        if not (0 <= idx < len(h.tiles)):
            self.log.append(("refused-sute", (seat, "slot %d off a %d-tile hand"
                                              % (idx, len(h.tiles)))))
            return [self.msg_tsumo(seat)]
        tile = h.tiles[idx]
        if mj.kind(tile) in k.kuikae_forbidden(seat):
            self.log.append(("refused-kuikae", (seat, tile)))
            self.trace("REFUSED %s discarding %s: kuikae after its own call"
                       % (self._seatname(seat), narration._tile(tile)))
            return [self.msg_tsumo(seat)]
        if h.riichi and h.drawn is not None and mj.kind(tile) != mj.kind(h.drawn):
            self.log.append(("refused-sute", (seat, "riichi hand must discard the draw")))
            return [self.msg_tsumo(seat)]
        if act == M.SUTE_RIICHI:
            err = k.riichi_error(seat, tile)
            if err is not None:
                self.log.append(("refused-riichi", (seat, err)))
                self.trace("REFUSED riichi by %s on %s: %s"
                           % (self._seatname(seat), narration._tile(tile), err))
                return [self.msg_tsumo(seat)]
        self.trace("DISCARD %s tile=%s slot=%d%s  [client-side render -- lands "
                   "in the player's own row]"
                   % (self._seatname(seat), narration._tile(tile), idx,
                      " RIICHI" if act == M.SUTE_RIICHI else ""))
        k.discard(seat, tile=tile, riichi=(act == M.SUTE_RIICHI))
        return self._continue(tile, seat)

    def on_nakiack(self, rec, seat=None):
        k = self.kyoku
        claimed = self.pending_naki[0] if self.pending_naki is not None else None
        p = M.parse_nakiack(rec, claimed=claimed)
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else -1
        # Same idempotency guard as on_sute: a call-ack that arrives after the
        # hand has resolved is a stale retry, not a new decision. Don't mutate a
        # finished kyoku; re-nudge with the record we are waiting on.
        #
        # WARNING: k IS None, not just resolved (2026-09-03, live crash): the client
        # re-sends a NAKIACK on its own timer, and one arrived ~62s after the
        # offer -- after the hand had already ended and `kyoku` was cleared
        # (a won/drawn hand, or a game reset). The old code fell straight
        # through to `k.last_discard` and raised, and the session-level
        # try/except swallowed it as a dropped line: a whole live game
        # went silent on a stray retry. A gone hand answers like a resolved
        # one -- re-nudge or stay silent, never crash.
        if k is None or k.result is not None:
            return self._renudge(seat if seat >= 0 else None)
        # WARNING: AN ACK WITH NO LIVE OFFER IS A RETRANSMISSION, NOT A DECISION
        # (2026-09-04, live: a player chose Ron and watched a BOT win by
        # tsumo instead). `pending_naki` is consumed by the first ack, and the
        # old code then fell back to `(k.last_discard, {})` -- so a re-sent or
        # late ack was applied to WHATEVER TILE happened to be at the end of
        # the pond by then. A ron scored against a tile the player was never
        # shown does not complete their hand, so `win_ron` refused it, the
        # refusal path advanced the turn, the bots ran forward and one of them
        # tsumo'd. Their click is what handed the hand away.
        #
        # A probe over 400 dealt hands found can_ron and win_ron agreeing 90/90
        # on consistent state, so the scorer was never the problem -- the drift
        # was. Answer a dead offer the way every other stale-retry path here
        # does: mutate nothing.
        if self.pending_naki is None:
            self.log.append(("stale-nakiack", (seat, p["action_name"])))
            self.trace("CALLACK %s -> %s  [STALE: no live offer -- the board "
                       "has moved on; whatever this seat is owed is in its "
                       "outbox]"
                       % (self._seatname(seat if seat >= 0 else None),
                          p["action_name"]))
            # WARNING: NOTHING BACK, deliberately. `handle_line` drains this
            # member's outbox on EVERY line regardless of what we return, so
            # anything this seat is genuinely owed rides out on this very line
            # -- and a re-nudge of a record it already holds is noise.
            return []
        tile, options = self.pending_naki
        offer_from = (self.pending_chankan[0] if self.pending_chankan is not None
                      else k.last_discard_seat)
        action = p["action_name"]

        # --- MULTI-SEAT ARBITRATION (2026-09-04) -----------------------------
        # `pending_naki` used to be consumed by the FIRST ack: with two humans
        # offered the same discard, whoever clicked first won outright and the
        # other seat's genuine answer arrived "stale" and was dropped -- a ron
        # could lose to a faster pon. Real mahjong resolves every claim on one
        # discard together: ron beats kan/pon beats chi, and several rons
        # resolve in turn order from the discarder (win_ron applies head bump /
        # double ron itself). So an ack is now RECORDED, and the offer resolves
        # only once the seats still deciding can no longer outrank the answers
        # on file. Bots are merged in at resolution time, which also closes the
        # older asymmetry where a granted human pon never asked whether a bot
        # held the ron.
        if seat < 0:
            # An anonymous ack names no seat. The old reading was "a pass";
            # keep it, as a pass from every seat still outstanding.
            for s in options:
                self.naki_answers.setdefault(s, ("pass", None))
        elif seat not in options:
            # A NAKIACK from a seat we never offered anything. msg_naki goes to
            # EVERY live seat (the menus inside it are per-seat), so a
            # menu-less client's reflexive pass must not answer FOR the seat
            # actually deciding -- which is exactly what the old first-ack-wins
            # code let it do. Note it and keep waiting for the real answers.
            self.log.append(("unoffered-nakiack", (seat, action)))
            return []
        else:
            if action != "pass" and action not in options[seat]:
                # A button we never lit (a spoofed or stale word): a pass.
                self.log.append(("refused-nakiack", (seat, action)))
                action = "pass"
            prev = self.naki_answers.get(seat)
            self.naki_answers[seat] = (action, p["tile"])
            if prev == (action, p["tile"]):
                return []          # the client's ~2s retry of an answer we hold
            self.trace("CALLACK %s -> %s on %s"
                       % (self._seatname(seat), action, narration._tile(tile)))

        outstanding = [s for s in sorted(options) if s not in self.naki_answers]
        best = max([self.NAKI_PRIORITY.get(a, 0)
                    for a, _t in self.naki_answers.values()] or [0])
        # ">= best" holds on exactly the answers that could still matter: two
        # seats can never hold the same tile-claim on one discard (a pon wants
        # two copies of a tile with only three left; the chi seat is unique),
        # so an equal-priority conflict across seats only ever means ron-vs-ron
        # -- which head bump / double ron must see. max(best, 1) makes any
        # offered seat a blocker while nothing but passes are on file.
        blockers = [s for s in outstanding
                    if max(self.NAKI_PRIORITY.get(o, 0) for o in options[s])
                    >= max(best, 1)]
        if blockers:
            self._awaiting = (M.MjNAKI, blockers[0])
            self.trace("CALLWAIT on %s  [answers on file cannot yet outrank "
                       "every seat still deciding]"
                       % "/".join(self._seatname(s) for s in blockers))
            return []
        answers, self.naki_answers = self.naki_answers, {}
        self.pending_naki = None
        return self._resolve_naki(tile, options, answers, offer_from)

    #: Call precedence. Ron beats a tile claim; kan/pon beat chi.
    NAKI_PRIORITY = {"ron": 3, "kan": 2, "pon": 2, "chi": 1}

    def _resolve_naki(self, tile, options, answers, from_seat):
        """Resolve one CLOSED call offer: every seat's claim, together.

        Humans answered with an ack (a missing answer is a pass -- the sweeper
        fills those in); bots are asked NOW, with the same policy
        `resolve_bot_calls` uses. Merging them here is what lets a bot's ron
        outrank a human's pon, which first-ack-wins never could -- until this,
        a human call silently cancelled every bot's claim on the discard.
        """
        k = self.kyoku
        if self.pending_chankan is not None:
            return self._resolve_chankan(tile, options, answers)
        claims = []            # (seat, action, acked tile) in turn order
        for step in range(1, 4):
            s = (from_seat + step) % 4
            if s in self.live:
                act, ptile = answers.get(s, ("pass", None))
                if s in options and act != "pass":
                    claims.append((s, act, ptile))
                elif s in options and "ron" in options[s]:
                    k.note_missed_ron(s)        # declined a completing tile
                continue
            opts = self.legal_calls(s, tile, from_seat)
            if not opts:
                continue
            bot = self.bots.get(s) or bots.Bot(s)
            want = bot.calls(k, tile, opts)
            if want in ("ron", "kan", "pon", "chi"):
                claims.append((s, want, None))
        rons = [s for s, a, _t in claims if a == "ron"]
        if rons:
            # Score the tile we OFFERED, not whatever is at the end of the pond
            # now -- see the stale-ack note above and win_ron's own banner.
            wins = k.win_ron(rons, tile=tile, from_seat=from_seat)
            if wins:
                return self.on_wins(wins, ron_from=from_seat)
            if k.result is not None:
                return self.on_kyoku_end()      # sanchahou: three rons abort
            # WARNING: We offered this ron ourselves, so a refusal here is OUR bug,
            # not a player action -- and it used to be a destructive one,
            # because advancing turns our disagreement into somebody else's
            # win. Log both sides so the next occurrence is diagnosable instead
            # of just surprising.
            for s in rons:
                self.log.append(("refused-ron", (s, tile, k.last_discard,
                                                 from_seat)))
                self.trace("REFUSED ron by %s on %s (offer_from=%s, pond tail "
                           "is %s) -- WE OFFERED THIS; treating it as a pass"
                           % (self._seatname(s), narration._tile(tile), from_seat,
                              narration._tile(k.last_discard)))
        for wanted in (("kan", "pon"), ("chi",)):
            for s, act, ptile in claims:
                if act not in wanted:
                    continue
                if s in self.live:
                    return self._grant_call(s, act, ptile, tile, options,
                                            from_seat)
                res = self.bot_call(s, act, tile, from_seat)
                if res is not None:
                    return res
        k.advance()
        if k.result is not None:
            return self.on_kyoku_end()
        return self.advance()

    def _grant_call(self, seat, action, acked_tile, tile, options, from_seat):
        """Grant a LIVE seat's winning claim: form the meld, animate it, and
        hand the seat its turn. Pulled out of on_nakiack when arbitration
        landed -- the pon/chi body, including the motion/resync split, is the
        code confirmed live 2026-09-04. A refused claim (the engine disagrees)
        is a pass: the offer is closed, so the hand runs on."""
        k = self.kyoku
        h = k.hands[seat]
        # The motion animates against the board as it stands NOW -- freeze it
        # before the engine forms the meld. See call_snapshot().
        pre = self.call_snapshot()
        discarder = max(0, from_seat)
        granted = None             # the CALL_MOTION key, once the engine agrees
        try:
            if action == "kan" or (action == "pon" and acked_tile is None
                                   and "kan" in options.get(seat, {})
                                   and mj.minkan_option(h, tile)):
                # DAIMINKAN: the engine draws the rinshan tile inside; both
                # `msg_kan` record paths carry it (the resync form is the one
                # proven live for a daiminkan).
                k.call_pon(seat, kan=True)
                return self.msg_kan("minkan", seat, pre, discarder=discarder)
            if action == "pon":
                k.call_pon(seat)
                granted = "pon"
            elif action == "chi":
                # THE EXACT PAIR the client's cursor meant (audit finding 20):
                # `chi_pair_from_ack` is the client's own cursor->pair table
                # (mahdisp.c:3906). First-match in `chi_options` order took the
                # wrong tiles out of the hand whenever both shapes were held.
                pairs = mj.chi_options(h, tile)
                chosen = None
                want = (M.chi_pair_from_ack(mj.kind(tile), mj.kind(acked_tile))
                        if acked_tile is not None else None)
                if want is not None:
                    for pair in pairs:
                        if sorted(mj.kind(t) for t in pair) == sorted(want):
                            chosen = pair
                            break
                if chosen is None and acked_tile is not None:
                    for pair in pairs:
                        if any(mj.kind(t) == mj.kind(acked_tile) for t in pair):
                            chosen = pair
                            break
                chosen = chosen or (pairs[0] if pairs else None)
                if chosen is None:
                    raise ValueError("no chi pair for %s" % mj.name(tile))
                k.call_chi(seat, chosen)
                granted = "chi"
            else:
                raise ValueError("unknown call %r" % (action,))
        except ValueError as e:
            self.log.append(("refused-%s" % action, (seat, str(e))))
            self.trace("REFUSED %s by %s: %s -- treated as a pass"
                       % (action, self._seatname(seat), e))
            k.advance()
            if k.result is not None:
                return self.on_kyoku_end()
            return self.advance()

        # After a call it is this seat's turn WITHOUT a draw.
        #
        # WARNING: THE CALL IS A MOTION, NOT A RESYNC (2026-09-04). The old code
        # answered every granted call with msg_alldata() + msg_tsumo() and
        # never sent a call motion at all, so the client's call handler
        # never ran -- and that handler is what fires the banner, plays the
        # sound, flies the tiles, pops the pond tail and builds the meld.
        # A live pon happened in total silence; the meld only
        # appeared because the resync redrew the board (which is also why
        # the 09-03 "invisible meld" fix was a resync in the first place).
        #
        # A pon/chi is ONE MjALLDATA carrying motion 4/5, and nothing
        # follows it but this seat's own MjTSUMO.
        return [self.msg_call(granted, seat, pre, discarder),
                self.msg_tsumo(seat, motion=0)]
