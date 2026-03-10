# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Kiro Gateway GUI.

Builds a single-folder distribution with native window (pywebview).
Works on macOS (.app) and Windows (.exe).

Usage:
    pyinstaller build.spec
"""

import sys
import os
from pathlib import Path

block_cipher = None

# Collect all kiro package files
kiro_data = []
for f in Path('kiro').rglob('*.py'):
    kiro_data.append((str(f), 'kiro'))

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('web/index.html', 'web'),
        ('kiro/*.py', 'kiro'),
    ],
    hiddenimports=[
        # FastAPI / Uvicorn
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'fastapi',
        'fastapi.responses',
        'fastapi.middleware.cors',
        'starlette',
        'starlette.responses',
        'starlette.routing',
        'starlette.middleware',
        'starlette.middleware.cors',
        'anyio',
        'anyio._backends',
        'anyio._backends._asyncio',
        # HTTP client
        'httpx',
        'httpx._transports',
        'httpx._transports.default',
        'httpcore',
        'httpcore._async',
        'httpcore._sync',
        'h11',
        'sniffio',
        'certifi',
        # Pydantic
        'pydantic',
        'pydantic.deprecated',
        'pydantic_core',
        # Logging
        'loguru',
        'loguru._logger',
        # Tokenizer
        'tiktoken',
        'tiktoken_ext',
        'tiktoken_ext.openai_public',
        # Config
        'dotenv',
        # pywebview backends
        'webview',
        'webview.platforms',
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'webview.platforms.cef',
        'webview.platforms.cocoa',
        'clr',
        'pythonnet',
        'System',
        'System.Windows.Forms',
        'System.Drawing',
        'System.Threading',
        # SQLite (for credential reading)
        'sqlite3',
        # Email (used by some deps)
        'email.mime',
        'email.mime.text',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest',
        'pytest_asyncio',
        'hypothesis',
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Filter out non-existent .env
a.datas = [(name, path, typ) for name, path, typ in a.datas if os.path.exists(path)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

icon_file = 'app_icon.icns' if sys.platform == 'darwin' else 'app_icon.ico'

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='KiroGateway',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Show console for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KiroGateway',
)

# macOS .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Kiro Gateway.app',
        icon='app_icon.icns',
        bundle_identifier='dev.kiro.gateway',
        info_plist={
            'CFBundleName': 'Kiro Gateway',
            'CFBundleDisplayName': 'Kiro Gateway',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.15',
        },
    )
