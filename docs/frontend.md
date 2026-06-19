# Trade Frontend Page Design

本文档描述 Trade 当前前端页面的产品设计、信息架构、交互模型和实现契约。它用于指导后续 `web/index.html`、`web/app.js`、`web/styles.css` 的迭代，避免页面再次变成装饰型工作台。

## 设计目标

Trade 前端不是营销页，而是本地量化研究和 paper 执行工作台。用户打开页面后，应能在同一个工作流里完成：

1. 查看当前策略、行情和风控状态。
2. 调整标的、仓位、样本和交易摩擦参数。
3. 触发真实后端回测，而不是静态假数据。
4. 查看 K 线、VWAP、收益指标、资产行、风险灯和订单日志。
5. 通过 paper-only 快速订单验证执行链路。
6. 查看数据源和已保存回测结果。

第一屏必须直接进入可操作工作台，不出现 landing page、宣传说明或大面积空白 hero。

## 用户和使用场景

目标用户是需要反复调参、检查信号和验证回测结果的策略研究者。使用场景通常是桌面端、专注模式、信息密度较高，用户希望快速扫描：

- 哪个标的正在观察。
- 当前数据来自哪个 source。
- 回测是否真的跑了后端。
- paper 订单是否成交或被拒绝。
- 风控拒绝原因是什么。
- 最近结果是否被持久化。

页面应传达“工业级工作台的雏形”，而不是“演示 demo”。所有 live、quick trade、strategy live 文案都必须保持 paper-only 边界，不能暗示已经接入真实交易。

## 总体信息架构

当前页面采用 QuantDinger 风格的左侧导航 + 顶部状态栏 + 主工作区结构。

```text
qd-app-shell
  qd-sidebar
    brand
    nav
    paper-only footer
  qd-main
    qd-topbar
    qd-workspace
      indicatorIdePage
      strategyLivePage
      paperOrdersPage
      dataResultsPage
  knowledgeCard
```

### 左侧导航

左侧导航负责切换主页面，不承载二级配置。

页面项：

| 页面 | DOM id | 目的 |
|---|---|---|
| IDE 指标开发 | `indicatorIdePage` | 默认工作台，承载策略代码形态、回测控制、图表和 quick paper 订单 |
| 策略与实盘监控 | `strategyLivePage` | 展示策略列表、paper 监控、实时持仓、日志和保护规则 |
| Paper 订单 | `paperOrdersPage` | 聚合展示 paper order/fill 日志 |
| 数据与结果 | `dataResultsPage` | 展示数据源和回测结果历史 |

导航按钮必须使用 `data-page-target` 绑定到对应 page id。切页只改变页面显示状态，不重置用户已填参数。

### 顶部状态栏

顶部栏展示系统级动作和状态：

- 菜单按钮：收起或展开侧边栏。
- 刷新按钮：重新请求静态状态、live 状态、数据源、paper 订单、结果历史和 K 线。
- 连接状态：`连接中`、`刷新中`、`已连接`、`连接失败`、`回测失败：...`。
- 更新时间：使用本地时间显示最近一次成功刷新时间。
- `paper_only=true`：始终可见，强化安全边界。

顶部栏不放复杂筛选项，筛选和交易参数留在页面内部。

## 页面一：IDE 指标开发

`indicatorIdePage` 是默认页，也是核心工作流页面。它使用三列布局：

```text
qd-code-panel | qd-market-stage | qd-quick-trade
```

### 左列：策略脚本和仓位分配

左列承载“策略是可解释、可配置的”这个心智。

区域：

| 区域 | 目的 | 当前状态 |
|---|---|---|
| 代码编辑器标题 | 告诉用户这里对应策略逻辑 | 已显示 |
| 代码 slab | 展示 T 策略、VWAP、paper-only order intent 的伪代码形态 | 只读 |
| 脚本编辑按钮 | 保留后续能力入口 | disabled，文案为暂未开放 |
| 仓位分配设置 | 修改总账户金额、每个 symbol 总仓位和 T 仓比例 | 已接后端 `/api/allocation` |

仓位表单规则：

- `totalAccountQuote` 必须大于 0。
- 所有 symbol 的 `totalPct` 合计不能超过 100%。
- 每个 symbol 的 `tPct` 不能大于自身 `totalPct`。
- 校验失败时，应在字段上加错误态，并显示中文原因。
- 保存成功后，重新拉取静态和 live 状态。

