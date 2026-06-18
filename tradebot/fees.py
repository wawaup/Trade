from dataclasses import dataclass


@dataclass(frozen=True)
class BinanceStockFeeModel:
    fixed_threshold: float = 350.0
    fixed_fee: float = 0.35
    percent_fee: float = 0.001

    def estimate(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        if notional < self.fixed_threshold:
            return self.fixed_fee
        return notional * self.percent_fee

    def round_trip_bps(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        return (self.estimate(notional) * 2 / notional) * 10_000

