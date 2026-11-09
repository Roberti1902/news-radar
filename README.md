# News Radar

Lokales Python-Tool: zieht alle 30 (oder 60) Minuten marktrelevante Nachrichten aus offiziellen Quellen und
Finanzpresse, dedupliziert sie, ordnet sie Tickern zu, bewertet sie mit Claude und gibt **nur** relevante
positive/negative Signale für Bestand, Watchlist und Themenuniversum in der Konsole aus. Alles, auch Verworfenes,
landet mit Grund in SQLite und JSONL.

Das Tool **handelt nie**, gibt **keine Positionsgrößen** aus und loggt sich nirgends ein.

## Setup

```powershell
cd news-radar
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# hinter TLS-inspizierendem Proxy/Virenscanner (Fehler CERTIFICATE_VERIFY_FAILED):
.venv\Scripts\python -m pip install --use-feature=truststore -r requirements.txt
```

Laufzeit-HTTP nutzt den Windows-Zertifikatsspeicher (`ssl.create_default_context()`), funktioniert also auch dort.

## Eigene Listen anlegen

`config/universe.yaml` (Bestand, Watchlist) und `config/exposure.yaml` sind privat und nicht im Repo. Vorlagen kopieren und anpassen:

```powershell
copy config\universe.example.yaml config\universe.yaml
copy config\exposure.example.yaml config\exposure.yaml
```

Danach läuft `python -m news_radar --dry-run` auch ohne Netz und ohne Keys.

## Umgebungsvariablen

Vorlage: `.env.example` → nach `news-radar/.env` kopieren und ausfüllen (steht in `.gitignore`). Beim Start wird `.env` geladen;
bereits gesetzte Umgebungsvariablen haben Vorrang.

| Variable | Pflicht | Zweck |
|---|---|---|
| `ANTHROPIC_API_KEY` | nein | nur bei `llm.provider: anthropic` (kostenpflichtig, Tagesdeckel `MAX_DAILY_LLM_USD`). Default ist `ollama`: lokal, kostenlos, kein Key |
| `FINNHUB_API_KEY` | empfohlen | kostenloser Key; nur für Kurse von US-Aktien (ohne Key: Kurs `n/v`) |
| `SEC_USER_AGENT` | nein | nur nötig, wenn die SEC-Quellen wieder aktiviert werden; wird **nur** an sec.gov gesendet |
| `MAX_DAILY_LLM_USD` | nein | Tagesbudget, Default 2 |
| `OPENFIGI_API_KEY` | nein | höheres OpenFIGI-Limit |
| `MARKETAUX_API_KEY` | nein | optionaler Adapter |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | nein | optionaler Adapter |

```powershell
$env:ANTHROPIC_API_KEY = "..."; $env:FINNHUB_API_KEY = "..."; $env:SEC_USER_AGENT = "Max Muster max@example.com"
```

## Bedienung

```
python -m news_radar                 # Dauerbetrieb, Intervall aus config (Default 30 min)
python -m news_radar --once          # ein Zyklus
python -m news_radar --interval 60
python -m news_radar --dry-run       # Fixtures statt Netz, gemockte LLM-Antworten (eigene DB data/dry_run.db)
python -m news_radar --backfill 24h  # Zeitraum statt "seit letztem erfolgreichen Lauf"
python -m news_radar --evaluate      # Outcomes nach 1/5/20 Handelstagen + Trefferquote je event_type/Richtung
python -m news_radar --config DIR    # anderes Config-Verzeichnis
python -m pytest -q                  # Tests
```

Ein Fehler in einem Zyklus beendet den Dauerbetrieb nicht (Traceback in `logs/news_radar.log`).

## Config-Dateien (`config/`)

