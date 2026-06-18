# Binance Stock-Token T Strategy

This project is a research backtester for a long-core plus T-bucket strategy.

## Portfolio Split

- Core allocation: default 70%. This represents the long-term SpaceX thesis and
  is not traded by the backtester.
- T bucket: default 30%. Only this bucket is used for buy/sell simulations.
- Order size: default 350 USD to avoid the high effective fee rate on tiny
  fixed-fee orders.

## Available Binance-Style Market Data

The code consumes a generic `Candle` interface that can be built from Binance
kline data:

- open time
- open, high, low, close
- volume
- close time
- quote volume
- trade count

The public spot kline endpoint follows this shape. If Binance exposes bStocks or
stock-token data through a different endpoint, adapt only `tradebot/data.py` and
keep the `Candle` model unchanged.

Useful extra fields for future live trading:

- best bid and ask from book ticker data
- recent trades for spread/slippage checks
- exchange trading status
- account balances and open orders

## Daily Filter

New T buys are paused when the daily trend breaks:

- `uptrend`: latest close > 5-day SMA > 10-day SMA > 20-day SMA
- `broken`: latest close < 10-day SMA or 5-day SMA < 10-day SMA
- `neutral`: anything in between

The current implementation opens normal pullback buys in `uptrend`. It also
allows a restricted `neutral` mode when daily ATR is high and the pullback is
deeper. `broken` remains a hard pause for new T buys.

## Intraday Trigger

The strategy waits for a pullback and reclaim:

1. Recent candles must include a close at least 0.8% below VWAP.
2. Current close must reclaim VWAP by at least 0.1%.
3. Daily ATR should be at least 1.8%; otherwise confidence is reduced.

This avoids buying every dip while price is still below intraday fair value.

Additional entries:

- Neutral mode: requires a deeper 1.5% VWAP pullback and high daily ATR.
- Momentum mode: if price holds above VWAP by 0.5% for the configured lookback
  with expanded volume, the strategy may enter a half-size 175 USD order.

## Exit Rules

- Take profit: dynamic by T layer count. One layer uses 1.8%, two layers use
  1.4%, and three or more layers use 1.0%.
- Stop loss: default 3.5% from average T-bucket entry.
- Slippage: default 30 bps per fill for SpaceX-style token liquidity.
- Fees: below 350 USD uses fixed 0.35 USD; at or above 350 USD uses 0.1%.

## Guardrails

- The backtester prevents repeated buys around the same price by requiring a
  1.2% lower price before adding another T order.
- It rejects trades when synthetic or live spread is above 0.5%.
- It rejects signals after an extreme 1-minute move of 5% or more.
- It never spends the core allocation.
- It does not place live orders.

## 24/5 Sessions And Wallet Isolation

For 24/5 token trading, VWAP should be reset at an explicit session boundary
instead of accumulating forever. `tradebot/data.py` includes `session_start_ms`
and `candles_for_session` helpers so a live adapter can reset VWAP at a chosen
UTC hour.

For live trading, keep a local virtual wallet for the T bucket. Do not derive
T buying power from the full API account balance on every loop, because core
holdings and other assets can distort the available balance.

## Practical Use

Start with synthetic simulation:

```bash
python3 -m tradebot.cli simulate --starting-quote 3500 --core-allocation-pct 0.70
```

Then backtest real CSVs:

```bash
python3 -m tradebot.cli backtest-csv --daily data/spcx_daily.csv --intraday data/spcx_1m.csv
```

Before any real automation, add live checks for bid/ask spread, stock-token
trading status, available balance, and order rejection handling.

## 中文说明

本项目是一个研究型回测工具，用于验证“长期核心仓 + 可交易 T 仓”的 Binance 美股代币策略。

## 仓位拆分

- 核心仓：默认 70%。代表长期看好 SpaceX 的底仓，回测器不会交易这部分。
- T 仓：默认 30%。只有这部分资金会参与买入和卖出模拟。
- 单笔金额：默认 350 USD，用来避开小额固定手续费导致的高实际费率。

