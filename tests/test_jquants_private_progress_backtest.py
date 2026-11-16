import unittest

from scripts.jquants_private_progress_backtest import build_progress_events, pace_bucket, summarize


class JQuantsProgressBacktestTests(unittest.TestCase):
    def test_annual_next_forecast_is_carried_into_following_quarter(self):
        rows = [
            {
                "DiscDate": "2022-05-10", "DiscTime": "15:00", "DiscNo": "1", "Code": "12340",
                "CurPerType": "FY", "CurFYSt": "2021-04-01", "CurFYEn": "2022-03-31",
                "NxtFYSt": "2022-04-01", "NxtFYEn": "2023-03-31", "NxFOP": "1000", "OP": "800",
            },
            {
                "DiscDate": "2022-08-10", "DiscTime": "15:00", "DiscNo": "2", "Code": "12340",
                "CurPerType": "1Q", "CurFYSt": "2022-04-01", "CurFYEn": "2023-03-31",
                "FOP": "", "OP": "400",
            },
        ]
        events = build_progress_events(rows)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["progress"], 0.4)
        self.assertAlmostEqual(events[0]["raw_pace"], 1.6)

    def test_only_prior_same_quarter_history_sets_seasonal_pace(self):
        rows = []
        for year, op in ((2021, 200), (2022, 250), (2023, 450)):
            rows.append({
                "DiscDate": f"{year}-08-10", "DiscTime": "15:00", "DiscNo": str(year), "Code": "12340",
                "CurPerType": "1Q", "CurFYSt": f"{year}-04-01", "CurFYEn": f"{year + 1}-03-31",
                "FOP": "1000", "OP": str(op),
            })
        events = build_progress_events(rows)
        self.assertIsNone(events[1]["seasonal_pace"])
        self.assertAlmostEqual(events[2]["seasonal_pace"], 2.0)
        self.assertEqual(pace_bucket(events[2]), "過去同四半期比150%以上")

    def test_forecast_revision_uses_only_forecast_known_before_disclosure(self):
        rows = [
            {
                "DiscDate": "2022-05-10", "DiscTime": "15:00", "DiscNo": "1", "Code": "12340",
                "CurPerType": "FY", "CurFYSt": "2021-04-01", "CurFYEn": "2022-03-31",
                "NxtFYSt": "2022-04-01", "NxtFYEn": "2023-03-31", "NxFOP": "1000", "OP": "800",
            },
            {
                "DiscDate": "2022-08-10", "DiscTime": "15:00", "DiscNo": "2", "Code": "12340",
                "CurPerType": "1Q", "CurFYSt": "2022-04-01", "CurFYEn": "2023-03-31",
                "FOP": "1200", "OP": "400",
            },
        ]
        event = build_progress_events(rows)[0]
        self.assertAlmostEqual(event["forecast_revision_pct"], 0.2)
        self.assertAlmostEqual(event["forecast_growth"], 0.5)

    def test_summary_reports_double_rate_from_completed_windows(self):
        rows = []
        for value in (120.0, 80.0, -10.0, None):
            row = {"code": "1234", "max_6m": value, "max_12m": value}
            for months in (1, 3, 6, 12):
                row[f"return_{months}m"] = 1.0
                row[f"alpha_{months}m"] = 0.5
            rows.append(row)
        result = summarize(rows)
        self.assertEqual(result["n_max_12m"], 3)
        self.assertEqual(result["double_rate_12m"], 33.3)


if __name__ == "__main__":
    unittest.main()
