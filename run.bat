@echo off
setlocal
cd /d "%~dp0"
title Session Ledger
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python 3.8+ from https://www.python.org/downloads/
  pause
  exit /b 1
)
python server.py %*
if errorlevel 1 pause