## 可用的 Binance 风格行情数据

代码使用通用的 `Candle` K线接口，可以由 Binance K 线数据构建：

- 开盘时间
- 开盘价、最高价、最低价、收盘价
- 成交量
- 收盘时间
- 成交额
- 成交笔数

Binance 公开现货 K 线接口就是这种结构。如果 Binance 的 bStocks 或美股代币数据来自另一个接口，只需要适配 `tradebot/data.py`，不要改动上层策略使用的 `Candle` 模型。

未来接实盘时，建议额外获取这些字段：

- 买一价/卖一价，用于判断盘口点差
- 最近成交，用于估算真实滑点
- 交易所或标的交易状态
- 账户余额和未成交订单

## 日K过滤

当日K趋势破坏时，暂停新的 T 仓买入：

- `uptrend`：最新收盘价 > 5日均线 > 10日均线 > 20日均线
- `broken`：最新收盘价 < 10日均线，或 5日均线 < 10日均线
- `neutral`：介于两者之间的状态

当前实现会在 `uptrend` 状态下开普通回踩买入；当日K处于 `neutral`、但 ATR 仍然很高且回踩更深时，允许受限震荡做 T。`broken` 仍然是硬暂停，不开新的 T 仓买入。

## 日内触发

日内策略等待“回踩 + 收复”：

1. 最近一段 K 线里，必须出现至少低于 VWAP 0.8% 的收盘价。
2. 当前收盘价必须重新站上 VWAP 至少 0.1%。
3. 日K ATR 至少应达到 1.8%；否则降低信号置信度。

这样可以避免价格仍低于日内公平价值时盲目接飞刀。

额外入口：

- 震荡模式：要求低于 VWAP 1.5% 的更深回踩，并且日K ATR 较高。
- 强势追随模式：如果价格在配置窗口内持续站在 VWAP 上方 0.5%，且成交量放大，可用 175 USD 半仓顺势试单。

## 退出规则

- 止盈：按 T 仓持仓层数动态调整。1 层用 1.8%，2 层用 1.4%，3 层及以上用 1.0%。
- 止损：默认相对 T 仓平均买入价下跌 3.5%。
- 滑点：针对 SpaceX 风格代币流动性，默认每次成交 30 bps。
- 手续费：350 USD 以下按 0.35 USD 固定费用估算；350 USD 及以上按 0.1% 估算。

## 保护规则

- 回测器要求价格比上一次 T 仓买入价低 1.2% 后，才允许继续加仓，避免同一价位反复买入。
- 当合成或实时盘口点差超过 0.5% 时，拒绝成交。
- 当 1 分钟价格突变达到 5% 或以上时，拒绝信号，避免接插针。
- 回测器永远不会动用核心仓资金。
- 当前代码不会下实盘订单。

## 24/5 交易日切片与虚拟钱包隔离

对于 24/5 美股代币交易，VWAP 不应该无限累计，必须在明确的交易日边界重置。`tradebot/data.py` 提供了 `session_start_ms` 和 `candles_for_session`，后续实盘适配器可以按指定 UTC 小时重置 VWAP。

实盘时应维护本地虚拟 T 钱包，不能在每轮循环里直接使用 API 总余额乘以 30% 作为可用 T 仓资金，因为核心仓和其他资产的市值变化会污染这个判断。

## 实际使用

先运行合成行情模拟：

```bash
python3 -m tradebot.cli simulate --starting-quote 3500 --core-allocation-pct 0.70
```

再使用真实 CSV 数据回测：

```bash
python3 -m tradebot.cli backtest-csv --daily data/spcx_daily.csv --intraday data/spcx_1m.csv
```

在接入任何实盘自动化之前，必须补充买卖盘口点差、标的交易状态、可用余额、订单拒绝处理等实时检查。
