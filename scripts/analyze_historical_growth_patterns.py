import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from scripts.backtest_strategy_compare import future_returns, loss_margin_improving, price_location, safe_pct
from scripts.weekly_cloud_ranker import CURRENT_TARGET_SECTORS


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "stocks.db"
OUTPUT_JSON = ROOT / "docs" / "historical_growth_patterns.json"
OUTPUT_MD = ROOT / "docs" / "historical_growth_patterns.md"
VALIDATION_START = pd.Timestamp("2024-01-01")


def revenue_bucket(value):
    if value is None:
        return "unknown"
    if value < 0:
        return "<0%"
    if value < 10:
        return "0-9.9%"
    if value < 15:
        return "10-14.9%"
    if value < 20:
        return "15-19.9%"
    if value < 30:
        return "20-29.9%"
    return "30%+"


def price_bucket(value):
    if value is None:
        return "unknown"
    if value < 0.1:
        return "0-9.9%"
    if value < 0.2:
        return "10-19.9%"
    if value < 0.3:
        return "20-29.9%"
    if value < 0.5:
        return "30-49.9%"
    return "50%+"


def profit_pattern(latest_net, previous_net, improving):
    if previous_net < 0 and latest_net < 0:
        return "two_loss_improving" if improving else "two_loss_not_improving"
    if previous_net < 0 <= latest_net:
        return "loss_to_profit"
    if previous_net >= 0 > latest_net:
        return "profit_to_loss"
    return "profitable"


def metrics(frame):
    if frame.empty:
        return {"Observations": 0, "Unique Tickers": 0}
    return {
        "Observations": int(len(frame)),
        "Unique Tickers": int(frame["Ticker"].nunique()),
        "Avg 1yr": round(frame["1yr Return"].mean() * 100, 1),
        "Median 1yr": round(frame["1yr Return"].median() * 100, 1),
        "Win 1yr": round((frame["1yr Return"] > 0).mean() * 100, 1),
        "Median 1yr Alpha": round(frame["1yr Alpha"].median() * 100, 1),
        "Win vs Universe": round((frame["1yr Alpha"] > 0).mean() * 100, 1),
        "Target 50 Hit": round(frame["Target 50 Hit"].mean() * 100, 1),
        "Doubler 2yr": round(frame["Target 100 Hit"].mean() * 100, 1),
        "Stop 25 Hit": round(frame["Stop 25 Hit"].mean() * 100, 1),
        "Rule 50 Avg": round(frame["Rule 50 Return"].mean() * 100, 1),
    }


def build_observations(conn):
    financials = pd.read_sql(
        "SELECT ticker, period_end, revenue, net_income, capital_expenditure, tangible_assets "
        "FROM financials ORDER BY ticker, period_end",
        conn,
    )
    tickers = pd.read_sql(
        "SELECT ticker, name, sector_name FROM tickers_master WHERE ticker IS NOT NULL AND ticker != ''",
        conn,
    )
    financials["period_end"] = pd.to_datetime(financials["period_end"])
    ticker_info = tickers.set_index("ticker").to_dict(orient="index")
    records = []
    latest_entry = pd.Timestamp(datetime.now() - timedelta(days=395))

    for ticker, group in financials.groupby("ticker"):
        info = ticker_info.get(ticker, {})
        if info.get("sector_name") not in CURRENT_TARGET_SECTORS:
            continue
        prices = pd.read_sql(
            "SELECT date, close, high, low FROM prices WHERE ticker = ? ORDER BY date",
            conn,
            params=(ticker,),
        )
        if prices.empty:
            continue
        prices["date"] = pd.to_datetime(prices["date"])
        prices = prices.set_index("date")
        group = group.sort_values("period_end").reset_index(drop=True)
        for index in range(1, len(group)):
            previous = group.iloc[index - 1]
            latest = group.iloc[index]
            values = [previous.get("net_income"), latest.get("net_income"), previous.get("revenue"), latest.get("revenue")]
            if any(pd.isna(value) for value in values):
                continue
            entry_date = pd.Timestamp(latest["period_end"]) + timedelta(days=60)
            if entry_date > latest_entry:
                continue
            location, buy_price = price_location(prices, entry_date)
            if location is None or buy_price is None:
                continue
            returns = future_returns(prices, entry_date, buy_price)
            if not returns:
                continue
            if returns["1yr Return"] < -0.95 or returns["1yr Return"] > 10 or returns["2yr Max Return"] > 20:
                continue
            growth = safe_pct(latest.get("revenue"), previous.get("revenue"))
            improving = loss_margin_improving(
                latest.get("net_income"),
                previous.get("net_income"),
                latest.get("revenue"),
                previous.get("revenue"),
            )
            records.append(
                {
                    "Ticker": ticker,
                    "Name": info.get("name"),
                    "Sector": info.get("sector_name"),
                    "Entry Date": entry_date.strftime("%Y-%m-%d"),
                    "Profit Pattern": profit_pattern(latest["net_income"], previous["net_income"], improving),
                    "Revenue Growth": growth,
                    "Revenue Bucket": revenue_bucket(growth),
                    "Price Location": location,
                    "Price Bucket": price_bucket(location),
                    "Loss Margin Improving": improving,
                    **returns,
                }
            )
    return pd.DataFrame(records)


