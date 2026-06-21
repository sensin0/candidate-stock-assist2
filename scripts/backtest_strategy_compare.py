import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "stocks.db"
REPORT_JSON = ROOT / "web-dashboard" / "public" / "strategy_backtest_results.json"
REPORT_MD = ROOT / "strategy_backtest_report.md"


def safe_pct(latest, previous):
    if latest is None or previous in (None, 0) or pd.isna(latest) or pd.isna(previous):
        return None
    return (latest - previous) / abs(previous) * 100


def loss_margin_improving(latest_income, previous_income, latest_revenue, previous_revenue):
    if latest_income is None or previous_income is None:
        return None
    if latest_revenue in (None, 0) or previous_revenue in (None, 0):
        return None
    if pd.isna(latest_income) or pd.isna(previous_income) or pd.isna(latest_revenue) or pd.isna(previous_revenue):
        return None
    return (latest_income / abs(latest_revenue)) > (previous_income / abs(previous_revenue))


def net_income_risk(latest_net_income, previous_net_income):
    if latest_net_income is None or previous_net_income is None:
        return None
    if pd.isna(latest_net_income) or pd.isna(previous_net_income):
        return None
    if previous_net_income >= 0 and latest_net_income < 0:
        return "profit_to_loss"
    if previous_net_income < 0 and latest_net_income < previous_net_income:
        return "deeper_loss"
    if latest_net_income < previous_net_income:
        return "profit_decline"
    return None


def price_location(prices, entry_date):
    two_years_prior = entry_date - timedelta(days=365 * 2)
    hist = prices[(prices.index >= two_years_prior) & (prices.index <= entry_date)]
    if hist.empty:
        return None, None
    loc_idx = prices.index.get_indexer([entry_date], method="pad")[0]
    if loc_idx < 0:
        return None, None
    buy_price = float(prices.iloc[loc_idx]["close"])
    min_price = float(hist["close"].min())
    max_price = float(hist["close"].max())
    if max_price == min_price:
        return 0.5, buy_price
    return (buy_price - min_price) / (max_price - min_price), buy_price


def future_returns(prices, entry_date, buy_price):
    future = prices[prices.index > entry_date]
    if future.empty or not buy_price:
        return None
    one_year = future[future.index <= entry_date + timedelta(days=365)]
    two_year = future[future.index <= entry_date + timedelta(days=365 * 2)]
    if one_year.empty:
        return None
    one_year_close = float(one_year.iloc[-1]["close"])
    max_two_year_high = float(two_year["high"].max()) if not two_year.empty else one_year_close
    return {
        "1yr Return": (one_year_close - buy_price) / buy_price,
        "2yr Max Return": (max_two_year_high - buy_price) / buy_price,
    }


def score_current(features):
    if not features["two_year_loss"]:
        return None
    score = 100
    price_loc = features["price_location"]
    rev_growth = features["revenue_growth"]
    loss_improving = features["loss_margin_improving"]
    loss_margin = features["loss_margin"]
    aggressive = features["aggressive"]
    sector_status = features["sector_status"]

    if price_loc is not None:
        if price_loc < 0.1:
            score += 25
        elif price_loc < 0.2:
            score += 10
        elif price_loc > 0.7:
            score -= 30
        elif price_loc > 0.5:
            score -= 15

    if sector_status == "Downturn":
        score += 20
    elif sector_status == "Boom":
        score -= 40

    if aggressive:
        score += 15

    if rev_growth is not None:
        if rev_growth >= 20:
            score += 50
        elif rev_growth >= 15:
            score += 70
        elif rev_growth >= 10:
            score += 35
        elif rev_growth >= 5:
            score += 5
        elif rev_growth < -10:
            score -= 60
        elif rev_growth < 0:
            score -= 20

    if loss_margin is not None:
        if loss_margin > -3:
            score += 30
        elif loss_margin > -5:
            score += 10

    if loss_improving is True:
        score += 50
    elif loss_improving is False:
        score -= 50

    if features["net_income_risk"] == "profit_to_loss":
        score -= 160
    elif features["net_income_risk"] == "deeper_loss":
        score -= 120
    elif features["net_income_risk"] == "profit_decline":
        score -= 60

    if rev_growth is not None and rev_growth >= 10:
        if loss_improving is True:
            score += 30
        if price_loc is not None and price_loc < 0.15:
            score += 20

    return score


