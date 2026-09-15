#!/usr/bin/env python3
"""Build Janhourou's `b/g/MJSTableInfoSub` -- the 33 mahjong rule settings.

WHY THE SCREEN IS BROKEN WITHOUT IT. Confirmed live 2026-08-16: served as 820
zero bytes, the rules dialog (麻雀ルール設定) draws **ten identical rows reading
配給原点** with an empty value column and a smear of overlapping text. Both
symptoms fall straight out of the parser.

THE READER is `ruleset__003473b0`, and it is a fixed 33-record loop -- no count
field. Records are **12 bytes each, starting at +0x20** (`pbVar5 = param_2 +
0x20`, `pbVar5 += 0xc`, `while (iVar3 < 0x21)`); the fetch is
`lfile__002ae000(obj, key, "b/g/MJSTableInfoSub", 0x330)` = 816 bytes, so the
tail past 0x20 + 33*12 = 428 is unread.

    +0x00  u8   FLAGS        -> ctx+0x440   (a bitfield, NOT a group id)
    +0x01  u8   CONFIG INDEX -> ctx+0x444, and it picks the row's LABEL
    +0x02  u8   CURRENT value
    +0x03  u8   DEFAULT value
    +0x04  u32  legal-value mask, bits 0..31
    +0x08  u32  legal-value mask, bits 32..63

**`SelectItems = popcount(mask_lo) + popcount(mask_hi)`**, and the selectable
values ARE the set bit positions, written out as bytes at `ctx+0x44c`. `+0x02`
and `+0x03` are matched against those positions to find which item is current
and which is default. So an all-zero mask means a setting with nothing to pick,
which is what the live screen showed.

`+0x00` IS A BITFIELD. Read in full off `ruleset__00347750`, the row builder:

    & 0x03   WHICH DIALOG: 0 = the first, non-zero = the second. A record whose
             low two bits are set is skipped by this builder entirely.
    & 0x30   THE ROW KIND, and there are THREE live branches, not two:
                0x00        a NORMAL row -- calls ruleset__003476a0, fills the
                            item list / current / default, and advances the row
                            cursor by ONE
                0x10        a PLAIN row -- writes ctx+0xf9c = 0 and does NOT
                            advance the cursor, so plain rows all land on the
                            same slot and the screen ends up empty
                0x20 / 0x30 a PAGE-BREAK row -- identical to 0x00 except that
                            after incrementing it ROUNDS THE CURSOR UP TO THE
                            NEXT MULTIPLE OF 10, i.e. it starts a new page
    (0x80 is compared against `& 0x30`, which can never equal it -- dead code.)

WARNING: **CORRECTION, 2026-08-17: kind 0x00 IS the populating kind, and it always
was.** The note this replaced said "a zero here matches no kind branch at all"
and "there is no way to populate this screen without the kind that crashes".
Both are wrong. `ruleset__00347750`'s final `else` arm handles `uVar3 == 0` and
does everything the selector arm does bar the page break. The live 2026-08-16
result under flags `0x00` -- **4 tabs, 10 rows each** -- is 33 rows paginated
ten to a tab, which is our data drawn correctly. They all read 配給原点 because
that blob wrote **config index 0 in every record**, so every row asked for
index 0's label. That was a data bug, not a kind bug, and it is already fixed
(`buf[o + F_INDEX] = i`).

WARNING: **Config indices 0x1A and 0x1B are special-cased** to `SelectItems = 1` with
the mask ignored (`ruleset__003473b0`'s first arm) -- they are `LIMIT_MONEY` and
`LIMIT_LEVEL`, which fits a free-entry value rather than a pick list.

WARNING: **A QUIRK IN SE'S OWN LOOP, not ours.** The high-word loop stores the item
value as `(char)bit` (0..31) while comparing `bit + 0x20` against current and
default. So a value above 31 selects correctly but stores its low 5 bits. Do not
"fix" this by biasing our data -- match what the client does, and prefer masks
that live in the low word.

THE 33 SETTINGS name themselves in the module (`MJS_CONFIG_IDX_*` at
0x00421350..0x00421770) and there are exactly 33 of them, so record *i* carries
config index *i* in that order. That is inference from the count matching, but
the live screen agrees: every record carrying index 0 drew every row as 配給原点,
index 0's own label.

WARNING: **WHAT EACH VALUE MEANS IS NOT ESTABLISHED.** The bit position is the value;
what the client PRINTS for it comes from elsewhere and has not been read. That
is what `--probe` is for: it opens every mask to the full 64 values, so the
screen itself enumerates each setting's vocabulary. 64 items is safe -- items
are bytes from `ctx+0x44c` and the per-row stride is 0x58 = 88.

    python tools/jantableinfo.py --out data/resources/8.b_g_MJSTableInfoSub.bin
    python tools/jantableinfo.py --probe --out ...     # learn the value labels
    python tools/jantableinfo.py --dump  data/resources/8.b_g_MJSTableInfoSub.bin
"""
import argparse
import struct

