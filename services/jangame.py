#!/usr/bin/env python3
"""Janhourou's TABLE AND GAME MANAGER -- the `Tgm` half of the world server.

This is the piece that makes `janhourou.py` stop being a responder. It owns a
table's seats, its rule set, the hand in progress and the sequence discipline,
and it turns one inbound record into the list of records that answer it.

    janhourou.py   the wire: sessions, lines, the opcodes with a measured ACK
    janmsgs.py     the in-game message bodies and the tile codec
    janmahjong.py  the rules: wall, hands, calls, yaku, fu, payment
    jangame.py     THIS -- who is sitting where, whose turn it is, what to send

=== WHY A PURE REQUEST/RESPONSE DRIVE IS ENOUGH ============================

The obvious worry with a game server behind a NOTICE carrier is that it has no
way to speak unprompted. It turns out not to need one: **the client acks
everything.** `console__00284380` sends `opcode + 1` back for MjHAIPAI,
MjYAKUDISP, MjSEISAN, MjGAMEEND and both GAMERESULT halves, and it answers
MjTSUMO with MjSUTE and MjNAKI with MjNAKIACK. So every message we send buys a
message back, and the whole hand cycle can be driven as replies:

    MjREADY      -> MjHAIPAI
    MjHAIPAIACK  -> MjTSUMO (or the bots play until a live seat is involved)
    MjSUTE       -> MjNAKI / the next MjTSUMO / MjYAKUDISP
    MjNAKIACK    -> whatever the call turned into
    MjYAKUDISPACK-> MjSEISAN
    MjSEISANACK  -> the next MjHAIPAI, or the end-of-game sequence
    HALF1ACK     -> MjGAMERESULTHALF2      HALF2ACK -> MjGAMEEND

One inbound line therefore yields SEVERAL outbound ones whenever the three
non-human seats have moves to make in between -- which is why `handle()` returns
a list and why `_game_notice_reply` had to learn to send more than one.

WARNING: WHAT THIS COSTS: a table with more than one live client needs real pushes (two
humans can be waiting on each other with nobody's ack outstanding). The seat
model below is already per-seat, so that is a transport change, not a redesign --
see `Table.pending_for()`, which is where a push scheduler would read from.

=== MEASURED vs INFERRED ==================================================

MEASURED (in janmsgs.py, off the client's own handlers): every field offset,
the in-game sub-code 1, the sequence/dedup rule, `ack = opcode + 1`, the discard
and call word encodings, and the wall starting at 70.

INFERRED (here): the ORDER of the conversation. Nothing in the module states
that HAIPAI follows READY or that SEISAN follows YAKUDISP -- the opcode names and
the phase ids each dispatch arm sets are strong evidence, but a live client
disagreeing is data, not a bug to paper over. Every step logs what it sent and
why, so a stall names its own message.

    python jangame.py --selftest       # a whole hanchan, no client, no socket
    python jangame.py --trace          # ...printing every record as it goes
"""
import argparse
import os
import random
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janwire                                                      # noqa: E402
import janmsgs as M                                                 # noqa: E402
import janmahjong as mj                                             # noqa: E402
try:
    # THE RULES THE MASTER CHOSE (audit finding 2): `janrules.rules_for(tid)`
    # is what `Manager.table_for_lobby` passes as `rules=`. Optional like
    # janstats: absent = `mj.Rules()` defaults, exactly the old behaviour.
    import janrules                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janrules = None
try:
    # THE SEAT STORE, for one seam only: `janseats.rejoin_pending(member,
    # clear=True)` -- a member re-entering a playing table is re-seated and
    # resynced on their next in-game line (finding 29).
    import janseats                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janseats = None
try:
    # THE PLAYER RECORD. Optional in the same way janhourou.py treats this
    # module: absent = the old behaviour, which is that a finished hanchan is
    # computed and then dropped on the floor. The client cannot write its own
    # save (`sqMgWriteFileCheck` has 0 callers in JanHouRou.pex), so if we do
    # not write the result down here, nothing anywhere does.
    import janstats                                                 # noqa: E402
except ImportError:                                                 # pragma: no cover
    janstats = None

GAME_ENABLE = os.environ.get("POL_JAN_GAME", "1") == "1"
#: 0 turns persistence off without touching the game path -- the results screen
#: then draws row 0 and four NO_RANK rows, which is the honest "no history".
STATS_ENABLE = os.environ.get("POL_JAN_STATS", "1") == "1"
BOT_POLICY = os.environ.get("POL_JAN_BOT", "shanten")   # shanten | tsumogiri
GAME_SEED = os.environ.get("POL_JAN_SEED")              # set for reproducibility
# The +0x23..+0x26 IsHuman bytes in every in-game record AND the MjREADYSTATUS
# that a human seat's between-hands gauge then waits for (janmsgs banners,
# 2026-09-09). "0" = the pre-09-09 wire: every seat a COM to the client.
ISHUMAN_ENABLE = os.environ.get("POL_JAN_ISHUMAN", "1") == "1"
#: Riichi tsumogiri: MjTSUMO +0x63 makes the client discard the drawn tile with
#: no input once the +0x4b riichi lock is on. "0" withdraws it with no deploy --
#: the turn then waits for the player to confirm the (already locked) slot.
RIICHI_AUTO = os.environ.get("POL_JAN_RIICHI_AUTO", "1") == "1"
#: The client only writes a seat's pond tile array (DAT_00451d50) for the LOCAL
#: player -- console__MjClient_Tsumo's fill is gated on `record[+0x28] ==
#: my_seat` (console.c:3384) -- and MjALLDATA is the one message that writes all
#: four ponds (every other DAT_00451d50 reference in the client is a read or the
#: round-reset clear). The discard ANIMATION also derives its landing spot from
#: that array: yamaguchi__002e73e0 -> 002eb150 -> mahdisp__002bfb30 walks the
#: seat's pond entries and targets the LAST occupied slot, and never writes the
#: output at all over an empty pond (the stale stack value lands at the screen
#: centre -- positions are in -160/-120 centred space). So the pond MUST already
#: contain the discard before its motion-3 flies.
#:
#: HOW to get it there decides whether the tile flashes settled first:
#:   "subtype3" (default) -- ONE MjALLDATA subtype 3 (draw+prep). The client
#:     pops the tile onto the seat's hand AND clears the settled flag of that
#:     seat's last pond tile, IN THE HANDLER, before any frame renders -- the
#:     same populate-then-animate order the local discard uses -- so the
#:     motion-3 sent right after flies with NO settled pre-draw. No separate
#:     draw record; subtype-3's pop IS the draw. A bot that CALLED has no draw
#:     beat: `_show_discard` sends the subtype-3 without one (the client's pop
#:     arm is gated off after a call motion) and the fly form after a kan.
#:   "fly" -- a plain subtype-0 MjALLDATA (which force-sets every pond flag to
#:     1) then motion-3. Accurate landing but the discard is drawn settled for
#:     the frames until motion-3 clears it => the "slingshot" seen live.
#:   "appear" -- subtype-0 MjALLDATA only, no motion-3: the discard just shows
#:     up in the pond, no fly.
DISCARD_ANIM = os.environ.get("POL_JAN_DISCARD_ANIM", "subtype3")

#: Hold the deal this many seconds after MjREADY before sending MjHAIPAI.
#: WARNING:KEY: THE BOARD-ZOOM FIX (2026-09-03, proven by Deck-vs-desktop savestate
#: compare). At board entry the client's MjClient task starts a ~0.55s camera
#: zoom-IN (projection scale obj+0xC0 ramps 512->2048, wide->close); the deal
#: handler PARKS the camera the instant it runs, freezing the zoom wherever it
#: got to. We answered MjREADY with MjHAIPAI instantly, so on fast hardware the
#: deal beat the zoom -> frozen half-done = "zoomed out" (obj+0xC0=1210); the
#: slower Steam Deck let the zoom finish first = correct (obj+0xC0=2038). It is
#: a timing RACE we lost by dealing too fast. Holding the deal ~0.7s lets the
#: zoom-in complete first, every time, regardless of client speed -- matching
#: how the real service's timing/latency behaved. NOT a client patch: pure
#: server timing. Set 0 to disable.
DEAL_DELAY = float(os.environ.get("POL_JAN_DEAL_DELAY", "0.7"))

#: Conceal non-local seats' HAND tiles (deal + every resync). DEFAULT OFF.
#:
#: WARNING:KEY: CONCEALMENT IS DISABLED because in THIS engine it means INVISIBLE, not
#: "backs" -- PROVEN in live RAM 2026-09-04. A PCSX2 savestate taken on the
#: local player's turn showed seats 1/2/3 holding 13 tiles each with bit 0x80 set
#: and their 2D hand window (`g_p2DTehaiWindow`) completely empty. The static
#: hand renderer `mahdisp__002c0f30` draws a tile's FACE when 0x80 is clear and
#: draws NOTHING when it is set -- no back branch, confirmed by four reads
#: (mahdisp.c:2662-2696) -- and there is no per-tile 3D back model (the back id
#: 0x3F walked null geometry and FROZE the host, 561a4fb1/reverted). So OR-ing
#: 0x80 (the 314cb2f2 deal "concealment") makes opponents VANISH, which is
#: exactly the live symptom "all their tiles are missing". Blanking to id 0 does
#: the same. Neither shows a back.
#:
#: An MjALLDATA hand byte cannot even encode face-down: the parser
#: (console.c:3849) maps a byte's 0x80 to u16 bit 0x8000 (the RED-FIVE sheet),
#: never to bit 0x80. So a resync can only send opponents FACE-UP (visible) or
#: EMPTY (invisible) -- there is no third state that reads as a back.
#:
#: With concealment OFF, opponents are sent FACE-UP everywhere, so they render
#: CONSISTENTLY (no vanish at the deal, no "reappear after the first move", no
#: last-discarder-only flicker). The cost is that a determined player can read a
#: bot's hand -- acceptable vs bots and consistent, which is the behaviour
#: actually wanted. WARNING: Drawing opponents as real BACKS is a separate,
#: unsolved RENDER problem: this engine's opponent-back path is NOT the hand
#: array, and where it lives is not yet found. Do not re-enable this expecting
#: backs -- it only hides. Set "1" to restore the (invisible) concealment.
CONCEAL_RESYNC = os.environ.get("POL_JAN_CONCEAL_RESYNC", "0") == "1"
#: Same switch governs the DEAL: with concealment off, msg_haipai sends
#: opponents face-up too, so the deal matches play.
CONCEAL_DEAL = CONCEAL_RESYNC
#: `POL_JAN_KAN_MOTION=1` drives a granted kan as the client's own motions
#: (6 daiminkan / 7 ankan / 8 kakan on ONE MjALLDATA whose +0x1c carries the
#: rinshan tile -- the handler plays the kan, flips the kan-dora and flies the
#: rinshan draw itself -- then MjTSUMO motion 0). WARNING: DEFAULT OFF: the path is
#: built from the C (janmsgs' CALL MOTIONS + WAMPAI banners) and
#: selftest-proven, but has NEVER been screen-proved, and the last unproved
#: animation path that went live (the 0x3F back id) froze the host. Off = the
#: resync path that is live today: MjALLDATA subtype 0 + MjTSUMO motion 2.
#: `msg_kan()` serves both.
KAN_MOTION = os.environ.get("POL_JAN_KAN_MOTION", "0") == "1"
#: `POL_JAN_WANPAI_LEGACY=1` sends the dead wall in the pre-2026-09-04 wire
#: form (indicator at slot 12, the revealed bit inverted -- janmsgs' WAMPAI
#: banner has the re-measurement). A rollback lever only: the new form is the
#: one every read site in the C agrees on, but it has not been screen-proved.
WANPAI_LEGACY = os.environ.get("POL_JAN_WANPAI_LEGACY", "0") == "1"

#: THE DEADLINE SWEEPER (audit finding 8). Until 2026-09-04 `Manager.tick`
#: had no caller in the process that holds the games: the auth band's Manager
#: was swept only by the NEXT inbound line, and a table where every human was
#: waiting on us never got one. `Manager.ensure_sweeper()` now starts a daemon
#: thread (from the first `handle()`) that ticks every table under the Manager
#: lock every POL_JAN_SWEEP_S seconds and queues what it produces into the
#: seats' outboxes -- the delivery path 8deee0f8 proved (`handle_line` drains
#: them, the `<DR>` poll drains them, and responders' idle push carries them
#: to a client that says nothing). `POL_JAN_SWEEPER=0` keeps the old
#: line-driven sweep only. The selftest never starts the thread.
SWEEPER = os.environ.get("POL_JAN_SWEEPER", "1") == "1"
SWEEP_S = float(os.environ.get("POL_JAN_SWEEP_S", "1.0") or 1.0)

#: WEB WATCHING (decided 2026-09-12): the public state of every table whose
#: creator allowed watchers, for jan.example.com's delayed view
#: (services/boardjan.py `Watch`, 30 s behind the table). The Manager writes it
#: under its lock after every mutation (`handle`) and every sweep (`tick`), but
#: only when something changed or WATCH_EVERY_S has passed (the board reads a
#: stale stamp as "the server is gone"). `Manager.watchable(lobby key)` is
#: janhourou's rule -- the creator's own GALLEY_LIMIT, no password; without a
#: rule nothing is watchable. POL_JAN_WATCH_FILE=0 turns the file off.
WATCH_FILE = os.environ.get("POL_JAN_WATCH_FILE", "jan-tables-live.json").strip()
WATCH_EVERY_S = 5.0
#: how long a FINISHED game's last frame stays in the file (the table itself
#: is forgotten at once): long enough for the board's 1 s sampler to see WHY
#: it stopped -- the last human left -- instead of a table that just vanishes
WATCH_FINAL_S = 15.0


