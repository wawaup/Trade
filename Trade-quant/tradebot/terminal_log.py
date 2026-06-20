from datetime import datetime, timezone
from typing import Optional


def format_signal_log(
    timestamp_ms: int,
    symbol: str,
    level: str,
    event: str,
    message: str,
) -> str:
    ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts}] [{level.upper()}] [{event.upper()}] {symbol} {message}"


def build_factor_dashboard(
    symbol: str,
    trend: str,
    current_price: float,
    vwap: float,
    atr_pct: float,
    layers: int,
    avg_entry: Optional[float],
) -> str:
    avg_entry_text = "-" if avg_entry is None else f"${avg_entry:.2f}"
    dev = current_price / vwap - 1 if vwap else 0.0
    return (
        "┌────────────────────────────────────────────────────────────┐\n"
        f"│ DATA FACTOR DASHBOARD | Target: {symbol:<8}                │\n"
        "├──────────────────────┬─────────────────────────────────────┤\n"
        f"│ Daily Trend          │ {trend:<35} │\n"
        f"│ Intraday VWAP        │ Price ${current_price:>8.2f} / VWAP ${vwap:>8.2f} │\n"
        f"│ VWAP Dev             │ {dev:>8.2%}                            │\n"
        f"│ Daily ATR            │ {atr_pct:>8.2%}                            │\n"
        f"│ T-Bucket Layers      │ {layers:<35} │\n"
        f"│ Avg Entry            │ {avg_entry_text:<35} │\n"
        "└──────────────────────┴─────────────────────────────────────┘"
    )
