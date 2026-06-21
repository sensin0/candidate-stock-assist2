import yfinance as yf
import pandas as pd
import argparse
import os
from datetime import datetime, timedelta
import numpy as np
from database_manager import get_connection
from tqdm import tqdm
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_ticker_data_from_db(ticker_symbol):
    """
    Fetches data from local SQLite DB.
    """
    try:
        conn = get_connection()
        
        # 1. Basic Info & Ticker Master
        df_master = pd.read_sql("SELECT * FROM tickers_master WHERE ticker = ?", conn, params=(ticker_symbol,))
        if df_master.empty:
            conn.close()
            return None
            
        ticker_name = df_master.iloc[0]['name']
        sector_name = df_master.iloc[0]['sector_name']
        
        # 2. Financials (Annual)
        # Get all financials sorted by date desc
        df_fin = pd.read_sql("SELECT * FROM financials WHERE ticker = ? ORDER BY period_end DESC", conn, params=(ticker_symbol,))
        
        if df_fin.empty:
            conn.close()
            return None
            
        # Extract History (Last 5 records)
        net_incomes = df_fin['net_income'].tolist()[:5]
        revenues = df_fin['revenue'].tolist()[:5]
        
        # New: CapEx and Assets
        capex_hist = df_fin['capital_expenditure'].tolist()[:5] if 'capital_expenditure' in df_fin.columns else []
        assets_hist = df_fin['tangible_assets'].tolist()[:5] if 'tangible_assets' in df_fin.columns else []
        
        latest_rev = revenues[0] if revenues else None
        prev_rev = revenues[1] if len(revenues) > 1 else None
        latest_ni = net_incomes[0] if net_incomes else None
        prev_ni = net_incomes[1] if len(net_incomes) > 1 else None
        
        # Revenue YoY Growth Rate
        rev_growth = None
        rev_growth_label = None
        if latest_rev and prev_rev and prev_rev != 0:
            rev_growth = (latest_rev - prev_rev) / abs(prev_rev)  # e.g. 0.15 = +15%
            if rev_growth >= 0.20:
                rev_growth_label = "爆発 (V字回復)"
            elif rev_growth >= 0.10:
                rev_growth_label = "確信 (BUY)"
            elif rev_growth >= 0.05:
                rev_growth_label = "兆し (Watch)"
            elif rev_growth < -0.10:
                rev_growth_label = "悪化中"
            else:
                rev_growth_label = "横ばい"
        
        # Loss Margin Improvement (赤字率改善チェック)
        # NI/Revenue ratio: closer to 0 = better for loss-making companies
        loss_margin_improving = None  # None = no data, True = improving, False = worsening
        latest_loss_margin = None
        prev_loss_margin = None
        if latest_ni is not None and latest_rev and latest_rev != 0:
            latest_loss_margin = latest_ni / abs(latest_rev)  # e.g. -0.09 = -9%
        if prev_ni is not None and prev_rev and prev_rev != 0:
            prev_loss_margin = prev_ni / abs(prev_rev)
        
        if latest_loss_margin is not None and prev_loss_margin is not None:
            # Both are negative (loss-making). Closer to 0 = better.
            # "improving" means latest is closer to 0 (less negative)
            loss_margin_improving = latest_loss_margin > prev_loss_margin
        
        # CapEx Analysis (Aggressive Investment?)
        # CapEx is usually negative. More negative = More Investment.
        latest_capex = capex_hist[0] if capex_hist else 0
        prev_capex = capex_hist[1] if len(capex_hist) > 1 else 0
        
        latest_assets = assets_hist[0] if assets_hist else 0
        prev_assets = assets_hist[1] if len(assets_hist) > 1 else 0
        
        is_aggressive = False
        aggressive_reason = []
        
        # Check if CapEx increased (absolute value)
        if latest_capex and prev_capex:
            if abs(latest_capex) > abs(prev_capex) * 1.05: # >5% increase
                is_aggressive = True
                aggressive_reason.append("CapEx Up")
                
        # Check if Tangible Assets increased
        if latest_assets and prev_assets:
            if latest_assets > prev_assets * 1.02: # >2% increase (assets serve as base)
                is_aggressive = True
                aggressive_reason.append("Assets Up")
        
        # 3. Price History (Last 5 Years)
        # We need this for Price Location and Historical PSR
        five_years_ago = (datetime.now() - timedelta(days=365*5)).strftime('%Y-%m-%d')
        df_prices = pd.read_sql("SELECT * FROM prices WHERE ticker = ? AND date >= ? ORDER BY date ASC", conn, params=(ticker_symbol, five_years_ago))
        
        conn.close()
        
        if df_prices.empty:
            return None
            
        # Convert date to datetime
        df_prices['date'] = pd.to_datetime(df_prices['date'])
        df_prices.set_index('date', inplace=True)
        
        current_price = df_prices.iloc[-1]['close']
        
        current_psr = None
        hist_psr_values = []
        market_cap = None
        shares_outstanding = 0
        
        # ... (PSR Logic same as before)
        if market_cap and latest_rev:
             current_psr = market_cap / latest_rev
             
             # Calc history
             df_fin_sorted = df_fin.sort_values(by='period_end', ascending=True) # Oldest first
             
             for _, row in df_fin_sorted.iterrows():
                 rev = row['revenue']
                 date_str = row['period_end']
                 if not rev: continue
                 
                 # Find price at date
                 try:
                     ts = pd.Timestamp(date_str)
                     # Find nearest price in DB
                     idx = df_prices.index.get_indexer([ts], method='nearest')[0]
                     price_at_date = df_prices.iloc[idx]['close']
                     
                     # Est Market Cap
                     est_mcap = price_at_date * shares_outstanding
                     hist_psr = est_mcap / rev
                     hist_psr_values.append(hist_psr)
                 except:
                     pass
        
        psr_rank = None
        if current_psr and hist_psr_values:
            min_psr = min(hist_psr_values)
            max_psr = max(hist_psr_values)
            if max_psr != min_psr:
                psr_rank = (current_psr - min_psr) / (max_psr - min_psr)
            else:
                psr_rank = 0.5

        # Price Location
        two_year_ago = datetime.now() - timedelta(days=730)
        hist_2y = df_prices[df_prices.index > two_year_ago]
        price_location_score = None
        min_price_2y = 0
        max_price_2y = 0
        if not hist_2y.empty:
            min_price_2y = hist_2y['close'].min()
            max_price_2y = hist_2y['close'].max()
            if max_price_2y != min_price_2y:
                price_location_score = (current_price - min_price_2y) / (max_price_2y - min_price_2y)
            else:
                price_location_score = 0

        # RSI Calculation (14-day)
        rsi_value = None
        try:
            if not df_prices.empty and len(df_prices) > 14:
                delta = df_prices['close'].diff()
                up = delta.clip(lower=0)
                down = -1 * delta.clip(upper=0)
                ema_up = up.ewm(com=13, adjust=False).mean()
                ema_down = down.ewm(com=13, adjust=False).mean()
                rs = ema_up / ema_down
                rsi_series = 100 - (100 / (1 + rs))
                rsi_value = rsi_series.iloc[-1]
        except Exception:
            pass

        # Extract Weekly Historical Prices for Sparkline
        try:
            weekly_prices = df_prices['close'].resample('W').last().dropna().round(1).tolist()
        except Exception:
            weekly_prices = []

        return {
            'Ticker': ticker_symbol,
            'Name': ticker_name,
            'Current Price': current_price,
            'Market Cap': market_cap if market_cap else 0,
            'Revenue': latest_rev,
            'Net Income': latest_ni,
            'Prev Net Income': prev_ni,
            'Net Income History': net_incomes,
            'PSR': current_psr,
            'PSR Rank': psr_rank,
            'Min Price 2Y': min_price_2y,
            'Max Price 2Y': max_price_2y,
            'Price Location': price_location_score,
            'Entry Score': (1.0 - price_location_score) * 100 if price_location_score is not None else 0,
            'RSI': rsi_value,
            'PBR': 0,
            'Sector': sector_name,
            'Is Aggressive': is_aggressive,
            'Aggressive Reason': "/".join(aggressive_reason),
            'Revenue Growth': round(rev_growth * 100, 1) if rev_growth is not None else None,
            'Revenue Growth Label': rev_growth_label,
            'Loss Margin Improving': loss_margin_improving,
            'Loss Margin': round(latest_loss_margin * 100, 1) if latest_loss_margin is not None else None,
            'Historical Prices': weekly_prices
        }

    except Exception as e:
        print(f"Error processing {ticker_symbol} from DB: {e}")
        return None

