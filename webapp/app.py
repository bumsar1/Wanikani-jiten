"""wkjiten, hosted: the same analysis, several accounts, one shared deck cache.

The command-line tool is imported rather than reimplemented, so the local
`python wkjiten.py serve` flow keeps working exactly as before and both
versions share one definition of what coverage means.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.parse
import zipfile
from collections import Counter

from flask import (Flask, abort, redirect, request, session, url_for,
                   Response, jsonify)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wkjiten as w          # noqa: E402
import render                # noqa: E402
import store                 # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("WKJITEN_SESSION_SECRET") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  MAX_CONTENT_LENGTH=4 << 20)
if os.environ.get("WKJITEN_HTTPS", "").lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True

STALE_HOURS = float(os.environ.get("WKJITEN_STALE_HOURS", "18"))
_refreshing: set[int] = set()
_lock = threading.Lock()

# Third-party answers worth a second look now and then, but not on every page
# load. The word lists are already cached in the database; what was left was
# twenty-odd round trips to jiten.moe and nihongotracker.app per render, which
# is where nearly all of the wait came from.
TAGS_TTL = 24 * 3600        # a fixed vocabulary of 252 names
NIHONGO_TTL = 300           # your own hours, which do not move by the second
_memo: dict[tuple, tuple[float, object]] = {}
_memo_lock = threading.Lock()


def cached(key: tuple, ttl: float, build):
    """Memo with a deadline. `build` runs outside the lock, so one slow call
    cannot hold up everybody else - at the cost of two of them occasionally
    running at once, which is harmless here."""
    now = time.time()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = build()
    with _memo_lock:
        if len(_memo) > 64:
            for k in [k for k, (until, _) in _memo.items() if until <= now]:
                _memo.pop(k, None)
        _memo[key] = (now + ttl, value)
    return value


# ------------------------------------------------------------------- helpers

def current_user():
    uid = session.get("uid")
    return store.get_user(uid) if uid else None


def require_login():
    """Raises a redirect to the login page, which Flask propagates as-is."""
    user = current_user()
    if not user:
        abort(redirect(url_for("login")))
    return user


def creds_of(user) -> dict:
    return store.get_creds(user["id"])


def refresh_wanikani(user_id: int, token: str,
                     push_key: str | None = None) -> None:
    """Pull the account's subjects and assignments and store the snapshot.

    With push_key, the fresh vocabulary is then sent on to jiten.moe, because
    the coverage column over there is only as new as the last upload.

    A refresh already in flight wins; the caller should wait for it rather
    than start a second one against the same rate limit.
    """
    with _lock:
        if user_id in _refreshing:
            return
        _refreshing.add(user_id)
    try:
        payload = w.wk_fetch(token)
        store.save_snapshot(user_id, payload)
    except SystemExit as e:
        app.logger.warning("wanikani refresh failed for %s: %s", user_id, e)
        return
    finally:
        with _lock:
            _refreshing.discard(user_id)
    if push_key:
        push_known_words(user_id, payload, push_key)


def push_known_words(user_id: int, snapshot: dict, key: str) -> None:
    """Send this account's WaniKani vocabulary to its jiten.moe account.

    This is what makes jiten.moe's own coverage column reflect WaniKani, and it
    is the one thing here that writes to somebody else's service - so it adds
    to the list rather than replacing it, and what happened is recorded.
    """
    try:
        known = w.wk_known(snapshot)
        content = w.known_words_txt(known)
        status, text = w.jiten_push_words(content, key)
    except SystemExit as e:
        store.log_push(user_id, 0, False, str(e))
        return
    ok = status < 400
    if not ok:
        app.logger.warning("jiten push failed for %s: %s %s", user_id, status, text)
        # Jiten answers 401 with an empty body, and "failed:" followed by
        # nothing is worse than no message at all.
        store.log_push(user_id, 0, False,
                       f"HTTP {status}" + (f" {text}" if text else ""))
        return
    # Jiten says what it did with the file - how many it recognised and how
    # many were new. That is worth more than a count of what we sent, which
    # says nothing about whether the account learned anything.
    try:
        said = json.loads(text)
    except (ValueError, TypeError):
        said = {}
    store.log_push(user_id, said.get("parsed") or len(set(known["words_known"])),
                   True, json.dumps({k: said.get(k) for k in ("added", "updated")}
                                    ) if said else f"HTTP {status}")


def await_snapshot(user_id: int, timeout: float = 120.0):
    """Wait out a refresh that is already running.

    Saving your keys kicks off a fetch in the background; opening the
    dashboard a moment later used to step aside for it, find nothing, and
    blame the token. A first fetch is ~9,000 subjects, so it takes a while.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            running = user_id in _refreshing
        if not running:
            break
        time.sleep(1.0)
        snap = store.get_snapshot(user_id)
        if snap:
            return snap
    return store.get_snapshot(user_id)


