# Trade-quant 项目交接文档（HANDOFF）

> 用途：新对话恢复上下文的起点。读完本文档就应该能承接后续工作。
> 最近更新：2026-07-17
> 相关配套文档：`FIX_PLAN.md`（审计与修复计划）、`research/RESEARCH_LOG.md` §12-14（策略演进）、
> `live/deploy/MOC_SWITCH_RUNBOOK.md`（VM 切换一页式操作清单）

**2026-07-16/17 增量**（详见 §3.1 / §4 P1 更新）：
- MOC 三段（plan/execute/reconcile）本地 dry-run 全通过；117 个单测本地全过（新建 `live/.venv312`，系统 3.9 跑不动依赖）
- `ALPACA_DATA_FEED` 从 sip 改为 iex（免费套餐禁查 sip 最近 15 分钟 → 全量拉取失败）
- 新增 `live/tools/repair_state.py`（state.json 半自动修复，§7.3 已解决）与 `live/deploy/MOC_SWITCH_RUNBOOK.md`
- P5 部分完成：TOP_N/MIN_SCORE/VOL_MIN/REBALANCE_DAYS/KILL_DD 单源化到 `research/strategy_params.py`（rebalance() 旧路径删除留待 paper 验证后）
- 用户决定：云端 state.json 修复推迟到"所有策略完成后"（期间云端 cron 因 F2 校验硬失败而停摆）
- 从 Alpaca 订单列表确认：`tq-retry-20260708-nvda` 是 404（从未被接受），恢复时 pending_buy 直接清空
- **新发现待查**：Jul 9 live trader 买入 DOCN 678 股 ≈$99k，突破 50% 仓位上限并造成 -$10k 亏损（账户 $100k→$89.8k 的来源）；根因需 VM 的 trader.log 与 .env 中 MAX_POSITION_PCT/SIM_CAPITAL_USD 实际值

---

## 0. 一分钟摘要

- **项目**：多因子选股 + QQQ MA50 状态机的截面动量策略，Alpaca paper 已上线，目标是 Alpaca paper → IBKR live。
- **2026-07-11 审计**发现原头条数字 +113% CAGR / Sharpe 1.36 / MaxDD −52% 是**四重同向偏差夸大**的幻影（幸存者股票池 + 信号收盘价成交 + 零成本 + 无实盘同款风控），实盘规则下 2022 年触发 −30% 熔断永久锁死。
- **本轮（第十二~十四阶段）修复后**，策略在实盘时序 + 风控约束下重新出现 alpha：**CAGR +39.7% / Sharpe 0.89 / MaxDD −38.5% / 熔断×3 全部复出**。已达 paper→小额 live 化的门槛。
- **执行侧**新增了 MOC 单段路径（`EXEC_MODE=moc_single`），与原三段式共存，把审计里 F1/H3/H4 三个高危项从"设计难题"降级到"实现细节"。
- **紧急事项**：云端 `state.json` 之前被测试污染损坏（`FIX_PLAN §0.1`），需要**下一个交易日前**人工从 `state.json.bak` 恢复。测试隔离（F3）已修，不会再发生。

---

## 1. 关键决策与画像

### 1.1 策略绩效（2022-01-03 ~ 2026-06-18，165 只半导体/AI 成长股池）

| 版本 | 年化 | Sharpe | MaxDD | 说明 |
|---|---:|---:|---:|---|
| README 头条（legacy） | +110-113% | 1.34-1.36 | −52% | ⚠️ 有前视+幸存者+零成本+无风控，**不可采信** |
| 第十二阶段 live 原版 | −7.8% | −0.62 | −30.9% | 2022-05-18 永久熔断 |
| **第十四阶段 平衡档（现推荐）** | **+39.7%** | **0.89** | **−38.5%** | 熔断×3 全部复出，可上 paper→live |

### 1.2 平衡档配置（回测新默认，直接 `python3 factor_combo_backtest.py` 即得）

