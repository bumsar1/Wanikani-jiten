<img src="assets/logo.png" alt="" width="112" align="right">

# wkjiten — WaniKani coverage for jiten.moe

Jiten works out how much of a title you can read from *your* known words, but it
only imports them from Anki, JPDB or a frequency band. There is no WaniKani
button. This bridges the gap two ways, because the two sites measure different
things:

* **Word coverage** — your WaniKani vocabulary becomes "known words" on jiten.moe,
  so its coverage column, filters and sorting start working for you.
* **Kanji coverage** — computed here: how much of a title's kanji WaniKani has
  taught you, and which level you need for 95%.

The word figure looks modest (WaniKani teaches ~6,500 words) while the kanji
figure runs high. Both are true, from different angles.

Python 3.9+ and the standard library. Nothing to install.

---

## Getting started

Double-click the launcher for your system:

* **Windows** — `Update coverage (Windows).bat`
* **macOS** — `Update coverage (macOS).command`

The first run creates three key files next to the script, each with instructions
inside it:

```
wanikani_token.txt     waiting for your key
jiten_key.txt          waiting for your key
jimaku_key.txt         waiting for your key
```

Paste each key on its own line — comments starting with `#` are ignored, so the
notes can stay.

| file | where to get it | needed for |
|---|---|---|
| `wanikani_token.txt` | [wanikani.com/settings/personal_access_tokens](https://www.wanikani.com/settings/personal_access_tokens) — **read-only** is enough | everything |
| `jiten_key.txt` | [jiten.moe/settings](https://jiten.moe/settings) → API Key, shown once | your account's word coverage, uploading known words, the list buttons |
| `jimaku_key.txt` | [jimaku.cc/account](https://jimaku.cc/account) | the **subs** link beside anime |

Only the first is required. Then run the launcher again and it does the rest.

> On a Mac, clone the repository rather than downloading the zip — a browser zip
> arrives quarantined and macOS refuses to open the launcher. If you already have
> one, run `xattr -dr com.apple.quarantine .` in the project folder once.

---

## What the launcher does

Fetches your WaniKani data, uploads your known words to jiten.moe, prints
coverage for everything you are tracking, lists the leeches holding you back,
and opens a dashboard showing:

* how many kanji and words you have learned since last time, and which
* coverage per tracked title, its trend, and what finishing your current level
  would add on its own
* a curve of what every WaniKani level would give you, with a slider to try one
* the kanji grid — all ~2,100 of them by level, coloured by SRS stage or by how
  much not knowing each costs you
* leeches, ranked by how often they block the titles you actually read
* a search box for the whole jiten.moe catalogue

Leave the window open while you use the dashboard; Ctrl+C stops it. Run it again
whenever you gain a level.

**The title list is your own jiten.moe statuses.** Whatever you have marked
*watching/reading* or *plan to watch/read* is what gets tracked — set a status on
the site and it appears on the next run. `decks.txt` is merged in on top for
anything you want to follow without putting it on a list.

From the dashboard you can search the catalogue, add a title to any of your lists,
mark one finished, and download a title's Japanese subtitles as a zip with the
Chinese ones filtered out.

---

## Commands

Everything the dashboard does is also a command, if you prefer the terminal.

| command | what it does |
|---|---|
| `setup` | create the key files and show which are filled in |
| `export` | write your WaniKani words to a txt Jiten can import |
| `push` | upload that file to your Jiten account |
| `search "title"` | browse the catalogue by title, type and genre |
| `when 21948` | should you start this yet — and if not, which level and roughly when |
| `deck 96859` | full coverage report for one title |
| `batch` | every tracked title into `coverage.csv` |
| `parts 21948` | per-episode or per-volume breakdown of a series |
| `next` | the kanji worth learning next, priced in coverage on your titles |
| `leeches` | Apprentice items ranked by what they block |
| `status` | progress since last run, and the best titles for you per media type |
| `like 21948` | titles built on vocabulary a title you know already used |
| `edge` | titles easier for you than their difficulty rating suggests |
| `gap 21948 --target 95` | CSV of the words in a title you cannot read yet |
| `text chapter1.txt` | read-check any Japanese text you paste in |
| `serve` | the dashboard, with live catalogue search |
| `report` | the same dashboard as a single HTML file |

`when` is the one worth trying first:

```
黒子のバスケ                              anime | 97,477 chars

Right now, at level 12
  kanji coverage    64.44%
  word coverage     64.48%   (jiten.moe)
  finishing level 12 takes kanji to 70.22% (+5.78pp)

   kanji  level  levels to go         time     around
     80%     19             7    ~4 months   Dec 2026
     90%     30            18   ~12 months   Aug 2027
     95%     41            29   ~19 months   Mar 2028
```

Dates use the median of your last six levels, not a lifetime average — one break
in your history would drag a mean into the hundreds of days.

---

## Useful flags

```
--refresh            re-fetch WaniKani data (do this after levelling up)
--min-stage N        what counts as known: 5=Guru I (default) … 9=Burned
--mode level --level N   count everything up to a level instead of by SRS stage
--status LIST        which Jiten lists to track (default ongoing,planning)
--sleep S            pause between titles (default 6s; Jiten allows 10/min)
--top N              how many rows to list (default 25)
--comfortable N      when: the coverage you consider comfortable (default 95)
--alert-at N         batch: shout when a title crosses this coverage (default 80)
--no-open            report: write the file without opening a browser
```

Flags work before or after the subcommand. Each command has more — `--help` lists
them.

---

## Worth knowing

* **Kanji coverage is a floor, not a ceiling.** Knowing the characters is
  necessary and nowhere near sufficient; grammar and the words WaniKani never
  teaches decide the rest. The word figure from jiten.moe is the honest one, and
  it climbs by reading rather than by levelling.
* **The Jiten key is not read-only.** Their own docs say it carries every
  permission your account has, including rewriting known words and deleting
  cards. `push` writes to your account — run `export` and look at the file first.
* Without a Jiten key the word-coverage figures fall back to a local estimate
  that matches on exact spelling and reads far too low. The kanji figures are
  unaffected.
* Word lists are cached in `cache/`, so repeat runs are fast. Delete the folder
  to force a refetch.
* Your keys stay out of git. The repository ships `.example` templates and the
  launcher copies them into place, so pasting a key never becomes a change you
  could push by accident.

## Running it for more than one person

[`webapp/`](webapp/README.md) is the same analysis with accounts, so a friend can
use it with their own keys — plus a page comparing what you are both watching.
Its README covers hosting it.