def deck_words_shared(deck_id: int, key: str | None, deck: dict | None):
    """One copy per title for the whole instance.

    A word list belongs to the title, not the account, so the second person to
    look at a series gets it instantly - and Jiten sees one download instead of
    one per user.
    """
    stamp = (deck or {}).get("lastUpdate")
    cached = store.get_deck_words(deck_id, stamp)
    if cached is not None:
        return Counter(cached)
    counts = Counter(w.jiten_deck_tokens(deck_id, key))
    store.put_deck_words(deck_id, stamp, dict(counts))
    return counts


SHARED_STATUSES = ("ongoing", "planning", "completed")


def user_decks(key: str | None):
    """Every title on this account's jiten.moe lists.

    Returns the ids to analyse (what they are actually on), the status of each,
    the full set including finished titles for the shared page - those are
    worth showing to a friend but not worth downloading word lists for - and
    the raw deck dicts, which carry the outside links but are far too big to
    put in a stored snapshot.
    """
    if not key:
        return [], {}, [], []
    ids, status, everything, raw = [], {}, [], []
    for st in SHARED_STATUSES:
        try:
            rows = w.jiten_status_decks(st, key)
        except SystemExit:
            continue
        for row in rows:
            did = row["deckId"]
            raw.append(row)
            everything.append({
                "deck_id": did, "status": st,
                "title": (row.get("originalTitle") or row.get("englishTitle")
                          or row.get("romajiTitle") or "?"),
                "media_type": row.get("mediaType"),
                "chars": row.get("characterCount"),
                "coverage": row.get("coverage")})
            if st != "completed" and did not in status:
                status[did] = st
                ids.append(did)
    return ids, status, everything, raw


# -------------------------------------------------------------------- routes

@app.get("/login")
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    return render.login_page(note=request.args.get("note", ""))


@app.post("/login")
def do_login():
    user = store.check_login(request.form.get("username", ""),
                             request.form.get("password", ""))
    if not user:
        return render.login_page(error="Wrong username or password."), 401
    session.clear()
    session["uid"] = user["id"]
    session.permanent = True
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/register/<code>")
def register(code):
    if not store.invite_open(code):
        return render.login_page(error="That invitation is not valid."), 403
    return render.register_page(code)


@app.post("/register/<code>")
def do_register(code):
    if not store.invite_open(code):
        return render.login_page(error="That invitation is not valid."), 403
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if len(username) < 2 or len(password) < 8:
        return render.register_page(code, "Pick a longer name or password."), 400
    try:
        # The first account to exist runs the place.
        uid = store.create_user(username, password, is_admin=store.user_count() == 0)
    except Exception:
        return render.register_page(code, "That name is taken."), 409
    store.consume_invite(code, uid)
    session.clear()
    session["uid"] = uid
    return redirect(url_for("settings"))