TOTAL = 816             # lfile__002ae000(..., 0x330)
REC_OFF = 0x20          # pbVar5 = param_2 + 0x20
REC = 12                # pbVar5 += 0xc
COUNT = 33              # while (iVar3 < 0x21)  -- FIXED, there is no count field
ROW_SLOTS = 40          # ruleset__003473b0 mallocs 0x28 of them, stride 0x80,
                        # at ctx+0xfbc and ctx+0x23c4. The row cursor at
                        # ctx+0x2398 indexes both and is NOT bounds-checked.

F_FLAGS = 0x00
F_INDEX = 0x01
F_CURRENT = 0x02
F_DEFAULT = 0x03
F_MASK_LO = 0x04
F_MASK_HI = 0x08

#: +0x00 bitfield values, from ruleset__00347750's three kind arms.
FLAG_DIALOG_B = 0x01        # & 3 != 0 -> the second dialog
FLAG_ROW_NORMAL = 0x00      # & 0x30 == 0    -> fills the row, cursor += 1
FLAG_ROW_PLAIN = 0x10       # & 0x30 == 0x10 -> no item list, cursor UNCHANGED
FLAG_SELECTOR = 0x20        # & 0x30 == 0x20/0x30 -> fills the row, then starts
                            #   a NEW PAGE: cursor = next multiple of 10.
                            #   At most SELECTOR_MAX of these fit in 40 slots.
#: Special-cased by the reader to a single item, mask ignored.
SINGLE_ITEM = (0x1A, 0x1B)

#: The module's own names, in module order -- so the list index IS the config
#: index. 33 names, 33 records; that is the correspondence.
NAMES = [
    "GENTEN", "RENCHAN", "DORA", "AKA_NUM1", "AKA_NUM2", "AKA_NUM3", "AKA_KIND",
    "DOUBLE_RON", "WAREME", "HAKO", "KIRIAGE", "YAKITORI", "IPPATSU", "KUITAN",
    "NOTEN", "PINZUMO", "KYOKUSU", "UMA", "REACH_AFTER_KAN", "TABLE_COMMENT",
    "GALLEY_LIMIT", "WAIT_TIME", "DISCONNECT_MODE", "DISCONNECT_ADJUST",
    "DISCONNECT_PENALTY", "LIMIT_PASS_WORD", "LIMIT_MONEY", "LIMIT_LEVEL",
    "LIMIT_SYOGO1", "LIMIT_SYOGO2", "LIMIT_SYOGO3", "LIMIT_SYOGO4",
    "LIMIT_SYOGO5",
]
assert len(NAMES) == COUNT

#: TWO values for every setting, deliberately. How many the client actually has
#: labels for is NOT measured, and offering more than it has is how you get the
#: out-of-range value text this screen was already showing ("GroupCell", a widget
#: class name, printed live 2026-08-16 when the item list was empty). Every
#: mahjong rule has at least an on and an off, so bits 0 and 1 are the one
#: choice that is safe without measuring. Widen with --choices once the value
#: labels are known, or use --probe to LEARN them.
#: HOW MANY VALUES EACH SETTING REALLY HAS -- measured, not guessed, out of the
#: table at JanHouRou.pex 0x0040be50: 34 entries of {?, config index, label array
#: pointer}, and each array's length is the gap to the next pointer. This is the
#: value vocabulary `--probe` was invented to discover, so the probe is now both
#: unnecessary and unsafe: ruleset__003476a0 indexes the array by the item value
#: with NO bounds check (0x00347700 `lb`, 0x0034770c `addu`), so offering 64
#: values against an array of 2 reads 62 pointers past the end of it.
LABEL_COUNTS = {
    "GENTEN": 8, "RENCHAN": 8, "DORA": 8, "AKA_NUM1": 8, "AKA_NUM2": 8,
    "AKA_NUM3": 8, "AKA_KIND": 10, "DOUBLE_RON": 2, "WAREME": 2, "HAKO": 2,
    "KIRIAGE": 4, "YAKITORI": 4, "IPPATSU": 2, "KUITAN": 2, "NOTEN": 12,
    "PINZUMO": 4, "KYOKUSU": 8, "UMA": 6, "REACH_AFTER_KAN": 4,
    "TABLE_COMMENT": 8, "GALLEY_LIMIT": 4, "WAIT_TIME": 6,
    "DISCONNECT_MODE": 6, "DISCONNECT_ADJUST": 2, "DISCONNECT_PENALTY": 4,
    "LIMIT_PASS_WORD": 2, "LIMIT_MONEY": 2, "LIMIT_LEVEL": 2,
    "LIMIT_SYOGO1": 2, "LIMIT_SYOGO2": 2, "LIMIT_SYOGO3": 2, "LIMIT_SYOGO4": 2,
    "LIMIT_SYOGO5": 2,          # last array -- no next pointer, so 2 is a FLOOR
}