```
--exec-stage moc_single       T+1 收盘 MOC 二腿成交
--cost-bps 25                 单边 25bp 成本
--max-pos-pct 0.50            单票权重上限
--kill-dd -0.30               熔断阈值
--cooldown-days 20            静默期最少 20 交易日
--bear-flat-days 45           熊市前 45 天空仓（避 22 年深熊初期）
--choppy-half                 震荡 regime 仓位减半（默认开启）
--soft-drawdown               EWMA 平滑软减仓（默认开启）
--soft-drawdown-scope bear    仅熊市触发（牛市保留满仓吃反弹）
--dd-ewma-span 5              回撤 EWMA 窗口
```

### 1.3 三档可选（研究里保留）

| 档次 | 命令 | CAGR | Sharpe | MaxDD |
|---|---|---:|---:|---:|
| 激进 | `--bear-flat-days 5` | +44.0% | 0.92 | −42.9% |
| **平衡（默认/推荐）** | 直接跑（默认参数） | +39.7% | 0.89 | −38.5% |
| 保守 | `--bear-flat-days 5 --soft-drawdown-scope all --dd-ewma-span 40` | +16.7% | 0.59 | −35.0% |

### 1.4 关键反直觉发现（避免以后走回头路）

- **熊市完全空仓（`bear_flat_days=999`）反而是 alpha 杀手**：原 REGIME_WEIGHTS 里的熊市权重（HV_ratio + BIAS 超卖反弹）在 2022 底和 2025 关税震荡的反弹里其实赚钱。只挡前 45 天避开深熊初期就够。
- **soft_drawdown 全 regime 触发也是 alpha 杀手**：牛市 V 型 shake-out 触发减仓 = 错过反弹。改成 `scope=bear` 恢复大部分 alpha。
- **`bear_flat_days` 15-20 天是"死亡窗口"**：既错过反弹又赶上二次下跌，绝对回避。
- **Top-N 3/5/7/10/15 Sharpe 完全相同**：说明双重过滤后候选常 ≤3 只，策略实际是"极度集中的少数股"，不是"Top-5 等权分散"。

---

## 2. 执行架构（live/）

### 2.1 两条并存路径

| 路径 | 触发时点 | 状态 | 推荐场景 |
|---|---|---|---|
| **`three_phase`（默认，向后兼容）** | plan T 16:05 → sell T+1 15:50 → buy T+2 09:15 | 生产运行中，代码稳定 | 现网当前运行的路径 |
| **`moc_single`（推荐迁移目标）** | plan T 16:05 → execute T+1 15:35 → reconcile T+2 09:45 | 新增，语法通过 + 单测就位，**未上 paper 验证** | 迁到 IBKR / paper 上验证后切默认 |

**切换开关**：`.env` 中 `EXEC_MODE=three_phase | moc_single`，改后重跑 `deploy/cron_setup.sh` 自动切两套 cron。

### 2.2 MOC 单段路径详细

- **execute（T+1 15:35 ET）**：读取 `cycle_plan`，一次性提交 `SELL MOC + BUY MOC` 二腿，同在 16:00 收盘拍卖成交；卖出金额通过 unsettled_proceeds 当天回笼供买入使用。
- **BUY 被拒时降级 LOO**：主 MOC BUY 被 broker 拒（非 duplicate 类）→ 立即提交 `LimitOrderRequest(time_in_force=OPG, limit_price=ref × (1 + 0.03))` 给 T+2 开盘拍卖，防止裸卖不裸买。
- **reconcile（T+2 09:45 ET）**：按 client_order_id 查每笔成交（主 MOC / LOO 兜底），补挂止损，清 state。发现负持仓即中止不清 state。

### 2.3 三段式路径的三个已修致命项

- **F1** sell 阶段实时持仓夹取（避免开裸空）：`alpaca_trader.py:1798-1830`
- **F2** state.json schema 校验（缺关键键硬失败而非静默补默认）：`alpaca_trader.py:175-219`
- **F3** 测试路径 env 化 + conftest.py 隔离（避免测试污染生产文件）：`live/tests/conftest.py`

