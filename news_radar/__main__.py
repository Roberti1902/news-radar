"""python -m news_radar [--once] [--interval 30|60] [--dry-run] [--backfill 24h] [--evaluate] [--config DIR]"""
import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.console import Console

from . import config, evaluate, output
from .db import DB
from .llm import LLM, Budget, load_mock, universe_text
from .market import Market
from .models import Item
from .net import Http
from .pipeline import run_cycle

log = logging.getLogger("news_radar")
FIXTURES = config.ROOT / "fixtures"


class Ctx:
    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg, self.dry_run = cfg, dry_run
        self.tz = ZoneInfo(cfg["timezone"])
        self.console = Console(highlight=False, soft_wrap=True)
        root = config.ROOT
        self.db = DB(root / (cfg["dry_run_db_path"] if dry_run else cfg["db_path"]), root / cfg["log_dir"])
        if dry_run:
            self.db.reset()
        self.http = Http(cfg["http"]["host_intervals"], cfg["http"]["timeout_s"], cfg["http"]["retries"])
        self.budget = Budget(self.db, cfg["llm"], cfg["timezone"])
        fx = json.loads((FIXTURES / "market.json").read_text(encoding="utf-8")) if dry_run else None
        self.market = Market(self, fx)
        self.llm = LLM(cfg["llm"], self.budget, universe_text(cfg), load_mock(FIXTURES / "llm.json") if dry_run else None)
        self.failed_sources: list[str] = []

    def fixture_items(self) -> list[Item]:
        now = datetime.now(timezone.utc)
        rows = json.loads((FIXTURES / "news.json").read_text(encoding="utf-8"))
        return [Item(**{k: v for k, v in r.items() if k not in ("minutes_ago", "_note")},
                     published_at=now - timedelta(minutes=r["minutes_ago"])) for r in rows]


def parse_duration(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([hd])", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("Format z. B. 24h oder 2d")
    return timedelta(hours=int(m[1]) * (24 if m[2] == "d" else 1))


def safe_cycle(ctx, since=None) -> None:
    try:
        run_cycle(ctx, since)
    except Exception:  # noqa: BLE001 – Dauerbetrieb darf an einem Zyklus nicht sterben
        log.exception("Zyklus abgebrochen")
        output.warn(ctx, "Zyklus abgebrochen – Details in logs/news_radar.log")


def main() -> None:
    ap = argparse.ArgumentParser(prog="news_radar", description="News Radar – Signale aus öffentlichen Nachrichten")
    ap.add_argument("--once", action="store_true", help="genau ein Zyklus")
    ap.add_argument("--interval", type=int, choices=(30, 60), help="Minuten zwischen Zyklen")
    ap.add_argument("--dry-run", action="store_true", help="Fixtures statt Netz, gemockte LLM-Antworten")
    ap.add_argument("--backfill", type=parse_duration, help="Zeitraum statt 'seit letztem Lauf', z. B. 24h")
    ap.add_argument("--evaluate", action="store_true", help="Outcomes nach 1/5/20 Handelstagen + Trefferquote")
    ap.add_argument("--config", help="Config-Verzeichnis (Default ./config)")
    a = ap.parse_args()

    cfg = config.load(a.config)
    (config.ROOT / cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=config.ROOT / cfg["log_dir"] / "news_radar.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    ctx = Ctx(cfg, a.dry_run)
    if not a.dry_run and cfg["llm"].get("provider") != "ollama" and not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        output.warn(ctx, "ANTHROPIC_API_KEY nicht gesetzt – LLM-Bewertung schlägt fehl, Events werden nur gespeichert.")

    if a.evaluate:
        evaluate.run(ctx)
        return
    since = datetime.now(timezone.utc) - a.backfill if a.backfill else None
    if a.once or a.dry_run:
        safe_cycle(ctx, since)
        return

    from apscheduler.schedulers.blocking import BlockingScheduler
    minutes = a.interval or cfg["interval_minutes"]
    sched = BlockingScheduler(timezone=ctx.tz)
    if since:
        safe_cycle(ctx, since)  # Backfill zuerst, danach regulär im Intervall
    sched.add_job(safe_cycle, "interval", minutes=minutes, args=[ctx], max_instances=1, coalesce=True,
                  **({} if since else {"next_run_time": datetime.now(ctx.tz)}))
    ctx.console.print(f"[dim]News Radar läuft – alle {minutes} min. Strg+C beendet.[/]")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