#: WHICH DIALOG EACH SETTING BELONGS TO. `ruleset__003473b0` calls TWO builders
#: back to back and they split the same 33 records on `flags & 3`:
#:
#:   ruleset__00347750   (flags & 3) == 0   rows -> ctx+0xfbc,  cursor ctx+0x2398
#:   ruleset__00347a70   (flags & 3) != 0   rows -> ctx+0x23c4, cursor ctx+0x37a0
#:
#: Both hold 40 slots at stride 0x80 and both have the same three kind arms and
#: the same page-break rounding, so everything in the SELECTOR_MAX note applies
#: to each independently.
#:
#: Confirmed live 2026-08-17: with all 33 records at `flags & 3 == 0`, the first
#: dialog (麻雀ルール設定) drew 4 tabs of real rows and the second
#: (テーブルルール設定) drew a single BLANK tab -- it had no records at all.
#:
#: WARNING: WHICH settings go in dialog B is INFERENCE, from SE's own names: 0..18 are
#: mahjong rules (starting points, dora, red tiles, yaku, uma) and 19..32 are
#: table settings (comment, gallery limit, wait time, disconnect handling and
#: the LIMIT_* entry restrictions). That reading fits the two dialog titles
#: exactly, but nothing in the module states the mapping. If a row shows up on
#: the wrong page, this list is the thing to move it in.
DIALOG_B = {
    "TABLE_COMMENT", "GALLEY_LIMIT", "WAIT_TIME",
    "DISCONNECT_MODE", "DISCONNECT_ADJUST", "DISCONNECT_PENALTY",
    "LIMIT_PASS_WORD", "LIMIT_MONEY", "LIMIT_LEVEL",
    "LIMIT_SYOGO1", "LIMIT_SYOGO2", "LIMIT_SYOGO3", "LIMIT_SYOGO4",
    "LIMIT_SYOGO5",
}

DEFAULT_CHOICES = 2

#: OK: **THE 2026-08-16 CRASH IS EXPLAINED -- statically, no bisect needed.**
#: Serving all 33 records with FLAG_SELECTOR killed the console with `Jump to
#: unaligned address (PC: 0x491f1222)`. The mechanism is the page-break arm of
#: `ruleset__00347750`, confirmed in MIPS at 0x003479b8:
#:
#:     lw v1, 0x2398(s2)      ; the row cursor
#:     addiu a1, v1, 1        ; +1
#:     sw a1, 0x2398(s2)
#:     div a1, 10 ; mfhi      ; remainder
#:     beq  -> done if 0
#:     ...(0x66666667 magic divide)... sll/addu/sll = *10
#:     sw a0, 0x2398(s2)      ; cursor = ((cursor+1)/10 + 1) * 10
#:
#: **So each 0x20/0x30 row consumes a whole 10-slot page.** The row arrays are
#: 40 entries (`iVar3 < 0x28`, stride 0x80, at ctx+0xfbc and ctx+0x23c4), so the
#: cursor walks 0, 10, 20, 30, then **40 -- off the end on the FIFTH row**.
#: `ruleset__003476a0` then takes its output pointer from
#: `*(u32 *)(ctx + cursor*0x80 + 0xfbc)`, which past slot 39 is whatever memory
#: follows, and writes `SelectItems` u32s through it. 33 selector records demand
#: 330 slots against 40; the wild write corrupts a vtable and the next virtual
#: call lands nowhere. That is the unaligned jump.
#:
#: WARNING: **THE CEILING IS FOUR.** `SELECTOR_MAX` below is enforced, not advisory.
#:
#: The default kind is now `FLAG_ROW_NORMAL` (0x00) -- the arm that fills the
#: item list AND advances by one, so all 33 settings fit in 40 slots and
#: paginate ten to a tab. `--kind` opts into the others.
SELECTOR_MAX = 4

DEFAULT_KIND = FLAG_ROW_NORMAL


