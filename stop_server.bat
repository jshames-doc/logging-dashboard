@echo off
setlocal
rem Stops the Session Ledger dashboard server (python server.py) listening on DASHBOARD_PORT (default 8765).
if "%DASHBOARD_PORT%"=="" (set "PORT=8765") else (set "PORT=%DASHBOARD_PORT%")

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pids = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique);" ^
  "if (-not $pids) { Write-Host 'No server is listening on port %PORT%.'; exit 0 }" ^
  "foreach ($p in $pids) {" ^
  "  $proc = Get-CimInstance Win32_Process -Filter \"ProcessId=$p\";" ^
  "  if ($proc.CommandLine -match 'server\.py') { Stop-Process -Id $p -Force; Write-Host \"Stopped dashboard server (PID $p) on port %PORT%.\" }" ^
  "  else { Write-Host \"Port %PORT% is used by another program (PID ${p}, $($proc.Name)); not stopping it.\" }" ^
  "}" ^
  "Start-Sleep -Seconds 3"