### 2.4 未修的高危项（在 three_phase 上仍存在，MOC 路径下天然消失或大幅收窄）

| 项 | 状态 |
|---|---|
| H1 buy duplicate 丢追踪 | 三段式仍存在；MOC 路径下不复用旧 pending_buy_orders，天然幂等 |
| H2 加仓撞 wash-trade | 两条路径都存在；影响资金安全较小（fail-safe），留待后续 |
| H3 半日市 15:50 卖单跨日 | 三段式仍存在；MOC 路径 15:35 提交 + Alpaca 硬截止 15:50，消失 |
| H4 止损裸露窗口 | 三段式仍存在；MOC reconcile 阶段真实运行也补挂止损，大幅收窄 |
| H5 无每日账户对账 | 两条路径都缺；应作为下一优先级补上 |

---

## 3. 立即需要处理的事项

### 3.1 P0：云端 state.json 恢复（**下一个交易日前必做**）

**位置**：GCP VM 上 `Trade-quant/live/state.json`
**问题**：当前只剩 `pending_buy`（NVDA×2，`tq-retry-20260708-nvda`，status=query_error），缺 `high_watermark` / `kill_switch` / `last_rebalance` 三个关键键。
**原因**：F3 修复前测试污染生产文件。
**F2 修复后果**：`_load_state` 现在会**拒绝加载**这样的损坏文件，硬失败让 cron 邮件走出来。所以**必须**先修好文件再跑。

**恢复步骤（2026-07-17 更新：已有半自动脚本，手工步骤作废）**：
1. `ssh` 到 GCP VM，`cd Trade-quant/live/`
2. `python3 tools/repair_state.py` 查看方案 → `--yes` 写回（自动备份、自动向 broker 核实每笔 pending 订单）
3. 已确认 `tq-retry-20260708-nvda` 在 broker 侧 404（从未被接受），脚本会自动剔除
4. 核对脚本打印的 broker 持仓 vs dashboard，不一致以 broker 为准

**用户决定（2026-07-16）**：此项推迟到"所有策略完成后"执行；推迟期间云端 cron 因 F2 校验硬失败而停摆。完整 VM 操作清单见 `live/deploy/MOC_SWITCH_RUNBOOK.md`。

---

## 4. 后续任务清单（按优先级）

### P1：MOC 单段路径 paper 验证（预计 1-2 周）

**目标**：在切默认前先确认 MOC 二腿 + LOO 兜底在真实 broker 上按预期工作。

**进度（2026-07-17）**：代码侧已全部就绪——本地三段 dry-run 通过、117 个单测通过、
切换操作已固化为 `live/deploy/MOC_SWITCH_RUNBOOK.md`。剩余步骤全部在 VM 上，照 runbook 执行即可。

**步骤**：
1. GCP VM 上 `.env` 设 `EXEC_MODE=moc_single`，重跑 `deploy/cron_setup.sh`；
2. 验证 cron 已切成 execute (15:35) + reconcile (09:45) 两条；
3. 触发一个完整调仓周期（plan → execute → reconcile），观察：
   - MOC 二腿在 16:00 收盘拍卖是否都成交（`filled_qty` 匹配）；
   - `LOO_FALLBACK` 分支是否被触发过（如触发，看限价保护是否合理）；
   - reconcile 是否正确补挂止损（`stop_added` 邮件字段）；
   - `state.json` 中 `pending_execute` 在 reconcile 完成后被清空；
4. 至少跑完 3 个完整调仓周期（约 3-4 周）无异常后，考虑切成 IBKR live 的默认。

**回退**：任何时候可以把 `.env` 改回 `EXEC_MODE=three_phase` 重跑 cron_setup.sh，立即切回历史路径。

### P2：账户级每日对账 cron（H5 兜底）

**目标**：无论走哪条路径，每日盘后比对 broker 实际持仓 / 挂单 / equity 与 state.json 记账，发现漂移即邮件报警。

