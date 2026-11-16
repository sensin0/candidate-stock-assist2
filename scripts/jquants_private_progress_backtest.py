"""Private J-Quants quarterly progress backtest.

Raw J-Quants responses are held in memory only. The script prints aggregate,
non-reconstructable statistics and never writes source records to disk.
"""

import argparse
import contextlib
import io
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

try:
    from scripts.weekly_cloud_ranker import CURRENT_TARGET_SECTORS
except ModuleNotFoundError:
    from weekly_cloud_ranker import CURRENT_TARGET_SECTORS


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "weekly_ranking_report.json"
API_BASE = "https://api.jquants.com/v2"
QUARTERS = {"1Q": 1, "2Q": 2, "3Q": 3}
HORIZONS = (1, 3, 6, 12)


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def fiscal_key(start, end):
    return (str(start or ""), str(end or ""))


def build_progress_events(records, min_seasonal_history=2):
    """Build point-in-time progress events without leaking future forecasts."""
    ordered = sorted(records, key=lambda row: (row.get("DiscDate", ""), row.get("DiscTime", ""), row.get("DiscNo", "")))
    forecasts = {}
    sales_forecasts = {}
    prior_progress = defaultdict(list)
    prior_sales_progress = defaultdict(list)
    prior_quarter_operating = defaultdict(list)
    latest_annual_operating = {}
    annual_operating_history = defaultdict(list)
    events = []

    for row in ordered:
        current_key = fiscal_key(row.get("CurFYSt"), row.get("CurFYEn"))
        next_key = fiscal_key(row.get("NxtFYSt"), row.get("NxtFYEn"))

        code = row.get("Code", "")
        prior_forecast = forecasts.get(current_key)
        current_forecast = number(row.get("FOP"))
        if current_forecast is not None and all(current_key):
            forecasts[current_key] = current_forecast
        next_forecast = number(row.get("NxFOP"))
        if next_forecast is not None and all(next_key):
            forecasts[next_key] = next_forecast

        prior_sales_forecast = sales_forecasts.get(current_key)
        current_sales_forecast = number(row.get("FSales"))
        if current_sales_forecast is not None and all(current_key):
            sales_forecasts[current_key] = current_sales_forecast
        next_sales_forecast = number(row.get("NxFSales"))
        if next_sales_forecast is not None and all(next_key):
            sales_forecasts[next_key] = next_sales_forecast

        quarter = QUARTERS.get(row.get("CurPerType"))
        operating = number(row.get("OP"))
        if row.get("CurPerType") == "FY" and operating is not None:
            latest_annual_operating[code] = operating
            annual_operating_history[code].append(operating)
        forecast = forecasts.get(current_key)
        if not quarter or operating is None or forecast is None or forecast <= 0:
            continue

        progress = operating / forecast
        expected = quarter / 4
        raw_pace = progress / expected
        history_key = (code, quarter)
        history = prior_progress[history_key]
        seasonal_baseline = float(pd.Series(history).median()) if len(history) >= min_seasonal_history else None
        seasonal_pace = progress / seasonal_baseline if seasonal_baseline and seasonal_baseline > 0 else None
        revision_pct = None
        if prior_forecast not in (None, 0) and current_forecast is not None:
            revision_pct = current_forecast / prior_forecast - 1
        prior_annual = latest_annual_operating.get(code)
        annual_history = annual_operating_history[code]
        forecast_growth = None if prior_annual in (None, 0) else forecast / abs(prior_annual) - (1 if prior_annual > 0 else 0)
        quarter_history = prior_quarter_operating[history_key]
        yoy_operating_growth = None
        if quarter_history and quarter_history[-1] not in (None, 0):
            yoy_operating_growth = operating / abs(quarter_history[-1]) - (1 if quarter_history[-1] > 0 else 0)

        sales = number(row.get("Sales"))
        sales_forecast = sales_forecasts.get(current_key)
        sales_progress = None if sales is None or not sales_forecast or sales_forecast <= 0 else sales / sales_forecast
        sales_history = prior_sales_progress[history_key]
        sales_seasonal_baseline = float(pd.Series(sales_history).median()) if len(sales_history) >= min_seasonal_history else None
        sales_seasonal_pace = (
            sales_progress / sales_seasonal_baseline
            if sales_progress is not None and sales_seasonal_baseline and sales_seasonal_baseline > 0
            else None
        )
        sales_revision_pct = None
        if prior_sales_forecast not in (None, 0) and current_sales_forecast is not None:
            sales_revision_pct = current_sales_forecast / prior_sales_forecast - 1
        events.append(
            {
                "code": code[:4],
                "disclosure_date": row.get("DiscDate"),
                "quarter": quarter,
                "progress": progress,
                "raw_pace": raw_pace,
                "seasonal_pace": seasonal_pace,
                "seasonal_history": len(history),
                "forecast_revision_pct": revision_pct,
                "forecast_growth": forecast_growth,
                "forecast_turnaround": prior_annual is not None and prior_annual <= 0 < forecast,
                "forecast_v_recovery": len(annual_history) >= 2 and annual_history[-2] > annual_history[-1] and forecast > annual_history[-1],
                "yoy_operating_growth": yoy_operating_growth,
                "sales_progress": sales_progress,
                "sales_seasonal_pace": sales_seasonal_pace,
                "sales_revision_pct": sales_revision_pct,
            }
        )
        history.append(progress)
        quarter_history.append(operating)
        if sales_progress is not None:
            sales_history.append(sales_progress)
    return events


