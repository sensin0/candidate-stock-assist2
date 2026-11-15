import unittest

import pandas as pd

from scripts.analyze_operating_profit_rank_performance import (
    infer_split_adjustments,
    operating_pattern,
    rank_bucket,
)


class OperatingProfitBacktestTests(unittest.TestCase):
    def test_operating_patterns_are_distinct(self):
        self.assertEqual(operating_pattern(-20, -10, 5), "two_loss_to_profit")
        self.assertEqual(operating_pattern(10, -10, 5), "loss_to_profit")
        self.assertEqual(operating_pattern(10, -10, -3), "loss_narrowing")
        self.assertEqual(operating_pattern(100, 60, 90), "profitable_v")
        self.assertEqual(operating_pattern(50, 70, 90), "continuous_growth")

    def test_rank_buckets_preserve_individual_top_positions(self):
        self.assertEqual(rank_bucket(1), "1位")
        self.assertEqual(rank_bucket(2), "2位")
        self.assertEqual(rank_bucket(3), "3位")
        self.assertEqual(rank_bucket(5), "4-5位")

    def test_split_adjustment_normalizes_pre_split_prices(self):
        frame = pd.DataFrame(
            {
                "close": [500.0, 510.0, 102.0, 105.0],
                "high": [505.0, 515.0, 104.0, 107.0],
                "low": [495.0, 505.0, 100.0, 103.0],
            },
            index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]),
        )
        adjusted = infer_split_adjustments(frame)
        self.assertAlmostEqual(adjusted.iloc[1]["close"], 102.0)
        self.assertAlmostEqual(adjusted.iloc[2]["close"], 102.0)


if __name__ == "__main__":
    unittest.main()
