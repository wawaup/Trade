# Trade Signal And Execution Contract

本文档描述当前 Trade 后端已经实现的执行契约。它覆盖数据源选择、K 线响应、回测执行、订单意图、风险事件、paper 订单、结果持久化和当前边界。

## 总原则

- 当前系统是研究和 paper-only 平台，不是实盘交易机器人。
- 策略函数只产生 `BUY`、`SELL`、`HOLD` 信号，不直接修改账户、不直接访问交易所、不决定最终成交价。
- 回测层负责把策略信号转换成订单意图、执行约束、成交记录和风险事件。
- paper 订单接口只模拟现金和仓位变化，不触碰真实交易所 API。
- 当前没有 live adapter；任何未来实盘 adapter 都必须显式增加环境开关、凭证隔离、审计和风控。

## 数据源契约

当前数据源入口是 `DataSourceFactory`。

| Source | 别名 | 数据来源 | 网络 | 当前用途 |
|---|---|---|---|---|
| `Synthetic` | `synthetic`, `mock`, `demo` | `generate_synthetic_spcx()` 合成 K 线 | 否 | 默认图表、默认回测、测试 |
| `CSV` | `csv`, `file` | 本地 CSV 文件 | 否 | 代码路径可用；HTTP 回测尚未暴露路径参数 |
| `Binance` | `binance`, `spot` | Binance public spot klines | 是 | 需要网络；沙盒或网络不可用时可能失败 |

### Candle 接口

所有数据源都必须返回：

```text
(daily: list[Candle], intraday: list[Candle])
```

`Candle` 字段：

| 字段 | 类型 | 用途 |
|---|---|---|
| `open_time` | int ms | 信号、成交、风险事件时间 |
| `open` | float | 当前策略不直接使用，但用于 K 线响应 |
| `high` | float | ATR |
| `low` | float | ATR |
| `close` | float | 趋势、VWAP 条件、止盈止损、成交建模 |
| `volume` | float | VWAP 分母、动量成交量 |
| `close_time` | int ms | 当前策略不直接使用 |
| `quote_volume` | float | VWAP 分子 |
| `trades` | int | 当前策略不直接使用 |

## HTTP API

### `GET /api/data-sources`

返回可用 source 列表。

响应形状：

```json
{
  "sources": [
    {"id": "Synthetic", "label": "Synthetic demo data", "requiresNetwork": false},
    {"id": "CSV", "label": "Local CSV files", "requiresNetwork": false},
    {"id": "Binance", "label": "Binance public klines", "requiresNetwork": true}
  ]
}
```

### `GET /api/klines?symbol=&resolution=&source=`

用途：给前端图表返回 Lightweight Charts 友好的 K 线数组和 VWAP 数组。

当前行为：

- `source` 默认 `Synthetic`。
- source 未知时返回 `400`。
- 数据源读取失败时返回 `502`。
- 当前只使用数据源返回的 `intraday`，取最后 180 根。
- `resolution` 当前只回传给响应；没有改变数据源 interval。Binance 数据源内部仍固定请求 `1m` intraday。

响应字段：

| 字段 | 说明 |
|---|---|
| `symbol` | 请求标的 |
| `resolution` | 请求周期文本 |
| `source` | 归一化后的 source |
| `dataStatus` | `Synthetic` 为 `demo`，其他 source 为 `live` |
| `candles` | `[time_seconds, open, high, low, close]` |
| `vwap` | `[time_seconds, value]` |

VWAP 计算：

```text
cumulative_quote += candle.quote_volume
cumulative_volume += candle.volume
vwap = cumulative_quote / cumulative_volume
```

### `POST /api/backtest/run`

请求字段：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `symbols` | `[]` | 空数组表示使用内置全部标的；非空时只跑这些 symbol |
| `source` | `Synthetic` | 数据源；未知 source 返回 `400` |
| `slippageBps` | 30 | 回测买卖滑点 bps |
| `spreadBps` | 20 | 回测合成点差 bps |

内置标的主题：

| Symbol | Theme |
|---|---|
| `SPCX` | 商业航天/星链 |
| `TSLA` | 科技巨头/高流动性 |
| `NVDA` | AI芯片/高流动性 |
| `MU` | 存储周期 |
| `CPOX` | CPO光通信 |

如果请求的 symbol 不在内置列表中，系统会按 `自定义标的` 主题运行。

当前资金分配：

