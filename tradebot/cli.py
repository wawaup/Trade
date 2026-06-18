import argparse
from pathlib import Path

from tradebot.backtest import BacktestConfig, run_backtest
from tradebot.data import (
    fetch_spot_klines,
    generate_synthetic_spcx,
    read_candles_csv,
    write_candles_csv,
)
from tradebot.strategy import StrategyConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest Binance-style stock-token T trading.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    simulate = subparsers.add_parser("simulate", help="Run a synthetic SPCX-like simulation.")
    simulate.add_argument("--starting-quote", type=float, default=3500)
    simulate.add_argument("--core-allocation-pct", type=float, default=0.70)
    simulate.add_argument("--seed", type=int, default=7)

    fetch = subparsers.add_parser("fetch-binance", help="Fetch Binance spot klines to CSV.")
    fetch.add_argument("--symbol", required=True, help="Example: BTCUSDT. Use the stock token symbol if exposed.")
    fetch.add_argument("--interval", default="1m")
    fetch.add_argument("--limit", type=int, default=500)
    fetch.add_argument("--out", required=True)

    backtest = subparsers.add_parser("backtest-csv", help="Run backtest from daily and intraday CSV files.")
    backtest.add_argument("--daily", required=True)
    backtest.add_argument("--intraday", required=True)
    backtest.add_argument("--starting-quote", type=float, default=3500)
    backtest.add_argument("--core-allocation-pct", type=float, default=0.70)

    return parser


def print_result(result) -> None:
    pnl = result.ending_equity - result.starting_quote * 0.30
    print(f"T bucket ending equity: {result.ending_equity:.2f}")
    print(f"T bucket PnL vs allocated T cash: {pnl:.2f}")
    print(f"Cash: {result.cash:.2f}")
    print(f"Open T position value: {result.t_position_value:.2f}")
    print(f"Max T position value: {result.max_t_position_quote:.2f}")
    print(f"Fees paid: {result.fees_paid:.2f}")
    print(f"Trades: {len(result.trades)}")
    for trade in result.trades:
        print(
            f"{trade.side:4} ts={trade.open_time} price={trade.price:.2f} "
            f"qty={trade.qty:.6f} quote={trade.quote:.2f} fee={trade.fee:.2f} reason={trade.reason}"
        )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "simulate":
        daily, intraday = generate_synthetic_spcx(args.seed)
        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(args.starting_quote, args.core_allocation_pct),
            StrategyConfig(),
        )
        print_result(result)
    elif args.command == "fetch-binance":
        candles = fetch_spot_klines(args.symbol, args.interval, args.limit)
        write_candles_csv(Path(args.out), candles)
        print(f"Wrote {len(candles)} candles to {args.out}")
    elif args.command == "backtest-csv":
        daily = read_candles_csv(Path(args.daily))
        intraday = read_candles_csv(Path(args.intraday))
        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(args.starting_quote, args.core_allocation_pct),
            StrategyConfig(),
        )
        print_result(result)


if __name__ == "__main__":
    main()

