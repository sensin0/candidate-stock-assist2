import sqlite3
import pandas as pd
import os

DB_PATH = 'stocks.db'

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    return conn

def init_db():
    """Initialize the database with the required schema."""
    conn = get_connection()
    cursor = conn.cursor()
    
    # Tickers Master Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tickers_master (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            sector_code TEXT,
            sector_name TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Financials Table (Quarterly/Yearly)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            period_end DATE,
            revenue REAL,
            net_income REAL,
            capital_expenditure REAL,
            tangible_assets REAL,
            eps REAL,
            bps REAL,
            total_assets REAL,
            net_assets REAL,
            is_prediction BOOLEAN DEFAULT 0,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, period_end, is_prediction)
        )
    ''')

    # Migration for existing tables
    try:
        cursor.execute("SELECT capital_expenditure FROM financials LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating financials table: Adding capital_expenditure...")
        cursor.execute("ALTER TABLE financials ADD COLUMN capital_expenditure REAL")
    
    try:
        cursor.execute("SELECT tangible_assets FROM financials LIMIT 1")
    except sqlite3.OperationalError:
        print("Migrating financials table: Adding tangible_assets...")
        cursor.execute("ALTER TABLE financials ADD COLUMN tangible_assets REAL")

    # Prices Table (Daily)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            date DATE,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            UNIQUE(ticker, date)
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

def upsert_tickers(df_tickers):
    """
    Update tickers master table.
    df_tickers should have: ticker, name, market, sector_code, sector_name
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    for _, row in df_tickers.iterrows():
        cursor.execute('''
            INSERT INTO tickers_master (ticker, name, market, sector_code, sector_name)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name,
            market=excluded.market,
            sector_code=excluded.sector_code,
            sector_name=excluded.sector_name,
            updated_at=CURRENT_TIMESTAMP
        ''', (row['ticker'], row['name'], row.get('market'), row.get('sector_code'), row.get('sector_name')))
    
    conn.commit()
    conn.close()
    print(f"Upserted {len(df_tickers)} tickers.")

def get_all_tickers():
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM tickers_master", conn)
    conn.close()
    return df['ticker'].tolist()

if __name__ == "__main__":
    init_db()
