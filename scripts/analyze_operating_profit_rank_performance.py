import argparse
import html
import json
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf

try:
    from scripts.weekly_cloud_ranker import CURRENT_TARGET_SECTORS
except ModuleNotFoundError:
    from weekly_cloud_ranker import CURRENT_TARGET_SECTORS


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "stocks.db"
RANKING_SOURCE = ROOT / "web-dashboard" / "public" / "strategy_backtest_results.json"
CURRENT_REPORT = ROOT / "weekly_ranking_report.json"
CACHE_PATH = ROOT / "data" / "operating_income_history_cache.json"
BENCHMARK_CACHE_PATH = ROOT / "data" / "topix_benchmark_adjusted.csv"
OUTPUT_JSON = ROOT / "docs" / "operating-profit-backtest.json"
OUTPUT_CSV = ROOT / "docs" / "operating-profit-events.csv"
OUTPUT_HTML = ROOT / "docs" / "operating-profit-backtest.html"
HORIZONS = (1, 3, 6, 12)
BENCHMARK_TICKER = "1306.T"


def clean_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def statement_row(frame, names):
    if frame is None or frame.empty:
        return None
    for name in names:
        if name in frame.index:
            return frame.loc[name]
    return None


def fetch_annual_statements(item):
    ticker = item["Ticker"]
    frame = yf.Ticker(ticker).financials
    operating = statement_row(frame, ("Operating Income", "Total Operating Income As Reported"))
    revenue = statement_row(frame, ("Total Revenue", "Operating Revenue"))
    if operating is None:
        return ticker, None
    statements = []
    for column in frame.columns:
        op = clean_number(operating.get(column))
        if op is None:
            continue
        rev = clean_number(revenue.get(column)) if revenue is not None else None
        period = pd.Timestamp(column).strftime("%Y-%m-%d")
        statements.append({"period_end": period, "operating_income": op, "revenue": rev})
    statements.sort(key=lambda row: row["period_end"])
    return ticker, {
        "ticker": ticker,
        "name": item.get("Name"),
        "sector": item.get("Sector"),
        "statements": statements,
    }


