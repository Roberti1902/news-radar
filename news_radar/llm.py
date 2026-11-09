"""Triage (günstiges Modell, gebündelt) und Impact (stärkeres Modell) mit Budget-Bremse und Prompt Caching."""
import json
import logging
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import ValidationError

from .models import Impact, ImpactBatch, Triage, TriageBatch

log = logging.getLogger("news_radar")

TRIAGE_SYSTEM = """Du sortierst Finanznachrichten für einen Swing-Trader (Horizont 1–24 Monate).
Bewerte jedes Event nur anhand des gelieferten Textes:
- relevance 0–1: Wie stark betrifft es die Firmen/Themen im Universum unten (Bestand > Watchlist > Themen)?
- is_material: Könnte es Umsatz, Marge, Bewertung oder Kapitalstruktur spürbar ändern?
- event_type: genau einer aus earnings, guidance, M&A, regulatory, macro, geopolitical, analyst, product, legal, capital_action.
- tickers: Ticker aller im Text genannten börsennotierten Firmen (leer, wenn keine).
Listicles, Werbung, Wiederholungen alter Kursziele und Meinungsstücke ohne neue Fakten: relevance ≤ 0.2.
Antworte für jede übergebene id genau einmal."""

IMPACT_SYSTEM = """Du schätzt die Wirkung eines Nachrichten-Events auf einzelne Aktien.
Regeln:
1. Urteile ausschließlich auf Basis des gelieferten Textes. Erfinde keine Zahlen, Kurse oder Fakten.
2. Ist die Wirkung unklar oder widersprüchlich: direction "neutral", confidence niedrig.
3. Halte Fakten, Interpretation und Prognose getrennt:
   - facts_used = wörtliche Zitate aus dem Text (Fakten),
   - reason = Interpretation, Deutsch, max. 25 Wörter, Form "Ursache → Wirkung auf Umsatz/Marge/Bewertung",
     immer mit dem Pfeilzeichen →, z. B. "Großauftrag füllt Backlog → höherer Umsatz 2027",
   - magnitude/horizon/confidence = Prognose. confidence ist immer 0–1 (Sicherheit des Urteils), auch bei negativer Richtung.
4. order "first" = Firma direkt betroffen; "second" = Wirkung über Kunden, Zulieferer, Rohstoffe oder Makro (siehe Exposure-Map).
5. Gib für jeden angefragten Ticker genau ein Objekt zurück. Keine Kauf-/Verkaufsempfehlung, keine Positionsgrößen.
event_summary: deutsche Kurzfassung der Fakten, max. 12 Wörter."""


class BudgetPaused(Exception):
    pass