| Datei | Inhalt |
|---|---|
| `config.yaml` | Intervall, Schwellen (confidence/magnitude, CANDIDATE strenger, max. 5/Tag), Dedup/Novelty-Fenster, Modelle, Budget, Preise, HTTP-Limits pro Host, Makro-Themen, Quellenliste |
| `universe.yaml` | Bestand, Watchlist, CANDIDATE-Firmen, Themen-Schlüsselwörter; ISINs am 2026-11-03 gegen die Xetra-Liste geprüft |
| `exposure.yaml` | Pro Bestands-/Watchlist-Ticker: Themen, Länder, Rohstoffe, Kunden/Zulieferer/Wettbewerber, Makro-Sensitivität |
| `whitelist.yaml` | Ticker/ISINs → Kaufbarkeit **bestätigt** (überschreibt alles) |
| `blacklist.yaml` | Ticker/ISINs → **ausgeschlossen** (überschreibt alles) |

## Pipeline pro Zyklus

1. **Fetch** seit letztem erfolgreichem Lauf (UTC in SQLite `meta.last_success`); fällt eine Quelle aus → Log + `source_runs`, Rest läuft weiter.
2. **Normalize** auf `title, summary, url, source, via, tier, published_at, tickers_raw`.
3. **Dedup**: URL-Hash (ohne Query/Fragment) + Titel-Ähnlichkeit (rapidfuzz `token_set_ratio ≥ 88`, Längenverhältnis ≥ 0,5). Ein Cluster = ein Event, weitere Artikel erhöhen `source_count` (48 h).
4. **Novelty** (72 h): Ein bereits bewertetes Event wird nur neu bewertet bei neuen Zahlen, neuen Guidance-/Bestätigungs-Wörtern oder erstmals einer Tier-1-Quelle. Ältere Meldungen werden verworfen.
5. **Entity-Mapping**: Ticker aus der Quelle, Namens-/Alias-Abgleich, Gegenparteien/Rohstoffe/Makro über `exposure.yaml`, zusätzlich LLM-Extraktion in der Triage.
6. **Vorfilter ohne LLM**: Listicles, Promo, wiederholte Kursziele, außerhalb des Universums ohne Thementreffer.
7. **Triage** (`llm.triage_model`, 10 Events pro Call): relevance, is_material, event_type, tickers. `< 0,5` → verworfen.
8. **Impact** (`llm.impact_model`): pro Ticker JSON, mit pydantic validiert (u. a. `reason` ≤ 25 Wörter und in der Form „Ursache → Wirkung“, `facts_used` nicht leer). Ungültige Objekte werden verworfen und protokolliert.
9. **Scalable-Gate** (siehe unten). „unbekannt“ → nie ausgegeben, nur `logs/unverified_tradability.jsonl`.
10. **Preis** in Listing-Währung + EUR über EZB-Referenzkurs mit dem richtigen Paar (EUR/USD, EUR/JPY, EUR/GBP inkl. Pence …), Tagesänderung, Verzögerung aus dem Quote-Zeitstempel.
11. **Ausgabe** nur bei direction ≠ neutral, confidence ≥ 0,6, magnitude ≥ medium (CANDIDATE: ≥ 0,75, high, max. 5/Tag).

**LLM-Backend** (`llm.provider` in `config.yaml`):
- `ollama` (Default, 0 €): lokales Modell über `http://localhost:11434/api/chat` mit JSON-Schema-Ausgabe. Ollama muss laufen
  (Tray-App oder `ollama serve`), das Modell muss mit `ollama pull <name>` geladen sein. Default `gemma3:12b` (8 GB, passt in
  12 GB VRAM, 6–30 s pro Aufruf gemessen am 2026-11-04 auf RTX 3060); `gemma4:31b` lief dort halb auf der CPU mit 4–10 min pro Aufruf. Triage und Impact nutzen dasselbe Modell.
  Kein Budget nötig, Kosten werden nicht gezählt. Passt das Modell nicht komplett in den Grafikspeicher, wird es sehr langsam.
- `anthropic`: Claude-API wie unten beschrieben, Modelle auf `claude-haiku-5-5` / `claude-sonnet-5-5` zurückstellen.

