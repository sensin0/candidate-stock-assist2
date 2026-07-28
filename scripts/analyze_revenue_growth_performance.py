import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_JSON = ROOT / "web-dashboard" / "public" / "strategy_backtest_results.json"
REPORT_MD = ROOT / "revenue_20_performance_report.md"
DETAIL_CSV = ROOT / "revenue_20_performance_details.csv"


def pct(value):
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def number(value, digits=1):
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def median(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return statistics.median(values)


def average(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def bucket_revenue_growth(value):
    if value is None:
        return "不明"
    if value < 0:
        return "マイナス"
    if value < 10:
        return "0-9.9%"
    if value < 15:
        return "10-14.9%"
    if value < 20:
        return "15-19.9%"
    if value < 30:
        return "20-29.9%"
    return "30%以上"


BUCKET_ORDER = ["マイナス", "0-9.9%", "10-14.9%", "15-19.9%", "20-29.9%", "30%以上", "不明"]


def load_current_rows():
    data = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    rows = []
    for month, strategies in data.get("by_month", {}).items():
        current_rows = strategies.get("現行版", []) if isinstance(strategies, dict) else []
        for rank, row in enumerate(current_rows, start=1):
            copied = dict(row)
            copied["Month"] = month
            copied["Rank"] = rank
            copied["Revenue Growth Bucket"] = bucket_revenue_growth(copied.get("Revenue Growth"))
            rows.append(copied)
    rows.sort(key=lambda r: (r["Month"], r["Rank"]))
    return data, rows


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["Revenue Growth Bucket"]].append(row)

    summary = []
    for bucket in BUCKET_ORDER:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        count = len(bucket_rows)
        target100 = sum(1 for r in bucket_rows if r.get("Target 100 Hit")) / count
        target50 = sum(1 for r in bucket_rows if r.get("Target 50 Hit")) / count
        stop_before100 = sum(1 for r in bucket_rows if r.get("Stop Before Target 100")) / count
        summary.append(
            {
                "売上成長率帯": bucket,
                "件数": count,
                "平均1年後": average([r.get("1yr Return") for r in bucket_rows]),
                "中央値1年後": median([r.get("1yr Return") for r in bucket_rows]),
                "平均2年内最大": average([r.get("2yr Max Return") for r in bucket_rows]),
                "中央値2年内最大": median([r.get("2yr Max Return") for r in bucket_rows]),
                "2倍到達率": target100,
                "50%到達率": target50,
                "2倍到達日数中央値": median([r.get("Days to Target 100") for r in bucket_rows]),
                "2倍前損切り率": stop_before100,
                "50%利確ルール平均": average([r.get("Rule 50 Return") for r in bucket_rows]),
                "100%利確ルール平均": average([r.get("Rule 100 Return") for r in bucket_rows]),
                "100%利確ルール勝率": sum(1 for r in bucket_rows if (r.get("Rule 100 Return") or 0) > 0) / count,
            }
        )
    return summary


def summarize_topn(rows, top_n):
    return summarize([r for r in rows if r["Rank"] <= top_n])


def dedupe_by_ticker(rows):
    best_by_ticker = {}
    for row in rows:
        ticker = row.get("Ticker")
        if not ticker:
            continue
        current = best_by_ticker.get(ticker)
        current_score = current.get("Score", -10**9) if current else -10**9
        row_score = row.get("Score", -10**9)
        if current is None or row_score > current_score or (row_score == current_score and row["Month"] < current["Month"]):
            best_by_ticker[ticker] = row
    return sorted(best_by_ticker.values(), key=lambda r: (r["Month"], r["Rank"]))


def markdown_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def make_summary_table(summary):
    rows = []
    for s in summary:
        rows.append(
            {
                "売上成長率帯": s["売上成長率帯"],
                "件数": s["件数"],
                "平均1年後": pct(s["平均1年後"]),
                "中央値1年後": pct(s["中央値1年後"]),
                "平均2年内最大": pct(s["平均2年内最大"]),
                "2倍到達率": pct(s["2倍到達率"]),
                "2倍到達日数中央値": "-" if s["2倍到達日数中央値"] is None else f"{s['2倍到達日数中央値']:.0f}日",
                "100%利確ルール平均": pct(s["100%利確ルール平均"]),
                "損切り先行率": pct(s["2倍前損切り率"]),
            }
        )
    return markdown_table(
        [
            "売上成長率帯",
            "件数",
            "平均1年後",
            "中央値1年後",
            "平均2年内最大",
            "2倍到達率",
            "2倍到達日数中央値",
            "100%利確ルール平均",
            "損切り先行率",
        ],
        rows,
    )


def top_examples(rows, bucket, limit=12):
    filtered = [r for r in rows if r["Revenue Growth Bucket"] == bucket]
    filtered.sort(key=lambda r: (not r.get("Target 100 Hit"), -(r.get("2yr Max Return") or -999), r["Month"], r["Rank"]))
    output = []
    for r in filtered[:limit]:
        output.append(
            {
                "月": r["Month"],
                "順位": r["Rank"],
                "銘柄": f"{r.get('Ticker')} {r.get('Name')}",
                "売上成長": number(r.get("Revenue Growth")) + "%",
                "買値": number(r.get("Buy Price"), 0),
                "2年内最大": pct(r.get("2yr Max Return")),
                "2倍": "達成" if r.get("Target 100 Hit") else "未達",
                "日数": "-" if r.get("Days to Target 100") is None else f"{r.get('Days to Target 100'):.0f}日",
                "100%利確ルール": pct(r.get("Rule 100 Return")),
            }
        )
    return output


def write_detail_csv(rows):
    headers = [
        "Month",
        "Rank",
        "Ticker",
        "Name",
        "Sector",
        "Revenue Growth",
        "Revenue Growth Bucket",
        "Score",
        "Buy Price",
        "1yr Return",
        "2yr Max Return",
        "2yr Final Return",
        "Target 50 Hit",
        "Target 100 Hit",
        "Days to Target 100",
        "Stop Before Target 100",
        "Rule 50 Return",
        "Rule 100 Return",
    ]
    with DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h) for h in headers})


