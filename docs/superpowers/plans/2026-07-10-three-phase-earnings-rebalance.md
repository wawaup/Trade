# 三阶段财报复核与等权重算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让三阶段调仓在 sell/buy 财报复核后基于完整目标集合和真实持仓重新计算订单，并在买单确认成交后才完成周期。

**Architecture:** `cycle_plan` 保存完整目标价格快照；`pending_sell` 保存过滤后的目标集合及卖单；buy 阶段从完整集合生成正向差额并写入 `pending_buy`；09:45 重试任务核实买单成交和补挂止损。旧 state 缺少新字段时降级兼容。

**Tech Stack:** Python 3.12、unittest/pytest、alpaca-py、pandas-market-calendars、JSON state machine。

---

### Task 1: 完整目标快照与 sell 触发的 buy 等权重算

**Files:**
- Modify: `Trade-quant/live/alpaca_trader.py`
- Test: `Trade-quant/live/tests/test_execution_safety.py`

- [ ] **Step 1: 写 sell 命中、buy 无新增命中仍重算的失败测试**

构造三只目标：AAPL 为 HOLD、MSFT 为 BUY、NVDA 为 TRIM。sell 阶段 NVDA 财报命中并清仓；buy 阶段财报返回空集合。断言 AAPL 和 MSFT 都按两只候选的新目标金额生成正向差额。

- [ ] **Step 2: 运行单测确认按旧股数下单而失败**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py::ExecutionSafetyTests::test_sell_blackout_reallocates_all_surviving_targets_at_buy -q`

Expected: FAIL，AAPL 不在订单中或 MSFT 仍使用旧 qty。

- [ ] **Step 3: 在 plan/pending_sell 中保存完整 target_snapshot**

`compute_rebalance_plan` 只把通过价格和异常波动校验的目标写入 `target_snapshot`。sell 阶段仅从该快照移除命中的原目标，并写入 `reallocation_required`。

- [ ] **Step 4: buy 对完整剩余目标计算正向 delta**

读取实时持仓，对全部 `target_snapshot` 重新计算目标股数；只保留 `target_qty - current_qty > 0` 的订单。缺少快照时保留旧 `buy_carry` 降级路径。

- [ ] **Step 5: 运行 Task 1 测试和 execution_safety 全文件**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -q`

Expected: PASS。

### Task 2: 修正候选分母与未释放资金

**Files:**
- Modify: `Trade-quant/live/alpaca_trader.py`
- Test: `Trade-quant/live/tests/test_execution_safety.py`

- [ ] **Step 1: 写非目标外部持仓不减少候选数的失败测试**

在 plan 后新增非目标持仓 XYZ，sell 财报命中 XYZ。断言 XYZ 会被强制清仓，但 `target_snapshot` 和目标数量不减少。

- [ ] **Step 2: 写 buy 命中已持仓标的不重复分配其市值的失败测试**

buy 阶段财报命中仍持有的 MSFT，断言 MSFT 不加仓，其当前市值从总可投资预算中扣除后再计算其余候选目标金额。

- [ ] **Step 3: 运行两个测试确认失败**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'non_target_blackout or held_blackout_budget' -q`

Expected: FAIL，旧逻辑按 forced 数量扣分母或使用全部 sizing_capital。

- [ ] **Step 4: 用目标 symbol 集合替代 forced 数量扣减**

候选集合变化使用集合差计算；预算使用 `sizing_capital * (1 - MIN_CASH_BUFFER_PCT) - excluded_held_market_value`，结果下限为 0。

- [ ] **Step 5: 运行 Task 2 和 Task 1 回归测试**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'earnings or target_snapshot or blackout' -q`

Expected: PASS。

### Task 3: pending_buy 成交确认与止损补挂

**Files:**
- Modify: `Trade-quant/live/alpaca_trader.py`
- Modify: `Trade-quant/live/deploy/cron_setup.sh`
- Test: `Trade-quant/live/tests/test_execution_safety.py`

- [ ] **Step 1: 写 accepted 买单不会立即完成周期的失败测试**

