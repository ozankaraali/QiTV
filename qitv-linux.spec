# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path

from PyInstaller.building.datastruct import Tree

ROOT = Path(SPECPATH)
NATIVE_MPV = ROOT / 'native' / 'mpv'
if not (NATIVE_MPV / 'bundle.json').is_file():
    raise SystemExit('Prepare bundled MPV first: uv run scripts/prepare_mpv.py')
if not json.loads((NATIVE_MPV / 'bundle.json').read_text()).get('redistributable'):
    raise SystemExit('Diagnostic MPV is not a release input; run the default source preparation.')

# Qt's xcb plugin needs this at runtime on desktop Linux.
xcb_cursor_binaries = [
    (str(path), '.') for path in Path('/usr/lib/x86_64-linux-gnu').glob('libxcb-cursor.so*')
]

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=xcb_cursor_binaries,
    datas=[
        (str(ROOT / 'pyproject.toml'), '.'),
        (str(ROOT / 'assets'), 'assets'),
    ],
    hiddenimports=['scripts.smoke_mpv'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['vlc', 'mpv'],
    noarchive=False,
)

# Append after Analysis so the prepared dependency closure is not reclassified,
# scanned against runner libraries, or rewritten. Executable DATA retains its
# execute bit when PyInstaller extracts the onefile archive.
native_mpv = Tree(str(NATIVE_MPV), prefix='native/mpv', typecode='DATA')
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    native_mpv,
    [],
    name='qitv',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
)
