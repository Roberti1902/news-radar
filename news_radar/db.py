import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists articles(url_hash text primary key, event_id integer, url text, title text, source text,
    via text, tier integer, published_at text, fetched_at text);
create table if not exists events(id integer primary key, title text, summary text, url text, tier_min integer,
    source_count integer, sources text, tickers text, numbers text, first_seen text, last_seen text,
    published_at text, status text, reason text, event_type text, relevance real, is_material integer,
    evaluated_at text);
create table if not exists signals(id integer primary key, event_id integer, ticker text, category text,
    emitted integer, reason text, direction text, magnitude text, horizon text, confidence real, ord text,
    why text, facts_used text, event_type text, tradability text, price real, currency text, price_eur real,
    created_at text);
create table if not exists signal_outcomes(signal_id integer, horizon_days integer, price real, return_pct real,
    hit integer, primary key(signal_id, horizon_days));
create table if not exists price_history(ticker text, date text, close real, primary key(ticker, date));
create table if not exists llm_costs(ts text, day text, model text, usd real, input_tokens integer,
    output_tokens integer, cache_read_tokens integer);
create table if not exists source_runs(ts text, source text, ok integer, items integer, error text);
create table if not exists cache(key text primary key, value text, ts text);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DB:
    def __init__(self, path: str | Path, jsonl_dir: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.jsonl_dir = Path(jsonl_dir)
        self.jsonl_dir.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        """Nur für --dry-run: Tabellen leeren, damit Fixtures jedes Mal frisch laufen."""
        for t in ("meta", "articles", "events", "signals", "signal_outcomes", "price_history", "llm_costs",
                  "source_runs"):
            self.con.execute(f"delete from {t}")
        self.con.commit()

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> int:
        cur = self.con.execute(sql, args)
        self.con.commit()
        return cur.lastrowid

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.x("insert or replace into meta values(?, ?)", key, value)
            return value
        row = self.q("select value from meta where key=?", key)
        return row[0]["value"] if row else None

    def cache_get(self, key: str, max_age_s: float | None = None):
        row = self.q("select value, ts from cache where key=?", key)
        if not row:
            return None
        if max_age_s is not None and (utcnow() - datetime.fromisoformat(row[0]["ts"])).total_seconds() > max_age_s:
            return None
        return json.loads(row[0]["value"])

    def cache_set(self, key: str, value) -> None:
        self.x("insert or replace into cache values(?, ?, ?)", key, json.dumps(value), utcnow().isoformat())

    def jsonl(self, name: str, record: dict) -> None:
        with open(self.jsonl_dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
