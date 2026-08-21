"""Page rendering for the hosted version.

Deliberately reuses the stylesheet and the interactive components from the
command-line tool rather than forking them: the grid, chart, slider, browse
panel and reach panel are all plain strings in wkjiten.py, so both versions
stay in step.
"""

from __future__ import annotations

import calendar
import json
import time
import urllib.parse
from collections import Counter

import wkjiten as w

STATUS_LABELS = w.STATUS_LABELS
MEDIA_TYPES = w.MEDIA_TYPES

# Served as a real file rather than inlined: the local tool writes one page that
# has to stand alone, this one serves dozens and can let the browser cache it.
ICON = "/icon.png"


def esc(s) -> str:
    return w.esc(s)


AUTH_CSS = """
.authbox { max-width:400px; margin:8vh auto; background:var(--raise);
  border:1px solid var(--line); border-radius:16px; padding:28px;
  box-shadow:var(--shadow); }
.authbox h1 { font-size:24px; margin-bottom:18px; }
.authbox .mark { width:60px; height:60px; display:block; margin:0 auto 12px;
  border-radius:17px; }
.authbox .mark + h1 { text-align:center; }   /* only where there is a mark */
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


BURN_CSS = """
.pager { display:flex; align-items:center; gap:12px; margin:10px 0 2px;
  font-size:13px; color:var(--muted); }
.pager button { font:inherit; font-size:13px; color:var(--muted);
  background:var(--raise); border:1px solid var(--line); border-radius:9px;
  padding:5px 11px; cursor:pointer; }
.pager button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.pager button:disabled { opacity:.4; cursor:default; }
.pager span { font-variant-numeric:tabular-nums; }
"""

BURN_JS = """
(function(){
  const tbl = document.getElementById('burn');
  const pager = document.getElementById('burnpager');
  if (!tbl || !pager) return;
  const PER = 10;
  const label = pager.querySelector('span');
  const [back, fwd] = pager.querySelectorAll('button');
  let page = 0;
  function draw(){
    const rows = [...tbl.rows].slice(1);          // read the live order
    const pages = Math.max(1, Math.ceil(rows.length / PER));
    page = Math.min(Math.max(page, 0), pages - 1);
    const from = page * PER;
    rows.forEach((r, i) => {
      r.style.display = (i >= from && i < from + PER) ? '' : 'none';
    });
    label.textContent = rows.length
      ? `${from + 1}\u2013${Math.min(from + PER, rows.length)} of ${rows.length}`
      : 'nothing here';
    back.disabled = page === 0;
    fwd.disabled = page >= pages - 1;
  }
  back.onclick = () => { page--; draw(); };
  fwd.onclick = () => { page++; draw(); };
  // Sorting re-appends every row, so the page has to be redrawn over the new
  // order. The header's own handler runs first; this fires as the click
  // bubbles up to the table, by which time the rows have moved.
  tbl.addEventListener('click', e => {
    if (e.target.closest('th')) { page = 0; draw(); }
  });
  draw();
})();
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
            f'<title>{esc(title)}</title>{w.favicon_link(ICON)}'
            f'<style>{w.REPORT_CSS}{AUTH_CSS}{extra_css}</style></head><body>'
            f'<main>{bar}{body}</main>{scripts}</body></html>')


