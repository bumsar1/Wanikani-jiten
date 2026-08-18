"""Storage for the hosted version: users, encrypted credentials, caches.

SQLite throughout. The deck word lists are shared between users - they are a
property of the title, not of the account - which is what makes the second
person to look at a series get an instant answer. Everything else is scoped to
a user id.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.environ.get("WKJITEN_DB", "/data/wkjiten.sqlite3")
SECRET_PATH = os.environ.get("WKJITEN_SECRET_FILE", "/data/secret.key")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  used_by INTEGER,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS creds (
  user_id INTEGER PRIMARY KEY,
  wk_token BLOB,
  jiten_key BLOB,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  user_id INTEGER NOT NULL,
  slot TEXT NOT NULL,          -- 'current' or 'previous'
  fetched_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (user_id, slot)
);
CREATE TABLE IF NOT EXISTS history (
  user_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  deck_id INTEGER NOT NULL,
  title TEXT,
  wk_level INTEGER,
  kanji_known INTEGER,
  words_known INTEGER,
  kanji_cov REAL,
  jiten_cov REAL,
  PRIMARY KEY (user_id, day, deck_id)
);
-- What each account is watching or reading, captured when they load their
-- dashboard. Stored so the shared page never has to use one person's API key
-- to answer someone else's request.
CREATE TABLE IF NOT EXISTS shared_lists (
  user_id INTEGER NOT NULL,
  deck_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  media_type INTEGER,
  chars INTEGER,
  coverage REAL,
  kanji_cov REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, deck_id)
);
-- Shared: a deck's word list is the same for everyone.
CREATE TABLE IF NOT EXISTS decks (
  deck_id INTEGER PRIMARY KEY,
  last_update TEXT,
  fetched_at TEXT NOT NULL,
  words TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- encryption

def _fernet() -> Fernet:
    """Key from the environment if set, otherwise a file next to the database.

    Losing this key means every stored API key becomes unreadable, which is
    recoverable - users just re-enter them - but worth backing up.
    """
    key = os.environ.get("WKJITEN_SECRET")
    if not key:
        if os.path.exists(SECRET_PATH):
            key = open(SECRET_PATH, encoding="utf-8").read().strip()
        else:
            key = Fernet.generate_key().decode()
            os.makedirs(os.path.dirname(SECRET_PATH) or ".", exist_ok=True)
            with open(SECRET_PATH, "w", encoding="utf-8") as f:
                f.write(key)
            os.chmod(SECRET_PATH, 0o600)
    if isinstance(key, str):
        key = key.encode()
    # Accept a raw passphrase too, so people can set anything in the env var.
    if len(key) != 44:
        key = base64.urlsafe_b64encode(__import__("hashlib").sha256(key).digest())
    return Fernet(key)


def encrypt(value: str | None) -> bytes | None:
    return _fernet().encrypt(value.encode()) if value else None


def decrypt(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob).decode()
    except InvalidToken:
        return None


# ------------------------------------------------------------------ database

@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with db() as con:
        con.executescript(SCHEMA)
        # Added after the first release; existing databases need the column.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "share_lists" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN share_lists"
                        " INTEGER NOT NULL DEFAULT 0")
        if "visibility" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN visibility TEXT"
                        " NOT NULL DEFAULT 'private'")
            # Anyone already sharing with the instance keeps exactly that.
            con.execute("UPDATE users SET visibility = 'instance'"
                        " WHERE share_lists = 1")
        if "share_token" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN share_token TEXT")
        if "share_stats" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN share_stats"
                        " INTEGER NOT NULL DEFAULT 0")
        scols = {r["name"] for r in con.execute("PRAGMA table_info(shared_lists)")}
        if "kanji_cov" not in scols:
            con.execute("ALTER TABLE shared_lists ADD COLUMN kanji_cov REAL")
        for col, decl in (("bio", "TEXT"), ("currently", "INTEGER"),
                          ("avatar", "BLOB"),
                          ("avatar_type", "TEXT"), ("banner", "BLOB"),
                          ("banner_type", "TEXT")):
            if col not in cols:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        ccols = {r["name"] for r in con.execute("PRAGMA table_info(creds)")}
        for col in ("jimaku_key", "nihongo_key"):
            if col not in ccols:
                con.execute(f"ALTER TABLE creds ADD COLUMN {col} BLOB")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------- users

def user_count() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def create_user(username: str, password: str, is_admin: bool = False) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO users (username, pw_hash, is_admin, created_at)"
            " VALUES (?,?,?,?)",
            (username.strip(), generate_password_hash(password),
             1 if is_admin else 0, now()))
        return cur.lastrowid


def check_login(username: str, password: str):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE username = ?",
                          (username.strip(),)).fetchone()
    if row and check_password_hash(row["pw_hash"], password):
        return dict(row)
    return None


def get_user(user_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def set_password(user_id: int, password: str) -> None:
    with db() as con:
        con.execute("UPDATE users SET pw_hash = ? WHERE id = ?",
                    (generate_password_hash(password), user_id))


def delete_user(user_id: int) -> None:
    with db() as con:
        for t in ("creds", "snapshots", "history", "shared_lists"):
            con.execute(f"DELETE FROM {t} WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ------------------------------------------------------------ shared lists

# Each level contains the one before it.
VISIBILITY = ("private", "instance", "link", "public")
VISIBILITY_LABELS = {
    "private": "Just me",
    "instance": "People with an account here",
    "link": "Anyone with the secret link",
    "public": "Anyone at all, at a permanent address",
}


def set_visibility(user_id: int, level: str) -> None:
    """Going private also drops what was stored, so 'off' means gone rather
    than merely hidden."""
    if level not in VISIBILITY:
        level = "private"
    with db() as con:
        con.execute("UPDATE users SET visibility = ?, share_lists = ?"
                    " WHERE id = ?",
                    (level, 0 if level == "private" else 1, user_id))
        if level == "private":
            con.execute("DELETE FROM shared_lists WHERE user_id = ?", (user_id,))


def get_visibility(user_id: int) -> str:
    with db() as con:
        row = con.execute("SELECT visibility FROM users WHERE id = ?",
                          (user_id,)).fetchone()
    return (row["visibility"] if row else "private") or "private"


def is_sharing(user_id: int) -> bool:
    return get_visibility(user_id) != "private"


def set_share_stats(user_id: int, on: bool) -> None:
    with db() as con:
        con.execute("UPDATE users SET share_stats = ? WHERE id = ?",
                    (1 if on else 0, user_id))


def shares_stats(user_id: int) -> bool:
    with db() as con:
        row = con.execute("SELECT share_stats FROM users WHERE id = ?",
                          (user_id,)).fetchone()
    return bool(row and row["share_stats"])


def share_token(user_id: int, regenerate: bool = False) -> str:
    """The unguessable half of a share link. Regenerating breaks old links,
    which is the only way to take one back."""
    with db() as con:
        row = con.execute("SELECT share_token FROM users WHERE id = ?",
                          (user_id,)).fetchone()
        token = row["share_token"] if row else None
        if not token or regenerate:
            token = secrets.token_urlsafe(12)
            con.execute("UPDATE users SET share_token = ? WHERE id = ?",
                        (token, user_id))
    return token


def user_by_token(token: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM users WHERE share_token = ? AND visibility IN"
            " ('link','public')", (token,)).fetchone()
    return dict(row) if row else None


def user_by_public_name(username: str):
    with db() as con:
        row = con.execute(
            "SELECT * FROM users WHERE username = ? AND visibility = 'public'",
            (username,)).fetchone()
    return dict(row) if row else None


def lists_of(user_id: int) -> dict[str, list[dict]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM shared_lists WHERE user_id = ?"
            " ORDER BY status, coverage DESC", (user_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["status"], []).append(dict(r))
    return out


def put_shared_lists(user_id: int, rows: list[dict]) -> None:
    """Replace this account's snapshot wholesale, so titles they removed on
    jiten.moe disappear here too."""
    with db() as con:
        con.execute("DELETE FROM shared_lists WHERE user_id = ?", (user_id,))
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO shared_lists (user_id, deck_id, status,"
                " title, media_type, chars, coverage, kanji_cov, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, r["deck_id"], r["status"], r.get("title"),
                 r.get("media_type"), r.get("chars"), r.get("coverage"),
                 r.get("kanji_cov"), now()))


def everyones_lists() -> list[dict]:
    """Every sharing account's titles, newest snapshot first."""
    with db() as con:
        rows = con.execute(
            "SELECT s.*, u.username FROM shared_lists s"
            " JOIN users u ON u.id = s.user_id"
            " WHERE u.visibility <> 'private'"
            " ORDER BY u.username, s.status, s.coverage DESC").fetchall()
    return [dict(r) for r in rows]