### 中列：行情、回测参数和结果

中列是页面视觉和任务重心。

顶部控制条：

- 观察池摘要：展示当前 watchlist。
- 时间周期按钮：`1m`、`5m`、`15m`、`1H`、`1D`。
- 运行回测按钮：调用 `POST /api/backtest/run`。

K 线主视窗：

- 使用 `lightweight-charts` 渲染 candle 和 VWAP。
- 数据来自 `GET /api/klines?symbol=&resolution=...`。
- candle 数据是 `[time, open, high, low, close]`。
- VWAP 数据是 `[time, value]`。
- 图表库加载失败时，显示非阻断空状态，表格和控制仍可用。

回测参数：

| 控件 | 当前字段 | 行为 |
|---|---|---|
| 标的复选框 | `backtestSymbol` | 任意 change 都触发回测 |
| 样本区间 segmented | `sampleSplit` | 当前前端保留状态，后端暂未使用 |
| 数据源 select | `dataSourceSelect` | 传给 K 线和回测接口 |
| Daily CSV 路径 | `csvDailyPath` | 非空时作为 `dailyPath` 传给 CSV 数据源 |
| Intraday CSV 路径 | `csvIntradayPath` | 非空时作为 `intradayPath` 传给 CSV 数据源 |
| 滑点 bps | `slippageBps` | 传给回测接口 |
| 点差 bps | `spreadBps` | 传给回测接口 |

结果 tabs：

| Tab | 内容 | 数据来源 |
|---|---|---|
| 回测结果 | summary metric 和 asset rows | `/api/state/static` 或 `/api/backtest/run` |
| 参数敏感度示例 | 静态参数敏感度展示样式 | `/api/state/static` |
| 压力情景示例 | 静态压力情景展示样式 | `/api/state/static` |
| 成交明细 | 当前为空状态，后续可接 result detail 的 trades/orderIntents | 待接入 |

每次参数变化触发回测时，页面必须更新 summary、asset rows、K 线和结果历史，不能只改前端状态。“参数敏感度示例”和“压力情景示例”当前只是静态示例，不能被当作真实调参或压测结果。

### 右列：Quick Paper Trade

右列用于快速验证 paper 执行链路，不提供实盘能力。

区域：

| 区域 | 目的 |
|---|---|
| 账户摘要 | 展示当前 paper cash 或首个持仓 symbol |
| 买入做T | 发送 paper buy |
| 卖出减T | 发送 paper sell |
| Amount | 输入模拟下单金额 |
| 订单类型 | 当前仅市价单可用，限价单 disabled |
| 风控指示灯 | 从 live positions 推导点差、插针、T 仓、虚拟钱包状态 |
| Paper 订单日志 | 展示最近 paper orders |

下单请求：

```json
{
  "symbol": "当前 activeSymbol",
  "side": "buy 或 sell",
  "quoteAmount": 350,
  "orderType": "market"
}
```

交互要求：

- 金额必须大于 0。
- 成功成交显示“模拟订单已成交”。
- 被拒绝显示“模拟订单被拒绝：原因”。
- 接口错误显示“模拟下单失败：错误”。
- 下单完成后刷新 paper 订单、账户摘要和结果区相关数据。

## 页面二：策略与实盘监控

`strategyLivePage` 的名称沿用 QuantDinger 式产品结构，但当前只展示 paper 监控。页面必须明确“模拟监控”，不能让用户误以为实盘已经打开。

布局：

```text
qd-strategy-list | qd-live-detail
```

左侧策略列表：

- 从 live positions 动态渲染当前 symbol 状态。
- 每行显示策略名、symbol、状态、层数和 spread。
- 默认第一行 active。
- “创建策略”按钮保留入口，但必须 disabled。

右侧详情：

- hero 区展示当前策略名和 `模拟监控`。
- “停止策略”按钮 disabled。
- 指标区复用回测 summary：总收益、最大回撤、胜率、盈亏比。
- live chart 复用 K 线/VWAP。
- 实时持仓表显示 symbol、状态、最新价、T 层数、总预算、T 预算、点差、风险。
- 策略日志使用 terminal 区，并自动滚动到底部。
- 保护规则列表展示后端返回 guardrails。

