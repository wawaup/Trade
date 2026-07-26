# Trade-quant 修复计划（FIX_PLAN）

> 编制日期：2026-07-11
> 依据：2026-07-11 全项目审计（live 执行链路逐行审查 + research 信号管线审查）
> 总体结论：**当前系统不可直接投入真金实盘。** 修复分三条线并行推进：
> ① 紧急处置（当天完成）② 研究侧修复（本文档执行重点，先行）③ live 执行层修复或迁移。

---

## 0. 紧急处置（P0，立即执行）

### 0.1 生产 state.json 已损坏，下次运行必崩

**现状**：`live/state.json` 只剩 `pending_buy`（NVDA×2，`tq-retry-20260708-nvda`，status=query_error），
缺失 `high_watermark` / `kill_switch` / `last_rebalance` 三个关键键。
下次任何 phase 运行到 `alpaca_trader.py:2795` 的 `state["high_watermark"]` 即 KeyError。
根因：测试套件未与生产文件隔离（见 2.3），跑 pytest 时把测试 fixture 写进了真实 state.json。

**处置步骤**：

1. 立即从 `state.json.bak` 恢复 `high_watermark`、`kill_switch`、`last_rebalance`，保留现有 `pending_buy`；
2. 登录 Alpaca 后台，人工核实 `tq-retry-20260708-nvda` 订单的真实终态（成交/取消/拒绝），
   据此修正 `pending_buy` 或清空；
3. 核对当前账户实际持仓 vs state.json 记账是否一致，不一致以 broker 为准修正；
4. **在 2.3 的测试隔离修复完成前，禁止在部署机上运行 pytest**。

---

## 1. 研究侧修复（P0，先行执行 —— 本轮工作重点）

> 原则：回测结论不可信时，修执行引擎没有意义。以下四项修完并重跑之前，
> 冻结一切实盘化决策；+113% / Sharpe 1.36 视为无效数字。

### 1.1 回测成交时点对齐实盘（消除"同收盘价成交"前视）

**问题**：`factor_combo_backtest.py:174-204` 用 prev 日因子选股、以 prev 日收盘价起算收益。
信号需要 prev 收盘价和全天成交量才能算出，等价于"收盘后算信号、以同一收盘价成交"。
策略过滤条件（Vol_Shock > 1.2 暴量）恰恰选中当天刚放量异动的股票，
隔夜跳空延续收益被全部记入，而实盘最早 T+2 早上才买入。

**修复方案**：`run_backtest` 增加与实盘三段式一致的执行模型（`--exec-mode live`，设为默认）：

- **信号**：T 日收盘后用 T 日数据计算（对应 live plan 阶段 16:05 ET）；
- **卖出**：T+1 日收盘价成交（对应 live sell 阶段 15:50 ET 吃买一价的限价单，
  用收盘价近似，方向略偏乐观，靠成本参数覆盖）；
- **买入**：T+2 日开盘价成交（对应 live buy 阶段 09:15 ET 提交、开盘集合竞价成交）；
- **资金衔接**：T+1 收盘到 T+2 开盘之间卖出资金空置，无收益；
- **持仓延续**：新旧目标共有的标的不卖不买、无成本（对应 live 的 HOLD）；
- **交易成本**：单边 `--cost-bps`（默认 25bp）作用于每笔卖出与买入；
- 保留 `--exec-mode legacy` 复现旧口径，用于前后对比。

**配套改动**：`factor_scanner.load_panel` 增加 `include_open` 参数返回 open 面板
（parquet 已含 open 列，现有 4 处调用方不受影响）。

### 1.2 回测加入实盘同款约束（消除"回测与风控规则矛盾"）

**问题**：live 有单票 50% 上限（`MAX_POSITION_PCT`）、−30% 熔断（`KILL_DD`）、整数股约束；
回测全无。实测 Top-N 扫 3/5/7/10/15 Sharpe 完全相同，说明双重过滤后候选常 ≤3 只，
回测在候选仅 1 只时 100% 单票满仓——回测的高收益段恰恰来自这些高集中期，实盘拿不到。
更致命：回测 MaxDD −52.2% > KILL_DD −30%，实盘规则下该路径会触发熔断永久锁死。

**修复方案**（均在 `--exec-mode live` 下生效）：

- 单票权重上限 50%（候选不足时多余资金留现金），与 `alpaca_trader.py:119` 对齐；
- `--min-stocks N`（默认 0=关闭，可选启用）：候选不足 N 只时本轮空仓；
- `--kill-dd -0.30` 熔断模拟：净值回撤触线即全仓清算（含成本）并停止交易，
  报告中同时给出"含熔断"与"不含熔断"两条净值，直观回答"这套风控下策略能不能活"。

