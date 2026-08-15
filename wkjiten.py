#!/usr/bin/env python3
"""wkjiten - see your WaniKani knowledge as coverage on jiten.moe decks.

Two things this does:

  1. `export` / `push`  - turn your WaniKani vocabulary into a word list that
     Jiten accepts as "known words", so jiten.moe's own coverage column,
     filters and sorting reflect what WaniKani has taught you.

  2. `deck` / `batch`   - a local, kanji-first coverage report per deck
     (the wanilog read-check angle): how many kanji occurrences in a title
     you can already read, and which WaniKani level you'd need for 95%/98%.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Iterable

WK_API = "https://api.wanikani.com/v2"
WK_REVISION = "20170710"
JITEN_API = "https://api.jiten.moe"

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
WK_CACHE = os.path.join(CACHE_DIR, "wanikani.json")
WK_CACHE_PREV = os.path.join(CACHE_DIR, "wanikani.prev.json")
DECK_CACHE_DIR = os.path.join(CACHE_DIR, "decks")
HISTORY_CSV = os.path.join(CACHE_DIR, "history.csv")

# CJK ideographs (BMP + compat). Excludes 々 and other iteration/marks on
# purpose: they are not kanji you learn on WaniKani.
KANJI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
FURIGANA_RE = re.compile(r"\[[^\]]*\]")

SRS_STAGE_NAMES = {
    1: "Apprentice I", 2: "Apprentice II", 3: "Apprentice III", 4: "Apprentice IV",
    5: "Guru I", 6: "Guru II", 7: "Master", 8: "Enlightened", 9: "Burned",
}


# --------------------------------------------------------------------------
# tiny HTTP helper
# --------------------------------------------------------------------------

def http(url: str, *, method: str = "GET", headers: dict | None = None,
         body: bytes | None = None, content_type: str | None = None,
         timeout: int = 120, retries: int = 4) -> tuple[int, bytes, dict]:
    hdrs = {"User-Agent": "wkjiten/1.0 (+personal use)"}
    if headers:
        hdrs.update(headers)
    if content_type:
        hdrs["Content-Type"] = content_type

    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            payload = e.read()
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After") or 5)
                print(f"  rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            return e.code, payload, dict(e.headers or {})
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"network error for {url}: {e}")
    raise SystemExit(f"gave up on {url}")


def get_json(url: str, **kw) -> Any:
    status, body, _ = http(url, **kw)
    if status >= 400:
        raise SystemExit(f"HTTP {status} from {url}\n{body[:500].decode('utf-8', 'replace')}")
    return json.loads(body.decode("utf-8"))


# --------------------------------------------------------------------------
# WaniKani
# --------------------------------------------------------------------------

def wk_token(cli_token: str | None) -> str:
    token = cli_token or os.environ.get("WANIKANI_TOKEN")
    if not token:
        token_file = os.path.join(HERE, "wanikani_token.txt")
        if os.path.exists(token_file):
            token = open(token_file, encoding="utf-8").read().strip()
    if not token:
        raise SystemExit(
            "No WaniKani token. Create a read-only personal access token at\n"
            "  https://www.wanikani.com/settings/personal_access_tokens\n"
            "then either set WANIKANI_TOKEN, pass --wk-token, or save it in\n"
            f"  {os.path.join(HERE, 'wanikani_token.txt')}"
        )
    return token


def wk_paged(path: str, token: str) -> Iterable[dict]:
    """Walk a WaniKani collection, yielding each resource."""
    url = f"{WK_API}/{path}"
    headers = {"Authorization": f"Bearer {token}", "Wanikani-Revision": WK_REVISION}
    page = 0
    while url:
        page += 1
        print(f"  wanikani: {path.split('?')[0]} page {page}...", file=sys.stderr)
        data = get_json(url, headers=headers)
        for item in data.get("data", []):
            yield item
        url = (data.get("pages") or {}).get("next_url")


def wk_fetch(token: str) -> dict:
    """Fetch everything we need from WaniKani and shape it for the cache."""
    user = get_json(
        f"{WK_API}/user",
        headers={"Authorization": f"Bearer {token}", "Wanikani-Revision": WK_REVISION},
    )["data"]

    subjects: dict[int, dict] = {}
    for s in wk_paged("subjects?types=kanji,vocabulary,kana_vocabulary", token):
        d = s["data"]
        chars = d.get("characters")
        if not chars:
            continue  # radicals with image-only characters; none for these types normally
        # Keep the readings WaniKani actually quizzes you on, primary first,
        # so a leech list can show what you are supposed to be recalling.
        readings = [r["reading"] for r in (d.get("readings") or [])
                    if r.get("primary")]
        readings += [r["reading"] for r in (d.get("readings") or [])
                     if r.get("accepted_answer") and not r.get("primary")]
        meaning = next((m["meaning"] for m in (d.get("meanings") or [])
                        if m.get("primary")), "")
        subjects[s["id"]] = {
            "type": s["object"],           # kanji | vocabulary | kana_vocabulary
            "characters": chars,
            "level": d["level"],
            "readings": readings[:3],
            "meaning": meaning,
        }

    assignments: dict[int, int] = {}       # subject_id -> srs_stage
    for a in wk_paged("assignments?started=true", token):
        assignments[a["data"]["subject_id"]] = a["data"]["srs_stage"]

    progressions = [
        {"level": p["data"]["level"], "started_at": p["data"].get("started_at"),
         "passed_at": p["data"].get("passed_at")}
        for p in wk_paged("level_progressions", token)
    ]

    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "username": user.get("username"),
        "level": user.get("level"),
        "subjects": {str(k): v for k, v in subjects.items()},
        "assignments": {str(k): v for k, v in assignments.items()},
        "progressions": progressions,
    }


def wk_pace(cache: dict, recent: int = 6) -> float | None:
    """Median days per level over your most recent levels.

    The lifetime average is useless if you ever took a break - one gap drags it
    into the hundreds. The median of the last few levels is what you are
    actually doing now.
    """
    days = []
    for p in sorted(cache.get("progressions") or [], key=lambda p: p["level"]):
        if not (p.get("started_at") and p.get("passed_at")):
            continue
        try:
            s = time.mktime(time.strptime(p["started_at"][:10], "%Y-%m-%d"))
            e = time.mktime(time.strptime(p["passed_at"][:10], "%Y-%m-%d"))
        except (ValueError, TypeError):
            continue
        days.append((e - s) / 86400)
    if not days:
        return None
    tail = sorted(days[-recent:])
    mid = len(tail) // 2
    return tail[mid] if len(tail) % 2 else (tail[mid - 1] + tail[mid]) / 2


def wk_load(token_arg: str | None, refresh: bool = False) -> dict:
    if not refresh and os.path.exists(WK_CACHE):
        with open(WK_CACHE, encoding="utf-8") as f:
            return json.load(f)
    data = wk_fetch(wk_token(token_arg))
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Keep the previous snapshot so `status` can say what you learned since.
    if os.path.exists(WK_CACHE):
        with open(WK_CACHE, encoding="utf-8") as f:
            old = f.read()
        with open(WK_CACHE_PREV, "w", encoding="utf-8") as f:
            f.write(old)
    with open(WK_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"  cached -> {WK_CACHE}", file=sys.stderr)
    return data


def wk_known(cache: dict, *, min_stage: int = 5, mode: str = "srs",
             level: int | None = None) -> dict:
    """Split WaniKani subjects into what you know and what you don't.

    mode="srs"   - a subject counts as known once its SRS stage reaches
                   --min-stage (5 = Guru I, i.e. WaniKani's own "passed").
    mode="level" - everything at or below a level counts as known, ignoring
                   SRS state. Closer to what wanilog's read-check assumes.
    """
    subjects = cache["subjects"]
    assignments = cache["assignments"]
    cutoff = level if level is not None else cache.get("level") or 1

    kanji_known: set[str] = set()
    kanji_level: dict[str, int] = {}
    words_known: list[str] = []

    for sid, s in subjects.items():
        chars, typ, lv = s["characters"], s["type"], s["level"]
        if typ == "kanji":
            kanji_level[chars] = lv

        if mode == "level":
            is_known = lv <= cutoff
        else:
            is_known = assignments.get(sid, 0) >= min_stage

        if not is_known:
            continue
        if typ == "kanji":
            kanji_known.add(chars)
        else:
            words_known.append(chars)
            # WaniKani writes counters and affixes with a tilde (〜人, 〜ヶ月).
            # JMdict headwords have none, so keep the bare form as well or
            # neither Jiten nor the local match will ever hit it.
            if "〜" in chars or "～" in chars:
                bare = chars.strip("〜～")
                if bare:
                    words_known.append(bare)

    return {
        "kanji_known": kanji_known,
        "kanji_level": kanji_level,
        "words_known": words_known,
        "words_known_set": set(words_known),
        "user_level": cache.get("level"),
        "username": cache.get("username"),
        "mode": mode,
        "min_stage": min_stage,
    }


# --------------------------------------------------------------------------
# Jiten
# --------------------------------------------------------------------------

def jiten_headers(api_key: str | None) -> dict:
    return {"X-Api-Key": api_key} if api_key else {}


def jiten_key(cli_key: str | None) -> str | None:
    key = cli_key or os.environ.get("JITEN_API_KEY")
    if not key:
        key_file = os.path.join(HERE, "jiten_key.txt")
        if os.path.exists(key_file):
            key = open(key_file, encoding="utf-8").read().strip()
    return key


def jiten_deck_detail(deck_id: int, api_key: str | None) -> dict:
    body = get_json(f"{JITEN_API}/api/media-deck/{deck_id}/detail",
                    headers=jiten_headers(api_key))
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, list):
        data = data[0] if data else {}
    deck = data.get("mainDeck") or data.get("deck") or data
    deck["_subDeckCount"] = len(data.get("subDecks") or [])
    return deck


def jiten_deck_tokens(deck_id: int, api_key: str | None) -> list[str]:
    """Every word occurrence in the deck, repeated by how often it appears.

    format 4 = TxtRepeated, downloadType 1 = Full, order 3 = DeckFrequency.
    One request per deck instead of paging /vocabulary 200 at a time.
    """
    status, body, _ = http(
        f"{JITEN_API}/api/media-deck/{deck_id}/download",
        method="POST",
        headers=jiten_headers(api_key),
        body=json.dumps({"format": 4, "downloadType": 1, "order": 3}).encode(),
        content_type="application/json",
        timeout=180,
    )
    if status >= 400:
        raise SystemExit(f"jiten download failed ({status}): "
                         f"{body[:300].decode('utf-8', 'replace')}")
    text = body.decode("utf-8")
    return [FURIGANA_RE.sub("", line).strip() for line in text.splitlines() if line.strip()]


def deck_words(deck_id: int, api_key: str | None, deck: dict | None = None,
               force: bool = False) -> Counter:
    """Word -> occurrences for a deck, cached on disk.

    A deck's word list only changes when Jiten re-parses the title, so the
    cache is keyed on the deck's own lastUpdate stamp. Without this, every
    command that wants occurrence counts would re-download the same decks,
    and these are the rate-limited heavy endpoints.
    """
    os.makedirs(DECK_CACHE_DIR, exist_ok=True)
    path = os.path.join(DECK_CACHE_DIR, f"{deck_id}.json")
    stamp = (deck or {}).get("lastUpdate")
    if not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if stamp is None or cached.get("lastUpdate") == stamp:
                return Counter(cached["words"])
        except (ValueError, KeyError):
            pass  # corrupt cache, just refetch

    counts = Counter(jiten_deck_tokens(deck_id, api_key))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"lastUpdate": stamp, "words": dict(counts)}, f, ensure_ascii=False)
    return counts


def jiten_search(query: str, api_key: str | None, limit: int = 15, *,
                 media_type: int | None = None, genres: str | None = None,
                 min_chars: int | None = None, sort_by: str = "wordCount",
                 descending: bool = True) -> list[dict]:
    url = (f"{JITEN_API}/api/media-deck/get-media-decks"
           f"?titleFilter={urllib.parse.quote(query)}"
           f"&sortBy={sort_by}&sortOrder={1 if descending else 0}")
    if media_type:
        url += f"&mediaType={media_type}"
    if genres:
        url += f"&genres={urllib.parse.quote(genres)}"
    if min_chars:
        url += f"&charCountMin={min_chars}"
    data = get_json(url, headers=jiten_headers(api_key))
    rows = data.get("data") if isinstance(data, dict) else data
    return (rows or [])[:limit]


def media_type_id(name: str | None) -> int | None:
    if not name:
        return None
    name = name.lower().strip()
    for num, label in MEDIA_TYPES.items():
        if label.startswith(name) or name == label:
            return num
    raise SystemExit(f"unknown type {name!r}; pick from: "
                     + ", ".join(sorted(MEDIA_TYPES.values())))


MEDIA_TYPES = {
    1: "anime", 2: "drama", 3: "movie", 4: "novel", 5: "non-fiction",
    6: "video game", 7: "visual novel", 8: "web novel", 9: "manga", 10: "audio",
}


# --------------------------------------------------------------------------
# coverage maths
# --------------------------------------------------------------------------

def analyse_deck(tokens, known: dict) -> dict:
    """tokens may be a list of word occurrences or an already-counted Counter."""
    kanji_known = known["kanji_known"]
    kanji_level = known["kanji_level"]
    words_known = known["words_known_set"]

    word_occ = tokens if isinstance(tokens, Counter) else Counter(tokens)
    kanji_occ: Counter[str] = Counter()
    for token, n in word_occ.items():
        for ch in KANJI_RE.findall(token):
            kanji_occ[ch] += n

    total_kanji_occ = sum(kanji_occ.values())
    known_kanji_occ = sum(n for ch, n in kanji_occ.items() if ch in kanji_known)
    unique_kanji = set(kanji_occ)
    known_unique_kanji = unique_kanji & kanji_known

    total_word_occ = sum(word_occ.values())
    known_word_occ = sum(n for w, n in word_occ.items() if w in words_known)
    known_unique_words = {w for w in word_occ if w in words_known}

    # How kanji coverage grows if you keep levelling: cumulative occurrences
    # unlocked by each WaniKani level, on top of what you already know.
    by_level: Counter[int] = Counter()
    not_in_wk = 0
    for ch, n in kanji_occ.items():
        lv = kanji_level.get(ch)
        if lv is None:
            not_in_wk += n
        else:
            by_level[lv] += n

    curve = []
    cumulative = 0
    for lv in range(1, 61):
        cumulative += by_level.get(lv, 0)
        curve.append((lv, cumulative / total_kanji_occ * 100 if total_kanji_occ else 0.0))

    return {
        "word_occ": word_occ,
        "kanji_occ": kanji_occ,
        "kanji_cov_occ": known_kanji_occ / total_kanji_occ * 100 if total_kanji_occ else 0.0,
        "kanji_cov_unique": len(known_unique_kanji) / len(unique_kanji) * 100 if unique_kanji else 0.0,
        "total_kanji_occ": total_kanji_occ,
        "unique_kanji": len(unique_kanji),
        "unique_kanji_known": len(known_unique_kanji),
        "word_cov_occ": known_word_occ / total_word_occ * 100 if total_word_occ else 0.0,
        "word_cov_unique": len(known_unique_words) / len(word_occ) * 100 if word_occ else 0.0,
        "total_word_occ": total_word_occ,
        "unique_words": len(word_occ),
        "unique_words_known": len(known_unique_words),
        "curve": curve,
        "not_in_wk_occ": not_in_wk,
        "not_in_wk_pct": not_in_wk / total_kanji_occ * 100 if total_kanji_occ else 0.0,
        "kanji_level": kanji_level,
        "kanji_known": kanji_known,
    }


def log_history(rows: list[dict]) -> None:
    """Append one dated row per deck so coverage can be tracked over time."""
    if not rows:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    fields = ["date", "deckId", "title", "wkLevel", "kanjiKnown", "wordsKnown",
              "kanjiCoverage", "jitenCoverage"]
    # One point per deck per day: running twice in an afternoon should update
    # today's figure, not stack a second dot on the chart.
    existing: list[dict] = []
    if os.path.exists(HISTORY_CSV):
        replacing = {(r["date"], str(r["deckId"])) for r in rows}
        with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
            existing = [r for r in csv.DictReader(f)
                        if (r.get("date"), str(r.get("deckId"))) not in replacing]
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in existing + rows:
            w.writerow({k: r.get(k, "") for k in fields})


def read_history() -> dict[int, list[dict]]:
    """deckId -> its rows, oldest first, one per run."""
    if not os.path.exists(HISTORY_CSV):
        return {}
    out: dict[int, list[dict]] = {}
    with open(HISTORY_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.setdefault(int(row["deckId"]), []).append(row)
            except (ValueError, KeyError):
                continue
    return out


def history_trend(rows: list[dict], field: str = "kanjiCoverage"):
    """(first value, latest value, days between) for a deck's history."""
    points = []
    for r in rows:
        try:
            points.append((r["date"], float(r[field])))
        except (ValueError, KeyError, TypeError):
            continue
    if len(points) < 2:
        return None
    first, last = points[0], points[-1]
    try:
        d0 = time.strptime(first[0][:10], "%Y-%m-%d")
        d1 = time.strptime(last[0][:10], "%Y-%m-%d")
        days = round((time.mktime(d1) - time.mktime(d0)) / 86400)
    except ValueError:
        days = 0
    return first[1], last[1], days


def finishing_level(res: dict, level: int | None) -> float | None:
    """Coverage once every kanji up to and including your current level is at
    Guru — i.e. what finishing the level you are on right now buys you.

    Your actual figure sits below this: some items on levels you have passed
    have fallen back to Apprentice, and the level you are on is only part done.
    """
    if not level:
        return None
    return res["curve"][min(60, level) - 1][1]


def level_for(curve: list[tuple[int, float]], target: float) -> int | None:
    for lv, pct in curve:
        if pct >= target:
            return lv
    return None


def pad(s: str, width: int) -> str:
    """Left-justify to a terminal width, counting CJK characters as two cells.

    Python pads by codepoint, so a column of Japanese ragged-edges badly.
    """
    cells = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))
    return str(s) + " " * max(0, width - cells)


