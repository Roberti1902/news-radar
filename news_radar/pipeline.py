"""Ein Zyklus: Fetch → Normalize → Dedup → Novelty → Entity-Mapping → Vorfilter → Triage → Impact → Gate → Preis → Ausgabe."""
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from . import output
from .llm import BudgetPaused
from .market import UNKNOWN, EXCLUDED
from .models import MAGNITUDE_RANK, Item
from .sources import fetch_all

log = logging.getLogger("news_radar")
UTC = timezone.utc
I = re.IGNORECASE

LISTICLE = re.compile(r"(^\s*\d+\s+(top\s+|best\s+|great\s+)?(\w+\s+)?(stocks?|shares|reasons|aktien)\b|\bstocks? to "
                      r"(buy|watch|own)\b|\b(better buy|should you buy|is it time to buy|millionaire[- ]maker|buy before"
                      r"|could make you|top \d+)\b)", I)
PROMO = re.compile(r"\b(sponsored|paid (post|promotion|advertisement)|advertorial|promoted content|anzeige)\b", I)
PT_REPEAT = re.compile(r"\b(reiterat\w*|maintain\w*|keeps?|reaffirm\w*|bestätigt)\b.{0,60}\b(price target|rating|kursziel)", I)
NEW_FACT_WORDS = re.compile(r"\b(guidance|outlook|forecast|raises?|lifts?|lowers?|cuts?|prognose|confirm\w*|approv\w*"
                            r"|official\w*|final rule|signed)\b", I)
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?:\s?%| ?(?:bn|billion|million|mrd|mio))?", I)
YEAR = re.compile(r"^(19|20)\d\d$")


def url_hash(url: str) -> str:
    u = re.sub(r"[?#].*$", "", url.strip().lower()).rstrip("/")
    return hashlib.sha1(u.encode()).hexdigest()


def norm_title(t: str) -> str:
    t = re.sub(r"\s[-–|]\s[^-–|]{2,40}$", "", t)  # " - Reuters" etc.
    return " ".join(re.sub(r"[^\w%]+", " ", t.lower()).split())


def similar(a: str, b: str, threshold: float) -> bool:
    ta, tb = a.split(), b.split()
    if not ta or not tb or min(len(ta), len(tb)) / max(len(ta), len(tb)) < 0.5:
        return False
    return fuzz.token_set_ratio(a, b) >= threshold


def numbers(text: str) -> set[str]:
    return {n.replace(" ", "").lower() for n in NUMBER.findall(text) if not YEAR.match(n)}


def new_facts(ev, item: Item) -> bool:
    """Neue Zahlen, Guidance-/Bestätigungswörter, die vorher fehlten, oder erstmals eine Tier-1-Quelle."""
    text = f"{item.title} {item.summary}"
    if numbers(text) - set(json.loads(ev["numbers"] or "[]")):
        return True
    old = f"{ev['title']} {ev['summary']}"
    if {w.lower() for w in NEW_FACT_WORDS.findall(text)} - {w.lower() for w in NEW_FACT_WORDS.findall(old)}:
        return True
    return item.tier == 1 and ev["tier_min"] > 1


def _kw_regex(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?!\w)", I)


