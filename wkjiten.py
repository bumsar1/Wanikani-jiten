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

def analyse_deck(tokens: list[str], known: dict) -> dict:
    kanji_known = known["kanji_known"]
    kanji_level = known["kanji_level"]
    words_known = known["words_known_set"]

    word_occ = Counter(tokens)
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

    for deck_id in args.deck_ids:
        deck = jiten_deck_detail(deck_id, key)
        tokens = jiten_deck_tokens(deck_id, key)
        res = analyse_deck(tokens, known)
        report(deck, res, known, args)
        if len(args.deck_ids) > 1:
            time.sleep(args.sleep)


def cmd_batch(args) -> None:
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)
    key = jiten_key(args.jiten_key)

    ids: list[int] = list(args.deck_ids or [])
    if args.search:
        for row in jiten_search(args.search, key, limit=args.limit):
            ids.append(row["deckId"])
    deck_file = args.deck_file or os.path.join(HERE, "decks.txt")
    if not ids and os.path.exists(deck_file):
        with open(deck_file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line.isdigit():
                    ids.append(int(line))
    if not ids:
        raise SystemExit(f"nothing to do: pass deck ids, --search, or list them in "
                         f"{deck_file}")
    ids = list(dict.fromkeys(ids))

    out = args.out or os.path.join(HERE, "coverage.csv")
    summary: list[tuple] = []
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["deckId", "title", "type", "chars", "difficulty",
                    "kanji_cov_occ", "kanji_cov_unique",
                    "jiten_coverage", "jiten_unique_coverage",
                    "vocab_cov_occ_offline", "unique_kanji",
                    "lvl_for_95", "lvl_for_98", "ceiling"])
        for i, deck_id in enumerate(ids):
            deck = jiten_deck_detail(deck_id, key)
            tokens = jiten_deck_tokens(deck_id, key)
            res = analyse_deck(tokens, known)
            title = (deck.get("originalTitle") or deck.get("englishTitle")
                     or deck.get("romajiTitle") or "")
            w.writerow([
                deck_id, title, MEDIA_TYPES.get(deck.get("mediaType"), "?"),
                deck.get("characterCount") or 0, deck.get("difficulty") or 0,
                f"{res['kanji_cov_occ']:.2f}", f"{res['kanji_cov_unique']:.2f}",
                deck.get("coverage") if deck.get("coverage") is not None else "",
                deck.get("uniqueCoverage") if deck.get("coverage") is not None else "",
                f"{res['word_cov_occ']:.2f}",
                res["unique_kanji"],
                level_for(res["curve"], 95) or "", level_for(res["curve"], 98) or "",
                f"{100 - res['not_in_wk_pct']:.2f}",
            ])
            live = deck.get("coverage")
            summary.append((res["kanji_cov_occ"], live, title,
                            MEDIA_TYPES.get(deck.get("mediaType"), "?"),
                            deck.get("characterCount") or 0,
                            level_for(res["curve"], 95),
                            100 - res["not_in_wk_pct"]))
            vocab = f"{live:.2f}% (jiten)" if live is not None else \
                    f"{res['word_cov_occ']:.2f}% (offline)"
            print(f"[{i+1}/{len(ids)}] {title}  kanji {res['kanji_cov_occ']:.2f}%  "
                  f"vocab {vocab}")
            time.sleep(args.sleep)

    print()
    print(f"{'kanji':>7} {'jiten':>7} {'loft':>7} {'lvl95':>6}  {'type':<12} "
          f"{'tegn':>10}  titel")
    for k, live, title, mtype, chars, lvl95, ceiling in sorted(summary, reverse=True):
        j = f"{live:6.2f}%" if live is not None else "     -"
        print(f"{k:6.2f}% {j} {ceiling:6.2f}% {lvl95 or '--':>6}  {mtype:<12} "
              f"{chars:>10,}  {title}")
    print(f"\nwrote {out}")


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
    (4, "romaner", 30000),
    (7, "visual novels", 50000),
    (1, "anime", 20000),
    (9, "manga", 30000),
    (6, "spil", 30000),
]