def bar(pct: float, width: int = 32) -> str:
    filled = int(round(pct / 100 * width))
    return "#" * filled + "." * (width - filled)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_export(args) -> None:
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)

    words = list(known["words_known"])
    if args.normalize:
        words = [unicodedata.normalize("NFKC", w) for w in words]
    words = sorted(set(words))

    out = args.out or os.path.join(HERE, "wanikani-known-words.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(words) + "\n")

    label = (f"level <= {args.level or cache.get('level')}" if args.mode == "level"
             else f"SRS stage >= {args.min_stage} ({SRS_STAGE_NAMES.get(args.min_stage, '?')})")
    print(f"WaniKani user {cache.get('username')} (level {cache.get('level')}), {label}")
    print(f"  {len(words)} vocabulary words, {len(known['kanji_known'])} kanji known")
    print(f"  wrote {out}")
    print()
    print("Import it on jiten.moe: Settings -> Vocabulary -> import from file")
    print("(the txt/csv importer reads everything before the first tab or comma),")
    print("or run:  python wkjiten.py push")


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----wkjiten7f3a9b2c1d"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def cmd_push(args) -> None:
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit(
            "No Jiten API key. Create one at jiten.moe -> Settings -> Advanced -> API Key\n"
            "(shown once), then set JITEN_API_KEY, pass --jiten-key, or save it in\n"
            f"  {os.path.join(HERE, 'jiten_key.txt')}"
        )
    path = args.file or os.path.join(HERE, "wanikani-known-words.txt")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - run `python wkjiten.py export` first.")

    content = open(path, "rb").read()
    body, ctype = multipart("file", os.path.basename(path), content)
    url = (f"{JITEN_API}/api/user/vocabulary/import-from-anki-txt"
           f"?parseWords={'true' if args.parse_words else 'false'}"
           f"&overwriteExisting={'true' if args.overwrite else 'false'}")

    status, resp, _ = http(url, method="POST", headers=jiten_headers(key),
                           body=body, content_type=ctype, timeout=300)
    text = resp.decode("utf-8", "replace")
    if status in (401, 403):
        raise SystemExit(
            f"Jiten rejected the key for this write endpoint (HTTP {status}).\n"
            "The public API key may be read-only. Import the file by hand instead:\n"
            "  jiten.moe -> Settings -> Vocabulary -> import from file\n"
            f"  {path}"
        )
    if status >= 400:
        raise SystemExit(f"import failed (HTTP {status}): {text[:500]}")

    print(f"imported ({status}): {text[:500]}")
    st, _, _ = http(f"{JITEN_API}/api/user/coverage/refresh", method="POST",
                    headers=jiten_headers(key))
    print(f"coverage refresh queued ({st})")


def cmd_search(args) -> None:
    key = jiten_key(args.jiten_key)
    rows = jiten_search(args.query or "", key, limit=args.limit,
                        media_type=media_type_id(args.type),
                        genres=args.genre, min_chars=args.min_chars,
                        sort_by=args.sort, descending=not args.ascending)
    if not rows:
        print("no decks found")
        return
    print(f"{'id':>8}  {'type':<13} {'chars':>9} {'diff':>5} {'cover':>7}  title")
    for d in rows:
        title = (d.get("originalTitle") or d.get("englishTitle")
                 or d.get("romajiTitle") or "")
        cov = d.get("coverage")
        print(f"{d.get('deckId', 0):>8}  "
              f"{MEDIA_TYPES.get(d.get('mediaType'), '?'):<13} "
              f"{d.get('characterCount') or 0:>9,} {d.get('difficulty') or 0:>5} "
              f"{f'{cov}%' if cov is not None else '-':>7}  {title}")
    if not key:
        print("\n(no Jiten API key, so no coverage column)")
    print(f"\nWorth reading? -> python wkjiten.py when {rows[0].get('deckId')}")


def report(deck: dict, res: dict, known: dict, args) -> None:
    title = (deck.get("originalTitle") or deck.get("englishTitle")
             or deck.get("romajiTitle") or "?")
    alt = deck.get("englishTitle") or deck.get("romajiTitle") or ""
    curve = res["curve"]
    lv = known["user_level"]

    print()
    print("=" * 66)
    print(f"{title}" + (f"  ({alt})" if alt and alt != title else ""))
    print(f"deck {deck.get('deckId')} | {MEDIA_TYPES.get(deck.get('mediaType'), '?')} | "
          f"{deck.get('characterCount') or 0:,} chars | difficulty {deck.get('difficulty') or 0}")
    print("=" * 66)

    print(f"\nKANJI coverage  (WaniKani level {lv}, {len(known['kanji_known'])} kanji known)")
    print(f"  by occurrence  {res['kanji_cov_occ']:6.2f}%  {bar(res['kanji_cov_occ'])}")
    print(f"  unique kanji   {res['kanji_cov_unique']:6.2f}%  "
          f"({res['unique_kanji_known']}/{res['unique_kanji']})")
    fin = finishing_level(res, lv)
    if fin is not None:
        print(f"  finishing level {lv} takes this to {fin:6.2f}%  "
              f"({fin - res['kanji_cov_occ']:+.2f}pp, no new levels needed)")
    print(f"  kanji outside WaniKani entirely: {res['not_in_wk_pct']:.2f}% of occurrences "
          f"-> hard ceiling {100 - res['not_in_wk_pct']:.2f}%")

    live = deck.get("coverage")
    if live is not None:
        # Authenticated: this is Jiten's own figure for the account, and it is
        # the one to trust. It counts redundant writings of a known word and
        # blacklisted word sets (names, places) that the local match cannot see.
        print("\nVOCAB coverage  (jiten.moe, your account)")
        print(f"  by occurrence  {live:6.2f}%  {bar(live)}")
        print(f"  unique words   {deck.get('uniqueCoverage') or 0:6.2f}%")
    else:
        print(f"\nVOCAB coverage  (offline estimate, {len(known['words_known_set'])} "
              f"WaniKani words)")
        print(f"  by occurrence  {res['word_cov_occ']:6.2f}%  {bar(res['word_cov_occ'])}")
        print(f"  unique words   {res['word_cov_unique']:6.2f}%  "
              f"({res['unique_words_known']}/{res['unique_words']})")
        print("  Exact-surface match only, so this reads low. Pass a Jiten API key")
        print("  to get the real number from your account instead.")

    print("\nKANJI coverage by WaniKani level")
    marks = [10, 20, 30, 40, 50, 60]
    if lv and lv not in marks:
        marks = sorted(set(marks + [lv]))
    for m in marks:
        pct = curve[m - 1][1]
        tag = "  <- you" if m == lv else ""
        print(f"  lvl {m:>2}  {pct:6.2f}%  {bar(pct, 28)}{tag}")
    for target in (90, 95, 98, 99):
        need = level_for(curve, target)
        print(f"  {target}% kanji coverage at WaniKani level "
              + (str(need) if need else "never (blocked by non-WaniKani kanji)"))

    top = [(ch, n) for ch, n in res["kanji_occ"].most_common()
           if ch not in res["kanji_known"]][:args.top]
    if top:
        print(f"\nTop {len(top)} unknown kanji (by occurrences)")
        for i in range(0, len(top), 5):
            chunk = top[i:i + 5]
            cells = []
            for ch, n in chunk:
                l = res["kanji_level"].get(ch)
                cells.append(f"{ch} {n:>4}x " + (f"L{l:<2}" if l else "--- "))
            print("   " + "  ".join(cells))


def cmd_deck(args) -> None:
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    key = jiten_key(args.jiten_key)

    for deck, words in load_decks(args.deck_ids, key, args.sleep):
        report(deck, analyse_deck(words, known), known, args)


def deck_title(deck: dict) -> str:
    return (deck.get("originalTitle") or deck.get("englishTitle")
            or deck.get("romajiTitle") or "?")


# Jiten's own list names, as the API spells them.
STATUS_LABELS = {
    "ongoing": "watching/reading", "planning": "plan to watch/read",
    "completed": "completed", "fav": "favourite", "dropped": "dropped",
}

# deckId -> the Jiten list it came from, filled in by tracked_ids.
DECK_STATUS: dict[int, str] = {}


def jiten_status_decks(status: str, key: str) -> list[dict]:
    """Every deck you have put on one of your Jiten lists, all pages of it."""
    out, offset = [], 0
    while True:
        url = (f"{JITEN_API}/api/media-deck/get-media-decks?status={status}"
               f"&offset={offset}")
        data = get_json(url, headers=jiten_headers(key))
        rows = data.get("data") or []
        out.extend(rows)
        offset += data.get("pageSize") or 50
        if not rows or offset >= (data.get("totalItems") or 0):
            return out


def tracked_ids(args, key: str | None) -> list[int]:
    """Which decks to work on.

    Explicit ids or a --search win outright. Otherwise the list comes from your
    own Jiten statuses (what you are actually watching or planning), with
    decks.txt merged in for anything you want to track manually.
    """
    ids: list[int] = list(getattr(args, "deck_ids", None) or [])
    if ids:
        return list(dict.fromkeys(ids))
    if getattr(args, "search", None):
        for row in jiten_search(args.search, key, limit=args.limit):
            ids.append(row["deckId"])
        return list(dict.fromkeys(ids))

    sources: list[str] = []
    statuses = [s.strip() for s in (getattr(args, "status", "") or "").split(",")
                if s.strip()]
    if statuses and not key:
        print("  note: --status needs a Jiten API key; falling back to decks.txt")
        statuses = []
    for status in statuses:
        rows = jiten_status_decks(status, key)
        for row in rows:
            DECK_STATUS.setdefault(row["deckId"], status)
            ids.append(row["deckId"])
        if rows:
            sources.append(f"{len(rows)} {STATUS_LABELS.get(status, status)}")

    deck_file = getattr(args, "deck_file", None) or os.path.join(HERE, "decks.txt")
    if os.path.exists(deck_file):
        before = len(ids)
        with open(deck_file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line.isdigit():
                    ids.append(int(line))
        if len(ids) > before:
            sources.append(f"{len(ids) - before} from decks.txt")

    ids = list(dict.fromkeys(ids))
    if not ids:
        raise SystemExit(
            "No titles to work on. Either set a status on jiten.moe (watching, "
            "plan to watch), list deck ids in decks.txt, or pass ids directly.")
    print(f"Tracking {len(ids)} titles: {', '.join(sources)}")
    return ids


def load_decks(ids: list[int], key: str | None, sleep: float,
               progress: bool = False):
    """Yield (deck, word occurrences) per id, hitting the network only for
    decks that are not cached yet."""
    for i, deck_id in enumerate(ids):
        deck = jiten_deck_detail(deck_id, key)
        cached = os.path.exists(os.path.join(DECK_CACHE_DIR, f"{deck_id}.json"))
        words = deck_words(deck_id, key, deck)
        if progress:
            print(f"[{i+1}/{len(ids)}] {deck_title(deck)}"
                  + ("" if cached else "  (downloaded)"))
        yield deck, words
        if not cached and i < len(ids) - 1:
            time.sleep(sleep)


def cmd_batch(args) -> None:
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    key = jiten_key(args.jiten_key)
    ids = tracked_ids(args, key)

    out = args.out or os.path.join(HERE, "coverage.csv")
    summary: list[tuple] = []
    history: list[dict] = []
    today = time.strftime("%Y-%m-%d")

    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["deckId", "title", "type", "chars", "difficulty",
                    "kanji_cov_occ", "kanji_cov_unique",
                    "jiten_coverage", "jiten_unique_coverage",
                    "vocab_cov_occ_offline", "unique_kanji",
                    "lvl_for_95", "lvl_for_98", "ceiling"])
        for i, (deck, words) in enumerate(load_decks(ids, key, args.sleep)):
            res = analyse_deck(words, known)
            title = deck_title(deck)
            deck_id = deck.get("deckId")
            live = deck.get("coverage")
            w.writerow([
                deck_id, title, MEDIA_TYPES.get(deck.get("mediaType"), "?"),
                deck.get("characterCount") or 0, deck.get("difficulty") or 0,
                f"{res['kanji_cov_occ']:.2f}", f"{res['kanji_cov_unique']:.2f}",
                live if live is not None else "",
                deck.get("uniqueCoverage") if live is not None else "",
                f"{res['word_cov_occ']:.2f}",
                res["unique_kanji"],
                level_for(res["curve"], 95) or "", level_for(res["curve"], 98) or "",
                f"{100 - res['not_in_wk_pct']:.2f}",
            ])
            summary.append((res["kanji_cov_occ"], live, title,
                            MEDIA_TYPES.get(deck.get("mediaType"), "?"),
                            deck.get("characterCount") or 0,
                            level_for(res["curve"], 95),
                            100 - res["not_in_wk_pct"],
                            finishing_level(res, cache.get("level"))))
            history.append({
                "date": today, "deckId": deck_id, "title": title,
                "wkLevel": cache.get("level"),
                "kanjiKnown": len(known["kanji_known"]),
                "wordsKnown": len(known["words_known_set"]),
                "kanjiCoverage": f"{res['kanji_cov_occ']:.2f}",
                "jitenCoverage": f"{live:.2f}" if live is not None else "",
            })
            vocab = f"{live:.2f}% (jiten)" if live is not None else \
                    f"{res['word_cov_occ']:.2f}% (offline)"
            print(f"[{i+1}/{len(ids)}] {title}  kanji {res['kanji_cov_occ']:.2f}%  "
                  f"vocab {vocab}")

    # Crossings are worked out against the stored history *before* today's rows
    # are written, or every title would look like it just crossed.
    previous = read_history()
    crossed = []
    for row in history:
        did = row["deckId"]
        earlier = [r for r in previous.get(did, []) if r.get("date") != row["date"]]
        if not earlier or not row["jitenCoverage"]:
            continue
        try:
            was = float(earlier[-1].get("jitenCoverage") or 0)
        except ValueError:
            continue
        now = float(row["jitenCoverage"])
        if was < args.alert_at <= now:
            crossed.append((row["title"], was, now))

    log_history(history)
    past = read_history()

    lvl = cache.get("level")
    print()
    print(f"{'kanji':>7} {f'finish L{lvl}':>12} {'jiten':>7} {'ceiling':>8} "
          f"{'lvl95':>6} {'trend':>16}  {'list':<18} {'chars':>10}  title")
    for k, live, title, mtype, chars, lvl95, ceiling, fin in sorted(summary,
                                                                   reverse=True):
        j = f"{live:6.2f}%" if live is not None else "     -"
        did = next((h["deckId"] for h in history if h["title"] == title), None)
        trend = history_trend(past.get(did, []))
        t = (f"{trend[1] - trend[0]:+.2f}pp / {trend[2]}d" if trend else "")
        lst = STATUS_LABELS.get(DECK_STATUS.get(did), "")
        f_txt = f"{fin:6.2f}% {fin - k:+5.2f}" if fin is not None else " " * 12
        print(f"{k:6.2f}% {f_txt:>12} {j} {ceiling:7.2f}% {lvl95 or '--':>6} "
              f"{t:>16}  {lst:<18} {chars:>10,}  {title}")
    if lvl:
        gains = [fin - k for k, *_rest, fin in summary if fin is not None]
        if gains:
            print(f"\nFinishing level {lvl} alone is worth "
                  f"{sum(gains) / len(gains):+.2f}pp of kanji coverage on average.")
    for title, was, now in crossed:
        print(f"\n*** {title} just crossed {args.alert_at}% coverage "
              f"({was:.1f}% -> {now:.1f}%). Might be time to start it. ***")
    print(f"\nwrote {out}")


def cmd_leeches(args) -> None:
    """Which WaniKani items you are struggling with actually block your reading.

    WaniKani ranks leeches by how often you fail them; that says nothing about
    whether the item matters for the books you want to read. Crossing SRS stage
    with occurrences in your tracked titles does.
    """
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    key = jiten_key(args.jiten_key)
    ids = tracked_ids(args, key)

    subjects, assignments = cache["subjects"], cache["assignments"]
    if not any("readings" in s for s in subjects.values()):
        print("  note: your cache predates reading support - run with --refresh "
              "to show readings")
    # Stage 1-4 is Apprentice: started, repeatedly seen, not yet passed.
    struggling: dict[str, tuple] = {}   # chars -> (stage, level, readings, meaning)
    for sid, s in subjects.items():
        stage = assignments.get(sid)
        if stage is not None and 1 <= stage <= args.max_stage:
            struggling[s["characters"]] = (stage, s["level"],
                                           "、".join(s.get("readings") or []),
                                           s.get("meaning") or "")

    kanji_occ: Counter[str] = Counter()
    word_occ: Counter[str] = Counter()
    for _deck, words in load_decks(ids, key, args.sleep, progress=True):
        for word, n in words.items():
            word_occ[word] += n
            for ch in KANJI_RE.findall(word):
                kanji_occ[ch] += n

    total_unknown = sum(n for ch, n in kanji_occ.items()
                        if ch not in known["kanji_known"])
    rows = [(n, ch) + struggling[ch] for ch, n in kanji_occ.items()
            if ch in struggling and ch not in known["kanji_known"]]
    rows.sort(reverse=True)

    print()
    print("=" * 70)
    print(f"  Leeches that block your reading  ({len(ids)} tracked titles)")
    print("=" * 70)
    if not rows:
        print("\nNothing in Apprentice appears in your tracked titles. Enjoy it.")
    else:
        blocked = sum(r[0] for r in rows)
        print(f"\nThese {len(rows)} Apprentice kanji account for {blocked:,} of the "
              f"{total_unknown:,} kanji occurrences")
        print(f"you cannot read yet - {blocked / total_unknown * 100:.1f}% of the "
              f"gap, sitting in items you have already unlocked.\n")
        print(f"{'occur':>7}  {pad('kanji', 6)}{pad('reading', 16)}"
              f"{pad('meaning', 15)}{pad('stage', 16)}{'lvl':>4}")
        for n, ch, stage, lv, readings, meaning in rows[:args.top]:
            print(f"{n:>7}  {pad(ch, 6)}{pad(readings, 16)}{pad(meaning[:14], 15)}"
                  f"{pad(SRS_STAGE_NAMES.get(stage, '?'), 16)}{lv:>4}")

    vocab_rows = [(word_occ.get(w, 0), w) + struggling[w] for w in struggling
                  if word_occ.get(w, 0) > 0 and w not in known["words_known_set"]]
    vocab_rows.sort(reverse=True)
    if vocab_rows:
        print("\nVocabulary you are struggling with, same ranking\n")
        print(f"{'occur':>7}  {pad('word', 12)}{pad('reading', 16)}"
              f"{pad('meaning', 15)}{pad('stage', 16)}{'lvl':>4}")
        for n, word, stage, lv, readings, meaning in vocab_rows[:args.top]:
            print(f"{n:>7}  {pad(word, 12)}{pad(readings, 16)}{pad(meaning[:14], 15)}"
                  f"{pad(SRS_STAGE_NAMES.get(stage, '?'), 16)}{lv:>4}")

    print("\nThese are already in your review queue - getting them to Guru is the")
    print("cheapest coverage you will ever buy.")


def jiten_top_by_coverage(media_type: int, api_key: str, *, min_chars: int,
                          limit: int) -> list[dict]:
    """Titles ranked by *your* coverage. Jiten sorts this server-side once the
    API key identifies the account, so one request covers a whole media type."""
    url = (f"{JITEN_API}/api/media-deck/get-media-decks?mediaType={media_type}"
           f"&sortBy=coverage&sortOrder=1&charCountMin={min_chars}")
    data = get_json(url, headers=jiten_headers(api_key))
    return (data.get("data") or [])[:limit]


# media type -> minimum character count worth recommending, so the lists are
# not topped by one-page shorts and single episodes.
RECOMMEND_TYPES = [
    (4, "novels", 30000),
    (7, "visual novels", 50000),
    (1, "anime", 20000),
    (9, "manga", 30000),
    (6, "games", 30000),
]


def progress_since_last(args) -> None:
    if not os.path.exists(WK_CACHE_PREV):
        print("Progress: no earlier snapshot yet - it appears from the next run on.")
        return
    with open(WK_CACHE_PREV, encoding="utf-8") as f:
        prev_cache = json.load(f)
    with open(WK_CACHE, encoding="utf-8") as f:
        cur_cache = json.load(f)

    opts = dict(min_stage=args.min_stage, mode=args.mode, level=args.level)
    prev = wk_known(prev_cache, **opts)
    cur = wk_known(cur_cache, **opts)

    new_kanji = cur["kanji_known"] - prev["kanji_known"]
    new_words = cur["words_known_set"] - prev["words_known_set"]
    lvl_before, lvl_now = prev_cache.get("level"), cur_cache.get("level")

    print(f"Progress since {prev_cache.get('fetched_at', '?')[:10]}")
    print(f"  kanji  {len(prev['kanji_known']):>5} -> {len(cur['kanji_known']):<5} "
          f"({len(new_kanji):+d})")
    print(f"  words  {len(prev['words_known_set']):>5} -> "
          f"{len(cur['words_known_set']):<5} ({len(new_words):+d})")
    if lvl_before != lvl_now:
        print(f"  level  {lvl_before} -> {lvl_now}   nice.")
    if new_kanji:
        shown = sorted(new_kanji, key=lambda c: cur["kanji_level"].get(c, 99))
        print("  new kanji: " + " ".join(shown[:40])
              + (f"  (+{len(shown) - 40} more)" if len(shown) > 40 else ""))


def collect_status(args, key: str, cache: dict, known: dict) -> dict:
    """The data behind `status`, shared with the HTML report."""
    recommendations: list[tuple[str, list[dict]]] = []
    candidates: list[dict] = []
    for mtype, label, min_chars in RECOMMEND_TYPES:
        rows = jiten_top_by_coverage(mtype, key, min_chars=min_chars,
                                     limit=args.top_n + args.soon_pool)
        recommendations.append((label, rows[:args.top_n]))
        candidates.extend(rows[args.top_n:])
        time.sleep(0.5)

    lvl_now = cache.get("level") or 1
    target = min(60, lvl_now + args.soon_levels)
    gains = []
    if args.soon_limit > 0:
        for d in candidates[:args.soon_limit]:
            deck_id = d.get("deckId")
            try:
                words = deck_words(deck_id, key, d)
            except SystemExit as e:
                print(f"  skipped {deck_id}: {e}")
                continue
            res = analyse_deck(words, known)
            now = res["kanji_cov_occ"]
            gains.append((res["curve"][target - 1][1] - now, now,
                          res["curve"][target - 1][1], d))
            time.sleep(args.sleep)
    return {"recommendations": recommendations,
            "gains": sorted(gains, key=lambda g: -g[0]),
            "target_level": target, "candidates": len(candidates)}


def cmd_status(args) -> None:
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit("`status` needs a Jiten API key - see the README.")
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)

    print()
    print("=" * 70)
    print(f"  {cache.get('username')} - WaniKani level {cache.get('level')}, "
          f"{len(known['kanji_known'])} kanji and "
          f"{len(known['words_known_set'])} words")
    print("=" * 70)
    print()
    progress_since_last(args)

    data = collect_status(args, key, cache, known)

    print()
    print("-" * 70)
    print("  Best titles for you right now (coverage from your Jiten account)")
    print("-" * 70)
    for label, rows in data["recommendations"]:
        print(f"\n{label}:")
        for d in rows:
            print(f"  {d.get('coverage') or 0:>6}%  "
                  f"{d.get('characterCount') or 0:>9,}  "
                  f"jiten.moe/decks/media/{d.get('deckId')}/detail  "
                  f"{d.get('originalTitle') or d.get('englishTitle') or '?'}")

    if not data["gains"]:
        return
    print()
    print("-" * 70)
    print(f"  Nearly within reach - kanji coverage now vs. level "
          f"{data['target_level']}")
    print("-" * 70)
    print(f"Checked {len(data['gains'])} of {data['candidates']} candidates "
          f"(raise it with --soon-limit).")
    print(f"\n{'now':>7} {'at lvl':>8} {'gain':>8}   title")
    for gain, now, later, d in data["gains"]:
        print(f"{now:6.2f}% {later:7.2f}% {gain:+7.2f}pp   "
              f"{d.get('originalTitle') or d.get('englishTitle') or '?'}")
        print(f"{'':29}jiten.moe/decks/media/{d.get('deckId')}/detail")


