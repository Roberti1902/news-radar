import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = {"holdings": "HOLDING", "watchlist": "WATCHLIST", "candidates": "CANDIDATE"}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE-Zeilen aus .env übernehmen; bereits gesetzte Umgebungsvariablen haben Vorrang."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value)


def load(config_dir: str | Path | None = None) -> dict:
    load_dotenv()
    d = Path(config_dir or ROOT / "config")
    read = lambda n: yaml.safe_load((d / f"{n}.yaml").read_text(encoding="utf-8")) or {}
    cfg = read("config")
    for name in ("universe", "exposure", "whitelist", "blacklist"):
        cfg[name] = read(name)
    cfg["llm"]["max_daily_usd"] = float(os.getenv("MAX_DAILY_LLM_USD", cfg["llm"]["max_daily_usd"]))
    cfg["companies"] = companies(cfg["universe"])
    return cfg


def companies(universe: dict) -> dict[str, dict]:
    """ticker -> {name, aliases, category, isin?, currency, ...}; höhere Kategorie gewinnt."""
    out: dict[str, dict] = {}
    for key in ("candidates", "watchlist", "holdings"):
        for ticker, c in (universe.get(key) or {}).items():
            out[ticker] = {"currency": "USD",
                           **c, "ticker": ticker, "category": CATEGORIES[key]}
    return out
