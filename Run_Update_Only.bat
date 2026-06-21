@echo off
cd /d "%~dp0"

echo ==========================================
echo  Cyclical Ranker: Daily Data Update
echo ==========================================

echo.
echo [1/4] Updating Database...
echo ------------------------------------------
powershell -ExecutionPolicy Bypass -File "%~dp0run_fetchers_parallel.ps1"
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo [2/4] Generating Local Dashboard Data...
echo ------------------------------------------
python cyclical_screener.py
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo [3/4] Generating Unified Ranking...
echo ------------------------------------------
python scripts\weekly_cloud_ranker.py --mode weekly --output weekly_ranking_report.json --top 10 --earnings-window-days 120 --state-file .github\ranking-state.json --update-state --workers 2
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo [4/4] Running Backtest...
echo ------------------------------------------
python backtest_cyclical.py
if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%

echo.
echo Update complete.
