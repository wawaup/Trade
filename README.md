# Tradebot

Small research backtester for a Binance-style USDT/USDC stock-token T strategy.

It models:

- Daily trend filter: only open new T buys when daily trend is healthy.
- Intraday trigger: buy a pullback after price reclaims VWAP.
- T bucket risk: core long allocation is separated from the tradable T bucket.
- Fee friction: below 350 USD uses 0.35 USD fixed fee, otherwise 0.1%.
- Slippage: default 8 bps per fill.

This is not financial advice and is not a live trading bot. Use it to validate
rules before connecting any real account.

## Run Tests

```bash
python3 -m unittest discover -s tests
```

## Synthetic Simulation

```bash
python3 -m tradebot.cli simulate --starting-quote 3500 --core-allocation-pct 0.70
```

## Fetch Binance Klines

The public Binance spot endpoint can fetch standard kline data:

```bash
python3 -m tradebot.cli fetch-binance --symbol BTCUSDT --interval 1m --limit 500 --out data/btcusdt_1m.csv
```

If Binance exposes a bStocks/stock-token symbol through the same market-data
shape, pass that symbol instead. If it uses a separate stock endpoint, adapt
`tradebot/data.py` while keeping the `Candle` interface unchanged.

## Backtest CSV

```bash
python3 -m tradebot.cli backtest-csv --daily data/daily.csv --intraday data/intraday.csv
```

## 中文说明

这是一个用于研究 Binance 风格 USDT/USDC 美股代币做 T 策略的小型回测工具。

它当前建模了：

- 日K趋势过滤：只有日线趋势健康时才开新的 T 仓买入。
- 日内触发：价格回踩后重新站上 VWAP 才买入。
- T 仓风险控制：长期核心仓和可交易 T 仓分开管理。
- 手续费摩擦：350 USD 以下按 0.35 USD 固定费估算，否则按 0.1%。
- 滑点：默认每次成交 8 bps。

这不是投资建议，也不是实盘交易机器人。它的用途是在连接真实账户之前，先验证规则、手续费、滑点和仓位逻辑。

## 运行测试

```bash
python3 -m unittest discover -s tests
```

## 运行合成行情模拟

```bash
python3 -m tradebot.cli simulate --starting-quote 3500 --core-allocation-pct 0.70
```

## 获取 Binance K 线

Binance 公开现货接口可以获取标准 K 线数据：

```bash
python3 -m tradebot.cli fetch-binance --symbol BTCUSDT --interval 1m --limit 500 --out data/btcusdt_1m.csv
```

如果 Binance 将 bStocks 或美股代币标的暴露为同样的行情数据结构，可以把 `--symbol` 换成对应交易对。如果它使用独立的股票接口，只需要适配 `tradebot/data.py`，并保持 `Candle` 数据接口不变。

## 使用 CSV 回测

```bash
python3 -m tradebot.cli backtest-csv --daily data/daily.csv --intraday data/intraday.csv
```