def pace_bucket(event):
    pace = event.get("seasonal_pace")
    if pace is None:
        return "季節性履歴不足"
    if pace >= 1.5:
        return "過去同四半期比150%以上"
    if pace >= 1.2:
        return "過去同四半期比120-150%"
    if pace >= 0.8:
        return "過去同四半期比80-120%"
    return "過去同四半期比80%未満"


class JQuantsClient:
    def __init__(self, api_key, timeout=30):
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "User-Agent": "candidate-stock-assist-private-backtest/1"})
        self.timeout = timeout

    def get_all(self, path, params):
        output = []
        query = dict(params)
        for _ in range(100):
            for attempt in range(6):
                response = self.session.get(f"{API_BASE}/{path}", params=query, timeout=self.timeout)
                if response.status_code != 429:
                    break
                time.sleep(min(2 ** attempt, 30))
            response.raise_for_status()
            payload = response.json()
            output.extend(payload.get("data", []))
            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                return output
            query["pagination_key"] = pagination_key
        raise RuntimeError("J-Quants pagination exceeded safety limit")


def load_universe(limit, universe_mode):
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if universe_mode == "target":
        selected = [row for row in report.get("rankings", []) if row.get("Sector") in CURRENT_TARGET_SECTORS]
    else:
        selected = report.get("current_rankings", [])
    rows = selected[:limit] if limit else selected
    seen = set()
    universe = []
    for row in rows:
        ticker = str(row.get("Ticker", ""))
        code = ticker.split(".")[0]
        if len(code) == 4 and code.isdigit() and code not in seen:
            seen.add(code)
            universe.append({"code": code, "ticker": f"{code}.T"})
    return universe


def fetch_price_frame(ticker, start, end):
    # yfinance can print ticker-specific warnings directly to stderr. Suppress
    # them so public Actions logs remain aggregate-only as well.
    logger = logging.getLogger("yfinance")
    previous_disabled = logger.disabled
    logger.disabled = True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False, threads=False)
    finally:
        logger.disabled = previous_disabled
    if frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return frame[["Close", "High", "Low"]].dropna(subset=["Close"]).rename(columns={"Close": "close", "High": "high", "Low": "low"})


def price_on_or_after(frame, date, days=10):
    date = pd.Timestamp(date)
    window = frame[(frame.index >= date) & (frame.index <= date + timedelta(days=days))]
    return None if window.empty else float(window.iloc[0]["close"])


