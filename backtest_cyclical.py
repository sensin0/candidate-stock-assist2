import sqlite3
import pandas as pd
import argparse
from datetime import datetime, timedelta
import numpy as np
from database_manager import get_connection

def run_backtest(db_path='stocks.db'):
    conn = get_connection()
    
    import json
    with open('target_sectors.json', 'r', encoding='utf-8') as f:
        TARGET_SECTORS = json.load(f)
    placeholders = ','.join(['?'] * len(TARGET_SECTORS))
    
    # 1. Fetch Processable Tickers (Filtered to Target Sectors)
    df_tickers = pd.read_sql(f"SELECT ticker, name, sector_name FROM tickers_master WHERE sector_name IN ({placeholders})", conn, params=TARGET_SECTORS)
    tickers = df_tickers['ticker'].tolist()
    ticker_names = dict(zip(df_tickers['ticker'], df_tickers['name']))
    
    results = []
    
    print(f"Backtesting on {len(tickers)} tickers...")
    
    # We simulate decisions made at specific dates.
    # Generate end-of-month (or start-of-month) dates from 2021-01-01 to roughly 6 months ago
    
    # Evaluation Points (Hypothetical entry dates)
    entry_dates = []
    current_date = datetime(2021, 1, 1)
    end_date = datetime.now() - timedelta(days=180) # Stop 6 months ago to allow for some return measurement
    
    while current_date <= end_date:
        entry_dates.append(current_date.strftime('%Y-%m-%d'))
        # Add 1 month
        if current_date.month == 12:
            current_date = current_date.replace(year=current_date.year + 1, month=1)
        else:
            current_date = current_date.replace(month=current_date.month + 1)
    
    for ticker in tickers:
        try:
            # Fetch all needed data for ticker
            df_fin = pd.read_sql("SELECT * FROM financials WHERE ticker = ? ORDER BY period_end ASC", conn, params=(ticker,))
            df_prices = pd.read_sql("SELECT date, close, high FROM prices WHERE ticker = ? ORDER BY date ASC", conn, params=(ticker,))
            
            if df_fin.empty or df_prices.empty:
                continue
                
            # Convert dates
            df_prices['date'] = pd.to_datetime(df_prices['date'])
            df_prices.set_index('date', inplace=True)
            
            # For each entry date, check if "Signal" existed
            for entry_date_str in entry_dates:
                entry_date = pd.Timestamp(entry_date_str)
                
                # DATA AVAILABILITY CHECK:
                # We can ONLY use financial data released BEFORE entry_date.
                # Usually financials are released 2-3 months after period_end.
                # We assume period_end + 3 months is safe.
                
                # Filter financials available at entry_date
                # period_end < entry_date - 90 days?
                # Actually, simply: period_end <= entry_date - 60 days
                cutoff_date = entry_date - timedelta(days=60)
                available_fin = df_fin[pd.to_datetime(df_fin['period_end']) <= cutoff_date]
                
                if len(available_fin) < 2:
                    continue
                    
                # Signal Check 1: 2-Year Consecutive Loss
                # Get last 2 records
                last_2 = available_fin.iloc[-2:]
                ni_1 = last_2.iloc[-1]['net_income']
                ni_2 = last_2.iloc[-2]['net_income']
                
                if ni_1 is None or ni_2 is None: continue
                
                is_2yr_loss = (ni_1 < 0 and ni_2 < 0)
                
                if not is_2yr_loss:
                    continue
                
                # Signal Check 2: Revenue Growth (YoY)
                rev_1 = last_2.iloc[-1]['revenue']
                rev_2 = last_2.iloc[-2]['revenue']
                rev_growth = None
                rev_growth_label = None
                if rev_1 and rev_2 and rev_2 != 0:
                    rev_growth = (rev_1 - rev_2) / abs(rev_2) * 100  # in %
                    if rev_growth >= 20:
                        rev_growth_label = "爆発 (V字回復)"
                    elif rev_growth >= 10:
                        rev_growth_label = "確信 (BUY)"
                    elif rev_growth >= 5:
                        rev_growth_label = "兆し (Watch)"
                    elif rev_growth < -10:
                        rev_growth_label = "悪化中"
                    else:
                        rev_growth_label = "横ばい"
                
                # Signal Check 3: Aggressive Investment (CapEx / Assets)
                is_aggressive = False
                aggressive_reason = []
                if 'capital_expenditure' in available_fin.columns and len(available_fin) >= 2:
                    ce_1 = last_2.iloc[-1].get('capital_expenditure')
                    ce_2 = last_2.iloc[-2].get('capital_expenditure')
                    if ce_1 and ce_2 and ce_2 != 0:
                        if abs(ce_1) > abs(ce_2) * 1.05:
                            is_aggressive = True
                            aggressive_reason.append("CapEx Up")
                
                if 'tangible_assets' in available_fin.columns and len(available_fin) >= 2:
                    ta_1 = last_2.iloc[-1].get('tangible_assets')
                    ta_2 = last_2.iloc[-2].get('tangible_assets')
                    if ta_1 and ta_2 and ta_2 != 0:
                        if ta_1 > ta_2 * 1.02:
                            is_aggressive = True
                            aggressive_reason.append("Assets Up")
                
                # Signal Check 4: Loss Margin Improvement
                loss_margin_improving = None
                loss_margin = None
                if rev_1 and rev_1 != 0 and ni_1 is not None:
                    loss_margin = ni_1 / abs(rev_1) * 100  # in %
                if rev_1 and rev_2 and rev_1 != 0 and rev_2 != 0 and ni_1 is not None and ni_2 is not None:
                    lm_1 = ni_1 / abs(rev_1)
                    lm_2 = ni_2 / abs(rev_2)
                    loss_margin_improving = lm_1 > lm_2  # closer to 0 = better
                    
                # Signal Check 2: Price Location (at Entry Date)
                # Look at 2 years prior to Entry Date
                two_years_prior = entry_date - timedelta(days=365*2)
                
                hist_prices = df_prices[(df_prices.index >= two_years_prior) & (df_prices.index <= entry_date)]
                if hist_prices.empty: continue
                
                current_price = 0
                # Get exact price at entry date or nearest before
                try:
                    loc_idx = df_prices.index.get_indexer([entry_date], method='pad')[0]
                    if loc_idx < 0: continue
                    current_price = df_prices.iloc[loc_idx]['close']
                except:
                    continue
                    
                min_price = hist_prices['close'].min()
                max_price = hist_prices['close'].max()
                
                price_loc = (current_price - min_price) / (max_price - min_price) if max_price != min_price else 0.5
                
                # ENTRY CONDITION
                # 2-Yr Loss AND Price Location < 0.2 (Bottom 20%)
                if price_loc < 0.3: # Relaxed slightly for backtest to get samples
                    
                    # BUY SIMULATION
                    buy_price = current_price
                    
                    # Track Future Performance (up to today)
                    future_prices = df_prices[df_prices.index > entry_date]
                    
                    if future_prices.empty:
                        max_return = 0
                        days_held = 0
                    else:
                        # Find Max Price in next 2 years (or until now)
                        end_window = entry_date + timedelta(days=365*2)
                        window_prices = future_prices[future_prices.index <= end_window]
                        
                        if window_prices.empty:
                             max_price_in_window = buy_price
                        else:
                             max_price_in_window = window_prices['high'].max()
                        
                        max_return = (max_price_in_window - buy_price) / buy_price
                        
                        # Current Return (checking if it crashed)
                        # ...
                    
                    # Compute combined score (refined via multibagger analysis)
                    # Price Location (底値圏ほどMB率高い)
                    if price_loc < 0.1:
                        entry_score = 25 + (1.0 - price_loc) * 100  # Bottom 10% bonus
                    else:
                        entry_score = 10 + (1.0 - price_loc) * 100  # Standard bottom bonus

                    # Revenue Growth (15-20%ゾーンが最強: MB率50%)
                    if rev_growth is not None:
                        if rev_growth >= 20:
                            entry_score += 50   # MB率25%, avg 60.4%
                        elif rev_growth >= 15:
                            entry_score += 70   # MB率50%, avg 140.8% — 最強
                        elif rev_growth >= 10:
                            entry_score += 35   # transition zone
                        elif rev_growth >= 5:
                            entry_score += 5
                        elif rev_growth < -10:
                            entry_score -= 60
                        elif rev_growth < 0:
                            entry_score -= 20

                    # Aggressive Investment (reduced weight)
                    if is_aggressive:
                        entry_score += 15  # was 50: backtest shows minimal lift

                    # Loss margin absolute value bonus
                    if loss_margin is not None:
                        if loss_margin > -3:
                            entry_score += 30  # near breakeven
                        elif loss_margin > -5:
                            entry_score += 10

                    # Loss Margin Direction (最強の単独指標: 改善42.9% vs 悪化7.1%)
                    if loss_margin_improving is True:
                        entry_score += 50  # 最重要
                    elif loss_margin_improving is False:
                        entry_score -= 50

                    # Combo Bonuses
                    if rev_growth is not None and rev_growth >= 10:
                        if loss_margin_improving is True:
                            entry_score += 30  # combo MB率50%
                        if price_loc < 0.15:
                            entry_score += 20  # combo MB率50%

                    # Rank quality guards: worsening businesses and high-price entries
                    # should not sit near the top even if they have one attractive signal.
                    if price_loc > 0.7:
                        entry_score -= 30
                    elif price_loc > 0.5:
                        entry_score -= 15
                    
                    results.append({
                        'Ticker': ticker,
                        'Name': ticker_names.get(ticker, ''),
                        'Entry Date': entry_date_str,
                        'Buy Price': buy_price,
                        'Net Income (Latest)': ni_1,
                        'Net Income (Prev)': ni_2,
                        'Revenue Growth': round(rev_growth, 1) if rev_growth is not None else None,
                        'Revenue Growth Label': rev_growth_label,
                        'Is Aggressive': is_aggressive,
                        'Aggressive Reason': '/'.join(aggressive_reason),
                        'Loss Margin Improving': loss_margin_improving,
                        'Loss Margin': round(loss_margin, 1) if loss_margin is not None else None,
                        'Price Location': price_loc,
                        'Max Price (2yr)': max_price_in_window,
                        'Max Return': max_return,
                        '1yr Return': (window_prices[window_prices.index <= entry_date + timedelta(days=365)]['high'].max() - buy_price) / buy_price if not window_prices[window_prices.index <= entry_date + timedelta(days=365)].empty else 0,
                        '2yr Return': max_return,
                        'Entry Score': entry_score
                    })
                    
        except Exception as e:
            # print(f"Error {ticker}: {e}")
            pass
            
    conn.close()
    
    # Organize by Month (Year-Month)
    results_by_month = {}
    for res in results:
        # 'Entry Date' is 'YYYY-MM-DD', so slice to 'YYYY-MM'
        month_key = res['Entry Date'][:7] 
        if month_key not in results_by_month:
            results_by_month[month_key] = []
        results_by_month[month_key].append(res)
        
    return pd.DataFrame(results), results_by_month

