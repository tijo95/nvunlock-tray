@echo off
rem Sonde ssdiag en elevation. Se relance eleve (UAC) si necessaire.
rem Appelle tray.py --probe-once : ne dessine rien, rafraichit etat.json.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b %errorlevel%
)
python "%~dp0tray.py" --probe-once