def all_usernames() -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT id, username FROM users ORDER BY username")
        return [dict(r) for r in rows]


def sharing_users() -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT u.id, u.username, u.bio, u.currently,"
            " u.avatar IS NOT NULL AS has_avatar,"
            " u.banner IS NOT NULL AS has_banner,"
            " (SELECT COUNT(*) FROM shared_lists s WHERE s.user_id = u.id) AS titles,"
            " (SELECT MAX(updated_at) FROM shared_lists s WHERE s.user_id = u.id) AS seen"
            " FROM users u WHERE u.visibility <> 'private'"
            " ORDER BY u.username").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- invites

def create_invite(created_by: int | None) -> str:
    code = secrets.token_urlsafe(9)
    with db() as con:
        con.execute("INSERT INTO invites (code, created_by, created_at)"
                    " VALUES (?,?,?)", (code, created_by, now()))
    return code


def invite_open(code: str) -> bool:
    with db() as con:
        row = con.execute("SELECT used_by FROM invites WHERE code = ?",
                          (code,)).fetchone()
    return bool(row) and row["used_by"] is None


def consume_invite(code: str, user_id: int) -> None:
    with db() as con:
        con.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
                    (user_id, now(), code))


def list_invites():
    with db() as con:
        rows = con.execute(
            "SELECT i.*, u.username AS used_by_name FROM invites i"
            " LEFT JOIN users u ON u.id = i.used_by ORDER BY i.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- credentials

def set_creds(user_id: int, wk_token: str | None, jiten_key: str | None,
              jimaku_key: str | None = None,
              nihongo_key: str | None = None) -> None:
    """Keys are only overwritten when a new value is supplied, so a blank field
    on the settings form leaves the stored one alone."""
    current = get_creds(user_id)
    wk = wk_token if wk_token else current.get("wk_token")
    jt = jiten_key if jiten_key else current.get("jiten_key")
    jm = jimaku_key if jimaku_key else current.get("jimaku_key")
    nt = nihongo_key if nihongo_key else current.get("nihongo_key")
    with db() as con:
        con.execute(
            "INSERT INTO creds (user_id, wk_token, jiten_key, jimaku_key,"
            " nihongo_key, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " wk_token = excluded.wk_token, jiten_key = excluded.jiten_key,"
            " jimaku_key = excluded.jimaku_key,"
            " nihongo_key = excluded.nihongo_key,"
            " updated_at = excluded.updated_at",
            (user_id, encrypt(wk), encrypt(jt), encrypt(jm), encrypt(nt), now()))


def clear_jiten_key(user_id: int) -> None:
    with db() as con:
        con.execute("UPDATE creds SET jiten_key = NULL WHERE user_id = ?",
                    (user_id,))


def get_creds(user_id: int) -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM creds WHERE user_id = ?",
                          (user_id,)).fetchone()
    if not row:
        return {}
    cols = row.keys()
    return {"wk_token": decrypt(row["wk_token"]),
            "jiten_key": decrypt(row["jiten_key"]),
            "jimaku_key": decrypt(row["jimaku_key"])
                          if "jimaku_key" in cols else None,
            "nihongo_key": decrypt(row["nihongo_key"])
                           if "nihongo_key" in cols else None,
            "updated_at": row["updated_at"]}


