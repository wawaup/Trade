# Trade-quant

个人量化交易项目：从因子研究到 Alpaca Paper/Live 自动化执行的完整链路。

策略核心是一套多因子选股 + 宏观状态开关的截面动量策略，在约 166 只半导体/AI 成长股组成的股票池中每 5 个交易日调仓一次，持有 Top-5 等权仓位。当前处于 **Paper Trading 验证阶段**，尚未实盘（Live）。

## 项目结构

```
Trade-quant/
├── research/         因子研究与回测（离线，Jupyter/脚本均可）
├── live/             Alpaca Paper/Live 自动化执行脚本
├── data/             本地缓存的历史行情（parquet，yfinance 下载）
└── report/           回测生成的 HTML 报告与图表
```

### research/ — 因子研究

| 文件 | 作用 |
|---|---|
| `build_universe.py` | 构建 166 只测试股票池（XSD/SOXX/ARKK/ARKW/IGV 成分股 + 手动补充） |
| `fetch_ohlcv.py` | 从 yfinance 下载日线 OHLCV，落盘为 parquet |
| `compute_factors.py` / `factor_scanner.py` | 因子计算（动量、反转、量价、波动率等）与截面 IC 扫描 |
| `factor_combo_backtest.py` | 当前正式策略的多因子合成回测（RS_Beta + MFI_14 + BIAS_20 + HV_ratio + QQQ MA50 状态机） |
| `factor_combo_optimize.py` | 参数扫描 / 消融实验框架（权重扫描、Top-N 扫描、Hold-out OOS 验证等） |
| `factor_earnings_impact.py` | 财报事件对持仓的冲击分析 |
| `factor_store.py` | 因子缓存存取 |
| `order_feasibility_audit.py` | 调仓历史成交可行性审计（信号有效性/下单量/流动性冲击/资金充裕性/跳空风险） |
| `RESEARCH_LOG.md` | **完整研究日志**，记录了从初始 6 因子到最终策略确定的全部实验过程与结论 |

当前正式策略（详见 `RESEARCH_LOG.md` 第九阶段）：

- **因子**：RS_Beta（残差动量）、MFI_14（资金流量）、BIAS_20（均值回归，负权重）、HV_ratio（波动率）
- **状态机**：QQQ 收盘价 vs MA50，牛市/熊市/震荡三态，各态独立权重矩阵
- **过滤**：`Combo_Score > 1.0` 且 `Vol_Shock > 1.2×`
- **持仓**：Top-5 等权，每 5 个交易日调仓
- **样本内绩效（2022-01-01 ~ 2026-06-18）**：年化 +113.3%，Sharpe 1.36，最大回撤 −52.2%（⚠️ 存在数据窥探偏差，真实表现以 Paper 实盘为准）

运行研究脚本：

```bash
cd research
pip install -r requirements.txt
python build_universe.py
python fetch_ohlcv.py
python factor_combo_backtest.py
```

### live/ — 实盘执行

基于 [alpaca-py](https://github.com/alpacahq/alpaca-py) 的自动化交易脚本，复用 `research/` 中的信号计算逻辑，通过 cron 定时驱动。

核心文件：

| 文件 | 作用 |
|---|---|
| `alpaca_trader.py` | 主策略脚本：三段式调仓状态机（plan → sell → buy）、风控熔断、止损单管理、邮件日报 |
| `alpaca_verify.py` | 部署前连通性验证（API Key、行情数据） |
| `universe_monitor.py` | 每日记录股票池/ARKK 成分变化，默认只记录不自动覆盖 |
| `service_watchdog.py` | 独立监控最近一次运行时间，主脚本未启动时报警 |
| `review_api.py` | 只读复盘 HTTP API（Bearer Token 鉴权，建议只绑定 127.0.0.1 + SSH tunnel） |
| `local_review_compare.py` | 本地拉取云端复盘数据，与本地口径比对 |
| `deploy/cron_setup.sh` | GCP VM 部署与 cron 注册向导 |
| `deploy/run_trader.sh` | cron 调用的执行包装脚本（含超时保护） |
| `REVIEW_LOG.md` | 三段式状态机复审日志 |

**三段式调仓状态机**（避开"盘后计算信号 + 盘后下单"的时序问题，同时确保资金衔接）：

1. **plan**（T 日收盘后 16:05 ET）：计算目标持仓与买卖计划，写入 `state.json`，不下单
2. **sell**（T+1 尾盘前 15:50 ET）：按计划挂限价卖单（吃买一价），确保收盘前成交
3. **buy**（T+2 开盘前 09:15 ET）：核实 T+1 卖单实际成交情况，用真实可用资金提交买单

风控机制：

- 账户级 Kill Switch：回撤达到 `KILL_DD` 阈值时清仓熔断并锁定，需人工解除
- 个股级 GTC Stop Order：新建仓位自动挂止损单
- 财报避雷：未来 N 个交易日内有财报的标的不建仓/强制平仓
- LULD 熔断重试：开盘集合竞价失败后按计划重试至截止时间
- Live 模式二次确认（`LIVE_CONFIRM=YES`）+ 同一信号日幂等保护，防止重复下单

部署与运行：

```bash
cd live
pip install -r requirements.txt
cp .env.example .env   # 填入 Alpaca API Key 等配置

python alpaca_verify.py                        # 验证连通性
python alpaca_trader.py --dry-run --force      # 本地预演，不下单

bash deploy/cron_setup.sh                      # GCP VM 上注册 plan/sell/buy 三段 cron
```

测试：

```bash
cd live
python -m pytest tests/ -q
```

## 数据

`data/` 目录存放 yfinance 下载的日线 parquet 缓存（`{symbol}_1d_raw.parquet`），由 `research/fetch_ohlcv.py` 生成，`live/` 与 `research/` 共用同一份历史数据。实盘信号计算的行情数据源默认改为 Alpaca（`MARKET_DATA_SOURCE=alpaca`），yfinance 仅用于离线研究和历史回测。

## 当前状态

- ✅ 因子研究与策略确定（`research/`，见 `RESEARCH_LOG.md`）
- ✅ Paper Trading 自动化执行、风控熔断、审计落盘、日报/告警（`live/`，见 `REVIEW_LOG.md`）
- 🔜 尚未 Go Live：滑点日报、信号一致性自动校验、stop order 生命周期管理、交易日历/半日市校验待补齐（详见 `RESEARCH_LOG.md` 第十一阶段"待研究事项"）

## 免责声明

本项目为个人学习与研究用途，回测结果存在样本内偏差，不构成任何投资建议。实盘交易存在亏损风险，使用前请充分理解代码逻辑并自行承担风险。
