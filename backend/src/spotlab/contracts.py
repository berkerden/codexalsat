"""Shared domain contracts. UTC milliseconds and Decimal money throughout."""

from dataclasses import dataclass
from decimal import Decimal

INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal
    ask_quantity: Decimal
    observed_at: int

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10_000
