"""--evaluate: Kursentwicklung nach 1/5/20 Handelstagen für ausgegebene Signale + Trefferquote."""
from datetime import datetime

from rich.table import Table

HORIZONS = (1, 5, 20)


def fill_outcomes(db) -> int:
    """Handelstag n = n-tes Datum mit gespeichertem Schlusskurs nach dem Signaltag.

    ponytail: Schlusskurse stammen aus den Quotes, die das Tool selbst sammelt (Finnhub Free hat keine
    Kurshistorie). Läuft das Tool an einem Handelstag nicht, verschiebt sich die Zählung – für exakte
    Werte eine Kurshistorien-API anbinden.
    """
    n = 0
    for s in db.q("select * from signals where emitted=1 and price is not null"):
        day = datetime.fromisoformat(s["created_at"]).date().isoformat()
        closes = db.q("select date, close from price_history where ticker=? and date>? order by date", s["ticker"], day)
        for h in HORIZONS:
            if len(closes) < h or db.q("select 1 from signal_outcomes where signal_id=? and horizon_days=?", s["id"], h):
                continue
            p = closes[h - 1]["close"]
            ret = (p / s["price"] - 1) * 100
            hit = ret > 0 if s["direction"] == "positive" else ret < 0
            db.x("insert into signal_outcomes values(?,?,?,?,?)", s["id"], h, p, ret, int(hit))
            n += 1
    return n


def hit_table(db) -> Table:
    t = Table(title="Trefferquote je event_type und Richtung")
    for col in ("event_type", "Richtung", "Signale", *[f"{h} T" for h in HORIZONS]):
        t.add_column(col)
    rows = db.q("select event_type, direction, count(*) n from signals where emitted=1 group by 1, 2 order by 1, 2")
    for r in rows:
        cells = []
        for h in HORIZONS:
            o = db.q("select count(*) n, avg(hit) hr, avg(return_pct) ret from signal_outcomes o join signals s "
                     "on s.id=o.signal_id where s.event_type=? and s.direction=? and o.horizon_days=?",
                     r["event_type"], r["direction"], h)[0]
            cells.append(f"{o['hr']:.0%} (n={o['n']}, Ø {o['ret']:+.1f} %)" if o["n"] else "–")
        t.add_row(r["event_type"] or "?", r["direction"], str(r["n"]), *cells)
    return t


def run(ctx) -> None:
    for (ticker,) in ctx.db.q("select distinct ticker from signals where emitted=1"):
        ctx.market.quote(ticker)  # speichert aktuellen Kurs in price_history
    n = fill_outcomes(ctx.db)
    ctx.console.print(f"{n} neue Outcomes nachgetragen.")
    ctx.console.print(hit_table(ctx.db))