Prompt Caching (nur `anthropic`): System-Prompt + Universum/Exposure-Map stehen als stabiles, sortiertes Präfix mit `cache_control`.
Für Sonnet 5.5 ist `fallbacks: "default"` aktiv (Beta `server-side-fallback-2026-07-01`): Lehnt das Modell eine Anfrage ab,
antwortet serverseitig ein passendes anderes Modell. Abschalten: `llm.server_fallbacks: false`.

**Budget**: Kosten pro Call aus `usage` (inkl. Cache-Write/Read) mit den Preisen in `config.yaml`, vorab grob geschätzt.
Ab 100 % des Tagesbudgets werden nur noch Events mit HOLDING-Bezug bewertet, ab 120 % pausiert das LLM (Konsolenmeldung).

## Speicher

- `data/news_radar.db`: `events` (Status `signal | evaluated | discarded | pending_confirmation | deferred | error` + `reason`),
  `articles`, `signals` (`emitted=1` = ausgegeben, sonst `reason`), `signal_outcomes`, `price_history`, `llm_costs`, `source_runs`, `cache`
- `logs/events.jsonl`: jedes Event mit Endstatus/Grund · `logs/unverified_tradability.jsonl` · `logs/news_radar.log`

## Quellen

### Aktiv: kostenlose RSS-Feeds (verifiziert am 2026-11-04)

Nur RSS, kein HTML-Scraping, kein Key. Abruf alle 30 min (10 Requests pro Zyklus). Alle Feeds zählen als Tier 2.

| Seite | Feed | Einträge/Abruf | Hinweis |
|---|---|---|---|
| BBC | `feeds.bbci.co.uk/news/business/rss.xml`, `…/news/world/rss.xml` | 44 / 25 | Titel + kurzer Teaser |
| Investing.com | `investing.com/rss/news_25.rss` (Aktien), `news_14.rss` (Wirtschaft), `news_11.rss` (Rohstoffe) | je 10 | nur Titel, kein Teaser; Zeitstempel ohne Zone (= UTC, live geprüft) |
| CNBC Asia | `cnbc.com/id/19832390/device/rss/rss.html` | 30 | Titel + Teaser |
| The Guardian | `theguardian.com/business/rss`, `…/business/stock-markets/rss`, `…/world/rss` | 40 / 20 / 45 | lange Teaser |
| FXStreet | `fxstreet.com/rss/news` | 30 | Titel + Teaser |

**Limits:** Keine der Seiten nennt ein Abruf-Limit für RSS. Das Tool fragt jeden Feed einmal pro Zyklus ab.
**Nutzungsbedingungen:** Keine der Seiten erlaubt automatisiertes Abrufen ausdrücklich, keine verbietet RSS ausdrücklich. Der
Footer von Investing.com untersagt, Daten der Website ohne schriftliche Erlaubnis zu „speichern“ oder „wiederzugeben“; das Tool
speichert Titel und Link lokal in SQLite. Nur privat nutzen, nichts weiterverbreiten.

Für die Kurszeile bleibt Finnhub `/quote` (kostenloser Key, nur US-Aktien), für EUR die EZB-Referenzkurse, für die
Kaufbarkeit OpenFIGI + die Xetra-Liste (beide ohne Key).

### Abgeschaltet (in `config.yaml` mit `enabled: false`, verifiziert am 2026-11-03)

Ein Eintrag wird wieder aktiv, wenn du `enabled: false` auf `enabled: true` setzt.


Geprüft wurde jeweils die offizielle Doku (soweit vorhanden) **und** ein Live-Request.