## 页面三：Paper 订单

`paperOrdersPage` 是独立订单审计页。它应比 quick trade 侧栏更适合长列表检查。

表格列：

| 列 | 字段 |
|---|---|
| 标的 | `symbol` |
| 方向 | `side` |
| 信号 | `sourceSignal` 或 `source_signal` |
| 状态 | `status` |
| 原因 | `reason` |

空状态文案为“暂无 Paper 订单。”。

后续升级方向：

- 增加成交价格、数量、fee、createdAt。
- 增加 order intent 与 fill snapshot 的分组展示。
- 增加 reset paper account 按钮，但必须二次确认。

## 页面四：数据与结果

`dataResultsPage` 让用户知道当前数据源能力和最近回测是否真的持久化。

数据源卡片：

- 来自 `GET /api/data-sources`。
- 显示 `id`、`label`、是否需要网络。
- `Synthetic` 应标记为本地可用。
- `Binance` 应标记为需要联网。

回测结果历史：

- 来自 `GET /api/backtest/results`。
- 展示 `resultId`、`createdAt`、`engineVersion`、`summary.totalReturnPct`。
- 空状态文案为“暂无保存的回测结果。”。

后续升级方向：

- 点击结果行请求 `GET /api/backtest/results/<id>`。
- 展开 trades、orderIntents、riskEvents、configSnapshot、executionAssumptions。
- 支持导出 JSON。

## 浮层：金融概念逐字说明

`knowledgeCard` 是左下角固定浮层，用于解释金融术语。它不是主导航，也不应遮挡核心工作台。

行为：

- 点击“金融概念逐字说明”展开或收起。
- 点击卡片外部自动收起。
- 内容来自 `/api/state/static` 的 glossary。
- 卡片内部滚动条隐藏得尽量轻，但仍可滚动。

注意：该浮层不能用 `<details>`，因为当前测试要求显式控制展开/收起逻辑。

## 数据流和 API 映射

启动流程：

```text
boot()
  bind navigation and controls
  initTradingChart(backtestChart)
  initTradingChart(liveChart)
  loadStaticState()
  refreshLiveState()
  loadPlatformData()
  loadKlines("NVDA")
  setInterval(refreshLiveState, 2000)
```

接口契约：

| 函数 | API | 更新区域 |
|---|---|---|
| `loadStaticState()` | `GET /api/state/static` | glossary、summary、asset rows、sensitivity、stress、allocation |
| `refreshLiveState()` | `GET /api/state/live` | live rows、guardrails、terminal、risk lights、strategy list |
| `loadKlines()` | `GET /api/klines` | backtest chart、live chart |
| `runBacktestFromControls()` | `POST /api/backtest/run` | summary、asset rows、K 线、结果历史 |
| `loadPlatformData()` | data sources、paper orders、backtest results | 数据源卡片、paper order 表、账户摘要、结果历史 |
| `saveAllocation()` | `POST /api/allocation` | allocation form、static/live state |
| `submitPaperOrder()` | `POST /api/paper/orders` | order message、paper orders、账户摘要 |

错误处理原则：

- 网络异常显示连接失败，并保留已有数据。
- 回测失败写入顶部连接状态，不清空已存在结果。
- 表单错误就地显示，不弹 modal。
- 图表失败不阻断表格和参数控制。

## 视觉设计规范

主题：深色、密集、偏工业工具感。

视觉原则：

- 主体是工作区，不是卡片拼贴。
- 页面 section 使用明确边界，但避免卡片套卡片。
- 表格、图表和参数区尺寸稳定，避免数据刷新导致布局跳动。
- 按钮文案要直接表达动作：运行回测、刷新数据、保存仓位比例、买入做T、卖出减T。
- disabled 能力必须写“暂未开放”，让用户知道不是坏了。
- 所有实盘相关入口必须显示 paper/simulated 边界。

颜色：

| token | 用途 |
|---|---|
| `--bg` | 页面底色 |
| `--sidebar` | 左侧导航 |
| `--surface` | 主容器 |
| `--surface-2` | 面板 |
| `--surface-3` | 次级控件 |
| `--blue` | 主操作和 active 状态 |
| `--green` | 正收益、安全、买入按钮 |
| `--red` | 负收益、危险、卖出强调 |
| `--amber` | 警告 |