# ----------------------------------------------------------------- snapshots

def save_snapshot(user_id: int, payload: dict) -> None:
    """Keep the outgoing snapshot as 'previous' so progress can be diffed."""
    with db() as con:
        cur = con.execute(
            "SELECT payload, fetched_at FROM snapshots"
            " WHERE user_id = ? AND slot = 'current'", (user_id,)).fetchone()
        if cur:
            con.execute(
                "INSERT INTO snapshots (user_id, slot, fetched_at, payload)"
                " VALUES (?,'previous',?,?) ON CONFLICT(user_id, slot)"
                " DO UPDATE SET fetched_at = excluded.fetched_at,"
                " payload = excluded.payload",
                (user_id, cur["fetched_at"], cur["payload"]))
        con.execute(
            "INSERT INTO snapshots (user_id, slot, fetched_at, payload)"
            " VALUES (?,'current',?,?) ON CONFLICT(user_id, slot)"
            " DO UPDATE SET fetched_at = excluded.fetched_at,"
            " payload = excluded.payload",
            (user_id, payload.get("fetched_at") or now(),
             json.dumps(payload, ensure_ascii=False)))


def mark_baseline(user_id: int) -> bool:
    """Copy the current snapshot over 'previous' so the counters start here.

    The since-last-refresh figures diff current against previous, and every
    refresh rotates one into the other - so a background refresh can quietly
    move the mark and swallow a session. This pins it to right now instead.
    """
    with db() as con:
        cur = con.execute(
            "SELECT payload, fetched_at FROM snapshots"
            " WHERE user_id = ? AND slot = 'current'", (user_id,)).fetchone()
        if not cur:
            return False
        con.execute(
            "INSERT INTO snapshots (user_id, slot, fetched_at, payload)"
            " VALUES (?,'previous',?,?) ON CONFLICT(user_id, slot)"
            " DO UPDATE SET fetched_at = excluded.fetched_at,"
            " payload = excluded.payload",
            (user_id, cur["fetched_at"], cur["payload"]))
    return True