@app.get("/settings")
def settings():
    user = require_login()
    return render.settings_page(user, creds_of(user),
                                note=request.args.get("note", ""),
                                age_hours=store.snapshot_age_hours(user["id"]),
                                push=store.get_push(user["id"]))


@app.post("/settings")
def save_settings():
    user = require_login()
    store.set_creds(user["id"],
                    request.form.get("wk_token", "").strip() or None,
                    request.form.get("jiten_key", "").strip() or None,
                    request.form.get("jimaku_key", "").strip() or None,
                    request.form.get("nihongo_key", "").strip() or None)
    creds = creds_of(user)
    if creds.get("wk_token") and not store.get_snapshot(user["id"]):
        threading.Thread(target=refresh_wanikani,
                         args=(user["id"], creds["wk_token"],
                               creds.get("jiten_key")), daemon=True).start()
    return redirect(url_for("settings", note="Saved."))


@app.post("/settings/drop-jiten")
def drop_jiten():
    user = require_login()
    store.clear_jiten_key(user["id"])
    return redirect(url_for("settings", note="Jiten key removed."))


@app.post("/settings/delete")
def delete_account():
    user = require_login()
    store.delete_user(user["id"])
    session.clear()
    return redirect(url_for("login", note="Account deleted."))


@app.post("/refresh")
def refresh():
    user = require_login()
    creds = creds_of(user)
    if not creds.get("wk_token"):
        return redirect(url_for("settings", note="Add a WaniKani token first."))
    threading.Thread(target=refresh_wanikani,
                     args=(user["id"], creds["wk_token"],
                           creds.get("jiten_key")), daemon=True).start()
    note = ("Refreshing from WaniKani, then sending your words to jiten.moe. "
            "Reload in a moment." if creds.get("jiten_key")
            else "Refreshing from WaniKani in the background.")
    return redirect(url_for("settings", note=note))


@app.post("/settings/baseline")
def baseline():
    """Start the since-last-refresh counters from where you are now."""
    user = require_login()
    if not store.mark_baseline(user["id"]):
        return redirect(url_for("settings",
                                note="Nothing to count from yet - refresh first."))
    return redirect(url_for("settings", note="Counting from now. Do your session, "
                                             "then refresh to see what it added."))


