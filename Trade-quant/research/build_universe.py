"""
构建因子研究股票池（Universe），保存为 data/universe.json。

池子构成（目标 150-200 只）：
  Layer 1: XSD 成分股  —— 中小盘半导体，与 AAOI/AXTI 同 DNA（~42只）
  Layer 2: SOXX 成分股 —— 大盘半导体基准（~27只）
  Layer 3: ARKK/ARKW  —— 高波动 AI/高成长科技（尝试在线抓取，~50只）
  Layer 4: 手动补充    —— 光通信、AI基建、OpenAI/Anthropic 战略投资方（~25只）

基准（不纳入因子测试，仅用于 RS 因子计算）：SPY, QQQ, XLK

用法：
  python build_universe.py           # 只构建并保存 universe.json
  python build_universe.py --fetch   # 构建后自动下载所有日线数据
  python build_universe.py --info    # 打印池子明细，不保存
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_PATH = DATA_DIR / "universe.json"

# ── Layer 1: XSD 成分股（S&P 半导体精选行业 ETF，等权重，偏中小盘）─────────
# 跟踪 S&P 500/MidCap400/SmallCap600 中的半导体细分行业，与 AAOI/AXTI 同DNA
XSD_COMPONENTS = [
    "ACMR", "ADI",  "AEHR", "ALGM", "AMD",  "AMBA", "AMKR", "AOSL",
    "AMAT", "ASYS", "AXTI", "CEVA", "COHU", "CRUS", "DIOD", "ENTG",
    "FORM", "GFS",  "HIMX", "IOSP", "IPGP", "KLIC", "LSCC", "MCHP",
    "MPWR", "MTSI", "MU",   "NVDA", "NVMI", "ONTO", "POWI", "QCOM",
    "QRVO", "RMBS", "SITM", "SLAB", "SMTC", "SWKS", "TER",  "TSEM",
    "TXN",  "UCTT", "WOLF",
]

# ── Layer 2: SOXX 成分股（iShares 费城半导体 ETF，大盘权重）────────────────
SOXX_COMPONENTS = [
    "ADI",  "AMAT", "AMD",  "ASML", "AVGO", "CDNS", "ENTG", "INTC",
    "KLAC", "LRCX", "LSCC", "MCHP", "MPWR", "MRVL", "MU",   "NXPI",
    "NVDA", "ON",   "QCOM", "QRVO", "SNPS", "STM",  "SWKS", "TER",
    "TSM",  "TXN",  "WOLF",
]

# ── Layer 3 备用硬编码（在线抓取失败时使用）──────────────────────────────────
# 覆盖 ARKK / ARKW / ARKQ（科技创新类 ARK 系列）
ARKK_FALLBACK = [
    # ARK 核心持仓
    "TSLA", "ROKU", "COIN", "TWLO", "TDOC", "SHOP", "SPOT", "RBLX",
    "PATH", "CRSP", "BEAM", "NTLA", "IOVA", "XYZ",  # SQ→XYZ (Block Inc)
    "SOFI", "HOOD", "DKNG", "PLTR", "AI",   "SOUN", "BBAI", "RXRX",
    "PACB", "FATE", "EDIT", "U",    "GTLB", "MDB",  "SNOW",
    "DDOG", "NET",  "ZS",   "CRWD", "AFRM", "UPST", "NU",
    "TOST", "DASH", "UBER", "ABNB", "LYFT",
    # ARKW 补充（互联网/金融科技/AI应用）
    "BKNG", "MELI", "SE",   "GRAB", "DLO",  "BILL",
    "PYPL", "V",    "MA",   "GOOGL","META",  "AMZN",
    # ARKQ 补充（自动化/机器人/太空）
    "KTOS", "RKLB", "SPCE", "ACHR", "JOBY",
    "TDG",  "HII",  "LHX",  "NOC",  "GD",
    # 高成长云/SaaS
    "HUBS", "VEEV", "WDAY", "NOW",  "CRM",   "ADBE", "INTU",
    "ZM",   "DOCU", "OKTA", "ESTC", "APPN",  "MNDY", "BRZE",
    # AI推理/MLOps
    "ASAN", "BOX",  "FROG", "DOCN", "CLOU",
    # 已确认退市/私有化（注释留档）：CFLT EXAS PSTG VERV SMAR LILM NKLA IIVI ADYEN DLOCAL
]

# ── Layer 3b: IGV 成分股（iShares 扩展科技软件板块 ETF）─────────────────────
# 覆盖高波动软件股，与 ARKK 的 SaaS 有重叠，合并后去重
IGV_COMPONENTS = [
    "MSFT", "ORCL", "NOW",  "ADBE", "INTU", "SNPS", "CDNS", "PANW",
    "CRWD", "FTNT", "PTC",  "TYL",  "MANH", "HUBS", "DDOG",
    "MDB",  "TEAM", "SNOW", "ZS",   "NET",  "OKTA", "WDAY", "CRM",
    "VEEV", "PCTY", "PAYC", "GWRE", "NCNO",
    "BRZE", "GTLB", "APPN", "DOCU", "DOCN", "ESTC", "FROG", "BOX",
    "ASAN", "ZM",   "TWLO", "TOST", "BILL", "AFRM", "HOOD", "SOFI",
    # 已退市：ANSS(被SNPS收购) COUP(私有化) ALTR(被Siemens收购) SMAR(私有化)
]

# ── Layer 4: 手动补充 ────────────────────────────────────────────────────────
MANUAL_SUPPLEMENT = [
    # 光通信 / CPO（Serenity 核心主题）
    "AAOI", "AOSL", "COHR", "IIVI", "LITE", "VIAV",
    # AI 算力基础设施
    "APLD", "CORZ", "DELL", "ETN",  "HPE",  "IREN", "NTAP", "SMCI", "VRT",
    # AI 应用 / 量子
    "IONQ", "QBTS",
    # OpenAI / Anthropic 战略投资方（代理）
    "AMZN", "GOOGL", "META", "MSFT", "ORCL",
    # Serenity 原有标的补填
    "AAPL", "PANW", "RKLB",
]

# ── 基准（仅用于 RS 因子，不纳入测试池）──────────────────────────────────────
BENCHMARKS = ["SPY", "QQQ", "XLK"]

# 已确认退市/私有化/并购旧代码；保留注释用于研究留档，但不进入可交易股票池。
EXCLUDED_SYMBOLS = {
    "ADYEN",
    "ALTR",
    "ANSS",
    "CFLT",
    "COUP",
    "DLOCAL",
    "EXAS",
    "IIVI",
    "LILM",
    "NKLA",
    "PSTG",
    "SMAR",
    "VERV",
}


def try_fetch_arkk(timeout: int = 10) -> list[str]:
    """尝试从 ARK 官网抓取 ARKK 最新成分股列表。"""
    url = (
        "https://ark-funds.com/wp-content/uploads/funds-etf-csv/"
        "ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv"
    )
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        symbols = []
        for line in r.text.splitlines()[1:]:  # 跳过表头
            parts = line.split(",")
            if len(parts) >= 3:
                ticker = parts[2].strip().strip('"')
                if ticker and ticker.isalpha() and ticker.upper() == ticker:
                    symbols.append(ticker)
        symbols = list(dict.fromkeys(symbols))  # 保序去重
        print(f"  在线抓取 ARKK: {len(symbols)} 只  ✓")
        return symbols
    except Exception as e:
        print(f"  在线抓取 ARKK 失败 ({e})，使用内置备用列表")
        return ARKK_FALLBACK


def build_universe() -> dict:
    """合并所有层，去重，返回 universe 字典。"""
    print("构建股票池...")

    arkk_symbols = try_fetch_arkk()

    layers = {
        "XSD":       XSD_COMPONENTS,
        "SOXX":      SOXX_COMPONENTS,
        "ARKK_ARKW": arkk_symbols,
        "IGV":       IGV_COMPONENTS,
        "manual":    MANUAL_SUPPLEMENT,
    }

    seen: set[str] = set()
    all_symbols: list[str] = []
    layer_counts: dict[str, int] = {}

    for layer_name, symbols in layers.items():
        added = 0
        for sym in symbols:
            sym = sym.upper().strip()
            if sym and sym not in seen and sym not in BENCHMARKS and sym not in EXCLUDED_SYMBOLS:
                seen.add(sym)
                all_symbols.append(sym)
                added += 1
        layer_counts[layer_name] = added
        print(f"  {layer_name:12s}: +{added:3d} 只  (累计 {len(all_symbols)})")

    all_symbols.sort()

    universe = {
        "version":    "1.0",
        "built_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total":      len(all_symbols),
        "symbols":    all_symbols,
        "benchmarks": BENCHMARKS,
        "layer_counts": layer_counts,
    }
    print(f"\n最终池子规模: {len(all_symbols)} 只 + {len(BENCHMARKS)} 个基准")
    return universe


def save_universe(universe: dict) -> None:
    with open(UNIVERSE_PATH, "w", encoding="utf-8") as f:
        json.dump(universe, f, ensure_ascii=False, indent=2)
    print(f"已保存至: {UNIVERSE_PATH}")


def load_universe() -> dict:
    if not UNIVERSE_PATH.exists():
        raise FileNotFoundError(f"找不到 universe.json，请先运行 build_universe.py: {UNIVERSE_PATH}")
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_all(universe: dict, tf: str = "1d", since: str = "2020-01-01") -> None:
    """批量下载 universe 中所有股票 + 基准的日线数据。"""
    sys.path.insert(0, str(Path(__file__).parent))
    from fetch_ohlcv import fetch_symbol, save

    all_targets = universe["symbols"] + universe["benchmarks"]
    print(f"\n开始下载 {len(all_targets)} 只股票的 {tf} 日线数据（从 {since}）...")

    until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ok, fail = 0, []

    for i, sym in enumerate(all_targets, 1):
        try:
            df = fetch_symbol(sym, tf, since, until)
            save(df, sym, tf)
            print(f"  [{i:3d}/{len(all_targets)}] ✓ {sym:8s}  {len(df)} 行")
            ok += 1
        except Exception as e:
            print(f"  [{i:3d}/{len(all_targets)}] ✗ {sym:8s}  失败: {e}")
            fail.append(sym)

    print(f"\n完成: 成功 {ok} 只，失败 {len(fail)} 只")
    if fail:
        print(f"失败列表: {fail}")


def main():
    parser = argparse.ArgumentParser(description="构建量化研究股票池")
    parser.add_argument("--fetch",  action="store_true", help="构建后自动下载所有日线数据")
    parser.add_argument("--since",  default="2020-01-01", help="数据起始日期（仅 --fetch 时有效）")
    parser.add_argument("--tf",     default="1d", help="K线周期（仅 --fetch 时有效）")
    parser.add_argument("--info",   action="store_true", help="打印现有 universe.json 明细")
    args = parser.parse_args()

    if args.info:
        u = load_universe()
        print(f"版本: {u['version']}  构建日期: {u['built_date']}")
        print(f"总计: {u['total']} 只  基准: {u['benchmarks']}")
        print(f"分层: {u['layer_counts']}")
        print(f"\n完整列表:\n{u['symbols']}")
        return

    universe = build_universe()
    save_universe(universe)

    if args.fetch:
        fetch_all(universe, tf=args.tf, since=args.since)


if __name__ == "__main__":
    main()
