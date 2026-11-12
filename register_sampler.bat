@echo off
setlocal
cd /d "%~dp0"
rem Register a scheduled task that samples quota every 15 minutes.
rem pythonw.exe (no console) is registered directly so no window flashes.
set "PYTHONW="
for /f "delims=" %%p in ('where pythonw.exe 2^>nul') do (
  if not defined PYTHONW set "PYTHONW=%%p"
)
if not defined PYTHONW (
  echo pythonw.exe was not found on PATH.
  echo It ships with Python from https://www.python.org/downloads/ - reinstall
  echo Python or run this file from an environment that has pythonw.exe.
  pause
  exit /b 1
)
rem -Daily can't take -RepetitionInterval; -Once + interval repeats indefinitely.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%PYTHONW%' -Argument '\"%~dp0server.py\" --sample-only' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15);" ^
  "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew;" ^
  "Register-ScheduledTask -TaskName 'Session Ledger quota sampler' -Action $a -Trigger $t -Settings $s -Description 'Samples Devin quota for Session Ledger runway/allowance.' -Force"
if errorlevel 1 (
  echo.
  echo Scheduled-task registration failed. If it needs elevation, re-run this
  echo file as Administrator, or register the task manually:
  echo   schtasks /Create /TN "Session Ledger quota sampler" /TR "\"%PYTHONW%\" \"%~dp0server.py\" --sample-only" /SC MINUTE /MO 15 /F
  pause
  exit /b 1
)
echo Registered "Session Ledger quota sampler" to run every 15 minutes via:
echo   %PYTHONW%
echo Verify with:  schtasks /Query /TN "Session Ledger quota sampler" /V /FO LIST
echo and check for one line per run in:  .cache\sampler.log
pause