```text
per_asset_quote = 15000 / len(selected_symbols)
BacktestConfig.starting_quote = per_asset_quote
BacktestConfig.core_allocation_pct = 0.70
```

每个 symbol 都会通过：

```text
DataSourceFactory.get_source(source).get_default_candles(symbol=symbol)
```

读取 `daily` 和 `intraday`，然后调用：

```text
run_backtest(daily, intraday, BacktestConfig(...), StrategyConfig())
```

如果数据源读取某个 symbol 失败，接口返回：

```json
{"error": "...", "source": "...", "symbol": "..."}
```

HTTP 状态为 `502`。

### `GET /api/backtest/results`

返回最近 20 条回测结果。

当前持久化路径：

```text
data/backtest_results.json
```

存储格式是 JSON 数组。写入时先写同目录临时文件，再 replace 成目标文件。

### `GET /api/backtest/results/<id>`

按 `resultId` 返回完整回测结果。

找不到时：

```json
{"error": "backtest result not found: <id>"}
```

HTTP 状态为 `404`。

### `GET /api/paper/orders`

返回当前 paper 账户和订单日志。

响应字段：

| 字段 | 说明 |
|---|---|
| `account.paperOnly` | 恒为 `true` |
| `account.cash` | 当前 paper 现金 |
| `account.positions` | symbol 到数量的映射 |
| `orders` | 手工 paper 订单日志 |

### `POST /api/paper/orders`

用途：快速 paper 下单，不是策略自动执行。

请求字段：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `symbol` | `NVDA` | 标的，转大写 |
| `side` | 必填 | `buy` 或 `sell` |
| `quoteAmount` | 0 | 买入名义金额；卖出时当前实现也用它推导卖出数量 |
| `orderType` | `market` | 只支持 `market` |
| `price` | 最新合成价 | 手工指定成交价格；不传时使用 `_latest_synthetic_price(symbol)` |

输入拒绝：

- `side` 不是 `buy`/`sell`，返回 `400`。
- `orderType` 不是 `market`，返回 `400`。
- JSON 或数值解析失败，返回 `400`。

当前手工 paper 映射：

| side | sourceSignal | reduceOnly | quoteAmount | quantity |
|---|---|---:|---:|---:|
| `buy` | `OPEN_T` | false | 请求 `quoteAmount` | 0 |
| `sell` | `REDUCE_T` | true | 0 | `quoteAmount / price` |

手续费：

```text
fee = max(0, quoteAmount * 0.001)
```

注意：这里的手工 paper fee 是 server helper 的简单 0.1% 模型，不是 `BinanceStockFeeModel` 的固定费/百分比切换模型。

### `POST /api/paper/reset`

用途：重置 paper 账户。

请求字段：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `cash` | 10000 | 重置后的 paper 现金 |

行为：

- `cash < 0` 返回 `400`。
- 清空 `PAPER_ACCOUNT.positions`。
- 清空 `PAPER_ORDER_LOG`。

## 信号词汇

策略层当前只返回：

| 策略信号 | 来源 | 含义 |
|---|---|---|
| `BUY` | `generate_signal()` | 有买入条件 |
| `SELL` | `generate_signal()` | 有退出条件 |
| `HOLD` | `generate_signal()` | 不交易 |

执行契约中的标准信号：

| 标准信号 | 当前产生位置 | 含义 |
|---|---|---|
| `OPEN_T` | 回测中 `BUY` 且 `layers <= 0`；手工 paper buy | 开第一层 T 仓 |
| `ADD_T` | 回测中 `BUY` 且 `layers > 0` | 增加 T 仓层 |
| `REDUCE_T` | 手工 paper sell | 减少 T 仓 |
| `CLOSE_T` | 回测中 `SELL` | 清空 T 仓 |
| `PAUSE` | 文档保留词；当前代码没有显式产出 | 显式暂停 |

## 回测执行模型

### 成交时点

当前 `executionAssumptions.fillTiming` 是：

```text
modeled_current_close
```

含义：信号用当前日内窗口算出后，成交按当前 K 线收盘价建模，再叠加滑点。

当前不是：

- 下一根开盘成交
- 真实 order book 成交
- limit maker 成交
- 分笔撮合

### 买入价格

```text
fill_price = current.close * (1 + slippage_bps / 10000)
```

默认 `slippage_bps=30`，即买入价比当前收盘价高 0.3%。

### 卖出价格

```text
fill_price = current.close * (1 - slippage_bps / 10000)
```

