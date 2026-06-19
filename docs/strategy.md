# Trade T Strategy

本文档描述当前代码已经实现的 T 仓策略口径。所有条件均以 `tradebot/strategy.py`、`tradebot/indicators.py` 和 `tradebot/backtest.py` 的当前实现为准。

## 策略目标

Trade 当前策略是“核心仓 + T 仓”的研究型回测策略。

- 核心仓：默认 70%，只作为长期持有仓位概念，回测引擎不会买卖这部分。
- T 仓：默认 30%，回测只用这部分现金做日内买入、卖出、止盈、止损。
- 策略方向：当前只实现 T 仓多头交易；没有做空、反手、杠杆、实盘下单。
- 策略信号：`generate_signal()` 只返回 `BUY`、`SELL`、`HOLD`。回测层再把 `BUY` 映射为 `OPEN_T` 或 `ADD_T`，把 `SELL` 映射为 `CLOSE_T`。

## 输入数据

策略使用两个 K 线序列：

- `daily`: 日 K 线，用于判断趋势和 ATR。
- `intraday`: 日内 K 线窗口，用于计算 VWAP、回踩、收复、动量、1 分钟异常波动。

每根 K 线使用统一 `Candle` 结构：

| 字段 | 用途 |
|---|---|
| `open_time` | 信号、订单意图、风险事件、交易记录时间戳 |
| `open/high/low/close` | 趋势、ATR、VWAP、回踩、止盈止损、成交价格建模 |
| `volume` | VWAP 分母、动量成交量放大判断 |
| `quote_volume` | VWAP 分子 |
| `close_time` | 当前策略不直接使用 |
| `trades` | 当前策略不直接使用 |

最小数据要求：

- `daily` 少于 20 根，直接 `HOLD`，原因是 `not enough candles`。
- `intraday` 少于 2 根，直接 `HOLD`，原因是 `not enough candles`。
- VWAP 无法计算时，直接 `HOLD`，原因是 `missing vwap`。

## 策略参数

当前默认参数来自 `StrategyConfig`。

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `min_order_quote` | 350.0 | 普通回踩买入金额 |
| `momentum_order_quote` | 175.0 | 强势动量买入金额 |
| `max_t_bucket_pct` | 0.35 | 目前策略配置里存在，但当前回测引擎没有直接使用；T 仓上限由 `BacktestConfig.core_allocation_pct` 决定 |
| `pullback_pct` | 0.008 | 上升趋势回踩阈值：低于 VWAP 0.8% |
| `neutral_pullback_pct` | 0.015 | 震荡趋势回踩阈值：低于 VWAP 1.5% |
| `pullback_lookback` | 24 | 回看最近 24 根日内 K 线寻找回踩 |
| `reclaim_buffer_pct` | 0.001 | 当前收盘价重新站上 VWAP 0.1% |
| `momentum_vwap_premium_pct` | 0.005 | 动量窗口内收盘价持续高于 VWAP 0.5% |
| `momentum_lookback` | 15 | 动量窗口长度 15 根日内 K 线 |
| `momentum_volume_multiplier` | 1.5 | 最新成交量至少达到基准均量的 1.5 倍 |
| `take_profit_pct` | 0.018 | 1 层 T 仓止盈 1.8% |
| `layer2_take_profit_pct` | 0.014 | 2 层 T 仓止盈 1.4% |
| `layer3_take_profit_pct` | 0.010 | 3 层及以上 T 仓止盈 1.0% |
| `stop_loss_pct` | 0.035 | T 仓平均成本下跌 3.5% 止损 |
| `min_daily_atr_pct` | 0.018 | 日 K ATR 至少 1.8% 才算波动足够 |
| `buy_grid_spacing_pct` | 0.012 | 加仓价格必须比上次买入价低 1.2% |
| `flash_crash_pct` | 0.05 | 单根日内 K 线涨跌达到 5% 视为异常波动 |

## 因子计算

### 日 K 趋势

`daily_trend_state(daily)` 使用最近收盘价和 5/10/20 日简单均线。

