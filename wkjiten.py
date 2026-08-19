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
import base64
import calendar
import csv
import json
import os
import re
import sys
import time
import unicodedata
import io
import urllib.error
import urllib.parse
import urllib.request
import zipfile
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

ASSET_DIR = os.path.join(HERE, "assets")
LOGO_FILE = os.path.join(ASSET_DIR, "logo.png")   # 512px, the whole character
ICON_FILE = os.path.join(ASSET_DIR, "icon.png")   # 128px, just the head: legible
                                                  # at the 16px a browser tab uses

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

def read_key_file(path: str) -> str | None:
    """First real line of a key file, ignoring blanks and # comments.

    The shipped templates carry their own instructions, so the reader has to
    skip past them rather than treating the guidance as a key.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def wk_token(cli_token: str | None) -> str:
    token = cli_token or os.environ.get("WANIKANI_TOKEN")
    if not token:
        token = read_key_file(os.path.join(HERE, "wanikani_token.txt"))
    if not token:
        raise SystemExit(
            "No WaniKani token. Create a read-only personal access token at\n"
            "  https://www.wanikani.com/settings/personal_access_tokens\n"
            "then either set WANIKANI_TOKEN, pass --wk-token, or save it in\n"
            f"  {os.path.join(HERE, 'wanikani_token.txt')}"
        )
    return token


def wk_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Wanikani-Revision": WK_REVISION}


def wk_paged(path: str, token: str) -> Iterable[dict]:
    """Walk a WaniKani collection, yielding each resource."""
    url = f"{WK_API}/{path}"
    headers = wk_headers(token)
    page = 0
    while url:
        page += 1
        print(f"  wanikani: {path.split('?')[0]} page {page}...", file=sys.stderr)
        data = get_json(url, headers=headers)
        for item in data.get("data", []):
            yield item
        url = (data.get("pages") or {}).get("next_url")


def togo_of(stage: int, gaps: dict, passing: int) -> int:
    """Seconds from the next review at `stage` until the item passes."""
    return sum(gaps.get(k, 0) for k in range(max(stage, 0) + 1, passing))


def wk_fetch(token: str) -> dict:
    """Fetch everything we need from WaniKani and shape it for the cache."""
    user = get_json(f"{WK_API}/user", headers=wk_headers(token))["data"]

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
            # Levels 1-2 run on the accelerated schedule, so the wait to Guru
            # is not the same number for everyone.
            "srs": d.get("spaced_repetition_system_id") or 1,
            # The radicals a kanji is locked behind. Without these, a level
            # whose kanji are mostly still locked has no date at all.
            "parts": d.get("component_subject_ids") or [],
        }

    # The intervals the SRS actually uses, rather than four numbers copied out
    # of a wiki: they differ between the standard and accelerated systems, and
    # the level-up estimate is only worth showing if they are right.
    srs_gap: dict[int, dict[int, int]] = {}
    srs_pass: dict[int, int] = {}
    try:
        for s in get_json(f"{WK_API}/spaced_repetition_systems",
                          headers=wk_headers(token))["data"]:
            d = s["data"]
            srs_pass[s["id"]] = d["passing_stage_position"]
            srs_gap[s["id"]] = {st["position"]: st.get("interval") or 0
                                for st in d["stages"]}
    except SystemExit:
        pass

    # All of them, not just the started ones: a level is not finished until 90%
    # of its kanji have passed, and the ones still sitting in your lesson queue
    # are exactly the ones holding that up. Everything downstream reads this
    # with .get(sid, 0), so the extra stage-0 rows change nothing for it.
    assignments: dict[int, int] = {}       # subject_id -> srs_stage
    lessons_by_month: Counter[str] = Counter()
    passed_by_month: Counter[str] = Counter()
    state: dict[int, dict] = {}
    for a in wk_paged("assignments", token):
        d = a["data"]
        sid = d["subject_id"]
        assignments[sid] = d["srs_stage"]
        if d.get("started_at"):
            lessons_by_month[d["started_at"][:7]] += 1
        if d.get("passed_at"):
            passed_by_month[d["passed_at"][:7]] += 1
        state[sid] = {"s": d["srs_stage"], "at": d.get("available_at"),
                      "locked": not d.get("unlocked_at")}

    progressions = [
        {"level": p["data"]["level"], "started_at": p["data"].get("started_at"),
         "passed_at": p["data"].get("passed_at")}
        for p in wk_paged("level_progressions", token)
    ]

    # Just this level's kanji, with the wait from their next review to Guru
    # worked out here - so the page can measure it against the clock at the
    # moment you look, not the moment this was fetched.
    level = user.get("level") or 0
    level_kanji = []
    for sid, s in subjects.items():
        if s["type"] != "kanji" or s["level"] != level:
            continue
        st = state.get(sid) or {"s": 0, "at": None, "locked": True}
        system = s.get("srs") or 1
        gaps = srs_gap.get(system) or {}
        passing = srs_pass.get(system, 5)

        def ladder(stage: int) -> int:
            """Seconds from the next review at `stage` to passing."""
            return sum(gaps.get(k, 0) for k in range(max(stage, 0) + 1, passing))

        stage = st["s"]
        # A locked kanji waits on its radicals, so carry what is left of each
        # one that has not passed yet - the deepest of them sets the date.
        deps = []
        if st["locked"]:
            for part in s.get("parts") or []:
                ps = state.get(part)
                if ps is None:
                    deps.append({"at": None, "togo": ladder(0), "unknown": True})
                elif ps["s"] < passing:
                    deps.append({"at": ps["at"], "togo": ladder(ps["s"]),
                                 "unknown": False})
        level_kanji.append({"c": s["characters"], "s": stage, "at": st["at"],
                            "locked": st["locked"], "togo": togo_of(stage, gaps, passing),
                            "lesson": ladder(0), "deps": deps,
                            "passed": stage >= passing})

    # /reviews was retired - WaniKani stopped keeping the records and it now
    # answers an empty collection for every account. review_statistics is what
    # is left: lifetime answer counts per subject, with no dates on them.
    answers = {"correct": 0, "incorrect": 0}
    try:
        for r in wk_paged("review_statistics", token):
            d = r["data"]
            answers["correct"] += (d.get("meaning_correct") or 0) +                                   (d.get("reading_correct") or 0)
            answers["incorrect"] += (d.get("meaning_incorrect") or 0) +                                     (d.get("reading_incorrect") or 0)
    except SystemExit:
        pass

    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "username": user.get("username"),
        "level": user.get("level"),
        "subjects": {str(k): v for k, v in subjects.items()},
        "assignments": {str(k): v for k, v in assignments.items()},
        "progressions": progressions,
        "lessons_by_month": dict(lessons_by_month),
        "passed_by_month": dict(passed_by_month),
        "level_kanji": level_kanji,
        "answers": answers,
    }


def level_progress(cache: dict) -> dict | None:
    """How far into the current level you are, and the earliest you could leave.

    WaniKani moves you up when 90% of the level's kanji have passed to Guru, so
    that ratio is the progress - not lessons done, not items burned. The date is
    the earliest possible one: every remaining review answered correctly, first
    time. Miss one and it slides.
    """
    items = cache.get("level_kanji")
    if not items:
        return None
    total = len(items)
    needed = -(-total * 9 // 10)          # ceil(0.9 * total), WaniKani's rule
    passed = sum(1 for k in items if k["passed"])
    now = time.time()

    # When each unpassed item could reach Guru, assuming nothing is missed.
    when = []
    for k in items:
        if k["passed"]:
            when.append(0.0)
            continue
        if k["at"]:
            # Already in the SRS: its own next review starts the clock.
            when.append(max(now, iso_seconds(k["at"])) + k["togo"])
        elif not k["locked"]:
            # Sitting in the lesson queue: earliest is doing it right now.
            when.append(now + k.get("lesson", k["togo"]))
        else:
            # Locked. It unlocks when the last of its radicals passes, and only
            # then can the lesson be done, so the two waits are sequential.
            deps = k.get("deps") or []
            if any(d.get("unknown") for d in deps):
                when.append(None)
                continue
            unlock = max((max(now, iso_seconds(d["at"])) + d["togo"] if d["at"]
                          else now + d["togo"]) for d in deps) if deps else now
            when.append(unlock + k.get("lesson", k["togo"]))

    dated = sorted(t for t in when if t is not None)
    eta = dated[needed - 1] if len(dated) >= needed else None
    return {"level": cache.get("level") or 0, "total": total, "passed": passed,
            "needed": needed, "eta": eta,
            # Locked is what to tell you about; undated is why there may be no
            # date - a radical chain this has no state for at all.
            "locked": sum(1 for k in items if k["locked"] and not k["passed"]),
            "undated": sum(1 for t in when if t is None),
            "apprentice": sum(1 for k in items if 1 <= k["s"] <= 4),
            "unstarted": sum(1 for k in items if k["s"] == 0)}


def iso_seconds(stamp: str) -> float:
    """A WaniKani timestamp as a unix time. They are always UTC with a Z."""
    try:
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return time.time()


def until_text(seconds: float) -> str:
    """A wait, in the roundest unit that still says something."""
    d = seconds - time.time()
    if d <= 0:
        return "any review now"
    if d < 3600:
        return f"in {d / 60:.0f} minutes"
    if d < 86400:
        return f"in {d / 3600:.0f} hours"
    if d < 86400 * 14:
        return f"in {d / 86400:.1f} days"
    return f"in {d / 86400 / 7:.0f} weeks"


def month_totals(cache: dict) -> dict | None:
    """Lessons and passes for this month and this year, plus lifetime answers.

    Reviews per month are not on offer: WaniKani retired /reviews and now
    answers an empty collection for every account, so the only review numbers
    left are the lifetime totals in review_statistics, which carry no dates.
    """
    if "lessons_by_month" not in cache:
        return None            # fetched before any of this was collected
    month = time.strftime("%Y-%m")
    year = time.strftime("%Y")
    lessons = cache.get("lessons_by_month") or {}
    passes = cache.get("passed_by_month") or {}

    def span(table, prefix):
        return sum(n for k, n in table.items() if k.startswith(prefix))

    answers = cache.get("answers") or {}
    correct = answers.get("correct") or 0
    wrong = answers.get("incorrect") or 0
    return {"lessons_month": span(lessons, month), "lessons_year": span(lessons, year),
            "passed_month": span(passes, month), "passed_year": span(passes, year),
            "lessons_all": sum(lessons.values()), "passed_all": sum(passes.values()),
            "answers": correct + wrong,
            "accuracy": (correct / (correct + wrong) * 100) if correct + wrong else None}


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
        key = read_key_file(os.path.join(HERE, "jiten_key.txt"))
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

# Jiten's Genre enum. Genres and tags are separate filters over there: Romance
# is a genre, Boarding School is one of ~250 tags. Both take ids, and both
# intersect - two of either means titles carrying both.
GENRES = {
    1: "action", 2: "adventure", 3: "comedy", 4: "drama", 5: "ecchi",
    6: "fantasy", 7: "horror", 8: "mecha", 9: "music", 10: "mystery",
    11: "psychological", 12: "romance", 13: "sci-fi", 14: "slice of life",
    15: "sports", 16: "supernatural", 17: "thriller", 18: "adults only",
}


# Jiten records where a title lives elsewhere, typed by site. Which one is
# worth offering depends on the medium: someone reading a visual novel wants
# VNDB, someone watching anime wants AniList.
LINK_SITES = {2: "vndb", 3: "tmdb", 4: "anilist", 5: "mal", 6: "books",
              7: "imdb", 8: "igdb", 9: "syosetu"}
LINK_PREFERENCE = {
    1: (4, 5, 3),     # anime
    2: (3, 7, 4),     # drama
    3: (7, 3, 4),     # movie
    4: (6, 4, 5),     # novel
    6: (8, 4),        # video game
    7: (2, 4, 5),     # visual novel
    8: (9, 6, 4),     # web novel
    9: (4, 5, 6),     # manga
}


def outside_link(deck: dict) -> tuple[str, str] | None:
    """(label, url) for the site people actually look this medium up on."""
    links = {l.get("linkType"): l.get("url")
             for l in (deck.get("links") or []) if l.get("url")}
    if not links:
        return None
    order = list(LINK_PREFERENCE.get(deck.get("mediaType"), ())) + sorted(links)
    for kind in order:
        url = links.get(kind)
        if not url:
            continue
        if kind == 6 and "googleapis.com/books/v1/volumes/" in url:
            # Stored as the API endpoint, which is not a page anyone can read.
            url = "https://books.google.com/books?id=" + url.rsplit("/", 1)[-1]
        return LINK_SITES.get(kind, "link"), url
    return None


def by_media_type(rows: list) -> list[tuple[int, list]]:
    """Tracked titles split per medium, biggest group first.

    One visual novel among twenty anime was a row you had to hunt for, and the
    numbers do not really compare across media anyway - a 60% manga and a 60%
    anime are different amounts of work.
    """
    groups: dict[int, list] = {}
    for deck, res in rows:
        groups.setdefault(deck.get("mediaType") or 0, []).append((deck, res))
    for group in groups.values():
        group.sort(key=lambda r: -r[1]["kanji_cov_occ"])
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def genre_ids(names: str | None) -> str | None:
    """Genre names to the ids the API filters on.

    It ignores a name it does not recognise instead of complaining, which made
    `--genre romance` look like it worked while returning the whole catalogue.
    """
    if not names:
        return None
    out = []
    for raw in names.split(","):
        want = raw.strip().lower().replace("-", "").replace(" ", "")
        for num, label in GENRES.items():
            if label.replace("-", "").replace(" ", "") == want:
                out.append(str(num))
                break
        else:
            raise SystemExit(f"unknown genre {raw.strip()!r}; pick from: "
                             + ", ".join(GENRES.values()))
    return ",".join(out)


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

KEY_FILES = ("wanikani_token.txt", "jiten_key.txt", "jimaku_key.txt",
             "nihongo_key.txt")


def ensure_key_files() -> list[str]:
    """Copy any missing key file from its shipped template.

    The real filenames stay out of git: were they tracked, pasting a key would
    show up as a change to commit, and one careless push would publish it.
    The templates travel instead, and this turns them into the real thing.
    """
    made = []
    for name in KEY_FILES:
        target = os.path.join(HERE, name)
        template = target + ".example"
        if not os.path.exists(target) and os.path.exists(template):
            with open(template, encoding="utf-8") as src:
                body = src.read()
            with open(target, "w", encoding="utf-8") as dst:
                dst.write(body)
            made.append(name)
    return made


def cmd_setup(args) -> None:
    made = ensure_key_files()
    print("Key files live next to this script:\n")
    for name in KEY_FILES:
        path = os.path.join(HERE, name)
        value = read_key_file(path)
        state = ("filled in" if value else
                 "waiting for your key" if os.path.exists(path) else "missing")
        mark = "  (just created)" if name in made else ""
        print(f"  {name:<22} {state}{mark}")
    print("\nOpen each one and paste the key on its own line. The comments can")
    print("stay - anything starting with # is ignored.")
    print("\nOnly the WaniKani token is required, and read-only is enough.")


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
                        genres=genre_ids(args.genre), min_chars=args.min_chars,
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


# --------------------------------------------------------------------------
# nihongotracker.app - what you have actually watched and read
# --------------------------------------------------------------------------
# The three sites answer different questions: WaniKani what you can read,
# Jiten how hard a title is, NihongoTracker what you have actually sat
# through. The join is exact rather than by title, because NihongoTracker
# keys its media on AniList ids (anime, manga) and VNDB ids (visual novels) -
# the same ids Jiten stores in a deck's `links`.

NIHONGO_API = "https://nihongotracker.app/api"
# An optional garnish must not be able to hold up a page. The default policy is
# four tries at a two-minute timeout, which is right for the calls a run cannot
# do without and badly wrong for one that only adds a column: a host that
# accepts the connection and then says nothing would stall for minutes.
OPTIONAL = {"timeout": 15, "retries": 2}
ANILIST_LINK_RE = re.compile(r"anilist\.co/(anime|manga)/(\d+)")
VNDB_LINK_RE = re.compile(r"vndb\.org/(v\d+)")
# AniList files light novels under /manga/, and NihongoTracker splits them out
# again, so a manga-shaped id has to be looked for under all three.
NIHONGO_KINDS = {"anime": ("anime",),
                 "manga": ("manga", "light-novel", "book"),
                 "vn": ("vn",)}


def nihongo_key(cli_key: str | None = None) -> str | None:
    key = cli_key or os.environ.get("NIHONGO_API_KEY")
    if not key:
        key = read_key_file(os.path.join(HERE, "nihongo_key.txt"))
    return key or None


def nihongo_headers(key: str | None) -> dict:
    return {"x-api-key": key} if key else {}


def nihongo_whoami(key: str) -> str | None:
    """Whose account the key is. Saves asking for a username as well."""
    try:
        data = get_json(f"{NIHONGO_API}/auth/verify", headers=nihongo_headers(key),
                        **OPTIONAL)
    except SystemExit:
        return None
    return ((data or {}).get("user") or {}).get("username")


def nihongo_ref(deck: dict) -> tuple[str, str] | None:
    """(kind, content id) for a Jiten deck, read off its own outside links."""
    for link in deck.get("links") or []:
        url = link.get("url") or ""
        m = VNDB_LINK_RE.search(url)
        if m:
            return "vn", m.group(1)
        m = ANILIST_LINK_RE.search(url)
        if m:
            return m.group(1), m.group(2)
    return None


def nihongo_index(username: str, key: str) -> dict[str, list[tuple[str, dict]]]:
    """content id -> the entries logged under it. One request for everything."""
    try:
        data = get_json(f"{NIHONGO_API}/users/{urllib.parse.quote(username)}"
                        f"/immersionlist", headers=nihongo_headers(key),
                        **OPTIONAL)
    except SystemExit:
        return {}
    index: dict[str, list[tuple[str, dict]]] = {}
    for kind, items in (data or {}).items():
        for item in items or []:
            cid = str(item.get("contentId") or "")
            if cid:
                index.setdefault(cid, []).append((kind, item))
    return index


def nihongo_media_stats(kind: str, content_id: str, key: str) -> dict | None:
    """Episodes, characters, hours and dates for one title."""
    try:
        data = get_json(f"{NIHONGO_API}/logs/stats/media"
                        f"?mediaId={urllib.parse.quote(content_id)}"
                        f"&type={urllib.parse.quote(kind)}",
                        headers=nihongo_headers(key), **OPTIONAL)
    except SystemExit:
        return None
    return (data or {}).get("total")


def nihongo_progress(decks: Iterable[dict], key: str | None,
                     username: str | None = None,
                     index: dict | None = None) -> dict[int, dict]:
    """deckId -> what you have logged on it, for the titles you have logged.

    The immersion list comes first so the per-title calls only go out for
    titles that are on both sides. Nothing here is fatal: an account with no
    logs, a title with no AniList link, a site that is down - each just means
    no number beside that row.

    Pass `index` when the caller already has one - the unmeasured list needs
    the same request, and fetching it twice per page was pure waste.
    """
    if not key:
        return {}
    username = username or nihongo_whoami(key)
    if not username:
        return {}
    if index is None:
        index = nihongo_index(username, key)
    if not index:
        return {}
    out: dict[int, dict] = {}
    for deck in decks:
        ref = nihongo_ref(deck)
        if not ref:
            continue
        hint, cid = ref
        entries = index.get(cid) or []
        wanted = NIHONGO_KINDS.get(hint, (hint,))
        match = next((e for e in entries if e[0] in wanted), None)
        if not match:
            continue
        kind, item = match
        total = nihongo_media_stats(kind, cid, key)
        if not total:
            continue
        out[deck.get("deckId")] = {
            "hours": total.get("hours") or 0,
            "episodes": total.get("episodes") or 0,
            "chars": total.get("characters") or 0,
            "logs": total.get("logs") or 0,
            "last": (total.get("lastLogDate") or "")[:10],
            "kind": kind,
            "done": bool(item.get("isCompleted")),
        }
    return out


# Which of Jiten's link types a NihongoTracker content id is. Videos are
# YouTube channel ids and games are IGDB ids: nothing Jiten indexes, so those
# are never looked up.
NIHONGO_LINK_TYPE = {"anime": 4, "manga": 4, "light-novel": 4, "book": 4,
                     "movie": 4, "tv show": 4, "vn": 2}


def jiten_decks_for_link(link_type: int, ident: str, key: str | None) -> list[int]:
    """Jiten decks carrying an AniList or VNDB id. Empty means Jiten has none.

    The reverse of the link on a deck, and the only way to answer "could this
    even be measured" without guessing from the title.
    """
    try:
        data = get_json(f"{JITEN_API}/api/media-deck/by-link-id/{link_type}/"
                        f"{urllib.parse.quote(str(ident))}",
                        headers=jiten_headers(key), **OPTIONAL)
    except SystemExit:
        return []
    return [int(x) for x in (data or []) if isinstance(x, int)]


def nihongo_unmeasured(index: dict, on_lists: Iterable[dict],
                       key: str | None) -> list[dict]:
    """What you log but no jiten.moe list is measuring.

    Split by whether Jiten has a deck at all, because those are two different
    situations: one you fix with a button, the other is just Jiten not having
    the title, and a button there would do nothing.
    """
    seen = {ref[1] for ref in (nihongo_ref(d) for d in on_lists) if ref}
    out = []
    for cid, entries in index.items():
        if cid in seen:
            continue
        kind, item = entries[0]
        titles = item.get("title") or {}
        row = {"kind": kind, "deck": None,
               "title": (titles.get("contentTitleNative")
                         or titles.get("contentTitleEnglish") or "?"),
               "logs": item.get("logCount") or 0,
               "last": (item.get("lastLogDate") or "")[:10]}
        link_type = NIHONGO_LINK_TYPE.get(kind)
        if link_type:
            for deck_id in jiten_decks_for_link(link_type, cid, key):
                deck = jiten_deck_detail(deck_id, key)
                ref = nihongo_ref(deck)
                # An AniList anime id and manga id can be the same number, so
                # the deck has to agree about which one it is.
                if ref and ref[1] == cid and kind in NIHONGO_KINDS.get(ref[0], ()):
                    row["deck"] = deck
                    break
        out.append(row)
    out.sort(key=lambda r: (r["deck"] is None, -r["logs"], r["title"]))
    return out


def nihongo_totals(key: str | None, username: str | None = None) -> dict | None:
    """Hours, streak and level across everything you have logged."""
    if not key:
        return None
    username = username or nihongo_whoami(key)
    if not username:
        return None
    try:
        data = get_json(f"{NIHONGO_API}/users/{urllib.parse.quote(username)}/stats",
                        headers=nihongo_headers(key), **OPTIONAL)
    except SystemExit:
        return None
    totals = (data or {}).get("totals") or {}
    if not totals:
        return None
    by_type = [{"type": t.get("type") or "?",
                "logs": t.get("count") or 0,
                "hours": t.get("totalTimeHours") or 0,
                "episodes": t.get("totalEpisodes") or 0,
                "chars": t.get("totalChars") or 0,
                "pages": t.get("totalPages") or 0}
               for t in (data or {}).get("statsByType") or [] if t.get("count")]
    by_type.sort(key=lambda t: -t["hours"])
    return {"username": username,
            "hours": totals.get("totalTimeHours") or 0,
            "reading": totals.get("readingHours") or 0,
            "listening": totals.get("listeningHours") or 0,
            "logs": totals.get("totalLogs") or 0,
            "chars": totals.get("totalChars") or 0,
            "streak": ((data or {}).get("streaks") or {}).get("currentStreak") or 0,
            "days": totals.get("dayCount") or 0,
            "by_type": by_type}


def nihongo_cell(entry: dict | None) -> tuple[str, str]:
    """(sort value, cell html) for one title's logged immersion.

    The sort value is separate because the cell carries two numbers, and the
    table sorter strips non-digits from the whole cell - "0.3h2 ep" would
    otherwise sort as 0.32 against "2h5 ep" as 25.
    """
    if not entry:
        return "", "&mdash;"
    hours = entry["hours"]
    head = f"{hours:.1f}h" if hours < 10 else f"{hours:.0f}h"
    if entry["episodes"]:
        detail = f'{entry["episodes"]} ep'
    elif entry["chars"]:
        detail = f'{entry["chars"]:,} chars'
    else:
        detail = f'{entry["logs"]} log{"" if entry["logs"] == 1 else "s"}'
    return f"{hours:.4f}", f'{head}<div class="und">{esc(detail)}</div>'


def level_bar_html(prog: dict | None, pace: float | None = None) -> str:
    """The level-up bar. 90% of this level's kanji at Guru is the finish line."""
    if not prog:
        return ""
    total, passed, needed = prog["total"], prog["passed"], prog["needed"]
    pct = min(100.0, passed / needed * 100) if needed else 0.0

    # Four buckets that add up to the level, rather than overlapping ones: the
    # locked kanji are also "not started", and saying both reads as double.
    queued = max(0, prog["unstarted"] - prog["locked"])
    bits = []
    if prog["apprentice"]:
        bits.append(f'{prog["apprentice"]} in Apprentice')
    if queued:
        bits.append(f'{queued} waiting in your lesson queue')
    if prog["locked"]:
        bits.append(f'{prog["locked"]} still locked behind radicals')

    if passed >= needed:
        when = "<b>Level up is waiting for you</b> &mdash; 90% is already there."
    elif prog["eta"]:
        when = (f'Earliest level {prog["level"] + 1} '
                f'<b>{esc(until_text(prog["eta"]))}</b>, and only if every '
                f'remaining review is right first time.')
    else:
        when = (f'No date yet &mdash; {prog["undated"]} of these sit behind '
                f'radicals with no state to read, so 90% cannot be reached '
                f'until those turn up.')
    if pace:
        when += f' Your recent pace is {pace:.0f} days a level.'

    return (f'<div class="lvlbar">'
            f'<div class="lvlhead"><span>Level {prog["level"]} &middot; '
            f'{passed} of {needed} kanji passed</span>'
            f'<span class="faint">{pct:.0f}%</span></div>'
            f'<div class="lvltrack"><i style="width:{pct:.1f}%"></i></div>'
            f'<p class="sub">{when} <span class="faint">'
            + " &middot; ".join(esc(b) for b in bits)
            + f'{" &middot; " if bits else ""}{total} kanji at this level'
              f'</span></p></div>')