| Tier | Quelle | Endpunkt | Limit | Verifiziert |
|---|---|---|---|---|
| 1 | SEC EDGAR | `browse-edgar?action=getcompany…&output=atom` je Firma, `action=getcurrent&type=8-K/6-K/10-Q` | **10 req/s**, User-Agent mit Kontakt Pflicht | Doku sec.gov/os/accessing-edgar-data + Live 200 |
| 1 | SEC Ticker→CIK | `sec.gov/files/company_tickers.json` (7 Tage gecacht) | wie oben | Live 200 |
| 1 | Fed | `federalreserve.gov/feeds/press_monetary.xml` | kein Limit dokumentiert (Tool: 2 req/s) | Live 200 |
| 1 | EZB | `ecb.europa.eu/rss/press.html` | kein Limit dokumentiert | Live 200 |
| 1 | BoJ | `boj.or.jp/en/rss/whatsnew.xml` | kein Limit dokumentiert | Live 200 |
| 1 | USTR | `ustr.gov/rss.xml` | kein Limit dokumentiert | Live 200 |
| 1 | EU-Kommission | `ec.europa.eu/commission/presscorner/api/rss?language=en` | kein Limit dokumentiert | Live 200 |
| 1 | BIS (Export Controls) | Federal-Register-API `documents.json?conditions[agencies][]=industry-and-security-bureau` | kein Key; Limit nicht einsehbar (Doku-Seite blockt Bots) → Tool 1 req/s | Live 200 |
| 2 | Finnhub company-news | `/api/v1/company-news` | Free **60/min** + **30/s** global; nur nordamerikanische Firmen | Doku + Pricing-Seite |
| 2 | Finnhub news | `/api/v1/news?category=general,merger` | wie oben | Doku |
| 2 | Finnhub quote | `/api/v1/quote` | wie oben; **nur US-Aktien** im Free-Tier | Doku |
| 2 | Yahoo Finance RSS | `feeds.finance.yahoo.com/rss/2.0/headline?s=…` (Nicht-US immer, US nur ohne Finnhub-Key) | kein Limit dokumentiert | Live 200 |
| 2 | PR Newswire | `prnewswire.com/rss/news-releases-list.rss` | kein Limit dokumentiert | Live 200 |
| 2 | Marketaux (optional) | `api.marketaux.com/v1/news/all` | Free **100 Requests/Tag, 3 Artikel/Request** | Doku + Pricing |
| 2 | Alpaca (optional) | `data.alpaca.markets/v1beta1/news` | im Header `X-RateLimit-Limit`; Tool 1 req/s | Doku |
| 3 | GDELT DOC 2.0 | `api.gdeltproject.org/api/v2/doc/doc` (mode=artlist, format=json) | **1 Request / 5 s** (Server-Meldung) | Doku-Blog + Live |
| – | EZB-Referenzkurse | `ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` (6 h gecacht) | – | Live 200 |
| – | OpenFIGI | `POST api.openfigi.com/v3/mapping` | ohne Key **25/min, 10 Jobs**; mit Key 250/min, 100 Jobs | Doku |
| – | Xetra-Instrumente | CSV-Link wird bei jedem Tageswechsel von der offiziellen Downloadseite `cashmarket.deutsche-boerse.com/…/Tradable-Instruments-Xetra/Downloads` gelesen | – | Live 200, 5.397 ISINs |

**Weggelassen** (nicht kostenlos/öffentlich oder nicht verifizierbar):
- **Polygon** heißt jetzt Massive (`api.massive.com`); Free-Limit und Auth-Parameter auf der News-Doku nicht angegeben.
- **Reuters, Bloomberg, Dow Jones**: keine öffentlichen RSS/APIs; Inhalte kommen indirekt über Finnhub/Yahoo.
- **GlobeNewswire RSS**: 403 für automatisierte Abrufe. **Business Wire RSS**: Feed liefert keine Einträge.
- **bis.gov RSS**: liefert HTML statt Feed → ersetzt durch die Federal-Register-API (offizielle BIS-Regeln).
- **Kurse für Nicht-US-Titel** (z. B. Prysmian): Finnhub Free liefert nur US-Kurse; Stooq-CSV existiert nicht mehr;
  Alpha-Vantage-Unterstützung für Mailand ist nicht dokumentiert. → Kurszeile zeigt `n/v (keine freie Kursquelle)`,
  bis ein `quote_symbol` mit passendem Plan gesetzt wird.

