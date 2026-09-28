"""The POL_JAN_* switches the game reads at import, and the process-wide shutdown flag."""
import os

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
#: rule nothing is watchable. The document goes to Valkey under WATCH_FILE,
#: which despite its name is now a key (POL_JAN_WATCH_KEY, default
#: jan:tables-live; it was the file <POL_DATA_DIR>/jan-tables-live.json), and
#: POL_JAN_WATCH_KEY=0 turns it off.
WATCH_FILE = os.environ.get("POL_JAN_WATCH_KEY", "jan:tables-live").strip()
#: the key outlives its last write by this long; the board calls a document
#: older than its own WATCH_STALE_S (120 s) gone anyway
WATCH_TTL_S = 300
WATCH_EVERY_S = 5.0
#: how long a FINISHED game's last frame stays in the file (the table itself
#: is forgotten at once): long enough for the board's 1 s sampler to see WHY
#: it stopped -- the last human left -- instead of a table that just vanishes
WATCH_FINAL_S = 15.0
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
