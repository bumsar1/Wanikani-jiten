# wkjiten — WaniKani coverage for jiten.moe decks

Jiten works out coverage from *your* known words, but it can only import from
Anki, JPDB, a frequency band, or its own backup. There is no WaniKani button.
This tool bridges the gap — two ways, because WaniKani and Jiten measure
different things:

| | What you get | Where |
|---|---|---|
| `export` + `push` | Your WaniKani words become "known words" on Jiten, so the coverage column, filters and sorting on **jiten.moe work for you** | on the website |
| `deck` / `batch` | **Kanji** coverage per deck — what wanilog's read-check measures — plus the WaniKani level you need to hit 95% / 98% | in the terminal |
| `status` | Progress since your last run, the best titles for you right now per media type, and what comes within reach as you level | in the terminal |
| `leeches` | The Apprentice items you keep failing, ranked by how often they actually block the titles you want to read | in the terminal |
| `search` | Browse jiten.moe by title, media type and genre, with your coverage | in the terminal |
| `when` | Whether a title is worth starting yet — and if not, which level and roughly when | in the terminal |
| `next` | The kanji worth learning next, priced in coverage on your own titles | in the terminal |
| `parts` | Per-episode / per-volume breakdown, so you know where a series opens up | in the terminal |
| `edge` | Titles that are easier for you than their difficulty rating suggests | in the terminal |
| `gap` | The words in a title you cannot read yet, as a CSV | a file |
| `serve` | The dashboard **plus a live search box for the whole jiten.moe catalogue** — pick any title and get its verdict on the spot | in your browser |
| `report` | The same dashboard as one self-contained HTML file | in your browser |

The vocabulary figure will look modest (WaniKani teaches ~6,500 words) while the
kanji figure runs high. Both are true; they just measure from different angles.

Python 3.9+ and the standard library. No pip install.

---

## The easy way: double-click

Once your keys are in place (see below), you never have to touch a terminal:

* **Windows** — `Update coverage.bat`
* **macOS** — `Update coverage.command`

It re-fetches your WaniKani data, sends the word list to jiten.moe, prints
coverage for everything you are tracking, lists the leeches that are holding you
back, and finishes by opening jiten.moe and a live dashboard with a **search box
for the whole catalogue** — plus:

* **how many new kanji and words** you have learned since the last run, and which
* **coverage per tracked title**, with the trend since you started measuring and
  what simply finishing your current level would add
* **what each WaniKani level would buy you** on those titles, as a curve
* **top 5 titles per media type** — novels, visual novels, anime, manga, games —
  ranked by your actual coverage
* **what is nearly within reach**: titles just below that list, with kanji
  coverage now vs. five levels from now, sorted by how much they move

Run it whenever you gain a level. The last step keeps running so the search box
works — leave that window open while you browse, and press Ctrl+C when done.

On macOS the file has to be made executable once: open Terminal, type `chmod +x `
(with a trailing space), drag the file into the window, press return.

**The title list comes from your own Jiten statuses.** Whatever you have marked
as *watching/reading* or *plan to watch/read* on jiten.moe is what gets tracked —
set a status on the site and it shows up on the next run, no config needed.

```bash
python wkjiten.py batch --status ongoing,planning,fav
python wkjiten.py batch --status ""     # ignore Jiten's lists
```

`decks.txt` is merged in on top, for titles you want to follow without putting
them on a list. One deck id per line, anything after `#` is ignored; find ids
with `python wkjiten.py search "title"`.

---

## Setup

**WaniKani token** (read-only is enough) — <https://www.wanikani.com/settings/personal_access_tokens>

```bash
setx WANIKANI_TOKEN "your-token-here"
```

or drop it in `wanikani_token.txt` next to the script.

**Jiten API key** (only needed for `push` and for live coverage) — jiten.moe →
Settings → Advanced → API Key. It is shown **once**. Save it in `jiten_key.txt`
or as `JITEN_API_KEY`.

The first run fetches ~9,000 subjects plus your assignments from WaniKani and
caches them in `cache/wanikani.json`. Use `--refresh` after you level up.

---

## 1) Get WaniKani coverage showing on jiten.moe itself

```bash
python wkjiten.py export
```

Writes `wanikani-known-words.txt`, one word per line — exactly the format
Jiten's importer expects ("everything before the first tab or comma").

Upload it:

```bash
python wkjiten.py push
```

…or by hand at jiten.moe → Settings → Vocabulary → import from file, if your API
key turns out to be read-only (`push` says so clearly if it is).

