from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EventType = Literal["earnings", "guidance", "M&A", "regulatory", "macro", "geopolitical",
                    "analyst", "product", "legal", "capital_action"]
MAGNITUDE_RANK = {"low": 0, "medium": 1, "high": 2}


class Item(BaseModel):
    title: str
    summary: str = ""
    url: str
    source: str            # Publisher (Reuters, SEC, Fed ...)
    via: str               # Adapter (Finnhub, Yahoo RSS ...)
    tier: int
    published_at: datetime
    tickers_raw: list[str] = []


class Triage(BaseModel):
    id: int
    relevance: float = Field(ge=0, le=1)
    is_material: bool
    event_type: EventType
    tickers: list[str] = Field(default=[], description="Ticker aller im Text genannten börsennotierten Firmen")


class TriageBatch(BaseModel):
    results: list[Triage]


class Impact(BaseModel):
    ticker: str
    direction: Literal["positive", "negative", "neutral"]
    magnitude: Literal["low", "medium", "high"]
    horizon: Literal["days", "weeks", "months"]
    confidence: float = Field(ge=0, le=1)
    order: Literal["first", "second"]
    reason: str = Field(description="max. 25 Wörter, Deutsch, Form 'Ursache → Wirkung auf Umsatz/Marge/Bewertung'")
    facts_used: list[str] = Field(description="wörtliche Zitate aus dem gelieferten Text")

    @field_validator("reason")
    @classmethod
    def _max_25_words(cls, v: str) -> str:
        if len(v.split()) > 25:
            raise ValueError("reason länger als 25 Wörter")
        if "→" not in v:
            raise ValueError("reason ohne 'Ursache → Wirkung'")
        return v

    @field_validator("facts_used")
    @classmethod
    def _facts_present(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("facts_used leer")
        return v


class ImpactBatch(BaseModel):
    event_summary: str = Field(description="Deutsche Kurzfassung des Events, max. 12 Wörter, nur Fakten")
    impacts: list[Impact]
