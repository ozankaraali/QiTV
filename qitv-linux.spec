# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import tomllib
from PyInstaller.building.build_main import Analysis, PYZ, EXE, BUNDLE


def read_version():
    # SPECPATH is provided by PyInstaller and points to the spec file directory
    pyproj = Path(SPECPATH) / 'pyproject.toml'
    with pyproj.open('rb') as f:
        return tomllib.load(f)['project']['version']


APP_VERSION = read_version()

VLC_PATH = '/usr/lib/x86_64-linux-gnu'  # Default path on Ubuntu

# Find the exact versions of libvlc and libvlccore
libvlc_version = "libvlc.so"
libvlccore_version = "libvlccore.so"

# Check if versioned libraries exist; use `ls` to find versions if not directly known
for file in os.listdir(VLC_PATH):
    if file.startswith("libvlc.so"):
        libvlc_version = file
    if file.startswith("libvlccore.so"):
        libvlccore_version = file

# Find libxcb-cursor (required by Qt xcb platform plugin on many distros)
xcb_cursor_binaries = []
for file in os.listdir(VLC_PATH):
    if file.startswith("libxcb-cursor.so"):
        xcb_cursor_binaries.append(
            (file, os.path.join(VLC_PATH, file), "BINARY")
        )

a = Analysis(
    ['main.py'],
    pathex=[VLC_PATH],
    binaries=[
        # libVLC 3 on Linux looks for plugins in <libvlccore's directory>/vlc/plugins, and
        # the bundled libvlccore sits at the bundle's top (VLC's own layout under VLC_PATH).
        (os.path.join(VLC_PATH, 'vlc/plugins/*'), 'vlc/plugins'),
    ],
    datas=[
        ('pyproject.toml', '.'),  # Include pyproject.toml so version can be read
        ('assets', 'assets'),  # Include icons and desktop file
        # QiTV's license, and those of what the app ships (VLC, Qt, Python libraries).
        ('LICENSE', '.'),
        ('THIRD-PARTY-NOTICES.md', '.'),
        # The texts in each language (services/i18n.py). Qt's own (qtbase_<code>.qm, its
        # buttons and file dialogs) come with PyInstaller's QtCore hook, under
        # PySide6/Qt/translations.
        ('i18n/*.json', 'i18n'),
        ('licenses', 'licenses'),
    ],
    hiddenimports=[
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
    # If using a custom path for VLC, ensure you include the libvlc libraries
    module_collection_mode={
        'vlc': 'py',
    }
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries + [
        (libvlc_version, os.path.join(VLC_PATH, libvlc_version), "BINARY"),
        (libvlccore_version, os.path.join(VLC_PATH, libvlccore_version), "BINARY"),
    ] + xcb_cursor_binaries,
    a.datas,
    [],
    name='qitv',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Set to False if you want to suppress the console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
