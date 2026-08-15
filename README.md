# wkjiten — WaniKani-coverage på jiten.moe-decks

Jiten regner coverage ud fra *dine* kendte ord, men kan kun importere fra Anki,
JPDB, frekvensbånd eller sin egen backup. Der er ingen WaniKani-knap. Det her
værktøj bygger broen — på to måder, fordi WaniKani måler to forskellige ting:

| | Hvad du får | Hvor |
|---|---|---|
| `export` + `push` | Dine WaniKani-ord bliver "kendte ord" på Jiten, så coverage-kolonnen, filtrene og sorteringen på **jiten.moe virker for dig** | på hjemmesiden |
| `deck` / `batch` | **Kanji**-coverage pr. deck — det wanilog/read-check måler — plus hvilket WaniKani-level du skal på for at ramme 95 % / 98 % | i terminalen |

Vokabular-tallet bliver lavt (WaniKani lærer dig ~6.500 ord), kanji-tallet bliver
højt. Begge dele er sandheden, bare fra hver sin vinkel.

Kun Python 3.9+ og standardbiblioteket. Ingen pip install.

---

## Den nemme vej: dobbeltklik

Når nøglerne ligger på plads (se nedenfor), behøver du aldrig røre terminalen:

* **Windows** — `Opdater coverage.bat`
* **macOS** — `Opdater coverage.command`

Den henter dine WaniKani-data forfra, sender ordlisten til jiten.moe, printer
coverage på alle titlerne i `decks.txt` og slutter med et statusoverblik:

* **hvor mange nye kanji og ord** du har lært siden sidste kørsel, og hvilke
* **top 5 titler pr. medietype** — romaner, visual novels, anime, manga, spil —
  sorteret efter din faktiske coverage
* **hvad der snart er inden for rækkevidde**: titler lige under listen, med
  kanji-coverage nu vs. om fem levels, sorteret efter hvor meget de rykker

Kør den når du er steget et level.

På macOS skal filen gøres kørbar første gang: åbn Terminal, skriv `chmod +x `
(med mellemrum til sidst), træk filen ind i vinduet og tryk retur.

`decks.txt` er din liste over titler — ét deck-id pr. linje, alt efter `#`
ignoreres. Find id'er med `python wkjiten.py search "titel"` og redigér frit.

---

## Opsætning

**WaniKani-token** (read-only er nok) — <https://www.wanikani.com/settings/personal_access_tokens>

```bash
setx WANIKANI_TOKEN "din-token-her"
```

eller læg den i `wanikani_token.txt` ved siden af scriptet.

**Jiten API-key** (kun nødvendig til `push`) — jiten.moe → Settings → Advanced →
API Key. Den vises **én gang**. Gem den i `jiten_key.txt` eller som `JITEN_API_KEY`.

Første kørsel henter ~9.000 subjects + dine assignments fra WaniKani og cacher
dem i `cache/wanikani.json`. Kør med `--refresh` når du er steget i level.

---

## 1) Få WaniKani-coverage vist på selve jiten.moe

```bash
python wkjiten.py export
```

Skriver `wanikani-known-words.txt` med ét ord pr. linje — præcis det format
Jitens importer forventer ("alt før første tab eller komma").

Upload den:

```bash
python wkjiten.py push
```

…eller manuelt på jiten.moe → Settings → Vocabulary → import fra fil, hvis din
API-key viser sig at være read-only (`push` siger klart til, hvis den er).

Derefter viser deck-listen på jiten.moe din rigtige coverage, og du kan sortere
og filtrere på `coverageMin` osv. som alle andre.

**Hvad tæller som kendt?** Default er SRS-stage ≥ 5 (Guru I — WaniKanis egen
"passed"). Skru på det:

```bash
python wkjiten.py export --min-stage 9          # kun Burned
python wkjiten.py export --mode level --level 30 # alt til og med level 30
```

To ting der er værd at slå til bagefter, inde på Jiten:

* **Composition inference** (Settings → Vocabulary) udleder sammensatte ord fra
  dem du kender. WaniKani-ord er netop byggeklodser, så det løfter tallet mærkbart.
* **Word sets** → "Particles & Common Grammar". WaniKani lærer dig ikke partikler,
  og de er de hyppigste ord i alt japansk.

---

## 2) Kanji-coverage pr. deck (wanilog-vinklen)

Find deck-id'et:

```bash
python wkjiten.py search "yotsuba"
```

```
      id  type              chars  diff  title
   96859  manga            167600  0.00  Yotsuba&!
```