## Scalable-Kaufbarkeit (Näherung)

Scalable hat keine öffentliche Instrumentenliste und keine API; das Tool loggt sich nie ein.
1. `blacklist.yaml` → **ausgeschlossen**, `whitelist.yaml` → **bestätigt** (Ticker oder ISIN).
2. ISIN: aus `universe.yaml`, sonst **OpenFIGI → Firmenname → Fuzzy-Abgleich gegen die Xetra-Instrumentnamen** (Score ≥ 92 und eindeutig).
   Hinweis: Die OpenFIGI-Antwort enthält laut Doku **keine ISIN**, deshalb der Umweg über den Namen. Ergebnis 30 Tage gecacht.
3. ISIN in `t7-xetr-allTradableInstruments.csv` → **wahrscheinlich kaufbar**, sonst **unbekannt** (nie Signal).

## Bekannte Grenzen

- **Scalable ≠ Xetra.** Scalable handelt über eigene Venues (u. a. gettex); auf Xetra gelistet heißt nicht sicher bei Scalable kaufbar
  und umgekehrt. Beispiel: **TLN** steht nicht in der Xetra-Liste → „unbekannt“, also keine Signale, bis TLN in die Whitelist kommt.
  Der Namensabgleich für Firmen ohne ISIN im Universum kann bei ähnlichen Namen falschliegen.
- **Kursverzögerung**: Die Verzögerung wird aus dem Zeitstempel des Quotes berechnet; außerhalb der Handelszeit steht „Stand TT.MM. HH:MM“.
  Finnhub Free liefert nur US-Kurse. Prysmian bekommt deshalb keinen Kurs.
- **Die RSS-Feeds liefern nur die jeweils neuesten Einträge.** Investing.com zeigt nur 10 pro Feed; erscheinen dort mehr als
  10 Meldungen in 30 Minuten, fehlen die ältesten. Tickerbezug entsteht nur über Namen, Gegenparteien und Makro-Themen,
  da diese Feeds keine Ticker mitliefern.
- **GDELT** (abgeschaltet) drosselt pro IP; an geteilten IPs (Firmen-Proxy/VPN) kommen auch bei 5 s Abstand 429er. Dann wird GDELT für den Zyklus übersprungen
  und Tier-3-Events bleiben aus.
- **SEC-Filings** liefern nur Metadaten (Formular, Items), nicht den Volltext. Die eigentliche Meldung kommt meist über Tier-2-Quellen.
- **--evaluate** nutzt Schlusskurse, die das Tool selbst sammelt (Finnhub Free hat keine Kurshistorie). Ohne täglichen Lauf verschiebt sich
  die Zählung der Handelstage.
- Events mit Status `deferred` (Budget) oder `error` werden nicht automatisch nachbewertet.
- Federal-Register-Einträge haben nur ein Datum (00:00 UTC) und gelten für den ganzen Tag; Duplikate fängt der URL-Hash ab.
- Entity-Mapping per Schlüsselwort ist bewusst simpel; generische Begriffe sind in `exposure.yaml` ausgespart, um Rauschen zu begrenzen.

## TODO (bewusst nicht gebaut)

- Web-UI / Dashboard für Signale und Trefferquoten
- Push-Benachrichtigungen (Telegram, E-Mail)
- Nachbewertung von `deferred`-Events am nächsten Budget-Tag
- Kurshistorie über eine API für exakte 1/5/20-Tage-Outcomes; Kursquelle für Nicht-US-Titel
- Volltext von 8-K/6-K-Exhibits (EX-99.1) für die Impact-Bewertung

---

*Quantitative Auswertung öffentlicher Daten, keine Anlageberatung.*
