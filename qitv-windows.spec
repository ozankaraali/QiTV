# -*- mode: python ; coding: utf-8 -*-

VLC_PATH = 'C:\\Program Files\\VideoLAN\\VLC'

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'], #insert your base VLC path here, ex: pathex=["D:\KivySchool\VLC"],
    binaries=[
        (os.path.join(VLC_PATH, 'plugins/*'), 'plugins'),
        (os.path.join(VLC_PATH, 'libvlc.dll'), '.'),
        (os.path.join(VLC_PATH, 'libvlccore.dll'), '.'),
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
    icon='assets/qitv.ico'
)
