# 三阶段财报复核与等权重算设计

## 目标

修复 `plan -> sell -> buy` 跨交易日执行过程中财报日历变化、候选集合变化和订单未确认成交导致的仓位偏差。T 日信号保持冻结，只允许执行层根据财报、真实持仓、成交状态和可用资金重新计算订单。

## 财报窗口

`EARNINGS_BLACKOUT_DAYS=2` 表示每个阶段都以自己的美东执行日为起点，检查 `[执行日, 第 2 个后续 NYSE 交易日]`，首尾包含：

- T 日 plan 检查 T、T+1、T+2。
- T+1 sell 检查 T+1、T+2、T+3。
- T+2 buy 检查 T+2、T+3、T+4。

日期必须使用 `America/New_York`，交易日必须来自 NYSE 日历，不能使用只排除周末的 `BDay`。查询失败继续采用 fail-open，不阻断卖出或买入，但失败标的必须进入阶段摘要和邮件。

## 状态模型

### cycle_plan

除现有买卖计划外，保存所有通过价格校验和 plan 财报过滤的目标快照：

```json
{
  "target_snapshot": {
    "AAPL": {"price": 100.0},
    "MSFT": {"price": 50.0}
  }
}
```

`target_snapshot` 是后续重新计算股数的唯一候选集合。旧版 state 缺少该字段时继续使用 `buy_carry`，只过滤、不进行完整等权重算。

### pending_sell

sell 阶段保存：

- `target_snapshot`：剔除 sell 阶段财报命中后的剩余目标。
- `earnings_forced_close`：sell 阶段新增强制清仓标的。
- `reallocation_required`：候选集合是否在 sell 阶段发生变化。
- `orders`：待 buy 阶段核实的卖单。
- `buy_carry`：仅作为旧 state 兼容和预览，不再作为完整重算候选集合。

sell 阶段只负责撤止损、强制清仓和原有卖单，不提交因等权变化产生的新买单。`target_snapshot` 只移除原目标集合中的财报命中标的，外部新增的非目标持仓不能改变等权分母。

### pending_buy

buy 阶段提交订单后不立即宣告周期完成，而是写入：

```json
{
  "signal_date": "2026-07-08",
  "submit_date": "2026-07-10",
  "orders": [
    {"symbol": "AAPL", "qty": 151, "client_order_id": "...", "status": "submitted"}
  ]
}
```

09:45 ET 的现有重试任务同时核实 `pending_buy`：

- 完全成交：读取真实持仓，补齐止损，清除 `pending_buy`，更新调仓完成状态。
- 部分成交或仍 accepted/new：保留状态，不重复已成交数量。
- 提交失败或 rejected：保留买入意图并告警，确认不存在有效同 ID 订单后才允许受控重试。
- 状态查询失败：不假设成交、不重复下单，等待下一次核实。

## buy 阶段完整重算

1. 核实 pending_sell 中卖单的成交情况。
2. 读取当前真实持仓数量和市值。
3. 对 `target_snapshot` 中全部剩余目标重新查询财报，而不是只查旧 `buy_carry`。
4. 移除 buy 阶段新命中的目标。
5. 若被移除目标仍有持仓，本阶段不卖出；其当前市值从可投资预算中扣除，避免把尚未释放的资本重复分配。
6. 对全部剩余目标按统一目标金额计算目标股数，再减去实时持仓得到正向 delta。
7. 只提交正向 delta；负向 delta 留给下一轮 sell，不在 buy 阶段扩张职责。
8. 若估算买入额超过实际 buying power，沿用现有比例缩减。

## earnings_allow

`--earnings-allow` 仍然只对当前命令生效。执行 `--phase sell/buy --earnings-allow MU` 时必须在该阶段复核结果中移除 MU；不会自动继承 plan 阶段曾使用的豁免。

## 邮件和兼容性

- sell 全部失败邮件也必须包含强制清仓名单和财报查询降级提示。
- buy 邮件区分“订单已提交”和“订单已确认成交”，存在 `pending_buy` 时不得写“调仓周期完成”。
- 升级前的 `cycle_plan/pending_sell` 缺少新字段时保持可执行，不抛异常，不伪造完整等权结果。
- `--phase both` 保持现有一次性执行路径，不纳入本次三阶段状态改造。

## 验证

- 每个行为先添加失败测试，再写最小实现。
- 覆盖 sell 命中后 buy 无新增命中、HOLD/trim 候选补仓、非目标持仓不影响分母、已持仓财报命中资金不重复分配、pending_buy 各订单状态、NYSE 节假日窗口和 earnings_allow。
- 最后运行 Python 3.12 语法检查和 `Trade-quant/live/tests/` 全量测试，不进行真实下单。
