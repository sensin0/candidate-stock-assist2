"""Point-in-time validation for the current ranking and its entry timing.

Raw J-Quants records stay in memory. Only aggregate, non-reconstructable
statistics are printed so licensed source data is never published.
"""

import argparse
import json
import math
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import pandas as pd

try:
    from scripts.jquants_private_progress_backtest import (
        HORIZONS,
        JQuantsClient,
        build_progress_events,
        jump_signal,
        load_universe,
        number,
        price_on_or_after,
        price_on_or_before,
        trading_entry,
    )
    from scripts.weekly_cloud_ranker import buy_timing_values, two_bagger_confirmation_values
except ModuleNotFoundError:
    from jquants_private_progress_backtest import (
        HORIZONS,
        JQuantsClient,
        build_progress_events,
        jump_signal,
        load_universe,
        number,
        price_on_or_after,
        price_on_or_before,
        trading_entry,
    )
    from weekly_cloud_ranker import buy_timing_values, two_bagger_confirmation_values


ELIGIBLE_STATUSES = {"会社予想先回り", "3Q先回り", "業績確認済み"}
TIMINGS = (("next_day", 0), ("one_week", 4), ("two_weeks", 9), ("one_month", 19))


def fetch_jquants_price_frame(client, code):
    """Load adjusted daily bars in memory without persisting licensed rows."""
    records = client.get_all("equities/bars/daily", {"code": code})
    rows = []
    for record in records:
        close = number(record.get("AdjC"))
        high = number(record.get("AdjH"))
        low = number(record.get("AdjL"))
        date = record.get("Date")
        if date and close is not None:
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "close": close,
                    "high": high if high is not None else close,
                    "low": low if low is not None else close,
                }
            )
    if not rows:
        return None
    return pd.DataFrame(rows).drop_duplicates("date", keep="last").set_index("date").sort_index()


def build_point_in_time_events(records):
    """Create annual-forecast and quarterly events using only prior disclosures."""
    ordered = sorted(records, key=lambda row: (row.get("DiscDate", ""), row.get("DiscTime", ""), row.get("DiscNo", "")))
    progress_events = build_progress_events(ordered)
    progress_by_key = {
        (event["disclosure_date"], event["quarter"]): event for event in progress_events
    }
    annual_net_history = defaultdict(list)
    annual_operating_history = defaultdict(list)
    same_quarter_sales = defaultdict(list)
    same_quarter_net = defaultdict(list)
    latest_shares = {}
    latest_forecast_net = {}
    events = []

    for row in ordered:
        code = str(row.get("Code", ""))[:4]
        period = row.get("CurPerType")
        shares = number(row.get("ShOutFY"))
        treasury = number(row.get("TrShFY")) or 0
        if shares is not None and shares > treasury:
            latest_shares[code] = shares - treasury

        if period == "FY":
            operating = number(row.get("OP"))
            next_operating = number(row.get("NxFOP"))
            net_income = number(row.get("NP"))
            next_net = number(row.get("NxFNp"))
            sales = number(row.get("Sales"))
            next_sales = number(row.get("NxFSales"))
            if next_net is not None:
                latest_forecast_net[code] = next_net
            prior_operating = annual_operating_history[code][-1] if annual_operating_history[code] else None
            prior_net = annual_net_history[code][-1] if annual_net_history[code] else None
            if operating is not None and next_operating is not None:
                recovery = None
                if operating <= 0 < next_operating:
                    recovery = 999.0
                elif operating > 0:
                    recovery = (next_operating / operating - 1) * 100
                events.append(
                    {
                        "code": code,
                        "disclosure_date": row.get("DiscDate"),
                        "stage": "company_forecast",
                        "quarter": 0,
                        "current_operating": operating,
                        "next_operating": next_operating,
                        "forecast_recovery": recovery,
                        "forecast_growth": None if operating <= 0 else next_operating / operating - 1,
                        "forecast_v_recovery": prior_operating is not None and prior_operating > operating and next_operating > operating,
                        "revenue_growth": None if sales in (None, 0) or next_sales is None else (next_sales / sales - 1) * 100,
                        "net_income": net_income,
                        "previous_net_income": prior_net,
                        "forecast_net": next_net,
                        "shares": latest_shares.get(code),
                    }
                )
            if operating is not None:
                annual_operating_history[code].append(operating)
            if net_income is not None:
                annual_net_history[code].append(net_income)
            continue

        quarter = {"1Q": 1, "2Q": 2, "3Q": 3}.get(period)
        if not quarter:
            continue
        base = progress_by_key.get((row.get("DiscDate"), quarter))
        if not base:
            continue
        sales = number(row.get("Sales"))
        net_income = number(row.get("NP"))
        sales_history = same_quarter_sales[(code, quarter)]
        net_history = same_quarter_net[(code, quarter)]
        previous_sales = sales_history[-1] if sales_history else None
        previous_net = net_history[-1] if net_history else None
        event = {
            **base,
            "stage": f"{quarter}Q",
            "revenue_growth": None if previous_sales in (None, 0) or sales is None else (sales / abs(previous_sales) - (1 if previous_sales > 0 else 0)) * 100,
            "net_income": net_income,
            "previous_net_income": previous_net,
            "forecast_net": number(row.get("FNP")) if number(row.get("FNP")) is not None else latest_forecast_net.get(code),
            "shares": latest_shares.get(code),
        }
        events.append(event)
        if sales is not None:
            sales_history.append(sales)
        if net_income is not None:
            net_history.append(net_income)
    return events