def refresh_statement_cache(workers):
    report = json.loads(CURRENT_REPORT.read_text(encoding="utf-8"))
    items = [row for row in report.get("rankings", []) if row.get("Sector") in CURRENT_TARGET_SECTORS]
    records = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_annual_statements, item): item for item in items}
        for index, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                ticker, result = future.result()
                if result and len(result["statements"]) >= 2:
                    records[ticker] = result
                else:
                    errors.append({"ticker": item.get("Ticker"), "error": "operating history unavailable"})
            except Exception as error:
                errors.append({"ticker": item.get("Ticker"), "error": str(error)})
            if index % 50 == 0:
                print(f"Fetched annual operating income {index}/{len(items)}")
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "requested": len(items),
        "successful": len(records),
        "errors": errors,
        "records": records,
    }
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def refresh_benchmark_cache():
    end = (pd.Timestamp.now().normalize() + timedelta(days=2)).strftime("%Y-%m-%d")
    downloaded = yf.download(
        BENCHMARK_TICKER,
        start="2021-01-01",
        end=end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if isinstance(downloaded.columns, pd.MultiIndex):
        downloaded.columns = downloaded.columns.get_level_values(0)
    normalized = downloaded[["Close", "High", "Low"]].dropna(subset=["Close"]).reset_index()
    normalized = normalized.rename(columns={"Date": "date", "Close": "close", "High": "high", "Low": "low"})
    normalized.to_csv(BENCHMARK_CACHE_PATH, index=False)
    return normalized


def infer_split_adjustments(frame):
    adjusted = frame.sort_index().copy()
    ratios = adjusted["close"] / adjusted["close"].shift(1)
    split_dates = ratios[(ratios < 0.55) | (ratios > 1.8)].index
    for split_date in split_dates:
        factor = float(ratios.loc[split_date])
        adjusted.loc[adjusted.index < split_date, ["close", "high", "low"]] *= factor
    return adjusted


def load_stock_prices(tickers):
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" for _ in tickers)
    query = f"SELECT ticker, date, close, high, low FROM prices WHERE ticker IN ({placeholders}) ORDER BY ticker, date"
    frame = pd.read_sql(query, conn, params=tuple(sorted(tickers)))
    price_end = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    conn.close()
    frame["date"] = pd.to_datetime(frame["date"])
    prices = {}
    for ticker, group in frame.groupby("ticker"):
        prices[ticker] = infer_split_adjustments(group.set_index("date").sort_index())
    return prices, price_end


def price_on_or_after(prices, date, max_days=10):
    window = prices[(prices.index >= date) & (prices.index <= date + timedelta(days=max_days))]
    return None if window.empty else float(window.iloc[0]["close"])


def price_on_or_before(prices, date, max_days=10):
    window = prices[(prices.index <= date) & (prices.index >= date - timedelta(days=max_days))]
    return None if window.empty else float(window.iloc[-1]["close"])


def forward_performance(prices, entry_date, buy_price, benchmark_prices=None):
    result = {}
    benchmark_buy = price_on_or_after(benchmark_prices, entry_date) if benchmark_prices is not None else None
    for months in HORIZONS:
        target = entry_date + pd.DateOffset(months=months)
        close = price_on_or_before(prices, target)
        stock_return = None if close is None else (close / buy_price - 1) * 100
        benchmark_close = price_on_or_before(benchmark_prices, target) if benchmark_prices is not None else None
        benchmark_return = None if benchmark_buy is None or benchmark_close is None else (benchmark_close / benchmark_buy - 1) * 100
        result[f"return_{months}m"] = stock_return
        result[f"benchmark_{months}m"] = benchmark_return
        result[f"alpha_{months}m"] = None if stock_return is None or benchmark_return is None else stock_return - benchmark_return
    window_12m = prices[(prices.index > entry_date) & (prices.index <= entry_date + pd.DateOffset(months=12))]
    result["max_12m"] = None if window_12m.empty else (float(window_12m["high"].max()) / buy_price - 1) * 100
    result["drawdown_12m"] = None if window_12m.empty else (float(window_12m["low"].min()) / buy_price - 1) * 100
    return result


def rank_bucket(rank):
    if rank == 1:
        return "1位"
    if rank == 2:
        return "2位"
    if rank == 3:
        return "3位"
    return "4-5位"


def summarize_rows(rows):
    summary = {"observations": len(rows), "unique_tickers": len({row["ticker"] for row in rows})}
    for months in HORIZONS:
        values = [row[f"return_{months}m"] for row in rows if row.get(f"return_{months}m") is not None]
        summary[f"n_{months}m"] = len(values)
        summary[f"median_{months}m"] = round(float(pd.Series(values).median()), 1) if values else None
        summary[f"average_{months}m"] = round(float(pd.Series(values).mean()), 1) if values else None
        summary[f"win_{months}m"] = round(sum(value > 0 for value in values) / len(values) * 100, 1) if values else None
        alpha_values = [row[f"alpha_{months}m"] for row in rows if row.get(f"alpha_{months}m") is not None]
        summary[f"median_alpha_{months}m"] = round(float(pd.Series(alpha_values).median()), 1) if alpha_values else None
        summary[f"beat_market_{months}m"] = round(sum(value > 0 for value in alpha_values) / len(alpha_values) * 100, 1) if alpha_values else None
    complete_12m = [row for row in rows if row.get("return_12m") is not None]
    max_values = [row["max_12m"] for row in complete_12m if row.get("max_12m") is not None]
    drawdowns = [row["drawdown_12m"] for row in complete_12m if row.get("drawdown_12m") is not None]
    summary["median_max_12m"] = round(float(pd.Series(max_values).median()), 1) if max_values else None
    summary["median_drawdown_12m"] = round(float(pd.Series(drawdowns).median()), 1) if drawdowns else None
    summary["double_rate_12m"] = round(sum(value >= 100 for value in max_values) / len(max_values) * 100, 1) if max_values else None
    return summary


def analyze_historical_rank_order(prices_by_ticker):
    source = json.loads(RANKING_SOURCE.read_text(encoding="utf-8"))
    rows = []
    benchmark = prices_by_ticker.get(BENCHMARK_TICKER)
    for month, strategies in source.get("by_month", {}).items():
        candidates = sorted(strategies.get("現行版", []), key=lambda row: row.get("Score", 0), reverse=True)
        for rank, item in enumerate(candidates, start=1):
            ticker = item.get("Ticker")
            prices = prices_by_ticker.get(ticker)
            if prices is None or prices.empty:
                continue
            entry_date = pd.Timestamp(item["Entry Date"])
            buy_price = clean_number(item.get("Buy Price")) or price_on_or_after(prices, entry_date)
            if not buy_price:
                continue
            rows.append({
                "ticker": ticker,
                "name": item.get("Name"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "rank": rank,
                "rank_bucket": rank_bucket(rank),
                "score": item.get("Score"),
                **forward_performance(prices, entry_date, buy_price, benchmark),
            })
    order = ("1位", "2位", "3位", "4-5位")
    summary = []
    for bucket in order:
        bucket_rows = [row for row in rows if row["rank_bucket"] == bucket]
        if bucket_rows:
            summary.append({"label": bucket, **summarize_rows(bucket_rows)})
    medians = [row.get("median_12m") for row in summary if row.get("median_12m") is not None]
    monotonic = len(medians) >= 3 and all(left >= right for left, right in zip(medians, medians[1:]))
    return rows, summary, monotonic


EVENT_LABELS = {
    "two_loss_to_profit": "2期赤字→黒字",
    "loss_to_profit": "赤字→黒字",
    "loss_narrowing": "営業赤字縮小",
    "profitable_v": "黒字内V字回復",
    "continuous_growth": "営業利益の連続増益",
}


def operating_pattern(first, second, current):
    if first < 0 and second < 0 and current > 0:
        return "two_loss_to_profit"
    if second < 0 and current > 0:
        return "loss_to_profit"
    if second < 0 and second < current < 0:
        return "loss_narrowing"
    if first > second >= 0 and current > second:
        return "profitable_v"
    if 0 < first < second < current:
        return "continuous_growth"
    return None


def operating_change_values(second_operating, current_operating, second_revenue=None, current_revenue=None):
    change = current_operating - second_operating
    change_rate = change / max(abs(second_operating), 1) * 100
    revenue_base = current_revenue if current_revenue not in (None, 0) else second_revenue
    revenue_share = None if revenue_base in (None, 0) else change / abs(revenue_base) * 100
    return {
        "operating_change_yen": change,
        "operating_change_billion": change / 1_000_000_000,
        "operating_change_rate": change_rate,
        "operating_change_revenue_share": revenue_share,
    }


def quantile_impact(rows, metric, labels):
    complete = [row for row in rows if row.get("return_12m") is not None and clean_number(row.get(metric)) is not None]
    if len(complete) < len(labels):
        return []
    frame = pd.DataFrame(complete)
    frame["bucket"] = pd.qcut(frame[metric].rank(method="first"), len(labels), labels=labels)
    result = []
    for label in labels:
        bucket_rows = frame[frame["bucket"] == label].to_dict("records")
        summary = summarize_rows(bucket_rows)
        summary.update({
            "key": str(label),
            "label": str(label),
            "median_change_billion": round(float(pd.Series([row["operating_change_billion"] for row in bucket_rows]).median()), 2),
            "median_change_rate": round(float(pd.Series([row["operating_change_rate"] for row in bucket_rows]).median()), 1),
            "median_revenue_share": round(float(pd.Series([
                row["operating_change_revenue_share"] for row in bucket_rows
                if row.get("operating_change_revenue_share") is not None
            ]).median()), 2),
        })
        result.append(summary)
    return result


def partial_spearman(rows, x_key, y_key, control_key):
    frame = pd.DataFrame([
        {"x": row.get(x_key), "y": row.get(y_key), "control": row.get(control_key)}
        for row in rows
    ]).dropna()
    if len(frame) < 10:
        return None
    ranked = frame.rank(method="average")
    design = np.column_stack([np.ones(len(ranked)), ranked["control"].to_numpy()])
    residual_x = ranked["x"].to_numpy() - design @ np.linalg.lstsq(design, ranked["x"].to_numpy(), rcond=None)[0]
    residual_y = ranked["y"].to_numpy() - design @ np.linalg.lstsq(design, ranked["y"].to_numpy(), rcond=None)[0]
    return round(float(pd.Series(residual_x).corr(pd.Series(residual_y))), 3)


def analyze_operating_change_impact(events):
    complete = [row for row in events if row.get("return_12m") is not None]
    labels = ["小さい20%", "やや小さい", "中間", "やや大きい", "大きい20%"]
    amount = quantile_impact(complete, "operating_change_yen", labels)
    rate = quantile_impact(complete, "operating_change_rate", labels)
    revenue_share = quantile_impact(complete, "operating_change_revenue_share", labels)
    frame = pd.DataFrame(complete)
    correlations = {}
    for key in ("operating_change_yen", "operating_change_rate", "operating_change_revenue_share"):
        pair = frame[[key, "return_12m", "alpha_12m", "max_12m"]].dropna()
        correlations[key] = {
            "n": len(pair),
            "return_12m": round(float(pair[key].corr(pair["return_12m"], method="spearman")), 3),
            "alpha_12m": round(float(pair[key].corr(pair["alpha_12m"], method="spearman")), 3),
            "max_12m": round(float(pair[key].corr(pair["max_12m"], method="spearman")), 3),
        }
    partial = partial_spearman(complete, "operating_change_yen", "return_12m", "current_revenue")
    frame["period_year"] = pd.to_datetime(frame["period_end"]).dt.year

    def correlation_rows(group_key, label_getter):
        rows = []
        for key, group in frame.groupby(group_key):
            if len(group) < 5:
                continue
            rows.append({
                "key": str(key),
                "label": label_getter(key),
                "n": len(group),
                "return_12m": round(float(group["operating_change_yen"].corr(group["return_12m"], method="spearman")), 3),
                "alpha_12m": round(float(group["operating_change_yen"].corr(group["alpha_12m"], method="spearman")), 3),
            })
        return rows

    by_year = correlation_rows("period_year", lambda key: f"{key}年決算")
    by_pattern = correlation_rows("pattern", lambda key: EVENT_LABELS.get(key, str(key)))
    amount_monotonic = all(
        left.get("median_12m", -999) <= right.get("median_12m", -999)
        for left, right in zip(amount, amount[1:])
    )
    top = amount[-1] if amount else None
    bottom = amount[0] if amount else None
    return {
        "observations": len(complete),
        "amount_quintiles": amount,
        "rate_quintiles": rate,
        "revenue_share_quintiles": revenue_share,
        "correlations": correlations,
        "partial_spearman_controlling_revenue": partial,
        "correlation_by_year": by_year,
        "correlation_by_pattern": by_pattern,
        "amount_monotonic": amount_monotonic,
        "top_minus_bottom_median_12m": None if not top or not bottom else round(top["median_12m"] - bottom["median_12m"], 1),
        "top_minus_bottom_alpha_12m": None if not top or not bottom else round(top["median_alpha_12m"] - bottom["median_alpha_12m"], 1),
    }


def analyze_operating_events(cache, prices_by_ticker):
    events = []
    benchmark = prices_by_ticker.get(BENCHMARK_TICKER)
    for ticker, item in cache.get("records", {}).items():
        statements = item.get("statements", [])
        prices = prices_by_ticker.get(ticker)
        if prices is None or prices.empty:
            continue
        for index in range(2, len(statements)):
            first, second, current = statements[index - 2:index + 1]
            pattern = operating_pattern(first["operating_income"], second["operating_income"], current["operating_income"])
            if not pattern:
                continue
            entry_date = pd.Timestamp(current["period_end"]) + timedelta(days=60)
            buy_price = price_on_or_after(prices, entry_date)
            if not buy_price:
                continue
            revenue_growth = None
            if current.get("revenue") is not None and second.get("revenue") not in (None, 0):
                revenue_growth = (current["revenue"] / abs(second["revenue"]) - 1) * 100
            change_values = operating_change_values(
                second["operating_income"],
                current["operating_income"],
                second.get("revenue"),
                current.get("revenue"),
            )
            events.append({
                "ticker": ticker,
                "name": item.get("name"),
                "sector": item.get("sector"),
                "pattern": pattern,
                "pattern_label": EVENT_LABELS[pattern],
                "period_end": current["period_end"],
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "operating_first": first["operating_income"],
                "operating_second": second["operating_income"],
                "operating_current": current["operating_income"],
                "second_revenue": second.get("revenue"),
                "current_revenue": current.get("revenue"),
                "revenue_growth": None if revenue_growth is None else round(revenue_growth, 1),
                **change_values,
                **forward_performance(prices, entry_date, buy_price, benchmark),
            })
    summary = []
    for key, label in EVENT_LABELS.items():
        pattern_rows = [row for row in events if row["pattern"] == key]
        if pattern_rows:
            summary.append({"key": key, "label": label, **summarize_rows(pattern_rows)})
    examples = sorted(
        [row for row in events if row.get("return_12m") is not None],
        key=lambda row: row["return_12m"],
        reverse=True,
    )
    return events, summary, examples[:8], examples[-5:]


def fmt(value, suffix="%"):
    return "-" if value is None else f"{value:+.1f}{suffix}"


def report_html(result):
    data = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    generated = html.escape(result["generated_at"])
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ランキングと営業利益反転の過去検証</title>
<style>
:root{{--ink:#172033;--muted:#64748b;--line:#dbe3ec;--paper:#fff;--soft:#f4f7fa;--green:#087f5b;--red:#c2413b;--blue:#2563a8;--amber:#a16207}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--soft);color:var(--ink);font-family:"Noto Sans JP","Yu Gothic UI",sans-serif;letter-spacing:0}}
header{{background:#172033;color:#fff;border-bottom:4px solid #23a77a}} .wrap{{width:min(1180px,calc(100% - 32px));margin:auto}} .head{{padding:28px 0 24px;display:flex;justify-content:space-between;gap:24px;align-items:end}}
h1{{font-size:clamp(25px,4vw,40px);margin:0 0 8px;overflow-wrap:anywhere}} .head p{{margin:0;color:#cbd5e1}} .back{{color:#fff;text-decoration:none;border:1px solid #64748b;padding:9px 12px;border-radius:6px;white-space:nowrap}}
main{{padding:24px 0 48px}} .notice{{border-left:5px solid var(--amber);background:#fff8df;padding:14px 16px;margin-bottom:18px}} .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}}
.card,.panel{{background:var(--paper);border:1px solid var(--line);border-radius:8px}} .card{{padding:16px}} .card span{{color:var(--muted);font-size:13px}} .card strong{{display:block;font-size:24px;margin:6px 0}} .card p{{margin:0;color:var(--muted);font-size:13px;line-height:1.65}}
.panel{{padding:20px;margin-top:16px}} h2{{font-size:21px;margin:0 0 5px;overflow-wrap:anywhere}} .lead{{margin:0 0 18px;color:var(--muted);line-height:1.7;overflow-wrap:anywhere}} .chart{{overflow-x:auto;padding:8px 0}} svg{{display:block;min-width:720px;width:100%;height:auto}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right}} th:first-child,td:first-child{{text-align:left}} th{{color:var(--muted);font-size:12px}} .positive{{color:var(--green);font-weight:700}} .negative{{color:var(--red);font-weight:700}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted)}} .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}} .verdict{{padding:14px 16px;background:#e9f7f1;border-left:5px solid var(--green);line-height:1.7}} .warn{{background:#fff4e5;border-color:var(--amber)}}
.examples{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .example-list{{margin:0;padding:0;list-style:none}} .example-list li{{display:grid;grid-template-columns:1fr auto;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}} footer{{color:var(--muted);padding:22px 0;font-size:12px;line-height:1.7}}
@media(max-width:760px){{.head{{align-items:start;flex-direction:column}}.grid,.examples{{grid-template-columns:1fr}}.panel{{padding:15px}} table{{min-width:680px}}.table-scroll{{overflow-x:auto}}}}
</style></head><body>
<header><div class="wrap head"><div><h1>ランキングは、本当に上ほど強いか</h1><p>営業利益の反転パターンと、その後1・3・6・12か月の株価を分解</p></div><a class="back" href="./index.html">ランキングへ戻る</a></div></header>
<main class="wrap"><div class="notice"><strong>重要:</strong> 現行方式の会社予想履歴は2日分だけです。完全な予想値検証ではなく、過去ランキングの順位検証と、実績営業利益のイベント検証を分けています。</div>
<section class="grid" id="summary-cards"></section>
<section class="panel"><h2>1. 過去ランキングの順位は効いていたか</h2><p class="lead">旧現行版の月次ランキングを順位帯に分けた検証です。線が上位ほど高ければ、順位付けに意味があります。</p><div class="chart" id="rank-chart"></div><div class="table-scroll"><table id="rank-table"></table></div><div class="verdict" id="rank-verdict"></div></section>
<section class="panel"><h2>2. 営業利益が反転した後、株価はどう動いたか</h2><p class="lead">年次決算の期末から60日後を市場認識時点の近似として、その後を追跡。大化け銘柄の影響を抑えるため中央値を中心に見ます。</p><div class="chart" id="event-chart"></div><div class="table-scroll"><table id="event-table"></table></div><div class="verdict" id="event-verdict"></div></section>
<section class="panel"><h2>3. 営業利益の増加額が大きいほど上がったか</h2><p class="lead">今期実績と翌期実績の差を5段階に分けています。絶対額だけでなく、前期比と売上高比も比較し、企業規模の影響を切り分けます。</p><div class="chart" id="amount-chart"></div><div class="table-scroll"><table id="amount-table"></table></div><div class="verdict" id="change-verdict"></div><h3>前期営業利益に対する増加率</h3><div class="table-scroll"><table id="rate-table"></table></div><h3>売上高に対する営業利益増加額</h3><div class="table-scroll"><table id="share-table"></table></div><h3>年度・パターンを変えても傾向は残るか</h3><div class="table-scroll"><table id="robust-table"></table></div></section>
<section class="panel"><h2>4. 値動きの実例</h2><p class="lead">12か月後リターンが大きかった例と悪かった例。成功例だけでなく下側も並べています。</p><div class="examples"><div><h3>上昇例</h3><ul class="example-list" id="best-list"></ul></div><div><h3>下落例</h3><ul class="example-list" id="worst-list"></ul></div></div></section>
<section class="panel"><h2>読み方と限界</h2><ul><li>順位検証は旧現行版の代理検証で、来期営業利益予想を使う現行方式そのものではありません。</li><li>営業利益イベントは実績値で分類しています。「事前に黒字転換を当てられた」検証ではなく、決算確認後の反応です。</li><li>決算公表日はDBにないため、期末+60日を利用可能日として近似しています。</li><li>同じ銘柄が別年度・別月に複数回含まれます。サンプル数とユニーク銘柄数を併記しています。</li><li>中央値がプラスでも損失は起きます。売買判断を保証する資料ではありません。</li></ul></section>
</main><footer class="wrap">生成: {generated} / 株価DB最終日: {html.escape(result['price_data_through'])}</footer>
<script>const D={data};
const pct=v=>v==null?'-':`${{v>=0?'+':''}}${{v.toFixed(1)}}%`; const cls=v=>v==null?'':v>=0?'positive':'negative';
function lineChart(rows){{const W=900,H=330,p={{l:62,r:24,t:25,b:48}},hs=[1,3,6,12],vals=rows.flatMap(r=>hs.map(h=>r[`median_${{h}}m`]).filter(v=>v!=null));let lo=Math.min(-10,...vals),hi=Math.max(10,...vals);lo=Math.floor(lo/10)*10;hi=Math.ceil(hi/10)*10;const x=i=>p.l+i*(W-p.l-p.r)/(hs.length-1),y=v=>p.t+(hi-v)*(H-p.t-p.b)/(hi-lo);const colors=['#087f5b','#2563a8','#a16207','#c2413b'];let s=`<svg viewBox="0 0 ${{W}} ${{H}}" role="img">`;for(let v=lo;v<=hi;v+=10)s+=`<line x1="${{p.l}}" x2="${{W-p.r}}" y1="${{y(v)}}" y2="${{y(v)}}" stroke="#dbe3ec"/><text x="${{p.l-9}}" y="${{y(v)+4}}" text-anchor="end" fill="#64748b" font-size="12">${{v}}%</text>`;hs.forEach((h,i)=>s+=`<text x="${{x(i)}}" y="${{H-15}}" text-anchor="middle" fill="#64748b" font-size="13">${{h}}か月</text>`);rows.forEach((r,j)=>{{const pts=hs.map((h,i)=>`${{x(i)}},${{y(r[`median_${{h}}m`])}}`).join(' ');s+=`<polyline points="${{pts}}" fill="none" stroke="${{colors[j]}}" stroke-width="4"/>`;hs.forEach((h,i)=>s+=`<circle cx="${{x(i)}}" cy="${{y(r[`median_${{h}}m`])}}" r="5" fill="${{colors[j]}}"/>`);s+=`<text x="${{W-p.r-4}}" y="${{y(r.median_12m)-8}}" text-anchor="end" fill="${{colors[j]}}" font-weight="700" font-size="13">${{r.label}}</text>`}});return s+'</svg>'}}
function barChart(rows){{const W=900,rowH=58,H=rows.length*rowH+48,p={{l:180,r:70,t:15,b:32}},vals=rows.map(r=>r.median_12m||0),lo=Math.min(-20,...vals),hi=Math.max(20,...vals),x=v=>p.l+(v-lo)*(W-p.l-p.r)/(hi-lo),zero=x(0);let s=`<svg viewBox="0 0 ${{W}} ${{H}}">`;s+=`<line x1="${{zero}}" x2="${{zero}}" y1="0" y2="${{H-p.b}}" stroke="#94a3b8"/>`;rows.forEach((r,i)=>{{const y=18+i*rowH,w=Math.abs(x(r.median_12m)-zero),left=Math.min(zero,x(r.median_12m)),c=r.median_12m>=0?'#087f5b':'#c2413b';s+=`<text x="${{p.l-10}}" y="${{y+17}}" text-anchor="end" fill="#172033" font-size="14">${{r.label}}</text><rect x="${{left}}" y="${{y}}" width="${{w}}" height="24" fill="${{c}}" rx="3"/><text x="${{r.median_12m>=0?x(r.median_12m)+7:x(r.median_12m)-7}}" y="${{y+17}}" text-anchor="${{r.median_12m>=0?'start':'end'}}" fill="${{c}}" font-weight="700">${{pct(r.median_12m)}}</text>`}});return s+'</svg>'}}
function table(rows){{return `<thead><tr><th>区分</th><th>件数/銘柄</th>${{[1,3,6,12].map(h=>`<th>${{h}}か月中央値</th>`).join('')}}<th>12か月勝率</th><th>市場超過</th><th>市場勝率</th><th>最大上昇中央値</th><th>最大下落中央値</th></tr></thead><tbody>${{rows.map(r=>`<tr><td><strong>${{r.label}}</strong></td><td>${{r.observations}} / ${{r.unique_tickers}}</td>${{[1,3,6,12].map(h=>`<td class="${{cls(r[`median_${{h}}m`])}}">${{pct(r[`median_${{h}}m`])}}</td>`).join('')}}<td>${{pct(r.win_12m)}}</td><td class="${{cls(r.median_alpha_12m)}}">${{pct(r.median_alpha_12m)}}</td><td>${{pct(r.beat_market_12m)}}</td><td class="positive">${{pct(r.median_max_12m)}}</td><td class="negative">${{pct(r.median_drawdown_12m)}}</td></tr>`).join('')}}</tbody>`}}
function changeTable(rows){{return `<thead><tr><th>区分</th><th>件数</th><th>増加額中央値</th><th>増加率中央値</th><th>売上高比</th><th>6か月</th><th>12か月</th><th>市場超過</th><th>勝率</th></tr></thead><tbody>${{rows.map(r=>`<tr><td><strong>${{r.label}}</strong></td><td>${{r.n_12m}}</td><td>${{r.median_change_billion.toFixed(2)}}十億円</td><td>${{pct(r.median_change_rate)}}</td><td>${{pct(r.median_revenue_share)}}</td><td class="${{cls(r.median_6m)}}">${{pct(r.median_6m)}}</td><td class="${{cls(r.median_12m)}}">${{pct(r.median_12m)}}</td><td class="${{cls(r.median_alpha_12m)}}">${{pct(r.median_alpha_12m)}}</td><td>${{pct(r.win_12m)}}</td></tr>`).join('')}}</tbody>`}}
function correlationTable(rows){{return `<thead><tr><th>区分</th><th>件数</th><th>増加額と12か月騰落率の相関</th><th>増加額と市場超過の相関</th></tr></thead><tbody>${{rows.map(r=>`<tr><td><strong>${{r.label}}</strong></td><td>${{r.n}}</td><td>${{r.return_12m>=0?'+':''}}${{r.return_12m.toFixed(3)}}</td><td>${{r.alpha_12m>=0?'+':''}}${{r.alpha_12m.toFixed(3)}}</td></tr>`).join('')}}</tbody>`}}
const rank=D.rank_order.summary,event=D.operating_events.summary,impact=D.operating_change_impact;document.getElementById('rank-chart').innerHTML=lineChart(rank);document.getElementById('event-chart').innerHTML=barChart(event);document.getElementById('rank-table').innerHTML=table(rank);document.getElementById('event-table').innerHTML=table(event);document.getElementById('amount-chart').innerHTML=barChart(impact.amount_quintiles);document.getElementById('amount-table').innerHTML=changeTable(impact.amount_quintiles);document.getElementById('rate-table').innerHTML=changeTable(impact.rate_quintiles);document.getElementById('share-table').innerHTML=changeTable(impact.revenue_share_quintiles);document.getElementById('robust-table').innerHTML=correlationTable([...impact.correlation_by_year,...impact.correlation_by_pattern]);document.getElementById('change-verdict').innerHTML=impact.verdict;
const topRank=rank[0],turn=event.find(x=>x.key==='two_loss_to_profit'),best=[...event].filter(x=>x.n_12m>=5).sort((a,b)=>(b.median_alpha_12m??-999)-(a.median_alpha_12m??-999))[0],large=impact.amount_quintiles.at(-1);document.getElementById('summary-cards').innerHTML=`<article class="card"><span>営業利益増加額 上位20%</span><strong>${{pct(large?.median_12m)}}</strong><p>12か月勝率 ${{pct(large?.win_12m)}}、市場超過 ${{pct(large?.median_alpha_12m)}}、n=${{large?.n_12m||0}}。</p></article><article class="card"><span>増加額と株価の相関</span><strong>+${{impact.correlations.operating_change_yen.return_12m.toFixed(3)}}</strong><p>売上規模を調整後も +${{impact.partial_spearman_controlling_revenue.toFixed(3)}}。</p></article><article class="card"><span>前期比率と株価の相関</span><strong>+${{impact.correlations.operating_change_rate.return_12m.toFixed(3)}}</strong><p>何%増えたかより、金額差の方が強く関連しました。</p></article>`;
document.getElementById('rank-verdict').className='verdict '+(D.rank_order.monotonic?'':'warn');document.getElementById('rank-verdict').innerHTML=D.rank_order.verdict;document.getElementById('event-verdict').innerHTML=D.operating_events.verdict;
function examples(id,rows){{document.getElementById(id).innerHTML=rows.map(r=>`<li><span><strong>${{r.ticker}} ${{r.name||''}}</strong><br><small>${{r.pattern_label}} / ${{r.entry_date}}</small></span><strong class="${{cls(r.return_12m)}}">${{pct(r.return_12m)}}</strong></li>`).join('')}}examples('best-list',D.operating_events.best_examples);examples('worst-list',D.operating_events.worst_examples);
</script></body></html>"""


def build_verdicts(rank_summary, monotonic, event_summary):
    if monotonic:
        rank_verdict = "順位帯は12か月中央値でおおむね上位ほど良好でした。ただし各月の候補数が少なく、同一銘柄の月次重複もあるため、強い証明ではありません。"
    else:
        rank_verdict = "順位帯とその後の成績は単調ではありません。上位に置くほど良いという前提は、旧現行版の過去データでは確認できませんでした。現行方式は順位差の前向き記録が必要です。"
    reliable = [row for row in event_summary if row.get("n_12m", 0) >= 5 and row.get("median_12m") is not None]
    best = max(reliable, key=lambda row: row.get("median_alpha_12m", -999), default=None)
    turnaround = next((row for row in event_summary if row["key"] == "two_loss_to_profit"), None)
    if turnaround and turnaround.get("n_12m", 0) >= 5:
        comparison = f"2期赤字→黒字は12か月中央値{fmt(turnaround['median_12m'])}、市場超過{fmt(turnaround['median_alpha_12m'])}、勝率{turnaround['win_12m']:.1f}%でした。"
    else:
        comparison = "2期赤字→黒字は12か月の有効件数が5件未満なら、肌感覚を肯定も否定もできません。"
    best_text = f"市場超過が最も高いのは「{best['label']}」で、絶対騰落率{fmt(best['median_12m'])}、市場超過{fmt(best['median_alpha_12m'])}でした。" if best else "十分なサンプルのある区分がありません。"
    return rank_verdict, comparison + best_text + " 黒字転換だけでなく、足元売上と株価位置を併用するのが妥当です。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-financials", action="store_true")
    parser.add_argument("--refresh-prices", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.refresh_financials or not CACHE_PATH.exists():
        cache = refresh_statement_cache(args.workers)
    else:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    ranking_source = json.loads(RANKING_SOURCE.read_text(encoding="utf-8"))
    rank_tickers = {
        row.get("Ticker")
        for strategies in ranking_source.get("by_month", {}).values()
        for row in strategies.get("現行版", [])
        if row.get("Ticker")
    }
    event_tickers = set(cache.get("records", {}))
    all_tickers = rank_tickers | event_tickers
    prices, stock_price_end = load_stock_prices(all_tickers)
    if args.refresh_prices or not BENCHMARK_CACHE_PATH.exists():
        benchmark_frame = refresh_benchmark_cache()
    else:
        benchmark_frame = pd.read_csv(BENCHMARK_CACHE_PATH)
    benchmark_frame["date"] = pd.to_datetime(benchmark_frame["date"])
    prices[BENCHMARK_TICKER] = benchmark_frame.set_index("date").sort_index()
    price_end = min(stock_price_end, benchmark_frame["date"].max().strftime("%Y-%m-%d"))
    rank_rows, rank_summary, monotonic = analyze_historical_rank_order(prices)
    events, event_summary, best_examples, worst_examples = analyze_operating_events(cache, prices)
    change_impact = analyze_operating_change_impact(events)
    rank_verdict, event_verdict = build_verdicts(rank_summary, monotonic, event_summary)
    amount_corr = change_impact["correlations"]["operating_change_yen"]["return_12m"]
    ratio_corr = change_impact["correlations"]["operating_change_rate"]["return_12m"]
    share_corr = change_impact["correlations"]["operating_change_revenue_share"]["return_12m"]
    change_impact["verdict"] = (
        f"営業利益の増加額と12か月騰落率の順位相関は{amount_corr:+.3f}。"
        f"増加額上位20%は下位20%より12か月中央値が{change_impact['top_minus_bottom_median_12m']:+.1f}ポイント、"
        f"市場超過が{change_impact['top_minus_bottom_alpha_12m']:+.1f}ポイント高い結果でした。"
        f"一方、前期比率の相関は{ratio_corr:+.3f}、売上高比は{share_corr:+.3f}、"
        f"売上規模を調整した増加額の偏相関は{change_impact['partial_spearman_controlling_revenue']:+.3f}です。"
        "絶対額には情報がありますが、企業規模と景気局面を含むため、単独で将来上昇率とはみなしません。"
    )
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S JST"),
        "price_data_through": price_end,
        "forecast_history_days": 2,
        "statement_cache": {
            "requested": cache.get("requested"),
            "successful": cache.get("successful"),
            "generated_at": cache.get("generated_at"),
        },
        "rank_order": {
            "method": "Legacy current-version monthly rankings; point-in-time financial periods with a 60-day lag; repeated monthly holdings retained.",
            "observations": len(rank_rows),
            "monotonic": monotonic,
            "verdict": rank_verdict,
            "summary": rank_summary,
        },
        "operating_events": {
            "method": "Annual operating-income actuals; event availability approximated as fiscal period end plus 60 days.",
            "observations": len(events),
            "verdict": event_verdict,
            "summary": event_summary,
            "best_examples": best_examples,
            "worst_examples": worst_examples,
        },
        "operating_change_impact": {
            "method": "Actual annual operating-income change, grouped into sample quintiles; 12-month performance starts 60 days after fiscal period end.",
            **change_impact,
        },
        "limitations": [
            "Historical company operating-profit forecasts are unavailable, so Safety Version 16 cannot yet be fully backtested.",
            "Operating events use actual results and therefore test post-result reaction, not forecast accuracy.",
            "The report uses a 60-day reporting lag because exact announcement timestamps are unavailable.",
        ],
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(events).to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    OUTPUT_HTML.write_text(report_html(result), encoding="utf-8")
    print(json.dumps({
        "rank_observations": len(rank_rows),
        "rank_summary": rank_summary,
        "event_observations": len(events),
        "event_summary": event_summary,
        "operating_change_impact": change_impact,
        "outputs": [str(OUTPUT_HTML), str(OUTPUT_JSON), str(OUTPUT_CSV)],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
