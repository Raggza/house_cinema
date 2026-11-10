@echo off
rem House Cinema: start the hub. Double-click this file, or run it in a terminal.
rem Stop the hub with Ctrl+C (or close the window). Extra arguments go to the hub.
rem PowerShell scripts are blocked by default on Windows: -ExecutionPolicy Bypass applies to this run only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set code=%errorlevel%
if not "%code%"=="0" (
  echo.
  echo Hub da dung voi ma loi %code%. Xem thong bao o tren.
  pause
)
exit /b %code%