def price_features(prices, disclosure_date, shares=None):
    before = prices[prices.index <= pd.Timestamp(disclosure_date)].tail(504)
    if before.empty:
        return None
    close = float(before.iloc[-1]["close"])
    low = float(before["close"].min())
    high = float(before["high"].max())
    location = None if high == low else (close - low) / (high - low)
    one_year = before.tail(252)
    running_low = one_year["close"].cummin()
    max_gain = float(((one_year["high"] / running_low - 1) * 100).max())
    drawdown = (close / float(one_year["high"].max()) - 1) * 100
    if max_gain >= 100 and drawdown <= -35:
        cycle = "2倍後の往復済み"
    elif max_gain >= 100:
        cycle = "2倍到達済み"
    elif max_gain >= 60:
        cycle = "上昇進行中"
    else:
        cycle = "上昇前"
    return {
        "price_location": location,
        "max_gain": max_gain,
        "drawdown": drawdown,
        "cycle": cycle,
        "market_cap": None if shares is None else close * shares,
    }


def net_risk(event):
    current = event.get("net_income")
    previous = event.get("previous_net_income")
    forecast = event.get("forecast_net")
    if forecast is not None and forecast < 0:
        if current is not None and current >= 0:
            return "profit_to_loss"
        return "deeper_loss"
    if current is not None and previous is not None and current < previous:
        return "profit_decline"
    return None


def score_event(event, features):
    risk = net_risk(event)
    forecast_growth = event.get("forecast_growth")
    two_bagger_score = 70 if event.get("forecast_turnaround") or (forecast_growth or -99) >= 0.2 else 55
    jump = jump_signal(event) if event.get("quarter") else None
    row = {
        "Ticker": f"{event['code']}.T",
        "Fiscal Quarters Reported": event.get("quarter", 0),
        "Operating Forecast Progress": None if event.get("progress") is None else event["progress"] * 100,
        "Operating Pace Index": None if event.get("raw_pace") is None else event["raw_pace"] * 100,
        "Revenue Growth": event.get("revenue_growth"),
        "Market Cap": features.get("market_cap"),
        "Price Location": features.get("price_location"),
        "Max Gain From Prior Low": features.get("max_gain"),
        "Drawdown From 1Y High": features.get("drawdown"),
        "Price Cycle Status": features.get("cycle"),
        "Operating Recovery Rate": None if forecast_growth is None else forecast_growth * 100,
        "Two Bagger Score": two_bagger_score,
        "Two Bagger Candidate": None if jump is None else jump["tier"],
        "Current Fiscal Operating Income": event.get("current_operating"),
        "Next Fiscal Operating Income": event.get("next_operating"),
        "Safety Score": 40 if risk in {"profit_to_loss", "deeper_loss"} else 85,
        "Net Income Risk": risk,
        "Blocks": ["純利益赤字予想"] if risk in {"profit_to_loss", "deeper_loss"} else [],
        "Action": "買い候補",
        "Decision Tier": "買い優先",
        "Danger Notes": [],
        "Current Version Notes": [],
    }
    row.update(two_bagger_confirmation_values(row))
    row.update(buy_timing_values(row))
    return {
        **event,
        "timing_status": row["Buy Timing Status"],
        "timing_grade": row["Buy Timing Grade"],
        "timing_score": row["Buy Timing Score"],
        "confirmation_score": row["Two Bagger Confirmation Score"],
        "market_cap": features.get("market_cap"),
        "market_cap_bucket": "small" if features.get("market_cap") is not None and features["market_cap"] <= 20_000_000_000 else "large_or_unknown",
        "price_location": features.get("price_location"),
        "max_gain_before_signal": features.get("max_gain"),
        "price_cycle": features.get("cycle"),
    }