| 状态 | 条件 | 策略含义 |
|---|---|---|
| `uptrend` | 最新收盘价 > MA5 > MA10 > MA20 | 允许上升趋势回踩买入，也允许强势动量试单 |
| `broken` | 最新收盘价 < MA10，或 MA5 < MA10 | 暂停新的 T 仓买入 |
| `neutral` | 不满足 `uptrend` 或 `broken` | 只允许更深回踩 + 高 ATR 的震荡 T |
| `unknown` | MA 数据不足 | 最终通常落入无交易条件 |

### VWAP

`vwap(intraday)` 使用当前传入的完整日内窗口：

```text
VWAP = sum(quote_volume) / sum(volume)
```

当前实现没有在策略内部按交易日切片重置 VWAP；调用方传入什么 `intraday` 窗口，VWAP 就按该窗口累计。

### 日 K ATR 百分比

`atr_pct(daily)` 默认周期为 14。

每根 true range：

```text
TR = max(
  high - low,
  abs(high - previous_close),
  abs(low - previous_close)
)
```

ATR 百分比：

```text
ATR% = average(TR over last 14 daily candles) / latest_close
```

如果日 K 少于 15 根或最新收盘价不合法，ATR 返回 `None`，策略按 `0.0` 处理。

### 1 分钟异常波动

当前日内 K 线与上一根日内 K 线比较：

```text
one_minute_move = abs(current.close / previous.close - 1)
```

当 `one_minute_move >= 5%`，直接 `HOLD`，原因包含 `extreme 1m move`。

### 持仓收益

只有当 `position_quote > 0` 且 `avg_entry_price` 存在时，才检查 T 仓退出。

```text
pnl_pct = current.close / avg_entry_price - 1
```

止盈阈值由层数决定：

| 当前层数 `layers` | 止盈阈值 |
|---:|---:|
| 0 或 1 | 1.8% |
| 2 | 1.4% |
| 3 及以上 | 1.0% |

## 信号优先级

`generate_signal()` 按下面顺序判断，前面的条件命中后立即返回，不再继续判断后面的条件。

### 1. 数据不足

条件：

- `len(daily) < 20`，或
- `len(intraday) < 2`

结果：

- 返回 `HOLD`
- 原因：`not enough candles`
- 置信度：`0.0`

### 2. VWAP 缺失

条件：

- 日内窗口成交量小于等于 0，导致 VWAP 无法计算

结果：

- 返回 `HOLD`
- 原因：`missing vwap`
- 置信度：`0.0`

### 3. 单根日内异常波动

条件：

- 当前收盘价相对上一根收盘价涨跌幅 `>= flash_crash_pct`
- 默认即 `>= 5%`

结果：

- 返回 `HOLD`
- 原因：`extreme 1m move: ...`
- 置信度：`0.0`

### 4. 已有 T 仓时优先处理退出

条件：

- `position_quote > 0`
- `avg_entry_price` 有值

先检查止盈：

- `pnl_pct >= dynamic_take_profit_pct(layers)`

结果：

- 返回 `SELL`
- 原因：`dynamic profit target hit: 当前收益 >= 目标收益`
- 置信度：`0.8`

再检查止损：

- `pnl_pct <= -stop_loss_pct`
- 默认即收益率 `<= -3.5%`

结果：

- 返回 `SELL`
- 原因：`t bucket stop loss hit: ...`
- 置信度：`0.7`

注意：退出检查发生在趋势破坏检查之前。也就是说，即使日 K 已经 `broken`，已有 T 仓仍然可以触发止盈或止损卖出。

### 5. 日 K 趋势破坏

条件：

- `trend == "broken"`

结果：

- 返回 `HOLD`
- 原因：`daily trend broken; pause new T buys`
- 置信度：`0.1`

这个条件只暂停新的 T 仓买入，不会阻止前一步已有仓位的止盈止损。

### 6. 强势动量买入

条件必须全部满足：

- `trend == "uptrend"`
- `position_quote <= 0`
- `has_strong_momentum(...) == True`

`has_strong_momentum` 的内部条件：

- 日内 K 线数量至少 `momentum_lookback`，默认 15。
- 最近 15 根日内 K 线的每根收盘价都满足：