控件规范：

- segmented control 用于周期、样本区间、结果 tab、订单类型。
- checkbox 用于多标的选择。
- number input 用于资金、仓位百分比、滑点、点差、下单金额。
- disabled button 保留未来能力，不允许触发请求。
- 表格必须包在 `.table-wrap` 中，窄屏允许横向滚动。

## 响应式设计

桌面优先，但不能在窄屏断裂。

断点行为：

- 大屏：`qd-ide-grid` 三列，左代码、中图表、右 quick trade。
- `max-width: 1280px`：核心工作台折叠为单列，代码、市场、quick trade 纵向排列。
- 策略监控页在窄屏下从双列变单列。
- 表格通过 `.table-wrap` 横向滚动，不压缩到不可读。
- 侧边栏可通过菜单按钮收起，释放横向空间。

移动端不隐藏核心功能，但可以改变顺序：控制区在图表之前，订单区在结果之后。

## 可访问性和中文文案

基础要求：

- 页面语言是 `zh-CN`。
- 主导航有 `aria-label`。
- disabled 按钮使用 `disabled` 和 `aria-disabled="true"`。
- 顶部 icon 按钮需要 `aria-label` 和 `title`。
- 输入错误必须有可见文本说明。
- 不使用仅靠颜色表达风险：风险灯同时显示文本状态。

中文文案规则：

- 使用“模拟”“Paper”“暂未开放”明确边界。
- 避免“实盘已连接”“自动交易已启动”这类未实现承诺。
- 错误信息要告诉用户怎么改，例如“总仓位合计不能超过100%”。
- 金融术语首次出现时，可通过 glossary 解释，不在主界面堆长说明。

## 前端测试契约

当前 `tests/test_frontend_files.py` 锁定以下内容：

- 页面必须包含 QuantDinger 风格 shell：`qd-app-shell`、`qd-sidebar`、`qd-code-panel`、`qd-market-stage`、`qd-quick-trade`。
- 必须保留中文页面项：IDE 指标开发、策略与实盘监控、Paper 订单。
- 必须保留核心按钮和状态：运行回测、刷新数据、买入做T、卖出减T、市价单、限价单暂未开放。
- 必须使用诚实占位文案：参数敏感度示例、压力情景示例；在真实后端分析完成前不能使用“智能调参”“压力测试”作为已实现能力。
- 必须保留四个 page root：`indicatorIdePage`、`strategyLivePage`、`paperOrdersPage`、`dataResultsPage`。
- JS 必须请求真实 API：`/api/state/static`、`/api/state/live`、`/api/backtest/run`、`/api/klines`、`/api/data-sources`、`/api/paper/orders`、`/api/backtest/results`。
- CSS 必须保留 risk lights、knowledge card、错误 input、source card、paper order table、result history table 等关键样式。

修改前端后至少运行：

```bash
PYTHONPATH=. python3 -m unittest tests.test_frontend_files -v
```

如果同时改了 API 字段，还要运行：

```bash
UV_CACHE_DIR=.uv-cache PYTHONPATH=. uv run pytest tests/test_server.py tests/test_api_split.py tests/test_frontend_files.py -q
```

## 当前边界

- 代码编辑器只是只读策略展示，不支持真实编辑和保存。
- `sampleSplit` 当前是前端状态，后端暂未使用，不能视为样本内/样本外验证。
- 成交明细 tab 尚未接 result detail。
- quick trade 只支持 market paper order。
- 策略与实盘监控页名字保留 QuantDinger 结构，但当前只允许 paper 模拟监控。
- IDE 页已经提供页面级 source selector 和 CSV 路径输入；数据与结果页的数据源卡片仍只展示能力。
- 页面暂未实现结果 detail drawer、导出 JSON、paper reset 二次确认。

## 后续页面迭代顺序

1. 接入 result detail，补齐成交明细、order intents、risk events。
2. 将 quick trade 的账户摘要扩展为现金、持仓、市值、最近 fill。
3. 为 Paper 订单页增加 reset paper account，但必须二次确认。
4. 将代码 slab 升级为只读策略协议说明，再考虑真实编辑能力。
5. 给移动端做专门排序：参数、图表、结果、订单、日志。