def _web_tile(t):
    """A tile for the web board: '1m'..'9m', '1p'.., '1s'.., honours '1z'..'7z'
    (E S W N haku hatsu chun), a red five '0m'/'0p'/'0s'; None for no tile."""
    if t is None or t < 0:
        return None
    k = mj.kind(t)
    if k >= 27:
        return "%dz" % (k - 26)
    return "%d%s" % (0 if mj.is_red(t) else k % 9 + 1, "mps"[k // 9])
#: A seat that strikes the deadline this many times IN A ROW is a client that
#: is gone, not one that is slow: it is dropped (MjMEMBERLEAVE to the others,
#: a bot plays on) instead of being auto-played for ever (finding 31/33).
TIMEOUTS_TO_DROP = int(os.environ.get("POL_JAN_TIMEOUTS_TO_DROP", "3") or 3)
#: `Table.log` is a ring: ~1.5k entries per hanchan for the life of the
#: process was finding 32.
LOG_MAX = int(os.environ.get("POL_JAN_LOG_MAX", "2000") or 2000)
#: A table nobody has spoken to for this long is forgotten by the Manager
#: (finished tables go sooner: after MjGAMEEND and every live seat's MjBYE).
TABLE_TTL = float(os.environ.get("POL_JAN_TABLE_TTL", "3600") or 3600)
#: How long a FINISHED table is held while a human who has not said MjBYE still
#: has records queued for it. `Manager._reap` says why. 0 = the old behaviour.
FINISHED_GRACE = float(os.environ.get("POL_JAN_FINISHED_GRACE", "120") or 0)

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

#: WARNING: GRACEFUL SHUTDOWN. Set by responders.py's SIGTERM handler when the
#: authsess container is stopping (a deploy, a manual restart). While set, the
#: NEXT in-game line from any playing table is answered with MjGAMEEND -- and
#: the deadline sweeper's next tick queues one for every playing table whose
#: clients are silent (a client waiting for its turn sends NOTHING; only one
#: on an ack screen re-acks, ~2 s) -- so the client drops cleanly back to the
#: menu instead of freezing on a socket that is about to close under it, the
#: "server went down and the game froze" report. Docker gives ~10 s of
#: SIGTERM grace, so a live table gets several chances to receive the end.
#: This is the same mechanism as the per-table escape hatch, fired process-wide
#: instead of per file-touch. Not a substitute for RESUMING a game across a
#: restart (that wants the Manager's state persisted); it just makes the
#: unavoidable restart a clean exit rather than a hang.
SHUTDOWN = False

def request_shutdown():
    """Called from the SIGTERM handler; ends every live game on its next line."""
    global SHUTDOWN
    SHUTDOWN = True

# The client's in-game loop sets DAT_00409b00 to a phase id in every arm; we
# keep the same names so a log line here can be lined up against one from the
# console's own tracing.
PHASE = {0x22: 4, 0x24: 5, 0x26: 6, 0x2E: 7, 0x3C: 8, 0x28: 9, 0x2A: 10,
         0x38: 12, 0x3A: 13, 0x2C: 14}

#: Human-readable per-move narration. janhourou.py points this at its log() so
#: the trace lands in janhourou.log alongside the wire records -- so a screen
#: event can be lined up against BOTH the raw record and a plain-English move.
#: Left None in the selftests (they assert on state, not prose) and gated by
#: POL_JAN_TRACE so it can be turned off without a code change.
TRACE = None
TRACE_ENABLE = os.environ.get("POL_JAN_TRACE", "1") == "1"


def _trace(msg):
    if TRACE is not None and TRACE_ENABLE:
        try:
            TRACE("GAME  " + msg)
        except Exception:
            pass


#: The four seat positions as the LOCAL player sees them, so a trace line names
#: where a tile should land on screen. Filled per-table from `self.live` (the
#: human is the live seat); a spectator or an all-bot table just reads "seatN".
_SEATPOS = {0: "bottom/self", 1: "right", 2: "across", 3: "left"}


def _round_p(x):
    """A P value (the +/-45 result point) as the INTEGER the results screen
    draws. Half away from zero, so +4.5 -> 5 and -4.5 -> -5; `round()` would
    make both 4 and turn a symmetric pair of results into an asymmetric one.
    The columns are u32-packed ints (MjGAMERESULTHALF1) and the screen's digit
    renderer has no decimal place -- see msg_results."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _tile(t):
    """A tile id as a short readable name, e.g. '3m', '0p' (red 5p), 'E', 'back'."""
    if t is None:
        return "--"
    try:
        return mj.name(t)
    except Exception:
        return "?%02x" % (t & 0xFF)


# --- bots --------------------------------------------------------------------

#: SASHIUMA -- the finishing-rank side-bet, entirely server-driven (janmsgs'
#: MjSASHIUMA banner). After every seat is READY and BEFORE the first deal each
#: human is offered the table (MjSASHIUMASTART), picks one opponent
#: (MjSASHIUMAREQUEST), sees the merged proposals (MjSASHIUMASELECT), agrees
#: (MjSASHIUMAARGEE) and is told which bets are ON (MjSASHIUMARESULT); the deal
#: rides in the same batch as the RESULT. The client draws the ON pairs as lines
#: on the table and computes NO money: at the end the pair's worse-placed seat
#: pays the better-placed one the stake, in result points (thousands), applied
#: to the MjGAMERESULT rows. `POL_JAN_SASHIUMA=1` turns the handshake ON.
#: WARNING: OFF BY DEFAULT UNTIL CONFIRMED LIVE (2026-09-04 audit): the first cut
#: defaulted ON with the knob unset, was pushed at 23:31Z, and pol-git-sync
#: deployed a never-exercised five-message handshake that holds EVERY deal
#: on prod. A feature with zero live instances does not get to sit in front
#: of the deal by default -- nothing counts as fixed without live proof.
_sv = os.environ.get("POL_JAN_SASHIUMA", "").strip().lower()
SASHIUMA = _sv in ("1", "on", "yes", "true")
#: The stake per bet in result points; 0 = the table uma's top value (20).
SASHIUMA_UMA = float(os.environ.get("POL_JAN_SASHIUMA_UMA", "").strip() or 0)
#: A bot: "accept" = agree to any bet proposed to it, propose none (default);
#: "propose" = also propose one bet of its own; "refuse" = never bet.
SASHIUMA_BOTS = (os.environ.get("POL_JAN_SASHIUMA_BOTS", "").strip().lower()
                 or "accept")


import collections  # noqa: E402  (local: keeps the import block above untouched)


class _Log(collections.deque):
    """`Table.log`: a bounded deque that still slices like the list it was
    (`t.log[-4:]` is how every selftest and probe reads it)."""

    def __getitem__(self, i):
        if isinstance(i, slice):
            return list(self)[i]
        return collections.deque.__getitem__(self, i)


#: The round state every builder reads (see Table._round_scalars).
RoundScalars = collections.namedtuple(
    "RoundScalars", "chiicha dealer round_wind kyoku honba riichi_sticks dice "
                    "current_seat")


class CallSnapshot(tuple):
    """`(hands, ponds)` frozen before a call, plus the dead-wall state a kan
    record needs. Unpacks as a 2-tuple, so existing callers are untouched."""

    def __new__(cls, hands, ponds, dora_shown=1, kans=0):
        return tuple.__new__(cls, (hands, ponds))

    def __init__(self, hands, ponds, dora_shown=1, kans=0):
        self._extra = (dora_shown, kans)

    @property
    def dora_shown(self):
        return self._extra[0]

    @property
    def kans(self):
        return self._extra[1]


class Bot(object):
    """A seat with no client behind it.

    Deliberately simple and deliberately HONEST: it wins when it can win, and
    otherwise it discards the tile that leaves it closest to tenpai. It never
    calls, which is legal play and keeps the meld path off the critical path
    until a live client has exercised it. `POL_JAN_BOT=tsumogiri` turns even the
    shanten search off, which matters if a slow host makes turns crawl.
    """

    #: How many terminal/honour TILES a hand may still be holding and still be
    #: called a plausible tanyao. They have to be discarded before the hand can
    #: win, and there are only so many turns left to do it in.
    TANYAO_SHED = 2

    #: How many terminal/honour tiles the concealed rest must already hold for
    #: a terminal-bearing call to read as chanta rather than an accident.
    CHANTA_LEAN = 4

    #: Pairs left in hand before a pon reads as a toitoi plan.
    TOITOI_PAIRS = 2

    def __init__(self, seat, policy=BOT_POLICY):
        self.seat = seat
        self.policy = policy

    def choose_discard(self, kyoku):
        """The tile to discard from a 14-tile (or post-call 11/8/5-tile) hand.

        2026-09-04 (audit, rules engine 3): a riichi hand discards what it
        drew, always; after a call there is no drawn tile and the hand is
        searched exactly like any other (it used to fall back to `tiles[-1]`
        -- the highest honour, whatever it did to the hand); and the kinds
        `kuikae_forbidden` names after this seat's own chi/pon are never
        chosen (the engine would refuse them and the bot would stall).
        """
        h = kyoku.hands[self.seat]
        if h.riichi and h.drawn in h.tiles:
            return h.drawn
        if self.policy == "tsumogiri" and h.drawn in h.tiles:
            return h.drawn
        try:
            forbidden = set(kyoku.kuikae_forbidden(self.seat))
        except Exception:
            forbidden = set()
        best, best_key = None, None
        seen = set()
        for t in h.tiles:
            k = mj.kind(t)
            if k in seen or k in forbidden:
                continue
            seen.add(k)
            c = h.counts()
            c[k] -= 1
            key = (mj.shanten(c, len(h.melds)), 0 if mj.is_red(t) else 1)
            if best_key is None or key < best_key:
                best, best_key = t, key
        if best is None:                    # every kind forbidden: cannot happen
            best = next((t for t in h.tiles if mj.kind(t) not in forbidden),
                        h.tiles[-1])
        return best

    def wants_kan(self, kyoku):
        """A concealed or added kan on this bot's own turn: ("ankan"|"kakan",
        kind) or None. Conservative: an ankan only when it does not push the
        hand further from tenpai (the engine has already checked a riichi
        ankan leaves the waits alone), a kakan only on the 4th tile just
        DRAWN. `kyoku.ankan_options/kakan_options` are the source of truth
        (turn, draw, 5th kan, dead wall, riichi)."""
        if self.policy == "tsumogiri":
            return None
        h = kyoku.hands[self.seat]
        if h.drawn is None:
            return None
        before = mj.shanten(h.counts(), len(h.melds))
        for k in kyoku.ankan_options(self.seat):
            c = h.counts()
            c[k] -= 4
            if h.riichi or mj.shanten(c, len(h.melds) + 1) <= before:
                return ("ankan", k)
        for k in kyoku.kakan_options(self.seat):
            if mj.kind(h.drawn) == k:
                return ("kakan", k)
        return None

    def wants_riichi(self, kyoku):
        h = kyoku.hands[self.seat]
        if h.riichi or not h.menzen or kyoku.scores[self.seat] < 1000:
            return False
        return kyoku.wall.remaining > 4 and mj.shanten(h.counts(), 0) <= 0

    def calls(self, kyoku, tile, options):
        """Which call to take on `tile`, or None.

        Until 2026-09-04 this said "ron or nothing" and had no callers at all,
        so a CPU never pon'd or chi'd in any hand -- it conceded every discard
        and made a solo game feel oddly passive (seen live: "they never
        chi or ponned which is just... odd?"). It also meant the call
        animations only ever fired when the human called.

        The judgement below is deliberately conservative. It is trying to be an
        opponent that PLAYS, not one that plays well.

        A DAIMINKAN is taken (2026-09-04) when the hand holds the triplet,
        the kan does not push it further from tenpai, and an open hand still
        has a yaku path -- the same tests a pon passes, with "not worse"
        instead of "closer" because the fourth tile adds nothing to shape.
        `Table.legal_calls` only offers "kan" while the dead wall and the
        kan count allow it. Both kan record paths (`msg_kan`) serve it.
        """
        if "ron" in options:
            return "ron"
        if self.policy == "tsumogiri":
            return None
        h = kyoku.hands[self.seat]
        if "kan" in options:
            take = mj.minkan_option(h, tile)
            if take and self._worth_calling(kyoku, h, tile, take, "pon",
                                            allow_equal=True):
                return "kan"
        # Rule precedence, and it is not cosmetic: a pon may be claimed by any
        # seat and beats a chi, which only the discarder's left neighbour can
        # make at all.
        for name in ("pon", "chi"):
            if name not in options:
                continue
            take = self._take_for(h, tile, name)
            if take and self._worth_calling(kyoku, h, tile, take, name):
                return name
        return None

    def _take_for(self, hand, tile, name):
        """The tiles this call would spend out of the hand, or None."""
        if name == "pon":
            return mj.pon_option(hand, tile)
        pairs = mj.chi_options(hand, tile)
        if not pairs:
            return None
        # Prefer the chi that leaves the hand closest to tenpai.
        best, best_key = None, None
        for pair in pairs:
            c = hand.counts()
            for t in pair:
                c[mj.kind(t)] -= 1
            key = mj.shanten(c, len(hand.melds) + 1)
            if best_key is None or key < best_key:
                best, best_key = pair, key
        return best

    def _worth_calling(self, kyoku, hand, tile, take, name, allow_equal=False):
        """Two tests: does it get us closer, and can the hand still score?"""
        before = mj.shanten(hand.counts(), len(hand.melds))
        c = hand.counts()
        for t in take:
            c[mj.kind(t)] -= 1
        after = mj.shanten(c, len(hand.melds) + 1)
        if after > before or (after == before and not allow_equal):
            return False                 # a call that does not advance is noise
        # WARNING: AN OPEN HAND WITH NO YAKU CAN NEVER WIN. Calling into one is how a
        # bot bricks itself for the rest of the hand -- it reaches tenpai and
        # then simply cannot claim anything. A closed hand may only be opened
        # if something will still score it.
        if hand.menzen and not self._open_yaku_path(kyoku, hand, tile, take, c,
                                                    name):
            return False
        return True

    def _open_yaku_path(self, kyoku, hand, tile, take, after_counts, name):
        """Is there still a yaku once this hand is open? Conservative: it names
        the three that survive opening and are cheap to test."""
        kinds = set(k for k, n in enumerate(after_counts) if n)
        kinds |= set(mj.kind(t) for t in take) | {mj.kind(tile)}
        for m in hand.melds:
            kinds |= set(mj.kind(t) for t in m.tiles)
        # Yakuhai: a triplet of a dragon, or of our own or the round's wind.
        if name == "pon":
            k = mj.kind(tile)
            if k in mj.DRAGONS or k in (kyoku.seat_wind(self.seat),
                                        kyoku.round_wind):
                return True
        for m in hand.melds:
            mk = set(mj.kind(t) for t in m.tiles)
            if len(mk) == 1:
                k = next(iter(mk))
                if k in mj.DRAGONS or k in (kyoku.seat_wind(self.seat),
                                            kyoku.round_wind):
                    return True
        # Tanyao. NOT "no terminal anywhere" -- that was the first cut and it
        # rejected 241 of 241 chi, because a 13-tile hand almost always holds a
        # terminal SOMEWHERE and tanyao is about the hand you finish with, not
        # the one you are holding. What actually kills tanyao is a MELD
        # containing a terminal or honour, since a meld can never be discarded.
        # The rest is only a question of how much you still have to shed.
        called = set(mj.kind(t) for t in take) | {mj.kind(tile)}
        for m in hand.melds:
            called |= set(mj.kind(t) for t in m.tiles)
        if not (called & mj.YAOCHUU):
            spare = sum(n for k, n in enumerate(after_counts)
                        if n and k in mj.YAOCHUU)
            if spare <= self.TANYAO_SHED:
                return True
        # Honitsu / chinitsu: one suit, plus honours.
        if len(set(k // 9 for k in kinds if k < mj.HONOR)) <= 1:
            return True
        # Chanta / junchan -- the MIRROR of tanyao, and the reason the first cut
        # rejected 211 of 214 improving chi: a 1-2-3 or 7-8-9 run is exactly
        # what this yaku is made of, and the terminal that disqualifies tanyao
        # is what QUALIFIES this. Every set must contain a yaochuu, and the
        # concealed rest has to already lean that way.
        if all(set(mj.kind(t) for t in m.tiles) & mj.YAOCHUU
               for m in hand.melds) and (called & mj.YAOCHUU):
            if sum(n for k, n in enumerate(after_counts)
                   if n and k in mj.YAOCHUU) >= self.CHANTA_LEAN:
                return True
        # Toitoi: all triplets. Only a pon can build it, and only if nothing
        # already called is a run.
        if name == "pon" and all(len(set(mj.kind(t) for t in m.tiles)) == 1
                                 for m in hand.melds):
            pairs = sum(1 for n in after_counts if n >= 2)
            if pairs >= self.TOITOI_PAIRS:
                return True
        return False


# --- one table ---------------------------------------------------------------

class Table(object):
    """A mahjong table: four seats, a rule set, and the hand in progress."""

    def __init__(self, table_id=1, channel=b"#MJS0R001", rules=None, seed=None):
        self.id = table_id
        self.channel = channel
        self.rules = rules or mj.Rules()
        self.seats = [None, None, None, None]      # member id per seat, or None
        self.nicks = [None] * 4
        self.live = set()                          # seats with a real client
        self.bots = {}
        self._seisan_ready = set()   # human seats that acked the current MjSEISAN
        self.game = None
        self.kyoku = None
        self.state = "idle"
        self._seq = 0
        self._last_sent = None
        self.sashiuma = None                       # pre-game side-bet handshake
        self.sashiuma_pairs = []                   # the (a, b) bets that are ON
        self._outgoing = []            # [(rec, seat or None)] for this call
        self.outbox = {}               # seat -> [rec] waiting for it to speak
        self.members = {}              # our member id -> seat (routing)
        self._awaiting = None
        self._awaiting_since = None     # monotonic seconds, set when we send
        self.dropped = set()            # seats we have given up waiting on
        self._escape_seen = self._escape_mtime()
        self.rng = random.Random(seed if seed is not None else
                                 (int(GAME_SEED) if GAME_SEED else None))
        # Injectable so the deadline can be tested without sleeping through it.
        self.clock = time.monotonic
        self.log = _Log(maxlen=LOG_MAX)
        self.pending_naki = None       # (tile, {seat: options}) while a call is open
        self.naki_answers = {}         # seat -> (action, acked tile) collected so far
        #: DOUBLE RON: the winners after the first, waiting for their own win
        #: screen. MjYAKUDISP names ONE winner, so a two-seat ron has to be
        #: shown as two screens -- see on_wins().
        self._pending_wins = []        # [(seat, Score)] still to show
        self._pending_ron_from = None  # the discarder those screens name
        self._bot_call_depth = 0       # guards a chain of bot calls
        self._recorded = False         # this hanchan has been written down
        # --- 2026-09-04 (audit 6/7/8/29/31/32) ---------------------------------
        self.timeouts = {}             # seat -> consecutive deadline strikes
        self._last_in = {}             # seat -> (op, seq, word) of the last line APPLIED
        self._last_for = {}            # seat (or None) -> the last record sent to it
        self._awaiting_tag = None      # the `_awaiting` tuple `_awaiting_recs` describes
        self._awaiting_recs = []       # [(rec, to)] the current `_awaiting` waits on
        self._resent_for = None        # (awaiting, since) the sweeper re-sent once
        self._byes = set()             # live seats that answered MjGAMEEND with MjBYE
        self.finished_at = None        # when `_finish` ran (the reaper's grace)
        self._reap_held_said = False   # one HOLD line per table, not per sweep
        self.pending_chankan = None    # (seat, kind, pre_snapshot) in a chankan window
        self._hold_s = 0.0             # the deal delay, slept by Manager AFTER its lock
        self._managed = False          # True once a Manager owns this table
        self.last_line_at = self.clock()   # the idle TTL reads this
        # --- the gallery (see the GALLERY banner) -------------------------
        self.gallery = {}              # member -> gallery slot (NEVER a seat)
        self.gallery_acked = set()     # members whose first MjGALLEYACK arrived
        self.gallery_seq = {}          # member -> seq of the last record it acked
        self._gallery_copied = None    # (opcode, seq) of the last copy queued
        self._gallery_released = False # the store was told the game ended
        self._gallery_evicted = []     # members dropped for a full outbox

    # -- seats ---------------------------------------------------------------

    def seat_player(self, member_id, nick=None, seat=None, live=True,
                    member=None):
        """Seat a player. `member_id` is the WIRE id (the client's PolID, which
        is what MjNOTICEMEMBER's four slots carry); `member` is our own account
        number, kept so a record can be routed back to the right connection.

        The two were never distinguished while one human could be at a table --
        there was only ever one place to send anything.
        """
        if seat is None:
            seat = next((s for s in range(4) if self.seats[s] is None), None)
        if seat is None:
            return None
        self.seats[seat] = member_id
        self.nicks[seat] = nick
        if member:
            self.members[int(member)] = seat
        if live:
            self.live.add(seat)
            self.bots.pop(seat, None)
        return seat

    def seat_of_member(self, member):
        """Our account number -> its seat, or None."""
        try:
            return self.members.get(int(member))
        except (TypeError, ValueError):
            return None

    #: A bot seat's member id. WARNING: IT MUST BE NON-ZERO, and that is MEASURED, not
    #: taste: `malloc__002c67a0` derives the SEATED PLAYER COUNT by counting how
    #: many of MjNOTICEMEMBER's four u64 ids at +0x30 are non-zero --
    #:
    #:     do { if (*(longlong *)(p + 0x30) != 0) count++; p += 8; } while (i < 4);
    #:
    #: -- and stores it in `_DAT_00446098` (`GetTableMemberList`). Seating three
    #: bots as id 0 therefore told a live client the table had ONE player, and it
    #: sat on 「皆さんをお待ちしております」 ("waiting for everyone") for ever.
    #: Confirmed on the console 2026-08-17: the game-start pair was accepted, the
    #: table drew READY!, and then it waited -- correctly, on our own data.
    #: The high nibble keeps them clearly distinguishable from a real POL id in
    #: a capture.
    BOT_ID_BASE = 0x0B07000000000000

    def bot_id(self, seat):
        return self.BOT_ID_BASE | (self.id << 8) | seat

    def fill_with_bots(self):
        for s in range(4):
            if self.seats[s] is None:
                self.seats[s] = self.bot_id(s)
                self.bots[s] = Bot(s)
        return self.bots

    def member_ids(self):
        """The four seat ids as MjNOTICEMEMBER wants them -- all non-zero once
        the table is full, because that count IS the client's player count."""
        return [self.seats[s] or self.bot_id(s) for s in range(4)]

    def member_of_seat(self, seat):
        """Our account number for a seat, or its wire id when we hold none
        (the solo `table_for` path seats the wire id as the member)."""
        for m, s in self.members.items():
            if s == seat:
                return m
        return self.seats[seat]

    # -- sequencing ----------------------------------------------------------

    def next_seq(self):
        """A byte that DIFFERS from the last one we sent.

        The client treats an equal `rec[0x13]` as a duplicate: it re-sends its
        previous message and does NOT handle ours. So this is not decoration --
        reusing a value silently stalls the hand.
        """
        self._seq = (self._seq + 1) & 0x7F
        if self._seq == 0:
            self._seq = 1
        return self._seq

    # -- the deadline --------------------------------------------------------
    #
    # WARNING: THE TURN CLOCK IS THE CLIENT'S, NOT OURS -- and that is measured.
    # `mahdisp__Skip_Request_002c21a0` is the `LimitTimeManager` thread:
    #
    #     if (timer_running) {
    #         ticks++;
    #         if (DAT_00445b87 * 0x3c <= ticks) { skip = 1; ... }
    #         yamaguchi2__TimeLimmit(widget, DAT_00445b87 - ticks / 0x3c);
    #     }
    #
    # `DAT_00445b87` is the limit in SECONDS (x0x3c = frames) and it is only
    # ever READ in the module -- it comes from the table config we author, i.e.
    # SE's own `MJS_CONFIG_IDX_WAIT_TIME`. When it expires the client sets a
    # flag that BOTH pickers poll (`mahdisp__002c2140`): the discard picker
    # returns its default slot and the call picker returns 0 = pass. A
    # spectator gets the same flag from an `MjGALLEYACK` (0x30) on sub 6.
    #
    # So a slow human cannot hang a table -- the client plays for them. What CAN
    # hang a table is a client that says nothing at all: a crash, a pulled
    # network cable, a PCSX2 window closed. Nothing arrives, no ack, no skip.
    # That is what this deadline is for, and it is why it must be LONGER than
    # the client's own clock -- otherwise we would race a player who is simply
    # thinking, and play a tile they did not choose.
    TURN_GRACE = float(os.environ.get("POL_JAN_TURN_GRACE", "15"))

    def deadline(self):
        return float(getattr(self.rules, "wait_time", 30)) + self.TURN_GRACE

    #: The broadcast acks a table can be waiting on (`_awaiting = (op, None)`).
    #: Every one of them has a ladder step in `_ack_advance`.
    BROADCAST_ACKED = frozenset([M.MjHAIPAI, M.MjYAKUDISP, M.MjSEISAN,
                                 M.MjGAMERESULTHALF1, M.MjGAMERESULTHALF2,
                                 M.MjGAMEEND])

    def tick(self, now):
        """The deadline, for a seat OR a broadcast ack.

        A silent SEAT gets its default move played, exactly as its own client
        would have; a seat that strikes the deadline TIMEOUTS_TO_DROP times in
        a row is a client that is gone and is dropped (finding 31/33). A
        BROADCAST ack (deal, win screen, settlement, results, game end) that
        nobody has answered is re-sent ONCE and then treated as acked by the
        seats that never answered (finding 31: these waits used to have no
        deadline at all). Returns the records that follow, or [].
        """
        if SHUTDOWN and self.state == "playing":
            self.log.append(("escape", "shutdown -- swept"))
            self.trace("GAMEEND (shutdown -- the sweeper ends the live game)")
            return [self.msg_gameend()]
        if self._awaiting is None or self._awaiting_since is None:
            return []
        if self.state not in ("playing", "over"):
            return []
        op, seat = self._awaiting
        if now - self._awaiting_since < self.deadline():
            return []
        if seat is None:
            if not self.live or op not in self.BROADCAST_ACKED:
                return []                   # nobody to hear it / not a ladder ack
            if self._resent_for == (self._awaiting, self._awaiting_since):
                self.log.append(("timeout-broadcast", (op, round(now - self._awaiting_since, 1))))
                self.trace("TIMEOUT nobody acked %s twice -- proceeding as if the "
                           "silent seats had" % M_NAME(op))
                if op == M.MjSEISAN:
                    # ...and tell the gauges so: every live seat counts as ready.
                    self._seisan_ready |= set(self.live)
                    return self._ready_status(None) + (self._ack_advance(op) or [])
                return self._ack_advance(op) or []
            self.log.append(("resend", (op, round(now - self._awaiting_since, 1))))
            out = self._renudge_all()
            self._awaiting_since = now
            self._resent_for = (self._awaiting, now)
            return out
        if seat not in self.live:
            return []                       # nothing is waiting on a person
        strikes = self.timeouts[seat] = self.timeouts.get(seat, 0) + 1
        self.log.append(("timeout", (seat, op, round(now - self._awaiting_since, 1))))
        if strikes >= TIMEOUTS_TO_DROP:
            self.trace("TIMEOUT %s struck %d times in a row -- dropping the seat"
                       % (self._seatname(seat), strikes))
            return self.drop_seat(seat, "timed out %d times" % strikes)
        return self.play_default_for(seat, op)

    def _ack_advance(self, op):
        """The ladder step an ack of broadcast `op` buys. ONE writer for the
        ack arms in `handle()` AND for the sweeper's proceed-as-acked, so the
        two cannot disagree. None = `op` is not a ladder ack."""
        if op in self.GALLERY_SKIP_AFTER:
            # The players are done with this screen: tell the spectators'
            # LimitTimeManager to skip theirs (GALLERY banner; off by default).
            self._gallery_skip()
        if op == M.MjHAIPAI:
            return self.advance()
        if op == M.MjYAKUDISP:
            # WARNING: A DOUBLE RON HAS TWO WINNERS AND ONE WIN SCREEN (2026-09-12):
            # `_settle` paid both, but every call site showed `wins[0]` only, so
            # the seat that lost the head-bump order never saw its own hand --
            # seen live: a player ronned and watched the win screen reveal the OTHER
            # player. Each ack pulls the next winner's screen; SEISAN waits
            # until the queue is empty.
            if self._pending_wins:
                seat, score = self._pending_wins.pop(0)
                return self.on_win(seat, score, ron_from=self._pending_ron_from)
            return [self.msg_seisan()]
        if op == M.MjSEISAN:
            return self.next_kyoku_or_end()
        if op == M.MjGAMERESULTHALF1:
            return [self.msg_results2()]
        if op == M.MjGAMERESULTHALF2:
            return [self.msg_gameend()]
        if op == M.MjGAMEEND:
            self._finish("every live seat left (or timed out) after MjGAMEEND")
            return []
        return None

    def _finish(self, why):
        """The table is done: nothing is awaited and the Manager may forget it.

        `finished_at` is what the reaper's grace window is measured from -- see
        `Manager._reap`: a table whose last records are still queued for a human
        who has not said MjBYE must outlive its own game for a few seconds.
        """
        if self.state != "finished":
            self.log.append(("finished", why))
            self.finished_at = self.clock()
        self.state = "finished"
        self._awaiting = None
        self._awaiting_since = None

    def play_default_for(self, seat, op):
        """The move the client's own skip flag would have produced.

        Discard: its default slot (the drawn tile). A call: pass. Anything else
        is an ack we can simply proceed without.
        """
        k = self.kyoku
        if k is None or k.result is not None:
            return []
        if op == M.MjTSUMO:
            h = k.hands[seat]
            if k.turn != seat or h.tile_total() != 14:
                return []                   # not actually this seat's move
            if k.pending_kan is not None:   # its own kan is mid-window: no-op
                return []
            forbidden = k.kuikae_forbidden(seat)
            tile = h.drawn if h.drawn in h.tiles else None
            if tile is None or mj.kind(tile) in forbidden:
                tile = next((t for t in reversed(h.tiles)
                             if mj.kind(t) not in forbidden), h.tiles[-1])
            k.discard(seat, tile=tile)
            return self._continue(tile, seat)
        if op == M.MjNAKI:
            if self.pending_naki is not None:
                # WARNING: An answer already COLLECTED must not be swept away with
                # the silence (the arbitration hold): seat A's ron waiting on
                # seat B's timeout is still a ron. The silent seats pass; the
                # answers on file resolve exactly as if the last ack had come.
                tile, options = self.pending_naki
                answers, self.naki_answers = self.naki_answers, {}
                for s in options:
                    answers.setdefault(s, ("pass", None))
                self.pending_naki = None
                return self._resolve_naki(tile, options, answers,
                                          k.last_discard_seat)
            res = self.resolve_bot_calls(k.last_discard, k.last_discard_seat)
            if res is not None:
                return res
            k.advance()
            if k.result is not None:
                return self.on_kyoku_end()
            return self.advance()
        # anything else a seat can be waited on for: re-nudge it. The client
        # dedups on the sequence byte, so a genuine straggler is safe.
        return self._renudge(seat)

    # -- a seat leaving ------------------------------------------------------

    def drop_seat(self, seat, reason="left"):
        """A seat is gone: tell the table, and play the rest of the hand for it.

        WARNING: `MjMEMBERLEAVE` puts a MODAL yes/no dialog in front of the other three
        (see janmsgs), so this costs each of them an answer before anything else
        moves. That is the client's design, not ours.
        """
        if seat in self.dropped:
            return []
        was_awaiting = self._awaiting
        self.dropped.add(seat)
        self.live.discard(seat)
        self.timeouts.pop(seat, None)
        self.bots[seat] = Bot(seat)         # the hand plays on, seat automated
        self.log.append(("drop", (seat, reason)))
        self.trace("LEAVE %s (%s) -- a CPU plays the seat from here"
                   % (self._seatname(seat), reason))
        if not self.live:
            # The last person is gone: there is nobody to announce it to and
            # nothing to play for. The Manager reaps a finished table.
            if self.state == "playing":
                # the web board says WHY the game stopped (public_state)
                self.ended_early = {"seat": seat, "reason": reason}
            self._finish("last live seat %d %s" % (seat, reason))
            return []
        rec = M.memberleave(self.next_seq(), seat=seat,
                            member_id=self.seats[seat] or 0)
        out = [self._emit(rec, "seat %d %s" % (seat, reason))]
        k = self.kyoku
        if self.state == "playing" and k is not None and k.result is None:
            # WARNING: THE TABLE MUST NEVER BE LEFT WAITING ON THE SEAT THAT LEFT.
            # `tick` skips a seat that is not live, so an `_awaiting` still
            # naming the leaver would stall the hand FOREVER, not 75s. Whatever
            # was owed is settled here, now:
            if self.pending_naki is not None:
                # The leaver's unanswered call offer becomes a pass. If live
                # seats are still deciding, point the deadline at one of them;
                # otherwise the offer is closed and resolves on the spot.
                tile, options = self.pending_naki
                if seat in options:
                    self.naki_answers.setdefault(seat, ("pass", None))
                blockers = [s for s in sorted(options)
                            if s in self.live and s not in self.naki_answers]
                if blockers:
                    self._awaiting = (M.MjNAKI, blockers[0])
                    return out
                answers, self.naki_answers = self.naki_answers, {}
                self.pending_naki = None
                return out + self._resolve_naki(tile, options, answers,
                                                k.last_discard_seat)
            if was_awaiting and was_awaiting[1] == seat:
                # The leaver held the turn: play its default immediately,
                # exactly as the deadline sweeper would have -- 75s from now.
                return out + self.play_default_for(seat, was_awaiting[0])
            # Waiting on another seat or on a broadcast ack: leave `_awaiting`
            # ALONE. The SEISAN/GAMERESULT ladders advance only while it still
            # names their record, and clobbering it here would wedge them for
            # the seats still playing.
            return out
        # WARNING: BETWEEN HANDS -- the win screen, the settlement, the results, the
        # end -- `_awaiting` is a BROADCAST ack (seat None) and it STAYS
        # EXACTLY AS IT IS (audit finding 6, probe P1). This used to set
        # `_awaiting = (MjMEMBERLEAVE, None)`: nothing acks MjMEMBERLEAVE, so
        # the survivor's YAKUDISPACK/SEISANACK no longer matched what the
        # ladder was waiting on and the table wedged for ever. The leaver's
        # outstanding ack is settled by the fact that any live seat's ack
        # advances the ladder, and `tick()` proceeds when nobody is left to.
        # Left during the pre-game side-bet handshake (no hand yet): the seat
        # is a bot now and no longer waited on -- finish the phase if it was
        # the last answer outstanding, so the others are not held for ever.
        if self.sashiuma and self.sashiuma["phase"] != "done":
            return out + self._sashiuma_seat_gone(seat)
        return out

    def rejoin(self, seat):
        """A member re-entered a playing table (finding 29): put the human back
        on its stored seat -- the bot that was substituted for it, if any,
        goes -- and resync it: a full MjALLDATA for that seat, plus the record
        it is being waited on for if it is its move."""
        if seat is None:
            return []
        was_bot = seat in self.bots
        self.bots.pop(seat, None)
        self.dropped.discard(seat)
        self.timeouts.pop(seat, None)
        self._byes.discard(seat)
        self.live.add(seat)
        self.log.append(("rejoin", (seat, was_bot)))
        self.trace("REJOIN %s%s -- resyncing"
                   % (self._seatname(seat), " (was a CPU)" if was_bot else ""))
        k = self.kyoku
        if k is None or self.state != "playing":
            return []
        out = [self.msg_alldata(label="rejoin resync seat %d" % seat, to=seat)]
        if k.result is not None:
            return out + self._renudge(seat)
        if (self.pending_naki is not None and seat in self.pending_naki[1]
                and seat not in self.naki_answers):
            return out + self._renudge(seat)
        if (k.turn == seat and k.hands[seat].tile_total() == 14
                and k.pending_kan is None):
            out.append(self.msg_tsumo(seat))
        return out

    # -- helpers -------------------------------------------------------------

    def _seatname(self, seat):
        """`seat 1 (right, CPU)` -- names the seat AND where it sits on the local
        player's screen, so a trace line can be matched to what is drawn."""
        if seat is None:
            return "seat ?"
        who = "YOU" if seat in self.live else "CPU"
        return "seat %d (%s, %s)" % (seat, _SEATPOS.get(seat, "?"), who)

    def trace(self, msg):
        """One human-readable move, tagged with the table and round so a long
        log stays legible. Goes to janhourou.log via the module TRACE hook."""
        k = self.kyoku
        rnd = ""
        if k is not None:
            rnd = "[t%s %s%d h%d] " % (self.id,
                                       "ESWN"[(k.round_wind - mj.EAST) & 3],
                                       (k.kyoku & 3) + 1, k.honba)
        _trace(rnd + msg)

    def _emit(self, rec, why="", to=None, gallery=None):
        """Record one outgoing message. `to` is the seat it is FOR, or None for
        every live seat. `gallery` is the copy the SPECTATORS get instead of
        `rec` (a builder passes one when `rec` conceals a hand); None = `rec`.

        WARNING: ROUTING IS ADDITIVE ON PURPOSE. Callers still get `rec` back and
        still build their own lists exactly as before, so with one live seat
        this changes nothing at all -- every route is either None or that seat,
        `Manager.handle` hands the whole list to the caller, and no outbox is
        ever written. The routes are a SIDE CHANNEL that only matters once a
        second human is at the table, which is the only case that was broken.
        """
        self._last_sent = rec
        self._awaiting_since = self.clock()
        h = janwire.unpack(rec)
        self.log.append((h["opcode"], why))
        self._outgoing.append((rec, to))
        # Per-seat "the last record you were sent" and "the record(s) the
        # current `_awaiting` is waiting on" -- what `_renudge` re-sends. A
        # builder assigns a NEW tuple to `_awaiting` before it emits, so
        # identity is the change detector; MjMEMBERLEAVE (emitted under an
        # unchanged wait) joins the list and is filtered out by opcode.
        if to is None:
            for s in self.live:
                self._last_for[s] = rec
            self._last_for[None] = rec
        else:
            self._last_for[to] = rec
        if self._awaiting is not self._awaiting_tag:
            self._awaiting_tag = self._awaiting
            self._awaiting_recs = []
        self._awaiting_recs.append((rec, to))
        # The spectators' copy -- of EVERY record, `to=seat` ones included
        # (GALLERY banner). Re-sends (`_renudge*`) bypass `_emit` on purpose:
        # a same-seq record would only make the spectator re-send its ack.
        self._gallery_copy(rec, h, gallery)
        return rec

    def _awaited_recs(self):
        op = self._awaiting[0] if self._awaiting else None
        return [(r, to) for r, to in self._awaiting_recs
                if janwire.unpack(r)["opcode"] == op]

    def _renudge(self, seat):
        """Re-send what `seat` is waiting on: the awaited record(s) addressed
        to it, else the last record it was sent. NOT re-stamped (the client
        dedups on the sequence byte: a straggler is harmless, a lost record
        is recovered) and routed through `_outgoing`, so `_route` never has to
        bypass for it -- the old `[self._last_sent]` was per-table, not
        per-seat, and with two humans handed one seat the other's record."""
        if seat is None:
            return []
        recs = [r for r, to in self._awaited_recs() if to is None or to == seat]
        if not recs:
            last = self._last_for.get(seat)
            recs = [last] if last is not None else []
        for r in recs:
            self._outgoing.append((r, seat))
        return recs

    def _is_human(self):
        """The +0x23..+0x26 IsHuman bytes (janmsgs `_put_is_human`): a seat a
        bot plays is a COM to the client, anything else is a human -- a seat
        whose client has not said READY yet included, so its panel and gauge
        take the human form from the first deal."""
        if not ISHUMAN_ENABLE:
            return (0, 0, 0, 0)
        return tuple(0 if s in self.bots else 1 for s in range(4))

    def _ready_status(self, seat):
        """MjREADYSTATUS after a MjSEISANACK: `seat` is ready; broadcast the
        four bytes (bots always ready, humans once they acked this MjSEISAN --
        `msg_seisan` clears the set). Routed through `_outgoing` like a
        re-send, NOT `_emit`: it is not a ladder record, so it must not
        become `_last_for` (a re-nudge would re-send it instead of the deal)
        and must not bump `_awaiting_since`. Its own sequence byte: the
        client only ever compares a seq with the PREVIOUS one (a gap is
        nothing, an equal byte is a retransmission), and the selftest's
        dedup guard walks every record in order."""
        if not ISHUMAN_ENABLE or self.game is None:
            return []
        if seat is not None and seat not in self.bots:
            self._seisan_ready.add(seat)
        ready = tuple(1 if (s in self.bots or s in self._seisan_ready) else 0
                      for s in range(4))
        rec = M.readystatus(self.next_seq(), ready)
        self.log.append((M.MjREADYSTATUS, "ready %r" % (ready,)))
        self._outgoing.append((rec, None))
        return [rec]

    def _renudge_all(self):
        """Re-send the awaited broadcast to every live seat, on its own route."""
        out = []
        for r, to in self._awaited_recs():
            if to is not None and to not in self.live:
                continue
            self._outgoing.append((r, to))
            out.append(r)
        return out

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
        self.log.append(("gallery-ignored", (member, M_NAME(op))))
        self.trace("SPECTATOR member %d sent %s -- ignored (no mutation, no "
                   "reply)" % (member, M_NAME(op)))
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

    def take_hold(self):
        """Seconds the Manager should sleep AFTER releasing its lock before
        answering -- the deal delay, which used to sleep inside `msg_haipai`
        and would now hold every other table and the sweeper with it."""
        s, self._hold_s = self._hold_s, 0.0
        return s

    # -- per-seat delivery ---------------------------------------------------
    #
    # The transport is reply-first: a record leaves on the socket of whoever
    # just spoke. That is fine for the seat taking its turn and useless for the
    # other three -- so anything for another seat waits in their outbox. Three
    # things drain it: that member's next game-band line (`handle_line`), the
    # `<DR>` poll of a client still in the room, and responders' idle push.
    # WARNING: The third is not optional: an in-game client that is WAITING for us
    # sends NOTHING (measured live 2026-09-04 -- responders' idle-push banner;
    # only a client on an ack screen re-sends, ~2 s). The deadline sweeper
    # thread queues here too (`Manager.tick`), and `Manager.on_swept` lets the
    # transport push at once.

    def begin_routing(self):
        self._outgoing = []

    def take_routes(self):
        out = self._outgoing
        self._outgoing = []
        return out

    def queue_for(self, seat, rec):
        self.outbox.setdefault(seat, []).append(rec)

    def take_outbox(self, seat, limit=None):
        """What is waiting for `seat`. `limit` leaves the remainder QUEUED.

        The remainder must survive: the client applies strictly in order and
        this band never resends, so a record dropped because a burst was too
        big is a hand that stops.
        """
        got = self.outbox.get(seat) or []
        if limit is None or len(got) <= limit:
            self.outbox.pop(seat, None)
            return got
        self.outbox[seat] = got[limit:]
        return got[:limit]

    def wall_count(self):
        return self.kyoku.wall.remaining if self.kyoku else 0

    def public_state(self):
        """The table as SE's own spectator sees it -- all four hands face up
        (the spectator view shows every hand), the ponds, melds, scores and the
        round -- as plain JSON for the web board's DELAYED view. Never the
        wall's order and never the ura dora (secret until a win shows them)."""
        k, g = self.kyoku, self.game
        seats = []
        for s in range(4):
            seat = {"name": self.nicks[s] or "", "bot": s in self.bots,
                    "left": s in self.dropped,
                    # The player's ACCOUNT number, for the board to turn into
                    # their PlayOnline portrait (it never passes it on); None
                    # for a COM.
                    #
                    # WARNING: NOT `self.seats[s]`, and not a magnitude test on it.
                    # `seat_player`'s own docstring says `self.seats[]` holds the
                    # WIRE id -- the client's PolID -- while `self.members` holds
                    # our account number, and the portrait lives in
                    # `handle_profile` under the ACCOUNT. The old line also tested
                    # `< BOT_ID_BASE` to mean "a person", but a real PolID
                    # (0x5b01...) is ABOVE the bot base (0x0b07...), so it named
                    # every HUMAN seat None: `jan-tables-live.json` showed
                    # every seat `"mid": null` while two people were playing.
                    # The COMs looked fine only because the page draws those
                    # from its own ACKY pair.
                    "mid": (None if s in self.bots else self.member_of_seat(s)),
                    "score": (k.scores[s] if k is not None
                              else (g.scores[s] if g is not None else None))}
            h = k.hands[s] if k is not None else None
            if h is not None:
                seat.update(
                    hand=[_web_tile(x) for x in h.tiles],
                    drawn=_web_tile(h.drawn),
                    pond=[{"tile": _web_tile(x), "called": i in h.pond_called,
                           "riichi": i == h.riichi_index}
                          for i, x in enumerate(h.pond)],
                    melds=[{"kind": m.kind, "tiles": [_web_tile(x) for x in m.tiles],
                            "from": m.from_seat, "called": _web_tile(m.called)}
                           for m in h.melds],
                    riichi=bool(h.riichi))
            seats.append(seat)
        out = {"state": self.state, "seats": seats, "round": {}, "dora": [], "game": self.id,
               "wall": None, "turn": None, "last": None, "result": None}
        key = getattr(self, "lobby_key", None)
        if key:
            out["room"], out["table"] = int(key) >> 16, int(key) & 0xFFFF
        if k is not None:
            out["round"] = {"wind": "ESWN"[(k.round_wind - mj.EAST) & 3],
                            "number": k.kyoku + 1, "dealer": k.dealer,
                            "honba": k.honba, "sticks": k.riichi_sticks}
            out["dora"] = [_web_tile(x) for x in k.wall.dora_indicators()]
            out["wall"] = k.wall.remaining
            out["turn"] = k.turn
            if k.last_discard is not None and k.last_discard_seat >= 0:
                out["last"] = {"tile": _web_tile(k.last_discard), "seat": k.last_discard_seat}
            if k.result:
                kind_, data = k.result[0], k.result[1] if len(k.result) > 1 else None
                res = {"kind": str(kind_)}
                if kind_ in (mj.RON, mj.TSUMO) and isinstance(data, (list, tuple)):
                    res["wins"] = [{"seat": ws, "han": getattr(sc, "han", None),
                                    "points": getattr(sc, "points", None),
                                    "limit": getattr(sc, "limit", None),
                                    "yaku": [[str(n), v] for n, v in (getattr(sc, "yaku", None) or [])]}
                                   for ws, sc in data]
                out["result"] = res
        if self.state == "finished":
            # a game's last frame (Manager._keep_final): over, or stopped early
            ee = getattr(self, "ended_early", None)
            out["ended"] = ({"early": True, "seat": ee["seat"], "why": ee["reason"],
                             "name": self.nicks[ee["seat"]] or ""} if ee
                            else {"early": False})
        return out

    def _dice(self):
        """The hand's two dice (1..6 each). `Kyoku.dice` when the engine has it
        (it draws them at construction); otherwise a deterministic pair derived
        from the wall order, so the same hand always shows the same dice.

        TODO(engine): `Kyoku.dice` is the intended source -- drop the fallback
        once every deployed engine carries it.
        """
        k = self.kyoku
        d = getattr(k, "dice", None)
        if d and len(d) >= 2 and all(1 <= int(x) <= 6 for x in d[:2]):
            return (int(d[0]), int(d[1]))
        rng = random.Random(hash(tuple(k.wall.tiles)) ^ (k.kyoku * 7 + k.honba))
        return (rng.randint(1, 6), rng.randint(1, 6))

    def _round_scalars(self):
        """THE round state, once, for every builder that carries it.

        MEASURED 2026-09-04 (janmsgs' MjHAIPAI banner: the client's own debug
        labels 場/局/起家/親 at mahdisp.c:3016, the Kyoku label writer's
        `kyoku + wind*4` index, the wall-break arithmetic at console.c:3304 and
        the dice draw at mahdisp.c:1892). Four messages carry these -- HAIPAI
        +0xd0.., YAKUDISP +0x1d.., SEISAN +0x28.., ALLDATA +0x13b.. -- and
        every one of them now reads this structure, so they cannot disagree.
        """
        k = self.kyoku
        return RoundScalars(
            # 起家: the seat that dealt E1 -- where the round-wind marker sits.
            chiicha=(k.dealer - k.kyoku) & 3,
            dealer=k.dealer & 3,
            round_wind=(k.round_wind - mj.EAST) & 1,       # 0 East, 1 South
            kyoku=k.kyoku & 3,
            honba=min(255, k.honba),
            riichi_sticks=min(255, k.riichi_sticks),
            dice=self._dice(),
            current_seat=k.turn & 3,
        )

    def _rule_scalars(self):
        """MjHAIPAI's rule vector (+0x2c, 18 bytes) and the per-suit red-five
        counts (+0x3e), from `self.rules`. Every field the client READS is
        named in janmsgs' banner; the engine worker's newer Rules fields are
        read with getattr so this works before and after their commit."""
        r = self.rules
        kind_ = getattr(r, "dora_kind", None)
        if kind_ is None:
            kind_ = M.DORA_KIND.get(
                (bool(getattr(r, "ura_dora", True)),
                 bool(getattr(r, "kan_dora", True)),
                 bool(getattr(r, "kan_ura",
                              getattr(r, "ura_dora", True)
                              and getattr(r, "kan_dora", True)))), 4)
        uma = tuple(getattr(r, "uma", (20, 10, -10, -20)))
        aka = getattr(r, "aka_counts", None)
        if aka is None:
            aka = getattr(r, "aka", None)
        if aka is None:
            n = getattr(r, "aka_count", 3)
            aka = tuple(1 if i < n else 0 for i in range(3))
        vec = M.rule_scalars(dora_kind=kind_,
                             aka_rank=getattr(r, "aka_rank", 5),
                             wareme=bool(getattr(r, "wareme", False)),
                             # the engine (and the bot) refuse a riichi under
                             # 1000 points; tell the client so its button agrees
                             riichi_1000=True,
                             yakitori=bool(getattr(r, "yakitori", 0)),
                             uma=(uma[0], uma[1]) if len(uma) >= 2 else (0, 0))
        return vec, bytes(min(255, int(a)) for a in aka[:3]).ljust(3, b"\0")

    def _wanpai_u16(self):
        k = self.kyoku
        return M.wanpai_u16s(k.wall.dead, k.wall.dora_shown, k.wall.kans,
                             legacy=WANPAI_LEGACY)

    def _wanpai_bytes(self, dora_shown=None, kans=None):
        k = self.kyoku
        return M.wanpai_bytes(k.wall.dead,
                              k.wall.dora_shown if dora_shown is None else dora_shown,
                              k.wall.kans if kans is None else kans,
                              legacy=WANPAI_LEGACY)

    def _pond_wire(self, seat):
        """A seat's pond as MjALLDATA wants it.

        The flag is `POND_FLAG`, and WARNING: it means THE TILE IS NOT DRAWN -- both
        of the client's pond loops bracket the draw AND the position advance in
        `if ((t & 0x80) == 0)`, so a flagged tile is skipped and the pond closes
        up over it (measured 2026-09-04, janmsgs' +0x8a note). That is the right
        thing for a CLAIMED tile, which a call takes out of the pond.

        WARNING: IT IS THE WRONG THING FOR THE RIICHI TILE, which is what this used to
        do: `i == h.riichi_index` flagged the declaring discard and the client
        then refused to draw it, so a riichi tile vanished from the pond on the
        next resync. The sideways riichi tile is +0x136 instead -- a per-seat
        one-based pond index -- and `_riichi_indices()` serves it.

        A CALL motion pops the discarder's tail itself, so its record has to
        carry the tile unflagged -- which is why `call_snapshot()` takes this
        BEFORE the engine adds the index to `pond_called`.
        """
        h = self.kyoku.hands[seat]
        return [(t, i in h.pond_called) for i, t in enumerate(h.pond)]

    def _riichi_indices(self):
        """MjALLDATA +0x136: each seat's riichi pond index, ONE-BASED, 0 = none.

        The pond renderer takes its sideways branch for the tile where
        `index + 1 >= value` and shifts the rest of the row -- measured
        2026-09-04. Sending zeros (which we did until then) drew every riichi
        discard upright.
        """
        k = self.kyoku
        out = []
        for s in range(4):
            h = k.hands[s]
            out.append(h.riichi_index + 1 if h.riichi and h.riichi_index is not None
                       and h.riichi_index >= 0 else 0)
        return out

    # -- the messages --------------------------------------------------------

    def msg_haipai(self):
        # Hold the deal a beat so the client's camera zoom-in finishes before
        # the deal freezes it (the board-zoom fix; see DEAL_DELAY). Thread-per-
        # connection server, so this blocks only this client's thread.
        if DEAL_DELAY > 0:
            if self._managed:
                self._hold_s = max(self._hold_s, DEAL_DELAY)   # Manager sleeps, unlocked
            else:
                time.sleep(DEAL_DELAY)
        k = self.kyoku
        hands = [list(k.hands[s].tiles) for s in range(4)]
        # The dealer holds 14 at this point; the client's array is 14 wide, so
        # the other three simply have a trailing empty slot.
        #
        # WARNING: RETRACTED 2026-09-02 (same day): "+0x44 is the 2D hand rail" was
        # wrong -- +0x44 is the DEAD WALL (client's own debug label: WAMPAI),
        # and stuffing seat 0's hand in it drew garbage stacks in the table
        # centre. The hand was invisible for a different reason entirely:
        # motion=0. The HAIPAI parser zeroes every per-tile visibility gate
        # (DAT_004521a0) and only MotionCommand 10 -- the deal animation, seats
        # picked by the +0x22 bitmask -- restores them as the tiles land. The
        # fluctuating 6/8/10-tile hands were single tiles being revealed by the
        # motion=2 draw animations instead. See janmsgs' WAMPAI banner.
        # Conceal every seat but the local players': retail shows only your
        # own hand, the other three as rows of backs (ml070i/ml073i). The
        # human is seat 0; `self.live` is the set of local seats.
        # WARNING: ONE DEAL RECORD PER LIVE SEAT. This used to conceal
        # `set(range(4)) - self.live` -- every seat that is not a person --
        # which is exactly right while there is ONE person and hands the other
        # player your tiles the moment there are two. Each live seat now gets
        # its own record concealing all three of the others.
        #
        # With a single live seat the result is byte-identical to what this
        # sent before: `{0,1,2,3} - {0}` is the same `{1,2,3}` the old
        # expression produced, so the proven-live solo path is untouched.
        #
        # ONE SEQUENCE FOR ALL OF THEM, deliberately: the client dedups on
        # `rec[0x13]` against the last record IT received, and these go to
        # different clients. Bumping per copy would work too, but sharing it
        # keeps the deal a single logical message.
        seq = self.next_seq()
        targets = sorted(self.live) if self.live else [None]
        out = []
        self._awaiting = (M.MjHAIPAI, None)     # before the emits: _emit keys on it
        gallery_rec = None
        for _s in targets:
            # Concealment via the face-down bit means INVISIBLE, not backs, in
            # this engine (see CONCEAL_DEAL / the RAM proof) -- so with it off,
            # deal every seat face-up and let opponents render consistently.
            if CONCEAL_DEAL:
                concealed = (set(range(4)) - {_s} if _s is not None
                             else set(range(4)) - set(self.live))
            else:
                concealed = set()
            sc = self._round_scalars()
            rules18, aka3 = self._rule_scalars()
            rec = M.haipai(seq, hands, motion=10, seat_mask=0x0F,
                           is_human=self._is_human(),
                           concealed=concealed,
                           # +0x44 the dead wall: dora-1 at slot 4 (the slot
                           # the client itself clears and auto-flips), all
                           # else face-down -- janmsgs' WAMPAI banner.
                           position=self._wanpai_u16(),
                           scalars18=rules18, three_a=aka3,
                           chiicha=sc.chiicha, dealer=sc.dealer,
                           round_wind=sc.round_wind, kyoku=sc.kyoku,
                           honba=sc.honba, dice=sc.dice)
            if self.gallery and concealed and gallery_rec is None:
                # The spectators' deal: the SAME seq, every seat face-up
                # (GALLERY banner). With CONCEAL_DEAL off this is never
                # needed -- `rec` already shows all four hands.
                gallery_rec = M.haipai(seq, hands, motion=10, seat_mask=0x0F,
                                       is_human=self._is_human(),
                                       concealed=set(),
                                       position=self._wanpai_u16(),
                                       scalars18=rules18, three_a=aka3,
                                       chiicha=sc.chiicha, dealer=sc.dealer,
                                       round_wind=sc.round_wind, kyoku=sc.kyoku,
                                       honba=sc.honba, dice=sc.dice)
            out.append(self._emit(rec, "deal", to=_s, gallery=gallery_rec))
        ds = k.wall.dora_shown
        if isinstance(ds, (list, tuple)):
            dora = "/".join(_tile(t) for t in ds) if ds else "?"
        else:
            dora = _tile(ds) if ds is not None else "?"
        self.trace("DEAL  dealer=%s  dora=%s  wall=%d  [%d record(s), one per "
                   "live seat]"
                   % (self._seatname(k.dealer), dora, self.wall_count(),
                      len(out)))
        for s in range(4):
            shown = mj.hand_str(k.hands[s].tiles)
            self.trace("  hand %s%s = %s"
                       % (self._seatname(s),
                          "" if s in self.live else " [concealed on screen]",
                          shown))
        return out

    #: MyMove action-menu byte indices (the MjTSUMO +0x4d block), from the
    #: client's itemmap DAT_004099b0 = [2,0,3,6] over [Riichi,Tsumo,Kan,Kyushu].
    #: Same unified layout as the call menu (NAKI_MENU_BYTE): a shared
    #: 7-byte enable block, so a self-turn and a call offer speak the same
    #: dialect. WARNING: We used to set only byte 0, which is TSUMO -- so Riichi
    #: (byte 2) never lit and Tsumo lit on any tenpai hand (2026-09-03 fix).
    MENU_TSUMO, MENU_RIICHI, MENU_KAN, MENU_KYUSHU = 0, 2, 3, 6

    def _riichi_discard_slots(self, h):
        """Hand-slot indices whose discard leaves this 14-tile hand tenpai.

        These are the riichi-legal discards. The turn-menu MjTSUMO must mark
        each such slot with **bit 0** of its +0x54 flag (DAT_00446d50): when the
        player presses Riichi, the client's tile picker (mahdisp__002c2ac0,
        lVar5==2 branch) scans ONLY bit-0 slots for a cursor landing. If none
        are set it falls through to the ±1000 sentinels (`uVar10 = 1000` at
        mahdisp.c:3560) and WEDGES with no message sent back -- the exact
        riichi freeze (cursor 1000, _DAT_00452010==1). bit 1 stays the general
        "discardable" flag for a normal (non-riichi) discard.
        """
        k = self.kyoku
        if k is not None and k.turn == h.seat and h.tile_total() == 14:
            # The engine's own answer (every riichi condition applied,
            # REACH_AFTER_KAN included) whenever it is in a position to give
            # one; the local tenpai scan below is the fallback for a hand
            # that is not on turn (the selftests build those directly).
            try:
                kinds = set(mj.kind(t) for t in k.riichi_discards(h.seat))
            except Exception:
                kinds = set()
            if kinds:
                return [i for i, t in enumerate(h.tiles) if mj.kind(t) in kinds]
        nmeld = len(h.melds)
        slots = []
        for i, t in enumerate(h.tiles):
            counts = list(h.counts())
            counts[mj.kind(t)] -= 1
            if mj.is_tenpai(counts, nmeld):
                slots.append(i)
        return slots

    def _can_riichi_discard(self, h):
        """True if some discard leaves this 14-tile hand tenpai (riichi-ready)."""
        return bool(self._riichi_discard_slots(h))

    def _mymove_menu(self, seat):
        """The 7-byte self-turn action-menu enable block for `seat`.

        Each button is authored to its REAL legality; the client draws only the
        lit ones (plus the always-present Cancel). The client applies its own
        gates to Tsumo (yamaguchi__002e6cb0), but Riichi/Kan/Kyushu are ours to
        decide, so the offer must be exactly right -- a lit button whose reply
        arm the server rejects is a dead button.
        """
        k = self.kyoku
        h = k.hands[seat]
        block = bytearray(7)
        # Tsumo: the drawn hand is a scoreable self-draw win.
        can_win = False
        if h.drawn is not None:
            ctx = k.context_for(seat, h.drawn, True)
            if mj.can_tsumo(h, ctx) is not None:
                block[self.MENU_TSUMO] = 1
                can_win = True
        # Riichi: closed, not already declared, >=1000 points, a draw still to
        # come, and some discard reaches tenpai.
        #
        # WARNING: NOT WHEN THE HAND ALREADY WON (2026-09-04 LIVE HANG). If the drawn
        # tile completes the hand, the menu must offer ONLY Tsumo -- you never
        # declare riichi on a hand you can win. We used to light both, and a
        # player, learning, pressed Riichi on a complete hand: the client's
        # riichi-select flow (mahdisp__002c2ac0) cannot declare on a winning
        # hand and WEDGED THE GAME with no message ever sent back to us (the
        # trace ends at that MjTSUMO). `can_win` gates it out.
        if (h.menzen and not h.riichi and not can_win
                and k.scores[seat] >= 1000
                and self.wall_count() >= 4 and self._can_riichi_discard(h)
                and (k.turn != seat or h.tile_total() != 14
                     or k.can_riichi(seat))):
            block[self.MENU_RIICHI] = 1
        # Kan: `Kyoku.ankan_options/kakan_options` are the source of truth
        # (turn, draw, riichi waits-unchanged, no 5th kan, dead wall) -- the
        # hand-only module functions lit the button for kans the engine then
        # refused (audit 11/21), and a lit button whose reply we reject is a
        # dead button.
        try:
            if k.ankan_options(seat) or k.kakan_options(seat):
                block[self.MENU_KAN] = 1
        except Exception:
            pass
        # Kyushu kyuhai: first uninterrupted go-round, still concealed, and 9+
        # distinct terminals/honors -> an abortive draw.
        if k.first_go_round and h.menzen and not any(x.melds for x in k.hands):
            distinct_yao = sum(1 for kk in set(mj.kind(t) for t in h.tiles)
                               if kk in mj.YAOCHUU)
            if distinct_yao >= 9:
                block[self.MENU_KYUSHU] = 1
        return bytes(block)

    def msg_tsumo(self, seat, motion=None, discard=None):
        """Seat `seat`'s state after a draw -- and the turn's ANIMATION.

        Every record carries ONE MotionCommand (janmsgs' +0x18 banner,
        2026-09-02). A bot's discard (motion 3) is the ANIMATION only: it never
        writes the pond array, so the pond-bearing MjALLDATA must land first
        (see DISCARD_ANIM). `discard=(tile, slot, seat[, riichi])` builds the
        motion-3 form -- `riichi` True sets +0x21, the byte the remote discard
        flight tests for the riichi cut-in, voice and sideways tile
        (yamaguchi.c:7920-7932; never sent before 2026-09-04, so every CPU
        riichi was silent). Otherwise a real draw gets motion 2 with the drawn
        tile (motion 9 = the same for a RINSHAN draw: the client pulls the
        dead-wall slot itself), and a turn with no draw (after a call)
        animates nothing. The local player's OWN discard is animated
        client-side -- never send motion 3 with the human's seat or the pond
        double-appends.
        """
        k = self.kyoku
        h = k.hands[seat]
        anim_tile = anim_slot = anim_seat = 0
        riichi = False
        if discard is not None:
            motion = 3
            anim_tile = M.tile_byte(discard[0])
            anim_slot = discard[1] & 0xFF
            anim_seat = discard[2]
            riichi = bool(discard[3]) if len(discard) > 3 else False
        elif h.drawn is not None:
            if motion is None:
                motion = 2
            if motion in (2, 9):
                anim_tile = M.tile_byte(h.drawn)
                anim_slot = 13
                anim_seat = seat
        if motion is None:
            motion = 0
        # The self-turn ACTION MENU (MyMove) -- the +0x4d block, same 7-byte
        # menu-enable structure the call menu uses at +0x4c, but read through
        # the MyMove itemmap DAT_004099b0 = [2,0,3,6]: Riichi=block[2],
        # Tsumo=block[0], Kan=block[3], Kyushu=block[6]. Only for the seat
        # whose turn it actually is (a live seat that just drew).
        menu = self._mymove_menu(seat) if seat in self.live else b"\0" * 7
        slot_flags = None
        auto = 0
        if h.riichi:
            # A declared riichi may only discard what it drew: offer that slot.
            # The DRAWN tile is the one `Kyoku.draw_for` appended, i.e. the LAST
            # -- not `index()`'s first match, which points at an older copy when
            # the hand already held that tile and disagrees with slot 13, the
            # only slot the client's auto-discard can name.
            slot_flags = [0] * 14
            idx = len(h.tiles) - 1
            if h.drawn is not None and h.tiles and h.tiles[-1] != h.drawn:
                idx = (h.tiles.index(h.drawn) if h.drawn in h.tiles
                       else len(h.tiles) - 1)
            slot_flags[idx] = 2
            # VERIFIED: RIICHI TSUMOGIRI (2026-09-12): with the +0x4b lock on, +0x63
            # makes the client discard the drawn tile with no input at all
            # (mahdisp.c:3645 -- `+0x63 && DAT_003865bc[seat] && DAT_003e4e08 &&
            # !menu_was_opened`). DAT_003e4e08 is never written by ANY of the
            # 4,146 decompiled functions and its .data image at 0x3e4e08 holds
            # 1, so that clause is a constant -- the gate is really ours. The
            # client returns hand slot **0xd**, always (the free cursor is
            # skipped while the lock is on, so the cursor never leaves its
            # initial 0xd), so this may only be armed when the drawn tile really
            # is at slot 13: true for a 14-tile closed hand, FALSE after an
            # ankan (10 tiles + rinshan), which is why `idx == 13` gates it.
            # And never when the seat still has a decision to make -- a menu
            # with Tsumo or a Kan lit must reach the player, so `any(menu)`
            # withholds it. A riichi hand offers nothing else: Riichi is gated
            # on `not h.riichi` and Kyushu on the first go-round.
            auto = 1 if (RIICHI_AUTO and seat in self.live and idx == 13
                         and h.drawn is not None and not any(menu)) else 0
        elif seat in self.live and menu[self.MENU_RIICHI]:
            # Riichi is OFFERED this turn. Every occupied slot keeps bit 1 (a
            # normal discard is still allowed if the player cancels riichi), and
            # each riichi-legal discard also gets bit 0 -- without which the
            # client's riichi picker finds no landing slot and freezes at cursor
            # 1000 (mahdisp.c:3546-3561). See _riichi_discard_slots.
            slot_flags = [2 if i < len(h.tiles) else 0 for i in range(14)]
            for i in self._riichi_discard_slots(h):
                slot_flags[i] |= 1
        elif seat in self.live and h.drawn is None:
            # The turn right after this seat's own chi/pon: the kinds kuikae
            # forbids lose bit 1 (not discardable) so the picker cannot land
            # on a tile the engine would refuse.
            forbidden = k.kuikae_forbidden(seat)
            if forbidden:
                slot_flags = [2 if (i < len(h.tiles)
                                    and mj.kind(h.tiles[i]) not in forbidden)
                              else 0 for i in range(14)]
        rec = M.tsumo(self.next_seq(), seat, h.tiles, self.wall_count(),
                      is_human=self._is_human(),
                      motion=motion, slot_flags=slot_flags,
                      riichi_test=menu,
                      # WARNING: +0x4b IS THE CLIENT'S OWN RIICHI LOCK (2026-09-12):
                      # console.c:3400 copies it into this seat's
                      # DAT_003865bc+seat*0x34, the byte mahdisp.c:3607 tests
                      # before it runs the FREE hand cursor at all -- non-zero
                      # and the cursor cannot leave the drawn tile. +0x54 alone
                      # is not enough: its bit 1 only binds the two MENU-driven
                      # pickers (mahdisp.c:3492 discard, :3554 riichi), and the
                      # ordinary "press X on a tile" path (mahdisp.c:3607-3672)
                      # never reads +0x54, which is why a riichi hand could
                      # still discard from anywhere in the hand. MjALLDATA
                      # +0x13a and MjNAKI +0x34 already carry this per seat; the
                      # draw record is the one that was clearing it back to 0.
                      f4b=1 if h.riichi else 0,
                      f63=auto,
                      per_seat=tuple(1 if k.hands[s].riichi else 0 for s in range(4)),
                      anim_tile=anim_tile, anim_slot=anim_slot,
                      anim_seat=anim_seat, riichi=riichi)
        self._awaiting = (M.MjTSUMO, seat)
        if discard is not None:
            # THE opponent-discard record. anim_seat (+0x22) is the seat the
            # client animates the tile FOR -- if a COM's tile lands in the
            # centre instead of its own pond, this is the field to check it
            # against on screen.
            self.trace("DISCARD %s tile=%s slot=%d  [motion 3, anim_seat=%d%s]"
                       % (self._seatname(discard[2]), _tile(discard[0]),
                          discard[1], discard[2], ", RIICHI +0x21" if riichi else ""))
        elif motion in (2, 9):
            self.trace("DRAW  %s tile=%s  [motion %d%s%s]"
                       % (self._seatname(seat), _tile(h.drawn), motion,
                          " rinshan" if motion == 9 else "",
                          # The one string that proves the tsumogiri path is
                          # LIVE: the client should answer with MjSUTE slot 13
                          # and no input at all.
                          ", RIICHI LOCK +0x4b" + (" + AUTO-DISCARD +0x63"
                                                   if auto else "")
                          if h.riichi else ""))
        # A DRAW IS PRIVATE, A DISCARD IS NOT. The draw form carries that
        # seat's hand AND its action menu (`riichi_test`), so sending it to the
        # other human would both show them the tiles and offer them somebody
        # else's Riichi/Tsumo/Kan buttons. The discard form is the animation
        # everyone has to see, so it still goes to everyone.
        #
        # `seat in self.live` keeps a BOT's draw broadcast, which is what makes
        # a COM's turn visible at all (the motion-2 animation).
        _to = seat if (discard is None and seat in self.live) else None
        return self._emit(rec, "draw seat %d" % seat if discard is None
                          else "discard seat %d" % discard[2], to=_to)

    #: The call menu's per-action byte inside the MjNAKI +0x4c 7-byte block,
    #: measured from the client's static tables (the OpenChoice window's
    #: item->block map DAT_004099d0 = [1,5,4,3,-1] for [Ron,Chi,Pon,Kan,Cancel]
    #: and its selection->action map DAT_00386210). Bytes 0/2/6 are the MyMove
    #: menu's Tsumo/Riichi/Kyushu and stay zero in a call offer.
    NAKI_MENU_BYTE = {"ron": 1, "kan": 3, "pon": 4, "chi": 5}

    def msg_naki(self, tile, options, from_seat=None):
        k = self.kyoku
        calls = {}
        slots = {}
        menus = {}
        for seat, opt in options.items():
            calls[seat] = {name: True for name in opt}
            blk = bytearray(7)
            for name in opt:
                blk[self.NAKI_MENU_BYTE[name]] = 1
            menus[seat] = bytes(blk)
            h = k.hands[seat]
            flags = [0] * 14
            if "chi" in opt:
                # The client's chi navigator walks EXACTLY the flagged slots
                # (mahdisp__002c36e0 copies +0x68 and steps between `& 7`
                # non-zero entries; the slot it stops on is the tile the ack
                # carries) -- so flag precisely the kinds that appear in some
                # legal chi pair with this discard, and nothing else. A pon
                # kind must NOT be flagged here: the cursor could stop on it
                # and send a chi the pair matcher cannot place.
                kinds = set()
                for pair in mj.chi_options(h, tile):
                    for t in pair:
                        kinds.add(mj.kind(t))
                for i, t in enumerate(h.tiles[:14]):
                    if mj.kind(t) in kinds:
                        flags[i] = 1
            elif "pon" in opt or "kan" in opt:
                # The pon/kan arms match the claimed tile by id, not by slot;
                # these flags are only the hand highlight.
                for i, t in enumerate(h.tiles[:14]):
                    if mj.kind(t) == mj.kind(tile):
                        flags[i] = 1
            slots[seat] = flags
        rec = M.naki(self.next_seq(), self.wall_count(), calls=calls,
                     is_human=self._is_human(),
                     # +0x28 (_DAT_00451f88) is the claimable tile itself: the
                     # client's pon arm spins scanning the hand for a tile with
                     # this id, so an unset value hangs the client.
                     u28=M.tile_u16(tile),
                     # +0x2a is the discarder; the client highlights that
                     # seat's newest pond tile (DAT_00452050 = value+1).
                     s2a=max(0, k.last_discard_seat if from_seat is None
                             else from_seat),
                     menu=menus, slot_flags=slots,
                     per_seat_34=tuple(1 if k.hands[s].riichi else 0
                                       for s in range(4)))
        self.pending_naki = (tile, options)
        self.naki_answers = {}
        self._awaiting = (M.MjNAKI, next(iter(options)) if options else None)
        for seat, opt in options.items():
            self.trace("OFFER call to %s on %s: %s  [client shows the call "
                       "buttons for these; a bare Cancel = it disagrees]"
                       % (self._seatname(seat), _tile(tile),
                          "/".join(sorted(opt))))
        return self._emit(rec, "calls on %s" % mj.name(tile))

    @staticmethod
    def _payment_words(score, winner, dealer, ron, honba):
        """MjYAKUDISP +0x64 (base) / +0x68 (dealer share), in POINTS, from the
        engine's per-seat payments with the honba stripped -- the client sums
        them itself (ron: base; dealer tsumo: base x3; other tsumo: dealer
        share + base x2, mahdisp.c:722-741)."""
        pay = getattr(score, "payments", None) or {}
        paid = {}
        for s, d in pay.items():
            if s != winner and d < 0:
                paid[s] = -d - (300 if ron else 100) * honba
        if ron:
            base = max(paid.values()) if paid else int(getattr(score, "points", 0))
            return max(0, base), 0
        if winner == dealer:
            base = max(paid.values()) if paid else int(getattr(score, "points", 0)) // 3
            return max(0, base), 0
        dealer_share = paid.get(dealer, 0)
        others = [v for s, v in paid.items() if s != dealer]
        base = max(others) if others else int(getattr(score, "points", 0)) // 4
        return max(0, base), max(0, dealer_share)

    def msg_yakudisp(self, seat, score, ron_from=None):
        """The win screen, every field where mahdisp__002bcba0 reads it
        (janmsgs' MjYAKUDISP banner, 2026-09-04): the seat block (+0x18 han,
        +0x19 fu, +0x1a WINNER, +0x1b dealer, +0x1c ron), the round header
        (+0x1d wind, +0x1e kyoku, +0x1f honba), the yakuman and show-ura
        flags, the (yaku id, han) PAIRS at +0x22 and the two payment words.

        WARNING: History: +0x22 used to carry the TILES (the panel draws those from
        the table state, not from here), so the client indexed its yaku table
        with tile codes and invented yaku; +0x64/+0x68 were never written
        ("0 pt"); +0x1d..+0x21 were zero, so the header read E1 and the ura
        never spread. And before 81afa6c3 the winner was in +0x18.
        """
        k = self.kyoku
        h = k.hands[seat]
        sc = self._round_scalars()
        ron = 0 if ron_from is None else 1
        rows = M.yaku_rows(list(getattr(score, "yaku", []) or []))
        # +0x20 -> DAT_00446c78: non-zero forces the limit path. A counted
        # yakuman reaches it by han (13+) anyway; the flag keeps the client's
        # own yakuman bookkeeping honest for both.
        yakuman = 1 if getattr(score, "limit", "") in ("yakuman", "kazoe_yakuman") else 0
        show_ura = 1 if (h.riichi and getattr(self.rules, "ura_dora", True)) else 0
        base, dealer_share = self._payment_words(score, seat, k.dealer, ron,
                                                 k.honba)
        rec = M.yakudisp(self.next_seq(), han=min(255, score.han),
                         fu=min(255, score.fu), winner=seat, dealer=k.dealer,
                         ron=ron, round_wind=sc.round_wind, kyoku=sc.kyoku,
                         honba=sc.honba, yakuman=yakuman, show_ura=show_ura,
                         yaku=rows, pay_base=base, pay_dealer=dealer_share)
        self._awaiting = (M.MjYAKUDISP, None)
        self.trace("YAKU  %s: %s  han=%d fu=%d  base=%d dealer=%d%s%s"
                   % (self._seatname(seat),
                      " ".join("%d:%d" % r for r in rows) or "(no rows)",
                      score.han, score.fu, base, dealer_share,
                      "  YAKUMAN" if yakuman else "",
                      "  ura shown" if show_ura else ""))
        return self._emit(rec, "yaku for seat %d (%s)"
                          % (seat, "ron" if ron else "tsumo"))

    def msg_seisan(self):
        sc = self._round_scalars()
        # +0x28..+0x2c in the reader's order (mahdisp.c:788-793): current
        # seat, dealer, round wind, kyoku, honba. Not wind/kyoku/honba/sticks.
        rec = M.seisan(self.next_seq(), self.kyoku.scores,
                       current_seat=sc.current_seat, dealer=sc.dealer,
                       round_wind=sc.round_wind, kyoku=sc.kyoku, honba=sc.honba)
        self._awaiting = (M.MjSEISAN, None)
        self._seisan_ready = set()          # MjREADYSTATUS starts over per settlement
        return self._emit(rec, "settlement")

    def msg_alldata(self, subtype=0, discard_event=None, hands=None, ponds=None,
                    call=None, label=None, reveal=None, wanpai=None,
                    seat_mask=None, seat_flags=None, to=None, emit=True):
        """The whole table. `discard_event=(tile, seat)` makes it a subtype-3
        DRAW+PREP: the client pops the tile onto the seat's hand and CLEARS the
        settled-draw flag of that seat's last pond tile, so a motion-3 record
        sent immediately after flies the discard with no settled pre-draw. The
        pond for that seat must already end with the discard (call this AFTER
        k.discard()). See DISCARD_ANIM.

        `subtype` is ALSO the MotionCommand -- same byte -- which is what makes
        this the message a granted call rides on. `hands`/`ponds` override the
        live board for exactly that case (a call motion wants PRE-call hands and
        ponds against POST-call melds; see janmsgs' CALL MOTIONS banner), and
        `call` is `{"from": discarder, "tiles": [...], "slots": [...],
        "seat": caller}`. Use `msg_call()` / `msg_kan()`.

        `reveal` is a set of seats whose hands stay populated even under
        CONCEAL_RESYNC (a win reveal, the tenpai seats at a draw); `wanpai`
        overrides the +0x44 dead wall (a kan record); `seat_mask` writes +0x22
        (motion 10's seat bitmask); `seat_flags` the +0x23..+0x26 per-seat
        block (1 on a kan caller's seat = hold the rinshan flight).

        `emit=False` builds the record WITHOUT `_emit` (a spectator's join
        snapshot: no route, no deadline reset, no spectator copy)."""
        k = self.kyoku
        if hands is None:
            hands = [list(k.hands[s].tiles) for s in range(4)]
        full_hands = [list(hd) for hd in hands]     # before any concealment
        reveal = set(reveal or ())
        if CONCEAL_RESYNC:
            # Blank every non-local seat's HAND (see CONCEAL_RESYNC) EXCEPT the
            # seat this record animates. An empty hand draws NOTHING (the safe
            # uVar1==0 path), matching the deal's concealed state, so the two
            # non-animating opponents never "reappear" face-up.
            #
            # WARNING: THE ANIMATING SEAT KEEPS ITS HAND. A subtype-3 discard flies
            # the tile from that seat's hand area, and a call motion builds its
            # meld from the caller's hand slots -- blank either and the fly
            # springs from nowhere (the 44e4fbbd centre-slingshot class). The
            # animating seat is the discarder (subtype 3) or the caller (a call
            # motion); a plain resync animates nobody, so all three opponents
            # blank. Ponds and melds below stay real -- they are public.
            anim_seat = (discard_event[1] if discard_event is not None
                         else (call.get("seat") if call is not None else None))
            hands = [list(hands[s]) if (s in self.live or s == anim_seat
                                        or s in reveal)
                     else [] for s in range(4)]
        if ponds is None:
            ponds = [self._pond_wire(s) for s in range(4)]
        melds = [M.meld_block(k.hands[s].melds) for s in range(4)]
        meld_types = {s: M.meld_types(k.hands[s].melds) for s in range(4)}
        sc = self._round_scalars()
        ev_tile = ev_seat = ev_present = 0
        if discard_event is not None:
            subtype = 3
            ev_tile = M.tile_byte(discard_event[0])
            ev_seat = discard_event[1] & 0xFF
            ev_present = 1
        if call is not None:
            # +0x22 is the CALLER on a call motion. +0x1c/+0x1e are overwritten
            # by the call fields inside M.alldata(), so the event bytes here are
            # dead and stay zero.
            ev_seat = call["seat"] & 0xFF
        seq = self.next_seq()

        def build(hands_):
            return M.alldata(seq, hands_, ponds, melds=melds,
                        is_human=self._is_human(),
                        meld_types=meld_types,
                        # +0x44 is the DEAD WALL (byte-per-tile here, u16 in
                        # MjHAIPAI) -- see msg_haipai's banner and janmsgs'
                        # WAMPAI banner: 0x40 = REVEALED, rinshan slot 0 empty
                        # once a kan has drawn, so a resync agrees with what the
                        # client's own flips, re-fills and motion-9 draws did to
                        # its copy.
                        position=self._wanpai_bytes() if wanpai is None else wanpai,
                        scores=k.scores, subtype=subtype,
                        current_seat=sc.current_seat,
                        wall_count=min(0x7F, self.wall_count()),
                        # +0x13b..+0x13f by NAME (console.c:3923-3935): honba,
                        # the riichi-stick pot (+0x13c, ONLY written here --
                        # a 0 blanked "ReachBou" every bot turn), dealer/
                        # kyoku/chiicha in +0x13d, the round wind in +0x13e
                        # bit 0 and the dice in +0x13f.
                        honba=sc.honba, riichi_sticks=sc.riichi_sticks,
                        dealer=sc.dealer, kyoku=sc.kyoku, chiicha=sc.chiicha,
                        round_wind=sc.round_wind, dice=sc.dice,
                        seat_mask=seat_mask,
                        seat_flags=tuple(seat_flags) if seat_flags else (0, 0, 0, 0),
                        f13a=sum((1 if k.hands[s].riichi else 0) << (2 * s)
                                 for s in range(4)),
                        # +0x136: the sideways riichi tile, per seat.
                        tail136=self._riichi_indices(),
                        event_tile=ev_tile, event_seat=ev_seat,
                        event_present=ev_present,
                        call_from=(call or {}).get("from", 0),
                        call_tiles=(call or {}).get("tiles", ()),
                        call_slots=(call or {}).get("slots", ()))
        rec = build(hands)
        if not emit:
            return rec
        # The spectators' copy under CONCEAL_RESYNC: same seq, no seat
        # blanked (GALLERY banner). Off (the default) `rec` is already full.
        gallery_rec = (build(full_hands) if CONCEAL_RESYNC and self.gallery
                       and hands != full_hands else None)
        return self._emit(rec, label or ("resync" if discard_event is None
                                         else "draw+prep seat %d"
                                         % discard_event[1]), to=to,
                          gallery=gallery_rec)

    #: Which motion animates each granted call (janmsgs' CALL MOTIONS banner).
    #: The three KAN motions are MEASURED (2026-09-04) and driven by `msg_kan()`
    #: behind KAN_MOTION (default off). Both of the 09-03 blockers resolved in
    #: the C: the chain's gate DAT_00452185 is MjHAIPAI +0x2d = the DORA rule
    #: enum (now sent from the rules), and the "new indicator still face-down"
    #: requirement was INVERTED -- 0x80 in the wall array is REVEALED, the chain
    #: scans 12,10,8,6 for the newest revealed indicator, so the engine's eager
    #: reveal is exactly what the record must carry (janmsgs' WAMPAI banner).
    #: And the rinshan draw is NOT a separate motion-9 MjTSUMO: the MjALLDATA
    #: handler synthesises it from +0x1c (janmsgs' CALL MOTIONS banner).
    CALL_MOTION = {"chi": 5, "pon": 4, "minkan": 6, "daiminkan": 6, "kan": 6,
                   "ankan": 7, "kakan": 8, "shouminkan": 8}

    def call_snapshot(self):
        """Freeze the hands and ponds a call motion animates against.

        Call this BEFORE the engine forms the meld. The motion hides the hand
        slots it is handed and pops the discarder's pond tail itself, so the
        record must still show both -- while its MELD block has to be the
        post-call one, because the client's free-meld scan lands on the LAST
        OCCUPIED meld and the tiles it draws come from that block. One record,
        two tenses; janmsgs' CALL MOTIONS banner has the derivation.

        Unpacks as `(hands, ponds)` exactly as before; the dead-wall state a KAN
        record needs (`dora_shown`, `kans`) rides as attributes.
        """
        k = self.kyoku
        return CallSnapshot([list(k.hands[s].tiles) for s in range(4)],
                            [self._pond_wire(s) for s in range(4)],
                            dora_shown=k.wall.dora_shown, kans=k.wall.kans)

    @staticmethod
    def taken_slots(pre_hand, post_hand):
        """Which slots of `pre_hand` the meld consumed, in order.

        A multiset walk rather than a set difference: two copies of a kind are
        exactly the case a pon hits, and only one of them left the hand.
        """
        rest = list(post_hand)
        out = []
        for i, t in enumerate(pre_hand):
            if t in rest:
                rest.remove(t)
            else:
                out.append(i)
        return out

    def msg_call(self, action, seat, pre, discarder):
        """A granted pon/chi as SE animates it: ONE MjALLDATA carrying the motion.

        WARNING: NOTHING MAY FOLLOW THIS BUT THE CALLER'S OWN MjTSUMO. The motion has
        already hidden the hand slots and zeroed the pond tail; a plain
        MjALLDATA behind it puts the claimed tile back in the pond and un-hides
        the slots -- the generalised form of the 09-03 slingshot. MjTSUMO is
        safe because MjClient_Tsumo dispatches its motion BEFORE it rewrites the
        hand array from +0x2a, so the repacked hand lands after the animation.
        """
        k = self.kyoku
        motion = self.CALL_MOTION[action]
        pre_hands, pre_ponds = pre
        slots = self.taken_slots(pre_hands[seat], k.hands[seat].tiles)[:2]
        while len(slots) < 2:                       # never index off the hand
            slots.append(0)
        tiles = [M.tile_byte(pre_hands[seat][i]) for i in slots]
        self.trace("CALL  %s %s from %s  slots=%s  [motion %d -- banner, sound, "
                   "meld build and pond pop are all client-side]"
                   % (self._seatname(seat), action.upper(),
                      self._seatname(discarder), slots, motion))
        return self.msg_alldata(
            subtype=motion, hands=pre_hands, ponds=pre_ponds,
            # +0x1d is unread by motions 4/5; +0x1e/+0x1f are the two slots.
            call={"from": discarder, "seat": seat,
                  "tiles": tiles, "slots": [0] + slots},
            label="%s seat %d" % (action, seat))

    def msg_kan(self, kind, seat, pre, discarder=None, rinshan=None):
        """A granted KAN -- daiminkan / ankan / kakan -- as records.

        `kind` is "minkan" (= "daiminkan" / "kan"), "ankan" or "kakan"
        (= "shouminkan"); `pre` is the `call_snapshot()` taken BEFORE the
        engine formed the kan (and drew the rinshan tile); `discarder` the seat
        whose discard a daiminkan claims. Call it AFTER `k.call_pon(seat,
        kan=True)` / `call_ankan` / `call_kakan` and send NOTHING after it but
        what the caller's turn needs -- the rinshan draw is already inside.

        `rinshan`: "now" (the rinshan tile is in hand: the record carries it),
        "defer" (a PROVISIONAL kakan/ankan holding for the chankan window: the
        record holds the flight and the caller sends `msg_tsumo(seat,
        motion=9)` after `complete_kakan()`), or None = "now" when the hand
        has drawn (`h.drawn`) else "defer".

        KAN_MOTION off (default): today's resync -- MjALLDATA subtype 0 (the
        post-kan board, rinshan tile in hand) + MjTSUMO motion 2. Proven live
        for a daiminkan; this also gives ankan/kakan the resync they lacked
        (the meld used to appear only on the next bot turn). A deferred kan
        sends the resync alone; the later motion-9 MjTSUMO is then a motion 2.

        KAN_MOTION on: ONE MjALLDATA carrying motion 6/7/8 with the fields the
        handler reads (yamaguchi__002e8ae0 / 002e9a40 / 002e93f0 -- janmsgs'
        CALL MOTIONS banner) in three tenses (PRE hands and ponds, POST melds),
        the dead wall with the new indicator REVEALED and the rinshan tile still
        in its slot, and the RINSHAN TILE at +0x1c: the MjALLDATA handler itself
        chains the kan-dora flip, re-fills the dead wall and flies that tile to
        hand slot 13 (console.c:4101-4118 -> yamaguchi__002e2660 = a synthesised
        motion 9). The MjTSUMO that follows carries motion 0 -- a motion 9
        there would pull a SECOND tile out of the dead wall. Falls back to the
        resync form when the hand slots do not have the shape the motion needs
        (ankan wants three consecutive slots + one).
        """
        k = self.kyoku
        h = k.hands[seat]
        motion = self.CALL_MOTION.get(kind)
        if motion not in (6, 7, 8):
            raise ValueError("msg_kan: kind %r is not a kan" % (kind,))
        if rinshan is None:
            rinshan = "now" if h.drawn is not None else "defer"
        deferred = rinshan == "defer"
        pre_hands, pre_ponds = pre
        post = list(h.tiles)
        # The rinshan tile is in `post` (draw_for appended it) and not in
        # `pre`, so the multiset walk names exactly the slots the kan took.
        slots = self.taken_slots(pre_hands[seat], post)
        need = {6: 3, 7: 4, 8: 1}[motion]
        shape_ok = len(slots) == need
        if motion == 7 and shape_ok:
            shape_ok = (slots[1] == slots[0] + 1 and slots[2] == slots[0] + 2)
        meld_index = None
        if motion == 8:
            for i, m in enumerate(h.melds):
                if m.kind == mj.KAKAN and slots and \
                        m.base == mj.kind(pre_hands[seat][slots[0]]):
                    meld_index = i
            shape_ok = shape_ok and meld_index is not None
        if not KAN_MOTION or not shape_ok:
            if KAN_MOTION and not shape_ok:
                self.log.append(("kan-shape", (kind, seat, slots)))
            self.trace("KAN   %s %s  [resync path%s%s]"
                       % (self._seatname(seat), kind,
                          "" if not KAN_MOTION else ", motion shape unavailable",
                          ", rinshan deferred" if deferred else ""))
            out = [self.msg_alldata(label="kan resync seat %d" % seat)]
            if not deferred:
                out.append(self.msg_tsumo(seat, motion=2))
            return out
        tile = M.tile_byte(pre_hands[seat][slots[0]])
        rinshan_tile = 0 if deferred else M.tile_byte(h.drawn)
        if motion == 6:
            call_slots = slots[:3]
        elif motion == 7:
            call_slots = [slots[0], slots[3], 0]
        else:
            call_slots = [meld_index, slots[0], 0]
        # The dead wall the motion reads: the rinshan tile STILL in its slot
        # (the handler's own motion 9 takes it), the new indicator ALREADY
        # revealed at its 4+2k slot (the chain scans 12,10,8,6 for the newest
        # revealed and flips it). A daiminkan/kakan indicator the engine
        # defers to the next discard (`pending_dora`) is shown now, as the
        # client will; a provisional kan has not counted its dora yet, so the
        # record shows one more than the engine does.
        pre_kans = getattr(pre, "kans", max(0, k.wall.kans - 1))
        pending = int(getattr(k, "pending_dora", 0))
        if deferred and getattr(k, "pending_kan", None) is not None:
            pending += 1
        shown = min(5, k.wall.dora_shown + pending)
        wanpai = self._wanpai_bytes(dora_shown=shown, kans=pre_kans)
        flags = [0, 0, 0, 0]
        if deferred:
            flags[seat] = 1                 # +0x23+seat: hold the rinshan flight
        self.trace("KAN   %s %s%s  slots=%s  [motion %d; kan-dora flip, meld "
                   "build, pond pop and the rinshan draw (%s) are client-side]"
                   % (self._seatname(seat), kind,
                      " from %s" % self._seatname(discarder)
                      if motion == 6 and discarder is not None else "",
                      slots, motion,
                      "HELD for the chankan window" if deferred
                      else "+0x1c " + _tile(h.drawn)))
        rec = self.msg_alldata(
            subtype=motion, hands=pre_hands, ponds=pre_ponds, wanpai=wanpai,
            call={"from": discarder or 0, "seat": seat,
                  "tiles": [tile, rinshan_tile], "slots": call_slots},
            seat_flags=flags, label="%s seat %d" % (kind, seat))
        if deferred:
            return [rec]
        return [rec, self.msg_tsumo(seat, motion=0)]

    def msg_draw_reveal(self):
        """The exhaustive-draw beat: ONE MjALLDATA with motion 10 and the
        TENPAI seats in +0x22 -- console.c:4085-4099 fires banner 6 (the 流局
        cut-in) and flies those seats' hands in from the hand block, so the
        block carries their real tiles; noten opponents stay blank (they do
        not show). An abortive draw sends mask 0: the banner still fires and
        nothing flies. Nothing follows it but MjSEISAN -- the motion mutates
        no state, so no resync is owed either.
        """
        k = self.kyoku
        kind_, data = k.result if k.result is not None else (None, {})
        shown = set()
        if kind_ == mj.DRAW and isinstance(data, dict):
            shown = set(data.get("tenpai", ()) or ())
            shown |= set(s for s, _sc in (data.get("nagashi", ()) or ()))
        mask = sum(1 << s for s in shown)
        hands = [list(k.hands[s].tiles) if (s in shown or s in self.live) else []
                 for s in range(4)]
        self.trace("RYUKYOKU banner  tenpai=%s  [motion 10, mask %#x]"
                   % ("/".join(self._seatname(s) for s in sorted(shown)) or "nobody",
                      mask))
        return self.msg_alldata(subtype=10, hands=hands, seat_mask=mask,
                                reveal=shown, label="draw reveal")

    def _record_result(self, rank):
        """Write the finished hanchan into each LIVE seat's record.

        THE ONLY PLACE A GAME IS PERSISTED, and it is here rather than in
        `msg_gameend` on purpose: the escape hatch and a dropped table both reach
        MjGAMEEND without a finished hanchan, and an abandoned game must not
        count as one played. `_recorded` makes it once per game whatever the
        client re-acks.

        WARNING: LIVE SEATS ONLY. `bot_id()` mints NON-ZERO ids deliberately -- the
        client derives its player count by counting non-zero ids -- so "the id is
        not 0" is NOT a test for "this is a person". `self.live` is. janstats
        refuses a bot id as a second line of defence.

        A stats failure is logged and swallowed: the hanchan is over either way
        and the player is looking at the results screen. Losing a record is bad;
        throwing here would lose the screen too.
        """
        if self._recorded or janstats is None or not STATS_ENABLE:
            return
        self._recorded = True
        by_seat = {r["seat"]: r for r in rank}
        hako = getattr(self.game, "end_reason", None) == "hako"
        for seat in sorted(self.live):
            # OUR account number when we hold one (the lobby path seats the
            # PolID as the wire id; the record is keyed by the member).
            member, r = self.member_of_seat(seat), by_seat.get(seat)
            if not member or r is None:
                continue
            try:
                if janstats.record_game(member, r["place"], r["score"],
                                        r["result"], table_id=self.id,
                                        bust=bool(hako and r["score"] < 0)):
                    self.log.append(("recorded", (member, seat, r["place"])))
            except Exception as e:
                self.log.append(
                    ("stats-error", "seat %d member %r: %s: %s"
                     % (seat, member, type(e).__name__, e)))

    def _history_rows(self, rows=4):
        """Rows 1..4 of `Ranking[5][4]` -- each seat's own recent placings.

        MEASURED: the geometry (`Ranking[row][seat]` = u32 at `+0x18 + row*0x10 +
        seat*4`), that the values are zero-based, and that `< 0` prints the "no
        rank" form. INFERRED: that rows 1..4 are history at all. The client's own
        log line names them `Ranking[1]`..`Ranking[4]` and nothing states what
        they hold; this reads them COLUMN-wise, so column `seat` is that seat's
        last four games.

        WARNING: The fill is `NO_RANK`, never 0. Both readings of those rows agree that
        an unknown slot has to be negative -- 0 is first place.

        `skip=1` because `_record_result` has already put the game that just
        finished at the head of the history, and it is row 0 on the wire.
        """
        out = [[M.NO_RANK] * 4 for _ in range(rows)]
        if janstats is None or not STATS_ENABLE:
            return out
        skip = 1 if self._recorded else 0
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                past = janstats.recent_places(member, n=rows, skip=skip)
            except Exception as e:
                self.log.append(("stats-error", "history seat %d: %s" % (seat, e)))
                continue
            for row in range(rows):
                out[row][seat] = past[row]
        return out

    def _money(self, seat, result):
        """(JAN before, JAN after) for a LIVE seat from its record -- the
        record already holds this game, and `record_game` kept the balance it
        started from in `money_prev`. The balance is floored at 0 game by game
        (janstats `money_balance`), so 'before' cannot be backed out of the
        lifetime sum: after two losses and a win that sum is still negative,
        and the results screen counted 0 -> 0 JAN for a won game."""
        member = self.member_of_seat(seat)
        if seat not in self.live or not member or janstats is None or not STATS_ENABLE:
            return 0, 0
        try:
            rec = janstats.load(member)
            after = janstats.derive(rec)["money"]
            before = rec.get("money_prev")
            if not isinstance(before, int):
                before = max(0, after - janstats.jan_for_points(result))
            return before, after
        except Exception as e:
            self.log.append(("stats-error", "money seat %d: %s" % (seat, e)))
            return 0, 0

    def msg_results(self):
        """HALF1 then HALF2 -- but only HALF1 goes out now; its ack pulls HALF2.

        HALF1 is PER SEAT (janmsgs' banner, 2026-09-04): the readers walk
        4 x u32 columns indexed by seat and animate stage to stage --
        raw score (+0x58/+0x68, the disconnect-penalty stage, skipped when
        equal) -> +/- points (+0x78: score - return, plus the oka for 1st) ->
        after uma (+0x88, the stage the client labels from HAIPAI +0x3c/+0x3d)
        -> after yakitori (+0x98, stage gated on HAIPAI +0x33) -> final
        (+0xa8 = `ranking()['result']`, the sashiuma stage gated on the +0xec
        block) -> money (+0x18 -> +0x38, JAN). Anything the engine folds into
        `result` beyond uma/oka/sashiuma (yakitori, when it lands) shows in the
        yakitori stage.

        WARNING: +0x78..+0xab ARE IN **P** -- THE +/-45 POINT, NOT THE 25000 SCORE
        (2026-09-12). We sent them x1000 and the results screen took OVER HALF
        AN HOUR to count up. Three of the five stages -- uma
        (`yamaguchi2__00332610`), yakitori (`00333370`) and sashiuma
        (`00333a90`) -- size their animation off the DATA:

            n = max over seats of (after - before)
            step[seat] = (after - before) / n     (floored, minimum 1)
            for n frames: display += step         <- ONE FRAME PER UNIT

        so the stage runs for as many frames as the biggest delta. At x1000 a
        10-20 uma is 20000 frames (5.5 min at full speed, far worse under
        emulation) and 30-90 is 90000; in P it is 20 and 90 frames -- the same
        ballpark as the two stages SE hard-coded to 0x5a (the penalty stage
        `00331f40` and the money stage `00334210`, which both divide by 0x5a
        and loop 0x5a, and so are scale-free). P is also the unit the money
        stage converts at ("This table's rate is 1P = %d JAN", and janstats'
        money = points x rate), so +0xa8 has to be the same number the JAN
        conversion is quoted against. +0x58/+0x68 stay RAW SCORES: that stage
        is the score column and its ramp is the scale-free 0x5a one.
        """
        rank = self._sashiuma_apply(self.game.ranking())
        self._record_result(rank)
        rules = self.game.rules
        by_seat = {r["seat"]: r for r in rank}
        genten = getattr(rules, "genten", 25000)
        ret = getattr(rules, "oka_return", 30000)
        uma = list(getattr(rules, "uma", (20, 10, -10, -20))) + [0] * 4
        oka = (ret - genten) * 4 / 1000.0        # in P, like everything below
        stake = self._sashiuma_stake() if self.sashiuma_pairs else 0
        sashi = [0.0] * 4
        for a, b in self.sashiuma_pairs or ():
            ra, rb = by_seat.get(a), by_seat.get(b)
            if ra is None or rb is None:
                continue
            win, lose = (a, b) if ra["place"] < rb["place"] else (b, a)
            sashi[win] += stake
            sashi[lose] -= stake
        places, scores, points, after_uma, after_yaki, final = ([0] * 4 for _ in range(6))
        money_before, money_after = [0] * 4, [0] * 4
        for s in range(4):
            r = by_seat[s]
            places[s] = r["place"]
            scores[s] = int(r["score"])
            # Every column below is P. The stage values are the RUNNING TOTAL
            # at each step, so each one is rounded from the exact figure rather
            # than from the previous (already rounded) stage -- that keeps the
            # last stage equal to the result we record and pay in JAN.
            base = (scores[s] - ret) / 1000.0 + (oka if r["place"] == 0 else 0)
            points[s] = _round_p(base)
            after_uma[s] = _round_p(base + int(uma[r["place"]]))
            final[s] = _round_p(float(r["result"]))
            after_yaki[s] = _round_p(float(r["result"]) - sashi[s])
            money_before[s], money_after[s] = self._money(s, r["result"])
        blk = bytearray(6)
        for a, b in self.sashiuma_pairs or ():
            blk[M.sashiuma_slot(a, b)] = M.sashiuma_bit(a) | M.sashiuma_bit(b)
        rec = M.gameresult_half1(self.next_seq(), places=places, scores=scores,
                                 points=points, after_uma=after_uma,
                                 after_yakitori=after_yaki, final=final,
                                 money_before=money_before,
                                 money_after=money_after, sashiuma=bytes(blk))
        self._awaiting = (M.MjGAMERESULTHALF1, None)
        self.trace("RESULT places=%s scores=%s points=%s final=%s (P)"
                   % (places, scores, points, final))
        return self._emit(rec, "results 1")

    def _levels(self):
        """`([Level per seat], [LevelUp per seat])` for MjGAMERESULTHALF2.

        THE LEVEL A PLAYER SEES COMES FROM HERE. It is not in the save --
        measured 2026-08-24, `U/g/MJSUserData` has no level field anywhere in
        the module; `yamaguchi2__00335050` reads it out of THIS message at
        `+0xA0 + seat*4`. So if we send zero, the results screen says "Your
        level is now 0".

        `LevelUp` is the DELTA and doubles as a flag: non-zero makes the client
        draw "Level up! Now %d.". `janstats.level` / `level_up` are the one
        ladder every screen reads (the lobby row, the popup, this) -- the
        delta is claimed only for the game just recorded.
        """
        levels, ups = [1] * 4, [0] * 4
        if janstats is None or not STATS_ENABLE:
            return levels, ups
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                levels[seat] = int(janstats.level(member))
                ups[seat] = int(janstats.level_up(member)) if self._recorded else 0
            except Exception as e:
                self.log.append(("stats-error", "level seat %d: %s" % (seat, e)))
        return levels, ups

    def _titles(self):
        """`(shogo, getshogo)` for HALF2 -- [title][seat], titles HELD and
        titles EARNED by the game just recorded, from the record screen's
        own thresholds (janstats). All zero without a record: `getshogo`
        non-zero makes the client announce "You earned the next title."."""
        held = [[0] * 4 for _ in range(5)]
        got = [[0] * 4 for _ in range(5)]
        if janstats is None or not STATS_ENABLE:
            return held, got
        for seat in sorted(self.live):
            member = self.member_of_seat(seat)
            if not member:
                continue
            try:
                s = list(janstats.shogo(member))[:5]
                g = list(janstats.getshogo(member))[:5] if self._recorded else []
                for i, v in enumerate(s):
                    held[i][seat] = int(v) & 0xFF
                for i, v in enumerate(g):
                    got[i][seat] = 1 if v else 0
            except Exception as e:
                self.log.append(("stats-error", "titles seat %d: %s" % (seat, e)))
        return held, got

    def msg_results2(self):
        rank = {r["seat"]: r["place"] for r in self.game.ranking()}
        # Five rows of four. Row 0 is this game's placing; rows 1..4 are each
        # seat's recent history, NO_RANK where we hold none. WARNING: ZERO-BASED --
        # the client prints rank+1, so 0 means first place and is never a blank.
        row0 = [rank[s] for s in range(4)]
        levels, ups = self._levels()
        shogo, getshogo = self._titles()
        rec = M.gameresult_half2(self.next_seq(), [row0] + self._history_rows(),
                                 levels=levels, levelups=ups,
                                 shogo=shogo, getshogo=getshogo)
        self._awaiting = (M.MjGAMERESULTHALF2, None)
        return self._emit(rec, "results 2")

    def msg_gameend(self):
        rec = M.gameend(self.next_seq())
        self._awaiting = (M.MjGAMEEND, None)
        self.state = "over"
        return self._emit(rec, "game over")

    # -- the drive loop ------------------------------------------------------

    # -- sashiuma: the pre-game side-bet handshake -----------------------------
    #
    # Wire form in janmsgs' MjSASHIUMA banner. State is `self.sashiuma`:
    # {"phase": "request"|"agree"|"done", "block": bytearray(6),
    #  "answered": set(), "started": set()}. Every human is asked; a bot's side
    # is decided here, instantly. The deal is HELD until the RESULT goes out and
    # then rides in the same batch, so the client sees phases 1-2-3 and then 4
    # (HAIPAI) -- the order its own dispatcher numbers them.

    def _sashiuma_names(self):
        names = []
        for s in range(4):
            nm = self.nicks[s]
            if not nm and s in self.bots:
                nm = "CPU %d" % s
            names.append(nm or "")
        return names

    def _sashiuma_stake(self):
        if SASHIUMA_UMA > 0:
            return float(SASHIUMA_UMA)
        uma = getattr(self.rules, "uma", None) or (20,)
        return float(abs(uma[0]))

    def msg_sashiuma_start(self, seat):
        self.sashiuma["started"].add(seat)
        rec = M.sashiuma_start(self.next_seq(), self._sashiuma_names())
        self._awaiting = (M.MjSASHIUMASTART, None)
        return self._emit(rec, "sashiuma start", to=seat)

    def _sashiuma_begin(self):
        self.sashiuma = {"phase": "request", "block": bytearray(M.SASHIUMA_BLOCK),
                         "answered": set(), "started": set()}
        self._sashiuma_bots_propose()
        self.trace("SASHIUMA offer -> %s  [stake %g]"
                   % (", ".join(self._seatname(s) for s in sorted(self.live)),
                      self._sashiuma_stake()))
        return [self.msg_sashiuma_start(s) for s in sorted(self.live)]

    def _sashiuma_bots_propose(self):
        """Policy "propose": each bot proposes one bet to a random other seat."""
        if SASHIUMA_BOTS != "propose":
            return
        blk = self.sashiuma["block"]
        for s in sorted(self.bots):
            x = self.rng.choice([o for o in range(4) if o != s])
            blk[M.sashiuma_slot(s, x)] |= M.sashiuma_bit(s)

    def _sashiuma_bots_agree(self):
        """Bots accept every bet proposed to them, unless policy "refuse"."""
        if SASHIUMA_BOTS == "refuse":
            return
        blk = self.sashiuma["block"]
        for b in self.bots:
            for x in range(4):
                if x == b:
                    continue
                i = M.sashiuma_slot(b, x)
                if blk[i] & M.sashiuma_bit(x):
                    blk[i] |= M.sashiuma_bit(b)

    def _sashiuma_merge(self, seat, block):
        """OR the sender's OWN bits from its reply into the table block. Only
        that seat's bit is honoured: a reply cannot set another seat's bit."""
        blk = self.sashiuma["block"]
        mine = M.sashiuma_bit(seat)
        for i in range(M.SASHIUMA_BLOCK):
            if block[i] & mine:
                blk[i] |= mine

    def _sashiuma_all_answered(self):
        return all(s in self.sashiuma["answered"] for s in self.live)

    def _sashiuma_to_select(self):
        st = self.sashiuma
        self._sashiuma_bots_agree()
        st["phase"] = "agree"
        st["answered"] = set()
        seq = self.next_seq()
        self._awaiting = (M.MjSASHIUMASELECT, None)
        return [self._emit(M.sashiuma_select(seq, st["block"]), "sashiuma select",
                           to=s) for s in sorted(self.live)]

    def on_sashiuma_request(self, rec, seat=None):
        st = self.sashiuma
        p = M.parse_sashiuma(rec)
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else None
        if st is None or st["phase"] != "request" or seat is None:
            # A re-sent reply after the phase moved on: re-nudge, never re-run.
            return self._renudge(seat)
        self._sashiuma_merge(seat, p["block"])
        st["answered"].add(seat)
        picks = [x for x in range(4) if x != seat
                 and st["block"][M.sashiuma_slot(seat, x)] & M.sashiuma_bit(seat)]
        self.trace("SASHIUMA %s proposes %s"
                   % (self._seatname(seat),
                      ", ".join(self._seatname(x) for x in picks) or "nothing"))
        if not self._sashiuma_all_answered():
            return []
        return self._sashiuma_to_select()

    def on_sashiuma_agree(self, rec, seat=None):
        st = self.sashiuma
        p = M.parse_sashiuma(rec)
        if seat is None:
            seat = p["seat"] if 0 <= p["seat"] < 4 else None
        if st is None or st["phase"] != "agree" or seat is None:
            return self._renudge(seat)
        self._sashiuma_merge(seat, p["block"])
        st["answered"].add(seat)
        if not self._sashiuma_all_answered():
            return []
        return self._sashiuma_finish()

    def _sashiuma_finish(self):
        """Settle the block to the ON pairs, announce it, and deal."""
        st = self.sashiuma
        blk = st["block"]
        pairs = []
        for i, (a, b) in enumerate(M.SASHIUMA_PAIRS):
            both = M.sashiuma_bit(a) | M.sashiuma_bit(b)
            if blk[i] & both == both:
                blk[i] = both
                pairs.append((a, b))
            else:
                blk[i] = 0
        st["phase"] = "done"
        self.sashiuma_pairs = pairs
        self.trace("SASHIUMA on: %s"
                   % (", ".join("%s-%s" % (self._seatname(a), self._seatname(b))
                                for a, b in pairs) or "no bets"))
        seq = self.next_seq()
        out = [self._emit(M.sashiuma_result(seq, bytes(blk)), "sashiuma result",
                          to=s) for s in sorted(self.live)]
        return out + self.start_kyoku()

    def _sashiuma_seat_gone(self, seat):
        """A human left mid-handshake: the rest must not wait on it for ever.
        `drop_seat` has already removed it from `live`, so re-check completion."""
        st = self.sashiuma
        if not st or st["phase"] == "done" or not self._sashiuma_all_answered():
            return []
        if st["phase"] == "request":
            return self._sashiuma_to_select()
        return self._sashiuma_finish()

    def _sashiuma_apply(self, rank):
        """Fold the side-bets into the final results: the pair's worse-placed
        seat pays the better-placed one the stake (result points)."""
        if not self.sashiuma_pairs:
            return rank
        stake = self._sashiuma_stake()
        by_seat = {r["seat"]: r for r in rank}
        for a, b in self.sashiuma_pairs:
            ra, rb = by_seat.get(a), by_seat.get(b)
            if ra is None or rb is None:
                continue
            win, lose = (ra, rb) if ra["place"] < rb["place"] else (rb, ra)
            win["result"] = round(win["result"] + stake, 1)
            lose["result"] = round(lose["result"] - stake, 1)
            self.trace("SASHIUMA %s beats %s: %+g / %+g"
                       % (self._seatname(win["seat"]), self._seatname(lose["seat"]),
                          stake, -stake))
        return rank

    def start_game(self):
        self.fill_with_bots()
        self.game = mj.Game(self.rules, rng=self.rng)
        self.state = "playing"
        # WARNING: RESET THE RECORD GUARD HERE, not in `msg_results`. `Manager.by_member`
        # keeps a Table for the life of the process, so the SECOND hanchan at
        # this table runs on the same object -- and a `_recorded` left True would
        # silently drop every game after the first. The reset has to sit on the
        # thing that starts a game, or it never runs for the case it exists for.
        self._recorded = False
        self.sashiuma = None
        self.sashiuma_pairs = []
        if SASHIUMA and self.live:
            return self._sashiuma_begin()
        return self.start_kyoku()

    def reset_for_new_game(self):
        """Clear per-hand state so the NEXT MjREADY deals a fresh game.

        WARNING: WHY (2026-09-02, live): `Manager.by_member` keeps one Table per member
        for the whole process, and the MjREADY handler only calls `start_game()`
        when `state != "playing"`. A hand that ends cleanly leaves state "over",
        but a hand that ERRORS OUT mid-play (e.g. a `None` kyoku after an
        exception) leaves it stuck at "playing" for ever -- so every subsequent
        Start Game drives the notices, the client sends MjREADY, and the handler
        returns nothing ("the table is waiting on another seat"), no deal. Called
        from janhourou's MjGAMESTART drive -- the master pressing Start Game is
        the explicit "new game" signal. Keeps the seats/table identity; the
        seat_player + fill_with_bots that follow re-establish `live`.
        """
        self.state = "idle"
        self.game = None
        self.kyoku = None
        self._awaiting = None
        self._awaiting_since = None
        self.pending_naki = None
        self.naki_answers = {}
        self._pending_wins = []
        self._pending_ron_from = None
        self.dropped = set()
        self._recorded = False
        self.live = set()          # re-added by the drive's seat_player(live=True)
        self.outbox = {}           # nothing from the last hanchan may survive
        self._outgoing = []
        self.sashiuma = None
        self.sashiuma_pairs = []
        self.timeouts = {}
        self._last_in = {}
        self._last_for = {}
        self._awaiting_tag = None
        self._awaiting_recs = []
        self._resent_for = None
        self._byes = set()
        self.pending_chankan = None
        self._hold_s = 0.0
        self._last_sent = None
        # A new game starts with an empty gallery: the last one's spectators
        # were dropped at MjGAMEEND (the client left the table screen then).
        self.gallery = {}
        self.gallery_acked = set()
        self.gallery_seq = {}
        self._gallery_copied = None
        self._gallery_released = False
        self._gallery_evicted = []

    #: THE TILE-NUMBERING PROBE. `POL_JAN_DEAL="123456789m1234p"` forces seat
    #: 0's opening hand to exactly those tiles, so ONE screenshot settles what
    #: no amount of looking at a random hand can: whether `janmsgs.WIRE_ID` maps
    #: rank correctly inside a suit.
    #:
    #: The 2026-08-17 live run confirmed the SUIT order (the hand drew
    #: man -> pin -> sou, which a permuted mapping could not produce) but 2m vs
    #: 3m is not readable off a screenshot of a random hand. A known ascending
    #: run is readable at a glance by anyone, mahjong player or not: if the
    #: screen shows 1-9 of characters followed by 1-4 of circles, the mapping is
    #: right; any other order names its own error.
    #:
    #: WARNING: Deliberately crude: it OVERWRITES the dealt hand without removing those
    #: tiles from the wall, so duplicates are possible and the hand is not a
    #: legal deal. That is fine for reading a screen and wrong for anything else,
    #: which is why it is off unless the variable is set.
    #: WARNING: AN ENV VAR WAS THE WRONG CHOICE HERE and it cost a live run: setting
    #: `$env:POL_JAN_DEAL` on the host does NOT reach the container. `docker
    #: compose` only injects variables the compose file declares, so a bare
    #: shell variable is used for interpolating the YAML and nothing else -- the
    #: probe silently never ran. It also needs a container restart, which ends
    #: the player's session.
    #:
    #: So the file wins, exactly as `config/polpro.json` does for the plaintext
    #: channel: `config/` is already bind-mounted, and this is re-read per deal,
    #: so trying a different hand costs one text edit with the client still
    #: connected. The env var stays as an override for the selftests.
    DEAL_FILE = os.environ.get("POL_JAN_DEAL_FILE", "/config/jan_deal.txt")
    DEBUG_DEAL = os.environ.get("POL_JAN_DEAL", "")

    def _deal_spec(self):
        """The forced hand, from the env var or the live-editable file."""
        if self.DEBUG_DEAL:
            return self.DEBUG_DEAL
        try:
            with open(self.DEAL_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        return line
        except OSError:
            pass
        return ""

    def _apply_debug_deal(self):
        spec = self._deal_spec()
        if not spec:
            return
        try:
            tiles = mj._hand_from(spec)
        except Exception as e:
            self.log.append(("bad-jan-deal", "%s: %s" % (spec, e)))
            return
        seat = min(self.live) if self.live else 0
        h = self.kyoku.hands[seat]
        want = 14 if seat == self.kyoku.dealer else 13
        h.tiles = (tiles + h.tiles)[:want]
        self.log.append(("debug-deal", (seat, mj.hand_str(h.tiles))))

    def start_kyoku(self):
        self.kyoku = self.game.start_kyoku()
        self.pending_naki = None
        self.naki_answers = {}
        self._pending_wins = []
        self._pending_ron_from = None
        self._apply_debug_deal()
        return self.msg_haipai()        # already a list: one record per seat

    def advance(self):
        """Run the table forward until a live seat has to decide something.

        Returns the records to send. This is the whole of the server's game
        logic: bots play instantly, and the loop stops the moment a human's
        input is needed -- which is exactly when a message goes out.
        """
        out = []
        guard = 0
        k = self.kyoku
        while k.result is None and guard < 400:
            guard += 1
            seat = k.turn
            h = k.hands[seat]
            if h.drawn is None and h.tile_total() < 14:
                if k.draw_for(seat) is None:
                    break                       # the wall ran out -> finish_draw
                h = k.hands[seat]
            if seat in self.live:
                out.append(self.msg_tsumo(seat))
                return out
            # --- a bot's turn ---------------------------------------------
            # WARNING: A BOT'S TURN IS VISIBLE OR IT DIDN'T HAPPEN (2026-09-02,
            # live): the old loop played bots in silence and the player
            # watched the game "skip to my turn with the tile count lower by
            # 3-4". The turn now animates; DISCARD_ANIM picks how (see there).
            recs, done = self._bot_turn(seat)
            out += recs
            if done:
                return out
        if k.result is not None:
            return out + self.on_kyoku_end()
        return out

    def _bot_turn(self, seat, drew_shown=False):
        """One bot's move from the tile it holds -- win, kan, riichi, discard --
        and whatever that discard provokes. Returns `(records, done)`: `done`
        means the caller must return, because a live seat is deciding or the
        hand ended (the end records are included).

        `drew_shown`: the draw this turn is already on screen (a kan's
        rinshan rides inside `msg_kan`), so the discard must not pop it
        again. Used by `advance()` for an ordinary turn and by `bot_call` /
        `_do_kan` for the turn that follows a call.
        """
        k = self.kyoku
        h = k.hands[seat]
        bot = self.bots.get(seat) or Bot(seat)
        out = []
        if h.drawn is not None:
            sc = mj.can_tsumo(h, k.context_for(seat, h.drawn, True))
            if sc is not None:
                if not drew_shown:
                    out.append(self.msg_tsumo(seat))    # show the winning draw
                k.win_tsumo(seat)
                return out + self.on_win(seat, sc), True
            want = bot.wants_kan(k)
            if want is not None:
                out += self._do_kan(seat, want[0], want[1])
                if k.result is not None or self.pending_chankan is not None:
                    return out, True
                recs, done = self._bot_turn(seat, drew_shown=True)
                return out + recs, done
        riichi = bot.wants_riichi(k)
        tile = bot.choose_discard(k)
        # The slot the tile leaves, read BEFORE discard() removes it.
        slot = h.tiles.index(tile) if tile in h.tiles else len(h.tiles) - 1
        had_draw = h.drawn is not None and not drew_shown
        if had_draw and DISCARD_ANIM != "subtype3":
            out.append(self.msg_tsumo(seat))            # the draw, motion 2
        k.discard(seat, tile=tile, riichi=riichi)
        if had_draw and DISCARD_ANIM == "subtype3":
            # ONE MjALLDATA subtype 3 (draw+prep): the client pops the tile
            # onto the bot's hand AND clears its last pond slot's settled
            # flag IN-HANDLER (the local discard's order), so the motion-3
            # that follows flies with NO settled pre-draw -- no slingshot.
            # The pond already ends with the discard (k.discard ran). This
            # subtype-3 pop IS the draw beat; a separate motion-2 draw would
            # double it. A bot that CALLED (pon/chi/kan) has no draw beat
            # here -- `_show_discard` below covers that turn.
            out.append(self.msg_alldata(discard_event=(tile, seat)))
            out.append(self.msg_tsumo(seat, discard=(tile, slot, seat, riichi)))
            self.trace("DRAW+DISCARD %s tile=%s  [subtype-3 prep + motion-3]"
                       % (self._seatname(seat), _tile(tile)))
        elif had_draw and DISCARD_ANIM == "appear":
            # No fly: the discard just shows up in the pond (subtype-0
            # populate marks it settled and the renderer draws it).
            out.append(self.msg_alldata())
            self.trace("DISCARD %s tile=%s  [appear: populate, no fly]"
                       % (self._seatname(seat), _tile(tile)))
        elif had_draw:  # "fly" -- accurate landing, but the discard flashes settled
            out.append(self.msg_alldata())
            out.append(self.msg_tsumo(seat, discard=(tile, slot, seat, riichi)))
            self.trace("DISCARD %s tile=%s  [fly: subtype-0 populate + motion-3]"
                       % (self._seatname(seat), _tile(tile)))
        else:
            out += self._show_discard(seat, tile, slot, riichi,
                                      after_draw=drew_shown)
        recs, done = self._after_discard(tile, seat)
        return out + recs, done

    def _after_discard(self, tile, seat):
        """What a discard provokes: a call offer to the live seats, a bot's
        claim, or the next draw. Returns `(records, done)` like `_bot_turn`;
        `done` False means the caller may keep running `advance()`."""
        k = self.kyoku
        calls = self.offer_calls(tile, seat)
        if calls:
            return [self.msg_naki(tile, calls)], True
        res = self.resolve_bot_calls(tile, seat)
        if res is not None:
            return res, True
        k.advance()
        if k.result is not None:
            return self.on_kyoku_end(), True
        return [], False

    def _continue(self, tile, seat):
        """`_after_discard` for a HUMAN's discard: the records, run forward."""
        recs, done = self._after_discard(tile, seat)
        return recs if done else recs + self.advance()

    def offer_calls(self, tile, from_seat):
        """Which LIVE seats have a legal call on `tile`. Bots resolve separately."""
        out = {}
        k = self.kyoku
        for seat in sorted(self.live):
            if seat == from_seat:
                continue
            opts = self.legal_calls(seat, tile, from_seat)
            if opts:
                out[seat] = opts
        return out

    def legal_calls(self, seat, tile, from_seat):
        k = self.kyoku
        h = k.hands[seat]
        opts = {}
        ctx = k.context_for(seat, tile, False, from_seat=from_seat)
        if mj.can_ron(h, tile, ctx):
            opts["ron"] = True
        if h.riichi or getattr(k, "_fourth_kan_discard", False):
            # A riichi hand may only ron -- and so may EVERYONE on the discard
            # after a shared fourth kan: suukaikan unless it is ronned, and
            # the engine aborts the hand the moment that discard passes.
            return opts
        if mj.pon_option(h, tile):
            opts["pon"] = True
        if (mj.minkan_option(h, tile) and k.kan_count() < 4
                and k.wall.remaining >= 1):
            opts["kan"] = True
        if seat == (from_seat + 1) % 4 and mj.chi_options(h, tile):
            opts["chi"] = True
        return opts

    def resolve_bot_calls(self, tile, from_seat):
        """The bots' claims on a discard no LIVE seat was offered: ron first,
        then kan/pon, then chi -- each in turn order.

        WARNING: THE ORDER IS TURN ORDER FROM THE DISCARDER, not seat 0..3. It decides
        two real things: who takes the riichi sticks when several seats ron the
        same tile, and -- with MJS_CONFIG_IDX_DOUBLE_RON off -- WHICH of them
        wins at all (head bump / atamahane). Iterating range(4) made seat 0 the
        favourite for no reason.
        """
        k = self.kyoku
        winners = []
        for step in range(1, 4):
            seat = (from_seat + step) % 4
            if seat in self.live:
                continue
            if "ron" in self.legal_calls(seat, tile, from_seat):
                winners.append(seat)
        if winners:
            wins = k.win_ron(winners, tile=tile, from_seat=from_seat)
            if wins:
                return self.on_wins(wins, ron_from=from_seat)
            if k.result is not None:
                return self.on_kyoku_end()      # sanchahou: three rons abort
        # --- no ron: kan, then pon, then chi, still in turn order -------------
        # A pon/kan may be claimed by ANY seat and beats a chi, which only the
        # discarder's left neighbour can make -- so the whole table is asked
        # about tile claims before anyone is asked about chi.
        for want in ("kan", "pon", "chi"):
            for step in range(1, 4):
                seat = (from_seat + step) % 4
                if seat in self.live:
                    continue
                opts = self.legal_calls(seat, tile, from_seat)
                if want not in opts:
                    continue
                bot = self.bots.get(seat) or Bot(seat)
                if bot.calls(k, tile, opts) == want:
                    return self.bot_call(seat, want, tile, from_seat)
        return None

    #: How deep a chain of bot-calls-then-discards may go before we stop
    #: honouring calls for this discard. Three bots can in principle keep
    #: claiming each other's discards; the hand still has to end.
    BOT_CALL_MAX_DEPTH = 6

    def bot_call(self, seat, action, tile, from_seat):
        """A bot takes a pon, chi or daiminkan: form the meld, ANIMATE it,
        then play the turn that follows.

        This is the same `msg_call()` / `msg_kan()` the human path uses, so a
        CPU's pon now fires the banner and flies the tiles exactly as yours
        does -- which until 2026-09-04 was impossible, because bots never
        called.
        """
        k = self.kyoku
        if self._bot_call_depth >= self.BOT_CALL_MAX_DEPTH:
            self.log.append(("bot-call-depth", (seat, action)))
            return None
        h = k.hands[seat]
        bot = self.bots.get(seat) or Bot(seat)
        pre = self.call_snapshot()
        if action == "chi":
            chosen = bot._take_for(h, tile, "chi")
            if not chosen:
                return None
            k.call_chi(seat, chosen)
        elif action == "pon":
            k.call_pon(seat)
        elif action == "kan":
            k.call_pon(seat, kan=True)          # the rinshan draw happens inside
        else:
            return None
        self._bot_call_depth += 1
        try:
            if action == "kan":
                out = self.msg_kan("minkan", seat, pre, discarder=from_seat)
                recs, done = self._bot_turn(seat, drew_shown=True)
            else:
                out = [self.msg_call(action, seat, pre, from_seat)]
                # It is now this seat's turn WITHOUT a draw, so it discards.
                recs, done = self._bot_turn(seat)
            out += recs
            if done:
                return out
            return out + self.advance()
        finally:
            self._bot_call_depth -= 1

    def _show_discard(self, seat, tile, slot, riichi=False, after_draw=False):
        """The records that make a NON-LOCAL seat's discard visible. `riichi`
        marks the motion-3 record (+0x21) as the riichi declaration.

        Deliberately a separate helper from the block in `_bot_turn` rather
        than a refactor of it: that block is confirmed live and also folds in the
        draw beat, which a seat that just CALLED does not have. Honours
        DISCARD_ANIM so both paths move together.

        The subtype-3 form is safe straight after a pon/chi, and measured: the
        client's draw-pop arm is gated on `DAT_003e5a80` -- the last call
        motion -- not being 4..8, so after a pon or chi it skips the pop by
        itself and only the discard flies. SE anticipated exactly this beat.
        `after_draw` names the turn after a KAN on the resync path (the
        rinshan draw was a motion-2 MjTSUMO). The subtype-3 form is used
        there too: its in-handler pop re-shows the draw beat -- cosmetic --
        while the subtype-0 form marks the pond tile settled and produces the
        measured slingshot, which is worse.
        """
        if DISCARD_ANIM == "appear":
            return [self.msg_alldata()]
        if DISCARD_ANIM == "subtype3":
            return [self.msg_alldata(discard_event=(tile, seat)),
                    self.msg_tsumo(seat, discard=(tile, slot, seat, riichi))]
        return [self.msg_alldata(),
                self.msg_tsumo(seat, discard=(tile, slot, seat, riichi))]

    # -- the kan family --------------------------------------------------------

    def _do_kan(self, seat, kind_, target):
        """An ankan or kakan by `seat` (human or bot) on kind `target`, with
        the CHANKAN window: the engine forms the kan provisionally, the seats
        that may rob it are asked (a live seat through an MjNAKI carrying
        only the Ron bit and the added tile at +0x28; a bot answers now), and
        the kan completes -- rinshan draw, kan-dora owed -- only when nobody
        does. `msg_kan` serves both record paths; the deferred form holds the
        rinshan flight while a live seat decides and `msg_tsumo(motion=9)`
        (motion 2 on the resync path) flies it afterwards. Returns records.
        """
        k = self.kyoku
        pre = self.call_snapshot()
        if kind_ == "ankan":
            t = k.call_ankan(seat, target, provisional=True)
        else:
            t = k.call_kakan(seat, target, provisional=True)
        cands = k.chankan_candidates(seat, t)
        live_c = [s for s in cands if s in self.live]
        self.trace("KAN   %s declares %s on %s%s"
                   % (self._seatname(seat), kind_, _tile(t),
                      "  [chankan window: %s]"
                      % "/".join(self._seatname(s) for s in cands)
                      if cands else ""))
        if live_c:
            out = self.msg_kan(kind_, seat, pre, rinshan="defer")
            self.pending_chankan = (seat, kind_, pre)
            out.append(self.msg_naki(t, {s: {"ron": True} for s in live_c},
                                     from_seat=seat))
            return out
        if cands:                                   # bots rob when they can
            wins = k.win_ron(list(cands), chankan=True, tile=t, from_seat=seat)
            if wins:
                return self.on_wins(wins, ron_from=seat)
            if k.result is not None:
                return self.on_kyoku_end()          # sanchahou
        k.complete_kakan()
        return self.msg_kan(kind_, seat, pre)

    def _resolve_chankan(self, tile, options, answers):
        """The chankan window closed: rob the kan or complete it."""
        k = self.kyoku
        kseat, kind_, _pre = self.pending_chankan
        self.pending_chankan = None
        rons = []
        for step in range(1, 4):
            s = (kseat + step) % 4
            if s in self.live:
                act = answers.get(s, ("pass", None))[0]
                if s in options and act == "ron":
                    rons.append(s)
                elif s in options:
                    k.note_missed_ron(s)        # declined a completing tile
            elif s in k.chankan_candidates(kseat, tile):
                rons.append(s)
        if rons:
            wins = k.win_ron(rons, chankan=True, tile=tile, from_seat=kseat)
            if wins:
                self.trace("CHANKAN %s robs %s's %s"
                           % (self._seatname(wins[0][0]), self._seatname(kseat),
                              kind_))
                return self.on_wins(wins, ron_from=kseat)
            if k.result is not None:
                return self.on_kyoku_end()
            for s in rons:
                self.log.append(("refused-chankan", (s, tile)))
        k.complete_kakan()
        out = [self.msg_tsumo(kseat, motion=9 if KAN_MOTION else 2)]
        if kseat in self.live:
            return out
        recs, done = self._bot_turn(kseat, drew_shown=True)
        out += recs
        return out if done else out + self.advance()

    def msg_win_reveal(self, seat, ron_from):
        """A RON's reveal: the win screen synthesises motion 10 on the winner
        (mahdisp.c:649, `+0x22 = 1 << winner`) which flies DAT_00451920[winner]
        -- the table-state hand, 13 tiles, the winning tile still in the
        discarder's pond. ONE plain MjALLDATA first, with the winning tile
        appended to the winner's hand and the claimed pond tile flagged (bit
        6 = not drawn, the pond closes over it), makes the reveal 14 tiles.
        Subtype 0 mutates nothing in-handler, so nothing double-applies; the
        YAKUDISP's own motion follows it. A tsumo needs none of this -- the
        winning draw is already in the hand the client holds.
        """
        k = self.kyoku
        tile = k.last_discard
        hands = [list(k.hands[s].tiles) for s in range(4)]
        if tile is not None and len(hands[seat]) % 3 == 1:
            hands[seat] = hands[seat] + [tile]
        ponds = [self._pond_wire(s) for s in range(4)]
        if ron_from is not None and 0 <= ron_from < 4 and ponds[ron_from]:
            last, _flag = ponds[ron_from][-1]
            if tile is None or mj.kind(last) == mj.kind(tile):
                ponds[ron_from][-1] = (last, True)
        return self.msg_alldata(hands=hands, ponds=ponds, reveal={seat},
                                label="win reveal seat %d" % seat)

    def on_wins(self, wins, ron_from=None):
        """EVERY winner on one discard, in the order `win_ron` settled them.

        With MJS_CONFIG_IDX_DOUBLE_RON on (the default) two seats can ron the
        same tile and `Kyoku._settle` pays both -- but MjYAKUDISP names exactly
        one winner (+0x1a), so a second winner needs a second screen. The first
        goes out now; the rest are queued and each MjYAKUDISPACK pulls the next
        (see `_ack_advance`). Head bump (double_ron off) trims the list to one
        inside `win_ron`, so this is a no-op there.
        """
        wins = list(wins)
        self._pending_wins = [(s, sc) for s, sc in wins[1:]]
        self._pending_ron_from = ron_from
        if len(wins) > 1:
            self.trace("DOUBLE RON off %s: %s  [%d win screens, in turn order]"
                       % (self._seatname(ron_from),
                          " + ".join(self._seatname(s) for s, _sc in wins),
                          len(wins)))
        return self.on_win(wins[0][0], wins[0][1], ron_from=ron_from)

    def on_win(self, seat, score, ron_from=None):
        self.log.append(("win", (seat, repr(score))))
        self.trace("WIN   %s  han=%s fu=%s  %s  scores=%s"
                   % (self._seatname(seat), getattr(score, "han", "?"),
                      getattr(score, "fu", "?"),
                      "ron off %s" % self._seatname(ron_from)
                      if ron_from is not None else "tsumo",
                      list(self.kyoku.scores)))
        self._record_win(seat, score)
        out = []
        if ron_from is not None:
            out.append(self.msg_win_reveal(seat, ron_from))
        out.append(self.msg_yakudisp(seat, score, ron_from=ron_from))
        return out

    def _record_win(self, seat, score):
        """A LIVE seat's win -> its per-yaku counters (finding 28)."""
        if seat not in self.live or janstats is None or not STATS_ENABLE:
            return
        member = self.member_of_seat(seat)
        if not member:
            return
        try:
            yakuman = getattr(score, "limit", "") in ("yakuman", "kazoe_yakuman")
            names = [n for n, _h in (getattr(score, "yaku", None) or [])]
            janstats.record_win(member, names, yakuman=yakuman)
        except Exception as e:
            self.log.append(("stats-error", "win seat %d: %s: %s"
                             % (seat, type(e).__name__, e)))

    def on_kyoku_end(self):
        """A hand finished with no winner (exhaustive draw or an abort): the
        ryukyoku banner + tenpai reveal (motion 10), then the settlement."""
        self.trace("DRAW  exhaustive/abort  scores=%s" % list(self.kyoku.scores))
        return [self.msg_draw_reveal(), self.msg_seisan()]

    def next_kyoku_or_end(self):
        over = self.game.end_kyoku()
        if over:
            return [self.msg_results()]
        return self.start_kyoku()

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
        if self.state == "playing" and (SHUTDOWN or self._escape_requested()):
            reason = ("shutdown -- ending the live game cleanly" if SHUTDOWN
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
            self.log.append(("seat-mismatch", (M_NAME(op), src, seat)))
            self.trace("REFUSED %s stamped seat %d from the member holding seat "
                       "%d -- re-nudging" % (M_NAME(op), src, seat))
            return self._renudge(seat)
        self.last_line_at = self.clock()
        if seat is not None:
            self.timeouts.pop(seat, None)       # it spoke: not a vanished client
        if op in self.DEDUP_OPS and seat is not None:
            word = struct.unpack_from("<I", rec, 0x18)[0] if len(rec) >= 0x1C else 0
            key = (op, h["f13"], word)
            if self._last_in.get(seat) == key:
                self.log.append(("replay", (seat, M_NAME(op), h["f13"])))
                if op in self.SILENT_ON_REPLAY:
                    return []           # the ladder acks: silence is proven live
                return self._renudge(seat)
            self._last_in[seat] = key
        try:
            return self._dispatch(rec, h, op, seat)
        except ValueError as e:
            # The engine refused the move and touched nothing.
            self.log.append(("refused-%s" % M_NAME(op)[2:].lower(),
                             (seat, str(e))))
            self.trace("REFUSED %s from %s: %s" % (M_NAME(op), self._seatname(seat), e))
            return self._reoffer(seat)
        except Exception as e:
            self.log.append(("error", "%s from seat %s: %s: %s"
                             % (M_NAME(op), seat, type(e).__name__, e)))
            self.trace("ERROR %s from %s: %s: %s -- resyncing the sender"
                       % (M_NAME(op), self._seatname(seat), type(e).__name__, e))
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
            self.log.append(("orphan-ingame", M_NAME(op)))
            self.trace("GAMEEND (no active game -- the server was restarted; "
                       "ending the client's ghost session cleanly)")
            return [self.msg_gameend()]
        if self.state == "finished" and op != M.MjREADY:
            # A finished table (every live seat gone) answers nothing but a
            # ghost end: the Manager is about to forget it.
            if op in (M.MjBYE, M.MjMEMBERLEAVEANSER):
                return []
            self.log.append(("finished-ingame", M_NAME(op)))
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
                          _tile(h.tiles[idx]) if target is not None else "?",
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
                       % (self._seatname(seat), _tile(tile)))
            return [self.msg_tsumo(seat)]
        if h.riichi and h.drawn is not None and mj.kind(tile) != mj.kind(h.drawn):
            self.log.append(("refused-sute", (seat, "riichi hand must discard the draw")))
            return [self.msg_tsumo(seat)]
        if act == M.SUTE_RIICHI:
            err = k.riichi_error(seat, tile)
            if err is not None:
                self.log.append(("refused-riichi", (seat, err)))
                self.trace("REFUSED riichi by %s on %s: %s"
                           % (self._seatname(seat), _tile(tile), err))
                return [self.msg_tsumo(seat)]
        self.trace("DISCARD %s tile=%s slot=%d%s  [client-side render -- lands "
                   "in the player's own row]"
                   % (self._seatname(seat), _tile(tile), idx,
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
                       % (self._seatname(seat), action, _tile(tile)))

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
            bot = self.bots.get(s) or Bot(s)
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
                           % (self._seatname(s), _tile(tile), from_seat,
                              _tile(k.last_discard)))
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


# --- the manager -------------------------------------------------------------

class Manager(object):
    """Every table this server is running, keyed by table id.

    `janhourou.py` holds one of these. A peer's records are routed to the table
    its member id is seated at; an unseated peer whose MjREADY starts a game
    is given one, which is what makes a single console playable today without
    a lobby round trip. Any OTHER in-game line from a member with no table is
    a ghost session (the server restarted under a live game) and is answered
    with MjGAMEEND without minting state (finding 32).

    THREADING (finding 9). ONE `RLock` guards `handle`, `tick`,
    `pending_for_member`, the registry and every outbox: two connection
    threads used to mutate one Table at once, and `begin_routing()` from
    thread B reset `_outgoing` under thread A, so `_route` bypassed and sent
    B's private MjTSUMO down A's socket. Everything a Table does now happens
    under the lock; the only thing done outside it is the deal delay
    (`take_hold`), so a 0.7 s deal does not stall every other table.

    THE DEADLINE SWEEPER (finding 8). `ensure_sweeper()` starts one daemon
    thread per Manager (lazily, from the first `handle`) that calls `tick()`
    every SWEEP_S under the lock. What a sweep produces is QUEUED into the
    live seats' outboxes, exactly as 8deee0f8 made the line-driven sweep do,
    and reaches the clients by the routes that already drain them: the next
    game-band line, the `<DR>` poll, and responders' idle push. `on_swept`
    (a callable taking member ids) lets the transport push at once.
    """

    def __init__(self, sweeper=None):
        self.tables = {}
        self.by_member = {}
        self.by_lobby = {}          # lobby key -> the Table playing on it
        self._lock = threading.RLock()
        self._sweeper = None
        self._sweeper_enabled = SWEEPER if sweeper is None else bool(sweeper)
        self.on_swept = None        # callable(member_ids) once a sweep queued records
        self._next_id = 1
        self.watchable = None       # callable(lobby key) -> bool: janhourou's rule
        self._watch_last = (None, 0.0)   # (the body last written, when)
        self._watch_err = 0.0
        self._watch_final = {}  # lobby key -> (when, entry): a game's LAST frame

    def _new_id(self):
        tid, self._next_id = self._next_id, self._next_id + 1
        return tid

    def _adopt(self, t):
        t._managed = True
        t.clock = self.clock
        t.last_line_at = self.clock()
        return t

    def table_for(self, member_id, nick=None, create=True):
        with self._lock:
            t = self.by_member.get(member_id)
            if t is not None:
                return t
            if not create:
                return None
            tid = self._new_id()
            t = self._adopt(Table(tid, channel=b"#MJS0R%03d" % tid))
            t.seat_player(member_id, nick, member=member_id)
            t.fill_with_bots()
            self.tables[tid] = t
            self.by_member[member_id] = t
            return t

    @staticmethod
    def lobby_key(lobby_id, room=None):
        """The Manager's key for a lobby table: the table id alone, or the
        ROOM-QUALIFIED id when the caller knows the room (finding 30), so
        Room 1-1 table 1 and Room 2-3 table 1 are different tables. The
        arithmetic is `janseats.room_table_id` (the seat store, the rule
        store and the lobby's per-room PTL all key by the same composite;
        janhourou passes it in as `lobby_id` with `room=None`)."""
        lobby_id = int(lobby_id or 0)
        if not room:
            return lobby_id
        if janseats is not None and hasattr(janseats, "room_table_id"):
            return janseats.room_table_id(room, lobby_id)
        return (int(room) << 16) | (lobby_id & 0xFFFF)

    @staticmethod
    def rules_for(key):
        """The rules the master stored for a lobby table (`janrules`), or
        None for the engine defaults."""
        if janrules is None:
            return None
        try:
            if hasattr(janrules, "enabled") and not janrules.enabled():
                return None
            return janrules.rules_for(key)
        except Exception:
            return None

    def table_for_lobby(self, lobby_id, members, nick=None, reset=True,
                        room=None):
        """The in-game table for one LOBBY table, seating everyone really there.

        WARNING: THIS IS WHAT `table_for` COULD NOT DO. That keys by member, so two
        humans at one table got two Tables and each played three bots -- proven
        live 2026-09-04, when a Start Game at a table this server knew held two
        people announced `0='PS2Tester' 1='COM 1' 2='COM 2' 3='COM 3'`.

        `members` is `janseats.seats_at()`: [(member, seat, name, polid)].
        Seats come from the STORE, not from arrival order, so the seat a player
        was told they had at reserve time is the seat they get -- the client
        stamps that number on every message it sends and a mismatch would
        misroute the whole hand.

        `rules=janrules.rules_for(lobby)` (finding 2): the hanchan plays what
        the dialog showed, re-read at every new game since the master may have
        changed them. `room` qualifies the table id (finding 30).
        """
        with self._lock:
            key = self.lobby_key(lobby_id, room)
            t = self.by_lobby.get(key)
            if t is None:
                tid = self._new_id()
                # THE CHANNEL IS THE PTL ROW'S NAME. `lobby_id` may already
                # be a composite (janhourou passes the store's key); the
                # channel a client JOINs at game start (`sqMgCpEnterTable`)
                # is the row name it copied at reserve time, which
                # `janseats.table_channel` authors: room-qualified
                # `#MJS0T<room:3><n:3>` since 2026-09-04, bare `#MJS0T00n`
                # for room 0. Informational here (nothing stamps it on a
                # record); it must only agree with what the lobby served.
                base = key & 0xFFFF
                if janseats is not None and hasattr(janseats, "split_table_id"):
                    base = janseats.split_table_id(key)[1]
                if janseats is not None and hasattr(janseats, "table_channel"):
                    chan = janseats.table_channel(key).encode("ascii")
                else:
                    chan = b"#MJS0T%03d" % (base or tid)
                t = self._adopt(Table(tid, channel=chan,
                                      rules=self.rules_for(key)))
                t.lobby_key = key
                self.tables[tid] = t
                self.by_lobby[key] = t
            # WARNING: A RUNNING HAND IS NEVER RE-SEATED. `reset=False` is for callers
            # that only want to LOOK at the table -- the member-list panel asks
            # on every open, and re-seating mid-hanchan would clear the wall,
            # the hands and every outbox out from under two players who are
            # mid-game.
            if t.state == "playing" and not reset:
                return t
            # Start Game is the explicit "new game" signal; clear the last one
            # (including anything still queued for a seat) before re-seating.
            if reset:
                t.reset_for_new_game()
                t.rules = self.rules_for(key) or t.rules
            t.seats = [None] * 4
            t.nicks = [None] * 4
            t.members = {}
            t.bots = {}
            for member, seat, name, polid in members:
                if not 0 <= seat < 4:
                    continue
                # The WIRE id must be the client's own PolID: a seat whose id
                # the client does not recognise as itself is a seat it will
                # not play.
                t.seat_player(polid or member, name or nick, seat=seat,
                              live=True, member=member)
                # A player is never also a spectator (one table screen).
                self.forget_spectator(member, "now seated", store=False)
                self.by_member[member] = t
            t.fill_with_bots()
            return t

    # -- the gallery (spectators; see the GALLERY banner) ----------------------

    def add_spectator(self, t, member, slot):
        """Route `member`'s lines to table `t` as a SPECTATOR and hand it the
        board. Any other gallery membership goes first."""
        member = int(member)
        with self._lock:
            old = self.by_member.get(member)
            if old is not None and old is not t and member in old.gallery:
                old.remove_spectator(member, "moved to table %s" % t.id)
            t.add_spectator(member, slot)
            self.by_member[member] = t
            return t.gallery.get(member)

    def forget_spectator(self, member, why="left", store=True):
        """Drop a spectator from its table (and, with `store`, from the seat
        store's gallery). A seated member is untouched. Returns the Table it
        was watching, or None."""
        try:
            member = int(member)
        except (TypeError, ValueError):
            return None
        with self._lock:
            t = self.by_member.get(member)
            if t is None or member not in t.gallery:
                return None
            t.remove_spectator(member, why)
            if t.seat_of_member(member) is None:
                del self.by_member[member]
        if store:
            self._store_gallery_drop(member)
        return t

    def spectator_table(self, member):
        """The Table `member` is WATCHING, or None."""
        with self._lock:
            try:
                t = self.by_member.get(int(member))
            except (TypeError, ValueError):
                return None
            return t if t is not None and int(member) in t.gallery else None

    @staticmethod
    def _store_gallery_drop(member):
        if janseats is None or not hasattr(janseats, "gallery_drop"):
            return
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return
            janseats.gallery_drop(member)
        except Exception:
            pass

    def _release_galleries(self):
        """A table whose MjGAMEEND went out: tell the seat store its gallery
        is over, ONCE (the client leaves the table screen on MjGAMEEND with
        no MjGALLEYLEAVEREQ, spec §1.11; a membership left in the store
        answers the next Watch with 3). The Table keeps its `gallery` until
        each spectator's exit ack or the reap, so queued copies still drain."""
        for t in self.tables.values():
            # Spectators `_gallery_copy` evicted for a full outbox: forget
            # them here and in the store (a Table cannot reach the store).
            while t._gallery_evicted:
                m = t._gallery_evicted.pop()
                if t.seat_of_member(m) is None and self.by_member.get(m) is t:
                    del self.by_member[m]
                self._store_gallery_drop(m)
            if t._gallery_released or t.state not in ("over", "finished"):
                continue
            t._gallery_released = True
            key = getattr(t, "lobby_key", None)
            if not t.gallery or key is None or janseats is None:
                continue
            if not hasattr(janseats, "clear_gallery"):
                continue
            try:
                if hasattr(janseats, "enabled") and not janseats.enabled():
                    continue
                gone = janseats.clear_gallery(key)
                if gone:
                    _trace("GALLERY of table %s released at game end: %s"
                           % (t.id, ", ".join(str(m) for m in gone)))
            except Exception:
                pass

    # -- the sweeper thread ----------------------------------------------------

    def ensure_sweeper(self):
        """Start the deadline thread once. Idempotent; a no-op when disabled."""
        if not self._sweeper_enabled or self._sweeper is not None:
            return
        with self._lock:
            if self._sweeper is not None:
                return
            th = threading.Thread(target=self._sweep_loop, name="jan-sweeper",
                                  daemon=True)
            self._sweeper = th
            th.start()
            _trace("SWEEPER started (every %.1fs, drop after %d timeouts)"
                   % (SWEEP_S, TIMEOUTS_TO_DROP))

    def _sweep_loop(self):
        while True:
            time.sleep(SWEEP_S)
            try:
                swept = self.tick()
            except Exception as e:                  # never let the thread die
                _trace("SWEEPER raised %s: %s" % (type(e).__name__, e))
                continue
            if not swept or self.on_swept is None:
                continue
            with self._lock:
                members = set()
                for t, _recs in swept:
                    for m, s in t.members.items():
                        if s in t.live and t.outbox.get(s):
                            members.add(m)
                    for m in t.gallery:
                        if t.outbox.get(t.gallery_key(m)):
                            members.add(m)
            try:
                self.on_swept(sorted(members))
            except Exception as e:
                _trace("SWEEPER push hook raised %s: %s" % (type(e).__name__, e))

    # -- inbound ---------------------------------------------------------------

    def handle(self, rec, member_id=0, nick=None):
        if not GAME_ENABLE:
            return []
        h = janwire.unpack(rec)
        op = h["opcode"]
        if op not in INGAME_OPCODES:
            return []
        self.ensure_sweeper()
        hold = 0.0
        with self._lock:
            t = self.table_for(member_id, nick, create=(op == M.MjREADY))
            if t is None:
                # No table and no MjREADY to start one: a client mid-game in
                # a hand this process does not hold. Answer the ghost end
                # WITHOUT minting a Table for it (finding 32); a stray MjBYE
                # is that client leaving and needs nothing.
                if op == M.MjBYE:
                    return []
                # A SPECTATOR's line (GALLERY banner): the exit acks after
                # its LEAVEREQ arrive with no table, and a ghost MjGAMEEND
                # is what would land in its room screen's FIFO. Silence --
                # for an MjGALLEYACK always, and for anything else from a
                # member the seat store says is watching.
                if op == M.MjGALLEYACK or self._store_says_spectator(member_id):
                    if op == M.MjGALLEYACK and h["f16"] == 0:
                        self._store_gallery_drop(member_id)   # its exit ack
                    return []
                _trace("GAMEEND (member %s sent %s with no table -- ghost "
                       "session, no state minted)" % (member_id, M_NAME(op)))
                return [M.gameend(1)]
            if member_id and int(member_id) in t.gallery:
                # A SPECTATOR: bookkeeping, no mutation, no reply. Its exit
                # ack after MjGAMEEND drops it here and in the store.
                t.begin_routing()
                t.on_gallery(rec, member_id)
                t.take_routes()
                if int(member_id) not in t.gallery:
                    if t.seat_of_member(member_id) is None:
                        self.by_member.pop(int(member_id), None)
                    self._store_gallery_drop(member_id)
                self._release_galleries()
                self._reap()
                return []
            try:
                seat = t.seat_of_member(member_id) if member_id else None
                if seat is None:
                    seat = h["src"] if 0 <= h["src"] < 4 else None
                t.begin_routing()
                out = []
                if self._rejoin_pending(member_id):
                    out += t.rejoin(seat)
                # Sweep THIS table's deadline first: if another seat at it has
                # gone quiet, this line is our chance to play for it (the
                # thread does the same for every table, every second).
                #
                # WARNING: BUT NEVER SWEEP THE SEAT THIS LINE IS FROM (2026-09-03,
                # live): that seat is not silent, it just answered -- slowly. A
                # human reading an unfamiliar board routinely takes longer than
                # the deadline, and sweeping first auto-played the tile they
                # were still choosing, THEN their real discard landed on top --
                # two advance() passes, so duplicated bot turns and a SECOND
                # call offer (seen live: "the tile blinks and I can only hit
                # confirm"). The sweep still catches a DIFFERENT seat that has
                # genuinely gone quiet.
                if not (t._awaiting and t._awaiting[1] == seat):
                    out += t.tick(self.clock())
                records = out + t.handle(rec, member=member_id)
                result = self._route(t, member_id, records, t.take_routes())
                hold = t.take_hold()
            except Exception as e:                  # a bug here must not kill a session
                t.log.append(("error", "%s: %s" % (type(e).__name__, e)))
                raise
            self._release_galleries()
            self._reap()
            self._write_watch()
        if hold > 0:
            time.sleep(hold)                        # the deal delay, unlocked
        return result

    @staticmethod
    def _store_says_spectator(member_id):
        if janseats is None or not member_id or not hasattr(janseats, "gallery_of"):
            return False
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return False
            return bool(janseats.gallery_of(member_id))
        except Exception:
            return False

    def _rejoin_pending(self, member_id):
        if janseats is None or not member_id:
            return False
        try:
            if hasattr(janseats, "enabled") and not janseats.enabled():
                return False
            return bool(janseats.rejoin_pending(member_id, clear=True))
        except Exception:
            return False

    def _route(self, t, member_id, records, routes):
        """Split this call's records: the caller's go back on its socket, every
        other live seat's waits in its outbox.

        Every record is built through `_emit`, so `routes` describes `records`
        exactly. One that is not is a BUG -- logged as `route-bypass` -- and
        with more than one live seat it is DROPPED rather than handed to
        whoever spoke: a private hand delivered to the wrong seat is worse than
        a lost record (finding 9). With one live seat the old behaviour (the
        caller gets it) is safe and kept.
        """
        by_id = {id(r): to for r, to in routes}
        unrouted = [r for r in records if id(r) not in by_id]
        if unrouted:
            t.log.append(("route-bypass", len(unrouted)))
            _trace("ROUTE BUG: %d record(s) built outside _emit on table %s"
                   % (len(unrouted), t.id))
            if len(t.live) > 1:
                records = [r for r in records if id(r) in by_id]
        caller = t.seat_of_member(member_id)
        if caller is None:
            if len(t.live) > 1:
                t.log.append(("route-no-caller", len(records)))
                return [r for r in records if by_id.get(id(r)) is None]
            return records              # a solo table: the one human gets it
        mine = [r for r in records if by_id.get(id(r)) in (None, caller)]
        for s in sorted(t.live):
            if s == caller:
                continue
            for r in records:
                to = by_id.get(id(r))
                if to is None or to == s:
                    t.queue_for(s, r)
        return mine

    def pending_for_member(self, member_id, limit=None):
        """Anything queued for this member while somebody else was talking, or
        while the sweeper played for a silent seat.

        Drained on EVERY line the member sends (game band or `<DR>`), and by
        responders' idle push for a client that is waiting and says nothing.
        """
        with self._lock:
            try:
                t = self.by_member.get(int(member_id))
            except (TypeError, ValueError):
                return []
            if t is None:
                return []
            seat = t.seat_of_member(member_id)
            if seat is None:
                # A SPECTATOR's copies (GALLERY banner) -- also after it was
                # dropped at game end, while its MjGAMEEND copy is queued.
                gk = t.gallery_key(member_id)
                if int(member_id) in t.gallery or gk in t.outbox:
                    return t.take_outbox(gk, limit=limit)
                return []
            return t.take_outbox(seat, limit=limit)

    def tick(self, now=None):
        """Sweep every table's deadline. Returns [(table, [records]), ...].

        Called every SWEEP_S by the sweeper thread (and by any caller that
        wants a sweep now, e.g. a selftest with a fake clock). Everything a
        sweep produces is QUEUED into the live seats' outboxes; the caller
        gets the list back for logging only.
        """
        with self._lock:
            now = self.clock() if now is None else now
            out = []
            for t in list(self.tables.values()):
                t.begin_routing()
                recs = t.tick(now)
                routes = t.take_routes()
                if not recs:
                    continue
                # WARNING: QUEUE THEM OR THEY ARE LOST (2026-09-04, live: a two-human
                # table where both consoles sat stuck). The sweeper is the ONLY
                # path that advances a table nobody is talking on -- and it
                # used to hand its records back to a caller that had, in its
                # own words, "no peer to send it to on this path", and dropped
                # them. The table's state moved on and NOBODY WAS EVER TOLD.
                by_id = {id(r): to for r, to in routes}
                unrouted = [r for r in recs if id(r) not in by_id]
                if unrouted:
                    # Routes do not describe these records, so we cannot say
                    # who each one is FOR -- and a private hand sent to the
                    # wrong seat is worse than a late one. Log it as the bug
                    # it is and queue only what IS described.
                    t.log.append(("tick-route-bypass", len(unrouted)))
                for r in recs:
                    to = by_id.get(id(r), False)
                    if to is False:
                        continue
                    for seat in sorted(t.live):
                        if to is None or to == seat:
                            t.queue_for(seat, r)
                # (the spectators' copies were queued by `_emit` itself)
                out.append((t, recs))
            self._release_galleries()
            self._reap()
            self._write_watch()
            return out

    def watch_tables(self):
        """{lobby key: {"watchable": bool, "state": ...}} for every table in a
        game (see WATCH_FILE). A table is listed even when it may not be
        watched, as watchable False with no state, so the board drops it at
        once when its creator switches watching off."""
        out = {}
        for t in list(self.tables.values()):
            key = getattr(t, "lobby_key", None)
            if not key or t.state not in ("playing", "over"):
                continue
            ok = False
            try:
                ok = bool(self.watchable and self.watchable(key))
            except Exception:
                ok = False
            out[str(key)] = ({"watchable": True, "state": t.public_state()} if ok
                             else {"watchable": False})
        now = time.time()
        for key, (when, entry) in list(self._watch_final.items()):
            if now - when > WATCH_FINAL_S:
                del self._watch_final[key]
            elif key not in out:
                out[key] = entry
        return out

    def _keep_final(self, t):
        """A finished game is forgotten at once (`_reap`); keep its last frame
        in the file for WATCH_FINAL_S so the board can show why it stopped
        (seen live 2026-09-13: the page just sat there after the last human
        left). Only a watchable game that got as far as a hand."""
        key = getattr(t, "lobby_key", None)
        if not key or t.kyoku is None or t.state != "finished":
            return
        try:
            if self.watchable and self.watchable(key):
                self._watch_final[str(key)] = (
                    time.time(), {"watchable": True, "state": t.public_state()})
        except Exception:                           # noqa: BLE001
            pass                                    # never touch a game over it

    def _write_watch(self, force=False):
        """WATCH_FILE, atomically, when the tables changed or WATCH_EVERY_S
        passed. Never raises: a failed write must not touch a game."""
        if not WATCH_FILE or WATCH_FILE == "0":
            return
        try:
            import json as _json
            body = _json.dumps(self.watch_tables(), sort_keys=True, separators=(",", ":"))
            now = time.time()
            if not force and body == self._watch_last[0] and \
                    now - self._watch_last[1] < WATCH_EVERY_S:
                return
            path = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), WATCH_FILE)
            tmp = "%s.tmp.%d" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write('{"stamp":%.3f,"tables":%s}' % (now, body))
            os.replace(tmp, path)
            self._watch_last = (body, now)
        except Exception as e:                      # noqa: BLE001
            if time.time() - self._watch_err > 300:
                self._watch_err = time.time()
                _trace("watch file not written (%s: %s) -- games unaffected"
                       % (type(e).__name__, e))

    @staticmethod
    def _owed_seats(t):
        """Seats whose LAST RECORDS ARE STILL IN THE OUTBOX and who are still
        sitting there waiting for them: a live human who has not said MjBYE.

        A bot has no socket, a seat that has said MjBYE has left the screen, and
        a spectator's key is not a seat -- none of those are owed anything."""
        return [s for s, q in t.outbox.items()
                if q and isinstance(s, int) and s in t.live
                and s not in t._byes and s not in t.bots]

    def _reap(self):
        """Forget finished tables, and idle ones nobody has spoken to for
        TABLE_TTL (finding 32). A playing table always finishes first: the
        sweeper drops its silent seats and `drop_seat` finishes it when the
        last live seat goes.

        WARNING: A FINISHED TABLE IS THE ONLY ROUTE ITS RECORDS HAVE. `pending_for_member`
        resolves through `by_member`, so popping the table here threw away
        whatever was still queued for the OTHER human -- silently, with no log
        and no error. The hand that does this is the ordinary one: the ladder
        advances on the FIRST ack (`BROADCAST_ACKED`), so player A can ack
        MjGAMEEND and send MjBYE while player B's copy of it is still in the
        outbox. B then sits on the results screen for ever, because the record
        that takes them off it no longer exists anywhere. That is "it hangs
        when you leave the table after a game is done", and it needs no network
        fault to happen -- only for one player to be a few seconds behind the
        other.

        So a finished table is HELD while a live human who has not said MjBYE
        still has records queued, for at most POL_JAN_FINISHED_GRACE seconds.
        The cap matters as much as the hold: a player who has walked away must
        not keep a table alive for ever, and the seat store -- not this object --
        is what makes the lobby row joinable again, so holding it costs nothing
        that a player can see.
        """
        for tid, t in list(self.tables.items()):
            idle = t.clock() - t.last_line_at
            if not (t.state == "finished"
                    or (t.state != "playing" and idle > TABLE_TTL)):
                continue
            if t.state == "finished":
                owed = self._owed_seats(t)
                if owed:
                    held = t.clock() - (t.finished_at or t.last_line_at)
                    if held < FINISHED_GRACE:
                        if not t._reap_held_said:
                            t._reap_held_said = True
                            _trace("HOLD table %d (finished) -- seat(s) %s still "
                                   "have %d record(s) queued; the reaper waits up "
                                   "to %gs for them"
                                   % (tid, ",".join(str(x) for x in owed),
                                      sum(len(t.outbox[x]) for x in owed),
                                      FINISHED_GRACE))
                        continue
                    _trace("DROPPING %d queued record(s) for seat(s) %s on table "
                           "%d -- finished %.0fs ago and nobody drained them "
                           "(POL_JAN_FINISHED_GRACE=%g)"
                           % (sum(len(t.outbox[x]) for x in owed),
                              ",".join(str(x) for x in owed), tid, held,
                              FINISHED_GRACE))
            self._keep_final(t)
            self.tables.pop(tid, None)
            for m, x in list(self.by_member.items()):
                if x is t:
                    del self.by_member[m]
            for k, x in list(self.by_lobby.items()):
                if x is t:
                    del self.by_lobby[k]
            for m in list(t.gallery):              # the store forgets them too
                self._store_gallery_drop(m)
            _trace("FORGET table %d (%s, idle %.0fs%s)"
                   % (tid, t.state, idle,
                      ", %d spectator(s)" % len(t.gallery) if t.gallery else ""))

    clock = staticmethod(time.monotonic)


# Every opcode the in-game loop can produce, plus the two the lobby sends on the
# way in. Anything else stays with janhourou.py's own responders.
INGAME_OPCODES = frozenset([
    M.MjREADY, M.MjHAIPAIACK, M.MjSUTE, M.MjNAKIACK, M.MjYAKUDISPACK,
    M.MjSEISANACK, M.MjALLDATA, M.MjALLDATAACK, M.MjBYE,
    M.MjGAMERESULTHALF1ACK, M.MjGAMERESULTHALF2ACK,
    M.MjSASHIUMAREQUEST, M.MjSASHIUMAARGEE, M.MjMEMBERLEAVEANSER,
    # The spectator's one ack (0x30, sub 6) -- routed to `Table.on_gallery`
    # by `Manager.handle`, never to `Table.handle` (GALLERY banner).
    M.MjGALLEYACK,
])


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

    global DEAL_DELAY
    DEAL_DELAY = 0.0          # never sleep on the deal during the selftest

    # Persistence is exercised for real, but NEVER against the deployed resource
    # directory: the member ids below are test ids and would litter it with
    # records for players who do not exist.
    import tempfile
    _old_res = os.environ.get("POL_RESOURCE_DIR")
    os.environ["POL_RESOURCE_DIR"] = tempfile.mkdtemp(prefix="jangame-")

    # The side-bet handshake changes what READY answers (START, not the
    # deal); the legacy tests assume the deal. The sashiuma block below
    # turns it on explicitly for its own tables.
    globals()["SASHIUMA"] = False
    # Never start the sweeper THREAD in here: the tests drive fake clocks and
    # call `Manager.tick` themselves. The thread is covered by its own block.
    globals()["SWEEPER"] = False

    def check(cond, msg):
        if not cond:
            print("FAIL: %s" % msg)
        return bool(cond)

    # --- TWO HUMANS AT ONE TABLE ------------------------------------------
    # Everything below this comment is about the case that was broken until
    # 2026-09-04: a Start Game at a table holding two people built a table for
    # ONE of them and three bots, and the other was left in the lobby.
    _m = Manager()
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

    if _deal and _deal2 and CONCEAL_DEAL:
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

    t = Table(1, seed=20260817)
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
    ok &= check(bytes(haipai[0x23:0x27]) == (b"\x01\0\0\0" if ISHUMAN_ENABLE
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
                         "got %s" % (rec[0x22], M_NAME(pv) if pv else None)):
                    base = M.ALLDATA_POND + rec[0x22] * M.ALLDATA_POND_STRIDE
                    pond = [b for b in prev[base:base + M.ALLDATA_POND_STRIDE]
                            if b]
                    ok &= check(pond and (pond[-1] & 0x3F) == (rec[0x1B] & 0x3F),
                                "that MjALLDATA's pond for seat %d already "
                                "ends with the discard (pond tail %s, tile %s)"
                                % (rec[0x22],
                                   "%#x" % pond[-1] if pond else "empty",
                                   "%#x" % rec[0x1B]))
                    if DISCARD_ANIM == "subtype3":
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
                      % (M_NAME(h["opcode"]), h["length"], h["f13"], h["sub"]))
            if (h["sub"] != M.INGAME_SUB and h["opcode"] != M.MjNOTICEGAMESTART
                    and h["opcode"] != M.MjREADYSTATUS):   # sub 2 by measurement
                ok = check(False, "%s went out on sub %d -- the in-game loop "
                                  "drains on %d and would never see it"
                                  % (M_NAME(h["opcode"]), h["sub"], M.INGAME_SUB))
            if h["length"] != len(rec):
                ok = check(False, "%s declares %d, is %d"
                           % (M_NAME(h["opcode"]), h["length"], len(rec)))
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
    if DISCARD_ANIM != "appear":     # "appear" emits no motion-3 to check
        ok &= check(bot_discards > 20,
                    "the pond-order check saw real bot discards (%d)"
                    % bot_discards)
    for op in (M.MjHAIPAI, M.MjTSUMO, M.MjSEISAN, M.MjGAMERESULTHALF1,
               M.MjGAMERESULTHALF2, M.MjGAMEEND):
        ok &= check(op in seen, "%s was never sent" % M_NAME(op))

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
    t2 = Table(2, seed=42)
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
                % [M_NAME(o) for o in ops])
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
                % [M_NAME(o) for o in ops])
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
                == sorted(Table.taken_slots(pre_hand, t2.kyoku.hands[0].tiles)),
                "and they are exactly the slots the meld consumed: %r vs %r"
                % (sorted((sa, sb)),
                   Table.taken_slots(pre_hand, t2.kyoku.hands[0].tiles)))
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
    t3 = Table(3, seed=42)
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
                % [M_NAME(o) for o in ops])
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
    tr = Table(13, seed=5)
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
    tm = Table(11, seed=7)
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
                % (_tile(hh.tiles[13]), _tile(hh.drawn)))
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
    td = Table(12, seed=3)
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
                % [M_NAME(o) for o in ops])
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
            ok &= check(not os.path.exists(janstats.stats_file(t.seats[s])),
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
        th = Table(9, seed=1)
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
        globals()["STATS_ENABLE"] = False
        try:
            ok &= check(all(v == M.NO_RANK for row in th._history_rows()
                            for v in row),
                        "POL_JAN_STATS=0 gives four NO_RANK rows")
        finally:
            globals()["STATS_ENABLE"] = _old_stats != "0"

    # --- the WIN path, driven directly ---------------------------------------
    # The random hanchan above ends every hand in an exhaustive draw (a
    # tsumogiri client against three bots that only ever ron), so MjYAKUDISP is
    # never reached by luck. Force it: give seat 0 a hand that is one tile from
    # a win and have it declare the self-draw the client's own way.
    tw = Table(3, seed=1)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in out])
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
                        % [M_NAME(janwire.unpack(r)["opcode"]) for r in resend])
            ok &= check(janwire.unpack(resend[0])["f13"]
                        == janwire.unpack(nxt[0])["f13"] if resend else True,
                        "and the re-nudge is the SAME record (client dedups on "
                        "the sequence byte), not a freshly minted one")
            after = tw.handle(_client_ack(nxt[0], 0))
            ok &= check(after and janwire.unpack(after[-1])["opcode"] == M.MjHAIPAI,
                        "MjSEISANACK -> the next deal (last in the reply)")
            rsl = [r for r in after if janwire.unpack(r)["opcode"] == M.MjREADYSTATUS]
            ok &= check(len(rsl) == (1 if ISHUMAN_ENABLE else 0)
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
                        % [M_NAME(janwire.unpack(r)["opcode"]) for r in storm])

    # a hand the server does NOT agree is a win must not be paid out
    tr = Table(4, seed=2)
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
    th = Table(5, seed=3)
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
    te = Table(8, seed=7)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in out])
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
    global SHUTDOWN
    ts = Table(9, seed=17)
    ts.seat_player(0x5D07, seat=0)
    ts.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    ok &= check(ts.state == "playing", "a game is running before shutdown")
    _saved_shutdown = SHUTDOWN
    SHUTDOWN = True
    try:
        out = ts.handle(_client_ack(ts._last_sent, 0) or
                        janwire.pack(opcode=M.MjHAIPAIACK, src=0, dst=4, f16=1,
                                     sub=3, length=0x18)[:0x18])
        ok &= check(out and janwire.unpack(out[0])["opcode"] == M.MjGAMEEND,
                    "SHUTDOWN ends the live game with MjGAMEEND on its next "
                    "line: %r" % [M_NAME(janwire.unpack(r)["opcode"]) for r in out])
    finally:
        SHUTDOWN = _saved_shutdown
    # ...and the SWEEPER ends a live table whose clients are silent (a client
    # waiting for its turn sends nothing, so "its next line" never comes).
    ts2 = Table(10, seed=18)
    ts2.seat_player(0x5D08, seat=0)
    ts2.handle(janwire.pack(opcode=M.MjREADY, src=0, dst=4, length=0x18)[:0x18])
    SHUTDOWN = True
    try:
        swept_end = ts2.tick(ts2.clock())
        ok &= check(swept_end and janwire.unpack(swept_end[0])["opcode"] == M.MjGAMEEND
                    and ts2.state == "over",
                    "SHUTDOWN: the sweeper tick ends a silent live table with "
                    "MjGAMEEND (queued for the idle push): %r"
                    % [M_NAME(janwire.unpack(r)["opcode"]) for r in swept_end])
        ok &= check(ts2.tick(ts2.clock()) == [], "...once")
    finally:
        SHUTDOWN = _saved_shutdown
    # and with SHUTDOWN clear again, a fresh table plays normally
    ok &= check(not SHUTDOWN, "SHUTDOWN restored for the rest of the selftest")

    # --- an in-game message for a game we no longer have (restart recovery) --
    # WARNING: 2026-09-04 LIVE CRASH: a deploy restarted authsess and wiped the
    # Manager; the console kept playing, its next MjSUTE built a fresh idle
    # table and raised 'NoneType' has no attribute 'hands' -- swallowed, so the
    # server went silent and the console froze. A stray in-game message on a
    # gameless table must answer MjGAMEEND (clean exit to the menu), and
    # on_sute must never touch k when it is None.
    to_ = Table(11, seed=19)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in out])
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
    t2 = Table(2, seed=5)
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
    if ad and CONCEAL_RESYNC:
        ok &= check(_hand_block(ad[0], 1),
                    "the LOCAL seat's hand is present in its own resync")
        ok &= check(all(not _hand_block(ad[0], s) for s in (0, 2, 3)),
                    "every non-local seat's hand is EMPTY -- no face leak: %r"
                    % {s: _hand_block(ad[0], s) for s in (0, 2, 3)})
    # A subtype-3 discard by a BOT keeps THAT seat's hand (the tile flies from
    # it) but still blanks the other two opponents.
    if CONCEAL_RESYNC:
        tcz = Table(24, seed=71)
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
    td = Table(6, seed=11)
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
    tn = Table(14, seed=23)
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
    tro = Table(15, seed=29)
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
    td = Table(41, seed=211)
    td.seat_player(0xD001, b"dbl", seat=0)
    td.fill_with_bots()
    td.handle(janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0,
                           sub=3, length=0x18)[:0x18])

    def _dops(recs):
        return [M_NAME(janwire.unpack(r)["opcode"]) for r in recs or ()]

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
    tq = Table(16, seed=31)
    tq.seat_player(0xAAA1, seat=0)
    tq.seat_player(0xAAA2, seat=1)
    fq = {"t": 9000.0}
    tq.clock = lambda: fq["t"]
    mq = Manager()
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
    tb = Table(17, seed=41)
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
                    "bot pon must animate too: %r" % [M_NAME(o) for o in ops])
        if ad is not None:
            ok &= check(ad[0x18] == 4 and ad[0x22] == 1 and ad[0x1A] == 0,
                        "motion 4, caller seat 1, discarder seat 0: "
                        "%d/%d/%d" % (ad[0x18], ad[0x22], ad[0x1A]))
        ok &= check(len(kb.hands[1].melds) == 1
                    and kb.hands[1].melds[0].kind == mj.PON,
                    "the meld is really on the bot's hand")
    # A bot must NOT open a hand that can no longer score: a chi that leaves no
    # yaku path is how a bot bricks itself for the rest of the hand.
    tn2 = Table(18, seed=43)
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
    bot1 = tn2.bots.get(1) or Bot(1)
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

    ta = Table(19, seed=47)
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
                % [M_NAME(o) for o in ops])
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
    tb3 = Table(20, seed=53)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in out3])

    # A BOT's ron must outrank a human's granted pon. Until arbitration, a
    # human answer closed the offer without ever asking the bots.
    tb4 = Table(21, seed=59)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in out4])

    # And the SWEEPER resolves the answers on file instead of discarding them:
    # seat 1's ron waiting on seat 0's silence is still a ron.
    tw = Table(22, seed=61)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in swept])

    # --- a mid-game QUIT substitutes, it does not strand the table ----------
    # WARNING: MjBYE used to set state="finished" unconditionally -- with two humans,
    # one player quitting mid-hand killed the whole table and every later line
    # from the OTHER human got silence (seen live: "it doesn't seem to quite
    # know how to handle someone leaving"). drop_seat existed for exactly this
    # and HAD NO CALLERS -- the same pattern as Bot.calls before 2026-09-04.
    tv = Table(23, seed=67)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in outq])
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
    tv2 = Table(25, seed=73)
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
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in outv])

    # --- a seat leaving ------------------------------------------------------
    tl = Table(7, seed=12)
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
    mgr = Manager()
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
    ms = Manager()
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
        return [M_NAME(janwire.unpack(r)["opcode"]) for r in recs]

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
    mp1 = Manager()
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
    tp2 = Table(102, seed=7)
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
    tp2b = Table(103, seed=8)
    tp2b.seat_player(0x1112, "H", seat=0, member=15)
    tp2b.fill_with_bots()
    d = tp2b.handle(_rdy0)
    a1 = tp2b.handle(_client_ack(d[0], 0))
    a2 = tp2b.handle(_client_ack(d[0], 0))
    ok &= check(bool(a1) and a2 == [] and any(e[0] == "replay" for e in tp2b.log),
                "P2: a replayed HAIPAIACK is silent, not a second draw: %r" % _ops(a2))

    # --- finding 10 (probes P3/P5): the client-stamped seat is not trusted ---
    tp3 = Table(104, seed=8)
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
    tp5 = Table(105, seed=10)
    tp5.seat_player(0x1111, "H", seat=0, member=15)
    tp5.fill_with_bots()
    o5 = tp5.handle(_stamped(M.MjREADY, 3, f16=0))
    ok &= check(tp5.live == set() or tp5.live == {0},
                "P5: a MjREADY stamped with a bot's seat does not make it live: %r"
                % (tp5.live,))
    ok &= check(3 in tp5.bots and any(e[0] == "ready-from-bot-seat" for e in tp5.log),
                "P5: seat 3 stays a bot, and it is logged")

    # --- finding 11 (probe P8): SUTE_KAN on a non-kan slot ------------------
    tp8 = Table(106, seed=3)
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
    tk = Table(107, seed=42)
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
    tc = Table(108, seed=43)
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
    mc2 = Manager()
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
    mc3 = Manager()
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
    _km = KAN_MOTION
    globals()["KAN_MOTION"] = True
    try:
        tkm = Table(111, seed=3)
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
        globals()["KAN_MOTION"] = _km

    # A BOT daiminkan: an open bot holding the triplet takes the kan, the
    # records animate it (resync path) and the bot plays on.
    tb = Table(112, seed=46)
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
    tchi = Table(113, seed=47)
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
    tx = Table(114, seed=48)
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
    m3 = Manager()
    m3.clock = lambda: clk3["t"]
    t3_ = _two_humans(m3, 115, 49)
    m3.handle(_rdy0, member_id=15)
    m3.handle(_client_ack(t3_._last_for[0], 0), member_id=15)
    aw3 = t3_._awaiting
    ok &= check(aw3 and aw3[0] == M.MjTSUMO and aw3[1] in (0, 1), "sweeper rig: a human on the clock")
    silent = aw3[1]
    other = 1 - silent
    strikes = 0
    for _i in range(TIMEOUTS_TO_DROP + 2):
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
                "(strikes %d): dropped=%r" % (TIMEOUTS_TO_DROP, strikes, t3_.dropped))
    ok &= check(t3_.timeouts.get(silent) is None or t3_.timeouts.get(silent) >= 1,
                "...the strike counter drove it")
    ok &= check("MjMEMBERLEAVE" in _ops(m3.pending_for_member(15 if other == 0 else 6)),
                "...and MjMEMBERLEAVE reaches the survivor's outbox")
    # A line from a seat resets its strike count.
    t3b = Table(116, seed=50)
    t3b.seat_player(0xA1, seat=0, member=15)
    t3b.timeouts[0] = 2
    t3b.handle(_stamped(M.MjALLDATAACK, 0), member=15)
    ok &= check(t3b.timeouts.get(0) is None, "any line from the seat clears its strikes")
    # A BROADCAST ack nobody answers: re-sent ONCE, then the ladder proceeds.
    clk4 = {"t": 30000.0}
    m4 = Manager()
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
    ok &= check(t4_.id in m4.tables and Manager._owed_seats(t4_),
                "...HELD first, because both seats are still owed records: %r"
                % (Manager._owed_seats(t4_),))
    clk4["t"] += FINISHED_GRACE + 1
    m4.tick()
    ok &= check(t4_.id not in m4.tables,
                "...and the Manager FORGETS it past the grace (finding 32): "
                "tables %r" % (sorted(m4.tables),))

    # The sweeper THREAD itself: real clock, tiny deadline, it fires and the
    # hook is told which members have records waiting.
    _ss = SWEEP_S
    globals()["SWEEP_S"] = 0.05
    try:
        m5 = Manager(sweeper=True)
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
        globals()["SWEEP_S"] = _ss

    # --- finding 9: two threads on one table, under the lock -----------------
    m6 = Manager()
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
    m7 = Manager()
    ok &= check(m7.handle(_stamped(M.MjBYE, 0, f16=0), member_id=77) == [] and not m7.tables,
                "a stray member's MjBYE mints NO table")
    g7 = m7.handle(_stamped(M.MjSUTE, 0), member_id=77)
    ok &= check(_ops(g7) == ["MjGAMEEND"] and not m7.tables and 77 not in m7.by_member,
                "a stray member's in-game line gets the ghost MjGAMEEND with no state: %r"
                % _ops(g7))
    t7_ = m7.table_for(78)
    ok &= check(78 in m7.by_member and t7_.seat_of_member(78) == 0,
                "the solo path maps the member to its seat (finding 10 needs it)")
    ok &= check(isinstance(t7_.log, _Log) and t7_.log.maxlen == LOG_MAX,
                "Table.log is a ring of %d" % LOG_MAX)
    for _i in range(LOG_MAX + 50):
        t7_.log.append(("x", _i))
    ok &= check(len(t7_.log) == LOG_MAX and t7_.log[-1] == ("x", LOG_MAX + 49)
                and t7_.log[-2:][0] == ("x", LOG_MAX + 48),
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
    m7b = Manager()
    m7b.clock = lambda: clk7["t"]
    t7b = m7b.table_for(79)
    clk7["t"] += TABLE_TTL + 1
    m7b.tick()
    ok &= check(t7b.id not in m7b.tables and 79 not in m7b.by_member,
                "an idle table past TABLE_TTL is forgotten")
    # And a finished one: every live seat's MjBYE after MjGAMEEND.
    m7c = Manager()
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
    m7d = Manager()
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
    clk7d["t"] += FINISHED_GRACE + 1
    m7d.tick()
    ok &= check(t7d.id not in m7d.tables and 6 not in m7d.by_member,
                "...and past POL_JAN_FINISHED_GRACE it is forgotten anyway "
                "(a player who walked away cannot hold a table for ever)")
    # The hold is for a seat that is WAITING. A bot, a seat that said MjBYE and
    # a spectator key are not waiting for anything.
    m7e = Manager()
    t7e = _two_humans(m7e, 122, 56)
    t7e.start_game()
    t7e.queue_for(1, _stamped(M.MjGAMEEND, 1))
    t7e._byes.add(1)
    ok &= check(Manager._owed_seats(t7e) == [],
                "a seat that has already said MjBYE is owed nothing")
    t7e._byes.discard(1)
    t7e.bots[1] = Bot(1)
    ok &= check(Manager._owed_seats(t7e) == [],
                "...nor is a seat a CPU is playing")
    t7e.bots.pop(1, None)
    ok &= check(Manager._owed_seats(t7e) == [1],
                "...but a live human waiting on a queued record IS")

    # --- finding 30: room-qualified lobby tables -------------------------------
    m8 = Manager()
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
    tc_ = m8.table_for_lobby(Manager.lobby_key(1, 2), [(6, 0, "B", 0xB1)])
    ok &= check(tc_ is tb_ and m8.table_for_lobby(2, [(7, 0, "C", 0xC1)],
                                                 room=2).channel
                == (b"#MJS0T002002" if _chan_qualified else b"#MJS0T002"),
                "a composite passed as lobby_id finds the same table")
    ok &= check(m8.table_for_lobby(3, [(9, 0, "D", 0xD1)]).channel == b"#MJS0T003",
                "room 0 (no room known) keeps the bare #MJS0T00n channel")
    ok &= check(Manager.lobby_key(1, 2) == (2 << 16) | 1 and Manager.lobby_key(3) == 3,
                "the key is room << 16 | table, the bare id without a room")
    if janseats is not None and hasattr(janseats, "split_table_id"):
        ok &= check(janseats.split_table_id(janseats.room_table_id(2, 1)) == (2, 1),
                    "janseats round-trips the composite id")

    # --- finding 29: rejoin ------------------------------------------------------
    m9 = Manager()
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
    globals()["janseats"] = _Seats
    try:
        o9 = m9.handle(_stamped(M.MjREADY, 1, f16=0), member_id=6)
    finally:
        globals()["janseats"] = _js
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
    tbq = Table(122, seed=56)
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
    thq = Table(123, seed=57)
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
        tst = Table(124, seed=58)
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
        tsw = Table(125, seed=59)
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
    _old_uma = (SASHIUMA, SASHIUMA_BOTS)
    globals()["SASHIUMA"] = True
    globals()["SASHIUMA_BOTS"] = "accept"
    tu = Table(21, seed=5)
    tu.seat_player(0x5151, b"Ume", seat=0)
    out = tu.handle(_ready())
    ops = [janwire.unpack(r)["opcode"] for r in out]
    ok &= check(ops == [M.MjSASHIUMASTART],
                "READY with sashiuma on offers the table FIRST, no deal yet: %r"
                % [M_NAME(o) for o in ops])
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
                "the last REQUEST answers with SELECT: %r" % [M_NAME(o) for o in ops])
    if out:
        sel = out[0][0x18:0x1E]
        ok &= check(sel[5] == (M.sashiuma_bit(0) | M.sashiuma_bit(2)),
                    "SELECT shows the bot ACCEPTED (both bits in slot 5): %r" % sel)
        ok &= check(all(sel[i] == 0 for i in range(5)),
                    "and no other pair is proposed: %r" % sel)
    # a REQUEST cannot set ANOTHER seat's bit
    tu2 = Table(22, seed=5)
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
                "batch: %r" % [M_NAME(o) for o in ops])
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
    tu3 = Table(23, seed=5)
    tu3.seat_player(0x5353, b"Pass", seat=0)
    tu3.handle(_ready())
    o1 = tu3.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, bytes(6)))
    o2 = tu3.handle(_client_uma(M.MjSASHIUMAARGEE, 0, bytes(6)))
    ok &= check([janwire.unpack(r)["opcode"] for r in o1] == [M.MjSASHIUMASELECT]
                and bool(o2) and o2[0][0x18:0x1E] == bytes(6)
                and tu3.sashiuma_pairs == [] and tu3.kyoku is not None,
                "an empty pick yields an all-zero RESULT, no bets, and the deal")
    # a human who quits mid-handshake stops being waited on (two humans)
    tu5 = Table(25, seed=5)
    tu5.seat_player(0x5555, b"Stay", seat=0)
    tu5.seat_player(0x5656, b"Quit", seat=1)
    o5 = tu5.handle(_ready())
    ok &= check([janwire.unpack(r)["opcode"] for r in o5]
                == [M.MjSASHIUMASTART, M.MjSASHIUMASTART],
                "both humans are offered the table: %r"
                % [M_NAME(janwire.unpack(r)["opcode"]) for r in o5])
    b5 = bytearray(6)
    b5[M.sashiuma_slot(0, 1)] = M.sashiuma_bit(0)      # seat 0 proposes to seat 1
    ok &= check(tu5.handle(_client_uma(M.MjSASHIUMAREQUEST, 0, b5)) == [],
                "one REQUEST in, the other human still owed: nothing goes out")
    o5 = tu5.drop_seat(1, "quit")
    ops5 = [janwire.unpack(r)["opcode"] for r in o5]
    ok &= check(ops5[:1] == [M.MjMEMBERLEAVE] and M.MjSASHIUMASELECT in ops5,
                "the quitter is announced and the phase moves on without it: %r"
                % [M_NAME(o) for o in ops5])
    selrec = [r for r in o5 if janwire.unpack(r)["opcode"] == M.MjSASHIUMASELECT]
    ok &= check(bool(selrec) and selrec[0][0x18 + M.sashiuma_slot(0, 1)] == 3,
                "and the quitter's seat, now a bot, accepts the bet (slot 0 = 3)")
    o5 = tu5.handle(_client_uma(M.MjSASHIUMAARGEE, 0, bytes([3, 0, 0, 0, 0, 0])))
    ok &= check(tu5.sashiuma_pairs == [(0, 1)] and tu5.kyoku is not None,
                "the bet is ON with the substitute and the hand deals: %r"
                % tu5.sashiuma_pairs)
    # switched off: READY deals at once, exactly as before
    globals()["SASHIUMA"] = False
    tu4 = Table(24, seed=5)
    tu4.seat_player(0x5454, b"Off", seat=0)
    o4 = tu4.handle(_ready())
    ok &= check(bool(o4) and janwire.unpack(o4[0])["opcode"] == M.MjHAIPAI
                and tu4.sashiuma is None,
                "POL_JAN_SASHIUMA=0: READY deals at once, no handshake")
    globals()["SASHIUMA"], globals()["SASHIUMA_BOTS"] = _old_uma

    # --- THE RECORD-BUILDING LAYER, at the client's read offsets --------------
    # (2026-09-04: the round-state block, the rule vector, the win screen,
    # the draw banner, the riichi flag, the kan records, HALF1.) Every offset
    # here is the one the C loads it from; janmsgs' banners cite the lines.
    _rdy = janwire.pack(opcode=M.MjREADY, f13=0, src=0, dst=4, f16=0, sub=3,
                        length=0x18)[:0x18]
    tq = Table(30, seed=101)
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
    pw = Table._payment_words
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
    tw2 = Table(31, seed=103)
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
                    % [M_NAME(o) for o in ops_w])
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
    tw3 = Table(32, seed=105)
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
    td = Table(33, seed=107)
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
                % ([M_NAME(o) for o in ops_d], out_d[0][0x22] if out_d else -1))
    if out_d:
        rows_d = [[out_d[0][M.ALLDATA_HAND + s * M.ALLDATA_HAND_STRIDE + i] & 0x3F
                   for i in range(14)] for s in range(4)]
        ok &= check(sum(1 for b in rows_d[2] if b) >= 13,
                    "and the tenpai bot's real tiles are in the hand block")
    _old_cr = CONCEAL_RESYNC
    globals()["CONCEAL_RESYNC"] = True
    out_d2 = td.on_kyoku_end()
    if out_d2:
        rows_d2 = [[out_d2[0][M.ALLDATA_HAND + s * M.ALLDATA_HAND_STRIDE + i] & 0x3F
                    for i in range(14)] for s in range(4)]
        ok &= check(any(rows_d2[0]) and any(rows_d2[2]) and not any(rows_d2[1])
                    and not any(rows_d2[3]),
                    "under CONCEAL_RESYNC the noten opponents stay blank, the "
                    "tenpai ones reveal")
    globals()["CONCEAL_RESYNC"] = _old_cr
    kd.result = (mj.ABORT, {"why": "kyushu", "dealer_repeat": True})
    out_a = td.on_kyoku_end()
    ok &= check(len(out_a) == 2 and out_a[0][0x18] == 10 and out_a[0][0x22] == 0,
                "an abortive draw still sends the banner record, mask 0 "
                "(the handler fires banner 6 unconditionally)")

    # Bot riichi: +0x21 on the motion-3 record (yamaguchi.c:7920-7932).
    tr2 = Table(34, seed=109)
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
    tk = Table(35, seed=111)
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
    _old_km = KAN_MOTION
    globals()["KAN_MOTION"] = False
    out_k = tk.msg_kan("ankan", kseat, pre_k)
    ok &= check([janwire.unpack(r)["opcode"] for r in out_k] == [M.MjALLDATA, M.MjTSUMO]
                and out_k[0][0x18] == 0 and out_k[1][0x18] == 2,
                "KAN_MOTION off: the resync path (ALLDATA subtype 0 + TSUMO motion 2)")
    wk0 = out_k[0][0x44:0x52]
    ok &= check(wk0[0] == 0 and wk0[1] != 0 and (wk0[6] & 0x40) and (wk0[4] & 0x40)
                and (wk0[8] & 0x40) == 0,
                "the resync's dead wall: rinshan slot 0 EMPTY, indicators at 4 "
                "and 6 revealed: %r" % ["%#x" % v for v in wk0])
    globals()["KAN_MOTION"] = True
    out_k = tk.msg_kan("ankan", kseat, pre_k)
    ops_k = [janwire.unpack(r)["opcode"] for r in out_k]
    ok &= check(ops_k == [M.MjALLDATA, M.MjTSUMO] and out_k[0][0x18] == 7
                and out_k[1][0x18] == 0,
                "KAN_MOTION on: ONE motion-7 ALLDATA then a motion-0 TSUMO (the "
                "handler flies the rinshan itself): %r motions %r"
                % ([M_NAME(o) for o in ops_k], [r[0x18] for r in out_k]))
    rk = out_k[0]
    ok &= check(rk[0x1B] == M.tile_byte(mj.MAN) and rk[0x1C] == M.tile_byte(hk.drawn)
                and rk[0x1D] == 0 and rk[0x1E] == 3 and rk[0x22] == kseat
                and rk[0x23 + kseat] == (1 if ISHUMAN_ENABLE and kseat not in tk.bots
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
    globals()["KAN_MOTION"] = False
    out_kd = tk.msg_kan("ankan", kseat, pre_k, rinshan="defer")
    ok &= check(len(out_kd) == 1 and out_kd[0][0x18] == 0,
                "deferred on the resync path: the ALLDATA alone")
    # A daiminkan on another seat's discard: motion 6 with the discarder.
    tk2 = Table(36, seed=113)
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
    globals()["KAN_MOTION"] = True
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
    globals()["KAN_MOTION"] = _old_km

    # HALF1: per-SEAT columns from a finished game (yamaguchi2.c:8430-9800).
    th = Table(37, seed=115)
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
    ok &= check(all(fin_h[r["seat"]] == _round_p(r["result"]) for r in rank_h)
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

    mg = Manager()
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
    ok &= check(not any(janwire.unpack(r)["opcode"] in Table.GALLERY_NEVER
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
    _old_skip = GALLERY_SKIP
    globals()["GALLERY_SKIP"] = True
    tg._gallery_skip()                       # what _ack_advance(MjYAKUDISP) calls
    globals()["GALLERY_SKIP"] = _old_skip
    skip_g = [r for r in (tg.outbox.get(tg.gallery_key(44)) or [])
              if janwire.unpack(r)["sub"] == 6]
    ok &= check(len(skip_g) == 1 and len(skip_g[0]) == 0x18 and skip_g[0][0x12] == 0x30
                and skip_g[0][0x14] == 2 and skip_g[0][0x17] == 6
                and not (tg.outbox.get(tg.gallery_key(45)) or []),
                "POL_JAN_GALLERY_SKIP: one 0x18-byte op-0x30 sub-6 record with the "
                "slot at +0x14 to the acked spectator only: %r" % [len(r) for r in skip_g])
    mg.pending_for_member(44)
    # Eviction: a spectator that stops draining is dropped, never re-nudged.
    tg.outbox[tg.gallery_key(44)] = [b"x"] * Table.GALLERY_OUTBOX_MAX
    tg.begin_routing()
    tg._emit(M.gameend(tg.next_seq()), "probe")        # any record
    tg.take_routes()
    mg._release_galleries()
    ok &= check(44 not in tg.gallery and mg.by_member.get(44) is None
                and tg.outbox.get(tg.gallery_key(44)) is None
                and any(e[0] == "spectator-gone" and e[1][0] == 44 for e in tg.log),
                "a spectator with %d undrained records is EVICTED (outbox dropped, "
                "routing forgotten)" % Table.GALLERY_OUTBOX_MAX)
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
    _old_cd, _old_cr = CONCEAL_DEAL, CONCEAL_RESYNC
    globals()["CONCEAL_DEAL"] = True
    globals()["CONCEAL_RESYNC"] = True
    mc_ = Manager()
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
    globals()["CONCEAL_DEAL"] = _old_cd
    globals()["CONCEAL_RESYNC"] = _old_cr
    if _old_seats_env is None:
        os.environ.pop("POL_JAN_SEATS", None)
    else:
        os.environ["POL_JAN_SEATS"] = _old_seats_env

    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    if _old_res is None:
        os.environ.pop("POL_RESOURCE_DIR", None)
    else:
        os.environ["POL_RESOURCE_DIR"] = _old_res
    return 0 if ok else 1


def M_NAME(op):
    for k, v in vars(M).items():
        if k.startswith("Mj") and v == op:
            return k
    return "op%#x" % op


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    return selftest(trace=a.trace)


if __name__ == "__main__":
    raise SystemExit(main())
