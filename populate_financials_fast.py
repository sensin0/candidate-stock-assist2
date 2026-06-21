import yfinance as yf
import pandas as pd
import sqlite3
import time
from tqdm import tqdm
from database_manager import get_connection
from concurrent.futures import ThreadPoolExecutor, as_completed

def fetch_and_update(ticker):
    try:
        # Use a new connection per thread if using SQLite? 
        # Actually SQLite doesn't like sharing connections across threads sometimes.
        # But we can just return data and write in main thread?
        # NO, returning data to main thread is safer for SQLite.
        
        stock = yf.Ticker(ticker)
        
        # Fast Info Fetch (Lazy)
        # We access properties to trigger fetch
        cf = stock.cashflow
        bs = stock.balance_sheet
        
        if cf.empty and bs.empty:
            return None

        records = []
        
        # 1. CapEx
        capex_series = None
        if not cf.empty:
            keys = [k for k in cf.index if 'Capital Expenditure' in str(k)]
            if keys: capex_series = cf.loc[keys[0]]

        # 2. Tangible Assets
        assets_series = None
        if not bs.empty:
            keys = [k for k in bs.index if 'Tangible Book Value' in str(k) or 'Net Tangible Assets' in str(k)]
            if keys: assets_series = bs.loc[keys[0]]

        all_dates = set()
        if capex_series is not None: all_dates.update(capex_series.index)
        if assets_series is not None: all_dates.update(assets_series.index)
        
        for date_val in all_dates:
            date_str = date_val.strftime('%Y-%m-%d')
            capex = None
            if capex_series is not None and date_val in capex_series:
                val = capex_series[date_val]
                if pd.notnull(val): capex = float(val)
            
            assets = None
            if assets_series is not None and date_val in assets_series:
                val = assets_series[date_val]
                if pd.notnull(val): assets = float(val)

            records.append((ticker, date_str, capex, assets))
            
        return records
        
    except Exception:
        return None

def populate_financials_fast():
    # 1. Get Tickers
    conn = get_connection()
    c = conn.cursor()
    print("Fetching Japanese stock tickers from DB...")
    c.execute("SELECT ticker FROM tickers_master WHERE ticker IS NOT NULL AND ticker != ''")
    tickers = [r[0] for r in c.fetchall()]
    conn.close() # Close mainly, we will open in loop for writes
    
    print(f"Starting Multi-threaded Fetch for {len(tickers)} tickers (Workers=30)...")
    
    # 2. Parallel Fetch
    results = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        future_to_ticker = {executor.submit(fetch_and_update, t): t for t in tickers}
        
        # Batch write to DB
        conn_write = get_connection()
        c_write = conn_write.cursor()
        
        batch_size = 50
        batch_data = []
        count = 0
        
        for future in tqdm(as_completed(future_to_ticker), total=len(tickers)):
            data = future.result()
            if data:
                batch_data.extend(data)
                
            if len(batch_data) >= batch_size:
                # Flush
                c_write.executemany("""
                    UPDATE financials 
                    SET capital_expenditure = ?, tangible_assets = ?
                    WHERE ticker = ? AND period_end = ?
                """, [(r[2], r[3], r[0], r[1]) for r in batch_data])
                
                # Also try insert if not affected? SQLite executeMany doesn't return rows affected easily per row.
                # Simplified: Just Upsert with specific logic is hard with executemany.
                # Actually, standardizing on upsert:
                c_write.executemany("""
                    INSERT INTO financials (ticker, period_end, capital_expenditure, tangible_assets)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(ticker, period_end, is_prediction) DO UPDATE SET
                    capital_expenditure=excluded.capital_expenditure,
                    tangible_assets=excluded.tangible_assets
                """, [(r[0], r[1], r[2], r[3]) for r in batch_data])
                
                conn_write.commit()
                batch_data = []
                
        # Final Flush
        if batch_data:
            c_write.executemany("""
                INSERT INTO financials (ticker, period_end, capital_expenditure, tangible_assets)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker, period_end, is_prediction) DO UPDATE SET
                capital_expenditure=excluded.capital_expenditure,
                tangible_assets=excluded.tangible_assets
            """, [(r[0], r[1], r[2], r[3]) for r in batch_data])
            conn_write.commit()
            
        conn_write.close()
    
    print("Done.")

if __name__ == "__main__":
    populate_financials_fast()
