# PyInstaller one-file build for the OPAL Sync command-line app.
# Keyring loads platform backends dynamically, so explicitly include Windows'.

a = Analysis(
    ["src/opal_sync/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["keyring.backends.Windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="opal_sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
