import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from backtest_strategy_compare import (
    DB_PATH,
    bucket_revenue_growth,
    compute_sector_status,
    future_returns,
    loss_margin_improving,
    net_income_risk,
    price_location,
    safe_pct,
    sanitize_json,
    score_current,
    summarize,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "earnings_entry_backtest_results.json"
REPORT_MD = ROOT / "earnings_entry_backtest_report.md"


def next_trading_date(prices, target_date):
    future = prices[prices.index >= target_date]
    if future.empty:
        return None
    return future.index[0]


def load_prices(conn, ticker):
    prices = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM prices WHERE ticker = ? ORDER BY date ASC",
        conn,
        params=(ticker,),
        parse_dates=["date"],
    )
    if prices.empty:
        return prices
    return prices.set_index("date")


def load_financials(conn, ticker):
    return pd.read_sql_query(
        "SELECT * FROM financials WHERE ticker = ? ORDER BY period_end ASC",
        conn,
        params=(ticker,),
        parse_dates=["period_end"],
    )


def summarize_by_revenue_bucket(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return []
    df["1yr Alpha"] = df["1yr Return"]
    result = []
    for bucket, group in df.groupby("Revenue Bucket", dropna=False):
        item = summarize(group, str(bucket))
        item["Revenue Bucket"] = str(bucket)
        del item["Strategy"]
        result.append(item)
    return sorted(result, key=lambda row: row.get("Rule 100 Avg") or -999, reverse=True)


def main():
    conn = sqlite3.connect(DB_PATH)
    tickers = pd.read_sql_query("SELECT ticker, name, sector_name FROM tickers_master ORDER BY ticker", conn)
    records = []

    for _, ticker_row in tickers.iterrows():
        ticker = ticker_row["ticker"]
        fin = load_financials(conn, ticker)
        if len(fin) < 2:
            continue
        prices = load_prices(conn, ticker)
        if prices.empty:
            continue

        for idx in range(1, len(fin)):
            latest = fin.iloc[idx]
            previous = fin.iloc[idx - 1]
            available_date = latest["period_end"] + timedelta(days=60)
            entry_date = next_trading_date(prices, available_date)
            if entry_date is None:
                continue
            price_loc, buy_price = price_location(prices, entry_date)
            if buy_price is None:
                continue
            returns = future_returns(prices, entry_date, buy_price)
            if not returns:
                continue

            latest_ni = latest.get("net_income")
            previous_ni = previous.get("net_income")
            latest_rev = latest.get("revenue")
            previous_rev = previous.get("revenue")
            rev_growth = safe_pct(latest_rev, previous_rev)
            loss_improving = loss_margin_improving(latest_ni, previous_ni, latest_rev, previous_rev)
            loss_margin = None
            if latest_ni is not None and latest_rev not in (None, 0) and not pd.isna(latest_ni) and not pd.isna(latest_rev):
                loss_margin = latest_ni / abs(latest_rev) * 100

            previous_period_rows = fin[fin["period_end"] <= latest["period_end"]]
            sector_status = compute_sector_status(previous_period_rows.assign(sector_name=ticker_row["sector_name"]))
            capex_latest = latest.get("capital_expenditure")
            capex_previous = previous.get("capital_expenditure")
            assets_latest = latest.get("tangible_assets")
            assets_previous = previous.get("tangible_assets")
            aggressive = False
            if capex_latest not in (None, 0) and capex_previous not in (None, 0) and not pd.isna(capex_latest) and not pd.isna(capex_previous):
                aggressive = aggressive or abs(capex_latest) > abs(capex_previous) * 1.05
            if assets_latest not in (None, 0) and assets_previous not in (None, 0) and not pd.isna(assets_latest) and not pd.isna(assets_previous):
                aggressive = aggressive or assets_latest > assets_previous * 1.02

            two_year_loss = (
                latest_ni is not None
                and previous_ni is not None
                and not pd.isna(latest_ni)
                and not pd.isna(previous_ni)
                and latest_ni < 0
                and previous_ni < 0
            )
            features = {
                "two_year_loss": two_year_loss,
                "price_location": price_loc,
                "revenue_growth": rev_growth,
                "loss_margin_improving": loss_improving,
                "loss_margin": loss_margin,
                "aggressive": aggressive,
                "sector_status": sector_status.get(ticker_row["sector_name"], "Unknown"),
                "latest_net_income": latest_ni,
                "previous_net_income": previous_ni,
                "net_income_risk": net_income_risk(latest_ni, previous_ni),
            }
            score = score_current(features)
            if score is None or score < 110:
                continue

            records.append(
                {
                    "Ticker": ticker,
                    "Name": ticker_row["name"],
                    "Sector": ticker_row["sector_name"],
                    "Period End": latest["period_end"].strftime("%Y-%m-%d"),
                    "Entry Date": entry_date.strftime("%Y-%m-%d"),
                    "Buy Price": buy_price,
                    "Score": score,
                    "Price Location": price_loc,
                    "Revenue Growth": rev_growth,
                    "Revenue Bucket": bucket_revenue_growth(rev_growth),
                    "Loss Margin Improving": loss_improving,
                    "Loss Margin": loss_margin,
                    "Two Year Loss": two_year_loss,
                    **returns,
                }
            )

    conn.close()
    records.sort(key=lambda row: (row["Entry Date"], -row["Score"]))
    df = pd.DataFrame(records)
    if not df.empty:
        df["1yr Alpha"] = df["1yr Return"]
    summary = summarize(df, "決算後エントリー") if not df.empty else {}
    by_revenue = summarize_by_revenue_bucket(records)
    top_examples = (
        df.sort_values("2yr Max Return", ascending=False)
        .head(20)
        .to_dict(orient="records")
        if not df.empty
        else []
    )
    result = sanitize_json(
        {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "assumptions": {
                "entry_timing": "Buy on the first trading day at or after period_end + 60 days. This approximates post-earnings availability because the DB has period_end, not announcement_date.",
                "filter": "Current-version score >= 110 using the shared ranking growth policy.",
            },
            "summary": summary,
            "by_revenue_bucket": by_revenue,
            "top_examples": top_examples,
            "records": records,
        }
    )
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")

    lines = [
        "# 決算後エントリー検証",
        "",
        f"Generated: {result['generated_at']}",
        "",
        "## 前提",
        "- 実際の決算発表日はDBにないため、`period_end + 60日` 以降の最初の営業日で買う近似検証。",
        "- 現行版スコア110以上だけを対象。",
        "",
        "## Summary",
    ]
    if summary:
        lines.append(
            f"- trades={summary['Trades']}, avg 1yr={summary.get('Avg 1yr')}%, "
            f"target100={summary.get('Target 100 Hit')}%, rule100 avg={summary.get('Rule 100 Avg')}%, "
            f"stop before 100={summary.get('Stop Before 100')}%"
        )
    else:
        lines.append("- 対象なし")
    lines.extend(["", "## 売上成長率帯別"])
    for row in by_revenue:
        lines.append(
            f"- {row['Revenue Bucket']}: trades={row['Trades']}, avg 1yr={row.get('Avg 1yr')}%, "
            f"target100={row.get('Target 100 Hit')}%, rule100 avg={row.get('Rule 100 Avg')}%"
        )
    lines.extend(["", "## 上昇率上位例"])
    for row in top_examples[:10]:
        lines.append(
            f"- {row['Entry Date']} {row['Ticker']} {row['Name']} | 売上 {row.get('Revenue Growth'):.1f}% | "
            f"Score {row.get('Score'):.1f} | 2年内最大 {row.get('2yr Max Return') * 100:.1f}% | "
            f"2倍 {'達成' if row.get('Target 100 Hit') else '未達'}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT_JSON}")
    print(f"Wrote {REPORT_MD}")
    if summary:
        print(summary)


if __name__ == "__main__":
    main()
