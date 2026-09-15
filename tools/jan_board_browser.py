#!/usr/bin/env python3
"""jan_board_browser.py -- drive the Jan rankings page in a real headless
Chrome (render-web-ui-before-shipping), over a temporary 12-player board.

    python tools/jan_board_browser.py [--shots DIR]

Fails on any JavaScript error, on art or a ROM web font that never loads, on
text that runs into its neighbour, or on a control that does not do what the
game's does. Needs Chrome/Edge and websocket-client (see fe_panel_browser.py).
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

from fe_panel_browser import Browser, free_port   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="jan-board-shots-"))
    o = ap.parse_args(argv)
    os.makedirs(o.shots, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="jan-board-browser-")
    res = os.path.join(tmp, "resources")
    os.makedirs(res)
    os.environ["POL_RESOURCE_DIR"] = res
    db = os.path.join(tmp, "accounts.db")
    os.environ["POL_ACCOUNTS_DB"] = db
    names = ["Seiryu", "Byakko", "Sennin", "Square", "Brandle", "Genbu",
             "Hikari", "pelix", "Kodai", "Suzaku", "Lex", "Quinn"]
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE handle (id INTEGER PRIMARY KEY, member_id INTEGER, "
              "handle_name TEXT, is_primary INTEGER)")
    c.executemany("INSERT INTO handle (member_id, handle_name, is_primary) VALUES (?,?,1)",
                  [(i + 1, n) for i, n in enumerate(names)])
    c.commit()
    c.close()
    for i in range(len(names)):
        with open(os.path.join(res, "%d.jan_stats.json" % (i + 1)), "w") as fh:
            json.dump({"games_played": 3 + i, "places": [i % 3, 1, 1, 1],
                       "result_x10": 900 - 130 * i, "result_x10_week": 50 * i,
                       "history": [{"place": i % 4, "result_x10": 100 - 37 * i,
                                    "score": 25000, "t": int(time.time()) - 600 * i,
                                    "table": 1}]}, fh)
    # a watchable live table, for the watching page (written as jangame does)
    os.environ["POL_DATA_DIR"] = tmp
    watch_id = str((2 << 16) | 1)
    p0 = [{"tile": "9m", "called": False, "riichi": False},
          {"tile": "1s", "called": False, "riichi": True},
          {"tile": "2z", "called": True, "riichi": False},
          {"tile": "4z", "called": False, "riichi": False}]
    watch_state = {
        "state": "playing", "room": 201, "table": 1,
        "seats": [
            {"name": "Seiryu", "bot": False, "score": 24000, "riichi": True, "drawn": "0p",
             "hand": ["1m", "2m", "3m", "4p", "6p", "7s", "8s", "9s", "1z", "1z", "5z", "5z",
                      "6p", "0p"], "pond": p0, "melds": []},
            {"name": "Byakko", "bot": False, "score": 26000, "riichi": False, "drawn": None,
             "hand": ["2m", "3m", "4m", "5s", "6s", "7s", "8p", "8p", "3z", "3z"],
             "pond": [{"tile": "9p", "called": False, "riichi": False},
                      {"tile": "1m", "called": False, "riichi": False}],
             "melds": [{"kind": "pon", "tiles": ["2z", "2z", "2z"], "from": 0, "called": "2z"}]},
            {"name": "Sennin", "bot": False, "score": 25000, "riichi": False, "drawn": None,
             "hand": ["1p", "1p", "2p", "3p", "5m", "6m", "7m", "9s", "9s", "7z"],
             "pond": [{"tile": "6z", "called": False, "riichi": False}],
             "melds": [{"kind": "chi", "tiles": ["3s", "4s", "5s"], "from": 1, "called": "4s"}]},
            {"name": "COM 4", "bot": True, "score": 24000, "riichi": False, "drawn": None,
             "hand": ["1s", "2s", "3s", "4m", "5m", "6m", "7p", "8p", "9p", "4z", "4z", "3p", "3p"],
             "pond": [{"tile": "8m", "called": False, "riichi": False},
                      {"tile": "7m", "called": False, "riichi": False},
                      {"tile": "5z", "called": False, "riichi": False}], "melds": []}],
        "round": {"wind": "E", "number": 2, "dealer": 1, "honba": 1, "sticks": 1},
        "dora": ["3p"], "wall": 52, "turn": 0, "last": {"tile": "5z", "seat": 3}, "result": None}
    # what the page must draw: every hand tile, every meld tile, the pond
    # minus the tiles someone called; sideways = riichi tiles + called meld tiles
    expect_tiles = sum(len(s["hand"]) + sum(len(m["tiles"]) for m in s["melds"])
                       + sum(1 for p in s["pond"] if not p["called"]) for s in watch_state["seats"])
    expect_side = sum(sum(1 for p in s["pond"] if p["riichi"] and not p["called"])
                      + sum(1 for m in s["melds"] if m["from"] >= 0 and m["called"])
                      for s in watch_state["seats"])

    def write_watch_file():
        with open(os.path.join(tmp, "jan-tables-live.json"), "w") as fh:
            json.dump({"stamp": time.time(),
                       "tables": {watch_id: {"watchable": True, "state": watch_state},
                                  "999": {"watchable": False}}}, fh)

    write_watch_file()
    import boardjan
    import polboards
    boardjan.WATCH = boardjan.Watch(delay=0)          # the test shows it at once
    boardjan.WATCH.sample()
    boardjan.WATCH.start(period=0.5)
    port = free_port()
    args = polboards.build_parser().parse_args(["--jan-port", str(port)])
    srv = polboards.serve(boardjan, args, port)
    fails = []

    def check(name, ok, detail=""):
        print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                                "  " + str(detail) if detail and not ok else ""))
        if not ok:
            fails.append(name)

    art_ok = ("[...document.querySelectorAll('#stage img')].every("
              "i => i.complete && i.naturalWidth > 0)")
    name0 = "E.rows[0].ChrName.textContent"
    # any two texts on the same row that touch (the old image text overlapped)
    overlap = """(() => {
      const bad = [];
      for (const row of E.rows){
        const r = Object.values(row).filter(e => e.textContent.trim())
          .map(e => [e.offsetLeft, e.offsetLeft + e.offsetWidth, e.textContent]).sort((a, b) => a[0] - b[0]);
        for (let i = 1; i < r.length; i++) if (r[i][0] < r[i - 1][1]) bad.push(r[i - 1][2] + '|' + r[i][2]);
      }
      const u = document.querySelector('#update'), hb = E.title.getBBox();
      if (u.textContent && u.offsetLeft < hb.x + hb.width && u.offsetLeft + u.offsetWidth > hb.x
          && u.offsetTop < hb.y + hb.height && u.offsetTop + u.offsetHeight > hb.y) bad.push('update|title');
      return bad; })()"""
    # the title sits between the L1 and R1 plates on every tab
    title_fits = """(() => { const b = E.title.getBBox(), sw = L.title.stroke_px;
      return b.width > 50 && b.x - sw >= 167 && b.x + b.width + sw <= 484 })()"""
    # every caption's line box inside the pill's body (sprite rows 6..26)
    captions_in = """[...document.querySelectorAll('.cat .t')].every(t =>
      t.offsetTop >= 6 && t.offsetTop + t.offsetHeight <= 27 && t.offsetWidth <= 138 - 54 + 0.5)"""
    # the corner pieces (the decoration) may lie over the sheet's margin, never
    # over a word, a button, a plate, the title or the sidebar's text; each
    # PIECE's tilted box is tested (a group's box would count the felt between
    # its pieces), which is still stricter than the ink
    on_text = """(() => {
      const P = [...document.querySelectorAll('#decor .dp')].map(c => c.getBoundingClientRect());
      const T = [...document.querySelectorAll('#stage .t, #stage .cat, #stage .plate, #recent li, #side-foot, #side svg text')]
        .filter(e => (!e.classList.contains('t') || e.textContent.trim()) && e.getBoundingClientRect().width > 0)
        .map(e => [e.getBoundingClientRect(), (e.textContent || e.className || '').trim().slice(0, 18)]);
      T.push([E.title.getBoundingClientRect(), 'title']);
      const bad = [];
      P.forEach((p, i) => T.forEach(([t, n]) => {
        if (p.left < t.right && p.right > t.left && p.top < t.bottom && p.bottom > t.top) bad.push(i + ':' + n); }));
      return bad; })()"""
    # the chocobo itself (its sprite, not the group) wholly inside the browser
    choco_in_view = """(() => { const r = document.querySelector('#decor .choco .dp').getBoundingClientRect();
      return r.width > 20 && r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight })()"""

    b = Browser(width=1400, height=1000, webgl=True)
    try:
        b.goto("http://127.0.0.1:%d/" % port, settle=3.0)
        check("the page has the board and its layout",
              b.js("S && L && S.categories.length") == 5)
        check("every art layer loaded", b.js(art_ok))
        check("the text face (M PLUS 1p, medium + bold) loaded",
              b.js("document.fonts.check('500 16px \"JanText\"') && "
                   "document.fonts.check('700 16px \"JanText\"')"))
        check("the row text is real text: row 1 is Seiryu",
              b.js(name0) == "Seiryu", b.js(name0))
        check("the window sits on the felt, as large as fits inside the margin",
              b.js("(() => { const r = stage.getBoundingClientRect();"
                   " return (Math.abs(r.width - (innerWidth - 2 * PAD)) < 2 || Math.abs(r.height - (innerHeight - 2 * PAD)) < 2)"
                   " && /radial-gradient/.test(getComputedStyle(document.body).backgroundImage) })()"))
        check("...with no sidebar when there is no room for one (1400x1000)", b.js("side.hidden"))
        check("the corner pieces lie over the sheet's margin -- over none of its words or buttons",
              b.js(on_text) == [], b.js(on_text))
        check("...and the chocobo stays in view even when the window fills the screen",
              b.js(choco_in_view), b.js("JSON.stringify(document.querySelector('#decor .choco .dp').getBoundingClientRect())"))
        check("the sheets have a deckled paper edge and a shadow that follows it",
              "mask_main.png" in (b.js("getComputedStyle(stage).webkitMaskImage || getComputedStyle(stage).maskImage") or "")
              and "mask_side.png" in (b.js("getComputedStyle(side).webkitMaskImage || getComputedStyle(side).maskImage") or "")
              and "drop-shadow" in b.js("getComputedStyle(wrap).filter"),
              [b.js("getComputedStyle(stage).webkitMaskImage"), b.js("getComputedStyle(wrap).filter")])
        check("the title has room between its letters",
              b.js("E.title.getAttribute('letter-spacing')") == "2")
        check("pressing a button moves its caption down with the pill (2 px, only while held)",
              b.js("[...document.styleSheets].flatMap(s => [...s.cssRules]).some(r =>"
                   " r.selectorText === '.cat:active .t' && /translateY\\(2px\\)/.test(r.style.transform))"))
        check("the live line is INSIDE the window",
              b.js("(() => { const s = document.querySelector('#status').getBoundingClientRect(),"
                   " r = stage.getBoundingClientRect(); return s.width > 0 && s.top >= r.top"
                   " && s.bottom <= r.bottom && /Page 1\\/2/.test(E.status.textContent) })()"),
              b.js("E.status.textContent"))
        check("no text runs into its neighbour", b.js(overlap) == [], b.js(overlap))
        # every header on its values' alignment, on all five tabs
        aligned = """(() => {
          const bad = [], T = L.fixups.table, keep = CAT;
          const edge = (e, s) => s === 'l' ? e.offsetLeft : s === 'r' ? e.offsetLeft + e.offsetWidth
                                 : e.offsetLeft + e.offsetWidth / 2;
          for (let cat = 0; cat < 5; cat++){
            setCat(cat);
            const rows = E.rows.filter(r => r.ChrName.textContent);
            const same = (h, vals, s, tag) => {
              if (!h.textContent) return;
              for (const v of vals) if (v.textContent && Math.abs(edge(h, s) - edge(v, s)) > 1.5){
                bad.push(cat + ':' + tag + ' ' + edge(h, s) + ' vs ' + edge(v, s)); break; } };
            same(E.hdr.ranking, rows.map(r => r._mark), 'l', 'rank');
            same(E.hdr.ChrName1, rows.map(r => r.ChrName), 'l', 'name');
            same(E.hdr.ChrLevel, rows.map(r => r.ChrLevel), 'c', 'lv');
            T.kinds[cat].forEach((kind, j) => {
              const col = j ? 'Str5' : 'Str4';
              if (kind === 'num') same(E.hdr[col], rows.map(r => r[col]), 'r', col);
              if (kind === 'text') same(E.hdr[col], rows.map(r => r[col]), 'l', col);
              if (kind === 'dec'){
                const p = rows.map(r => edge(r[col], 'r'));
                if (p.length && Math.max(...p) - Math.min(...p) > 1) bad.push(cat + ':' + col + ' decimal points');
              }
            });
          }
          setCat(keep);
          return bad; })()"""
        check("every column header lines up with its values, on all five tabs",
              b.js(aligned) == [], b.js(aligned))
        # reported 2026-09-13: the rating headers changed size from tab to tab
        hdr_sizes = """(() => { const keep = CAT, sizes = new Set();
          for (let cat = 0; cat < 5; cat++){
            setCat(cat);
            for (const e of Object.values(E.hdr)) if (e.textContent) sizes.add(e.style.fontSize);
          }
          setCat(keep); return [...sizes]; })()"""
        check("every header is ONE size, on all five tabs (no jump between tabs)",
              len(b.js(hdr_sizes)) == 1, b.js(hdr_sizes))
        check("the title is TEXT: 'JongHoLow Ranking' in the title face",
              b.js("E.title.textContent") == "JongHoLow Ranking"
              and b.js("document.fonts.check('30px \"JanTitle\"')"), b.js("E.title.textContent"))
        check("...between the L1 and R1 plates", b.js(title_fits),
              b.js("(() => { const b = E.title.getBBox(); return [b.x, b.width, E.title.getAttribute('font-size')] })()"))
        # reported 2026-09-13: the big title changed size with its length
        title_sizes = """(() => { const keep = CAT, s = new Set();
          for (let c = 0; c < 5; c++){ setCat(c); s.add(E.title.getAttribute('font-size')); }
          setCat(keep); return [...s]; })()"""
        check("...ONE size on all five tabs (the longest title's)",
              len(b.js(title_sizes)) == 1, b.js(title_sizes))
        check("the button captions sit inside the pill, one shared size, at most the game's 14",
              b.js(captions_in) and b.js(
                  "(() => { const s = [...document.querySelectorAll('.cat .t')].map(t => parseFloat(t.style.fontSize));"
                  " return s.every(v => v === s[0]) && s[0] <= 14 && s[0] >= 10 })()"),
              b.js("[...document.querySelectorAll('.cat .t')].map(t => [t.offsetTop, t.offsetHeight, t.offsetWidth])"))
        check("12 players = 2 pages in Jan Rating", b.js("S.categories[0].pages") == 2)
        check("the current category uses the game's flat highlight sprite (s3), not the pressed one",
              b.js("getComputedStyle(document.querySelector('.cat[data-cat=\"0\"]'))"
                   ".backgroundImage").endswith('btn138_s3.png")'),
              b.js("getComputedStyle(document.querySelector('.cat[data-cat=\"0\"]')).backgroundImage"))
        check("...and the others the normal one",
              b.js("getComputedStyle(document.querySelector('.cat[data-cat=\"2\"]'))"
                   ".backgroundImage").endswith('btn138_s0.png")'))
        b.screenshot(os.path.join(o.shots, "1-jan-rating.png"))
        b.click_el("#plate-R")
        b.pump(0.6)
        check("R1 pages forward", b.js("CAT") == 0 and b.js("PAGE") == 1
              and b.js(name0) == "Lex", (b.js("CAT"), b.js("PAGE"), b.js(name0)))
        b.screenshot(os.path.join(o.shots, "2-page2.png"))
        b.click_el("#plate-R")
        b.pump(0.6)
        check("R1 on the last page switches to the next tab (Titles)",
              b.js("CAT") == 1 and b.js("PAGE") == 0)
        b.click_el("#plate-L")
        b.pump(0.6)
        check("L1 on a tab's first page goes back to the previous tab's last page",
              b.js("CAT") == 0 and b.js("PAGE") == 1)
        b.click_el(".cat[data-cat='1']")
        b.pump(0.6)
        check("the Titles button switches category and resets the page",
              b.js("CAT") == 1 and b.js("PAGE") == 0
              and b.js("E.hdr.Str4.textContent") == "Title score")
        b.screenshot(os.path.join(o.shots, "3-titles.png"))
        b.key("3", "Digit3", 51)
        b.pump(0.6)
        check("key 3 opens Overall Gamble", b.js("CAT") == 2 and b.js(art_ok))
        check("...and its long title still fits between the plates", b.js(title_fits),
              b.js("(() => { const b = E.title.getBBox(); return [b.x, b.width, E.title.getAttribute('font-size')] })()"))
        headers_on_bar = ("Object.values(E.hdr).every(h => !h.textContent || "
                          "(h.offsetLeft >= 16 && h.offsetLeft + h.offsetWidth <= 624))")
        check("...and its long column header stays on the header bar", b.js(headers_on_bar),
              b.js("Object.values(E.hdr).map(h => [h.textContent, h.offsetLeft, h.offsetWidth])"))
        check("labels and rows are set in the text face",
              "JanText" in b.js("E.hdr.ChrName1.style.fontFamily")
              and "JanText" in b.js("document.querySelector('.cat .t').style.fontFamily")
              and "JanText" in b.js("E.rows[0].ChrName.style.fontFamily"))
        check("...with no overlapping text there either", b.js(overlap) == [], b.js(overlap))
        b.screenshot(os.path.join(o.shots, "4-overall.png"))
        check("the hidden table mirrors the screen for screen readers",
              b.js("document.querySelectorAll('#rows tr').length") == 10)
        b.call("Emulation.setDeviceMetricsOverride", width=1920, height=1080,
               deviceScaleFactor=1, mobile=False)
        b.goto("http://127.0.0.1:%d/" % port, settle=2.5)
        check("a 16:9 screen gets the sidebar beside the window",
              b.js("!side.hidden && side.getBoundingClientRect().left > stage.getBoundingClientRect().right"))
        check("...listing the newest games first, by name",
              b.js("document.querySelectorAll('#recent li').length") >= 4
              and b.js("document.querySelector('#recent li:not(.live) .nm').textContent") == "Seiryu",
              b.js("[...document.querySelectorAll('#recent .nm')].map(e => e.textContent)"))
        check("...none of them running into the footer",
              b.js("(() => { const l = document.querySelector('#recent li:last-child').getBoundingClientRect(),"
                   " f = E.sideFoot.getBoundingClientRect(); return l.bottom <= f.top })()"))
        check("...and the players on file", "12 players on file" in b.js("E.sideFoot.textContent"),
              b.js("E.sideFoot.textContent"))
        b.screenshot(os.path.join(o.shots, "6-wide.png"))

        # --- watching -----------------------------------------------------
        write_watch_file()
        check("the sidebar offers the watchable table (and only that one)",
              b.js("[...document.querySelectorAll('#recent li.live a')].map(a => a.getAttribute('href'))")
              == ["watch#" + watch_id],
              b.js("[...document.querySelectorAll('#recent li.live a')].map(a => a.getAttribute('href'))"))
        check("...and the status line says a table is live",
              "Watch live" in b.js("E.status.textContent"))
        b.goto("http://127.0.0.1:%d/watch#%s" % (port, watch_id), settle=4.0)
        check("the watching page is up in 3D: WebGL, the game's own pieces loaded",
              b.js("!!(window.__jan3d && __jan3d.ok && __jan3d.frames > 0)"),
              b.js("window.__jan3d && [__jan3d.ok, __jan3d.frames]"))
        n_up = expect_tiles + len(watch_state["dora"])
        check("...every hand, meld and pond tile, and the dora indicator: %d" % n_up,
              b.js("__jan3d.tiles()") == n_up, b.js("__jan3d.tiles()"))
        check("...a card per seat, named, with the riichi and the dealer marked",
              sorted(b.js("[...document.querySelectorAll('.seat .n')].map(e => e.textContent)"))
              == ["Byakko", "COM 4", "Seiryu", "Sennin"]
              and b.js("document.querySelectorAll('.seat .chip.riichi').length") == 1
              and b.js("document.querySelectorAll('.seat .chip.deal').length") == 1,
              b.js("[...document.querySelectorAll('.seat .n')].map(e => e.textContent)"))
        ids = b.js("[...document.querySelectorAll('.seat .pf')].map(e => +((/id=(\\d+)/.exec(e.style.backgroundImage) || [0, -1])[1]))")
        check("...portraits: the COM an ACKY Gallery face, each player theirs (or the blank)",
              len(ids) == 4 and sum(1 for i in ids if 1616 <= i <= 1631) == 1
              and ids.count(0) == 3, ids)
        check("...the round, the dora and how late the view is",
              "East 2" in b.js("document.querySelector('#round .rw').textContent")
              and b.js("document.querySelectorAll('#round .face').length") == 1
              and "behind the table" in b.js("document.querySelector('#lag').textContent"))
        b.screenshot(os.path.join(o.shots, "7-watch.png"))
        x, y = b.js("__jan3d.tileAt(0)")
        # a plain hover (Browser.mouse always says button "left", which Chrome
        # reports as a held button -- a drag, not a hover)
        b.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none", buttons=0)
        b.pump(0.5)
        tip = b.js("document.querySelector('#tip').hidden ? '' : document.querySelector('#tip').textContent")
        check("hovering a tile names it", any(w in tip for w in (" of ", "wind", "dragon")), tip)
        b.key("2", "Digit2", 50)
        b.pump(1.3)
        check("pressing 2 zooms in on the next seat",
              b.js("__jan3d.focus()") == 1
              and "focus" in b.js("document.querySelectorAll('.seat')[1].className"))
        b.key("Escape", "Escape", 27)
        b.pump(1.2)
        check("...and Esc puts the whole table back", b.js("__jan3d.focus()") is None)
        # a win: the game's results screen, the points rolling to the new scores
        won = copy.deepcopy(watch_state)
        won["result"] = {"kind": "ron", "wins": [{"seat": 1, "han": 3, "points": 3900, "limit": None,
                                                  "yaku": [["riichi", 1], ["dora", 1]]}]}
        won["seats"][1]["score"] += 3900
        won["seats"][3]["score"] -= 3900
        with open(os.path.join(tmp, "jan-tables-live.json"), "w") as fh:
            json.dump({"stamp": time.time(), "tables": {watch_id: {"watchable": True, "state": won}}}, fh)
        b.pump(7.0)
        nows = b.js("[...document.querySelectorAll('#results .pl')].map(e => [e.querySelector('.n').textContent, e.querySelector('.now').textContent])")
        want = {s["name"]: "{:,}".format(s["score"]) for s in won["seats"]}
        check("a win opens the game's results screen: four players, their points rolled on",
              b.js("!document.querySelector('#results').hidden") and len(nows or []) == 4
              and all(want.get(n) == v for n, v in nows), nows)
        check("...with the game's RON banner and the winner",
              b.js("!!document.querySelector('#results .mid img[src$=\"call-ron.png\"]')")
              and "Byakko wins by ron" in b.js("document.querySelector('#results .mid h2').textContent"))
        b.screenshot(os.path.join(o.shots, "7b-results.png"))
        write_watch_file()
        b.goto("http://127.0.0.1:%d/watch#999" % port, settle=2.5)
        check("a table its players do not allow to be watched is not shown",
              b.js("!document.querySelector('#gone').hidden") and b.js("__jan3d.tiles()") == 0)
        b.call("Emulation.setDeviceMetricsOverride", width=420, height=900,
               deviceScaleFactor=2, mobile=True)
        b.goto("http://127.0.0.1:%d/?m=1#1" % port, settle=2.5)
        check("a phone-width page opens on #1 with no sideways scroll",
              b.js("CAT") == 1 and b.js(art_ok)
              and b.js("document.documentElement.scrollWidth <= innerWidth + 1"))
        b.screenshot(os.path.join(o.shots, "5-phone.png"))
        check("on a phone too: the corner pieces cover no text, and the chocobo is in view",
              b.js(on_text) == [] and b.js(choco_in_view), b.js(on_text))
        b.call("Emulation.setDeviceMetricsOverride", width=1000, height=1500,
               deviceScaleFactor=1, mobile=False)
        b.goto("http://127.0.0.1:%d/" % port, settle=2.5)
        check("...and on a tall desktop window", b.js(on_text) == [] and b.js(choco_in_view),
              b.js(on_text))
        b.screenshot(os.path.join(o.shots, "8-tall.png"))
        # the pieces are PART of the window: after any resize each sits at the
        # same place on it (its offset over the window's size does not change),
        # so nothing can pop, fade or wander
        rel = """(() => { const s = stage.getBoundingClientRect();
          return [...document.querySelectorAll('#decor .cl')].map(c => { const r = c.getBoundingClientRect();
            return [(r.left - s.left) / s.width, (r.top - s.top) / s.height, r.width / s.width]; }) })()"""
        before = b.js(rel)
        moved = []
        # (sizes that keep the sidebar hidden: when it shows, the right-hand
        # pieces rightly move out to ITS corners)
        for w_, h_ in ((1000, 1100), (1300, 1000), (700, 1300)):
            b.call("Emulation.setDeviceMetricsOverride", width=w_, height=h_,
                   deviceScaleFactor=1, mobile=False)
            b.pump(0.4)
            after = b.js(rel)
            if after is None or len(after) != len(before) or any(
                    abs(x - y) > 0.004 for p, q in zip(before, after) for x, y in zip(p, q)):
                moved.append(((w_, h_), after))
        check("resizing never moves a piece on the window: they scale with it, nothing pops",
              len(before or []) == 4 and not moved, moved[:2])
        check("no JavaScript errors", not b.errors, b.errors)
    finally:
        b.close()
        srv.shutdown()
    print("screenshots in %s" % o.shots)
    if fails:
        raise SystemExit("[jan_board_browser] %d FAILED: %s" % (len(fails), ", ".join(fails)))
    print("[jan_board_browser] OK")


if __name__ == "__main__":
    main()