def get_snapshot(user_id: int, slot: str = "current"):
    with db() as con:
        row = con.execute("SELECT payload, fetched_at FROM snapshots"
                          " WHERE user_id = ? AND slot = ?",
                          (user_id, slot)).fetchone()
    return json.loads(row["payload"]) if row else None


def snapshot_age_hours(user_id: int) -> float | None:
    with db() as con:
        row = con.execute("SELECT fetched_at FROM snapshots"
                          " WHERE user_id = ? AND slot = 'current'",
                          (user_id,)).fetchone()
    if not row:
        return None
    try:
        t = time.mktime(time.strptime(row["fetched_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return (time.time() - t) / 3600


# ------------------------------------------------------------- shared decks

def get_deck_words(deck_id: int, last_update: str | None):
    with db() as con:
        row = con.execute("SELECT last_update, words FROM decks WHERE deck_id = ?",
                          (deck_id,)).fetchone()
    if row and (last_update is None or row["last_update"] == last_update):
        return json.loads(row["words"])
    return None


def put_deck_words(deck_id: int, last_update: str | None, words: dict) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO decks (deck_id, last_update, fetched_at, words)"
            " VALUES (?,?,?,?) ON CONFLICT(deck_id) DO UPDATE SET"
            " last_update = excluded.last_update,"
            " fetched_at = excluded.fetched_at, words = excluded.words",
            (deck_id, last_update, now(),
             json.dumps(words, ensure_ascii=False, separators=(",", ":"))))


def deck_cache_size() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) c FROM decks").fetchone()["c"]


# ------------------------------------------------------------------- history

def log_history(user_id: int, day: str, rows: list[dict]) -> None:
    with db() as con:
        for r in rows:
            con.execute(
                "INSERT INTO history (user_id, day, deck_id, title, wk_level,"
                " kanji_known, words_known, kanji_cov, jiten_cov)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(user_id, day, deck_id) DO UPDATE SET"
                " kanji_cov = excluded.kanji_cov, jiten_cov = excluded.jiten_cov,"
                " wk_level = excluded.wk_level, kanji_known = excluded.kanji_known,"
                " words_known = excluded.words_known, title = excluded.title",
                (user_id, day, r["deck_id"], r.get("title"), r.get("wk_level"),
                 r.get("kanji_known"), r.get("words_known"),
                 r.get("kanji_cov"), r.get("jiten_cov")))


def get_history(user_id: int) -> dict[int, list[dict]]:
    with db() as con:
        rows = con.execute(
            "SELECT * FROM history WHERE user_id = ? ORDER BY day", (user_id,)
        ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["deck_id"], []).append(dict(r))
    return out


# ------------------------------------------------------------------ profile

# Only formats a browser renders as an image and nothing else. SVG is absent on
# purpose: it can carry script, and it would be served from this origin.
IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
MAX_IMAGE = 3 * 1024 * 1024


def sniff_image(data: bytes) -> str | None:
    """The declared type is whatever the uploader claimed; this reads the bytes."""
    if not data or len(data) > MAX_IMAGE:
        return None
    for magic, mime in IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def set_image(user_id: int, kind: str, data: bytes | None) -> str | None:
    """kind is 'avatar' or 'banner'. Returns an error message, or None on success."""
    if kind not in ("avatar", "banner"):
        return "Unknown image."
    if not data:
        with db() as con:
            con.execute(f"UPDATE users SET {kind} = NULL, {kind}_type = NULL"
                        " WHERE id = ?", (user_id,))
        return None
    mime = sniff_image(data)
    if not mime:
        return ("That file is not a PNG, JPEG, GIF or WebP under 3 MB."
                if len(data) <= MAX_IMAGE else "That image is over 3 MB.")
    with db() as con:
        con.execute(f"UPDATE users SET {kind} = ?, {kind}_type = ? WHERE id = ?",
                    (data, mime, user_id))
    return None


def get_image(user_id: int, kind: str):
    if kind not in ("avatar", "banner"):
        return None, None
    with db() as con:
        row = con.execute(f"SELECT {kind} AS img, {kind}_type AS mime"
                          " FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["img"]:
        return None, None
    return row["img"], row["mime"]


def has_image(user_id: int, kind: str) -> bool:
    return get_image(user_id, kind)[0] is not None


def set_bio(user_id: int, bio: str) -> None:
    with db() as con:
        con.execute("UPDATE users SET bio = ? WHERE id = ?",
                    (bio.strip()[:160] or None, user_id))


def get_profile(user_id: int) -> dict:
    with db() as con:
        row = con.execute("SELECT username, bio, currently,"
                          " avatar IS NOT NULL AS has_avatar,"
                          " banner IS NOT NULL AS has_banner FROM users"
                          " WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else {}


def set_currently(user_id: int, deck_id) -> None:
    """The one title you are on right now, shown large. None clears it."""
    with db() as con:
        con.execute("UPDATE users SET currently = ? WHERE id = ?",
                    (int(deck_id) if deck_id else None, user_id))


def currently_of(user_id: int):
    """The featured title, but only while it is still on a shared list -
    otherwise removing something from jiten.moe would leave it stranded here."""
    with db() as con:
        row = con.execute(
            "SELECT s.* FROM users u JOIN shared_lists s"
            " ON s.user_id = u.id AND s.deck_id = u.currently"
            " WHERE u.id = ?", (user_id,)).fetchone()
    return dict(row) if row else None