def counters_html(t: dict) -> str:
    """Lessons and passes for the month and the year."""
    def row(label, month, year, allt):
        return (f'<tr><td>{esc(label)}</td><td class="num">{month:,}</td>'
                f'<td class="num">{year:,}</td><td class="num">{allt:,}</td></tr>')
    acc = (f'{t["accuracy"]:.1f}%' if t["accuracy"] is not None else "&mdash;")
    return (
        '<div class="wrap"><table class="tight"><tr><th></th>'
        '<th class="num">this month</th><th class="num">this year</th>'
        '<th class="num">all time</th></tr>'
        + row("lessons", t["lessons_month"], t["lessons_year"], t["lessons_all"])
        + row("items passed to Guru", t["passed_month"], t["passed_year"],
              t["passed_all"])
        + '</table></div>'
        f'<p class="sub">Reviews per month are not something the API will say: '
        f'WaniKani retired that endpoint and it answers an empty list for every '
        f'account now. What survives is the lifetime total &mdash; '
        f'<b>{t["answers"]:,} answers</b> at <b>{acc}</b> correct &mdash; and '
        f'the two counts above, which come from when each item was actually '
        f'unlocked and passed.</p>')


def immersion_html(totals: dict | None, unmeasured: list[dict] | None) -> str:
    """The NihongoTracker section, shared by the report and the hosted version.

    Everything NihongoTracker knows that this tool can do something with, in
    one place - rather than a second copy of their own stats page, which is
    one click away and better.

    totals=None with a key set means the key was refused or the site did not
    answer; say so rather than leaving a section-shaped hole.
    """
    if not totals:
        return ('<p class="empty">Your NihongoTracker key did not get an answer '
                '&mdash; either it has been revoked, or the site is not '
                'reachable right now. Everything else on this page is '
                'unaffected.</p>')
    h = [f'<p class="sub">From <a href="https://nihongotracker.app/user/'
         f'{urllib.parse.quote(totals["username"])}" target="_blank"'
         f' rel="noopener">nihongotracker.app</a> &middot; '
         f'{totals["logs"]:,} logs over {totals["days"]:,} days.</p>']

    if totals.get("by_type"):
        h.append('<div class="wrap"><table class="sortable tight">'
                 '<tr><th>type</th><th class="num">hours</th>'
                 '<th class="num">how much</th><th class="num">logs</th></tr>')
        for t in totals["by_type"]:
            if t["episodes"]:
                amount = f'{t["episodes"]:,} episodes'
            elif t["chars"]:
                amount = f'{t["chars"]:,} characters'
            elif t["pages"]:
                amount = f'{t["pages"]:,} pages'
            else:
                amount = "&mdash;"
            h.append(f'<tr><td>{esc(t["type"])}</td>'
                     f'<td class="num" data-sort="{t["hours"]:.4f}">'
                     f'{t["hours"]:.1f}h</td>'
                     f'<td class="num">{amount}</td>'
                     f'<td class="num">{t["logs"]:,}</td></tr>')
        h.append("</table></div>")

    addable = [r for r in (unmeasured or []) if r["deck"]]
    missing = [r for r in (unmeasured or []) if not r["deck"]]

    if addable:
        h.append('<h3 class="subhead">Logged, but nothing is measuring it</h3>')
        # Deliberately does not promise a button: the saved report strips the
        # ones that write to your account, because there is no server there.
        h.append('<p class="sub">You are putting hours into these and no '
                 'jiten.moe list has them, so no coverage is worked out for '
                 'them. Once one is on a list, it joins the table above.</p>')
        h.append('<div class="wrap"><table class="sortable tight">'
                 '<tr><th>title</th><th>type</th><th class="num">logs</th>'
                 '<th class="num">last</th><th></th></tr>')
        for r in addable:
            did = r["deck"].get("deckId")
            h.append(
                f'<tr><td><a href="https://jiten.moe/decks/media/{did}/detail"'
                f' target="_blank" rel="noopener">{esc(deck_title(r["deck"]))}</a></td>'
                f'<td>{esc(MEDIA_TYPES.get(r["deck"].get("mediaType"), r["kind"]))}</td>'
                f'<td class="num">{r["logs"]}</td>'
                f'<td class="num">{esc(r["last"]) or "&mdash;"}</td>'
                f'<td class="acts">'
                f'<button class="subs setst" data-deck="{did}" data-st="2"'
                f' data-done="watching ✓">watching</button>'
                f'<button class="subs setst" data-deck="{did}" data-st="1"'
                f' data-done="planned ✓">plan</button></td></tr>')
        h.append("</table></div>")

    if missing:
        names = ", ".join(f'{esc(r["title"])} <span class="faint">'
                          f'({esc(r["kind"])})</span>' for r in missing)
        h.append(f'<p class="sub" style="margin-top:16px">jiten.moe has no deck '
                 f'for {names} &mdash; so there is nothing to measure them '
                 f'against, however much you log. YouTube never will have one.</p>')
    return "".join(h)