def score_reversal(features):
    score = 0
    price_loc = features["price_location"]
    rev_growth = features["revenue_growth"]
    loss_improving = features["loss_margin_improving"]
    net_income = features["latest_net_income"]
    previous_net_income = features["previous_net_income"]
    aggressive = features["aggressive"]

    if features["two_year_loss"]:
        score += 70
    if net_income is not None and not pd.isna(net_income) and net_income < 0:
        score += 20
    if loss_improving is True:
        score += 70
    elif loss_improving is False:
        score -= 70

    if previous_net_income is not None and net_income is not None:
        net_loss_improving = loss_improving
        if net_loss_improving is True:
            score += 25
        elif net_loss_improving is False:
            score -= 30

    if features["net_income_risk"] == "profit_to_loss":
        score -= 160
    elif features["net_income_risk"] == "deeper_loss":
        score -= 120
    elif features["net_income_risk"] == "profit_decline":
        score -= 60

    if rev_growth is not None:
        if rev_growth >= 10:
            score += 35
        elif rev_growth >= 0:
            score += 15
        elif rev_growth < -10:
            score -= 60
        else:
            score -= 20

    if price_loc is not None:
        if price_loc < 0.15:
            score += 30
        elif price_loc < 0.3:
            score += 15
        elif price_loc > 0.7:
            score -= 30
        elif price_loc > 0.5:
            score -= 15

    if aggressive:
        score += 20

    return score


def bucket_price_location(value):
    if value is None:
        return "unknown"
    if value < 0.15:
        return "bottom 0-15%"
    if value < 0.3:
        return "low 15-30%"
    if value < 0.5:
        return "middle 30-50%"
    if value < 0.7:
        return "high 50-70%"
    return "top 70%+"


def bucket_revenue_growth(value):
    if value is None:
        return "unknown"
    if value >= 15:
        return "+15% or more"
    if value >= 10:
        return "+10% to +15%"
    if value >= 0:
        return "0% to +10%"
    if value >= -10:
        return "-10% to 0%"
    return "below -10%"


def compute_sector_status(latest_rows):
    sector_status = {}
    for sector, group in latest_rows.groupby("sector_name"):
        ratio = (pd.to_numeric(group["net_income"], errors="coerce").fillna(0) < 0).mean()
        if ratio >= 0.5:
            sector_status[sector] = "Downturn"
        elif ratio > 0.2:
            sector_status[sector] = "Mixed"
        else:
            sector_status[sector] = "Boom"
    return sector_status


def summarize(df, label):
    if df.empty:
        return {"Strategy": label, "Trades": 0}
    return {
        "Strategy": label,
        "Trades": int(len(df)),
        "Avg 1yr": round(df["1yr Return"].mean() * 100, 1),
        "Median 1yr": round(df["1yr Return"].median() * 100, 1),
        "Avg 2yr Max": round(df["2yr Max Return"].mean() * 100, 1),
        "Median 2yr Max": round(df["2yr Max Return"].median() * 100, 1),
        "Win 1yr > Universe": round((df["1yr Alpha"] > 0).mean() * 100, 1),
        "Doubler 2yr Max": round((df["2yr Max Return"] >= 1.0).mean() * 100, 1),
    }


def group_summary(df, group_col):
    if df.empty:
        return []
    grouped = []
    for key, group in df.groupby(group_col):
        if len(group) < 10:
            continue
        grouped.append(
            {
                group_col: str(key),
                "Trades": int(len(group)),
                "Avg 1yr": round(group["1yr Return"].mean() * 100, 1),
                "Win 1yr > Universe": round((group["1yr Alpha"] > 0).mean() * 100, 1),
                "Avg 2yr Max": round(group["2yr Max Return"].mean() * 100, 1),
                "Doubler 2yr Max": round((group["2yr Max Return"] >= 1.0).mean() * 100, 1),
            }
        )
    return sorted(grouped, key=lambda row: (row["Avg 1yr"], row["Trades"]), reverse=True)


