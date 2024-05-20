# -*- mode: python ; coding: utf-8 -*-

VLC_PATH = '/Applications/VLC.app/Contents'  # Adjust this path if necessary

a = Analysis(
    ['main.py'],
    pathex=[VLC_PATH],
    binaries=[
        (os.path.join(VLC_PATH, 'MacOS/plugins/*'), 'plugins'),
    ],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    #https://github.com/pyinstaller/pyinstaller/issues/7851#issuecomment-1677986648
    module_collection_mode={
        'vlc': 'py',
    }
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries + [
        ("libvlc.dylib", os.path.join(VLC_PATH, 'MacOS/lib/libvlc.dylib'), "BINARY"),
        ("libvlccore.dylib", os.path.join(VLC_PATH, 'MacOS/lib/libvlccore.dylib'), "BINARY")
        ],
    #a.binaries,
    a.datas,
    [],
    name='qitv',
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
app = BUNDLE(
    exe,
    name='qitv.app',
    icon='assets/qitv.icns',
    bundle_identifier=None,
)
