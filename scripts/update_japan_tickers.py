import argparse
import io
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "japan_tickers.csv"
JPX_LISTED_ISSUES_URLS = [
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls",
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
]


def find_column(columns, candidates):
    normalized = {str(column).strip(): column for column in columns}
    for candidate in candidates:
        for name, original in normalized.items():
            if candidate in name:
                return original
    raise ValueError(f"Required column not found. candidates={candidates} columns={list(normalized)}")


def download_listed_issues(urls):
    last_error = None
    for url in urls:
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return pd.read_excel(io.BytesIO(response.content), dtype=str)
        except Exception as error:
            last_error = error
    raise RuntimeError(f"Could not download JPX listed issues: {last_error}")


def build_ticker_rows(df):
    code_col = find_column(df.columns, ["コード", "Code"])
    name_col = find_column(df.columns, ["銘柄名", "Name"])
    market_col = find_column(df.columns, ["市場・商品区分", "Market"])
    sector_col = find_column(df.columns, ["33業種区分", "33 Sector", "Sector"])

    rows = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip()
        market = str(row.get(market_col, "")).strip()
        sector = str(row.get(sector_col, "")).strip()

        if not code.isdigit() or len(code) != 4:
            continue
        if "内国株式" not in market:
            continue
        if not name or name.lower() == "nan":
            continue
        if not sector or sector.lower() == "nan" or sector == "-":
            sector = "未分類"

        rows.append((code, name, sector))

    rows = sorted(set(rows), key=lambda item: item[0])
    return rows


def write_csv(rows, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(escape_csv(value) for value in row) for row in rows]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_csv(value):
    text = str(value)
    if any(char in text for char in [",", '"', "\n"]):
        return '"' + text.replace('"', '""') + '"'
    return text


def main():
    parser = argparse.ArgumentParser(description="Update japan_tickers.csv from the official JPX listed issues file.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--min-count", type=int, default=3000)
    parser.add_argument("--url", action="append", help="Override JPX listed issues URL. Can be specified multiple times.")
    args = parser.parse_args()

    urls = args.url or JPX_LISTED_ISSUES_URLS
    df = download_listed_issues(urls)
    rows = build_ticker_rows(df)
    if len(rows) < args.min_count:
        raise RuntimeError(f"Ticker count looks too small: {len(rows)} < {args.min_count}")

    output = Path(args.output)
    write_csv(rows, output)
    print(f"Wrote {len(rows)} tickers to {output}")


if __name__ == "__main__":
    main()
