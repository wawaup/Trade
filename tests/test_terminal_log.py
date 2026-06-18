import unittest

from tradebot.terminal_log import format_signal_log


class TerminalLogTest(unittest.TestCase):
    def test_formats_terminal_style_signal_log(self):
        line = format_signal_log(
            timestamp_ms=1_780_000_000_000,
            symbol="NVDA",
            level="INFO",
            event="SIGNAL",
            message="日K过滤通过，允许T仓买入",
        )

        self.assertIn("[INFO]", line)
        self.assertIn("[SIGNAL]", line)
        self.assertIn("NVDA", line)
        self.assertIn("日K过滤通过", line)


if __name__ == "__main__":
    unittest.main()