After that the deck list on jiten.moe shows your real coverage, and you can sort
and filter on `coverageMin` and friends like any other user.

**What counts as known?** The default is SRS stage ≥ 5 (Guru I — WaniKani's own
"passed"). Adjust it:

```bash
python wkjiten.py export --min-stage 9           # burned only
python wkjiten.py export --mode level --level 30 # everything up to level 30
```

Two things worth enabling on Jiten afterwards:

* **Composition inference** (Settings → Vocabulary) infers compound words from
  the ones you know. WaniKani words are exactly those building blocks, so this
  lifts the number noticeably.
* **Word sets** → "Particles & Common Grammar". WaniKani never teaches particles,
  and they are the most frequent words in all Japanese.

---

## 2) Kanji coverage per deck (the wanilog angle)

Find something to read:

```bash
python wkjiten.py search "yotsuba"
python wkjiten.py search --type anime --min-chars 30000 --sort coverage
python wkjiten.py search --type novel --genre romance --sort difficulty --ascending
```

The query is optional — leave it out and browse by filter alone. With an API key
the `cover` column is your own coverage, and `--sort coverage` puts the titles
you can most nearly read at the top:

```
      id  type              chars  diff   cover  title
   48529  anime            49,922     0  77.63%  一週間フレンズ。
     614  anime            61,391     2   76.6%  pet
    9129  anime            30,716     1  75.71%  true tears
```

Then run the report:

```bash
python wkjiten.py deck 96859
```

You get kanji coverage by both occurrence and unique characters, the same for
vocabulary, a curve showing what each WaniKani level would give you on that
specific title, which level reaches 90/95/98/99%, and the most frequent kanji
you are missing along with the level they sit at.

The **hard ceiling** is the share of kanji in the work that WaniKani never
teaches — names, rare characters. Even at level 60 you do not get past it.

Several decks into a CSV you can sort in Excel:

```bash
python wkjiten.py batch                      # your Jiten lists + decks.txt
python wkjiten.py batch 96859 118624 --out coverage.csv
python wkjiten.py batch --search "one piece" --limit 10
```

The `list` column shows which Jiten status each title came from, so you can see
at a glance how far along you are on what you are actually reading versus what
is still sitting on the plan-to-read pile.

The `finish Lnn` column is what your current level is still worth: coverage once
every kanji up to and including the level you are on sits at Guru. It is always
ahead of your actual figure, because some items on levels you have passed have
slipped back to Apprentice and the level you are on is only part done. Closing
that gap needs no new levels at all — just reviews.

---

## 3) Status: progress and recommendations

```bash
python wkjiten.py status
```

Progress is measured against a snapshot saved automatically every time you run
with `--refresh`. The very first run has nothing to compare against.

The recommendations come from Jiten itself: with an API key, `get-media-decks`
can sort by `coverage` — *your* coverage — server-side. That is one request per
media type instead of pulling down thousands of decks and computing locally.

"Nearly within reach" is computed locally instead, because no server knows the
kanji curve per WaniKani level. It costs one request per title, so it defaults
to 6 candidates:

```bash
python wkjiten.py status --soon-limit 15 --soon-levels 10
python wkjiten.py status --soon-limit 0     # skip the projection entirely
```

---

## 4) Leeches that actually block your reading

```bash
python wkjiten.py leeches
```

WaniKani ranks leeches by how often you fail them, which says nothing about
whether the item matters for the books you want to read. This crosses your SRS
stage with occurrence counts in your tracked titles:

```
These 23 Apprentice kanji account for 1,769 of the 18,399 kanji occurrences
you cannot read yet - 9.6% of the gap, sitting in items you have already unlocked.

  occur  kanji reading         meaning        stage            lvl
    575  言    げん、ごん      Say            Apprentice IV      5
    221  彼    かれ、かの      He             Apprentice III    12
    181  持    じ              Hold           Apprentice IV      9
```

Vocabulary you are struggling with is listed the same way underneath.

Both APIs are needed for this and neither site can do it alone: WaniKani knows
your SRS stage, Jiten knows the frequencies, and only the two together tell you
which review is worth the most reading.

---

## 5) Is this title worth starting yet?

```bash
python wkjiten.py when 21948
python wkjiten.py when "kuroko" "yotsuba"     # ids or title fragments
```

