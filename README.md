# CrystalHoLo

A server reimplementation for Square Enix's Janhourou, the PlayStation 2
mahjong parlour of the PlayOnline service (2003-2007, Japan only). Together
with the OpenLobby core it lets an unmodified client enter the parlours and
rooms, sit at a table, play a hanchan against the computer players or other
people, keep a record, and see the weekly rankings, with no connection to
Square Enix. Calls, riichi, kans, the dead wall, the dora, the scoring
tables and the five ranking lists are all served.

This project is a clean-room reimplementation based on protocol observation.
It contains no Square Enix code, art, or data: the game reads no client
tables at all, and every file the client fetches is built by the server.

## How it fits the core

A hand of mahjong is played on the connection the client already holds to
the core's login service, plus the lobby's resource fetches, so this title
runs INSIDE the core's `login` and `authsess` processes as a plugin
(OpenLobby's `services/titles.py`, `POL_TITLES=jantitle`). The parlour and
zone listener the client also dials, `gi003.pol.com:51272`, is this
repository's own `jan` service. The repository ships an image layered on
the core's, a compose override that swaps it into those two services and
adds the listener, and an optional live board.

## Prerequisites

- The OpenLobby core, checked out beside this repository and already built
  once (`docker compose up -d --build` in that checkout)
- A PlayStation 2 with PlayOnline and Janhourou installed on its hard disk,
  or an emulator running such an install; see "PlayStation 2 clients" in
  the OpenLobby README for what that involves. There is no PC client.
- Docker with Compose v2

## Bring-up

```
cp .env.example .env
docker compose --project-directory ../openlobby \
    -f ../openlobby/docker-compose.yml -f docker-compose.yml up -d --build
```

That rebuilds the core's `login` and `authsess` containers from the
`crystalholo` image (the core image plus this title), starts the `jan`
listener on 51272, and leaves everything else the core's. The image is
layered on whatever `openlobby:latest` is on your machine: after pulling a
newer OpenLobby, rebuild it there first (`docker compose build` in its
checkout), or this title runs on the old core underneath. The long command
is the price of running inside the core's project; put it in a shell alias,
or set `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` in your environment. To take
the title out again, run the core's own `docker compose up -d` from its
checkout.

### Moving from an earlier release

Earlier releases kept the event record, the ranking's previous order and the
board's Discord bookkeeping as files. They now live in OpenLobby's PostgreSQL
(`jan_event`, `jan_rank_snapshot`, `jan_board_state`), and the files are
imported once, after OpenLobby's own import (its docs/database.md, "Moving
an existing /data") and before the title starts. From this directory:

```
DC="docker compose --project-directory ../openlobby -f ../openlobby/docker-compose.yml -f docker-compose.yml"
$DC run --rm --no-deps --entrypoint python jan janstore.py import event /data/resources/janevent.json
$DC run --rm --no-deps --entrypoint python jan janstore.py import rank_snapshot /data/resources/jan-rank-snapshot.json
$DC run --rm --no-deps -v crystalholo_jan-board-state:/state:ro --entrypoint python jan janstore.py import board_state /state
```

The last reads the board's old state volume (`crystalholo_jan-board-state`,
from when this was a compose project of its own; `docker volume ls` shows the
name) and matters only where the board posted to Discord; without it the
board posts its messages afresh. Each command only reads its source, runs in
one transaction, prints what it imported and each entry it could not map,
and refuses a table that already holds rows unless given `--merge`, which
adds only the keys the table lacks. `--dry-run` prints the same report and
writes nothing, and a second run changes nothing. A file that is not there
has nothing to import: an install that never ran an event has no
`janevent.json`.

### With Tetra Master

Both titles run in the same processes. With CrystalMaster brought up once
(so its image exists), build this image on top of it and name both plugins:

```
OPENLOBBY_IMAGE=crystalmaster:latest POL_TITLES=tmtitle,jantitle \
docker compose --project-directory ../openlobby \
    -f ../openlobby/docker-compose.yml -f ../crystalmaster/docker-compose.yml \
    -f docker-compose.yml up -d --build
```

(or set the two variables in `.env`). Each title serves its own players:
the lobby lists a console asks for are the title's it is in.

### Without building

The image is published to `ghcr.io/prettyopenlobby/crystalholo` on every push,
layered on the published OpenLobby image. Apply the pull-only overrides of
both repositories after their compose files, from this directory:

