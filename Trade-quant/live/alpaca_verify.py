"""
Alpaca Paper Trading 连通性验证脚本

运行：
  cd Trade-quant/live
  cp .env.example .env    # 填入你的 API Keys
  python alpaca_verify.py

期望输出：✅ 连接成功，显示账户信息
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv(Path(__file__).parent / ".env")
API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() != "false"


def verify_connection() -> bool:
    """验证 API Key 有效，并打印账户基本信息"""
    if not API_KEY or "YOUR_API_KEY" in API_KEY:
        print("❌ 未配置 API Key。请先编辑 .env 文件")
        return False

    print(f"{'Paper Trading（模拟）' if PAPER else '⚠️  LIVE Trading（实盘）'} 模式")
    print(f"API Key: {API_KEY[:8]}...")

    try:
        client  = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
        account = client.get_account()
    except Exception as e:
        print(f"❌ 连接失败：{e}")
        return False

    if account.trading_blocked:
        print("❌ 账户被限制交易，请登录 Alpaca 控制台检查")
        return False

    positions = client.get_all_positions()

    print(f"\n✅ 连接成功")
    print(f"   账户状态    : {account.status}")
    print(f"   净值 Equity : ${float(account.equity):>12,.2f}")
    print(f"   可用资金    : ${float(account.buying_power):>12,.2f}")
    print(f"   当前持仓数  : {len(positions)} 只")
    if positions:
        print(f"\n当前持仓：")
        for p in positions:
            pnl = float(p.unrealized_plpc) * 100
            print(f"   {p.symbol:<8} {float(p.qty):>6.1f} 股  "
                  f"均价 ${float(p.avg_entry_price):<8.2f}  "
                  f"未实现 {pnl:+.2f}%")
    else:
        print("\n  当前无持仓（正常，初始状态）")

    return True


def verify_data_api() -> bool:
    """验证市场数据 API 可用（获取 QQQ 最新报价）"""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
        req   = StockLatestQuoteRequest(symbol_or_symbols=["QQQ", "SPY"])
        quote = data_client.get_stock_latest_quote(req)
        print(f"\n市场数据 API：")
        for sym, q in quote.items():
            print(f"   {sym}  bid=${float(q.bid_price):.2f}  ask=${float(q.ask_price):.2f}")
        return True
    except Exception as e:
        print(f"\n市场数据 API 异常（非致命）：{e}")
        print("  策略使用 yfinance 拉取历史数据，不依赖此接口")
        return True  # 不阻塞主流程


if __name__ == "__main__":
    ok = verify_connection()
    if ok:
        verify_data_api()
        print("\n✅ 验证完成，可以运行 alpaca_trader.py")
    else:
        print("\n❌ 验证失败，请检查 .env 文件中的 API Keys")
        sys.exit(1)
