@echo off
rem One double-click install: checks Python, installs the two dependencies,
rem starts the icon. No path hardcoded, no elevation.
rem
rem WHY THIS RESOLVES A FULL PATH: a bare "python.exe" is NOT one program.
rem HKCU\...\App Paths\python.exe points at the Python Manager stub in
rem WindowsApps, and CreateProcess/ShellExecute apply App Paths to a name with no
rem backslash. Measured on one machine with 3 Pythons installed: "cmd /c where
rem python.exe" gave C:\Python315 (pystray installed), while CreateProcess on the
rem bare name reached Python313 WITHOUT pystray -> the icon never started. So every
rem step below uses the SAME full path, and the stub is skipped.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
for /f "delims=" %%i in ('where python.exe 2^>nul') do (
  set "CAND=%%i"
  call set "STRIPPED=!CAND:WindowsApps=!"
  if not defined PY ( if "!STRIPPED!"=="!CAND!" set "PY=!CAND!" )
)

if not defined PY (
  echo No usable python.exe found in PATH ^(a "python.exe" living in WindowsApps is
  echo the Microsoft Store stub and is not used by this script^).
  echo Install Python 3.9 or newer from https://www.python.org/downloads/ , ticking
  echo "Add python.exe to PATH", then double-click this file again.
  pause
  exit /b 1
)

echo Interpreter: %PY%
"%PY%" -c "import sys;print('  python',sys.version.split()[0]);sys.exit(0 if sys.version_info[:2]>=(3,9) else 2)"
if errorlevel 2 (
  echo Python 3.9 or newer is required.
  pause
  exit /b 1
)

echo Installing pystray and pillow into THAT interpreter...
"%PY%" -m pip install --disable-pip-version-check -q pystray pillow

rem Verify in the SAME interpreter that will run the icon, not in another one.
"%PY%" -c "import pystray, PIL" 2>nul
if errorlevel 1 (
  echo.
  echo pystray/PIL are still missing in %PY% .
  echo Retry by hand: "%PY%" -m pip install pystray pillow
  echo If this PC uses a corporate proxy, see the README.
  pause
  exit /b 1
)

set "PYW=%PY:python.exe=pythonw.exe%"
echo.
echo Done. Starting the icon with: %PYW%
echo Look at the bottom-right of the taskbar (click the small up arrow if icons are
echo hidden). The icon is GREY at first.
echo.
echo Next, in its right-click menu:
echo   "Install ssdiag (verified download)"  then  "Probe now".
echo.
rem The icon is started with CreateProcess (DETACHED, no console), NOT with `start`.
rem `start` goes through ShellExecute, and ShellExecute is exactly the path that App
rem Paths corrupts (see the header). CreateProcess with the full path is the path that
rem was measured working: journal written + the icon registered in the tray.
ping -n 4 -w 1000 127.0.0.1 >nul
rem NOTE: %~dp0 ENDS with a backslash. Inside r'...' in Python that backslash escapes the
rem closing quote -> SyntaxError, and the icon silently never starts. Strip it first.
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
"%PY%" -c "import subprocess,sys;f=getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0);sys.exit(subprocess.Popen([r'%PYW%',r'%HERE%\tray.py'],creationflags=f,stdin=subprocess.DEVNULL).poll())"
ping -n 12 -w 1000 127.0.0.1 >nul
"%PY%" -c "import pathlib,sys;p=pathlib.Path(r'%HERE%\tray-run.log');sys.exit(0 if p.exists() else 1)"
if errorlevel 1 (
  echo The icon did not write a start record in tray-run.log.
  echo Start it by hand, in a command window in this folder:  "%PYW%" tray.py
  pause
  exit /b 1
)
echo Icon started. A record was written in tray-run.log.
exit /b 0