def analyze_sector_sync(df_results):
    if 'Sector' not in df_results.columns:
        return df_results

    sector_stats = {}
    for sector, group in df_results.groupby('Sector'):
        total = len(group)
        ni = pd.to_numeric(group['Net Income'], errors='coerce').fillna(0)
        red_ink_count = len(ni[ni < 0])
        ratio = red_ink_count / total if total > 0 else 0
        
        status = "Boom" 
        if ratio >= 0.5:
            status = "Downturn"
        elif ratio > 0.2:
            status = "Mixed"
            
        sector_stats[sector] = status
        
    df_results['Sector Status'] = df_results['Sector'].map(sector_stats)
    return df_results

def main():
    parser = argparse.ArgumentParser(description='Advanced Cyclical Screener (DB Version)')
    parser.add_argument('--output', type=str, default='web-dashboard/public/cyclical_data.json', help='JSON output path')
    parser.add_argument('--skip-mcap', action='store_true', help='Skip yfinance market cap fetch (fastest mode)')
    args = parser.parse_args()

    import json
    with open('target_sectors.json', 'r', encoding='utf-8') as f:
        TARGET_SECTORS = json.load(f)
    placeholders = ','.join(['?'] * len(TARGET_SECTORS))
    query = f"SELECT ticker, sector_name FROM tickers_master WHERE sector_name IN ({placeholders})"
    
    conn = get_connection()
    df_tickers = pd.read_sql(query, conn, params=TARGET_SECTORS)
    conn.close()
    
    tickers = df_tickers['ticker'].tolist()
    print(f"Analyzing {len(tickers)} tickers from Database...")
    
    # Phase 1: DB processing (fast, no network)
    t_start = time.time()
    results = []
    
    for t_info in tqdm(tickers, desc="DB Analysis"):
        t_data = get_ticker_data_from_db(t_info)
        if t_data:
            results.append(t_data)
    
    t_db = time.time() - t_start
    print(f"DB Analysis: {len(results)} stocks in {t_db:.1f}s")
    
    if not results:
        print("No data found in DB.")
        return
    
    # Phase 2: Market Cap fetch (parallel network calls)
    if not args.skip_mcap:
        t_start2 = time.time()
        
        def fetch_mcap(ticker_symbol):
            try:
                info = yf.Ticker(ticker_symbol).fast_info
                return ticker_symbol, info.get('marketCap', None)
            except:
                return ticker_symbol, None
        
        # Build lookup for fast update
        ticker_to_idx = {r['Ticker']: i for i, r in enumerate(results)}
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(fetch_mcap, r['Ticker']): r['Ticker'] for r in results}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Market Cap"):
                ticker_sym, mcap = future.result()
                idx = ticker_to_idx.get(ticker_sym)
                if idx is not None and mcap:
                    r = results[idx]
                    r['Market Cap'] = mcap
                    current_price = r['Current Price']
                    shares = mcap / current_price if current_price else 0
                    latest_rev = r['Revenue']
                    if latest_rev and latest_rev > 0:
                        r['PSR'] = mcap / latest_rev
                        # Recalc PSR Rank would need hist prices - skip for speed
        
        t_mcap = time.time() - t_start2
        print(f"Market Cap: {t_mcap:.1f}s ({len(futures)} tickers, 20 threads)")
    else:
        print("Skipping market cap fetch (--skip-mcap)")
        
    df = pd.DataFrame(results)
    
    # Apply Sector Analysis
    df = analyze_sector_sync(df)
    
    # Apply Scoring Logic
    def determine_status_and_action(row):
        ni_hist = row['Net Income History'] 
        psr_rank = row['PSR Rank']
        price_loc = row['Price Location']
        sector_status = row.get('Sector Status', 'Unknown')
        rev_growth = row.get('Revenue Growth', None)  # Already in %
        is_aggressive = row.get('Is Aggressive', False)
        loss_margin_improving = row.get('Loss Margin Improving', None)
        
        is_2yr_loss = False
        if len(ni_hist) >= 2:
            if ni_hist[0] is not None and ni_hist[1] is not None:
                if ni_hist[0] < 0 and ni_hist[1] < 0:
                    is_2yr_loss = True
        
        status = "Unknown"
        action = "Wait"
        score = 0
        
        if is_2yr_loss:
            status = "**2-YR LOSS**"
            score += 100
            
            buy_signals = []
            # Price Location (底値圏ほどMB率高い: Bottom10%=38.5%, 10-20%=22.2%, 20-30%=0%)
            if price_loc is not None and price_loc < 0.1:
                buy_signals.append("Price at Bottom 10%")
                score += 25
            elif price_loc is not None and price_loc < 0.2:
                buy_signals.append("Price at Bottom")
                score += 10
            elif price_loc is not None and price_loc > 0.7:
                buy_signals.append("高値圏・押し目待ち (>70%)")
                score -= 30
            elif price_loc is not None and price_loc > 0.5:
                buy_signals.append("中高値圏 (>50%)")
                score -= 15
            
            if psr_rank is not None and psr_rank < 0.2:
                buy_signals.append("PSR Historic Low")
                score += 10
                
            if sector_status == "Downturn":
                buy_signals.append("Sector Sync")
                score += 20
            
            if is_aggressive:
                buy_signals.append(f"AGGRESSIVE ({row.get('Aggressive Reason', '')})")
                score += 15  # Reduced: backtest shows 23% MB rate (not significantly better than non-aggressive 27%)
            
            # Revenue Growth Signal (15-20%ゾーンが最強: MB率50%, 平均リターン140.8%)
            if rev_growth is not None:
                if rev_growth >= 20:
                    buy_signals.append("売上爆発 (+20%↑)")
                    score += 50  # MB率25%, avg 60.4% — strong but less than 15-20%
                elif rev_growth >= 15:
                    buy_signals.append("売上急成長 (+15%↑)")
                    score += 70  # MB率50%, avg 140.8% — 最強の成長ゾーン
                elif rev_growth >= 10:
                    buy_signals.append("売上確信 (+10%↑)")
                    score += 35  # transition zone
                elif rev_growth >= 5:
                    buy_signals.append("売上兆し (+5%↑)")
                    score += 5
                elif rev_growth < -10:
                    buy_signals.append("売上悪化 (-10%↓)")
                    score -= 60
                elif rev_growth < 0:
                    buy_signals.append("売上減少")
                    score -= 20
            
            # Loss Margin Absolute Value (黒字まであと少し = 回復が近い)
            loss_margin = row.get('Loss Margin', None)
            if loss_margin is not None and loss_margin > -3:
                buy_signals.append("赤字率軽微 (>-3%)")
                score += 30
            elif loss_margin is not None and loss_margin > -5:
                buy_signals.append("赤字率小 (>-5%)")
                score += 10
            
            # Loss Margin Direction (最強の単独指標: 改善42.9% vs 悪化7.1% MB率)
            if loss_margin_improving is True:
                buy_signals.append("赤字率改善中 ↗")
                score += 50  # 最重要: MB率6倍の差
            elif loss_margin_improving is False:
                buy_signals.append("⚠️ 赤字率悪化中")
                score -= 50
            
            # Combo Bonuses (コンボでMB率50%到達)
            if rev_growth is not None and rev_growth >= 10:
                if loss_margin_improving is True:
                    buy_signals.append("🔥 売上成長+赤字改善コンボ")
                    score += 30  # combo MB率50%, avg 129.5%
                if price_loc is not None and price_loc < 0.15:
                    buy_signals.append("🔥 売上成長+底値圏コンボ")
                    score += 20  # combo MB率50%, avg 139.1%
            
            # Penalty: Sector is booming but company still in 2yr loss
            # = 業界好調なのに赤字 → 構造的問題（シクリカルではなく個社問題）
            if sector_status == "Boom":
                buy_signals.append("⚠️ セクター好調なのに赤字")
                score -= 40

            buy_blocks = []
            if loss_margin_improving is False:
                buy_blocks.append("赤字率悪化")
            if rev_growth is not None and rev_growth < -10:
                buy_blocks.append("売上悪化")

            if buy_blocks:
                action = "Watch (Blocked: " + "/".join(buy_blocks) + ")"
            elif price_loc is not None and price_loc > 0.7 and score >= 110:
                action = "Watch (Pullback)"
            elif score >= 150:
                action = "**BUY CANDIDATE** (STRONG)"
            elif score >= 110:
                action = "**BUY CANDIDATE**"
            elif score >= 80:
                action = "Watch (Wait for Profit Turn)"
            elif len(buy_signals) >= 1:
                action = "Watch (Wait for Price/Vol)"
            else:
                action = "Watch (Wait for Price/Vol)"
                
        elif row['Net Income'] is not None and row['Net Income'] < 0:
            status = "Red Ink (1yr)"
            action = "Watch (Wait for 2nd yr?)"
            score += 50
        elif row['Prev Net Income'] and row['Prev Net Income'] < 0:
            status = "Recovering"
            action = "Check Trend"
            score += 30
        else:
            status = "Profitable"
            action = "Pass"
            
        target_price_1 = None
        target_price_2 = None
        stop_loss = None
        target_price_per5 = None
        sell_signal = None
        sell_reason = None
        
        current_price = row.get('Current Price')
        market_cap = row.get('Market Cap', 0)
        
        # Calculate Target Price PER5 regardless of BUY status to help with Portfolio Tracking
        if market_cap and market_cap > 0 and current_price and current_price > 0:
            ni_hist = row.get('Net Income History') or []
            positive_ni = [ni for ni in ni_hist if ni and ni > 0]
            if positive_ni:
                target_mcap = max(positive_ni) * 5
                target_price_per5 = current_price * (target_mcap / market_cap)
                
                # Check for Sell Signal: Reached Target Price
                if current_price >= target_price_per5:
                    sell_signal = True
                    sell_reason = "目標株価(PER5倍)到達"
                    
        # Check for Sell Signal: Loss Margin Worsening
        if loss_margin_improving is False and row.get('Loss Margin') is not None and row.get('Loss Margin') < -5:
            # Only trigger sell if it's getting worse and is already bad (<-5%)
            if not sell_signal: # Prioritize target price reason
                sell_signal = True
                sell_reason = "業績悪化(赤字率拡大)"

        if "BUY CANDIDATE" in action and current_price is not None:
            target_price_1 = current_price * 1.5  # +50% target
            target_price_2 = current_price * 2.0  # +100% target
            stop_loss = current_price * 0.8       # -20% stop loss

        return pd.Series([status, action, score, target_price_1, target_price_2, stop_loss, target_price_per5, sell_signal, sell_reason])

    df[['Status', 'Action', 'Score', 'Target Price 1', 'Target Price 2', 'Stop Loss', 'Target Price PER5', 'Sell Signal', 'Sell Reason']] = df.apply(determine_status_and_action, axis=1)
    
    # Sort
    df = df.sort_values(by='Score', ascending=False)
    
    # Add Rank
    df['Rank'] = range(1, len(df) + 1)
    
    # Check for Sell Signal: Ranking Drop
    # (Simplified for now: If a stock is in portfolio.json and its rank > 50, trigger sell on dashboard)

    # JSON Output
    json_path = args.output
    # Ensure directory exists
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    df.to_json(json_path, orient='records', force_ascii=False, date_format='iso')
    print(f"JSON Data output to {json_path}")
    
    # MD Output (Simplified)
    # ... (Same as before, omitted for brevity if not strictly needed, but let's keep it consistent)
    # Be careful not to truncate in actual file write.
    # I will rely on JSON for the dashboard.
    
    print(f"Analysis Complete. {len(df)} companies processed.")

if __name__ == "__main__":
    main()
