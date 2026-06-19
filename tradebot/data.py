import csv
import json
import math
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

from tradebot.models import Candle


BINANCE_REST_BASE = "https://api.binance.com"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def parse_binance_kline(row: list) -> Candle:
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=int(row[6]),
        quote_volume=float(row[7]),
        trades=int(row[8]),
    )


def fetch_spot_klines(symbol: str, interval: str, limit: int = 500) -> list[Candle]:
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    url = f"{BINANCE_REST_BASE}/api/v3/klines?{params}"
    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [parse_binance_kline(row) for row in data]


def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    max_pages: int = 200,
) -> list[Candle]:
    """Fetch all klines in [start_ms, end_ms] via paginated Binance API calls."""
    candles: list[Candle] = []
    cursor = start_ms
    for _ in range(max_pages):
        if cursor >= end_ms:
            break
        params = urllib.parse.urlencode({
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        })
        url = f"{BINANCE_REST_BASE}/api/v3/klines?{params}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data:
            break
        batch = [parse_binance_kline(row) for row in data]
        candles.extend(batch)
        if len(data) < 1000:
            break
        cursor = batch[-1].close_time + 1
    return candles


def session_start_ms(timestamp_ms: int, reset_utc_hour: int = 8) -> int:
    if not 0 <= reset_utc_hour <= 23:
        raise ValueError("reset_utc_hour must be in 0..23")
    reset_offset = reset_utc_hour * HOUR_MS
    return ((timestamp_ms - reset_offset) // DAY_MS) * DAY_MS + reset_offset


def candles_for_session(candles: list[Candle], timestamp_ms: int, reset_utc_hour: int = 8) -> list[Candle]:
    start = session_start_ms(timestamp_ms, reset_utc_hour)
    return [c for c in candles if c.open_time >= start and c.open_time <= timestamp_ms]


def write_candles_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"]
        )
        for c in candles:
            writer.writerow(
                [c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time, c.quote_volume, c.trades]
            )


def read_candles_csv(path: Path) -> list[Candle]:
    with path.open() as handle:
        reader = csv.DictReader(handle)
        return [
            Candle(
                open_time=int(row["open_time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                close_time=int(row["close_time"]),
                quote_volume=float(row["quote_volume"]),
                trades=int(row["trades"]),
            )
            for row in reader
        ]


def generate_synthetic_spcx(seed: int = 7) -> tuple[list[Candle], list[Candle]]:
    random.seed(seed)
    now = int(time.time() * 1000)
    day_ms = 86_400_000
    minute_ms = 60_000

    daily = []
    price = 135.0
    for i in range(35):
        drift = 0.018 if i < 24 else (-0.004 if i < 29 else 0.022)
        shock = random.gauss(0, 0.02 if i < 29 else 0.003)
        open_ = price
        close = max(20, price * (1 + drift + shock))
        high = max(open_, close) * (1 + abs(random.gauss(0.015, 0.01)))
        low = min(open_, close) * (1 - abs(random.gauss(0.014, 0.01)))
        volume = 1_000_000 + random.random() * 800_000
        ts = now - (35 - i) * day_ms
        daily.append(Candle(ts, open_, high, low, close, volume, ts + day_ms - 1, volume * close, 10000))
        price = close

    intraday = []
    price = daily[-1].close
    for i in range(240):
        if i < 35:
            drift = 0.0025 + random.gauss(0, 0.0015)
        elif i < 70:
            drift = -0.0028 + random.gauss(0, 0.0015)
        elif i < 115:
            drift = 0.0038 + random.gauss(0, 0.0018)
        else:
            drift = math.sin(i / 13) * 0.002 + random.gauss(0, 0.0025)
        open_ = price
        close = max(20, price * (1 + drift))
        high = max(open_, close) * (1 + abs(random.gauss(0.0025, 0.001)))
        low = min(open_, close) * (1 - abs(random.gauss(0.0025, 0.001)))
        volume = 15_000 + random.random() * 20_000
        ts = now + i * minute_ms
        intraday.append(Candle(ts, open_, high, low, close, volume, ts + minute_ms - 1, volume * close, 100))
        price = close

    return daily, intraday