**要点**：
- 新增 `live/daily_reconciliation.py`，独立 cron 15:55 ET 运行；
- 比对项：持仓集合 + 每仓活跃止损单存在性 + 无 `tq-` 前缀之外的意外挂单 + equity 对账（±0.5% 容差）；
- 差异任一即紧急邮件；
- 与 `service_watchdog.py` 独立，watchdog 只看 paper_runs.csv 新鲜度不做对账。

### P3：参数 walk-forward 验证

**目标**：确认 `bear_flat_days=45`、`dd_ewma_span=5`、`min_score=1.0` 等参数不是样本内过拟合。

**难点**：现有 `factor_combo_optimize.py:369-399` 的 hold-out 是"假 OOS"（用最终参数在全样本跑一遍再切开）——重写需要真正的滚动窗口。

**思路**：把 2022-2026 切成滑动 6-12 个月的训练 + 3 个月的测试窗口，每个窗口独立选参数，最后拼接 OOS 净值曲线看是否稳定。

### P4：Lumibot 迁移调研

**目标**：把策略搬去 Lumibot，切 IBKR live 时执行层由框架接管。

**要点**：
- Lumibot 的 `time_in_force="cls"` / `"opg"` 对应 MOC/LOO；
- 策略类的 `on_trading_iteration` 里 `import` 现有 `research/compute_factors` 等模块，保持单一来源；
- Alpaca paper + IBKR live 都是 Lumibot 一等公民，`.env` 切换 broker；
- 参考文档：https://lumibot.lumiwealth.com/brokers.alpaca.html / brokers.interactive_brokers.html

### P5：清理陈旧代码

- `alpaca_trader.py:2970` 附近的 `rebalance()` 一次性路径仅供 dry-run，MOC 稳定后可删；
- ~~阈值常量三处重复 → 单源化~~ ✅ 2026-07-17 完成：`research/strategy_params.py` 是
  TOP_N/MIN_SCORE/VOL_MIN/REBALANCE_DAYS/KILL_DD 的单一来源，三处均改为导入。

---

## 5. 关键文件速查

### 5.1 研究侧

| 文件 | 用途 |
|---|---|
| `research/factor_combo_backtest.py` | 回测主脚本，新默认=平衡档 |
| `research/factor_combo_backtest.py:233-` | `run_backtest_live` 引擎（MOC 单段 + 三件套 + 静默期熔断） |
| `research/factor_combo_backtest.py:180-210` | `_drawdown_exposure_multiplier` / `_regime_exposure_multiplier` |
| `research/build_universe.py` | 股票池；`--include-delisted` 尝试回补（yfinance 全失败，已固化 warning） |
| `research/factor_scanner.py:90-115` | `load_panel(include_open=True)` 供 T+2 开盘成交口径使用 |
| `research/RESEARCH_LOG.md` | 十四阶段完整演进记录；§12-14 是本轮工作 |

### 5.2 live 侧

| 文件 | 用途 |
|---|---|
| `live/alpaca_trader.py:52-95` | 顶层路径与 EXEC_MODE 常量 |
| `live/alpaca_trader.py:175-219` | `_load_state` schema 校验（F2 修复） |
| `live/alpaca_trader.py:299-330` | `build_moc_market_order` + `build_loo_limit_order` |
| `live/alpaca_trader.py:1626-1633` | 状态机 key 定义（含新增 `PENDING_EXECUTE_KEY`） |
| `live/alpaca_trader.py:1798-1830` | F1 修复：sell 阶段实时持仓夹取 |
| `live/alpaca_trader.py:2090+` | `execute_moc_phase`（MOC 单段主逻辑） |
| `live/alpaca_trader.py:2290+` | `reconcile_moc_execute`（MOC 单段核实） |
| `live/tests/conftest.py` | 全局测试隔离（F3 修复） |
| `live/tests/test_moc_single_execution.py` | 15 个 MOC 单测 |
| `live/deploy/cron_setup.sh:84-125` | 按 EXEC_MODE 条件注册 cron |
| `live/.env.example:14-33` | ORDER_TIF/EXEC_MODE/MOC_CUTOFF/LOO_FALLBACK 配置 |

