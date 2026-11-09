"""Konsolenausgabe (rich) im festen Format."""
from datetime import datetime

from rich.markup import escape

DIR = {"positive": ("▲", "POSITIV", "green"), "negative": ("▼", "NEGATIV", "red")}
MAG = {"low": "gering", "medium": "mittel", "high": "hoch"}
HOR = {"days": "Tage", "weeks": "Wochen", "months": "Monate"}
ORD = {"first": "Erstwirkung", "second": "Zweitwirkung"}
TYPE = {"earnings": "EARNINGS", "guidance": "GUIDANCE", "M&A": "M&A", "regulatory": "REGULIERUNG", "macro": "MAKRO",
        "geopolitical": "GEOPOLITIK", "analyst": "ANALYST", "product": "PRODUKT", "legal": "RECHT",
        "capital_action": "KAPITALMASSNAHME"}
CAT_ORDER = {"HOLDING": 0, "WATCHLIST": 1, "CANDIDATE": 2}
DISCLAIMER = "Quantitative Auswertung öffentlicher Daten, keine Anlageberatung."


def num(v: float, digits: int = 2) -> str:
    s = f"{abs(v):,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if v < 0 else "") + s


def pct(v: float | None) -> str:
    if v is None:
        return "n/v"
    return ("+" if v > 0 else "−" if v < 0 else "±") + num(abs(v), 1) + " %"


def stamp(ctx) -> str:
    now = datetime.now(ctx.tz)
    return f"[{now:%Y-%m-%d %H:%M} {'MESZ' if now.dst() else 'MEZ'}]"


def price_text(ctx, p: dict | None, short: bool = False) -> str:
    if not p:
        return "n/v (keine freie Kursquelle)"
    d = p["delay_min"]
    age = f"{d} min verzögert" if d <= 60 else f"Stand {datetime.fromtimestamp(p['ts'], ctx.tz):%d.%m. %H:%M}"
    tail = f"{pct(p['change_pct'])} heute" if short else f"{pct(p['change_pct'])} heute · {age}"
    if p["currency"] == "EUR":
        return f"{num(p['price'])} EUR ({tail})"
    eur = f"{num(p['eur'])} EUR" if p["eur"] is not None else "EUR n/v"
    fx = f"{p['pair']} {num(p['rate'], 3)} · " if p["pair"] else ""
    return f"{num(p['price'])} {p['currency']} / {eur} ({fx}{tail})"


def sources_text(ev: dict) -> str:
    names = [s for s in ev["sources"] if s]
    n = ev["source_count"]
    return f"[{', '.join(names[:3])} · {n} {'Quelle' if n == 1 else 'Quellen'}]"


def render(ctx, ev: dict, signals: list[dict]) -> None:
    c = ctx.console
    lbl = lambda s: f"[bold]{s:<9}[/bold]"
    if len(signals) == 1:
        s = signals[0]
        imp = s["impact"]
        arrow, word, col = DIR[imp.direction]
        c.print(f"{stamp(ctx)} [{col} bold]{arrow} {word}[/] · {s['category']} · [bold]{escape(s['ticker'])}[/]  "
                f"{escape(s['name'])}")
        c.print(lbl("Kurs") + escape(price_text(ctx, s["price"])))
        c.print(lbl("Event") + f"{escape(ev['summary_de'])}   {escape(sources_text(ev))}")
        c.print(lbl("Warum") + escape(imp.reason))
        c.print(lbl("Stärke") + f"{MAG[imp.magnitude]} · Horizont {HOR[imp.horizon]} · Confidence "
                                f"{imp.confidence:.2f} · {ORD[imp.order]}")
        c.print(lbl("Link") + escape(ev["url"]), soft_wrap=True)
    else:
        c.print(f"{stamp(ctx)} [bold]◆ {TYPE.get(ev['event_type'], ev['event_type'])}[/] · {len(signals)} Ticker betroffen")
        c.print(lbl("Event") + f"{escape(ev['summary_de'])}   {escape(sources_text(ev))}")
        for s in sorted(signals, key=lambda s: CAT_ORDER[s["category"]]):
            imp = s["impact"]
            arrow, word, col = DIR[imp.direction]
            c.print(f"  [{col} bold]{arrow} {word}[/] · {s['category']} · [bold]{escape(s['ticker'])}[/]  "
                    f"{escape(price_text(ctx, s['price'], short=True))}  Warum: {escape(imp.reason)} "
                    f"[dim]({MAG[imp.magnitude]} · {HOR[imp.horizon]} · {imp.confidence:.2f} · {ORD[imp.order]})[/]")
        c.print(lbl("Link") + escape(ev["url"]), soft_wrap=True)
    c.print()


def warn(ctx, msg: str) -> None:
    ctx.console.print(f"{stamp(ctx)} [yellow bold]⚠ {escape(msg)}[/]")


def cycle_summary(ctx, stats, signals: list) -> None:
    failed = f" · Ausfall: {', '.join(ctx.failed_sources)}" if ctx.failed_sources else ""
    ctx.console.print(f"[dim]{stamp(ctx)} Zyklus fertig: {stats['items']} Items · {stats['dup_url']} URL-Duplikate · "
                      f"{stats['clustered']} geclustert · {stats['discarded']} verworfen · {len(signals)} Signale · "
                      f"LLM heute {ctx.budget.spent():.3f} USD{escape(failed)}[/]")
    ctx.console.print(f"[dim]{DISCLAIMER}[/]")
