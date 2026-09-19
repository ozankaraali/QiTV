# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path
import tomllib

from PyInstaller.building.datastruct import Tree

ROOT = Path(SPECPATH)
with (ROOT / 'pyproject.toml').open('rb') as file:
    APP_VERSION = tomllib.load(file)['project']['version']
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

# Preserve the prepared MPV closure, install names and signatures. BUNDLE places
# this complete data tree in Resources and cross-links it from sys._MEIPASS.
# Native executable DATA keeps its execute bit; Analysis must not rewrite it.
native_mpv = Tree(str(NATIVE_MPV), prefix='native/mpv', typecode='DATA')
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
    icon=str(ROOT / 'assets' / 'qitv.icns'),
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    native_mpv,
    strip=False,
    upx=False,
    name='QiTV',
)
app = BUNDLE(
    collection,
    name='QiTV.app',
    icon=str(ROOT / 'assets' / 'qitv.icns'),
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
    },
)
