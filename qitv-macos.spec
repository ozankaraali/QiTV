# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import tomllib


def read_version():
    # SPECPATH is provided by PyInstaller and points to the spec file directory
    pyproj = Path(SPECPATH) / 'pyproject.toml'
    with pyproj.open('rb') as f:
        return tomllib.load(f)['project']['version']


APP_VERSION = read_version()

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
    name='QiTV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='assets/qitv.icns',
)
app = BUNDLE(
    exe,
    name='QiTV.app',
    icon='assets/qitv.icns',
    bundle_identifier='com.ozankaraali.QiTV',
    version=APP_VERSION,
    info_plist={
        'CFBundleDisplayName': 'QiTV',
        'CFBundleExecutable': 'QiTV',
        'CFBundleIdentifier': 'com.ozankaraali.QiTV',
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'QiTV',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': APP_VERSION,
        'CFBundleVersion': APP_VERSION,
        'LSApplicationCategoryType': 'public.app-category.video',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
    }
)
