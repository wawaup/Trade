import unittest

from tradebot.wallet import VirtualWallet


class VirtualWalletTest(unittest.TestCase):
    def test_t_wallet_is_isolated_from_total_api_balance(self):
        wallet = VirtualWallet(core_quote=2450, t_quote=1050)

        wallet.sync_observed_total_quote(5000)

        self.assertEqual(wallet.available_t_quote, 1050)

    def test_t_wallet_debits_and_credits_only_t_bucket(self):
        wallet = VirtualWallet(core_quote=2450, t_quote=1050)

        wallet.reserve_t_quote(350)
        wallet.release_t_quote(360)

        self.assertEqual(wallet.available_t_quote, 1060)
        self.assertEqual(wallet.core_quote, 2450)


if __name__ == "__main__":
    unittest.main()
