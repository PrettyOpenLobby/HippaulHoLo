#!/usr/bin/env python3
"""Janhourou (PlayOnline content id 3) world-server -- 雀鳳楼.

STATUS: the message set, the record layout and the text framing are DECODED --
read out of the decrypted `JanHouRou.pex`, not guessed from captures (the
addresses are in `janwire.py`). What is NOT yet decoded is how a finished line reaches
the socket: the game hands it to the `sqMg*` gateway with a destination that is
either a channel name or a member id, and `sqMgJoinChannel` /
"Join Table Channel Name = %s" say it joins one IRC channel per table. So the
outermost carriage is very likely a PRIVMSG-shaped envelope we have not read.

This module is therefore two things at once, in the shape of tetramaster.py:

  * a real, testable implementation of everything that IS known -- the record,
    both framings, the opcode names, the addressing model, and the one exchange
    that is specified end to end (MjTGMPING -> MjTGMPONG);
  * a CAPTURE HARNESS for the rest. Anything we cannot yet speak is logged with
    its decoded header and a hexdump rather than answered, so the first real
    client contact tells us what the envelope is instead of being swallowed.

    python janhourou.py --selftest        # loopback: ping in, pong out
    python janhourou.py --serve           # listen (POL_JAN_PORT, default 51272)
    python janhourou.py --decode 'B@...'  # one line -> named fields

The client dials **gi003.pol.com** ("gi" + content id 3; IP 61.195.49.200 is
baked into the module) and 51272 is the only POL-band port it references.
"""
# The code lives in the `janworld` package, one module per concern
# (janworld/__init__.py lists them). This module is the entry point and a
# compatibility facade: `import janhourou` still resolves every name, reading
# or writing, to the module that owns it, so the services, tools and tests
# written against the single-file layout keep working unchanged.
import os
import sys
import types  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ONE COPY OF THIS MODULE. `python janhourou.py` runs it as `__main__`; a later
# `import janhourou` (from the package's own selftest, say) must find this copy
# rather than load a second one.
if __name__ == "__main__":
    sys.modules.setdefault("janhourou", sys.modules[__name__])

from janworld import (  # noqa: E402
    deps as _deps,
    wirelog as _wirelog,
    opcodes as _opcodes,
    reservelimits as _reservelimits,
    opener as _opener,
    seating as _seating,
    notices as _notices,
    builds as _builds,
    pushqueue as _pushqueue,
    dispatch as _dispatch,
    social as _social,
    galley as _galley,
    server as _server,
    selftest as _selftest,
)

