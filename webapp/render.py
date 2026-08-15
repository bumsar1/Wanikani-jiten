"""Page rendering for the hosted version.

Deliberately reuses the stylesheet and the interactive components from the
command-line tool rather than forking them: the grid, chart, slider, browse
panel and reach panel are all plain strings in wkjiten.py, so both versions
stay in step.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections import Counter

import wkjiten as w

STATUS_LABELS = w.STATUS_LABELS
MEDIA_TYPES = w.MEDIA_TYPES


def esc(s) -> str:
    return w.esc(s)


AUTH_CSS = """
.authbox { max-width:400px; margin:8vh auto; background:var(--raise);
  border:1px solid var(--line); border-radius:16px; padding:28px;
  box-shadow:var(--shadow); }
.authbox h1 { font-size:24px; margin-bottom:18px; }
.field { margin-bottom:14px; }
.field label { display:block; font-size:12px; color:var(--faint); margin-bottom:5px;
  text-transform:uppercase; letter-spacing:.07em; font-weight:600; }
.field input { width:100%; font:inherit; padding:10px 13px; border-radius:11px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); }
.field .hint { font-size:12px; color:var(--faint); margin-top:5px; }
.err { background:var(--accent-soft); color:var(--accent); padding:10px 14px;
  border-radius:11px; font-size:14px; margin-bottom:14px; }
.ok { background:var(--accent-soft); color:var(--good); padding:10px 14px;
  border-radius:11px; font-size:14px; margin-bottom:14px; }