### 1.3 股票池幸存者偏差缓解（诚实披露 + 尽力修补）

**问题**：`build_universe.py` 用 2026-06-23 的当前 ETF 成分回溯跑 2022 年起的回测；
`EXCLUDED_SYMBOLS`（104-118 行）系统性剔除已退市/私有化标的；
手动层（86-98 行）直接纳入事后已知的 AI 大赢家（SMCI、VRT、IONQ 等）。
这是对回测收益夸大最严重的单一因素。

**修复方案**（分两级，第一级本轮做，第二级列为后续）：

- **本轮**：`build_universe.py` 增加 `--include-delisted`：把 `EXCLUDED_SYMBOLS` 中
  yfinance 仍能取到历史数据的标的放回池子（universe.json 中以 `delisted` 字段标记），
  让它们的下跌/退市段重新参与截面打分与选股；同时在 universe.json 与回测报告中
  写入显式的幸存者偏差警示元数据。
- **后续（需付费数据）**：接入 point-in-time 成分数据（Norgate / Sharadar / QuantConnect
  自带数据均可），按历史快照重建池子。在此之前，**所有回测数字都应标注
  "幸存者池上限值"**。yfinance 对多数已退市标的不提供数据，此偏差无法在免费数据下根除。

### 1.4 重跑回测并记录（验收本节全部修复）

- 以 `--exec-mode live --cost-bps 25 --kill-dd -0.30`（+ 尽可能 include-delisted 后的池子）重跑；
- 与 legacy 口径并排对比：年化 / Sharpe / MaxDD / 熔断是否触发 / 触发日期；
- 结果无论好坏，如实写入 `RESEARCH_LOG.md` 新阶段（第十二阶段：执行保真度修正）；
- **决策门**：若修正后策略在含熔断口径下无法存活，或 Sharpe 显著恶化至无吸引力，
  实盘化工作停止，回到因子研究阶段；Paper 继续跑作为唯一真实 OOS。

### 1.5 后续研究债（本轮不做，列入待办）

- Walk-forward 参数选择（当前 Hold-out 被 2026 年 IC 因子筛选污染，属"假 OOS"）；
- IC 显著性检验改用非重叠区间或 Newey-West 修正（当前 5 日重叠收益 t 值虚高）；
- 信号一致性自动校验（research vs live 同日 Top-N 对比）与滑点日报闭环
  （RESEARCH_LOG 待办已列，优先级最高）；
- ~~阈值常量单源化~~ ✅ 2026-07-17 完成：`research/strategy_params.py` 单一来源，
  alpaca_trader / order_feasibility_audit / 回测 argparse 默认值三处均改为导入。

---

## 2. live 执行层修复清单（P0-P2）

> 若采纳第 3 节迁移方案，本节 2.1-2.2 仅需修到"paper 能安全继续跑"的程度；
> 2.3（测试隔离）无论如何必须修。

### 2.1 致命（真金实盘的资损路径，必须修）

| # | 缺陷 | 位置 | 修法 |
|---|------|------|------|
| F1 | sell 阶段不校验实时持仓：止损 T+1 盘中先成交后，15:50 卖单按 plan 数量照提，保证金账户下直接开出裸空头 | `alpaca_trader.py:1712-1713`、`1786-1899` | 提交前 `get_open_position` 将 qty 夹取到 `min(plan_qty, 实际可用)`；为 0 则跳过并记录"止损已代为出场"；buy 阶段对负持仓硬校验+报警 |
| F2 | `_load_state` 无 schema 校验，缺键直接 KeyError；高水位丢失会静默抬高熔断基准 | `alpaca_trader.py:171-181`、`2795` | 读取后与默认 schema 合并；关键键缺失时报警而非静默补默认；高水位缺失时取 `max(当前净值, bak 中记录)` |
| F3 | 测试直接 exec 生产模块，`retry_halted_orders` 内部 `_save_state` 写真实 state.json（已实际发生） | `tests/test_execution_safety.py:19-24`、`alpaca_trader.py:1031-1033` | 新增 `conftest.py` 全局把 STATE_FILE / LOG_FILE / AUDIT_DIR / HALT_PENDING_FILE patch 到 tmp_path；长期改依赖注入 |

### 2.2 高危（大概率触发，风控失效或周期卡死）

