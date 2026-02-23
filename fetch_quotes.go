package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

const (
	DBPath = "stocks.db?_pragma=journal_mode(WAL)&_pragma=busy_timeout(30000)"
	// Targeting main Cyclical sectors
	TargetSectorsQuery = `
		SELECT ticker FROM tickers_master 
		WHERE sector_name IN (
			'鉄鋼', '非鉄金属', '鉱業', '石油・石炭製品', 
			'ガラス・土石製品', 'ゴム製品', '化学', 'パルプ・紙', 
			'繊維製品', '海運業', '輸送用機器'
		)
	`
	// API Endpoint (Using Yahoo Finance Chart API - Unofficial but standard)
	// Query1 is cleaner. We need chart (price) and quoteSummary (financials).
	// https://query1.finance.yahoo.com/v8/finance/chart/7203.T?interval=1d&range=5y
)

func main() {
	db, err := sql.Open("sqlite", DBPath)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()

	// Limit concurrency for SQLite (modernc might be sensitive)
	db.SetMaxOpenConns(1)

	// 1. Get Target Tickers
	rows, err := db.Query(TargetSectorsQuery)
	if err != nil {
		log.Printf("Error querying tickers: %v", err)
		log.Println("Note: If sectors don't match, verify sector_name in DB.")
		return
	}
	defer rows.Close()

	var tickers []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err == nil {
			tickers = append(tickers, t)
		}
	}

	if len(tickers) == 0 {
		log.Println("No tickers found for target sectors in DB. Please run populate_db_tickers.py first.")
		return
	}

	fmt.Printf("Fetching data for %d cyclical tickers...\n", len(tickers))

	// 2. Concurrent Fetching
	var wg sync.WaitGroup
	semaphore := make(chan struct{}, 25) // Increased concurrency to 25 for faster fetching

	for i, ticker := range tickers {
		wg.Add(1)
		semaphore <- struct{}{}

		go func(t string, index int) {
			defer wg.Done()
			defer func() { <-semaphore }()

			// Friendly logging
			if index%50 == 0 {
				fmt.Printf("Progress: %d/%d\n", index, len(tickers))
			}

			// Fetch & Update
			if err := updateTickerData(db, t); err != nil {
				// Log strictly
				log.Printf("[%s] ERROR: %v", t, err)
			} else {
				// fmt.Printf("[%s] OK\n", t)
			}

			time.Sleep(200 * time.Millisecond)

		}(ticker, i)
	}

	wg.Wait()
	fmt.Println("All done.")
}

// --- Data Structures for JSON Decoding ---
// Simplified structures for what we need.

type ChartResponse struct {
	Chart struct {
		Result []struct {
			Meta struct {
				Currency           string  `json:"currency"`
				Symbol             string  `json:"symbol"`
				RegularMarketPrice float64 `json:"regularMarketPrice"`
			} `json:"meta"`
			Timestamp  []int64 `json:"timestamp"`
			Indicators struct {
				Quote []struct {
					Open   []float64 `json:"open"`
					High   []float64 `json:"high"`
					Low    []float64 `json:"low"`
					Close  []float64 `json:"close"`
					Volume []int64   `json:"volume"`
				} `json:"quote"`
			} `json:"indicators"`
		} `json:"result"`
		Error interface{} `json:"error"`
	} `json:"chart"`
}

// QuoteSummary for Financials
// https://query2.finance.yahoo.com/v10/finance/quoteSummary/7203.T?modules=incomeStatementHistory,cashflowStatementHistory,balanceSheetHistory
type FinancialsResponse struct {
	QuoteSummary struct {
		Result []struct {
			IncomeStatementHistory struct {
				IncomeStatementHistory []struct {
					EndDate struct {
						Fmt string `json:"fmt"`
					} `json:"endDate"`
					TotalRevenue struct {
						Raw int64 `json:"raw"`
					} `json:"totalRevenue"`
					NetIncome struct {
						Raw int64 `json:"raw"`
					} `json:"netIncome"`
				} `json:"incomeStatementHistory"`
			} `json:"incomeStatementHistory"`
			CashflowStatementHistory struct {
				CashflowStatementHistory []struct {
					EndDate struct {
						Fmt string `json:"fmt"`
					} `json:"endDate"`
					CapitalExpenditures struct {
						Raw int64 `json:"raw"`
					} `json:"capitalExpenditures"`
				} `json:"cashflowStatementHistory"`
			} `json:"cashflowStatementHistory"`
			BalanceSheetHistory struct {
				BalanceSheetHistory []struct {
					EndDate struct {
						Fmt string `json:"fmt"`
					} `json:"endDate"`
					TotalAssets struct {
						Raw int64 `json:"raw"`
					} `json:"totalAssets"`
					NetTangibleAssets struct {
						Raw int64 `json:"raw"`
					} `json:"netTangibleAssets"`
				} `json:"balanceSheetHistory"`
			} `json:"balanceSheetHistory"`
		} `json:"result"`
		Error interface{} `json:"error"`
	} `json:"quoteSummary"`
}

type FinRecord struct {
	Revenue        int64
	NetIncome      int64
	CapEx          int64
	TotalAssets    int64
	TangibleAssets int64
}