.topbar { display:flex; justify-content:space-between; align-items:center;
  gap:12px; padding:14px 0; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.topbar .who { color:var(--muted); font-size:13px; }
.topbar nav { margin:0; }
form.inline { display:inline; }
.code { font-family:ui-monospace,Menlo,Consolas,monospace; background:var(--bg);
  padding:2px 7px; border-radius:6px; border:1px solid var(--line); }
"""


def shell(title: str, body: str, *, user=None, extra_css: str = "",
          scripts: str = "") -> str:
    """Common HTML skeleton. Same tokens as the local dashboard."""
    bar = ""
    if user:
        bar = (f'<div class="topbar"><nav><a href="/">dashboard</a>'
               f'<a href="/together">together</a><a href="/settings">settings</a>'
               + ('<a href="/invites">invites</a>' if user.get("is_admin") else "")
               + f'</nav><span class="who">{esc(user["username"])} &middot; '
                 f'<a href="/logout">log out</a></span></div>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title>'
            f'<style>{w.REPORT_CSS}{AUTH_CSS}{extra_css}</style></head><body>'
            f'<main>{bar}{body}</main>{scripts}</body></html>')


def login_page(error: str = "", note: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    return shell("Sign in", f"""
      <div class="authbox"><h1>wkjiten</h1>{msg}
      <form method="post">
        <div class="field"><label for="u">username</label>
          <input id="u" name="username" autocomplete="username" autofocus required></div>
        <div class="field"><label for="p">password</label>
          <input id="p" name="password" type="password"
                 autocomplete="current-password" required></div>
        <button class="go" type="submit">Sign in</button>
      </form>
      <p class="sub" style="margin-top:16px">Accounts are invitation only.</p>
      </div>""")


def register_page(code: str, error: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    return shell("Create account", f"""
      <div class="authbox"><h1>Create your account</h1>{msg}
      <form method="post">
        <input type="hidden" name="code" value="{esc(code)}">
        <div class="field"><label for="u">username</label>
          <input id="u" name="username" autocomplete="username" autofocus required></div>
        <div class="field"><label for="p">password</label>
          <input id="p" name="password" type="password" minlength="8"
                 autocomplete="new-password" required>
          <div class="hint">At least 8 characters.</div></div>
        <button class="go" type="submit">Create account</button>
      </form></div>""")


def settings_page(user, creds, note: str = "", error: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    has_wk = "stored" if creds.get("wk_token") else "not set"
    has_jt = "stored" if creds.get("jiten_key") else "not set"
    return shell("Settings", f"""
      <h1>Settings</h1>{msg}
      <h2>API keys</h2>
      <p class="sub">Both are encrypted before they touch the database. Leave a
      field blank to keep what is already stored.</p>
      <form method="post" class="authbox" style="margin:0 0 24px;max-width:560px">
        <div class="field">
          <label for="wk">WaniKani token &mdash; currently {has_wk}</label>
          <input id="wk" name="wk_token" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Make it <b>read-only</b> at
            wanikani.com/settings/personal_access_tokens. Read-only is all this
            needs.</div>
        </div>
        <div class="field">
          <label for="jt">Jiten API key &mdash; currently {has_jt}</label>
          <input id="jt" name="jiten_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Without it you still get the full kanji
            analysis; with it you also get your account's word coverage, the
            known-words upload and the list buttons. Jiten's own docs say this
            key <b>carries every permission your account has</b> &mdash; treat
            it like a password, and remove it here whenever you like.</div>
        </div>
        <button class="go" type="submit">Save</button>
      </form>
      <h2>Data</h2>
      <form method="post" action="/refresh" class="inline">
        <button>Refresh from WaniKani now</button></form>
      <form method="post" action="/settings/drop-jiten" class="inline">
        <button>Forget my Jiten key</button></form>
      <form method="post" action="/settings/delete" class="inline"
            onsubmit="return confirm('Delete your account and all of its data?')">
        <button>Delete my account</button></form>
      """, user=user)


def invites_page(user, invites, base_url: str) -> str:
    cells = []
    for i in invites:
        used = esc(i["used_by_name"]) if i["used_by_name"] else "&mdash;"
        link = ("&mdash;" if i["used_by"] else
                f'<span class="code">{esc(base_url)}/register/{esc(i["code"])}</span>')
        cells.append(f'<tr><td class="code">{esc(i["code"])}</td>'
                     f'<td>{esc(i["created_at"][:10])}</td>'
                     f'<td>{used}</td><td>{link}</td></tr>')
    rows = "".join(cells)
    return shell("Invites", f"""
      <h1>Invitations</h1>
      <p class="sub">Accounts can only be created with a code. Send someone the
      link; it works once.</p>
      <form method="post"><button class="go">Create an invitation</button></form>
      <h2>Codes</h2>
      <div class="wrap"><table><tr><th>code</th><th>created</th><th>used by</th>
      <th>link</th></tr>{rows}</table></div>""", user=user)


TOGETHER_CSS = """
.people { display:grid; gap:20px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
  margin-bottom:8px; }
.person { background:var(--raise); border:1px solid var(--line); border-radius:14px;
  box-shadow:var(--shadow); overflow:hidden; }
.person > header { padding:16px 18px 12px; border-bottom:1px solid var(--line);
  display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
.person h3 { margin:0; font-size:18px; font-weight:640; letter-spacing:-.015em; }
.person .meta { color:var(--faint); font-size:12px; }
.person .group { padding:12px 18px 4px; }
.person .group h4 { margin:0 0 8px; font-size:10.5px; letter-spacing:.09em;
  text-transform:uppercase; color:var(--faint); font-weight:650; }
.titlelist { list-style:none; margin:0 0 12px; padding:0; }
.titlelist li { display:flex; justify-content:space-between; gap:12px; padding:5px 0;
  border-bottom:1px solid var(--line-soft); font-size:14.5px; }
.titlelist li:last-child { border-bottom:0; }
.titlelist .cov { color:var(--muted); font-variant-numeric:tabular-nums;
  white-space:nowrap; }
.titlelist .both { color:var(--accent); font-weight:600; }
.overlap { background:var(--accent-soft); border:1px solid var(--line);
  border-radius:14px; padding:4px 18px 14px; margin-bottom:8px; }
.who-has { color:var(--muted); font-size:13px; }
.notsharing { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
  background:var(--raise); border:1px solid var(--line); border-radius:14px;
  padding:16px 18px; box-shadow:var(--shadow); }
"""


def together_page(user, sharing: bool, people, by_user, overlap, absent) -> str:
    if not sharing:
        head = f"""
      <div class="notsharing">
        <div style="flex:1 1 320px">
          <b>You are not sharing.</b>
          <div class="sub" style="margin:4px 0 0">You can see what others share
          without sharing yourself, but it is a nicer trade the other way.</div>
        </div>
        <form method="post" action="/together/share"><input type="hidden"
          name="on" value="1"><button class="go">Share my lists</button></form>
      </div>"""
    else:
        head = f"""
      <div class="notsharing">
        <div style="flex:1 1 320px">
          <b>You are sharing your lists.</b>
          <div class="sub" style="margin:4px 0 0">Everyone with an account here can
          see your titles, their status and your coverage on them. Not your keys,
          not your reviews.</div>
        </div>
        <form method="post" action="/together/share"><input type="hidden"
          name="on" value="0"><button>Stop sharing</button></form>
      </div>"""

    cards = []
    for p in people:
        groups = []
        for status, label in (("ongoing", "watching / reading"),
                              ("planning", "plan to watch / read"),
                              ("completed", "finished")):
            items = by_user.get(p["id"], {}).get(status, [])
            if not items:
                continue
            lis = "".join(
                f'<li><span class="{"both" if it["deck_id"] in overlap else ""}">'
                f'<a href="https://jiten.moe/decks/media/{it["deck_id"]}/detail"'
                f' target="_blank" rel="noopener">{esc(it["title"])}</a></span>'
                f'<span class="cov">'
                f'{f"{it['coverage']:.0f}%" if it.get("coverage") else "&mdash;"}'
                f'</span></li>' for it in items[:30])
            more = (f'<li class="who-has">and {len(items) - 30} more</li>'
                    if len(items) > 30 else "")
            groups.append(f'<div class="group"><h4>{label} &middot; {len(items)}</h4>'
                          f'<ul class="titlelist">{lis}{more}</ul></div>')
        seen = (p.get("seen") or "")[:10]
        cards.append(
            f'<div class="person"><header><h3>{esc(p["username"])}'
            f'{" (you)" if p["id"] == user["id"] else ""}</h3>'
            f'<span class="meta">{p["titles"]} titles &middot; {esc(seen)}</span>'
            f'</header>{"".join(groups) or "<div class=group><p class=empty>Nothing yet.</p></div>"}</div>')

    overlap_html = ""
    if overlap:
        rows = "".join(
            f'<li><span><a href="https://jiten.moe/decks/media/{d}/detail"'
            f' target="_blank" rel="noopener">{esc(info["title"])}</a></span>'
            f'<span class="who-has">{esc(", ".join(info["who"]))}</span></li>'
            for d, info in sorted(overlap.items(),
                                  key=lambda kv: -len(kv[1]["who"]))[:20])
        overlap_html = (f'<h2>Both of you</h2><div class="overlap">'
                        f'<ul class="titlelist">{rows}</ul></div>')

    missing = ""
    if absent:
        missing = (f'<p class="sub">Not sharing yet: '
                   f'{esc(", ".join(absent))}. They can turn it on from this page.</p>')

    body = (f'<h1>Together</h1><p class="sub">What everyone here is watching, '
            f'reading and has finished, straight from their jiten.moe lists.</p>'
            f'{head}{overlap_html}<h2>Everyone</h2>'
            f'<div class="people">{"".join(cards) or ""}</div>{missing}')
    if not people:
        body = (f'<h1>Together</h1><p class="sub">Nobody is sharing their lists yet.'
                f'</p>{head}{missing}')
    return shell("Together", body, user=user, extra_css=TOGETHER_CSS)


# ---------------------------------------------------------------- dashboard

def dashboard(user, cache, known, decks, history, extras) -> str:
    """decks is a list of (deck dict, analysis dict) for the user's titles."""
    lvl = cache.get("level") or 0
    subjects, assignments = cache["subjects"], cache["assignments"]
    sections: list[tuple[str, str]] = []

    def h2(label: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
        sections.append((slug, label))
        return f'<h2 id="{slug}">{esc(label)}</h2>'

    h = [f'<div class="hero"><h1>{esc(user["username"])} on '
         f'<span>jiten.moe</span></h1><p class="sub">WaniKani level {lvl} &middot; '
         f'data from {esc((cache.get("fetched_at") or "")[:16].replace("T", " "))}'
         f'</p></div>', w.NAV_SLOT, '<div class="cards">']

    def card(n, label, delta=None):
        d = f'<div class="d">{delta:+d} since last refresh</div>' if delta else ""
        return (f'<div class="card"><div class="n">{n}</div>'
                f'<div class="l">{label}</div>{d}</div>')

    h.append(card(lvl, "level"))
    h.append(card(len(known["kanji_known"]), "kanji known", extras.get("d_kanji")))
    h.append(card(len(known["words_known_set"]), "words known", extras.get("d_words")))
    if decks:
        best = max(decks, key=lambda r: r[1]["kanji_cov_occ"])
        h.append(card(f'{best[1]["kanji_cov_occ"]:.0f}%',
                      f'best: {esc(w.deck_title(best[0])[:14])}'))
    h.append("</div>")
    h.append(w.BROWSE_SLOT)

    if not decks:
        h.append(h2("Your titles"))
        h.append('<p class="empty">Nothing on your jiten.moe lists yet. Mark '
                 'something as watching/reading or plan to watch/read on '
                 'jiten.moe, or use the search above, then refresh.</p>')
    else:
        h.append(h2("Your tracked titles"))
        h.append(f'<div class="wrap"><table class="sortable"><tr><th>title</th>'
                 f'<th>list</th><th class="num">kanji</th><th></th>'
                 f'<th class="num">finish L{lvl}</th><th class="num">jiten</th>'
                 f'<th class="num">lvl for 95%</th><th class="num">ceiling</th>'
                 f'<th class="num">trend</th></tr>')
        for deck, res in sorted(decks, key=lambda r: -r[1]["kanji_cov_occ"]):
            did = deck.get("deckId")
            live = deck.get("coverage")
            k = res["kanji_cov_occ"]
            fin = w.finishing_level(res, lvl)
            trend = _trend(history.get(did, []))
            h.append(
                f'<tr><td><a href="https://jiten.moe/decks/media/{did}/detail"'
                f' target="_blank" rel="noopener">{esc(w.deck_title(deck))}</a></td>'
                f'<td>{esc(STATUS_LABELS.get(extras["status"].get(did), "—"))}</td>'
                f'<td class="num">{k:.1f}%</td>'
                f'<td><span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
                f'<td class="num">{f"{fin:.1f}% <span class=\'up\'>{fin-k:+.1f}</span>" if fin else "&mdash;"}</td>'
                f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
                f'<td class="num">{w.level_for(res["curve"], 95) or "&mdash;"}</td>'
                f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
                f'<td class="num">{trend}</td></tr>')
        h.append("</table></div>")

        h.append(h2("What each level would buy you"))
        h.append(w.CHART_HTML)
        h.append(w.SLIDER_HTML)

    # Kanji grid
    occ: Counter[str] = Counter()
    for _d, res in decks:
        occ.update(res["kanji_occ"])
    per_title: dict[str, list] = {}
    for i, (_d, res) in enumerate(decks):
        for ch, n in res["kanji_occ"].items():
            per_title.setdefault(ch, []).append([i, n])
    for ch in per_title:
        per_title[ch].sort(key=lambda p: -p[1])

    grid = sorted(
        ({"c": s["characters"], "l": s["level"], "s": assignments.get(sid, 0),
          "k": s["characters"] in known["kanji_known"],
          "n": occ.get(s["characters"], 0),
          "r": "、".join(s.get("readings") or []), "m": s.get("meaning") or "",
          "d": per_title.get(s["characters"], []),
          **({"up": 1} if s["characters"] in extras.get("moved_up", set()) else {})}
         for sid, s in subjects.items() if s["type"] == "kanji"),
        key=lambda k: (k["l"], -k["n"], k["c"]))

    h.append(h2("Kanji grid"))
    h.append(f'<p class="sub">All {len(grid):,} WaniKani kanji by level. '
             f'{sum(1 for k in grid if k["n"]):,} of them turn up in your '
             f'titles.</p>')
    h.append(w.GRID_HTML)

    # Leeches
    struggling = {s["characters"]: (assignments[sid], s["level"],
                                    "、".join(s.get("readings") or []),
                                    s.get("meaning") or "")
                  for sid, s in subjects.items()
                  if s["type"] == "kanji" and 1 <= assignments.get(sid, 0) <= 4}
    leeches = sorted(((n, ch) + struggling[ch] for ch, n in occ.items()
                      if ch in struggling and ch not in known["kanji_known"]),
                     reverse=True)[:24]
    if leeches:
        h.append(h2("Leeches blocking your reading"))
        h.append('<p class="sub">Apprentice kanji, ranked by how often they turn '
                 'up in the titles above.</p>')
        h.append('<div class="wrap"><table class="sortable"><tr><th>kanji</th>'
                 '<th>reading</th><th>meaning</th><th class="num">occurrences</th>'
                 '<th>stage</th><th class="num">wk level</th></tr>')
        for n, ch, stage, klvl, rd, mean in leeches:
            url = "https://www.wanikani.com/kanji/" + urllib.parse.quote(ch)
            h.append(f'<tr><td class="kanji"><a href="{url}" target="_blank"'
                     f' rel="noopener">{esc(ch)}</a></td><td>{esc(rd)}</td>'
                     f'<td>{esc(mean)}</td><td class="num">{n:,}</td>'
                     f'<td>{w.SRS_STAGE_NAMES.get(stage, "?")}</td>'
                     f'<td class="num">{klvl}</td></tr>')
        h.append("</table></div>")

    h.append(h2("Nearly within reach"))
    h.append(w.REACH_HTML)
    h.append('<footer>Kanji figures computed here from Jiten word lists; the '
             'jiten column is your own account coverage. Deck word lists are '
             'cached and shared between accounts.</footer>')

    # Data the client scripts need.
    pace = round(w.wk_pace(cache) or 0, 1)
    blob = json.dumps({"level": lvl, "pace": pace,
                       "known": "".join(sorted(known["kanji_known"])),
                       "levels": known["kanji_level"], "types": MEDIA_TYPES},
                      ensure_ascii=False, separators=(",", ":"))
    track = json.dumps({"level": lvl, "pace": pace, "titles": [
        {"t": w.deck_title(d), "now": round(r["kanji_cov_occ"], 2),
         "c": [round(p, 2) for _l, p in r["curve"]]}
        for d, r in sorted(decks, key=lambda r: -r[1]["kanji_cov_occ"])]},
        ensure_ascii=False, separators=(",", ":"))

    body = "".join(h)
    links = [("browse", "Browse jiten.moe")] + sections
    body = body.replace(w.BROWSE_SLOT, w.BROWSE_HTML)
    body = body.replace(w.NAV_SLOT, "<nav>" + "".join(
        f'<a href="#{s}">{esc(t)}</a>' for s, t in links) + "</nav>")

    scripts = (
        f"<script>const TRACK={track};const WK={blob};"
        f"const GRID={json.dumps(grid, ensure_ascii=False, separators=(',', ':'))};"
        f"const GRID_LEVEL={lvl};"
        f"const GRID_TITLES={json.dumps([w.deck_title(d) for d, _ in decks], ensure_ascii=False)};"
        f"const LIVE=true;const OTHERS=[];"
        f"const TAGS={json.dumps(extras.get('tags', []), ensure_ascii=False)};"
        f"const REACH_TARGET={min(60, lvl + 5)};</script>"
        f"<script>{w.SORT_JS}</script><script>{w.SLIDER_JS}</script>"
        f"<script>{w.CHART_JS}</script><script>{w.GRID_JS}</script>"
        f"<script>{w.REACH_JS}</script><script>{w.BROWSE_JS}</script>")

    return shell(f'{user["username"]} - coverage', body, user=user,
                 extra_css=w.SLIDER_CSS + w.GRID_CSS + w.CHART_CSS + w.REACH_CSS
                 + w.BROWSE_CSS, scripts=scripts)


def _trend(rows) -> str:
    pts = [(r["day"], r["kanji_cov"]) for r in rows if r.get("kanji_cov") is not None]
    if len(pts) < 2:
        return "&mdash;"
    delta = pts[-1][1] - pts[0][1]
    try:
        d0 = time.mktime(time.strptime(pts[0][0], "%Y-%m-%d"))
        d1 = time.mktime(time.strptime(pts[-1][0], "%Y-%m-%d"))
        days = round((d1 - d0) / 86400)
    except ValueError:
        days = 0
    cls = ' class="up"' if delta > 0 else ""
    return f'<span{cls}>{delta:+.1f}pp</span> / {days}d'