| # | 缺陷 | 位置 | 修法 |
|---|------|------|------|
| H1 | buy 阶段 duplicate client_order_id 被丢弃追踪：崩溃重跑后已成交买入不核实、止损不补挂，裸奔最长 5-7 交易日 | `alpaca_trader.py:2195-2208` | duplicate 分支照常 `pending_buy_orders.append` 再 continue（与 sell 阶段 1842-1843 对齐） |
| H2 | 加仓买单撞在挂 GTC 止损单的 wash-trade 拒单，周期卡死 | `alpaca_trader.py:2158-2211`、`2584-2621` | 买入前撤该标的止损单，reconcile 成交核实后统一重挂；paper 环境先复现验证 |
| H3 | 半日市无感知：15:50 卖单跨日排队 + 重试造成同一仓位卖两次，叠加 F1 变空头 | `alpaca_trader.py:157-158` + cron 固定 15:50 | 用 `mcal.schedule` 的 `market_close` 判定真实收盘时刻；buy 阶段对"未成交但仍存活"的卖单先撤再写回重试 |
| H4 | 止损裸露窗口无自愈（trim 后无买单 / 卖单未成交 / 撤止损后进程被杀三条路径） | `ensure_stop_orders_for_positions` 仅 `3024`、`2371` 两个真实调用点；`2219` 补挂被 `if dry_run` 限定 | buy 阶段真实运行也全量补挂止损；watchdog 增加"每个持仓必须有活跃止损"巡检；撤止损前意图落盘 |
| H5 | 无账户级对账：state.json 与 broker 漂移永不被发现 | 全代码库缺失 | 新增每日 reconcile 任务：持仓集合、止损存在性、意外挂单、equity 对账，差异即紧急邮件 |

### 2.3 中危（择要修复）

- `--retry-halted` 绕过 kill_switch 与 LIVE_CONFIRM（`alpaca_trader.py:2674-2717` 先于 2739/2763 执行）→ 入口补检查；
- 告警单通道：邮件失败仅 warning（`3146`）→ 加 webhook/SMS 第二通道，失败落盘重发；
- watchdog 只看最后一行、`--retry-halted` 不写 paper_runs.csv → 扫当日全部行；
- `ORDER_TIF` 模板（`.env.example:15` 推荐 opg）与代码（`alpaca_trader.py:88`）矛盾，市价兜底单会被拒 → sell 兜底硬编码 DAY；
- 实际调仓节奏 5+2 天 ≠ 回测 5 天 → 间隔改按 signal_date 计（若继续自研）；
- 时区：cron 机器非 America/New_York 时 `cron_setup.sh:74-82` 只警告不阻断，Debian 默认 cron 不支持 CRON_TZ → 改 exit 1 + 各 phase 入口 ET 时刻窗口自检；
- 财报避雷单点依赖 yfinance，查询失败默认放行（`705`）→ 失败标的按保守模式处理或双源交叉；
- 邮件附件全量 CSV 无限增长、trader.log 无轮转 → 附件只带当日切片 + logrotate；
- ProcessLock 跳过完全静默（`2661-2666`）→ 写审计行 + 锁文件时间戳校验。

### 2.4 低危（记录在案）

`.env.example` 携带 `SIM_CAPITAL_USD=5000`、碎股 `int()` 截断尾仓、fills 无分页、
`validate_panel` 用本地时区、BRK-B 类符号规范差异手工维护。

---

## 3. 执行层路线建议：迁移 Lumibot（推荐）

**判断**：第 2 节暴露的缺陷全部属于同一类——自建执行引擎对 broker 状态的单向假设
（不对账、不校验实时持仓、订单生命周期边角案例）。成熟框架已把这一层解决。
策略逻辑（因子计算 + 选股）是薄薄一层，可整体搬走。

**选型**（2026-07 调研）：

| 方案 | Alpaca paper | IBKR live | 结论 |
|---|---|---|---|
| **Lumibot** | ✅ 一等公民 | ✅ 一等公民 | **推荐**：纯 Python、活跃维护、同一份策略代码 .env 切换 broker |
| QuantConnect LEAN CLI | ✅ 官方插件 | ✅ 官方插件 | 备选：最健壮但需付费档账号 + 框架重写 + IBKR 周日手机重认证 |
| NautilusTrader | ❌ 仅 RFC | ✅ | 排除：不满足 Alpaca paper 要求 |

**迁移步骤**：

1. 研究侧修复（第 1 节）完成且策略通过决策门之后再动手；
2. 新建 `lumibot/` 目录，策略类在 `on_trading_iteration` 中 import 现有
   `compute_factors` / `zscore_factors` / `REGIME_WEIGHTS`（保持单一来源）；
   财报避雷、Kill Switch 作为策略层逻辑迁入（Lumibot 也有内置风控可叠加）；
3. Alpaca paper 上与现有 alpaca_trader.py 并行跑 ≥1 个完整调仓周期，逐日对比
   目标持仓与实际成交；
