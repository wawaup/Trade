from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quote_amount: float = 0.0
    quantity: float = 0.0
    order_type: str = "market"
    source_signal: str = ""
    strategy_id: str = ""
    reduce_only: bool = False
    paper_only: bool = True
    risk_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FillSnapshot:
    symbol: str
    side: str
    filled_qty: float
    avg_price: float
    fee: float
    status: str
    reason: str = ""


@dataclass
class PositionSnapshot:
    symbol: str
    quantity: float
    avg_price: float = 0.0


@dataclass
class RiskLimits:
    max_t_position_quote: Optional[float] = None
    max_order_quote: Optional[float] = None


@dataclass
class PaperAccount:
    cash: float
    positions: dict = field(default_factory=dict)


def order_intent_from_signal(signal, symbol, quote_amount, strategy_id, quantity=0.0):
    sig = str(signal or "").strip().upper()
    if sig in {"OPEN_T", "ADD_T"}:
        return OrderIntent(
            symbol=str(symbol).upper(),
            side="buy",
            quote_amount=float(quote_amount or 0.0),
            quantity=float(quantity or 0.0),
            source_signal=sig,
            strategy_id=str(strategy_id or ""),
        )
    if sig in {"REDUCE_T", "CLOSE_T"}:
        return OrderIntent(
            symbol=str(symbol).upper(),
            side="sell",
            quote_amount=float(quote_amount or 0.0),
            quantity=float(quantity or 0.0),
            source_signal=sig,
            strategy_id=str(strategy_id or ""),
            reduce_only=True,
        )
    raise ValueError(f"Unsupported Trade signal: {signal}")


class PaperExecutionAdapter:
    def __init__(self, account, risk_limits=None):
        self.account = account
        self.risk_limits = risk_limits or RiskLimits()
        self.fills = []

    def execute(self, intent, *, price, fee=0.0):
        try:
            px = float(price)
            order_fee = float(fee or 0.0)
        except (TypeError, ValueError):
            return self._reject(intent, "invalid price or fee")
        if px <= 0:
            return self._reject(intent, "invalid price")

        if intent.side == "buy":
            fill = self._buy(intent, px, order_fee)
        elif intent.side == "sell":
            fill = self._sell(intent, px, order_fee)
        else:
            fill = self._reject(intent, f"invalid side: {intent.side}")
        self.fills.append(fill)
        return fill

    def _buy(self, intent, price, fee):
        quote = float(intent.quote_amount or 0.0)
        if quote <= 0:
            return self._reject(intent, "buy quote amount must be positive")
        if self.risk_limits.max_order_quote is not None and quote > self.risk_limits.max_order_quote:
            return self._reject(intent, "max order quote exceeded")
        if self.risk_limits.max_t_position_quote is not None:
            current_quote = self.account.positions.get(intent.symbol, 0.0) * price
            if current_quote + quote > self.risk_limits.max_t_position_quote:
                return self._reject(intent, "T bucket cap exceeded")
        if self.account.cash < quote + fee:
            return self._reject(intent, "insufficient cash")

        qty = max(0.0, (quote - fee) / price)
        self.account.cash -= quote + fee
        self.account.positions[intent.symbol] = self.account.positions.get(intent.symbol, 0.0) + qty
        return FillSnapshot(intent.symbol, intent.side, qty, price, fee, "filled")

    def _sell(self, intent, price, fee):
        held = self.account.positions.get(intent.symbol, 0.0)
        qty = float(intent.quantity or held)
        if qty <= 0:
            return self._reject(intent, "sell quantity must be positive")
        if intent.reduce_only and qty > held:
            qty = held
        if qty <= 0:
            return self._reject(intent, "no position to reduce")

        gross = qty * price
        self.account.cash += gross - fee
        remaining = max(0.0, held - qty)
        if remaining:
            self.account.positions[intent.symbol] = remaining
        else:
            self.account.positions.pop(intent.symbol, None)
        return FillSnapshot(intent.symbol, intent.side, qty, price, fee, "filled")

    def _reject(self, intent, reason):
        return FillSnapshot(
            symbol=intent.symbol,
            side=intent.side,
            filled_qty=0.0,
            avg_price=0.0,
            fee=0.0,
            status="rejected",
            reason=reason,
        )
