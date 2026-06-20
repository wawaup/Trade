# IBKR 接入方案

本文档覆盖从开户到上线的完整 IBKR 接入计划，包括技术架构、开发顺序、合规要求和测试策略。目标是在开户完成后 2～3 周内具备 paper trading 能力，4～6 周内具备可控的真实资金执行能力。

---

## 一、IBKR 是什么，为什么选它

Interactive Brokers（盈透证券）是目前最适合个人量化交易者的美股券商，核心原因：
- **API 成熟**：官方提供 Python SDK（`ibapi`），社区封装库 `ib_insync` 更易用
- **手续费低**：美股市价单约 $0.005/股，最低 $1/笔，比大多数零售券商便宜
- **产品覆盖广**：美股、期权、期货、外汇均支持，策略扩展余地大
- **Paper Trading 账户**：开户后自动附赠模拟账户，和真实账户共用同一套 API，切换只需改一个参数

---

## 二、开户前准备清单（现在就可以做）

在等待开户审核期间（通常 1～5 个工作日），提前完成以下准备：

### 2.1 安装 IB Gateway

IB Gateway 是 IBKR 提供的轻量级后台程序，不需要 TWS 图形界面，是服务器或无头运行的首选：
- 下载地址：[ibkr.com/en/trading/ibgateway](https://www.interactivebrokers.com/en/trading/ibgateway-latest.php)
- 两个版本：**Stable**（生产用）和 **Latest**（测试用），建议用 Stable
- 启动后在本地监听 7496（真实账户）或 7497（Paper 账户）端口

### 2.2 安装 ib_insync

```bash
uv add ib_insync
# 或
pip install ib_insync
```

`ib_insync` 是社区封装的异步 Python 库，比官方 `ibapi` 简洁很多，底层仍调用 TWS API。

### 2.3 验证本地连接

```python
from ib_insync import IB, Stock

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)  # 7497 = Paper 账户端口

contract = Stock("AAPL", "SMART", "USD")
bars = ib.reqHistoricalData(
    contract,
    endDateTime="",
    durationStr="5 D",
    barSizeSetting="1 min",
    whatToShow="TRADES",
    useRTH=True,
)
print(bars[:3])
ib.disconnect()
```

能打印出 K 线数据，说明 API 连接正常。

### 2.4 确认账户类型

开户时选择：
- **个人账户（Individual）**，不要选 Joint
- **Reg T Margin Account**（保证金账户）：注意 PDT 规则（净值 < $25K 时限制日内交易次数），但提供更多交易功能
- **Cash Account**（现金账户）：无 PDT 限制，但资金 T+2 结算，不能用未结算资金再交易

**建议**：如果账户净值能保持在 $25K 以上，选保证金账户；否则选现金账户避免 PDT 限制。

---

## 三、技术架构设计

当前代码已有良好的分层，IBKR 只需插入两个新模块，不需要改现有逻辑：

```
现有结构：
  DataSourceFactory → BinanceDataSource / SyntheticDataSource / CSVDataSource
  PaperExecutionAdapter（paper only）

新增：
  DataSourceFactory → IBKRDataSource（新增）
  IBKRExecutionAdapter（新增，先接 paper 账户，再切真实）
```

### 3.1 IBKRDataSource

路径：`tradebot/data_sources.py`（在现有文件里追加）

职责：
- 从 IBKR 拉取历史 K 线（日线 + 分钟线）
- 将 IBKR 的 `BarData` 格式转换为项目的 `Candle` 结构

接口设计：

```python
class IBKRDataSource:
    name = "IBKR"

    def __init__(self, host="127.0.0.1", port=7497, client_id=1):
        self.host = host
        self.port = port
        self.client_id = client_id

    def get_default_candles(self, symbol="SPCX", use_rth=True, **kwargs):
        # 1. 连接 IB Gateway
        # 2. 构造 Stock contract
        # 3. 拉取 daily（过去 60 天）和 intraday（过去 500 根 1 分钟线）
        # 4. 转换为 list[Candle]
        # 5. 断开连接
        ...
```

关键转换：IBKR 的 bar 包含 `date`、`open`、`high`、`low`、`close`、`volume`，需要补充 `quote_volume`（IBKR 没有直接提供，可用 `close * volume` 近似，或改用 `MIDPOINT` whatToShow）。

### 3.2 IBKRExecutionAdapter

路径：`tradebot/execution.py`（新增类）

职责：
- 把 `OrderIntent` 转换为 IBKR `MarketOrder`
- 提交订单并等待成交回调
- 返回 `FillSnapshot`

接口设计：

```python
class IBKRExecutionAdapter:
    def __init__(self, ib: IB, account_type="paper"):
        # account_type: "paper" | "live"
        self.ib = ib
        self.account_type = account_type

    def execute(self, intent: OrderIntent, price: float, ...) -> FillSnapshot:
        # 1. 校验 paper_only 字段与 account_type 是否匹配
        # 2. 构造 Stock contract + MarketOrder
        # 3. ib.placeOrder()
        # 4. 等待 fill（ib_insync 的 waitOnUpdate 或 trade.fills）
        # 5. 返回 FillSnapshot
        ...
```

### 3.3 DataSourceFactory 注册

在现有 `DataSourceFactory._SOURCES` 和 `_ALIASES` 中追加：

```python
_ALIASES = {
    ...
    "ibkr": "IBKR",
    "interactivebrokers": "IBKR",
    "ib": "IBKR",
}
_SOURCES = {
    ...
    "IBKR": IBKRDataSource,
}
```

HTTP 接口无需修改，`source=IBKR` 即可使用。

---

## 四、VWAP session 重置时间调整

SPCXB（B-Stock）和 SPCX（美股直连）的活跃时段都集中在美股开盘时间。

`session_vwap()` 当前以 UTC 08:00 作为每日重置点，这在 SPCXB 场景下会把开盘前几乎无量的时段也算进今天的 VWAP，导致数据污染。

需要调整：

| 标的 | 建议 session reset | 对应时区 |
|---|---|---|
| SPCXB（币安 B-Stock） | UTC 13:30 | 美东 9:30 开盘 |
| SPCX（IBKR 美股，`useRTH=True`） | UTC 13:30 | 同上 |
| BTC 等加密货币 | UTC 00:00 | 自然日 |

建议在 `StrategyConfig` 或 `session_vwap()` 增加 `session_reset_hour` 参数，而不是继续硬编码。

---

## 五、PDT 规则监控（保证金账户专用）

如果使用保证金账户且净值 < $25,000：

**PDT 规则定义**：5 个交易日内，同一标的完成"当日买入+当日卖出"4 次及以上，账户被标记 PDT，限制后续交易。

**需要在代码中实现**：

```python
class PDTMonitor:
    """跟踪最近 5 个交易日的日内往返交易次数。"""

    def record_round_trip(self, symbol: str, date: date): ...
    def day_trade_count(self) -> int: ...  # 5 天内总计
    def is_approaching_limit(self) -> bool: ...  # >= 3 次时预警
    def would_trigger_pdt(self) -> bool: ...  # 本次操作是否会触发第 4 次
```

在 `IBKRExecutionAdapter.execute()` 中，每次执行卖出（当日已有买入）前先调用 `would_trigger_pdt()`，如果会触发且账户净值 < $25K，拒绝执行并记录风险事件。

---

## 六、开发阶段计划

### 阶段 1：数据接入（开户完成后第 1 周）

目标：能通过 IBKR API 拉取 SPCX 历史 K 线，用于回测。

- [ ] 实现 `IBKRDataSource`
- [ ] 注册到 `DataSourceFactory`
- [ ] 修复 `session_vwap()` 的 reset hour 参数化
- [ ] 用真实 SPCX 数据跑一次完整回测，对比合成数据结果
- [ ] 补充 `quote_volume` 的近似处理方案

验收标准：`POST /api/backtest/run` 传入 `source=IBKR&symbol=SPCX` 能返回基于真实历史数据的回测结果。

### 阶段 2：Paper 执行接入（第 2 周）

目标：策略信号能通过 IBKR Paper 账户真实提交，观察成交质量。

- [ ] 实现 `IBKRExecutionAdapter`（仅 paper 模式）
- [ ] 添加环境开关：`TRADE_MODE=paper|live`，强制隔离
- [ ] 打通信号 → 执行的完整路径（策略信号 → `IBKRExecutionAdapter.execute()` → IBKR paper 订单）
- [ ] 统一 `RiskLimits` 检查路径（手工下单和自动信号共用同一套检查）
- [ ] 实现基本的审计日志（记录每笔订单意图和成交结果到本地文件）

验收标准：在 IBKR Paper 账户里能看到策略自动产生的订单，手动对比信号和实际成交价。

### 阶段 3：风控完善（第 3 周）

目标：真实资金接入前的最后安全层。

- [ ] 实现 `PDTMonitor`（如果使用保证金账户）
- [ ] 实现紧急平仓（kill switch）：一键平掉所有 IBKR 持仓
- [ ] 实现日内最大亏损熔断：当日亏损超过 X 后自动停止接单
- [ ] 实现隔夜持仓检查：收盘前 5 分钟如果有 T 仓未平，自动发出警告（或自动平仓）
- [ ] 回测结果中的 `core_only_return_pct` 在 IBKR 场景下补全（核心仓真实买入价格）

验收标准：Paper 账户模拟运行 5 个交易日，无未处理的异常，风险事件日志完整。

### 阶段 4：真实资金（开户完成后第 4～6 周）

- 先用最小资金（$500 以内）测试完整链路
- 确认手续费计算与实际账单一致
- 确认 T 仓止损在真实成交时的价格偏差在容忍范围内
- 逐步调大资金

---

## 七、手续费模型更新

当前 `BinanceStockFeeModel` 基于 Binance 手续费结构，接入 IBKR 后需要新增：

```python
class IBKRStockFeeModel:
    """
    IBKR 美股 Fixed 费率模型（零售客户常用）：
    - $0.005/股，最低 $1.00，最高成交金额的 1%
    """
    RATE_PER_SHARE = 0.005
    MIN_FEE = 1.0

    def estimate(self, qty: float, price: float) -> float:
        notional = qty * price
        fee = max(self.RATE_PER_SHARE * qty, self.MIN_FEE)
        return min(fee, notional * 0.01)
```

回测时传入 `fee_model=IBKRStockFeeModel()` 即可。

---

## 八、IBKR vs 币安 B-Stock 对比

| 维度 | 币安 B-Stock（SPCXB） | IBKR 美股（SPCX） |
|---|---|---|
| 交易时间 | 平台时间（流动性集中美股时段） | 美东 9:30～16:00（正盘）；盘前盘后另行开通 |
| PDT 限制 | 无 | 有（净值 < $25K 时） |
| 流动性 | 较低，大单滑点明显 | 高，纳斯达克/NYSE 深度 |
| API 接入 | Binance REST（已接入） | IBKR TWS API + ib_insync（待接入） |
| 手续费 | 0.1%（BNB 可降） | $0.005/股，最低 $1 |
| 价格追踪 | 锚定底层股票，可能有溢价/折价 | 直接定价 |
| 平台风险 | 币安可能下架或暂停 B-Stock | 受 SEC/FINRA 监管，稳定性高 |
| 结算 | 即时 T+0 | T+2（现金账户有影响） |
| 隔夜风险 | 持仓过夜时 B-Stock 可能与底层股票有价差 | 隔夜跳空风险同底层股票 |

**策略建议**：接入 IBKR 后，优先在 IBKR 上执行，以更好的流动性和更低的滑点提升策略实际表现；币安 B-Stock 作为备用（IBKR 不可用时）或加密资产的统一入口。

---

## 九、当前待办（开户前可提前完成的）

| 任务 | 优先级 | 说明 |
|---|---|---|
| 修复 `session_vwap()` reset hour 参数化 | P0 | 影响当前 SPCXB 回测准确性，不必等 IBKR |
| ATR 动态止损接入 | P0 | 独立于数据源，现在就能做 |
| 置信度影响仓位大小 | P0 | 独立于数据源，现在就能做 |
| 安装 IB Gateway | 准备 | 开户审核期间就可以安装测试连接 |
| 下载 ib_insync 并跑通示例 | 准备 | 验证本地环境，开户前无法拉真实数据但可测结构 |
