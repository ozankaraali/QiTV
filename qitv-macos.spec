# -*- mode: python ; coding: utf-8 -*-
#
# macOS: QiTV.app as one directory inside the bundle, with VLC's own libVLC in it.
#
# The app ships VLC.app's whole libVLC, nothing trimmed (lib/ and plugins/), and needs no VLC
# install:
# - libvlc and libvlccore sit at the bundle's top (Contents/Frameworks, sys._MEIPASS);
# - the plugins sit where that libvlccore looks for them: <its directory>/vlc/plugins;
# - before python-vlc loads, main.py points it at these (services/vlc_bundle.py), since
#   python-vlc prefers /Applications/VLC.app and its plugins when that exists.
# One directory, not one file: a one-file app unpacks into a new directory at every launch,
# and macOS validates each freshly written plugin on its first load (56 s on an Intel Mac);
# inside the .app that happens once.

import os
from pathlib import Path
import tomllib


def read_version():
    # SPECPATH is provided by PyInstaller and points to the spec file directory
    pyproj = Path(SPECPATH) / 'pyproject.toml'
    with pyproj.open('rb') as f:
        return tomllib.load(f)['project']['version']


APP_VERSION = read_version()

VLC_PATH = '/Applications/VLC.app/Contents/MacOS'  # where VLC's installer puts it

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        (os.path.join(VLC_PATH, 'lib', '*'), '.'),
        (os.path.join(VLC_PATH, 'plugins', '*'), 'vlc/plugins'),
    ],
    datas=[
        ('pyproject.toml', '.'),  # Include pyproject.toml so version can be read
        ('assets/qitv.png', 'assets'),  # the logo the Library, welcome card and About show
    ],
    hiddenimports=[
        # The live time-shift helper process (spawned) and what its source imports.
        'services.live.worker',
        'm3u8',
        'Cryptodome.Cipher.AES',
    ],
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
    [],
    exclude_binaries=True,
    name='QiTV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='assets/qitv.icns',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='QiTV',
)
app = BUNDLE(
    coll,
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
