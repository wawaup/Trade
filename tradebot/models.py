from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int


@dataclass(frozen=True)
class Signal:
    action: str
    reason: str
    confidence: float
    suggested_quote: float = 0.0


@dataclass(frozen=True)
class Trade:
    side: str
    open_time: int
    price: float
    qty: float
    quote: float
    fee: float
    reason: str


@dataclass(frozen=True)
class CoreSignal:
    action: str  # "HOLD", "REDUCE", "EXIT_ALL"
    reason: str
    target_allocation_pct: float  # 0.0–1.0; caller compares to current to decide direction