def jiten_subdecks(deck_id: int, key: str | None) -> list[dict]:
    """Episodes / volumes / chapters of a deck, all pages.

    Each one already carries your coverage, so the breakdown costs nothing
    beyond the detail calls themselves.
    """
    out, offset = [], 0
    while True:
        body = get_json(f"{JITEN_API}/api/media-deck/{deck_id}/detail?offset={offset}",
                        headers=jiten_headers(key))
        data = body.get("data", body)
        rows = data.get("subDecks") or []
        out.extend(rows)
        offset += body.get("pageSize") or 25
        if not rows or offset >= (body.get("totalItems") or 0):
            return out


def cmd_parts(args) -> None:
    """Where to start inside a series, instead of judging it as one lump."""
    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)

    for deck_id in args.deck_ids:
        deck = jiten_deck_detail(deck_id, key)
        subs = jiten_subdecks(deck_id, key)
        print()
        print("=" * 72)
        print(f"  {deck_title(deck)} - {len(subs)} parts")
        print("=" * 72)
        if not subs:
            print("This title has no subdecks; it is measured as a whole.")
            continue

        covs = [s.get("coverage") for s in subs if s.get("coverage") is not None]
        if covs:
            print(f"\nYour coverage across parts: {min(covs):.2f}% to {max(covs):.2f}% "
                  f"(whole title: {deck.get('coverage') or 0}%)")

        wanted = subs
        if args.kanji:
            wanted = sorted(subs, key=lambda s: -(s.get("coverage") or 0))[:args.limit]
            print(f"Computing kanji coverage for the {len(wanted)} easiest parts...")

        rows = []
        for s in wanted:
            k = None
            if args.kanji:
                k = analyse_deck(deck_words(s["deckId"], key, s),
                                 known)["kanji_cov_occ"]
                time.sleep(args.sleep)
            rows.append((s, k))

        rows.sort(key=lambda r: -(r[0].get("coverage") or 0))
        print(f"\n{'jiten':>7} {'kanji':>7} {'chars':>8}  part")
        for s, k in rows[:args.limit]:
            kt = f"{k:6.2f}%" if k is not None else "      -"
            print(f"{s.get('coverage') or 0:>6}% {kt} {s.get('characterCount') or 0:>8,}"
                  f"  {s.get('originalTitle') or s.get('englishTitle') or '?'}")
        if not covs:
            continue
        spread = max(covs) - min(covs)
        best = rows[0][0]
        if spread < args.flat:
            print(f"\nSpread is only {spread:.1f}pp, so the parts are all much of a "
                  f"muchness.\nNo reason to skip around - start at the beginning.")
        else:
            print(f"\nSpread is {spread:.1f}pp. Easiest entry point: "
                  f"{best.get('originalTitle')} at {best.get('coverage')}%"
                  f", hardest is {min(covs):.2f}%.")


