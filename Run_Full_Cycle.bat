@echo off
cd /d "%~dp0"

echo ==========================================
echo  Cyclical Ranker: Full Investment Cycle
echo ==========================================

echo.
echo [1/5] Updating Database (Go and Python Fetchers in Parallel)...
echo ------------------------------------------

rem Run fetchers in parallel using helper PowerShell script
powershell -ExecutionPolicy Bypass -File "%~dp0run_fetchers_parallel.ps1"

if %ERRORLEVEL% NEQ 0 (
    echo Error fetching data.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [2/5] Running Analysis and Generating Dashboard Data...
echo ------------------------------------------
python cyclical_screener.py
if %ERRORLEVEL% NEQ 0 (
    echo Error running screener.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo.
echo [3/5] Generating Unified Ranking for Web...
echo ------------------------------------------
python scripts\weekly_cloud_ranker.py --mode refresh --output weekly_ranking_report.json --top 10 --earnings-window-days 120 --state-file .github\ranking-state.json --update-state --workers 2 --chunk-size 450
if %ERRORLEVEL% NEQ 0 (
    echo Error generating unified ranking.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [4/5] Running Backtest (Validation)...
echo ------------------------------------------
python backtest_cyclical.py
if %ERRORLEVEL% NEQ 0 (
    echo Error running backtest.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [5/5] Launching Web Dashboard...
echo ------------------------------------------
cd web-dashboard
echo Starting Next.js Dev Server...
echo Please open http://localhost:3000 in your browser if not opened automatically.
start http://localhost:3000
npm run dev
