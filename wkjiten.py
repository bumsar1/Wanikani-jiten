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
        chars = s["data"].get("characters")
        if not chars:
            continue  # radicals with image-only characters; none for these types normally
        subjects[s["id"]] = {
            "type": s["object"],           # kanji | vocabulary | kana_vocabulary
            "characters": chars,
            "level": s["data"]["level"],
        }

    assignments: dict[int, int] = {}       # subject_id -> srs_stage
    for a in wk_paged("assignments?started=true", token):
        assignments[a["data"]["subject_id"]] = a["data"]["srs_stage"]

    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "username": user.get("username"),
        "level": user.get("level"),
        "subjects": {str(k): v for k, v in subjects.items()},
        "assignments": {str(k): v for k, v in assignments.items()},
    }


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


def jiten_search(query: str, api_key: str | None, limit: int = 15) -> list[dict]:
    url = (f"{JITEN_API}/api/media-deck/get-media-decks"
           f"?titleFilter={urllib.parse.quote(query)}&sortBy=wordCount&sortOrder=1")
    data = get_json(url, headers=jiten_headers(api_key))
    rows = data.get("data") if isinstance(data, dict) else data
    return (rows or [])[:limit]


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


def level_for(curve: list[tuple[int, float]], target: float) -> int | None:
    for lv, pct in curve:
        if pct >= target:
            return lv
    return None


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
    rows = jiten_search(args.query, jiten_key(args.jiten_key), limit=args.limit)
    if not rows:
        print("no decks found")
        return
    print(f"{'id':>8}  {'type':<13} {'chars':>9} {'diff':>5}  title")
    for d in rows:
        title = d.get("englishTitle") or d.get("romajiTitle") or d.get("originalTitle") or ""
        print(f"{d.get('deckId', 0):>8}  {MEDIA_TYPES.get(d.get('mediaType'), '?'):<13} "
              f"{d.get('characterCount') or 0:>9} {d.get('difficulty') or 0:>5.2f}  {title}")


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
                            100 - res["not_in_wk_pct"]))
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

    log_history(history)
    past = read_history()

    print()
    print(f"{'kanji':>7} {'jiten':>7} {'ceiling':>8} {'lvl95':>6} {'trend':>16}  "
          f"{'list':<18} {'type':<12} {'chars':>10}  title")
    for k, live, title, mtype, chars, lvl95, ceiling in sorted(summary, reverse=True):
        j = f"{live:6.2f}%" if live is not None else "     -"
        did = next((h["deckId"] for h in history if h["title"] == title), None)
        trend = history_trend(past.get(did, []))
        t = (f"{trend[1] - trend[0]:+.2f}pp / {trend[2]}d" if trend else "")
        lst = STATUS_LABELS.get(DECK_STATUS.get(did), "")
        print(f"{k:6.2f}% {j} {ceiling:7.2f}% {lvl95 or '--':>6} {t:>16}  "
              f"{lst:<18} {mtype:<12} {chars:>10,}  {title}")
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
    # Stage 1-4 is Apprentice: started, repeatedly seen, not yet passed.
    struggling: dict[str, tuple[int, int, str]] = {}   # chars -> (stage, level, type)
    for sid, s in subjects.items():
        stage = assignments.get(sid)
        if stage is not None and 1 <= stage <= args.max_stage:
            struggling[s["characters"]] = (stage, s["level"], s["type"])

    kanji_occ: Counter[str] = Counter()
    word_occ: Counter[str] = Counter()
    titles: dict[str, set] = {}
    for deck, words in load_decks(ids, key, args.sleep, progress=True):
        title = deck_title(deck)
        for word, n in words.items():
            word_occ[word] += n
            for ch in set(KANJI_RE.findall(word)):
                if ch in struggling:
                    titles.setdefault(ch, set()).add(title)
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
        print(f"{'occur':>7}  {'kanji':<6} {'stage':<16} {'lvl':>4}  appears in")
        for n, ch, stage, lv, _typ in rows[:args.top]:
            where = sorted(titles.get(ch, ()))
            shown = ", ".join(where[:2]) + (f" +{len(where) - 2}" if len(where) > 2 else "")
            print(f"{n:>7}  {ch:<6} {SRS_STAGE_NAMES.get(stage, '?'):<16} "
                  f"{lv:>4}  {shown}")

    vocab_rows = [(word_occ.get(w, 0), w) + struggling[w] for w in struggling
                  if word_occ.get(w, 0) > 0 and w not in known["words_known_set"]]
    vocab_rows.sort(reverse=True)
    if vocab_rows:
        print(f"\n{'occur':>7}  {'word':<12} {'stage':<16} {'lvl':>4}")
        for n, word, stage, lv, _typ in vocab_rows[:args.top]:
            print(f"{n:>7}  {word:<12} {SRS_STAGE_NAMES.get(stage, '?'):<16} {lv:>4}")

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
    return {"recommendations": recommendations, "gains": sorted(gains, reverse=True),
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
                  f"jiten.moe/decks/media/{d.get('deckId')}  "
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
        print(f"{'':29}jiten.moe/decks/media/{d.get('deckId')}")


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
:root { --bg:#fbfaf8; --fg:#1c1a17; --muted:#6b6660; --line:#e3ded6;
        --card:#fff; --accent:#c2410c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#171614; --fg:#eae7e2; --muted:#a09a92; --line:#332f2a;
          --card:#211f1c; --accent:#fb923c; }
}
* { box-sizing:border-box; }
body { margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }
main { max-width:860px; margin:0 auto; }
h1 { font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:17px; margin:40px 0 12px; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:0 0 28px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 8px; }
.card { flex:1 1 150px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:14px 16px; }
.card .n { font-size:24px; font-weight:600; letter-spacing:-.02em; }
.card .l { color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.06em; }
.card .d { color:var(--accent); font-size:13px; font-weight:600; }
.wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { border-collapse:collapse; width:100%; font-size:14px; min-width:520px; }
th { text-align:left; font-weight:600; color:var(--muted); font-size:12px;
  text-transform:uppercase; letter-spacing:.05em; padding:0 10px 6px 0;
  border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:7px 10px 7px 0; border-bottom:1px solid var(--line);
  vertical-align:middle; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums;
  white-space:nowrap; }
a { color:inherit; text-decoration:none; border-bottom:1px solid var(--line); }
a:hover { border-bottom-color:var(--accent); }
.meter { display:block; width:110px; height:7px; background:var(--line);
  border-radius:4px; overflow:hidden; }
.meter i { display:block; height:100%; background:var(--accent); }
.up { color:#15803d; font-weight:600; }
.chart { width:100%; height:auto; display:block; margin:4px 0 8px; }
.grid { stroke:var(--line); stroke-width:1; }
.you { stroke:var(--accent); stroke-width:1; stroke-dasharray:3 3; }
.tick, .legend { fill:var(--muted); font-size:11px; }
.legend { fill:var(--fg); }
.kanji { font-size:19px; }
.empty { color:var(--muted); font-style:italic; }
footer { color:var(--muted); font-size:12px; margin-top:44px;
  border-top:1px solid var(--line); padding-top:14px; }
"""


def cmd_report(args) -> None:
    """Everything the terminal prints, as one self-contained HTML page."""
    key = jiten_key(args.jiten_key)
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
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
    h = ["<main>",
         f"<h1>{esc(cache.get('username'))} on jiten.moe</h1>",
         f'<p class="sub">WaniKani level {lvl} &middot; generated '
         f'{time.strftime("%Y-%m-%d %H:%M")}</p>', '<div class="cards">']

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

    h.append("<h2>Your tracked titles</h2>")
    h.append('<div class="wrap"><table><tr><th>title</th><th>list</th>'
             '<th class="num">kanji</th>'
             '<th></th><th class="num">jiten</th><th class="num">lvl for 95%</th>'
             '<th class="num">ceiling</th><th class="num">trend</th></tr>')
    for deck, res in sorted(rows, key=lambda r: -r[1]["kanji_cov_occ"]):
        deck_id = deck.get("deckId")
        live = deck.get("coverage")
        trend = history_trend(past.get(deck_id, []))
        t = (f'<span class="up">{trend[1] - trend[0]:+.1f}pp</span> / {trend[2]}d'
             if trend and trend[1] > trend[0] else
             (f"{trend[1] - trend[0]:+.1f}pp / {trend[2]}d" if trend else "&mdash;"))
        k = res["kanji_cov_occ"]
        h.append(
            f'<tr><td><a href="https://jiten.moe/decks/media/{deck_id}">'
            f'{esc(deck_title(deck))}</a></td>'
            f'<td>{esc(STATUS_LABELS.get(DECK_STATUS.get(deck_id), "—"))}</td>'
            f'<td class="num">{k:.1f}%</td>'
            f'<td><span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
            f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
            f'<td class="num">{level_for(res["curve"], 95) or "&mdash;"}</td>'
            f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
            f'<td class="num">{t}</td></tr>')
    h.append("</table></div>")

    h.append("<h2>What each level would buy you</h2>")
    h.append(svg_curves(curves, lvl))

    h.append("<h2>Coverage over time</h2>")
    h.append(svg_history(past, titles))

    # Leeches: Apprentice items weighted by how often they block your titles.
    subjects, assignments = cache["subjects"], cache["assignments"]
    struggling = {s["characters"]: (assignments[sid], s["level"])
                  for sid, s in subjects.items()
                  if s["type"] == "kanji" and 1 <= assignments.get(sid, 0) <= 4}
    occ: Counter[str] = Counter()
    for _deck, res in rows:
        occ.update(res["kanji_occ"])
    leeches = sorted(((n, ch) + struggling[ch] for ch, n in occ.items()
                      if ch in struggling and ch not in known["kanji_known"]),
                     reverse=True)[:24]
    h.append("<h2>Leeches blocking your reading</h2>")
    if leeches:
        h.append('<p class="sub">Apprentice kanji, ranked by how often they appear '
                 'in the titles above. Already in your review queue.</p>')
        h.append('<div class="wrap"><table><tr><th>kanji</th><th class="num">'
                 'occurrences</th><th>stage</th><th class="num">wk level</th></tr>')
        for n, ch, stage, klvl in leeches:
            h.append(f'<tr><td class="kanji">{esc(ch)}</td>'
                     f'<td class="num">{n:,}</td>'
                     f'<td>{SRS_STAGE_NAMES.get(stage, "?")}</td>'
                     f'<td class="num">{klvl}</td></tr>')
        h.append("</table></div>")
    else:
        h.append('<p class="empty">Nothing in Apprentice shows up in your tracked '
                 'titles.</p>')

    if key and not args.no_recommend:
        data = collect_status(args, key, cache, known)
        h.append("<h2>Best titles for you right now</h2>")
        h.append('<div class="wrap"><table><tr><th>type</th><th>title</th>'
                 '<th class="num">coverage</th><th class="num">chars</th></tr>')
        for label, recs in data["recommendations"]:
            for d in recs:
                h.append(
                    f'<tr><td>{esc(label)}</td>'
                    f'<td><a href="https://jiten.moe/decks/media/{d.get("deckId")}">'
                    f'{esc(d.get("originalTitle") or d.get("englishTitle") or "?")}'
                    f'</a></td>'
                    f'<td class="num">{d.get("coverage") or 0}%</td>'
                    f'<td class="num">{d.get("characterCount") or 0:,}</td></tr>')
        h.append("</table></div>")
        if data["gains"]:
            h.append(f'<h2>Nearly within reach at level {data["target_level"]}</h2>')
            h.append('<div class="wrap"><table><tr><th>title</th><th class="num">now'
                     '</th><th class="num">then</th><th class="num">gain</th></tr>')
            for gain, now, later, d in data["gains"]:
                h.append(
                    f'<tr><td><a href="https://jiten.moe/decks/media/'
                    f'{d.get("deckId")}">'
                    f'{esc(d.get("originalTitle") or d.get("englishTitle") or "?")}'
                    f'</a></td><td class="num">{now:.1f}%</td>'
                    f'<td class="num">{later:.1f}%</td>'
                    f'<td class="num up">{gain:+.1f}pp</td></tr>')
            h.append("</table></div>")

    h.append('<footer>Kanji figures computed locally from Jiten word lists; '
             'the jiten column is your account\'s own coverage. '
             'Built by wkjiten.</footer></main>')

    out = args.out or os.path.join(HERE, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                + head + "</head><body>" + "".join(h) + "</body></html>")
    print(f"\nwrote {out}")
    if not args.no_open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out).replace("\\", "/"))


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

    s = subparser("search", help="find deck ids by title")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_search)

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
    s.add_argument("--out")
    s.set_defaults(func=cmd_batch)

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
