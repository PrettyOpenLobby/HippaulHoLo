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
                    (a row of the core's blob table per member)
  jansave.py        the player save the client fetches (MJSUserData)
  janevent.py       the running event, for the Event ranking
  janstore.py       reaches the core's polcore: the durable tables, the
                    live keys (jan:*), migrate, status and import
  jan_migrations/   CrystalHoLo's migrations, numbered from 4001
  boardjan.py       the live board of the rankings (the `board` profile)
  polboards.py      the board service the live board runs in
  polgateway.py     the Discord presence of a board bot
tools/              self-tests (`jan_*_test.py`), jan_run_all.py, the test
                    database helper (janpg.py), and operator tools
                    (lobby-list inspectors, icon and font builders)
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

### Changing the packages

`jantable/` and `janworld/` were generated once from the single-file
`jangame.py` and `janhourou.py`, in commit edb5a51. The packages are the
source now and are edited directly; nothing regenerates them. A change
written against a single file elsewhere is carried over by hand into the
module that owns that code today.

`janhourou.py` and `jangame.py` stay as the entry points and as the facades
described above. Each one forwards only the names in its `_OWNERS` table,
which maps every name to the module that owns it, so a new top-level name
is not reachable as `janhourou.NAME` or `jangame.NAME` until it has a line
there. Code in the same package does not need one, since it uses
`<module>.<name>`. `jantitle.py`, the other package, a tool or a test that
reads or rebinds the name through a facade does, and
`tools/facade_rebind_check.py` fails on a rebinding of a name the table does
not list. A new module is imported at the top of the facade and added to
`_MODULES`.

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

Every suite imports the core's `polcore` through `janstore.py`, so the core
has to be found for all of them, and the drivers have to be installed
(`pip install "psycopg[binary]" psycopg-pool valkey`). The suites in
`NEEDS_DB` in `tools/jan_run_all.py`, among them every suite that records a
game, get a throwaway PostgreSQL database each from `tools/janpg.py`, which
uses Docker or the server `POL_TEST_DATABASE_URL` names; `jan_store_test`
also starts a Valkey, or uses `POL_TEST_VALKEY_URL`. Without a server those
suites report SKIP, and `POL_TEST_REQUIRE_DB=1` makes that a failure.
`tools/jan_import_test.py` rebuilds the old files from git at a pinned
commit, so a shallow clone needs `git fetch --unshallow` first.

On Windows, `jan_board_test` checks for the container path
`/state/jan_discord.json`, which Windows resolves against the current drive:
run the suite from a drive that has no `\state` folder.

A new self-test is registered by hand in `tools/jan_run_all.py`. The list is
explicit on purpose: a suite that is not registered does not run.

## Where state lives

Anything that must survive a restart is in the core's PostgreSQL. A
member's record is the blob (`<member>`, `jan_stats.json`) in the core's
`blob` table, read and written through `janstats.load` and `janstats.store`.
The event record, the ranking's previous order and the board's Discord
bookkeeping are CrystalHoLo's own tables (`jan_event`, `jan_rank_snapshot`,
`jan_board_state`), through `janstore.py`. Live state that several
containers read (the seats, each table's rules, the room-key cache, the
tables being watched) goes in Valkey through the core's `polcore.kv` under
`jan:` keys. Nothing durable goes in Valkey: losing it loses who is seated
and what is being played, and nothing else. Files are only for what the
operator edits.

A schema change is a new file in `services/jan_migrations/` with the next
number. A shipped migration is never edited. The core's `schema_migrations`
table is keyed by the number alone and shared with the core and the other
titles, so CrystalHoLo keeps to 4001-4999 and a table name that starts with
`jan_`; a reused number is silently skipped.

Moving a file into the database comes with an importer in `janstore.py`
(`python janstore.py import event|rank_snapshot|board_state ...`). It only
reads its source, runs in one transaction, refuses a table that already
holds rows unless given `--merge`, writes nothing with `--dry-run`, and
changes nothing on a second run. Its command is added to `TITLES` in
OpenLobby's `tools/db_import.py` and to its `docs/database.md`.

`live_sessions.py` is the core's. Janhourou publishes its live-game count
with `live_sessions.write_marker` (`live:authsess-jan`); a copy of the
module must not be added here. `.dockerignore` keeps one out of the image
and the build refuses an image whose `live_sessions` is not the core's. A
deploy script asks a running container, for example
`docker compose exec -T authsess python live_sessions.py count authsess-jan`.

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
