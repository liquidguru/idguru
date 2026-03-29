@echo off
echo ============================================
echo  idGuru — Setup
echo ============================================
echo.
:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)
:: Check Python version — warn if 3.13 or later
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)
if %PYMAJ% EQU 3 if %PYMIN% GEQ 13 (
    echo.
    echo WARNING: Python %PYVER% detected.
    echo idGuru requires Python 3.11 or 3.12. Python 3.13 and later are not yet
    echo supported due to missing pre-built wheels for pydantic-core.
    echo.
    echo Please install Python 3.12 from https://www.python.org/downloads/
    echo and re-run this setup using that version.
    echo.
    pause
    exit /b 1
)
echo Python %PYVER% found. Installing dependencies...
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
    echo Install it by running:  winget install ffmpeg
    echo Or download from https://ffmpeg.org/download.html and add the bin folder to PATH.
    echo Photos will still work without ffmpeg.
    echo.
)
echo.
echo ============================================
echo  Setup complete!
echo  Double-click launcher.pyw to start idGuru.
echo ============================================
echo.
pause
