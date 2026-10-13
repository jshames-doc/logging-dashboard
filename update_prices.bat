@echo off
setlocal
cd /d "%~dp0"
python update_prices.py %*
pause