### 5.3 文档

| 文件 | 内容 |
|---|---|
| `FIX_PLAN.md` | 完整审计与修复计划；§3.5 是 MOC 路径落地进度 |
| `HANDOFF.md`（本文件） | 新对话起点 |
| `research/RESEARCH_LOG.md` | 十四阶段策略演进日志 |
| `README.md` | 项目总览（**注意其中的 +113% 数字已过时**，以 RESEARCH_LOG §14 为准） |

---

## 6. 复现命令速查

```bash
# ── 研究：三档回测对比 ──────────────────────────────────
cd Trade-quant/research
python3 factor_combo_backtest.py                        # 默认=平衡档 (+39.7%/0.89/-38.5%)
python3 factor_combo_backtest.py --bear-flat-days 5     # 激进 (+44.0%/0.92/-42.9%)
python3 factor_combo_backtest.py --exec-mode legacy     # 复现 README 的 +110% 幻影
python3 factor_combo_backtest.py --exec-stage two_leg   # 原两段时序对比

# ── live 侧本地干跑 ────────────────────────────────────
cd Trade-quant/live
python3 alpaca_trader.py --phase plan --dry-run --force
python3 alpaca_trader.py --phase execute --dry-run --force   # 需 EXEC_MODE=moc_single
python3 alpaca_trader.py --phase reconcile --dry-run --force

# ── 生产切换 MOC 单段 ─────────────────────────────────
# 1) SSH 到 GCP VM
# 2) 编辑 live/.env，把 EXEC_MODE=three_phase 改成 EXEC_MODE=moc_single
# 3) 重跑：cd Trade-quant/live && bash deploy/cron_setup.sh
# 4) 验证 crontab -l 已经从 3 条主策略行（plan/sell/buy）变为 3 条（plan/execute/reconcile）

# ── 测试（GCP VM Python 3.10+，本地 3.9 跑不了） ────────
cd Trade-quant/live
python3 -m pytest tests/                                 # 全套
python3 -m pytest tests/test_moc_single_execution.py -v  # 只跑 MOC 单测
```

---

## 7. 尚未解决的疑问 / 决策点

1. **`bear_flat_days=45` 具体数值的样本外泛化能力未验证**（RESEARCH_LOG §14.2 已警示）。做 P3 walk-forward 之前，实盘应从最小额度起步。
2. **MOC 单段路径未做真金实盘验证**：本轮所有测试都在代码/回测层面，未在 Alpaca paper 上真的跑一次周期。**P1 是切默认前的门槛**。
3. ~~`state.json` 云端恢复方案~~ ✅ 2026-07-17 已做：`live/tools/repair_state.py`（默认 dry-run，`--yes` 写回，自动向 broker 核实 pending 订单终态）。
4. **回测的幸存者偏差无法根除**：yfinance 对退市股 0 数据可得性。彻底修复需要付费 point-in-time 数据（Norgate / Sharadar / QC 自带），暂未列为强制项。
5. **`ORDER_TIF` 从 `opg` 改为 `day` 的默认变更**：旧生产 `.env` 若沿用 `opg`，`three_phase` 路径的市价兜底会在盘中被拒。切换时机需要与用户确认。

---

## 8. 上下文交接

- 本轮工作历时约 6 小时，覆盖第十二/十三/十四阶段。
- 用户偏好：中文回答；实用主义（宁可少 alpha 也要能存活）；关注"确保成功换仓"这个具体执行目标；愿意接受工程实现细节的取舍。
- 避免的坑：
  - 用户明确否决过"永久熔断锁死"设计，坚持要静默期可复出；
  - 用户敏锐指出 `soft_drawdown` 应按 regime 门控，实测证明方向正确；
  - 用户信任详细数据表 + 消融对比来做决策，避免抽象讨论。
- 云端 state.json 恢复**由用户自行操作**（现在不是交易日不急）；本对话不写云端。
