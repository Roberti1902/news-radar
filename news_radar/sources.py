"""Quellen-Adapter. Gemeinsames Interface: adapter(ctx, src, since) -> list[Item].

Nur APIs und RSS/Atom, kein HTML-Scraping. Rate-Limits/Retry/Timeout kommen aus net.Http.
"""
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import feedparser

from .models import Item

log = logging.getLogger("news_radar")
UTC = timezone.utc
TAG = re.compile(r"<[^>]+>")


def clean(text: str, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", text or ""))).strip()[:limit]


def _feed_items(content: bytes, src: dict, tickers: list[str] | None = None) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"kein gültiger Feed: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts or not e.get("link"):
            continue
        out.append(Item(title=clean(e.get("title", ""), 300), summary=clean(e.get("summary", "")), url=e["link"],
                        source=src.get("publisher", src["name"]), via=src["name"], tier=src["tier"],
                        published_at=datetime(*ts[:6], tzinfo=UTC), tickers_raw=tickers or src.get("tickers", [])))
    return out


def rss(ctx, src, since):
    return _feed_items(ctx.http.get(src["url"]).content, src)


# --- SEC EDGAR -------------------------------------------------------------------------------------------

def _sec_headers() -> dict:
    ua = os.getenv("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("SEC_USER_AGENT nicht gesetzt (SEC verlangt 'Name kontakt@domain')")
    return {"User-Agent": ua}


def _cik_map(ctx) -> dict[str, str]:
    """ticker -> CIK (10-stellig), wöchentlich aus company_tickers.json gecacht."""
    m = ctx.db.cache_get("sec_cik_map", max_age_s=7 * 86400)
    if m is None:
        data = ctx.http.get("https://www.sec.gov/files/company_tickers.json", headers=_sec_headers()).json()
        m = {r["ticker"]: f'{r["cik_str"]:010d}' for r in data.values()}
        ctx.db.cache_set("sec_cik_map", m)
    return m


def _sec_entries(ctx, src, url, forms, cik_to_ticker, fixed_ticker=None):
    out = []
    for e in feedparser.parse(ctx.http.get(url, headers=_sec_headers()).content).entries:
        form = (e.get("tags") or [{}])[0].get("term", "")
        if form.split("/")[0] not in forms:
            continue
        ticker = fixed_ticker
        if ticker is None:
            m = re.search(r"\((\d{10})\)", e.get("title", ""))
            ticker = cik_to_ticker.get(m.group(1)) if m else None
        ts = e.get("updated_parsed")
        title = clean(e.get("title", ""), 300)
        out.append(Item(title=f"{ticker}: {title}" if ticker and fixed_ticker else title,
                        summary=clean(e.get("summary", "")), url=e["link"], source="SEC EDGAR", via=src["name"],
                        tier=1, published_at=datetime(*ts[:6], tzinfo=UTC), tickers_raw=[ticker] if ticker else []))
    return out


def sec_company(ctx, src, since):
    """Atom-Feed pro Firma (US-Bestand + Watchlist)."""
    ciks = _cik_map(ctx)
    out = []
    for ticker, c in ctx.cfg["companies"].items():
        if c["category"] == "CANDIDATE" or ticker not in ciks:
            continue
        url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=" + ciks[ticker] +
               "&type=&dateb=&owner=include&count=20&output=atom")
        out += _sec_entries(ctx, src, url, src["forms"], {}, fixed_ticker=ticker)
    return out


def sec_current(ctx, src, since):
    """Globale 'latest filings' je Formular – findet Kandidaten im Themenuniversum."""
    rev = {v: k for k, v in _cik_map(ctx).items()}
    out = []
    for form in src["forms"]:
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count=100&output=atom"
        out += _sec_entries(ctx, src, url, src["forms"], rev)
    return out


# --- Finnhub / Yahoo / optionale Keys --------------------------------------------------------------------

def _finnhub(ctx, path, **params):
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError("FINNHUB_API_KEY nicht gesetzt")
    return ctx.http.get(f"https://finnhub.io/api/v1/{path}", params=params, headers={"X-Finnhub-Token": key}).json()


def _finnhub_items(rows, src, extra_tickers=()):
    return [Item(title=clean(r.get("headline", ""), 300), summary=clean(r.get("summary", "")), url=r["url"],
                 source=r.get("source") or "Finnhub", via=src["name"], tier=src["tier"],
                 published_at=datetime.fromtimestamp(r["datetime"], UTC),
                 tickers_raw=[*extra_tickers, *[t for t in (r.get("related") or "").split(",") if t]])
            for r in rows if r.get("url") and r.get("datetime")]


def finnhub_company_tickers(ctx) -> list[str]:
    """Finnhub company-news gibt es im Free-Tier nur für nordamerikanische Firmen."""
    return [t for t, c in ctx.cfg["companies"].items() if c["category"] != "CANDIDATE" and "." not in t]


def finnhub_company(ctx, src, since):
    frm, to = since.date().isoformat(), datetime.now(UTC).date().isoformat()
    out = []
    for t in finnhub_company_tickers(ctx):
        out += _finnhub_items(_finnhub(ctx, "company-news", symbol=t, **{"from": frm, "to": to}), src, [t])
    return out


def finnhub_general(ctx, src, since):
    return [i for cat in src.get("categories", ["general"]) for i in _finnhub_items(_finnhub(ctx, "news", category=cat), src)]


def yahoo(ctx, src, since):
    """Yahoo-RSS pro Ticker als Fallback: Nicht-US-Titel immer, US-Titel nur ohne Finnhub-Key."""
    covered = set(finnhub_company_tickers(ctx)) if os.getenv("FINNHUB_API_KEY") else set()
    out = []
    for t, c in ctx.cfg["companies"].items():
        if c["category"] == "CANDIDATE" or t in covered:
            continue
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={c.get('yahoo', t)}&region=US&lang=en-US"
        out += _feed_items(ctx.http.get(url).content, src, [t])
    return out


def marketaux(ctx, src, since):
    key = os.getenv("MARKETAUX_API_KEY")
    if not key:
        return []
    r = ctx.http.get("https://api.marketaux.com/v1/news/all", params={
        "symbols": ",".join(finnhub_company_tickers(ctx)), "filter_entities": "true", "language": "en",
        "published_after": since.strftime("%Y-%m-%dT%H:%M:%S"), "api_token": key}).json()
    return [Item(title=clean(a["title"], 300), summary=clean(a.get("description", "")), url=a["url"],
                 source=a.get("source") or "Marketaux", via=src["name"], tier=src["tier"],
                 published_at=datetime.fromisoformat(a["published_at"].replace("Z", "+00:00")),
                 tickers_raw=[e["symbol"] for e in a.get("entities", [])]) for a in r.get("data", [])]


def alpaca(ctx, src, since):
    kid, secret = os.getenv("ALPACA_API_KEY_ID"), os.getenv("ALPACA_API_SECRET_KEY")
    if not (kid and secret):
        return []
    r = ctx.http.get("https://data.alpaca.markets/v1beta1/news", params={
        "symbols": ",".join(finnhub_company_tickers(ctx)), "start": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 50, "sort": "desc"}, headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": secret}).json()
    return [Item(title=clean(a["headline"], 300), summary=clean(a.get("summary", "")), url=a["url"],
                 source=a.get("source") or "Alpaca", via=src["name"], tier=src["tier"],
                 published_at=datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")),
                 tickers_raw=a.get("symbols", [])) for a in r.get("news", []) if a.get("url")]


# --- Offizielle APIs ohne RSS -----------------------------------------------------------------------------

def federal_register(ctx, src, since):
    """BIS-Regeln (Export Controls) über die Federal-Register-API (bis.gov bietet keinen RSS-Feed)."""
    params = [("conditions[agencies][]", src["agency"]), ("conditions[publication_date][gte]", since.date().isoformat()),
              ("order", "newest"), ("per_page", "20")] + [("fields[]", f) for f in
                                                          ("title", "html_url", "publication_date", "abstract")]
    rows = ctx.http.get("https://www.federalregister.gov/api/v1/documents.json", params=params).json().get("results", [])
    return [Item(title=clean(r["title"], 300), summary=clean(r.get("abstract") or ""), url=r["html_url"],
                 source=src.get("publisher", "Federal Register"), via=src["name"], tier=src["tier"],
                 published_at=datetime.fromisoformat(r["publication_date"]).replace(tzinfo=UTC)) for r in rows]


def gdelt(ctx, src, since):
    """GDELT DOC 2.0, Themen-Queries. Limit: 1 Request / 5 s (vom Server erzwungen)."""
    start = max(since, datetime.now(UTC) - timedelta(days=89)).strftime("%Y%m%d%H%M%S")
    out, errors = [], []
    for q in src["queries"]:
        try:
            r = ctx.http.get("https://api.gdeltproject.org/api/v2/doc/doc", params={
                "query": f"{q} sourcelang:english", "mode": "artlist", "format": "json", "maxrecords": 50,
                "sort": "datedesc", "startdatetime": start})
            if not r.text.lstrip().startswith("{"):
                raise RuntimeError(r.text[:120])
        except Exception as e:  # noqa: BLE001 – GDELT drosselt per IP; nach dem ersten Fehlschlag Zyklus-Pause
            errors.append(f"{q}: {e}")
            break
        for a in r.json().get("articles", []):
            out.append(Item(title=clean(a.get("title", ""), 300), url=a["url"], source=a.get("domain", "GDELT"),
                            via=src["name"], tier=3,
                            published_at=datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)))
    if errors and not out:
        raise RuntimeError("GDELT: " + errors[0][:200])
    for e in errors:
        log.warning("GDELT-Query fehlgeschlagen, Rest übersprungen: %s", e[:200])
    return out


ADAPTERS = {f.__name__: f for f in (rss, sec_company, sec_current, finnhub_company, finnhub_general, yahoo,
                                    marketaux, alpaca, federal_register, gdelt)}


def fetch_all(ctx, since: datetime) -> list[Item]:
    """Alle aktiven Quellen; fällt eine aus, wird geloggt und mit den übrigen weitergemacht."""
    items = []
    for src in ctx.cfg["sources"]:
        if not src.get("enabled", True):
            continue
        try:
            got = ADAPTERS[src["type"]](ctx, src, since)
            ctx.db.x("insert into source_runs values(?,?,?,?,?)", datetime.now(UTC).isoformat(), src["name"], 1, len(got), None)
            items += got
        except Exception as e:  # noqa: BLE001 – eine Quelle darf den Zyklus nie beenden
            log.warning("Quelle %s ausgefallen: %s", src["name"], e)
            ctx.db.x("insert into source_runs values(?,?,?,?,?)", datetime.now(UTC).isoformat(), src["name"], 0, 0, str(e)[:500])
            ctx.failed_sources.append(src["name"])
    return items