def in_months(days: float) -> str:
    if days <= 0:
        return "now"
    if days < 60:
        return f"~{days / 7:.0f} weeks"
    if days < 730:
        return f"~{days / 30.4:.0f} months"
    return f"~{days / 365:.1f} years"


def eta(days: float) -> str:
    return time.strftime("%b %Y", time.localtime(time.time() + days * 86400))


def resolve_deck(target: str, key: str | None) -> dict | None:
    """A deck id, or the closest title match."""
    if target.isdigit():
        return jiten_deck_detail(int(target), key)
    hits = jiten_search(target, key, limit=8)
    if not hits:
        return None
    if len(hits) > 1:
        print(f'"{target}" matches {len(hits)} titles; using the first. '
              f"Others:")
        for d in hits[1:6]:
            print(f"    {d['deckId']:>7}  {MEDIA_TYPES.get(d.get('mediaType'), '?'):<12}"
                  f"  {d.get('originalTitle') or d.get('englishTitle')}")
    return jiten_deck_detail(hits[0]["deckId"], key)


def cmd_when(args) -> None:
    """Is this title worth starting, and if not yet, when?"""
    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    lvl = cache.get("level") or 1
    pace = wk_pace(cache)

    for target in args.targets:
        deck = resolve_deck(str(target), key)
        if not deck:
            print(f'No title matching "{target}".')
            continue
        res = analyse_deck(deck_words(deck["deckId"], key, deck), known)
        curve = res["curve"]
        live = deck.get("coverage")

        print()
        print("=" * 72)
        print(f"  {deck_title(deck)}")
        print("=" * 72)
        print(f"{MEDIA_TYPES.get(deck.get('mediaType'), '?')} | "
              f"{deck.get('characterCount') or 0:,} chars | "
              f"difficulty {deck.get('difficulty') or 0} | "
              f"jiten.moe/decks/media/{deck['deckId']}/detail")

        print(f"\nRight now, at level {lvl}")
        print(f"  kanji coverage   {res['kanji_cov_occ']:6.2f}%  "
              f"{bar(res['kanji_cov_occ'])}")
        if live is not None:
            print(f"  word coverage    {live:6.2f}%  {bar(live)}   (jiten.moe)")
        fin = finishing_level(res, lvl)
        if fin is not None:
            print(f"  finishing level {lvl} takes kanji to {fin:.2f}% "
                  f"({fin - res['kanji_cov_occ']:+.2f}pp)")

        print(f"\nWhen the kanji stop getting in the way")
        if pace:
            print(f"  (at your recent pace of {pace:.0f} days per level)")
        print(f"\n  {'kanji':>6}  {'level':>5}  {'levels to go':>12}  "
              f"{'time':>11}  {'around':>9}")
        for threshold in (80, 90, 95, 98):
            need = level_for(curve, threshold)
            if need is None:
                print(f"  {threshold:>5}%  {'never':>5}  "
                      f"{'blocked by non-WaniKani kanji':>38}")
                continue
            to_go = max(0, need - lvl)
            if to_go == 0:
                print(f"  {threshold:>5}%  {need:>5}  {'already there':>12}")
            elif pace:
                d = to_go * pace
                print(f"  {threshold:>5}%  {need:>5}  {to_go:>12}  "
                      f"{in_months(d):>11}  {eta(d):>9}")
            else:
                print(f"  {threshold:>5}%  {need:>5}  {to_go:>12}")

        verdict_level = level_for(curve, args.comfortable) or 61
        if res["kanji_cov_occ"] >= args.comfortable:
            v = "Go for it - the kanji are not what is stopping you."
        elif verdict_level - lvl <= 5:
            v = (f"Close. {verdict_level - lvl} more levels and the kanji settle "
                 f"down; worth starting now if you like looking things up.")
        elif verdict_level > 60:
            v = ("WaniKani alone never gets you there - this one needs vocabulary "
                 "and name-reading from outside it.")
        else:
            v = (f"Early. Kanji stay in the way until about level {verdict_level}"
                 + (f", {in_months((verdict_level - lvl) * pace)} off." if pace else "."))
        print(f"\n{v}")

    print("\nOne caveat worth keeping in mind: this is kanji coverage, which is a")
    print("floor, not a ceiling. Knowing the characters is necessary but not")
    print("sufficient - grammar and the ~30k words WaniKani never teaches decide")
    print("the rest. The word coverage figure from jiten.moe is the honest one,")
    print("and it climbs by reading, not by levelling.")


def cmd_next(args) -> None:
    """The kanji worth learning next, priced in coverage on your own titles."""
    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    ids = tracked_ids(args, key)
    lvl = cache.get("level") or 1

    subjects, assignments = cache["subjects"], cache["assignments"]
    meta = {}
    for sid, s in subjects.items():
        if s["type"] == "kanji":
            meta[s["characters"]] = (s["level"], assignments.get(sid, 0),
                                     "、".join(s.get("readings") or []),
                                     s.get("meaning") or "")

    occ: Counter[str] = Counter()
    for _deck, words in load_decks(ids, key, args.sleep, progress=True):
        for word, n in words.items():
            for ch in KANJI_RE.findall(word):
                occ[ch] += n
    total = sum(occ.values())
    if not total:
        raise SystemExit("no kanji found in those titles")

    unknown = [(n, ch) for ch, n in occ.items() if ch not in known["kanji_known"]]
    unknown.sort(reverse=True)

    print()
    print("=" * 72)
    print(f"  Best kanji to learn next, by what they unlock in your titles")
    print("=" * 72)
    print(f"\n{'rank':>4} {'occur':>7} {pad('kanji', 6)}{pad('reading', 16)}"
          f"{pad('meaning', 15)}{'lvl':>4} {'gain':>7} {'running':>8}  status")
    running = 0.0
    shown = 0
    for n, ch in unknown:
        lv, stage, readings, meaning = meta.get(ch, (None, 0, "", ""))
        gain = n / total * 100
        running += gain
        shown += 1
        if lv is None:
            status = "not in WaniKani"
        elif stage:
            status = SRS_STAGE_NAMES.get(stage, "?")
        elif lv <= lvl:
            status = "unlocked, not started"
        else:
            status = f"locked until level {lv}"
        print(f"{shown:>4} {n:>7} {pad(ch, 6)}{pad(readings, 16)}{pad(meaning[:14], 15)}"
              f"{lv if lv else '--':>4} {gain:6.2f}% {running:7.2f}%  {status}")
        if shown >= args.top:
            break
    print(f"\nLearning those {shown} kanji would take you from "
          f"{ (sum(v for c, v in occ.items() if c in known['kanji_known']) / total * 100):.2f}% "
          f"to {(sum(v for c, v in occ.items() if c in known['kanji_known']) / total * 100) + running:.2f}% "
          f"kanji coverage.")


