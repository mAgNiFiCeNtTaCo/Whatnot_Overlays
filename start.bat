@echo off
rem Whatnot hit counter: starts the server and opens the control panel.
rem Extra arguments go to server.py, e.g.  start.bat --host 0.0.0.0   (phone control on your Wi-Fi)
cd /d "%~dp0"
if not defined PORT set PORT=7777

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)

rem Open the control panel a moment after the server starts (same window, no wait).
start "" /b python -c "import time, webbrowser; time.sleep(1.5); webbrowser.open('http://127.0.0.1:%PORT%/control')"

python server.py --port %PORT% %*
echo.
echo Server stopped.
pause