def price_on_or_before(frame, date, days=10):
    date = pd.Timestamp(date)
    window = frame[(frame.index <= date) & (frame.index >= date - timedelta(days=days))]
    return None if window.empty else float(window.iloc[-1]["close"])


def trading_entry(frame, disclosure_date, session_offset):
    eligible = frame[frame.index > pd.Timestamp(disclosure_date)]
    if len(eligible) <= session_offset:
        return None, None
    return eligible.index[session_offset], float(eligible.iloc[session_offset]["close"])


def attach_returns(events, prices, benchmark, session_offset=0, timing_key=None):
    completed = []
    for event in events:
        disclosure = pd.Timestamp(event["disclosure_date"])
        entry_date, buy = trading_entry(prices, disclosure, session_offset)
        if entry_date is None:
            continue
        benchmark_buy = price_on_or_after(benchmark, entry_date)
        row = dict(event)
        if timing_key:
            row["timing_key"] = timing_key
        for months in HORIZONS:
            target = entry_date + pd.DateOffset(months=months)
            close = price_on_or_before(prices, target)
            benchmark_close = price_on_or_before(benchmark, target)
            stock_return = None if close is None else (close / buy - 1) * 100
            benchmark_return = None if benchmark_buy is None or benchmark_close is None else (benchmark_close / benchmark_buy - 1) * 100
            row[f"return_{months}m"] = stock_return
            row[f"alpha_{months}m"] = None if stock_return is None or benchmark_return is None else stock_return - benchmark_return
        for months in (6, 12):
            target = entry_date + pd.DateOffset(months=months)
            complete = prices.index.max() >= target - timedelta(days=10)
            window = prices[(prices.index >= entry_date) & (prices.index <= target)]
            row[f"max_{months}m"] = None if not complete or window.empty else (float(window["high"].max()) / buy - 1) * 100
            row[f"drawdown_{months}m"] = None if not complete or window.empty else (float(window["low"].min()) / buy - 1) * 100
        completed.append(row)
    return completed


def attach_pullback_returns(events, prices, benchmark, threshold=0.95, sessions=20):
    completed = []
    eligible_events = 0
    for event in events:
        disclosure = pd.Timestamp(event["disclosure_date"])
        eligible = prices[prices.index > disclosure]
        if eligible.empty:
            continue
        eligible_events += 1
        initial = float(eligible.iloc[0]["close"])
        search = eligible.iloc[1:sessions + 1]
        hits = search[search["close"] <= initial * threshold]
        if hits.empty:
            continue
        entry_date = hits.index[0]
        buy = float(hits.iloc[0]["close"])
        benchmark_buy = price_on_or_after(benchmark, entry_date)
        row = {**event, "timing_key": "pullback_5"}
        for months in HORIZONS:
            target = entry_date + pd.DateOffset(months=months)
            close = price_on_or_before(prices, target)
            benchmark_close = price_on_or_before(benchmark, target)
            stock_return = None if close is None else (close / buy - 1) * 100
            benchmark_return = None if benchmark_buy is None or benchmark_close is None else (benchmark_close / benchmark_buy - 1) * 100
            row[f"return_{months}m"] = stock_return
            row[f"alpha_{months}m"] = None if stock_return is None or benchmark_return is None else stock_return - benchmark_return
        for months in (6, 12):
            target = entry_date + pd.DateOffset(months=months)
            complete = prices.index.max() >= target - timedelta(days=10)
            window = prices[(prices.index >= entry_date) & (prices.index <= target)]
            row[f"max_{months}m"] = None if not complete or window.empty else (float(window["high"].max()) / buy - 1) * 100
            row[f"drawdown_{months}m"] = None if not complete or window.empty else (float(window["low"].min()) / buy - 1) * 100
        completed.append(row)
    return completed, eligible_events


