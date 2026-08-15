"""wkjiten, hosted: the same analysis, several accounts, one shared deck cache.

The command-line tool is imported rather than reimplemented, so the local
`python wkjiten.py serve` flow keeps working exactly as before and both
versions share one definition of what coverage means.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
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
                  MAX_CONTENT_LENGTH=1 << 20)
if os.environ.get("WKJITEN_HTTPS", "").lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True

STALE_HOURS = float(os.environ.get("WKJITEN_STALE_HOURS", "18"))
_refreshing: set[int] = set()
_lock = threading.Lock()


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


def refresh_wanikani(user_id: int, token: str) -> None:
    """Pull the account's subjects and assignments and store the snapshot.

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
    finally:
        with _lock:
            _refreshing.discard(user_id)


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


def user_decks(key: str | None):
    """The titles on this account's jiten.moe lists."""
    if not key:
        return [], {}
    ids, status = [], {}
    for st in ("ongoing", "planning"):
        try:
            for row in w.jiten_status_decks(st, key):
                if row["deckId"] not in status:
                    status[row["deckId"]] = st
                    ids.append(row["deckId"])
        except SystemExit:
            continue
    return ids, status


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
                                note=request.args.get("note", ""))


@app.post("/settings")
def save_settings():
    user = require_login()
    store.set_creds(user["id"],
                    request.form.get("wk_token", "").strip() or None,
                    request.form.get("jiten_key", "").strip() or None)
    creds = creds_of(user)
    if creds.get("wk_token") and not store.get_snapshot(user["id"]):
        threading.Thread(target=refresh_wanikani,
                         args=(user["id"], creds["wk_token"]), daemon=True).start()
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
                     args=(user["id"], creds["wk_token"]), daemon=True).start()
    return redirect(url_for("settings", note="Refreshing in the background."))


@app.get("/")
def dashboard():
    user = require_login()
    creds = creds_of(user)
    if not creds.get("wk_token"):
        return redirect(url_for("settings",
                                note="Add your WaniKani token to get started."))

    cache = store.get_snapshot(user["id"])
    if not cache:
        # First visit: fetch inline, or wait out the one already running.
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
        threading.Thread(target=refresh_wanikani,
                         args=(user["id"], creds["wk_token"]), daemon=True).start()

    known = w.wk_known(cache)
    key = creds.get("jiten_key")
    ids, status = user_decks(key)

    decks = []
    for deck_id in ids[:40]:
        try:
            deck = w.jiten_deck_detail(deck_id, key)
            words = deck_words_shared(deck_id, key, deck)
        except SystemExit:
            continue
        decks.append((deck, w.analyse_deck(words, known)))

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
    extras = {"status": status, "tags": [], "moved_up": set()}
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
        try:
            extras["tags"] = w.get_json(f"{w.JITEN_API}/api/media-deck/tags",
                                        headers=w.jiten_headers(key))
        except SystemExit:
            pass

    return render.dashboard(user, cache, known, decks,
                            store.get_history(user["id"]), extras)


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

if __name__ == "__main__":
    app.run(host=os.environ.get("WKJITEN_HOST", "127.0.0.1"),
            port=int(os.environ.get("WKJITEN_PORT", "8080")),
            debug=os.environ.get("WKJITEN_DEBUG") == "1")