```
  黒子のバスケ
anime | 97,477 chars | difficulty 2

Right now, at level 12
  kanji coverage    64.44%  #####################...........
  word coverage     64.48%  #####################...........   (jiten.moe)
  finishing level 12 takes kanji to 70.22% (+5.78pp)

When the kanji stop getting in the way
  (at your recent pace of 20 days per level)

   kanji  level  levels to go         time     around
     80%     19             7    ~4 months   Dec 2026
     90%     30            18   ~12 months   Aug 2027
     95%     41            29   ~19 months   Mar 2028
     98%     50            38   ~2.0 years   Aug 2028

Early. Kanji stay in the way until about level 41, ~19 months off.
```

The dates come from your own `level_progressions`, using the **median of your
last six levels** rather than a lifetime average — one break in your history
drags the mean into the hundreds of days while the median stays honest.

Set your own bar with `--comfortable 90` if 95% kanji coverage is stricter than
you need.

**What this does not say.** Kanji coverage is a floor, not a ceiling. Knowing
the characters is necessary but nowhere near sufficient — grammar and the tens
of thousands of words WaniKani never teaches decide the rest. The word coverage
figure from jiten.moe is the honest one, and it climbs by reading rather than by
levelling. The two also diverge: 一週間フレンズ。sits at 77.6% word coverage but
only 60.7% kanji coverage, because dialogue-heavy shows reuse common words while
still reaching for characters you have not met.

---

## 6) What to learn next, and where to start

```bash
python wkjiten.py next
```

The mirror image of `leeches`: every kanji you cannot read yet, ranked by how
much coverage it would buy you on the titles you actually track, with a running
total and whether it is unlocked, locked, or already in your queue.

```
rank   occur kanji reading         meaning         lvl    gain  running  status
   1     624 俺    おれ            I                13   1.35%    1.35%  locked until level 13
   2     575 言    げん、ごん      Say               5   1.25%    2.60%  Apprentice IV
   3     402 合    ごう、がっ      Suit             12   0.87%    3.47%  unlocked, not started
```

```bash
python wkjiten.py parts 21948           # per-episode / per-volume
python wkjiten.py parts 21948 --kanji   # also compute kanji coverage per part
```

A series is not one difficulty. `parts` reads the subdecks — episodes, volumes,
chapters — which already carry your coverage, so the breakdown costs no extra
requests. It reports the spread and only suggests an entry point when the spread
is wide enough to be worth skipping around for; otherwise it tells you to start
at the beginning.

```bash
python wkjiten.py edge
```

Jiten's difficulty score is one number for everybody, but WaniKani front-loads
particular kanji, so some titles sit well above the trend for *your* account.
`edge` samples titles across the catalogue, fits coverage against difficulty,
and reports the biggest positive residuals — titles punching above their rating
for you specifically. It prints the fitted slope and the difficulty range it
covers so you can judge the fit.

```bash
python wkjiten.py gap 21948 --target 95 --no-sentences
```

Writes a CSV of the words in a title you do not know yet — word, furigana,
reading, occurrences, pitch — filtered server-side against your account.
`--target 95` stops once the list is enough to reach 95% coverage.

`batch` also shouts when a title crosses a coverage threshold since the last
run, which is what makes the plan-to-read pile useful:

```
*** 捏造トラップ―NTR― just crossed 65.0% coverage (60.0% -> 73.2%). Might be time to start it. ***
```

Set the bar with `--alert-at 85`.

---

## 7) The dashboard, and searching jiten.moe from it

```bash
python wkjiten.py serve               # live dashboard, Ctrl+C to stop
python wkjiten.py report              # static file, no search
python wkjiten.py report --no-open    # just write the file
```

Both give you the tracked-title table with progress trends, an SVG curve of what
every WaniKani level would give you on each title, coverage over time once you
have two days of history, the leech table, and the recommendations. No external
scripts, fonts or trackers, and it follows your system's light/dark setting.

Both also carry a **level slider** — drag it and every tracked title's coverage
updates to what it would be at that level, with how far off that is at your pace —
and a **kanji grid**: all ~2,100 WaniKani kanji laid out by level, coloured either
by SRS stage in WaniKani's own colours, or by how much *not* knowing each one is
costing you across the titles you track. Click any tile for its reading, meaning,
stage and occurrence count. Both work offline in the saved file, since the data is
embedded rather than fetched.

`serve` adds a **search box for the whole jiten.moe catalogue**. Type a title and
press Enter (it does not search as you type), filter by type, sort by your own
coverage, then hit *when?* on any title and it works out — right there — the kanji
coverage you have now, what finishing your current level adds, and which level and
roughly which month each threshold lands on. Same numbers as `wkjiten when`, no
terminal needed.