def entry_for_timing(prices, disclosure_date, timing_key):
    if timing_key == "pullback_5":
        eligible = prices[prices.index > pd.Timestamp(disclosure_date)]
        if eligible.empty:
            return None, None
        initial = float(eligible.iloc[0]["close"])
        hits = eligible.iloc[1:21]
        hits = hits[hits["close"] <= initial * 0.95]
        if hits.empty:
            return None, None
        return hits.index[0], float(hits.iloc[0]["close"])
    offset = dict(TIMINGS)[timing_key]
    return trading_entry(prices, disclosure_date, offset)


def attach_performance(event, prices, benchmark, timing_key):
    entry_date, buy = entry_for_timing(prices, event["disclosure_date"], timing_key)
    if entry_date is None:
        return None
    result = {**event, "timing_key": timing_key}
    benchmark_buy = price_on_or_after(benchmark, entry_date)
    for months in HORIZONS:
        target = entry_date + pd.DateOffset(months=months)
        close = price_on_or_before(prices, target)
        benchmark_close = price_on_or_before(benchmark, target)
        result[f"return_{months}m"] = None if close is None else (close / buy - 1) * 100
        benchmark_return = None if benchmark_buy is None or benchmark_close is None else (benchmark_close / benchmark_buy - 1) * 100
        result[f"alpha_{months}m"] = None if result[f"return_{months}m"] is None or benchmark_return is None else result[f"return_{months}m"] - benchmark_return
    for months in (6, 12):
        target = entry_date + pd.DateOffset(months=months)
        complete = prices.index.max() >= target - timedelta(days=10)
        window = prices[(prices.index >= entry_date) & (prices.index <= target)]
        result[f"max_{months}m"] = None if not complete or window.empty else (float(window["high"].max()) / buy - 1) * 100
        result[f"drawdown_{months}m"] = None if not complete or window.empty else (float(window["low"].min()) / buy - 1) * 100
        if months == 12 and complete and not window.empty:
            target_hits = list(window.index[window["high"] >= buy * 1.5])
            double_hits = list(window.index[window["high"] >= buy * 2.0])
            stop_hits = list(window.index[window["low"] <= buy * 0.75])
            first_target = target_hits[0] if target_hits else None
            first_double = double_hits[0] if double_hits else None
            first_stop = stop_hits[0] if stop_hits else None
            result["target50_before_stop"] = first_target is not None and (first_stop is None or first_target < first_stop)
            result["double_before_stop"] = first_double is not None and (first_stop is None or first_double < first_stop)
            result["stop25_first"] = first_stop is not None and (first_target is None or first_stop <= first_target)
    return result


def median(values):
    return None if not values else round(float(pd.Series(values).median()), 1)


def summarize(rows):
    output = {"events": len(rows), "companies": len({row["code"] for row in rows})}
    for months in HORIZONS:
        returns = [row[f"return_{months}m"] for row in rows if row.get(f"return_{months}m") is not None]
        alphas = [row[f"alpha_{months}m"] for row in rows if row.get(f"alpha_{months}m") is not None]
        output[f"n_{months}m"] = len(returns)
        output[f"median_{months}m"] = median(returns)
        output[f"win_{months}m"] = None if not returns else round(sum(value > 0 for value in returns) / len(returns) * 100, 1)
        output[f"median_alpha_{months}m"] = median(alphas)
        output[f"beat_market_{months}m"] = None if not alphas else round(sum(value > 0 for value in alphas) / len(alphas) * 100, 1)
    complete = [row for row in rows if row.get("max_12m") is not None]
    output["n_complete_12m"] = len(complete)
    for key in ("target50_before_stop", "double_before_stop", "stop25_first"):
        output[f"{key}_rate"] = None if not complete else round(sum(bool(row.get(key)) for row in complete) / len(complete) * 100, 1)
    output["median_max_12m"] = median([row["max_12m"] for row in complete])
    output["median_drawdown_12m"] = median([row["drawdown_12m"] for row in complete])
    rule_returns = []
    for row in complete:
        if row.get("target50_before_stop"):
            rule_returns.append(50.0)
        elif row.get("stop25_first"):
            rule_returns.append(-25.0)
        elif row.get("return_12m") is not None:
            rule_returns.append(row["return_12m"])
    output["median_target50_stop25_return"] = median(rule_returns)
    output["average_target50_stop25_return"] = None if not rule_returns else round(sum(rule_returns) / len(rule_returns), 1)
    return output


def grouped(rows, key, order=None):
    values = order or sorted({row.get(key) for row in rows if row.get(key) is not None})
    return [{key: value, **summarize([row for row in rows if row.get(key) == value])} for value in values]