def summarize(rows):
    summary = {"events": len(rows), "companies": len({row["code"] for row in rows})}
    for months in HORIZONS:
        values = [row[f"return_{months}m"] for row in rows if row.get(f"return_{months}m") is not None]
        alphas = [row[f"alpha_{months}m"] for row in rows if row.get(f"alpha_{months}m") is not None]
        summary[f"n_{months}m"] = len(values)
        summary[f"median_{months}m"] = round(float(pd.Series(values).median()), 1) if values else None
        summary[f"win_{months}m"] = round(sum(value > 0 for value in values) / len(values) * 100, 1) if values else None
        summary[f"median_alpha_{months}m"] = round(float(pd.Series(alphas).median()), 1) if alphas else None
        summary[f"beat_market_{months}m"] = round(sum(value > 0 for value in alphas) / len(alphas) * 100, 1) if alphas else None
    for months in (6, 12):
        maxima = [row[f"max_{months}m"] for row in rows if row.get(f"max_{months}m") is not None]
        summary[f"n_max_{months}m"] = len(maxima)
        summary[f"median_max_{months}m"] = round(float(pd.Series(maxima).median()), 1) if maxima else None
        summary[f"double_rate_{months}m"] = round(sum(value >= 100 for value in maxima) / len(maxima) * 100, 1) if maxima else None
        drawdowns = [row[f"drawdown_{months}m"] for row in rows if row.get(f"drawdown_{months}m") is not None]
        summary[f"median_drawdown_{months}m"] = round(float(pd.Series(drawdowns).median()), 1) if drawdowns else None
    return summary


def condition_definitions():
    return (
        ("進捗150%以上", lambda row: (row.get("seasonal_pace") or 0) >= 1.5),
        ("進捗150%以上＋上方修正", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("forecast_revision_pct") or 0) > 0),
        ("進捗150%以上＋上方修正＋1Q/2Q", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("forecast_revision_pct") or 0) > 0 and row.get("quarter") in (1, 2)),
        ("進捗150%以上＋上方修正＋営業利益予想20%以上", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("forecast_revision_pct") or 0) > 0 and (row.get("forecast_growth") or -99) >= 0.2),
        ("進捗150%以上＋上方修正＋営業利益予想20%以上＋1Q/2Q", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("forecast_revision_pct") or 0) > 0 and (row.get("forecast_growth") or -99) >= 0.2 and row.get("quarter") in (1, 2)),
        ("進捗150%以上＋営業利益予想20%以上", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("forecast_growth") or -99) >= 0.2),
        ("進捗150%以上＋営業利益V字回復", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and row.get("forecast_v_recovery")),
        ("進捗150%以上＋赤字から黒字予想", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and row.get("forecast_turnaround")),
        ("進捗150%以上＋前年同期営業利益20%以上", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("yoy_operating_growth") or -99) >= 0.2),
        ("進捗150%以上＋売上進捗120%以上", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and (row.get("sales_seasonal_pace") or 0) >= 1.2),
        ("進捗150%以上＋1Q", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and row.get("quarter") == 1),
        ("進捗150%以上＋2Q", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and row.get("quarter") == 2),
        ("進捗150%以上＋3Q", lambda row: (row.get("seasonal_pace") or 0) >= 1.5 and row.get("quarter") == 3),
    )


def condition_summaries(rows):
    output = []
    for label, predicate in condition_definitions():
        selected = [row for row in rows if predicate(row)]
        years = defaultdict(int)
        for row in selected:
            if row.get("disclosure_date"):
                years[str(row["disclosure_date"])[:4]] += 1
        output.append(
            {
                "condition": label,
                **summarize(selected),
                "signals_per_year": dict(sorted(years.items())),
                "average_signals_per_year": round(sum(years.values()) / len(years), 1) if years else 0,
            }
        )
    return output


def current_signal_counts(events, as_of):
    latest_by_code = {}
    for event in events:
        code = event.get("code")
        if code and (code not in latest_by_code or str(event.get("disclosure_date")) > str(latest_by_code[code].get("disclosure_date"))):
            latest_by_code[code] = event
    cutoff = pd.Timestamp(as_of) - timedelta(days=210)
    recent = [event for event in latest_by_code.values() if pd.Timestamp(event["disclosure_date"]) >= cutoff]
    return {
        "as_of": as_of,
        "recent_latest_quarterly_companies": len(recent),
        "counts": [
            {"condition": label, "companies": sum(1 for event in recent if predicate(event))}
            for label, predicate in condition_definitions()
        ],
    }


