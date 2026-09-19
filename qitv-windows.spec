# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path
import tomllib

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
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

ROOT = Path(SPECPATH)
NATIVE_MPV = ROOT / 'native' / 'mpv'
if not (NATIVE_MPV / 'bundle.json').is_file():
    raise SystemExit('Prepare bundled MPV first: uv run scripts/prepare_mpv.py')
if not json.loads((NATIVE_MPV / 'bundle.json').read_text()).get('redistributable'):
    raise SystemExit('Diagnostic MPV is not a release input; run the default source preparation.')

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'assets'), 'assets'),
        (str(ROOT / 'pyproject.toml'), '.'),
    ],
    hiddenimports=['scripts.smoke_mpv'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['vlc', 'mpv'],
    noarchive=False,
)
# Keep the prepared standalone closure intact, without dependency analysis or
# UPX rewriting of the native player and its DLLs.
native_mpv = Tree(str(NATIVE_MPV), prefix='native/mpv', typecode='DATA')
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    native_mpv,
    [],
    name='qitv.exe',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / 'assets' / 'qitv.ico'),
    version=version_resource,
)
