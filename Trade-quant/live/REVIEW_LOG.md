# 三段式调仓（plan/sell/buy）复审日志

> 分支：`quant/factor-research`
> 范围：`alpaca_trader.py` 状态机（`cycle_plan` → `pending_sell` → 完成）及配套测试
> 方式：多轮独立 code-review agent 循环复审，每轮发现独立验证后修复，直到某轮报告零新发现为止

---

## 背景

`d56e465` 引入了 plan（T 日算计划）→ sell（T+1 尾盘卖）→ buy（T+2 开盘核实并买）三段式状态机，
用 `state.json` 里的 `cycle_plan` / `pending_sell` 两个 key 在三个独立 cron 触发的进程间传递状态。
由于状态只靠磁盘 JSON 传递、任一阶段都可能因网络/API/崩溃而失败或被跳过，围绕"失败重试"和
"状态覆盖"的边界条件是本轮复审的重点。

## 修复历史（按提交顺序）

| commit | 修复内容 |
|---|---|
| `fe1f106` | 首轮修复：GTC 止损单阻塞卖出、财报避雷仓位 bug、5% 缓冲逻辑等（HIGH-1/MEDIUM-2~5/LOW-6~7） |
| `aea44ab` | 二轮修复：重试计划 client_order_id 复用 signal_date 导致撞车死锁；legacy `rebalance()` 遗漏止损单撤销 |
| `ae339a8` | 非交易日免打扰状态（`NO_EMAIL_STATUSES`）若处理逻辑内部再抛异常会被静默吞掉，抽出 `should_escalate_to_error()` 修复；`execute_sell_phase` 中非幂等提交失败的标的被 `continue` 丢弃且无追踪，改为写回仅含失败标的的重试 `cycle_plan`，新增 `sell_submitted_with_issues` 报警状态 |
| `fadaf9e` | `execute_buy_phase` 写回重试 `cycle_plan` 时无条件覆盖，会顶掉 `execute_sell_phase` 此前为其它失败标的写回的重试计划——改为按 symbol 去重合并 |
| `dd00c73` | `execute_sell_phase` 无条件覆盖 `state[PENDING_SELL_KEY]`，若上一轮卖单尚未被 buy 阶段核实（buy 阶段漏跑/崩溃），会静默丢失核实指针与买入计划——检测到未消费的 `pending_sell` 时直接跳过提交并报警（新增 `pending_sell_stale_blocked` 状态）；合并重试计划时增加 `signal_date` 不一致检测 |
| `377e025` | 报警粒度调优：`cycle_already_pending`（上一周期未完成）纳入 `is_emergency_status`；`signal_date` 混合检测从纯日志升级为 `summary` 标志 + 邮件正文提示 |

## 状态机关键不变量（本轮复审确认）

1. **同一时刻只应有一个未完成周期**：`plan` 阶段发现 `cycle_plan` 或 `pending_sell` 非空时跳过重新计算（`cycle_already_pending`，现属紧急报警）。
2. **重试写回不能相互覆盖**：`execute_sell_phase`（提交失败重试）和 `execute_buy_phase`（未确认成交重试）都可能写 `CYCLE_PLAN_KEY`，两者必须按 symbol 合并而非覆盖。
3. **`PENDING_SELL_KEY` 是跨阶段游标**：只能由 `execute_buy_phase` 消费（`pop`），`execute_sell_phase` 绝不能在其未被消费时覆盖。
4. **client_order_id 用 `plan_date`（写回时的日期）而非 `signal_date`**：保证每次重试生成不同的 client_order_id，避免被券商误判为重复提交。
5. **免打扰状态（`NO_EMAIL_STATUSES`）不能吞真实故障**：`should_escalate_to_error()` 统一判断，凡是该状态处理过程中再抛异常都会升级为 `error` 并发邮件。

## 测试

回归测试全部添加在 `tests/test_execution_safety.py`，遵循"能在修复前失败、断言真实失败场景而非数据形状"的原则。
本轮复审后 pytest 从 58 增至 **64 passed**（`/tmp/tqv4/bin/python -m pytest tests/ -q`）。

## 已知遗留（非本轮引入，接受现状）

- **无重试熔断**：`cycle_plan` 重试写回机制（无论是提交失败还是未确认成交触发）没有次数上限，一个永久停牌/退市的标的会无限期重试，只是报警级别较高（`sell_submitted_with_issues`/`buy_completed_with_issues`），不会静默。如需收紧可加 `retry_count`/`first_retry_date` 字段，达到阈值后升级为独立的终止性报警状态。
- **`should_escalate_to_error` 只覆盖 `NO_EMAIL_STATUSES`**：其它"非紧急中间状态"（如 `plan_saved`/`no_pending_plan`）若在赋值后、`return` 前的收尾代码里抛异常，仍不会自动升级为 `error`（这属于早已存在的语义，不是本轮回归）。