JIMAKU_API = "https://jimaku.cc/api"
JIMAKU_CACHE = os.path.join(CACHE_DIR, "jimaku.json")


def jimaku_key(cli_key: str | None = None) -> str | None:
    key = cli_key or os.environ.get("JIMAKU_API_KEY")
    if not key:
        key = read_key_file(os.path.join(HERE, "jimaku_key.txt"))
    return key or None


def anilist_id(deck: dict) -> int | None:
    """The AniList id Jiten stores for a title, which is the only reliable way
    to join it to anything else."""
    for link in deck.get("links") or []:
        m = re.search(r"anilist\.co/anime/(\d+)", link.get("url") or "")
        if m:
            return int(m.group(1))
    return None


def jimaku_entry(anilist: int, key: str) -> int | None:
    """jimaku's entry id for an AniList id, remembered once resolved.

    Linking at their front page instead means shipping the browser their whole
    2 MB index and hoping its search filters to the right show, which it does
    not. One lookup here gives a direct address to the right page.
    """
    cache: dict = {}
    if os.path.exists(JIMAKU_CACHE):
        try:
            with open(JIMAKU_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except ValueError:
            cache = {}
    hit = cache.get(str(anilist), "miss")
    if hit != "miss":
        return hit

    status, body, _ = http(f"{JIMAKU_API}/entries/search?anilist_id={anilist}",
                           headers={"Authorization": key}, **OPTIONAL)
    entry = None
    if status < 400:
        try:
            rows = json.loads(body.decode("utf-8"))
            if isinstance(rows, list) and rows:
                entry = rows[0].get("id")
        except (ValueError, AttributeError, IndexError):
            entry = None
    # Remember misses too: a show with no subtitles will not grow any by being
    # asked about repeatedly.
    cache[str(anilist)] = entry
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(JIMAKU_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return entry


JA_TOKENS = {"ja", "jp", "jpn", "jap", "japanese", "ja-en", "jaen", "ja_en"}
CN_TOKENS = {"chs", "cht", "chi", "zh", "cn", "sc", "tc", "gb", "gb2312",
             "big5", "zh-cn", "zh-tw", "zhcn", "zhtw", "chinese"}
DUAL_TOKENS = {"jpsc", "jptc", "jpcn", "jp_sc", "jp_tc", "scjp", "tcjp",
               "ja-zh", "jazh"}
CN_MARKS = ("简", "繁", "中文", "简体", "繁體", "繁体")
# Commas and semicolons matter: "[JPN, CHS]" is a dual release, and missing the
# separator reads it as Chinese-only and throws away a usable Japanese track.
TOKEN_SPLIT = re.compile(r"[.\[\]_(),;/&+\s-]+")


def subtitle_language(name: str) -> str:
    """Guess a subtitle file's language from its name: jp, cn, dual, unknown.

    jimaku's uploads are named by whoever made them, so this reads the tokens
    conventions actually put there - .ja.srt, .JPSC.ass, .chs.ass - rather than
    assuming a single scheme. Anything unrecognised counts as unknown and is
    kept, since most single-language Japanese releases carry no marker at all.
    """
    stem = re.sub(r"\.(ass|srt|ssa|vtt|sub|idx|zip|7z|rar)$", "", name, flags=re.I)
    tokens = {t.lower() for t in TOKEN_SPLIT.split(stem) if t}
    if tokens & DUAL_TOKENS:
        return "dual"
    has_cn = bool(tokens & CN_TOKENS) or any(m in name for m in CN_MARKS)
    has_ja = bool(tokens & JA_TOKENS)
    if has_cn and has_ja:
        return "dual"
    if has_cn:
        return "cn"
    if has_ja:
        return "jp"
    return "unknown"


def jimaku_files(entry_id: int, key: str) -> list[dict]:
    status, body, _ = http(f"{JIMAKU_API}/entries/{entry_id}/files",
                           headers={"Authorization": key}, timeout=60)
    if status >= 400:
        return []
    try:
        rows = json.loads(body.decode("utf-8"))
    except ValueError:
        return []
    for r in rows:
        r["lang"] = subtitle_language(r.get("name", ""))
    return rows


def wanted_subtitles(rows: list[dict], allow_dual: bool = False) -> list[dict]:
    """Japanese files, with Chinese ones left behind.

    If a title only exists as dual Japanese/Chinese, those come back anyway -
    half a subtitle beats none, and the caller says so rather than showing an
    empty list.
    """
    keep = [r for r in rows if r["lang"] in ("jp", "unknown")]
    dual = [r for r in rows if r["lang"] == "dual"]
    if allow_dual or not keep:
        keep += dual
    return sorted(keep, key=lambda r: r.get("name", ""))


def jimaku_url(deck: dict, key: str | None = None) -> str | None:
    """Direct link to this title's subtitles, or nothing at all.

    Without a jimaku key there is no honest link to offer, so no button is
    shown rather than one that lands somewhere unhelpful.
    """
    if deck.get("mediaType") not in (1, 2, 3):
        return None
    key = key or jimaku_key()
    if not key:
        return None
    aid = anilist_id(deck)
    if not aid:
        return None
    entry = jimaku_entry(aid, key)
    return f"https://jimaku.cc/entry/{entry}" if entry else None


def vocab_overlap(source: Counter, candidate: Counter) -> float:
    """How much of the candidate's running text the source already covers.

    Weighted on the candidate's side on purpose: the question is what share of
    the new thing the old one prepared you for, not how much of the old thing
    reappears. A short source that happens to use very common words should not
    score well against a long candidate for that reason alone.
    """
    total = sum(candidate.values())
    if not total:
        return 0.0
    shared = sum(n for word, n in candidate.items() if word in source)
    return shared / total * 100


def cover_url(deck_id) -> str:
    """Jiten's cover art for a title. The path is predictable from the id, so a
    thumbnail costs no extra request."""
    return f"https://cdn.jiten.moe/{deck_id}/cover.jpg"


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


def cmd_like(args) -> None:
    """Titles built on the vocabulary of one you already know.

    Not "same genre" - what share of the new title's running text is made of
    words the one you name already taught you.
    """
    key = jiten_key(args.jiten_key)
    if not key:
        raise SystemExit("`like` needs a Jiten API key.")
    source_deck = jiten_deck_detail(args.deck_id, key)
    source = deck_words(args.deck_id, key, source_deck)

    url = (f"{JITEN_API}/api/media-deck/get-media-decks?sortBy=coverage"
           f"&sortOrder=1&charCountMin={args.min_chars}")
    if args.type:
        url += f"&mediaType={media_type_id(args.type)}"
    rows = [r for r in (get_json(url, headers=jiten_headers(key)).get("data") or [])
            if r["deckId"] != args.deck_id][:args.limit]

    print(f"\nBecause you know {deck_title(source_deck)}")
    print(f"Reading {len(rows)} candidates; each is downloaded once and then "
          f"cached.\n")
    scored = []
    for i, r in enumerate(rows):
        title = r.get("originalTitle") or r.get("englishTitle") or "?"
        print(f"  [{i+1}/{len(rows)}] {title[:40]}", flush=True)
        try:
            words = deck_words(r["deckId"], key, r)
        except SystemExit:
            continue
        scored.append((vocab_overlap(source, words), r))
        time.sleep(args.sleep)

    scored.sort(key=lambda s: -s[0])
    print(f"\n{'shared':>7} {'yours':>7} {'chars':>9}  title")
    for share, r in scored[:args.top_n]:
        cov = r.get("coverage")
        print(f"{share:6.1f}% {f'{cov}%' if cov is not None else '-':>7} "
              f"{r.get('characterCount') or 0:>9,}  "
              f"{r.get('originalTitle') or r.get('englishTitle') or '?'}")
        print(f"{'':>26}jiten.moe/decks/media/{r['deckId']}/detail")


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


_ASSET_URI: dict[str, str] = {}


def asset_uri(path: str = ICON_FILE) -> str:
    """A PNG as a data: URI, so a saved report carries its own artwork.

    Missing assets are not an error - the tool is one file plus a folder, and
    it should still run if someone copies only the script.
    """
    if path not in _ASSET_URI:
        try:
            with open(path, "rb") as f:
                _ASSET_URI[path] = ("data:image/png;base64,"
                                    + base64.b64encode(f.read()).decode("ascii"))
        except OSError:
            _ASSET_URI[path] = ""
    return _ASSET_URI[path]


def favicon_link(src: str = "") -> str:
    """Tab icon. An empty src inlines the file, for a report that travels."""
    src = src or asset_uri()
    return f'<link rel="icon" type="image/png" href="{src}">' if src else ""


def brand_mark(src: str = "", cls: str = "mark") -> str:
    """The logo beside a heading. Decorative, so it carries no alt text."""
    src = src or asset_uri()
    return (f'<img class="{cls}" src="{src}" alt="" width="128" height="128">'
            if src else "")


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
  padding:44px 0 26px; position:relative;
  display:flex; align-items:center; gap:16px; }
.hero::before { content:""; position:absolute; top:0; left:0; right:0; height:3px;
  background:linear-gradient(90deg,var(--accent),transparent 70%); }
.hero .hd { min-width:0; }
.mark { width:52px; height:52px; flex:none; border-radius:14px;
  box-shadow:var(--shadow); }
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
/* The default chunky scrollbar looks awful against a rounded card. */
.wrap, .gridout { scrollbar-width:thin; scrollbar-color:var(--line) transparent; }
.wrap::-webkit-scrollbar, .gridout::-webkit-scrollbar { height:8px; width:8px; }
.wrap::-webkit-scrollbar-track, .gridout::-webkit-scrollbar-track {
  background:transparent; }
.wrap::-webkit-scrollbar-thumb, .gridout::-webkit-scrollbar-thumb {
  background:var(--line); border-radius:99px; border:2px solid var(--raise); }
.wrap:hover::-webkit-scrollbar-thumb { background:var(--faint); }
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
/* Title columns want room; a column of numbers does not, or it strands the
   figures miles from their header. */
td:first-child:not(.num) { min-width:11em; font-weight:520; }
td.kanji { min-width:auto; font-size:26px; line-height:1; font-weight:400; }
a { color:inherit; text-decoration:none; border-bottom:1px solid var(--line); }
a:hover { border-bottom-color:var(--accent); color:var(--accent); }

/* bits */
.meter { display:block; width:96px; height:6px; background:var(--line);
  border-radius:99px; overflow:hidden; margin-top:5px; margin-left:auto; }
table.tight { min-width:0; }
table.tight th, table.tight td { padding-left:9px; padding-right:9px; }
table.tight td:first-child:not(.num) { min-width:9em; }
.meter i { display:block; height:100%; border-radius:99px; background:var(--accent); }
.up { color:var(--good); font-weight:640; }
.subs { display:inline-block; font-size:10.5px; font-weight:650; padding:1px 7px;
  border-radius:99px; border:1px solid var(--line); color:var(--faint);
  vertical-align:2px; margin-left:6px; letter-spacing:.03em; }
.mediahead { font-size:14px; font-weight:640; letter-spacing:-.005em;
  margin:26px 0 8px; text-transform:capitalize; }
/* One table per medium means each would size its own columns, so a one-row
   visual novel would not line up with five anime above it. Fixed widths make
   the stack read as one table; the title column takes whatever is left. */
table.grouped { table-layout:fixed; min-width:640px; }
table.grouped th:nth-child(2) { width:118px; }
table.grouped th:nth-child(3) { width:100px; }
table.grouped th:nth-child(4) { width:66px; }
table.grouped th:nth-child(5) { width:74px; }
table.grouped th:nth-child(6) { width:74px; }
table.grouped th:nth-child(7) { width:68px; }
table.grouped th:nth-child(8) { width:104px; }
table.grouped td:first-child { word-break:break-word; }
/* The immersion column only exists when there is a NihongoTracker key. */
table.nt { min-width:744px; }
.und { font-size:11px; font-weight:500; color:var(--faint); margin-top:1px; }
.faint { color:var(--faint); }
.subhead { font-size:15px; font-weight:640; margin:28px 0 6px; }
.lvlbar { background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:15px 17px; margin:0 0 18px;
  box-shadow:var(--shadow); }
.lvlhead { display:flex; justify-content:space-between; align-items:baseline;
  font-size:13.5px; font-weight:620; margin-bottom:9px; gap:12px; }
.lvltrack { height:9px; border-radius:99px; background:var(--line-soft);
  overflow:hidden; }
.lvltrack i { display:block; height:100%; border-radius:99px;
  background:linear-gradient(90deg,var(--accent),var(--good)); }
.lvlbar .sub { margin:10px 0 0; font-size:13px; }
.mediahead span { font-size:11.5px; font-weight:600; color:var(--faint);
  background:var(--line-soft); border-radius:99px; padding:2px 8px;
  margin-left:6px; vertical-align:2px; }
.subs:hover { border-color:var(--accent); color:var(--accent); }
button.subs { font-family:inherit; cursor:pointer; background:var(--bg); }
button.subs:disabled { opacity:.55; cursor:default; }
button.subs.done { color:var(--good); border-color:var(--good); }
button.subs.arm { color:var(--accent); border-color:var(--accent);
  background:var(--accent-soft); }
tr.gone { opacity:.42; }
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
  .hero { padding:30px 0 20px; gap:12px; }
  .mark { width:40px; height:40px; border-radius:11px; }
  h2 { margin-top:38px; }
}
"""


BROWSE_SLOT = "<!--browse-->"
NAV_SLOT = "<!--nav-->"

SUBS_JS = """
(function(){
  const box = document.getElementById('subsbox');
  if (!box) return;
  let entry = null, title = '', dual = false;

  function bytes(n){
    return n > 1048576 ? (n/1048576).toFixed(1) + ' MB'
         : n > 1024 ? Math.round(n/1024) + ' kB' : (n || 0) + ' B';
  }

  async function load(){
    box.hidden = false;
    box.innerHTML = '<p class="empty">Asking jimaku&hellip;</p>';
    let d;
    try { d = await (await fetch(`/subs/${entry}?dual=${dual ? 1 : 0}`)).json(); }
    catch (e){ box.innerHTML = '<p class="empty">Could not reach jimaku.</p>'; return; }
    if (!d.files.length){
      box.innerHTML = `<p class="empty">No Japanese subtitles for this one.</p>`;
      return;
    }
    const withCn = d.files.filter(f => f.lang === 'dual').length;
    const note = d.onlyDual
      ? `Only Japanese-and-Chinese versions exist for this title, so those are
         what you get.`
      : withCn
      ? `${withCn} of these carry Chinese subtitles alongside the Japanese.`
      : d.skipped
      ? `${d.skipped} Chinese ${d.skipped === 1 ? 'file' : 'files'} left out of
         ${d.total}.`
      : `All ${d.total} files are Japanese.`;
    box.innerHTML = `
      <div class="subshead">
        <div><b>${title}</b> <span class="sub">&middot; ${d.files.length} files
          &middot; ${note}</span></div>
        <div class="modes">
          <a class="go dl" href="/subs/${entry}/zip?dual=${dual ? 1 : 0}"
             download>Download all as .zip</a>
          <button id="subsdual">${dual ? 'Hide' : 'Include'} JP+CN versions</button>
          <button id="subsclose">Close</button>
        </div>
      </div>
      <div class="wrap"><table><tr><th>file</th><th class="num">size</th>
        <th></th></tr>` +
      d.files.map(f => `<tr><td>${f.name}</td>
        <td class="num">${bytes(f.size)}</td>
        <td class="num"><a href="${f.url}" download>download</a></td></tr>`).join('') +
      '</table></div>';
    document.getElementById('subsclose').onclick = () => { box.hidden = true; };
    document.getElementById('subsdual').onclick = () => { dual = !dual; load(); };
  }

  document.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-entry]');
    if (!btn) return;
    e.preventDefault();
    entry = btn.dataset.entry;
    title = btn.dataset.title || '';
    dual = false;
    load();
    box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  });
})();
"""

STATUS_JS = """
(function(){
  // The saved report has no server behind it, so a button that writes to your
  // jiten.moe account would only ever fail there.
  if (typeof LIVE === 'undefined' || !LIVE){
    document.querySelectorAll('.setst').forEach(b => b.remove());
    return;
  }
  document.querySelectorAll('.setst').forEach(function (btn) {
    const was = btn.textContent;
    let timer = 0;
    btn.addEventListener('click', async function () {
      // Taking a title off your lists is one stray click from being an
      // accident, so it asks first. Setting a status is not - you can just
      // set another one.
      if (btn.dataset.confirm && !timer){
        btn.textContent = 'sure?'; btn.classList.add('arm');
        timer = setTimeout(function (){
          timer = 0; btn.textContent = was; btn.classList.remove('arm');
        }, 4000);
        return;
      }
      clearTimeout(timer); timer = 0; btn.classList.remove('arm');
      btn.disabled = true; btn.textContent = 'saving…';
      try {
        const r = await fetch('/api/user/deck-preferences/' + btn.dataset.deck +
                              '/status', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: +btn.dataset.st})});
        if (!r.ok) throw new Error(r.status);
        btn.textContent = btn.dataset.done || 'done ✓';
        btn.classList.add('done');
        // The row is stale from here on: it stays visible so you can see what
        // you did, and is gone on the next refresh.
        if (btn.dataset.drop) btn.closest('tr')?.classList.add('gone');
      } catch (e){
        btn.textContent = 'failed'; btn.disabled = false;
        setTimeout(() => { btn.textContent = was; }, 2000);
      }
    });
  });
})();
"""

SUBS_CSS = """
.subsbox { background:var(--raise); border:1px solid var(--line);
  border-radius:14px; padding:14px 16px; margin:12px 0 0; box-shadow:var(--shadow); }
