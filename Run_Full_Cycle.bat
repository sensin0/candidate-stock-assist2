@echo off
cd /d "%~dp0"

echo ==========================================
echo  Ta-Chan 2: Full Investment Cycle
echo ==========================================

echo.
echo [1/4] Updating Database (Go and Python Fetchers in Parallel)...
echo ------------------------------------------

rem Run fetchers in parallel using helper PowerShell script
powershell -ExecutionPolicy Bypass -File "%~dp0run_fetchers_parallel.ps1"

if %ERRORLEVEL% NEQ 0 (
    echo Error fetching data.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [2/4] Running Analysis and Generating Dashboard Data...
echo ------------------------------------------
python cyclical_screener.py
if %ERRORLEVEL% NEQ 0 (
    echo Error running screener.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [3/4] Running Backtest (Validation)...
echo ------------------------------------------
python backtest_cyclical.py
if %ERRORLEVEL% NEQ 0 (
    echo Error running backtest.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [4/4] Launching Web Dashboard...
echo ------------------------------------------
cd web-dashboard
echo Starting Next.js Dev Server...
echo Please open http://localhost:3000 in your browser if not opened automatically.
start http://localhost:3000
npm run dev