def progress_since_last(args) -> None:
    if not os.path.exists(WK_CACHE_PREV):
        print("Fremgang: ingen tidligere måling endnu - den kommer næste gang.")
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

    print(f"Fremgang siden {prev_cache.get('fetched_at', '?')[:10]}")
    print(f"  kanji  {len(prev['kanji_known']):>5} -> {len(cur['kanji_known']):<5} "
          f"({len(new_kanji):+d})")
    print(f"  ord    {len(prev['words_known_set']):>5} -> "
          f"{len(cur['words_known_set']):<5} ({len(new_words):+d})")
    if lvl_before != lvl_now:
        print(f"  level  {lvl_before} -> {lvl_now}   nice.")
    if new_kanji:
        shown = sorted(new_kanji, key=lambda c: cur["kanji_level"].get(c, 99))
        print("  nye kanji: " + " ".join(shown[:40])
              + (f"  (+{len(shown) - 40} mere)" if len(shown) > 40 else ""))


def cmd_status(args) -> None:
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit("`status` skal bruge en Jiten API-key - se README.")
    cache = wk_load(args.wk_token, refresh=args.refresh)
    known = wk_known(cache, min_stage=args.min_stage, mode=args.mode, level=args.level)

    print()
    print("=" * 70)
    print(f"  {cache.get('username')} - WaniKani level {cache.get('level')}, "
          f"{len(known['kanji_known'])} kanji og "
          f"{len(known['words_known_set'])} ord")
    print("=" * 70)
    print()
    progress_since_last(args)

    print()
    print("-" * 70)
    print(f"  Bedste titler for dig lige nu (coverage fra din Jiten-konto)")
    print("-" * 70)
    candidates: list[dict] = []
    for mtype, label, min_chars in RECOMMEND_TYPES:
        rows = jiten_top_by_coverage(mtype, key, min_chars=min_chars,
                                     limit=args.top_n + args.soon_pool)
        print(f"\n{label}:")
        for d in rows[:args.top_n]:
            title = d.get("originalTitle") or d.get("englishTitle") or "?"
            print(f"  {d.get('coverage') or 0:>6}%  "
                  f"{d.get('characterCount') or 0:>9,}  "
                  f"jiten.moe/decks/media/{d.get('deckId')}  {title}")
        candidates.extend(rows[args.top_n:])
        time.sleep(0.5)

    if args.soon_limit <= 0 or not candidates:
        return

    print()
    print("-" * 70)
    print(f"  Snart inden for rækkevidde - kanji-coverage nu vs. level "
          f"{(cache.get('level') or 0) + args.soon_levels}")
    print("-" * 70)
    print(f"Undersøger {min(args.soon_limit, len(candidates))} af "
          f"{len(candidates)} kandidater (hæv med --soon-limit).")

    lvl_now = cache.get("level") or 1
    target = min(60, lvl_now + args.soon_levels)
    gains = []
    for d in candidates[:args.soon_limit]:
        deck_id = d.get("deckId")
        try:
            tokens = jiten_deck_tokens(deck_id, key)
        except SystemExit as e:
            print(f"  sprang {deck_id} over: {e}")
            continue
        res = analyse_deck(tokens, known)
        now = res["kanji_cov_occ"]
        later = res["curve"][target - 1][1]
        gains.append((later - now, now, later, d))
        time.sleep(args.sleep)

    print(f"\n{'nu':>7} {'ved lvl':>8} {'gevinst':>8}   titel")
    for gain, now, later, d in sorted(gains, reverse=True):
        title = d.get("originalTitle") or d.get("englishTitle") or "?"
        print(f"{now:6.2f}% {later:7.2f}% {gain:+7.2f}pp   {title}")
        print(f"{'':29}jiten.moe/decks/media/{d.get('deckId')}")


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
    s.add_argument("--out")
    s.set_defaults(func=cmd_batch)

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
