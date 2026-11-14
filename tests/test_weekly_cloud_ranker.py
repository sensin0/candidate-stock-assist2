import sys
import types
import unittest

import pandas as pd


sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

from scripts.weekly_cloud_ranker import (  # noqa: E402
    SAFETY_VERSION,
    build_current_rankings,
    decision_scores,
    local_current_version_score,
    parse_company_forecast_tables,
)


class DecisionScoreTests(unittest.TestCase):
    def test_fifteen_percent_growth_can_be_buy_priority(self):
        result = decision_scores("化学", 0.4, 0.1, 17.0, True, None, True)
        self.assertEqual(result["Decision Tier"], "買い優先")
        self.assertGreaterEqual(result["Safety Score"], 75)
        self.assertGreaterEqual(result["Two Bagger Score"], 65)

    def test_twenty_percent_growth_is_long_term_watch(self):
        result = decision_scores("化学", 0.4, 0.1, 24.0, True, None, True)
        self.assertEqual(result["Decision Tier"], "長期監視")
        self.assertGreaterEqual(result["Two Bagger Score"], 60)

    def test_recovered_company_stays_on_watch(self):
        result = decision_scores("化学", 0.4, 0.1, 17.0, True, None, False)
        self.assertEqual(result["Decision Tier"], "監視上位")

    def test_negative_growth_is_not_buy_priority(self):
        result = decision_scores("化学", 0.3, 0.05, -2.0, True, None, True)
        self.assertNotEqual(result["Decision Tier"], "買い優先")

    def test_revenue_collapse_is_avoided(self):
        result = decision_scores("輸送用機器", 0.3, 0.05, -13.0, True, None, False)
        self.assertEqual(result["Decision Tier"], "避ける")
        self.assertIn("売上成長率-10%以下", result["Danger Notes"])

    def test_high_legacy_score_cannot_make_negative_growth_a_buy(self):
        item = {
            "Sector": "化学",
            "Price Location": 0.05,
            "Revenue Growth": -2.0,
            "Loss Margin Improving": True,
            "Operating Loss Improving": True,
            "Net Loss Improving": True,
            "Loss Margin": -1.0,
            "PSR": 0.3,
            "Net Income History": [-100.0, -200.0],
            "Current Price": 100.0,
        }
        score, action, _, _, _, _, decision = local_current_version_score(item)
        self.assertGreater(score, 100)
        self.assertNotIn("買い候補", action)
        self.assertNotEqual(decision["Decision Tier"], "買い優先")

    def test_danger_condition_never_becomes_buy(self):
        result = decision_scores("化学", 8.0, 0.1, 17.0, True, None, True)
        self.assertEqual(result["Decision Tier"], "避ける")

    def test_current_action_keeps_twenty_percent_growth_out_of_buy(self):
        item = {
            "Sector": "化学",
            "Price Location": 0.1,
            "Revenue Growth": 24.0,
            "Loss Margin Improving": True,
            "Operating Loss Improving": True,
            "Net Loss Improving": True,
            "Loss Margin": -2.0,
            "PSR": 0.4,
            "Net Income History": [-100.0, -200.0],
            "Current Price": 100.0,
        }
        _, action, _, blocks, _, _, decision = local_current_version_score(item)
        self.assertEqual(action, "長期監視")
        self.assertEqual(blocks, [])
        self.assertEqual(decision["Decision Tier"], "長期監視")

    def test_operating_loss_worsening_overrides_net_loss_improvement(self):
        item = {
            "Sector": "鉄鋼",
            "Price Location": 0.05,
            "Revenue Growth": 15.4,
            "Loss Margin Improving": True,
            "Operating Loss Improving": False,
            "Pretax Loss Improving": False,
            "Net Loss Improving": True,
            "Loss Margin": -2.9,
            "PSR": 0.6,
            "Net Income History": [-175.0, -223.0],
            "Current Price": 1000.0,
        }
        _, action, _, blocks, _, _, decision = local_current_version_score(item)
        self.assertIn("営業赤字拡大", blocks)
        self.assertNotIn("買い候補", action)
        self.assertEqual(decision["Decision Tier"], "避ける")

    def test_company_forecast_operating_loss_expansion_is_avoided(self):
        item = {
            "Sector": "鉄鋼",
            "Price Location": 0.05,
            "Revenue Growth": 16.0,
            "Operating Loss Improving": True,
            "Pretax Loss Improving": True,
            "Net Loss Improving": True,
            "Loss Margin Improving": True,
            "Loss Margin": -2.0,
            "PSR": 0.5,
            "Net Income History": [-100.0, -200.0],
            "Current Price": 1000.0,
            "Forecast Status": "ok",
            "Forecast Operating Income": -900.0,
            "Previous Annual Operating Income": -309.0,
        }
        _, action, _, blocks, _, _, decision = local_current_version_score(item)
        self.assertIn("営業赤字予想拡大", blocks)
        self.assertNotIn("買い候補", action)
        self.assertEqual(decision["Decision Tier"], "避ける")

    def test_parse_company_forecast_table(self):
        table = pd.DataFrame(
            [
                ["forecast", 25000, None, None, -900, None, -800, None, -800],
                ["actual", 23598, None, None, -309, None, -201, None, -339],
            ]
        )
        result = parse_company_forecast_tables([table])
        self.assertEqual(result["Forecast Operating Income"], -900)
        self.assertEqual(result["Previous Annual Operating Income"], -309)

    def test_active_rows_sort_above_pass_rows(self):
        active = {
            "Ticker": "A.T",
            "Name": "Active",
            "Sector": "化学",
            "Price Location": 0.1,
            "Revenue Growth": 17.0,
            "Loss Margin Improving": True,
            "Operating Loss Improving": True,
            "Net Loss Improving": True,
            "Loss Margin": -2.0,
            "PSR": 0.4,
            "Net Income History": [-100.0, -200.0],
            "Current Price": 100.0,
        }
        passed = {
            "Ticker": "B.T",
            "Name": "Pass",
            "Sector": "化学",
            "Revenue Growth": 17.0,
            "PSR": 0.4,
            "Net Income History": [100.0, 80.0],
        }
        rankings = build_current_rankings([passed, active])
        self.assertEqual(rankings[0]["Ticker"], "A.T")
        self.assertNotIn("パス", rankings[0]["Action"])

    def test_safety_version_bumped(self):
        self.assertEqual(SAFETY_VERSION, 12)


if __name__ == "__main__":
    unittest.main()
