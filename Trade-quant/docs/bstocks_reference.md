# Binance bStocks 交易对参考

## 重要说明

bStocks **不是**美股本身，是由 BTech Holdings Limited 发行的代币化证券，代表对底层股票的间接权益。
交易在 **Binance Spot 现货**上进行（不是 Futures、不是 Alpha）。

---

## 交易对映射

| 网页显示       | API 参数（无斜杠）  | 底层标的          | 开放日期       |
|---------------|-------------------|-----------------|--------------|
| NVDAB/USDT    | `NVDABUSDT`       | NVIDIA          | 2026-06-11   |
| MUB/USDT      | `MUBUSDT`         | Micron          | 2026-06-11   |
| SNDKB/USDT    | `SNDKBUSDT`       | SanDisk         | 2026-06-11   |
| TSLAB/USDT    | `TSLABUSDT`       | Tesla           | 2026-06-11   |
| CRCLB/USDT    | `CRCLBUSDT`       | Circle          | 2026-06-11   |
| SPCXB/USDT    | `SPCXBUSDT`       | SpaceX          | 2026-06-12   |

```python
BSTOCK_SYMBOLS = [
    "NVDABUSDT",   # NVIDIA
    "MUBUSDT",     # Micron
    "SNDKBUSDT",   # SanDisk
    "TSLABUSDT",   # Tesla
    "CRCLBUSDT",   # Circle
    "SPCXBUSDT",   # SpaceX（主标的）
]
```

---

## API 端点

**基础 URL**：`https://api.binance.com`（Binance Spot，不是 Futures）

| 用途               | 端点                              |
|--------------------|----------------------------------|
| K 线 / OHLCV       | `GET /api/v3/klines`             |
| 交易对规则查询     | `GET /api/v3/exchangeInfo`       |
| 当前最优买卖价     | `GET /api/v3/ticker/bookTicker`  |
| 24h 行情           | `GET /api/v3/ticker/24hr`        |
| 盘口深度           | `GET /api/v3/depth`              |

---

## K 线接口参数

```
GET https://api.binance.com/api/v3/klines
  ?symbol=SPCXBUSDT
  &interval=1h
  &startTime=<UTC毫秒>
  &endTime=<UTC毫秒>
  &limit=1000
```

- `symbol` / `interval` 必填
- `limit` 默认 500，最大 1000
- `interval` 大小写敏感：`1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`

建议先用 `1h`，bStocks 历史数据较短，1m 噪音大。

---

## 回测注意事项

### 数据历史极短
bStocks 上线时间（2026-06-11/12）距今仅约一周，历史数据非常有限：
- ✅ 适合：API 联通验证、数据清洗、策略框架跑通、小样本演示
- ❌ 不适合：严肃长周期策略验证

长周期研发可用对应美股历史数据（NVDA、MU、TSLA 等），实盘前再用 bStocks 数据验证盘口/滑点。

### 不要混用的品种
| 混淆品种       | 说明                              |
|----------------|----------------------------------|
| Alpha `NVDAon` | 不是 bStocks，API 接口不同         |
| Futures 合约   | 不是现货，接口不同                 |

### 手续费参考
- Maker fee：2026-08-31 前为 0（活动期）
- Taker fee：约 0.1%（保守按此估算）
- 滑点：bStocks 流动性较浅，建议回测加 0.05%–0.1%

---

## 与美股代码的对应关系

项目代码中的 symbol 写法是美股短代码（`SPCX`、`TSLA`、`NVDA`、`MU`）：
- **回测 / 策略逻辑层**：使用美股短代码（`SPCX`）
- **Binance API 请求**：使用 bStocks 格式（`SPCXBUSDT`）

转换规则：`{美股代码}B + USDT`，但需注意个别 symbol 有差异（如 MU → MUB、Circle → CRCLB）。