def cmd_gap(args) -> None:
    """The words in a title you cannot read yet, straight from Jiten."""
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit("`gap` needs a Jiten API key so it knows what you know.")
    for deck_id in args.deck_ids or tracked_ids(args, key):
        deck = jiten_deck_detail(deck_id, key)
        payload = {"format": 2, "downloadType": 1, "order": 3,
                   "excludeMatureMasteredBlacklisted": True,
                   "excludeExampleSentences": args.no_sentences}
        if args.target:
            payload.update({"downloadType": 5, "targetPercentage": args.target})
        if args.min_occurrences:
            payload["minOccurrences"] = args.min_occurrences

        status, body, _ = http(f"{JITEN_API}/api/media-deck/{deck_id}/download",
                               method="POST", headers=jiten_headers(key),
                               body=json.dumps(payload).encode(),
                               content_type="application/json", timeout=180)
        if status >= 400:
            print(f"  {deck_title(deck)}: failed ({status}) "
                  f"{body[:200].decode('utf-8', 'replace')}")
            continue
        safe = re.sub(r"[^\w\- ]", "", deck_title(deck)).strip()[:40] or str(deck_id)
        out = os.path.join(args.out or HERE, f"gap {safe}.csv")
        with open(out, "wb") as f:
            f.write(body)
        lines = body.decode("utf-8", "replace").count("\n")
        print(f"{deck_title(deck)}: {lines - 1:,} unknown words -> {out}")
        time.sleep(args.sleep)


def cmd_edge(args) -> None:
    """Titles that are easier for you than their difficulty rating suggests.

    Jiten's difficulty is one number for everybody. Yours is not: WaniKani
    front-loads certain kanji, so some titles land well above the trend line
    for your account. Fit coverage against difficulty over a sampled pool and
    report the biggest positive residuals.
    """
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit("`edge` needs a Jiten API key to read your coverage.")

    pool: list[dict] = []
    for mtype, label, min_chars in RECOMMEND_TYPES:
        # Sort by title: alphabetical order is uncorrelated with difficulty, so
        # the fit is not dragged toward one end of the range the way sorting by
        # difficulty or coverage would drag it. Then take pages spread across
        # the whole catalogue rather than the first N, or every candidate comes
        # back starting with あ.
        base = (f"{JITEN_API}/api/media-deck/get-media-decks?mediaType={mtype}"
                f"&charCountMin={min_chars}&sortBy=title&sortOrder=0")
        first = get_json(base + "&offset=0", headers=jiten_headers(key))
        total = first.get("totalItems") or 0
        pages = max(1, args.sample // 50)
        step = max(1, (total // 50) // pages) * 50 if total > 50 else 50
        offsets = [min(i * step, max(0, total - 50)) for i in range(pages)]
        for offset in dict.fromkeys(offsets):
            rows = (first.get("data") if offset == 0 else
                    get_json(f"{base}&offset={offset}",
                             headers=jiten_headers(key)).get("data")) or []
            for r in rows:
                r["_type"] = label
            pool.extend(rows)
            time.sleep(0.4)

    pts = [(float(d["difficulty"]), float(d["coverage"]), d) for d in pool
           if d.get("difficulty") and d.get("coverage") is not None]
    if len(pts) < 10:
        raise SystemExit("not enough sampled titles to fit a trend")

    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    var = sum((p[0] - mx) ** 2 for p in pts)
    slope = (sum((p[0] - mx) * (p[1] - my) for p in pts) / var) if var else 0.0
    intercept = my - slope * mx

    # Sort on the residual only: ties would otherwise fall through to comparing
    # the deck dicts, which raises.
    scored = [(y - (slope * x + intercept), x, y, d) for x, y, d in pts]
    scored.sort(key=lambda r: -r[0])

    print()
    print("=" * 74)
    print(f"  Easier for you than for the average learner")
    print("=" * 74)
    lo, hi = min(p[0] for p in pts), max(p[0] for p in pts)
    print(f"\nFitted over {n} sampled titles spanning difficulty {lo:.1f}-{hi:.1f}: "
          f"every point of difficulty costs {-slope:.2f}pp of coverage.")
    print("'edge' is how far above that line your coverage sits.\n")
    print(f"{'edge':>6} {'cover':>7} {'diff':>6}  {'type':<14} {'chars':>9}  title")
    seen = set()
    for resid, diff, cov, d in scored:
        if d["deckId"] in seen:
            continue
        seen.add(d["deckId"])
        print(f"{resid:+5.1f}pp {cov:6.2f}% {diff:6.2f}  {d['_type']:<14} "
              f"{d.get('characterCount') or 0:>9,}  "
              f"{d.get('originalTitle') or d.get('englishTitle') or '?'}")
        print(f"{'':>15}jiten.moe/decks/media/{d['deckId']}/detail")
        if len(seen) >= args.top_n:
            break


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def svg_curves(series: list[tuple[str, list[tuple[int, float]]]], user_level: int,
               width: int = 760, height: int = 320) -> str:
    """Kanji coverage against WaniKani level, one line per title."""
    pad_l, pad_b, pad_t, pad_r = 44, 34, 12, 150
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    palette = ["#c2410c", "#0369a1", "#15803d", "#7e22ce", "#b45309",
               "#0f766e", "#be123c", "#4338ca", "#65a30d", "#a21caf",
               "#0891b2", "#9f1239", "#1d4ed8"]

    def x(lv):
        return pad_l + (lv - 1) / 59 * pw

    def y(pct):
        return pad_t + ph - pct / 100 * ph

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
             f'role="img" aria-label="Kanji coverage by WaniKani level">']
    for pct in range(0, 101, 25):
        parts.append(f'<line x1="{pad_l}" y1="{y(pct):.1f}" x2="{pad_l + pw}" '
                     f'y2="{y(pct):.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y(pct) + 4:.1f}" '
                     f'class="tick" text-anchor="end">{pct}%</text>')
    for lv in (1, 10, 20, 30, 40, 50, 60):
        parts.append(f'<text x="{x(lv):.1f}" y="{height - 12}" class="tick" '
                     f'text-anchor="middle">{lv}</text>')
    if user_level:
        parts.append(f'<line x1="{x(user_level):.1f}" y1="{pad_t}" '
                     f'x2="{x(user_level):.1f}" y2="{pad_t + ph}" class="you"/>')
        parts.append(f'<text x="{x(user_level) + 4:.1f}" y="{pad_t + 10}" '
                     f'class="tick">you</text>')
    for i, (label, curve) in enumerate(series):
        colour = palette[i % len(palette)]
        pts = " ".join(f"{x(lv):.1f},{y(pct):.1f}" for lv, pct in curve)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                     f'stroke-width="1.8"/>')
        ly = pad_t + 12 + i * 15
        parts.append(f'<line x1="{pad_l + pw + 10}" y1="{ly}" '
                     f'x2="{pad_l + pw + 26}" y2="{ly}" stroke="{colour}" '
                     f'stroke-width="2.4"/>')
        short = label if len(label) <= 16 else label[:15] + "…"
        parts.append(f'<text x="{pad_l + pw + 31}" y="{ly + 4}" '
                     f'class="legend">{esc(short)}</text>')
    parts.append(f'<text x="{pad_l + pw / 2}" y="{height - 1}" class="tick" '
                 f'text-anchor="middle">WaniKani level</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_history(past: dict[int, list[dict]], titles: dict[int, str],
                width: int = 760, height: int = 260) -> str:
    """Coverage per title over time. Needs at least two dated runs."""
    series = []
    for deck_id, rows in past.items():
        pts = []
        for r in rows:
            try:
                pts.append((r["date"][:10], float(r["kanjiCoverage"])))
            except (ValueError, KeyError, TypeError):
                continue
        if len(pts) >= 2:
            series.append((titles.get(deck_id, str(deck_id)), pts))
    if not series:
        return ('<p class="empty">Not enough history yet - this chart appears once '
                'you have run the update on two different days.</p>')

    dates = sorted({d for _, pts in series for d, _ in pts})
    lo = min(v for _, pts in series for _, v in pts)
    hi = max(v for _, pts in series for _, v in pts)
    lo, hi = max(0, lo - 2), min(100, hi + 2)
    pad_l, pad_b, pad_t, pad_r = 44, 34, 12, 150
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    palette = ["#c2410c", "#0369a1", "#15803d", "#7e22ce", "#b45309",
               "#0f766e", "#be123c", "#4338ca", "#65a30d", "#a21caf",
               "#0891b2", "#9f1239", "#1d4ed8"]

    def x(d):
        i = dates.index(d)
        return pad_l + (i / max(1, len(dates) - 1)) * pw

    def y(v):
        return pad_t + ph - (v - lo) / max(0.001, hi - lo) * ph

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="Coverage over time">']
    for frac in (0, 0.5, 1):
        v = lo + (hi - lo) * frac
        parts.append(f'<line x1="{pad_l}" y1="{y(v):.1f}" x2="{pad_l + pw}" '
                     f'y2="{y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{y(v) + 4:.1f}" class="tick" '
                     f'text-anchor="end">{v:.0f}%</text>')
    for d in (dates[0], dates[-1]) if len(dates) > 1 else dates:
        parts.append(f'<text x="{x(d):.1f}" y="{height - 12}" class="tick" '
                     f'text-anchor="middle">{d}</text>')
    for i, (label, pts) in enumerate(series):
        colour = palette[i % len(palette)]
        coords = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in pts)
        parts.append(f'<polyline points="{coords}" fill="none" stroke="{colour}" '
                     f'stroke-width="1.8"/>')
        for d, v in pts:
            parts.append(f'<circle cx="{x(d):.1f}" cy="{y(v):.1f}" r="2.6" '
                         f'fill="{colour}"/>')
        ly = pad_t + 12 + i * 15
        parts.append(f'<line x1="{pad_l + pw + 10}" y1="{ly}" '
                     f'x2="{pad_l + pw + 26}" y2="{ly}" stroke="{colour}" '
                     f'stroke-width="2.4"/>')
        short = label if len(label) <= 16 else label[:15] + "…"
        parts.append(f'<text x="{pad_l + pw + 31}" y="{ly + 4}" '
                     f'class="legend">{esc(short)}</text>')
    parts.append("</svg>")
    return "".join(parts)


REPORT_CSS = """
:root {
  --bg:#faf8f5; --raise:#fff; --fg:#1a1815; --muted:#6f6961; --faint:#948d84;
  --line:#e6e0d7; --line-soft:#f0ebe4; --accent:#c2410c; --accent-soft:#fdf1e9;
  --good:#15803d; --shadow:0 1px 2px rgba(60,50,40,.05),0 4px 16px rgba(60,50,40,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#141312; --raise:#1e1c1a; --fg:#ece9e4; --muted:#9c958c; --faint:#736c64;
    --line:#302c28; --line-soft:#252220; --accent:#fb923c; --accent-soft:#2a1d13;
    --good:#4ade80; --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.25);
  }
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; padding:0 0 80px; background:var(--bg); color:var(--fg);
  font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",
       system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;
  -webkit-font-smoothing:antialiased; }
main { max-width:920px; margin:0 auto; padding:0 22px; }

/* header */
.hero { border-bottom:1px solid var(--line); margin-bottom:28px;
  padding:44px 0 26px; position:relative; }
.hero::before { content:""; position:absolute; top:0; left:0; right:0; height:3px;
  background:linear-gradient(90deg,var(--accent),transparent 70%); }
h1 { font-size:clamp(26px,4vw,34px); margin:0 0 6px; letter-spacing:-.022em;
  font-weight:640; }
h1 span { color:var(--accent); }
h2 { font-size:13px; margin:52px 0 14px; letter-spacing:.09em; font-weight:650;
  text-transform:uppercase; color:var(--muted);
  border-bottom:1px solid var(--line-soft); padding-bottom:8px; }
h3 { font-size:20px; margin:0 0 4px; letter-spacing:-.015em; font-weight:620; }
.sub { color:var(--muted); margin:0 0 20px; font-size:14px; }
.sub:last-child { margin-bottom:0; }

/* jump nav */
nav { display:flex; flex-wrap:wrap; gap:6px; margin:-6px 0 6px; }
nav a { font-size:12.5px; color:var(--muted); padding:5px 11px; border:0;
  border-radius:99px; background:var(--raise); box-shadow:var(--shadow); }
nav a:hover { color:var(--accent); }

/* stat cards */
.cards { display:grid; gap:12px; margin:0 0 10px;
  grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); }
.card { background:var(--raise); border:1px solid var(--line); border-radius:14px;
  padding:16px 18px; box-shadow:var(--shadow); }
.card .n { font-size:29px; font-weight:640; letter-spacing:-.03em; line-height:1.1;
  font-variant-numeric:tabular-nums; }
.card .l { color:var(--faint); font-size:11px; text-transform:uppercase;
  letter-spacing:.08em; margin-top:3px; font-weight:600; }
.card .d { color:var(--accent); font-size:12.5px; font-weight:650; margin-top:5px; }

/* tables */
.wrap { overflow-x:auto; -webkit-overflow-scrolling:touch;
  background:var(--raise); border:1px solid var(--line); border-radius:14px;
  box-shadow:var(--shadow); }
table { border-collapse:collapse; width:100%; font-size:14px; min-width:540px; }
th { text-align:left; font-weight:650; color:var(--faint); font-size:10.5px;
  text-transform:uppercase; letter-spacing:.07em; padding:13px 14px 9px;
  border-bottom:1px solid var(--line); white-space:nowrap;
  background:var(--raise); position:sticky; top:0; }
td { padding:11px 14px; border-bottom:1px solid var(--line-soft);
  vertical-align:middle; }
tr:last-child td { border-bottom:0; }
tbody tr:hover td, table tr:hover td { background:var(--accent-soft); }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums;
  white-space:nowrap; }
td:first-child { min-width:11em; font-weight:520; }
td.kanji { min-width:auto; font-size:26px; line-height:1; font-weight:400; }
a { color:inherit; text-decoration:none; border-bottom:1px solid var(--line); }
a:hover { border-bottom-color:var(--accent); color:var(--accent); }

/* bits */
.meter { display:block; width:96px; height:6px; background:var(--line);
  border-radius:99px; overflow:hidden; }
.meter i { display:block; height:100%; border-radius:99px;
  background:linear-gradient(90deg,var(--accent),color-mix(in srgb,var(--accent) 60%,#e879f9)); }
.up { color:var(--good); font-weight:640; }
.pill { display:inline-block; font-size:11px; padding:2px 9px; border-radius:99px;
  background:var(--accent-soft); color:var(--accent); font-weight:620;
  white-space:nowrap; }
.chart { width:100%; height:auto; display:block; padding:14px 4px 4px; }
.grid { stroke:var(--line-soft); stroke-width:1; }
.you { stroke:var(--accent); stroke-width:1.5; stroke-dasharray:4 4; }
.tick { fill:var(--faint); font-size:11px; }
.legend { fill:var(--fg); font-size:11px; }
.empty { color:var(--faint); font-style:italic; padding:18px; margin:0; }
footer { color:var(--faint); font-size:12px; margin-top:56px;
  border-top:1px solid var(--line); padding-top:16px; }
@media (max-width:640px) {
  main { padding:0 14px; }
  .hero { padding:30px 0 20px; }
  h2 { margin-top:38px; }
}
"""


