# Trade T Strategy

本文档分两部分：
1. **策略完整设计框架**（目标状态，部分尚未在代码中实现）
2. **当前代码实现口径**（以 `tradebot/strategy.py`、`tradebot/indicators.py` 和 `tradebot/backtest.py` 为准）

---

## 一、策略完整设计框架

### 策略目标

“核心仓长持 + T 仓做T + 趋势走坏逐步清仓”，适用于具有明确趋势特征的高流动性股票/标的。

核心思路：
- **核心仓**吃趋势主升浪，长期持有不频繁操作
- **T 仓**在日内用”低买高卖”降低整体持仓成本，或锁定日内利润
- 趋势破坏时**分批减仓**，不赌反弹，等明确反转信号再重建

### 四层过滤体系

信号的产生从大周期到小周期逐层过滤，每一层都必须通过才进入下一层。

#### 第一层：日线趋势状态（决定总体仓位方向）

```
上升趋势 = 收盘价 > MA5 > MA10 > MA20
震荡趋势 = 不满足上升，且未跌破 MA20
破位趋势 = 收盘价 < MA10，或 MA5 死叉 MA10
```

| 趋势状态 | 核心仓 | T 仓 |
|---|---|---|
| 上升趋势 | 满仓持有 | 可满额做 T |
| 震荡趋势 | 满仓持有 | T 仓减半，只做深回踩 |
| 破位趋势 | 按”逐步清仓计划”减仓 | 停止新 T，已有 T 仓正常止盈止损出场 |

#### 第二层：日内 VWAP 锚点（决定当天能否做 T）

VWAP（成交量加权平均价）是机构资金的当日平均成本线：
- 价格在 VWAP 之上 → 多头主导，可做 T 买入
- 价格在 VWAP 之下 → 空头主导，等待 VWAP 收复后再买

#### 第三层：KDJ + 成交量过滤（决定入场时机质量）

买入条件须全部满足：
1. 日线趋势 = 上升
2. 日内价格回踩至 VWAP ± 0.8% 附近（有回踩行为）
3. KDJ 的 J 值 < 70（未处于超买区域，避免追高）
4. 最新 K 线成交量 ≥ 前 15 根均量的 1.5 倍（有资金入场确认）
5. 当前收盘重新站回 VWAP（收复确认，上方有缓冲 0.1%）

#### 第四层：动态止盈止损（基于 ATR，而非固定百分比）

```
止损 = 平均成本 × (1 - 1.5 × ATR%)   ← 随波动率自适应
止盈：
  1 层 T 仓：成本 × (1 + 1.8%)
  2 层 T 仓：成本 × (1 + 1.4%)
  3 层及以上：成本 × (1 + 1.0%)
```

ATR 动态止损的优势：波动大的股票不容易被随机噪音洗出，波动小的股票止损更收紧。

### 日内两种 T 仓形态

**形态 A：回踩收复 VWAP（最易量化，核心信号）**

价格在 VWAP 下方回踩后重新站上 VWAP，说明多头守住了成本线。这是现有代码的主要入场逻辑。

**形态 B：低开冲高（开盘形态）**

开盘低于前日收盘（受压低开），随后价格快速拉升收复 VWAP。
本质上与”形态 A 回踩收复”一致，区别在于回踩发生在开盘阶段，信号往往在开盘后 30 分钟内出现。

**不做 T 的情形：冲高回落**

价格快速冲到日内高点后开始回落，此时 MACD 5 分钟柱状图面积缩短，KDJ J 值超过 80。
这不是买入 T 仓的时机，而是**卖出 T 仓**或等待回踩完成后再入场。

### 逐步清仓计划（趋势走坏时核心仓减仓）

分四个触发档位，不一次性清仓：

| 触发信号 | 核心仓操作 | 累计剩余仓位 |
|---|---|---|
| MA5 首次死叉 MA10，且日内成交量放大 | 减 25% 核心仓 | 75% |
| 收盘价跌破 MA20 | 再减 25% | 50% |
| 日线 MACD 死叉 + 价格连续 2 日收于 MA20 下方 | 再减 25% | 25% |
| 连续 3 根日 K 收盘均低于 MA20 | 清空剩余底仓 | 0% |

重建仓位触发条件：收盘价重新站上 MA5，且 MA5 > MA10，且成交量放大。

---

## 二、当前代码实现口径

以下内容以代码实际实现为准。所有”计划中”或”待实现”的特性会在”当前差距”一节单独列出。

### 策略目标（当前实现）

Trade 当前策略是”核心仓 + T 仓”的研究型回测策略。

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
| `max_layers` | 3 | 最大 T 仓加仓层数；达到后拒绝继续买入 |

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

策略使用 `session_vwap(intraday)`。它只使用最新 K 线所在交易日 session 内的日内 K 线，默认以 UTC 08:00 作为 session 重置点。

```text
VWAP = sum(quote_volume) / sum(volume)
```