```
docker compose --project-directory ../openlobby     -f ../openlobby/docker-compose.yml -f ../openlobby/docker-compose.ghcr.yml     -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

`CRYSTALHOLO_TAG` and `OPENLOBBY_TAG` pick the versions (default `latest`). A second image, tagged `with-crystalmaster`, is built on
CrystalMaster's instead of OpenLobby's: set
`CRYSTALHOLO_TAG=with-crystalmaster` and `POL_TITLES=tmtitle,jantitle`
in `.env` to run both PS2 titles from it, with the four overrides
(OpenLobby's two, then this repository's two).

## Pointing a client at it

Everything client-side is the core's: DNS redirection (the console asks for
`gi003.pol.com`, which the core's DNS answers), the CA, an account. Grant
the account Janhourou (content id 3) in the admin panel. The console's main
menu draws a shortcut icon per installed title; Square Enix retired this
one's, so `tools/make_jan_icon.py --www <your portal tree>` composes it from
the neighbouring icons' own pixels and puts it in place.

## Both client builds

Two builds of the game exist: the 2002 one and the 2004 one (20040727_2,
the build a US Viewer installs). They send identical openers, so the server
tells them apart by the build the client claimed on its patch channel
before launch, which the core's patch server records per address in
Valkey (`clientbuild:<address>`, see OpenLobby's docs/database.md). The
2004 build is then served
its own save, table-info, parlour and room-list layouts and its own
in-game records (`services/janmsgs2004.py`). `POL_JAN_SAVE_2004=1` treats
every client as the 2004 build, `0` as the 2002 one; the default `auto`
decides per client.

## Optional switches

Both are off by default and are set in `.env` (the compose file passes
them to the `jan` listener); an empty value is the default.

- `POL_JAN_RESERVE_LIMITS=1` enforces the entry limits a table's master
  sets in the second rules dialog (Money limit, Level limit, Title 1..5
  limit). A player who misses one is answered with the client's own "Entry
  limits not met" instead of a seat. Each limit is stored by the client as
  on or off, so what "Money limit" and "Level limit" demand are the
  server's numbers: `POL_JAN_LIMIT_MONEY_MIN` (JAN balance, default 1) and
  `POL_JAN_LIMIT_LEVEL_MIN` (level, default 1). A title limit demands that
  the player holds that title. The table's seated members are never
  refused.
- `POL_JAN_LNDV_CHANNELS=1` fills the profile, rank and lobby channel ids
  and the lobby domain of the MjGETLNDVACK record with the values Project
  Crystal Server serves, instead of zeros. The explicit `POL_JAN_LNDV_F18`
  / `_F20` / `_F28` / `_F34` / `_F36` fields still win. It needs a core
  image that carries `mgkey.py`; leave it off unless you are comparing the
  two servers.

## The weekly rankings

The five ranking lists (Jan Rating, Titles, Overall Gamble, Weekly Gamble,
Event) are built from the players' records on request; there is no batch
job to run.

The Event list only opens while an event is running. The client cannot
join one (it has no entry screen), so an event is a window in which what
players win is counted:

```
docker compose ... exec authsess python janevent.py --open "Weekend Cup" --days 3
docker compose ... exec authsess python janevent.py --show
docker compose ... exec authsess python janevent.py --close
```

The record lives in OpenLobby's PostgreSQL database (the `jan_event`
table), so the commands run wherever `POL_DATABASE_URL` reaches it; inside
the stack's `authsess` container it already does. `janevent.py --rotate`, run
hourly by cron or a timer the same way, opens and closes events from the
shared event calendar (`eventcal`, when the core provides it);
`POL_JAN_EVENT_AUTO=0` turns that off.

## The live board (optional)

`docker compose ... --profile board up -d` adds a read-only web page of the
standings on port 8791 (bound to localhost; put a reverse proxy in front),
with Discord posting configured by the `POL_BOARDS_JAN_*` variables in
`.env.example`. Out of the box it is a text page: the rendered screen
(`/render.png`, the Discord image) and the 3D watch page draw with art
baked from the game's own files, and that bake needs tools this
repository does not carry. The page's two faces are OFL-licensed subsets
(built by `tools/jan_textfont_build.py` and `jan_titlefont_build.py` from
the fonts' sources); the game's own bitmap font can be traced into web
faces from your copy with `tools/jan_boardfont_build.py --kanji
<install>/Warashi/font/kanji.bin`.

## Selftests

```
python tools/jan_run_all.py
```

runs the offline suite (the rules engine, the game manager, the records,
the lobby lists, the switches under `tests/`, the seam with the core); it
needs the OpenLobby checkout beside this one (or `OPENLOBBY_SERVICES`
pointing at its `services/` directory).
CONTRIBUTING.md says where things are in the tree and what a pull request
needs.

## What is not included, and why

- No Square Enix data: the game reads none, and the lobby lists, the table
  record and the player save are built by the server from the layouts the
  client's readers dictate.
- No board art: the sheets the live board draws on, the game's bitmap font
  and the 3D pieces come from the game's own files and are not shipped.
- No PlayStation 2 install, image, or installer. See the OpenLobby README.
- The Tetra Master rooms that share the lobby's list format are that
  title's, not this one's.

## License

AGPL-3.0 (see LICENSE). If you run a modified version as a service, the
license obliges you to offer your modifications' source to its users.

## Credits

- The PlayOnline preservation community.
- M PLUS 1p and Nunito (SIL Open Font License); the subsets in
  `services/boardart/jan/` keep the OFL's terms.
- three.js (MIT), served by the live board's watch page.