BROWSE_SLOT = "<!--browse-->"
NAV_SLOT = "<!--nav-->"

# Works in the saved file too: the curves are already embedded, so dragging the
# slider needs no network at all.
SLIDER_HTML = """
<div class="slider">
  <label for="lvl">If I were level <b id="lvlout"></b></label>
  <input id="lvl" type="range" min="1" max="60">
  <span id="eta" class="pill"></span>
</div>
<div class="wrap"><table id="whatif"><tr><th>title</th>
  <th class="num">now</th><th class="num">then</th><th class="num">gain</th>
  <th></th></tr></table></div>
"""

SLIDER_JS = """
(function(){
  const slider = document.getElementById('lvl');
  // TRACK is a top-level `const` in a sibling script: script-scoped, so it is
  // reachable by name but never a property of window.
  if (!slider || typeof TRACK === 'undefined' || !TRACK.titles.length) return;
  slider.min = TRACK.level; slider.value = Math.min(60, TRACK.level + 8);
  function draw(){
    const lv = +slider.value;
    document.getElementById('lvlout').textContent = lv;
    const away = lv - TRACK.level;
    const eta = document.getElementById('eta');
    if (away <= 0) eta.textContent = 'where you are now';
    else if (TRACK.pace){
      const d = away * TRACK.pace;
      eta.textContent = (d < 60 ? `~${Math.round(d/7)} weeks`
                       : d < 730 ? `~${Math.round(d/30.4)} months`
                       : `~${(d/365).toFixed(1)} years`) + ' away';
    } else eta.textContent = away + ' levels away';
    let rows = '';
    for (const t of TRACK.titles){
      const then = t.c[lv-1], gain = then - t.now;
      rows += `<tr><td>${t.t}</td><td class="num">${t.now.toFixed(1)}%</td>
        <td class="num">${then.toFixed(1)}%</td>
        <td class="num up">+${gain.toFixed(1)}pp</td>
        <td><span class="meter"><i style="width:${then.toFixed(1)}%"></i></span></td></tr>`;
    }
    document.getElementById('whatif').innerHTML =
      `<tr><th>title</th><th class="num">now</th><th class="num">at that level</th>
       <th class="num">gain</th><th></th></tr>` + rows;
  }
  slider.addEventListener('input', draw);
  draw();
})();
"""

GRID_HTML = """
<div class="gridbar">
  <div class="modes">
    <button data-mode="srs" class="on">by SRS stage</button>
    <button data-mode="impact">by what it costs you</button>
  </div>
  <div id="legend" class="legend-row"></div>
</div>
<div id="gridout" class="gridout"></div>
<div id="kdetail" class="kdetail"><span class="hint">Pick a kanji.</span></div>
"""

GRID_JS = """
(function(){
  const out = document.getElementById('gridout');
  if (!out || typeof GRID === 'undefined' || !GRID.length) return;
  const STAGES = [
    [0, 'locked or unstarted', 'var(--k0)'],
    [1, 'Apprentice',          '#dd0093'],
    [5, 'Guru',                '#882d9e'],
    [7, 'Master',              '#294ddb'],
    [8, 'Enlightened',         '#0093dd'],
    [9, 'Burned',              '#8a7355'],
  ];
  const band = s => s >= 9 ? 5 : s >= 8 ? 4 : s >= 7 ? 3 : s >= 5 ? 2 : s >= 1 ? 1 : 0;
  const max = Math.max(1, ...GRID.filter(k => !k.k).map(k => k.n));
  let mode = 'srs';

  function colour(k){
    if (mode === 'srs') return STAGES[band(k.s)][2];
    if (k.k) return 'var(--k-known)';
    if (!k.n) return 'var(--k0)';
    // Unknown and common: the darker the red, the more it is costing you.
    const t = Math.sqrt(k.n / max);
    return `color-mix(in srgb, var(--k-hot) ${Math.round(18 + t * 82)}%, var(--k0))`;
  }

  function draw(){
    const byLevel = new Map();
    for (const k of GRID){
      if (!byLevel.has(k.l)) byLevel.set(k.l, []);
      byLevel.get(k.l).push(k);
    }
    let html = '';
    for (const [lv, list] of [...byLevel].sort((a, b) => a[0] - b[0])){
      html += `<div class="lvlrow"><span class="lvlnum${lv === GRID_LEVEL ?
        ' now' : ''}">${lv}</span><div class="kanjis">`;
      for (const k of list)
        html += `<button class="k" style="background:${colour(k)}"
                 data-c="${k.c}" title="${k.c}">${k.c}</button>`;
      html += '</div></div>';
    }
    out.innerHTML = html;
    out.querySelectorAll('.k').forEach(b => b.onclick = () => show(b.dataset.c));

    document.getElementById('legend').innerHTML = mode === 'srs'
      ? STAGES.map(s => `<span class="lg"><i style="background:${s[2]}"></i>${s[1]}</span>`).join('')
      : `<span class="lg"><i style="background:var(--k-known)"></i>known</span>
         <span class="lg"><i style="background:var(--k0)"></i>never appears</span>
         <span class="lg"><i style="background:var(--k-hot)"></i>costs you most</span>`;
  }

  function show(c){
    const k = GRID.find(x => x.c === c);
    if (!k) return;
    const stage = STAGES[band(k.s)][1];
    document.getElementById('kdetail').innerHTML =
      `<span class="big">${k.c}</span>
       <div><b>${k.m || '—'}</b> &nbsp; <span class="rd">${k.r || ''}</span>
       <div class="sub">WaniKani level ${k.l} &middot; ${k.k ? 'known' : stage}
       &middot; appears ${k.n.toLocaleString()}&times; in your titles</div></div>`;
  }

  document.querySelectorAll('.modes button').forEach(b => b.onclick = () => {
    mode = b.dataset.mode;
    document.querySelectorAll('.modes button').forEach(x =>
      x.classList.toggle('on', x === b));
    draw();
  });
  draw();
})();
"""

GRID_CSS = """
:root { --k0:#e8e2d9; --k-known:#15803d; --k-hot:#dc2626; }
@media (prefers-color-scheme: dark) {
  :root { --k0:#2a2622; --k-known:#22c55e; --k-hot:#ef4444; }
}
.gridbar { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center;
  justify-content:space-between; margin-bottom:14px; }
.modes { display:flex; gap:6px; }
.modes button.on { background:var(--accent); border-color:var(--accent);
  color:#fff; }
.modes button.on:hover { background:var(--accent); color:#fff; }
.legend-row { display:flex; flex-wrap:wrap; gap:12px; font-size:11.5px;
  color:var(--muted); }
.lg { display:inline-flex; align-items:center; gap:5px; }
.lg i { width:11px; height:11px; border-radius:3px; display:inline-block; }
.gridout { background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:12px 14px; box-shadow:var(--shadow); }
.lvlrow { display:flex; gap:10px; align-items:flex-start; padding:2px 0; }
.lvlnum { width:2em; text-align:right; font-size:10.5px; color:var(--faint);
  padding-top:7px; font-variant-numeric:tabular-nums; flex:none; }
.lvlnum.now { color:var(--accent); font-weight:700; }
.kanjis { display:flex; flex-wrap:wrap; gap:3px; }
.k { width:27px; height:27px; padding:0; border:0; border-radius:6px;
  font-size:16px; line-height:1; color:#fff; cursor:pointer;
  text-shadow:0 1px 2px rgba(0,0,0,.35); font-family:inherit; }
.k:hover { outline:2px solid var(--fg); outline-offset:1px; background-clip:padding-box; }
.kdetail { display:flex; align-items:center; gap:16px; margin-top:12px;
  min-height:62px; background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:12px 16px; box-shadow:var(--shadow); }
.kdetail .big { font-size:40px; line-height:1; }
.kdetail .rd { color:var(--accent); }
.kdetail .hint { color:var(--faint); font-style:italic; }
.kdetail .sub { margin:2px 0 0; font-size:12.5px; }
"""

SLIDER_CSS = """
.slider { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  background:var(--raise); border:1px solid var(--line); border-radius:14px;
  padding:16px 18px; margin-bottom:12px; box-shadow:var(--shadow); }
.slider label { font-size:14px; color:var(--muted); white-space:nowrap; }
.slider label b { color:var(--accent); font-size:17px;
  font-variant-numeric:tabular-nums; }
.slider input[type=range] { flex:1 1 220px; accent-color:var(--accent);
  height:22px; }
"""

BROWSE_HTML = """
<h2 id="browse">Browse jiten.moe</h2>
<p class="sub">Search the whole catalogue. Pick a title to work out, right here,
what level it stops fighting you at.</p>
<div class="controls">
  <input id="q" type="search" placeholder="title, romaji or English&hellip;"
         autocomplete="off">
  <select id="type">
    <option value="">any type</option>
    <option value="1">anime</option><option value="9">manga</option>
    <option value="4">novel</option><option value="7">visual novel</option>
    <option value="6">game</option><option value="2">drama</option>
    <option value="3">movie</option><option value="8">web novel</option>
  </select>
  <select id="sort">
    <option value="coverage">best coverage first</option>
    <option value="difficulty">easiest first</option>
    <option value="wordCount">longest first</option>
    <option value="communityVotes">most popular</option>
  </select>
  <input id="minchars" type="number" placeholder="min chars" min="0" step="10000">
</div>
<div id="results" class="wrap"></div>
<div id="detail"></div>
"""