@app.get("/")
def dashboard():
    user = require_login()
    creds = creds_of(user)
    if not creds.get("wk_token"):
        return redirect(url_for("settings",
                                note="Add your WaniKani token to get started."))

    cache = store.get_snapshot(user["id"])
    if not cache:
        # First visit: fetch inline, or wait out the one already running. No
        # upload here - this one is holding up the page as it is.
        refresh_wanikani(user["id"], creds["wk_token"])
        cache = store.get_snapshot(user["id"]) or await_snapshot(user["id"])
        if not cache:
            return redirect(url_for(
                "settings",
                note="WaniKani did not answer in time. If the terminal is still "
                     "listing pages, give it a moment and reload; otherwise "
                     "check the token."))
    age = store.snapshot_age_hours(user["id"]) or 0
    if age > STALE_HOURS:
        # The upload rides along, so the jiten column does not quietly fall
        # behind what WaniKani has taught you since. It only ever adds words.
        threading.Thread(target=refresh_wanikani,
                         args=(user["id"], creds["wk_token"],
                               creds.get("jiten_key")), daemon=True).start()

    known = w.wk_known(cache)
    key = creds.get("jiten_key")
    ids, status, everything, raw_decks = user_decks(key)

    # The rows the list endpoints already returned carry exactly the fields
    # /detail does - links, coverage, character counts, the lot - so asking for
    # each title again was one request per tracked title for nothing.
    by_id = {row["deckId"]: row for row in raw_decks}
    decks = []
    for deck_id in ids[:40]:
        deck = by_id.get(deck_id)
        if deck is None:
            continue
        try:
            words = deck_words_shared(deck_id, key, deck)
        except SystemExit:
            continue
        decks.append((deck, w.analyse_deck(words, known)))

    if store.is_sharing(user["id"]) and everything:
        # Attach the kanji figure to the titles that have one; finished titles
        # are listed but never analysed, so they simply do not get one.
        kanji_by_id = {d.get("deckId"): round(r["kanji_cov_occ"], 1)
                       for d, r in decks}
        for row in everything:
            row["kanji_cov"] = kanji_by_id.get(row["deck_id"])
        store.put_shared_lists(user["id"], everything)

    day = time.strftime("%Y-%m-%d")
    store.log_history(user["id"], day, [
        {"deck_id": d.get("deckId"), "title": w.deck_title(d),
         "wk_level": cache.get("level"),
         "kanji_known": len(known["kanji_known"]),
         "words_known": len(known["words_known_set"]),
         "kanji_cov": round(r["kanji_cov_occ"], 2),
         "jiten_cov": d.get("coverage")}
        for d, r in decks])

    prev = store.get_snapshot(user["id"], "previous")
    # The outside links live on the raw rows, which are too big to keep in a
    # stored snapshot - so they are attached here, for this render only.
    links = {row["deckId"]: w.outside_link(row) for row in raw_decks}
    finished = []
    for r in everything:
        if r["status"] == "completed":
            finished.append(dict(r, link=links.get(r["deck_id"])))
    extras = {"status": status, "tags": [], "moved_up": set(),
              "jimaku_key": creds.get("jimaku_key"), "finished": finished}
    if prev:
        old = w.wk_known(prev)
        extras["d_kanji"] = len(known["kanji_known"]) - len(old["kanji_known"])
        extras["d_words"] = (len(known["words_known_set"])
                             - len(old["words_known_set"]))
        old_stage = prev.get("assignments", {})
        extras["moved_up"] = {
            s["characters"] for sid, s in cache["subjects"].items()
            if s["type"] == "kanji"
            and cache["assignments"].get(sid, 0) > old_stage.get(sid, 0)
            and sid in prev.get("subjects", {})}
    if key:
        def fetch_tags():
            try:
                return w.get_json(f"{w.JITEN_API}/api/media-deck/tags",
                                  headers=w.jiten_headers(key))
            except SystemExit:
                return []
        extras["tags"] = cached(("tags",), TAGS_TTL, fetch_tags)

    # NihongoTracker, if this account brought a key. Every step is allowed to
    # come back empty; the column simply does not appear then.
    nkey = creds.get("nihongo_key")
    extras["has_nihongo_key"] = bool(nkey)
    if nkey:
        def fetch_nihongo():
            who = w.nihongo_whoami(nkey)
            if not who:
                return None
            index = w.nihongo_index(who, nkey)
            return {
                "progress": w.nihongo_progress([d for d, _ in decks], nkey,
                                               who, index) or {},
                "totals": w.nihongo_totals(nkey, who),
                # raw_decks rather than the analysed ones: finished titles are
                # on a list too, so logging them is not "nothing measures this".
                "unmeasured": w.nihongo_unmeasured(index, raw_decks, key)}

        # Keyed on which titles are on the lists, so pressing a track button
        # shows up at once rather than after the timeout - it is the change
        # that would make a stale answer look broken.
        listed = tuple(sorted(row["deckId"] for row in raw_decks))
        nt = cached(("nihongo", user["id"], listed), NIHONGO_TTL, fetch_nihongo)
        if nt:
            extras["nihongo"] = nt["progress"]
            extras["nihongo_totals"] = nt["totals"]
            extras["nihongo_unmeasured"] = nt["unmeasured"]

    return render.dashboard(user, cache, known, decks,
                            store.get_history(user["id"]), extras)


