#!/bin/bash
echo "============================================"
echo " idGuru — Setup"
echo "============================================"
echo
# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found."
    echo "Install it from https://www.python.org/downloads/ or via Homebrew:"
    echo "  brew install python@3.12"
    exit 1
fi
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
if [ "$PYTHON_MINOR" -lt 11 ]; then
    echo "ERROR: Python 3.11 or later is required (3.12 recommended)."
    echo "Please upgrade: brew install python@3.12"
    exit 1
fi
if [ "$PYTHON_MINOR" -ge 13 ]; then
    echo "ERROR: Python 3.$PYTHON_MINOR detected."
    echo "idGuru requires Python 3.11 or 3.12. Python 3.13 and later are not yet"
    echo "supported due to missing pre-built wheels for pydantic-core."
    echo "Install Python 3.12:  brew install python@3.12"
    echo "Then re-run:  python3.12 setup.sh"
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
    echo "If you see a pydantic-core error, make sure you are using Python 3.12:"
    echo "  brew install python@3.12 && python3.12 setup.sh"
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
chmod +x launcher.pyw 2>/dev/null
echo
echo "============================================"
echo " Setup complete!"
echo " Run:  python3 launcher.pyw"
echo " Or double-click launcher.pyw in Finder"
echo "============================================"
echo