BROWSE_JS = """
const KANJI = /[\\u3400-\\u4dbf\\u4e00-\\u9fff\\uf900-\\ufaff]/;
const known = new Set(Array.from(WK.known));
const $ = s => document.querySelector(s);
let timer, lastRows = [];

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function title(d){ return d.originalTitle || d.englishTitle || d.romajiTitle || '?'; }

async function search(){
  const q = $('#q').value.trim();
  const type = $('#type').value, sort = $('#sort').value;
  const min = $('#minchars').value;
  if (!q && !type && !min){ $('#results').innerHTML =
    '<p class="empty">Type something, or pick a filter.</p>'; return; }
  $('#results').innerHTML = '<p class="empty">Searching&hellip;</p>';
  let url = `/api/media-deck/get-media-decks?sortBy=${sort}&sortOrder=1`;
  if (q) url += `&titleFilter=${encodeURIComponent(q)}`;
  if (type) url += `&mediaType=${type}`;
  if (min) url += `&charCountMin=${min}`;
  try {
    const r = await fetch(url);
    const data = await r.json();
    lastRows = data.data || [];
    render();
  } catch (e){
    $('#results').innerHTML = '<p class="empty">Search failed: ' + esc(e) + '</p>';
  }
}

function render(){
  if (!lastRows.length){ $('#results').innerHTML =
    '<p class="empty">Nothing matched.</p>'; return; }
  let h = '<table><tr><th>title</th><th>type</th><th class="num">chars</th>' +
          '<th class="num">difficulty</th><th class="num">your coverage</th>' +
          '<th></th></tr>';
  for (const d of lastRows.slice(0, 40)){
    h += `<tr><td><a href="https://jiten.moe/decks/media/${d.deckId}/detail"
          target="_blank" rel="noopener">${esc(title(d))}</a></td>
          <td>${esc(WK.types[d.mediaType] || '?')}</td>
          <td class="num">${(d.characterCount||0).toLocaleString()}</td>
          <td class="num">${d.difficulty ?? '—'}</td>
          <td class="num">${d.coverage != null ? d.coverage + '%' : '—'}</td>
          <td class="acts"><button data-when="${d.deckId}">when?</button>
            <button data-track="${d.deckId}" data-status="2">reading</button>
            <button data-track="${d.deckId}" data-status="1">plan</button></td></tr>`;
  }
  $('#results').innerHTML = h + '</table>';
  document.querySelectorAll('#results [data-when]').forEach(b =>
    b.onclick = () => analyse(+b.dataset.when, b));
  document.querySelectorAll('#results [data-track]').forEach(b =>
    b.onclick = () => track(+b.dataset.track, +b.dataset.status, b));
}

// Puts the title on your Jiten list, which is also what makes the next run
// pick it up automatically.
async function track(id, status, btn){
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = 'saving…';
  try {
    const r = await fetch(`/api/user/deck-preferences/${id}/status`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    btn.textContent = status === 2 ? 'reading ✓' : 'planned ✓';
    btn.classList.add('done');
  } catch (e){
    btn.textContent = 'failed'; btn.disabled = false;
    setTimeout(() => { btn.textContent = was; }, 2000);
  }
}

function levelFor(curve, target){
  for (let i = 0; i < curve.length; i++) if (curve[i] >= target) return i + 1;
  return null;
}
function when(levels){
  if (levels <= 0) return 'already there';
  if (!WK.pace) return levels + ' levels away';
  const d = levels * WK.pace;
  const when = new Date(Date.now() + d * 864e5);
  const span = d < 60 ? `~${Math.round(d/7)} weeks`
             : d < 730 ? `~${Math.round(d/30.4)} months`
             : `~${(d/365).toFixed(1)} years`;
  return `${span}, around ${when.toLocaleString('en', {month:'short', year:'numeric'})}`;
}

async function analyse(id, btn){
  btn.disabled = true; btn.textContent = 'reading…';
  const d = lastRows.find(r => r.deckId === id) || {};
  try {
    const r = await fetch(`/api/media-deck/${id}/download`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({format: 4, downloadType: 1, order: 3})});
    const text = await r.text();
    const occ = new Map();
    for (const ch of text) if (KANJI.test(ch)) occ.set(ch, (occ.get(ch)||0) + 1);

    let total = 0, mine = 0, outside = 0;
    const byLevel = new Array(61).fill(0);
    for (const [ch, n] of occ){
      total += n;
      if (known.has(ch)) mine += n;
      const lv = WK.levels[ch];
      if (lv) byLevel[lv] += n; else outside += n;
    }
    const curve = []; let run = 0;
    for (let lv = 1; lv <= 60; lv++){ run += byLevel[lv]; curve.push(run / total * 100); }
    const now = mine / total * 100;

    let rows = '';
    for (const t of [80, 90, 95, 98]){
      const need = levelFor(curve, t);
      rows += `<tr><td class="num">${t}%</td><td class="num">${need ?? 'never'}</td>
               <td>${need ? when(need - WK.level)
                          : 'blocked by kanji WaniKani never teaches'}</td></tr>`;
    }
    $('#detail').innerHTML = `
      <h2>${esc(title(d))}</h2>
      <p class="sub">${occ.size} distinct kanji ·
        ${total.toLocaleString()} occurrences ·
        ceiling ${(100 - outside/total*100).toFixed(1)}%</p>
      <div class="cards">
        <div class="card"><div class="n">${now.toFixed(1)}%</div>
          <div class="l">kanji coverage now</div></div>
        <div class="card"><div class="n">${curve[WK.level-1].toFixed(1)}%</div>
          <div class="l">after finishing level ${WK.level}</div>
          <div class="d">+${(curve[WK.level-1]-now).toFixed(1)}pp</div></div>
        <div class="card"><div class="n">${d.coverage != null ? d.coverage+'%' : '—'}</div>
          <div class="l">word coverage (jiten)</div></div>
      </div>
      <div class="wrap"><table><tr><th class="num">kanji</th>
        <th class="num">at level</th><th>which is</th></tr>${rows}</table></div>
      <p class="sub">Kanji coverage is a floor, not a ceiling — grammar and the
      words WaniKani never teaches decide the rest.</p>`;
    $('#detail').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e){
    $('#detail').innerHTML = '<p class="empty">Could not read that title: ' +
      esc(e) + '</p>';
  }
  btn.disabled = false; btn.textContent = 'when?';
}

$('#q').addEventListener('input', () => { clearTimeout(timer);
  timer = setTimeout(search, 350); });
for (const id of ['#type', '#sort', '#minchars'])
  $(id).addEventListener('change', search);
"""

BROWSE_CSS = """
.controls { display:flex; flex-wrap:wrap; gap:9px; margin:0 0 14px; }
.controls input, .controls select { font:inherit; font-size:14px; padding:10px 13px;
  border:1px solid var(--line); border-radius:11px; background:var(--raise);
  color:var(--fg); box-shadow:var(--shadow); }
.controls input:focus, .controls select:focus { outline:2px solid var(--accent);
  outline-offset:-1px; }
.controls #q { flex:1 1 260px; }
.controls #minchars { width:130px; }
button { font:inherit; font-size:12.5px; font-weight:560; padding:5px 12px;
  cursor:pointer; border:1px solid var(--line); border-radius:99px;
  background:var(--bg); color:var(--muted); white-space:nowrap; }
button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent);
  background:var(--accent-soft); }
button:disabled { opacity:.55; cursor:default; }
button.done { color:var(--good); border-color:var(--good); }
td.acts { display:flex; gap:5px; justify-content:flex-end; }
#detail:not(:empty) { margin-top:34px; padding-top:6px;
  border-top:2px solid var(--accent); }
"""


def build_report_html(args, key, cache, known, interactive: bool = False,
                      both: bool = False):
    """The dashboard. The interactive variant carries the live browser, which
    only works when served by `serve` - the Jiten API sends no CORS headers, so
    a file:// page is not allowed to read its responses.

    both=True returns (static, interactive) from a single pass, so serving and
    saving a copy does not fetch everything twice.
    """
    ids = tracked_ids(args, key)
    lvl = cache.get("level") or 0

    rows, curves, titles = [], [], {}
    for deck, words in load_decks(ids, key, args.sleep, progress=True):
        res = analyse_deck(words, known)
        deck_id = deck.get("deckId")
        titles[deck_id] = deck_title(deck)
        rows.append((deck, res))
        curves.append((deck_title(deck), res["curve"]))

    past = read_history()
    prev_summary = None
    if os.path.exists(WK_CACHE_PREV):
        with open(WK_CACHE_PREV, encoding="utf-8") as f:
            prev = wk_known(json.load(f), min_stage=args.min_stage,
                            mode=args.mode, level=args.level)
        prev_summary = (len(known["kanji_known"]) - len(prev["kanji_known"]),
                        len(known["words_known_set"]) - len(prev["words_known_set"]))

    head = (f"<title>{esc(cache.get('username'))} - WaniKani coverage</title>"
            f"<style>{REPORT_CSS}</style>")

    sections: list[tuple[str, str]] = []

    def h2(label: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        sections.append((slug, label))
        return f'<h2 id="{slug}">{esc(label)}</h2>'

    h = ["<main>",
         f'<div class="hero"><h1>{esc(cache.get("username"))} on '
         f'<span>jiten.moe</span></h1>'
         f'<p class="sub">WaniKani level {lvl} &middot; generated '
         f'{time.strftime("%d %b %Y, %H:%M")}</p></div>',
         NAV_SLOT, '<div class="cards">']

    def card(n, label, delta=None):
        d = f'<div class="d">{delta:+d} since last run</div>' if delta else ""
        return (f'<div class="card"><div class="n">{n}</div>'
                f'<div class="l">{label}</div>{d}</div>')

    h.append(card(lvl, "level"))
    h.append(card(len(known["kanji_known"]), "kanji known",
                  prev_summary[0] if prev_summary else None))
    h.append(card(len(known["words_known_set"]), "words known",
                  prev_summary[1] if prev_summary else None))
    if rows:
        best = max(rows, key=lambda r: r[1]["kanji_cov_occ"])
        h.append(card(f'{best[1]["kanji_cov_occ"]:.0f}%',
                      f"best: {esc(deck_title(best[0])[:14])}"))
    h.append("</div>")
    h.append(BROWSE_SLOT)

    h.append(h2("Your tracked titles"))
    h.append(f'<div class="wrap"><table><tr><th>title</th><th>list</th>'
             f'<th class="num">kanji</th>'
             f'<th></th><th class="num">finish L{lvl}</th>'
             f'<th class="num">jiten</th><th class="num">lvl for 95%</th>'
             f'<th class="num">ceiling</th><th class="num">trend</th></tr>')
    for deck, res in sorted(rows, key=lambda r: -r[1]["kanji_cov_occ"]):
        deck_id = deck.get("deckId")
        live = deck.get("coverage")
        trend = history_trend(past.get(deck_id, []))
        t = (f'<span class="up">{trend[1] - trend[0]:+.1f}pp</span> / {trend[2]}d'
             if trend and trend[1] > trend[0] else
             (f"{trend[1] - trend[0]:+.1f}pp / {trend[2]}d" if trend else "&mdash;"))
        k = res["kanji_cov_occ"]
        fin = finishing_level(res, lvl)
        fin_cell = ("&mdash;" if fin is None else
                    f'{fin:.1f}% <span class="up">{fin - k:+.1f}</span>')
        h.append(
            f'<tr><td><a href="https://jiten.moe/decks/media/{deck_id}/detail">'
            f'{esc(deck_title(deck))}</a></td>'
            f'<td>{esc(STATUS_LABELS.get(DECK_STATUS.get(deck_id), "—"))}</td>'
            f'<td class="num">{k:.1f}%</td>'
            f'<td><span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
            f'<td class="num">{fin_cell}</td>'
            f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
            f'<td class="num">{level_for(res["curve"], 95) or "&mdash;"}</td>'
            f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
            f'<td class="num">{t}</td></tr>')
    h.append("</table></div>")

    h.append(h2("What each level would buy you"))
    h.append(svg_curves(curves, lvl))
    h.append(SLIDER_HTML)

    h.append(h2("Coverage over time"))
    h.append(svg_history(past, titles))

    # Leeches: Apprentice items weighted by how often they block your titles.
    subjects, assignments = cache["subjects"], cache["assignments"]
    struggling = {s["characters"]: (assignments[sid], s["level"],
                                    "、".join(s.get("readings") or []),
                                    s.get("meaning") or "")
                  for sid, s in subjects.items()
                  if s["type"] == "kanji" and 1 <= assignments.get(sid, 0) <= 4}
    occ: Counter[str] = Counter()
    for _deck, res in rows:
        occ.update(res["kanji_occ"])
    leeches = sorted(((n, ch) + struggling[ch] for ch, n in occ.items()
                      if ch in struggling and ch not in known["kanji_known"]),
                     reverse=True)[:24]
    # Every WaniKani kanji, by level, coloured either by how well you know it
    # or by how much not knowing it costs you in the titles you track.
    grid_data = sorted(
        ({"c": s["characters"], "l": s["level"],
          "s": assignments.get(sid, 0),
          "k": s["characters"] in known["kanji_known"],
          "n": occ.get(s["characters"], 0),
          "r": "、".join(s.get("readings") or []),
          "m": s.get("meaning") or ""}
         for sid, s in subjects.items() if s["type"] == "kanji"),
        key=lambda k: (k["l"], -k["n"], k["c"]))
    grid_json = json.dumps(grid_data, ensure_ascii=False, separators=(",", ":"))
    seen_kanji = sum(1 for k in grid_data if k["n"])
    h.append(h2("Kanji grid"))
    h.append(f'<p class="sub">All {len(grid_data):,} WaniKani kanji by level. '
             f'{seen_kanji:,} of them turn up in the titles you track. '
             f'Switch the colouring to see which gaps actually cost you.</p>')
    h.append(GRID_HTML)

    h.append(h2("Leeches blocking your reading"))
    if leeches:
        h.append('<p class="sub">Apprentice kanji, ranked by how often they appear '
                 'in the titles above. Already in your review queue.</p>')
        h.append('<div class="wrap"><table><tr><th>kanji</th><th>reading</th>'
                 '<th>meaning</th><th class="num">occurrences</th><th>stage</th>'
                 '<th class="num">wk level</th></tr>')
        for n, ch, stage, klvl, readings, meaning in leeches:
            h.append(f'<tr><td class="kanji">{esc(ch)}</td>'
                     f'<td>{esc(readings)}</td><td>{esc(meaning)}</td>'
                     f'<td class="num">{n:,}</td>'
                     f'<td>{SRS_STAGE_NAMES.get(stage, "?")}</td>'
                     f'<td class="num">{klvl}</td></tr>')
        h.append("</table></div>")
    else:
        h.append('<p class="empty">Nothing in Apprentice shows up in your tracked '
                 'titles.</p>')

    if key and not args.no_recommend:
        data = collect_status(args, key, cache, known)
        h.append(h2("Best titles for you right now"))
        h.append('<div class="wrap"><table><tr><th>type</th><th>title</th>'
                 '<th class="num">coverage</th><th class="num">chars</th></tr>')
        for label, recs in data["recommendations"]:
            for d in recs:
                h.append(
                    f'<tr><td>{esc(label)}</td>'
                    f'<td><a href="https://jiten.moe/decks/media/{d.get("deckId")}/detail">'
                    f'{esc(d.get("originalTitle") or d.get("englishTitle") or "?")}'
                    f'</a></td>'
                    f'<td class="num">{d.get("coverage") or 0}%</td>'
                    f'<td class="num">{d.get("characterCount") or 0:,}</td></tr>')
        h.append("</table></div>")
        if data["gains"]:
            h.append(h2(f'Nearly within reach at level {data["target_level"]}'))
            h.append('<div class="wrap"><table><tr><th>title</th><th class="num">now'
                     '</th><th class="num">then</th><th class="num">gain</th></tr>')
            for gain, now, later, d in data["gains"]:
                h.append(
                    f'<tr><td><a href="https://jiten.moe/decks/media/'
                    f'{d.get("deckId")}/detail">'
                    f'{esc(d.get("originalTitle") or d.get("englishTitle") or "?")}'
                    f'</a></td><td class="num">{now:.1f}%</td>'
                    f'<td class="num">{later:.1f}%</td>'
                    f'<td class="num up">{gain:+.1f}pp</td></tr>')
            h.append("</table></div>")

    h.append('<footer>Kanji figures computed locally from Jiten word lists; '
             'the jiten column is your account\'s own coverage. '
             'Built by wkjiten.</footer></main>')

    pace = round(wk_pace(cache) or 0, 1)
    blob = json.dumps({
        "level": lvl, "pace": pace,
        "known": "".join(sorted(known["kanji_known"])),
        "levels": known["kanji_level"],
        "types": MEDIA_TYPES,
    }, ensure_ascii=False, separators=(",", ":"))
    track = json.dumps({
        "level": lvl, "pace": pace,
        "titles": [{"t": deck_title(d),
                    "now": round(r["kanji_cov_occ"], 2),
                    "c": [round(p, 2) for _lv, p in r["curve"]]}
                   for d, r in sorted(rows, key=lambda r: -r[1]["kanji_cov_occ"])],
    }, ensure_ascii=False, separators=(",", ":"))
    head += f"<style>{SLIDER_CSS}{GRID_CSS}</style>"

    def compose(live: bool) -> str:
        parts, css = list(h), head
        parts.append(f"<script>const TRACK={track};const GRID={grid_json};"
                     f"const GRID_LEVEL={lvl};</script>"
                     f"<script>{SLIDER_JS}</script><script>{GRID_JS}</script>")
        links = list(sections)
        if live:
            # The browser panel goes near the top: it is what you came to use.
            css += f"<style>{BROWSE_CSS}</style>"
            parts[parts.index(BROWSE_SLOT)] = BROWSE_HTML
            parts.append(f"<script>const WK={blob};</script>"
                         f"<script>{BROWSE_JS}</script>")
            links.insert(0, ("browse", "Browse jiten.moe"))
        else:
            parts[parts.index(BROWSE_SLOT)] = ""
        parts[parts.index(NAV_SLOT)] = (
            "<nav>" + "".join(f'<a href="#{s}">{esc(t)}</a>' for s, t in links)
            + "</nav>")
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                + css + "</head><body>" + "".join(parts) + "</body></html>")

    if both:
        return compose(False), compose(True)
    return compose(interactive)


