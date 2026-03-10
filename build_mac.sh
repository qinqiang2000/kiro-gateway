#!/bin/bash
# Build Kiro Gateway GUI for macOS
# Output: dist/Kiro Gateway.app
#
# Prerequisites:
#   pip install pywebview pyinstaller
#
# Usage:
#   chmod +x build_mac.sh && ./build_mac.sh

set -e
cd "$(dirname "$0")"

echo "=== Kiro Gateway macOS Build ==="

# Check dependencies
python3 -c "import webview" 2>/dev/null || { echo "Installing pywebview..."; pip3 install pywebview; }
python3 -c "import PyInstaller" 2>/dev/null || { echo "Installing pyinstaller..."; pip3 install pyinstaller; }

# Clean previous build
rm -rf build/ dist/

# Build
echo "Building..."
pyinstaller build.spec --noconfirm

# Fix Python framework symlink (Homebrew Python + PyInstaller BUNDLE issue)
APP="dist/Kiro Gateway.app"
FRAMEWORKS="$APP/Contents/Frameworks"
if [ -d "$FRAMEWORKS/Python.framework" ] && [ ! -f "$FRAMEWORKS/Python" ]; then
    ln -sf Python.framework/Versions/3.11/Python "$FRAMEWORKS/Python"
    echo "Fixed Python framework symlink"
fi

# Package as zip
echo "Packaging..."
cd dist && zip -r "Kiro Gateway.zip" "Kiro Gateway.app" > /dev/null && cd ..

echo ""
echo "Build complete!"
echo "App: dist/Kiro Gateway.app"
echo "Zip: dist/Kiro Gateway.zip"
echo ""
echo "To run: open 'dist/Kiro Gateway.app'"
echo "Note: First launch may require right-click → Open (unsigned app)"
