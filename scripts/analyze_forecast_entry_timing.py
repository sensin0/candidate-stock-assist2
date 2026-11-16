import argparse
import html
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "stocks.db"
EVENTS_PATH = ROOT / "docs" / "operating-profit-events.csv"
BENCHMARK_PATH = ROOT / "data" / "topix_benchmark_adjusted.csv"
CACHE_PATH = ROOT / "data" / "earnings_dates_cache.json"
OUTPUT_JSON = ROOT / "docs" / "forecast-entry-timing.json"
OUTPUT_CSV = ROOT / "docs" / "forecast-entry-timing-events.csv"
OUTPUT_HTML = ROOT / "docs" / "forecast-entry-timing.html"
BENCHMARK_TICKER = "1306.T"
TIMINGS = {
    "next_day": ("翌営業日", 0),
    "one_week": ("1週間後", 4),
    "two_weeks": ("2週間後", 9),
    "one_month": ("1か月後", 19),
}


def fetch_earnings_dates(ticker):
    dates = yf.Ticker(ticker).get_earnings_dates(limit=24)
    if dates is None or dates.empty:
        return ticker, []
    normalized = []
    for value in dates.index:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("Asia/Tokyo").tz_localize(None)
        normalized.append(timestamp.strftime("%Y-%m-%d"))
    return ticker, sorted(set(normalized))


def refresh_date_cache(tickers, workers):
    records = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch_earnings_dates, ticker): ticker for ticker in sorted(tickers)}
        for index, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                key, dates = future.result()
                records[key] = dates
            except Exception as error:
                errors.append({"ticker": ticker, "error": str(error)})
            if index % 50 == 0:
                print(f"Fetched earnings dates {index}/{len(tickers)}")
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S JST"),
        "requested": len(tickers),
        "successful": sum(bool(value) for value in records.values()),
        "records": records,
        "errors": errors,
    }
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def match_annual_earnings_date(period_end, dates):
    period = pd.Timestamp(period_end)
    candidates = [
        pd.Timestamp(value)
        for value in dates
        if period + pd.Timedelta(days=20) <= pd.Timestamp(value) <= period + pd.Timedelta(days=100)
    ]
    return min(candidates) if candidates else None


def load_prices(tickers):
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" for _ in tickers)
    frame = pd.read_sql(
        f"SELECT ticker, date, close, high, low FROM prices WHERE ticker IN ({placeholders}) ORDER BY ticker, date",
        conn,
        params=tuple(sorted(tickers)),
    )
    conn.close()
    frame["date"] = pd.to_datetime(frame["date"])
    return {ticker: group.set_index("date").sort_index() for ticker, group in frame.groupby("ticker")}


def load_benchmark():
    frame = pd.read_csv(BENCHMARK_PATH)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").sort_index()


def trading_entry(prices, announcement_date, session_offset):
    eligible = prices[prices.index > announcement_date]
    if len(eligible) <= session_offset:
        return None, None
    row = eligible.iloc[session_offset]
    return eligible.index[session_offset], float(row["close"])


def close_on_or_before(prices, target, max_days=10):
    window = prices[(prices.index <= target) & (prices.index >= target - pd.Timedelta(days=max_days))]
    return None if window.empty else float(window.iloc[-1]["close"])


def fixed_end_performance(prices, benchmark, entry_date, entry_price, announcement_date):
    target = announcement_date + pd.DateOffset(months=12)
    end_price = close_on_or_before(prices, target)
    benchmark_entry_rows = benchmark[benchmark.index >= entry_date]
    benchmark_entry = None if benchmark_entry_rows.empty else float(benchmark_entry_rows.iloc[0]["close"])
    benchmark_end = close_on_or_before(benchmark, target)
    stock_return = None if end_price is None else (end_price / entry_price - 1) * 100
    benchmark_return = None if benchmark_entry is None or benchmark_end is None else (benchmark_end / benchmark_entry - 1) * 100
    window = prices[(prices.index > entry_date) & (prices.index <= target)]
    return {
        "return_12m_end": stock_return,
        "benchmark_12m_end": benchmark_return,
        "alpha_12m_end": None if stock_return is None or benchmark_return is None else stock_return - benchmark_return,
        "max_to_12m_end": None if window.empty else (float(window["high"].max()) / entry_price - 1) * 100,
        "drawdown_to_12m_end": None if window.empty else (float(window["low"].min()) / entry_price - 1) * 100,
    }


