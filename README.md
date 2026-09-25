# OBX Frontier

Markowitz-optimalisert portefølje av likvide aksjer på Oslo Børs, målt mot OSEBX.
Alt kjører gratis på GitHub: GitHub Actions henter kurser og regner ut porteføljen hver
børsdag, og GitHub Pages viser resultatet.

```
Yahoo Finance ─┐
SSB (NIBOR) ───┼─▶ Python i GitHub Actions ─▶ site/data/resultat.json ─▶ GitHub Pages
               │        │
               │        └─▶ state/live.json (committes ved hver rebalansering)
```

## Oppsett, steg for steg

Du trenger bare en GitHub-konto og nettleseren. Ingen installasjon på egen maskin.

### 1. Lag repoet
1. Gå til <https://github.com/new>.
2. Repository name: `obx-frontier` (navnet blir en del av adressen).
3. Velg **Public**. Ikke huk av for README, .gitignore eller lisens.
4. Trykk **Create repository**.

### 2. Last opp filene (uten workflow-filen)
1. Pakk ut zip-filen på maskinen din.
2. I det tomme repoet: trykk lenken **uploading an existing file**.
3. Dra inn alt innholdet i mappen `obx-frontier`: `pipeline/`, `site/`, `state/`,
   `config.yaml`, `kandidater.csv`, `requirements.txt`, `README.md`.
   Last opp mappene som mapper, slik at strukturen beholdes.
4. Trykk **Commit changes**.

Mappen `.github` og filen `.gitignore` er skjult på Mac og ofte på Windows.
Dem legger du inn i steg 4 og 5.

### 3. Slå på GitHub Pages og skrivetilgang
1. **Settings → Pages**. Under *Build and deployment*, sett **Source** til **GitHub Actions**.
2. **Settings → Actions → General**. Under *Workflow permissions*, velg
   **Read and write permissions** og trykk **Save**.

### 4. Legg inn .gitignore
1. **Add file → Create new file**. Filnavn: `.gitignore`
2. Lim inn innholdet fra `.gitignore` i zip-filen (åpne den i en teksteditor), og trykk **Commit changes**.

### 5. Legg inn workflow-filen (starter første kjøring)
1. **Add file → Create new file**.
2. Skriv filnavnet `.github/workflows/oppdater.yml`. Mappene lages automatisk når du skriver `/`.
3. Lim inn hele innholdet fra `.github/workflows/oppdater.yml` i zip-filen.
4. Trykk **Commit changes**. Dette starter første kjøring.

### 6. Følg med på første kjøring
1. Åpne fanen **Actions**. Du ser «Oppdater portefølje» kjøre.
2. Første kjøring tar 3–6 minutter (henter 15 år med kurser og antall aksjer).
3. Grønn hake betyr ferdig. Adressen står under jobben **publiser**, og er
   `https://<brukernavn>.github.io/obx-frontier/`.
4. I **Code**-fanen ser du nå en ny commit fra `github-actions[bot]` med `state/live.json`.
   Det er starten på live-sporingen.

### 7. Sjekk kandidatlisten
Åpne siden og se under **Univers og data → Mangler data fra Yahoo**. Står det
selskaper der, er tickeren feil eller selskapet avnotert. Rett eller slett linjen i
`kandidater.csv` (blyantikonet på GitHub), og commit. Workflowen kjører da på nytt.

Ferdig. Siden oppdateres hver børsdag rundt kl. 19 (18 om vinteren), og porteføljen
rebalanseres første handelsdag i hver måned.

## Hva som skjer automatisk

| Når | Hva |
|---|---|
| Hver børsdag 17:00 UTC | Nye kurser, nye estimater, ny effisient front, oppdatert backtest og live-avkastning |
| Første kjøring i en ny måned | Ny portefølje for begge metoder, lagret i `state/live.json` |
| Når du pusher en endring | Samme som daglig kjøring. Live-porteføljen byttes likevel først neste måned |

Feiler en kjøring (for eksempel fordi Yahoo er nede), blir forrige versjon av siden
stående, og GitHub sender deg en e-post. Kjøringen dagen etter tar igjen det som manglet.

## Vedlikehold

**Når OSEBX endres (1. juni og 1. desember):** Oppdater `kandidater.csv`. Euronext
publiserer sammensetningen på live.euronext.com under indeksen OSEBX. Nye selskaper
trenger tre års kurshistorikk før de kan velges.

**Endre regler:** Alt ligger i `config.yaml`, med forklaring på hver linje. Eksempler:
- Større portefølje: `portefoljestorrelse_nok`. Da blir likviditetsgrensen per aksje strengere.
- Strengere likviditet: `min_median_omsetning_mnok`.
- Bytte hovedmetode på siden: `hovedmetode: bayes_stein`.

**Starte live-sporingen på nytt:** Slett `state/live.json` og commit.

