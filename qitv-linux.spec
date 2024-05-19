import site
import os

PACKAGES_PATH = site.getsitepackages()[0]
# Attempt to locate VLC library files in common directories
VLC_PATHS = [
    '/usr/lib/x86_64-linux-gnu',
    '/usr/lib',  # Try this common path as a fallback
    '/usr/lib64' # Another common path on some distributions
]
found_vlc_paths = []

# Check if the VLC libraries exist in any of the specified paths
for path in VLC_PATHS:
    if os.path.exists(os.path.join(path, 'libvlc.so')):
        found_vlc_paths.append((os.path.join(path, 'libvlc.so'), '.'))
    if os.path.exists(os.path.join(path, 'libvlccore.so')):
        found_vlc_paths.append((os.path.join(path, 'libvlccore.so'), '.'))

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=found_vlc_paths,
    datas=[
        ('assets/qitv.png', 'assets/qitv.png')
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
    name='qitv',
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon='assets/qitv.png'
)
