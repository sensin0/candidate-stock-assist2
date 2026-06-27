import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TICKERS = ROOT / "japan_tickers.csv"
DEFAULT_OUTPUT = ROOT / "weekly_ranking_report.json"
DEFAULT_STATE = ROOT / ".github" / "ranking-state.json"
DEFAULT_REPORT_URL = "https://sensin0.github.io/candidate-stock-assist2/"
SAFETY_VERSION = 3
CURRENT_EXCLUDED_SECTORS = {"サービス業"}


def clean_number(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def find_statement_value(statement, names):
    if statement is None or statement.empty:
        return []
    normalized = {str(index).lower(): index for index in statement.index}
    for name in names:
        key = name.lower()
        if key in normalized:
            return [clean_number(value) for value in statement.loc[normalized[key]].tolist()]
    for name in names:
        key = name.lower()
        for normalized_name, original_name in normalized.items():
            if key in normalized_name:
                return [clean_number(value) for value in statement.loc[original_name].tolist()]
    return []


def pct_change(latest, previous):
    if latest is None or previous in (None, 0):
        return None
    return (latest - previous) / abs(previous) * 100


def growth_label(growth):
    if growth is None:
        return None
    if growth >= 20:
        return "爆発"
    if growth >= 10:
        return "確信"
    if growth >= 5:
        return "兆し"
    if growth < -10:
        return "悪化"
    return "横ばい"


def loss_improving(latest_loss, previous_loss, latest_revenue, previous_revenue):
    if latest_loss is None or previous_loss is None:
        return None
    if latest_revenue in (None, 0) or previous_revenue in (None, 0):
        return None
    latest_margin = latest_loss / abs(latest_revenue)
    previous_margin = previous_loss / abs(previous_revenue)
    return latest_margin > previous_margin


def net_income_risk(latest_net_income, previous_net_income):
    if latest_net_income is None or previous_net_income is None:
        return None
    if previous_net_income >= 0 and latest_net_income < 0:
        return "profit_to_loss"
    if previous_net_income < 0 and latest_net_income < previous_net_income:
        return "deeper_loss"
    if latest_net_income < previous_net_income:
        return "profit_decline"
    return None


def exit_plan_values(current_price, price_location, revenue_growth, blocks=None, net_risk=None):
    target_50 = current_price * 1.5 if current_price else None
    target_100 = current_price * 2 if current_price else None
    target_200 = current_price * 3 if current_price else None
    stop_loss = current_price * 0.75 if current_price else None
    score = 0
    notes = []

    if price_location is not None:
        if price_location < 0.15:
            score += 20
            notes.append("利確余地大")
        elif price_location < 0.3:
            score += 8
            notes.append("利確余地あり")
        elif price_location > 0.7:
            score -= 40
            notes.append("高値圏で利確余地小")
        elif price_location > 0.5:
            score -= 20

    if revenue_growth is not None:
        if 10 <= revenue_growth < 15:
            score += 10
            notes.append("過去検証で到達率良好")
        elif revenue_growth >= 15:
            score += 5
        elif revenue_growth < 0:
            score -= 10

    if blocks or net_risk in {"profit_to_loss", "deeper_loss"}:
        score -= 20

    if score >= 25:
        plan = "+50%で一部利確、+100%で主力利確。急騰時は欲張らず段階的に回収。"
    elif score >= 5:
        plan = "+50%で一部利確、+100%は決算継続確認後。"
    elif score < 0:
        plan = "利確余地は弱め。買うなら小さく、-25%または悪材料で撤退確認。"
    else:
        plan = "+50%を最初の利確目安、+100%は伸びた場合の上限目安。"

    return {
        "Target Price 1": round(target_50, 2) if target_50 is not None else None,
        "Target Price 2": round(target_100, 2) if target_100 is not None else None,
        "Target Price 3": round(target_200, 2) if target_200 is not None else None,
        "Stop Loss": round(stop_loss, 2) if stop_loss is not None else None,
        "Exit Score": round(score, 1),
        "Sell Plan": plan,
        "Sell Notes": notes,
    }


def local_current_version_score(item):
    price_location = item.get("Price Location")
    revenue_growth = item.get("Revenue Growth")
    loss_margin_improving = item.get("Loss Margin Improving")
    loss_margin = item.get("Loss Margin")
    psr_rank = item.get("PSR Rank")
    sector_status = item.get("Sector Status")
    net_history = item.get("Net Income History") or []
    latest_net_income = net_history[0] if len(net_history) >= 1 else item.get("Net Income")
    previous_net_income = net_history[1] if len(net_history) >= 2 else item.get("Prev Net Income")
    is_aggressive = item.get("Is Aggressive") is True

    score = 0
    notes = []
    blocks = []

    two_year_loss = (
        latest_net_income is not None
        and previous_net_income is not None
        and latest_net_income < 0
        and previous_net_income < 0
    )
    if two_year_loss:
        score += 100
        notes.append("2期連続赤字")

        if price_location is not None:
            if price_location < 0.1:
                score += 25
                notes.append("Price at Bottom 10%")
            elif price_location < 0.2:
                score += 10
                notes.append("Price at Bottom")
            elif price_location > 0.7:
                score -= 30
                notes.append("高値圏・押し目待ち (>70%)")
            elif price_location > 0.5:
                score -= 15
                notes.append("中高値圏 (>50%)")

        if psr_rank is not None and psr_rank < 0.2:
            score += 10
            notes.append("PSR Historic Low")

        if sector_status == "Downturn":
            score += 20
            notes.append("Sector Sync")

        if is_aggressive:
            score += 15
            reason = item.get("Aggressive Reason") or ""
            notes.append(f"AGGRESSIVE ({reason})")

        if revenue_growth is not None:
            if revenue_growth >= 20:
                score += 50
                notes.append("売上爆発 (+20%↑)")
            elif revenue_growth >= 15:
                score += 70
                notes.append("売上急成長 (+15%↑)")
            elif revenue_growth >= 10:
                score += 35
                notes.append("売上確信 (+10%↑)")
            elif revenue_growth >= 5:
                score += 5
                notes.append("売上兆し (+5%↑)")
            elif revenue_growth < -10:
                score -= 60
                notes.append("売上悪化 (-10%↓)")
            elif revenue_growth < 0:
                score -= 20
                notes.append("売上減少")

        if loss_margin is not None and loss_margin > -3:
            score += 30
            notes.append("赤字率軽微 (>-3%)")
        elif loss_margin is not None and loss_margin > -5:
            score += 10
            notes.append("赤字率小 (>-5%)")

        if loss_margin_improving is True:
            score += 50
            notes.append("赤字率改善中")
        elif loss_margin_improving is False:
            score -= 50
            notes.append("赤字率悪化中")

        if revenue_growth is not None and revenue_growth >= 10:
            if loss_margin_improving is True:
                score += 30
                notes.append("売上成長+赤字改善コンボ")
            if price_location is not None and price_location < 0.15:
                score += 20
                notes.append("売上成長+底値圏コンボ")

        if sector_status == "Boom":
            score -= 40
            notes.append("セクター好調なのに赤字")

        if loss_margin_improving is False:
            blocks.append("赤字率悪化")
        if revenue_growth is not None and revenue_growth < -10:
            blocks.append("売上悪化")

        if blocks:
            action = "Watch (Blocked: " + "/".join(blocks) + ")"
        elif price_location is not None and price_location > 0.7 and score >= 110:
            action = "Watch (Pullback)"
        elif score >= 150:
            action = "**BUY CANDIDATE** (STRONG)"
        elif score >= 110:
            action = "**BUY CANDIDATE**"
        elif score >= 80:
            action = "Watch (Wait for Profit Turn)"
        else:
            action = "Watch (Wait for Price/Vol)"
        status = "**2-YR LOSS**"
    elif latest_net_income is not None and latest_net_income < 0:
        score += 50
        status = "Red Ink (1yr)"
        action = "Watch (Wait for 2nd yr?)"
    elif previous_net_income is not None and previous_net_income < 0:
        score += 30
        status = "Recovering"
        action = "Check Trend"
    else:
        status = "Profitable"
        action = "Pass"

    return round(score, 1), action, notes, blocks, status


def build_current_rankings(rankings):
    current_rankings = []
    for item in rankings:
        if item.get("Sector") in CURRENT_EXCLUDED_SECTORS:
            continue
        score, action, notes, blocks, status = local_current_version_score(item)
        row = dict(item)
        row["Score"] = score
        row["Entry Score"] = score
        row["Action"] = action
        row["Current Version Notes"] = notes
        row["Blocks"] = blocks
        row["Status"] = status
        current_price = row.get("Current Price")
        if "BUY CANDIDATE" in action and current_price is not None:
            row["Target Price 1"] = round(current_price * 1.5, 2)
            row["Target Price 2"] = round(current_price * 2.0, 2)
            row["Stop Loss"] = round(current_price * 0.8, 2)
        else:
            row["Target Price 1"] = None
            row["Target Price 2"] = None
            row["Stop Loss"] = None
        current_rankings.append(row)
    current_rankings.sort(key=lambda row: row.get("Score", 0), reverse=True)
    for index, item in enumerate(current_rankings, start=1):
        item["Rank"] = index
    return current_rankings


def apply_stored_safety_guard(item):
    if item.get("Safety Version") == SAFETY_VERSION:
        return item

    score = 0
    status = str(item.get("Status") or "")
    if "2-YR LOSS" in status:
        score += 60
    elif item.get("Net Income") is not None and item.get("Net Income") < 0:
        score += 10

    if item.get("Operating Loss Improving") is True:
        score += 55
    elif item.get("Operating Loss Improving") is False:
        score -= 80

    if item.get("Pretax Loss Improving") is True:
        score += 25
    elif item.get("Pretax Loss Improving") is False:
        score -= 35

    revenue_growth = item.get("Revenue Growth")
    if revenue_growth is not None:
        if revenue_growth >= 15:
            score += 30
        elif revenue_growth >= 10:
            score += 55
        elif revenue_growth >= 0:
            score += 10
        elif revenue_growth < -10:
            score -= 90
        else:
            score -= 40

    psr = item.get("PSR")
    if psr is not None:
        if psr < 0.5:
            score += 25
        elif psr < 1:
            score += 10

    price_location = item.get("Price Location")
    if price_location is not None:
        if price_location < 0.15:
            score += 75
        elif price_location < 0.3:
            score += 20
        elif price_location > 0.7:
            score -= 100
        elif price_location > 0.5:
            score -= 45

    if item.get("Net Loss Improving") is True:
        score += 35
    elif item.get("Net Loss Improving") is False:
        score -= 90

    history = item.get("Net Income History") or []
    latest = history[0] if len(history) > 0 else item.get("Net Income")
    previous = history[1] if len(history) > 1 else None
    risk = net_income_risk(latest, previous)

    blocks = list(item.get("Blocks") or [])
    notes = list(item.get("Notes") or [])

    if risk == "profit_to_loss":
        score -= 160
        if "黒字から赤字転落" not in blocks:
            blocks.append("黒字から赤字転落")
        if "純利益悪化" not in notes:
            notes.append("純利益悪化")
    elif risk == "deeper_loss":
        score -= 120
        if "純損失拡大" not in blocks:
            blocks.append("純損失拡大")
        if "純損失拡大" not in notes:
            notes.append("純損失拡大")
    elif risk == "profit_decline":
        score -= 60
        if "純利益減少" not in notes:
            notes.append("純利益減少")

    if revenue_growth is not None and revenue_growth < -10 and "売上悪化" not in blocks:
        blocks.append("売上悪化")
    if price_location is not None and price_location > 0.7 and "高値圏" not in blocks:
        blocks.append("高値圏")

    if blocks and risk in {"profit_to_loss", "deeper_loss"}:
        score -= 50
        item["Action"] = "監視（除外条件あり）"
    elif blocks:
        score -= 50

    exit_plan = exit_plan_values(item.get("Current Price"), price_location, revenue_growth, blocks, risk)
    score += exit_plan["Exit Score"]

    item["Score"] = round(score, 1)
    item["Entry Score"] = round(score, 1)
    item["Blocks"] = blocks
    item["Notes"] = notes
    item["Net Income Risk"] = risk
    item["Safety Version"] = SAFETY_VERSION
    item.update(exit_plan)
    return item


def latest_statement_period(statement):
    if statement is None or statement.empty:
        return None
    try:
        columns = list(statement.columns)
    except Exception:
        return None
    if not columns:
        return None
    latest = columns[0]
    try:
        if hasattr(latest, "to_pydatetime"):
            return latest.to_pydatetime().date().isoformat()
        parsed = pd.to_datetime(latest, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()
    except Exception:
        return None


def days_since(date_text, now_jst):
    if not date_text:
        return None
    try:
        target = datetime.fromisoformat(date_text).date()
    except ValueError:
        return None
    return (now_jst.date() - target).days


def score_stock(row):
    ticker = f"{str(row['code']).strip()}.T"
    name = str(row.get("name", "")).strip()
    sector = str(row.get("sector", "")).strip()

    stock = yf.Ticker(ticker)
    try:
        income = stock.financials
        quarterly_income = stock.quarterly_financials
        cashflow = stock.cashflow
        balance = stock.balance_sheet
        prices = stock.history(period="2y", auto_adjust=False)
    except Exception as error:
        return {"Ticker": ticker, "Name": name, "Sector": sector, "Error": str(error)}

    if prices.empty or "Close" not in prices:
        return {"Ticker": ticker, "Name": name, "Sector": sector, "Error": "price data unavailable"}

    latest_quarter_period = latest_statement_period(quarterly_income)
    source_income = quarterly_income if quarterly_income is not None and not quarterly_income.empty else income

    revenues = find_statement_value(source_income, ["Total Revenue", "Operating Revenue"])
    net_incomes = find_statement_value(source_income, ["Net Income", "Net Income Common Stockholders"])
    operating_incomes = find_statement_value(source_income, ["Operating Income", "Operating Income Loss"])
    pretax_incomes = find_statement_value(source_income, ["Pretax Income", "Income Before Tax"])
    capex = find_statement_value(cashflow, ["Capital Expenditure", "Capital Expenditures"])
    total_assets = find_statement_value(balance, ["Total Assets"])

    latest_revenue = revenues[0] if len(revenues) > 0 else None
    previous_revenue = revenues[1] if len(revenues) > 1 else None
    latest_net_income = net_incomes[0] if len(net_incomes) > 0 else None
    previous_net_income = net_incomes[1] if len(net_incomes) > 1 else None
    latest_operating = operating_incomes[0] if len(operating_incomes) > 0 else None
    previous_operating = operating_incomes[1] if len(operating_incomes) > 1 else None
    latest_pretax = pretax_incomes[0] if len(pretax_incomes) > 0 else None
    previous_pretax = pretax_incomes[1] if len(pretax_incomes) > 1 else None

    current_price = clean_number(prices["Close"].iloc[-1])
    min_price = clean_number(prices["Close"].min())
    max_price = clean_number(prices["Close"].max())
    price_location = None
    if current_price is not None and min_price is not None and max_price not in (None, min_price):
        price_location = (current_price - min_price) / (max_price - min_price)

    market_cap = None
    try:
        market_cap = clean_number(stock.fast_info.get("marketCap"))
    except Exception:
        market_cap = None
    psr = market_cap / latest_revenue if market_cap and latest_revenue and latest_revenue > 0 else None

    revenue_growth = pct_change(latest_revenue, previous_revenue)
    net_loss_improving = loss_improving(latest_net_income, previous_net_income, latest_revenue, previous_revenue)
    operating_loss_improving = loss_improving(latest_operating, previous_operating, latest_revenue, previous_revenue)
    pretax_loss_improving = loss_improving(latest_pretax, previous_pretax, latest_revenue, previous_revenue)
    net_risk = net_income_risk(latest_net_income, previous_net_income)
    loss_margin = None
    if latest_net_income is not None and latest_revenue not in (None, 0):
        loss_margin = latest_net_income / abs(latest_revenue) * 100

    aggressive_reasons = []
    if len(capex) >= 2 and capex[0] is not None and capex[1] is not None:
        if abs(capex[0]) > abs(capex[1]) * 1.05:
            aggressive_reasons.append("CapEx Up")
    if len(total_assets) >= 2 and total_assets[0] is not None and total_assets[1] is not None:
        if total_assets[0] > total_assets[1] * 1.02:
            aggressive_reasons.append("Assets Up")

    score = 0
    notes = []
    blocks = []

    two_year_net_loss = (
        latest_net_income is not None
        and previous_net_income is not None
        and latest_net_income < 0
        and previous_net_income < 0
    )
    if two_year_net_loss:
        score += 60
        notes.append("2期連続赤字")

    if latest_operating is not None and latest_operating < 0:
        score += 10
        notes.append("営業赤字")
    if operating_loss_improving is True:
        score += 55
        notes.append("営業赤字縮小")
    elif operating_loss_improving is False:
        score -= 80
        blocks.append("営業赤字拡大")

    if pretax_loss_improving is True:
        score += 25
        notes.append("経常/税前赤字縮小")
    elif pretax_loss_improving is False:
        score -= 35

    if revenue_growth is not None:
        if revenue_growth >= 15:
            score += 30
            notes.append("売上大幅成長")
        elif revenue_growth >= 10:
            score += 55
            notes.append("売上成長")
        elif revenue_growth >= 0:
            score += 10
            notes.append("売上維持")
        elif revenue_growth < -10:
            score -= 90
            blocks.append("売上悪化")
        else:
            score -= 40

    if psr is not None:
        if psr < 0.5:
            score += 25
            notes.append("PSR低位")
        elif psr < 1:
            score += 10

    if price_location is not None:
        if price_location < 0.15:
            score += 75
            notes.append("底値圏")
        elif price_location < 0.3:
            score += 20
        elif price_location > 0.7:
            score -= 100
            blocks.append("高値圏")
            notes.append("高値圏・押し目待ち")
        elif price_location > 0.5:
            score -= 45

    if net_loss_improving is True:
        score += 35
        notes.append("純損失縮小")
    elif net_loss_improving is False:
        score -= 90

    if net_risk == "profit_to_loss":
        score -= 160
        blocks.append("黒字から赤字転落")
        notes.append("純利益悪化")
    elif net_risk == "deeper_loss":
        score -= 120
        blocks.append("純損失拡大")
        notes.append("純損失拡大")
    elif net_risk == "profit_decline":
        score -= 60
        notes.append("純利益減少")

    if "CapEx Up" in aggressive_reasons:
        score += 10
        notes.append("赤字下の投資")
    if "Assets Up" in aggressive_reasons:
        score += 5
        notes.append("資産増")

    if blocks:
        score -= 50

    exit_plan = exit_plan_values(current_price, price_location, revenue_growth, blocks, net_risk)
    score += exit_plan["Exit Score"]

    if blocks:
        action = "監視（除外条件あり）"
    elif price_location is not None and price_location > 0.7 and score >= 110:
        action = "押し目待ち"
    elif score >= 150:
        action = "買い候補 強"
    elif score >= 110:
        action = "買い候補"
    elif score >= 80:
        action = "監視"
    else:
        action = "パス"

    if two_year_net_loss:
        status = "**2-YR LOSS**"
    elif latest_net_income is not None and latest_net_income < 0:
        status = "Red Ink (1yr)"
    elif latest_net_income is not None and latest_net_income >= 0:
        status = "Profitable"
    else:
        status = "-"

    return {
        "Ticker": ticker,
        "Name": name,
        "Sector": sector,
        "Current Price": current_price,
        "Market Cap": market_cap,
        "Score": round(score, 1),
        "Entry Score": round(score, 1),
        "Action": action,
        "Status": status,
        "Sector Status": "-",
        "Price Location": round(price_location, 3) if price_location is not None else None,
        "Revenue Growth": round(revenue_growth, 1) if revenue_growth is not None else None,
        "Revenue Growth Label": growth_label(revenue_growth),
        "Loss Margin Improving": net_loss_improving,
        "Loss Margin": round(loss_margin, 1) if loss_margin is not None else None,
        "Is Aggressive": bool(aggressive_reasons),
        "Aggressive Reason": "/".join(aggressive_reasons),
        "PSR Rank": None,
        "PSR": round(psr, 3) if psr is not None else None,
        "Net Income": latest_net_income,
        "Net Income History": net_incomes[:5],
        "RSI": None,
        "Operating Income": latest_operating,
        "Pretax Income": latest_pretax,
        "Operating Loss Improving": operating_loss_improving,
        "Pretax Loss Improving": pretax_loss_improving,
        "Net Loss Improving": net_loss_improving,
        "Net Income Risk": net_risk,
        "Latest Quarter Period": latest_quarter_period,
        "Notes": notes,
        "Blocks": blocks,
        "Safety Version": SAFETY_VERSION,
        **exit_plan,
    }


def dashboard_url():
    return os.getenv("DASHBOARD_URL") or DEFAULT_REPORT_URL


def append_dashboard_link(lines):
    url = dashboard_url()
    if url:
        lines.extend(["", f"GitHubサイト: {url}"])


def build_weekly_message(report, top_n, earnings_window_days):
    generated_at = report["generated_at_jst"]
    reversal_rows = report["rankings"][:top_n]
    current_rows = report.get("current_rankings", [])[:top_n]
    lines = [f"隔週ランキング確認 ({generated_at})", ""]
    if not reversal_rows and not current_rows:
        lines.append("ランキングを作成できませんでした。Actionsのログを確認してください。")
        return "\n".join(lines)

    def append_rows(title, rows):
        lines.extend([title])
        for item in rows[:5]:
            price_location = item.get("Price Location")
            price_text = "-" if price_location is None else f"{price_location * 100:.0f}%"
            growth = item.get("Revenue Growth")
            growth_text = "-" if growth is None else f"{growth:+.1f}%"
            notes = " / ".join((item.get("Current Version Notes") or item.get("Notes") or [])[:3]) or "-"
            target = item.get("Target Price 1")
            target_text = "-" if target is None else f"{target:.0f}円"
            lines.append(
                f"{item['Rank']}. {item['Ticker']} {item['Name']} | {item['Action']} | "
                f"Score {item['Score']} | 位置 {price_text} | 売上 {growth_text} | +50% {target_text} | {notes}"
            )
        lines.append("")

    append_rows("現行版 Top5", current_rows)
    append_rows("反転狙い版 Top5", reversal_rows)
    earnings_rows = [
        item
        for item in reversal_rows
        if item.get("Recent Earnings Data") is True
    ]
    if earnings_rows:
        lines.extend(["", f"決算チェック優先（上位{top_n}位以内・直近{earnings_window_days}日以内の四半期データ）"])
        for item in earnings_rows:
            period = item.get("Latest Quarter Period") or "-"
            lines.append(
                f"・#{item['Rank']} {item['Ticker']} {item['Name']} | {item['Action']} | "
                f"Score {item['Score']} | 最新期 {period}"
            )
    append_dashboard_link(lines)
    return "\n".join(lines)


def build_earnings_message(report, earnings_rows):
    generated_at = report["generated_at_jst"]
    lines = [f"決算チェック通知 ({generated_at})", ""]
    if not earnings_rows:
        lines.append("上位10位以内で新しく決算データが更新された銘柄はありません。")
        return "\n".join(lines)

    lines.append("ランキング上位10位以内で、前回チェック時から決算データが更新された銘柄です。")
    for item in earnings_rows:
        period = item.get("Latest Quarter Period") or "-"
        price_location = item.get("Price Location")
        price_text = "-" if price_location is None else f"{price_location * 100:.0f}%"
        growth = item.get("Revenue Growth")
        growth_text = "-" if growth is None else f"{growth:+.1f}%"
        notes = " / ".join(item.get("Notes", [])[:3]) or "-"
        lines.append(
            f"・#{item['Rank']} {item['Ticker']} {item['Name']} | {item['Action']} | "
            f"Score {item['Score']} | 最新期 {period} | 位置 {price_text} | 売上 {growth_text} | {notes}"
        )
    if report.get("current_rankings"):
        current_top = report["current_rankings"][0]
        lines.extend(["", f"現行版トップ: #{current_top['Rank']} {current_top['Ticker']} {current_top['Name']} | Score {current_top['Score']}"])
    append_dashboard_link(lines)
    return "\n".join(lines)


def load_state(path):
    state_path = Path(path)
    if not state_path.exists():
        return {"tickers": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"tickers": {}}


def load_existing_report(path):
    report_path = Path(path)
    if not report_path.exists():
        return {}
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def ticker_from_row(row):
    code = str(row.get("code", "")).strip()
    if not code:
        return ""
    return code if code.endswith(".T") else f"{code}.T"


def select_refresh_rows(rows, state, chunk_size):
    if chunk_size <= 0 or chunk_size >= len(rows):
        return rows, 0, len(rows), 0

    start = int(state.get("refresh_cursor", 0) or 0) % len(rows)
    selected = []
    for offset in range(chunk_size):
        selected.append(rows[(start + offset) % len(rows)])
    next_cursor = (start + chunk_size) % len(rows)
    return selected, start, len(selected), next_cursor


def merge_rankings(existing_report, fetched_rankings, now_utc, now_jst):
    merged = {}
    for item in existing_report.get("rankings", []):
        ticker = item.get("Ticker")
        if ticker:
            merged[ticker] = item

    refreshed_utc = now_utc.isoformat()
    refreshed_jst = now_jst.strftime("%Y-%m-%d %H:%M:%S %Z")
    for item in fetched_rankings:
        item["Last Successful Refresh UTC"] = refreshed_utc
        item["Last Successful Refresh JST"] = refreshed_jst
        merged[item["Ticker"]] = item

    rankings = [apply_stored_safety_guard(item) for item in merged.values()]
    rankings.sort(key=lambda item: item.get("Score", 0), reverse=True)
    for index, item in enumerate(rankings, start=1):
        item["Rank"] = index
    return rankings


def detect_top_earnings_updates(rankings, top_n, state):
    previous = state.get("tickers", {}) if isinstance(state, dict) else {}
    if not previous:
        return []

    updates = []
    for item in rankings[:top_n]:
        ticker = item.get("Ticker")
        latest_period = item.get("Latest Quarter Period")
        previous_period = previous.get(ticker, {}).get("Latest Quarter Period")
        if ticker and latest_period and previous_period and latest_period != previous_period:
            updates.append(
                {
                    **item,
                    "Previous Quarter Period": previous_period,
                }
            )
    return updates


def detect_top_rank_changes(rankings, top_n, state, state_key="tickers"):
    previous = state.get(state_key, {}) if isinstance(state, dict) else {}
    if not previous:
        return {"entered": [], "exited": []}

    previous_top = {
        ticker: data
        for ticker, data in previous.items()
        if data.get("Rank") is not None and data.get("Rank") <= top_n
    }
    current_top = {
        item.get("Ticker"): item
        for item in rankings[:top_n]
        if item.get("Ticker")
    }
    entered = []
    for item in rankings[:top_n]:
        ticker = item.get("Ticker")
        if not ticker:
            continue
        previous_rank = previous.get(ticker, {}).get("Rank")
        if previous_rank is None or previous_rank > top_n:
            entered.append({**item, "Previous Rank": previous_rank})
    exited = [
        {
            "Ticker": ticker,
            "Previous Rank": data.get("Rank"),
            "Previous Score": data.get("Score"),
        }
        for ticker, data in previous_top.items()
        if ticker not in current_top
    ]
    exited.sort(key=lambda item: item.get("Previous Rank") or 999)
    return {"entered": entered, "exited": exited}


def build_top_rank_change_message(report, changes, top_n):
    generated_at = report["generated_at_jst"]
    lines = [f"現行版ランキング上位{top_n}の顔ぶれが変わりました ({generated_at})", ""]
    entered = changes.get("entered", [])
    exited = changes.get("exited", [])
    if entered:
        lines.append("入ってきた銘柄")
    for item in entered:
        previous_rank = item.get("Previous Rank")
        previous_text = "圏外" if previous_rank is None else f"#{previous_rank}"
        price_location = item.get("Price Location")
        price_text = "-" if price_location is None else f"{price_location * 100:.0f}%"
        growth = item.get("Revenue Growth")
        growth_text = "-" if growth is None else f"{growth:+.1f}%"
        target = item.get("Target Price 1")
        target_text = "-" if target is None else f"{target:.0f}円"
        notes = " / ".join(item.get("Notes", [])[:3]) or "-"
        lines.append(
            f"・{previous_text} -> #{item['Rank']} {item['Ticker']} {item['Name']} | {item['Action']} | "
            f"Score {item['Score']} | 位置 {price_text} | 売上 {growth_text} | +50% {target_text} | {notes}"
        )
    if exited:
        lines.extend(["", "外れた銘柄"])
        for item in exited:
            score = item.get("Previous Score")
            score_text = "-" if score is None else score
            lines.append(f"・#{item['Previous Rank']} {item['Ticker']} | 前回Score {score_text}")
    append_dashboard_link(lines)
    return "\n".join(lines)


def build_state(rankings, now_utc, existing_state=None, refresh_cursor=None, universe_count=None, current_rankings=None):
    state = {
        "updated_at_utc": now_utc.isoformat(),
        "tickers": {
            item["Ticker"]: {
                "Rank": item.get("Rank"),
                "Score": item.get("Score"),
                "Latest Quarter Period": item.get("Latest Quarter Period"),
            }
            for item in rankings
            if item.get("Ticker")
        },
        "current_tickers": {
            item["Ticker"]: {
                "Rank": item.get("Rank"),
                "Score": item.get("Score"),
                "Latest Quarter Period": item.get("Latest Quarter Period"),
            }
            for item in (current_rankings or [])
            if item.get("Ticker")
        },
    }
    if isinstance(existing_state, dict):
        for key in ("refresh_cursor", "universe_count"):
            if key in existing_state:
                state[key] = existing_state[key]
    if refresh_cursor is not None:
        state["refresh_cursor"] = refresh_cursor
    if universe_count is not None:
        state["universe_count"] = universe_count
    return state


def save_state(path, state):
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_discord(message, webhook_url):
    max_len = 1900
    chunks = [message[i : i + max_len] for i in range(0, len(message), max_len)]
    for chunk in chunks:
        response = requests.post(webhook_url, json={"content": chunk}, timeout=20)
        response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description="Cloud ranking data updater and notifier")
    parser.add_argument("--tickers", default=str(DEFAULT_TICKERS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--earnings-window-days", type=int, default=120)
    parser.add_argument("--mode", choices=["refresh", "notify", "weekly", "earnings"], default="weekly")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE))
    parser.add_argument("--update-state", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Debug: limit number of tickers")
    parser.add_argument("--workers", type=int, default=2, help="Number of parallel ticker fetches")
    parser.add_argument("--chunk-size", type=int, default=450, help="Tickers to refresh per run. Use 0 for full refresh")
    parser.add_argument("--notify-new-top", type=int, default=0, help="Notify if the current-version top N membership changes after refresh")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(ZoneInfo("Asia/Tokyo"))
    generated_at_jst = now_jst.strftime("%Y-%m-%d %H:%M:%S %Z")
    state = load_state(args.state_file)
    existing_report = load_existing_report(args.output)

    if args.mode == "notify":
        if not existing_report.get("rankings"):
            raise RuntimeError("No saved ranking report found. Run refresh mode first.")
        rankings = existing_report.get("rankings", [])
        if not existing_report.get("current_rankings"):
            existing_report["current_rankings"] = build_current_rankings(rankings)[:200]
        for item in rankings:
            days = days_since(item.get("Latest Quarter Period"), now_jst)
            item["Days Since Latest Quarter"] = days
            item["Recent Earnings Data"] = days is not None and 0 <= days <= args.earnings_window_days
        existing_report["top_n"] = args.top
        existing_report["earnings_window_days"] = args.earnings_window_days
        discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
        if discord_webhook:
            send_discord(build_weekly_message(existing_report, args.top, args.earnings_window_days), discord_webhook)
        else:
            print("DISCORD_WEBHOOK_URL is not set. Notification skipped.")
        print("Weekly notification sent from saved ranking report. Data refresh skipped.")
        return

    df = pd.read_csv(args.tickers, names=["code", "name", "sector"], dtype={"code": str})
    if args.limit > 0:
        df = df.head(args.limit)

    fetched_rankings = []
    errors = []
    rows = [row for _, row in df.iterrows()]
    rows_to_fetch, refresh_start, refresh_count, next_cursor = select_refresh_rows(rows, state, args.chunk_size)
    workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(score_stock, row): row for row in rows_to_fetch}
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                item = future.result()
            except Exception as error:
                row = futures[future]
                code = str(row.get("code", "")).strip()
                item = {
                    "Ticker": f"{code}.T" if code and not code.endswith(".T") else code,
                    "Name": str(row.get("name", "")).strip(),
                    "Sector": str(row.get("sector", "")).strip(),
                    "Error": str(error),
                }
            if "Error" in item:
                errors.append(item)
            else:
                fetched_rankings.append(item)
            if index % 100 == 0:
                print(f"Fetched {index}/{len(rows_to_fetch)} tickers in this run...")

    rankings = merge_rankings(existing_report, fetched_rankings, now_utc, now_jst)
    for item in rankings:
        days = days_since(item.get("Latest Quarter Period"), now_jst)
        item["Days Since Latest Quarter"] = days
        item["Recent Earnings Data"] = days is not None and 0 <= days <= args.earnings_window_days

    current_rankings = build_current_rankings(rankings)[:200]
    earnings_updates = detect_top_earnings_updates(rankings, args.top, state)
    top_rank_changes = (
        detect_top_rank_changes(current_rankings, args.notify_new_top, state, state_key="current_tickers")
        if args.notify_new_top > 0
        else {"entered": [], "exited": []}
    )

    report = {
        "generated_at_utc": now_utc.isoformat(),
        "generated_at_jst": generated_at_jst,
        "source": {
            "tickers_file": str(Path(args.tickers).name),
            "requested_tickers": len(df),
            "stored_successful_tickers": len(rankings),
            "refreshed_this_run": len(fetched_rankings),
            "failed_this_run": len(errors),
            "refresh_start": refresh_start,
            "refresh_count": refresh_count,
            "next_refresh_cursor": next_cursor,
            "failed_tickers": len(errors),
            "provider": "yfinance",
            "safety_version": SAFETY_VERSION,
            "ranking_note": "反転版は売上成長・底値圏・赤字縮小・Exit Score重視。現行版も同じデータから保守的に再採点。",
        },
        "rankings": rankings,
        "current_rankings": current_rankings,
        "earnings_updates": earnings_updates,
        "new_top_entries": top_rank_changes.get("entered", []),
        "top_rank_changes": top_rank_changes,
        "top_n": args.top,
        "earnings_window_days": args.earnings_window_days,
        "errors": errors[:50],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.update_state:
        save_state(
            args.state_file,
            build_state(
                rankings,
                now_utc,
                existing_state=state,
                refresh_cursor=next_cursor,
                universe_count=len(df),
                current_rankings=current_rankings,
            ),
        )

    if args.mode == "refresh":
        if top_rank_changes.get("entered") or top_rank_changes.get("exited"):
            message = build_top_rank_change_message(report, top_rank_changes, args.notify_new_top)
            print(message)
            discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
            if discord_webhook:
                send_discord(message, discord_webhook)
            else:
                print("\nNo notification secret configured. Set DISCORD_WEBHOOK_URL.")
        print(
            f"Ranking data refreshed: {len(fetched_rankings)}/{len(rows_to_fetch)} tickers this run, "
            f"{len(rankings)}/{len(df)} stored successful tickers at {generated_at_jst}. "
            f"Top {args.notify_new_top} changes: "
            f"+{len(top_rank_changes.get('entered', []))}/-{len(top_rank_changes.get('exited', []))}."
        )
        return

    if args.mode == "earnings":
        if not earnings_updates:
            print("No top-ranked earnings updates detected. Discord notification skipped.")
            return
        message = build_earnings_message(report, earnings_updates)
    else:
        message = build_weekly_message(report, args.top, args.earnings_window_days)

    print(message)

    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_webhook:
        send_discord(message, discord_webhook)
    else:
        print("\nNo notification secret configured. Set DISCORD_WEBHOOK_URL.")


if __name__ == "__main__":
    main()
