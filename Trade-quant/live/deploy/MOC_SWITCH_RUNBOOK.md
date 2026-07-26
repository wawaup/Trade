# MOC 单段路径切换 Runbook（VM 一页式操作清单）

> 目标：把 GCP VM 上的执行路径从 `three_phase` 切到 `moc_single` 并开始 paper 验证。
> 全程约 15 分钟。所有命令在 VM 的 `Trade-quant/live/` 目录下执行。
> 最近更新：2026-07-17（本地已验证：117 个单测全过 + 三段 dry-run 全过）

## 0. 前置状态确认

```bash
cd Trade-quant/live
python3 -c "import json;print(json.load(open('state.json')))"   # 看当前 state
crontab -l | grep -A10 TRADE_QUANT                              # 看当前 cron
```

切换的硬前提：**无 in-flight 周期**——`cycle_plan` / `pending_sell` / `pending_buy` /
`pending_execute` 必须全空（execute 阶段检测到 three_phase 残留会拒绝执行）。

## 1. 修复 state.json（P0，一次性）

```bash
python3 tools/repair_state.py          # 先看方案（dry-run，不写盘）
python3 tools/repair_state.py --yes    # 确认后写回（自动备份原文件）
```

脚本会：补齐 `high_watermark`（取 max(bak, 当前净值)，宁高勿低）/ `last_rebalance` /
`kill_switch`；逐笔向 broker 核实 pending 订单，404/终态的剔除（已确认
`tq-retry-20260708-nvda` 属 404，会被自动清掉）。

**核对**：脚本打印的 broker 持仓 vs 你在 Alpaca dashboard 看到的一致；不一致以 broker 为准。

## 2. 核对并修改 .env

```bash
grep -E "^(EXEC_MODE|ALPACA_DATA_FEED|ORDER_TIF|MAX_POSITION_PCT|SIM_CAPITAL_USD)" .env
```

| 变量 | 应为 | 说明 |
|---|---|---|
| `EXEC_MODE` | `moc_single` | **本次切换的核心改动** |
| `ALPACA_DATA_FEED` | `iex` | 免费套餐禁查 sip 最近 15 分钟数据；若 VM 是 sip 且之前能跑，记录原因再改 |
| `ORDER_TIF` | `day` | Jul 1 的 expired 买单已实锤 opg 隐患（moc_single 路径不受此项影响，仍建议改正） |
| `MAX_POSITION_PCT` | `0.50` | **顺手排查 DOCN 678 股大单**：若此值是 1.0 或未设，就找到了根因 |
| `SIM_CAPITAL_USD` | `5000` | 同上排查：若未设，sizing 会用全额净值 |

同时拉取日志供 DOCN 大单排查：`grep -B5 -A5 "DOCN" trader.log | head -80`（找 Jul 9 的 sizing 计算行）。

## 3. 重装 cron 并验证

```bash
bash deploy/cron_setup.sh
crontab -l | grep -A10 TRADE_QUANT
```

**必须看到**（moc_single 套装）：
- `05 16 * * 1-5` plan
- `35 15 * * 1-5` execute ← 新
- `45 09 * * 1-5` reconcile ← 新
- **不得再有** `50 15` sell、`15 09` buy、`--retry-halted` 三条旧行

## 4. 首周期观察清单（plan T → execute T+1 → reconcile T+2）

每个阶段跑完后看 `trader.log` 和邮件：

| 时点 | 观察项 | 预期 |
|---|---|---|
| T 16:05 后 | `cycle_plan` 已写入 state.json | 计划含 close/trim/buy 明细 |
| T+1 15:35 后 | `SELL_MOC` / `BUY_MOC` 行；Alpaca dashboard 出现 `tq-moc-*` 订单 | 二腿都是 `cls` TIF，15:50 前提交完成 |
| T+1 16:00 后 | dashboard 中 MOC 单 filled，`filled_qty` = 计划 qty | 同一收盘拍卖价成交 |
| T+1 | `LOO_FALLBACK` 是否触发 | 正常不触发；若触发，核对限价 = ref×1.03 |
| T+2 09:45 后 | reconcile 日志：`stop_added` 补挂止损；`pending_execute` 被清空 | `status=reconcile_complete`；dashboard 有 `tq-stop-*` GTC 单 |
| 任意 | 邮件报警 | 无 CRITICAL/负持仓告警 |

**验证目标：连续 3 个完整周期（约 3-4 周）无异常**，再讨论切 IBKR live 默认。

## 5. 回退（任何时候）

```bash
# .env 改回 EXEC_MODE=three_phase
bash deploy/cron_setup.sh        # 自动换回 plan/sell/buy 三条 cron
```

回退前同样确认无 in-flight `pending_execute`（有的话先等 reconcile 跑完或人工核实订单终态）。

## 6. 已知差异备忘（本地验证时发现）

- 本地系统 Python 3.9 跑不动依赖，本地开发用 `live/.venv312`；VM 是 3.10+ 不受影响。
- `iex` 数据源成交量只有全市场约 2-3%，策略 Vol 过滤用相对倍数（1.2x）可接受，
  但切 live 前建议抽查几只股票 iex vs 官方收盘价的偏差。
