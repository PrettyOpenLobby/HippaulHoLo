# Contributing to CrystalHoLo

CrystalHoLo is the Janhourou title for the OpenLobby core: the mahjong
game, its lobby lists, the player records and rankings, and an optional live
board. This page says where things are, how to run the checks, and what a
pull request needs. The README covers bringing the server up.

## Where things are

```
services/
  jantitle.py       the title plugin the core loads (POL_TITLES=jantitle);
                    the seam between the core's bands and everything below
  janhourou.py      entry point of the parlour listener (--serve, --selftest)
                    and a facade over janworld/
  janworld/         the world server: one inbound line to its answer
  jangame.py        entry point of the game's selftest and a facade over
                    jantable/
  jantable/         the table and game manager: seats, the hand in progress,
                    the records each move produces
  janwire.py        the 32-byte message record and its text framing
  janmsgs.py        the in-game records: tile codec, builders, parsers
  janmsgs2004.py    the same records reshaped for the 2004 client build
  janmahjong.py     the rules engine: wall, hands, calls, yaku, fu, payment
  janrules.py       the rule set a table's master picked, per table
  janseats.py       who reserved which table and who is its master
  janlobby.py       the parlour, room and table lists, built from live state
  janstats.py       the player record a finished hanchan leaves behind
  jansave.py        the player save the client fetches (MJSUserData)
  janevent.py       the running event, for the Event ranking
  boardjan.py       the live board of the rankings (the `board` profile)
  polboards.py      the board service the live board runs in
  polgateway.py     the Discord presence of a board bot
tools/              self-tests (`jan_*_test.py`), jan_run_all.py, and
                    operator tools (lobby-list inspectors, icon and font builders)
tools/split/        the generator that cut jantable/ and janworld/, and its maps
tests/              newer self-tests, run by jan_run_all.py
```

`janworld/` is cut along the wire and `jantable/` along the game. Each
package's `__init__.py` lists its modules with one line each.

janworld:

```
wirelog.py        the janhourou log channel: timestamps, hexdumps, a record described
opcodes.py        opcode and notice names, result codes, which ACK answers which request
reservelimits.py  a table's entry limits and the reservation they refuse
opener.py         MjGETLNDV, MjCHECKSAVEDATA, EmISEVENT: what the client waits on first
seating.py        the room a member is in, the seats the lobby store holds, the names drawn
notices.py        server-to-client notices and the table member list
builds.py         which client build is asking, and records reshaped for the 2004 one
pushqueue.py      records for a member who is not the one talking
dispatch.py       handle_line: one inbound line to its answer
social.py         the table master's seat and commands, kicks, stored rules, table chat
galley.py         spectator requests, and which tables the web board may show
server.py         the standalone TCP listener on 51272
selftest.py       the offline selftest and the command line
```

jantable:

```
knobs.py          the POL_JAN_* switches and the shutdown flag
narration.py      the plain-English move trace and the names it prints
bots.py           Bot, the seat with no client behind it
table.py          Table: seats, sequencing, deadlines, a seat leaving, per-seat delivery
tablegallery.py   spectators and the copies they are sent
tablestate.py     the board as the records carry it
tablemsgs.py      the records a table sends: deal, draw, call offer, yaku, settlement
tableresults.py   the end of a hanchan: the player record and the result screens
tablesashiuma.py  the pre-game side bet
tableflow.py      the drive loop: turns, bot moves, calls, kans, wins, the end of a hand
tableinbound.py   inbound in-game records: replays, the dispatch, discards, call answers
manager.py        Manager: every table in the process, routing, the sweeper, the watch file
selftest.py       the offline selftest (a whole hanchan with no client)
```

`Table` lives in `table.py` and inherits the classes of the other `table*.py`
modules (`TableGallery`, `TableFlow`, ...), one per concern. A method is
found the same way whichever file holds it, so `t.msg_tsumo(...)` and
`Table.GALLERY_NEVER` work as they always did.

Reading order for a first visit:

1. `jantitle.py`: how a line from the client reaches this title from the
   core's login and authsess processes, and how the answers go back.
2. `janwire.py` and `janworld/opcodes.py`: the record every message rides
   in, and the names of the messages.
3. `janworld/dispatch.py`: `handle_line` and `_handle_line`, where every
   lobby-side request is answered or handed to the game manager.
4. `jantable/manager.py`, then `jantable/table.py` and `jantable/tableflow.py`:
   how an in-game record becomes the list of records that answer it.
5. `janmsgs.py` and `janmahjong.py`: the record layouts and the rules.
6. `janlobby.py`, `janseats.py`, `janstats.py`: the lists, the seats and the
   records the lobby screens are built from.

### The facades

`services/janhourou.py` and `services/jangame.py` are where the code used to
live. They are now thin modules that import their package and forward
`janhourou.<name>` and `jangame.<name>` reads and writes to the module that
owns the name. `jantitle.py`, the tools and the tests use the old names and
keep working, including the ones that rebind a name
(`janhourou.LIVE_ROOMS = ...`): the write lands in the owning module, so the
code under test sees it. New code should import from the packages directly.
`tools/facade_rebind_check.py` proves the forwarding holds for every
rebinding in the tree.

### Regenerating the split

The packages are generated. `tools/split/split_jan_pkg.py` takes the flat
module (the single file as it was before the split) and a map
(`tools/split/split_jangame_map.txt`, `split_janhourou_map.txt`) that names
the module each function, class, global and Table method goes to, and writes
the package and the facade. A change made to the flat file, for instance one
ported from another tree, is split again like this:

```
git show <commit before the split>:services/janhourou.py > janhourou_flat.py
# apply the change to janhourou_flat.py
python tools/split/split_jan_pkg.py --src janhourou_flat.py \
    --map tools/split/split_janhourou_map.txt --package janworld \
    --out services/janworld --facade services/janhourou.py
```

A new top-level name has to be added to the map; the tool refuses to write
while anything is unmapped. `--check` prints the report without writing, and
`--verify` confirms the tree is exactly what a flat file and the map give.
Regenerating rewrites the packages from the flat file, so it only fits while
changes still arrive as edits to that file. A change made in a package module
directly has to be made in the flat file too, or the next regeneration
drops it.

## Running the checks

```
python check.py --selftest     # the hygiene scanner can fail (positive controls)
python check.py                # nothing private or proprietary in the tree
python tools/jan_run_all.py    # every self-test; -k <substring> picks a few
```

The suites that exercise the seam with the core need an OpenLobby checkout
beside this one, or `OPENLOBBY_SERVICES` pointing at its `services/`
directory; without it they are skipped and say so. Every suite is expected
to pass on a clean checkout.

On Windows, `jan_board_test` checks for the container path
`/state/jan_discord.json`, which Windows resolves against the current drive:
run the suite from a drive that has no `\state` folder.

A new self-test is registered by hand in `tools/jan_run_all.py`. The list is
explicit on purpose: a suite that is not registered does not run.

## What a pull request needs

- One topic per pull request, with a subject line that says what the server
  now does differently ("Janhourou: a table's entry limits can refuse a
  reservation").
- The checks above green, and a self-test for behaviour that can be pinned
  offline. A change to what a suite pins updates the suite in the same pull
  request.
- No Square Enix content: no client files, captured server blobs, art or
  fonts, and no captured packets in tests. Code that reads such data from the
  user's own install is fine.
- Nothing private: no real addresses or hostnames, no member names or ids.
  A new address becomes an environment knob with a loopback or empty default.
- Plain prose in comments and docs: say what the code does and why.
- Behaviour that exists because the client needs it keeps a comment saying
  which client build, which of its routines, and what happens without it.
  Much of this server is shaped by what the 2002 and 2004 clients check, and
  a reader cannot tell a client requirement from a mistake without that note.

## Reporting a bug

Open an issue with the client build (2002 or 2004), what the screen showed,
and the `janhourou` log lines around it (`/logs` in the containers).
