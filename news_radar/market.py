"""Kurse, EZB-Referenzkurse, Ticker→ISIN und Xetra-Kaufbarkeit (Scalable-Näherung)."""
import csv
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from rapidfuzz import fuzz

log = logging.getLogger("news_radar")
UTC = timezone.utc
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
XETRA_PAGE = "https://www.cashmarket.deutsche-boerse.com/cash-en/trading/Tradable-Instruments-Xetra/Downloads"
# Pence-Notierungen: Preis /100, dann GBP-Paar.
SUB_UNITS = {"GBX": ("GBP", 100), "GBp": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}

CONFIRMED, PROBABLE, EXCLUDED, UNKNOWN = "bestätigt", "wahrscheinlich kaufbar", "ausgeschlossen", "unbekannt"


def to_eur(price: float, currency: str, rates: dict[str, float]) -> tuple[float, str | None, float | None]:
    """Listing-Preis -> EUR über das passende EZB-Paar EUR/<Währung>. Gibt (eur, paar, kurs) zurück."""
    if currency == "EUR":
        return price, None, None
    base, div = SUB_UNITS.get(currency, (currency, 1))
    rate = rates[base]  # KeyError gewollt: ohne Referenzkurs keine Umrechnung raten
    return price / div / rate, f"EUR/{base}", rate


