@echo off
rem Card scanner: camera preview + training photos per rarity. Opens http://127.0.0.1:7778/
rem Extra arguments go to the scanner, e.g.  scan.bat --camera 1   or   scan.bat --demo
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3 from https://www.python.org/downloads/
  pause
  exit /b 1
)

python -c "import cv2, numpy" >nul 2>nul
if errorlevel 1 (
  echo The scanner needs OpenCV and NumPy. Installing them now...
  python -m pip install --user opencv-python numpy
  if errorlevel 1 (
    echo Install failed. Run:  python -m pip install --user opencv-python numpy
    pause
    exit /b 1
  )
)

rem Card names use Windows' built-in text recognition (OCR); optional, small download.
python -c "import winrt.windows.media.ocr" >nul 2>nul
if errorlevel 1 (
  echo Installing the Windows text recognition bindings for card names...
  python -m pip install --user winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Globalization
  if errorlevel 1 echo Card names will be off: that install failed. The scanner still works without them.
)

start "" /b python -c "import time, webbrowser; time.sleep(2); webbrowser.open('http://127.0.0.1:7778/')"
python -m scanner %*
echo.
echo Scanner stopped.
pause