Og kør rapporten:

```bash
python wkjiten.py deck 96859
```

Du får kanji-coverage målt både på forekomster og på unikke tegn, det samme for
vokabular, en kurve over hvad hvert WaniKani-level ville give dig på lige præcis
den titel, hvilket level der rammer 90/95/98/99 %, og listen over de hyppigste
kanji du mangler (med det level de ligger på).

"Hard ceiling" er andelen af kanji i værket som WaniKani **aldrig** lærer dig —
navne, sjældne tegn. Selv på level 60 kommer du ikke over det loft.

Flere decks til en CSV, som du kan sortere i Excel:

```bash
python wkjiten.py batch 96859 118624 --out coverage.csv
python wkjiten.py batch --search "one piece" --limit 10
```

---

## 3) Status: fremgang og anbefalinger

```bash
python wkjiten.py status
```

Fremgang siden sidst måles mod et snapshot, der gemmes automatisk hver gang du
kører med `--refresh`. Første kørsel har intet at sammenligne med.

Anbefalingerne kommer fra Jiten selv: med API-key kan `get-media-decks` sortere
på `coverage`, altså *din* coverage, server-side. Det er én request pr.
medietype i stedet for at hente tusindvis af decks ned og regne lokalt.

"Snart inden for rækkevidde" er derimod lokalt regnet — kanji-kurven for hver
kandidat, nu vs. om fem levels. Det koster én request pr. titel, så den er
sat til 6 kandidater som standard:

```bash
python wkjiten.py status --soon-limit 15 --soon-levels 10
python wkjiten.py status --soon-limit 0     # spring projektionen helt over
```

---

Vilkårlig tekst, som wanilogs read-check:

```bash
python wkjiten.py text kapitel1.txt
```

---

## Nyttige flag

```
--mode srs|level     srs = SRS-stage tæller (default), level = alt op til et level
--min-stage N        5=Guru I (default), 6=Guru II, 7=Master, 8=Enlightened, 9=Burned
--level N            level-grænse når --mode level
--refresh            hent WaniKani-data forfra
--top N              hvor mange ukendte kanji der listes (default 25)
--sleep S            pause mellem decks i batch (default 6s)
--top-n N            titler pr. medietype i status (default 5)
--soon-levels N      hvor mange levels frem status projicerer (default 5)
--soon-limit N       antal kandidat-titler status analyserer (default 6, 0=fra)
```

Flagene virker både før og efter underkommandoen.

---

## Hvordan det virker

**WaniKani** ([docs](https://docs.api.wanikani.com/)) — `GET /v2/subjects?types=kanji,vocabulary,kana_vocabulary`
for tegn og level, `GET /v2/assignments?started=true` for din SRS-stage pr. subject.
Bearer-token, 60 requests/min.

**Jiten** ([guide](https://jiten.moe/guides/using-the-api), [swagger](https://api.jiten.moe/swagger/v1/swagger.json)) —
`GET /api/media-deck/{id}/detail` for titeldata, og
`POST /api/media-deck/{id}/download` med `format: 4` (TxtRepeated) for hele
ordlisten, hvor hvert ord er gentaget lige så mange gange som det optræder i
værket. Det er én request pr. deck i stedet for at side gennem `/vocabulary`
200 ad gangen, og det giver vægtningen gratis. `POST /api/user/vocabulary/import-from-anki-txt`
tager txt-filen. 300 requests/min, 10/min på de tunge — derfor `--sleep`.

## Forbehold

* **Vokabular-coverage: brug Jitens eget tal.** Med en API-key henter `deck` og
  `batch` `coverage` direkte fra din konto. Uden key falder de tilbage på et
  lokalt estimat, der matcher på nøjagtig skrivemåde og derfor rammer markant
  for lavt — målt på 13 titler gav det 14-19 %, hvor Jiten selv sagde 61-71 %.
  Jiten tæller nemlig også redundante skrivemåder af et ord du kender, og de
  word sets du har blacklistet (navne, stednavne), og det kan et lokalt
  streng-match ikke se.
* Kanji-tallene er lokale og upåvirkede af det. Ordformerne fra Jiten er
  JMdicts hovedform og ikke nødvendigvis den skrivemåde værket faktisk bruger,
  men for kanji-optælling er den forskel lille.
* WaniKani skriver tællere og affikser med tilde (〜人, 〜ヶ月). Værktøjet
  tilføjer den bare form, ellers ville de aldrig matche mod JMdict.
* `push` skriver til din Jiten-konto. Kør `export` og kig filen igennem først.
