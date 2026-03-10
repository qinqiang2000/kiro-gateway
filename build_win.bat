@echo off
REM Build Kiro Gateway GUI for Windows
REM Output: dist\KiroGateway\KiroGateway.exe
REM
REM Prerequisites:
REM   pip install pywebview pyinstaller
REM
REM Usage:
REM   build_win.bat

cd /d "%~dp0"

echo === Kiro Gateway Windows Build ===

REM Check dependencies
python -c "import webview" 2>nul || (echo Installing pywebview... && pip install pywebview)
python -c "import PyInstaller" 2>nul || (echo Installing pyinstaller... && pip install pyinstaller)

REM Clean previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build
echo Building...
pyinstaller build.spec --noconfirm

echo.
echo Build complete!
echo Exe: dist\KiroGateway\KiroGateway.exe
echo.
pause
