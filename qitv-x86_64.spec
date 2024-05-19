import site

# Determine path to site-packages
PACKAGES_PATH = site.getsitepackages()[0]
block_cipher = None
# Add paths
a = Analysis(
    ['main.py'],
    binaries=[],
    pathex=[],
    datas=[
        # Add necessary resources here
        (f"{PACKAGES_PATH}/python_vlc", "python_vlc"),
        ('assets/qitv.icns', 'assets/qitv.icns')
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='qitv',
          debug=False,
          strip=False,
          upx=True,
          runtime_tmpdir=None,
          console=False,
          icon='assets/qitv.icns')
app = BUNDLE(exe,
             name='qitv.app',
             icon='assets/qitv.icns',
             bundle_identifier=None,
             info_plist={
                'NSPrincipalClass': 'NSApplication',
                'NSAppleScriptEnabled': False,
                'NSMicrophoneUsageDescription': 'This app requires access to the microphone for audio processing.',
                'NSCameraUsageDescription': 'This app requires access to the camera for hand gesture detection.',
                'NSAccessibilityUsageDescription': 'This app requires accessibility permissions to control the mouse using hand gestures.'
             },
             version='0.0.1')