def main():
    conn = sqlite3.connect(DB_PATH)
    tickers = pd.read_sql(
        "SELECT ticker, name, sector_name FROM tickers_master WHERE ticker IS NOT NULL AND ticker != ''",
        conn,
    )
    entry_dates = pd.date_range("2022-01-01", datetime.now() - timedelta(days=395), freq="MS")
    records = []
    benchmark_rows = []

    print(f"Backtesting {len(tickers)} tickers across {len(entry_dates)} monthly entry dates...")
    for entry_date in entry_dates:
        latest_fin_rows = []
        ticker_cache = {}
        benchmark_returns = []

        for _, ticker_row in tickers.iterrows():
            ticker = ticker_row["ticker"]
            fin = pd.read_sql(
                "SELECT * FROM financials WHERE ticker = ? ORDER BY period_end ASC",
                conn,
                params=(ticker,),
            )
            prices = pd.read_sql(
                "SELECT date, close, high FROM prices WHERE ticker = ? ORDER BY date ASC",
                conn,
                params=(ticker,),
            )
            if fin.empty or prices.empty:
                continue
            prices["date"] = pd.to_datetime(prices["date"])
            prices = prices.set_index("date")
            cutoff = entry_date - timedelta(days=60)
            available = fin[pd.to_datetime(fin["period_end"]) <= cutoff]
            if len(available) < 2:
                continue
            price_loc, buy_price = price_location(prices, entry_date)
            if buy_price is None:
                continue
            returns = future_returns(prices, entry_date, buy_price)
            if not returns:
                continue
            latest_fin_rows.append({**available.iloc[-1].to_dict(), "sector_name": ticker_row["sector_name"]})
            ticker_cache[ticker] = (ticker_row, available, prices, price_loc, buy_price, returns)
            benchmark_returns.append(returns["1yr Return"])

        if not benchmark_returns:
            continue
        universe_1yr = sum(benchmark_returns) / len(benchmark_returns)
        sector_status = compute_sector_status(pd.DataFrame(latest_fin_rows)) if latest_fin_rows else {}
        benchmark_rows.append(
            {
                "Entry Date": entry_date.strftime("%Y-%m-%d"),
                "Universe Count": len(benchmark_returns),
                "Universe Avg 1yr": universe_1yr,
            }
        )

        for ticker, (ticker_row, available, prices, price_loc, buy_price, returns) in ticker_cache.items():
            last_2 = available.iloc[-2:]
            latest = last_2.iloc[-1]
            previous = last_2.iloc[-2]
            latest_ni = latest.get("net_income")
            previous_ni = previous.get("net_income")
            latest_rev = latest.get("revenue")
            previous_rev = previous.get("revenue")
            rev_growth = safe_pct(latest_rev, previous_rev)
            loss_improving = loss_margin_improving(latest_ni, previous_ni, latest_rev, previous_rev)
            loss_margin = None
            if latest_ni is not None and latest_rev not in (None, 0) and not pd.isna(latest_ni) and not pd.isna(latest_rev):
                loss_margin = latest_ni / abs(latest_rev) * 100
            two_year_loss = (
                latest_ni is not None
                and previous_ni is not None
                and not pd.isna(latest_ni)
                and not pd.isna(previous_ni)
                and latest_ni < 0
                and previous_ni < 0
            )
            capex_latest = latest.get("capital_expenditure")
            capex_previous = previous.get("capital_expenditure")
            assets_latest = latest.get("tangible_assets")
            assets_previous = previous.get("tangible_assets")
            aggressive = False
            if capex_latest not in (None, 0) and capex_previous not in (None, 0) and not pd.isna(capex_latest) and not pd.isna(capex_previous):
                aggressive = aggressive or abs(capex_latest) > abs(capex_previous) * 1.05
            if assets_latest not in (None, 0) and assets_previous not in (None, 0) and not pd.isna(assets_latest) and not pd.isna(assets_previous):
                aggressive = aggressive or assets_latest > assets_previous * 1.02

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
            current_score = score_current(features)
            reversal_score = score_reversal(features)
            base = {
                "Ticker": ticker,
                "Name": ticker_row["name"],
                "Sector": ticker_row["sector_name"],
                "Entry Date": entry_date.strftime("%Y-%m-%d"),
                "Buy Price": buy_price,
                "Price Location": price_loc,
                "Price Bucket": bucket_price_location(price_loc),
                "Revenue Growth": rev_growth,
                "Revenue Bucket": bucket_revenue_growth(rev_growth),
                "Loss Margin Improving": loss_improving,
                "Loss Margin": loss_margin,
                "Two Year Loss": two_year_loss,
                "Aggressive": aggressive,
                "Universe 1yr": universe_1yr,
                **returns,
            }
            if current_score is not None and current_score >= 110:
                records.append({**base, "Strategy": "現行版", "Score": current_score})
            if reversal_score >= 110:
                records.append({**base, "Strategy": "反転狙い版", "Score": reversal_score})

    conn.close()
    all_trades = pd.DataFrame(records)
    if all_trades.empty:
        print("No trades found.")
        return
    all_trades["1yr Alpha"] = all_trades["1yr Return"] - all_trades["Universe 1yr"]
    all_trades["Score Band"] = pd.cut(
        all_trades["Score"],
        bins=[-999, 109, 149, 199, 999],
        labels=["<110", "110-149", "150-199", "200+"],
    ).astype(str)

    summary = [summarize(group, strategy) for strategy, group in all_trades.groupby("Strategy")]
    timing = {
        strategy: {
            "price_location": group_summary(group, "Price Bucket"),
            "revenue_growth": group_summary(group, "Revenue Bucket"),
            "loss_improving": group_summary(group, "Loss Margin Improving"),
            "score_band": group_summary(group, "Score Band"),
        }
        for strategy, group in all_trades.groupby("Strategy")
    }
    top_examples = (
        all_trades.sort_values(["Strategy", "1yr Alpha"], ascending=[True, False])
        .groupby("Strategy")
        .head(15)
        .to_dict(orient="records")
    )
    by_month = {}
    for (month, strategy), group in all_trades.groupby(["Entry Date", "Strategy"]):
        by_month.setdefault(month[:7], {})[strategy] = (
            group.sort_values("Score", ascending=False)
            .head(50)
            .to_dict(orient="records")
        )
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "assumptions": {
            "entry_dates": "Monthly starts from 2022-01 to the latest date with at least 1yr forward data.",
            "financial_availability": "Uses only financial periods ending at least 60 days before entry.",
            "outperformance": "1yr return above equal-weight universe average for the same entry month.",
        },
        "summary": summary,
        "timing": timing,
        "by_month": by_month,
        "top_examples": top_examples,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = ["# Strategy Backtest Report", "", f"Generated: {result['generated_at']}", ""]
    lines.append("## Summary")
    for row in summary:
        lines.append(
            f"- {row['Strategy']}: trades={row['Trades']}, avg 1yr={row.get('Avg 1yr')}%, "
            f"win vs universe={row.get('Win 1yr > Universe')}%, avg 2yr max={row.get('Avg 2yr Max')}%, "
            f"doubler={row.get('Doubler 2yr Max')}%"
        )
    lines.append("")
    lines.append("## Timing Highlights")
    for strategy, sections in timing.items():
        lines.append(f"### {strategy}")
        for section, rows in sections.items():
            if rows:
                best = rows[0]
                label_key = next(iter(best.keys()))
                lines.append(f"- {section}: best={best[label_key]} / trades={best['Trades']} / avg 1yr={best['Avg 1yr']}% / win vs universe={best['Win 1yr > Universe']}%")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT_JSON}")
    print(f"Wrote {REPORT_MD}")
    print(pd.DataFrame(summary).to_string(index=False))


if __name__ == "__main__":
    main()
