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
# The code lives in the `jantable` package, one module per concern
# (jantable/__init__.py lists them). This module is the entry point and a
# compatibility facade: `import jangame` still resolves every name, reading
# or writing, to the module that owns it, so the services, tools and tests
# written against the single-file layout keep working unchanged.
import os
import sys
import types  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ONE COPY OF THIS MODULE. `python jangame.py` runs it as `__main__`; a later
# `import jangame` (from the package's own selftest, say) must find this copy
# rather than load a second one.
if __name__ == "__main__":
    sys.modules.setdefault("jangame", sys.modules[__name__])

from jantable import (  # noqa: E402
    deps as _deps,
    knobs as _knobs,
    narration as _narration,
    bots as _bots,
    table as _table,
    tablegallery as _tablegallery,
    tablestate as _tablestate,
    tablemsgs as _tablemsgs,
    tableresults as _tableresults,
    tablesashiuma as _tablesashiuma,
    tableflow as _tableflow,
    tableinbound as _tableinbound,
    manager as _manager,
    selftest as _selftest,
)

# Which jantable module owns each top-level name of the old jangame.py.
_OWNERS = {
    'BOT_POLICY': 'knobs',
    'Bot': 'bots',
    'CONCEAL_DEAL': 'knobs',
    'CONCEAL_RESYNC': 'knobs',
    'CallSnapshot': 'table',
    'DEAL_DELAY': 'knobs',
    'DISCARD_ANIM': 'knobs',
    'FINISHED_GRACE': 'knobs',
    'GALLERY_SKIP': 'tablegallery',
    'GAME_ENABLE': 'knobs',
    'GAME_SEED': 'knobs',
    'INGAME_OPCODES': 'manager',
    'ISHUMAN_ENABLE': 'knobs',
    'KAN_MOTION': 'knobs',
    'LOG_MAX': 'knobs',
    'M': 'deps',
    'M_NAME': 'narration',
    'Manager': 'manager',
    'PHASE': 'narration',
    'RIICHI_AUTO': 'knobs',
    'RoundScalars': 'table',
    'SASHIUMA': 'tablesashiuma',
    'SASHIUMA_BOTS': 'tablesashiuma',
    'SASHIUMA_UMA': 'tablesashiuma',
    'SHUTDOWN': 'knobs',
    'STATS_ENABLE': 'knobs',
    'SWEEPER': 'knobs',
    'SWEEP_S': 'knobs',
    'TABLE_TTL': 'knobs',
    'TIMEOUTS_TO_DROP': 'knobs',
    'TRACE': 'narration',
    'TRACE_ENABLE': 'narration',
    'Table': 'table',
    'WANPAI_LEGACY': 'knobs',
    'WATCH_EVERY_S': 'knobs',
    'WATCH_FILE': 'knobs',
    'WATCH_FINAL_S': 'knobs',
    '_Log': 'table',
    '_SEATPOS': 'narration',
    '_client_ack': 'selftest',
    '_client_naki': 'selftest',
    '_client_sute': 'selftest',
    '_round_p': 'narration',
    '_sv': 'tablesashiuma',
    '_tile': 'narration',
    '_trace': 'narration',
    '_web_tile': 'narration',
    'argparse': 'deps',
    'collections': 'deps',
    'janrules': 'deps',
    'janseats': 'deps',
    'janstats': 'deps',
    'janwire': 'deps',
    'main': 'selftest',
    'mj': 'deps',
    'os': 'deps',
    'random': 'deps',
    'request_shutdown': 'knobs',
    'selftest': 'selftest',
    'struct': 'deps',
    'sys': 'deps',
    'threading': 'deps',
    'time': 'deps',
}
_MODULES = {
    'deps': _deps,
    'knobs': _knobs,
    'narration': _narration,
    'bots': _bots,
    'table': _table,
    'tablegallery': _tablegallery,
    'tablestate': _tablestate,
    'tablemsgs': _tablemsgs,
    'tableresults': _tableresults,
    'tablesashiuma': _tablesashiuma,
    'tableflow': _tableflow,
    'tableinbound': _tableinbound,
    'manager': _manager,
    'selftest': _selftest,
}


class _Facade(types.ModuleType):
    """`jangame.<name>` reads and writes go to the owning jantable module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module 'jangame' has no attribute {name!r}")
        return getattr(_MODULES[mod], name)

    def __setattr__(self, name, value):
        mod = _OWNERS.get(name)
        if mod is None:
            super().__setattr__(name, value)
            return
        setattr(_MODULES[mod], name, value)
        if mod == "deps":
            # an imported name is a copy in every module that imported it;
            # a patch has to reach each copy
            for other in _MODULES.values():
                if other is not _MODULES["deps"] and hasattr(other, name):
                    setattr(other, name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNERS))


sys.modules[__name__].__class__ = _Facade

if __name__ == "__main__":
    raise SystemExit(_selftest.main())