class Budget:
    """Tagesbudget in USD (lokaler Kalendertag). ≥100 %: nur HOLDING. ≥120 %: LLM pausiert."""

    def __init__(self, db, cfg: dict, tz: str):
        self.db, self.cfg, self.tz = db, cfg, ZoneInfo(tz)

    def day(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def spent(self) -> float:
        return self.db.q("select coalesce(sum(usd),0) s from llm_costs where day=?", self.day())[0]["s"]

    def state(self) -> str:
        s, cap = self.spent(), self.cfg["max_daily_usd"]
        return "paused" if s >= 1.2 * cap else "holdings_only" if s >= cap else "ok"

    def price(self, model: str) -> dict:
        prices = self.cfg["prices_per_mtok"]
        return prices.get(model) or max(prices.values(), key=lambda p: p["input"])  # unbekannt -> teuerstes

    def estimate(self, model: str, prompt_chars: int, max_out: int) -> float:
        p = self.price(model)
        return (prompt_chars / 3.5 * p["input"] + max_out * p["output"]) / 1e6  # ponytail: grobe Zeichen→Token-Schätzung

    def check(self, model: str, prompt_chars: int, max_out: int) -> None:
        if self.spent() + self.estimate(model, prompt_chars, max_out) >= 1.2 * self.cfg["max_daily_usd"]:
            raise BudgetPaused()

    def record(self, model: str, usage) -> float:
        p = self.price(model)
        cw, cr = getattr(usage, "cache_creation_input_tokens", 0) or 0, getattr(usage, "cache_read_input_tokens", 0) or 0
        usd = (usage.input_tokens * p["input"] + cw * p["input"] * 1.25 + cr * p["input"] * 0.1
               + usage.output_tokens * p["output"]) / 1e6
        self.db.x("insert into llm_costs values(?,?,?,?,?,?,?)", datetime.now(self.tz).isoformat(), self.day(),
                  model, usd, usage.input_tokens, usage.output_tokens, cr)
        return usd


def _validate_each(model, rows: list[dict], errors: list[str]):
    out = []
    for r in rows:
        try:
            out.append(model.model_validate(r))
        except ValidationError as e:
            errors.append(f"{r.get('ticker', r.get('id'))}: {e.errors()[0]['msg']}")
    return out


class LLM:
    def __init__(self, cfg: dict, budget: Budget, universe_text: str, mock: dict | None = None):
        self.cfg, self.budget, self.mock = cfg, budget, mock
        # Statischer, sortierter Kontext -> stabiles Präfix für Prompt Caching.
        self.context_block = {"type": "text", "text": universe_text, "cache_control": {"type": "ephemeral"}}
        self.local = cfg.get("provider") == "ollama"
        if mock is None and not self.local:
            import anthropic
            self.client = anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(verify=ssl.create_default_context()))
            self.schema = anthropic.transform_schema

    def _ollama(self, model: str, system: str, user: str, schema, max_tokens: int) -> dict:
        """Lokales Modell über die Ollama-REST-API: kostenlos, kein Budget nötig.
        Gleiches System-Präfix bei jedem Aufruf -> Ollama nutzt den KV-Cache wieder."""
        r = httpx.post(f"{self.cfg.get('ollama_url', 'http://localhost:11434')}/api/chat",
                       timeout=self.cfg.get("ollama_timeout_s", 900), json={
                           "model": model, "stream": False, "format": schema.model_json_schema(),
                           "messages": [{"role": "system", "content": system + "\n\n" + self.context_block["text"]},
                                        {"role": "user", "content": user}],
                           "options": {"temperature": 0, "num_ctx": self.cfg.get("num_ctx", 16384),
                                       "num_predict": max_tokens}})
        r.raise_for_status()
        d = r.json()
        if d.get("done_reason") != "stop":
            raise RuntimeError(f"Ollama done_reason={d.get('done_reason')}")
        log.info("Ollama %s: %s Prompt-Token, %s Antwort-Token, %.1f s", model, d.get("prompt_eval_count"),
                 d.get("eval_count"), d.get("total_duration", 0) / 1e9)
        return json.loads(d["message"]["content"])

    def _call(self, model: str, system: str, user: str, schema, max_tokens: int, effort: str) -> dict:
        if self.local:
            return self._ollama(model, system, user, schema, max_tokens)
        self.budget.check(model, len(system) + len(self.context_block["text"]) + len(user), max_tokens // 4)
        kw = dict(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": user}],
                  system=[{"type": "text", "text": system}, self.context_block],
                  output_config={"effort": effort, "format": {"type": "json_schema", "schema": self.schema(schema)}})
        if self.cfg.get("server_fallbacks") and model.startswith("claude-sonnet-5-5"):
            r = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        else:
            r = self.client.messages.create(**kw)
        self.budget.record(r.model, r.usage)
        if r.stop_reason != "end_turn":
            raise RuntimeError(f"LLM stop_reason={r.stop_reason}")
        return json.loads(next(b.text for b in r.content if b.type == "text"))

    def triage(self, events: list[dict]) -> tuple[dict[int, Triage], list[str]]:
        """events: [{id, title, summary, sources, tier, tickers_hint}] -> {id: Triage}"""
        if self.mock is not None:
            rows = [{"id": e["id"], **self.mock["triage"].get(e["url"], {"relevance": 0.1, "is_material": False,
                                                                         "event_type": "product"})} for e in events]
        else:
            payload = json.dumps([{k: e[k] for k in ("id", "title", "summary", "sources", "tier", "tickers_hint")}
                                  for e in events], ensure_ascii=False)
            rows = self._call(self.cfg["triage_model"], TRIAGE_SYSTEM, payload, TriageBatch, 8000, "low")["results"]
        errors: list[str] = []
        return {t.id: t for t in _validate_each(Triage, rows, errors)}, errors

    def impact(self, event: dict, targets: list[dict]) -> tuple[str, list[Impact], list[str]]:
        """targets: [{ticker, name, category, via}] -> (event_summary, [Impact], errors)"""
        if self.mock is not None:
            data = self.mock["impact"].get(event["url"], {"event_summary": event["title"], "impacts": []})
        else:
            user = (f"EVENT ({event['event_type']}, Quellen: {', '.join(event['sources'])}):\n{event['text']}\n\n"
                    "Bewerte diese Ticker:\n" + "\n".join(
                        f"- {t['ticker']} ({t['name']}, {t['category']}, Zuordnung: {t['via']})" for t in targets))
            data = self._call(self.cfg["impact_model"], IMPACT_SYSTEM, user, ImpactBatch, 16000, "medium")
        errors: list[str] = []
        wanted = {t["ticker"] for t in targets}
        impacts = [i for i in _validate_each(Impact, data.get("impacts", []), errors) if i.ticker in wanted]
        return data.get("event_summary") or event["title"], impacts, errors


def universe_text(cfg: dict) -> str:
    u = cfg["universe"]
    return yaml.safe_dump({
        "bestand": {t: c["name"] for t, c in (u.get("holdings") or {}).items()},
        "watchlist": {t: c["name"] for t, c in (u.get("watchlist") or {}).items()},
        "themen": sorted((u.get("themes") or {}).keys()),
        "exposure_map": cfg["exposure"],
    }, allow_unicode=True, sort_keys=True)


def load_mock(path) -> dict:
    return json.loads(open(path, encoding="utf-8").read())