**Kjøre manuelt:** Workflowen har ingen egen knapp. Gjør en liten endring i
`config.yaml` eller `kandidater.csv` og commit, eller trykk **Re-run all jobs** på en
tidligere kjøring i Actions-fanen.

**Planlagte kjøringer stopper:** GitHub slår av planlagte workflows i offentlige repoer
etter 60 dager uten aktivitet. Den månedlige committen fra boten holder repoet aktivt,
men står det et gult varsel i Actions-fanen, trykk **Enable workflow**.

## Feilsøking

| Symptom | Løsning |
|---|---|
| Kjøringen feiler i «Hent data» med `Too Many Requests` eller `RateLimit` | Yahoo begrenser trafikk. Neste kjøring prøver igjen. Skjer det ofte, øk versjonen av yfinance i `requirements.txt` |
| Feil i «Lagre live-logg» med `403` | Steg 3.2 er ikke gjort: sett Workflow permissions til Read and write |
| Feil i «publiser» | Steg 3.1 er ikke gjort: Pages-kilden må være GitHub Actions |
| Siden viser «Fant ingen resultater» | Første kjøring er ikke ferdig, eller den feilet. Se Actions-fanen |
| Risikofri rente står som «fast rente fra config.yaml» | SSB svarte ikke. Siden bruker `fast_rente` til SSB svarer igjen |

## Lokal testing (valgfritt)

```bash
pip install -r requirements.txt
python -m pipeline.run --demo      # syntetiske data, ingen nett
python -m pipeline.run             # ekte data
cd site && python -m http.server   # åpne http://localhost:8000
```

`--demo` skriver live-loggen til `build/`, så den ekte `state/` blir ikke rørt.

## Metode

**Univers.** Alle aksjer i `kandidater.csv` (dagens OSEBX). En aksje er med når median
daglig omsetning de siste 63 handelsdagene er minst 5 MNOK, den har handlet minst 90 % av
dagene og den har kurs i minst 95 % av de siste 156 ukene. I tillegg begrenses hver
posisjon slik at den kan selges på én dag med høyst 20 % av dagsomsetningen.

**Kovarians.** Ukentlige, utbyttejusterte avkastninger over tre år, krympet med
Ledoit–Wolf.

**Forventet avkastning, to metoder som begge beregnes:**
- *Black–Litterman:* likevektsavkastning π = δΣw fra markedsvektene i universet
  (antall aksjer fra Yahoo × kurs), justert mot historiske snitt med usikkerhet lik
  standardfeilen til snittet. τ = 0,05, δ = 2,5.
- *Bayes–Stein (Jorion 1986):* historiske snitt krympet mot avkastningen til
  minimum-varians-porteføljen.

**Optimering.** Maks Sharpe-ratio med risikofri rente lik 3 mnd. NIBOR (SSB, tabell
10701), løst som et konvekst kvadratisk problem med cvxpy/Clarabel. Grenser: kun long,
maks 20 % per selskap, maks 40 % per sektor, maks 30 % omsetning per måned (én vei).
Grensen på 5–15 selskaper og minstevekten på 2 % håndteres ved å løse, beholde de
største posisjonene og løse på nytt. Kan ikke alle grenser oppfylles samtidig, lempes
omsetningsgrensen først, og det vises på siden.

**Tidslogikk.** Beslutning etter børsslutt første handelsdag i måneden, med data til og
med den dagen. Handel til sluttkurs neste handelsdag. 0,20 % transaksjonskostnad av
handlet beløp. Backtest og live bruker nøyaktig samme kode.

**Kjente svakheter.**
- *Overlevelsesskjevhet:* backtesten bruker dagens OSEBX-selskaper. Selskaper som gikk
  konkurs eller ble kjøpt opp før i dag er ikke med, så backtesten ser bedre ut enn den
  ville gjort i virkeligheten. Live-sporingen har ikke denne feilen.
- *Markedsvekter i backtesten* bruker dagens antall aksjer, ikke historisk.
- *Yahoo Finance* er gratis, men uoffisiell og har av og til hull eller feil i kursene.
  Åpenbare engangsfeil fjernes automatisk.

Dette er et modellresultat og ikke investeringsråd.

## Filer

```
config.yaml              alle innstillinger
kandidater.csv           ticker, navn, sektor
requirements.txt         Python-pakker
pipeline/
  run.py                 hovedskriptet
  data.py                Yahoo, SSB, rensing, demodata
  model.py               univers, Ledoit–Wolf, Bayes–Stein, Black–Litterman
  optimizer.py           maks Sharpe, antallsgrense, effisient front
  engine.py              beslutning, simulering, backtest, nøkkeltall
  live.py                live-logg
site/                    nettsiden (HTML, CSS, JS). data/ lages av workflowen
state/                   live-logg og mellomlager, skrives av workflowen
.github/workflows/       GitHub Actions
```
