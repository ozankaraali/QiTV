import site
import os

PACKAGES_PATH = site.getsitepackages()[0]
VLC_PATH = 'C:\\Program Files\\VideoLAN\\VLC'  # Adjust this path if necessary

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        (os.path.join(VLC_PATH, 'libvlc.dll'), '.'),
        (os.path.join(VLC_PATH, 'libvlccore.dll'), '.'),
        (os.path.join(VLC_PATH, 'axvlc.dll'), '.'),
        (os.path.join(VLC_PATH, 'npvlc.dll'), '.'),
    ],
    datas=[
        ('assets/qitv.ico', 'assets/qitv.ico')
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='qitv.exe',
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon='assets/qitv.ico'
)