# Which janworld module owns each name that `janhourou.<name>` reaches. A new
# top-level name that code outside the package uses through janhourou gets a line.
_OWNERS = {
    'ACK_FOR': 'opcodes',
    'ACK_SUB': 'opcodes',
    'BANISH_TARGET_OFF': 'opcodes',
    'BOT_NAME': 'seating',
    'EmISEVENT': 'opener',
    'EmISEVENTACK': 'opener',
    'GALLERY_ENABLE': 'galley',
    'GALLEYREQ_NAME_LEN': 'galley',
    'GALLEYREQ_NAME_OFF': 'galley',
    'GALLEYREQ_PASSWORD_LEN': 'galley',
    'GALLEYREQ_PASSWORD_OFF': 'galley',
    'GALLEY_LEFT': 'galley',
    'GALLEY_LIMIT_DEFAULT': 'galley',
    'GALLEY_NO': 'galley',
    'GALLEY_NO_CHAT': 'galley',
    'GALLEY_REFUSED': 'galley',
    'GALLEY_YES': 'galley',
    'GAMES': 'dispatch',
    'GM_VERSION': 'opener',
    'GM_VERSION_2004': 'opener',
    'HEADER_LEN': 'notices',
    'HOST': 'server',
    'Handler': 'server',
    'INGAME_2004': 'builds',
    'ISEVENT_DRAIN_SUB': 'opener',
    'ISEVENT_ENABLE': 'opener',
    'ISEVENT_FLAG': 'opener',
    'ISEVENT_LEN': 'opener',
    'ISEVENT_SUB': 'opener',
    'IS_2004': 'builds',
    'LIVE_ROOMS': 'seating',
    'LNDV_BODY': 'opener',
    'LNDV_DRAIN_SUB': 'opener',
    'LNDV_DUAL': 'opener',
    'LNDV_ENABLE': 'opener',
    'LNDV_LEN': 'opener',
    'LNDV_LOBBY_DOMAIN': 'opener',
    'LNDV_POLID_LOBBY': 'opener',
    'LNDV_POLID_PROFILE': 'opener',
    'LNDV_POLID_RANK': 'opener',
    'LNDV_SUB': 'opener',
    'LOG_DIR': 'wirelog',
    'MEMBERLIST_ENABLE': 'notices',
    'MjCHATINFO': 'opcodes',
    'MjCHATMEMBERACK': 'opcodes',
    'MjCHATMEMBERADD': 'opcodes',
    'MjCHATMEMBERDEL': 'opcodes',
    'MjCHECKSAVEDATA': 'opener',
    'MjCHECKSAVEDATAACK': 'opener',
    'MjCHMASTER': 'opcodes',
    'MjENTERGAME': 'opcodes',
    'MjENTERGAMEACK': 'opcodes',
    'MjENTERROOM': 'opcodes',
    'MjGALLEYLEAVEREQ': 'opcodes',
    'MjGALLEYREQ': 'opcodes',
    'MjGAMESTART': 'opcodes',
    'MjGETLNDV': 'opener',
    'MjGETLNDVACK': 'opener',
    'MjLEAVECONTENTS': 'opcodes',
    'MjLEAVEGAME': 'opcodes',
    'MjLEAVEGAMEACK': 'opcodes',
    'MjLEAVEROOM': 'opcodes',
    'MjMASTERCMDACK': 'opcodes',
    'MjMEMBERBANISH': 'opcodes',
    'MjMEMBERLISTREQ': 'notices',
    'MjNOTICEBANISH': 'notices',
    'MjNOTICEMEMBER': 'notices',
    'MjNOTICESERVERQUIT': 'opcodes',
    'MjNOTICETIMEUPWARNING': 'opcodes',
    'MjPLAYCANCEL': 'opcodes',
    'MjPLAYREQ': 'opcodes',
    'MjRESERVEACK': 'opcodes',
    'MjTBLCONFALL': 'opcodes',
    'MjTBLCONFSTART': 'opcodes',
    'NAME_LEN': 'notices',
    'NAME_SLOT': 'notices',
    'NOTICES': 'opcodes',
    'NOTICE_BIT': 'notices',
    'OPCODES': 'opcodes',
    'PLAYREQ_FACE_OFF': 'opcodes',
    'PLAYREQ_VOICE_OFF': 'opcodes',
    'PORT': 'server',
    'PUSH_QUEUE_MAX': 'pushqueue',
    'RESERVE_LIMIT_LEVEL': 'reservelimits',
    'RESERVE_LIMIT_MONEY': 'reservelimits',
    'RESERVE_LIMIT_RESULT': 'reservelimits',
    'RESERVE_LIMIT_TITLE': 'reservelimits',
    'RESERVE_QUEUE': 'opcodes',
    'RESERVE_RESULT': 'opcodes',
    'RESERVE_RESULTS': 'opcodes',
    'RESERVE_TAG_OPCODES': 'opcodes',
    'RESULT_OK': 'notices',
    'SAVEDATA_DRAIN_SUB': 'opener',
    'SAVEDATA_ENABLE': 'opener',
    'SAVEDATA_OK': 'opener',
    'SAVEDATA_SUB': 'opener',
    'SUB_TO_QUEUE': 'opcodes',
    'Server': 'server',
    'TMCMD_RESULT': 'opcodes',
    'TMCMD_RESULTS': 'opcodes',
    '_NAME_CACHE': 'seating',
    '_NUL': 'notices',
    '_PUSH': 'pushqueue',
    '_PUSH_LOCK': 'pushqueue',
    '_chat_member': 'social',
    '_chat_member_ack': 'social',
    '_chat_table_of': 'social',
    '_cstr': 'galley',
    '_decline_master': 'social',
    '_env_int': 'opener',
    '_gallery_limit': 'galley',
    '_gallery_members': 'pushqueue',
    '_galley': 'galley',
    '_handle_line': 'dispatch',
    '_kick': 'social',
    '_lndv_channels_on': 'opener',
    '_lobby_seating': 'seating',
    '_master_seat_of': 'social',
    '_member_names': 'seating',
    '_peer_is_2004': 'builds',
    '_relay_chat': 'social',
    '_reserve_limits_on': 'reservelimits',
    '_seat_summary': 'notices',
    '_stamp': 'wirelog',
    '_store_rules': 'social',
    '_table_for': 'seating',
    '_table_members': 'pushqueue',
    '_table_scores_names': 'builds',
    '_take_pushed': 'pushqueue',
    '_tl': 'seating',
    'accounts': 'deps',
    'ack_for': 'opcodes',
    'argparse': 'deps',
    'banish_notice': 'notices',
    'broadcast_server_quit': 'social',
    'checksavedata_ack': 'opener',
    'describe': 'wirelog',
    'display_name': 'seating',
    'emisevent_ack': 'opener',
    'getlndv_ack': 'opener',
    'handle_line': 'dispatch',
    'hexdump': 'wirelog',
    'janevent': 'deps',
    'jangame': 'deps',
    'janlobby': 'deps',
    'janmsgs': 'deps',
    'janmsgs2004': 'deps',
    'janrules': 'deps',
    'janseats': 'deps',
    'janwire': 'deps',
    'lndv_channel_ids': 'opener',
    'lobby_notice': 'social',
    'log': 'wirelog',
    'main': 'selftest',
    'member_list_notice': 'notices',
    'member_room': 'seating',
    'notify_master_changes': 'social',
    'opname': 'opcodes',
    'os': 'deps',
    'push_line': 'pushqueue',
    'push_record': 'pushqueue',
    'push_table': 'pushqueue',
    'pushed_count': 'pushqueue',
    'queue_for_sub': 'opcodes',
    'reserve_limits_unmet': 'reservelimits',
    'result_name': 'opcodes',
    'selftest': 'selftest',
    'serve': 'server',
    'serverquit_notice': 'notices',
    'socket': 'deps',
    'socketserver': 'deps',
    'struct': 'deps',
    'sys': 'deps',
    'take_pending': 'dispatch',
    'threading': 'deps',
    'timeup_notice': 'notices',
    'to_peer_build': 'builds',
    'warn_expiring_seats': 'social',
    'web_watchable': 'galley',
}
_MODULES = {
    'deps': _deps,
    'wirelog': _wirelog,
    'opcodes': _opcodes,
    'reservelimits': _reservelimits,
    'opener': _opener,
    'seating': _seating,
    'notices': _notices,
    'builds': _builds,
    'pushqueue': _pushqueue,
    'dispatch': _dispatch,
    'social': _social,
    'galley': _galley,
    'server': _server,
    'selftest': _selftest,
}


class _Facade(types.ModuleType):
    """`janhourou.<name>` reads and writes go to the owning janworld module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module 'janhourou' has no attribute {name!r}")
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