class Market:
    def __init__(self, ctx, fixtures: dict | None = None):
        self.ctx, self.fix = ctx, fixtures

    # --- FX ---
    def rates(self) -> dict[str, float]:
        if self.fix:
            return self.fix["fx"]
        r = self.ctx.db.cache_get("ecb_rates", max_age_s=6 * 3600)
        if r is None:
            root = ET.fromstring(self.ctx.http.get(ECB_URL).content)
            cubes = [c for c in root.iter() if c.get("currency")]
            day = next(c.get("time") for c in root.iter() if c.get("time"))
            r = {"_date": day, **{c.get("currency"): float(c.get("rate")) for c in cubes}}
            self.ctx.db.cache_set("ecb_rates", r)
        return r

    # --- Kurs ---
    def quote(self, ticker: str) -> dict | None:
        """{price, currency, change_pct, ts} oder None. Finnhub Free liefert nur US-Kurse."""
        c = self.ctx.cfg["companies"].get(ticker, {"currency": "USD"})
        if self.fix:
            q = self.fix["quotes"].get(ticker)
            return q and {**q, "currency": c["currency"],
                          "ts": datetime.now(UTC).timestamp() - q["minutes_ago"] * 60}
        if not os.getenv("FINNHUB_API_KEY"):
            return None
        try:
            from .sources import _finnhub
            q = _finnhub(self.ctx, "quote", symbol=c.get("quote_symbol", ticker))
        except Exception as e:  # noqa: BLE001 – z. B. 403 bei Nicht-US-Symbolen im Free-Tier
            log.info("Kein Kurs für %s: %s", ticker, e)
            return None
        if not q or not q.get("c"):
            return None
        self.ctx.db.x("insert or replace into price_history values(?,?,?)", ticker,
                      datetime.fromtimestamp(q["t"], UTC).date().isoformat(), q["c"])
        return {"price": q["c"], "change_pct": q.get("dp"), "currency": c["currency"], "ts": q["t"]}

    def priced(self, ticker: str) -> dict | None:
        q = self.quote(ticker)
        if not q:
            return None
        try:
            eur, pair, rate = to_eur(q["price"], q["currency"], self.rates())
        except Exception as e:  # noqa: BLE001
            log.warning("FX für %s fehlgeschlagen: %s", ticker, e)
            eur, pair, rate = None, None, None
        return {**q, "eur": eur, "pair": pair, "rate": rate,
                "delay_min": max(0, round((datetime.now(UTC).timestamp() - q["ts"]) / 60))}

    # --- Kaufbarkeit ---
    def xetra_isins(self) -> dict[str, str]:
        """ISIN -> Instrumentname aus t7-xetr-allTradableInstruments.csv, täglich gecacht."""
        if self.fix:
            return self.fix["xetra"]
        today = datetime.now(UTC).date().isoformat()
        cached = self.ctx.db.cache_get("xetra_isins")
        if cached and cached["date"] == today:
            return cached["isins"]
        page = self.ctx.http.get(XETRA_PAGE).text
        m = re.search(r'href="([^"]*t7-xetr-allTradableInstruments\.csv)"', page)
        if not m:
            raise RuntimeError("Link auf t7-xetr-allTradableInstruments.csv nicht auf der Downloadseite gefunden")
        url = m.group(1) if m.group(1).startswith("http") else "https://www.cashmarket.deutsche-boerse.com" + m.group(1)
        text = self.ctx.http.get(url).content.decode("utf-8-sig", errors="replace")
        rows = csv.reader(io.StringIO(text), delimiter=";")
        header = next(r for r in rows if "ISIN" in r)
        i_isin, i_name = header.index("ISIN"), header.index("Instrument")
        isins = {r[i_isin]: r[i_name] for r in rows if len(r) > i_isin and r[i_isin]}
        self.ctx.db.cache_set("xetra_isins", {"date": today, "isins": isins, "url": url})
        return isins

    def isin(self, ticker: str) -> str | None:
        """Universe-ISIN, sonst OpenFIGI (Name) + Namensabgleich gegen die Xetra-Liste.

        OpenFIGI liefert laut Doku keine ISIN zurück, nur FIGI/Name. Daher: OpenFIGI-Name
        fuzzy gegen Xetra-Instrumentnamen. ponytail: Namensabgleich kann bei ähnlichen Namen
        danebenliegen – deshalb nur 'wahrscheinlich'; sicher wird es erst per whitelist.yaml.
        """
        c = self.ctx.cfg["companies"].get(ticker, {})
        if c.get("isin"):
            return c["isin"]
        key = f"isin:{ticker}"
        hit = self.ctx.db.cache_get(key, max_age_s=30 * 86400)
        if hit is not None:
            return hit or None
        name = self._figi_name(ticker, c)
        best = None
        if name:
            scored = sorted(((fuzz.token_set_ratio(_norm(name), _norm(n)), i) for i, n in self.xetra_isins().items()),
                            reverse=True)[:2]
            if scored and scored[0][0] >= 92 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                best = scored[0][1]
        self.ctx.db.cache_set(key, best or "")
        return best

    def _figi_name(self, ticker: str, c: dict) -> str | None:
        if self.fix:
            return self.fix.get("figi", {}).get(ticker)
        sym, exch = (c.get("figi_ticker", ticker.split(".")[0]), c.get("figi_exch", "US"))
        headers = {"X-OPENFIGI-APIKEY": os.environ["OPENFIGI_API_KEY"]} if os.getenv("OPENFIGI_API_KEY") else {}
        data = self.ctx.http.post("https://api.openfigi.com/v3/mapping", headers=headers,
                                  json=[{"idType": "TICKER", "idValue": sym, "exchCode": exch}]).json()
        rows = [r for r in (data[0].get("data") or []) if r.get("marketSector") == "Equity"]
        return rows[0]["name"] if rows else None

    def tradability(self, ticker: str) -> tuple[str, str | None]:
        wl, bl = self.ctx.cfg["whitelist"], self.ctx.cfg["blacklist"]
        isin = None
        if ticker in (bl.get("tickers") or []):
            return EXCLUDED, None
        if ticker in (wl.get("tickers") or []):
            return CONFIRMED, None
        try:
            isin = self.isin(ticker)
            if isin in (bl.get("isins") or []):
                return EXCLUDED, isin
            if isin in (wl.get("isins") or []):
                return CONFIRMED, isin
            return (PROBABLE if isin and isin in self.xetra_isins() else UNKNOWN), isin
        except Exception as e:  # noqa: BLE001 – im Zweifel unbekannt, nie raten
            log.warning("Kaufbarkeit %s nicht prüfbar: %s", ticker, e)
            return UNKNOWN, isin


def _norm(name: str) -> str:
    """Rechtsform-/Nennwert-Rauschen entfernen: 'AMAZON.COM INC.  DL-,01' -> 'amazon com'."""
    n = re.sub(r"\b(DL|EO|SF|LS|YN|CD|AD|HD)\b.*$", "", name.upper())
    n = re.sub(r"\b(INC|CORP|CORPORATION|HLDGS?|HOLDINGS?|PLC|SE|AG|NV|SPA|S\.P\.A|LTD|CO|CL|A|REGISTERED|SHS)\b\.?", " ", n)
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", n).lower().split())
