-- The learned b/g/PTL room keys (janlobby, the room-key map).
--
-- The client names the room it is listing with a u64 that nobody has decoded,
-- so login learns which channel each key belongs to by watching the asker JOIN
-- it. The map was kept only in the Valkey hash jan:room-keys, and a Valkey
-- recreate on 2026-10-08 emptied it: the next fetch was served room 0 tables
-- and the master of a room 1-1 table could not press Start Game. A key never
-- changes its room, so the observation is durable. Valkey stays the copy login
-- reads, and this table refills it after a loss.
--
-- room_key is the key in decimal, as text, the same string the hash uses.
CREATE TABLE jan_room_keys (
    room_key   TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,
    learned_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