@app.get("/together")
def together():
    user = require_login()
    people = store.sharing_users()
    rows = store.everyones_lists()

    by_user: dict[int, dict[str, list]] = {}
    for r in rows:
        by_user.setdefault(r["user_id"], {}).setdefault(r["status"], []).append(r)

    # A title more than one person has is the interesting bit.
    seen: dict[int, dict] = {}
    for r in rows:
        entry = seen.setdefault(r["deck_id"], {"title": r["title"], "who": []})
        if r["username"] not in entry["who"]:
            entry["who"].append(r["username"])
    overlap = {d: v for d, v in seen.items() if len(v["who"]) > 1}

    sharing_ids = {p["id"] for p in people}
    absent = [u["username"] for u in store.all_usernames()
              if u["id"] not in sharing_ids]

    vis = store.get_visibility(user["id"])
    token = store.share_token(user["id"]) if vis in ("link", "public") else ""
    return render.together_page(user, vis, token, request.url_root.rstrip("/"),
                                people, by_user, overlap, absent,
                                store.shares_stats(user["id"]),
                                store.get_profile(user["id"]),
                                bool(creds_of(user).get("jiten_key")),
                                request.args.get("note", ""),
                                {p["id"]: store.currently_of(p["id"])
                                 for p in people})


@app.post("/together/share")
def set_sharing():
    user = require_login()
    level = request.form.get("visibility", "private")
    store.set_visibility(user["id"], level)
    store.set_share_stats(user["id"], request.form.get("stats") == "1")
    # Publish straight away rather than making them reload the dashboard first.
    if level != "private":
        key = creds_of(user).get("jiten_key")
        if key:
            _ids, _status, everything, _raw = user_decks(key)
            if everything:
                store.put_shared_lists(user["id"], everything)
    return redirect(url_for("together"))


@app.post("/profile")
def save_profile():
    user = require_login()
    store.set_bio(user["id"], request.form.get("bio", ""))
    store.set_currently(user["id"], request.form.get("currently") or None)
    problems = []
    for kind in ("avatar", "banner"):
        if request.form.get(f"clear_{kind}") == "1":
            store.set_image(user["id"], kind, None)
            continue
        upload = request.files.get(kind)
        if upload and upload.filename:
            err = store.set_image(user["id"], kind, upload.read())
            if err:
                problems.append(f"{kind}: {err}")
    note = "; ".join(problems) if problems else "Profile saved."
    return redirect(url_for("together", note=note))


@app.get("/icon.png")
@app.get("/favicon.ico")
def icon():
    """The site logo. Shipped with the code, so it can be cached hard."""
    try:
        with open(w.ICON_FILE, "rb") as f:
            data = f.read()
    except OSError:
        abort(404)
    return Response(data, mimetype="image/png", headers={
        "Cache-Control": "public, max-age=604800"})


@app.get("/media/<kind>/<int:user_id>")
def media(kind, user_id):
    """User-supplied images, served with the type read from their own bytes and
    sniffing switched off, so nothing here can be coaxed into running."""
    data, mime = store.get_image(user_id, kind)
    if not data:
        abort(404)
    return Response(data, mimetype=mime, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "public, max-age=300"})


@app.post("/together/newlink")
def new_share_link():
    user = require_login()
    store.share_token(user["id"], regenerate=True)
    return redirect(url_for("together"))


def _public_view(owner):
    profile = store.get_profile(owner["id"])
    stats = None
    if store.shares_stats(owner["id"]):
        snap = store.get_snapshot(owner["id"])
        if snap:
            known = w.wk_known(snap)
            stats = {"level": snap.get("level"),
                     "kanji": len(known["kanji_known"]),
                     "words": len(known["words_known_set"]),
                     "pace": round(w.wk_pace(snap) or 0, 1),
                     "as_of": (snap.get("fetched_at") or "")[:10]}
    return render.public_profile(owner, profile, stats,
                                 store.lists_of(owner["id"]),
                                 request.url_root.rstrip("/"),
                                 store.currently_of(owner["id"]),
                                 viewer=current_user())


@app.get("/s/<token>")
def shared_link(token):
    """Deliberately open: the whole point is that it works without an account."""
    owner = store.user_by_token(token)
    if not owner:
        return Response("This link is not active.", status=404,
                        mimetype="text/plain")
    return _public_view(owner)


