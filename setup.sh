#!/bin/bash
echo "============================================"
echo " idGuru — Setup"
echo "============================================"
echo

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found."
    echo "Install it from https://www.python.org/downloads/ or via Homebrew:"
    echo "  brew install python"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$PYTHON_VERSION" -lt 10 ]; then
    echo "ERROR: Python 3.10 or later is required."
    echo "Please upgrade Python."
    exit 1
fi

echo "Python found: $(python3 --version)"
echo "Installing dependencies..."
echo

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo
    echo "ERROR: Failed to install dependencies."
    exit 1
fi

# Check ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo
    echo "WARNING: ffmpeg not found."
    echo "ffmpeg is required to index video files."
    echo "Install via Homebrew:  brew install ffmpeg"
    echo "Photos will still work without ffmpeg."
    echo
fi

# Make launcher executable
chmod +x launcher.py 2>/dev/null

echo
echo "============================================"
echo " Setup complete!"
echo " Run:  python3 launcher.py"
echo " Or double-click launcher.py in Finder"
echo "============================================"
echo
