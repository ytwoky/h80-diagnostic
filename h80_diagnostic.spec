# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec — GE H80-200 Diagnostic Tool v4.1
# Сборка: pyinstaller h80_diagnostic.spec
#
# macOS  → dist/H80 Diagnostic.app
# Windows → dist/H80 Diagnostic/H80 Diagnostic.exe  (запускать на Windows)

import sys
from pathlib import Path

APP_NAME    = "H80 Diagnostic"
APP_VERSION = "4.1.0"
SCRIPT      = "h80_diagnostic.py"

is_mac = sys.platform == "darwin"
is_win = sys.platform == "win32"

icon_mac = "mgtu_logo.icns"
icon_win = "mgtu_logo.ico"
icon = (icon_mac if is_mac else icon_win) if Path(icon_mac if is_mac else icon_win).exists() else None

a = Analysis(
    [SCRIPT],
    pathex=[],
    binaries=[],
    datas=[
        ("mgtu_logo.png", "."),
    ],
    hiddenimports=[
        # matplotlib / tkinter backend
        "matplotlib.backends.backend_tkagg",
        "matplotlib.backends.backend_agg",
        "matplotlib.backends._backend_tk",
        # scipy
        "scipy.interpolate",
        "scipy.interpolate._interpolate",
        "scipy.interpolate._fitpack_py",
        "scipy._lib.messagestream",
        # Pillow
        "PIL",
        "PIL.Image",
        "PIL.ImageTk",
        "PIL._imaging",
        # tkinter
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython", "jupyter", "notebook", "pandas",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "wx", "gi", "gtk",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

if is_mac:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="ru.mgtuga.h80diagnostic",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleVersion": APP_VERSION,
            "CFBundleShortVersionString": "4.1",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSMinimumSystemVersion": "11.0",
        },
    )