def monthly_rank_bands(rows):
    selected = []
    by_month = defaultdict(list)
    for row in rows:
        if row.get("timing_key") == "next_day":
            by_month[str(row["disclosure_date"])[:7]].append(row)
    for month_rows in by_month.values():
        best_by_code = {}
        for row in month_rows:
            if row["code"] not in best_by_code or row["timing_score"] > best_by_code[row["code"]]["timing_score"]:
                best_by_code[row["code"]] = row
        for rank, row in enumerate(sorted(best_by_code.values(), key=lambda item: item["timing_score"], reverse=True)[:5], start=1):
            selected.append({**row, "rank_band": "1" if rank == 1 else "2-3" if rank <= 3 else "4-5"})
    return grouped(selected, "rank_band", ["1", "2-3", "4-5"])


def pattern_definitions():
    small = lambda row: row.get("market_cap") is not None and row["market_cap"] <= 20_000_000_000
    unmoved = lambda row: row.get("price_cycle") == "上昇前" and (row.get("max_gain_before_signal") or 999) <= 50
    revised = lambda row: (row.get("forecast_revision_pct") or 0) > 0
    fast = lambda row: (row.get("raw_pace") or 0) >= 1.3
    sales15 = lambda row: (row.get("revenue_growth") or -999) >= 15
    return (
        ("現行ランキング候補", lambda row: row.get("timing_status") in ELIGIBLE_STATUSES),
        ("3Q急進捗", lambda row: row.get("quarter") == 3 and fast(row)),
        ("3Q急進捗＋上方修正", lambda row: row.get("quarter") == 3 and fast(row) and revised(row)),
        ("1Q/2Q急進捗＋上方修正", lambda row: row.get("quarter") in (1, 2) and fast(row) and revised(row)),
        ("小型＋未上昇＋上方修正", lambda row: small(row) and unmoved(row) and revised(row)),
        ("小型＋未上昇＋売上15%＋急進捗", lambda row: small(row) and unmoved(row) and sales15(row) and (row.get("raw_pace") or 0) >= 1.2),
        ("赤字黒字予想＋小型＋未上昇", lambda row: bool(row.get("forecast_turnaround")) and small(row) and unmoved(row)),
        ("V字予想＋小型＋未上昇", lambda row: bool(row.get("forecast_v_recovery")) and small(row) and unmoved(row)),
        ("営業予想20%＋小型＋未上昇", lambda row: (row.get("forecast_growth") or -999) >= 0.2 and small(row) and unmoved(row)),
        ("売上20%＋小型＋未上昇", lambda row: (row.get("revenue_growth") or -999) >= 20 and small(row) and unmoved(row)),
    )


def validate_patterns(rows, split_date="2024-01-01"):
    output = []
    for label, predicate in pattern_definitions():
        selected = [row for row in rows if predicate(row)]
        train = [row for row in selected if str(row.get("disclosure_date")) < split_date]
        test = [row for row in selected if str(row.get("disclosure_date")) >= split_date]
        train_summary = summarize(train)
        test_summary = summarize(test)
        output.append(
            {
                "pattern": label,
                "train_period": f"before {split_date}",
                "test_period": f"from {split_date}",
                "train": train_summary,
                "test": test_summary,
                "enough_samples": train_summary["n_complete_12m"] >= 15 and test_summary["n_complete_12m"] >= 10,
            }
        )
    output.sort(
        key=lambda item: (
            item["enough_samples"],
            item["train"].get("double_before_stop_rate") or -1,
            item["test"].get("double_before_stop_rate") or -1,
        ),
        reverse=True,
    )
    return output


def evaluate_company(api_key, item, benchmark):
    client = JQuantsClient(api_key)
    records = client.get_all("fins/summary", {"code": item["code"]})
    events = build_point_in_time_events(records)
    dates = [event["disclosure_date"] for event in events if event.get("disclosure_date")]
    if not dates:
        return {"rows": [], "research_rows": [], "candidate_events": 0, "pullback_eligible": 0, "dates": []}
    prices = fetch_jquants_price_frame(client, item["code"])
    if prices is None:
        raise RuntimeError("price history unavailable")
    rows = []
    research_rows = []
    candidate_events = 0
    pullback_eligible = 0
    for event in events:
        features = price_features(prices, event["disclosure_date"], event.get("shares"))
        if features is None:
            continue
        scored = score_event(event, features)
        if scored["timing_status"] not in {"対象外", "上昇一巡", "データ確認待ち"}:
            research_row = attach_performance(scored, prices, benchmark, "next_day")
            if research_row:
                research_rows.append(research_row)
        if scored["timing_status"] not in ELIGIBLE_STATUSES:
            continue
        candidate_events += 1
        for timing_key, _ in TIMINGS:
            row = attach_performance(scored, prices, benchmark, timing_key)
            if row:
                rows.append(row)
        pullback_eligible += 1
        row = attach_performance(scored, prices, benchmark, "pullback_5")
        if row:
            rows.append(row)
    return {
        "rows": rows,
        "research_rows": research_rows,
        "candidate_events": candidate_events,
        "pullback_eligible": pullback_eligible,
        "dates": dates,
    }