.subshead { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:center;
  justify-content:space-between; margin-bottom:10px; }
a.go.dl { text-decoration:none; border:1px solid var(--accent);
  background:var(--accent); color:#fff; padding:6px 14px; border-radius:99px;
  font-size:12.5px; font-weight:600; }
a.go.dl:hover { filter:brightness(1.08); color:#fff; }
.subsbox td:first-child { min-width:16em; font-weight:400; font-size:13px;
  word-break:break-word; }
/* The flex goes on a wrapper inside the cell. Making the <td> itself a flex
   container drops it out of the table layout, so its row stops lining up with
   the columns beside it. */
td.withcover { min-width:15em; }
.pager { display:flex; gap:12px; align-items:center; justify-content:center;
  padding:12px; color:var(--muted); font-size:13px; }
td.withcover .ct { display:flex; gap:10px; align-items:center; }
img.cover { width:34px; height:48px; object-fit:cover; border-radius:4px;
  flex:none; background:var(--line); }
"""

# Works in the saved file too: the curves are already embedded, so dragging the
# slider needs no network at all.
SLIDER_HTML = """
<div class="slider">
  <label for="lvl">If I were level <b id="lvlout"></b></label>
  <input id="lvl" type="range" min="1" max="60">
  <span id="eta" class="pill"></span>
  <span class="pace" title="WaniKani's own floor is 6 days 20 hours on levels 3-42:
two SRS chains of 3 days 10 hours, radicals to Guru and then the kanji they
unlock. On the fast levels - 1, 2 and 43-60 - the kanji do not wait for
radicals, so one chain of 3 days 10 hours is the whole level.">
    <label for="pace">at</label>
    <input id="pace" type="number" min="3.4" max="365" step="0.5"
           inputmode="decimal" placeholder="days">
    <label for="pace">days per level</label>
    <button id="paceback" type="button" hidden>back to my <b></b></button>
  </span>
</div>
<div class="wrap"><table id="whatif" class="tight"><tr><th>title</th>
  <th class="num">now</th><th class="num">then</th><th class="num">gain</th>
  </tr></table></div>
"""

SLIDER_JS = """
(function(){
  const slider = document.getElementById('lvl');
  // TRACK is a top-level `const` in a sibling script: script-scoped, so it is
  // reachable by name but never a property of window.
  if (!slider || typeof TRACK === 'undefined' || !TRACK.titles.length) return;
  slider.min = TRACK.level; slider.value = Math.min(60, TRACK.level + 8);

  // Your measured pace is only the starting point. A couple of slow levels sit
  // in the median for six levels after you speed up again, so the field lets
  // you ask "and if I did one every 9 days?" without waiting for the median to
  // catch up. Nothing here changes what WaniKani says you actually did.
  const paceIn = document.getElementById('pace');
  const paceBack = document.getElementById('paceback');
  const mine = TRACK.pace ? Math.round(TRACK.pace * 10) / 10 : 0;
  if (mine){
    paceIn.value = mine;
    paceBack.querySelector('b').textContent = mine + ' days';
  }
  function draw(){
    const lv = +slider.value;
    document.getElementById('lvlout').textContent = lv;
    const away = lv - TRACK.level;
    const eta = document.getElementById('eta');
    // A blank or nonsense field means "use what I actually do".
    const pace = +paceIn.value > 0 ? +paceIn.value : mine;
    paceBack.hidden = !mine || pace === mine;
    if (away <= 0) eta.textContent = 'where you are now';
    else if (pace){
      const d = away * pace;
      // Days below a fortnight: one level at full speed is 6.8 days, which
      // rounded to weeks came out as "~0 weeks away".
      const span = (n, unit) => `~${n} ${unit}${n === 1 ? '' : 's'}`;
      eta.textContent = (d < 14 ? span(Math.round(d), 'day')
                       : d < 60 ? span(Math.round(d / 7), 'week')
                       : d < 730 ? span(Math.round(d / 30.4), 'month')
                       : `~${(d/365).toFixed(1)} years`) + ' away';
    } else eta.textContent = away + ' levels away';
    let rows = '';
    for (const t of TRACK.titles){
      const then = t.c[lv-1], gain = then - t.now;
      rows += `<tr><td>${t.t}</td><td class="num">${t.now.toFixed(1)}%</td>
        <td class="num">${then.toFixed(1)}%</td>
        <td class="num up">+${gain.toFixed(1)}pp</td></tr>`;
    }
    document.getElementById('whatif').innerHTML =
      `<tr><th>title</th><th class="num">now</th><th class="num">at that level</th>
       <th class="num">gain</th></tr>` + rows;
  }
  slider.addEventListener('input', draw);
  paceIn.addEventListener('input', draw);
  // Snap on the way out rather than mid-keystroke, so typing "12" is not
  // fought over at "1". Only what you type is held to the floor - a measured
  // median stands as it is, because it is what the API says happened.
  paceIn.addEventListener('change', () => {
    const v = +paceIn.value, floor = +paceIn.min;
    if (v && v < floor) paceIn.value = floor;
    draw();
  });
  paceBack.addEventListener('click', () => { paceIn.value = mine; draw(); });
  draw();
})();
"""

REACH_HTML = """
<p class="sub">Kanji coverage at a level you have not reached yet. Word
coverage cannot be asked that question &mdash; WaniKani teaches ~6,500 words,
so the rest of it comes from reading rather than levelling &mdash; which is why
it sits here as a column and not as a target.</p>
<div class="controls levelbar">
  <label for="rlevel">Judge everything as if I were level</label>
  <input id="rlevel" type="number" min="1" max="60">
</div>
<div id="reach-others">
  <div class="controls">
    <select id="rtype">
      <option value="">any type</option>
      <option value="1">anime</option><option value="9">manga</option>
      <option value="4">novel</option><option value="7">visual novel</option>
      <option value="6">game</option><option value="2">drama</option>
      <option value="3">movie</option><option value="8">web novel</option>
    </select>
    <select id="rtag"></select>
    <label for="rpct">reaching</label>
    <input id="rpct" type="number" min="0" max="100" step="1" value="70"
           title="The kanji coverage you want once you are at that level">
    <label for="rpct">% kanji, from</label>
    <input id="rmin" type="number" placeholder="min chars" min="0" step="10000"
           value="20000" title="Without this the list fills up with one-page entries">
    <label for="rmin">characters up</label>
    <button id="rgo" class="go">Find</button>
  </div>
  <div id="reach-results"></div>
</div>
"""

REACH_JS = """
(function(){
  const box0 = document.getElementById('reach-others');
  if (!box0 || typeof TRACK === 'undefined') return;
  const lvlInput = document.getElementById('rlevel');
  lvlInput.min = TRACK.level;
  lvlInput.value = REACH_TARGET;
  const target = () => Math.max(TRACK.level, Math.min(60, +lvlInput.value || TRACK.level));

  const sel = document.getElementById('rtag');
  if (sel && typeof TAGS !== 'undefined'){
    sel.innerHTML = '<option value="">any tag</option>' +
      TAGS.map(t => `<option value="${t.tagId}">${t.name}</option>`).join('');
  }

  function othersFallback(){
    if (!OTHERS.length) return '<p class="empty">Nothing precomputed here.</p>';
    return `<div class="wrap"><table class="sortable"><tr><th>title</th>
      <th class="num">now</th><th class="num">at ${target}</th>
      <th class="num">gain</th></tr>` + OTHERS.map(o =>
      `<tr><td><a href="https://jiten.moe/decks/media/${o.id}/detail"
        target="_blank" rel="noopener">${o.t}</a></td>
        <td class="num">${o.now}%</td><td class="num">${o.then}%</td>
        <td class="num up">+${o.gain}pp</td></tr>`).join('') + '</table></div>';
  }

  const planCell = id => `<td class="acts"><button data-track="${id}"
    data-status="1">plan to watch/read</button></td>`;
  const titleCell = r => `<td class="withcover"><span class="ct"><img class="cover"
    loading="lazy" alt="" src="https://cdn.jiten.moe/${r.deckId}/cover.jpg"
    onerror="this.style.visibility='hidden'">
    <a href="https://jiten.moe/decks/media/${r.deckId}/detail"
    target="_blank" rel="noopener">${r.originalTitle || r.englishTitle || '?'}</a>
    </span></td>`;

  function wire(box){
    box.querySelectorAll('[data-track]').forEach(b =>
      b.onclick = () => track(+b.dataset.track, +b.dataset.status, b));
    sortable();
  }

  async function find(){
    const box = document.getElementById('reach-results');
    if (!LIVE){ box.innerHTML = othersFallback(); return; }
    const type = document.getElementById('rtype').value;
    const tag = document.getElementById('rtag').value;
    const min = document.getElementById('rmin').value;
    const pct = +document.getElementById('rpct').value || 0;
    const lv = target();

    let url = '/api/media-deck/get-media-decks?sortBy=coverage&sortOrder=1';
    if (type) url += '&mediaType=' + type;
    if (tag) url += '&tags=' + tag;
    if (min) url += '&charCountMin=' + min;
    // Jiten's word coverage is only a pre-filter here, to pick which titles are
    // worth downloading: cast wider, because the two measures diverge by ten
    // points or more in either direction.
    const floor = Math.max(0, pct - 20);
    if (floor) url += '&coverageMin=' + floor;

    box.innerHTML = '<p class="empty">Looking&hellip;</p>';
    let d;
    try { d = await (await fetch(url)).json(); }
    catch (e){ box.innerHTML = '<p class="empty">Lookup failed.</p>'; return; }
    const rows = d.data || [];
    if (!rows.length){
      box.innerHTML = `<p class="empty">Nothing here comes near ${pct}%. Lower the
        bar, widen the filters, or come back a few levels from now.</p>`;
      return;
    }

    // Kanji coverage has to be computed per title, one word list each. Ten is
    // not an arbitrary cap: Jiten allows ten of these downloads a minute, so a
    // sweep over unfamiliar titles is one minute's work rather than several.
    const budget = Math.min(rows.length, 10);
    const found = [];
    const draw = (progress) => {
      const table = found.length ? `<div class="wrap"><table class="sortable tight">
        <tr><th>title</th><th>type</th><th class="num">chars</th>
        <th class="num">kanji now</th><th class="num">at ${lv}</th>
        <th class="num">word cov</th><th></th></tr>` +
        found.slice().sort((a, b) => b.then - a.then).map(f => `<tr>${titleCell(f.r)}
          <td>${WK.types[f.r.mediaType] || '?'}</td>
          <td class="num">${(f.r.characterCount||0).toLocaleString()}</td>
          <td class="num">${f.now.toFixed(1)}%</td>
          <td class="num up">${f.then.toFixed(1)}%</td>
          <td class="num">${f.r.coverage != null ? f.r.coverage + '%' : '—'}</td>
          ${planCell(f.r.deckId)}</tr>`).join('') + '</table></div>' : '';
      box.innerHTML = (progress || '') + table;
      if (found.length) wire(box);
    };

    for (let i = 0; i < budget; i++){
      const r = rows[i];
      draw(`<p class="empty">Reading ${i + 1} of ${budget}
        &mdash; ${found.length} over ${pct}% so far&hellip;<br>
        <small>Each title is read once and then cached, so this is slow only
        the first time.</small></p>`);
      let occ;
      try { occ = await kanjiCounts(r.deckId); }
      catch (e){ continue; }
      let total = 0, now = 0;
      const byLevel = new Array(61).fill(0);
      for (const [ch, n] of occ){
        total += n;
        if (known.has(ch)) now += n;
        const kl = WK.levels[ch];
        if (kl) byLevel[kl] += n;
      }
      if (!total) continue;
      let run = 0;
      for (let l = 1; l <= lv; l++) run += byLevel[l];
      const then = run / total * 100;
      if (then >= pct) found.push({r, now: now / total * 100, then});
    }

    if (!found.length){
      box.innerHTML = `<p class="empty">None of the ${budget} closest candidates
        reach ${pct}% kanji coverage at level ${lv}. Lower the bar or aim at a
        higher level.</p>`;
      return;
    }
    draw(`<p class="sub">Read the ${budget} titles you are closest to;
      ${found.length} clear ${pct}% kanji coverage once you reach level ${lv}.</p>`);
  }

  document.getElementById('rgo').onclick = find;
  // Changing the level invalidates whatever is on screen, but re-running is a
  // sweep of downloads - so clear it and let them press Find again.
  lvlInput.addEventListener('change', () => {
    document.getElementById('reach-results').innerHTML =
      '<p class="empty">Level changed. Press Find to look again.</p>';
  });
  sortable();
})();
"""

# Any table marked sortable gets clickable headers. Cheaper than threading a
# sort order through every server-rendered table.
SORT_JS = """
function sortable(){
  document.querySelectorAll('table.sortable').forEach(tbl => {
    if (tbl.dataset.wired) return;
    tbl.dataset.wired = '1';
    const head = tbl.rows[0];
    [...head.cells].forEach((th, i) => {
      if (!th.textContent.trim()) return;
      th.classList.add('sortcol');
      th.onclick = () => {
        const dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
        [...head.cells].forEach(c => { delete c.dataset.dir;
          c.classList.remove('sorted'); });
        th.dataset.dir = dir; th.classList.add('sorted');
        const rows = [...tbl.rows].slice(1);
        const val = r => {
          // data-sort wins: a cell holding two numbers cannot be read by
          // stripping the non-digits out of all of its text.
          const cell = r.cells[i];
          const t = (cell?.dataset.sort ?? cell?.textContent ?? '').trim();
          const n = parseFloat(t.replace(/[^0-9.+-]/g, ''));
          return isNaN(n) ? null : n;
        };
        rows.sort((a, b) => {
          const x = val(a), y = val(b);
          if (x === null || y === null)
            return (a.cells[i]?.textContent || '').localeCompare(
                    b.cells[i]?.textContent || '', 'ja');
          return x - y;
        });
        if (dir === 'desc') rows.reverse();
        rows.forEach(r => tbl.appendChild(r));
      };
    });
  });
}
"""

REACH_CSS = """
.levelbar { align-items:center; margin-bottom:12px; }
/* Direct children only: the tag menu's checkbox labels sit inside a .controls
   too, and they are not part of the row. */
.controls > label { font-size:14px; color:var(--muted); align-self:center; }
.levelbar input { width:82px; }
#rpct { width:76px; }
#rmin { width:104px; }
/* A select is as wide as its longest option, and one of the 252 tags is
   "Cute Girls Doing Cute Things". Cap it so the row still fits on one line. */
#rtag { max-width:170px; }
.sortcol { cursor:pointer; user-select:none; }
.sortcol:hover { color:var(--accent); }
.sorted::after { content:" \\2193"; color:var(--accent); }
th.sorted[data-dir=asc]::after { content:" \\2191"; }
.hit { cursor:help; }
"""

LIKE_HTML = """
<div class="controls">
  <label for="likesrc">Titles built on the vocabulary of</label>
  <select id="likesrc"></select>
  <select id="liketype">
    <option value="">any type</option>
    <option value="1">anime</option><option value="9">manga</option>
    <option value="4">novel</option><option value="7">visual novel</option>
    <option value="6">game</option><option value="2">drama</option>
  </select>
  <button id="likego" class="go">Find</button>
</div>
<div id="likeout"></div>
"""

LIKE_JS = """
(function(){
  const out = document.getElementById('likeout');
  if (!out || typeof TRACK === 'undefined') return;
  const sel = document.getElementById('likesrc');
  sel.innerHTML = TRACK.titles.map((t, i) =>
    `<option value="${TRACK.ids[i]}">${t.t}</option>`).join('');

  document.getElementById('likego').onclick = async () => {
    const id = sel.value, type = document.getElementById('liketype').value;
    out.innerHTML = `<p class="empty">Reading candidates &mdash; each title is
      downloaded once and then cached, so this is slow only the first time.</p>`;
    let d;
    try {
      d = await (await fetch(`/like/${id}?type=${type}`)).json();
    } catch (e){ out.innerHTML = '<p class="empty">Lookup failed.</p>'; return; }
    if (!d.results || !d.results.length){
      out.innerHTML = '<p class="empty">Nothing to compare against.</p>'; return;
    }
    out.innerHTML = `<p class="sub">Share of each title's running text made of
      words ${d.source} already used. Roughly two thirds is the floor for any
      two Japanese works, so the gap above that is the signal.</p>
      <div class="wrap"><table class="sortable"><tr><th>title</th>
      <th class="num">shared vocabulary</th><th class="num">your coverage</th>
      <th class="num">chars</th></tr>` +
      d.results.map(r => `<tr><td class="withcover"><span class="ct">
          <img class="cover" loading="lazy" alt=""
            src="https://cdn.jiten.moe/${r.id}/cover.jpg"
            onerror="this.style.visibility='hidden'">
          <a href="https://jiten.moe/decks/media/${r.id}/detail" target="_blank"
             rel="noopener">${r.title}</a></span></td>
        <td class="num"><b>${r.shared.toFixed(1)}%</b></td>
        <td class="num">${r.coverage != null ? r.coverage + '%' : '—'}</td>
        <td class="num">${(r.chars||0).toLocaleString()}</td></tr>`).join('') +
      '</table></div>';
    sortable();
  };
})();
"""

CHART_HTML = """
<div class="chartbar">
  <div id="series" class="series"></div>
  <div class="modes">
    <button id="allon">show all</button>
    <button id="tall">taller</button>
  </div>
</div>
<div id="chart" class="chartbox"></div>
"""

CHART_JS = """
(function(){
  const box = document.getElementById('chart');
  if (!box || typeof TRACK === 'undefined' || !TRACK.titles.length) return;
  const PAL = ['#c2410c','#0369a1','#15803d','#7e22ce','#b45309','#0f766e',
               '#be123c','#4338ca','#65a30d','#a21caf','#0891b2','#9f1239'];
  const on = TRACK.titles.map(() => true);
  let tall = false;

  // The slider sits directly under this chart and used to move nothing on it,
  // which read as if the two were one instrument that had stopped working.
  // The chart watches the slider itself, so neither script has to know about
  // the other and load order stops mattering.
  const slider = document.getElementById('lvl');
  const markLevel = () => slider ? Math.max(1, Math.min(60, +slider.value || 0)) : 0;

  function draw(){
    const W = 760, H = tall ? 460 : 300, L = 46, R = 14, T = 12, B = 34;
    const pw = W - L - R, ph = H - T - B;
    const x = lv => L + (lv - 1) / 59 * pw;
    const y = p => T + ph - p / 100 * ph;
    let s = `<svg viewBox="0 0 ${W} ${H}" class="chart">`;
    for (let p = 0; p <= 100; p += 25){
      s += `<line x1="${L}" y1="${y(p)}" x2="${L+pw}" y2="${y(p)}" class="grid"/>
            <text x="${L-8}" y="${y(p)+4}" class="tick" text-anchor="end">${p}%</text>`;
    }
    for (const lv of [1,10,20,30,40,50,60])
      s += `<text x="${x(lv)}" y="${H-12}" class="tick" text-anchor="middle">${lv}</text>`;
    s += `<line x1="${x(TRACK.level)}" y1="${T}" x2="${x(TRACK.level)}" y2="${T+ph}"
          class="you"/><text x="${x(TRACK.level)+5}" y="${T+11}" class="tick">you</text>`;
    TRACK.titles.forEach((t, i) => {
      if (!on[i]) return;
      const pts = t.c.map((p, j) => `${x(j+1).toFixed(1)},${y(p).toFixed(1)}`).join(' ');
      s += `<polyline points="${pts}" fill="none" stroke="${PAL[i % PAL.length]}"
            stroke-width="2" stroke-linejoin="round"/>`;
      // Two markers at your level: the filled one is where the curve says you
      // would be with everything up to here at Guru, the hollow one is what you
      // actually have. The gap between them is the reviews you owe.
      const cx = x(TRACK.level).toFixed(1);
      const at = t.c[TRACK.level - 1], col = PAL[i % PAL.length];
      s += `<circle cx="${cx}" cy="${y(at).toFixed(1)}" r="3.5" fill="${col}"/>
            <circle cx="${cx}" cy="${y(at).toFixed(1)}" r="11" fill="transparent"
              class="hit"><title>${t.t}
${at.toFixed(1)}% with all of level ${TRACK.level} at Guru</title></circle>`;
      if (Math.abs(at - t.now) > 0.3){
        s += `<circle cx="${cx}" cy="${y(t.now).toFixed(1)}" r="3.5" fill="var(--raise)"
                stroke="${col}" stroke-width="1.8"/>
              <circle cx="${cx}" cy="${y(t.now).toFixed(1)}" r="11" fill="transparent"
                class="hit"><title>${t.t}
${t.now.toFixed(1)}% right now</title></circle>`;
      }
    });
    const mk = markLevel();
    if (mk > TRACK.level){
      s += `<line x1="${x(mk)}" y1="${T}" x2="${x(mk)}" y2="${T+ph}" class="mark"/>
            <rect x="${x(mk) - 13}" y="${T - 2}" width="26" height="16" rx="8"
              class="markchip"/>
            <text x="${x(mk)}" y="${T + 10}" class="marktext"
              text-anchor="middle">${mk}</text>`;
      TRACK.titles.forEach(function (t, i) {
        if (!on[i]) return;
        const v = t.c[mk - 1];
        s += `<rect x="${(x(mk) - 3.5).toFixed(1)}" y="${(y(v) - 3.5).toFixed(1)}"
                width="7" height="7" fill="${PAL[i % PAL.length]}"
                stroke="var(--raise)" stroke-width="1.2"/>
              <circle cx="${x(mk).toFixed(1)}" cy="${y(v).toFixed(1)}" r="10"
                fill="transparent" class="hit"><title>${t.t}
${v.toFixed(1)}% at level ${mk}</title></circle>`;
      });
    }
    s += `<text x="${L+pw/2}" y="${H-1}" class="tick" text-anchor="middle">WaniKani level</text></svg>`;
    box.innerHTML = s;
  }

  function chips(){
    document.getElementById('series').innerHTML = TRACK.titles.map((t, i) =>
      `<button class="chip${on[i] ? ' on' : ''}" data-i="${i}"
        style="--c:${PAL[i % PAL.length]}"><i></i>${t.t}</button>`).join('');
    document.querySelectorAll('#series .chip').forEach(b => {
      const i = +b.dataset.i;
      b.onclick = e => {
        // Plain click solos a line when it is the only way to read a cluster;
        // clicking it again brings everything back.
        if (on[i] && on.filter(Boolean).length === 1) on.fill(true);
        else { on.fill(false); on[i] = true; }
        chips(); draw();
      };
      b.oncontextmenu = e => { e.preventDefault(); on[i] = !on[i]; chips(); draw(); };
    });
  }

  if (slider) slider.addEventListener('input', draw);
  document.getElementById('allon').onclick = () => { on.fill(true); chips(); draw(); };
  document.getElementById('tall').onclick = e => {
    tall = !tall; e.target.textContent = tall ? 'shorter' : 'taller'; draw();
  };
  chips(); draw();
})();
"""

CHART_CSS = """
.chartbar { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:flex-start;
  justify-content:space-between; margin-bottom:10px; }
.series { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-flex; align-items:center; gap:7px; opacity:.42; }
.chip.on { opacity:1; }
.chip i { width:11px; height:3px; border-radius:2px; background:var(--c);
  display:inline-block; }
.chip.on { border-color:var(--c); color:var(--fg); }
.chartbox { background:var(--raise); border:1px solid var(--line);
  border-radius:14px 14px 0 0; box-shadow:var(--shadow);
  padding:6px 10px; border-bottom:0; }
.mark { stroke:var(--accent); stroke-width:1.5; }
.markchip { fill:var(--accent); }
.marktext { fill:#fff; font:700 10px/1 var(--sans, sans-serif); }
"""

GRID_HTML = """
<details class="fold" id="gridfold">
  <summary><span class="tw">Show the grid</span><span class="cnt"></span></summary>
  <div class="gridbar">
    <div class="modes">
      <button data-mode="srs" class="on">by SRS stage</button>
      <button data-mode="impact">by what it costs you</button>
    </div>
    <div class="modes scope">
      <button data-scope="mine" class="on">levels you have</button>
      <button data-scope="all">all 60</button>
    </div>
  </div>
  <div id="legend" class="legend-row"></div>
  <div id="gridout" class="gridout"></div>
  <div id="kdetail" class="kdetail"><span class="hint">Pick a kanji to see its
    reading, and to open it on WaniKani.</span></div>
</details>
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
  let mode = 'srs', scope = 'mine', drawn = false;

  // Resolve the themed colours once and blend numerically. Leaving 2,000-odd
  // color-mix() calls in inline styles makes every style recalculation on the
  // page - including ones triggered by typing in the search box - walk the
  // whole grid, which is enough to make the cursor stutter.
  const css = getComputedStyle(document.documentElement);
  const rgb = name => {
    const v = css.getPropertyValue(name).trim();
    const m = v.match(/^#([0-9a-f]{6})$/i);
    if (m) return [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16));
    const n = v.match(/\\d+/g);
    return n ? n.slice(0, 3).map(Number) : [128, 128, 128];
  };
  const COLD = rgb('--k0'), HOT = rgb('--k-hot'), KNOWN = rgb('--k-known');
  const hex = c => '#' + c.map(v =>
    Math.round(v).toString(16).padStart(2, '0')).join('');
  const IMPACT = new Map();
  for (const k of GRID){
    if (k.k) IMPACT.set(k.c, hex(KNOWN));
    else if (!k.n) IMPACT.set(k.c, hex(COLD));
    else {
      const t = 0.18 + Math.sqrt(k.n / max) * 0.82;
      IMPACT.set(k.c, hex(COLD.map((c, i) => c + (HOT[i] - c) * t)));
    }
  }
  const SRS = new Map(GRID.map(k => [k.c, STAGES[band(k.s)][2]]));
  const colour = k => (mode === 'srs' ? SRS : IMPACT).get(k.c);

  function draw(){
    const byLevel = new Map();
    for (const k of GRID){
      // Levels above yours are all locked and identical to look at; showing
      // them by default buries the part you can act on.
      if (scope === 'mine' && k.l > GRID_LEVEL) continue;
      if (!byLevel.has(k.l)) byLevel.set(k.l, []);
      byLevel.get(k.l).push(k);
    }
    let html = '';
    for (const [lv, list] of [...byLevel].sort((a, b) => a[0] - b[0])){
      html += `<div class="lvlrow"><span class="lvlnum${lv === GRID_LEVEL ?
        ' now' : ''}">${lv}</span><div class="kanjis">`;
      for (const k of list)
        html += `<span class="k" style="background:${colour(k)}"
                 data-c="${k.c}">${k.c}</span>`;
      html += '</div></div>';
    }
    out.innerHTML = html;

    document.getElementById('legend').innerHTML = mode === 'srs'
      ? STAGES.map(s => `<span class="lg"><i style="background:${s[2]}"></i>${s[1]}</span>`).join('')
      : `<span class="lg"><i style="background:var(--k-known)"></i>known</span>
         <span class="lg"><i style="background:var(--k0)"></i>never appears</span>
         <span class="lg"><i style="background:var(--k-hot)"></i>costs you most</span>`;
  }

  async function show(c){
    const k = GRID.find(x => x.c === c);
    if (!k) return;
    const stage = STAGES[band(k.s)][1];
    const wk = 'https://www.wanikani.com/kanji/' + encodeURIComponent(k.c);
    const where = (k.d || []).map(([i, n]) =>
      `${GRID_TITLES[i]} ${n.toLocaleString()}&times;`).join(' &middot; ');
    const moved = k.up ? `<span class="pill">moved up since last run</span>` : '';
    document.getElementById('kdetail').innerHTML =
      `<a class="big" href="${wk}" target="_blank" rel="noopener"
          title="Open on WaniKani">${k.c}</a>
       <div class="kmeta">
         <div><b>${k.m || '—'}</b> <span class="rd">${k.r || ''}</span> ${moved}</div>
         <div class="sub">WaniKani level ${k.l} &middot; ${k.k ? 'known' : stage}
           &middot; <a href="${wk}" target="_blank" rel="noopener">on WaniKani
           &#8599;</a></div>
         ${where ? `<div class="sub">${where}</div>`
                 : '<div class="sub">Does not appear in your titles.</div>'}
         <div class="words sub" id="kwords"></div>
       </div>`;
    if (!LIVE) return;
    // Example words come from Jiten and so need the proxy; the saved file
    // simply does without them.
    const slot = document.getElementById('kwords');
    slot.textContent = 'looking up words…';
    try {
      const r = await fetch(`/api/kanji/${encodeURIComponent(k.c)}`);
      const d = await r.json();
      const words = (d.topWords || []).slice(0, 12);
      slot.innerHTML = words.length
        ? 'common words: ' + words.map(w =>
            `<span class="w" title="${(w.mainDefinition || '').replace(/"/g, '')}"
             >${w.reading || ''}</span>`).join('')
        : '';
    } catch (e){ slot.textContent = ''; }
  }

  // One delegated listener beats 2,000 closures.
  out.addEventListener('click', e => {
    const tile = e.target.closest('.k');
    if (tile) show(tile.dataset.c);
  });
  function pick(sel, set){
    document.querySelectorAll(sel).forEach(b => b.onclick = () => {
      set(b);
      document.querySelectorAll(sel).forEach(x => x.classList.toggle('on', x === b));
      draw();
    });
  }
  pick('[data-mode]', b => { mode = b.dataset.mode; });
  pick('[data-scope]', b => { scope = b.dataset.scope; });

  // 2,000-odd tiles are not worth building until the fold is actually opened.
  const fold = document.getElementById('gridfold');
  const inTitles = GRID.filter(k => k.n).length;
  fold.querySelector('.cnt').textContent =
    `${GRID.length.toLocaleString()} kanji, ${inTitles.toLocaleString()} of them in your titles`;
  fold.addEventListener('toggle', () => {
    fold.querySelector('.tw').textContent = fold.open ? 'Hide the grid'
                                                      : 'Show the grid';
    if (fold.open && !drawn){ drawn = true; draw(); }
  });

  // Any other fold on the page just flips its own wording.
  document.querySelectorAll('details.fold').forEach(function (d) {
    if (d === fold) return;
    const label = d.querySelector('.tw');
    if (!label) return;
    const shut = label.textContent;
    d.addEventListener('toggle', () => {
      label.textContent = d.open ? shut.replace(/^Show/, 'Hide') : shut;
    });
  });
})();
"""

GRID_CSS = """
:root { --k0:#e8e2d9; --k-known:#15803d; --k-hot:#dc2626; }
@media (prefers-color-scheme: dark) {
  :root { --k0:#2a2622; --k-known:#22c55e; --k-hot:#ef4444; }
}
.fold { background:var(--raise); border:1px solid var(--line); border-radius:14px;
  box-shadow:var(--shadow); padding:2px 18px 4px; }
.fold summary { cursor:pointer; padding:14px 0; list-style:none; display:flex;
  flex-wrap:wrap; gap:4px 12px; align-items:baseline; }
.fold summary::-webkit-details-marker { display:none; }
.fold summary::before { content:"\\25B8"; color:var(--accent); margin-right:8px;
  transition:transform .15s; display:inline-block; }
.fold[open] summary::before { transform:rotate(90deg); }
.fold summary .tw { font-weight:600; }
.fold summary .cnt { color:var(--faint); font-size:13px; }
.fold[open] { padding-bottom:18px; }
.gridbar { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center;
  justify-content:space-between; margin-bottom:12px; }
.scope button.on { background:var(--fg); border-color:var(--fg);
  color:var(--bg); }
.scope button.on:hover { background:var(--fg); color:var(--bg); }
.means { color:var(--faint); font-weight:400; margin-left:8px; font-size:13px; }
.modes { display:flex; gap:6px; }
.modes button.on { background:var(--accent); border-color:var(--accent);
  color:#fff; }
.modes button.on:hover { background:var(--accent); color:#fff; }
.legend-row { display:flex; flex-wrap:wrap; gap:12px; font-size:11.5px;
  color:var(--muted); }
.lg { display:inline-flex; align-items:center; gap:5px; }
.lg i { width:11px; height:11px; border-radius:3px; display:inline-block; }
.gridout { border:1px solid var(--line); border-radius:12px; padding:10px 12px;
  overflow-x:auto; }
.lvlrow { display:flex; gap:10px; align-items:flex-start; padding:2px 0; }
.lvlnum { width:2em; text-align:right; font-size:10.5px; color:var(--faint);
  padding-top:7px; font-variant-numeric:tabular-nums; flex:none; }
.lvlnum.now { color:var(--accent); font-weight:700; }
.kanjis { display:flex; flex-wrap:wrap; gap:3px; }
.k { width:27px; height:27px; border-radius:6px; font-size:16px; line-height:27px;
  text-align:center; color:#fff; cursor:pointer; user-select:none;
  text-shadow:0 1px 2px rgba(0,0,0,.35); }
.k:hover { outline:2px solid var(--fg); outline-offset:1px; }
.kdetail { display:flex; align-items:center; gap:16px; margin-top:12px;
  min-height:62px; border:1px solid var(--line);
  border-radius:12px; padding:12px 16px; }
.kdetail .big { font-size:40px; line-height:1; border:0; }
.kdetail .big:hover { color:var(--accent); }
.kdetail .rd { color:var(--accent); }
.kdetail .hint { color:var(--faint); font-style:italic; }
.kdetail .sub { margin:2px 0 0; font-size:12.5px; }
.kdetail .kmeta { min-width:0; }
.kdetail .words .w { display:inline-block; margin:2px 8px 0 0; }
.kdetail .pill { margin-left:8px; }
"""

SLIDER_CSS = """
/* Joined to the chart above it: it drives the marker on that chart, so it
   should not look like a separate widget that happens to sit nearby. */
.slider { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  background:var(--raise); border:1px solid var(--line); border-radius:0 0 14px 14px;
  border-top:1px dashed var(--line);
  padding:14px 18px; margin-bottom:12px; box-shadow:var(--shadow); }
.slider label { font-size:14px; color:var(--muted); white-space:nowrap; }
.slider label b { color:var(--accent); font-size:17px;
  font-variant-numeric:tabular-nums; }
.slider input[type=range] { flex:1 1 220px; accent-color:var(--accent);
  height:22px; }
.slider .pace { display:flex; align-items:center; gap:7px; flex:0 0 auto;
  font-size:14px; color:var(--muted); }
.slider .pace input[type=number] { width:66px; font:inherit; font-size:14px;
  padding:5px 8px; border-radius:9px; border:1px solid var(--line);
  background:var(--bg); color:var(--fg); text-align:center;
  font-variant-numeric:tabular-nums; }
.slider .pace input[type=number]:focus { outline:none; border-color:var(--accent); }
.slider .pace button { font:inherit; font-size:12.5px; color:var(--faint);
  background:none; border:0; border-bottom:1px solid var(--line);
  padding:0 0 1px; cursor:pointer; white-space:nowrap; }
.slider .pace button b { color:inherit; font-weight:600; }
.slider .pace button:hover { color:var(--accent); border-bottom-color:var(--accent); }
"""

BROWSE_HTML = """
<h2 id="browse">Browse jiten.moe</h2>
<p class="sub">Search the whole catalogue. Pick a title to work out, right here,
what level it stops fighting you at.</p>
<div class="controls">
  <input id="q" type="search" placeholder="title, romaji or English &mdash; press Enter"
         autocomplete="off">
  <button id="go" class="go">Search</button>
  <select id="type">
    <option value="">any type</option>
    <option value="1">anime</option><option value="9">manga</option>
    <option value="4">novel</option><option value="7">visual novel</option>
    <option value="6">game</option><option value="2">drama</option>
    <option value="3">movie</option><option value="8">web novel</option>
  </select>
  <details class="tagpick" id="tagpick">
    <summary id="tagsum">tags</summary>
    <div class="tagmenu">
      <input id="tagq" type="search" placeholder="filter the list" autocomplete="off">
      <div id="taglist" class="taglist"></div>
      <p class="tagnote">Jiten narrows: a title has to carry every box you tick.</p>
      <div class="tagfoot">
        <button type="button" id="tagclear">clear</button>
        <button type="button" id="tagdone" class="go">Search</button>
      </div>
    </div>
  </details>
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
let lastRows = [];
// Jiten pages at 50; we show 10. Fetched pages are kept so paging back and
// forth inside the same fifty costs nothing.
let page = 0, total = 0, fetched = new Map(), lastUrl = '';
const PER = 10, API_PAGE = 50;

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function title(d){ return d.originalTitle || d.englishTitle || d.romajiTitle || '?'; }

// Genres and tags are two different filters on Jiten's side - Romance is a
// genre, Boarding School is a tag - but nobody picking one thinks in that
// distinction, so one menu drives both. The genre list is a fixed enum, so it
// is spelled out here rather than fetched.
const GENRES = [[1,'Action'],[2,'Adventure'],[3,'Comedy'],[4,'Drama'],
  [5,'Ecchi'],[6,'Fantasy'],[7,'Horror'],[8,'Mecha'],[9,'Music'],[10,'Mystery'],
  [11,'Psychological'],[12,'Romance'],[13,'Sci-Fi'],[14,'Slice of Life'],
  [15,'Sports'],[16,'Supernatural'],[17,'Thriller'],[18,'Adults only']];

// Both parameters intersect: two ticks asks for titles carrying both, not
// either. 270 boxes in all, so the menu needs a filter of its own.
const chosen = new Set();          // "g:12" for a genre, "t:87" for a tag
const picked = kind => [...chosen].filter(k => k[0] === kind)
                                  .map(k => k.slice(2)).join(',');

(function tagpicker(){
  const pick = $('#tagpick');
  if (!pick) return;
  const list = $('#taglist'), sum = $('#tagsum');
  const box = (kind, id, name) =>
    `<label data-name="${esc(String(name).toLowerCase())}">
       <input type="checkbox" value="${kind}:${id}">${esc(name)}</label>`;
  const group = (title, rows) =>
    `<div class="taggroup"><h5>${title}</h5>${rows.join('')}</div>`;

  let html = group('Genres', GENRES.map(([id, n]) => box('g', id, n)));
  if (typeof TAGS !== 'undefined' && TAGS.length)
    html += group('Tags', TAGS.map(t => box('t', t.tagId, t.name)));
  list.innerHTML = html;

  function summarise(){
    sum.textContent = chosen.size ? `${chosen.size} tag${chosen.size > 1 ? 's' : ''}`
                                  : 'tags';
    pick.classList.toggle('on', chosen.size > 0);
  }
  list.addEventListener('change', e => {
    const b = e.target;
    if (b.checked) chosen.add(b.value); else chosen.delete(b.value);
    summarise();
  });
  $('#tagq').addEventListener('input', e => {
    const needle = e.target.value.trim().toLowerCase();
    list.querySelectorAll('label').forEach(l =>
      l.classList.toggle('off', !!needle && !l.dataset.name.includes(needle)));
    // A heading with nothing under it left "Tags" floating over empty space.
    list.querySelectorAll('.taggroup').forEach(g =>
      g.classList.toggle('off', !g.querySelector('label:not(.off)')));
    list.classList.toggle('blank', !list.querySelector('label:not(.off)'));
  });
  $('#tagq').addEventListener('keydown', e => {
    if (e.key === 'Enter'){ e.preventDefault(); pick.open = false; search(); }
  });
  $('#tagclear').addEventListener('click', () => {
    chosen.clear();
    list.querySelectorAll('input').forEach(b => b.checked = false);
    summarise();
  });
  summarise();
  $('#tagdone').addEventListener('click', () => { pick.open = false; search(); });
  // A <details> stays open until told otherwise; ticking boxes must not close
  // it, clicking anywhere else must.
  document.addEventListener('click', e => {
    if (pick.open && !pick.contains(e.target)) pick.open = false;
  });
})();

async function search(){
  const q = $('#q').value.trim();
  const type = $('#type').value, sort = $('#sort').value;
  const min = $('#minchars').value;
  const genres = picked('g'), tags = picked('t');
  if (!q && !type && !min && !genres && !tags){ $('#results').innerHTML =
    '<p class="empty">Type a title and press Enter, or pick a filter.</p>'; return; }
  $('#results').innerHTML = '<p class="empty">Searching&hellip;</p>';
  let url = `/api/media-deck/get-media-decks?sortBy=${sort}&sortOrder=1`;
  if (q) url += `&titleFilter=${encodeURIComponent(q)}`;
  if (type) url += `&mediaType=${type}`;
  if (genres) url += `&genres=${genres}`;
  if (tags) url += `&tags=${tags}`;
  if (min) url += `&charCountMin=${min}`;
  lastUrl = url;
  fetched = new Map();
  page = 0;
  await showPage(0);
}

async function pageRows(p){
  const apiOffset = Math.floor(p * PER / API_PAGE) * API_PAGE;
  if (!fetched.has(apiOffset)){
    const r = await fetch(lastUrl + '&offset=' + apiOffset);
    const data = await r.json();
    total = data.totalItems || 0;
    fetched.set(apiOffset, data.data || []);
  }
  const block = fetched.get(apiOffset);
  const start = p * PER - apiOffset;
  return block.slice(start, start + PER);
}

async function showPage(p){
  try {
    page = p;
    lastRows = await pageRows(p);
    render();
  } catch (e){
    $('#results').innerHTML = '<p class="empty">Search failed: ' + esc(e) + '</p>';
  }
}

function render(){
  if (!lastRows.length){ $('#results').innerHTML =
    '<p class="empty">Nothing matched.' + (chosen.size > 1 ?
      ' Every box you tick has to sit on the same title &mdash; try fewer.' : '')
    + '</p>'; return; }
  let h = '<table class="sortable tight"><tr><th>title</th><th>type</th>' +
          '<th class="num">chars</th><th class="num">diff</th>' +
          '<th class="num">coverage</th><th></th></tr>';
  for (const d of lastRows){
    h += `<tr><td class="withcover"><span class="ct"><img class="cover"
            loading="lazy" alt=""
            src="https://cdn.jiten.moe/${d.deckId}/cover.jpg"
            onerror="this.style.visibility='hidden'">
          <a href="https://jiten.moe/decks/media/${d.deckId}/detail"
          target="_blank" rel="noopener">${esc(title(d))}</a></span></td>
          <td>${esc(WK.types[d.mediaType] || '?')}</td>
          <td class="num">${(d.characterCount||0).toLocaleString()}</td>
          <td class="num">${d.difficulty ?? '—'}</td>
          <td class="num">${d.coverage != null ? d.coverage + '%' : '—'}</td>
          <td class="acts"><button data-when="${d.deckId}">when?</button>
            <select class="setlist" data-track="${d.deckId}">
              <option value="">add to&hellip;</option>
              <option value="2">watching/reading</option>
              <option value="1">plan to watch/read</option>
              <option value="3">finished</option>
            </select></td></tr>`;
  }
  const pages = Math.max(1, Math.ceil(total / PER));
  const pager = `<div class="pager">
      <button ${page === 0 ? 'disabled' : ''} data-page="${page - 1}">Previous</button>
      <span>Page ${page + 1} of ${pages.toLocaleString()}
        &middot; ${total.toLocaleString()} titles</span>
      <button ${page + 1 >= pages ? 'disabled' : ''} data-page="${page + 1}">Next</button>
    </div>`;
  $('#results').innerHTML = h + '</table>' + (total > PER ? pager : '');
  document.querySelectorAll('#results [data-when]').forEach(b =>
    b.onclick = () => analyse(+b.dataset.when, b));
  document.querySelectorAll('#results select[data-track]').forEach(sel =>
    sel.onchange = () => {
      if (sel.value) track(+sel.dataset.track, +sel.value, sel);
    });
  document.querySelectorAll('#results [data-page]').forEach(b =>
    b.onclick = () => showPage(+b.dataset.page));
}

// Puts the title on your Jiten list, which is also what makes the next run
// pick it up automatically.
async function track(id, status, el){
  const isSelect = el.tagName === 'SELECT';
  const was = isSelect ? '' : el.textContent;
  const label = txt => {
    if (isSelect) el.options[0].textContent = txt;
    else el.textContent = txt;
  };
  el.disabled = true; label('saving…');
  try {
    const r = await fetch(`/api/user/deck-preferences/${id}/status`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    label({2: 'watching/reading ✓', 1: 'planned ✓',
           3: 'finished ✓'}[status] || 'saved ✓');
    el.classList.add('done');
    if (isSelect) el.selectedIndex = 0;
  } catch (e){
    label('failed'); el.disabled = false;
    setTimeout(() => { label(isSelect ? 'add to…' : was); }, 2000);
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

// word -> occurrences, straight from the server's cache, tallied into
// kanji -> occurrences.
async function kanjiCounts(deckId){
  const words = await (await fetch('/words/' + deckId)).json();
  const occ = new Map();
  for (const w in words){
    const n = words[w];
    for (let i = 0; i < w.length; i++){
      const c = w.charCodeAt(i);
      if ((c >= 0x4e00 && c <= 0x9fff) || (c >= 0x3400 && c <= 0x4dbf) ||
          (c >= 0xf900 && c <= 0xfaff)){
        const ch = w[i];
        occ.set(ch, (occ.get(ch) || 0) + n);
      }
    }
  }
  return occ;
}

async function analyse(id, btn){
  btn.disabled = true; btn.textContent = 'reading…';
  const d = lastRows.find(r => r.deckId === id) || {};
  try {
    const occ = await kanjiCounts(id);

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

    const MEANS = {
      80: 'every few lines needs a lookup',
      90: 'readable with a dictionary at hand',
      95: 'comfortable — the usual bar for reading for pleasure',
      98: 'you stop noticing the kanji',
    };
    let rows = '';
    for (const t of [80, 90, 95, 98]){
      const need = levelFor(curve, t);
      const done = now >= t;
      rows += `<tr><td>${t}% <span class="means">${MEANS[t]}</span></td>
               <td class="num">${done ? '<span class="up">reached</span>'
                                      : (need ?? 'never')}</td>
               <td class="num">${done ? '' : (need
                    ? when(need - WK.level)
                    : 'blocked by kanji WaniKani never teaches')}</td></tr>`;
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
      <div class="wrap"><table><tr><th>kanji coverage</th>
        <th class="num">at level</th><th class="num">which is</th></tr>
        ${rows}</table></div>
      <p class="sub">Kanji coverage is a floor, not a ceiling — grammar and the
      words WaniKani never teaches decide the rest.</p>`;
    $('#detail').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e){
    $('#detail').innerHTML = '<p class="empty">Could not read that title: ' +
      esc(e) + '</p>';
  }
  btn.disabled = false; btn.textContent = 'when?';
}

// Searching on every keystroke meant a request and a re-render while you were
// still typing. Enter only.
$('#q').addEventListener('keydown', e => {
  // Enter also confirms an IME conversion; that one is not a submit.
  if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); search(); }
});
$('#go').addEventListener('click', search);
for (const id of ['#type', '#sort', '#minchars'])
  $(id).addEventListener('change', () => { if ($('#q').value.trim() ||
    $('#type').value || $('#minchars').value || chosen.size) search(); });
"""

BROWSE_CSS = """
.controls { display:flex; flex-wrap:wrap; gap:9px; margin:0 0 14px; }
.controls input, .controls select { font:inherit; font-size:14px; padding:10px 13px;
  border:1px solid var(--line); border-radius:11px; background:var(--raise);
  color:var(--fg); box-shadow:var(--shadow); }
.controls input:focus, .controls select:focus { outline:2px solid var(--accent);
  outline-offset:-1px; }
/* Basis, not width: six controls have to share one row at the 876px the
   page gives them, and the search box absorbs whatever is left over. */
.controls #q { flex:1 1 200px; }
.controls #minchars { width:120px; }

/* tag picker: a <details> pretending to be a multi-select */
.tagpick { position:relative; }
.tagpick > summary { list-style:none; cursor:pointer; user-select:none;
  font-size:14px; padding:10px 13px; border:1px solid var(--line);
  border-radius:11px; background:var(--raise); color:var(--fg);
  box-shadow:var(--shadow); white-space:nowrap; min-width:76px; }
.tagpick > summary::-webkit-details-marker { display:none; }
.tagpick > summary::after { content:" \\25be"; color:var(--faint); }
.tagpick[open] > summary, .tagpick.on > summary { border-color:var(--accent);
  color:var(--accent); }
.tagmenu { position:absolute; z-index:30; top:calc(100% + 6px); left:0;
  width:296px; max-width:82vw; padding:12px; background:var(--raise);
  border:1px solid var(--line); border-radius:14px;
  box-shadow:0 12px 34px rgba(0,0,0,.24); }
.tagmenu #tagq { width:100%; margin:0 0 9px; padding:8px 11px; }
.taglist { max-height:238px; overflow:auto; margin:0 -4px; }
.taglist label { display:flex; align-items:center; gap:9px; font-size:13.5px;
  padding:4px 7px; border-radius:8px; cursor:pointer; }
.taglist label:hover { background:var(--accent-soft); color:var(--accent); }
.taglist label.off, .taggroup.off { display:none; }
.taglist.blank::after { content:"No genre or tag by that name."; display:block;
  color:var(--faint); font-size:13px; font-style:italic; padding:8px 7px; }
.taggroup h5 { font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--faint); font-weight:650; margin:10px 0 3px; padding:0 7px; }
.taggroup:first-child h5 { margin-top:0; }
.taglist input { accent-color:var(--accent); margin:0; }
.tagnote { color:var(--faint); font-size:11.5px; line-height:1.45;
  margin:9px 0 0; }
.tagfoot { display:flex; align-items:center; justify-content:space-between;
  gap:9px; margin-top:10px; }
.tagfoot button.go { padding:7px 16px; }
/* Narrow screens: hang the menu off the whole row rather than off the button,
   which otherwise pushes 296px of menu past the right edge. */
@media (max-width:640px) {
  .controls { position:relative; }
  .tagpick { position:static; }
  .tagmenu { left:0; right:0; width:auto; max-width:none; }
}
button.go { padding:10px 20px; border-radius:11px; font-size:14px;
  background:var(--accent); border-color:var(--accent); color:#fff; }
button.go:hover { background:var(--accent); color:#fff; filter:brightness(1.08); }
button { font:inherit; font-size:12.5px; font-weight:560; padding:5px 12px;
  cursor:pointer; border:1px solid var(--line); border-radius:99px;
  background:var(--bg); color:var(--muted); white-space:nowrap; }
button:hover:not(:disabled) { border-color:var(--accent); color:var(--accent);
  background:var(--accent-soft); }
button:disabled { opacity:.55; cursor:default; }
button.done { color:var(--good); border-color:var(--good); }
td.acts { white-space:nowrap; text-align:right; }
td.acts button, td.acts select { margin-left:5px; }
select.setlist { font:inherit; font-size:12px; padding:4px 8px; border-radius:99px;
  border:1px solid var(--line); background:var(--bg); color:var(--muted);
  max-width:11em; }
select.setlist:hover { border-color:var(--accent); color:var(--accent); }
select.setlist.done { color:var(--good); border-color:var(--good); }
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
    jkey = jimaku_key(getattr(args, "jimaku_key", None))
    nkey = nihongo_key(getattr(args, "nihongo_key", None))

    # Listed but never analysed: you are done with these, and each would cost a
    # word-list download to measure.
    finished = []
    if key:
        try:
            finished = jiten_status_decks("completed", key)
        except SystemExit:
            finished = []

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

    # What you have actually watched and read. Optional, and quiet when it is
    # not configured: no key, no logs, or no AniList link on a title all just
    # mean the column does not appear.
    nprog: dict[int, dict] = {}
    ntotals = None
    nunmeasured: list[dict] = []
    if nkey:
        who = nihongo_whoami(nkey)
        if who:
            ntotals = nihongo_totals(nkey, who)
            nindex = nihongo_index(who, nkey)
            nprog = nihongo_progress([d for d, _ in rows], nkey, who, nindex)
            nunmeasured = nihongo_unmeasured(
                nindex, [d for d, _ in rows] + finished, key)
            print(f"NihongoTracker: {who}, {len(nprog)} of {len(rows)} tracked "
                  f"titles have logs, {len(nunmeasured)} logged but untracked")

    head = (f"<title>{esc(cache.get('username'))} - WaniKani coverage</title>"
            f"<style>{REPORT_CSS}</style>")

    sections: list[tuple[str, str]] = []

    def h2(label: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        sections.append((slug, label))
        return f'<h2 id="{slug}">{esc(label)}</h2>'

    h = ["<main>",
         f'<div class="hero">{brand_mark()}<div class="hd">'
         f'<h1>{esc(cache.get("username"))} on '
         f'<span>jiten.moe</span></h1>'
         f'<p class="sub">WaniKani level {lvl} &middot; generated '
         f'{time.strftime("%d %b %Y, %H:%M")}</p></div></div>',
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
    if ntotals:
        streak = ntotals["streak"]
        h.append(f'<div class="card"><div class="n">{ntotals["hours"]:.0f}h</div>'
                 f'<div class="l">immersion logged</div>'
                 f'<div class="d">{ntotals["listening"]:.0f}h listening &middot; '
                 f'{ntotals["reading"]:.0f}h reading'
                 + (f' &middot; {streak}d streak' if streak else "")
                 + '</div></div>')
    h.append("</div>")
    h.append(level_bar_html(level_progress(cache), wk_pace(cache)))
    h.append(BROWSE_SLOT)

    h.append(h2("Your tracked titles"))
    groups = by_media_type(rows)
    split = len(groups) > 1
    tcls = "sortable tight grouped" if split else "sortable tight"
    if nprog:
        tcls += " nt"
    for mtype, group in groups:
        if split:
            h.append(f'<h3 class="mediahead">'
                     f'{esc(MEDIA_TYPES.get(mtype, "other"))}'
                     f' <span>{len(group)}</span></h3>')
        h.append(f'<div class="wrap"><table class="{tcls}"><tr><th>title</th>'
                 f'<th class="num">kanji</th><th class="num">finish L{lvl}</th>'
                 f'<th class="num">jiten</th><th class="num">lvl 95%</th>'
                 f'<th class="num">ceiling</th><th class="num">trend</th>'
                 + ('<th class="num">immersion</th>' if nprog else "")
                 + '</tr>')
        for deck, res in group:
            deck_id = deck.get("deckId")
            live = deck.get("coverage")
            trend = history_trend(past.get(deck_id, []))
            t = (f'<span class="up">{trend[1] - trend[0]:+.1f}pp</span> / {trend[2]}d'
                 if trend and trend[1] > trend[0] else
                 (f"{trend[1] - trend[0]:+.1f}pp / {trend[2]}d" if trend else "&mdash;"))
            k = res["kanji_cov_occ"]
            subs = jimaku_url(deck, jkey)
            out = outside_link(deck)
            fin = finishing_level(res, lvl)
            fin_cell = ("&mdash;" if fin is None else
                        f'{fin:.1f}% <span class="up">{fin - k:+.1f}</span>')
            nsort, ncell = nihongo_cell(nprog.get(deck_id))
            h.append(
                f'<tr><td><a href="https://jiten.moe/decks/media/{deck_id}/detail">'
                f'{esc(deck_title(deck))}</a>'
                + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                   f' rel="noopener" title="Look it up on {esc(out[0])}">'
                   f'{esc(out[0])}</a>' if out else "")
                + (f' <button class="subs" data-entry="{subs.rsplit("/", 1)[-1]}"'
                   f' data-title="{esc(deck_title(deck))}"'
                   f' title="Japanese subtitles on jimaku.cc">subs</button>'
                   if subs else "")
                + f' <button class="subs setst" data-deck="{deck_id}" data-st="3"'
                  f' data-done="finished ✓"'
                  f' title="Mark as finished on jiten.moe">finished</button>'
                + f' <button class="subs setst" data-deck="{deck_id}" data-st="0"'
                  f' data-done="removed ✓" data-confirm="1" data-drop="1"'
                  f' title="Take it off your jiten.moe lists">remove</button>'
                + f'</td>'
                f'<td class="num">{k:.1f}%'
                f'<span class="meter"><i style="width:{k:.1f}%"></i></span></td>'
                f'<td class="num">{fin_cell}</td>'
                f'<td class="num">{f"{live:.1f}%" if live is not None else "&mdash;"}</td>'
                f'<td class="num">{level_for(res["curve"], 95) or "&mdash;"}</td>'
                f'<td class="num">{100 - res["not_in_wk_pct"]:.1f}%</td>'
                f'<td class="num">{t}</td>'
                + (f'<td class="num" data-sort="{nsort}">{ncell}</td>'
                   if nprog else "")
                + '</tr>')
        h.append("</table></div>")

    h.append('<div id="subsbox" class="subsbox" hidden></div>')

    if nkey:
        h.append(h2("Immersion"))
        h.append(immersion_html(ntotals, nunmeasured))

    if finished:
        h.append(h2("Finished"))
        n = len(finished)
        h.append(f'<p class="sub">{n} title{"" if n == 1 else "s"} you have '
                 f'finished. No coverage is worked out for these &mdash; you are '
                 f'done with them. They are what <b>Because you know these</b> '
                 f'compares against, so the more you mark, the better those '
                 f'suggestions get.</p>')
        h.append('<div class="wrap"><table class="sortable"><tr><th>title</th>'
                 '<th>type</th><th class="num">chars</th>'
                 '<th class="num">jiten coverage</th></tr>')
        for d in finished:
            did = d.get("deckId")
            name = d.get("originalTitle") or d.get("englishTitle") or "?"
            cov = d.get("coverage")
            cov_txt = f"{cov}%" if cov is not None else "&mdash;"
            hide = "this.style.visibility='hidden'"
            out = outside_link(d)
            h.append(
                f'<tr><td class="withcover"><span class="ct">'
                f'<img class="cover" loading="lazy" alt="" src="{cover_url(did)}"'
                f' onerror="{hide}">'
                f'<span><a href="https://jiten.moe/decks/media/{did}/detail"'
                f' target="_blank" rel="noopener">{esc(name)}</a>'
                + (f' <a class="subs" href="{esc(out[1])}" target="_blank"'
                   f' rel="noopener" title="Look it up on {esc(out[0])}">'
                   f'{esc(out[0])}</a>' if out else "")
                + '</span></span></td>'
                f'<td>{MEDIA_TYPES.get(d.get("mediaType"), "?")}</td>'
                f'<td class="num">{d.get("characterCount") or 0:,}</td>'
                f'<td class="num">{cov_txt}</td></tr>')
        h.append("</table></div>")
    else:
        h.append(h2("Finished"))
        h.append('<p class="empty">Nothing marked finished yet. Search above for '
                 'something you have already seen and press <b>finished</b>, or '
                 'use the button beside a title you are tracking.</p>')

    counts = month_totals(cache)
    if counts:
        h.append(h2("Lessons and passes"))
        h.append(counters_html(counts))

    h.append(h2("What each level would buy you"))
    h.append(CHART_HTML)
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
    # Which title each kanji turns up in, and how often - "899x in your titles"
    # is less useful than knowing it is nearly all one show.
    grid_titles = [deck_title(d) for d, _ in rows]
    per_title: dict[str, list] = {}
    for i, (_deck, res) in enumerate(rows):
        for ch, n in res["kanji_occ"].items():
            per_title.setdefault(ch, []).append([i, n])
    for ch in per_title:
        per_title[ch].sort(key=lambda p: -p[1])

    # Anything that climbed an SRS stage since the previous snapshot.
    moved_up: set[str] = set()
    if os.path.exists(WK_CACHE_PREV):
        with open(WK_CACHE_PREV, encoding="utf-8") as f:
            old = json.load(f)
        old_stage = old.get("assignments", {})
        old_subj = old.get("subjects", {})
        for sid, s in subjects.items():
            if s["type"] != "kanji":
                continue
            if sid in old_subj and assignments.get(sid, 0) > old_stage.get(sid, 0):
                moved_up.add(s["characters"])

    grid_data = sorted(
        ({"c": s["characters"], "l": s["level"],
          "s": assignments.get(sid, 0),
          "k": s["characters"] in known["kanji_known"],
          "n": occ.get(s["characters"], 0),
          "r": "、".join(s.get("readings") or []),
          "m": s.get("meaning") or "",
          "d": per_title.get(s["characters"], []),
          **({"up": 1} if s["characters"] in moved_up else {})}
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
        blocked = sum(row[0] for row in leeches)
        h.append(f'<details class="fold"><summary><span class="tw">Show the '
                 f'{len(leeches)} worst</span><span class="cnt">{blocked:,} '
                 f'occurrences you cannot read, all in items already sitting in '
                 f'your review queue</span></summary>')
        h.append('<p class="sub">Apprentice kanji, ranked by how often they appear '
                 'in the titles above.</p>')
        h.append('<div class="wrap"><table class="sortable"><tr><th>kanji</th><th>reading</th>'
                 '<th>meaning</th><th class="num">occurrences</th><th>stage</th>'
                 '<th class="num">wk level</th></tr>')
        for n, ch, stage, klvl, readings, meaning in leeches:
            wk = f"https://www.wanikani.com/kanji/{urllib.parse.quote(ch)}"
            h.append(f'<tr><td class="kanji"><a href="{wk}" target="_blank" '
                     f'rel="noopener" title="Open on WaniKani">{esc(ch)}</a></td>'
                     f'<td>{esc(readings)}</td><td>{esc(meaning)}</td>'
                     f'<td class="num">{n:,}</td>'
                     f'<td>{SRS_STAGE_NAMES.get(stage, "?")}</td>'
                     f'<td class="num">{klvl}</td></tr>')
        h.append("</table></div></details>")
    else:
        h.append('<p class="empty">Nothing in Apprentice shows up in your tracked '
                 'titles.</p>')

    others: list[dict] = []
    tags: list[dict] = []
    target_level = min(60, lvl + getattr(args, "soon_levels", 5))
    if key:
        # Both the browse and the reach panel filter on these, so they are worth
        # one request even when the recommendations are switched off.
        try:
            tags = get_json(f"{JITEN_API}/api/media-deck/tags",
                            headers=jiten_headers(key))
        except SystemExit:
            tags = []
    if key and not args.no_recommend:
        data = collect_status(args, key, cache, known)
        target_level = data["target_level"]
        h.append(h2("Best titles for you right now"))
        h.append('<div class="wrap"><table class="sortable"><tr><th>type</th><th>title</th>'
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
        others = [{"t": d.get("originalTitle") or d.get("englishTitle") or "?",
                   "id": d.get("deckId"), "now": round(now, 1),
                   "then": round(later, 1), "gain": round(gain, 1)}
                  for gain, now, later, d in data["gains"]]

    h.append(h2("Nearly within reach"))
    h.append(REACH_HTML)

    h.append(h2("Because you know these"))
    h.append(LIKE_HTML)

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
    ranked = sorted(rows, key=lambda r: -r[1]["kanji_cov_occ"])
    track = json.dumps({
        "level": lvl, "pace": pace,
        # Deck ids in the same order as the titles, so the picker can name one.
        "ids": [d.get("deckId") for d, _r in ranked],
        "titles": [{"t": deck_title(d),
                    "now": round(r["kanji_cov_occ"], 2),
                    "c": [round(p, 2) for _lv, p in r["curve"]]}
                   for d, r in ranked],
    }, ensure_ascii=False, separators=(",", ":"))
    head += f"<style>{SLIDER_CSS}{GRID_CSS}{CHART_CSS}{REACH_CSS}{SUBS_CSS}</style>"
    titles_json = json.dumps(grid_titles, ensure_ascii=False, separators=(",", ":"))
    others_json = json.dumps(others, ensure_ascii=False, separators=(",", ":"))
    tags_json = json.dumps(tags, ensure_ascii=False, separators=(",", ":"))

    def compose(live: bool) -> str:
        parts, css = list(h), head
        parts.append(f"<script>const TRACK={track};const GRID={grid_json};"
                     f"const GRID_LEVEL={lvl};const GRID_TITLES={titles_json};"
                     f"const LIVE={'true' if live else 'false'};"
                     f"const OTHERS={others_json};const TAGS={tags_json};"
                     f"const REACH_TARGET={target_level};</script>"
                     f"<script>{SORT_JS}</script><script>{SLIDER_JS}</script>"
                     f"<script>{CHART_JS}</script><script>{GRID_JS}</script>"
                     f"<script>{REACH_JS}</script><script>{SUBS_JS}</script>"
                     f"<script>{LIKE_JS}</script><script>{STATUS_JS}</script>")
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
                + favicon_link() + css + "</head><body>"
                + "".join(parts) + "</body></html>")

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
            # Word lists come from the on-disk cache, not straight through the
            # proxy. Jiten allows ten of these heavy downloads a minute, and a
            # page that analyses a dozen titles would stall on the eleventh.
            # It is also far less for the browser to chew on: a few thousand
            # word counts instead of half a megabyte of repeated lines.
            # Titles built on vocabulary you have already met elsewhere.
            if self.path.startswith("/like/"):
                parts = self.path.split("?")[0].strip("/").split("/")
                query = urllib.parse.parse_qs(
                    self.path.split("?")[1] if "?" in self.path else "")
                try:
                    source_id = int(parts[1])
                except (ValueError, IndexError):
                    return self._send(400, b"bad deck", "text/plain")
                src_deck = jiten_deck_detail(source_id, key)
                source = deck_words(source_id, key, src_deck)
                url = (f"{JITEN_API}/api/media-deck/get-media-decks"
                       f"?sortBy=coverage&sortOrder=1&charCountMin=20000")
                mtype = (query.get("type") or [""])[0]
                if mtype:
                    url += f"&mediaType={mtype}"
                cands = [r for r in
                         (get_json(url, headers=jiten_headers(key)).get("data") or [])
                         if r["deckId"] != source_id][:10]
                results = []
                for r in cands:
                    try:
                        words = deck_words(r["deckId"], key, r)
                    except SystemExit:
                        continue
                    results.append({
                        "id": r["deckId"],
                        "title": (r.get("originalTitle") or r.get("englishTitle")
                                  or "?"),
                        "shared": round(vocab_overlap(source, words), 1),
                        "coverage": r.get("coverage"),
                        "chars": r.get("characterCount")})
                results.sort(key=lambda x: -x["shared"])
                return self._send(200, json.dumps(
                    {"source": deck_title(src_deck), "results": results},
                    ensure_ascii=False).encode(), "application/json")

            # Subtitles: list what is worth having, or hand over a zip of it.
            if self.path.startswith("/subs/"):
                jkey = jimaku_key(getattr(args, "jimaku_key", None))
                if not jkey:
                    return self._send(400, b"no jimaku key", "text/plain")
                parts = self.path.split("?")[0].strip("/").split("/")
                query = urllib.parse.parse_qs(
                    self.path.split("?")[1] if "?" in self.path else "")
                dual = query.get("dual", ["0"])[0] == "1"
                try:
                    entry = int(parts[1])
                except (ValueError, IndexError):
                    return self._send(400, b"bad entry", "text/plain")
                rows = jimaku_files(entry, jkey)
                keep = wanted_subtitles(rows, allow_dual=dual)
                if len(parts) > 2 and parts[2] == "zip":
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for r in keep:
                            st, data, _ = http(r["url"], timeout=120)
                            if st < 400:
                                z.writestr(r["name"], data)
                    name = urllib.parse.quote(f"jimaku-{entry}.zip")
                    body = buf.getvalue()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition",
                                     f"attachment; filename={name}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionAbortedError):
                        pass
                    return
                payload = json.dumps({
                    "files": [{"name": r["name"], "url": r["url"],
                               "size": r.get("size"), "lang": r["lang"]}
                              for r in keep],
                    "skipped": sum(1 for r in rows if r not in keep),
                    "total": len(rows),
                    "onlyDual": bool(keep) and all(r["lang"] == "dual" for r in keep),
                }, ensure_ascii=False).encode()
                return self._send(200, payload, "application/json")

            if self.path.startswith("/words/"):
                try:
                    deck_id = int(self.path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._send(400, b"bad id", "text/plain")
                try:
                    deck = jiten_deck_detail(deck_id, key)
                    counts = deck_words(deck_id, key, deck)
                except SystemExit as e:
                    return self._send(502, str(e).encode(), "text/plain")
                body = json.dumps(dict(counts), ensure_ascii=False,
                                  separators=(",", ":")).encode()
                return self._send(200, body, "application/json")
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
        parser.add_argument("--jimaku-key", default=d(None),
                            help="jimaku.cc API key, for subtitle links")
        parser.add_argument("--nihongo-key", default=d(None),
                            help="nihongotracker.app API key, for the hours "
                                 "you have logged on each title")
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

    s = subparser("setup", help="create the key files and show what is filled in")
    s.set_defaults(func=cmd_setup)

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
    s.add_argument("--genre", help="comma-separated genres, e.g. romance,comedy "
                                   "(a title has to have all of them)")
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

    s = subparser("like", help="titles built on vocabulary you already met")
    s.add_argument("deck_id", type=int, help="the title you already know")
    s.add_argument("--type", help="restrict to anime, manga, novel, ...")
    s.add_argument("--min-chars", type=int, default=20000)
    s.add_argument("--limit", type=int, default=12,
                   help="candidates to read (each is one download, then cached)")
    s.add_argument("--top-n", type=int, default=10)
    s.set_defaults(func=cmd_like)

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
    made = ensure_key_files()
    if made:
        print("Created " + ", ".join(made) + " - paste your keys into them.",
              file=sys.stderr)
    args.func(args)


if __name__ == "__main__":
    main()