def timing_summaries(rows, pullback_eligible):
    labels = {
        "next_day": "翌営業日",
        "one_week": "1週間後",
        "two_weeks": "2週間後",
        "one_month": "1か月後",
        "pullback_5": "20営業日以内の5%押し目",
    }
    output = []
    for key, label in labels.items():
        selected = [row for row in rows if row.get("timing_key") == key]
        result = {"timing": label, **summarize(selected)}
        if key == "pullback_5":
            result["entry_hit_rate"] = round(len(selected) / pullback_eligible * 100, 1) if pullback_eligible else None
        output.append(result)
    return output


def run(api_key, limit, universe_mode):
    client = JQuantsClient(api_key)
    universe = load_universe(limit, universe_mode)
    all_events = []
    all_progress_events = []
    timing_rows = []
    pullback_eligible = 0
    failures = 0
    earliest = None
    latest = None
    benchmark = fetch_price_frame("1306.T", "2021-01-01", (pd.Timestamp.now() + timedelta(days=2)).strftime("%Y-%m-%d"))
    if benchmark is None:
        raise RuntimeError("benchmark price history unavailable")
    for index, item in enumerate(universe, start=1):
        try:
            records = client.get_all("fins/summary", {"code": item["code"]})
            events = build_progress_events(records)
            all_progress_events.extend(events)
            if records:
                dates = [row.get("DiscDate") for row in records if row.get("DiscDate")]
                if dates:
                    earliest = min([earliest] + dates) if earliest else min(dates)
                    latest = max([latest] + dates) if latest else max(dates)
            if events:
                start = (pd.Timestamp(min(row["disclosure_date"] for row in events)) - timedelta(days=10)).strftime("%Y-%m-%d")
                end = (pd.Timestamp(max(row["disclosure_date"] for row in events)) + pd.DateOffset(months=12) + timedelta(days=15)).strftime("%Y-%m-%d")
                prices = fetch_price_frame(item["ticker"], start, end)
                if prices is not None:
                    all_events.extend(attach_returns(events, prices, benchmark))
                    fast_events = [event for event in events if (event.get("seasonal_pace") or 0) >= 1.5]
                    for key, offset in (("next_day", 0), ("one_week", 4), ("two_weeks", 9), ("one_month", 19)):
                        timing_rows.extend(attach_returns(fast_events, prices, benchmark, offset, key))
                    pullback_rows, eligible_count = attach_pullback_returns(fast_events, prices, benchmark)
                    timing_rows.extend(pullback_rows)
                    pullback_eligible += eligible_count
        except Exception:
            failures += 1
        if index % 25 == 0:
            print(f"Processed {index}/{len(universe)} companies; raw records are not stored")

    groups = []
    for label in ("過去同四半期比150%以上", "過去同四半期比120-150%", "過去同四半期比80-120%", "過去同四半期比80%未満", "季節性履歴不足"):
        rows = [row for row in all_events if pace_bucket(row) == label]
        if rows:
            groups.append({"pace_group": label, **summarize(rows)})
    return {
        "privacy": "aggregate-only; no J-Quants source records persisted or printed",
        "universe_companies": len(universe),
        "failed_companies": failures,
        "source_coverage": {"earliest_disclosure": earliest, "latest_disclosure": latest},
        "groups": groups,
        "conditions": condition_summaries(all_events),
        "current_signal_counts": current_signal_counts(all_progress_events, latest) if latest else None,
        "entry_timing_for_150pct_progress": timing_summaries(timing_rows, pullback_eligible),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50, help="Top current candidates to test; 0 means all current candidates")
    parser.add_argument("--universe", choices=("current", "target"), default="current")
    args = parser.parse_args()
    api_key = os.environ.get("J_QUANTS", "").strip()
    if not api_key:
        raise SystemExit("J_QUANTS is required")
    print(json.dumps(run(api_key, args.limit, args.universe), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