这样可以避免跨日 intraday 窗口把前一交易日价格和成交量带入当前日 VWAP。若后续接入具体市场，应按市场时区和交易时段调整 reset hour。

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

回测在信号 K 线收盘后生成信号，并在下一根 K 线开盘价上建模成交。合成盘口也围绕下一根 K 线开盘价构造：

```text
best_bid = next_bar.open * (1 - synthetic_spread_bps / 20000)
best_ask = next_bar.open * (1 + synthetic_spread_bps / 20000)
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
or next_bar.open <= last_buy_price * (1 - buy_grid_spacing_pct)
```

默认即下一根 K 线开盘价必须比上一次买入成交价低至少 1.2%。

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
fill_price = next_bar.open * (1 + slippage_bps / 10000)
```

默认 `slippage_bps=30`，即买入按下一根 K 线开盘价上浮 0.3%。

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
fill_price = next_bar.open * (1 - slippage_bps / 10000)
```

默认 `slippage_bps=30`，即卖出按下一根 K 线开盘价下浮 0.3%。

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
| `core_only_return_pct` | 核心仓只买入持有的 close-to-close 回报 |
| `strategy_vs_core_only_alpha` | T 仓回测收益相对核心仓只持有基准的差值 |

## 当前代码与完整设计框架的差距

### 已补齐的设计项

这些项已经进入当前代码实现：

| 项目 | 当前实现 |
|---|---|
| **ATR 动态止损** | `generate_signal()` 使用 `atr_stop_loss_multiplier` 计算有效止损；ATR 不可用时回退 `stop_loss_pct`。 |
| **置信度驱动仓位** | BUY 信号通过 `confidence_sized_quote()` 按 confidence 和 `confidence_size_floor` 缩放建议金额。 |
| **逐步清仓触发器** | `core_position_signal()` 支持 MA 死叉、跌破 MA20、MACD 弱势和灾难性单日下跌等核心仓减仓信号。 |
| **KDJ 入场过滤** | `kdj()` 已实现，`generate_signal()` 会在 J 值高于阈值时阻断新 BUY。 |
| **MACD 日线过滤** | `macd()` 已实现，`require_macd_histogram_positive=True` 时日线 histogram 为负会阻断新 BUY。 |
| **隔夜跳空保护** | `generate_signal()` 会在最新 intraday 相对最近 daily close 出现超阈值缺口时暂停新 T 买入。 |
| **手工 paper 风控** | `POST /api/paper/orders` 已接入单笔金额、T 仓上限、点差、行情新鲜度、日内亏损、网格间距和最大层数检查。 |

### 已补齐（本轮新增）

| 项目 | 当前实现 |
|---|---|
| **逐步清仓触发器** | `core_position_signal()` 4 层触发：MA5 死叉→75% 减仓、+跌破 MA20→50%、+MACD 死叉→25%、连续 3 日破 MA20→清仓；单日跌幅 ≥ 8% 触发 EXIT_ALL。 |
| **隔夜跳空保护** | `generate_signal()` 检测 intraday 首 K 与上一根日线收盘价的缺口；缺口 ≥ `overnight_gap_pct`（默认 4%）时暂停 T 仓新买入。 |
| **固定初始每股资金** | `POST /api/backtest/run` 支持 `perSymbolQuote` 或 `totalQuote + idleBufferPct` 指定固定分配；T 仓始终为初始额度的 30%。 |
| **滚动窗口回测** | `run_walk_forward()` 对长时序日线数据做滚动分割（默认 in_sample=40 日、oos=20 日、step=10 日），`POST /api/backtest/walkforward` 可直接对接 Binance B-Stock 历史数据。 |
| **币安历史数据拉取** | `fetch_klines_range()` 分页获取 Binance API 任意时间段 K 线；`BinanceHistoricalDataSource` 封装日期范围接口，支持 SPCXBUSDT 等 B-Stock。 |

### 仍有差距

| 差距 | 说明 |
|---|---|
| **冲高回落形态未实现** | 当前策略没有"MACD 柱状图缩短 + KDJ 超买"的组合卖出信号，只有固定止盈止损。 |
| **开盘形态（低开冲高）未单独标注** | 形态 B 与当前"回踩收复 VWAP"信号本质相同，但缺少开盘阶段的专项识别。 |
| **组合相关性矩阵** | 多标的同向配置（科技股集中）时，单次系统性下跌可能同时打穿多个止损；待 IBKR Paper 接入后实现，参见 `docs/ibkr_integration.md`。 |
| **真实盘口与部分成交未建模** | 回测和 paper 仍没有真实 order book、排队、部分成交和真实 broker 回报。 |

### 其他边界（当前设计范围内不解决）

- 策略没有实盘下单能力，不是实盘机器人。
- 策略没有做空、反手、杠杆、限价单、追踪止损。
- `max_t_bucket_pct` 在配置中存在，但当前回测 T 仓额度由 `core_allocation_pct` 反推。
- 当前回测成交时点是下一根 K 线开盘价；不建模真实 order book、排队、部分成交。
