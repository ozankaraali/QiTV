import site
import os

PACKAGES_PATH = site.getsitepackages()[0]
VLC_PATH = '/Applications/VLC.app/Contents/MacOS/lib'  # Adjust this path if necessary

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        (os.path.join(VLC_PATH, 'libvlc.dylib'), '.'),
        (os.path.join(VLC_PATH, 'libvlccore.dylib'), '.')
     ],
    datas=[
        ('assets/qitv.icns', 'assets/qitv.icns')
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
    icon='assets/qitv.icns'
)
app = BUNDLE(
    exe,
    name='qitv.app',
    icon='assets/qitv.icns',
    bundle_identifier=None,
    info_plist={
        'CFBundleName': 'qitv',
        'CFBundleDisplayName': 'qitv',
        'CFBundleIdentifier': 'com.ozankaraali.qitv',
        'CFBundleVersion': '1.0',
        'CFBundleExecutable': 'qitv',
        'CFBundleIconFile': 'qitv.icns',
        'NSPrincipalClass': 'NSApplication',
        'LSUIElement': True,
    },
    version='0.0.1'
)
