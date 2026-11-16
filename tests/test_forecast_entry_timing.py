import unittest

import pandas as pd

from scripts.analyze_forecast_entry_timing import match_annual_earnings_date, trading_entry


class ForecastEntryTimingTests(unittest.TestCase):
    def test_matches_first_annual_result_after_period_end(self):
        matched = match_annual_earnings_date("2025-03-31", ["2025-02-10", "2025-05-13", "2025-08-01"])
        self.assertEqual(matched, pd.Timestamp("2025-05-13"))

    def test_entry_starts_after_announcement_day(self):
        prices = pd.DataFrame(
            {"close": [100.0, 105.0, 103.0], "high": [101.0, 106.0, 104.0], "low": [99.0, 104.0, 102.0]},
            index=pd.to_datetime(["2025-05-13", "2025-05-14", "2025-05-15"]),
        )
        date, price = trading_entry(prices, pd.Timestamp("2025-05-13"), 0)
        self.assertEqual(date, pd.Timestamp("2025-05-14"))
        self.assertEqual(price, 105.0)


if __name__ == "__main__":
    unittest.main()
