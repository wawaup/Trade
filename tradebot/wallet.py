from dataclasses import dataclass
from typing import Optional


@dataclass
class VirtualWallet:
    core_quote: float
    t_quote: float
    observed_total_quote: Optional[float] = None

    @property
    def available_t_quote(self) -> float:
        return self.t_quote

    def sync_observed_total_quote(self, observed_total_quote: float) -> None:
        self.observed_total_quote = observed_total_quote

    def reserve_t_quote(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("amount must be non-negative")
        if amount > self.t_quote:
            raise ValueError("insufficient T wallet balance")
        self.t_quote -= amount

    def release_t_quote(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("amount must be non-negative")
        self.t_quote += amount