断言 buy 提交成功后写入 `pending_buy`，不更新 `last_rebalance`，不立即清除所有周期核实信息。

- [ ] **Step 2: 写 filled/partial/rejected/query-error 四类核实测试**

完全成交时清状态并挂止损；其余状态保留 `pending_buy`，且查询失败或未终态时不重复下单。

- [ ] **Step 3: 运行测试确认当前立即清状态行为失败**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'pending_buy' -q`

Expected: FAIL，当前不存在 `pending_buy`。

- [ ] **Step 4: 实现 reconcile_pending_buy 并接入 09:45 路径**

买单提交只记录订单意图和 client order id。`--retry-halted` 先核实 pending_buy，再运行原停牌重试。只有全部订单确认成交后才更新调仓完成状态并补挂止损。

- [ ] **Step 5: 运行订单安全相关测试**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'buy or stop or order' -q`

Expected: PASS。

### Task 4: earnings_allow 和邮件分支

**Files:**
- Modify: `Trade-quant/live/alpaca_trader.py`
- Test: `Trade-quant/live/tests/test_execution_safety.py`

- [ ] **Step 1: 写 sell/buy 当前运行豁免测试和 sell 全失败邮件测试**

断言 `earnings_allow={"MU"}` 会从该阶段 blackout 中移除 MU；sell 全失败邮件包含强制清仓名单和 degraded 提示。

- [ ] **Step 2: 运行测试确认失败**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'earnings_allow or all_failed_email' -q`

Expected: FAIL。

- [ ] **Step 3: 透传 earnings_allow 并统一邮件财报行生成**

为 sell/buy 函数添加可选参数，上层传入当前命令的集合；把财报提示行提取为两个状态分支都复用的数据。

- [ ] **Step 4: 运行相关测试**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'earnings or email' -q`

Expected: PASS。

### Task 5: 美东日期、NYSE 窗口和查询失败可见性

**Files:**
- Modify: `Trade-quant/live/alpaca_trader.py`
- Test: `Trade-quant/live/tests/test_execution_safety.py`

- [ ] **Step 1: 写节假日和美东跨日失败测试**

固定美东执行日，构造窗口中包含 NYSE 休市日，断言截止日是第 2 个后续 NYSE 交易日；固定 UTC/新加坡已跨日而美东未跨日，断言使用美东日期。

- [ ] **Step 2: 写部分查询失败仍放行但列出失败标的的测试**

断言 blackout 只包含确认命中标的，失败标的进入返回元数据，主流程不抛异常。

- [ ] **Step 3: 运行测试确认 BDay 和当前返回结构失败**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'earnings_window or earnings_failure_symbols' -q`

Expected: FAIL。

- [ ] **Step 4: 使用 America/New_York 和 NYSE schedule 计算窗口**

新增内部窗口函数；扩展财报查询结果以携带失败标的，并在 plan/sell/buy 摘要和邮件中展示，但不阻断订单。

- [ ] **Step 5: 运行财报测试**

Run: `/tmp/trade-review-venv/bin/python -m pytest tests/test_execution_safety.py -k 'earnings' -q`

Expected: PASS。

### Task 6: 全量验证与最终复审

**Files:**
- Verify: `Trade-quant/live/alpaca_trader.py`
- Verify: `Trade-quant/live/tests/`

- [ ] **Step 1: 运行语法检查**

Run: `PYTHONPYCACHEPREFIX=/tmp/trade-pycache /Users/admin/.local/bin/python3.12 -m py_compile Trade-quant/live/alpaca_trader.py`

Expected: exit 0。

- [ ] **Step 2: 运行完整测试**

Run: `cd Trade-quant/live && PYTHONPYCACHEPREFIX=/tmp/trade-pycache /tmp/trade-review-venv/bin/python -m pytest tests/ -q`

Expected: 全部 PASS。

- [ ] **Step 3: 检查 diff 和状态兼容性**

Run: `git diff --check -- Trade-quant/live/alpaca_trader.py Trade-quant/live/deploy/cron_setup.sh Trade-quant/live/tests/test_execution_safety.py`

Expected: 无输出、exit 0；确认未覆盖用户其他未提交改动。
