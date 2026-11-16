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
    prior_progress = defaultdict(list)
    events = []

    for row in ordered:
        current_key = fiscal_key(row.get("CurFYSt"), row.get("CurFYEn"))
        next_key = fiscal_key(row.get("NxtFYSt"), row.get("NxtFYEn"))

        current_forecast = number(row.get("FOP"))
        if current_forecast is not None and all(current_key):
            forecasts[current_key] = current_forecast
        next_forecast = number(row.get("NxFOP"))
        if next_forecast is not None and all(next_key):
            forecasts[next_key] = next_forecast

        quarter = QUARTERS.get(row.get("CurPerType"))
        operating = number(row.get("OP"))
        forecast = forecasts.get(current_key)
        if not quarter or operating is None or forecast is None or forecast <= 0:
            continue

        progress = operating / forecast
        expected = quarter / 4
        raw_pace = progress / expected
        history_key = (row.get("Code", ""), quarter)
        history = prior_progress[history_key]
        seasonal_baseline = float(pd.Series(history).median()) if len(history) >= min_seasonal_history else None
        seasonal_pace = progress / seasonal_baseline if seasonal_baseline and seasonal_baseline > 0 else None
        events.append(
            {
                "code": row.get("Code", "")[:4],
                "disclosure_date": row.get("DiscDate"),
                "quarter": quarter,
                "progress": progress,
                "raw_pace": raw_pace,
                "seasonal_pace": seasonal_pace,
                "seasonal_history": len(history),
            }
        )
        history.append(progress)
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


def load_universe(limit):
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    current = report.get("current_rankings", [])
    rows = current[:limit] if limit else current
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
    return frame[["Close", "High"]].dropna(subset=["Close"]).rename(columns={"Close": "close", "High": "high"})


def price_on_or_after(frame, date, days=10):
    date = pd.Timestamp(date)
    window = frame[(frame.index >= date) & (frame.index <= date + timedelta(days=days))]
    return None if window.empty else float(window.iloc[0]["close"])


def price_on_or_before(frame, date, days=10):
    date = pd.Timestamp(date)
    window = frame[(frame.index <= date) & (frame.index >= date - timedelta(days=days))]
    return None if window.empty else float(window.iloc[-1]["close"])


def attach_returns(events, prices, benchmark):
    completed = []
    for event in events:
        disclosure = pd.Timestamp(event["disclosure_date"])
        entry_date = disclosure + timedelta(days=1)
        buy = price_on_or_after(prices, entry_date)
        benchmark_buy = price_on_or_after(benchmark, entry_date)
        if buy is None:
            continue
        row = dict(event)
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
        completed.append(row)
    return completed


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
    return summary


def run(api_key, limit):
    client = JQuantsClient(api_key)
    universe = load_universe(limit)
    all_events = []
    failures = 0
    earliest = None
    latest = None
    for index, item in enumerate(universe, start=1):
        try:
            records = client.get_all("fins/summary", {"code": item["code"]})
            events = build_progress_events(records)
            if records:
                dates = [row.get("DiscDate") for row in records if row.get("DiscDate")]
                if dates:
                    earliest = min([earliest] + dates) if earliest else min(dates)
                    latest = max([latest] + dates) if latest else max(dates)
            if events:
                start = (pd.Timestamp(min(row["disclosure_date"] for row in events)) - timedelta(days=10)).strftime("%Y-%m-%d")
                end = (pd.Timestamp(max(row["disclosure_date"] for row in events)) + pd.DateOffset(months=12) + timedelta(days=15)).strftime("%Y-%m-%d")
                prices = fetch_price_frame(item["ticker"], start, end)
                benchmark = fetch_price_frame("1306.T", start, end)
                if prices is not None and benchmark is not None:
                    all_events.extend(attach_returns(events, prices, benchmark))
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
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50, help="Top current candidates to test; 0 means all current candidates")
    args = parser.parse_args()
    api_key = os.environ.get("J_QUANTS", "").strip()
    if not api_key:
        raise SystemExit("J_QUANTS is required")
    print(json.dumps(run(api_key, args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