def build(probe=False, choices=DEFAULT_CHOICES, kind=None, selector_only=None,
          one_dialog=False, values=None):
    """The 816-byte blob. `probe` opens every mask to all 64 values so the
    screen enumerates each setting's own value labels.

    `values` is {NAME: index} overriding the served CURRENT value, which is 0
    for everything otherwise.

    WARNING: WHY THAT MATTERS, and it is not cosmetic: **WAIT_TIME at value 0 makes the
    game unplayable.** Measured live 2026-08-17 -- the first hand ever dealt by
    this server advanced on its own, one discard every ~2 s, with the pad
    untouched. The client's `LimitTimeManager` thread counts `DAT_00445b87 * 60`
    frames and then sets the skip flag; `mahdisp__002c2ac0` polls it at the top
    of the discard picker and returns its default slot immediately, which is why
    every MjSUTE carried the word 0x0d. So the turn clock the player gets is a
    number WE serve, and serving index 0 gives them no time at all.

    WARNING: Which of WAIT_TIME's 6 label values is the longest is NOT measured -- the
    labels are Japanese strings in the client's own array and we have never read
    them. Index 5 is the far end of the range, which is the reason to try it
    first, not evidence that it is 60 seconds.
    """
    values = values or {}
    buf = bytearray(TOTAL)
    for i, name in enumerate(NAMES):
        o = REC_OFF + i * REC
        # Never offer more values than the setting has labels for.
        n = min(64 if probe else choices, LABEL_COUNTS.get(name, 2))
        # The values are the SET BIT POSITIONS, so n choices = the low n bits.
        # Keep them in the low word where we can: SE's high-word loop stores a
        # value's low 5 bits (see the header), so a mask that stays under 32 is
        # the one shape with no quirk in it.
        mask = (1 << n) - 1
        if selector_only is not None:
            # One record starts a new page, the rest are ordinary rows. This was
            # written as a crash bisect; the crash is explained now (see
            # SELECTOR_MAX), so it is just a way to lay the screen out.
            flags = FLAG_SELECTOR if i == selector_only else FLAG_ROW_NORMAL
        else:
            flags = DEFAULT_KIND if kind is None else kind
        if not one_dialog and name in DIALOG_B:
            flags |= FLAG_DIALOG_B
        buf[o + F_FLAGS] = flags
        buf[o + F_INDEX] = i
        # Clamped to what the setting actually has labels for: ruleset__003476a0
        # indexes the label array by this value with NO bounds check.
        cur = min(values.get(name, 0), LABEL_COUNTS.get(name, 2) - 1)
        # The item list must CONTAIN the current value: the reader matches
        # `current` against the item positions to decide which row is selected,
        # so a current of 5 inside a 2-item mask selects nothing. Widen this
        # setting's mask far enough to hold it (still clamped to its real label
        # count by `n` above).
        if cur >= n:
            n = min(cur + 1, LABEL_COUNTS.get(name, 2))
            mask = (1 << n) - 1
        buf[o + F_CURRENT] = cur
        buf[o + F_DEFAULT] = cur
        struct.pack_into("<I", buf, o + F_MASK_LO, mask & 0xFFFFFFFF)
        struct.pack_into("<I", buf, o + F_MASK_HI, (mask >> 32) & 0xFFFFFFFF)
    _check_cursor(buf)
    assert len(buf) == TOTAL
    return bytes(buf)


