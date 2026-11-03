@echo off
rem ssdiag probe with elevation. Relaunches elevated (UAC) when needed.
rem Calls tray.py --probe-once: draws nothing, refreshes state.json.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b %errorlevel%
)
python "%~dp0tray.py" --probe-once