```text
close >= VWAP * (1 + momentum_vwap_premium_pct)
```

默认即持续高于 VWAP 0.5%。

- 最新一根动量窗口 K 线成交量满足：

```text
latest_volume >= average(volume before momentum window) * momentum_volume_multiplier
```

默认即最新成交量至少达到窗口之前平均成交量的 1.5 倍。

结果：

- 返回 `BUY`
- 原因：`momentum follow-through above VWAP`
- 置信度：`0.55`
- 建议金额：`momentum_order_quote`，默认 175

### 7. 上升趋势回踩收复买入

先计算最近窗口：

```text
recent = intraday[-pullback_lookback:]
```

默认回看最近 24 根日内 K 线。

条件必须全部满足：

- `trend == "uptrend"`
- 最近窗口中，当前 K 线之前至少有一根收盘价满足：

```text
close <= VWAP * (1 - pullback_pct)
```

默认即低于 VWAP 0.8%。

- 当前收盘价满足：

```text
current.close >= VWAP * (1 + reclaim_buffer_pct)
```

默认即重新站上 VWAP 0.1%。

结果：

- 返回 `BUY`
- 原因基础值：`uptrend pullback reclaimed VWAP`
- 建议金额：`min_order_quote`，默认 350

置信度计算：

```text
confidence = 0.65 + min(daily_atr, 0.08)
```

如果 `daily_atr < min_daily_atr_pct`，默认低于 1.8%：

- 原因追加：`; ATR is low, size conservatively`
- 置信度再减 `0.1`

最终置信度：

```text
min(confidence, 0.9)
```

### 8. 震荡趋势深回踩收复买入

条件必须全部满足：

- `trend == "neutral"`
- `daily_atr >= min_daily_atr_pct`，默认至少 1.8%
- 最近窗口中，当前 K 线之前至少有一根收盘价满足：

```text
close <= VWAP * (1 - neutral_pullback_pct)
```

默认即低于 VWAP 1.5%。

- 当前收盘价满足：

```text
current.close >= VWAP * (1 + reclaim_buffer_pct)
```

默认即重新站上 VWAP 0.1%。

结果：

- 返回 `BUY`
- 原因：`neutral range pullback reclaimed VWAP`
- 置信度：`0.55`
- 建议金额：`min_order_quote`，默认 350

### 9. 价格仍低于 VWAP

条件：

- `trend` 是 `uptrend` 或 `neutral`
- `current.close < VWAP`

结果：

- 返回 `HOLD`
- 原因：`below VWAP; wait for reclaim`
- 置信度：`0.2`

### 10. 无设置

如果以上条件都没有命中：

- 返回 `HOLD`
- 原因：`no setup: trend=..., vwap=...`
- 置信度：`0.3`

## 回测层附加条件

策略信号不是最终成交。`run_backtest()` 会对 `BUY`/`SELL` 再做执行约束。

### T 仓现金

初始现金：

```text
cash = starting_quote * (1 - core_allocation_pct)
```

默认 `starting_quote=3500`、`core_allocation_pct=0.70` 时，T 仓初始现金为 1050。

核心仓不参与交易，也不会进入 `cash`。

### 合成盘口点差

回测用当前收盘价构造合成盘口：

```text
best_bid = close * (1 - synthetic_spread_bps / 20000)
best_ask = close * (1 + synthetic_spread_bps / 20000)
spread_pct = (best_ask - best_bid) / mid
```

默认：

- `synthetic_spread_bps = 20`
- `max_spread_pct = 0.005`

当 `spread_pct > max_spread_pct`：

- 不成交
- 记录 `risk_event.type = spread_rejected`
- `reason` 来自 `market_quality_allows_trade()`，例如 `spread too wide: ...`

### 买入间隔

重复买入必须满足：

```text
last_buy_price is None
or current.close <= last_buy_price * (1 - buy_grid_spacing_pct)
```

默认即当前收盘价必须比上一次买入价低至少 1.2%。

不满足时：

- 不成交
- 记录 `risk_event.type = grid_spacing_rejected`

### 买入成交

当 `signal.action == "BUY"` 时，先记录一个订单意图：