4. 并行期结束后停用自研执行层（保留 `order_feasibility_audit.py` 与审计思路）；
5. paper 跑完一个含财报季的完整周期、滑点日报确认损耗可承受后，
   小额切 IBKR live（需建无 2FA 副用户名供自动化登录）。

**若不迁移**：按 2.1 → 2.2 → 2.3 顺序修复，估计 1-2 天核心代码 + 每项补回归测试，
再以修复版跑完一个含财报季与半日市的完整 paper 周期方可考虑实盘。

---

## 3.5 MOC 单段路径落地进度（2026-07-12 更新）

按第 3 节推荐方案 A+，`alpaca_trader.py` 已新增 MOC 单段执行路径，与原三段式共存，
由 `.env` 中 `EXEC_MODE` 控制切换，不破坏现有部署。

**新增代码**：
- `build_moc_market_order` / `build_loo_limit_order`：CLS/OPG TIF 订单构造器（`alpaca_trader.py:299-330`）
- `execute_moc_phase`：T+1 15:35 提交 MOC 二腿，BUY 被拒时自动降级 LOO（`alpaca_trader.py:2090+`）
- `reconcile_moc_execute`：T+2 09:45 核实成交、补挂止损、清理 state
- `PENDING_EXECUTE_KEY` state 键与 `has_incomplete_cycle` 同步覆盖
- CLI `--phase execute` / `--phase reconcile` 分派与状态邮件
- `deploy/cron_setup.sh` 按 EXEC_MODE 条件注册两套 cron
- `live/tests/test_moc_single_execution.py`：15 个新用例覆盖上述所有分支
- `.env.example` 增加 EXEC_MODE / MOC_CUTOFF / LOO_FALLBACK 三个配置项

**审计攻击面变化**（对照 §2.1/§2.2）：

| 原缺陷 | MOC 单段路径下的状况 |
|---|---|
| **F1** sell 不校验实时持仓 → 裸空 | 已修（原路径）+ MOC 路径同款夹取逻辑（`execute_moc_phase` 内实时 qty 夹取） |
| **F2** state.json schema | 已修（新增 PENDING_EXECUTE_KEY 已纳入 has_incomplete_cycle） |
| **F3** 测试隔离 | 已修（新测试也走同一 conftest） |
| **H1** buy duplicate 丢追踪 | MOC 路径不复用 execute_buy_phase 的 pending_buy_orders 流程；reconcile 直接按 client_order_id 查真实成交，天然幂等 |
| **H2** 加仓撞 wash-trade | 保留问题（reconcile 补挂止损时若持仓已在挂止损会被 duplicate 拒绝，不影响资金安全）——可留待后续 |
| **H3** 半日市 15:50 卖单跨日 | **消失**：MOC 15:35 提交，硬截止 15:50 前必然被 broker 决定 accept/reject。半日市 Alpaca 会直接拒 CLS，走 fallback 而非跨日排队 |
| **H4** 止损裸露窗口 | **大幅收窄**：reconcile 阶段真实运行也补挂止损（原 dry_run 才补）；执行只在 T+1 收盘发生一次，中间无跨日空窗 |
| **H5** 无账户对账 | reconcile 阶段对每笔订单查 filled_qty + 检查负持仓；仍缺完整每日对账任务，后续补 |

**F1/H3/H4 三个高危项在 MOC 路径下从"设计难题"降级为"实现细节"**——原因是跨日 sell 与
"限价单不成交"这两个 root cause 在 MOC 路径下不存在。

**待办**：
- [ ] paper 上跑一个完整调仓周期（plan → execute → reconcile）验证 MOC 主路径 + LOO 兜底
- [ ] 加账户级每日对账 cron（H5 兜底，覆盖 EXEC_MODE 两条路径）
- [ ] `alpaca_trader.py:2970` 里 `rebalance()` 单次路径仍是旧三段思路，dry-run/手动测试用，
      迁 MOC 路径后可考虑一起清理

## 4. 验收标准

- [ ] state.json 恢复且与 broker 实际持仓对账一致；
- [ ] pytest 全量运行后生产文件哈希不变（测试隔离生效）；
- [ ] 回测 `--exec-mode live` 与 legacy 口径并排输出，差异已写入 RESEARCH_LOG 第十二阶段；
- [ ] 含 −30% 熔断模拟的净值曲线明确回答"实盘风控下策略是否存活"；
- [ ] universe.json 含幸存者偏差警示元数据与（可获得的）退市标的回补；
- [ ] 决策门通过后方启动 Lumibot 迁移；
- [ ] Lumibot 并行期逐日持仓对比无未解释差异后，方切换执行层；
- [ ] IBKR live 上线前：小额资金 + 双通道告警 + 每日对账任务就位。
