@echo off
rem Runs the icon with no console window. Double-click, Startup link, task.
rem pythonw is looked up next to python.exe (PATH), no hardcoded path.
set PYW=pythonw.exe
where "%PYW%" >nul 2>nul || (
  echo pythonw.exe not found in PATH. Install Python, then: py -m pip install pystray pillow
  pause & exit /b 1
)
start "" "%PYW%" "%~dp0tray.py"