def login_page(error: str = "", note: str = "") -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    return shell("Sign in", f"""
      <div class="authbox">{w.brand_mark(ICON)}<h1>wkjiten</h1>{msg}
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
      <div class="authbox">{w.brand_mark(ICON)}<h1>Create your account</h1>{msg}
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


def settings_page(user, creds, note: str = "", error: str = "",
                  age_hours: float | None = None, push: dict | None = None) -> str:
    msg = f'<div class="err">{esc(error)}</div>' if error else ""
    if note:
        msg += f'<div class="ok">{esc(note)}</div>'
    has_wk = "stored" if creds.get("wk_token") else "not set"
    has_jt = "stored" if creds.get("jiten_key") else "not set"
    has_jm = "stored" if creds.get("jimaku_key") else "not set"
    if age_hours is None:
        fetched = "Nothing fetched from WaniKani yet."
    elif age_hours < 1:
        fetched = f"Fetched from WaniKani {age_hours * 60:.0f} minutes ago."
    elif age_hours < 48:
        fetched = f"Fetched from WaniKani {age_hours:.0f} hours ago."
    else:
        fetched = f"Fetched from WaniKani {age_hours / 24:.0f} days ago."
    has_nt = "stored" if creds.get("nihongo_key") else "not set"
    # Whether the words actually reached jiten.moe. "Sent in the background"
    # looks identical whether it worked or was refused, so say which.
    if not creds.get("jiten_key"):
        sent = ("Without a Jiten key nothing is uploaded, so the "
                "<b>jiten</b> column stays at whatever that account already knew.")
    elif not push:
        sent = "Your words have not been sent to jiten.moe from here yet."
    elif push["ok"]:
        import json as _json
        try:
            did = _json.loads(push["note"])
        except (ValueError, TypeError):
            did = {}
        new = did.get("added")
        tail = ("" if new is None else
                (f' &mdash; <b>{new:,} of them new to it</b>.' if new
                 else ' &mdash; it already knew every one.'))
        sent = (f'Last sent to jiten.moe on '
                f'{esc(push["at"][:16].replace("T", " "))}: '
                f'<b>{push["words"]:,} words</b> recognised{tail}')
    else:
        sent = (f'The last upload to jiten.moe <b>failed</b> on '
                f'{esc(push["at"][:16].replace("T", " "))}: '
                f'<span class="code">{esc(push["note"][:120])}</span>')
    return shell("Settings", f"""
      <h1>Settings</h1>{msg}
      <h2>API keys</h2>
      <p class="sub">All of them are encrypted before they touch the database.
      Leave a field blank to keep what is already stored.</p>
      <form method="post" class="authbox" style="margin:0 0 24px;max-width:560px">
        <div class="field">
          <label for="wk">WaniKani token &mdash; currently {has_wk}</label>
          <input id="wk" name="wk_token" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Make it <b>read-only</b> at
            <a href="https://www.wanikani.com/settings/personal_access_tokens"
               target="_blank" rel="noopener"
               >wanikani.com/settings/personal_access_tokens</a>. Read-only is
            all this needs.</div>
        </div>
        <div class="field">
          <label for="jt">Jiten API key &mdash; currently {has_jt}</label>
          <input id="jt" name="jiten_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Without it you still get the full kanji
            analysis; with it you also get your account's word coverage, the
            known-words upload and the list buttons. Yours is at
            <a href="https://jiten.moe/settings" target="_blank" rel="noopener"
               >jiten.moe/settings</a>. Jiten's own docs say this key
            <b>carries every permission your account has</b> &mdash; treat it
            like a password, and remove it here whenever you like.</div>
        </div>
        <div class="field">
          <label for="jm">jimaku.cc API key &mdash; currently {has_jm}</label>
          <input id="jm" name="jimaku_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Adds a <b>subs</b> link beside anime,
            drama and film, straight to that title's Japanese subtitles. Yours
            is at <a href="https://jimaku.cc/account" target="_blank"
               rel="noopener">jimaku.cc/account</a>.</div>
        </div>
        <div class="field">
          <label for="nt">NihongoTracker API key &mdash; currently {has_nt}</label>
          <input id="nt" name="nihongo_key" placeholder="paste to replace"
                 autocomplete="off">
          <div class="hint">Optional. Adds the hours and episodes you have
            logged beside each title, matched on the AniList and VNDB ids both
            sites already use. Yours is at
            <a href="https://nihongotracker.app" target="_blank" rel="noopener"
               >nihongotracker.app</a>. This one is <b>not read-only</b>
            either &mdash; it is accepted everywhere your account can reach,
            including deleting logs.</div>
        </div>
        <button class="go" type="submit">Save</button>
      </form>
      <h2>Data</h2>
      <p class="sub">{fetched} The counters on the dashboard compare that
      against the fetch before it. {sent}</p>
      <form method="post" action="/refresh" class="inline">
        <button>Refresh stats</button></form>
      <form method="post" action="/settings/baseline" class="inline">
        <button>Count changes from now</button></form>
      <form method="post" action="/settings/drop-jiten" class="inline">
        <button>Forget my Jiten key</button></form>
      <form method="post" action="/settings/delete" class="inline"
            onsubmit="return confirm('Delete your account and all of its data?')">
        <button>Delete my account</button></form>
      <p class="sub" style="margin-top:10px"><b>Refresh stats</b> pulls your
      WaniKani data and then sends your known words on to jiten.moe, which is
      what its coverage column is worked out from. It adds to that list and
      never replaces it, so anything you know from elsewhere stays.
      <b>Count changes from now</b> moves
      that comparison point to this moment, so a session you are about to do
      shows up on its own. Press it before you study, then refresh afterwards.</p>
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
.titlelist .covs { display:flex; gap:10px; white-space:nowrap; }
.titlelist .cov { color:var(--muted); font-variant-numeric:tabular-nums;
  font-size:13px; }
.titlelist .cov b { color:var(--fg); font-weight:600; }
.titlelist .kanjicov { color:var(--faint); }
.statsbox { display:flex; align-items:center; gap:7px; font-size:14px;
  color:var(--muted); cursor:pointer; }
.statsbox input { width:16px; height:16px; accent-color:var(--accent); }
.banner { height:120px; border-radius:14px 14px 0 0; background-size:cover;
  background-position:center; border-bottom:1px solid var(--line); }
.person .idbar { display:flex; gap:12px; align-items:center; padding:14px 18px 12px;
  border-bottom:1px solid var(--line); }
.person .idbar.pulled { margin-top:-34px; border-bottom:0; padding-bottom:6px; }
.avatar { width:52px; height:52px; border-radius:50%; object-fit:cover; flex:none;
  border:2px solid var(--raise); background:var(--sunk); }
.avatar.blank { display:grid; place-items:center; font:600 20px/1 var(--sans, sans-serif);
  color:var(--faint); }
.bio { color:var(--muted); font-size:13.5px; margin:2px 0 0; }
.profileform { display:grid; gap:14px; }
.profileform .row2 { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; }
.profileform input[type=file] { font-size:13px; color:var(--muted); }
.profileform input[type=text] { width:100%; font:inherit; padding:9px 12px;
  border-radius:10px; border:1px solid var(--line); background:var(--bg);
  color:var(--fg); }
.addbtn { font:600 11px/1 var(--sans, sans-serif); padding:3px 9px; border-radius:99px;
  border:1px solid var(--line); background:var(--bg); color:var(--muted);
  cursor:pointer; margin-left:8px; }
.addbtn:hover:not(:disabled) { border-color:var(--accent); color:var(--accent); }
.addbtn:disabled { opacity:.5; cursor:default; }
.addbtn.done { color:var(--good); border-color:var(--good); }
.profilewrap { max-width:620px; margin:0 auto; }
.backlink { padding-top:18px; margin:0; font-size:13.5px; }
.backlink a { color:var(--muted); border:0; }
.backlink a:hover { color:var(--accent); }
.nowplaying { display:flex; gap:16px; align-items:center; padding:16px 18px;
  border-bottom:1px solid var(--line); background:var(--accent-soft); }
.nowplaying img { width:64px; height:90px; object-fit:cover; border-radius:6px;
  flex:none; background:var(--line); box-shadow:var(--shadow); }
.nowplaying .lbl { font:650 10px/1 var(--sans, sans-serif); letter-spacing:.11em;
  text-transform:uppercase; color:var(--accent); }
.nowplaying h4 { margin:5px 0 4px; font-size:19px; line-height:1.25;
  letter-spacing:-.015em; font-weight:640; }
.nowplaying .covs { display:flex; gap:10px; }
.vs { display:inline-flex; gap:6px; align-items:baseline; margin-left:8px;
  font-size:12px; color:var(--faint); white-space:nowrap; }
.vs b { color:var(--accent); font-variant-numeric:tabular-nums; }
.titlelist .both { color:var(--accent); font-weight:600; }
.overlap { background:var(--accent-soft); border:1px solid var(--line);
  border-radius:14px; padding:4px 18px 14px; margin-bottom:8px; }
.who-has { color:var(--muted); font-size:13px; }
.sharebox { background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:16px 18px; box-shadow:var(--shadow);
  margin-bottom:22px; }
.sharebox .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.sharebox select { font:inherit; font-size:14px; padding:8px 11px; border-radius:10px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); }
.sharelink { margin-top:16px; padding-top:14px; border-top:1px solid var(--line-soft); }
.sharelink label { display:block; font:600 10.5px/1 var(--sans, inherit);
  letter-spacing:.09em; text-transform:uppercase; color:var(--faint);
  margin-bottom:7px; }