def pattern_rows(observations):
    dimensions = [
        ("Profit Pattern",),
        ("Profit Pattern", "Revenue Bucket"),
        ("Profit Pattern", "Price Bucket"),
        ("Profit Pattern", "Revenue Bucket", "Price Bucket"),
    ]
    rows = []
    for columns in dimensions:
        grouper = columns[0] if len(columns) == 1 else list(columns)
        for key, group in observations.groupby(grouper, dropna=False):
            keys = (key,) if len(columns) == 1 else tuple(key)
            train = group[pd.to_datetime(group["Entry Date"]) < VALIDATION_START]
            validation = group[pd.to_datetime(group["Entry Date"]) >= VALIDATION_START]
            if len(train) < 5 or len(validation) < 3 or group["Ticker"].nunique() < 4:
                continue
            row = {column: value for column, value in zip(columns, keys)}
            row["Dimensions"] = " + ".join(columns)
            row["Train"] = metrics(train)
            row["Validation"] = metrics(validation)
            validation_metrics = row["Validation"]
            row["Robust"] = bool(
                row["Train"].get("Median 1yr", -999) > 0
                and validation_metrics.get("Median 1yr", -999) > 0
                and validation_metrics.get("Win 1yr", 0) >= 55
                and row["Train"].get("Median 1yr Alpha", -999) > 0
                and validation_metrics.get("Median 1yr Alpha", -999) > 0
                and validation_metrics.get("Win vs Universe", 0) >= 55
                and validation_metrics.get("Stop 25 Hit", 100) <= 40
            )
            rows.append(row)
    return sorted(
        rows,
        key=lambda row: (
            row["Robust"],
            row["Validation"].get("Median 1yr", -999),
            row["Validation"].get("Win 1yr", 0),
            row["Validation"].get("Unique Tickers", 0),
        ),
        reverse=True,
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    observations = build_observations(conn)
    conn.close()
    observations["Entry Month"] = pd.to_datetime(observations["Entry Date"]).dt.to_period("M").astype(str)
    observations["Universe 1yr"] = observations.groupby("Entry Month")["1yr Return"].transform("median")
    observations["1yr Alpha"] = observations["1yr Return"] - observations["Universe 1yr"]
    patterns = pattern_rows(observations)
    robust = [row for row in patterns if row["Robust"]]
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "Annual financial event + 60-day reporting lag; train before 2024; validate from 2024; target cyclical sectors only.",
        "limitations": [
            "Historical DB does not contain operating-income forecasts or historical market caps.",
            "Patterns require at least 5 train observations, 3 validation observations, and 4 unique tickers.",
            "This is pattern discovery, not a guarantee of future returns.",
        ],
        "universe": metrics(observations),
        "robust_patterns": robust[:20],
        "all_tested_patterns": patterns,
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Historical Growth Pattern Validation",
        "",
        f"Generated: {result['generated_at']}",
        "",
        "## Method",
        "",
        result["method"],
        "",
        "## Robust patterns",
        "",
        "| Pattern | Train n / median / alpha | Validation n / median / alpha / win-vs / stop |",
        "|---|---:|---:|",
    ]
    for row in robust[:20]:
        labels = [f"{key}={row[key]}" for key in ("Profit Pattern", "Revenue Bucket", "Price Bucket") if key in row]
        train = row["Train"]
        validation = row["Validation"]
        lines.append(
            f"| {' / '.join(labels)} | {train['Observations']} / {train['Median 1yr']}% / {train['Median 1yr Alpha']}% | "
            f"{validation['Observations']} / {validation['Median 1yr']}% / {validation['Median 1yr Alpha']}% / "
            f"{validation['Win vs Universe']}% / {validation['Stop 25 Hit']}% |"
        )
    lines.extend(["", "## Limitations", ""] + [f"- {item}" for item in result["limitations"]])
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"observations": len(observations), "robust_patterns": len(robust)}, ensure_ascii=False))
    for row in robust[:10]:
        print(json.dumps(row, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