func updateTickerData(db *sql.DB, ticker string) error {
	client := &http.Client{Timeout: 10 * time.Second}

	// A. Fetch Price History (Chart API)
	urlChart := fmt.Sprintf("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=5y", ticker)
	req, _ := http.NewRequest("GET", urlChart, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("chart http err: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("chart bad status: %s", resp.Status)
	}

	body, _ := io.ReadAll(resp.Body)
	var chartData ChartResponse
	if err := json.Unmarshal(body, &chartData); err != nil {
		return fmt.Errorf("chart json err: %w", err)
	}

	// Insert Prices
	if len(chartData.Chart.Result) > 0 {
		result := chartData.Chart.Result[0]
		timestamps := result.Timestamp
		quotes := result.Indicators.Quote[0]

		tx, err := db.Begin()
		if err != nil {
			return fmt.Errorf("db begin err: %w", err)
		}

		stmt, err := tx.Prepare(`
            INSERT OR IGNORE INTO prices (ticker, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        `)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("db prepare err: %w", err)
		}
		defer stmt.Close()

		for i, ts := range timestamps {
			if i >= len(quotes.Close) {
				break
			}
			if quotes.Close[i] == 0 {
				continue
			}

			dateStr := time.Unix(ts, 0).Format("2006-01-02")
			_, err := stmt.Exec(
				ticker, dateStr,
				quotes.Open[i], quotes.High[i], quotes.Low[i], quotes.Close[i], quotes.Volume[i],
			)
			if err != nil {
				// Log but continue (duplicates likely)
			}
		}
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("db commit prices err: %w", err)
		}
	}

	// B. Fetch Financials (QuoteSummary API)
	urlFin := fmt.Sprintf("https://query2.finance.yahoo.com/v10/finance/quoteSummary/%s?modules=incomeStatementHistory,cashflowStatementHistory,balanceSheetHistory", ticker)
	reqFin, _ := http.NewRequest("GET", urlFin, nil)
	reqFin.Header.Set("User-Agent", "Mozilla/5.0")
	respFin, err := client.Do(reqFin)
	if err == nil {
		defer respFin.Body.Close()
		if respFin.StatusCode == 200 {
			bodyFin, _ := io.ReadAll(respFin.Body)
			var finData FinancialsResponse
			if err := json.Unmarshal(bodyFin, &finData); err == nil {
				if len(finData.QuoteSummary.Result) > 0 {
					res := finData.QuoteSummary.Result[0]

					// Map by Date string
					dataMap := make(map[string]*FinRecord)

					// 1. Income Statement
					for _, item := range res.IncomeStatementHistory.IncomeStatementHistory {
						d := item.EndDate.Fmt
						if d == "" {
							continue
						}
						if _, ok := dataMap[d]; !ok {
							dataMap[d] = &FinRecord{}
						}
						dataMap[d].Revenue = item.TotalRevenue.Raw
						dataMap[d].NetIncome = item.NetIncome.Raw
					}

					// 2. Cashflow Statement (CapEx)
					for _, item := range res.CashflowStatementHistory.CashflowStatementHistory {
						d := item.EndDate.Fmt
						if d == "" {
							continue
						}
						if _, ok := dataMap[d]; !ok {
							dataMap[d] = &FinRecord{}
						}
						dataMap[d].CapEx = item.CapitalExpenditures.Raw
					}

					// 3. Balance Sheet (Assets)
					for _, item := range res.BalanceSheetHistory.BalanceSheetHistory {
						d := item.EndDate.Fmt
						if d == "" {
							continue
						}
						if _, ok := dataMap[d]; !ok {
							dataMap[d] = &FinRecord{}
						}
						dataMap[d].TotalAssets = item.TotalAssets.Raw
						dataMap[d].TangibleAssets = item.NetTangibleAssets.Raw
					}

					// Insert/Update DB
					if len(dataMap) > 0 {
						tx, err := db.Begin()
						if err != nil {
							return err
						}

						// Upsert logic
						stmt, err := tx.Prepare(`
                            INSERT INTO financials (ticker, period_end, revenue, net_income, capital_expenditure, tangible_assets, total_assets)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(ticker, period_end, is_prediction) DO UPDATE SET
                            revenue=excluded.revenue,
                            net_income=excluded.net_income,
                            capital_expenditure=excluded.capital_expenditure,
                            tangible_assets=excluded.tangible_assets,
                            total_assets=excluded.total_assets,
                            recorded_at=CURRENT_TIMESTAMP
                        `)
						if err != nil {
							tx.Rollback()
							return err
						}
						defer stmt.Close()

						for dateStr, rec := range dataMap {
							_, err := stmt.Exec(ticker, dateStr, rec.Revenue, rec.NetIncome, rec.CapEx, rec.TangibleAssets, rec.TotalAssets)
							if err != nil {
								// log.Printf("Fin insert err: %v", err)
							}
						}

						if err := tx.Commit(); err != nil {
							return fmt.Errorf("db commit fin err: %w", err)
						}
					}
				}
			} else {
				return fmt.Errorf("fin json err: %w", err)
			}
		}
	}

	return nil
}