def build_report(data, rows):
    summary_all = summarize(rows)
    summary_top5 = summarize_topn(rows, 5)
    summary_top10 = summarize_topn(rows, 10)
    deduped_rows = dedupe_by_ticker(rows)
    summary_deduped = summarize(deduped_rows)
    bucket20 = next((s for s in summary_all if s["売上成長率帯"] == "20-29.9%"), None)
    bucket20_deduped = next((s for s in summary_deduped if s["売上成長率帯"] == "20-29.9%"), None)
    best_doubler = max(summary_all, key=lambda s: (s["2倍到達率"], s["件数"]))
    best_rule100 = max(summary_all, key=lambda s: (s["100%利確ルール平均"] if s["100%利確ルール平均"] is not None else -999, s["件数"]))

    conclusion = []
    if bucket20:
        if best_doubler["売上成長率帯"] == "20-29.9%" and best_rule100["売上成長率帯"] == "20-29.9%":
            conclusion.append("20%台は、2倍到達率と100%利確ルール平均の両方で最上位。仮説はこの検証範囲では支持。")
        elif best_doubler["売上成長率帯"] == "20-29.9%" or best_rule100["売上成長率帯"] == "20-29.9%":
            conclusion.append("20%台は一部指標では最上位。ただし全面的に一番とは言い切れない。")
        else:
            conclusion.append("20%台が一番という仮説は、この検証範囲では強くは支持されない。")
        conclusion.append(
            f"20%台は件数{bucket20['件数']}、2倍到達率{pct(bucket20['2倍到達率'])}、"
            f"100%利確ルール平均{pct(bucket20['100%利確ルール平均'])}。"
        )
        if bucket20_deduped:
            conclusion.append(
                f"銘柄重複を除くと20%台は件数{bucket20_deduped['件数']}、"
                f"2倍到達率{pct(bucket20_deduped['2倍到達率'])}、"
                f"100%利確ルール平均{pct(bucket20_deduped['100%利確ルール平均'])}。"
            )
    else:
        conclusion.append("20%台のサンプルがなく、仮説検証できなかった。")
    conclusion.append(
        f"比較上の2倍到達率トップは{best_doubler['売上成長率帯']}、"
        f"100%利確ルール平均トップは{best_rule100['売上成長率帯']}。"
    )

    report = []
    report.append("# 売上成長20%台候補 パフォーマンス検証\n")
    report.append(f"- 作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"- 元データ: `{SOURCE_JSON.relative_to(ROOT)}` / generated_at `{data.get('generated_at')}`")
    report.append("- 対象: 月初ごとのランキング上位に出た「現行版」候補")
    report.append("- 評価: 買付後2年以内の最大上昇率、2倍到達、50%/100%利確ルール後の利益率")
    report.append("- 注意: 月次バックテストJSONに残っている候補のみの比較。全市場全銘柄をその場で再生成した完全検証ではない。\n")

    report.append("## 1. 結論\n")
    for item in conclusion:
        report.append(f"- {item}")
    report.append("")

    report.append("## 2. 売上成長率帯別パフォーマンス（現行版・全ランキング候補）\n")
    report.append(make_summary_table(summary_all))
    report.append("")

    report.append("## 3. ランキング上位5だけに絞った場合\n")
    report.append(make_summary_table(summary_top5))
    report.append("")

    report.append("## 4. ランキング上位10だけに絞った場合\n")
    report.append(make_summary_table(summary_top10))
    report.append("")

    report.append("## 5. 銘柄重複を除いた場合\n")
    report.append(make_summary_table(summary_deduped))
    report.append("")

    report.append("## 6. 20%台候補の代表例\n")
    report.append(
        markdown_table(
            ["月", "順位", "銘柄", "売上成長", "買値", "2年内最大", "2倍", "日数", "100%利確ルール"],
            top_examples(rows, "20-29.9%"),
        )
    )
    report.append("")

    report.append("## 7. 30%以上候補の代表例（比較用）\n")
    report.append(
        markdown_table(
            ["月", "順位", "銘柄", "売上成長", "買値", "2年内最大", "2倍", "日数", "100%利確ルール"],
            top_examples(rows, "30%以上"),
        )
    )
    report.append("")

    report.append("## 8. 実運用への読み替え\n")
    report.append("- 20%台は「候補として強い」かを見たが、単独条件ではなくランキング上位・低位置・赤字縮小などとセットで見る。")
    report.append("- 2倍狙いでは、2年以内2倍到達率と損切り先行率を同時に見る。到達率だけ高くても損切り先行率が高い帯は扱いにくい。")
    report.append("- 利確は、検証上は100%利確ルール平均を主指標にし、50%利確ルールは守り重視の比較指標として見る。")
    report.append("- 件数が少ない帯は過信しない。特に20%台はサンプル件数を見て判断する。")
    report.append("- 月違いで同じ銘柄が何度も入るため、銘柄重複を除いた表を優先して見る。")
    report.append("")
    REPORT_MD.write_text("\n".join(report), encoding="utf-8")


def main():
    data, rows = load_current_rows()
    write_detail_csv(rows)
    build_report(data, rows)
    print(f"wrote {REPORT_MD}")
    print(f"wrote {DETAIL_CSV}")
    print(f"rows {len(rows)}")


if __name__ == "__main__":
    main()
