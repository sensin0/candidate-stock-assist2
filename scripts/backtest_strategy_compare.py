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
    exit_window = two_year if not two_year.empty else one_year
    final_exit_close = float(exit_window.iloc[-1]["close"])
    result = {
        "1yr Return": (one_year_close - buy_price) / buy_price,
        "2yr Max Return": (max_two_year_high - buy_price) / buy_price,
        "2yr Final Return": (final_exit_close - buy_price) / buy_price,
    }
    targets = {30: 1.3, 50: 1.5, 100: 2.0, 200: 3.0}
    stop_price = buy_price * 0.75
    stop_date = None
    if "low" in two_year:
        stop_rows = two_year[two_year["low"] <= stop_price]
        if not stop_rows.empty:
            stop_date = stop_rows.index[0]
    for pct, multiplier in targets.items():
        target_price = buy_price * multiplier
        hit_rows = two_year[two_year["high"] >= target_price]
        hit = not hit_rows.empty
        hit_date = hit_rows.index[0] if hit else None
        result[f"Target {pct} Hit"] = hit
        result[f"Days to Target {pct}"] = int((hit_date - entry_date).days) if hit else None
        result[f"Stop Before Target {pct}"] = bool(stop_date is not None and (not hit or stop_date < hit_date))
    result["Stop 25 Hit"] = stop_date is not None
    result["Rule 50 Return"] = exit_rule_return(two_year, buy_price, entry_date, 1.5, 0.75)
    result["Rule 100 Return"] = exit_rule_return(two_year, buy_price, entry_date, 2.0, 0.75)
    return result


def exit_rule_return(window, buy_price, entry_date, target_multiplier, stop_multiplier):
    if window.empty:
        return None
    target_price = buy_price * target_multiplier
    stop_price = buy_price * stop_multiplier
    target_rows = window[window["high"] >= target_price]
    stop_rows = window[window["low"] <= stop_price] if "low" in window else pd.DataFrame()
    target_date = target_rows.index[0] if not target_rows.empty else None
    stop_date = stop_rows.index[0] if not stop_rows.empty else None

    if stop_date is not None and (target_date is None or stop_date < target_date):
        return stop_multiplier - 1
    if target_date is not None:
        return target_multiplier - 1
    final_close = float(window.iloc[-1]["close"])
    return (final_close - buy_price) / buy_price


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
            score += 40
        elif price_loc < 0.2:
            score += 20
        elif price_loc > 0.7:
            score -= 100
        elif price_loc > 0.5:
            score -= 45

    if sector_status == "Downturn":
        score += 20
    elif sector_status == "Boom":
        score -= 40

    if aggressive:
        score += 15

    if rev_growth is not None:
        if rev_growth >= 20:
            score += 30
        elif rev_growth >= 15:
            score += 45
        elif rev_growth >= 10:
            score += 55
        elif rev_growth >= 5:
            score += 5
        elif rev_growth < -10:
            score -= 90
        elif rev_growth < 0:
            score -= 40

    if loss_margin is not None:
        if loss_margin > -3:
            score += 30
        elif loss_margin > -5:
            score += 10

    if loss_improving is True:
        score += 70
    elif loss_improving is False:
        score -= 80

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
        score += 60
    if net_income is not None and not pd.isna(net_income) and net_income < 0:
        score += 10
    if loss_improving is True:
        score += 55
    elif loss_improving is False:
        score -= 80

    if previous_net_income is not None and net_income is not None:
        net_loss_improving = loss_improving
        if net_loss_improving is True:
            score += 35
        elif net_loss_improving is False:
            score -= 90

    if features["net_income_risk"] == "profit_to_loss":
        score -= 160
    elif features["net_income_risk"] == "deeper_loss":
        score -= 120
    elif features["net_income_risk"] == "profit_decline":
        score -= 60

    if rev_growth is not None:
        if rev_growth >= 15:
            score += 30
        elif rev_growth >= 10:
            score += 55
        elif rev_growth >= 0:
            score += 10
        elif rev_growth < -10:
            score -= 90
        else:
            score -= 40

    if price_loc is not None:
        if price_loc < 0.15:
            score += 75
        elif price_loc < 0.3:
            score += 20
        elif price_loc > 0.7:
            score -= 100
        elif price_loc > 0.5:
            score -= 45

    if aggressive:
        score += 10

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
        "Target 30 Hit": round(df["Target 30 Hit"].mean() * 100, 1),
        "Target 50 Hit": round(df["Target 50 Hit"].mean() * 100, 1),
        "Target 100 Hit": round(df["Target 100 Hit"].mean() * 100, 1),
        "Target 200 Hit": round(df["Target 200 Hit"].mean() * 100, 1),
        "Median Days to 50": safe_median_days(df["Days to Target 50"]),
        "Median Days to 100": safe_median_days(df["Days to Target 100"]),
        "Stop Before 50": round(df["Stop Before Target 50"].mean() * 100, 1),
        "Stop Before 100": round(df["Stop Before Target 100"].mean() * 100, 1),
        "Win 1yr > 0": round((df["1yr Return"] > 0).mean() * 100, 1),
        "Rule 50 Avg": round(df["Rule 50 Return"].mean() * 100, 1),
        "Rule 50 Win": round((df["Rule 50 Return"] > 0).mean() * 100, 1),
        "Rule 100 Avg": round(df["Rule 100 Return"].mean() * 100, 1),
        "Rule 100 Win": round((df["Rule 100 Return"] > 0).mean() * 100, 1),
    }


def safe_median_days(series):
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return int(values.median())


def sanitize_json(value):
    if isinstance(value, dict):
        return {key: sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


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
                "Target 50 Hit": round(group["Target 50 Hit"].mean() * 100, 1),
                "Target 100 Hit": round(group["Target 100 Hit"].mean() * 100, 1),
                "Rule 50 Avg": round(group["Rule 50 Return"].mean() * 100, 1),
                "Rule 50 Win": round((group["Rule 50 Return"] > 0).mean() * 100, 1),
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
                "SELECT date, close, high, low FROM prices WHERE ticker = ? ORDER BY date ASC",
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
            "sell_rule": "Checks whether +30%, +50%, +100%, and +200% targets are hit within 2 years, and whether -25% stop is hit before each target.",
            "profit_rule": "Rule 50 Return exits at +50%, exits at -25% if the stop is hit first, otherwise marks at the final close within the 2-year window. Rule 100 uses +100% instead of +50%.",
        },
        "summary": summary,
        "timing": timing,
        "by_month": by_month,
        "top_examples": top_examples,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    result = sanitize_json(result)
    REPORT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")

    lines = ["# Strategy Backtest Report", "", f"Generated: {result['generated_at']}", ""]
    lines.append("## Summary")
    for row in summary:
        lines.append(
            f"- {row['Strategy']}: trades={row['Trades']}, avg 1yr={row.get('Avg 1yr')}%, "
            f"win vs universe={row.get('Win 1yr > Universe')}%, avg 2yr max={row.get('Avg 2yr Max')}%, "
            f"doubler={row.get('Doubler 2yr Max')}%, target50={row.get('Target 50 Hit')}%, "
            f"target100={row.get('Target 100 Hit')}%, median days to 50={row.get('Median Days to 50')}, "
            f"stop before 50={row.get('Stop Before 50')}%, rule50 avg={row.get('Rule 50 Avg')}%, "
            f"rule50 win={row.get('Rule 50 Win')}%, rule100 avg={row.get('Rule 100 Avg')}%"
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