| 字段 | 当前值 |
|---|---|
| `sourceSignal` | 没有持仓层时为 `OPEN_T`，已有层数时为 `ADD_T` |
| `side` | `buy` |
| `quoteAmount` | 策略建议金额，通常 350 或 175 |
| `strategyId` | `t-vwap` |
| `paperOnly` | `true` |

成交需要同时满足：

- 点差检查通过
- `cash >= signal.suggested_quote`
- 买入间隔检查通过
- `quote = min(signal.suggested_quote, cash, max_t_bucket - t_value)` 后仍然 `>= signal.suggested_quote`

买入成交价：

```text
fill_price = current.close * (1 + slippage_bps / 10000)
```

默认 `slippage_bps=30`，即买入按当前收盘价上浮 0.3%。

手续费：

- 名义金额 `< 350`，手续费固定 `0.35`
- 名义金额 `>= 350`，手续费为 `notional * 0.001`

买入数量：

```text
qty = (quote - fee) / fill_price
```

成交后：

- `cash -= quote`
- `t_qty += qty`
- `t_cost += quote`
- `layers += 1`
- `last_buy_price = fill_price`
- 记录一条 `Trade("BUY", ...)`

### 卖出成交

当 `signal.action == "SELL"` 时，先记录一个订单意图：

| 字段 | 当前值 |
|---|---|
| `sourceSignal` | `CLOSE_T` |
| `side` | `sell` |
| `quantity` | 当前全部 T 仓数量 |
| `reduceOnly` | `true` |
| `paperOnly` | `true` |

成交需要同时满足：

- 点差检查通过
- `t_qty > 0`

卖出成交价：

```text
fill_price = current.close * (1 - slippage_bps / 10000)
```

默认 `slippage_bps=30`，即卖出按当前收盘价下浮 0.3%。

卖出金额与收益：

```text
gross = t_qty * fill_price
fee = fee_model.estimate(gross)
pnl = gross - fee - t_cost
```

成交后：

- `cash += gross - fee`
- `trade_pnls.append(pnl)`
- 记录一条 `Trade("SELL", ...)`
- `t_qty = 0`
- `t_cost = 0`
- `last_buy_price = None`
- `layers = 0`

当前实现每次卖出都是清空 T 仓，不做部分止盈。

## 回测结果字段

`BacktestResult` 会记录：

| 字段 | 说明 |
|---|---|
| `starting_quote` | 初始总资金 |
| `ending_equity` | 回测结束时 T 仓现金 + T 仓市值 |
| `cash` | T 仓剩余现金 |
| `t_position_qty` | 剩余 T 仓数量 |
| `t_position_value` | 剩余 T 仓按最后价格估值 |
| `max_t_position_quote` | 回测期间最大 T 仓市值 |
| `fees_paid` | 累计手续费 |
| `trades` | 实际成交记录 |
| `equity_curve` | 每根日内 K 线后的 T 仓权益曲线 |
| `trade_pnls` | 每次卖出对应的 PnL |
| `result_id` | 基于配置快照 hash 的本地结果 id |
| `engine_version` | 当前为 `tradebot-backtest-v2` |
| `config_snapshot` | 回测参数与策略参数快照 |
| `execution_assumptions` | 成交时点、手续费模型、paper-only 等假设 |
| `order_intents` | 每次可执行信号对应的标准订单意图 |
| `risk_events` | 信号被执行层拒绝的原因 |

## 当前未实现或需要特别注意的点

- 策略没有实盘下单能力。
- 策略没有做空、反手、杠杆、限价单、追踪止损。
- 策略没有多策略运行时；当前核心信号函数是 `generate_signal()`。
- `max_t_bucket_pct` 在配置中存在，但当前回测 T 仓额度由 `core_allocation_pct` 反推。
- 当前回测成交时点是当前建模 K 线收盘价，不是下一根开盘价。
- 当前回测没有独立 warmup 截断；只通过最小日 K 和日内 K 数量保护。
- 当前 HTTP 回测接口会调用所选 `DataSourceFactory` 数据源；CSV 数据源本身要求 `daily_path` 和 `intraday_path`，但 HTTP 接口目前没有暴露这两个路径参数。