@app.get("/u/<username>")
def public_profile(username):
    owner = store.user_by_public_name(username)
    if not owner:
        return Response("No public profile here.", status=404,
                        mimetype="text/plain")
    return _public_view(owner)


# ---------------------------------------------------- per-user Jiten access

@app.get("/words/<int:deck_id>")
def words(deck_id):
    user = require_login()
    key = creds_of(user).get("jiten_key")
    try:
        deck = w.jiten_deck_detail(deck_id, key)
        counts = deck_words_shared(deck_id, key, deck)
    except SystemExit as e:
        return Response(str(e), status=502, mimetype="text/plain")
    return jsonify(dict(counts))


# Jiten's kanji lookup needs no key, and it is the one thing the grid asks the
# server for. The local tool answers it by proxying anything under /api/ to
# Jiten, which is fine on a server only you can reach - here that would be an
# open relay with somebody's account behind it, so this is the one endpoint and
# it takes one character.
_kanji_words: dict[str, dict] = {}


@app.get("/api/kanji/<ch>")
def kanji_words(ch: str):
    """Common words containing a kanji, for the panel under the grid."""
    require_login()
    if len(ch) != 1 or not w.KANJI_RE.fullmatch(ch):
        abort(404)
    hit = _kanji_words.get(ch)
    if hit is None:
        try:
            raw = w.get_json(f"{w.JITEN_API}/api/kanji/"
                             f"{urllib.parse.quote(ch)}")
        except SystemExit:
            raw = {}
        # Jiten sends 17kB, 13kB of which is a breakdown by reading that the
        # page never looks at. There are only ~2,100 kanji, so what is left is
        # small enough to keep for everyone on the instance rather than ask
        # again per account.
        hit = {"topWords": [{"reading": t.get("reading"),
                             "mainDefinition": t.get("mainDefinition")}
                            for t in (raw.get("topWords") or [])[:12]]}
        _kanji_words[ch] = hit
    return jsonify(hit)


@app.get("/gap/<int:deck_id>")
def gap(deck_id):
    """The words in a title this account has not learned, most frequent first."""
    user = require_login()
    key = creds_of(user).get("jiten_key")
    if not key:
        return jsonify({"error": "This needs a Jiten API key - it is your "
                                 "account that knows which words you have "
                                 "learned."})
    try:
        target = max(1, min(100, int(request.args.get("target", 95))))
    except ValueError:
        target = 95
    try:
        raw = w.jiten_gap_csv(deck_id, key, target)
    except SystemExit as e:
        return jsonify({"error": str(e)[:200]})
    rows, total = w.gap_rows(raw, limit=40)
    return jsonify({"rows": rows, "total": total})


@app.get("/gap/<int:deck_id>/csv")
def gap_csv(deck_id):
    user = require_login()
    key = creds_of(user).get("jiten_key")
    if not key:
        return Response("no Jiten key on this account", status=403,
                        mimetype="text/plain")
    try:
        target = max(1, min(100, int(request.args.get("target", 95))))
    except ValueError:
        target = 95
    try:
        raw = w.jiten_gap_csv(deck_id, key, target)
    except SystemExit as e:
        return Response(str(e), status=502, mimetype="text/plain")
    return Response(raw, mimetype="text/csv", headers={
        "Content-Disposition":
            f'attachment; filename="gap {deck_id} {target}.csv"'})


@app.get("/subs/<int:entry_id>")
def subs_list(entry_id):
    """That title's subtitle files, Chinese-only ones left out."""
    user = require_login()
    key = creds_of(user).get("jimaku_key")
    if not key:
        return Response("no jimaku key on this account", status=403,
                        mimetype="text/plain")
    dual = request.args.get("dual") == "1"
    rows = w.jimaku_files(entry_id, key)
    keep = w.wanted_subtitles(rows, allow_dual=dual)
    return jsonify({
        "files": [{"name": r["name"], "url": r["url"], "size": r.get("size"),
                   "lang": r["lang"]} for r in keep],
        "skipped": sum(1 for r in rows if r not in keep),
        "total": len(rows),
        "onlyDual": bool(keep) and all(r["lang"] == "dual" for r in keep),
    })


