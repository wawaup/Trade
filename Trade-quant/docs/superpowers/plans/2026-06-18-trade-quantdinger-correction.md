# Trade QuantDinger Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the thin platform slice so Trade visually and structurally follows QuantDinger's workspace screenshots while filling the most important backend gaps left by the first pass.

**Architecture:** Keep Trade as Python standard-library backend plus static frontend, but replace the generic top-tab dashboard with a QuantDinger-style app shell: left product navigation, top toolbar, Indicator IDE workbench, Strategy & Live management view, and right-side paper execution panel. Backend additions remain lightweight and paper-only: persisted JSON result history, strategy protocol metadata, and paper order logs derived from standardized order intents.

**Tech Stack:** Python `unittest`, static HTML/CSS/JS, `lightweight-charts`, local JSON storage under project data directory.

---

## File Structure

- Modify `web/index.html`: rebuild shell around QuantDinger screenshot patterns.
- Modify `web/styles.css`: add dense dark product shell, IDE/code panel, chart center, quick-trade rail, strategy live master/detail view.
- Modify `web/app.js`: bind sidebar navigation, render paper quick-trade account state, strategy list, result history, and existing chart/backtest data into new DOM.
- Modify `tests/test_frontend_files.py`: replace weak four-page assertions with QuantDinger-shell contract assertions.
- Modify `tradebot/server.py`: persist recent backtest summaries into `BACKTEST_RESULTS`; expose paper order reset later if needed.
- Add/modify backend tests only where behavior changes.

## Task 1: QuantDinger Shell Contract

**Files:**
- Modify: `tests/test_frontend_files.py`
- Modify: `web/index.html`
- Modify: `web/styles.css`

- [ ] **Step 1: Write failing frontend shell test**

Assert that `web/index.html` contains:

- `class="qd-app-shell"`
- `class="qd-sidebar"`
- `Indicator IDE`
- `Strategy & Live`
- `Trading Bot`
- `id="indicatorIdePage"`
- `id="strategyLivePage"`
- `id="paperOrdersPage"`
- `class="qd-code-panel"`
- `class="qd-market-stage"`
- `class="qd-quick-trade"`

- [ ] **Step 2: Run frontend file tests**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: FAIL because the current UI still uses a generic top tab shell.

- [ ] **Step 3: Rebuild HTML shell**

Replace the current top-tab structure with a QuantDinger-style shell:

- Fixed left navigation.
- Top toolbar with menu/refresh/status.
- `indicatorIdePage` as the default active page.
- Three-column Indicator IDE page: code editor, chart/backtest center, paper quick-trade rail.
- `strategyLivePage` with left strategy list and right performance/detail panel.
- `paperOrdersPage` for raw order/fill history.

- [ ] **Step 4: Add shell CSS**

Add layout classes for the shell and ensure desktop density resembles the QuantDinger screenshots.

- [ ] **Step 5: Verify**

Run:

```bash
python3 -m unittest tests.test_frontend_files -v
python3 -m unittest discover -s tests
```

- [ ] **Step 6: Review and commit**

Review for missing DOM IDs used by `app.js`, then commit:

```bash
git add web/index.html web/styles.css tests/test_frontend_files.py
git commit -m "feat:重做QuantDinger风格前端外壳"
```

## Task 2: Frontend Data Binding Repair

**Files:**
- Modify: `tests/test_frontend_files.py`
- Modify: `web/app.js`

- [ ] **Step 1: Write failing JS contract test**

Assert `web/app.js` contains renderers/binders for:

- `switchWorkspace`
- `renderStrategyList`
- `renderQuickTradeAccount`
- `renderStrategyPerformance`
- `renderPaperOrders`

- [ ] **Step 2: Run frontend file tests**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: FAIL until the new renderers exist.

- [ ] **Step 3: Update JS bindings**

Keep existing API calls, but bind them to the new shell:

- Sidebar navigation toggles `.qd-page.active`.
- Backtest result updates center metrics and strategy performance cards.
- Live state fills strategy list and quick-trade account summary.
- Paper orders render in both quick-trade rail and paper orders page.

- [ ] **Step 4: Verify and commit**

Run:

```bash
python3 -m unittest tests.test_frontend_files -v
python3 -m unittest discover -s tests
```

Commit:

```bash
git add web/app.js tests/test_frontend_files.py
git commit -m "feat:修复QuantDinger外壳数据绑定"
```

## Task 3: Backtest Result History Persistence

**Files:**
- Modify: `tests/test_server.py`
- Modify: `tradebot/server.py`

- [ ] **Step 1: Write failing server test**

Assert that `build_backtest_response()` appends one result summary to `BACKTEST_RESULTS`, and `build_backtest_results_response()` returns it with `resultId`, `createdAt`, `summary`, and `engineVersion`.

- [ ] **Step 2: Run server tests**

Run: `python3 -m unittest tests.test_server -v`

Expected: FAIL until history append exists.

- [ ] **Step 3: Implement in-memory result history**

Append a stable summary object after each successful backtest run. Keep it in memory for this phase; JSON persistence can follow once the UI is correct.

- [ ] **Step 4: Verify and commit**

Run:

```bash
python3 -m unittest tests.test_server -v
python3 -m unittest discover -s tests
```

Commit:

```bash
git add tradebot/server.py tests/test_server.py
git commit -m "feat:记录回测结果历史"
```

## Task 4: Visual Verification

**Files:**
- Modify only if verification finds layout defects.

- [ ] **Step 1: Start local server**

Run: `python3 -m tradebot.server`.

- [ ] **Step 2: Browser verify desktop**

Open `http://127.0.0.1:8765` and compare against:

- `QuantDinger-main/docs/screenshots/v31.png`
- `QuantDinger-main/docs/screenshots/v34.png`

Check for left nav, three-column IDE, quick-trade rail, strategy live master/detail view, and no overlapping text.

- [ ] **Step 3: Browser verify mobile/narrow**

Check the UI collapses to usable vertical panels without horizontal text overlap.

- [ ] **Step 4: Final verification**

Run:

```bash
python3 -m unittest discover -s tests
```

Commit any visual fixes using `style:修复QuantDinger工作台细节`.
