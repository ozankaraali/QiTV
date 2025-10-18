# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
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
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries + [("libVLC.dll", os.path.join(VLC_PATH, 'libVLC.dll'), "BINARY")],
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
