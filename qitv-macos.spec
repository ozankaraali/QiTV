# -*- mode: python ; coding: utf-8 -*-

VLC_PATH = '/Applications/VLC.app/Contents/MacOS'  # Adjust this path if necessary

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[
        (os.path.join(VLC_PATH, 'plugins/*'), 'plugins'),
        (os.path.join(VLC_PATH, 'lib/*'), 'lib'),
    ],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    #https://github.com/pyinstaller/pyinstaller/issues/7851#issuecomment-1677986648
    module_collection_mode={
        'vlc': 'py',
    }
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries + [
        ("libvlc.dylib", os.path.join(VLC_PATH, 'MacOS/lib/libvlc.dylib'), "BINARY"),
        ("libvlccore.dylib", os.path.join(VLC_PATH, 'MacOS/lib/libvlccore.dylib'), "BINARY")
        ],
    #a.binaries,
    a.datas,
    [],
    name='QiTV',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='assets/qitv.icns',
)
app = BUNDLE(
    exe,
    name='QiTV.app',
    icon='assets/qitv.icns',
    bundle_identifier='com.ozankaraali.QiTV',
    version='1.1.2',
    info_plist={
        'CFBundleDisplayName': 'QiTV',
        'CFBundleExecutable': 'QiTV',
        'CFBundleIdentifier': 'com.ozankaraali.QiTV',
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'QiTV',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'LSApplicationCategoryType': 'public.app-category.video',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
    }
)