def _check_cursor(buf):
    """Walk ruleset__00347750's row cursor over the blob and refuse to emit one
    that overruns the 40-slot row array. This is the 2026-08-16 crash; see the
    SELECTOR_MAX note above."""
    cursor = {0: 0, 1: 0}               # dialog A and dialog B, independent
    for i in range(COUNT):
        o = REC_OFF + i * REC
        flags = buf[o + F_FLAGS]
        dlg = 1 if flags & 0x03 else 0  # which builder claims this record
        kind = flags & 0x30
        if kind == 0x10:                # plain: no cursor movement
            continue
        if cursor[dlg] >= ROW_SLOTS:
            raise ValueError(
                "record %d (%s) puts dialog %s's row cursor at %d, and "
                "the row array holds %d. Past the end it reads its output "
                "pointer from out of bounds and writes through it -- that is "
                "the `Jump to unaligned address` crash.\n"
                "A page-break (0x20/0x30) row costs the REST OF ITS 10-SLOT "
                "PAGE, not one slot, so the budget is not a simple count: %d "
                "page-breaks and nothing else is the maximum, and every normal "
                "row you mix in eats the same pages. Use fewer page-breaks."
                % (i, NAMES[i], "AB"[dlg], cursor[dlg], ROW_SLOTS, SELECTOR_MAX))
        cursor[dlg] += 1
        if kind in (0x20, 0x30) and cursor[dlg] % 10:
            cursor[dlg] = (cursor[dlg] // 10 + 1) * 10


def dump(blob):
    print("total %d B, %d records of %d at +0x%02X (fixed -- no count field)"
          % (len(blob), COUNT, REC, REC_OFF))
    dead = 0
    cursor = {0: 0, 1: 0}   # the two builders' row cursors (ctx+0x2398, ctx+0x37a0)
    for i in range(COUNT):
        o = REC_OFF + i * REC
        idx = blob[o + F_INDEX]
        lo = struct.unpack_from("<I", blob, o + F_MASK_LO)[0]
        hi = struct.unpack_from("<I", blob, o + F_MASK_HI)[0]
        items = bin(lo).count("1") + bin(hi).count("1")
        if idx in SINGLE_ITEM:
            items, note = 1, "  (reader forces 1 item for 0x%02X)" % idx
        else:
            note = "" if items else "   <-- NOTHING SELECTABLE"
        dead += not items and idx not in SINGLE_ITEM
        flags = blob[o + F_FLAGS]
        kind = flags & 0x30
        slot = ""
        dlg = 1 if flags & 0x03 else 0
        if kind == 0x10:
            note += "   <-- plain: the row cursor does not advance, so this " \
                    "draws nothing and the next row overwrites its slot"
        else:
            if cursor[dlg] >= ROW_SLOTS:
                note += "   <-- WARNING: CURSOR %d IS PAST THE %d-SLOT ARRAY: wild " \
                        "write, this is the crash" % (cursor[dlg], ROW_SLOTS)
            slot = " slot=%s%d" % ("AB"[dlg], cursor[dlg])
            cursor[dlg] += 1
            if kind in (0x20, 0x30) and cursor[dlg] % 10:
                cursor[dlg] = (cursor[dlg] // 10 + 1) * 10
        print("  [%2d] index=%2d %-19s flags=%#04x(dlg %s,%s)%s cur=%d def=%d "
              "mask=%08x%08x items=%d%s"
              % (i, idx, NAMES[idx] if idx < COUNT else "??", flags,
                 "B" if flags & 3 else "A",
                 {0x00: "normal", 0x10: "plain",
                  0x20: "page-break", 0x30: "page-break"}[kind],
                 slot, blob[o + F_CURRENT], blob[o + F_DEFAULT],
                 hi, lo, items, note))
    if dead:
        print("*** %d setting(s) have an empty mask -- those rows draw with no "
              "value, which is what the zero blob did to all 33. ***" % dead)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    ap.add_argument("--set", action="append", metavar="NAME=IDX", default=[],
                    help="override a setting's served value, e.g. WAIT_TIME=5. "
                         "Clamped to that setting's measured label count.")
    ap.add_argument("--choices", type=int, default=DEFAULT_CHOICES,
                    help="values to offer per setting (default 2)")
    ap.add_argument("--one-dialog", action="store_true",
                    help="put every record in dialog A, as before 2026-08-17. "
                         "Leaves the second dialog blank")
    ap.add_argument("--plain", action="store_true",
                    help="use FLAG_ROW_PLAIN (0x10) for every record, which is "
                         "what this tool emitted before 2026-08-17. Draws no "
                         "rows, but it is the pre-change baseline")
    ap.add_argument("--selector", action="store_true",
                    help="use FLAG_SELECTOR (0x20) instead of the plain kind. "
                         "WARNING: THIS CRASHED A LIVE CLIENT -- see the note above")
    ap.add_argument("--selector-only", type=int, metavar="IDX",
                    help="bisect: give ONLY record IDX the selector kind, the "
                         "rest plain. Expect a crash or one drawn row; both "
                         "are answers")
    ap.add_argument("--probe", action="store_true",
                    help="WARNING: LARGELY OBSOLETE and unsafe -- LABEL_COUNTS now "
                         "carries the real per-setting counts, and values are "
                         "clamped to them, so this no longer opens all 64")
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        kind = None
        if a.selector:
            kind = FLAG_SELECTOR
        elif a.plain:
            kind = FLAG_ROW_PLAIN
        values = {}
        for spec in a.set:
            name, _, idx = spec.partition("=")
            name = name.strip().upper()
            if name not in NAMES:
                raise SystemExit("--set: %r is not a setting. One of: %s"
                                 % (name, ", ".join(NAMES)))
            values[name] = int(idx, 0)
        blob = build(a.probe, a.choices, kind,
                     getattr(a, 'selector_only', None), a.one_dialog,
                     values=values)
        dump(blob)
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)%s"
                  % (a.out, len(blob), "  [PROBE]" if a.probe else ""))
