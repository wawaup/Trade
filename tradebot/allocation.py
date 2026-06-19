from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolAllocation:
    symbol: str
    total_pct: float
    t_pct: float


@dataclass(frozen=True)
class AllocationConfig:
    total_account_quote: float
    symbols: list[SymbolAllocation]


@dataclass(frozen=True)
class AllocationRow:
    symbol: str
    total_pct: float
    t_pct: float
    symbol_budget: float
    core_budget: float
    t_budget: float


def build_allocations(config: AllocationConfig) -> list[AllocationRow]:
    if config.total_account_quote <= 0:
        raise ValueError("total_account_quote must be positive")
    total_pct = sum(item.total_pct for item in config.symbols)
    if total_pct > 1.000001:
        raise ValueError("sum of symbol allocation percentages cannot exceed 100%")

    rows = []
    for item in config.symbols:
        if not item.symbol:
            raise ValueError("symbol is required")
        if item.total_pct < 0 or item.total_pct > 1:
            raise ValueError("total_pct must be between 0 and 1")
        if item.t_pct < 0 or item.t_pct > 1:
            raise ValueError("t_pct must be between 0 and 1")

        symbol_budget = config.total_account_quote * item.total_pct
        t_budget = symbol_budget * item.t_pct
        rows.append(
            AllocationRow(
                symbol=item.symbol,
                total_pct=item.total_pct,
                t_pct=item.t_pct,
                symbol_budget=symbol_budget,
                core_budget=symbol_budget - t_budget,
                t_budget=t_budget,
            )
        )
    return rows


def default_allocation_config(total_account_quote: float = 15_000) -> AllocationConfig:
    return AllocationConfig(
        total_account_quote=total_account_quote,
        symbols=[
            SymbolAllocation("SPCX", 0.25, 0.20),
            SymbolAllocation("TSLA", 0.20, 0.15),
            SymbolAllocation("NVDA", 0.20, 0.15),
            SymbolAllocation("MU", 0.15, 0.10),
            SymbolAllocation("CPOX", 0.10, 0.10),
        ],
    )


def allocation_config_from_dict(payload: dict) -> AllocationConfig:
    return AllocationConfig(
        total_account_quote=float(payload["totalAccountQuote"]),
        symbols=[
            SymbolAllocation(
                symbol=str(item["symbol"]).upper(),
                total_pct=float(item["totalPct"]),
                t_pct=float(item["tPct"]),
            )
            for item in payload.get("symbols", [])
        ],
    )


def allocation_config_to_dict(config: AllocationConfig) -> dict:
    return {
        "totalAccountQuote": config.total_account_quote,
        "symbols": [
            {"symbol": item.symbol, "totalPct": item.total_pct, "tPct": item.t_pct}
            for item in config.symbols
        ],
    }