Each result also has **reading** and **plan** buttons that set the status on your
Jiten account, which is what makes the next run pick the title up. Find something,
mark it, done — no config file, no second tab.

`serve` saves the static `report.html` on the way up, so you keep a dashboard
after stopping the server.

**Why a server at all?** api.jiten.moe sends no CORS headers, so a page opened
from `file://` is not allowed by the browser to read its responses — live search
from a plain HTML file is impossible, not merely awkward. A small local server
in front of the API solves that, and it keeps your API key on the machine rather
than baking it into a file you might share. It binds to `127.0.0.1` only, and
proxies nothing except paths beginning with `/api/`.

Coverage history is appended to `cache/history.csv`, one row per title per day.

---

Arbitrary text, like wanilog's read-check:

```bash
python wkjiten.py text chapter1.txt
```

---

## Useful flags

```
--mode srs|level     srs = SRS stage counts (default), level = everything up to a level
--min-stage N        5=Guru I (default), 6=Guru II, 7=Master, 8=Enlightened, 9=Burned
--level N            level cutoff when using --mode level
--refresh            re-fetch WaniKani data
--top N              how many unknown kanji to list (default 25)
--sleep S            pause between decks in batch (default 6s)
--top-n N            titles per media type in status (default 5)
--soon-levels N      how many levels ahead status projects (default 5)
--soon-limit N       candidate titles status analyses (default 6, 0=off)
--status LIST        Jiten lists to track (default ongoing,planning; "" to disable)
--max-stage N        highest SRS stage still counted as a leech (default 4)
--alert-at N         batch: shout when a title crosses this coverage (default 80)
--flat N             parts: spread below this counts as uniform (default 5pp)
--sample N           edge: titles sampled per media type (default 100)
--target N           gap: stop once the list reaches this % coverage
--comfortable N      when: kanji coverage you consider comfortable (default 95)
--type / --genre     search: filter by media type and genre
--sort / --ascending search: ordering, e.g. --sort coverage
--no-open            report: do not launch a browser
--no-recommend       report: skip the recommendation sections (faster)
```

Flags work both before and after the subcommand.

---

## How it works

**WaniKani** ([docs](https://docs.api.wanikani.com/)) — `GET /v2/subjects?types=kanji,vocabulary,kana_vocabulary`
for characters, levels, readings and meanings, `GET /v2/assignments?started=true`
for your SRS stage per subject, and `GET /v2/level_progressions` for how fast you
actually move. Bearer token, 60 requests/min.

**Jiten** ([guide](https://jiten.moe/guides/using-the-api), [swagger](https://api.jiten.moe/swagger/v1/swagger.json)) —
`GET /api/media-deck/{id}/detail` for title data, and
`POST /api/media-deck/{id}/download` with `format: 4` (TxtRepeated) for the whole
word list, where each word is repeated as many times as it occurs in the work.
That is one request per deck instead of paging `/vocabulary` 200 at a time, and
it gives you the occurrence weighting for free.
`POST /api/user/vocabulary/import-from-anki-txt` takes the txt file, and
`get-media-decks?sortBy=coverage` ranks the library by your own coverage, and
`get-media-decks?status=ongoing` returns your own watching/reading list.
300 requests/min, 10/min on the heavy endpoints — hence `--sleep`.

Deck word lists are cached in `cache/decks/`, keyed on the deck's own
`lastUpdate` stamp, so repeat runs cost nothing until Jiten re-parses a title.
Delete the folder to force a refetch.

## Caveats

* **Vocabulary coverage: trust Jiten's own number.** With an API key, `deck` and
  `batch` read `coverage` straight from your account. Without one they fall back
  to a local estimate that matches on exact spelling and therefore reads far too
  low — across 13 titles it gave 14–19% where Jiten itself said 61–71%. Jiten
  also counts redundant writings of a word you know and the word sets you have
  blacklisted (names, places), and a local string match cannot see any of that.
* The kanji figures are local and unaffected by this. Word forms from Jiten are
  JMdict's headword rather than the spelling the work actually uses, but for
  counting kanji that difference is small.
* WaniKani writes counters and affixes with a tilde (〜人, 〜ヶ月). The tool adds
  the bare form as well, or they would never match against JMdict.
* `push` writes to your Jiten account. Run `export` and look at the file first.