def run(api_key, limit, universe_mode, workers=3):
    client = JQuantsClient(api_key)
    universe = load_universe(limit, universe_mode)
    benchmark = fetch_jquants_price_frame(client, "1306")
    if benchmark is None:
        raise RuntimeError("benchmark price history unavailable")
    all_rows = []
    research_rows = []
    candidate_events = 0
    pullback_eligible = 0
    failures = 0
    failure_reasons = defaultdict(int)
    earliest = None
    latest = None
    worker_count = max(1, workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(evaluate_company, api_key, item, benchmark): item for item in universe}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                result = future.result()
                all_rows.extend(result["rows"])
                research_rows.extend(result["research_rows"])
                candidate_events += result["candidate_events"]
                pullback_eligible += result["pullback_eligible"]
                dates = result["dates"]
                if dates:
                    earliest = min([earliest] + dates) if earliest else min(dates)
                    latest = max([latest] + dates) if latest else max(dates)
            except Exception as error:
                failures += 1
                message = str(error).lower()
                if "price history unavailable" in message:
                    reason = "price_history_unavailable"
                elif "429" in message:
                    reason = "rate_limited_after_retries"
                elif "timed out" in message or "timeout" in message:
                    reason = "network_timeout"
                else:
                    reason = type(error).__name__
                failure_reasons[reason] += 1
            if completed % 25 == 0:
                print(f"Validated {completed}/{len(universe)} companies; source records are not stored")

    timing_labels = {
        "next_day": "翌営業日",
        "one_week": "1週間後",
        "two_weeks": "2週間後",
        "one_month": "1か月後",
        "pullback_5": "20営業日以内の5%押し目",
    }
    timing = []
    for key, label in timing_labels.items():
        selected = [row for row in all_rows if row["timing_key"] == key]
        item = {"timing_key": key, "timing": label, **summarize(selected)}
        if key == "pullback_5":
            item["entry_hit_rate"] = None if not pullback_eligible else round(len(selected) / pullback_eligible * 100, 1)
        timing.append(item)
    next_day = [row for row in all_rows if row["timing_key"] == "next_day"]
    stage_entry_timing = []
    for stage in ("会社予想先回り", "業績確認済み", "3Q先回り"):
        for timing_key, label in timing_labels.items():
            selected = [
                row for row in all_rows
                if row["timing_status"] == stage and row["timing_key"] == timing_key
            ]
            item = {
                "timing_status": stage,
                "timing_key": timing_key,
                "timing": label,
                **summarize(selected),
            }
            stage_entry_timing.append(item)
    return {
        "privacy": "aggregate-only; raw J-Quants records and company-level events are not persisted or printed",
        "methodology": "point-in-time current-ranking-v1",
        "universe_companies": len(universe),
        "failed_companies": failures,
        "failure_reasons": dict(failure_reasons),
        "candidate_events": candidate_events,
        "source_coverage": {"earliest_disclosure": earliest, "latest_disclosure": latest},
        "entry_timing": timing,
        "stage_entry_timing": stage_entry_timing,
        "by_stage": grouped(next_day, "timing_status", ["会社予想先回り", "業績確認済み", "3Q先回り"]),
        "by_grade": grouped(next_day, "timing_grade", ["S", "A", "B"]),
        "monthly_top5_rank_bands": monthly_rank_bands(next_day),
        "alternative_pattern_validation": validate_patterns(research_rows),
        "limitations": [
            "Current listed target-sector universe creates survivorship bias.",
            "Historical market cap uses disclosed year-end shares times pre-disclosure adjusted close.",
            "Same-day target and stop touches are counted conservatively as stop first.",
            "Transaction costs, taxes, liquidity, and limit-up execution are excluded.",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--universe", choices=("current", "target"), default="target")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    api_key = os.environ.get("J_QUANTS", "").strip()
    if not api_key:
        raise SystemExit("J_QUANTS is required")
    print(json.dumps(run(api_key, args.limit, args.universe, args.workers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
