# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import re
import tomllib
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo,
    FixedFileInfo,
    StringFileInfo,
    StringTable,
    StringStruct,
    VarFileInfo,
    VarStruct,
)


def read_version():
    # SPECPATH is provided by PyInstaller and points to the spec file directory
    pyproj = Path(SPECPATH) / 'pyproject.toml'
    with pyproj.open('rb') as f:
        return tomllib.load(f)['project']['version']


def to_4tuple(ver: str):
    parts = [int(p) for p in ver.split('.') if p.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


APP_VERSION = read_version()
FILEVERS = to_4tuple(APP_VERSION)
PRODVERS = FILEVERS
FILEVER_STR = '.'.join(map(str, FILEVERS))

version_resource = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=FILEVERS,
        prodvers=PRODVERS,
        mask=0x3F,
        flags=0x0,
        OS=0x4,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    '040904B0',
                    [
                        StringStruct('CompanyName', 'ozankaraali'),
                        StringStruct('FileDescription', 'QiTV'),
                        StringStruct('FileVersion', FILEVER_STR),
                        StringStruct('InternalName', 'qitv.exe'),
                        StringStruct('LegalCopyright', ''),
                        StringStruct('OriginalFilename', 'qitv.exe'),
                        StringStruct('ProductName', 'QiTV'),
                        StringStruct('ProductVersion', FILEVER_STR),
                    ],
                ),
            ]
        ),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)

VLC_PATH = 'C:\\Program Files\\VideoLAN\\VLC'

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[VLC_PATH], #insert your base VLC path here, ex: pathex=["D:\KivySchool\VLC"],
    binaries=[
        (os.path.join(VLC_PATH, 'plugins/*'), 'plugins'),
    ],
    datas=[
        ('assets/*', 'assets'),
        ('pyproject.toml', '.'),  # Include pyproject.toml so version can be read
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
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
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
    a.binaries + [
        ("libvlc.dll", os.path.join(VLC_PATH, 'libvlc.dll'), "BINARY"),
        ("libvlccore.dll", os.path.join(VLC_PATH, 'libvlccore.dll'), "BINARY"),
    ],
    a.datas,
    [],
    name='qitv.exe',
    debug=False,
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
    icon='assets/qitv.ico',
    version=version_resource,
)
