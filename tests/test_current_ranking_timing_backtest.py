import unittest

import pandas as pd

from scripts.backtest_current_ranking_timing import fetch_jquants_price_frame, price_features, score_event


class CurrentRankingTimingBacktestTests(unittest.TestCase):
    def test_jquants_prices_use_adjusted_daily_values(self):
        class Client:
            def get_all(self, path, params):
                self.path = path
                return [{"Date": "2024-01-05", "AdjC": "101", "AdjH": "103", "AdjL": "99"}]

        client = Client()
        result = fetch_jquants_price_frame(client, "1234")
        self.assertEqual(client.path, "equities/bars/daily")
        self.assertEqual(float(result.iloc[0]["close"]), 101.0)

    def test_annual_turnaround_is_eligible_before_large_move(self):
        event = {
            "code": "1234",
            "disclosure_date": "2024-05-10",
            "stage": "company_forecast",
            "quarter": 0,
            "current_operating": -100.0,
            "next_operating": 300.0,
            "revenue_growth": 16.0,
            "net_income": 10.0,
            "previous_net_income": 5.0,
            "forecast_net": 20.0,
        }
        features = {"market_cap": 4_000_000_000, "price_location": 0.3, "max_gain": 20.0, "drawdown": -10.0, "cycle": "上昇前"}
        result = score_event(event, features)
        self.assertEqual(result["timing_status"], "会社予想先回り")
        self.assertIn(result["timing_grade"], {"S", "A", "B"})

    def test_forecast_net_loss_is_not_eligible(self):
        event = {
            "code": "1234", "disclosure_date": "2024-05-10", "stage": "company_forecast", "quarter": 0,
            "current_operating": -100.0, "next_operating": 300.0, "revenue_growth": 20.0,
            "net_income": 10.0, "previous_net_income": 5.0, "forecast_net": -20.0,
        }
        features = {"market_cap": 3_000_000_000, "price_location": 0.2, "max_gain": 10.0, "drawdown": -5.0, "cycle": "上昇前"}
        self.assertEqual(score_event(event, features)["timing_status"], "対象外")

    def test_price_features_reject_completed_two_bagger(self):
        prices = pd.DataFrame(
            {"close": [100.0, 120.0, 210.0, 130.0], "high": [105.0, 125.0, 220.0, 135.0], "low": [95.0, 115.0, 200.0, 125.0]},
            index=pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]),
        )
        result = price_features(prices, "2024-04-02", 10_000_000)
        self.assertEqual(result["cycle"], "2倍後の往復済み")


if __name__ == "__main__":
    unittest.main()
