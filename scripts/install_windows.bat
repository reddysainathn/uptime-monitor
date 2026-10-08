@echo off
REM UptimeMonitor Windows installer — no admin needed for manual run.
REM Autostart via Task Scheduler (runs at logon, restarts on failure).
set REPO=%~dp0..
where python >nul 2>nul || (echo Install Python 3.9+ from python.org and retry & exit /b 1)
python -m unittest discover -s tests -v
mkdir "%REPO%\data" 2>nul
mkdir "%REPO%\dashboard" 2>nul
mkdir "%REPO%\reports" 2>nul
schtasks /create /tn "UptimeMonitor" /tr "python -m src.monitor" /sc onlogon /rl limited /f
echo Bookmark: http://127.0.0.1:8899
echo Manual run: python -m src.monitor
echo ISP report: python -m src.report --days 7
