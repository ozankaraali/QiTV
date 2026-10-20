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
import re
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
        # QiTV's license, and those of what the app ships (VLC, Qt, Python libraries).
        ('LICENSE', '.'),
        ('THIRD-PARTY-NOTICES.md', '.'),
        # The texts in each language (services/i18n.py). Qt's own (qtbase_<code>.qm, its
        # buttons and file dialogs) come with PyInstaller's QtCore hook, under
        # PySide6/Qt/translations.
        ('i18n/*.json', 'i18n'),
        # The phone remote's page (services/remote/hub.py serves it on the home network).
        ('services/remote/web', 'remote_web'),
        ('licenses', 'licenses'),
    ],
    hiddenimports=[
        'segno',  # the phone remote's pairing QR (imported where it is drawn)
        # The live time-shift helper process (spawned) and what its source imports.
        'services.live.worker',
        'm3u8',
        'Cryptodome.Cipher.AES',
        # Loaded on first use (services/lazy_import.py): no import statement names them.
        'aiohttp',
        'asyncio',
        'requests',
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

# Qt's PDF library comes in only with Qt's image plugin for PDF files (imageformats/qpdf), and
# QiTV opens no PDF: neither ships (8 MB in the macOS app). Qt's web engine, a wheel of its own
# from PySide6 6.12, is never imported, so PyInstaller leaves it out by itself.
QT_PDF = re.compile(r'QtPdf\.framework/|Qt6Pdf|imageformats/(lib)?qpdf\.', re.IGNORECASE)


def ships(entry):
    """False for Qt's PDF library and plugin, and for the links PyInstaller makes to them."""
    return not any(QT_PDF.search(str(part).replace('\\', '/')) for part in entry[:2])


a.binaries = [entry for entry in a.binaries if ships(entry)]
a.datas = [entry for entry in a.datas if ships(entry)]

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