class Matcher:
    """Entity-Mapping ohne LLM: Ticker aus Quelle, Namensabgleich, Themen, Makro über die Exposure-Map."""

    def __init__(self, cfg: dict):
        self.comps = cfg["companies"]
        self.alias = {t: _kw_regex(c.get("aliases") or [c["name"]]) for t, c in self.comps.items()}
        self.themes = {th: _kw_regex(kws) for th, kws in (cfg["universe"].get("themes") or {}).items()}
        self.macro = {m: _kw_regex(kws) for m, kws in cfg["macro_topics"].items()}
        self.exposure = cfg["exposure"]
        self.parties = {t: [(role, _kw_regex([n])) for role in ("customers", "suppliers", "competitors")
                            for n in (ex.get(role) or [])] for t, ex in self.exposure.items()}
        self.commodities = {t: _kw_regex(ex["commodities"]) for t, ex in self.exposure.items() if ex.get("commodities")}

    def map(self, text: str, tickers_raw: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
        """-> ({ticker: Zuordnung}, themes, macro_topics)"""
        themes = [th for th, rx in self.themes.items() if rx.search(text)]
        macros = [m for m, rx in self.macro.items() if rx.search(text)]
        out: dict[str, str] = {}
        for t in tickers_raw:
            if t in self.comps:
                out[t] = "direkt (Quelle)"
            elif themes and re.fullmatch(r"[A-Z]{1,5}", t):
                out[t] = f"Quelle, Thema {themes[0]}"  # Firma außerhalb der Listen, aber im Themenuniversum
        for t, rx in self.alias.items():
            if t not in out and rx.search(text):
                out[t] = "direkt (Name)"
        for t, ex in self.exposure.items():
            if t in out:
                continue
            party = next((f"{role[:-1]} {m.group(0)}" for role, rx in self.parties[t] if (m := rx.search(text))), None)
            macro = next((m for m in macros if m in (ex.get("macro_sensitivity") or [])), None)
            cm = self.commodities.get(t) and self.commodities[t].search(text)
            if party:
                out[t] = f"Exposure: {party}"
            elif macro:
                out[t] = f"Makro: {macro}"
            elif cm:
                out[t] = f"Rohstoff: {cm.group(0)}"
        return out, themes, macros


def category(cfg, ticker: str) -> str:
    return cfg["companies"].get(ticker, {}).get("category", "CANDIDATE")


def prefilter(title: str, mapped: dict, themes: list, macros: list) -> str | None:
    if LISTICLE.search(title):
        return "Vorfilter: Listicle"
    if PROMO.search(title):
        return "Vorfilter: Promo/Werbung"
    if PT_REPEAT.search(title):
        return "Vorfilter: wiederholtes Kursziel ohne neue Info"
    if not mapped:
        return "Vorfilter: außerhalb Universum ohne Thementreffer" if not (themes or macros) else \
            "Vorfilter: Thema/Makro ohne betroffenen Ticker"
    return None


def gate(cfg, cat: str, imp, candidates_today: int) -> str | None:
    th = cfg["thresholds"]
    if imp.direction == "neutral":
        return "Gate: neutral"
    min_conf, min_mag = (th["candidate_min_confidence"], th["candidate_min_magnitude"]) if cat == "CANDIDATE" else \
        (th["min_confidence"], th["min_magnitude"])
    if imp.confidence < min_conf:
        return f"Gate: confidence {imp.confidence:.2f} < {min_conf}"
    if MAGNITUDE_RANK[imp.magnitude] < MAGNITUDE_RANK[min_mag]:
        return f"Gate: magnitude {imp.magnitude} < {min_mag}"
    if cat == "CANDIDATE" and candidates_today >= th["candidate_max_per_day"]:
        return "Gate: CANDIDATE-Tageslimit erreicht"
    return None


# --------------------------------------------------------------------------------------------------------

def run_cycle(ctx, since: datetime | None = None) -> dict:
    cfg, db = ctx.cfg, ctx.db
    now = datetime.now(UTC)
    last = db.meta("last_success")
    since = since or (datetime.fromisoformat(last) if last else now - timedelta(hours=cfg["first_run_backfill_hours"]))
    ctx.failed_sources = []
    items = ctx.fixture_items() if ctx.dry_run else fetch_all(ctx, since)
    stats = Counter(items=len(items))
    touched: dict[int, str] = {}
    dedup_win, nov_win = timedelta(hours=cfg["dedup"]["window_hours"]), timedelta(hours=cfg["novelty"]["window_hours"])

    for it in sorted(items, key=lambda i: i.published_at):
        # Datumsgenaue Quellen (00:00 UTC) gelten für den ganzen Tag; Rest strikt ab 'since'.
        day_only = it.published_at.time() == datetime.min.time()
        if it.published_at < since and not (day_only and it.published_at.date() >= since.date()):
            stats["vor_since"] += 1
            continue
        h = url_hash(it.url)
        if db.q("select 1 from articles where url_hash=?", h):
            stats["dup_url"] += 1
            continue
        nt = norm_title(it.title)
        text = f"{it.title} {it.summary}"
        ev = next((e for e in db.q("select * from events where last_seen>=? order by id desc", (now - nov_win).isoformat())
                   if similar(nt, norm_title(e["title"]), cfg["dedup"]["title_similarity"])), None)
        if ev is None:
            eid = db.x("insert into events(title, summary, url, tier_min, source_count, sources, tickers, numbers, "
                       "first_seen, last_seen, published_at, status) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                       it.title, it.summary, it.url, it.tier, 1, json.dumps(sorted({it.source, it.via})),
                       json.dumps(it.tickers_raw), json.dumps(sorted(numbers(text))), now.isoformat(), now.isoformat(),
                       it.published_at.isoformat(), "new")
            touched[eid] = "new"
        else:
            eid = ev["id"]
            stats["clustered"] += 1
            assessed = ev["status"] in ("signal", "evaluated")
            fresh = new_facts(ev, it)
            in_dedup = datetime.fromisoformat(ev["first_seen"]) >= now - dedup_win
            db.x("update events set source_count=source_count+?, sources=?, tickers=?, numbers=?, tier_min=?, "
                 "last_seen=?, summary=? where id=?", 1 if in_dedup else 0,
                 json.dumps(sorted(set(json.loads(ev["sources"])) | {it.source, it.via})),
                 json.dumps(sorted(set(json.loads(ev["tickers"])) | set(it.tickers_raw))),
                 json.dumps(sorted(set(json.loads(ev["numbers"])) | numbers(text))), min(ev["tier_min"], it.tier),
                 now.isoformat(), (ev["summary"] + " | " + it.title + ": " + it.summary)[:4000], eid)
            if ev["status"] == "pending_confirmation" and it.tier <= 2:
                touched[eid] = "bestätigt"
            elif assessed and fresh:
                touched[eid] = "neue Fakten"
            elif assessed:
                stats["novelty_drop"] += 1
                db.jsonl("events.jsonl", {"ts": now, "event_id": eid, "status": "discarded", "url": it.url,
                                          "title": it.title, "reason": "Novelty: bereits gemeldet, keine neuen Fakten"})
        db.x("insert into articles values(?,?,?,?,?,?,?,?,?)", h, eid, it.url, it.title, it.source, it.via, it.tier,
             it.published_at.isoformat(), now.isoformat())

    signals = process_events(ctx, touched, now, stats)
    if len(ctx.failed_sources) < sum(src.get("enabled", True) for src in cfg["sources"]):
        db.meta("last_success", now.isoformat())
    output.cycle_summary(ctx, stats, signals)
    return {"stats": stats, "signals": signals}


def _finish(ctx, ev_id: int, status: str, reason: str | None, now, extra: dict | None = None) -> None:
    ctx.db.x("update events set status=?, reason=?, evaluated_at=? where id=?", status, reason, now.isoformat(), ev_id)
    row = dict(ctx.db.q("select * from events where id=?", ev_id)[0])
    ctx.db.jsonl("events.jsonl", {"ts": now, "event_id": ev_id, "status": status, "reason": reason,
                                  "title": row["title"], "url": row["url"], "tier": row["tier_min"],
                                  "source_count": row["source_count"], **(extra or {})})


def process_events(ctx, touched: dict[int, str], now, stats: Counter) -> list[dict]:
    cfg, db = ctx.cfg, ctx.db
    matcher = Matcher(cfg)
    nov_win = timedelta(hours=cfg["novelty"]["window_hours"])
    queue = []
    for eid, why in touched.items():
        ev = dict(db.q("select * from events where id=?", eid)[0])
        if datetime.fromisoformat(ev["published_at"]) < now - nov_win:
            _finish(ctx, eid, "discarded", "Novelty: alte/recycelte Meldung (>72 h)", now)
            stats["discarded"] += 1
            continue
        if ev["tier_min"] == 3:
            _finish(ctx, eid, "pending_confirmation", "Tier 3: wartet auf Bestätigung durch Tier-1/2-Quelle", now)
            stats["tier3_wartend"] += 1
            continue
        text = f"{ev['title']} {ev['summary']}"
        mapped, themes, macros = matcher.map(text, json.loads(ev["tickers"]))
        reason = prefilter(ev["title"], mapped, themes, macros)
        if reason:
            _finish(ctx, eid, "discarded", reason, now, {"themes": themes, "macro": macros})
            stats["discarded"] += 1
            continue
        queue.append({**ev, "mapped": mapped, "themes": themes, "macros": macros, "trigger": why})

    # ---- Triage, gebündelt ----
    state = ctx.budget.state()
    if state == "paused":
        output.warn(ctx, "LLM pausiert: Tagesbudget ≥ 120 % verbraucht – Events werden nur gespeichert.")
    elif state == "holdings_only":
        output.warn(ctx, "Tagesbudget erreicht – bewerte nur noch HOLDING-Events.")
    ready = []
    for ev in queue:
        has_holding = any(category(cfg, t) == "HOLDING" for t in ev["mapped"])
        if state == "paused" or (state == "holdings_only" and not has_holding):
            _finish(ctx, ev["id"], "deferred", f"Budget: {state}", now)
            stats["budget_skip"] += 1
        else:
            ready.append(ev)
    bs = cfg["llm"]["triage_batch_size"]
    triaged = []
    for i in range(0, len(ready), bs):
        batch = ready[i:i + bs]
        try:
            res, errors = ctx.llm.triage([{"id": e["id"], "url": e["url"], "title": e["title"],
                                           "summary": e["summary"][:600], "sources": json.loads(e["sources"]),
                                           "tier": e["tier_min"], "tickers_hint": sorted(e["mapped"])} for e in batch])
        except BudgetPaused:
            for e in batch:
                _finish(ctx, e["id"], "deferred", "Budget: paused", now)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("Triage fehlgeschlagen")
            for ev in batch:
                _finish(ctx, ev["id"], "error", f"Triage-Fehler: {e}"[:300], now)
            continue
        for ev in batch:
            t = res.get(ev["id"])
            if t is None:
                _finish(ctx, ev["id"], "error", "Triage: keine/ungültige Antwort " + "; ".join(errors)[:200], now)
                continue
            db.x("update events set relevance=?, is_material=?, event_type=? where id=?", t.relevance,
                 int(t.is_material), t.event_type, ev["id"])
            if t.relevance < cfg["thresholds"]["triage_min_relevance"]:
                _finish(ctx, ev["id"], "discarded", f"Triage: relevance {t.relevance:.2f} < "
                                                    f"{cfg['thresholds']['triage_min_relevance']}", now,
                        {"event_type": t.event_type})
                stats["discarded"] += 1
                continue
            llm_map, _, _ = matcher.map(" ".join(t.tickers), t.tickers)  # LLM-Extraktion: nur bekannte/thematische Ticker
            for tk, via in llm_map.items():
                ev["mapped"].setdefault(tk, "LLM-Extraktion")
            direct = any(v.startswith(("direkt", "Quelle", "LLM")) for v in ev["mapped"].values())
            if not direct and not t.is_material:
                _finish(ctx, ev["id"], "discarded", "Triage: nur indirekt betroffen und nicht wesentlich", now,
                        {"event_type": t.event_type})
                stats["discarded"] += 1
                continue
            triaged.append({**ev, "event_type": t.event_type})

    # ---- Impact + Gate ----
    emitted = []
    cand_today = db.q("select count(*) n from signals where emitted=1 and category='CANDIDATE' and created_at>=?",
                      datetime.now(ctx.tz).replace(hour=0, minute=0, second=0).isoformat())[0]["n"]
    order = {"HOLDING": 0, "WATCHLIST": 1, "CANDIDATE": 2}
    for ev in triaged:
        # Limit pro Event: direkte Treffer vor Gegenpartei vor Makro, dann Themen-Überlappung, dann Kategorie.
        def rank(t, via):
            overlap = len(set(ev["themes"]) & set((cfg["exposure"].get(t) or {}).get("themes") or []))
            return (0 if via.startswith(("direkt", "LLM", "Quelle")) else 1 if via.startswith("Exposure") else 2,
                    -overlap, order[category(cfg, t)])
        targets = [{"ticker": t, "via": via, "category": category(cfg, t),
                    "name": cfg["companies"].get(t, {}).get("name", t)}
                   for t, via in sorted(ev["mapped"].items(), key=lambda kv: rank(*kv))][:cfg["llm"]["max_tickers_per_event"]]
        if ctx.budget.state() == "holdings_only":
            targets = [t for t in targets if t["category"] == "HOLDING"]
        if not targets:
            _finish(ctx, ev["id"], "deferred", "Budget: holdings_only", now)
            continue
        sources = json.loads(ev["sources"])
        try:
            summary, impacts, errors = ctx.llm.impact(
                {"url": ev["url"], "title": ev["title"], "event_type": ev["event_type"], "sources": sources,
                 "text": f"{ev['title']}\n{ev['summary']}"[:6000]}, targets)
        except BudgetPaused:
            _finish(ctx, ev["id"], "deferred", "Budget: paused", now)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("Impact fehlgeschlagen")
            _finish(ctx, ev["id"], "error", f"Impact-Fehler: {e}"[:300], now)
            continue
        by_t = {t["ticker"]: t for t in targets}
        passed, decisions = [], []
        for imp in impacts:
            tgt = by_t[imp.ticker]
            reason = gate(cfg, tgt["category"], imp, cand_today)
            trad, isin, price = None, None, None
            if reason is None:
                trad, isin = ctx.market.tradability(imp.ticker)
                if trad == UNKNOWN:
                    reason = "Kaufbarkeit unbekannt"
                    db.jsonl("unverified_tradability.jsonl", {"ts": now, "ticker": imp.ticker, "isin": isin,
                                                              "event_id": ev["id"], "title": ev["title"],
                                                              "impact": imp.model_dump()})
                elif trad == EXCLUDED:
                    reason = "Kaufbarkeit: ausgeschlossen (blacklist)"
            if reason is None:
                price = ctx.market.priced(imp.ticker)
                cand_today += tgt["category"] == "CANDIDATE"
                passed.append({"ticker": imp.ticker, "name": tgt["name"], "category": tgt["category"], "impact": imp,
                               "price": price, "tradability": trad})
            decisions.append({"ticker": imp.ticker, "emitted": reason is None, "reason": reason})
            db.x("insert into signals(event_id, ticker, category, emitted, reason, direction, magnitude, horizon, "
                 "confidence, ord, why, facts_used, event_type, tradability, price, currency, price_eur, created_at) "
                 "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ev["id"], imp.ticker, tgt["category"],
                 int(reason is None), reason, imp.direction, imp.magnitude, imp.horizon, imp.confidence, imp.order,
                 imp.reason, json.dumps(imp.facts_used, ensure_ascii=False), ev["event_type"], trad,
                 price and price["price"], price and price["currency"], price and price["eur"],
                 datetime.now(ctx.tz).isoformat())
        status = "signal" if passed else "evaluated"
        _finish(ctx, ev["id"], status, None if passed else ("; ".join(d["reason"] for d in decisions) or
                                                            "Impact: keine gültige Bewertung") +
                (f" | Schemafehler: {'; '.join(errors)}" if errors else ""), now,
                {"event_type": ev["event_type"], "summary": summary, "decisions": decisions, "trigger": ev["trigger"]})
        if passed:
            output.render(ctx, {**ev, "summary_de": summary, "sources": sources}, passed)
            emitted += passed
    return emitted