def cmd_report(args) -> None:
    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    html = build_report_html(args, key, cache, known)

    out = args.out or os.path.join(HERE, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nwrote {out}")
    if not args.no_open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out).replace("\\", "/"))


def cmd_serve(args) -> None:
    """Serve the dashboard locally, proxying the API so search works.

    api.jiten.moe sends no CORS headers, so a page loaded from file:// is not
    allowed to read its responses. Putting a tiny local server in front solves
    that, and keeps the API key on this machine instead of baking it into a
    file that could be shared by accident.
    """
    # Aliased: a plain `import http.server` would shadow this module's own
    # http() helper inside this function, and the proxy needs it.
    import http.server as httpserver
    import webbrowser

    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    print("Building the dashboard...")
    static, live = build_report_html(args, key, cache, known, both=True)
    page = live.encode("utf-8")
    saved = args.out or os.path.join(HERE, "report.html")
    with open(saved, "w", encoding="utf-8") as f:
        f.write(static)
    print(f"saved a static copy to {saved}")

    class Handler(httpserver.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        def _send(self, status, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

        def _proxy(self, body: bytes | None):
            # Only the Jiten API, and only under /api/ - this server is a
            # bridge for the page, not an open relay.
            path = self.path
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            status, payload, headers = http(
                JITEN_API + path,
                method="POST" if body is not None else "GET",
                headers=jiten_headers(key), body=body,
                content_type="application/json" if body is not None else None,
                timeout=180)
            self._send(status, payload,
                       headers.get("Content-Type", "application/json"))

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            self._proxy(None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._proxy(self.rfile.read(length))

    # Loopback only: nothing on the network can reach the proxy or the key.
    server = httpserver.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\nDashboard with live search: {url}")
    print("Search box is at the top. Press Ctrl+C here when you are done.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def cmd_text(args) -> None:
    """wanilog-style read-check on arbitrary text you paste in."""
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    text = open(args.path, encoding="utf-8").read() if args.path else sys.stdin.read()
    tokens = KANJI_RE.findall(text)
    res = analyse_deck(tokens, known)
    print(f"{len(text):,} chars, {res['total_kanji_occ']:,} kanji occurrences, "
          f"{res['unique_kanji']} unique")
    print(f"kanji coverage: {res['kanji_cov_occ']:.2f}% by occurrence, "
          f"{res['kanji_cov_unique']:.2f}% unique")
    top = [(ch, n) for ch, n in res["kanji_occ"].most_common()
           if ch not in res["kanji_known"]][:args.top]
    if top:
        print("unknown: " + "  ".join(
            f"{ch}({n}, L{res['kanji_level'].get(ch, '-')})" for ch, n in top))


# --------------------------------------------------------------------------

def main() -> None:
    if sys.platform == "win32":
        # Windows consoles default to a legacy codepage; piped Japanese text
        # arrives as UTF-8 bytes and would otherwise be mojibake.
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    p = argparse.ArgumentParser(
        prog="wkjiten",
        description="WaniKani knowledge -> jiten.moe deck coverage")

    def add_common(parser, suppress: bool = False):
        """Shared flags, accepted both before and after the subcommand.

        On the subparsers the defaults are suppressed so an omitted flag does
        not clobber the value already parsed by the main parser.
        """
        d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
        parser.add_argument("--wk-token", default=d(None),
                            help="WaniKani personal access token")
        parser.add_argument("--jiten-key", default=d(None),
                            help="Jiten API key (optional for public reads)")
        parser.add_argument("--refresh", action="store_true",
                            default=d(False), help="re-fetch WaniKani data")
        parser.add_argument("--mode", choices=["srs", "level"], default=d("srs"),
                            help="srs: known = SRS stage >= --min-stage (default). "
                                 "level: known = everything up to --level.")
        parser.add_argument("--min-stage", type=int, default=d(5),
                            help="SRS stage that counts as known (5=Guru I, 9=Burned)")
        parser.add_argument("--level", type=int, default=d(None),
                            help="level cutoff for --mode level")
        parser.add_argument("--top", type=int, default=d(25),
                            help="how many unknown kanji to list")
        parser.add_argument("--sleep", type=float, default=d(6.0),
                            help="seconds between decks (Jiten limits heavy endpoints)")

    add_common(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    def subparser(name, **kw):
        sp = sub.add_parser(name, **kw)
        add_common(sp, suppress=True)
        return sp

    s = subparser("export", help="write your WaniKani words to a Jiten-importable txt")
    s.add_argument("--out")
    s.add_argument("--normalize", action="store_true", help="NFKC-normalise the words")
    s.set_defaults(func=cmd_export)

    s = subparser("push", help="upload that txt to Jiten as known words")
    s.add_argument("--file")
    s.add_argument("--parse-words", action="store_true",
                   help="let Jiten resolve conjugated surfaces through its parser")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_push)

    s = subparser("search", help="browse jiten.moe by title, type and genre")
    s.add_argument("query", nargs="?", default="",
                   help="title fragment; omit to browse by filter alone")
    s.add_argument("--type", help="anime, manga, novel, visual novel, game, ...")
    s.add_argument("--genre", help="comma-separated genres")
    s.add_argument("--min-chars", type=int, help="skip anything shorter")
    s.add_argument("--sort", default="wordCount",
                   help="title, difficulty, charCount, wordCount, coverage, "
                        "releaseDate, communityVotes (default wordCount)")
    s.add_argument("--ascending", action="store_true")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_search)

    s = subparser("when", help="should you read this yet, and if not, when")
    s.add_argument("targets", nargs="+",
                   help="deck ids or title fragments")
    s.add_argument("--comfortable", type=float, default=95.0,
                   help="kanji coverage you consider comfortable (default 95)")
    s.set_defaults(func=cmd_when)

    s = subparser("deck", help="coverage report for one or more decks")
    s.add_argument("deck_ids", nargs="+", type=int)
    s.set_defaults(func=cmd_deck)

    s = subparser("batch", help="coverage for many decks -> csv")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--search", help="add every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--deck-file", help="file of deck ids, one per line "
                                       "(default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists: ongoing, planning, "
                        "completed, fav, dropped. Empty string to disable.")
    s.add_argument("--alert-at", type=float, default=80.0,
                   help="shout when a title crosses this coverage (default 80)")
    s.add_argument("--out")
    s.set_defaults(func=cmd_batch)

    s = subparser("parts", help="per-episode / per-volume breakdown of a series")
    s.add_argument("deck_ids", nargs="+", type=int)
    s.add_argument("--kanji", action="store_true",
                   help="also compute kanji coverage per part (one request each)")
    s.add_argument("--limit", type=int, default=15, help="parts to show")
    s.add_argument("--flat", type=float, default=5.0,
                   help="spread below this counts as uniform (default 5pp)")
    s.set_defaults(func=cmd_parts)

    s = subparser("next", help="kanji worth learning next, priced in coverage")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--deck-file", help="file of deck ids (default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists")
    s.add_argument("--search", help="use every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_next)

    s = subparser("gap", help="export the words in a title you cannot read yet")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--deck-file", help="file of deck ids (default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists")
    s.add_argument("--search", help="use every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--target", type=float,
                   help="stop once the deck reaches this %% coverage, e.g. 95")
    s.add_argument("--min-occurrences", type=int,
                   help="skip words appearing fewer than this many times")
    s.add_argument("--no-sentences", action="store_true",
                   help="leave out example sentences")
    s.add_argument("--out", help="directory to write into (default: here)")
    s.set_defaults(func=cmd_gap)

    s = subparser("edge", help="titles easier for you than their difficulty says")
    s.add_argument("--sample", type=int, default=100,
                   help="titles to sample per media type (default 100)")
    s.add_argument("--top-n", type=int, default=15)
    s.set_defaults(func=cmd_edge)

    s = subparser("leeches", help="Apprentice items ranked by what they block")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--deck-file", help="file of deck ids (default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists: ongoing, planning, "
                        "completed, fav, dropped. Empty string to disable.")
    s.add_argument("--search", help="use every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--max-stage", type=int, default=4,
                   help="highest SRS stage still counted as a leech (4=Apprentice IV)")
    s.set_defaults(func=cmd_leeches)

    s = subparser("report", help="write an HTML dashboard and open it")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--deck-file", help="file of deck ids (default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists: ongoing, planning, "
                        "completed, fav, dropped. Empty string to disable.")
    s.add_argument("--search", help="use every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--out", help="output path (default: report.html)")
    s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s.add_argument("--no-recommend", action="store_true",
                   help="skip the recommendation sections (faster)")
    s.add_argument("--top-n", type=int, default=5)
    s.add_argument("--soon-levels", type=int, default=5)
    s.add_argument("--soon-limit", type=int, default=6)
    s.add_argument("--soon-pool", type=int, default=4)
    s.set_defaults(func=cmd_report)

    s = subparser("serve", help="dashboard with live jiten.moe search in a browser")
    s.add_argument("deck_ids", nargs="*", type=int)
    s.add_argument("--deck-file", help="file of deck ids (default: decks.txt)")
    s.add_argument("--status", default="ongoing,planning",
                   help="pull titles from your Jiten lists")
    s.add_argument("--search", help="use every deck matching this title filter")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--out", help="where to save the static copy")
    s.add_argument("--no-open", action="store_true")
    s.add_argument("--no-recommend", action="store_true",
                   help="skip the recommendation sections (starts faster)")
    s.add_argument("--verbose", action="store_true", help="log every request")
    s.add_argument("--top-n", type=int, default=5)
    s.add_argument("--soon-levels", type=int, default=5)
    s.add_argument("--soon-limit", type=int, default=6)
    s.add_argument("--soon-pool", type=int, default=4)
    s.set_defaults(func=cmd_serve)

    s = subparser("status", help="progress since last run + what to read next")
    s.add_argument("--top-n", type=int, default=5,
                   help="titles per media type (default 5)")
    s.add_argument("--soon-levels", type=int, default=5,
                   help="how many levels ahead to project (default 5)")
    s.add_argument("--soon-limit", type=int, default=6,
                   help="how many near-miss titles to analyse; 0 to skip")
    s.add_argument("--soon-pool", type=int, default=4,
                   help="candidates to pull past the top N, per media type")
    s.set_defaults(func=cmd_status)

    s = subparser("text", help="read-check arbitrary Japanese text")
    s.add_argument("path", nargs="?", help="file to read (default: stdin)")
    s.set_defaults(func=cmd_text)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
