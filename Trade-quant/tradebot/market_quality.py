from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MarketQuality:
    best_bid: float
    best_ask: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return 1.0
        return (self.best_ask - self.best_bid) / self.mid


def market_quality_allows_trade(
    quality: Optional[MarketQuality],
    max_spread_pct: float,
):
    if quality is None:
        return True, "ok: no book data"
    if quality.best_bid <= 0 or quality.best_ask <= 0 or quality.best_ask < quality.best_bid:
        return False, "invalid book"
    if quality.spread_pct > max_spread_pct:
        return False, f"spread too wide: {quality.spread_pct:.2%}"
    return True, "ok"