默认 `slippage_bps=30`，即卖出价比当前收盘价低 0.3%。

### 合成点差

回测中没有真实盘口时，用当前收盘价构造：

```text
best_bid = close * (1 - synthetic_spread_bps / 20000)
best_ask = close * (1 + synthetic_spread_bps / 20000)
mid = (best_bid + best_ask) / 2
spread_pct = (best_ask - best_bid) / mid
```

默认：

- `synthetic_spread_bps=20`
- `max_spread_pct=0.005`

当 `spread_pct > max_spread_pct`，交易被拒绝。

### 回测手续费

回测使用 `BinanceStockFeeModel`。

| 名义金额 | 手续费 |
|---:|---:|
| `notional <= 0` | 0 |
| `0 < notional < 350` | 0.35 |
| `notional >= 350` | `notional * 0.001` |

### 回测买入必须满足的条件

策略返回 `BUY` 后，回测层会先记录 `orderIntent`，再判断是否成交。

成交必须同时满足：

- 点差检查通过。
- `cash >= signal.suggested_quote`。
- 买入间隔通过：

```text
last_buy_price is None
or current.close <= last_buy_price * (1 - buy_grid_spacing_pct)
```

- T 仓额度通过：

```text
quote = min(signal.suggested_quote, cash, max_t_bucket - t_value)
quote >= signal.suggested_quote
```

其中：

```text
max_t_bucket = initial_t_cash = starting_quote * (1 - core_allocation_pct)
t_value = current_t_qty * current.close
```

### 回测卖出必须满足的条件

策略返回 `SELL` 后，回测层会先记录 `orderIntent`，再判断是否成交。

成交必须同时满足：

- 点差检查通过。
- `t_qty > 0`。

当前回测卖出是全平 T 仓，不支持部分减仓。

## Order Intent

回测中的 `orderIntents` 是普通 dict，字段如下：

| 字段 | 含义 |
|---|---|
| `timestamp` | 当前 K 线 `open_time` |
| `symbol` | 当前固定为 `T_BUCKET` |
| `side` | `buy` 或 `sell` |
| `quoteAmount` | 买入名义金额；卖出为 0 |
| `quantity` | 卖出数量；买入为 0 |
| `orderType` | 当前固定 `market` |
| `sourceSignal` | `OPEN_T` / `ADD_T` / `CLOSE_T` |
| `strategyId` | 当前固定 `t-vwap` |
| `reduceOnly` | 卖出为 true |
| `paperOnly` | true |
| `reason` | 策略信号原因 |

手工 paper 订单使用 `OrderIntent` dataclass，字段：

| 字段 | 含义 |
|---|---|
| `symbol` | 标的 |
| `side` | `buy` 或 `sell` |
| `quote_amount` | 买入名义金额 |
| `quantity` | 卖出数量 |
| `order_type` | 当前只支持 `market` |
| `source_signal` | `OPEN_T` 或 `REDUCE_T` |
| `strategy_id` | 当前为 `quick-paper-t` |
| `reduce_only` | 卖出为 true |
| `paper_only` | true |
| `risk_metadata` | 当前保留字段，server 手工订单暂未填充 |

## Risk Events

回测层当前记录这些风险事件：

| type | 触发条件 | reason |
|---|---|---|
| `spread_rejected` | 点差检查失败 | `invalid book` 或 `spread too wide: ...` |
| `cash_rejected` | `cash < signal.suggested_quote` | `insufficient cash` |
| `grid_spacing_rejected` | 当前价格未比上次买入价低足够比例 | `buy grid spacing not reached` |
| `t_bucket_cap_rejected` | T 仓额度不足以覆盖完整建议买入金额 | `T bucket cap exceeded` |
| `position_rejected` | 策略给出 SELL 但没有 T 仓 | `no T position` |

风险事件字段：

| 字段 | 含义 |
|---|---|
| `timestamp` | 当前 K 线 `open_time` |
| `type` | 风险事件类型 |
| `reason` | 执行层拒绝原因 |
| `signalReason` | 原始策略信号原因 |

## Paper Execution Adapter

`PaperExecutionAdapter` 是本地模拟成交器。

### 通用拒绝

所有订单先检查：

- `price` 或 `fee` 无法转成数字：拒绝，`invalid price or fee`。
- `price <= 0`：拒绝，`invalid price`。

### 可选风险限制

`RiskLimits` 支持：

