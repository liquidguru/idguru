@echo off
echo ============================================
echo  idGuru — Setup
echo ============================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python 3.10 or later from https://www.python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo Python found. Installing dependencies...
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies.
    echo Please check the error above and try again.
    pause
    exit /b 1
)

:: Check ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: ffmpeg not found.
    echo ffmpeg is required to index video files.
    echo Download from https://ffmpeg.org/download.html and add to PATH.
    echo Photos will still work without ffmpeg.
    echo.
)

echo.
echo ============================================
echo  Setup complete!
echo  Double-click launcher.py to start idGuru.
echo ============================================
echo.
pause
