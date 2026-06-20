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
| `CSV` | `csv`, `file` | 本地 CSV 文件 | 否 | 可通过 HTTP `dailyPath` / `intradayPath` 传入本地文件路径 |
| `Binance` | `binance`, `spot` | Binance public spot klines | 是 | 需要网络；沙盒或网络不可用时可能失败 |

### 数据源与标的匹配说明

**主要标的**：`SPCXB/USDT`，即 Binance B-Stock 代币化股票，Binance API 可直接获取其 K 线数据。

**什么是 B-Stock（代币化股票）**：Binance 将部分美股（如 SPCX）以 1:1 方式发行成链上代币（symbol 加 B 后缀），用户可在币安直接用 USDT 交易，无需开设美股账户。价格锚定底层美股，由持牌托管商（CM-Equity）保证 1:1 备兑。

当前各数据源实际可用范围：

| 数据源 | 可用标的范围 | 说明 |
|---|---|---|
| `Binance` | `SPCXB/USDT` 及其他 B-Stock、主流加密货币 | **主力数据源**，可直接获取真实 SPCXB K 线 |
| `CSV` | 用户自行提供的任意市场数据 | 导入历史数据、离线回测 |
| `Synthetic` | 任意 symbol（合成数据） | 验证代码逻辑，参数无真实市场意义 |

### B-Stock 与普通美股的关键差异

使用 Binance B-Stock 数据时，以下特性与直接交易美股不同，会影响策略参数：

| 特性 | 普通美股 | Binance B-Stock（SPCXB） |
|---|---|---|
| 交易时段 | 仅美东时间 9:30~16:00 | 币安平台交易时间（可能更长但流动性集中在美股时段） |
| VWAP session 重置 | 按美东时区开盘重置 | 当前代码以 UTC 08:00 重置，需确认是否符合 SPCXB 活跃时段 |
| 流动性 | 纽交所/纳斯达克深度 | 币安盘口深度，通常低于底层市场 |
| 价格跟踪 | 直接定价 | 锚定底层股票，可能有轻微溢价/折价 |
| 交易手续费 | 券商佣金 | 币安现货手续费（默认 0.1%，VIP 或 BNB 抵扣可更低） |
| 停牌/分红 | 依美股规则 | Binance 会公告停牌或调整，行为可能与底层股票有延迟 |

**VWAP 重置时间注意**：当前代码 `session_vwap()` 以 UTC 08:00 作为每日 session 重置点。SPCXB 在美股开盘前（UTC 14:30）流动性极低，建议将 reset hour 调整为 **UTC 13:30**（美东 9:30 开盘时间），使 VWAP 真正反映当天美股活跃时段的成交均价。

后续接入 IBKR（美股直连）的推荐路径：
- 新增 `IBKR` DataSource 适配器，接入真实美股行情和历史 K 线
- 届时 SPCX（无 B 后缀）的 VWAP session 应按 NYSE 交易时段重置

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

### `GET /api/klines?symbol=&resolution=&source=&dailyPath=&intradayPath=`

用途：给前端图表返回 Lightweight Charts 友好的 K 线数组和 VWAP 数组。

当前行为：

- `source` 默认 `Synthetic`。
- source 未知时返回 `400`。
- 数据源读取失败时返回 `502`。
- 当前只使用数据源返回的 `intraday`，取最后 180 根。
- `resolution` 当前只回传给响应；没有改变数据源 interval。Binance 数据源内部仍固定请求 `1m` intraday。
- `source=CSV` 时需要 `dailyPath` 和 `intradayPath`。

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
| `dailyPath` | 无 | `source=CSV` 时的 daily CSV 路径 |
| `intradayPath` | 无 | `source=CSV` 时的 intraday CSV 路径 |
| `slippageBps` | 30 | 回测买卖滑点 bps |
| `spreadBps` | 20 | 回测合成点差 bps |

内置标的主题：

| Symbol | 实际可交易形式 | Theme |
|---|---|---|
| `SPCX` | 币安 B-Stock：`SPCXB/USDT` | 商业航天/星链 |
| `TSLA` | 币安 B-Stock：`TSLABUSDT`；或 IBKR 美股 | 科技巨头/高流动性 |
| `NVDA` | IBKR 美股（B-Stock 可用性以币安公告为准） | AI芯片/高流动性 |
| `MU` | IBKR 美股 | 存储周期 |
| `CPOX` | IBKR 美股 | CPO光通信 |

**当前主力标的**：`SPCXB`（SPCX 的币安 B-Stock 版本），通过 `source=Binance` + `symbol=SPCXB` 可获取真实 K 线数据。

如果请求的 symbol 不在内置列表中，系统会按 `自定义标的` 主题运行。

当前资金分配：

```text
per_asset_quote = 15000 / len(selected_symbols)
BacktestConfig.starting_quote = per_asset_quote
BacktestConfig.core_allocation_pct = 0.70
```

每个 symbol 都会通过：

```text
DataSourceFactory.get_source(source).get_default_candles(symbol=symbol, daily_path=dailyPath, intraday_path=intradayPath)
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
next_bar_open
```

含义：信号用 bar N 收盘后可见的数据生成，成交按 bar N+1 开盘价建模，再叠加滑点。

当前不是：

- 真实 order book 成交
- limit maker 成交
- 分笔撮合

### 买入价格

