-- Janhourou's durable state, formerly JSON files under /data.
--
-- CrystalHoLo's migrations are numbered 4001..4999 because they share
-- OpenLobby's schema_migrations table, which is keyed by version alone.
-- Every table here starts with jan_. None has a foreign key into the account
-- tables: a member id is kept as the game already used it, and a row
-- outliving a deleted member is what the files did too.

-- The event record (janevent.py, formerly <resources>/janevent.json): the
-- one event the ranking screen runs, its window, and whether the scheduler
-- opened it. The server uses the row named 'current'; the self-test works on
-- a row of its own, so running it on a live server cannot touch the event.
CREATE TABLE jan_event (
    name       TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The ranking's previous order (janstats.py, formerly
-- <resources>/jan-rank-snapshot.json), which feeds the up/down/New glyph on
-- each rank list. One row per category.
CREATE TABLE jan_rank_snapshot (
    category   TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The web board's Discord bookkeeping (polboards.py, formerly
-- /state/<name>_discord.json and /state/discord_channels.json): which
-- messages the board posted and edits, and where each feed posts. `name` is
-- what the file was called without its extension.
CREATE TABLE jan_board_state (
    name       TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
