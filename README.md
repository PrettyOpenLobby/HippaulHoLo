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
listener on 51272, and leaves everything else the core's. The long command
is the price of running inside the core's project; put it in a shell alias,
or set `COMPOSE_FILE` and `COMPOSE_PROJECT_NAME` in your environment. To take
the title out again, run the core's own `docker compose up -d` from its
checkout.

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

## Pointing a client at it

Everything client-side is the core's: DNS redirection (the console asks for
`gi003.pol.com`, which the core's DNS answers), the CA, an account. Grant
the account Janhourou (content id 3) in the admin panel. The console's main
menu draws a shortcut icon per installed title; Square Enix retired this
one's, so `tools/make_jan_icon.py --www <your portal tree>` composes it from
the neighbouring icons' own pixels and puts it in place.

## The weekly rankings

The five ranking lists (Jan Rating, Titles, Overall Gamble, Weekly Gamble,
Event) are built from the players' records on request; there is no batch
job to run.

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
the lobby lists, the seam with the core); it needs the OpenLobby checkout
beside this one (or `OPENLOBBY_SERVICES` pointing at its `services/`
directory).

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