@app.get("/subs/<int:entry_id>/zip")
def subs_zip(entry_id):
    user = require_login()
    key = creds_of(user).get("jimaku_key")
    if not key:
        return Response("no jimaku key on this account", status=403,
                        mimetype="text/plain")
    dual = request.args.get("dual") == "1"
    keep = w.wanted_subtitles(w.jimaku_files(entry_id, key), allow_dual=dual)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in keep:
            status, data, _ = w.http(r["url"], timeout=120)
            if status < 400:
                z.writestr(r["name"], data)
    return Response(buf.getvalue(), mimetype="application/zip", headers={
        "Content-Disposition": f"attachment; filename=jimaku-{entry_id}.zip"})


@app.route("/api/<path:rest>", methods=["GET", "POST"])
def proxy(rest):
    """Relay for the page's own calls, signed with this user's key.

    Deliberately narrow: only /api/ paths, only this account's key, and never
    someone else's. api.jiten.moe sends no CORS headers, so the browser cannot
    reach it directly.
    """
    user = require_login()
    key = creds_of(user).get("jiten_key")
    if not key:
        return Response("no Jiten key on this account", status=403,
                        mimetype="text/plain")
    url = f"{w.JITEN_API}/api/{rest}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    body = request.get_data() if request.method == "POST" else None
    status, payload, headers = w.http(
        url, method=request.method, headers=w.jiten_headers(key), body=body,
        content_type="application/json" if body else None, timeout=180)
    return Response(payload, status=status,
                    mimetype=headers.get("Content-Type", "application/json"))


# -------------------------------------------------------------------- admin

@app.get("/invites")
def invites():
    user = require_login()
    if not user["is_admin"]:
        abort(403)
    return render.invites_page(user, store.list_invites(),
                               request.url_root.rstrip("/"))


@app.post("/invites")
def make_invite():
    user = require_login()
    if not user["is_admin"]:
        abort(403)
    store.create_invite(user["id"])
    return redirect(url_for("invites"))


@app.get("/healthz")
def healthz():
    return {"ok": True, "users": store.user_count(),
            "cached_decks": store.deck_cache_size()}


def bootstrap() -> None:
    """First run: make sure there is a way in."""
    store.init()
    if store.user_count() == 0 and not store.list_invites():
        code = store.create_invite(None)
        print("\n" + "=" * 62, flush=True)
        print("  No accounts yet. Create the first one (it becomes admin) at:",
              flush=True)
        print(f"    /register/{code}", flush=True)
        print("=" * 62 + "\n", flush=True)


bootstrap()

def lan_address() -> str | None:
    """This machine's address on the local network.

    Opens a UDP socket towards a public address to see which interface the
    routing table would pick. Nothing is sent, and it works offline.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    host = os.environ.get("WKJITEN_HOST", "127.0.0.1")
    port = int(os.environ.get("WKJITEN_PORT", "8080"))
    if host not in ("127.0.0.1", "localhost"):
        ip = lan_address()
        print("\n" + "=" * 62, flush=True)
        print("  Reachable from other machines on this network at:", flush=True)
        print(f"    http://{ip or 'this-machine'}:{port}/", flush=True)
        print("", flush=True)
        print("  This is plain HTTP, so the password travels unencrypted over", flush=True)
        print("  your network. Fine at home; do not do it on shared wifi, and", flush=True)
        print("  never forward this port on your router.", flush=True)
        print("=" * 62 + "\n", flush=True)
    app.run(host=host, port=port,
            debug=os.environ.get("WKJITEN_DEBUG") == "1")
