# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import tomllib
from PyInstaller.building.build_main import Analysis, PYZ, EXE, BUNDLE


def read_version():
    pyproj = Path(__file__).with_name('pyproject.toml')
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

a = Analysis(
    ['main.py'],
    pathex=[VLC_PATH],
    binaries=[
        (os.path.join(VLC_PATH, 'vlc/plugins/*'), 'plugins'),
    ],
    datas=[],
    hiddenimports=[],
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
        (libvlccore_version, os.path.join(VLC_PATH, libvlccore_version), "BINARY")
    ],
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