def pullback_entry(prices, announcement_date, initial_price, threshold=0.95, sessions=20):
    eligible = prices[prices.index > announcement_date].iloc[1:sessions + 1]
    hit = eligible[eligible["close"] <= initial_price * threshold]
    if hit.empty:
        return None, None
    return hit.index[0], float(hit.iloc[0]["close"])


def summarize(rows, key, label):
    values = [row for row in rows if row["timing_key"] == key and row.get("return_12m_end") is not None]
    returns = pd.Series([row["return_12m_end"] for row in values], dtype=float)
    alpha = pd.Series([row["alpha_12m_end"] for row in values if row.get("alpha_12m_end") is not None], dtype=float)
    maximum = pd.Series([row["max_to_12m_end"] for row in values if row.get("max_to_12m_end") is not None], dtype=float)
    drawdown = pd.Series([row["drawdown_to_12m_end"] for row in values if row.get("drawdown_to_12m_end") is not None], dtype=float)
    return {
        "key": key,
        "label": label,
        "n": len(values),
        "median_return": None if returns.empty else round(float(returns.median()), 1),
        "average_return": None if returns.empty else round(float(returns.mean()), 1),
        "win_rate": None if returns.empty else round(float((returns > 0).mean() * 100), 1),
        "median_alpha": None if alpha.empty else round(float(alpha.median()), 1),
        "beat_market": None if alpha.empty else round(float((alpha > 0).mean() * 100), 1),
        "median_max": None if maximum.empty else round(float(maximum.median()), 1),
        "median_drawdown": None if drawdown.empty else round(float(drawdown.median()), 1),
    }


def report_html(result):
    data = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>決算発表後の買いタイミング検証</title>
