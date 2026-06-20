import unittest

from tradebot.data import session_start_ms


class DataSessionsTest(unittest.TestCase):
    def test_session_start_uses_configured_utc_hour(self):
        hour_ms = 3_600_000
        timestamp = 10 * hour_ms + 123

        self.assertEqual(session_start_ms(timestamp, reset_utc_hour=8), 8 * hour_ms)

    def test_session_start_rolls_to_previous_day_before_reset_hour(self):
        day_ms = 86_400_000
        hour_ms = 3_600_000
        timestamp = 2 * hour_ms

        self.assertEqual(session_start_ms(timestamp, reset_utc_hour=8), -day_ms + 8 * hour_ms)


if __name__ == "__main__":
    unittest.main()
