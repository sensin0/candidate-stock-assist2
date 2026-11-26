import unittest

import pandas as pd

from scripts.backtest_current_ranking_timing import fetch_jquants_price_frame, price_features, score_event, validate_patterns
from scripts.backtest_current_ranking_timing import monthly_forecast_rank_comparison, sales_filter_comparison


class CurrentRankingTimingBacktestTests(unittest.TestCase):
    def test_monthly_ranking_filters_before_looking_at_returns(self):
        base = dict(disclosure_date="2024-05-01", timing_status="会社予想先回り",
                    timing_priority=95, confirmation_score=0, current_operating=10, next_operating=15)
        rows = [
            dict(base, code=code, timing_score=score, revenue_growth=sales,
                 month_end_returns={"next_day": dict(code=code, return_3m=ret)})
            for code, score, sales, ret in (("1234", 90, -1, -5), ("1235", 80, 4, 2), ("1236", 75, 8, 6))
        ]
        result = monthly_forecast_rank_comparison(rows, top=1)["later"]["paired_baskets"][0]
        self.assertEqual(result["median_delta"], {"both_filter": 7.0, "both_sales_first": 11.0})
        rows[0]["month_end_returns"] = {}
        self.assertEqual(monthly_forecast_rank_comparison(rows, top=1)["later"]["paired_baskets"], [])

    def test_sales_comparison_separates_zero_missing_and_incomplete(self):
        base = dict(code="1234", disclosure_date="2024-05-01", quarter=0,
                    timing_key="next_day", forecast_growth=0.25, max_12m=20,
                    return_12m=10, drawdown_12m=-5)
        rows = [dict(base, revenue_growth=growth) for growth in (None, 0, 0.5, 10)]
        rows.append(dict(base, revenue_growth=20, max_12m=None))
        results = sales_filter_comparison(rows)
        counts = {row["sales"]: row["events"] for row in results
                  if row["timing"] == "next_day" and row["period"] == "later" and row["annual"]}
        self.assertEqual(counts, dict(baseline=4, positive=2, at_least_10=1, flat_or_down=1, missing=1))

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

    def test_pattern_validation_splits_train_and_test_without_future_leakage(self):
        rows = []
        for date in ("2023-05-01", "2024-05-01"):
            row = {
                "code": date[:4], "disclosure_date": date, "timing_status": "会社予想先回り",
                "return_1m": 1.0, "return_3m": 2.0, "return_6m": 3.0, "return_12m": 4.0,
                "alpha_1m": 0.0, "alpha_3m": 0.0, "alpha_6m": 0.0, "alpha_12m": 0.0,
                "max_12m": 120.0, "drawdown_12m": -10.0, "target50_before_stop": True,
                "double_before_stop": True, "stop25_first": False,
            }
            rows.append(row)
        current = next(item for item in validate_patterns(rows) if item["pattern"] == "現行ランキング候補")
        self.assertEqual(current["train"]["n_complete_12m"], 1)
        self.assertEqual(current["test"]["n_complete_12m"], 1)


if __name__ == "__main__":
    unittest.main()