.sharelink input { flex:1 1 260px; font:13px ui-monospace,Consolas,monospace;
  padding:9px 12px; border-radius:10px; border:1px solid var(--line);
  background:var(--bg); color:var(--fg); }
.sharelink .hint { display:block; margin-top:8px; color:var(--faint); font-size:12.5px; }
.sharelink form.inline { display:inline; margin-top:8px; }
"""


SHARE_JS = """
<script>
document.querySelectorAll('[data-copy]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var text = document.getElementById(btn.dataset.copy).value;
    var done = function () {
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = 'Copy link'; }, 1500);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, done);
    else {
      var el = document.getElementById(btn.dataset.copy);
      el.select(); try { document.execCommand('copy'); done(); } catch (e) {}
    }
  });
});
</script>
"""


def share_panel(visibility: str, token: str, base_url: str, username: str,
                share_stats: bool = False) -> str:
    """One control, four levels, each containing the one before it."""
    checked = "checked" if share_stats else ""
    opts = "".join(
        f'<option value="{v}"{" selected" if v == visibility else ""}>'
        f'{esc(w_labels[v])}</option>' for v in w_levels)

    links = ""
    if visibility in ("link", "public"):
        url = f"{base_url}/s/{token}"
        links += f"""
        <div class="sharelink">
          <label for="secret">Secret link &mdash; works for anyone you send it to</label>
          <div class="row">
            <input id="secret" value="{esc(url)}" readonly onclick="this.select()">
            <button type="button" data-copy="secret">Copy link</button>
          </div>
          <form method="post" action="/together/newlink" class="inline">
            <button>Make a new link</button></form>
          <span class="hint">Making a new one stops the old link working. That is
          the only way to take a shared link back.</span>
        </div>"""
    if visibility == "public":
        pub = f"{base_url}/u/{username}"
        links += f"""
        <div class="sharelink">
          <label for="pub">Permanent address &mdash; open to anyone, no link needed</label>
          <div class="row">
            <input id="pub" value="{esc(pub)}" readonly onclick="this.select()">
            <button type="button" data-copy="pub">Copy link</button>
          </div>
        </div>"""

    return f"""
      <div class="sharebox">
        <form method="post" action="/together/share" class="row">
          <label for="vis"><b>Who can see my lists</b></label>
          <select id="vis" name="visibility" onchange="this.form.submit()">{opts}</select>
          <label class="statsbox"><input type="checkbox" name="stats" value="1"
            {checked} onchange="this.form.submit()">
            Include my WaniKani stats</label>
          <noscript><button>Save</button></noscript>
        </form>
        <p class="sub" style="margin:10px 0 0">Shared: the title, whether you are
        watching, planning or finished, and your coverage on it. Never your keys,
        your reviews or anything from WaniKani.</p>
        {links}
      </div>"""


w_levels = ("private", "instance", "link", "public")
w_labels = {
    "private": "Just me",
    "instance": "People with an account here",
    "link": "Anyone with the secret link",
    "public": "Anyone at all, at a permanent address",
}


def profile_panel(profile: dict, user, choices=None) -> str:
    """Picture, banner, a line about yourself and the title you are on now.
    All optional, all removable."""
    now = profile.get("currently")
    options = '<option value="">nothing in particular</option>' + "".join(
        f'<option value="{c["deck_id"]}"{" selected" if c["deck_id"] == now else ""}>'
        f'{esc(c["title"])}</option>' for c in (choices or []))
    return f"""
      <div class="sharebox">
        <form method="post" action="/profile" enctype="multipart/form-data"
              class="profileform">
          <div class="row">
            {avatar_tag(user["id"], profile.get("has_avatar"), user["username"])}
            <div style="flex:1 1 260px">
              <b>Your profile</b>
              <div class="sub" style="margin:2px 0 0">Shown on the shared pages.
              Leave it all empty and your name stands on its own.</div>
            </div>
          </div>
          <input type="text" name="bio" maxlength="160" value="{esc(profile.get("bio") or "")}"
                 placeholder="A line about yourself, if you like">
          <label class="statsbox" style="gap:10px">Currently
            <select name="currently">{options}</select></label>
          <div class="row2">
            <label>Picture<br><input type="file" name="avatar"
              accept="image/png,image/jpeg,image/gif,image/webp"></label>
            <label>Banner<br><input type="file" name="banner"
              accept="image/png,image/jpeg,image/gif,image/webp"></label>
            <button class="go">Save profile</button>
          </div>
          <div class="row" style="gap:16px">
            {'<label class="statsbox"><input type="checkbox" name="clear_avatar" value="1"> Remove picture</label>' if profile.get("has_avatar") else ""}
            {'<label class="statsbox"><input type="checkbox" name="clear_banner" value="1"> Remove banner</label>' if profile.get("has_banner") else ""}
          </div>
          <span class="hint">PNG, JPEG, GIF or WebP, up to 3 MB.</span>
        </form>
      </div>"""


def together_page(user, visibility, token, base_url, people, by_user, overlap,
                  absent, share_stats=False, profile=None, can_add=False,
                  note="", featured=None) -> str:
    featured = featured or {}
    head = share_panel(visibility, token, base_url, user["username"], share_stats)
    head += profile_panel(profile or {}, user,
                          by_user.get(user["id"], {}).get("ongoing", []))
    if note:
        head = f'<div class="ok">{esc(note)}</div>' + head
    mine = {r["deck_id"]: r for st in by_user.get(user["id"], {}).values()
            for r in st}

    cards = []
    for p in people:
        groups = []
        for status, label in (("ongoing", "watching / reading"),
                              ("planning", "plan to watch / read"),
                              ("completed", "finished")):
            items = by_user.get(p["id"], {}).get(status, [])
            if not items:
                continue
            lis = ""
            for it in items[:30]:
                # Only worth offering for someone else's titles, and only when
                # you have a Jiten key to add it with.
                add = ""
                # If you have the same title, put the two figures side by side.
                versus = ""
                ours = mine.get(it["deck_id"])
                if ours is not None and p["id"] != user["id"] and ours.get("coverage"):
                    versus = (f'<span class="vs">you <b>{ours["coverage"]:.0f}%</b>'
                              f'</span>')
                if can_add and p["id"] != user["id"] and it["deck_id"] not in mine:
                    add = (f'<button class="addbtn" data-add="{it["deck_id"]}"'
                           f' title="Add to your plan to watch/read">+ my list</button>')
                lis += (
                    f'<li><span class="{"both" if it["deck_id"] in overlap else ""}">'
                    f'<a href="https://jiten.moe/decks/media/{it["deck_id"]}/detail"'
                    f' target="_blank" rel="noopener">{esc(it["title"])}</a>'
                    f'{versus}{add}</span>'
                    f'<span class="covs">{cov_cells(it)}</span></li>')
            more = (f'<li class="who-has">and {len(items) - 30} more</li>'
                    if len(items) > 30 else "")
            groups.append(f'<div class="group"><h4>{label} &middot; {len(items)}</h4>'
                          f'<ul class="titlelist">{lis}{more}</ul></div>')
        seen = (p.get("seen") or "")[:10]
        top = ""
        if p.get("has_banner"):
            top = (f'<div class="banner" style="background-image:url(/media/banner/'
                   f'{p["id"]})"></div>')
        idbar = (f'<div class="idbar{" pulled" if top else ""}">'
                 f'{avatar_tag(p["id"], p.get("has_avatar"), p["username"])}'
                 f'<div><h3>{esc(p["username"])}'
                 f'{" (you)" if p["id"] == user["id"] else ""}</h3>'
                 f'<div class="meta">{p["titles"]} titles &middot; {esc(seen)}</div>'
                 + (f'<p class="bio">{esc(p["bio"])}</p>' if p.get("bio") else "")
                 + '</div></div>')
        cards.append(
            f'<div class="person">{top}{idbar}'
            f'{now_playing(featured.get(p["id"]))}'
            f'{"".join(groups) or "<div class=group><p class=empty>Nothing yet.</p></div>"}</div>')

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
    return shell("Together", body, user=user, extra_css=TOGETHER_CSS,
                 scripts=SHARE_JS + ADD_JS)


ADD_JS = """
<script>
document.querySelectorAll('[data-add]').forEach(function (btn) {
  btn.addEventListener('click', async function () {
    var was = btn.textContent;
    btn.disabled = true; btn.textContent = 'adding\u2026';
    try {
      var r = await fetch('/api/user/deck-preferences/' + btn.dataset.add + '/status', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 1})});
      if (!r.ok) throw new Error(r.status);
      btn.textContent = 'on your list';
      btn.classList.add('done');
    } catch (e) {
      btn.textContent = 'failed'; btn.disabled = false;
      setTimeout(function () { btn.textContent = was; }, 2000);
    }
  });
});
</script>
"""


def avatar_tag(user_id: int, has: bool, name: str, cls: str = "avatar") -> str:
    if has:
        return (f'<img class="{cls}" src="/media/avatar/{user_id}" alt=""'
                f' width="52" height="52">')
    return f'<div class="{cls} blank" aria-hidden="true">{esc(name[:1].upper())}</div>'


def now_playing(item, label="Currently") -> str:
    if not item:
        return ""
    return (f'<div class="nowplaying">'
            f'<img loading="lazy" alt="" src="{w.cover_url(item["deck_id"])}"'
            f' onerror="this.style.visibility=\'hidden\'">'
            f'<div><div class="lbl">{esc(label)}</div>'
            f'<h4><a href="https://jiten.moe/decks/media/{item["deck_id"]}/detail"'
            f' target="_blank" rel="noopener">{esc(item["title"])}</a></h4>'
            f'<span class="covs">{cov_cells(item)}</span></div></div>')


def cov_cells(it) -> str:
    """Words as jiten measures them, kanji as WaniKani reaches them.

    Finished titles are listed but never analysed, so they carry no kanji
    figure and say so rather than showing a zero.
    """
    word = f'{it["coverage"]:.0f}%' if it.get("coverage") else "&mdash;"
    kanji = f'{it["kanji_cov"]:.0f}%' if it.get("kanji_cov") else "&mdash;"
    return (f'<span class="cov"><b>{word}</b> words</span>'
            f'<span class="cov kanjicov">{kanji} kanji</span>')


def public_profile(owner, profile: dict, stats, lists, base_url: str,
                   featured=None, viewer=None) -> str:
    """The read-only view behind a share link. No login, no navigation."""
    username = owner["username"]
    groups = []
    total = 0
    for status, label in (("ongoing", "watching / reading"),
                          ("planning", "plan to watch / read"),
                          ("completed", "finished")):
        items = lists.get(status, [])
        total += len(items)
        if not items:
            continue
        lis = "".join(
            f'<li><span><a href="https://jiten.moe/decks/media/{it["deck_id"]}/detail"'
            f' target="_blank" rel="noopener">{esc(it["title"])}</a></span>'
            f'<span class="covs">{cov_cells(it)}</span></li>' for it in items)
        groups.append(f'<div class="group"><h4>{label} &middot; {len(items)}</h4>'
                      f'<ul class="titlelist">{lis}</ul></div>')

    inner = "".join(groups) or '<div class="group"><p class="empty">Nothing here yet.</p></div>'

    cards = ""
    if stats:
        pace = ""
        if stats.get("pace"):
            pace = (f'<div class="card"><div class="n">{stats["pace"]:.0f}</div>'
                    f'<div class="l">days per level</div></div>')
        cards = f"""
      <div class="cards" style="margin-bottom:20px">
        <div class="card"><div class="n">{stats["level"]}</div>
          <div class="l">WaniKani level</div></div>
        <div class="card"><div class="n">{stats["kanji"]:,}</div>
          <div class="l">kanji known</div></div>
        <div class="card"><div class="n">{stats["words"]:,}</div>
          <div class="l">words known</div></div>
        {pace}
      </div>"""

    asof = ""
    if stats and stats.get("as_of"):
        asof = f' &middot; as of {esc(stats["as_of"])}'
    banner = ""
    if profile.get("has_banner"):
        banner = (f'<div class="banner" style="margin-top:26px;'
                  f'border-radius:14px;background-image:url(/media/banner/'
                  f'{owner["id"]})"></div>')
    # A stranger gets no navigation at all; someone signed in should not be
    # stranded on a page with no way out.
    back = ('<p class="backlink"><a href="/">&larr; back to your dashboard</a></p>'
            if viewer else "")
    body = f"""
      <div class="profilewrap">
      {back}
      {banner}""" + f"""
      <div class="hero" style="display:block;padding:{"18px" if banner else "48px"} 0 22px">
        <div style="display:flex;gap:14px;align-items:center">
          {avatar_tag(owner["id"], profile.get("has_avatar"), username)}
          <div>
            <h1 style="margin:0">{esc(username)}</h1>
            <p class="sub" style="margin:2px 0 0">What {esc(username)} is watching
            and reading on jiten.moe &middot; {total} titles{asof}</p>
          </div>
        </div>
        {f'<p class="bio" style="margin-top:10px">{esc(profile["bio"])}</p>' if profile.get("bio") else ""}
      </div>
      {cards}
      <div class="person">{now_playing(featured)}{inner}</div>
      <footer>Word coverage is how much of each title's
      vocabulary {esc(username)} already knows, measured by
      <a href="https://jiten.moe" target="_blank" rel="noopener">jiten.moe</a>.
      Kanji coverage is how many of its kanji WaniKani has taught them, weighted
      by how often each one appears. This page is shared deliberately and can be
      withdrawn at any time.</footer>
      </div>"""
    return shell(f"{username} on jiten.moe", body, extra_css=TOGETHER_CSS)


# ---------------------------------------------------------------- dashboard

def _burn_when(at: str, now: float) -> tuple[str, float]:
    """How long until a review, and the epoch seconds it lands on.

    WaniKani stamps these in UTC; timegm reads them as such rather than as
    whatever the server happens to be set to.
    """
    try:
        t = calendar.timegm(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return "&mdash;", 0.0
    d = t - now
    if d <= 0:
        return "in your queue now", t
    if d < 86400:
        return f"in {d / 3600:.0f}h", t
    if d < 86400 * 14:
        return f"in {d / 86400:.0f} days", t
    return f"in {d / 86400 / 7:.0f} weeks", t


def dashboard(user, cache, known, decks, history, extras) -> str:
    """decks is a list of (deck dict, analysis dict) for the user's titles."""
    lvl = cache.get("level") or 0
    subjects, assignments = cache["subjects"], cache["assignments"]
    sections: list[tuple[str, str]] = []

    def h2(label: str) -> str:
        slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
        sections.append((slug, label))
        return f'<h2 id="{slug}">{esc(label)}</h2>'

    h = [f'<div class="hero">{w.brand_mark(ICON)}<div class="hd">'
         f'<h1>{esc(user["username"])}</h1>'
         f'<p class="sub">WaniKani level {lvl} &middot; '
         f'data from {esc((cache.get("fetched_at") or "")[:16].replace("T", " "))}'
         f'</p></div></div>', w.NAV_SLOT, '<div class="cards">']

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
    ntot = extras.get("nihongo_totals")
    if ntot:
        h.append(f'<div class="card"><div class="n">{ntot["hours"]:.0f}h</div>'
                 f'<div class="l">immersion logged</div>'
                 f'<div class="d">{ntot["listening"]:.0f}h listening &middot; '
                 f'{ntot["reading"]:.0f}h reading'
                 + (f' &middot; {ntot["streak"]}d streak' if ntot["streak"] else "")
                 + '</div></div>')
    h.append("</div>")
    # The bar and the counters are one thought - where WaniKani has got you -
    # so they sit together at the top rather than as a section to scroll to.
    h.append(w.level_bar_html(w.level_progress(cache), w.wk_pace(cache)))
    counts = w.month_totals(cache)
    if counts:
        h.append(w.counters_html(counts))
    h.append(w.BROWSE_SLOT)

    if not decks:
        h.append(h2("Your titles"))
        h.append('<p class="empty">Nothing on your jiten.moe lists yet. Mark '
                 'something as watching/reading or plan to watch/read on '
                 'jiten.moe, or use the search above, then refresh.</p>')
    else:
        h.append(h2("Your tracked titles"))
        groups = w.by_media_type(decks)
        split = len(groups) > 1
        nprog = extras.get("nihongo") or {}
        tcls = "sortable tight grouped" if split else "sortable tight"
        if nprog:
            tcls += " nt"
        for mtype, group in groups:
            if split:
                h.append(f'<h3 class="mediahead">'
                         f'{esc(w.MEDIA_TYPES.get(mtype, "other"))}'
                         f' <span>{len(group)}</span></h3>')
            h.append(f'<div class="wrap"><table class="{tcls}"><tr>'
                     f'<th>title</th><th class="num">kanji</th>'
                     f'<th class="num">finish L{lvl}</th><th class="num">jiten</th>'
                     f'<th class="num">lvl 95%</th><th class="num">ceiling</th>'
                     f'<th class="num">trend</th>'
                     + ('<th class="num">immersion</th>' if nprog else "")
                     + '</tr>')
            for deck, res in group:
                did = deck.get("deckId")
                live = deck.get("coverage")
                k = res["kanji_cov_occ"]
                subs = w.jimaku_url(deck, extras.get("jimaku_key"))
                out = w.outside_link(deck)
                fin = w.finishing_level(res, lvl)
                trend = _trend(history.get(did, []))
                nsort, ncell = w.nihongo_cell(nprog.get(did))
                h.append(
                    f'<tr><td><a href="https://jiten.moe/decks/media/{did}/detail"'
                    f' target="_blank" rel="noopener">{esc(w.deck_title(deck))}</a>'
                    + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                       f' rel="noopener" title="Look it up on {esc(out[0])}">'
                       f'{esc(out[0])}</a>' if out else "")
                    + (f' <button class="subs" data-entry="{subs.rsplit("/", 1)[-1]}"'
                       f' data-title="{esc(w.deck_title(deck))}"'
                       f' title="Japanese subtitles on jimaku.cc">subs</button>'
                       if subs else "")
                    + (f' <button class="subs gapbtn" data-deck="{did}"'
                       f' data-title="{esc(w.deck_title(deck))}"'
                       f' title="The words you cannot read in this yet">'
                       f'words</button>')
                    + f' <button class="subs setst" data-deck="{did}" data-st="3"'
                      f' data-done="finished ✓"'
                      f' title="Mark as finished on jiten.moe">finished</button>'
                    + f' <button class="subs setst" data-deck="{did}" data-st="0"'
                      f' data-done="removed ✓" data-confirm="1" data-drop="1"'
                      f' title="Take it off your jiten.moe lists">remove</button>'
                    + f'</td>'
                    f'<td class="num">{k:.1f}%'
                    f'<span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
                    f'<td class="num">{f"{fin:.1f}% <span class=\'up\'>{fin-k:+.1f}</span>" if fin else "&mdash;"}</td>'
                    f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
                    f'<td class="num">{w.level_for(res["curve"], 95) or "&mdash;"}</td>'
                    f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
                    f'<td class="num">{trend}</td>'
                    + (f'<td class="num" data-sort="{nsort}">{ncell}</td>'
                       if nprog else "")
                    + '</tr>')
            h.append("</table></div>")
        h.append('<div id="subsbox" class="subsbox" hidden></div>')
        h.append('<div id="gapbox" class="gapbox" hidden></div>')

    done = extras.get("finished") or []
    h.append(h2("Finished"))
    if done:
        h.append('<div class="wrap"><table class="sortable"><tr><th>title</th>'
                 '<th class="num">chars</th>'
                 '<th class="num">jiten coverage</th></tr>')
        hide = "this.style.visibility='hidden'"
        for d in done:
            did = d["deck_id"]
            cov = d.get("coverage")
            out = d.get("link")
            h.append(
                f'<tr><td class="withcover"><span class="ct">'
                f'<img class="cover" loading="lazy" alt="" src="{w.cover_url(did)}"'
                f' onerror="{hide}">'
                f'<span><a href="https://jiten.moe/decks/media/{did}/detail"'
                f' target="_blank" rel="noopener">{esc(d["title"])}</a>'
                + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                   f' rel="noopener" title="Look it up on {esc(out[0])}">'
                   f'{esc(out[0])}</a>' if out else "")
                + '</span></span></td>'
                f'<td class="num">{d.get("chars") or 0:,}</td>'
                f'<td class="num">{f"{cov:.0f}%" if cov else "&mdash;"}</td></tr>')
        h.append("</table></div>")
    else:
        h.append('<p class="empty">Nothing marked finished yet. Search above for '
                 'something you have already seen and press <b>finished</b>, or '
                 'use the button beside a title you are tracking.</p>')

    if decks:
        if extras.get("has_nihongo_key"):
            h.append(h2("Immersion"))
            h.append(w.immersion_html(extras.get("nihongo_totals"),
                                      extras.get("nihongo_unmeasured")))

        h.append(h2("Read-check any text"))
        h.append(w.READ_HTML)

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
        blocked = sum(row[0] for row in leeches)
        h.append(f'<details class="fold"><summary><span class="tw">Show the '
                 f'{len(leeches)} worst</span><span class="cnt">{blocked:,} '
                 f'occurrences you cannot read, all in items already sitting in '
                 f'your review queue</span></summary>')
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
        h.append("</table></div></details>")

    # Items one correct answer from Burned. Enlightened is four months long, so
    # these are easy to miss entirely - the item is in the queue for a day and
    # then either gone for good or back down the ladder.
    burning = cache.get("burning") or {}
    if burning:
        now = time.time()
        rows = []
        for sid, at in burning.items():
            s = subjects.get(sid)
            if not s:
                continue
            label, t = _burn_when(at, now)
            rows.append((t or float("inf"), label, s))
        rows.sort(key=lambda r: r[0])
        due = sum(1 for t, _l, _s in rows if t and t <= now)
        week = sum(1 for t, _l, _s in rows if t and now < t <= now + 86400 * 7)
        total = cache.get("burning_total") or len(rows)

        h.append(h2("One answer from burned"))
        shown = ("" if total <= len(rows) else
                 f", the soonest {len(rows):,} of {total:,}")
        h.append(f'<details class="fold"><summary><span class="tw">Show what is '
                 f'about to burn</span><span class="cnt">{due:,} in your queue '
                 f'now &middot; {week:,} within the week{shown}</span>'
                 f'</summary>')
        h.append('<p class="sub">Enlightened items, whose next review is the one '
                 'that burns them &mdash; if you answer it correctly. Get it '
                 'wrong and the item drops back down for months.</p>')
        h.append('<div class="wrap"><table id="burn" class="sortable"><tr><th>item</th>'
                 '<th>reading</th><th>meaning</th><th>type</th>'
                 '<th class="num">wk level</th><th>could burn</th></tr>')
        for t, label, s in rows:
            ch = s["characters"]
            kind = {"kanji": "kanji", "vocabulary": "vocab",
                    "kana_vocabulary": "kana"}.get(s["type"], s["type"])
            url = ("https://www.wanikani.com/"
                   + ("kanji/" if s["type"] == "kanji" else "vocabulary/")
                   + urllib.parse.quote(ch))
            h.append(f'<tr><td class="kanji"><a href="{url}" target="_blank"'
                     f' rel="noopener">{esc(ch)}</a></td>'
                     f'<td>{esc("、".join(s.get("readings") or []))}</td>'
                     f'<td>{esc(s.get("meaning") or "")}</td><td>{kind}</td>'
                     f'<td class="num">{s["level"]}</td>'
                     f'<td data-sort="{t if t != float("inf") else 0:.0f}">'
                     f'{label}</td></tr>')
        h.append("</table></div>")
        # Ten at a time: the whole point is the next few, and a sortable table
        # of four hundred rows buries them.
        h.append('<div class="pager" id="burnpager">'
                 '<button type="button">&lsaquo; prev</button><span></span>'
                 '<button type="button">next &rsaquo;</button></div>')
        h.append("</details>")

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
        f"<script>{w.REACH_JS}</script><script>{w.BROWSE_JS}</script><script>{w.READ_JS}</script><script>{w.GAP_JS}</script>"
        f"<script>{w.SUBS_JS}</script><script>{w.STATUS_JS}</script>"
        f"<script>{BURN_JS}</script>")

    return shell(f'{user["username"]} - coverage', body, user=user,
                 extra_css=w.SLIDER_CSS + w.GRID_CSS + w.CHART_CSS + w.REACH_CSS
                 + w.BROWSE_CSS + w.SUBS_CSS + w.READ_CSS + w.GAP_CSS + BURN_CSS,
                 scripts=scripts)


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