```text
fill_price = next_bar.open * (1 + slippage_bps / 10000)
```

默认 `slippage_bps=30`，即买入价比下一根 K 线开盘价高 0.3%。

### 卖出价格

```text
fill_price = next_bar.open * (1 - slippage_bps / 10000)
```

默认 `slippage_bps=30`，即卖出价比下一根 K 线开盘价低 0.3%。

### 合成点差

回测中没有真实盘口时，用下一根 K 线开盘价构造：

```text
best_bid = next_bar.open * (1 - synthetic_spread_bps / 20000)
best_ask = next_bar.open * (1 + synthetic_spread_bps / 20000)
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
or next_bar.open <= last_buy_price * (1 - buy_grid_spacing_pct)
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
| `max_layers_rejected` | 当前 T 仓层数已经达到 `StrategyConfig.max_layers` | `max layers reached` |
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
account.cash -= quote
account.positions[symbol] += qty
```

返回：

```text
FillSnapshot(symbol, "buy", qty, price, fee, "filled")
```

这里采用“名义金额内扣 fee”的口径：现金只减少 `quote`，fee 通过减少买入数量体现。

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
| `coreOnlyReturnPct` | 第一根 daily close 到最后一根 daily close 的核心仓只持有回报 |
| `strategyVsCoreOnlyAlpha` | T 仓策略收益相对核心仓只持有基准的差值 |
| `assetDetails` | 多标的完整审计明细，每个标的包含 equityCurve、trades、orderIntents、riskEvents |

聚合 summary 当前包含：

| 字段 | 说明 |
|---|---|
| `totalReturnPct` | 聚合收益率 |
| `maxDrawdownPct` | 聚合最大回撤 |
| `winRate` | 聚合交易胜率 |
| `profitFactor` | 聚合盈亏比 |
| `globalTExposureCap` | 所有标的最大 T 仓市值之和，与 `15000 * 0.30` 取较小值 |

## 当前边界和风险

### 执行层边界

- 当前没有真实交易所下单，全部为 paper-only 模拟。
- 当前没有 pending order worker；所有订单同步处理，不排队。
- 当前没有用户、权限、token、审计日志；不能多用户隔离。
- 当前没有真实 bid/ask 接入；回测点差是合成点差（`synthetic_spread_bps`）。
- 当前 `source=Binance` 需要网络访问，网络不可用或 Binance 不支持该 symbol 时会返回 502 错误。

### 手工 Paper 下单风控

`POST /api/paper/orders` 当前仍是 paper-only 同步模拟订单，但已经接入服务端 `RiskLimits` 和 T 仓状态检查：

| 检查项 | 回测层 | 手工 paper 下单 |
|---|---|---|
| 合成点差过宽拒绝 | ✅ | ✅（`spreadPct` 输入触发） |
| 市场数据新鲜度 | ✅（可配置） | ✅（`marketDataAgeSec` 输入触发，默认 30 秒） |
| 日内最大亏损上限 | ✅（可配置） | ✅（服务端累计 `PAPER_DAILY_LOSS`，默认 500 quote） |
| 加仓网格间距 | ✅ | ✅（按 `PAPER_LAST_BUY_PRICE` 检查） |
| 最大层数限制 | ✅ | ✅（按 `PAPER_T_LAYERS` 检查） |
| T 仓市值上限 | ✅ | ✅（`max_t_position_quote`） |
| 单笔金额上限 | ✅（可配置） | ✅（`max_order_quote`） |

手工 paper 卖出成功后会同步更新 `PAPER_POSITION_COST`、`PAPER_T_LAYERS` 和 `PAPER_LAST_BUY_PRICE`。当一次卖出释放至少一层 T 仓时，旧买价会被清理，后续买入不会被已释放层的网格状态误拒。

仍需注意：这些检查依赖调用方提供 `spreadPct` 和 `marketDataAgeSec`。在接入真实 broker 前，还必须接入真实 bid/ask、真实行情时间戳、订单幂等 key、权限、审计日志和券商回报对账。

### 结算规则与交易限制

本系统的做 T 策略（当天买入、当天卖出同一标的）在不同市场受不同规则约束：

**币安 B-Stock（SPCXB/USDT，当前主要标的）**

- 币安现货属于加密货币交易规则，**不受美股 PDT 规则约束**，可以无限制当日往返交易
- 结算为即时结算（T+0），买入后立即可卖，不存在资金锁定期
- 注意：币安 B-Stock 流动性比底层美股低，深度不足时成交价会偏离理论价；当策略建议金额较大时，实际滑点可能高于回测假设的 0.3%

**美股（未来通过 IBKR 扩展时）**

- 允许 T+0，但受 **PDT 规则**约束：保证金账户净值 < $25,000，5 个交易日内 4 次以上当日往返会被标记限制
- 建议使用现金账户（Cash Account）规避 PDT 限制

**A 股（如果未来扩展）**

- A 股是 T+1 结算：当天买入的股不能当天卖出
- “正 T”需要先有底仓，操作顺序是”先卖后买”；当前代码框架不适配，需要专门设计

### 当前系统状态声明

当前系统所有”账户余额”、”持仓”、”收益”均为 paper 模拟数据，不代表任何真实账户状态。所有回测结果仅反映在历史合成数据或 CSV 数据上的假设成交，不保证对未来真实市场的预测有效性。