<style>:root{{--ink:#172033;--muted:#64748b;--line:#dbe3ec;--paper:#fff;--soft:#f4f7fa;--green:#087f5b;--red:#c2413b;--amber:#a16207}}*{{box-sizing:border-box}}body{{margin:0;background:var(--soft);color:var(--ink);font-family:"Noto Sans JP","Yu Gothic UI",sans-serif;letter-spacing:0}}header{{background:#172033;color:#fff;border-bottom:4px solid #23a77a}}.wrap{{width:min(1120px,calc(100% - 30px));margin:auto}}.head{{padding:28px 0 24px;display:flex;justify-content:space-between;gap:20px;align-items:end}}h1{{font-size:clamp(25px,4vw,40px);margin:0 0 8px}}header p{{margin:0;color:#cbd5e1}}a{{color:inherit}}.back{{border:1px solid #64748b;padding:9px 12px;border-radius:6px;text-decoration:none}}main{{padding:24px 0 48px}}.notice,.verdict{{padding:14px 16px;border-left:5px solid var(--amber);background:#fff8df;line-height:1.7}}.panel{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:20px;margin-top:16px}}h2{{margin:0 0 6px;font-size:21px}}.lead{{color:var(--muted);line-height:1.7}}.chart{{overflow-x:auto}}svg{{min-width:720px;width:100%;height:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:11px 8px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{color:var(--muted);font-size:12px}}.positive{{color:var(--green);font-weight:700}}.negative{{color:var(--red);font-weight:700}}.table-scroll{{overflow-x:auto}}ul{{line-height:1.8}}@media(max-width:700px){{.head{{align-items:start;flex-direction:column}}.panel{{padding:14px}}table{{min-width:760px}}}}</style></head>
<body><header><div class="wrap head"><div><h1>会社予想が出たあと、いつ買うか</h1><p>年次決算発表の翌営業日から1か月後まで、同じ終了日で比較</p></div><a class="back" href="./index.html">ランキングへ戻る</a></div></header><main class="wrap">
<div class="notice"><strong>重要:</strong> 発表日は過去決算日から照合していますが、当時の会社予想数値そのものは保存されていません。営業利益が実際に改善した銘柄を使った「発表後タイミング」の検証です。</div>
<section class="panel"><h2>買い日を遅らせるとどう変わるか</h2><p class="lead">発表日から12か月後を共通の売却基準日にしています。遅く買うほど保有期間が短くなるため、純粋にどこで入ると値幅を取りやすいかを比べられます。</p><div class="chart" id="chart"></div><div class="table-scroll"><table id="table"></table></div><div class="verdict" id="verdict"></div></section>
<section class="panel"><h2>営業利益の増加額が大きい銘柄だけ</h2><p class="lead">今回の金額差検証で上位40%に入った銘柄に絞った結果です。</p><div class="table-scroll"><table id="large-table"></table></div></section>
<section class="panel"><h2>金額差で買い日を変えた場合</h2><p class="lead">増加額上位40%は翌営業日、それ以外は1か月後に買うルールです。発表時点で分かる営業利益差だけを使います。</p><div class="table-scroll"><table id="adaptive-table"></table></div></section>
<section class="panel"><h2>読み方と限界</h2><ul><li>翌営業日の終値で購入する前提です。寄り付き価格や売買コストは含みません。</li><li>会社予想が上方修正されたか、予想が市場期待を上回ったかは判定できません。</li><li>5%押し目は20営業日以内に実際に5%下がった場合だけ買い、来なければ見送ります。</li><li>結果は過去の傾向で、将来の利益を保証しません。</li></ul></section></main>
<script>const D={data};const pct=v=>v==null?'-':`${{v>=0?'+':''}}${{v.toFixed(1)}}%`;const cls=v=>v==null?'':v>=0?'positive':'negative';function table(rows){{return `<thead><tr><th>買いタイミング</th><th>件数</th><th>12か月終了時</th><th>勝率</th><th>市場超過</th><th>市場勝率</th><th>期間内最大上昇</th><th>最大下落</th></tr></thead><tbody>${{rows.map(r=>`<tr><td><strong>${{r.label}}</strong></td><td>${{r.n}}</td><td class="${{cls(r.median_return)}}">${{pct(r.median_return)}}</td><td>${{pct(r.win_rate)}}</td><td class="${{cls(r.median_alpha)}}">${{pct(r.median_alpha)}}</td><td>${{pct(r.beat_market)}}</td><td class="positive">${{pct(r.median_max)}}</td><td class="negative">${{pct(r.median_drawdown)}}</td></tr>`).join('')}}</tbody>`}}function chart(rows){{const W=880,H=320,p={{l:130,r:45,t:22,b:45}},vals=rows.map(r=>r.median_return),lo=Math.min(-10,...vals),hi=Math.max(10,...vals),x=i=>p.l+i*(W-p.l-p.r)/(rows.length-1),y=v=>p.t+(hi-v)*(H-p.t-p.b)/(hi-lo);let s=`<svg viewBox="0 0 ${{W}} ${{H}}">`;for(let v=Math.floor(lo/10)*10;v<=hi;v+=10)s+=`<line x1="${{p.l}}" x2="${{W-p.r}}" y1="${{y(v)}}" y2="${{y(v)}}" stroke="#dbe3ec"/><text x="${{p.l-8}}" y="${{y(v)+4}}" text-anchor="end" fill="#64748b">${{v}}%</text>`;s+=`<polyline points="${{rows.map((r,i)=>`${{x(i)}},${{y(r.median_return)}}`).join(' ')}}" fill="none" stroke="#087f5b" stroke-width="4"/>`;rows.forEach((r,i)=>s+=`<circle cx="${{x(i)}}" cy="${{y(r.median_return)}}" r="6" fill="#087f5b"/><text x="${{x(i)}}" y="${{H-14}}" text-anchor="middle" fill="#64748b">${{r.label}}</text><text x="${{x(i)}}" y="${{y(r.median_return)-11}}" text-anchor="middle" fill="#087f5b" font-weight="700">${{pct(r.median_return)}}</text>`);return s+'</svg>'}}document.getElementById('chart').innerHTML=chart(D.all.summary.filter(r=>r.key!=='pullback_5'));document.getElementById('table').innerHTML=table(D.all.summary);document.getElementById('large-table').innerHTML=table(D.large_change.summary);document.getElementById('adaptive-table').innerHTML=table([D.adaptive]);document.getElementById('verdict').innerHTML=D.verdict;</script></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-dates", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    events = pd.read_csv(EVENTS_PATH)
    events = events[(events["return_12m"].notna()) & (events["operating_change_yen"] > 0)].copy()
    tickers = set(events["ticker"])
    if args.refresh_dates or not CACHE_PATH.exists():
        cache = refresh_date_cache(tickers, args.workers)
    else:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    prices = load_prices(tickers)
    benchmark = load_benchmark()
    rows = []
    matched_events = []
    for event in events.to_dict("records"):
        ticker = event["ticker"]
        announcement = match_annual_earnings_date(event["period_end"], cache.get("records", {}).get(ticker, []))
        ticker_prices = prices.get(ticker)
        if announcement is None or ticker_prices is None or ticker_prices.empty:
            continue
        matched_events.append(event)
        _, initial_price = trading_entry(ticker_prices, announcement, 0)
        prior_window = ticker_prices[ticker_prices.index <= announcement]
        prior_close = None if prior_window.empty else float(prior_window.iloc[-1]["close"])
        initial_reaction = None if initial_price is None or prior_close in (None, 0) else (initial_price / prior_close - 1) * 100
        for key, (label, offset) in TIMINGS.items():
            entry_date, entry_price = trading_entry(ticker_prices, announcement, offset)
            if entry_date is None:
                continue
            rows.append({**event, "announcement_date": announcement.strftime("%Y-%m-%d"), "initial_reaction": initial_reaction, "timing_key": key, "timing_label": label, "entry_date_actual": entry_date.strftime("%Y-%m-%d"), "entry_price": entry_price, **fixed_end_performance(ticker_prices, benchmark, entry_date, entry_price, announcement)})
        if initial_price is not None:
            entry_date, entry_price = pullback_entry(ticker_prices, announcement, initial_price)
            if entry_date is not None:
                rows.append({**event, "announcement_date": announcement.strftime("%Y-%m-%d"), "initial_reaction": initial_reaction, "timing_key": "pullback_5", "timing_label": "5%押し目", "entry_date_actual": entry_date.strftime("%Y-%m-%d"), "entry_price": entry_price, **fixed_end_performance(ticker_prices, benchmark, entry_date, entry_price, announcement)})
    ordered = [(key, value[0]) for key, value in TIMINGS.items()] + [("pullback_5", "5%押し目")]
    summary = [summarize(rows, key, label) for key, label in ordered]
    changes = pd.Series([row["operating_change_yen"] for row in matched_events], dtype=float)
    large_threshold = float(changes.quantile(0.60)) if not changes.empty else 0
    large_tickers = {(row["ticker"], row["period_end"]) for row in matched_events if row["operating_change_yen"] >= large_threshold}
    large_rows = [row for row in rows if (row["ticker"], row["period_end"]) in large_tickers]
    large_summary = [summarize(large_rows, key, label) for key, label in ordered]
    adaptive_rows = []
    for event_key in {(row["ticker"], row["period_end"]) for row in rows}:
        is_large = event_key in large_tickers
        target_key = "next_day" if is_large else "one_month"
        selected = next((row for row in rows if (row["ticker"], row["period_end"]) == event_key and row["timing_key"] == target_key), None)
        if selected:
            adaptive_rows.append({**selected, "timing_key": "adaptive"})
    adaptive_summary = summarize(adaptive_rows, "adaptive", "金額差大は翌日・他は1か月後")
    fixed = [row for row in summary if row["key"] != "pullback_5" and row["median_return"] is not None]
    best = max(fixed, key=lambda row: row["median_return"])
    fastest = next(row for row in summary if row["key"] == "next_day")
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S JST"),
        "event_source": "Annual earnings dates matched 20-100 days after fiscal period end; operating-profit events use actual results.",
        "requested_events": len(events),
        "matched_events": len(matched_events),
        "date_cache": {"generated_at": cache.get("generated_at"), "requested": cache.get("requested"), "successful": cache.get("successful")},
        "all": {"summary": summary},
        "large_change": {"threshold_yen": large_threshold, "events": len(large_tickers), "summary": large_summary},
        "adaptive": adaptive_summary,
        "pullback_hit_rate": round(next(row for row in summary if row["key"] == "pullback_5")["n"] / len(matched_events) * 100, 1),
        "verdict": f"全体では中央値が最も高いのは「{best['label']}」の{best['median_return']:+.1f}%、翌営業日は{fastest['median_return']:+.1f}%でした。ただし営業利益増加額の上位40%は翌営業日が最良です。金額差で買い日を変えるルールは{adaptive_summary['median_return']:+.1f}%でした。5%押し目は全体の{round(next(row for row in summary if row['key'] == 'pullback_5')['n'] / len(matched_events) * 100, 1):.1f}%しか来ません。",
        "limitations": [
            "Historical company forecast values are unavailable; this tests entry timing after annual earnings, not forecast surprise accuracy.",
            "Yahoo earnings dates are used as the announcement-date source and may include timestamp estimation errors.",
            "Entries use daily closing prices and exclude transaction costs.",
        ],
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    OUTPUT_HTML.write_text(report_html(result), encoding="utf-8")
    print(json.dumps({"matched_events": len(matched_events), "all": summary, "large_change": large_summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