| 字段 | 条件 | 拒绝原因 |
|---|---|---|
| `max_spread_pct` | `spread_pct > max_spread_pct` | `spread exceeds max` |
| `market_data_max_age_sec` | `market_data_age_sec > market_data_max_age_sec` | `stale market data` |
| `daily_loss_limit_quote` | `daily_loss_quote > daily_loss_limit_quote` | `daily loss cap breached` |
| `max_order_quote` | buy quote 超过上限 | `max order quote exceeded` |
| `max_t_position_quote` | 当前持仓市值 + 买入金额超过上限 | `T bucket cap exceeded` |

如果对应输入参数是 `None`，该项不参与检查。

### Paper 买入

买入拒绝：

- `quote_amount <= 0`：`buy quote amount must be positive`
- 超过 `max_order_quote`
- 超过 `max_t_position_quote`
- `account.cash < quote + fee`：`insufficient cash`

买入成交：

```text
qty = max(0, (quote - fee) / price)
account.cash -= quote + fee
account.positions[symbol] += qty
```

返回：

```text
FillSnapshot(symbol, "buy", qty, price, fee, "filled")
```

注意：这里现金扣减是 `quote + fee`，但数量计算是 `(quote - fee) / price`。也就是说 fee 同时减少买入数量并额外从 cash 中扣除，这是当前实现口径，后续若要和真实交易所严格对齐，需要单独修正并补回归测试。

### Paper 卖出

卖出数量：

```text
held = account.positions.get(symbol, 0)
qty = intent.quantity or held
```

卖出拒绝：

- `qty <= 0`：`sell quantity must be positive`
- `reduce_only` 且请求数量大于持仓时，会自动截断为 `held`
- 截断后 `qty <= 0`：`no position to reduce`

卖出成交：

```text
gross = qty * price
account.cash += gross - fee
remaining = held - qty
```

如果 `remaining > 0`，更新剩余仓位；否则删除该 symbol 仓位。

返回：

```text
FillSnapshot(symbol, "sell", qty, price, fee, "filled")
```

## 持久化结果字段

`serialize_backtest_result()` 写入这些字段：

| 字段 | 说明 |
|---|---|
| `resultId` | server 生成的 `bt-0001` 风格 id |
| `createdAt` | UTC ISO 时间 |
| `engineVersion` | 来自 `BacktestResult.engine_version`，当前为 `tradebot-backtest-v2` |
| `source` | 本次回测使用的数据源 |
| `symbols` | 本次回测标的 |
| `configSnapshot` | 回测参数和策略参数 |
| `executionAssumptions` | 成交假设 |
| `summary` | 聚合指标 |
| `assets` | 每个标的的指标行 |
| `equityCurve` | `{index, value}` 数组 |
| `trades` | 成交记录 |
| `tradePnls` | 每次卖出的 PnL |
| `orderIntents` | 标准订单意图 |
| `riskEvents` | 风险拒绝事件 |

聚合 summary 当前包含：

| 字段 | 说明 |
|---|---|
| `totalReturnPct` | 聚合收益率 |
| `maxDrawdownPct` | 聚合最大回撤 |
| `winRate` | 聚合交易胜率 |
| `profitFactor` | 聚合盈亏比 |
| `globalTExposureCap` | 所有标的最大 T 仓市值之和，与 `15000 * 0.30` 取较小值 |

## 当前边界和风险

- 当前没有真实交易所下单。
- 当前没有 pending order worker。
- 当前没有用户、权限、token、审计。
- 当前没有真实 bid/ask 接入；回测点差是合成点差。
- 当前 HTTP `source=CSV` 会进入 `CSVDataSource`，但没有传 `daily_path` 和 `intraday_path` 的 API 字段，因此会返回错误。
- 当前 `source=Binance` 需要网络访问，网络不可用或 Binance 不支持该 symbol 时会返回错误。
- 当前回测只持久化第一条资产结果的详细 `trades/equityCurve/orderIntents/riskEvents`，但 `assets/summary/symbols` 覆盖本次多标的聚合。
- 当前 paper 手工订单没有应用 `max_spread_pct`、`market_data_max_age_sec`、`daily_loss_limit_quote`，因为 server 调用 `PaperExecutionAdapter` 时没有传这些运行时风险参数；这些能力在 adapter 层已经存在。
- 当前系统所有 live 文案都应理解为“模拟/展示状态”，不是实盘账户状态。
