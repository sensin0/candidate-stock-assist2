import sys
import types
import unittest

import pandas as pd


sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

from scripts.weekly_cloud_ranker import (  # noqa: E402
    SAFETY_VERSION,
    add_priority_refresh_rows,
    build_current_rankings,
    build_strategy_competition,
    build_strategy_rankings,
    decision_scores,
    financial_comparison_index,
    historical_growth_pattern,
    local_current_version_score,
    operating_dip_candidates,
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
        self.assertIn("来期営業赤字予想拡大", blocks)
        self.assertNotIn("買い候補", action)
        self.assertEqual(decision["Decision Tier"], "避ける")

    def test_next_year_operating_profit_decline_is_avoided(self):
        item = {
            "Sector": "化学",
            "Price Location": 0.3,
            "Revenue Growth": 12.0,
            "PSR": 0.8,
            "Net Income History": [100.0, 80.0],
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": 120.0,
            "Next Fiscal Operating Income": 100.0,
            "Annual Operating Income History": [120_000_000.0, 90_000_000.0],
        }
        _, action, _, blocks, _, _, decision = local_current_version_score(item)
        self.assertIn("来期営業減益予想", blocks)
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
        self.assertEqual(result["Next Fiscal Operating Income"], -900)
        self.assertEqual(result["Current Fiscal Operating Income"], -309)

    def test_priority_rankings_are_added_to_daily_refresh(self):
        rows = [
            {"code": "1001"},
            {"code": "1002"},
            {"code": "1003"},
        ]
        report = {"current_rankings": [{"Ticker": "1003.T"}]}
        result = add_priority_refresh_rows(rows[:1], rows, report, limit=50)
        self.assertEqual([row["code"] for row in result], ["1001", "1003"])

    def test_operating_dip_candidates_prioritize_largest_drop(self):
        rows = [
            {"Ticker": "A.T", "Sector": "化学", "Annual Operating Income History": [80.0, 100.0]},
            {"Ticker": "B.T", "Sector": "鉄鋼", "Annual Operating Income History": [20.0, 100.0]},
            {"Ticker": "C.T", "Sector": "化学", "Annual Operating Income History": [120.0, 100.0]},
        ]
        result = operating_dip_candidates(rows, limit=1)
        self.assertEqual([row["Ticker"] for row in result], ["B.T"])

    def test_quarterly_financials_compare_same_quarter_last_year(self):
        self.assertEqual(financial_comparison_index(True, [1, 2, 3, 4, 5]), 4)
        self.assertEqual(financial_comparison_index(False, [1, 2, 3, 4, 5]), 1)

    def test_validated_pattern_a(self):
        label, score = historical_growth_pattern(120.0, 100.0, 12.0, 0.4)
        self.assertTrue(label.startswith("検証A"))
        self.assertEqual(score, 100)

    def test_validated_pattern_a_becomes_buy_candidate(self):
        item = {
            "Sector": "化学",
            "Price Location": 0.4,
            "Revenue Growth": 5.0,
            "Annual Revenue Growth": 12.0,
            "Historical Pattern": "検証A: 黒字継続+年商10-14.9%+株価位置30-49.9%",
            "Historical Pattern Score": 100,
            "Operating Loss Improving": True,
            "Pretax Loss Improving": True,
            "Net Loss Improving": True,
            "Loss Margin Improving": True,
            "PSR": 1.0,
            "Net Income History": [120.0, 100.0],
            "Current Price": 1000.0,
            "Forecast Status": "ok",
            "Previous Annual Operating Income": 100.0,
            "Forecast Operating Income": 120.0,
        }
        _, action, _, blocks, _, _, decision = local_current_version_score(item)
        self.assertEqual(blocks, [])
        self.assertEqual(action, "買い候補 検証A")
        self.assertEqual(decision["Decision Tier"], "買い優先")

    def test_operating_turnaround_strategy(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "A.T",
            "Name": "Turnaround",
            "Sector": "化学",
            "Annual Operating Income History": [100.0, -50.0, -20.0],
            "Net Income History": [80.0, -40.0],
            "Annual Revenue Growth": 8.0,
            "Revenue Growth": 6.0,
            "Operating Loss Improving": True,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": -50.0,
            "Next Fiscal Operating Income": 120.0,
        }
        rankings = build_strategy_rankings([item])
        self.assertEqual(rankings["operating_turnaround"][0]["Ticker"], "A.T")

    def test_operating_v_recovery_strategy(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "V.T",
            "Name": "V Recovery",
            "Sector": "鉄鋼",
            "Annual Operating Income History": [40.0, 70.0, 100.0],
            "Net Income History": [20.0, -30.0],
            "Annual Revenue Growth": 4.0,
            "Revenue Growth": 3.0,
            "Operating Loss Improving": True,
            "PSR": 1.2,
            "Price Location": 0.6,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": 40.0,
            "Next Fiscal Operating Income": 90.0,
        }
        rankings = build_strategy_rankings([item])
        self.assertEqual(rankings["operating_v_recovery"][0]["Ticker"], "V.T")

    def test_v_recovery_converts_yen_history_to_forecast_millions(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "UNIT.T",
            "Sector": "化学",
            "Annual Operating Income History": [80_000_000_000.0, 100_000_000_000.0],
            "Net Income History": [20.0, 10.0],
            "Revenue Growth": 5.0,
            "Operating Loss Improving": True,
            "PSR": 1.0,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": 80_000.0,
            "Next Fiscal Operating Income": 130_000.0,
        }
        rankings = build_strategy_rankings([item])
        self.assertEqual(rankings["operating_v_recovery"][0]["Ticker"], "UNIT.T")
        self.assertEqual(rankings["operating_v_recovery"][0]["Strategy Score"], 98)

    def test_v_recovery_ranks_above_continuous_growth(self):
        base = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Sector": "化学",
            "Net Income History": [100.0, 80.0],
            "Revenue Growth": 12.0,
            "Operating Loss Improving": True,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
        }
        v_recovery = {
            **base,
            "Ticker": "V.T",
            "Annual Operating Income History": [80_000_000.0, 100_000_000.0],
            "Current Fiscal Operating Income": 80.0,
            "Next Fiscal Operating Income": 130.0,
        }
        continuous = {
            **base,
            "Ticker": "G.T",
            "Annual Operating Income History": [120_000_000.0, 90_000_000.0],
            "Current Fiscal Operating Income": 120.0,
            "Next Fiscal Operating Income": 140.0,
        }
        rankings = build_current_rankings([continuous, v_recovery])
        self.assertEqual(rankings[0]["Ticker"], "V.T")
        self.assertEqual(rankings[0]["Action"], "買い候補 黒字内営業V字回復")
        self.assertIn("連続増益", " ".join(rankings[1]["Current Version Notes"]))

    def test_one_year_operating_loss_turnaround_is_top_priority(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "TURN.T",
            "Sector": "化学",
            "Net Income History": [100.0, 80.0],
            "Annual Operating Income History": [-20_000_000.0, 100_000_000.0],
            "Revenue Growth": 8.0,
            "Operating Loss Improving": True,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": -20.0,
            "Next Fiscal Operating Income": 50.0,
        }
        ranking = build_current_rankings([item])[0]
        self.assertEqual(ranking["Action"], "買い候補 1期赤字→営業黒字転換")
        self.assertEqual(ranking["Composite Score"], 99)

    def test_persistent_operating_losses_are_watch_only_even_with_turnaround_forecast(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "LONGLOSS.T",
            "Sector": "鉄鋼",
            "Net Income History": [-20.0, -30.0],
            "Annual Operating Income History": [-50_000_000.0, -100_000_000.0],
            "Revenue Growth": 8.0,
            "Operating Loss Improving": True,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": -50.0,
            "Next Fiscal Operating Income": 20.0,
        }
        ranking = build_current_rankings([item])[0]
        self.assertEqual(ranking["Action"], "監視 2期以上赤字から黒字予想")
        self.assertEqual(ranking["Composite Score"], 70)

    def test_operating_loss_narrowing_is_watch_only(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "NARROW.T",
            "Sector": "化学",
            "Net Income History": [-20.0, -30.0],
            "Annual Operating Income History": [-50_000_000.0, -20_000_000.0],
            "Revenue Growth": 8.0,
            "Operating Loss Improving": True,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": -50.0,
            "Next Fiscal Operating Income": -10.0,
        }
        ranking = build_current_rankings([item])[0]
        self.assertEqual(ranking["Action"], "監視 営業赤字縮小")
        self.assertEqual(ranking["Composite Score"], 65)

    def test_v_recovery_with_weak_latest_quarter_is_watch_only(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "WATCH.T",
            "Sector": "化学",
            "Net Income History": [100.0, 80.0],
            "Annual Operating Income History": [80_000_000.0, 100_000_000.0],
            "Revenue Growth": 12.0,
            "Operating Loss Improving": False,
            "PSR": 0.8,
            "Price Location": 0.4,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": 80.0,
            "Next Fiscal Operating Income": 130.0,
        }
        ranking = build_current_rankings([item])[0]
        self.assertEqual(ranking["Action"], "監視 営業V字予想（直近四半期悪化）")
        self.assertEqual(ranking["Decision Tier"], "監視上位")
        self.assertEqual(ranking["Composite Score"], 84)

    def test_strategy_requires_quarterly_revenue_comparison(self):
        item = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Ticker": "MISSING.T",
            "Sector": "化学",
            "Annual Operating Income History": [80.0, -100.0, 40.0],
            "Revenue Growth": None,
            "Operating Loss Improving": True,
            "Forecast Status": "ok",
            "Previous Annual Operating Income": -100.0,
            "Forecast Operating Income": 90.0,
        }
        rankings = build_strategy_rankings([item])
        self.assertEqual(rankings["operating_v_recovery"], [])

    def test_strategy_competition_balances_return_and_drawdown(self):
        competition = build_strategy_competition({key: [] for key in (
            "validated_a",
            "profitable_growth",
            "operating_turnaround",
            "operating_v_recovery",
            "operating_loss_narrowing",
            "quarterly_momentum",
            "value_growth",
        )})
        self.assertEqual(competition[0]["key"], "validated_a")
        self.assertGreater(competition[0]["league_score"], competition[1]["league_score"])
        self.assertEqual(competition[-1]["status"], "forward_testing")

    def test_active_rows_sort_above_pass_rows(self):
        active = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
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
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
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

    def test_higher_expected_upside_ranks_first(self):
        base = {
            "Safety Version": SAFETY_VERSION,
            "Quarterly Comparison": "year_over_year",
            "Sector": "化学",
            "Net Income History": [100.0, 80.0],
            "Annual Operating Income History": [60_000_000.0, 100_000_000.0],
            "Operating Loss Improving": True,
            "Forecast Status": "ok",
            "Current Fiscal Operating Income": 60.0,
            "PSR": 0.8,
        }
        weaker = dict(base, Ticker="WEAK.T", **{"Next Fiscal Operating Income": 80.0, "Revenue Growth": 3.0, "Price Location": 0.45})
        stronger = dict(base, Ticker="STRONG.T", **{"Next Fiscal Operating Income": 140.0, "Revenue Growth": 16.0, "Price Location": 0.12})
        rankings = build_current_rankings([weaker, stronger])
        self.assertEqual(rankings[0]["Ticker"], "STRONG.T")
        self.assertGreater(rankings[0]["Expected Upside Score"], rankings[1]["Expected Upside Score"])
        self.assertEqual(rankings[0]["Historical 12M Median Return"], 34.9)

    def test_safety_version_bumped(self):
        self.assertEqual(SAFETY_VERSION, 16)


if __name__ == "__main__":
    unittest.main()