def generate_report(df):
    if df.empty:
        print("No signals found in the backtest period.")
        return
        
    print(f"Signals Found: {len(df)}")
    
    # Metrics
    win_rate_50 = len(df[df['Max Return'] >= 0.5]) / len(df)
    win_rate_100 = len(df[df['Max Return'] >= 1.0]) / len(df)
    avg_return = df['Max Return'].mean()
    
    print("="*30)
    print(" BACKTEST RESULTS (2021-2024)")
    print("="*30)
    print(f"Total Trades: {len(df)}")
    print(f"Win Rate (>+50%): {win_rate_50*100:.1f}%")
    print(f"Doubaggers (>+100%): {win_rate_100*100:.1f}%")
    print(f"Avg Max Return: {avg_return*100:.1f}%")
    print("-" * 30)
    
    # Top Performers
    print("\nTop Performers (by Entry Score):")
    print(df.sort_values(by='Entry Score', ascending=False).head(10)[['Ticker', 'Entry Date', 'Buy Price', 'Revenue Growth Label', 'Is Aggressive', 'Max Return', 'Entry Score']])

if __name__ == "__main__":
    df_res, dict_res = run_backtest()
    generate_report(df_res)
    
    # Save JSON for Dashboard
    import json
    import os
    
    output_path = 'web-dashboard/public/backtest_results.json'
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Convert for JSON serialization (handle dates/numpy types)
    # Simple approach: utilize pandas json conversion per year
    final_json = {}
    for year, rows in dict_res.items():
        # Convert list of dicts to localized logic or just dump
        # We need to ensure types are valid
        final_json[year] = rows
        
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, default=str)
        
    print(f"Backtest JSON saved to {output_path}")
