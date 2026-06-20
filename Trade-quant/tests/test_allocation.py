import unittest

from tradebot.allocation import AllocationConfig, SymbolAllocation, build_allocations


class AllocationTest(unittest.TestCase):
    def test_allocates_symbol_and_t_budgets_from_total_account_value(self):
        config = AllocationConfig(
            total_account_quote=10_000,
            symbols=[
                SymbolAllocation("NVDA", total_pct=0.30, t_pct=0.25),
                SymbolAllocation("TSLA", total_pct=0.20, t_pct=0.30),
            ],
        )

        rows = build_allocations(config)

        nvda = rows[0]
        self.assertEqual(nvda.symbol, "NVDA")
        self.assertEqual(nvda.symbol_budget, 3000)
        self.assertEqual(nvda.t_budget, 750)
        self.assertEqual(nvda.core_budget, 2250)

        tsla = rows[1]
        self.assertEqual(tsla.symbol_budget, 2000)
        self.assertEqual(tsla.t_budget, 600)
        self.assertEqual(tsla.core_budget, 1400)

    def test_rejects_invalid_total_allocation(self):
        config = AllocationConfig(
            total_account_quote=10_000,
            symbols=[
                SymbolAllocation("NVDA", total_pct=0.80, t_pct=0.25),
                SymbolAllocation("TSLA", total_pct=0.30, t_pct=0.30),
            ],
        )

        with self.assertRaises(ValueError):
            build_allocations(config)

    def test_rejects_invalid_t_ratio(self):
        config = AllocationConfig(
            total_account_quote=10_000,
            symbols=[SymbolAllocation("NVDA", total_pct=0.30, t_pct=1.2)],
        )

        with self.assertRaises(ValueError):
            build_allocations(config)


if __name__ == "__main__":
    unittest.main()
